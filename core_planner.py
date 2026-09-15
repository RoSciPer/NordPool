"""
Schedule planner: turn (heating profile + 15-min prices) into an ON/OFF schedule.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
from typing import Dict, List, Optional, Set

from core_storage import DeviceProfile, BoilerConfig, WORKDAYS, WEEKEND
from core_prices import LOCAL_TZ, to_15min_slot

logger = logging.getLogger(__name__)

# Optional: holidays package
try:
    from holidays import country_holidays  # type: ignore
    _LV_HOLIDAYS = country_holidays("LV")
except Exception:
    _LV_HOLIDAYS = {}


@dataclass
class PlannedSchedule:
    """The plan we hand to the Shelly device."""
    generated_at: datetime
    valid_until: datetime
    on_slots: Set[datetime] = field(default_factory=set)  # 15-min starts
    coverage_by_window: Dict[str, List[datetime]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_events(self) -> List[tuple]:
        """Compress ON/OFF slots into transitions for legacy scripts."""
        if not self.on_slots:
            return []
        slots_sorted = sorted(self.on_slots)
        events: List[tuple] = []
        prev_end: Optional[datetime] = None
        for s in slots_sorted:
            slot_end = s + timedelta(minutes=15)
            if prev_end is None or s != prev_end:
                if prev_end is not None:
                    events.append((int(prev_end.timestamp()), 0))
                events.append((int(s.timestamp()), 1))
            prev_end = slot_end
        if prev_end is not None:
            events.append((int(prev_end.timestamp()), 0))
        return events

    def to_shelly_schedules(self, switch_id: int = 0) -> List[dict]:
        """Convert ON/OFF slots into Shelly Schedule.Create calls data."""
        if not self.on_slots:
            return []
        
        # Group into contiguous ranges
        sorted_slots = sorted(self.on_slots)
        ranges = []
        if not sorted_slots:
            return []
            
        run_start = sorted_slots[0]
        run_end = run_start + timedelta(minutes=15)
        for s in sorted_slots[1:]:
            if s == run_end:
                run_end = s + timedelta(minutes=15)
            else:
                ranges.append((run_start, run_end))
                run_start = s
                run_end = s + timedelta(minutes=15)
        ranges.append((run_start, run_end))

        # Shelly RPC for Schedule.Create expects cron-like or specific time.
        # For Gen2/Gen3 we can use "timespec" or similar.
        # Actually, for Nord Pool we want specific date/time.
        # Shelly Plus/Pro supports "start_period" or we can just use 15-min precision.
        
        # Strategy: One schedule for ON, one for OFF per range.
        # But Shelly has a limit of ~20-50 schedules.
        # If we have many 15-min slots, we might hit the limit.
        # Let's produce a list of commands for the script.
        cmds = []
        for start, end in ranges:
            tag = f"np-{start.strftime('%m%d%H%M')}"
            cmds.append({
                "enable": True,
                "name": tag + "-on",
                "timespec": f"0 {start.minute} {start.hour} {start.day} {start.month} *",
                "calls": [{
                    "method": "Switch.Set",
                    "params": {"id": switch_id, "on": True}
                }]
            })
            cmds.append({
                "enable": True,
                "name": tag + "-off",
                "timespec": f"0 {end.minute} {end.hour} {end.day} {end.month} *",
                "calls": [{
                    "method": "Switch.Set",
                    "params": {"id": switch_id, "on": False}
                }]
            })
        return cmds

    def to_daily_recurring_schedules(self, switch_id: int = 0) -> List[dict]:
        """Daily recurring ON/OFF (no calendar date) — survives offline nights."""
        return ranges_to_daily_recurring_schedules(self.to_ranges(), switch_id)

    def to_shelly_schedules_for_date(self, target_date: date, switch_id: int = 0) -> List[dict]:
        day_slots = {s for s in self.on_slots if s.date() == target_date}
        if not day_slots:
            return []
        return PlannedSchedule(
            generated_at=self.generated_at,
            valid_until=self.valid_until,
            on_slots=day_slots,
        ).to_shelly_schedules(switch_id)

    def to_ranges(self) -> List[dict]:
        """Return contiguous ON ranges in epoch timestamps for API consumers."""
        if not self.on_slots:
            return []

        sorted_slots = sorted(self.on_slots)
        ranges: List[dict] = []
        run_start = sorted_slots[0]
        run_end = run_start + timedelta(minutes=15)
        for s in sorted_slots[1:]:
            if s == run_end:
                run_end = s + timedelta(minutes=15)
            else:
                ranges.append({
                    "start": int(run_start.timestamp()),
                    "end": int(run_end.timestamp()),
                })
                run_start = s
                run_end = s + timedelta(minutes=15)
        ranges.append({
            "start": int(run_start.timestamp()),
            "end": int(run_end.timestamp()),
        })
        return ranges


def _parse_hhmm(s: str) -> dt_time:
    parts = s.strip().split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    return dt_time(hour=h, minute=m)


def _effective_weekday(d: date, holiday_as: str) -> Optional[int]:
    if d in _LV_HOLIDAYS:
        if holiday_as == "off":
            return None
        if holiday_as == "weekend":
            return 5
    return d.weekday()


def _select_cheapest_slots_in_window(
    candidates: List[tuple[float, datetime]],
    num_slots: int,
) -> List[datetime]:
    if num_slots <= 0:
        return []
    ranked = sorted(candidates, key=lambda p: (p[0], -p[1].timestamp()))
    return [dt for _, dt in ranked[:num_slots]]


MORNING_LOOKBACK_HOURS = 8  # allow cheap slots from previous evening (e.g. 23:15)


def _morning_search_candidates(
    prices: Dict[datetime, float],
    target_date: date,
    search_from: dt_time,
    ready_by: dt_time,
) -> List[tuple[float, datetime]]:
    """Morning block may start before midnight on the previous calendar day."""
    ready_dt = datetime.combine(target_date, ready_by, tzinfo=LOCAL_TZ)
    search_dt = datetime.combine(target_date, search_from, tzinfo=LOCAL_TZ)
    lookback_start = ready_dt - timedelta(hours=MORNING_LOOKBACK_HOURS)
    prev_date = target_date - timedelta(days=1)

    cands: List[tuple[float, datetime]] = []
    for dt, p in prices.items():
        if dt >= ready_dt:
            continue
        if dt.date() == target_date:
            if search_dt <= dt < ready_dt:
                cands.append((p, dt))
        elif dt.date() == prev_date and dt >= lookback_start:
            cands.append((p, dt))
    return cands


def _cheapest_contiguous_block(
    candidates: List[tuple[float, datetime]],
    num_slots: int,
    *,
    search_from: datetime,
    ready_by: datetime,
) -> List[datetime]:
    """Lowest-total-cost contiguous block of 15-min slots, ending by ready_by."""
    if num_slots <= 0 or not candidates:
        return []

    slot_map = {dt: p for p, dt in candidates}
    slot_dur = timedelta(minutes=15)
    block_dur = slot_dur * num_slots

    best_start: Optional[datetime] = None
    best_cost = float("inf")

    for start_dt in sorted(slot_map):
        if start_dt < search_from:
            continue
        end_dt = start_dt + block_dur
        if end_dt > ready_by:
            continue

        cost = 0.0
        ok = True
        cur = start_dt
        for _ in range(num_slots):
            p = slot_map.get(cur)
            if p is None:
                ok = False
                break
            cost += p
            cur += slot_dur
        if ok and cost < best_cost:
            best_cost = cost
            best_start = start_dt

    if best_start is None:
        return []
    return [best_start + i * slot_dur for i in range(num_slots)]


def plan_for_device(
    profile: DeviceProfile,
    prices: Dict[datetime, float],
    *,
    horizon_days: int = 2,
    now: Optional[datetime] = None,
) -> PlannedSchedule:
    now_local = now or datetime.now(LOCAL_TZ)
    today = now_local.date()

    sched = PlannedSchedule(
        generated_at=now_local,
        valid_until=datetime.combine(today + timedelta(days=horizon_days),
                                     dt_time.min, tzinfo=LOCAL_TZ),
    )

    if profile.vacation_mode:
        sched.notes.append("Atvaļinājuma režīms — viss izslēgts.")
        return sched

    config = profile.boiler_config

    for day_offset in range(horizon_days):
        target_date = today + timedelta(days=day_offset)
        day_prices = {dt: p for dt, p in prices.items() if dt.date() == target_date}
        if not day_prices:
            sched.notes.append(f"{target_date}: cenas nav pieejamas, izlaists.")
            continue

        effective_wd = _effective_weekday(target_date, profile.holiday_as)
        if effective_wd is None:
            sched.notes.append(f"{target_date}: svētki, profils '{profile.holiday_as}' → izlaists.")
            continue

        on_slots_for_day: Set[datetime] = set()

        if effective_wd in WORKDAYS:
            # Morning
            start_t = _parse_hhmm(config.weekday_morning_search_from)
            end_t = _parse_hhmm(config.weekday_morning_ready_by)
            end_dt = datetime.combine(target_date, end_t, tzinfo=LOCAL_TZ)
            cands = _morning_search_candidates(prices, target_date, start_t, end_t)
            lookback_start = end_dt - timedelta(hours=MORNING_LOOKBACK_HOURS)
            sel = _cheapest_contiguous_block(
                cands, config.weekday_morning_slots,
                search_from=lookback_start, ready_by=end_dt,
            )
            on_slots_for_day.update(sel)
            sched.coverage_by_window.setdefault("weekday_morning", []).extend(sel)

            # Evening
            start_t = _parse_hhmm(config.weekday_evening_search_from)
            end_t = _parse_hhmm(config.weekday_evening_ready_by)
            start_dt = datetime.combine(target_date, start_t, tzinfo=LOCAL_TZ)
            end_dt = datetime.combine(target_date, end_t, tzinfo=LOCAL_TZ)
            cands = [(p, dt) for dt, p in day_prices.items() if start_dt <= dt < end_dt]
            sel = _cheapest_contiguous_block(
                cands, config.weekday_evening_slots,
                search_from=start_dt, ready_by=end_dt,
            )
            on_slots_for_day.update(sel)
            sched.coverage_by_window.setdefault("weekday_evening", []).extend(sel)

        elif effective_wd in WEEKEND:
            # Morning
            start_t = _parse_hhmm(config.weekend_morning_search_from)
            end_t = _parse_hhmm(config.weekend_morning_ready_by)
            end_dt = datetime.combine(target_date, end_t, tzinfo=LOCAL_TZ)
            cands = _morning_search_candidates(prices, target_date, start_t, end_t)
            lookback_start = end_dt - timedelta(hours=MORNING_LOOKBACK_HOURS)
            sel = _cheapest_contiguous_block(
                cands, config.weekend_morning_slots,
                search_from=lookback_start, ready_by=end_dt,
            )
            on_slots_for_day.update(sel)
            sched.coverage_by_window.setdefault("weekend_morning", []).extend(sel)

            # Daytime
            start_t = _parse_hhmm(config.weekend_daytime_search_from)
            end_t = _parse_hhmm(config.weekend_daytime_ready_by)
            start_dt = datetime.combine(target_date, start_t, tzinfo=LOCAL_TZ)
            end_dt = datetime.combine(target_date, end_t, tzinfo=LOCAL_TZ)
            cands = [(p, dt) for dt, p in day_prices.items() if start_dt <= dt < end_dt]
            if config.weekend_peak_cut_eur_kwh is not None:
                cands = [(p, dt) for p, dt in cands if p <= config.weekend_peak_cut_eur_kwh]
            sel = _cheapest_contiguous_block(
                cands, config.weekend_daytime_slots,
                search_from=start_dt, ready_by=end_dt,
            )
            on_slots_for_day.update(sel)
            sched.coverage_by_window.setdefault("weekend_daytime", []).extend(sel)
        
        sched.on_slots.update(on_slots_for_day)

    return sched


def _slots_for_plan_date(on_slots: Set[datetime], target_date: date) -> Set[datetime]:
    """Include cross-midnight morning slots from the previous evening."""
    prev_date = target_date - timedelta(days=1)
    lookback_start = datetime.combine(
        target_date, dt_time.min, tzinfo=LOCAL_TZ
    ) - timedelta(hours=MORNING_LOOKBACK_HOURS)
    kept: Set[datetime] = set()
    for s in on_slots:
        if s.date() == target_date:
            kept.add(s)
        elif s.date() == prev_date and s >= lookback_start:
            kept.add(s)
    return kept


def schedule_for_date(
    profile: DeviceProfile,
    prices: Dict[datetime, float],
    target_date: date,
    *,
    now: Optional[datetime] = None,
) -> PlannedSchedule:
    full = plan_for_device(profile, prices, horizon_days=2, now=now)
    kept = _slots_for_plan_date(full.on_slots, target_date)
    full.on_slots = kept
    full.coverage_by_window = {
        k: [s for s in v if s in kept]
        for k, v in full.coverage_by_window.items()
    }
    return full


def humanize_schedule(sched: PlannedSchedule, *, max_lines: int = 40) -> str:
    if not sched.on_slots:
        if sched.notes:
            return "Grafiks tukšs:\n" + "\n".join(f"• {n}" for n in sched.notes)
        return "Grafiks tukšs."

    sorted_slots = sorted(sched.on_slots)
    ranges = []
    run_start = sorted_slots[0]
    run_end = run_start + timedelta(minutes=15)
    for s in sorted_slots[1:]:
        if s == run_end:
            run_end = s + timedelta(minutes=15)
        else:
            ranges.append((run_start, run_end))
            run_start = s
            run_end = s + timedelta(minutes=15)
    ranges.append((run_start, run_end))

    lines = []
    current_date = None
    for start, end in ranges[:max_lines]:
        if start.date() != current_date:
            current_date = start.date()
            lines.append(f"\n📅 {current_date.strftime('%A, %d.%m.%Y')}")
        lines.append(f"  {start.strftime('%H:%M')}–{end.strftime('%H:%M')}")
    if len(ranges) > max_lines:
        lines.append(f"  … un vēl {len(ranges) - max_lines} intervāli")

    total_h = len(sched.on_slots) * 0.25
    header = f"🔥 Sildīšana kopā {total_h:.2f} h"
    return header + "\n" + "\n".join(lines)


def _fallback_block_cron(ready_by: str, slots: int, wdays: str, switch_id: int, tag: str) -> List[dict]:
    """Weekly recurring ON/OFF pair ending at ready_by (not price-optimized)."""
    ready = _parse_hhmm(ready_by)
    end_min = ready.hour * 60 + ready.minute
    start_min = end_min - slots * 15
    if start_min < 0:
        return []
    sh, sm = divmod(start_min, 60)
    eh, em = ready.hour, ready.minute
    return [
        {
            "enable": True,
            "name": f"np-fb-{tag}-on",
            "timespec": f"0 {sm} {sh} * * {wdays}",
            "calls": [{"method": "Switch.Set", "params": {"id": switch_id, "on": True}}],
        },
        {
            "enable": True,
            "name": f"np-fb-{tag}-off",
            "timespec": f"0 {em} {eh} * * {wdays}",
            "calls": [{"method": "Switch.Set", "params": {"id": switch_id, "on": False}}],
        },
    ]


def fallback_recurring_schedules(profile: DeviceProfile, switch_id: int = 0) -> List[dict]:
    """Conservative weekly fallback if dated tomorrow sync fails."""
    if profile.vacation_mode:
        return []
    cfg = profile.boiler_config
    out: List[dict] = []
    out.extend(_fallback_block_cron(cfg.weekday_morning_ready_by, cfg.weekday_morning_slots, "1-5", switch_id, "wd-m"))
    out.extend(_fallback_block_cron(cfg.weekday_evening_ready_by, cfg.weekday_evening_slots, "1-5", switch_id, "wd-e"))
    out.extend(_fallback_block_cron(cfg.weekend_morning_ready_by, cfg.weekend_morning_slots, "0,6", switch_id, "we-m"))
    out.extend(_fallback_block_cron(cfg.weekend_daytime_ready_by, cfg.weekend_daytime_slots, "0,6", switch_id, "we-d"))
    return out


def ranges_to_daily_recurring_schedules(
    ranges: List[dict],
    switch_id: int = 0,
) -> List[dict]:
    """Convert ON/OFF ranges to daily cron (* * *) — works offline until next sync."""
    cmds: List[dict] = []
    for i, r in enumerate(ranges, start=1):
        s = datetime.fromtimestamp(r["start"], LOCAL_TZ)
        e = datetime.fromtimestamp(r["end"], LOCAL_TZ)
        tag = f"np-d{i}"
        cmds.append({
            "enable": True,
            "name": tag + "-on",
            "timespec": f"0 {s.minute} {s.hour} * * *",
            "calls": [{"method": "Switch.Set", "params": {"id": switch_id, "on": True}}],
        })
        cmds.append({
            "enable": True,
            "name": tag + "-off",
            "timespec": f"0 {e.minute} {e.hour} * * *",
            "calls": [{"method": "Switch.Set", "params": {"id": switch_id, "on": False}}],
        })
    return cmds
