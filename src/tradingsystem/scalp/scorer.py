"""Scoring a brief on the real ticks (D-058, plan §3.2-3.4), free of any model call: did the levels hold or break,
did the plans trigger and reach the target or the stop first, what did the owner trade by hand that hour. The rules
are fixed before the results (the same tick-walk conventions as ``tools/backtest_whatif.py``: a BUY is filled on the
ask and exits on the bid, a SELL the other way round, no slippage, a same-tick tie is a stop). Pure numpy functions
over a stored record; the two IO helpers (``read_ticks``, ``fetch_deals``) are thin and never run in the unit suite."""
from __future__ import annotations

import os
from typing import Any, Callable

import numpy as np

from ..core.timeutil import MS_PER_MINUTE
from .checks import tolerance

GAP_MS = 5 * MS_PER_MINUTE          # ticks missing for longer inside a window = the outcome is unknown
ATTRIBUTION_MS = 60 * MS_PER_MINUTE  # a manual trade belongs to the brief of the hour it was opened in
DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY = 0, 1, 3     # MetaTrader5 constants (mt5_backend.py uses the names)
DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1


# ------------------------------------------------------------------------------------------------- IO: the ticks
def read_ticks(reader, exe, t0: int, t1: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(time_msc int64, bid, ask)`` of the execution instrument's recorded ticks in ``[t0, t1]`` (UTC ms), sorted."""
    from ..storage.tablespec import spec_for

    cols = reader.read_range(spec_for(exe, "ticks"), t0, t1 + 1, ["time_msc", "bid", "ask"])
    t = np.asarray(cols["time_msc"], dtype=np.int64)
    bid = np.asarray(cols["bid"], dtype=np.float64)
    ask = np.asarray(cols["ask"], dtype=np.float64)
    if len(t) > 1 and np.any(np.diff(t) < 0):
        order = np.argsort(t, kind="stable")
        t, bid, ask = t[order], bid[order], ask[order]
    return t, bid, ask


# ------------------------------------------------------------------------------------------------- maturity
def _hold_ms(rec: dict) -> int:
    plans = (rec.get("brief") or {}).get("plans") or []
    return max((int(p.get("max_hold_min") or 0) for p in plans), default=0) * MS_PER_MINUTE


def maturity_ms(rec: dict, cfg) -> int:
    """When the record can be scored: every plan's trigger window + its hold, and every level's horizon + hold."""
    as_of = int(rec["as_of_ms"])
    plans_end = cfg.trigger_window_min * MS_PER_MINUTE + _hold_ms(rec)
    levels_end = (cfg.level_horizon_min + cfg.hold_window_min) * MS_PER_MINUTE
    return as_of + max(plans_end, levels_end)


def is_mature(rec: dict, now: int, cfg) -> bool:
    return now >= maturity_ms(rec, cfg)


# ------------------------------------------------------------------------------------------------- levels
def _has_gap(t: np.ndarray, lo: int, hi: int) -> bool:
    """Ticks missing for more than ``GAP_MS`` anywhere in ``[lo, hi]`` (including before the first / after the last)."""
    if hi <= lo:
        return False
    if len(t) == 0:
        return hi - lo > GAP_MS
    edges = np.concatenate(([lo], t, [hi]))
    return bool(np.max(np.diff(edges)) > GAP_MS)


def score_levels(levels: list[dict], t: np.ndarray, bid: np.ndarray, ask: np.ndarray, *, as_of: int, price_at: float,
                 tol: float, horizon_ms: int, hold_ms: int) -> list[dict]:
    """Per level: ``untouched`` (the mid never came within ``tol`` in the horizon), then from the first touch within
    ``hold_ms``: ``held`` (moved ``>= tol`` away on its role side first), ``broken`` (crossed through by ``> tol``
    first) or ``undecided``. The role comes from where the price stood at the brief: below it = support. ``unknown``
    when the ticks of the horizon are missing (a gap over 5 min before any touch)."""
    mid = (bid + ask) / 2.0
    out = []
    for lv in levels:
        price = float(lv["price"])
        role = "support" if price < float(price_at) else "resistance"
        row: dict[str, Any] = {"price": price, "role": role, "outcome": "untouched", "touched_ms": None}
        win = (t >= as_of) & (t <= as_of + horizon_ms)
        idx = np.flatnonzero(win & (np.abs(mid - price) <= tol))
        if len(idx) == 0:
            if _has_gap(t[win], as_of, as_of + horizon_ms):
                row["outcome"] = "unknown"
            out.append(row)
            continue
        i0 = int(idx[0])
        touched = int(t[i0])
        if _has_gap(t[(t >= as_of) & (t <= touched)], as_of, touched):
            row["outcome"] = "unknown"
            out.append(row)
            continue
        row["touched_ms"] = touched
        sel = slice(i0, int(np.searchsorted(t, touched + hold_ms, "right")))
        m = mid[sel]
        if role == "support":
            held, broken = m >= price + tol, m < price - tol
        else:
            held, broken = m <= price - tol, m > price + tol
        i_held = int(np.argmax(held)) if held.any() else None
        i_broken = int(np.argmax(broken)) if broken.any() else None
        if i_held is not None and (i_broken is None or i_held < i_broken):
            row["outcome"], row["decided_ms"] = "held", int(t[sel][i_held])
        elif i_broken is not None:
            row["outcome"], row["decided_ms"] = "broken", int(t[sel][i_broken])
        else:
            row["outcome"] = "undecided"
        # ticks missing between the touch and the decision (the whole hold window when undecided) = unknown
        until = row.get("decided_ms", touched + hold_ms)
        if _has_gap(t[sel][t[sel] <= until], touched, until):
            row["outcome"] = "unknown"
        out.append(row)
    return out


# ------------------------------------------------------------------------------------------------- plans
def _fill_index(plan: dict, t: np.ndarray, bid: np.ndarray, ask: np.ndarray, win: np.ndarray, tol: float) -> int | None:
    """The index of the first tick that fills the plan inside the trigger window, or None."""
    side, kind = str(plan["side"]).upper(), str(plan.get("entry_type") or "limit")
    entry = float(plan["entry"])
    if kind == "limit":
        hit = (ask <= entry) if side == "BUY" else (bid >= entry)
        hit = hit & win
    else:                                   # stop / market_after_rejection: only after the level was touched
        mid = (bid + ask) / 2.0
        touch = np.flatnonzero(win & (np.abs(mid - float(plan.get("level_price") or entry)) <= tol))
        if len(touch) == 0:
            return None
        hit = (ask >= entry) if side == "BUY" else (bid <= entry)
        hit = hit & win
        hit[:int(touch[0])] = False
    idx = np.flatnonzero(hit)
    return int(idx[0]) if len(idx) else None


def score_plan(plan: dict, t: np.ndarray, bid: np.ndarray, ask: np.ndarray, *, as_of: int, trigger_window_ms: int,
               tol: float) -> dict:
    """``{outcome, fill_ms, exit_ms, r, mfe_r, mae_r, minutes}``: ``not_triggered`` inside the trigger window, else the
    walk to ``max_hold_min`` after the fill: ``target`` / ``stop`` (first touched; both on one tick = ``stop``) or
    ``expired`` with the signed open P&L in R. ``unknown`` when the ticks of a window are missing."""
    side = str(plan["side"]).upper()
    entry, stop, target = float(plan["entry"]), float(plan["stop"]), float(plan["target"])
    risk = abs(entry - stop)
    hold_ms = int(plan.get("max_hold_min") or 0) * MS_PER_MINUTE
    row: dict[str, Any] = {"outcome": "not_triggered", "fill_ms": None, "exit_ms": None, "r": None, "mfe_r": None,
                           "mae_r": None, "minutes": None}
    if risk <= 0:
        row["outcome"] = "unknown"
        return row
    t_end = as_of + trigger_window_ms
    win = (t >= as_of) & (t <= t_end)
    i_fill = _fill_index(plan, t, bid, ask, win, tol)
    if i_fill is None:
        if _has_gap(t[win], as_of, t_end):
            row["outcome"] = "unknown"
        return row
    t_fill = int(t[i_fill])
    if _has_gap(t[(t >= as_of) & (t <= t_fill)], as_of, t_fill):
        row["outcome"] = "unknown"
        return row
    row["fill_ms"] = t_fill
    t_hold_end = t_fill + hold_ms
    j_end = int(np.searchsorted(t, t_hold_end, "right"))
    px = (bid if side == "BUY" else ask)[i_fill:j_end]
    th = t[i_fill:j_end]
    sign = 1.0 if side == "BUY" else -1.0
    exc = sign * (px - entry) / risk                 # the open result in R at every tick
    hit_stop = (px <= stop) if side == "BUY" else (px >= stop)
    hit_tgt = (px >= target) if side == "BUY" else (px <= target)
    i_stop = int(np.argmax(hit_stop)) if hit_stop.any() else None
    i_tgt = int(np.argmax(hit_tgt)) if hit_tgt.any() else None
    if i_stop is not None and (i_tgt is None or i_stop <= i_tgt):
        i_exit, row["outcome"], row["r"] = i_stop, "stop", -1.0
    elif i_tgt is not None:
        i_exit, row["outcome"], row["r"] = i_tgt, "target", round(abs(target - entry) / risk, 3)
    else:
        i_exit, row["outcome"], row["r"] = len(px) - 1, "expired", round(float(exc[-1]), 3)
    # ticks missing up to the exit (the whole hold for an expiry) = nothing can be said; after the exit they are moot
    if _has_gap(th[:i_exit + 1], t_fill, t_hold_end if row["outcome"] == "expired" else int(th[i_exit])):
        row.update(outcome="unknown", r=None)
        return row
    seen = exc[:i_exit + 1]
    row["exit_ms"] = int(th[i_exit])
    row["mfe_r"] = round(max(0.0, float(seen.max())), 3)
    row["mae_r"] = round(min(0.0, float(seen.min())), 3)
    row["minutes"] = round((row["exit_ms"] - t_fill) / MS_PER_MINUTE, 1)
    return row


# ------------------------------------------------------------------------------------------------- the record
def score_record(rec: dict, t, bid, ask, *, now: int, cfg) -> dict:
    """The stored record's score (plan §3): levels, plans (a plan a hard check rejected is marked ``rejected`` and not
    walked) and the summary the page and the journal read."""
    t = np.asarray(t, dtype=np.int64)
    bid = np.asarray(bid, dtype=np.float64)
    ask = np.asarray(ask, dtype=np.float64)
    as_of = int(rec["as_of_ms"])
    brief = rec.get("brief") or {}
    tol = float(rec.get("tol") or tolerance(rec.get("atr5m"), rec.get("spread_median")))
    price_at = float(rec.get("price_at") or 0.0)
    if not price_at and len(t):
        i = int(np.searchsorted(t, as_of, "right")) - 1
        price_at = float((bid[max(i, 0)] + ask[max(i, 0)]) / 2.0)
    levels = score_levels(brief.get("levels") or [], t, bid, ask, as_of=as_of, price_at=price_at, tol=tol,
                          horizon_ms=cfg.level_horizon_min * MS_PER_MINUTE, hold_ms=cfg.hold_window_min * MS_PER_MINUTE)
    checks = rec.get("checks") or {}
    plans = []
    for idx, plan in enumerate(brief.get("plans") or []):
        if (checks.get(str(idx)) or {}).get("rejected"):
            row: dict[str, Any] = {"outcome": "rejected", "fill_ms": None, "exit_ms": None, "r": None, "mfe_r": None,
                                   "mae_r": None, "minutes": None}
        else:
            row = score_plan(plan, t, bid, ask, as_of=as_of, trigger_window_ms=cfg.trigger_window_min * MS_PER_MINUTE,
                             tol=tol)
        plans.append({"idx": idx, **row})
    counted = [p for p in plans if p["outcome"] not in ("rejected", "unknown")]
    triggered = [p for p in counted if p["outcome"] != "not_triggered"]
    summary = {"levels_touched": sum(1 for lv in levels if lv["outcome"] not in ("untouched", "unknown")),
               "levels_held": sum(1 for lv in levels if lv["outcome"] == "held"),
               "levels_unknown": sum(1 for lv in levels if lv["outcome"] == "unknown"),
               "plans_n": len(counted), "triggered": len(triggered),
               "target_first": sum(1 for p in triggered if p["outcome"] == "target"),
               "stop_first": sum(1 for p in triggered if p["outcome"] == "stop"),
               "expired": sum(1 for p in triggered if p["outcome"] == "expired"),
               "unknown": sum(1 for p in plans if p["outcome"] == "unknown"),
               "rejected": sum(1 for p in plans if p["outcome"] == "rejected"),
               "sum_r": round(sum(float(p["r"] or 0.0) for p in triggered), 3),
               "ticks": int(len(t)), "scored_at_ms": int(now)}
    return {"levels": levels, "plans": plans, "summary": summary}


# ------------------------------------------------------------------------------------------------- manual deals
def _money(d) -> float:
    """A deal's net effect on the balance: profit + commission + swap + fee (each missing = 0)."""
    return sum(float(getattr(d, k, 0) or 0) for k in ("profit", "commission", "swap", "fee"))


def manual_trades(deals, *, symbol: str, server_to_utc: Callable[[int], int], t0: int, t1: int) -> list[dict]:
    """The owner's manual trades (magic 0 on ``symbol``) opened in ``[t0, t1]`` UTC, one row per position: the IN
    deal gives side / entry / open time, the OUT (and OUT_BY) deals the exit and close time; P&L = profit +
    commission + swap + fee over every deal of the position. A position without an OUT yet is ``open``."""
    by_pos: dict[int, list] = {}
    for d in deals or ():
        if int(getattr(d, "magic", 0) or 0) != 0 or getattr(d, "symbol", None) != symbol:
            continue
        by_pos.setdefault(int(getattr(d, "position_id", 0) or 0), []).append(d)
    out = []
    for pid, ds in by_pos.items():
        ins = [d for d in ds if int(d.entry) == DEAL_ENTRY_IN]
        outs = [d for d in ds if int(d.entry) in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY)]
        if not ins:
            continue                             # opened before the window: nothing to attribute
        ins.sort(key=lambda d: int(d.time_msc))
        vol_in = sum(float(d.volume) for d in ins) or 1.0
        open_ms = int(server_to_utc(int(ins[0].time_msc)))
        if not t0 <= open_ms <= t1:
            continue
        row: dict[str, Any] = {
            "position_id": pid, "side": "BUY" if int(ins[0].type) == DEAL_TYPE_BUY else "SELL",
            "entry": round(sum(float(d.price) * float(d.volume) for d in ins) / vol_in, 3), "exit": None,
            "open_ms": open_ms, "close_ms": None, "volume": round(vol_in, 2),
            "pnl_usd": round(sum(_money(d) for d in ds), 2),
            "open": not outs, "comment": str(getattr(ins[0], "comment", "") or ""), "brief_id": None,
            "followed_plan": False}
        if outs:
            vol_out = sum(float(d.volume) for d in outs) or 1.0
            row["exit"] = round(sum(float(d.price) * float(d.volume) for d in outs) / vol_out, 3)
            row["close_ms"] = int(server_to_utc(max(int(d.time_msc) for d in outs)))
        out.append(row)
    out.sort(key=lambda r: r["open_ms"])
    return out


def attribute(trades: list[dict], records: list[dict], tol: float) -> None:
    """Sets ``brief_id`` (the brief whose hour holds the trade's open) and ``followed_plan`` (a plan of that brief
    with the same side and an entry within ``tol``) on every trade, in place."""
    recs = sorted((r for r in records if isinstance(r, dict) and r.get("as_of_ms") is not None),
                  key=lambda r: int(r["as_of_ms"]))
    for tr in trades:
        tr["brief_id"], tr["followed_plan"] = None, False
        open_ms = int(tr.get("open_ms") or 0)
        rec = None
        for r in recs:
            if int(r["as_of_ms"]) <= open_ms < int(r["as_of_ms"]) + ATTRIBUTION_MS:
                rec = r                          # the latest brief whose hour holds the open wins
        if rec is None:
            continue
        tr["brief_id"] = rec.get("id")
        for i, plan in enumerate((rec.get("brief") or {}).get("plans") or []):
            if str(plan.get("side")).upper() == str(tr.get("side")).upper() and \
                    abs(float(plan["entry"]) - float(tr.get("entry") or 0)) <= tol:
                tr["followed_plan"], tr["plan_idx"] = True, i
                break


# ------------------------------------------------------------------------------------------------- IO: the deals
_SERVER_TIME = None


def server_to_utc(server_ms: int) -> int:
    """The broker's deal time (server ms) as UTC ms, the executor's convention (``prefer='earlier'``)."""
    global _SERVER_TIME
    if _SERVER_TIME is None:
        from ..ingest.mt5.servertime import ServerTimeModel
        _SERVER_TIME = ServerTimeModel()
    return _SERVER_TIME.server_to_utc(int(server_ms), prefer="earlier")


def fetch_deals(s, t0: int, t1: int) -> list:
    """The account's deals in ``[t0, t1]`` UTC from the RUNNING terminal (attach only, never a launch: the terminal is
    the supervisor's). Raises ``MT5Unavailable`` when it is not running or the attach fails."""
    from ..ingest.mt5.servertime import ServerTimeModel
    from ..ingest.mt5.terminal import NO_LAUNCH_ENV, NO_LAUNCH_MIN_AGE_S, MT5Terminal, MT5Unavailable, terminal_running

    profile = s.mt5_data_profile()
    if not terminal_running(profile.terminal_path, min_age_s=NO_LAUNCH_MIN_AGE_S):
        raise MT5Unavailable("MT5 terminal is not running: manual deals not read")
    os.environ[NO_LAUNCH_ENV] = "1"
    model = ServerTimeModel()
    term = MT5Terminal(profile)
    term.connect()
    try:
        deals = term.mt5.history_deals_get(model.utc_to_server(int(t0)) // 1000, model.utc_to_server(int(t1)) // 1000)
    finally:
        term.shutdown()
    return list(deals or ())
