"""D-058: the code's checks on a scalp plan (scalp/checks.py) - the stop / target / news / desk / level / rr rules on
synthetic inputs, and the tolerance floors."""
import datetime as dt

from tradingsystem.scalp.checks import check_plan, tolerance

AS_OF = 1_760_000_000_000
MIN = 60_000
LEVELS = [{"price": 4200.0}, {"price": 4185.0}]


def plan(**kw):
    p = {"level_price": 4200.0, "side": "BUY", "entry_type": "limit", "entry": 4200.0, "stop": 4197.0,
         "target": 4205.0, "rr": 1.67, "max_hold_min": 30}
    p.update(kw)
    return p


def check(p=None, **kw):
    args = dict(atr5m=4.0, spread=0.24, blackout=False, next_events=[], news_windows={"high": [30, 15]},
                desk_sides=[], levels=LEVELS, tol=1.0, as_of=AS_OF, trigger_window_min=60)
    args.update(kw)
    return check_plan(p or plan(), **args)


def test_tolerance_floors():
    assert tolerance(None, None) == 0.5
    assert tolerance(4.0, 0.24) == 1.0          # a quarter of the ATR wins
    assert tolerance(1.0, 0.4) == 0.8           # twice the spread wins
    assert tolerance(1.0, 0.1) == 0.5           # the floor


def test_all_good():
    c = check()
    assert all(c[k] for k in ("stop_ok", "target_ok", "news_ok", "desk_ok", "level_ok", "rr_ok"))
    assert c["rejected"] == [] and c["notes"] == []


def test_stop_inside_the_noise_is_hard():
    c = check(plan(stop=4198.5, target=4203.0, rr=2.0))         # $1.5 < max(0.5 x 4, 2)
    assert c["stop_ok"] is False and "stop_ok" in c["rejected"] and any("stop" in n for n in c["notes"])
    assert check(plan(stop=4197.5, target=4204.0, rr=2.6), atr5m=6.0)["stop_ok"] is False   # 2.5 < 3.0
    assert check(plan(stop=4197.5, target=4204.0, rr=2.6), atr5m=None)["stop_ok"] is True   # None = $2 floor


def test_target_under_the_costs_is_hard():
    assert check(plan(target=4204.0, rr=1.33))["target_ok"] is False          # 4 < 1.5 x 3
    c = check(plan(stop=4197.0, target=4205.0, rr=1.67), spread=1.2)           # 5 < 5 x 1.2
    assert c["target_ok"] is False and c["rejected"] == ["target_ok"]
    assert check(spread=None)["target_ok"] is True


def test_news_blackout_now_and_overlapping_window():
    c = check(blackout=True)
    assert c["news_ok"] is False and c["rejected"] == ["news_ok"]
    # the plan is exposed for trigger 60 + hold 30 = 90 min; the event window is [t-30, t+15]
    assert check(next_events=[[AS_OF + 90 * MIN, "high", "CPI", 90]])["news_ok"] is False
    assert check(next_events=[[AS_OF + 130 * MIN, "high", "CPI", 130]])["news_ok"] is True   # 100 > 90
    assert check(next_events=[[AS_OF + 120 * MIN, "high", "CPI", 120]])["news_ok"] is False  # 90 == 90: touches
    assert check(next_events=[[AS_OF + 50 * MIN, "medium", "PPI", 50]])["news_ok"] is True  # tier without a window
    assert check(next_events=[{"time_ms": AS_OF + 50 * MIN, "tier": "high"}])["news_ok"] is False
    # the iso form news.block writes: the time string is parsed, the minutes-ahead is only a fallback
    as_of = int(dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    assert check(next_events=[["2026-10-12T12:50:00.000Z", "high", "CPI", 999]], as_of=as_of)["news_ok"] is False
    assert check(next_events=[["2026-10-12T14:20:00.000Z", "high", "CPI", 0]], as_of=as_of)["news_ok"] is True
    assert check(next_events=[["not a time", "high", "CPI", 50]], as_of=as_of)["news_ok"] is False   # the fallback
    assert check(next_events=[["not a time", "high", "CPI"]], as_of=as_of)["news_ok"] is True       # nothing usable


def test_against_the_desk_is_hard():
    c = check(desk_sides=["SELL"])
    assert c["desk_ok"] is False and c["rejected"] == ["desk_ok"] and "SELL" in c["notes"][0]
    assert check(desk_sides=["BUY"])["desk_ok"] is True
    assert check(plan(side="SELL", stop=4203.0, target=4195.0, rr=1.67), desk_sides=["BUY_LIMIT"])["desk_ok"] is False
    assert check(desk_sides=[None, ""])["desk_ok"] is True


def test_level_and_rr_are_soft():
    c = check(plan(level_price=4190.0))
    assert c["level_ok"] is False and c["rejected"] == []
    assert check(plan(level_price=4200.9))["level_ok"] is True
    c = check(plan(target=4203.9, rr=1.3))
    assert c["rr_ok"] is False and c["target_ok"] is False and "rr_ok" not in c["rejected"]
    assert check(plan(rr=None))["rr_ok"] is True                       # computed from the prices: 5 / 3


def test_everything_fails_at_once():
    c = check(plan(stop=4199.0, target=4199.5, rr=1.0, level_price=4100.0), blackout=True, desk_sides=["SELL"])
    assert c["rejected"] == ["news_ok", "desk_ok", "stop_ok", "target_ok"] and len(c["notes"]) == 6
