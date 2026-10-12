"""D-061 (owner, 2026-10-12): the scalp brief's trade placed automatically - when the owner says so on the Scalp tab.

Two settings the dashboard writes to ``data/scalp/settings.json`` (``POST /api/scalp/settings``; no restart):
``mode`` = ``analyze`` (the default: the brief only, the owner trades by hand) or ``auto`` (THE trade of the brief -
``record["trade"]``, the plan that passed the code checks - becomes a pending order on the same account), and the
fixed ``lot`` the brief analyses with and trades with (the prompt states it; a plan's risk = |entry - stop| x lot x
contract size). ``min_confidence`` filters what gets placed.

Every gate is code (``auto_gate``): mode auto; ``execution.mode`` demo (auto scalps never run on a live account
without a decision of their own); a plan was chosen; its entry is ``limit`` or ``stop`` (a ``market_after_rejection``
plan needs a human: the code reads no candles); its confidence >= ``min_confidence``; no global / pair kill switch;
the market open; no scalp order or position already open (one at a time); the risk <= ``scalp.auto_risk_cap_pct``
of the account equity. The order goes through the desk's own ``MT5Backend`` (``order_check`` then ``order_send``,
SL + TP attached, ``ORDER_TIME_SPECIFIED`` = the brief's time + ``trigger_window_min``: the broker removes an
untriggered order itself) under the SCALP MAGIC (``execution.magic`` base + ``scalp.auto_magic_offset``), comment
``ts:sc<brief id>:1``: the desk never manages it (not ``mine()``) but counts it in its caps (``sibling()``, the
executor adds the magic to its family). The hourly loop runs :func:`manage` every minute while something is open: a
position older than its plan's ``max_hold_min`` is closed at market, a vanished order / position is settled in the
state. ``auto_state.json`` holds what is open, ``auto_log.jsonl`` every event; the brief record gets ``execution``.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from ..core.killswitch import is_on as kill_switch_on
from ..core.notify import notify
from ..core.settings import Settings
from ..core.timeutil import MS_PER_MINUTE, iso, now_ms
from .files import read_json, scalp_dir, write_json_atomic

log = logging.getLogger(__name__)

MODES = ("analyze", "auto")
SETTINGS_FILE = "settings.json"
STATE_FILE = "auto_state.json"
LOG_FILE = "auto_log.jsonl"
LOG_MAX_BYTES = 2_000_000              # then the file starts over (the record and the state keep the facts)
AUTO_ENTRY_TYPES = ("limit", "stop")
COMMENT_PREFIX = "sc"                   # the backend tags the order ts:sc<brief id>:1 (<= 31 chars)
MANAGE_EVERY_S = 60.0
MIN_LOT = 0.01
EVERY_MIN_CHOICES = (15, 20, 30, 60, 120)   # D-062: the loop's cadence the owner may pick (minutes between briefs)


# ------------------------------------------------------------------------------------------------- settings
def scalp_magic(s: Settings) -> int:
    """The scalp trades' magic: the project's base magic + ``scalp.auto_magic_offset`` (no pair folder's own)."""
    from ..execution.executor import magics
    return magics(s)[1] + s.scalp.auto_magic_offset


def default_settings(s: Settings) -> dict:
    return {"mode": "analyze", "lot": float(s.scalp.auto_default_lot), "min_confidence": int(s.scalp.auto_min_confidence),
            "hours_utc": [int(s.scalp.hours_utc[0]), int(s.scalp.hours_utc[1])], "every_min": 60,
            "updated_ms": None, "by": "default"}


def validate_settings(s: Settings, doc: dict) -> tuple[dict | None, str | None]:
    """``(clean, error)``: the owner-set values (mode, lot, confidence floor; D-062 the loop's hours and cadence),
    bounded by the config."""
    base = default_settings(s)
    try:
        mode = str(doc.get("mode", "analyze")).strip().lower()
        raw = float(doc.get("lot", s.scalp.auto_default_lot))
        conf = int(doc.get("min_confidence", s.scalp.auto_min_confidence))
        hours = doc.get("hours_utc", base["hours_utc"])
        if not isinstance(hours, (list, tuple)) or len(hours) != 2:
            return None, "hours_utc must be [start, end] in UTC hours"
        h0, h1 = int(hours[0]), int(hours[1])
        every = int(doc.get("every_min", 60))
    except (TypeError, ValueError) as exc:
        return None, f"settings: {type(exc).__name__}: {exc}"
    if mode not in MODES:
        return None, f"mode must be one of {', '.join(MODES)} (got {mode!r})"
    if abs(raw / MIN_LOT - round(raw / MIN_LOT)) > 1e-6:
        return None, f"lot must be a multiple of {MIN_LOT:g} (got {raw:g})"
    lot = round(raw, 2)
    if not MIN_LOT <= lot <= float(s.scalp.auto_max_lot) + 1e-9:
        return None, f"lot must be between {MIN_LOT:g} and {s.scalp.auto_max_lot:g} (got {lot:g})"
    if not 0 <= conf <= 100:
        return None, f"min_confidence must be 0-100 (got {conf})"
    if not 0 <= h0 < h1 <= 24:
        return None, f"hours_utc must be [start, end) with 0 <= start < end <= 24 (got [{h0}, {h1}])"
    if every not in EVERY_MIN_CHOICES:
        return None, f"every_min must be one of {', '.join(str(x) for x in EVERY_MIN_CHOICES)} (got {every})"
    return {"mode": mode, "lot": lot, "min_confidence": conf, "hours_utc": [h0, h1], "every_min": every}, None


def read_settings(s: Settings) -> dict:
    """The owner's settings, the defaults for anything missing or invalid (never raises)."""
    base = default_settings(s)
    doc = read_json(scalp_dir(s) / SETTINGS_FILE)
    if not isinstance(doc, dict):
        return base
    clean, err = validate_settings(s, {**base, **doc})
    if err:
        log.warning("scalp settings.json ignored (%s): the defaults apply", err)
        return base
    return {**clean, "updated_ms": doc.get("updated_ms"), "by": doc.get("by") or "dashboard"}


def write_settings(s: Settings, *, by: str = "dashboard", now: int | None = None, **changes: Any) -> dict:
    """Validate and store (atomic) the given values over the current ones (a partial update keeps the rest).
    Raises ``ValueError`` with the reason."""
    cur = read_settings(s)
    clean, err = validate_settings(s, {**{k: v for k, v in cur.items() if k not in ("updated_ms", "by")}, **changes})
    if err:
        raise ValueError(err)
    doc = {**clean, "updated_ms": int(now if now is not None else now_ms()), "by": by}
    write_json_atomic(scalp_dir(s) / SETTINGS_FILE, doc)
    log.info("scalp settings: mode %s, lot %g, min confidence %d, hours %s UTC every %d min (%s)", doc["mode"], doc["lot"],
             doc["min_confidence"], doc["hours_utc"], doc["every_min"], by)
    return doc


def schedule(s: Settings) -> tuple[list[int], int]:
    """D-062: ``(hours_utc, every_min)`` the loop and the hourly gate follow (the owner's settings, else the config)."""
    st = read_settings(s)
    return [int(st["hours_utc"][0]), int(st["hours_utc"][1])], int(st["every_min"])


def execution_text(s: Settings, settings: dict, equity: float | None, open_info: dict | None = None) -> str:
    """The line the user prompt carries: the fixed lot, the risk cap in USD, whether the trade is placed, and
    (D-063) the previous brief's auto-trade still open."""
    lot, oz = float(settings["lot"]), float(settings["lot"]) * float(s.scalp.contract_size)
    cap = (f"{equity * s.scalp.auto_risk_cap_pct / 100:.2f} USD ({s.scalp.auto_risk_cap_pct:g} % of equity {equity:.2f})"
           if isinstance(equity, (int, float)) and equity > 0 else f"{s.scalp.auto_risk_cap_pct:g} % of the equity")
    head = (f"The owner's lot is FIXED at {lot:g} lots = {oz:g} oz: a plan's risk in USD = |entry - stop| x {oz:g}. "
            f"Keep every plan's risk at or under {cap}.")
    if settings.get("mode") == "auto":
        head += (f" EXECUTION IS AUTOMATIC: the plan `best_plan` is placed as a pending order at its `entry` with "
                 f"its stop and target, {lot:g} lots, expiring {s.scalp.trigger_window_min} min after this brief, and the "
                 f"position is closed after its `max_hold_min`. Only `limit` and `stop` entries can be automated "
                 f"(a `market_after_rejection` plan stays a plan for the owner's hand); a plan under confidence "
                 f"{int(settings['min_confidence'])} is not placed.")
        return head + open_trade_text(open_info)
    return head + " Execution is BY HAND: the owner reads the brief and places the order himself."


def open_trade_text(open_info: dict | None) -> str:
    """D-063: the previous brief's auto-trade as the model reads it (nothing: an explicit sentence)."""
    if not open_info or not open_info.get("kind"):
        return " YOUR OPEN TRADE: none (set manage_open to none)."
    o = open_info
    if o["kind"] == "position":
        pnl = o.get("profit_usd")
        pnl_s = f"{pnl:+.2f} USD" if isinstance(pnl, (int, float)) else "unknown"
        return (f" YOUR OPEN TRADE (from brief {o.get('brief_id')}): {o.get('side')} {o.get('lot'):g} lots filled at "
                f"{o.get('entry')}, stop {o.get('sl')}, target {o.get('tp')}, now {pnl_s} at price {o.get('price')}, held "
                f"{o.get('held_min', 0):.0f} of {o.get('max_hold_min')} min"
                + (" - IN PROFIT beyond the spread" if o.get("in_profit") else " - not in profit")
                + ". Decide manage_open: hold / close / breakeven, with manage_reason.")
    return (f" YOUR PENDING ORDER (from brief {o.get('brief_id')}, not triggered): {o.get('side')} {o.get('order_type')} "
            f"{o.get('lot'):g} lots at {o.get('entry')}, stop {o.get('sl')}, target {o.get('tp')} - it is cancelled now "
            f"and replaced by this brief's trade (or just cancelled when you give none). manage_open: none.")


# ------------------------------------------------------------------------------------------------- the state and the log
def read_state(s: Settings) -> dict:
    doc = read_json(scalp_dir(s) / STATE_FILE)
    items = doc.get("open") if isinstance(doc, dict) else None
    return {"open": [e for e in items if isinstance(e, dict) and e.get("ticket") is not None] if isinstance(items, list)
            else [], "updated_ms": doc.get("updated_ms") if isinstance(doc, dict) else None}


def write_state(s: Settings, state: dict, now: int | None = None) -> None:
    write_json_atomic(scalp_dir(s) / STATE_FILE, {"open": list(state.get("open") or []),
                                                  "updated_ms": int(now if now is not None else now_ms())})


def log_event(s: Settings, event: dict) -> None:
    """One JSON line per event (placed, refused, failed, closed, settled); a huge file starts over."""
    path = scalp_dir(s) / LOG_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_name(LOG_FILE + ".1"))
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except OSError:
        log.warning("scalp auto log %s not written", path, exc_info=True)


def read_log(s: Settings, n: int = 30) -> list[dict]:
    """The last ``n`` events, newest first (a bounded read)."""
    path = scalp_dir(s) / LOG_FILE
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 200_000))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict):
            out.append(doc)
        if len(out) >= n:
            break
    return out


# ------------------------------------------------------------------------------------------------- pure decisions
def risk_usd(plan: dict, lot: float, contract_size: float) -> float:
    return abs(float(plan["entry"]) - float(plan["stop"])) * float(lot) * float(contract_size)


def order_rec(plan: dict, *, as_of: int, window_min: int) -> dict:
    """The decision-like record ``MT5Backend.place`` builds one pending order from (one leg, the whole target)."""
    side = str(plan["side"]).upper()
    kind = "LIMIT" if plan.get("entry_type") == "limit" else "STOP"
    return {"decision": side, "order_type": f"{side}_{kind}", "stop_loss": float(plan["stop"]),
            "take_profits": [{"price": float(plan["target"]), "close_fraction": 1.0}],
            "valid_until": iso(int(as_of) + int(window_min) * MS_PER_MINUTE)}


def auto_gate(*, settings: dict, rec: dict, equity: float | None, cfg, kill: bool, market_open: bool,
              open_now: int, execution_mode: str) -> tuple[str, str]:
    """``('place' | 'skip', why)`` for the brief ``rec`` under the owner's ``settings``. Pure."""
    if settings.get("mode") != "auto":
        return "skip", "mode analyze: the owner trades by hand"
    if execution_mode != "demo":
        return "skip", f"execution.mode {execution_mode}: auto scalps run on the demo account only (a decision of its own)"
    tr = rec.get("trade") or {}
    plans = (rec.get("brief") or {}).get("plans") or []
    idx = tr.get("idx")
    if not isinstance(idx, int) or not 0 <= idx < len(plans):
        return "skip", f"no plan chosen: {str(tr.get('text') or '')[:80]}"
    plan = plans[idx]
    if plan.get("entry_type") not in AUTO_ENTRY_TYPES:
        return "skip", f"plan {idx + 1} is {plan.get('entry_type')}: by hand only (the code reads no candles)"
    conf = int(plan.get("confidence") or 0)
    if conf < int(settings.get("min_confidence") or 0):
        return "skip", f"plan {idx + 1} confidence {conf} < min {int(settings.get('min_confidence') or 0)}"
    if kill:
        return "skip", "kill switch on"
    if not market_open:
        return "skip", "market closed"
    if open_now:
        return "skip", f"{open_now} scalp order(s) / position(s) still open: one at a time"
    if not isinstance(equity, (int, float)) or equity <= 0:
        return "skip", "equity unknown (the executor row is stale)"
    risk = risk_usd(plan, settings["lot"], cfg.contract_size)
    cap = float(equity) * float(cfg.auto_risk_cap_pct) / 100
    if risk > cap + 1e-9:
        return "skip", (f"plan {idx + 1} risk {risk:.2f} USD > cap {cap:.2f} USD ({cfg.auto_risk_cap_pct:g} % of "
                        f"equity {equity:.2f}) at {settings['lot']:g} lots")
    return "place", (f"plan {idx + 1} {plan['side']} {plan['entry_type']} @ {plan['entry']} sl {plan['stop']} "
                     f"tp {plan['target']} {settings['lot']:g} lots, risk {risk:.2f} USD, confidence {conf}")


def _comment_tag(brief_id: str) -> str:
    return f"ts:{COMMENT_PREFIX}{brief_id}"


def brief_id_of(comment: str | None) -> str | None:
    """The brief id an order's comment carries (``ts:sc<id>:1``), None for anything else."""
    c = str(comment or "")
    if not c.startswith(f"ts:{COMMENT_PREFIX}"):
        return None
    body = c[len(f"ts:{COMMENT_PREFIX}"):]
    return body.split(":", 1)[0] or None


# ------------------------------------------------------------------------------------------------- MT5
def attach(s: Settings, symbol: str):
    """``(terminal, backend)`` on the RUNNING execution terminal (never launched), with the scalp magic; the desk's
    family so the backend knows the desk's positions as siblings. Raises ``MT5Unavailable``."""
    from ..execution.backends.mt5_backend import MT5Backend
    from ..execution.executor import magics
    from ..ingest.mt5.terminal import NO_LAUNCH_ENV, NO_LAUNCH_MIN_AGE_S, MT5Terminal, MT5Unavailable, terminal_running
    pname = s.mt5.execution_profile_by_mode.get(s.execution.mode)
    if pname is None:
        raise MT5Unavailable(f"execution.mode {s.execution.mode} has no MT5 profile")
    prof = s.mt5.profiles[pname]
    if not terminal_running(prof.terminal_path, min_age_s=NO_LAUNCH_MIN_AGE_S):
        raise MT5Unavailable("MT5 terminal is not running: no scalp order")
    os.environ[NO_LAUNCH_ENV] = "1"
    term = MT5Terminal(prof)
    term.connect()
    try:
        own, base, family = magics(s)
        be = MT5Backend(term, scalp_magic(s), expected_account_type=prof.account_type,
                        pair_by_symbol={symbol: s.scalp.pair}, own_pairs={s.scalp.pair}, adopt_magic=None,
                        family=family)
        be.assert_account()
    except Exception:
        term.shutdown()
        raise
    return term, be


def _notify(s: Settings, level: str, title: str, text: str, key: str) -> None:
    if s.scalp.notify:
        notify(s, level, title, text, key=key, pair=s.scalp.pair)


# ------------------------------------------------------------------------------------------------- the open trade
def _live(be, entries: list[dict]) -> tuple[list, list]:
    m = be.mt5
    orders = [o for o in (m.orders_get() or ()) if be.mine(o)]
    positions = [p for p in (m.positions_get() or ()) if be.mine(p)]
    return orders, positions


def _find(e: dict, orders: list, positions: list):
    tagc = _comment_tag(str(e.get("brief_id")))
    pos = next((p for p in positions if int(getattr(p, "ticket", -1)) == int(e["ticket"])
                or str(getattr(p, "comment", "") or "").startswith(tagc)), None)
    order = next((o for o in orders if int(getattr(o, "ticket", -1)) == int(e["ticket"])), None)
    return pos, order


def _in_profit(side: str, entry: float, bid: float, ask: float) -> bool:
    spread = max(0.0, float(ask) - float(bid))
    return (float(bid) > float(entry) + spread) if side == "BUY" else (float(ask) < float(entry) - spread)


def open_snapshot(s: Settings, symbol: str, *, now: int | None = None,
                  attach_fn: Callable[[Settings, str], tuple[Any, Any]] | None = None) -> dict | None:
    """D-063: the tracked auto-trade as it is at the broker now (for the prompt): a ``position`` with its floating
    P&L, minutes held and whether it is in profit beyond the spread; a ``pending`` order; ``{"kind": None}`` when the
    tracked item is gone; None when nothing is tracked (no attach) or the terminal is away."""
    state = read_state(s)
    if not state["open"]:
        return None
    now = int(now if now is not None else now_ms())
    try:
        term, be = (attach_fn or attach)(s, symbol)
    except Exception as exc:  # noqa: BLE001
        log.warning("scalp auto snapshot: %s", f"{type(exc).__name__}: {str(exc)[:200]}")
        return None
    try:
        orders, positions = _live(be, state["open"])
        q = be.mt5.symbol_info_tick(symbol)
        bid, ask = (float(q.bid), float(q.ask)) if q is not None else (None, None)
        for e in state["open"]:
            pos, order = _find(e, orders, positions)
            if pos is not None:
                filled = e.get("filled_ms")
                if not filled:
                    try:
                        filled = be.model.server_to_utc(int(getattr(pos, "time_msc", 0) or 0), prefer="earlier")
                    except Exception:  # noqa: BLE001
                        filled = now
                side = str(e.get("side"))
                return {"kind": "position", "brief_id": e.get("brief_id"), "ticket": int(pos.ticket), "side": side,
                        "entry": float(pos.price_open), "sl": float(pos.sl or 0) or None, "tp": float(pos.tp or 0) or None,
                        "lot": float(pos.volume), "profit_usd": round(float(pos.profit) + float(getattr(pos, "swap", 0) or 0), 2),
                        "price": (bid if side == "BUY" else ask), "held_min": max(0.0, (now - int(filled)) / MS_PER_MINUTE),
                        "max_hold_min": int(e.get("max_hold_min") or 0), "filled_ms": int(filled),
                        "in_profit": bool(bid is not None and _in_profit(side, float(pos.price_open), bid, ask))}
            if order is not None:
                return {"kind": "pending", "brief_id": e.get("brief_id"), "ticket": int(order.ticket), "side": e.get("side"),
                        "order_type": f"{e.get('side')}_{'LIMIT' if 'LIMIT' in str(getattr(order, 'type', '')) else ''}".rstrip("_")
                        if False else e.get("order_type") or f"{e.get('side')}_PENDING",
                        "entry": float(order.price_open), "sl": float(order.sl or 0) or None, "tp": float(order.tp or 0) or None,
                        "lot": float(getattr(order, "volume_current", e.get("lot") or 0)), "expires_ms": e.get("expires_ms")}
        return {"kind": None}
    except Exception as exc:  # noqa: BLE001
        log.warning("scalp auto snapshot failed: %s", f"{type(exc).__name__}: {str(exc)[:200]}")
        return None
    finally:
        try:
            term.shutdown()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------------------------------------- the brief acts
def execute(s: Settings, rec: dict, *, symbol: str, equity: float | None, market_open: bool, now: int | None = None,
            attach_fn: Callable[[Settings, str], tuple[Any, Any]] | None = None) -> dict:
    """After the brief is stored (mode auto): (1) D-063 a tracked PENDING order that has not triggered is cancelled
    (replaced by this brief's trade, or just cancelled); (2) a tracked POSITION gets the brief's ``manage_open``:
    close / breakeven (when in profit) / hold; (3) THE trade is placed when every gate passes (one at a time: a held
    position blocks it). Returns the execution record (the caller writes it into the brief record); ``decision`` =
    analyze | placed | skipped | failed; ``managed`` = the management events."""
    cfg = s.scalp
    now = int(now if now is not None else now_ms())
    st = read_settings(s)
    state = read_state(s)
    ex: dict[str, Any] = {"ts": now, "mode": st["mode"], "lot": st["lot"], "min_confidence": st["min_confidence"],
                          "decision": "analyze", "why": "mode analyze: the owner trades by hand", "managed": []}
    if st["mode"] != "auto":
        return ex
    brief = rec.get("brief") or {}
    tr = rec.get("trade") or {}
    has_plan = isinstance(tr.get("idx"), int)
    term = be = None
    if state["open"] or has_plan:
        try:
            term, be = (attach_fn or attach)(s, symbol)
        except Exception as exc:  # noqa: BLE001 - the terminal is not there: the brief stands, nothing happens
            ex.update(decision="failed", why=f"MT5: {type(exc).__name__}: {str(exc)[:200]}")
            log.warning("scalp auto: %s", ex["why"])
            log_event(s, {"event": "failed", "brief_id": rec.get("id"), **{k: v for k, v in ex.items() if k != "managed"}})
            _notify(s, "warn", "Scalp AUTO: nothing done", ex["why"], key=f"scalp:auto:{rec.get('id')}:failed")
            return ex
    try:
        # (1) + (2): what the previous briefs left
        keep: list[dict] = []
        if state["open"]:
            orders, positions = _live(be, state["open"])
            for e in state["open"]:
                pos, order = _find(e, orders, positions)
                if order is not None and pos is None:
                    r = be.cancel_order(int(order.ticket))
                    ev = {"event": "replaced" if has_plan else "cancelled", "brief_id": e.get("brief_id"), "ticket": e.get("ticket"),
                          "side": e.get("side"), "entry": e.get("entry"), "stop": e.get("stop"), "target": e.get("target"),
                          "lot": e.get("lot"), "ts": now, "ok": bool(r.get("ok")), "why": ("by brief " + str(rec.get("id")))
                          + ("" if r.get("ok") else f" - cancel failed: {r.get('meaning')}")}
                    ex["managed"].append(ev)
                    log_event(s, ev)
                    if r.get("ok"):
                        _notify(s, "info", f"Scalp AUTO: pending {e.get('side')} @ {e.get('entry')} {ev['event']}",
                                f"brief {rec.get('id')}: " + ("the new trade follows" if has_plan else "no trade now"),
                                key=f"scalp:auto:{e.get('brief_id')}:{ev['event']}")
                    else:
                        keep.append(e)
                        _notify(s, "warn", "Scalp AUTO: cancel failed", f"{e.get('brief_id')}: {r.get('meaning')}",
                                key=f"scalp:auto:{e.get('brief_id')}:cancel_failed")
                elif pos is not None:
                    e = {**e, "stage": "position", "position_ticket": int(pos.ticket),
                         "filled_ms": e.get("filled_ms") or now}
                    decision = str(brief.get("manage_open") or "none")
                    reason = str(brief.get("manage_reason") or "")[:300]
                    q = be.mt5.symbol_info_tick(symbol)
                    bid, ask = (float(q.bid), float(q.ask)) if q is not None else (None, None)
                    pnl = round(float(pos.profit) + float(getattr(pos, "swap", 0) or 0), 2)
                    base_ev = {"brief_id": e.get("brief_id"), "ticket": e.get("ticket"), "side": e.get("side"),
                               "entry": float(pos.price_open), "stop": float(pos.sl or 0) or None, "target": float(pos.tp or 0) or None,
                               "lot": float(pos.volume), "ts": now, "profit_usd": pnl, "by_brief": rec.get("id"), "reason": reason}
                    if decision == "close":
                        r = be.close_position(int(pos.ticket))
                        ev = {**base_ev, "event": "managed_close", "ok": bool(r.get("ok")), "why": reason or "the brief closed it",
                              "close": r.get("price")}
                        if not r.get("ok"):
                            ev["why"] = f"close failed: {r.get('meaning')}"
                            keep.append(e)
                        ex["managed"].append(ev)
                        log_event(s, ev)
                        _notify(s, "info" if r.get("ok") else "warn",
                                f"Scalp AUTO: {e.get('side')} closed by the brief ({pnl:+.2f} USD)" if r.get("ok") else "Scalp AUTO: close failed",
                                ev["why"], key=f"scalp:auto:{e.get('brief_id')}:managed_close")
                    elif decision == "breakeven":
                        side = str(e.get("side"))
                        if bid is not None and _in_profit(side, float(pos.price_open), bid, ask) and not e.get("breakeven_ms"):
                            r = be.modify_sl(int(pos.ticket), symbol, float(pos.price_open), float(pos.tp or 0) or None)
                            ok = bool(r.get("ok")) or r.get("status") == "applied"
                            ev = {**base_ev, "event": "managed_breakeven", "ok": ok,
                                  "why": reason or "stop moved to the entry" if ok else f"breakeven {r.get('status')}: {r.get('meaning')}"}
                            if ok:
                                e = {**e, "breakeven_ms": now, "stop": float(pos.price_open)}
                            _notify(s, "info" if ok else "warn", f"Scalp AUTO: {side} stop to breakeven" if ok else "Scalp AUTO: breakeven not applied",
                                    ev["why"], key=f"scalp:auto:{e.get('brief_id')}:breakeven")
                        else:
                            ev = {**base_ev, "event": "managed_hold", "ok": True,
                                  "why": ("breakeven already done; " if e.get("breakeven_ms") else "breakeven refused: not in profit beyond the spread; ") + reason}
                            _notify(s, "info", f"Scalp AUTO: holding {side} ({pnl:+.2f} USD)", ev["why"], key=f"scalp:auto:{e.get('brief_id')}:hold:{rec.get('id')}")
                        keep.append(e)
                        ex["managed"].append(ev)
                        log_event(s, ev)
                    else:
                        ev = {**base_ev, "event": "managed_hold", "ok": True, "why": reason or ("hold" if decision == "hold" else "no decision (none)")}
                        keep.append(e)
                        ex["managed"].append(ev)
                        log_event(s, ev)
                        _notify(s, "info", f"Scalp AUTO: holding {e.get('side')} ({pnl:+.2f} USD, {max(0.0, (now - int(e['filled_ms'])) / MS_PER_MINUTE):.0f} min)",
                                ev["why"], key=f"scalp:auto:{e.get('brief_id')}:hold:{rec.get('id')}")
                else:
                    ev = {**e, "event": "settled", "stage": "settled", "settled_ms": now,
                          "reason": "expired" if e.get("stage") == "pending" and now >= int(e.get("expires_ms") or 0) else
                          "closed" if e.get("stage") == "position" else "gone"}
                    ex["managed"].append({"event": "settled", "brief_id": e.get("brief_id"), "reason": ev["reason"], "ts": now})
                    log_event(s, ev)
            write_state(s, {"open": keep}, now)
            state = {"open": keep}
        # (3) THE trade
        kill = kill_switch_on(s, None) or kill_switch_on(s, cfg.pair)
        kind, why = auto_gate(settings=st, rec=rec, equity=equity, cfg=cfg, kill=kill, market_open=market_open,
                              open_now=len(state["open"]), execution_mode=s.execution.mode)
        ex.update(decision="skipped" if kind != "place" else "pending", why=why)
        if kind != "place":
            log.info("scalp auto: no order - %s", why)
            log_event(s, {"event": "refused", "brief_id": rec.get("id"), **{k: v for k, v in ex.items() if k != "managed"}})
            _notify(s, "info", "Scalp AUTO: no order", why[:300], key=f"scalp:auto:{rec.get('id')}:refused")
            return ex
        idx = int(tr["idx"])
        plan = brief["plans"][idx]
        orec = order_rec(plan, as_of=int(rec["as_of_ms"]), window_min=cfg.trigger_window_min)
        expires = int(rec["as_of_ms"]) + cfg.trigger_window_min * MS_PER_MINUTE
        try:
            res = be.place(decision_id=f"{COMMENT_PREFIX}{rec['id']}", symbol=symbol, rec=orec, lots=float(st["lot"]),
                           entry=float(plan["entry"]), dry_run=False)
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
        ex["backend"] = {k: v for k, v in res.items() if k in ("ok", "reason", "placed", "note", "attempts")}
        if res.get("ok"):
            placed = (res.get("placed") or [{}])[0]
            ticket = placed.get("order")
            ex.update(decision="placed", ticket=ticket, order_type=orec["order_type"], side=plan["side"],
                      entry=float(plan["entry"]), stop=float(plan["stop"]), target=float(plan["target"]),
                      expires_ms=expires, max_hold_min=int(plan["max_hold_min"]), plan_idx=idx)
            state["open"].append({"ticket": ticket, "brief_id": rec["id"], "idx": idx, "side": plan["side"],
                                  "symbol": symbol, "entry": float(plan["entry"]), "stop": float(plan["stop"]),
                                  "target": float(plan["target"]), "lot": float(st["lot"]), "placed_ms": now,
                                  "expires_ms": expires, "max_hold_min": int(plan["max_hold_min"]), "stage": "pending",
                                  "filled_ms": None, "order_type": orec["order_type"]})
            write_state(s, state, now)
            log.info("scalp auto: %s %s @ %s placed (ticket %s): %s", orec["order_type"], st["lot"], plan["entry"], ticket, why)
            log_event(s, {"event": "placed", "brief_id": rec["id"], **{k: v for k, v in ex.items() if k != "managed"}})
            _notify(s, "info", f"Scalp AUTO: {orec['order_type']} {st['lot']:g} @ {plan['entry']} placed",
                    f"SL {plan['stop']} / TP {plan['target']} · risk {risk_usd(plan, st['lot'], cfg.contract_size):.2f} USD "
                    f"· expires {iso(expires)[11:16]} UTC · closed after {plan['max_hold_min']} min · ticket {ticket}",
                    key=f"scalp:auto:{rec['id']}:placed")
        else:
            ex.update(decision="failed", why=str(res.get("reason") or "order refused")[:300])
            log.warning("scalp auto: order not placed - %s", ex["why"])
            log_event(s, {"event": "failed", "brief_id": rec["id"], **{k: v for k, v in ex.items() if k != "managed"}})
            _notify(s, "warn", "Scalp AUTO: order NOT placed", ex["why"], key=f"scalp:auto:{rec['id']}:failed")
        return ex
    finally:
        if term is not None:
            try:
                term.shutdown()
            except Exception:  # noqa: BLE001
                pass


# ------------------------------------------------------------------------------------------------- managing
def manage_decisions(open_entries: list[dict], *, orders: list, positions: list, now: int,
                     filled_ms: Callable[[Any], int | None]) -> tuple[list[dict], list[dict], list[dict]]:
    """Pure: ``(keep, close, settled)`` for the tracked entries against the broker's live orders / positions (the
    backend's own, by ticket or comment). ``close`` = positions past their hold time; ``settled`` = gone."""
    keep, close, settled = [], [], []
    for e in open_entries:
        tagc = _comment_tag(str(e.get("brief_id")))
        pos = next((p for p in positions if int(getattr(p, "ticket", -1)) == int(e["ticket"])
                    or str(getattr(p, "comment", "") or "").startswith(tagc)), None)
        order = next((o for o in orders if int(getattr(o, "ticket", -1)) == int(e["ticket"])), None)
        if pos is not None:
            e = {**e, "stage": "position", "position_ticket": int(pos.ticket),
                 "filled_ms": e.get("filled_ms") or filled_ms(pos) or now}
            if now - int(e["filled_ms"]) >= int(e["max_hold_min"]) * MS_PER_MINUTE:
                close.append(e)
            else:
                keep.append(e)
        elif order is not None:
            keep.append({**e, "stage": "pending"})
        else:
            reason = "expired" if e.get("stage") == "pending" and now >= int(e.get("expires_ms") or 0) else \
                "closed" if e.get("stage") == "position" else "gone"
            settled.append({**e, "stage": "settled", "settled_ms": now, "reason": reason})
    return keep, close, settled


def manage(s: Settings, *, now: int | None = None, attach_fn: Callable[[Settings, str], tuple[Any, Any]] | None = None) -> dict:
    """The loop's minute tick (and ``--manage``): nothing to do while nothing is tracked; else one attach, the
    decisions, the closes, the state. Never raises."""
    state = read_state(s)
    if not state["open"]:
        return {"tracked": 0}
    now = int(now if now is not None else now_ms())
    symbol = str(state["open"][0].get("symbol") or "")
    try:
        term, be = (attach_fn or attach)(s, symbol)
    except Exception as exc:  # noqa: BLE001 - the terminal is away: try again next minute
        log.warning("scalp auto manage: %s", f"{type(exc).__name__}: {str(exc)[:200]}")
        return {"tracked": len(state["open"]), "error": str(exc)[:200]}
    out = {"tracked": len(state["open"]), "closed": 0, "settled": 0, "errors": 0}
    try:
        m = be.mt5
        orders = [o for o in (m.orders_get() or ()) if be.mine(o)]
        positions = [p for p in (m.positions_get() or ()) if be.mine(p)]

        def filled_ms(pos) -> int | None:
            try:
                return be.model.server_to_utc(int(getattr(pos, "time_msc", 0) or 0), prefer="earlier") or None
            except Exception:  # noqa: BLE001
                return None
        keep, close, settled = manage_decisions(state["open"], orders=orders, positions=positions, now=now,
                                                filled_ms=filled_ms)
        for e in close:
            r = be.close_position(int(e["position_ticket"]))
            if r.get("ok"):
                out["closed"] += 1
                ev = {**e, "stage": "settled", "settled_ms": now, "reason": "hold_time", "close": r.get("price")}
                log.info("scalp auto: position %s closed after %d min (%s)", e["position_ticket"], e["max_hold_min"], e["brief_id"])
                log_event(s, {"event": "closed_hold", **ev})
                _notify(s, "info", f"Scalp AUTO: {e['side']} closed after {e['max_hold_min']} min",
                        f"brief {e['brief_id']} · entry {e['entry']} · at {r.get('price')}", key=f"scalp:auto:{e['brief_id']}:closed")
            else:
                out["errors"] += 1
                keep.append(e)
                log.warning("scalp auto: close of %s failed: %s", e["position_ticket"], r.get("meaning"))
                _notify(s, "warn", "Scalp AUTO: close failed", f"{e['brief_id']}: {r.get('meaning')}",
                        key=f"scalp:auto:{e['brief_id']}:close_failed")
        for e in settled:
            out["settled"] += 1
            log.info("scalp auto: %s %s (%s)", e["brief_id"], e["reason"], e.get("ticket"))
            log_event(s, {"event": "settled", **e})
            _notify(s, "info", f"Scalp AUTO: {e['side']} {e['reason']}",
                    f"brief {e['brief_id']} · entry {e['entry']} · ticket {e.get('ticket')}", key=f"scalp:auto:{e['brief_id']}:{e['reason']}")
        write_state(s, {"open": keep}, now)
    except Exception as exc:  # noqa: BLE001
        out["errors"] += 1
        log.exception("scalp auto manage failed")
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        try:
            term.shutdown()
        except Exception:  # noqa: BLE001
            pass
    return out


__all__ = ["MODES", "AUTO_ENTRY_TYPES", "scalp_magic", "default_settings", "validate_settings", "read_settings",
           "write_settings", "schedule", "execution_text", "open_trade_text", "open_snapshot", "read_state", "write_state",
           "log_event", "read_log", "risk_usd", "order_rec", "auto_gate", "brief_id_of", "attach", "execute",
           "manage_decisions", "manage"]
