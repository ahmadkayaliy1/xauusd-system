"""scalp/prompts.py (D-058): the brief's own prompt pair - every placeholder filled, the system text stable across
runs (the CLI's cache, the record's prompt hash), the library's legend and gold field notes appended verbatim, the
Arabic instruction present, and the prompt LIBRARY untouched (its hash and versions never move because of the
brief: the files live outside ``ai/prompts``)."""
from __future__ import annotations

import importlib
import re

import pytest

from tradingsystem.ai import prompts as lib
from tradingsystem.scalp import prompts as sp

SYS = {"pair": "XAUUSD", "price_reference": "mt5:XAUUSD", "output_language": "Arabic",
       "min_stop_rule": "|entry - stop| >= max(0.5 x the 5-minute ATR14, 2 USD)", "min_rr": "1.5", "spread_mult": 5,
       "max_plans": 5, "news_windows": "fomc 60/75, high 30/30, medium 10/10",
       "hours_text": "every hour from 07:00 to 15:00 UTC"}
USER = {"now_utc": "2026-10-13T10:00:00.000Z", "reason": "hourly", "desk": "equity 100.36 USD; holds: nothing",
        "manual_recent": "none in the last days", "journal": "no scored brief yet",
        "charts_note": "Attached: 3 charts (1h, 15m, 5m)", "payload": '{"meta":{"pair":"XAUUSD"}}',
        "execution": "The owner's lot is FIXED at 0.01 lots = 1 oz (D-061 test)"}
PLACEHOLDER = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]*\})")


def test_every_placeholder_filled_and_versions_recorded():
    system, user, versions = sp.render_scalp(SYS, USER)
    for name in sp.SYSTEM_VARS:
        assert f"${name}" not in system and f"${name}" not in user
    for name in sp.USER_VARS:
        assert f"${name}" not in user
    # the values are in the text: the stable ones in the system prompt, the hour's in the user prompt
    for key in ("pair", "price_reference", "output_language", "min_stop_rule", "min_rr", "news_windows", "hours_text"):
        assert str(SYS[key]) in system
    for key in ("now_utc", "reason", "desk", "manual_recent", "journal", "charts_note", "payload"):
        assert str(USER[key]) in user
    # no $name is left in the scalp files' own text (the library's legend may use `$` in prose; it is appended verbatim)
    own = system.split(lib._read_versioned("shared/payload_legend.md")[0], 1)[0]
    assert not PLACEHOLDER.findall(own), PLACEHOLDER.findall(own)
    assert versions["scalp/system"] == 4 and versions["scalp/instructions"] == 3       # D-060/61/63
    assert "`trade_call`" in system and "`best_plan`" in system and "trade_call" in user
    assert "shared/payload_legend" in versions and "desks/xauusd_fields" in versions
    assert "<!-- prompt:" not in system and "<!-- prompt:" not in user


def test_system_text_identical_across_runs_with_different_user_vars():
    s1, u1, _ = sp.render_scalp(SYS, USER)
    s2, u2, _ = sp.render_scalp(SYS, {**USER, "now_utc": "2026-10-13T11:00:00.000Z", "reason": "dashboard",
                                      "payload": '{"meta":{"pair":"XAUUSD","as_of":"11:00"}}', "journal": "x"})
    assert s1 == s2 and sp.prompt_hash(s1) == sp.prompt_hash(s2)
    assert u1 != u2 and "dashboard" in u2 and len(sp.prompt_hash(s1)) == 16


def test_legend_and_gold_fields_appended_verbatim_after_the_rules():
    system, _, _ = sp.render_scalp(SYS, USER)
    legend = lib._read_versioned("shared/payload_legend.md")[0]
    fields = lib._read_versioned("desks/xauusd_fields.md")[0]
    assert legend in system and fields in system
    assert system.index("## Hard rules") < system.index(legend) < system.index(fields)
    assert system.endswith(fields)                      # the schema is appended by the provider, after this


def test_hard_rules_and_calibration_present_and_arabic_instruction_in_user_text():
    system, user, _ = sp.render_scalp(SYS, USER)
    for phrase in ("never place", "AGAINST the desk", "no_scalp_now: true", "Confidence calibration", "Below 40",
                   "market_after_rejection", "5-minute"):
        assert phrase in system, phrase
    assert "Arabic" in system
    assert re.search(r"[؀-ۿ]", user), "the fill instruction is written in Arabic"
    assert "ONE JSON object" in user
    # the user prompt's order: the time, the desk, the manual trades, the journal, the charts, the payload, the ask
    idx = [user.index(k) for k in ("Time now", "## The desk", "## The owner's recent manual trades", "## Your journal",
                                   "## Charts", "## Payload", "## Answer")]
    assert idx == sorted(idx)


def test_prompt_library_untouched_and_scalp_files_outside_it():
    before = lib.library_hash()
    importlib.reload(sp)
    sp.render_scalp(SYS, USER)
    assert lib.library_hash() == before
    assert not sp.DIR.resolve().is_relative_to(lib.DIR.resolve())
    assert not any(k.startswith("scalp/") for k in lib.versions())
    assert (sp.DIR / "system.md").is_file() and (sp.DIR / "instructions.md").is_file()


def test_missing_var_raises_and_model_values_are_never_reparsed():
    with pytest.raises(lib.PromptError):
        sp.render_scalp({k: v for k, v in SYS.items() if k != "hours_text"}, USER)
    with pytest.raises(lib.PromptError):
        sp.render_scalp(SYS, {k: v for k, v in USER.items() if k != "payload"})
    _, user, _ = sp.render_scalp(SYS, {**USER, "journal": "levels of kind $PDH held 4/4; $BTC n/a"})
    assert "$PDH held 4/4; $BTC" in user
