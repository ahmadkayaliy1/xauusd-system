"""D-052: the gold desk in ``mode: live`` (the owner's decision for the demo account) - the desk trades through the
ordinary path with the owner's gold limits (15 % per trade at the 1-oz minimum, 30 % per day, leverage up to 60x, no
account drawdown stop), entry calls all day with ``windows: []``, a review call before a fomc/high release and before
the weekend flat, and the weekend flat itself (a system ``close_all`` rule at ``desk.flat_before_weekend``).

The gate, the sizing, the store, the paper account and the management are the real code (``_desk_harness``); the
quotes, the ATR and the clock are the harness's labelled stand-ins. The news calendar file is written in the stored
format with the real Forex Factory titles of the week of 2026-09-28."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tradingsystem.analysis.desk_windows import (WEEKEND_FLAT_SPAN_MS, desk_window_state, friday_flat_ms,
                                                 last_window_end, weekend_flat_ms)
from tradingsystem.core.settings import DeskCfg, DeskClockCfg, DeskDateFlatCfg, DeskWindowCfg, load_settings
from tradingsystem.core.timeutil import MS_PER_HOUR, MS_PER_MINUTE, iso
from tradingsystem.execution import desk as desk_mod
from tradingsystem.execution import executor as ex_mod
from tradingsystem.execution.management import rules_of

from . import _desk_harness as H
from .test_desk_windows import WED_0510, fire_as, n_events, tick
from .test_desk_windows import desk  # noqa: F401
from .test_engine_rationing import eng  # noqa: F401

UTC = dt.timezone.utc
D052_RISK = {"max_risk_per_trade_pct": 15.0, "max_daily_loss_pct": 30.0, "max_effective_leverage": 60.0,
             "account_drawdown_stop_pct": 100.0, "max_correlated_risk_pct": 30.0,
             "max_losing_trades_per_day": 3}
FLAT = DeskClockCfg(time="16:30", tz="America/New_York")
LIVE = DeskCfg(mode="live", brief="desks/xau_live", windows=[], friday_cutoff=DeskClockCfg(time="15:00",
               tz="America/New_York"), flat_before_weekend=FLAT, pre_news_review_min=15)


def utc(y, mo, d, h, mi=0):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp() * 1000)


class Open:
    def is_open(self, ms):
        return True


# ------------------------------------------------------------------------------------------------ settings
def test_live_mode_all_day_windows_and_the_weekend_flat_validate():
    assert LIVE.mode == "live" and LIVE.windows == [] and LIVE.flat_before_weekend.time == "16:30"
    assert DeskCfg().mode == "shadow" and len(DeskCfg().windows) == 2       # the code default is unchanged
    with pytest.raises(ValidationError):
        DeskCfg(mode="paper")
    with pytest.raises(ValidationError):                                     # a shadow desk holds nothing to flatten
        DeskCfg(mode="shadow", flat_before_weekend=FLAT)
    with pytest.raises(ValidationError):
        DeskCfg(mode="live", pre_news_review_min=121)


def test_this_folders_config_is_the_owners_d052():
    cfg = Path(__file__).resolve().parents[2] / "config" / "config.yaml"      # the folder's own (D-052)
    s = load_settings(cfg, local_path=cfg.parent / "no.local.yaml", env_path=cfg.parent / "no.env",
                      extra_env={"TS_INSTANCE": "XAUUSD"})
    d = s.pairs["XAUUSD"].desk
    assert (d.mode, d.brief, d.windows, d.reopen_grace_min) == ("live", "desks/xau_live", [], 60)
    assert (d.friday_cutoff.time, d.flat_before_weekend.time, d.flat_before_weekend.tz) == \
        ("15:00", "16:30", "America/New_York")
    assert {k: getattr(s.risk, k) for k in D052_RISK} == D052_RISK
    assert s.risk.risk_per_trade_pct == 1.0 and s.risk.min_rr == 1.5 and s.risk.min_confidence == 55
    assert (s.monitor.equity_drop_warn_pct, s.monitor.equity_drop_kill_pct) == (15.0, 35.0)
    assert s.risk.correlated_groups == [] and s.pairs["XAUUSD"].news_blackout.enabled


# ------------------------------------------------------------------------------------------------ the clock
def test_no_windows_means_all_day_with_the_other_reasons_unchanged():
    wed_asia = utc(2026, 9, 30, 2, 0)                    # Asia: the shadow desk was closed here (outside_window)
    assert desk_window_state(LIVE, wed_asia, Open()) == (True, "all_day")
    assert desk_window_state(LIVE, wed_asia, Open(), news="news_blackout") == (False, "news_blackout")
    fri_1501_ny = utc(2026, 10, 2, 19, 1)                # EDT: 15:01 New York
    assert desk_window_state(LIVE, fri_1501_ny, Open()) == (False, "friday_cutoff")
    assert desk_window_state(LIVE, fri_1501_ny - 2 * MS_PER_MINUTE, Open()) == (True, "all_day")
    assert last_window_end(LIVE, wed_asia + 5 * MS_PER_HOUR) == utc(2026, 9, 30, 0, 0)   # one skip event a day


def test_the_weekend_flat_follows_new_york_time_and_lasts_the_rest_of_friday():
    edt = utc(2026, 10, 2, 20, 30)                       # Friday 16:30 EDT
    est = utc(2026, 12, 4, 21, 30)                       # Friday 16:30 EST
    assert friday_flat_ms(LIVE, utc(2026, 9, 28, 9)) == edt          # asked on the Monday of that week
    assert friday_flat_ms(LIVE, utc(2026, 12, 1, 9)) == est
    assert weekend_flat_ms(LIVE, edt - 1) is None and weekend_flat_ms(LIVE, edt) == edt
    assert weekend_flat_ms(LIVE, edt + WEEKEND_FLAT_SPAN_MS - 1) == edt
    assert weekend_flat_ms(LIVE, utc(2026, 10, 4, 22, 5)) is None    # the Sunday reopen: the gap is behind us
    shadow = DeskCfg()
    assert friday_flat_ms(shadow, edt) is None and weekend_flat_ms(LIVE.model_copy(
        update={"flat_before_weekend": None}), edt) is None


# ------------------------------------------------------------------------------------------------ the gate
def live_settings(tmp_path, equity=107.38, desk=LIVE):
    s = H.settings(tmp_path, desk=True, equity=equity)
    pairs = {**s.pairs, "XAUUSD": s.pairs["XAUUSD"].model_copy(update={"desk": desk})}
    return s.model_copy(update={"pairs": pairs, "risk": s.risk.model_copy(update=D052_RISK)})


def run(tmp_path, monkeypatch, *, equity=107.38, desk=LIVE, **kw):
    ex = H.make_executor(live_settings(tmp_path, equity, desk), monkeypatch)
    did = "a" * 32
    ex.handle(H.store_candidate(ex, "XAUUSD", H.rec("XAUUSD", **kw), did))
    st, det = H.stored(ex, did)
    return ex, did, st, det


def failed(det):
    return [g["check"] for g in det.get("gate", []) if not g["ok"]]


def test_a_live_desk_idea_is_executed_at_one_ounce_within_the_owners_limits(tmp_path, monkeypatch):
    ex, did, st, det = run(tmp_path, monkeypatch, sl_dist=9.6)
    try:
        assert st == "executed" and failed(det) == [] and not det.get("shadow")
        assert det["lots"] == 0.01 and 8.9 < det["risk_pct"] < 9.0          # $9.60 of $107.38
        assert not ex.shadow_pairs and [lg["status"] for lg in ex.paper.decision_legs(did)] == ["open"]
    finally:
        ex.paper.close()


@pytest.mark.parametrize("kw, check", [({"sl_dist": 17.0}, "position_size"),            # 15.8 % > 15 %
                                       ({"sl_dist": 9.6, "equity": 70.0}, "effective_leverage")])   # 61x > 60x
def test_beyond_the_owners_limits_the_gate_refuses(tmp_path, monkeypatch, kw, check):
    ex, did, st, det = run(tmp_path, monkeypatch, **kw)
    try:
        assert st == "rejected" and check in failed(det) and not ex.paper.decision_legs(did)
    finally:
        ex.paper.close()


# ------------------------------------------------------------------------------------------------ the desk's duties
def calendar(path, events, fetched_ms):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "fetched_ms": fetched_ms, "fetched": iso(fetched_ms),
                                "source_url": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                                "events": events}), encoding="utf-8")


def review_events(ex):
    con = sqlite3.connect(ex.app_db)
    try:
        return [json.loads(r[0]) for r in con.execute("SELECT detail FROM ingestion_events WHERE event='desk_review'")]
    finally:
        con.close()


def test_an_open_trade_gets_one_review_call_before_a_high_release(tmp_path, monkeypatch):
    ex, did, st, _ = run(tmp_path, monkeypatch, sl_dist=9.6)
    try:
        assert st == "executed"
        nfp = H.NOW + 10 * MS_PER_MINUTE
        calendar(ex.s.paths.shared() / "news_calendar.json",
                 [[nfp, "Non-Farm Employment Change", "USD", "High"],
                  [H.NOW + 5 * MS_PER_MINUTE, "Unemployment Claims", "USD", "Medium"]], H.NOW - MS_PER_HOUR)
        ex.desk_reviews()
        ex.desk_reviews()                                   # the same occasion: never twice
        (ev,) = review_events(ex)
        assert ev["pair"] == "XAUUSD" and ev["kind"] == "news" and "Non-Farm Employment Change" in ev["text"]
        assert "10 min" in ev["text"]
    finally:
        ex.paper.close()


def test_without_an_open_trade_or_beyond_the_lead_time_no_review(tmp_path, monkeypatch):
    s = live_settings(tmp_path)
    ex = H.make_executor(s, monkeypatch)
    try:
        calendar(ex.s.paths.shared() / "news_calendar.json",
                 [[H.NOW + 10 * MS_PER_MINUTE, "Non-Farm Employment Change", "USD", "High"]], H.NOW - MS_PER_HOUR)
        ex.desk_reviews()                                   # nothing open
        assert review_events(ex) == []
    finally:
        ex.paper.close()
    pcfg = s.pairs["XAUUSD"]
    far = [[H.NOW + 40 * MS_PER_MINUTE, "Non-Farm Employment Change", "USD", "High"]]
    calendar(tmp_path / "far" / "news_calendar.json", far, H.NOW - MS_PER_HOUR)
    assert desk_mod.reviews_due(pcfg, tmp_path / "far", H.NOW) == []


def test_the_review_before_the_weekend_flat_is_due_in_its_last_half_hour():
    pcfg = SimpleNamespace(desk=LIVE, news_blackout=SimpleNamespace(enabled=False))
    flat = utc(2026, 10, 2, 20, 30)
    assert desk_mod.reviews_due(pcfg, None, flat - 31 * MS_PER_MINUTE) == []
    ((kind, key, text),) = desk_mod.reviews_due(pcfg, None, flat - 20 * MS_PER_MINUTE)
    assert (kind, key) == ("weekend", str(flat)) and "20 min" in text
    assert desk_mod.reviews_due(pcfg, None, flat) == []


def test_the_weekend_flat_closes_the_open_trade_through_the_management(tmp_path, monkeypatch):
    ex, did, st, _ = run(tmp_path, monkeypatch, sl_dist=9.6)
    try:
        assert st == "executed"
        flat = friday_flat_ms(LIVE, H.NOW)
        monkeypatch.setattr(ex_mod, "now_ms", lambda: flat - MS_PER_MINUTE)
        ex.manage_positions()                                # before the flat: nothing happens
        assert [lg["status"] for lg in ex.paper.decision_legs(did)] == ["open"]
        monkeypatch.setattr(ex_mod, "now_ms", lambda: flat + MS_PER_MINUTE)
        ex.manage_positions()
        legs = ex.paper.decision_legs(did)
        assert [lg["status"] for lg in legs] == ["closed"]
        assert legs[0]["close_reason"] == "rule"            # the trade declared none: the desk's flat rule
    finally:
        ex.paper.close()


def test_the_flat_rule_is_appended_after_the_trades_own_rules_and_the_stored_decision_is_untouched():
    own = [{"action": "move_sl_to_breakeven", "trigger": "tp_hit", "value": 1, "params": {}}]
    d = {"id": "x", "pair": "XAUUSD", "recommendation": {}, "execution_detail": {"executed_management": own}}
    out = desk_mod.with_weekend_flat(d, 123)
    assert rules_of(out)[:1] == own and rules_of(out)[1]["trigger"] == "at_time" and rules_of(out)[1]["value"] == 123
    assert d["execution_detail"]["executed_management"] == own


# ------------------------------------------------------------------------------------------------ the engine
def make_live(e, windows=None):
    pc = e.s.pairs["XAUUSD"]
    d = pc.desk.model_copy(update={"mode": "live", "windows": [] if windows is None else windows})
    e.s = e.s.model_copy(update={"pairs": {**e.s.pairs, "XAUUSD": pc.model_copy(update={"desk": d})}})


def test_an_all_day_live_desk_calls_where_the_shadow_desk_waited(desk):
    make_live(desk)
    tick(desk, WED_0510)                                  # outside both former windows
    assert "XAUUSD" in desk.rec.dispatched
    assert n_events(desk, "skipped: outside_desk_window") == 0


def test_a_live_desk_with_unknown_holdings_keeps_its_review_calls(desk, monkeypatch):
    make_live(desk, windows=desk.s.pairs["XAUUSD"].desk.windows)   # windows kept: WED_0510 is outside them
    fire_as(desk, monkeypatch, "review: price_above", "review")
    tick(desk, WED_0510)                                  # the executor reports nothing: holdings unknown
    assert "XAUUSD" in desk.rec.dispatched                # a live desk may hold a trade: not held back


# ------------------------------------------------------------------------------------------------ the brief
def test_the_live_brief_is_versioned_small_and_says_the_live_rules():
    from tradingsystem.ai.prompts import _read_versioned
    text, name, version = _read_versioned("desks/xau_live.md")
    assert (name, version) == ("desks/xau_live", 3)          # v3 = B's entries + the fixed bracket (D-053)
    assert len(text) / 3.6 <= 900                          # ≈ 840 tokens in the cached system prompt
    assert "15:00 New York" in text and "holiday close" in text
    assert "SENT" in text and "15 %" in text and "30 %" in text and "16:30 New York" in text
    assert "fixed bracket" in text and "breakeven once the move reaches 2R" in text and "NO_TRADE" in text
    assert "do not move the stop or the target" in text


# ------------------------------------------------------------------------------------------------ the review's fixes
HOLIDAY = LIVE.model_copy(update={"holiday_flats": [
    DeskDateFlatCfg(date="2026-11-26", time="12:00", tz="America/New_York")]})       # Thanksgiving, EST: 17:00 UTC


def test_a_holiday_flat_closes_entries_90_min_before_and_flattens_for_the_rest_of_the_session():
    flat = utc(2026, 11, 26, 17, 0)
    assert desk_window_state(HOLIDAY, flat - 95 * MS_PER_MINUTE, Open()) == (True, "all_day")
    assert desk_window_state(HOLIDAY, flat - 75 * MS_PER_MINUTE, Open()) == (False, "pre_flat")
    assert desk_window_state(HOLIDAY, flat + 5 * MS_PER_MINUTE, Open()) == (False, "pre_flat")
    assert weekend_flat_ms(HOLIDAY, flat + 5 * MS_PER_MINUTE) == flat
    assert weekend_flat_ms(HOLIDAY, flat - 1) is None
    shadow = DeskCfg()                                       # the shadow desk's clock is unchanged
    assert desk_window_state(shadow, utc(2026, 9, 23, 7, 0), Open()) == (True, "london")


def test_the_review_before_a_holiday_flat_names_it():
    pcfg = SimpleNamespace(desk=HOLIDAY, news_blackout=SimpleNamespace(enabled=False))
    flat = utc(2026, 11, 26, 17, 0)
    ((kind, key, text),) = desk_mod.reviews_due(pcfg, None, flat - 10 * MS_PER_MINUTE)
    assert (kind, key) == ("weekend", str(flat)) and "holiday" in text


def test_the_gate_refuses_a_live_desk_entry_outside_its_clock(tmp_path, monkeypatch):
    london_only = LIVE.model_copy(update={"windows": [DeskWindowCfg(name="london", tz="Europe/London",
                                                                    start="07:45", end="11:00")]})
    ex, did, st, det = run(tmp_path, monkeypatch, sl_dist=9.6, desk=london_only)      # NOW = 14:13 UTC: outside
    try:
        assert st == "rejected" and failed(det) == ["desk_clock"] and not ex.paper.decision_legs(did)
        g = next(x for x in det["gate"] if x["check"] == "desk_clock")
        assert "outside_window" in g["detail"]
    finally:
        ex.paper.close()


def test_the_gate_check_exists_only_for_a_live_desk(tmp_path, monkeypatch):
    ex, did, st, det = run(tmp_path, monkeypatch, sl_dist=9.6)
    try:
        assert next(x for x in det["gate"] if x["check"] == "desk_clock")["ok"] is True
    finally:
        ex.paper.close()
    s = H.settings(tmp_path / "plain", desk=False)
    ex2 = H.make_executor(s, monkeypatch)
    try:
        assert ex2._desk_check("XAUUSD") is None and ex2._desk_check("BTCUSDT") is None
    finally:
        ex2.paper.close()


def test_unknown_holdings_hold_back_a_live_desks_entry_trigger_but_not_its_review(desk, monkeypatch):
    make_live(desk, windows=desk.s.pairs["XAUUSD"].desk.windows)   # WED_0510 is outside the windows
    fire_as(desk, monkeypatch, "new strong setup", "strong")
    tick(desk, WED_0510)                                  # the executor reports nothing: holdings unknown
    assert "XAUUSD" not in desk.rec.dispatched
    desk.rec.dispatched.clear()
    fire_as(desk, monkeypatch, "review: price_above", "review")
    tick(desk, WED_0510 + 5 * MS_PER_MINUTE)
    assert "XAUUSD" in desk.rec.dispatched


# ------------------------------------------------------------------------------------------------ the day's loss stop
def add_losses(db, n, at, pair="XAUUSD", first=0):
    import sqlite3 as _sq
    from tradingsystem.ai.store import DecisionRecord, DecisionStore
    st = DecisionStore(db)
    try:
        for k in range(first, first + n):
            rec = DecisionRecord(pair, "agent_per_pair", "t", "valid", recommendation={"decision": "SELL"},
                                 id=f"{k:031x}f", ts=at - 3_600_000)
            st.save(rec)
    finally:
        st.close()
    con = _sq.connect(db)
    con.execute("UPDATE ai_decisions SET execution_state='executed', outcome='closed_loss', outcome_ts=? "
                "WHERE id LIKE '%f'", (at - 600_000,))
    con.commit()
    con.close()


def test_the_gate_refuses_a_new_entry_after_the_days_third_losing_trade(tmp_path, monkeypatch):
    s = live_settings(tmp_path)
    ex = H.make_executor(s, monkeypatch)
    try:
        add_losses(ex.app_db, 2, H.NOW)
        assert ex._losing_trades_today("XAUUSD") == 2
        ex.handle(H.store_candidate(ex, "XAUUSD", H.rec("XAUUSD", sl_dist=9.6), "b" * 32))
        assert H.stored(ex, "b" * 32)[0] == "executed"                  # two losses: still allowed
        add_losses(ex.app_db, 1, H.NOW, first=2)                        # the third
        assert ex._losing_trades_today("XAUUSD") == 3
        ex.handle(H.store_candidate(ex, "XAUUSD", H.rec("XAUUSD", side="SELL", sl_dist=9.6), "c" * 32))
        st, det = H.stored(ex, "c" * 32)
        assert st == "rejected" and failed(det) == ["losing_trades_today"]
    finally:
        ex.paper.close()


def test_without_the_setting_there_is_no_such_check(tmp_path, monkeypatch):
    ex, did, st, det = run(tmp_path, monkeypatch, sl_dist=9.6)
    try:
        assert any(g["check"] == "losing_trades_today" for g in det["gate"])     # the live settings carry it
    finally:
        ex.paper.close()
    s = H.settings(tmp_path / "plain", desk=False)
    assert s.risk.max_losing_trades_per_day is None                           # BTC/ETH and the code default: off


def test_after_the_loss_stop_the_engine_makes_no_entry_call_but_keeps_reviews(desk, monkeypatch):
    make_live(desk)
    desk.s = desk.s.model_copy(update={"risk": desk.s.risk.model_copy(update={"max_losing_trades_per_day": 3})})
    add_losses(desk.s.paths.state() / "app.db", 3, WED_0510)
    fire_as(desk, monkeypatch, "new strong setup", "strong")
    tick(desk, WED_0510)
    assert "XAUUSD" not in desk.rec.dispatched and n_events(desk, "skipped: loss_stop") == 1


def test_back_after_an_outage_with_a_trade_open_the_desk_asks_for_one_review(tmp_path, monkeypatch):
    ex, did, st, _ = run(tmp_path, monkeypatch, sl_dist=9.6)
    try:
        assert st == "executed"
        ex.down_since, ex.started = H.NOW - 20 * MS_PER_MINUTE, H.NOW      # the previous run's last beat: 20 min ago
        ex.desk_reviews()
        ex.desk_reviews()
        (ev,) = review_events(ex)
        assert ev["kind"] == "resume" and "20 min" in ev["text"]
        ex2_down = H.NOW - 2 * MS_PER_MINUTE                               # a short restart: no review
        ex.down_since, ex.started = ex2_down, H.NOW + 1
        ex.desk_reviews()
        assert len(review_events(ex)) == 1
    finally:
        ex.paper.close()


# ------------------------------------------------------------------------------------- D-053: the fixed bracket
def test_the_be_2r_policy_replaces_the_trades_own_rules_and_the_weekend_flat_follows():
    own = [{"action": "trail_structure", "trigger": "candle_close", "value": 4146.0, "params": {}},
           {"action": "move_sl_to_breakeven", "trigger": "r_multiple", "value": 1.0, "params": {}}]
    d = {"id": "x", "pair": "XAUUSD", "recommendation": {}, "execution_detail": {"executed_management": own}}
    out = desk_mod.with_weekend_flat(desk_mod.with_mgmt_policy(d, "be_2r"), 123)
    assert [(r["action"], r["trigger"], r["value"]) for r in rules_of(out)] == [
        ("move_sl_to_breakeven", "r_multiple", 2.0), ("close_all", "at_time", 123)]
    assert d["execution_detail"]["executed_management"] == own          # the stored decision is untouched
    assert desk_mod.with_mgmt_policy(d, "model") is d
    assert DeskCfg().management == "model"
    with pytest.raises(ValidationError):
        DeskCfg(management="trail")


def test_the_executor_applies_the_desks_policy_and_knows_its_fixed_bracket():
    fixed = SimpleNamespace(s=SimpleNamespace(pairs={"XAUUSD": SimpleNamespace(
        desk=LIVE.model_copy(update={"management": "be_2r"}))}))
    own = [{"action": "trail_atr", "trigger": "r_multiple", "value": 1.0, "params": {}}]
    d = {"id": "x", "pair": "XAUUSD", "recommendation": {}, "execution_detail": {"executed_management": own}}
    wed = int(dt.datetime(2026, 9, 30, 14, 0, tzinfo=UTC).timestamp() * 1000)          # no flat on a Wednesday
    out = ex_mod.Executor._with_desk_rules(fixed, d, wed)
    assert [(r["action"], r["value"]) for r in rules_of(out)] == [("move_sl_to_breakeven", 2.0)]
    assert ex_mod.Executor._fixed_bracket(fixed, "XAUUSD")
    model = SimpleNamespace(s=SimpleNamespace(pairs={"XAUUSD": SimpleNamespace(desk=LIVE)}))
    assert rules_of(ex_mod.Executor._with_desk_rules(model, d, wed)) == own
    assert not ex_mod.Executor._fixed_bracket(model, "XAUUSD")
