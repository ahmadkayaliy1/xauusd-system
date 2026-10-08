"""Entry votes (desk.entry_votes, self-consistency): a live desk's new trade is asked again of the same model and placed
only on a majority for its side; the first answer's levels and actions stand; NO_TRADE answers are never re-asked."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tradingsystem.ai import orchestrator as orch_mod
from tradingsystem.ai.budget import CostGovernor, UsageStore
from tradingsystem.ai.orchestrator import CycleRequest, Orchestrator
from tradingsystem.ai.store import DecisionStore
from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import AIBudgetCfg, DeskCfg, load_settings

from .test_ai_path_fixes import AS_OF, Double
from .test_orchestrator import FakeBuilder, rec_for

BUY, NO = rec_for(), rec_for(decision="NO_TRADE")
FAIL = RuntimeError("no answer within 180s")


@pytest.fixture
def desk(tmp_path, monkeypatch):
    made = []

    def _make(answers, votes=3, mode="live", flat=None):
        s = load_settings(env_path=Path("nope.env"), extra_env={"AGENT_MODE": "agent_per_pair"})
        pc = s.pairs["XAUUSD"]
        d = (pc.desk or DeskCfg()).model_copy(update={"mode": mode, "entry_votes": votes})
        s = s.model_copy(update={"pairs": {**s.pairs, "XAUUSD": pc.model_copy(update={"desk": d})}})
        left = list(answers)

        def act(schema, user):
            a = left.pop(0)
            if isinstance(a, Exception):
                raise a
            return dict(a)
        prov = Double(act)
        monkeypatch.setattr(orch_mod, "make_provider", lambda settings, name=None, **kw: prov)
        usage = UsageStore(tmp_path / "app.db")
        store = DecisionStore(tmp_path / "app.db", "cfg")
        made.append((usage, store))
        builder = FakeBuilder()
        if flat is not None:                  # the account block of a flat (or holding) desk
            builder.payload["account"] = {**builder.payload["account"],
                                          "open_positions": [] if flat else [{"decision": "aaaaaaaa", "side": "SELL"}]}
        o = Orchestrator(s, InstrumentRegistry.from_settings(s), builder, store, usage,
                         CostGovernor(AIBudgetCfg(), usage))
        [r] = asyncio.run(o.run_cycle([CycleRequest("XAUUSD", "test")], as_of=AS_OF))
        return r, prov
    yield _make
    for usage, store in made:
        usage.close()
        store.close()


def test_a_majority_places_the_first_answers_trade(desk):
    r, prov = desk([BUY, BUY, NO])
    assert len(prov.calls) == 3 and r.status == "valid" and r.recommendation["decision"] == "BUY"
    assert r.recommendation["stop_loss"] == BUY["stop_loss"]
    assert any(e.startswith("vote 2/3 BUY") and "placed" in e for e in r.errors)
    assert [s["role"] for s in r.sub_outputs].count("vote") == 2


def test_no_majority_is_a_valid_no_trade_that_says_why(desk):
    r, _ = desk([BUY, NO, NO])
    assert r.status == "valid" and r.recommendation["decision"] == "NO_TRADE"
    assert r.recommendation["stop_loss"] is None and "NOT placed" in r.recommendation["operator_notes"]
    assert any("no majority" in e for e in r.errors)


def test_an_unanswered_sample_counts_against(desk):
    r, _ = desk([BUY, BUY, FAIL])
    assert r.recommendation["decision"] == "BUY"                     # 2 of 3 still agree
    r, _ = desk([BUY, FAIL, FAIL])
    assert r.status == "valid" and r.recommendation["decision"] == "NO_TRADE"


def test_no_trade_is_never_re_asked_and_one_vote_is_off(desk):
    r, prov = desk([NO])
    assert len(prov.calls) == 1 and r.recommendation["decision"] == "NO_TRADE"
    r, prov = desk([BUY], votes=1)
    assert len(prov.calls) == 1 and r.recommendation["decision"] == "BUY"
    r, prov = desk([BUY], mode="shadow")                              # a shadow desk is never voted
    assert len(prov.calls) == 1


# ------------------------------------------------------------------ flat: every answer at once (no extra wait)
def test_flat_cycles_ask_every_answer_at_once_and_a_majority_trades(desk):
    r, prov = desk([NO, BUY, BUY], flat=True)                          # the first said no, the majority BUY
    assert len(prov.calls) == 3 and r.status == "valid" and r.recommendation["decision"] == "BUY"
    assert any(e.startswith("vote 2/3 BUY") for e in r.errors)
    assert [s["role"] for s in r.sub_outputs].count("vote") == 2


def test_flat_cycles_without_a_trade_majority_are_no_trade(desk):
    r, prov = desk([BUY, NO, NO], flat=True)
    assert len(prov.calls) == 3 and r.status == "valid" and r.recommendation["decision"] == "NO_TRADE"
    assert any("no majority" in e for e in r.errors)
    r, _ = desk([BUY, FAIL, FAIL], flat=True)                           # the lone trade answer is not placed
    assert r.status == "valid" and r.recommendation["decision"] == "NO_TRADE"
    assert "NOT placed" in r.recommendation["operator_notes"]
    r, _ = desk([NO, NO, NO], flat=True)                               # all three asked even when nothing trades
    assert r.recommendation["decision"] == "NO_TRADE"


def test_a_holding_desk_asks_once_and_votes_only_a_new_trade(desk):
    r, prov = desk([NO], flat=False)
    assert len(prov.calls) == 1
    r, prov = desk([BUY, BUY, NO], flat=False)                         # a new trade next to a live one: asked after
    assert len(prov.calls) == 3 and r.recommendation["decision"] == "BUY"
