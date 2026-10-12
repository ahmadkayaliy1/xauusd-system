"""tools/live_report.py (D-057 / D-059): the daily live report, its summary and the --telegram notification."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

from tradingsystem.ai.store import DecisionStore
from tradingsystem.core import notify as nt
from tradingsystem.execution.management import ActionLog

ROOT = Path(__file__).resolve().parents[2]
A = "a1b2c3d4000000000000000000000000"


def tool(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def ms(h: int, m: int, s: int = 0) -> int:
    return int(dt.datetime(2026, 10, 7, h, m, s, tzinfo=dt.timezone.utc).timestamp() * 1000)


@pytest.fixture
def db(tmp_path) -> Path:
    path = tmp_path / "app.db"
    st = DecisionStore(path)
    log = ActionLog(path)
    con = st._con
    ex = {"executed_levels": {"entry": 4133.59, "stop_loss": 4143.6, "take_profits": [4113.44]},
          "backend": {"ok": True, "placed": [{"price": 4132.57, "volume": 0.01}]}}
    od = {"venue": "mt5", "open_ms": ms(5, 17, 26), "close_ms": ms(7, 32, 57),
          "legs": [{"leg": "113691689", "tp_index": 1, "filled": True, "reason": "sl"}]}
    con.execute("INSERT INTO ai_decisions (id, ts, pair, mode, status, decision, order_type, execution_state, "
                "execution_detail, outcome, outcome_ts, outcome_pnl_usd, outcome_detail, latency_ms) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (A, ms(5, 17), "XAUUSD", "demo", "valid", "SELL", "MARKET", "executed", json.dumps(ex),
                 "closed_loss", ms(7, 33, 40), -5.63, json.dumps(od), 42_000))
    con.execute("INSERT INTO ai_decisions (id, ts, pair, mode, status, decision, execution_state) VALUES "
                "(?,?,?,?,?,?,?)", ("b" * 32, ms(9, 0), "XAUUSD", "demo", "valid", "NO_TRADE", "not_executed"))
    con.execute("INSERT INTO ai_decisions (id, ts, pair, mode, status, decision, order_type, execution_state, "
                "execution_detail) VALUES (?,?,?,?,?,?,?,?,?)",
                ("c" * 32, ms(11, 5), "XAUUSD", "demo", "valid", "BUY", "LIMIT", "rejected",
                 json.dumps({"gate": [{"check": "max_open_positions", "ok": False, "detail": "1 open"}]})))
    con.execute("CREATE TABLE IF NOT EXISTS collector_status (collector TEXT PRIMARY KEY, state TEXT, "
                "last_data_ms INTEGER, last_error TEXT, last_error_ms INTEGER, detail TEXT, updated_ms INTEGER)")
    con.execute("INSERT OR REPLACE INTO collector_status (collector, state, detail, updated_ms) VALUES (?,?,?,?)",
                ("executor", "live", json.dumps({"equity": 94.37, "open_positions": 0, "open_orders": 0}), ms(12, 0)))
    con.commit()
    log.close()
    st.close()
    return path


def test_the_facts_the_markdown_and_the_summary(db):
    lr = tool("live_report")
    facts = lr.collect(db, dt.date(2026, 10, 7))
    assert len(facts["trades"]) == 1 and len(facts["closed"]) == 1 and facts["pnl"] == -5.63 and facts["wins"] == 0
    assert len(facts["refused"]) == 1 and len(facts["calls"]) == 3 and facts["ex"]["equity"] == 94.37
    md = lr.render(facts)
    assert md.startswith("# تقرير الحي اليومي - الدهب XAUUSD - 2026-10-07") and "| 05:17 | بيع" in md
    assert "max_open_positions (1 open)" in md and "عدد الأسئلة: **3**" in md
    assert lr.report(db, dt.date(2026, 10, 7)) == md
    text = lr.brief(facts, Path("data/reviews/x.md"))
    lines = text.splitlines()
    assert lines[0] == "تقرير الدهب اليومي - 2026-10-07"
    assert lines[1] == "الصفقات: 1 (تسكّرت 1، ربحت 0، خسرت 1) - النتيجة: -5.63 $"
    assert lines[2] == "الرصيد: 94.37 $ (مفتوح 0، معلّق 0)"
    assert lines[4].startswith("- 05:17 بيع ") and "الوقف 07:3" in lines[4] and "-5.63 $" in lines[4]
    assert lines[5] == "رفض نظام المخاطر: 1 فكرة"
    assert lines[6].startswith("الأسئلة: 3 (أخطاء 0)") and lines[-1].endswith("x.md")
    assert len(text) < nt.TOAST_TEXT_MAX
    # an empty day
    empty = lr.collect(db, dt.date(2026, 10, 8))
    assert lr.brief(empty).splitlines()[1] == "الصفقات: 0 (تسكّرت 0، ربحت 0، خسرت 0) - النتيجة: +0.00 $"
    assert "ما في صفقات اليوم." in lr.render(empty)


def test_telegram_sends_the_summary_through_core_notify(db, tmp_path, monkeypatch, capsys):
    """The suite's TS_NOTIFY_DISABLE keeps it a log line; the result line and the key are what the monitor relies on."""
    lr = tool("live_report")
    sent: list[tuple] = []
    monkeypatch.setattr(nt, "notify", lambda s, level, title, text, *, key=None, pair=None: sent.append(
        (level, title, text, key, pair)))
    monkeypatch.setattr(nt, "last_result", lambda: {"toast": "shown", "telegram": "sent"})
    rc = lr.main(["--day", "2026-10-07", "--db", str(db), "--print", "--telegram"])
    out = capsys.readouterr().out
    assert rc == 0 and "notification: toast shown, telegram sent" in out
    (level, title, text, key, pair), = sent
    assert level == "info" and title == "XAUUSD daily report 2026-10-07" and key == "live_report:2026-10-07"
    assert pair == "XAUUSD" and text.startswith("تقرير الدهب اليومي - 2026-10-07") and "الملف:" not in text
