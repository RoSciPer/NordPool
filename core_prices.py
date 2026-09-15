"""
Nord Pool / Elering price fetching and local cache.

This module is the single source of truth for electricity prices used by both
the Telegram bot and the FastAPI server. Prices are stored in SQLite (WAL mode)
so both processes can read concurrently without blocking each other.

Times are stored in UTC ISO-8601 in the DB; in-memory dicts use timezone-aware
datetimes in Europe/Riga, because the rest of the project reasons in local time
(15:30 refresh, schedule generation, etc.).
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Europe/Riga")
ELERING_URL = "https://dashboard.elering.ee/api/nps/price"

# Daily price publication: Nord Pool typically publishes "tomorrow" between
# ~13:00 and ~14:00 CET. Elering surfaces the LV slice shortly after.
# We refresh from 15:30 Riga time onwards and retry hourly if "tomorrow" is missing.
DAILY_REFRESH_HOUR = 15
DAILY_REFRESH_MINUTE = 30
RETRY_INTERVAL_MINUTES = 60
SLOTS_PER_DAY = 96  # 24 h × 4 (15-min)


def day_complete(prices: Dict[datetime, float], day: date) -> bool:
    """True when we have (nearly) all 15-min slots for a local calendar day."""
    count = sum(1 for dt in prices if dt.date() == day)
    if count >= SLOTS_PER_DAY - 4:
        return True
    end_slot = datetime.combine(day, dt_time(23, 45), tzinfo=LOCAL_TZ)
    return end_slot in prices


@dataclass(frozen=True)
class PricePoint:
    """A single 15-minute price observation."""
    start: datetime  # tz-aware, Europe/Riga
    price_eur_per_kwh: float


class PriceStore:
    """SQLite-backed cache of 15-minute prices.

    Designed to be safe for concurrent use by multiple processes via WAL mode.
    Each method opens its own short-lived connection (cheap, robust).
    """

    def __init__(self, db_path: str | Path = "prices.db"):
        self.db_path = Path(db_path)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS prices (
                    timestamp TEXT PRIMARY KEY,
                    price REAL NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_sync (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    next_day_loaded INTEGER NOT NULL DEFAULT 0,
                    last_checked TEXT
                )
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO price_sync (id, next_day_loaded, last_checked) "
                "VALUES (1, 0, NULL)"
            )
            try:
                conn.execute("ALTER TABLE price_sync ADD COLUMN last_notify_date TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists

    def save(self, prices: Dict[datetime, float]) -> None:
        """Replace cached prices with the given set. Times must be tz-aware."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute("DELETE FROM prices")
            conn.executemany(
                "INSERT INTO prices (timestamp, price, updated_at) VALUES (?, ?, ?)",
                [(dt.astimezone(timezone.utc).isoformat(), p, now_str) for dt, p in prices.items()],
            )
            conn.execute("UPDATE price_sync SET last_checked = ? WHERE id = 1", (now_str,))

    def load(self) -> Dict[datetime, float]:
        """Load all cached prices into a {tz-aware Riga datetime -> EUR/kWh} dict."""
        with self._conn() as conn:
            rows = conn.execute("SELECT timestamp, price FROM prices").fetchall()

        out: Dict[datetime, float] = {}
        for ts, price in rows:
            try:
                dt = datetime.fromisoformat(ts)
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out[dt.astimezone(LOCAL_TZ)] = float(price)
        return out

    def get_sync_state(self) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT next_day_loaded, last_checked FROM price_sync WHERE id = 1"
            ).fetchone()
        if not row:
            return {"next_day_loaded": False, "last_checked": None}
        last_checked = datetime.fromisoformat(row[1]) if row[1] else None
        return {"next_day_loaded": bool(row[0]), "last_checked": last_checked}

    def set_sync_state(self, *, next_day_loaded: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE price_sync SET next_day_loaded = ?, last_checked = ? WHERE id = 1",
                (1 if next_day_loaded else 0, datetime.now(timezone.utc).isoformat()),
            )

    def get_last_notify_date(self) -> Optional[str]:
        """ISO date (YYYY-MM-DD) for which we last sent tomorrow-schedule alerts."""
        with self._conn() as conn:
            try:
                row = conn.execute(
                    "SELECT last_notify_date FROM price_sync WHERE id = 1"
                ).fetchone()
            except sqlite3.OperationalError:
                return None
        return row[0] if row and row[0] else None

    def set_last_notify_date(self, target_date: str) -> None:
        with self._conn() as conn:
            try:
                conn.execute(
                    "UPDATE price_sync SET last_notify_date = ? WHERE id = 1",
                    (target_date,),
                )
            except sqlite3.OperationalError:
                pass


class PriceFetcher:
    """Fetches Latvia 15-minute prices from Elering and persists them via PriceStore."""

    def __init__(self, store: PriceStore, session: Optional[requests.Session] = None):
        self.store = store
        self.session = session or requests.Session()

    def _query_window(self) -> dict:
        """Today 00:00 → day-after-tomorrow 00:00 in UTC (covers tomorrow's prices)."""
        today_local = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = today_local + timedelta(days=2) - timedelta(seconds=1)
        return {
            "start": today_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def fetch_raw(self) -> dict:
        try:
            response = self.session.get(ELERING_URL, params=self._query_window(), timeout=15)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logger.error("Elering API fetch failed: %s", exc)
            return {"error": str(exc)}

    @staticmethod
    def parse(data: dict) -> Dict[datetime, float]:
        """Parse Elering payload → {Europe/Riga datetime -> EUR/kWh}."""
        if "error" in data:
            return {}
        lv = data.get("data", {}).get("lv", []) or []
        out: Dict[datetime, float] = {}
        for entry in lv:
            ts = entry.get("timestamp")
            price = entry.get("price")
            if ts is None or price is None:
                continue
            # Elering returns price in EUR/MWh → convert to EUR/kWh.
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(LOCAL_TZ)
            dt = dt.replace(second=0, microsecond=0)
            out[dt] = float(price) / 1000.0
        return out

    def refresh(self, *, mark_daily: bool = False) -> bool:
        """Fetch latest prices and save them. Returns True on success."""
        raw = self.fetch_raw()
        if "error" in raw:
            return False
        prices = self.parse(raw)
        if not prices:
            logger.warning("Elering returned no LV prices.")
            return False
        self.store.save(prices)
        if mark_daily:
            tomorrow = datetime.now(LOCAL_TZ).date() + timedelta(days=1)
            has_tomorrow = day_complete(prices, tomorrow)
            self.store.set_sync_state(next_day_loaded=has_tomorrow)
            logger.info("Daily refresh complete (tomorrow loaded: %s).", has_tomorrow)
        else:
            logger.info("Prices refreshed (%d slots).", len(prices))
        return True

    def ensure_fresh(self) -> bool:
        """Refresh if needed:
        - if cache is empty for today, fetch immediately
        - if it's past 15:30 local time and tomorrow is missing, fetch
        - retry hourly while tomorrow is missing
        Returns True if any successful fetch happened, False otherwise.
        """
        now = datetime.now(LOCAL_TZ)
        today = now.date()
        tomorrow = today + timedelta(days=1)
        cached = self.store.load()
        sync = self.store.get_sync_state()

        has_today = day_complete(cached, today)
        has_tomorrow = day_complete(cached, tomorrow)

        if not has_today:
            logger.info("Today's prices missing — fetching now.")
            return self.refresh(mark_daily=False)

        refresh_at = datetime.combine(
            today, dt_time(DAILY_REFRESH_HOUR, DAILY_REFRESH_MINUTE), tzinfo=LOCAL_TZ
        )
        past_refresh_time = now >= refresh_at
        last_checked = sync["last_checked"]
        retry_due = (
            not has_tomorrow
            and (last_checked is None
                 or (now - last_checked.astimezone(LOCAL_TZ)).total_seconds() >= RETRY_INTERVAL_MINUTES * 60)
        )
        should_daily = past_refresh_time and (not has_tomorrow or not sync["next_day_loaded"])

        if should_daily or retry_due:
            return self.refresh(mark_daily=True)
        return False


def to_15min_slot(dt: datetime) -> datetime:
    """Floor a datetime to its enclosing 15-minute slot."""
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def hourly_average(prices: Dict[datetime, float]) -> Dict[datetime, float]:
    """Aggregate 15-min prices to hourly averages keyed by the hour start."""
    buckets: Dict[datetime, list] = {}
    for dt, p in prices.items():
        key = dt.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(key, []).append(p)
    return {h: sum(vs) / len(vs) for h, vs in buckets.items()}


def filter_day(prices: Dict[datetime, float], day) -> Dict[datetime, float]:
    """Return only prices whose start falls on the given local date."""
    return {dt: p for dt, p in prices.items() if dt.date() == day}
