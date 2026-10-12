"""D-061: the scalp brief's trade placed automatically - the owner's settings file, the gates, the order built for the
desk's MT5 backend, the state the loop manages, and the ``--manage`` decisions. No terminal is ever touched: the
attach is replaced by fakes (a test that reached the real MT5 would fail on ``terminal_running``)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from tradingsystem.core.killswitch import set_kill_switch
from tradingsystem.core.settings import INSTANCE_ENV, load_settings
from tradingsystem.execution.executor import magics
from tradingsystem.scalp import autotrade as at
from tradingsystem.scalp import brief as bf
from tradingsystem.scalp import scorer as sc
from tradingsystem.scalp.contract import ScalpBrief
from tradingsystem.scalp.files import scalp_dir

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "scalp_brief_sample.json"
T = int(dt.datetime(2026, 10, 13, 10, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
MIN = 60_000


def settings(tmp_path: Path, **scalp):
    s = load_settings(extra_env={INSTANCE_ENV: "XAUUSD", "EXECUTION_MODE": "demo"})
    paths = s.paths.model_copy(update={"data_dir": str(tmp_path / "data"), "logs_dir": str(tmp_path / "logs")})
    return s.model_copy(update={"paths": paths, "scalp": s.scalp.model_copy(update={"enabled": True, **scalp})})


def record(**over) -> dict:
    """A stored-brief record with THE trade chosen by the code (the fixture's plan 1: SELL limit 4186 / 4189 / 4180)."""
    brief = ScalpBrief.model_validate({**json.loads(FIXTURE.read_text(encoding="utf-8")), **over}).model_dump()
    return {"id": "20261013T1000Z", "as_of_ms": T, "brief": brief, "checks": {}, "trade": bf.trade_summary(brief, {}),
            "desk": {"equity": 500.0, "legs": [], "sides": []}}


class FakeBackend:
    """The slice of MT5Backend the auto-trader uses; records every call."""

    def __init__(self, *, ok=True, orders=(), positions=(), close_ok=True):
        self.calls: list[tuple] = []
        self.ok, self.close_ok = ok, close_ok
        self.orders, self.positions = list(orders), list(positions)
        self.mt5 = NS(orders_get=lambda: list(self.orders), positions_get=lambda: list(self.positions))
        self.model = NS(server_to_utc=lambda ms, prefer="earlier": int(ms) - 3 * 3_600_000)
        self.magic = 26092510

    def mine(self, x) -> bool:
        return int(getattr(x, "magic", 0)) == self.magic

    def place(self, **kw):
        self.calls.append(("place", kw))
        if not self.ok:
            return {"ok": False, "reason": "order_check failed for ts:sc20261013T1000Z:1: Invalid stops"}
        return {"ok": True, "placed": [{"comment": "ts:sc20261013T1000Z:1", "order": 555, "deal": 0, "price": kw["entry"],
                                        "requested": kw["entry"], "slippage": 0.0, "volume": kw["lots"]}], "attempts": []}

    def close_position(self, ticket, volume=None, **kw):
        self.calls.append(("close", ticket))
        return {"ok": self.close_ok, "status": "applied" if self.close_ok else "failed", "price": 4185.5,
                "meaning": "done" if self.close_ok else "Requote"}


class FakeTerminal:
    def __init__(self):
        self.down = 0

    def shutdown(self):
        self.down += 1


def fake_attach(be: FakeBackend):
    term = FakeTerminal()
    seen = []

    def attach(s, symbol):
        seen.append(symbol)
        return term, be
    return attach, term, seen


# ------------------------------------------------------------------------------------------- settings
def test_settings_defaults_roundtrip_and_bounds(tmp_path):
    s = settings(tmp_path)
    assert at.read_settings(s) == {"mode": "analyze", "lot": 0.01, "min_confidence": 55, "hours_utc": [7, 16],
                                   "every_min": 60, "updated_ms": None, "by": "default"}
    doc = at.write_settings(s, mode="auto", lot=0.02, min_confidence=60, now=T)
    assert doc == {"mode": "auto", "lot": 0.02, "min_confidence": 60, "hours_utc": [7, 16], "every_min": 60,
                   "updated_ms": T, "by": "dashboard"}
    assert at.read_settings(s) == doc and json.loads((scalp_dir(s) / at.SETTINGS_FILE).read_text(encoding="utf-8")) == doc
    for bad in ({"mode": "yolo", "lot": 0.01, "min_confidence": 50}, {"mode": "auto", "lot": 0.5, "min_confidence": 50},
                {"mode": "auto", "lot": 0.005, "min_confidence": 50}, {"mode": "auto", "lot": 0.015, "min_confidence": 50},
                {"mode": "auto", "lot": 0.01, "min_confidence": 101}, {"mode": "auto", "lot": "x", "min_confidence": 1}):
        with pytest.raises(ValueError):
            at.write_settings(s, **bad)
    assert at.read_settings(s) == doc                                   # a refused write changes nothing
    (scalp_dir(s) / at.SETTINGS_FILE).write_text('{"mode": "auto", "lot": 9}', encoding="utf-8")
    assert at.read_settings(s)["mode"] == "analyze"                     # an out-of-bounds file: the defaults
    (scalp_dir(s) / at.SETTINGS_FILE).write_text("{not json", encoding="utf-8")
    assert at.read_settings(s)["mode"] == "analyze"


def test_the_scalp_magic_is_outside_the_pair_folders_and_the_prompt_line_states_the_lot(tmp_path):
    s = settings(tmp_path)
    own, base, family = magics(s)
    assert at.scalp_magic(s) == base + 9 and at.scalp_magic(s) not in family
    line = at.execution_text(s, {"mode": "analyze", "lot": 0.02, "min_confidence": 55}, 500.0)
    assert line.startswith("The owner's lot is FIXED at 0.02 lots = 2 oz: a plan's risk in USD = |entry - stop| x 2.")
    assert "25.00 USD (5 % of equity 500.00)" in line and "BY HAND" in line
    auto = at.execution_text(s, {"mode": "auto", "lot": 0.01, "min_confidence": 60}, None)
    assert "EXECUTION IS AUTOMATIC" in auto and "under confidence 60" in auto and "5 % of the equity" in auto
    system, user, versions = bf.compose(s, __import__("tradingsystem.core.instruments", fromlist=["x"]).InstrumentRegistry.from_settings(s),
                                        bf.Inputs(pair="XAUUSD", as_of=T, payload={}, payload_text="{}"), reason="manual",
                                        desk={"equity": 500.0}, manual_text="", journal_text="")
    assert "The owner's lot is FIXED at 0.01 lots" in user and versions["scalp/instructions"] == 3
    assert "(k) The owner's LOT is fixed" in system


# ------------------------------------------------------------------------------------------- the gates
def test_auto_gate_matrix(tmp_path):
    s = settings(tmp_path)
    cfg = s.scalp
    auto = {"mode": "auto", "lot": 0.01, "min_confidence": 55}
    rec = record()
    ok = dict(settings=auto, rec=rec, equity=500.0, cfg=cfg, kill=False, market_open=True, open_now=0, execution_mode="demo")
    kind, why = at.auto_gate(**ok)
    assert kind == "place" and why.startswith("plan 1 SELL limit @ 4186.0 sl 4189.0 tp 4180.0 0.01 lots, risk 3.00 USD")
    assert at.auto_gate(**{**ok, "settings": {**auto, "mode": "analyze"}}) == ("skip", "mode analyze: the owner trades by hand")
    assert at.auto_gate(**{**ok, "execution_mode": "live"})[1].startswith("execution.mode live: auto scalps run on the demo")
    assert at.auto_gate(**{**ok, "execution_mode": "paper"})[0] == "skip"
    assert at.auto_gate(**{**ok, "kill": True}) == ("skip", "kill switch on")
    assert at.auto_gate(**{**ok, "market_open": False}) == ("skip", "market closed")
    assert at.auto_gate(**{**ok, "open_now": 1})[1].startswith("1 scalp order(s) / position(s) still open")
    assert at.auto_gate(**{**ok, "equity": None})[1].startswith("equity unknown")
    assert at.auto_gate(**{**ok, "settings": {**auto, "min_confidence": 56}})[1] == "plan 1 confidence 55 < min 56"
    assert at.auto_gate(**{**ok, "settings": {**auto, "lot": 0.10}})[1].startswith("plan 1 risk 30.00 USD > cap 25.00 USD")
    # the chosen plan is a market_after_rejection one (plan 2 of the fixture): by hand only
    rec2 = record(best_plan=1)
    assert rec2["trade"]["idx"] == 1
    assert at.auto_gate(**{**ok, "rec": rec2})[1] == "plan 2 is market_after_rejection: by hand only (the code reads no candles)"
    none = record(plans=[], no_scalp_now=True, best_plan=None, trade_call="")
    assert at.auto_gate(**{**ok, "rec": none})[1].startswith("no plan chosen: لا صفقة هلق")


def test_order_rec_and_the_comment_tag():
    plan = record()["brief"]["plans"][0]
    assert at.order_rec(plan, as_of=T, window_min=60) == {
        "decision": "SELL", "order_type": "SELL_LIMIT", "stop_loss": 4189.0,
        "take_profits": [{"price": 4180.0, "close_fraction": 1.0}], "valid_until": "2026-10-13T11:00:00.000Z"}
    assert at.order_rec({**plan, "side": "BUY", "entry_type": "stop"}, as_of=T, window_min=30)["order_type"] == "BUY_STOP"
    assert at.brief_id_of("ts:sc20261013T1000Z:1") == "20261013T1000Z"
    assert at.brief_id_of("ts:07296dabb44d40148b57:1") is None and at.brief_id_of("") is None and at.brief_id_of(None) is None
    assert len(f"ts:sc20261013T1000Z:1") <= 31


# ------------------------------------------------------------------------------------------- placing
def test_execute_places_the_trade_records_the_state_and_notifies(tmp_path, monkeypatch):
    s = settings(tmp_path)
    sent = []
    monkeypatch.setattr(at, "notify", lambda s_, level, title, text, *, key=None, pair=None: sent.append((level, title, text, key)))
    rec = record()
    # mode analyze: nothing is placed, nothing attached, the record says so
    be = FakeBackend()
    attach, term, seen = fake_attach(be)
    ex = at.execute(s, rec, symbol="XAUUSD@", equity=500.0, market_open=True, now=T, attach_fn=attach)
    assert ex["decision"] == "analyze" and not be.calls and not seen and sent == []
    # mode auto: the order goes through the backend with the scalp magic's decision id, lot and entry
    at.write_settings(s, mode="auto", lot=0.02, min_confidence=50, now=T)
    ex = at.execute(s, rec, symbol="XAUUSD@", equity=500.0, market_open=True, now=T, attach_fn=attach)
    assert ex["decision"] == "placed" and ex["ticket"] == 555 and ex["order_type"] == "SELL_LIMIT" and ex["lot"] == 0.02
    assert ex["expires_ms"] == T + 60 * MIN and ex["max_hold_min"] == 45 and ex["plan_idx"] == 0
    (kind, kw), = be.calls
    assert kind == "place" and kw["decision_id"] == "sc20261013T1000Z" and kw["symbol"] == "XAUUSD@"
    assert kw["lots"] == 0.02 and kw["entry"] == 4186.0 and kw["dry_run"] is False
    assert kw["rec"]["order_type"] == "SELL_LIMIT" and kw["rec"]["stop_loss"] == 4189.0
    assert seen == ["XAUUSD@"] and term.down == 1
    state = at.read_state(s)
    assert len(state["open"]) == 1 and state["open"][0]["ticket"] == 555 and state["open"][0]["stage"] == "pending"
    assert state["open"][0]["symbol"] == "XAUUSD@" and state["open"][0]["max_hold_min"] == 45
    assert [e["event"] for e in at.read_log(s)] == ["placed"]
    assert sent[-1][0] == "info" and sent[-1][1] == "Scalp AUTO: SELL_LIMIT 0.02 @ 4186.0 placed"
    assert "SL 4189.0 / TP 4180.0 · risk 6.00 USD" in sent[-1][2] and sent[-1][3] == "scalp:auto:20261013T1000Z:placed"
    # one at a time: the next brief is refused while this one is tracked (no attach)
    seen.clear()
    ex2 = at.execute(s, {**rec, "id": "20261013T1100Z", "as_of_ms": T + 3_600_000}, symbol="XAUUSD@", equity=500.0,
                     market_open=True, now=T + 3_600_000, attach_fn=attach)
    assert ex2["decision"] == "skipped" and "still open" in ex2["why"] and not seen
    assert [e["event"] for e in at.read_log(s)][0] == "refused" and sent[-1][1] == "Scalp AUTO: no order"


def test_execute_failures_are_recorded_and_never_raise(tmp_path, monkeypatch):
    s = settings(tmp_path)
    sent = []
    monkeypatch.setattr(at, "notify", lambda s_, level, title, text, *, key=None, pair=None: sent.append((level, title, text)))
    at.write_settings(s, mode="auto", lot=0.01, min_confidence=50, now=T)
    rec = record()
    # the terminal is not there
    def no_terminal(s_, symbol):
        raise RuntimeError("MT5 terminal is not running")
    ex = at.execute(s, rec, symbol="XAUUSD@", equity=500.0, market_open=True, now=T, attach_fn=no_terminal)
    assert ex["decision"] == "failed" and ex["why"].startswith("MT5: RuntimeError") and at.read_state(s)["open"] == []
    assert sent[-1][:2] == ("warn", "Scalp AUTO: order NOT placed")
    # the broker refuses
    be = FakeBackend(ok=False)
    attach, term, _ = fake_attach(be)
    ex = at.execute(s, rec, symbol="XAUUSD@", equity=500.0, market_open=True, now=T, attach_fn=attach)
    assert ex["decision"] == "failed" and "Invalid stops" in ex["why"] and term.down == 1
    assert at.read_state(s)["open"] == [] and [e["event"] for e in at.read_log(s)][:2] == ["failed", "failed"]
    # the kill switch
    set_kill_switch(s, None, reason="test", actor="test")
    ex = at.execute(s, rec, symbol="XAUUSD@", equity=500.0, market_open=True, now=T, attach_fn=attach)
    assert ex["decision"] == "skipped" and ex["why"] == "kill switch on" and len(be.calls) == 1


# ------------------------------------------------------------------------------------------- managing
def entry(**over) -> dict:
    return {"ticket": 555, "brief_id": "20261013T1000Z", "idx": 0, "side": "SELL", "symbol": "XAUUSD@", "entry": 4186.0,
            "stop": 4189.0, "target": 4180.0, "lot": 0.01, "placed_ms": T, "expires_ms": T + 60 * MIN,
            "max_hold_min": 45, "stage": "pending", "filled_ms": None, **over}


def test_manage_decisions_pending_filled_held_and_gone():
    pending = NS(ticket=555, magic=26092510, comment="ts:sc20261013T1000Z:1")
    pos = NS(ticket=555, magic=26092510, comment="ts:sc20261013T1000Z:1", time_msc=T + 10 * MIN)
    fill = lambda p: p.time_msc  # noqa: E731
    keep, close, settled = at.manage_decisions([entry()], orders=[pending], positions=[], now=T + 5 * MIN, filled_ms=fill)
    assert [e["stage"] for e in keep] == ["pending"] and not close and not settled
    keep, close, settled = at.manage_decisions([entry()], orders=[], positions=[pos], now=T + 20 * MIN, filled_ms=fill)
    assert keep[0]["stage"] == "position" and keep[0]["filled_ms"] == T + 10 * MIN and keep[0]["position_ticket"] == 555
    keep, close, settled = at.manage_decisions([keep[0]], orders=[], positions=[pos], now=T + 55 * MIN, filled_ms=fill)
    assert not keep and close[0]["ticket"] == 555 and not settled          # 45 min after the fill: close it
    keep, close, settled = at.manage_decisions([entry()], orders=[], positions=[], now=T + 61 * MIN, filled_ms=fill)
    assert settled[0]["reason"] == "expired" and settled[0]["stage"] == "settled"
    keep, close, settled = at.manage_decisions([entry(stage="position", filled_ms=T + 10 * MIN)], orders=[], positions=[],
                                               now=T + 30 * MIN, filled_ms=fill)
    assert settled[0]["reason"] == "closed"                                  # SL / TP / by hand: gone before the hold time
    # a position whose ticket differs (the broker numbers it anew) is found by its comment
    pos2 = NS(ticket=777, magic=26092510, comment="ts:sc20261013T1000Z:1", time_msc=T + 10 * MIN)
    keep, _, _ = at.manage_decisions([entry()], orders=[], positions=[pos2], now=T + 20 * MIN, filled_ms=fill)
    assert keep[0]["position_ticket"] == 777


def test_manage_closes_after_the_hold_time_and_settles_what_is_gone(tmp_path, monkeypatch):
    s = settings(tmp_path)
    sent = []
    monkeypatch.setattr(at, "notify", lambda s_, level, title, text, *, key=None, pair=None: sent.append((level, title, key)))
    assert at.manage(s, now=T) == {"tracked": 0}                            # nothing tracked: no attach at all
    at.write_state(s, {"open": [entry(), entry(ticket=556, brief_id="20261013T0900Z", placed_ms=T - 3_600_000,
                                                expires_ms=T - 3_000_000)]}, T)
    pos = NS(ticket=555, magic=26092510, comment="ts:sc20261013T1000Z:1", time_msc=T + 10 * MIN + 3 * 3_600_000)
    be = FakeBackend(positions=[pos])
    attach, term, _ = fake_attach(be)
    out = at.manage(s, now=T + 20 * MIN, attach_fn=attach)
    assert out == {"tracked": 2, "closed": 0, "settled": 1, "errors": 0} and term.down == 1
    state = at.read_state(s)
    assert [e["ticket"] for e in state["open"]] == [555] and state["open"][0]["stage"] == "position"
    assert state["open"][0]["filled_ms"] == T + 10 * MIN                   # server time -> UTC through the model
    assert sent[-1] == ("info", "Scalp AUTO: SELL expired", "scalp:auto:20261013T0900Z:expired")
    out = at.manage(s, now=T + 56 * MIN, attach_fn=attach)
    assert out["closed"] == 1 and be.calls == [("close", 555)] and at.read_state(s)["open"] == []
    assert sent[-1] == ("info", "Scalp AUTO: SELL closed after 45 min", "scalp:auto:20261013T1000Z:closed")
    assert [e["event"] for e in at.read_log(s)] == ["closed_hold", "settled"]
    # a close the broker refuses keeps the entry for the next minute
    at.write_state(s, {"open": [entry(stage="position", filled_ms=T, position_ticket=555)]}, T)
    be2 = FakeBackend(positions=[pos], close_ok=False)
    attach2, _, _ = fake_attach(be2)
    out = at.manage(s, now=T + 50 * MIN, attach_fn=attach2)
    assert out["errors"] == 1 and len(at.read_state(s)["open"]) == 1 and sent[-1][1] == "Scalp AUTO: close failed"
    # the terminal away: nothing changes, no exception
    def no_terminal(s_, symbol):
        raise RuntimeError("no MT5")
    assert "error" in at.manage(s, now=T + 51 * MIN, attach_fn=no_terminal) and len(at.read_state(s)["open"]) == 1


# ------------------------------------------------------------------------------------------- the scorer sees them
def test_the_auto_trades_are_read_with_their_brief_and_count_as_followed_plans():
    deals = [NS(magic=26092510, symbol="XAUUSD@", position_id=555, entry=0, type=1, time_msc=T + 10 * MIN, price=4186.0,
                volume=0.02, profit=0.0, commission=0.0, swap=0.0, fee=0.0, comment="ts:sc20261013T1000Z:1"),
             NS(magic=26092510, symbol="XAUUSD@", position_id=555, entry=1, type=0, time_msc=T + 40 * MIN, price=4180.0,
                volume=0.02, profit=12.0, commission=0.0, swap=0.0, fee=0.0, comment="ts:sc20261013T1000Z:1"),
             NS(magic=0, symbol="XAUUSD@", position_id=556, entry=0, type=0, time_msc=T + 12 * MIN, price=4184.0,
                volume=0.05, profit=0.0, commission=0.0, swap=0.0, fee=0.0, comment=""),
             NS(magic=26092504, symbol="XAUUSD@", position_id=557, entry=0, type=0, time_msc=T + 13 * MIN, price=4184.0,
                volume=0.01, profit=0.0, commission=0.0, swap=0.0, fee=0.0, comment="ts:07296dab:1")]
    rows = sc.manual_trades(deals, symbol="XAUUSD@", server_to_utc=lambda ms: ms, t0=T, t1=T + 3_600_000, auto_magic=26092510)
    assert [(r["position_id"], r["auto"]) for r in rows] == [(555, True), (556, False)]      # the desk's own: never
    assert rows[0]["pnl_usd"] == 12.0 and rows[0]["exit"] == 4180.0 and rows[0]["side"] == "SELL"
    sc.attribute(rows, [record()], tol=1.0)
    assert rows[0]["brief_id"] == "20261013T1000Z" and rows[0]["followed_plan"] is True
    assert rows[1]["brief_id"] == "20261013T1000Z" and rows[1]["followed_plan"] is False   # the owner's own BUY
    assert sc.manual_trades(deals, symbol="XAUUSD@", server_to_utc=lambda ms: ms, t0=T, t1=T + 3_600_000) \
        and all(not r["auto"] for r in sc.manual_trades(deals, symbol="XAUUSD@", server_to_utc=lambda ms: ms, t0=T,
                                                        t1=T + 3_600_000))


# ------------------------------------------------------------------------------------------- D-062: the schedule
def test_hours_and_cadence_are_owner_settings_with_partial_updates(tmp_path):
    s = settings(tmp_path)
    st = at.read_settings(s)
    assert st["hours_utc"] == [7, 16] and st["every_min"] == 60 and at.schedule(s) == ([7, 16], 60)
    doc = at.write_settings(s, hours_utc=[9, 18], every_min=30, now=T)
    assert doc["hours_utc"] == [9, 18] and doc["every_min"] == 30 and doc["mode"] == "analyze" and doc["lot"] == 0.01
    assert at.write_settings(s, mode="auto", now=T)["hours_utc"] == [9, 18]      # a partial update keeps the rest
    assert at.schedule(s) == ([9, 18], 30)
    for bad in ({"hours_utc": [16, 7]}, {"hours_utc": [7, 25]}, {"hours_utc": [7]}, {"hours_utc": "7-16"},
                {"every_min": 45}, {"every_min": 0}, {"every_min": "x"}):
        with pytest.raises(ValueError):
            at.write_settings(s, **bad)
    assert at.schedule(s) == ([9, 18], 30)
    (scalp_dir(s) / at.SETTINGS_FILE).write_text('{"mode": "auto", "lot": 0.01, "min_confidence": 50}', encoding="utf-8")
    assert at.read_settings(s)["hours_utc"] == [7, 16] and at.read_settings(s)["every_min"] == 60   # older file: defaults
