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

    def _make(answers, votes=3, mode="live"):
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
        o = Orchestrator(s, InstrumentRegistry.from_settings(s), FakeBuilder(), store, usage,
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
