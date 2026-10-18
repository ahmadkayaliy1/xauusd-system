"""D-058: the scalp job's settings (core/settings.py ``ScalpCfg`` / ``Settings.scalp`` / ``AIModelsCfg.scalp``) — the
defaults (off, hours [7, 16), Fable 5.1 at medium effort), the validators (hours, chart timeframes, the cap), and
this folder's real ``config/config.yaml`` loading with ``scalp.enabled`` false and ``models.scalp`` on the full
model id (the string goes verbatim to the CLI's ``--model``)."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from tradingsystem.core.settings import INSTANCE_ENV, AIModelsCfg, ScalpCfg, Settings, load_settings

ROOT = Path(__file__).resolve().parents[2]
THREE = ROOT / "tests" / "fixtures" / "config_three_pairs.yaml"
FOLDER = ROOT / "config" / "config.yaml"
NO_LOCAL = ROOT / "tests" / "fixtures" / "no_config.local.yaml"
NO_ENV = ROOT / "tests" / "fixtures" / "no.env"


def test_defaults():
    c = ScalpCfg()
    assert c.enabled is False and c.pair == "XAUUSD" and c.hours_utc == [7, 16] and c.run_offset_s == 20
    assert c.max_per_day == 12 and c.charts == ["1h", "15m", "5m"] and c.min_free_ram_mb == 700
    assert c.output_language == "ar" and c.journal_n == 6 and c.stats_n == 20
    assert (c.trigger_window_min, c.level_horizon_min, c.hold_window_min) == (60, 60, 30)
    assert c.gate_retry_min == 20 and c.run_timeout_s == 600 and c.manual_lookback_days == 7
    assert c.notify is True and c.notify_level == "info" and c.manual_risk_cap_pct == 5.0
    m = AIModelsCfg()
    assert m.scalp.model == "claude-fable-5-1" and m.scalp.effort == "medium"
    assert m.decision.model != m.scalp.model                                   # the desk's own role untouched
    assert Settings.model_fields["scalp"].default == ScalpCfg()


@pytest.mark.parametrize("bad", [{"hours_utc": [16, 7]}, {"hours_utc": [7, 7]}, {"hours_utc": [-1, 7]},
                                 {"hours_utc": [7, 25]}, {"hours_utc": [7]}, {"hours_utc": [7, 16, 20]},
                                 {"charts": ["1h", "7m"]}, {"charts": ["daily"]}, {"max_per_day": 0},
                                 {"max_per_day": 41}, {"notify_level": "critical"}, {"manual_risk_cap_pct": 0},
                                 {"manual_risk_cap_pct": 50}, {"stats_n": 4}, {"gate_retry_min": 60},
                                 {"run_timeout_s": 30}, {"unknown_key": 1}])
def test_validation_errors(bad):
    with pytest.raises(ValidationError):
        ScalpCfg(**bad)


def test_valid_overrides():
    c = ScalpCfg(hours_utc=[0, 24], charts=["4h", "1m"], max_per_day=40, notify_level="warn", manual_lookback_days=0)
    assert c.hours_utc == [0, 24] and c.charts == ["4h", "1m"] and c.manual_lookback_days == 0


def test_the_three_pair_fixture_and_the_default_settings_carry_the_scalp_block():
    s = load_settings(THREE, local_path=NO_LOCAL, env_path=NO_ENV, extra_env={INSTANCE_ENV: ""})
    assert s.scalp.enabled is False and s.scalp.pair == "XAUUSD" and s.ai.models.scalp.model == "claude-fable-5-1"
    s2 = load_settings(env_path=Path("nope.env"), extra_env={"EXECUTION_MODE": "paper"})
    assert s2.scalp == s.scalp or s2.scalp.enabled is False


def test_this_folders_real_config_loads_with_scalp_off_and_fable_pinned():
    """As test_split_pairs loads the folder's config: never the local overlay, never the folder's .env."""
    s = load_settings(FOLDER, local_path=NO_LOCAL, env_path=NO_ENV, extra_env={INSTANCE_ENV: ""})
    assert s.scalp.enabled is False
    assert s.ai.models.scalp.model == "claude-fable-5-1" and s.ai.models.scalp.effort == "medium"
    assert s.scalp.pair in s.pairs                                           # the brief's pair is configured here
    assert set(s.scalp.charts) <= set(s.ai.charts.bars)                      # every scalp chart has a bar count
    assert s.scalp.hours_utc == [7, 16] and s.scalp.max_per_day == 12 and s.scalp.min_free_ram_mb == 700
    text = FOLDER.read_text(encoding="utf-8")
    assert "scalp: {model: claude-fable-5-1, effort: medium}" in text and "\nscalp:" in text
    assert "enabled: false" in text.split("\nscalp:", 1)[1]


def test_the_scalp_call_timeout_is_its_own(monkeypatch):
    """2026-10-18: the provider's 180 s is the desk's; two of 28 medium briefs timed out, a higher effort needs more."""
    import asyncio
    from tradingsystem.core.settings import INSTANCE_ENV, load_settings
    from tradingsystem.scalp import brief as bf
    s = load_settings(extra_env={INSTANCE_ENV: "XAUUSD"})
    assert s.scalp.call_timeout_s == 300 and ScalpCfg(call_timeout_s=600).call_timeout_s == 600
    seen = {}

    class Prov:
        cfg = s.ai.providers[bf.provider_name(s)]
        availability_pending = False
        caps_dir = None

        def unavailable_reason(self):
            return None

    monkeypatch.setattr(bf, "make_provider", lambda s_, name, model=None, effort=None: Prov())

    async def fake_generate(prov, *a, **kw):
        seen["timeout"] = prov.cfg.timeout_s
        raise RuntimeError("stop here")
    monkeypatch.setattr(bf, "generate_validated", fake_generate)
    try:
        asyncio.run(bf.call_model(s, None, "sys", "user", []))
    except RuntimeError:
        pass
    assert seen["timeout"] == 300.0 and s.ai.providers[bf.provider_name(s)].timeout_s == 180
