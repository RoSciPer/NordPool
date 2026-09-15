"""
Device registry + per-device heating profile storage.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

DAY_NAMES = ["pirmd.", "otrd.", "trešd.", "ceturtd.", "piektd.", "sestd.", "svētd."]
WORKDAYS = [0, 1, 2, 3, 4]
WEEKEND = [5, 6]
ALL_DAYS = list(range(7))


@dataclass
class BoilerConfig:
    # Workday Morning Block (~2–2.5 h contiguous, done before shower)
    weekday_morning_slots: int = 9
    weekday_morning_search_from: str = "03:00"
    weekday_morning_ready_by: str = "07:00"

    # Workday Evening Block (done by ~19:00, usually off ~17:00)
    weekday_evening_slots: int = 9
    weekday_evening_search_from: str = "10:00"
    weekday_evening_ready_by: str = "19:00"

    # Weekend Morning Block
    weekend_morning_slots: int = 9
    weekend_morning_search_from: str = "03:00"
    weekend_morning_ready_by: str = "09:00"

    # Weekend Daytime Block
    weekend_daytime_slots: int = 9
    weekend_daytime_search_from: str = "10:00"
    weekend_daytime_ready_by: str = "19:00"
    
    # Weekend Peak Cut
    weekend_peak_cut_eur_kwh: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BoilerConfig":
        return cls(
            weekday_morning_slots=data.get("weekday_morning_slots", 9),
            weekday_morning_search_from=data.get("weekday_morning_search_from", "03:00"),
            weekday_morning_ready_by=data.get("weekday_morning_ready_by", "07:00"),
            weekday_evening_slots=data.get("weekday_evening_slots", 9),
            weekday_evening_search_from=data.get("weekday_evening_search_from", "10:00"),
            weekday_evening_ready_by=data.get("weekday_evening_ready_by", "19:00"),
            weekend_morning_slots=data.get("weekend_morning_slots", 9),
            weekend_morning_search_from=data.get("weekend_morning_search_from", "03:00"),
            weekend_morning_ready_by=data.get("weekend_morning_ready_by", "09:00"),
            weekend_daytime_slots=data.get("weekend_daytime_slots", 9),
            weekend_daytime_search_from=data.get("weekend_daytime_search_from", "10:00"),
            weekend_daytime_ready_by=data.get("weekend_daytime_ready_by", "19:00"),
            weekend_peak_cut_eur_kwh=data.get("weekend_peak_cut_eur_kwh"),
        )


@dataclass
class DeviceProfile:
    boiler_config: BoilerConfig = field(default_factory=BoilerConfig)
    vacation_mode: bool = False
    holiday_as: str = "weekend"
    timezone: str = "Europe/Riga"
    fallback_hours: List[int] = field(default_factory=lambda: [2, 3, 4, 5])
    weekend_friday: bool = False
    active_profile: str = "boiler"

    def to_dict(self) -> dict:
        return {
            "boiler_config": self.boiler_config.to_dict(),
            "vacation_mode": self.vacation_mode,
            "holiday_as": self.holiday_as,
            "timezone": self.timezone,
            "fallback_hours": self.fallback_hours,
            "weekend_friday": self.weekend_friday,
            "active_profile": self.active_profile,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DeviceProfile":
        return cls(
            boiler_config=BoilerConfig.from_dict(data.get("boiler_config", {})),
            vacation_mode=bool(data.get("vacation_mode", False)),
            holiday_as=data.get("holiday_as", "weekend"),
            timezone=data.get("timezone", "Europe/Riga"),
            fallback_hours=list(data.get("fallback_hours", [2, 3, 4, 5])),
            weekend_friday=bool(data.get("weekend_friday", False)),
            active_profile=data.get("active_profile", "boiler"),
        )

    def set_block_slots(self, block: str, slots_delta: int) -> None:
        if block == "wd_m": self.boiler_config.weekday_morning_slots = max(0, self.boiler_config.weekday_morning_slots + slots_delta)
        elif block == "wd_e": self.boiler_config.weekday_evening_slots = max(0, self.boiler_config.weekday_evening_slots + slots_delta)
        elif block == "we_m": self.boiler_config.weekend_morning_slots = max(0, self.boiler_config.weekend_morning_slots + slots_delta)
        elif block == "we_d": self.boiler_config.weekend_daytime_slots = max(0, self.boiler_config.weekend_daytime_slots + slots_delta)

    def set_profile_preset(self, preset: str) -> None:
        self.active_profile = preset
        if preset == "boiler":
            self.boiler_config = BoilerConfig()
        elif preset == "heat":
            self.boiler_config = BoilerConfig(
                weekday_morning_slots=16,
                weekday_evening_slots=0,
                weekend_morning_slots=16,
                weekend_daytime_slots=0
            )
        elif preset == "generic":
            self.boiler_config = BoilerConfig(
                weekday_morning_slots=8,
                weekday_evening_slots=8,
                weekend_morning_slots=8,
                weekend_daytime_slots=8
            )

    def set_weekend_friday(self, enabled: bool) -> None:
        self.weekend_friday = enabled
        self.holiday_as = "weekend" if enabled else "workday"

    @classmethod
    def default_boiler(cls) -> "DeviceProfile":
        return cls(boiler_config=BoilerConfig())


@dataclass
class Device:
    token: str
    chat_id: int
    device_name: str
    created_at: datetime
    last_seen: Optional[datetime] = None
    last_ip: Optional[str] = None
    firmware: Optional[str] = None


def generate_token() -> str:
    return secrets.token_urlsafe(24)


class DeviceStore:
    def __init__(self, db_path: str | Path = "prices.db"):
        self.db_path = Path(db_path)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    token TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    device_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen TEXT,
                    last_ip TEXT,
                    firmware TEXT,
                    refresh_requested_at TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_chat ON devices(chat_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS device_profiles (
                    token TEXT PRIMARY KEY,
                    profile_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (token) REFERENCES devices(token) ON DELETE CASCADE
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schedule_reports (
                    token TEXT PRIMARY KEY,
                    reported_at TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    FOREIGN KEY (token) REFERENCES devices(token) ON DELETE CASCADE
                )
                """
            )

    def save_schedule_report(self, token: str, report: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO schedule_reports (token, reported_at, report_json)
                VALUES (?, ?, ?)
                ON CONFLICT(token) DO UPDATE SET
                    reported_at = excluded.reported_at,
                    report_json = excluded.report_json
                """,
                (token, now, json.dumps(report)),
            )
        self.clear_schedule_refresh(token)

    def get_schedule_report(self, token: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT reported_at, report_json FROM schedule_reports WHERE token = ?",
                (token,),
            ).fetchone()
        if not row:
            return None
        return {
            "reported_at": row[0],
            "report": json.loads(row[1]),
        }

    def create_device(self, chat_id: int, device_name: str,
                       profile: Optional[DeviceProfile] = None) -> Device:
        token = generate_token()
        now = datetime.now(timezone.utc)
        profile = profile or DeviceProfile.default_boiler()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO devices (token, chat_id, device_name, created_at) VALUES (?, ?, ?, ?)",
                (token, chat_id, device_name, now.isoformat()),
            )
            conn.execute(
                "INSERT INTO device_profiles (token, profile_json, updated_at) VALUES (?, ?, ?)",
                (token, json.dumps(profile.to_dict()), now.isoformat()),
            )
        return Device(token=token, chat_id=chat_id, device_name=device_name, created_at=now)

    def list_devices(self, chat_id: int) -> List[Device]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM devices WHERE chat_id = ? ORDER BY created_at", (chat_id,)).fetchall()
        return [self._row_to_device(r) for r in rows]

    def get_device(self, token: str) -> Optional[Device]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM devices WHERE token = ?", (token,)).fetchone()
        return self._row_to_device(row) if row else None

    def delete_device(self, token: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM devices WHERE token = ?", (token,))
        return cur.rowcount > 0

    def request_schedule_refresh(self, token: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("UPDATE devices SET refresh_requested_at = ? WHERE token = ?",
                               (datetime.now(timezone.utc).isoformat(), token))
        return cur.rowcount > 0

    def clear_schedule_refresh(self, token: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE devices SET refresh_requested_at = NULL WHERE token = ?", (token,))

    def is_refresh_pending(self, token: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT refresh_requested_at FROM devices WHERE token = ?", (token,)).fetchone()
        return bool(row and row[0])

    def touch_device(self, token: str, ip: Optional[str] = None, firmware: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE devices SET last_seen = ?, last_ip = COALESCE(?, last_ip), firmware = COALESCE(?, firmware) WHERE token = ?",
                (datetime.now(timezone.utc).isoformat(), ip, firmware, token),
            )

    def get_profile(self, token: str) -> Optional[DeviceProfile]:
        with self._conn() as conn:
            row = conn.execute("SELECT profile_json FROM device_profiles WHERE token = ?", (token,)).fetchone()
        if not row: return None
        return DeviceProfile.from_dict(json.loads(row[0]))

    def save_profile(self, token: str, profile: DeviceProfile) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO device_profiles (token, profile_json, updated_at) VALUES (?, ?, ?) ON CONFLICT(token) DO UPDATE SET profile_json=excluded.profile_json, updated_at=excluded.updated_at",
                (token, json.dumps(profile.to_dict()), now),
            )

    @staticmethod
    def _row_to_device(row: sqlite3.Row) -> Device:
        def parse_dt(value):
            if not value: return None
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return Device(
            token=row["token"], chat_id=row["chat_id"], device_name=row["device_name"],
            created_at=parse_dt(row["created_at"]), last_seen=parse_dt(row["last_seen"]),
            last_ip=row["last_ip"], firmware=row["firmware"]
        )

    def all_devices(self) -> Iterable[Device]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM devices").fetchall()
        return [self._row_to_device(r) for r in rows]

    def is_device_online(self, token: str, threshold_sec: int = 3600) -> bool:
        device = self.get_device(token)
        if not device or not device.last_seen: return False
        return (datetime.now(timezone.utc) - device.last_seen).total_seconds() < threshold_sec
