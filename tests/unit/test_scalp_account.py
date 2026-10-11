"""D-058: the desk's account as the scalp job reads it (scalp/account.py) — a read-only replica of
``Engine.live_account`` from the executor's status row: fresh vs stale (``EXECUTOR_FRESH_MS``), the same keys, the
desk's legs on the pair only, the compact desk block for the prompt, and a KV view that never writes."""
import inspect
import json
import sqlite3
from pathlib import Path

import pytest

from tradingsystem.analysis.engine import EXECUTOR_FRESH_MS as ENGINE_FRESH_MS, Engine
from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import PathsCfg, load_settings
from tradingsystem.scalp.account import (EXECUTOR_FRESH_MS, ReadOnlyKV, default_account, desk_account, desk_block,
                                         executor_row, ro_connect)

NOW = 1_760_000_000_000
DDL = ("CREATE TABLE IF NOT EXISTS collector_status (collector TEXT PRIMARY KEY, state TEXT NOT NULL, last_data_ms "
       "INTEGER, last_error TEXT, last_error_ms INTEGER, detail TEXT, updated_ms INTEGER NOT NULL)")
EXPOSURE = [{"pair": "XAUUSD", "kind": "position", "side": "SELL", "price": 4196.1, "sl": 4201.0, "tp": 4186.0,
             "volume": 0.01, "profit_usd": -1.2, "since_ms": NOW - 600_000, "sl_missing": False},
            {"pair": "XAUUSD", "kind": "order", "side": "BUY", "order_type": "BUY_LIMIT", "price": 4180.0, "sl": 4175.0,
             "tp": 4190.0, "volume": 0.01, "expires_ms": NOW + 3_600_000},
            {"pair": "BTCUSDT", "kind": "position", "side": "BUY", "price": 60000.0, "sl": 59000.0, "tp": 62000.0}]
DETAIL = {"mode": "demo", "currency": "USD", "equity": 101.5, "balance": 100.0, "today_pnl_pct": 1.5,
          "kill_switch": False, "account_drawdown": {"drawdown_pct": 3.2, "tripped": None}, "exposure": EXPOSURE}


@pytest.fixture
def s(tmp_path):
    base = load_settings(env_path=Path("nope.env"), extra_env={"EXECUTION_MODE": "paper"})
    return base.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"))})


def seeded(tmp_path, *, updated_ms=NOW - 30_000, state="live", detail=DETAIL) -> Path:
    db = tmp_path / "app.db"
    with sqlite3.connect(db) as con:
        con.execute(DDL)
        con.execute("INSERT INTO collector_status VALUES ('executor', ?, NULL, NULL, NULL, ?, ?)",
                    (state, json.dumps(detail), updated_ms))
        con.execute("INSERT INTO collector_status VALUES ('engine', 'live', NULL, NULL, NULL, '{}', ?)", (updated_ms,))
    return db


def test_fresh_row_gives_the_live_account_with_the_desks_legs_on_the_pair(s, tmp_path):
    reg = InstrumentRegistry.from_settings(s)
    with ro_connect(seeded(tmp_path)) as con:
        acct = desk_account(s, reg, con, "XAUUSD", NOW)
    assert acct["mode"] == "demo" and acct["equity"] == 101.5 and acct["balance"] == 100.0
    assert acct["equity_source"] == "live demo account, reported by the executor 30s ago"
    assert acct["today_pnl_pct"] == 1.5 and acct["daily_loss_limit_pct"] == s.risk.max_daily_loss_pct
    assert acct["account_drawdown_pct"] == 3.2 and acct["account_drawdown_stop_pct"] == s.risk.account_drawdown_stop_pct
    assert "account_drawdown_tripped" not in acct and acct["kill_switch"] is False
    assert acct["holdings_price_space"] == reg.with_role("XAUUSD", "execution")[0].key
    assert acct["open_positions"] == [{"side": "SELL", "price": 4196.1, "sl": 4201.0, "tp": 4186.0, "volume": 0.01,
                                       "profit_usd": -1.2, "since_ms": NOW - 600_000}]     # sl_missing False dropped
    assert acct["pending_orders"] == [{"side": "BUY", "order_type": "BUY_LIMIT", "price": 4180.0, "sl": 4175.0,
                                       "tp": 4190.0, "volume": 0.01, "expires_ms": NOW + 3_600_000}]   # no profit_usd
    # the keys are Engine.live_account's (the payload the desk's model reads) plus the kill switch
    src = inspect.getsource(Engine.live_account)
    for k in set(acct) - {"kill_switch"}:
        assert f'"{k}"' in src, k
    assert EXECUTOR_FRESH_MS == ENGINE_FRESH_MS == 120_000


@pytest.mark.parametrize("age_ms, state, why", [
    (EXECUTOR_FRESH_MS + 1, "live", "the executor is not reporting"),
    (30_000, "stopped", "the executor is stopped"),
    (30_000, "market_closed", "the executor is market_closed"),
])
def test_stale_or_not_live_row_falls_back_to_the_configured_size(s, tmp_path, age_ms, state, why):
    reg = InstrumentRegistry.from_settings(s)
    with ro_connect(seeded(tmp_path, updated_ms=NOW - age_ms, state=state)) as con:
        acct = desk_account(s, reg, con, "XAUUSD", NOW)
    assert acct == {**default_account(s), "equity_source": f"configured account size ({why}); positions unknown"}
    assert acct["equity"] == s.execution.paper_equity and "open_positions" not in acct
    assert desk_block(acct)["legs"] == [] and desk_block(acct)["sides"] == []


def test_no_row_no_table_or_a_torn_detail(s, tmp_path):
    reg = InstrumentRegistry.from_settings(s)
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with ro_connect(empty) as con:
        assert executor_row(con) == ({}, None, None)                                     # no table
        acct = desk_account(s, reg, con, "XAUUSD", NOW)
        assert "configured account size (the executor is not reporting)" in acct["equity_source"]
    db = seeded(tmp_path, detail=DETAIL)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE collector_status SET detail='{\"equity\": 1' WHERE collector='executor'")
    with ro_connect(db) as con:
        assert executor_row(con) == ({}, NOW - 30_000, "live")
        assert "positions unknown" in desk_account(s, reg, con, "XAUUSD", NOW)["equity_source"]
    (tmp_path / "b").mkdir()
    with ro_connect(seeded(tmp_path / "b", detail={**DETAIL, "equity": 0})) as con:
        assert "positions unknown" in desk_account(s, reg, con, "XAUUSD", NOW)["equity_source"]   # no equity: no report


def test_desk_block_is_the_compact_prompt_view(s, tmp_path):
    reg = InstrumentRegistry.from_settings(s)
    with ro_connect(seeded(tmp_path)) as con:
        blk = desk_block(desk_account(s, reg, con, "XAUUSD", NOW))
    assert blk["equity"] == 101.5 and blk["currency"] == "USD" and blk["mode"] == "demo"
    assert blk["drawdown_pct"] == 3.2 and blk["kill_switch"] is False and blk["today_pnl_pct"] == 1.5
    assert blk["legs"] == [{"kind": "position", "side": "SELL", "entry": 4196.1, "sl": 4201.0, "tp": 4186.0,
                            "volume": 0.01, "profit_usd": -1.2, "since_ms": NOW - 600_000},
                           {"kind": "order", "side": "BUY", "order_type": "BUY_LIMIT", "entry": 4180.0, "sl": 4175.0,
                            "tp": 4190.0, "volume": 0.01, "expires_ms": NOW + 3_600_000}]
    assert blk["sides"] == ["BUY", "SELL"]                                   # what checks.desk_ok reads
    assert "does NOT see manual trades" in blk["note"]


def test_read_only_kv_reads_the_engines_cache_and_never_writes(tmp_path):
    db = tmp_path / "app.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE engine_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_ms INTEGER NOT NULL)")
        con.execute("INSERT INTO engine_kv VALUES ('sessions:XAUUSD', ?, 1)", (json.dumps({"days": [1, 2]}),))
        con.execute("INSERT INTO engine_kv VALUES ('bad', 'not json', 1)")
    with ro_connect(db) as con:
        kv = ReadOnlyKV(con)
        assert kv.kv_get("sessions:XAUUSD") == {"days": [1, 2]}
        assert kv.kv_get("missing", "dflt") == "dflt" and kv.kv_get("bad", 7) == 7
        assert kv.kv_set("sessions:XAUUSD", {"days": []}) is None
        assert kv.kv_get("sessions:XAUUSD") == {"days": [1, 2]}                   # unchanged
        with pytest.raises(sqlite3.OperationalError):                               # the connection itself is read-only
            con.execute("INSERT INTO engine_kv VALUES ('x', '1', 1)")
        # the daily-profile worker thread reads the same kv: a thread-bound connection answers with the default
        import threading
        got = []
        t = threading.Thread(target=lambda: got.append(kv.kv_get("sessions:XAUUSD", "dflt")))
        t.start()
        t.join(5)
        assert got == ["dflt"]
    con.close()                                                                       # as the brief does
    assert kv.kv_get("sessions:XAUUSD", "closed") == "closed"
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM engine_kv").fetchone()[0] == 2
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with ro_connect(empty) as con:
        assert ReadOnlyKV(con).kv_get("anything", None) is None                      # no table yet
