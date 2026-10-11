"""The code's checks on every plan the model proposes (D-058, plan §2.4): the same rules the system prompt states,
verified in code because the model's word is not enough on a money path. A hard failure (news, the desk's side, a
stop inside the noise, a target under the costs) rejects the plan - it is shown greyed and never enters the hit-rate;
a soft failure (level not among the brief's own levels, rr < 1.5) is only noted. Pure: plain dicts in, a dict out."""
from __future__ import annotations

import datetime as dt

from ..core.timeutil import MS_PER_MINUTE

MIN_STOP_USD = 2.0             # a stop under $2 on gold is inside the 5m noise (ATR14 median $3.9)
MIN_STOP_ATR = 0.5             # ... and under half the 5m ATR too
MIN_RR = 1.5
TARGET_SPREAD_MULT = 5.0       # the target must clear the spread five times over
TOL_ATR = 0.25                 # tolerance = max(0.25 ATR5m, 2 x median spread, $0.5)
TOL_SPREAD_MULT = 2.0
TOL_FLOOR_USD = 0.5
HARD = ("news_ok", "desk_ok", "stop_ok", "target_ok")


def tolerance(atr5m: float | None, spread_median: float | None) -> float:
    """How close is "at the level" (USD): a quarter of the 5m ATR, twice the median spread, never under $0.5."""
    return max(TOL_ATR * float(atr5m or 0.0), TOL_SPREAD_MULT * float(spread_median or 0.0), TOL_FLOOR_USD)


def _opposite(side: str) -> str:
    return "SELL" if side == "BUY" else "BUY"


def _leg_side(x) -> str | None:
    s = str(x or "").upper()
    return "BUY" if s.startswith("BUY") else "SELL" if s.startswith("SELL") else None


def _event_ms(ev, as_of: int) -> tuple[int | None, str | None]:
    """(time ms, tier) of a ``market.news.next`` entry: ``[iso, tier, title, minutes_ahead]`` as ``news.block``
    writes it, or a dict with ``time_ms`` / ``tier``."""
    if isinstance(ev, dict):
        t = ev.get("time_ms")
        return (int(t) if t is not None else None), ev.get("tier")
    if not isinstance(ev, (list, tuple)) or len(ev) < 2:
        return None, None
    when, tier = ev[0], ev[1]
    if isinstance(when, (int, float)):
        return int(when), tier
    try:
        return int(dt.datetime.fromisoformat(str(when).replace("Z", "+00:00")).timestamp() * 1000), tier
    except ValueError:
        pass
    if len(ev) >= 4 and isinstance(ev[3], (int, float)):
        return as_of + int(ev[3]) * MS_PER_MINUTE, tier
    return None, tier


def check_plan(plan: dict, *, atr5m: float | None, spread: float | None, blackout: bool, next_events: list,
               news_windows: dict, desk_sides: list[str], levels: list[dict], tol: float, as_of: int,
               trigger_window_min: int) -> dict:
    """``{stop_ok, target_ok, news_ok, desk_ok, level_ok, rr_ok, rejected, notes}`` for one plan.

    ``next_events`` / ``news_windows``: the payload's ``market.news.next`` and the pair's ``news_blackout.windows``
    (minutes [before, after] per tier; a tier without a window is never a blackout). The plan's exposure window is
    ``[as_of, as_of + trigger_window_min + max_hold_min]``. ``desk_sides``: the sides of the desk's legs on the pair."""
    entry, stop, target = float(plan["entry"]), float(plan["stop"]), float(plan["target"])
    side = str(plan.get("side", "")).upper()
    risk = abs(entry - stop)
    reward = abs(target - entry)
    notes: list[str] = []

    min_stop = max(MIN_STOP_ATR * float(atr5m or 0.0), MIN_STOP_USD)
    stop_ok = risk >= min_stop - 1e-9
    if not stop_ok:
        notes.append(f"stop {risk:.2f} < {min_stop:.2f} (max of {MIN_STOP_ATR} x ATR5m, ${MIN_STOP_USD:g})")

    min_target = max(MIN_RR * risk, TARGET_SPREAD_MULT * float(spread or 0.0))
    target_ok = reward >= min_target - 1e-9
    if not target_ok:
        notes.append(f"target {reward:.2f} < {min_target:.2f} (max of {MIN_RR} R, {TARGET_SPREAD_MULT:g} x spread)")

    hold_min = int(plan.get("max_hold_min") or 0)
    lo, hi = as_of, as_of + (int(trigger_window_min) + hold_min) * MS_PER_MINUTE
    news_ok = not blackout
    if blackout:
        notes.append("news blackout at the brief's time")
    for ev in next_events or []:
        t, tier = _event_ms(ev, as_of)
        win = (news_windows or {}).get(tier) if tier is not None else None
        if t is None or not win:
            continue
        before, after = int(win[0]), int(win[1])
        if t - before * MS_PER_MINUTE <= hi and t + after * MS_PER_MINUTE >= lo:
            news_ok = False
            notes.append(f"{tier} release at {ev[0] if isinstance(ev, (list, tuple)) else t} inside the plan's window")
            break

    legs = {s for s in (_leg_side(x) for x in desk_sides or []) if s}
    desk_ok = _opposite(side) not in legs
    if not desk_ok:
        notes.append(f"against the desk's open {_opposite(side)} leg")

    lp = float(plan.get("level_price") or 0.0)
    level_ok = any(abs(lp - float(lv.get("price") or 0.0)) <= tol for lv in levels or [])
    if not level_ok:
        notes.append(f"level {lp:g} is not among the brief's levels (tol {tol:.2f})")

    rr = float(plan["rr"]) if plan.get("rr") is not None else (reward / risk if risk else 0.0)
    rr_ok = rr >= MIN_RR - 1e-9
    if not rr_ok:
        notes.append(f"rr {rr:.2f} < {MIN_RR}")

    out = {"stop_ok": stop_ok, "target_ok": target_ok, "news_ok": news_ok, "desk_ok": desk_ok, "level_ok": level_ok,
           "rr_ok": rr_ok}
    out["rejected"] = [k for k in HARD if not out[k]]
    out["notes"] = notes
    return out
