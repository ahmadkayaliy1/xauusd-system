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


# ------------------------------------------------------------------------------------------------- settings
def scalp_magic(s: Settings) -> int:
    """The scalp trades' magic: the project's base magic + ``scalp.auto_magic_offset`` (no pair folder's own)."""
    from ..execution.executor import magics
    return magics(s)[1] + s.scalp.auto_magic_offset


def default_settings(s: Settings) -> dict:
    return {"mode": "analyze", "lot": float(s.scalp.auto_default_lot), "min_confidence": int(s.scalp.auto_min_confidence),
            "updated_ms": None, "by": "default"}


def validate_settings(s: Settings, doc: dict) -> tuple[dict | None, str | None]:
    """``(clean, error)``: the three owner-set values, bounded by the config."""
    try:
        mode = str(doc.get("mode", "analyze")).strip().lower()
        raw = float(doc.get("lot", s.scalp.auto_default_lot))
        conf = int(doc.get("min_confidence", s.scalp.auto_min_confidence))
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
    return {"mode": mode, "lot": lot, "min_confidence": conf}, None


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


def write_settings(s: Settings, *, mode: str, lot: float, min_confidence: int, by: str = "dashboard",
                   now: int | None = None) -> dict:
    """Validate and store (atomic). Raises ``ValueError`` with the reason."""
    clean, err = validate_settings(s, {"mode": mode, "lot": lot, "min_confidence": min_confidence})
    if err:
        raise ValueError(err)
    doc = {**clean, "updated_ms": int(now if now is not None else now_ms()), "by": by}
    write_json_atomic(scalp_dir(s) / SETTINGS_FILE, doc)
    log.info("scalp settings: mode %s, lot %g, min confidence %d (%s)", doc["mode"], doc["lot"], doc["min_confidence"], by)
    return doc


def execution_text(s: Settings, settings: dict, equity: float | None) -> str:
    """The line the user prompt carries: the fixed lot, the risk cap in USD, and whether the trade is placed."""
    lot, oz = float(settings["lot"]), float(settings["lot"]) * float(s.scalp.contract_size)
    cap = (f"{equity * s.scalp.auto_risk_cap_pct / 100:.2f} USD ({s.scalp.auto_risk_cap_pct:g} % of equity {equity:.2f})"
           if isinstance(equity, (int, float)) and equity > 0 else f"{s.scalp.auto_risk_cap_pct:g} % of the equity")
    head = (f"The owner's lot is FIXED at {lot:g} lots = {oz:g} oz: a plan's risk in USD = |entry - stop| x {oz:g}. "
            f"Keep every plan's risk at or under {cap}.")
    if settings.get("mode") == "auto":
        return (head + f" EXECUTION IS AUTOMATIC: the plan `best_plan` is placed as a pending order at its `entry` with "
                f"its stop and target, {lot:g} lots, expiring {s.scalp.trigger_window_min} min after this brief, and the "
                f"position is closed after its `max_hold_min`. Only `limit` and `stop` entries can be automated "
                f"(a `market_after_rejection` plan stays a plan for the owner's hand); a plan under confidence "
                f"{int(settings['min_confidence'])} is not placed.")
    return head + " Execution is BY HAND: the owner reads the brief and places the order himself."


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


# ------------------------------------------------------------------------------------------------- placing
def execute(s: Settings, rec: dict, *, symbol: str, equity: float | None, market_open: bool, now: int | None = None,
            attach_fn: Callable[[Settings, str], tuple[Any, Any]] | None = None) -> dict:
    """After the brief is stored: place THE trade when every gate passes. Returns the execution record (the caller
    writes it into the brief record); ``decision`` = analyze | placed | skipped | failed."""
    cfg = s.scalp
    now = int(now if now is not None else now_ms())
    st = read_settings(s)
    state = read_state(s)
    kill = kill_switch_on(s, None) or kill_switch_on(s, cfg.pair)
    kind, why = auto_gate(settings=st, rec=rec, equity=equity, cfg=cfg, kill=kill, market_open=market_open,
                          open_now=len(state["open"]), execution_mode=s.execution.mode)
    ex: dict[str, Any] = {"ts": now, "mode": st["mode"], "lot": st["lot"], "min_confidence": st["min_confidence"],
                          "decision": "analyze" if st["mode"] != "auto" else "skipped" if kind != "place" else "pending",
                          "why": why}
    if st["mode"] != "auto":
        return ex
    if kind != "place":
        log.info("scalp auto: no order - %s", why)
        log_event(s, {"event": "refused", "brief_id": rec.get("id"), **ex})
        _notify(s, "info", "Scalp AUTO: no order", why[:300], key=f"scalp:auto:{rec.get('id')}:refused")
        return ex
    idx = int(rec["trade"]["idx"])
    plan = rec["brief"]["plans"][idx]
    orec = order_rec(plan, as_of=int(rec["as_of_ms"]), window_min=cfg.trigger_window_min)
    expires = int(rec["as_of_ms"]) + cfg.trigger_window_min * MS_PER_MINUTE
    try:
        term, be = (attach_fn or attach)(s, symbol)
    except Exception as exc:  # noqa: BLE001 - the terminal is not there: the brief stands, the order does not
        ex.update(decision="failed", why=f"MT5: {type(exc).__name__}: {str(exc)[:200]}")
        log.warning("scalp auto: %s", ex["why"])
        log_event(s, {"event": "failed", "brief_id": rec.get("id"), **ex})
        _notify(s, "warn", "Scalp AUTO: order NOT placed", ex["why"], key=f"scalp:auto:{rec.get('id')}:failed")
        return ex
    try:
        res = be.place(decision_id=f"{COMMENT_PREFIX}{rec['id']}", symbol=symbol, rec=orec, lots=float(st["lot"]),
                       entry=float(plan["entry"]), dry_run=False)
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
    finally:
        try:
            term.shutdown()
        except Exception:  # noqa: BLE001
            pass
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
                              "filled_ms": None})
        write_state(s, state, now)
        log.info("scalp auto: %s %s @ %s placed (ticket %s): %s", orec["order_type"], st["lot"], plan["entry"], ticket, why)
        log_event(s, {"event": "placed", "brief_id": rec["id"], **ex})
        _notify(s, "info", f"Scalp AUTO: {orec['order_type']} {st['lot']:g} @ {plan['entry']} placed",
                f"SL {plan['stop']} / TP {plan['target']} · risk {risk_usd(plan, st['lot'], cfg.contract_size):.2f} USD "
                f"· expires {iso(expires)[11:16]} UTC · closed after {plan['max_hold_min']} min · ticket {ticket}",
                key=f"scalp:auto:{rec['id']}:placed")
    else:
        ex.update(decision="failed", why=str(res.get("reason") or "order refused")[:300])
        log.warning("scalp auto: order not placed - %s", ex["why"])
        log_event(s, {"event": "failed", "brief_id": rec["id"], **ex})
        _notify(s, "warn", "Scalp AUTO: order NOT placed", ex["why"], key=f"scalp:auto:{rec['id']}:failed")
    return ex


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
           "write_settings", "execution_text", "read_state", "write_state", "log_event", "read_log", "risk_usd",
           "order_rec", "auto_gate", "brief_id_of", "attach", "execute", "manage_decisions", "manage"]
