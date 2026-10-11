"""The desk's live account as the scalp job sees it (D-058): a read-only replica of ``Engine.live_account`` from the
executor's status row, plus a read-only ``engine_kv`` view for the snapshot builder's caches."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..core.instruments import InstrumentRegistry
from ..core.settings import Settings

EXECUTOR_FRESH_MS = 120_000           # as analysis/engine.py: an older executor row = positions unknown


def ro_connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def default_account(s: Settings) -> dict:
    return {"equity": s.execution.paper_equity, "currency": "USD", "mode": s.execution.mode}


def executor_row(con: sqlite3.Connection) -> tuple[dict, int | None, str | None]:
    """(detail, updated_ms, state) of the executor's status row; ({}, None, None) without one."""
    try:
        r = con.execute("SELECT detail, updated_ms, state FROM collector_status WHERE collector='executor'").fetchone()
    except sqlite3.OperationalError:
        return {}, None, None
    if r is None:
        return {}, None, None
    try:
        d = json.loads(r["detail"]) if r["detail"] else {}
    except ValueError:
        d = {}
    return d, int(r["updated_ms"]) if r["updated_ms"] is not None else None, r["state"]


def desk_account(s: Settings, reg: InstrumentRegistry, con: sqlite3.Connection, pair: str, now: int) -> dict:
    """The payload's ``account`` block (same keys as ``Engine.live_account``): equity, today's P&L of the desk, the
    account drawdown and, for ``pair``, the positions and pending orders the DESK holds (its own legs only - manual
    trades are invisible to it). The configured size when the executor is not reporting."""
    base = default_account(s)
    d, updated, state = executor_row(con)
    age = now - updated if updated is not None else None
    if not d.get("equity") or age is None or age > EXECUTOR_FRESH_MS or state != "live":
        why = "the executor is not reporting" if age is None or age > EXECUTOR_FRESH_MS else f"the executor is {state}"
        return {**base, "equity_source": f"configured account size ({why}); positions unknown"}
    out: dict[str, Any] = {"mode": d.get("mode", s.execution.mode), "currency": d.get("currency", "USD"),
                           "equity": d["equity"], "balance": d.get("balance"),
                           "equity_source": f"live {d.get('mode')} account, reported by the executor {age // 1000}s ago",
                           "today_pnl_pct": d.get("today_pnl_pct"), "daily_loss_limit_pct": s.risk.max_daily_loss_pct,
                           "kill_switch": bool(d.get("kill_switch"))}
    dd = d.get("account_drawdown") or {}
    if dd:
        out["account_drawdown_pct"] = dd.get("drawdown_pct")
        out["account_drawdown_stop_pct"] = s.risk.account_drawdown_stop_pct
        if dd.get("tripped"):
            out["account_drawdown_tripped"] = dd["tripped"]
    exe = next(iter(reg.with_role(pair, "execution")), None)
    if exe is not None:
        out["holdings_price_space"] = exe.key
    rows = [{k: v for k, v in x.items() if k not in ("pair", "kind") and not (k == "sl_missing" and not v)}
            for x in d.get("exposure") or [] if x.get("pair") == pair]
    kinds = [x.get("kind") for x in d.get("exposure") or [] if x.get("pair") == pair]
    out["open_positions"] = [r for r, k in zip(rows, kinds) if k == "position"]
    out["pending_orders"] = [{k: v for k, v in r.items() if k != "profit_usd"} for r, k in zip(rows, kinds) if k == "order"]
    return out


def desk_block(acct: dict) -> dict:
    """The compact 'what the desk holds' block for the prompt and the record."""
    legs = []
    for p in acct.get("open_positions") or []:
        legs.append({"kind": "position", "side": p.get("side"), "entry": p.get("price"), "sl": p.get("sl"),
                     "tp": p.get("tp"), "volume": p.get("volume"), "profit_usd": p.get("profit_usd"),
                     "since_ms": p.get("since_ms")})
    for o in acct.get("pending_orders") or []:
        legs.append({"kind": "order", "side": o.get("side"), "order_type": o.get("order_type"), "entry": o.get("price"),
                     "sl": o.get("sl"), "tp": o.get("tp"), "volume": o.get("volume"), "expires_ms": o.get("expires_ms")})
    sides = sorted({str(x.get("side")) for x in legs if x.get("side")})
    return {"equity": acct.get("equity"), "currency": acct.get("currency", "USD"), "mode": acct.get("mode"),
            "equity_source": acct.get("equity_source"), "today_pnl_pct": acct.get("today_pnl_pct"),
            "drawdown_pct": acct.get("account_drawdown_pct"), "kill_switch": acct.get("kill_switch", False),
            "legs": legs, "sides": sides,
            "note": "the desk's model does NOT see manual trades; its weekend flat closes its own trades only"}


class ReadOnlyKV:
    """``engine_kv`` as the snapshot builder expects it (``kv_get`` / ``kv_set``), reading the engine's cache and
    never writing (the engine owns that table)."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    def kv_get(self, key: str, default: Any = None) -> Any:
        # sqlite3.Error, not only OperationalError: the daily-profile worker thread reads this kv too (a connection
        # is bound to its thread, and the brief closes it once the snapshot is built) - the default, never a raise
        try:
            r = self._con.execute("SELECT value FROM engine_kv WHERE key=?", (key,)).fetchone()
        except sqlite3.Error:
            return default
        if r is None or r["value"] is None:
            return default
        try:
            return json.loads(r["value"])
        except (ValueError, TypeError):
            return default

    def kv_set(self, key: str, value: Any) -> None:
        return None
