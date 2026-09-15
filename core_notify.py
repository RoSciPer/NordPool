"""Telegram notifications for schedule cost quality."""

from __future__ import annotations

import json
import logging
import os
import statistics
import urllib.request
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from core_prices import LOCAL_TZ
from core_planner import schedule_for_date
from core_storage import DeviceProfile

logger = logging.getLogger(__name__)

FIXED_EUR_KWH = 0.065


def _send_telegram(chat_id: int, text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — skip notify")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception:
        logger.exception("Telegram notify failed for chat %s", chat_id)
        return False


def _classify_window(avg_spot: float, day_median: float) -> str:
    if day_median <= 0:
        return "vidējs"
    if avg_spot <= day_median * 0.85:
        return "lēts"
    if avg_spot >= day_median * 1.15:
        return "dārgs"
    return "vidējs"


def analyze_schedule_costs(
    profile: DeviceProfile,
    prices: Dict[datetime, float],
    target_date: date,
) -> dict:
    sched = schedule_for_date(profile, prices, target_date)
    day_vals = [p for dt, p in prices.items() if dt.date() == target_date]
    day_median = statistics.median(day_vals) if day_vals else 0.0

    windows: List[dict] = []
    for r in sched.to_ranges():
        s = datetime.fromtimestamp(r["start"], LOCAL_TZ)
        e = datetime.fromtimestamp(r["end"], LOCAL_TZ)
        slot_prices = [prices[dt] for dt in prices if s <= dt < e]
        if not slot_prices:
            continue
        avg_spot = sum(slot_prices) / len(slot_prices)
        total = avg_spot + FIXED_EUR_KWH
        verdict = _classify_window(avg_spot, day_median)
        windows.append({
            "label": f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}",
            "avg_spot": round(avg_spot, 4),
            "total": round(total, 4),
            "verdict": verdict,
        })

    verdicts = [w["verdict"] for w in windows]
    if not verdicts:
        overall = "nav"
    elif all(v == "dārgs" for v in verdicts):
        overall = "dārgs"
    elif any(v == "dārgs" for v in verdicts):
        overall = "daļēji_dārgs"
    elif all(v == "lēts" for v in verdicts):
        overall = "lēts"
    else:
        overall = "ok"

    return {
        "target_date": target_date.isoformat(),
        "day_median": round(day_median, 4),
        "windows": windows,
        "overall": overall,
    }


def format_schedule_notify_message(device_name: str, analysis: dict) -> str:
    d = analysis["target_date"]
    lines = [f"📅 *{device_name}* — grafiks ielādēts (`{d}`)"]
    for w in analysis["windows"]:
        icon = {"lēts": "🟢", "vidējs": "🟡", "dārgs": "🔴"}.get(w["verdict"], "⚪")
        lines.append(
            f"{icon} {w['label']}: spot *{w['avg_spot']:.3f}* + fiks. = *{w['total']:.3f}* €/kWh ({w['verdict']})"
        )
    lines.append(f"_Dienas mediāna: {analysis['day_median']:.3f} €/kWh_")

    overall = analysis["overall"]
    if overall == "dārgs":
        lines.append("\n⚠️ *Abi logi ir dārgi* — apsver manuālu kontroli.")
    elif overall == "daļēji_dārgs":
        lines.append("\n⚠️ *Daļēji dārgs logs* — viens lētāks, otrs dārgāks.")
    elif overall == "lēts":
        lines.append("\n✅ Abi logi relatīvi lēti.")
    return "\n".join(lines)


def notify_schedule_installed(
    *,
    chat_id: int,
    device_name: str,
    profile: DeviceProfile,
    prices: Dict[datetime, float],
    target_date: Optional[date] = None,
    is_fallback: bool = False,
) -> None:
    if is_fallback:
        text = (
            f"⚠️ *{device_name}* — ielādēts *rezerves* grafiks (nedēļas atkārtojums).\n"
            "Rītdienas cenas nebija gatavas vai sync neizdevās.\n"
            "Laiki nav pēc Nord Pool — bet boileris strādās."
        )
        _send_telegram(chat_id, text)
        return
    target = target_date or (datetime.now(LOCAL_TZ).date() + timedelta(days=1))
    analysis = analyze_schedule_costs(profile, prices, target)
    if not analysis["windows"]:
        return
    text = format_schedule_notify_message(device_name, analysis)
    _send_telegram(chat_id, text)
