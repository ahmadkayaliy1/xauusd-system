"""D-055: the live desk's trade journal - the store's trades / stop changes / reviews, the price after a close (only
closed candles up to the cycle time), the contract's trade_review, the desk switch and the backtest's desk settings."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np
import pytest

from tradingsystem.ai.contract import Recommendation, TradeReview
from tradingsystem.ai.store import DecisionStore
from tradingsystem.analysis.frames import Frame
from tradingsystem.analysis.snapshot import journal_block
from tradingsystem.core.settings import DeskCfg
from tradingsystem.core.timeframes import Timeframe
from tradingsystem.execution.management import ActionLog

A = "10ccc308618048eb89986e2d60605ad8"      # the 2026-10-07 live SELL: stop moved to a smaller loss, then hit
R = "055a6651000000000000000000000000"


def ms(h: int, m: int, s: int = 0) -> int:
    return int(datetime(2026, 10, 7, h, m, s, tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture()
def store(tmp_path):
    db = tmp_path / "app.db"
    st = DecisionStore(db)
    log = ActionLog(db)                                      # creates position_actions
    con = st._con
    ex = {"executed_levels": {"entry": 4133.59, "stop_loss": 4143.6, "take_profits": [4113.44]},
          "backend": {"ok": True, "placed": [{"price": 4132.57, "volume": 0.01}]}}
    od = {"venue": "mt5", "open_ms": ms(5, 17, 26), "close_ms": ms(7, 32, 57),
          "legs": [{"leg": "113691689", "tp_index": 1, "filled": True, "reason": "sl"}]}
    con.execute("INSERT INTO ai_decisions (id, ts, pair, mode, status, decision, order_type, execution_state, "
                "execution_detail, outcome, outcome_ts, outcome_pnl_usd, outcome_detail) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (A, ms(5, 17), "XAUUSD", "demo", "valid", "SELL", "MARKET", "executed", json.dumps(ex),
                 "closed_loss", ms(7, 33, 40), -5.63, json.dumps(od)))
    review = {"decision": A[:8], "cause": "stop_moved", "lesson": "Do not pull the stop to a smaller loss under a "
              "fresh 15m high: a sweep takes it.", "idea_alive": True, "re_entry": "SELL LIMIT 4138 back under the swept high"}
    con.execute("INSERT INTO ai_decisions (id, ts, pair, mode, status, decision, execution_state, recommendation) "
                "VALUES (?,?,?,?,?,?,?,?)", (R, ms(7, 35), "XAUUSD", "demo", "valid", "NO_TRADE", "not_executed",
                                            json.dumps({"trade_review": review})))
    for seq, (ts, st_, val) in enumerate(((ms(7, 16, 4), "applied", 4138.2), (ms(7, 20), "rejected", 4136.0))):
        con.execute("INSERT INTO position_actions (ts, pair, source, source_decision, seq, target_decision, leg, "
                    "action, requested, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ts, "XAUUSD", "model", "88540cb4" + "0" * 24, seq, A, "113691689", "modify_sl",
                     json.dumps({"value": val, "reason": "Tighten behind fresh 15m swing high"}), st_))
    con.commit()
    yield st
    log.close()
    st.close()


def test_the_journal_shows_the_trade_its_stop_change_its_end_and_the_review(store):
    j = store.journal("XAUUSD", ms(8, 0))
    [t] = j["trades"]
    assert (t["decision"], t["side"], t["entry"], t["stop"], t["target"]) == ("10ccc308", "SELL", 4132.57, 4143.6, 4113.44)
    [c] = t["changes"]
    assert (c["stop"], c["by"], c["why"]) == ([4143.6, 4138.2], "model", "Tighten behind fresh 15m swing high")
    assert c["at"].startswith("2026-10-07T07:16")
    assert t["stop_changes_refused"] == 1
    assert (t["status"], t["exit"], t["exit_price"], t["pnl_usd"], t["r"]) == ("closed", "sl", 4138.2, -5.63, -0.51)
    assert t["review"]["cause"] == "stop_moved" and t["review"]["idea_alive"] is True
    assert j["today"] == {"closed": 1, "losses": 1, "pnl_usd": -5.63}
    assert j["lessons"] and "smaller loss" in j["lessons"][0]


def test_the_journal_never_shows_what_had_not_happened_at_the_cycle_time(store):
    early = store.journal("XAUUSD", ms(7, 10))               # before the stop move, the close and the review
    [t] = early["trades"]
    assert t["status"] == "open" and "changes" not in t and "review" not in t and "exit" not in t
    assert "today" not in early and "lessons" not in early
    assert store.journal("XAUUSD", ms(5, 0)) == {}           # nothing traded yet
    assert store.journal("XAUUSD", ms(8, 0) + 25 * 3_600_000) == {}   # out of the 24 h window


def frame(t0: int, highs, lows, closes) -> Frame:
    n = len(highs)
    ot = np.array([t0 + i * 60_000 for i in range(n)], dtype=float)
    a = lambda xs: np.array(xs, dtype=float)                # noqa: E731
    return Frame("mt5:XAUUSD@", Timeframe.parse("1m"), ot, a(closes), a(highs), a(lows), a(closes), np.ones(n), "tick")


def test_the_price_after_the_close_uses_only_candles_opened_after_it(store):
    j = store.journal("XAUUSD", ms(8, 0))
    close_ms = j["trades"][0]["close_ms"]                    # 07:32:57
    fr = frame(ms(7, 32), highs=[4139.4, 4137.0, 4134.0, 4120.0], lows=[4137.0, 4133.0, 4125.0, 4112.9],
               closes=[4138.0, 4134.0, 4126.0, 4113.5])
    out = journal_block(j, {"1m": fr}, None, 2)
    ac = out["trades"][0]["after_close"]
    assert "close_ms" not in out["trades"][0]
    assert (ac["high"], ac["low"], ac["last"], ac["candles"]) == (4137.0, 4112.9, 4113.5, "1m")   # 07:32 bar left out
    assert (ac["original_stop_hit"], ac["target_hit"]) == (0, 1)
    assert "after_close" not in journal_block(j, {"1m": frame(ms(7, 30), [4139.4], [4137.0], [4138.0])}, None, 2)["trades"][0]


def test_trade_review_is_an_optional_part_of_the_answer():
    base = {"pair": "XAUUSD", "timestamp": "2026-10-07T07:35:07Z", "valid_until": "2026-10-07T08:35:07Z",
            "price_reference": "mt5:XAUUSD@", "market_summary": "stopped by a sweep", "decision": "NO_TRADE",
            "confidence": 45, "next_review": {"in_minutes": 45}, "reasoning_trace": "the idea is alive"}
    assert Recommendation.model_validate(base).trade_review is None
    r = Recommendation.model_validate({**base, "trade_review": {"decision": "10ccc308", "cause": "stop_moved",
                                                                "lesson": "keep the structural stop", "idea_alive": True}})
    assert r.trade_review == TradeReview(decision="10ccc308", cause="stop_moved", lesson="keep the structural stop",
                                         idea_alive=True)
    with pytest.raises(ValueError):
        Recommendation.model_validate({**base, "trade_review": {"decision": "10ccc308", "cause": "bad luck",
                                                                "lesson": "x" * 5, "idea_alive": False}})


def test_the_desk_switches_default_to_the_old_behaviour():
    d = DeskCfg()
    assert (d.sl_moves, d.journal) == ("tighter", False)
    assert DeskCfg(sl_moves="profit_only", journal=True).sl_moves == "profit_only"
    with pytest.raises(ValueError):
        DeskCfg(sl_moves="looser")


def test_the_payload_carries_the_journal_only_for_a_live_desk_that_keeps_one(store):
    from pathlib import Path
    from types import SimpleNamespace as NS

    from tradingsystem.ai.orchestrator import Orchestrator
    from tradingsystem.core.settings import load_settings

    seen = []

    class Builder:
        def build(self, pair, as_of, **kw):
            seen.append(kw)
            return {}

    base = load_settings(env_path=Path("nope.env"))

    def with_desk(**upd):
        pc = base.pairs["XAUUSD"]
        desk = (pc.desk or DeskCfg()).model_copy(update=upd)
        return base.model_copy(update={"pairs": {**base.pairs, "XAUUSD": pc.model_copy(update={"desk": desk})}})

    for s, has in ((with_desk(mode="live", journal=True), True), (with_desk(mode="live", journal=False), False),
                   (with_desk(mode="shadow", journal=True), False)):
        me = NS(s=s, builder=Builder(), store=store, default_account=lambda: {"equity": 100.0})
        Orchestrator.payload(me, "XAUUSD", ms(8, 0))
        assert ("journal" in seen[-1]) is has
    assert seen[0]["journal"]["trades"][0]["decision"] == "10ccc308"
