"""scalp/service.py (D-058): the gate matrix, the next slot, the loop (STOP ends it, never two), the dry run
(``--no-model --fixture``) through the whole store / notify path with no ledger row, and the run lock.

Nothing here calls a model, attaches to MT5 or pops a notification: the sibling modules the service loads at call
time (checks / scorer / journal) are stand-ins in ``sys.modules``, ``brief.build_inputs`` is replaced, the notifier
is recorded, every file lives under tmp_path."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from tradingsystem.core.filelock import FileLock, locks_dir
from tradingsystem.core.settings import INSTANCE_ENV, ScalpCfg, load_settings
from tradingsystem.scalp import brief as bf
from tradingsystem.scalp import service as sv
from tradingsystem.scalp.files import list_ids, read_json, scalp_dir
from tradingsystem.scalp.safety import SAFETY_TEXT

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "scalp_brief_sample.json"
T = int(dt.datetime(2026, 10, 13, 10, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)     # Tuesday 10:00 UTC
MIN = 60_000
H = 3_600_000


def settings(tmp_path: Path, **scalp):
    s = load_settings(extra_env={INSTANCE_ENV: "XAUUSD"})
    paths = s.paths.model_copy(update={"data_dir": str(tmp_path / "data"), "logs_dir": str(tmp_path / "logs")})
    return s.model_copy(update={"paths": paths, "scalp": s.scalp.model_copy(update={"enabled": True, **scalp})})


def fake_siblings(monkeypatch, *, mature=False):
    """checks / scorer / journal as the service sees them (the other builders' modules), recording the calls."""
    calls: dict[str, list] = {"check_plan": [], "stats": [], "journal_text": [], "is_mature": []}
    checks = types.ModuleType("tradingsystem.scalp.checks")
    checks.tolerance = lambda atr, med: max(0.25 * (atr or 0), 2 * (med or 0), 0.5)

    def check_plan(plan, **kw):
        calls["check_plan"].append((plan, kw))
        return {"stop_ok": True, "target_ok": True, "news_ok": True, "desk_ok": True, "level_ok": True,
                "rr_ok": plan["rr"] >= 1.5, "rejected": [], "notes": []}
    checks.check_plan = check_plan
    scorer = types.ModuleType("tradingsystem.scalp.scorer")

    def is_mature(rec, now, cfg):
        calls["is_mature"].append(rec["id"])
        return mature
    scorer.is_mature = is_mature
    scorer.read_ticks = lambda reader, exe, t0, t1: (_ for _ in ()).throw(AssertionError("no ticks in this test"))
    journal = types.ModuleType("tradingsystem.scalp.journal")

    def stats(records, n=20):
        calls["stats"].append(len(records))
        return {"n_scored": len(records)}
    journal.stats = stats

    def journal_text(records, manual, *, n_briefs, max_chars=2400):
        calls["journal_text"].append((len(records), len(manual), n_briefs))
        return "no scored brief yet"
    journal.journal_text = journal_text
    for name, mod in (("checks", checks), ("scorer", scorer), ("journal", journal)):
        monkeypatch.setitem(sys.modules, f"tradingsystem.scalp.{name}", mod)
    return calls


def fake_inputs(monkeypatch, **over):
    seen = []

    def build_inputs(s, reg, pair, as_of):
        seen.append((pair, as_of))
        return bf.Inputs(pair=pair, as_of=as_of, payload={"meta": {"payload_hash": "abc123"}},
                         payload_text='{"meta":{"pair":"XAUUSD"}}', images=[], charts_note=bf.NO_CHARTS, charts=[],
                         account={"equity": 100.36, "currency": "USD", "mode": "demo",
                                  "equity_source": "live demo account, reported by the executor 3s ago",
                                  "open_positions": [], "pending_orders": []},
                         atr5m=3.9, spread_now=0.24, spread_median=0.24, price_at=4181.2, blackout=False,
                         next_events=[["2026-10-13T12:30:00.000Z", "high", "CPI", 150]],
                         news_windows={"high": [30, 30]}, **over)
    monkeypatch.setattr(bf, "build_inputs", build_inputs)
    return seen


def record_notify(monkeypatch):
    sent = []
    monkeypatch.setattr(sv, "notify", lambda s, level, title, text, *, key=None, pair=None:
                        sent.append({"level": level, "title": title, "text": text, "key": key, "pair": pair}))
    monkeypatch.setattr(sv, "flush", lambda timeout_s=15.0: None)
    return sent


@pytest.fixture(autouse=True)
def quiet_machine(monkeypatch):
    """No real machine: the RAM probe, the gate pause, the MT5 terminal probe (the live desk's terminal runs on this
    PC) and - above all - the model call are replaced; a test that reaches the CLI fails at once."""
    monkeypatch.setattr(sv, "_free_ram_mb", lambda: 4000.0)
    monkeypatch.setattr(sv, "_sleep", lambda seconds: None)
    from tradingsystem.ingest.mt5 import terminal
    monkeypatch.setattr(terminal, "terminal_running", lambda path, min_age_s=0.0: False)

    async def no_model(*a, **kw):
        raise AssertionError("the unit suite must never call the model")
    monkeypatch.setattr(bf, "call_model", no_model)


# ------------------------------------------------------------------------------------------------- gate()
def gate(now=T, cfg=None, **kw):
    base = dict(engine_detail={"cycles_inflight": {}}, engine_age_s=5.0, executor_age_s=5.0, free_ram_mb=2000.0,
                used_today=0, market_open=True, hourly=True)
    return sv.gate(now, cfg or ScalpCfg(enabled=True), **{**base, **kw})


def test_gate_matrix():
    assert gate() == ("run", "ok")
    assert gate(cfg=ScalpCfg(enabled=False)) == ("skip", "disabled")
    assert gate(market_open=False) == ("skip", "market_closed")
    assert gate(cfg=ScalpCfg(enabled=False), market_open=False)[1] == "disabled"      # the order of the gates
    busy = {"cycles_inflight": {"XAUUSD": "2026-10-13T09:59:40Z"}}
    assert gate(engine_detail=busy) == ("wait", "desk_call_in_flight")
    # a stale engine row (older than 60 s) cannot be in the middle of a call: run, with a note
    kind, note = gate(engine_detail={"cycles_inflight": {"XAUUSD": "x"}}, engine_age_s=120.0)
    assert kind == "run" and "engine not reporting" in note
    assert gate(engine_detail=None, engine_age_s=None)[0] == "run"
    kind, why = gate(free_ram_mb=500.0)
    assert kind == "wait" and why.startswith("low_ram")
    assert gate(used_today=12) == ("skip", "daily_cap (12/12)")
    assert gate(used_today=11)[0] == "run"
    late = T + 7 * H                                                                   # 17:00 UTC
    assert gate(now=late) == ("skip", "outside_hours")
    assert gate(now=late, hourly=False)[0] == "run"                                    # the button ignores the hours
    assert gate(now=T - 3 * H, hourly=True)[0] == "run"                                # 07:00 is in [7, 16)
    assert gate(now=T - 4 * H, hourly=True) == ("skip", "outside_hours")               # 06:00 is not
    kind, note = gate(executor_age_s=None)
    assert kind == "run" and "executor not reporting" in note


def test_next_slot():
    cfg = ScalpCfg(enabled=True)                           # [7, 16), offset 20 s
    day0 = T - 10 * H
    assert sv.next_slot(day0 + 6 * H + 30 * MIN, cfg) == day0 + 7 * H + 20_000
    assert sv.next_slot(T + 10_000, cfg) == T + 20_000     # 10:00:10 → 10:00:20
    assert sv.next_slot(T + 20_000, cfg) == T + H + 20_000  # exactly the slot → the next one
    assert sv.next_slot(day0 + 15 * H + 30 * MIN, cfg) == day0 + 24 * H + 7 * H + 20_000
    assert sv.next_slot(day0 + 15 * H + 20_000, cfg) == day0 + 24 * H + 7 * H + 20_000
    assert sv.horizon_min({"brief": {"plans": [{"max_hold_min": 45}, {"max_hold_min": 120}]}}, cfg) == 180
    assert sv.horizon_min({"brief": {"plans": []}}, cfg) == 90


# ------------------------------------------------------------------------------------------------- the loop
def test_loop_runs_the_slot_writes_status_and_ends_on_stop(tmp_path):
    s = settings(tmp_path)
    sdir = scalp_dir(s)
    clock = {"t": T + 30 * MIN}                            # 10:30 → the next slot is 11:00:20

    def now():
        clock["t"] += 5 * MIN
        return clock["t"]
    spawned, slept = [], []

    def spawn():
        spawned.append(clock["t"])
        (sdir / "STOP").write_text("stop")
        return 0
    rc = sv.loop(s, clock=now, sleep=lambda x: slept.append(x), spawn=spawn, market_open=lambda t: True, poll_s=15)
    assert rc == 0 and len(spawned) == 1 and spawned[0] >= T + H + 20_000
    assert all(0 <= x <= 15 for x in slept)
    st = read_json(sdir / "status.json")
    assert st["pid"] is None and st["next_run_ms"] is None and st["loop_started_ms"] == T + 35 * MIN
    assert FileLock(sdir / "loop.lock").acquire(0)         # released: acquirable again


def test_loop_skips_a_closed_market_slot_and_never_runs_twice(tmp_path):
    s = settings(tmp_path)
    sdir = scalp_dir(s)
    clock = {"t": T + 30 * MIN}

    def now():
        clock["t"] += 10 * MIN
        return clock["t"]
    sleeps = []

    def sleep(x):
        sleeps.append(x)
        if len(sleeps) >= 8:
            (sdir / "STOP").write_text("stop")
    spawned = []
    rc = sv.loop(s, clock=now, sleep=sleep, spawn=lambda: spawned.append(1), market_open=lambda t: False)
    assert rc == 0 and not spawned
    # a second loop while one holds loop.lock ends at once with the skipped code
    held = FileLock(sdir / "loop.lock")
    assert held.acquire(0)
    try:
        assert sv.loop(s, clock=now, sleep=sleep, spawn=lambda: spawned.append(1), market_open=lambda t: True) == 2
    finally:
        held.release()
    assert not spawned


# ------------------------------------------------------------------------------------------------- run_once
def test_dry_run_writes_the_record_md_latest_status_and_notifies_without_a_ledger_row(tmp_path, monkeypatch):
    s = settings(tmp_path)
    calls = fake_siblings(monkeypatch)
    seen = fake_inputs(monkeypatch)
    sent = record_notify(monkeypatch)
    assert sv.run_once(s, reason="manual", no_model=True, fixture=FIXTURE, now=T) == 0
    assert seen == [("XAUUSD", T)]
    sdir = scalp_dir(s)
    bid = "20261013T1000Z"
    assert list_ids(s) == [bid]
    rec = read_json(sdir / "briefs" / "20261013" / "1000Z.json")
    md = (sdir / "briefs" / "20261013" / "1000Z.md").read_text(encoding="utf-8")
    assert rec["id"] == bid and rec["as_of_ms"] == T and rec["reason"] == "manual" and rec["version"] == 1
    assert rec["model"] == s.ai.models.scalp.model and rec["effort"] == "medium"
    assert rec["tokens"] == {"input": 0, "cached": 0, "output": 0} and rec["latency_ms"] == 0 and rec["images"] == 0
    assert rec["score"] is None and rec["scored_ms"] is None and rec["manual"] == []
    assert rec["payload_hash"] == "abc123" and rec["atr5m"] == 3.9 and rec["spread_median"] == 0.24
    assert rec["tol"] == pytest.approx(0.975) and rec["price_at"] == 4181.2 and rec["blackout"] is False
    assert rec["next_events"][0][2] == "CPI" and rec["news_windows"] == {"high": [30, 30]}
    assert rec["desk"]["equity"] == 100.36 and rec["desk"]["legs"] == [] and rec["desk"]["sides"] == []
    assert rec["brief"]["regime"] == "range" and len(rec["brief"]["levels"]) == 4 and len(rec["brief"]["plans"]) == 2
    assert set(rec["checks"]) == {"0", "1"} and rec["checks"]["0"]["rejected"] == []
    assert len(rec["prompt_hash"]) == 16 and rec["prompt_versions"]["scalp/system"] == 2      # D-060
    assert rec["charts"] == []
    # the checks got the record's numbers and the desk's sides
    plan, kw = calls["check_plan"][0]
    assert plan["side"] == "SELL" and kw["atr5m"] == 3.9 and kw["spread"] == 0.24 and kw["desk_sides"] == []
    assert kw["tol"] == pytest.approx(0.975) and kw["as_of"] == T and kw["trigger_window_min"] == 60
    assert len(kw["levels"]) == 4 and kw["news_windows"] == {"high": [30, 30]}
    # the markdown: Arabic headline, the tables, the safety text, the footer
    assert md.startswith("# Scalp 10:00 UTC — تذبذب / محايد (55%)")
    assert "## المستويات" in md and "| 4186.00 | asia_high |" in md and "## الخطط" in md and "| 1 | بيع |" in md
    assert SAFETY_TEXT[0]["ar"] in md and "## السلامة" in md and bid in md
    latest = read_json(sdir / "latest.json")
    assert latest["id"] == bid
    st = read_json(sdir / "status.json")
    assert st["last_outcome"] == "ok" and st["last_id"] == bid and st["last_error"] is None and st["last_run_ms"]
    assert read_json(sdir / "stats.json") == {"n_scored": 0}
    assert calls["stats"] == [0] and calls["journal_text"] == [(0, 0, 6)]
    # no ledger row: the shared usage ledger was never even created
    assert not (s.paths.shared() / "ai_usage.db").exists()
    assert not (s.paths.state() / "app.db").exists()
    # the notification: info, the hour in the title, the trade first (D-060), the on-demand key
    assert len(sent) == 1
    n = sent[0]
    assert n["level"] == "info" and n["key"] == f"scalp:{bid}:manual" and n["pair"] == "XAUUSD"
    assert n["title"].startswith("Scalp 10:00 UTC — range / neutral") and "NO SCALP" not in n["title"]
    assert n["text"].splitlines()[1].startswith("الصفقة: بيع limit من 4186.0") and len(n["text"]) <= 1500
    assert "## الصفقة المقترحة" in md and rec["trade"]["idx"] == 0
    # the locks are released
    assert FileLock(sdir / "run.lock").acquire(0) and FileLock(locks_dir(s) / sv.OPERATOR_LOCK).acquire(0)


def test_hourly_reason_uses_the_hour_key_and_a_second_run_scores_the_first_when_mature(tmp_path, monkeypatch):
    s = settings(tmp_path)
    fake_siblings(monkeypatch, mature=False)
    fake_inputs(monkeypatch)
    sent = record_notify(monkeypatch)
    assert sv.run_once(s, reason="hourly", no_model=True, fixture=FIXTURE, now=T) == 0
    assert sent[0]["key"] == "scalp:20261013T1000Z"
    # the next hour: the stored brief is mature → scored on the ticks the scorer reads, written back, stats refreshed
    calls = fake_siblings(monkeypatch, mature=True)
    sc = sys.modules["tradingsystem.scalp.scorer"]
    ticks = []

    def read_ticks(reader, exe, t0, t1):
        ticks.append((exe.symbol, t0, t1))
        return [t0], [4181.0], [4181.3]
    sc.read_ticks = read_ticks
    summary = {"levels_touched": 0, "levels_held": 0, "plans_n": 2, "triggered": 0, "target_first": 0,
               "stop_first": 0, "expired": 0, "sum_r": 0.0}
    sc.score_record = lambda rec, t, bid, ask, *, now, cfg: {"levels": [], "plans": [], "summary": summary}
    assert sv.run_once(s, reason="hourly", no_model=True, fixture=FIXTURE, now=T + H) == 0
    assert calls["is_mature"] == ["20261013T1000Z"]
    assert ticks == [("XAUUSD@", T, T + 120 * MIN)]       # the execution symbol; trigger 60 + the longest hold 60
    old = read_json(scalp_dir(s) / "briefs" / "20261013" / "1000Z.json")
    assert old["score"]["summary"]["plans_n"] == 2 and old["scored_ms"] == T + H
    assert read_json(scalp_dir(s) / "stats.json") == {"n_scored": 1}
    assert list_ids(s) == ["20261013T1100Z", "20261013T1000Z"]
    assert read_json(scalp_dir(s) / "latest.json")["id"] == "20261013T1100Z"
    assert calls["journal_text"] == [(1, 0, 6)]           # the scored brief reaches the next prompt


def test_a_run_while_run_lock_is_held_exits_2(tmp_path, monkeypatch):
    s = settings(tmp_path)
    fake_siblings(monkeypatch)
    seen = fake_inputs(monkeypatch)
    sent = record_notify(monkeypatch)
    held = FileLock(scalp_dir(s) / "run.lock")
    assert held.acquire(0)
    try:
        assert sv.run_once(s, reason="dashboard", no_model=True, fixture=FIXTURE, now=T) == 2
    finally:
        held.release()
    assert seen == [] and sent == []
    assert read_json(scalp_dir(s) / "status.json")["last_outcome"] == "skip:run_in_progress"
    assert sv.score_only(s) == 0                           # the lock is free again; nothing to score


def test_model_path_gates_disabled_in_flight_and_operator_lock(tmp_path, monkeypatch):
    fake_siblings(monkeypatch)
    seen = fake_inputs(monkeypatch)
    sent = record_notify(monkeypatch)
    s = settings(tmp_path, enabled=False)
    assert sv.run_once(s, reason="hourly", now=T) == 2
    assert read_json(scalp_dir(s) / "status.json")["last_outcome"] == "skip:disabled"
    # the desk in the middle of a call: wait, then skip when gate_retry_min is over (0 here)
    s = settings(tmp_path, gate_retry_min=0)
    db = s.paths.state() / "app.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE collector_status (collector TEXT PRIMARY KEY, state TEXT NOT NULL, last_data_ms "
                    "INTEGER, last_error TEXT, last_error_ms INTEGER, detail TEXT, updated_ms INTEGER NOT NULL)")
        con.execute("INSERT INTO collector_status VALUES ('engine', 'live', ?, NULL, NULL, ?, ?)",
                    (T, json.dumps({"cycles_inflight": {"XAUUSD": "2026-10-13T09:59:50Z"}}), T - 5000))
    assert sv.run_once(s, reason="manual", now=T) == 2
    assert read_json(scalp_dir(s) / "status.json")["last_outcome"].startswith("skip:desk_call_in_flight")
    assert sv.status_rows(db, T)[0] == {"cycles_inflight": {"XAUUSD": "2026-10-13T09:59:50Z"}}
    # idle desk, but an operator session holds the shared lock
    with sqlite3.connect(db) as con:
        con.execute("UPDATE collector_status SET detail=?", (json.dumps({"cycles_inflight": {}}),))
    op = FileLock(locks_dir(s) / sv.OPERATOR_LOCK)
    assert op.acquire(0)
    try:
        assert sv.run_once(s, reason="manual", now=T) == 2
    finally:
        op.release()
    assert read_json(scalp_dir(s) / "status.json")["last_outcome"].startswith("skip:operator_session")
    assert seen == [] and sent == []                       # no inputs were built, nothing was sent
    # the cap: 12 scalp rows today in the shared ledger → skip:daily_cap
    from tradingsystem.ai.budget import UsageStore, usage_db
    st = UsageStore(usage_db(s))
    try:
        for _ in range(12):
            st.record(None, provider="claude_code", model="claude-fable-5-1", purpose="scalp_brief", pair=None, ok=True,
                      role="scalp")
        st._con.execute("UPDATE ai_usage SET ts=?", (T - 1000,))          # the rows belong to T's quota day
        st._con.commit()
        assert st.count_since("claude_code", 0) == 0     # the desk's quota ladder never sees them
        assert st.count_role_since("scalp", T - 86_400_000) == 12
    finally:
        st.close()
    assert sv.run_once(s, reason="manual", now=T) == 2
    assert read_json(scalp_dir(s) / "status.json")["last_outcome"].startswith("skip:daily_cap (12/12)")
    # one row fewer → the gates pass and the (replaced) model is reached: the guard says so, the run fails cleanly
    st = UsageStore(usage_db(s))
    try:
        st._con.execute("DELETE FROM ai_usage WHERE id = (SELECT min(id) FROM ai_usage)")
        st._con.commit()
    finally:
        st.close()
    assert sv.run_once(s, reason="manual", now=T) == 1
    assert read_json(scalp_dir(s) / "status.json")["last_outcome"] == "failed:model"
    assert sent[-1]["key"] == "scalp:fail:20261013" and sent[-1]["level"] == "warn"
    assert seen == [("XAUUSD", T)]


def test_status_doc_and_bad_arguments(tmp_path, monkeypatch):
    s = settings(tmp_path)
    doc = sv.status_doc(s)
    assert doc["loop_running"] is False and doc["stop_file"] is False and doc["latest_id"] is None
    assert doc["enabled"] is True and doc["hours_utc"] == [7, 16]
    with pytest.raises(ValueError):
        sv.run_once(s, reason="cron", now=T)
    with pytest.raises(ValueError):
        sv.run_once(s, reason="manual", no_model=True, now=T)
    assert sv.manual_text([{"open_ms": T, "side": "SELL", "entry": 4196.1, "exit": 4192.9, "pnl_usd": 3.2,
                            "brief_id": "20261013T1000Z", "followed_plan": True}]) == \
        "10-13 10:00 SELL 4196.1 -> 4192.9 +3.20 USD (plan followed)"


def test_the_loop_lets_a_child_wait_its_gate_window_before_killing_it(tmp_path, monkeypatch):
    """2026-10-12: the 600 s run timeout killed every hourly child while it was still waiting (up to gate_retry_min)
    on low RAM / the desk's call - the loop's kill timeout now covers the gate wait plus the run."""
    s = settings(tmp_path)
    assert sv.child_timeout_s(s.scalp) == s.scalp.gate_retry_min * 60 + s.scalp.run_timeout_s == 1800
    sdir = scalp_dir(s)
    clock = {"t": T + 30 * MIN}

    def now():
        clock["t"] += 5 * MIN
        return clock["t"]
    calls = []

    def fake_spawn_once(s_, *, timeout_s, python=None):
        calls.append(timeout_s)
        (sdir / "STOP").write_text("stop")
        return 0
    monkeypatch.setattr(sv, "spawn_once", fake_spawn_once)
    assert sv.loop(s, clock=now, sleep=lambda x: None, market_open=lambda t: True, poll_s=15) == 0
    assert calls == [1800]
