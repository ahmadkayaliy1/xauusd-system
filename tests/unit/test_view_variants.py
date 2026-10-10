"""The 2026-10-10 desk tests: ``desk.view_cadence`` (a look every 5 minutes, always or while something is held),
``desk.view_plus`` (a code regime label + the last 20 5m candles), and the backtest's chart-set / daily-cap overrides.
Pure logic on hand-built blocks (labelled stand-ins), plus the real builder on the enrichment test's XAU bars."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from tradingsystem.ai.model_view import RECENT, model_view
from tradingsystem.ai.triggers import TriggerDecision
from tradingsystem.analysis import context as ctx
from tradingsystem.analysis.engine import Engine
from tradingsystem.analysis.snapshot import SnapshotBuilder
from tradingsystem.backtest.runner import RunConfig, run_settings
from tradingsystem.core.settings import DeskCfg

from .test_snapshot_enrichments import AS_OF_REAL, env, seed_15m  # noqa: F401 - the builder fixture

THREE = Path(__file__).resolve().parents[1] / "fixtures" / "config_three_pairs.yaml"
START = 1790146800000                                     # 2026-09-23 06:00 UTC


# ------------------------------------------------------------------ view_cadence
def cad_engine(cadence: str, held, *, backoff: int = 0, mode: str = "live"):
    e = object.__new__(Engine)
    e.s = NS(pairs={"XAUUSD": NS(desk=DeskCfg(mode=mode, windows=[], view_cadence=cadence))})
    e._holds = lambda pair: held
    e._backoff_ms = lambda pair: backoff
    return e


QUIET = TriggerDecision(False, ["spacing: last AI call 5 min ago (next after 15 min); waiting: weak",
                                "price inside 15m bearish fvg 4159.71-4165.26"], "none", frozenset({"k"}))
PAYLOAD = {"meta": {}}


def test_the_default_cadence_changes_nothing():
    assert cad_engine("trigger", False)._cadence("XAUUSD", QUIET, True, PAYLOAD) is QUIET


def test_every_5m_looks_at_every_screen_close_without_the_spacing_text():
    d = cad_engine("every_5m", False)._cadence("XAUUSD", QUIET, True, PAYLOAD)
    assert d.fire and d.strength == "idle" and d.signature == QUIET.signature
    assert d.reasons == ["idle: the 5-minute look at the market (desk.view_cadence)",
                         "price inside 15m bearish fvg 4159.71-4165.26"]
    held = cad_engine("every_5m", True)._cadence("XAUUSD", QUIET, True, PAYLOAD)
    assert held.fire and held.strength == "review" and "open trade" in held.reasons[0]


def test_every_5m_holding_looks_only_while_something_is_held():
    assert cad_engine("every_5m_holding", False)._cadence("XAUUSD", QUIET, True, PAYLOAD) is QUIET
    assert cad_engine("every_5m_holding", None)._cadence("XAUUSD", QUIET, True, PAYLOAD) is QUIET   # unknown
    d = cad_engine("every_5m_holding", True)._cadence("XAUUSD", QUIET, True, PAYLOAD)
    assert d.fire and d.strength == "review"


def test_the_forced_look_needs_a_screen_close_and_no_failure_back_off():
    e = cad_engine("every_5m", True)
    fired = TriggerDecision(True, ["15m BOS bearish through 4150"], "strong")
    assert e._cadence("XAUUSD", fired, True, PAYLOAD) is fired                  # a real trigger stays as it is
    assert e._cadence("XAUUSD", QUIET, False, PAYLOAD) is QUIET                 # no close, no look
    assert e._cadence("XAUUSD", QUIET, True, None) is QUIET
    assert cad_engine("every_5m", True, backoff=60_000)._cadence("XAUUSD", QUIET, True, PAYLOAD) is QUIET
    assert cad_engine("every_5m", True, mode="shadow")._cadence("XAUUSD", QUIET, True, PAYLOAD) is QUIET


# ------------------------------------------------------------------ view_plus
def reg(adx, d, pct=0.5, vol="normal"):
    return {"regime": {"adx": adx, "trend_strength": "strong_trend" if adx >= 30 else "trend" if adx >= 22 else "range",
                       "di_direction": d, "atr_percentile": pct, "volatility": vol}}


def test_the_regime_label():
    assert ctx.regime_label({"1h": reg(35, "down", 0.97, "high"), "15m": reg(56, "down"), "4h": reg(22, "down")}) == {
        "label": "strong trend down", "volatility": "high", "source": "code (ADX / DI / ATR)",
        "basis": "4h ADX 22 DI down ATR pct 0.5; 1h ADX 35 DI down ATR pct 0.97; 15m ADX 56 DI down ATR pct 0.5",
        "note": "15m ADX above 40: an extended, fast move"}
    assert ctx.regime_label({"1h": reg(25, "up"), "15m": reg(23, "up")})["label"] == "trend up"
    assert ctx.regime_label({"1h": reg(15, "up"), "15m": reg(12, "down")})["label"] == "range"
    assert ctx.regime_label({"1h": reg(28, "up"), "15m": reg(26, "down")})["label"] == \
        "1h trend up, 15m against it (pullback or turn)"
    assert ctx.regime_label({"1h": reg(15, "up"), "15m": reg(30, "up")})["label"] == "mixed"
    assert ctx.regime_label({"1h": reg(30, "up")}) is None and ctx.regime_label({}) is None


def test_the_view_keeps_the_extra_5m_candles_only_when_the_payload_asks():
    bars = [[f"2026-09-29T0{i // 12}:{i % 12 * 5:02d}:00.000Z", 1, 2, 0, 1, 5] for i in range(20)]
    p = {"meta": {"as_of": "2026-09-29T07:25:00.000Z"}, "timeframes": {"5m": {"recent": bars}}}
    assert len(model_view(p)["timeframes"]["5m"]["recent"]) == RECENT["5m"] == 12
    p["meta"]["view_keep_recent"] = {"5m": 20}
    v = model_view(p)
    assert len(v["timeframes"]["5m"]["recent"]) == 20 and "view_keep_recent" not in v["meta"]


def test_the_builder_adds_the_regime_label_and_20_candles_for_view_plus_only(env):  # noqa: F811
    seed_15m(env, "XAUUSD")
    plain = env.builder().build("XAUUSD", AS_OF_REAL)
    assert "regime" not in plain["market"] and "view_keep_recent" not in plain["meta"]
    pc = env.s.pairs["XAUUSD"]
    s2 = env.s.model_copy(update={"pairs": {**env.s.pairs, "XAUUSD": pc.model_copy(
        update={"desk": pc.desk.model_copy(update={"view_plus": True})})}})
    b = SnapshotBuilder(s2, env.reg)
    try:
        plus = b.build("XAUUSD", AS_OF_REAL)
    finally:
        b.wait_background()
        b.close()
    assert plus["meta"]["view_keep_recent"] == {"5m": 20}
    assert plus["market"].get("regime") == ctx.regime_label(plus["timeframes"])
    assert plus["meta"]["payload_hash"] != plain["meta"]["payload_hash"]


# ------------------------------------------------------------------ backtest overrides
def test_a_run_raises_the_providers_daily_cap_with_its_own_and_may_change_the_charts(tmp_path):
    rc = RunConfig(start_ms=START, end_ms=START + 3_600_000)
    s = run_settings(THREE, tmp_path / "r1", rc)
    base = s.ai.providers[s.ai.active_provider].rpd
    assert s.ai.daily_calls_per_pair == 120 and len(s.ai.charts.timeframes) == 6
    rc = RunConfig(start_ms=START, end_ms=START + 3_600_000)
    rc.ai = {**rc.ai, "daily_calls_per_pair": 700, "charts_timeframes": ["1h", "15m", "5m"]}
    s = run_settings(THREE, tmp_path / "r2", rc)
    assert s.ai.daily_calls_per_pair == 700 and s.ai.charts.timeframes == ["1h", "15m", "5m"]
    if base:
        assert s.ai.providers[s.ai.active_provider].rpd == 740
    d = run_settings(THREE, tmp_path / "r3", RunConfig(start_ms=START, end_ms=START + 3_600_000,
                                                      desk={"view_cadence": "every_5m_holding",
                                                            "view_plus": True})).pairs["XAUUSD"].desk
    assert (d.view_cadence, d.view_plus) == ("every_5m_holding", True)
    with pytest.raises(ValueError):
        run_settings(THREE, tmp_path / "r4", RunConfig(start_ms=START, end_ms=START + 3_600_000,
                                                       desk={"view_cadence": "every_minute"}))
