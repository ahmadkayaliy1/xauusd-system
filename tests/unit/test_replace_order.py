"""F (owner, 2026-10-08): an answer that replaces its unfilled pending order - a new trade plus ``cancel_order`` of the
old same-side order in ONE answer - has the cancel carried out before the new order is gated, so the replacement is
placed at once; without the cancel the same-side check refuses it as before (never two orders on one side)."""
from __future__ import annotations

from tradingsystem.ai.store import DecisionRecord
from tradingsystem.core.timeutil import iso, now_ms

from .test_executor_paper import place
from .test_phase5_exec import PAIR, _decision, armed  # noqa: F401 - armed: the paper executor fixture

OLD = "e" * 32
NEW = "c" * 32


def _old_buy_limit(ex, q) -> None:
    """An executed BUY_LIMIT 15.00 below the ask, not filled (the price ran away from it)."""
    r = {"decision": "BUY", "order_type": "BUY_LIMIT", "entry": {"price": round(q.ask - 15, 2)},
         "stop_loss": round(q.ask - 25, 2), "take_profits": [{"price": round(q.ask + 20, 2), "close_fraction": 1.0}],
         "valid_until": iso(now_ms() + 3_600_000), "management": []}
    assert place(ex.paper, OLD, r, q, entry=round(q.ask - 15, 2))["ok"]
    ex.store.save(DecisionRecord(PAIR, "agent_per_pair", "t", "valid", id=OLD, ts=now_ms() - 600_000,
                                 recommendation={**r, "pair": PAIR, "timestamp": iso(now_ms() - 600_000)}))
    ex.store.set_execution_state(OLD, "executed", {"mode": "paper", "executed_levels": {"stop_loss": r["stop_loss"]}})


def _new_buy(ex, q, cancel_old: bool) -> None:
    rec = {"pair": PAIR, "decision": "BUY", "order_type": "MARKET", "timestamp": iso(now_ms() - 5_000),
           "valid_until": iso(now_ms() + 3_600_000), "entry": {"price": None}, "stop_loss": round(q.bid - 10, 2),
           "take_profits": [{"price": round(q.ask + 25, 2), "close_fraction": 1.0}], "confidence": 80,
           "risk_management": {"risk_percent_suggested": 1.0}, "management": [],
           "position_actions": ([{"target": {"decision": OLD[:8], "kind": "order"}, "action": "cancel_order",
                                  "reason": "unfilled: replaced by a shallower entry"}] if cancel_old else [])}
    r = DecisionRecord(PAIR, "agent_per_pair", "t", "valid", recommendation=rec, id=NEW)
    r.actions_state = "pending" if cancel_old else None
    ex.store.save(r)
    ex.store.set_execution_state(NEW, "queued")


def _live_kinds(ex, did):
    return [lg.kind for lg in ex.model_legs.legs_of(did)]


def test_a_replacement_cancels_the_old_order_first_and_is_placed(armed):  # noqa: F811
    ex, ticks = armed
    q = ticks[-1]
    _old_buy_limit(ex, q)
    _new_buy(ex, q, cancel_old=True)
    ex.step(False)
    state, detail = _decision(ex, NEW)
    assert state == "executed", [(c["check"], c["detail"]) for c in detail.get("gate", []) if not c["ok"]]
    assert "order" not in _live_kinds(ex, OLD)                          # the old pending order is gone
    assert _live_kinds(ex, NEW) == ["position"]


def test_without_the_cancel_the_same_side_order_is_still_refused(armed):  # noqa: F811
    ex, ticks = armed
    q = ticks[-1]
    _old_buy_limit(ex, q)
    _new_buy(ex, q, cancel_old=False)
    ex.step(False)
    state, detail = _decision(ex, NEW)
    assert state == "rejected" and "no_same_direction" in detail.get("reason", "")
    assert _live_kinds(ex, OLD) == ["order"]


def test_the_executor_measures_the_best_move_in_r_of_the_original_stop(armed):  # noqa: F811
    # a SELL filled 5.00 above the ask with its stop 15.00 above the fill: 5 / 15 R so far (no candles: the quote)
    from .test_phase5_exec import _open_sell_in_profit
    ex, ticks = armed
    did, sl0, fill = _open_sell_in_profit(ex, ticks[-1])
    [lg] = ex.model_legs.legs_of(did)
    assert abs(ex.excursion_r(lg) - 5.0 / 15.0) < 0.02
