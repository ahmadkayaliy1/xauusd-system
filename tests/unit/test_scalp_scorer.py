"""D-058: scoring a brief on ticks (scalp/scorer.py) - levels held / broken, fills by order type on bid / ask, target or
stop first within the hold, ties, expiry, gaps, maturity, the record aggregation and the tick reader's columns."""
from types import SimpleNamespace as NS

import numpy as np
import pytest

from tradingsystem.core.instruments import Instrument
from tradingsystem.core.timeframes import Timeframe
from tradingsystem.scalp import scorer
from tradingsystem.scalp.scorer import is_mature, maturity_ms, read_ticks, score_levels, score_plan, score_record

AS_OF = 1_760_000_000_000
MIN = 60_000
CFG = NS(trigger_window_min=60, level_horizon_min=60, hold_window_min=30)
SPREAD = 0.2


def ticks(mids, start=AS_OF, step_s=10, spread=SPREAD):
    """One tick per ``step_s`` seconds from ``start``, the mid following ``mids``."""
    m = np.asarray(mids, dtype=np.float64)
    t = start + np.arange(len(m), dtype=np.int64) * step_s * 1000
    return t, m - spread / 2, m + spread / 2


def flat(price, minutes, **kw):
    return ticks([price] * (minutes * 6 + 1), **kw)


def plan(**kw):
    p = {"level_price": 4200.0, "side": "BUY", "entry_type": "limit", "entry": 4200.0, "stop": 4197.0,
         "target": 4206.0, "rr": 2.0, "max_hold_min": 30}
    p.update(kw)
    return p


# ------------------------------------------------------------------------------------------------- levels
def levels(mids, price, price_at=4210.0, tol=1.0):
    t, b, a = ticks(mids)
    return score_levels([{"price": price}], t, b, a, as_of=AS_OF, price_at=price_at, tol=tol, horizon_ms=60 * MIN,
                        hold_ms=30 * MIN)[0]


def test_level_untouched_and_role():
    r = levels([4210.0] * 361, 4200.0)
    assert r["outcome"] == "untouched" and r["touched_ms"] is None and r["role"] == "support"
    assert levels([4210.0] * 361, 4220.0)["role"] == "resistance"


def test_support_held_then_broken():
    held = levels([4210.0] * 10 + [4200.5] + [4202.0] * 350, 4200.0)          # away >= tol on the top side first
    assert held["outcome"] == "held" and held["touched_ms"] == AS_OF + 10 * 10_000
    broken = levels([4210.0] * 10 + [4200.5] + [4198.5] * 350, 4200.0)        # through by > tol first
    assert broken["outcome"] == "broken"
    assert levels([4210.0] * 10 + [4200.5] * 351, 4200.0)["outcome"] == "undecided"


def test_resistance_held_requires_the_move_within_the_hold_window():
    # touched at +100 s, the move away only 31 min later: outside hold_ms -> undecided
    mids = [4190.0] * 10 + [4200.2] * (31 * 6) + [4195.0] * 100
    assert levels(mids, 4200.0, price_at=4190.0)["outcome"] == "undecided"
    mids = [4190.0] * 10 + [4200.2] * (20 * 6) + [4195.0] * 100
    assert levels(mids, 4200.0, price_at=4190.0)["outcome"] == "held"
    mids = [4190.0] * 10 + [4200.2] * 5 + [4202.0] * 100
    assert levels(mids, 4200.0, price_at=4190.0)["outcome"] == "broken"


def test_level_unknown_when_the_ticks_are_missing():
    t, b, a = ticks([4210.0] * 30)                      # 5 min of ticks only
    r = score_levels([{"price": 4200.0}], t, b, a, as_of=AS_OF, price_at=4210.0, tol=1.0, horizon_ms=60 * MIN,
                     hold_ms=30 * MIN)[0]
    assert r["outcome"] == "unknown"
    r = score_levels([{"price": 4200.0}], np.empty(0, np.int64), np.empty(0), np.empty(0), as_of=AS_OF,
                     price_at=4210.0, tol=1.0, horizon_ms=60 * MIN, hold_ms=30 * MIN)[0]
    assert r["outcome"] == "unknown"


# ------------------------------------------------------------------------------------------------- plans
def scored(mids, p=None, **kw):
    t, b, a = ticks(mids)
    return score_plan(p or plan(), t, b, a, as_of=AS_OF, trigger_window_ms=60 * MIN, tol=1.0, **kw)


def test_buy_limit_fills_on_the_ask_and_hits_the_target_on_the_bid():
    # mid 4199.9 -> ask 4200.0 = entry fills; mid 4206.0 -> bid 4205.9 < target; mid 4206.2 -> bid 4206.1 = target
    r = scored([4203.0] * 6 + [4199.9] + [4206.0] * 5 + [4206.2] + [4210.0] * 300)
    assert r["outcome"] == "target" and r["fill_ms"] == AS_OF + 60_000 and r["r"] == 2.0
    assert r["exit_ms"] == AS_OF + 120_000 and r["minutes"] == 1.0
    assert r["mfe_r"] == pytest.approx(2.0, abs=0.05) and r["mae_r"] == pytest.approx(-0.067, abs=0.01)
    assert scored([4203.0] * 6 + [4200.3] + [4210.0] * 400)["outcome"] == "not_triggered"   # ask 4200.4 > entry
    assert scored([4203.0] * 6 + [4200.3] + [4210.0] * 300)["outcome"] == "unknown"         # 51 min of ticks only


def test_sell_limit_fills_on_the_bid_and_exits_on_the_ask():
    p = plan(side="SELL", stop=4203.0, target=4194.0)
    r = scored([4197.0] * 6 + [4200.1] + [4194.0] * 300, p)      # bid 4200.0 fills; ask 4194.1 > target ... yet
    assert r["outcome"] == "expired"
    r = scored([4197.0] * 6 + [4200.1] + [4193.8] * 300, p)      # ask 4193.9 <= target
    assert r["outcome"] == "target" and r["r"] == 2.0


def test_stop_and_market_entries_need_the_touch_first():
    p = plan(entry_type="stop", level_price=4198.0, entry=4200.0, stop=4197.0, target=4206.0)
    assert scored([4203.0] * 400, p)["outcome"] == "not_triggered"             # ask above entry, never touched
    r = scored([4203.0] * 6 + [4198.5] + [4200.5] * 300, p)                   # touched, then ask 4200.6 >= entry
    assert r["outcome"] == "expired" and r["fill_ms"] == AS_OF + 70_000
    p = plan(side="SELL", entry_type="market_after_rejection", level_price=4202.0, entry=4200.0, stop=4203.0,
             target=4194.0)
    assert scored([4197.0] * 400, p)["outcome"] == "not_triggered"
    assert scored([4197.0] * 6 + [4202.3] + [4199.5] * 300, p)["fill_ms"] == AS_OF + 70_000


def test_stop_first_and_same_tick_tie_is_a_stop():
    r = scored([4199.9] + [4196.0] * 300)
    assert r["outcome"] == "stop" and r["r"] == -1.0 and r["mfe_r"] == 0.0 and r["mae_r"] <= -1.0
    t, b, a = ticks([4199.9, 4199.9])
    b[1], a[1] = 4196.0, 4207.0                                   # one tick spanning both: the stop wins
    r = score_plan(plan(), t, b, a, as_of=AS_OF, trigger_window_ms=60 * MIN, tol=1.0)
    assert r["outcome"] == "stop" and r["exit_ms"] == AS_OF + 10_000      # decided: later missing ticks are moot
    # a hole between the fill and the exit: unknown
    t, b, a = ticks([4199.9] + [4201.0] * 30 + [4196.0] * 10)
    t[31:] += 6 * MIN
    r = score_plan(plan(), t, b, a, as_of=AS_OF, trigger_window_ms=60 * MIN, tol=1.0)
    assert r["outcome"] == "unknown" and r["r"] is None


def test_target_outside_max_hold_is_an_expiry_with_the_open_pnl():
    mids = [4199.9] + [4201.5] * (30 * 6) + [4210.0] * 100    # the target comes 31 min after the fill
    r = scored(mids, plan(max_hold_min=30))
    assert r["outcome"] == "expired" and r["minutes"] == 30.0
    assert r["r"] == pytest.approx((4201.4 - 4200.0) / 3.0, abs=0.01)         # bid 4201.4 at the last held tick
    assert scored(mids, plan(max_hold_min=40))["outcome"] == "target"


def test_not_triggered_outside_the_trigger_window():
    mids = [4203.0] * (61 * 6) + [4199.0] * 100                  # the dip comes after 61 min
    assert scored(mids)["outcome"] == "not_triggered"


def test_gaps_make_the_outcome_unknown():
    t, b, a = ticks([4203.0] * 361)                              # 60 min at 10 s, no fill
    keep = (t < AS_OF + 10 * MIN) | (t > AS_OF + 16 * MIN)       # a 6-minute hole
    r = score_plan(plan(), t[keep], b[keep], a[keep], as_of=AS_OF, trigger_window_ms=60 * MIN, tol=1.0)
    assert r["outcome"] == "unknown"
    # ticks stop 20 min after the fill: the hold window is not covered
    r = scored([4199.9] + [4201.0] * (20 * 6), plan(max_hold_min=30))
    assert r["outcome"] == "unknown" and r["fill_ms"] == AS_OF
    r = score_plan(plan(), np.empty(0, np.int64), np.empty(0), np.empty(0), as_of=AS_OF, trigger_window_ms=60 * MIN,
                   tol=1.0)
    assert r["outcome"] == "unknown"


# ------------------------------------------------------------------------------------------------- the record
def record(plans=None, levels=None, checks=None, as_of=AS_OF):
    return {"id": "x", "as_of_ms": as_of, "price_at": 4203.0, "atr5m": 4.0, "spread_median": 0.24, "tol": 1.0,
            "brief": {"levels": levels if levels is not None else [{"price": 4200.0}, {"price": 4210.0}],
                      "plans": plans if plans is not None else
                      [plan(), plan(side="SELL", level_price=4210.0, entry=4210.0, stop=4213.0, target=4204.0)]},
            "checks": checks or {}}


def test_maturity():
    rec = record()
    assert maturity_ms(rec, CFG) == AS_OF + 90 * MIN                  # trigger 60 + hold 30 = level 60 + 30
    assert maturity_ms(record(plans=[plan(max_hold_min=120)]), CFG) == AS_OF + 180 * MIN
    assert maturity_ms(record(plans=[]), CFG) == AS_OF + 90 * MIN
    assert not is_mature(rec, AS_OF + 89 * MIN, CFG) and is_mature(rec, AS_OF + 90 * MIN, CFG)


def test_score_record_aggregates_and_skips_rejected_plans():
    mids = [4203.0] * 6 + [4199.9] + [4206.2] + [4203.0] * (95 * 6)
    t, b, a = ticks(mids)
    rec = record(checks={"1": {"rejected": ["desk_ok"]}})
    sc = score_record(rec, t, b, a, now=AS_OF + 100 * MIN, cfg=CFG)
    assert [x["outcome"] for x in sc["levels"]] == ["held", "untouched"]
    assert [p["idx"] for p in sc["plans"]] == [0, 1]
    assert sc["plans"][0]["outcome"] == "target" and sc["plans"][1]["outcome"] == "rejected"
    s = sc["summary"]
    assert s == {"levels_touched": 1, "levels_held": 1, "levels_unknown": 0, "plans_n": 1, "triggered": 1,
                 "target_first": 1, "stop_first": 0, "expired": 0, "unknown": 0, "rejected": 1, "sum_r": 2.0,
                 "ticks": len(t), "scored_at_ms": AS_OF + 100 * MIN}
    # plain lists in, the same out
    sc2 = score_record(rec, list(t), list(b), list(a), now=AS_OF + 100 * MIN, cfg=CFG)
    assert sc2["summary"] == s


def test_score_record_without_ticks_is_all_unknown():
    sc = score_record(record(), [], [], [], now=AS_OF + 100 * MIN, cfg=CFG)
    assert all(x["outcome"] == "unknown" for x in sc["levels"]) and all(p["outcome"] == "unknown" for p in sc["plans"])
    assert sc["summary"]["plans_n"] == 0 and sc["summary"]["unknown"] == 2 and sc["summary"]["levels_unknown"] == 2


# ------------------------------------------------------------------------------------------------- read_ticks
def test_read_ticks_uses_the_ticks_spec_columns():
    inst = Instrument(pair="XAUUSD", venue="mt5", symbol="XAUUSD@", roles=("primary", "execution"),
                      datatypes=("ticks", "candles"), timeframes=(Timeframe.parse("5m"),))
    calls = []

    class Reader:
        def read_range(self, spec, t0, t1, cols):
            calls.append((spec, t0, t1, tuple(cols)))
            return {"time_msc": np.array([3, 1, 2]), "bid": np.array([3.0, 1.0, 2.0]), "ask": np.array([3.2, 1.2, 2.2])}

    t, bid, ask = read_ticks(Reader(), inst, 100, 200)
    spec, t0, t1, cols = calls[0]
    assert spec.datatype == "ticks" and spec.name == "xauusd_ticks" and spec.time_col == "time_msc"
    assert set(cols) <= set(spec.column_names) and cols == ("time_msc", "bid", "ask")
    assert (t0, t1) == (100, 201)                                     # the end is inclusive
    assert t.dtype == np.int64 and t.tolist() == [1, 2, 3] and bid.tolist() == [1.0, 2.0, 3.0]
    assert ask.tolist() == pytest.approx([1.2, 2.2, 3.2])


def test_fetch_deals_refuses_without_a_running_terminal(monkeypatch):
    import tradingsystem.ingest.mt5.terminal as term
    monkeypatch.setattr(term, "terminal_running", lambda path, min_age_s=0.0: False)
    s = NS(mt5_data_profile=lambda: NS(terminal_path="C:/nowhere/terminal64.exe"))
    with pytest.raises(term.MT5Unavailable):
        scorer.fetch_deals(s, AS_OF, AS_OF + MIN)
