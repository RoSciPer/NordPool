"""
Public FastAPI service for Shelly devices.

Endpoints:
  GET  /api/v1/prices                       — Latvia 15-min prices (today + tomorrow if known)
  GET  /api/v1/schedule?token=...           — Pre-computed ON/OFF schedule for the device
  GET  /api/v1/refresh_pending?token=...    — Shelly polls whether to pull schedule now
  POST /api/v1/heartbeat?token=...          — Shelly check-in (updates last_seen)
  GET  /api/v1/health                       — Liveness probe
  GET  /                                    — Static landing page (instructions)

Design notes:
- Prices are refreshed in a background asyncio task. The 15:30 daily refresh
  is the same logic used by the Telegram bot — kept in core_prices.
- Schedules are computed on demand from cached prices + the device's profile.
  No mutable per-device state lives in memory: any process can serve any token.
- Shelly fetches via plain HTTPS GET; we keep the response very compact
  (numeric epoch timestamps + 0/1 flags) to fit comfortably in the Shelly
  script's memory budget.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Body
from fastapi.responses import JSONResponse, PlainTextResponse

from core_prices import PriceFetcher, PriceStore, LOCAL_TZ, day_complete
from core_planner import (
    plan_for_device,
    schedule_for_date,
    fallback_recurring_schedules,
    ranges_to_daily_recurring_schedules,
)
from core_notify import notify_schedule_installed
from core_storage import DeviceStore

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO"),
)

DB_PATH = os.environ.get("NORDPOOL_DB", "prices.db")
REFRESH_INTERVAL_SEC = int(os.environ.get("REFRESH_INTERVAL_SEC", "1800"))  # 30 min


# ---------------------------------------------------------------------------
# Background refresher
# ---------------------------------------------------------------------------

async def _refresher_loop(fetcher: PriceFetcher):
    """Run ensure_fresh() roughly every REFRESH_INTERVAL_SEC.

    `ensure_fresh` is cheap when nothing needs doing: it just consults the
    in-DB sync state. It triggers a real HTTP request only when it's past
    15:30 and tomorrow's prices are missing, or hourly retry while missing.
    """
    while True:
        try:
            await asyncio.to_thread(fetcher.ensure_fresh)
        except Exception:
            logger.exception("Price refresher failed (will retry).")
        await asyncio.sleep(REFRESH_INTERVAL_SEC)


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = PriceStore(DB_PATH)
    devices = DeviceStore(DB_PATH)
    fetcher = PriceFetcher(store)

    # Try one fetch up-front so /prices is useful immediately after boot.
    try:
        await asyncio.to_thread(fetcher.ensure_fresh)
    except Exception:
        logger.exception("Initial price fetch failed (will retry in background).")

    app.state.store = store
    app.state.devices = devices
    app.state.fetcher = fetcher
    task = asyncio.create_task(_refresher_loop(fetcher))
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(
    title="Nord Pool Shelly Schedule API",
    description=(
        "Publiska Latvijas Nord Pool cenu un Shelly grafiku API. "
        "Lieto ar Shelly Plus/Pro skriptu, kas tiek konfigurēts caur Telegram botu."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=PlainTextResponse, include_in_schema=False)
def landing():
    return (
        "Nord Pool Shelly Schedule API\n"
        "-----------------------------\n"
        "Endpoint-i:\n"
        "  GET  /api/v1/health\n"
        "  GET  /api/v1/prices\n"
        "  GET  /api/v1/schedule?token=...\n"
        "  GET  /api/v1/boiler/schedule?token=...\n"
        "  GET  /api/v1/plan_data?token=...\n"
        "  POST /api/v1/heartbeat?token=...\n"
        "\n"
        "Konfigurē savu Shelly ierīci caur Telegram botu (sk. README.md).\n"
    )


@app.get("/api/v1/health")
def health(request: Request):
    store: PriceStore = request.app.state.store
    sync = store.get_sync_state()
    prices = store.load()
    now_local = datetime.now(LOCAL_TZ)
    today = now_local.date()
    return {
        "status": "ok",
        "server_time": now_local.isoformat(),
        "prices_cached": len(prices),
        "today_slots": sum(1 for dt in prices if dt.date() == today),
        "tomorrow_loaded": sync["next_day_loaded"],
        "last_checked": sync["last_checked"].isoformat() if sync["last_checked"] else None,
    }


@app.get("/api/v1/prices")
def prices_endpoint(request: Request):
    """Return all cached 15-min prices, compact format.

    Response:
      {
        "currency": "EUR",
        "unit": "EUR/kWh",
        "timezone": "Europe/Riga",
        "generated_at": 1747000000,
        "prices": [[unix_ts, eur_per_kwh], ...]
      }
    """
    store: PriceStore = request.app.state.store
    prices = store.load()
    items = sorted(prices.items())
    return {
        "currency": "EUR",
        "unit": "EUR/kWh",
        "timezone": "Europe/Riga",
        "generated_at": int(datetime.now(timezone.utc).timestamp()),
        "prices": [[int(dt.timestamp()), round(p, 5)] for dt, p in items],
    }


@app.get("/api/v1/schedule")
def schedule_endpoint(
    request: Request,
    token: str = Query(..., min_length=8, max_length=128),
):
    """Compute and return the device's ON/OFF schedule."""
    devices: DeviceStore = request.app.state.devices
    store: PriceStore = request.app.state.store

    device = devices.get_device(token)
    if not device:
        raise HTTPException(status_code=404, detail="Unknown token")

    profile = devices.get_profile(token)
    if not profile:
        raise HTTPException(status_code=500, detail="Profile missing for device")

    prices = store.load()
    if not prices:
        # Don't 500 — let device keep its last schedule. Return empty events.
        sched = plan_for_device(profile, prices)
    else:
        sched = plan_for_device(profile, prices)

    # Touch device last_seen + client IP
    client_ip = request.client.host if request.client else None
    devices.touch_device(token, ip=client_ip)
    devices.clear_schedule_refresh(token)

    return {
        "token_hint": token[:6] + "…",
        "device_name": device.device_name,
        "generated_at": int(sched.generated_at.timestamp()),
        "valid_until": int(sched.valid_until.timestamp()),
        "vacation_mode": profile.vacation_mode,
        "fallback_hours": profile.fallback_hours,
        "events": sched.to_events(),
        "notes": sched.notes,
    }


# ---------------------------------------------------------------------------
# Shelly install payload helper
# ---------------------------------------------------------------------------

def _build_install_payload(
    devices: DeviceStore,
    store: PriceStore,
    token: str,
    switch_id: int = 0,
    *,
    scope: str = "tomorrow",
) -> dict:
    device = devices.get_device(token)
    if not device:
        raise HTTPException(status_code=404, detail="Unknown token")

    profile = devices.get_profile(token)
    if not profile:
        raise HTTPException(status_code=500, detail="Profile missing")

    now_local = datetime.now(LOCAL_TZ)
    today = now_local.date()
    tomorrow = today + timedelta(days=1)
    now_ts = int(now_local.timestamp())
    prices = store.load()
    tomorrow_ready = day_complete(prices, tomorrow)

    targets: list[tuple[date, bool]] = []
    if scope == "tomorrow":
        targets = [(tomorrow, tomorrow_ready)]
    elif scope == "today":
        targets = [(today, True)]
    elif scope == "all":
        targets = [(today, True)]
        if tomorrow_ready:
            targets.append((tomorrow, True))

    all_ranges: list = []
    replace_dates: list[str] = []

    range_dicts: list = []
    if not profile.vacation_mode:
        for target, ready_flag in targets:
            if not ready_flag:
                continue
            day_sched = schedule_for_date(profile, prices, target)
            day_ranges = [r for r in day_sched.to_ranges() if r["end"] > now_ts]
            if not day_ranges:
                continue
            replace_dates.append(target.isoformat())
            for r in day_ranges:
                s = datetime.fromtimestamp(r["start"], LOCAL_TZ)
                e = datetime.fromtimestamp(r["end"], LOCAL_TZ)
                range_dicts.append(r)
                all_ranges.append({
                    "start": r["start"],
                    "end": r["end"],
                    "date": target.isoformat(),
                    "label": f"{target.strftime('%d.%m')} {s.strftime('%H:%M')}–{e.strftime('%H:%M')}",
                })

    # Daily recurring (* * *) — laiki no rītdienas plāna, bet bez konkrēta datuma.
    # Ja slēdzis offline, vecie laiki turpina strādāt.
    schedules = ranges_to_daily_recurring_schedules(range_dicts, switch_id=switch_id)

    fallback = fallback_recurring_schedules(profile, switch_id=switch_id)

    return {
        "device_name": device.device_name,
        "tomorrow_ready": tomorrow_ready,
        "target_date": tomorrow.isoformat(),
        "scope": scope,
        "replace_dates": replace_dates,
        "vacation_mode": profile.vacation_mode,
        "ranges": all_ranges,
        "schedules": schedules,
        "fallback_schedules": fallback,
        "schedule_kind": "daily" if schedules else ("fallback" if fallback else "none"),
    }


@app.get("/api/v1/install_schedules")
def install_schedules_endpoint(
    request: Request,
    token: str = Query(..., min_length=8, max_length=128),
    id: int = Query(0, alias="id"),
    scope: str = Query("tomorrow", pattern="^(tomorrow|today|all)$"),
):
    """Viegla atbilde Shelly ierīcei — tikai gatavie Schedule.Create ieraksti.

    scope=tomorrow (noklusējums) — neaiztiek šodienas grafikus.
    scope=all — manuālam testam (aizstāj arī šodienu).
    """
    devices: DeviceStore = request.app.state.devices
    store: PriceStore = request.app.state.store
    payload = _build_install_payload(devices, store, token, switch_id=id, scope=scope)

    client_ip = request.client.host if request.client else None
    devices.touch_device(token, ip=client_ip)
    return payload


@app.get("/api/v1/plan_data")
def plan_data_endpoint(
    request: Request,
    token: str = Query(..., min_length=8, max_length=128),
    id: int = Query(0, alias="id"),
):
    """Prices + config for on-device adaptive schedule planning (Shelly script).

    Shelly reads tomorrow's 15-min prices and builds two contiguous heating
    windows locally. If tomorrow_ready is false, the script must keep existing
    schedules and retry later.
    """
    devices: DeviceStore = request.app.state.devices
    store: PriceStore = request.app.state.store

    device = devices.get_device(token)
    if not device:
        raise HTTPException(status_code=404, detail="Unknown token")

    profile = devices.get_profile(token)
    if not profile:
        raise HTTPException(status_code=500, detail="Profile missing")

    now_local = datetime.now(LOCAL_TZ)
    tomorrow = now_local.date() + timedelta(days=1)
    prices = store.load()
    tomorrow_prices = sorted(
        (dt, p) for dt, p in prices.items() if dt.date() == tomorrow
    )
    tomorrow_ready = day_complete(prices, tomorrow)
    is_weekend = tomorrow.weekday() >= 5
    cfg = profile.boiler_config

    if is_weekend:
        plan_cfg = {
            "morning_slots": cfg.weekend_morning_slots,
            "morning_search_from": cfg.weekend_morning_search_from,
            "morning_ready_by": cfg.weekend_morning_ready_by,
            "evening_slots": cfg.weekend_daytime_slots,
            "evening_search_from": cfg.weekend_daytime_search_from,
            "evening_ready_by": cfg.weekend_daytime_ready_by,
            "peak_cut_eur_kwh": cfg.weekend_peak_cut_eur_kwh,
        }
    else:
        plan_cfg = {
            "morning_slots": cfg.weekday_morning_slots,
            "morning_search_from": cfg.weekday_morning_search_from,
            "morning_ready_by": cfg.weekday_morning_ready_by,
            "evening_slots": cfg.weekday_evening_slots,
            "evening_search_from": cfg.weekday_evening_search_from,
            "evening_ready_by": cfg.weekday_evening_ready_by,
            "peak_cut_eur_kwh": None,
        }
    plan_cfg["fixed_eur_kwh"] = 0.065

    tomorrow_sched = schedule_for_date(profile, prices, tomorrow)
    shelly_schedules = (
        tomorrow_sched.to_shelly_schedules(switch_id=id)
        if tomorrow_ready and not profile.vacation_mode
        else []
    )

    client_ip = request.client.host if request.client else None
    devices.touch_device(token, ip=client_ip)

    return {
        "device_name": device.device_name,
        "tomorrow_ready": tomorrow_ready,
        "target_date": tomorrow.isoformat(),
        "is_weekend": is_weekend,
        "vacation_mode": profile.vacation_mode,
        "prices": [[int(dt.timestamp()), round(p, 5)] for dt, p in tomorrow_prices],
        "config": plan_cfg,
        "ranges": tomorrow_sched.to_ranges(),
        "schedules": shelly_schedules,
    }


@app.post("/api/v1/trigger_sync")
def trigger_sync_endpoint(
    request: Request,
    token: str = Query(..., min_length=8, max_length=128),
    id: int = Query(0, alias="id"),
):
    """Manuāli pacelt Shelly sinhronizāciju un atgriezt rītdienas grafiku."""
    devices: DeviceStore = request.app.state.devices
    store: PriceStore = request.app.state.store

    payload = _build_install_payload(devices, store, token, switch_id=id)
    devices.request_schedule_refresh(token)

    return {
        "ok": True,
        **payload,
        "schedules_count": len(payload["schedules"]),
        "pull_schedule": True,
        "message": "Shelly: ilgi nospied ierīces pogu VAI Script Stop→Start.",
    }


@app.get("/api/v1/boiler/schedule")
def boiler_schedule_endpoint(
    request: Request,
    token: str = Query(..., min_length=8, max_length=128),
    id: int = Query(0, alias="id"),
):
    """Return Shelly Schedule.Create RPC calls data for the device."""
    devices: DeviceStore = request.app.state.devices
    store: PriceStore = request.app.state.store

    device = devices.get_device(token)
    if not device:
        raise HTTPException(status_code=404, detail="Unknown token")

    profile = devices.get_profile(token)
    if not profile:
        raise HTTPException(status_code=500, detail="Profile missing")

    prices = store.load()
    sched = plan_for_device(profile, prices, horizon_days=2)

    client_ip = request.client.host if request.client else None
    devices.touch_device(token, ip=client_ip)
    devices.clear_schedule_refresh(token)

    return {
        "device_name": device.device_name,
        "generated_at": int(sched.generated_at.timestamp()),
        "valid_until": int(sched.valid_until.timestamp()),
        "schedules": sched.to_shelly_schedules(switch_id=id),
    }



@app.get("/api/v1/schedule_for_shelly")
def schedule_for_shelly_endpoint(
    request: Request,
    token: str = Query(..., min_length=8, max_length=128),
):
    devices: DeviceStore = request.app.state.devices
    store: PriceStore = request.app.state.store

    device = devices.get_device(token)
    if not device:
        raise HTTPException(status_code=404, detail="Unknown token")

    profile = devices.get_profile(token)
    if not profile:
        raise HTTPException(status_code=500, detail="Profile missing for device")

    prices = store.load()
    sched = plan_for_device(profile, prices)

    client_ip = request.client.host if request.client else None
    devices.touch_device(token, ip=client_ip)
    devices.clear_schedule_refresh(token)

    return {
        "token_hint": token[:6] + "…",
        "device_name": device.device_name,
        "generated_at": int(sched.generated_at.timestamp()),
        "valid_until": int(sched.valid_until.timestamp()),
        "vacation_mode": profile.vacation_mode,
        "fallback_hours": profile.fallback_hours,
        "ranges": sched.to_ranges(),
        "events": sched.to_events(),
        "notes": sched.notes,
    }


@app.post("/api/v1/report_schedule")
def report_schedule_endpoint(
    request: Request,
    token: str = Query(..., min_length=8, max_length=128),
    payload: dict = Body(...),
):
    devices: DeviceStore = request.app.state.devices
    store: PriceStore = request.app.state.store
    device = devices.get_device(token)
    if not device:
        raise HTTPException(status_code=404, detail="Unknown token")

    ok = int(payload.get("ok_count") or 0)
    total = int(payload.get("total") or 0)
    is_fallback = payload.get("note") == "fallback" or payload.get("schedule_kind") == "fallback"
    target_s = payload.get("target_date")
    kind = "fallback" if is_fallback else "daily"

    prev = devices.get_schedule_report(token)
    already_notified = False
    if prev:
        pr = prev.get("report") or {}
        already_notified = (
            pr.get("notified")
            and pr.get("target_date") == target_s
            and pr.get("schedule_kind") == kind
        )

    out_payload = {**payload, "schedule_kind": kind}
    if already_notified:
        out_payload["notified"] = True
    devices.save_schedule_report(token, out_payload)

    if ok > 0 and ok >= total:
        profile = devices.get_profile(token)
        if profile and not profile.vacation_mode and not already_notified:
            target_d = date.fromisoformat(target_s) if target_s else None
            try:
                notify_schedule_installed(
                    chat_id=device.chat_id,
                    device_name=device.device_name,
                    profile=profile,
                    prices=store.load(),
                    target_date=target_d,
                    is_fallback=is_fallback,
                )
                devices.save_schedule_report(
                    token, {**out_payload, "notified": True}
                )
            except Exception:
                logger.exception("Schedule notify failed for %s", token[:8])

    return {"ok": True}


@app.get("/api/v1/device_schedule_status")
def device_schedule_status(
    request: Request,
    token: str = Query(..., min_length=8, max_length=128),
):
    devices: DeviceStore = request.app.state.devices
    device = devices.get_device(token)
    if not device:
        raise HTTPException(status_code=404, detail="Unknown token")
    report = devices.get_schedule_report(token)
    if not report:
        return {"has_report": False}
    return {
        "has_report": True,
        "reported_at": report["reported_at"],
        "report": report["report"],
    }


@app.get("/api/v1/refresh_pending")
def refresh_pending_endpoint(
    request: Request,
    token: str = Query(..., min_length=8, max_length=128),
):
    """Lightweight poll: Shelly checks ~1/min if Telegram requested a schedule refresh."""
    devices: DeviceStore = request.app.state.devices
    device = devices.get_device(token)
    if not device:
        raise HTTPException(status_code=404, detail="Unknown token")
    return {"pull_schedule": devices.is_refresh_pending(token)}


@app.get("/api/v1/boiler/status")
def boiler_status_endpoint(
    request: Request,
    token: str = Query(..., min_length=8, max_length=128),
):
    """Placeholder for fetching real schedule from Shelly via some mechanism.
    Since we can't easily reach Shelly behind NAT, we rely on Shelly reporting its status.
    Or we can return what we HAVE in the DB as the 'target' state.
    """
    devices: DeviceStore = request.app.state.devices
    device = devices.get_device(token)
    if not device:
        raise HTTPException(status_code=404, detail="Unknown token")
    
    profile = devices.get_profile(token)
    return {
        "online": devices.is_device_online(token), # Need to implement this helper
        "last_seen": device.last_seen.isoformat() if device.last_seen else None,
        "config": profile.to_dict() if profile else None
    }


@app.post("/api/v1/heartbeat")
def heartbeat_endpoint(
    request: Request,
    token: str = Query(..., min_length=8, max_length=128),
    fw: str | None = Query(None, max_length=80),
):
    """Lightweight check-in from the Shelly script.

    Used by the bot to display "online / offline since X" status.
    """
    devices: DeviceStore = request.app.state.devices
    device = devices.get_device(token)
    if not device:
        raise HTTPException(status_code=404, detail="Unknown token")
    client_ip = request.client.host if request.client else None
    devices.touch_device(token, ip=client_ip, firmware=fw)
    return {"ok": True, "server_time": int(datetime.now(timezone.utc).timestamp())}


def main():
    """Entrypoint for `python server.py` (dev). In production use uvicorn directly."""
    import uvicorn
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("server:app", host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
