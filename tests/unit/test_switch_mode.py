"""tools/switch_mode.py (D-064): the refusal matrix, the config.local block editor, the blocks written per mode, the
bat script. Nothing here touches MT5 or the running system."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from tradingsystem.core.settings import LIVE_CONFIRMATION_PHRASE

ROOT = Path(__file__).resolve().parents[2]


def tool():
    spec = importlib.util.spec_from_file_location("switch_mode", ROOT / "tools" / "switch_mode.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


DEMO = {"server": "WindsorBrokers1-Demo", "trade_mode": 0, "login": 20232911}
REAL = {"server": "WindsorBrokers1-Real1", "trade_mode": 2, "login": 555}


def test_precheck_matrix():
    sm = tool()
    ok = dict(current="demo", target="live", executor={"open_positions": 0, "open_orders": 0}, scalp_open=[],
              account=REAL, profile_server="WindsorBrokers1-Real1", profile_kind="real")
    assert sm.precheck(**ok) == []
    assert sm.precheck(**{**ok, "executor": {"open_positions": 1, "open_orders": 0}})[0].startswith("the desk holds 1 position(s)")
    assert sm.precheck(**{**ok, "scalp_open": [{"ticket": 1}]})[0].startswith("1 scalp auto-trade(s) still tracked")
    assert sm.precheck(**{**ok, "account": None}) == ["the MT5 terminal is not running or not reachable - open it and log into the target account"]
    assert sm.precheck(**{**ok, "account": DEMO})[0].startswith("the terminal is logged into a demo account")
    assert sm.precheck(**{**ok, "account": {**REAL, "server": "Other-Real"}})[0].startswith("the terminal is on server 'Other-Real'")
    assert sm.precheck(**{**ok, "target": "paper"}) == ["target must be demo or live (got 'paper')"]
    assert sm.precheck(**{**ok, "env_mode": "demo"})[0].startswith("EXECUTION_MODE=demo in .env")     # 2026-10-13: the real cause
    assert sm.precheck(**{**ok, "env_mode": "live"}) == [] and sm.precheck(**{**ok, "env_mode": None}) == []
    # back to demo: the terminal must be on the demo account, not the real one
    back = dict(current="live", target="demo", executor=None, scalp_open=[], account=DEMO,
                profile_server="WindsorBrokers1-Demo", profile_kind="demo")
    assert sm.precheck(**back) == []
    assert sm.precheck(**{**back, "account": REAL})[0].startswith("the terminal is logged into a real account")


def test_the_block_editor_replaces_only_its_sections_and_keeps_the_rest():
    sm = tool()
    text = ("# header comment\n"
            "ai:                                  # the desk\n"
            "  daily_calls_per_pair: 250\n"
            "monitor:\n"
            "  diagnose_enabled: false\n"
            "scalp:\n"
            "  enabled: true\n"
            "  auto_allow_live: true\n")
    new = sm.set_blocks(text, sm.blocks_for("live", "WindsorBrokers1-Real1", "C:/MT5/terminal64.exe"))
    doc = yaml.safe_load(new)
    assert doc["execution"] == {"mode": "live", "live_confirmation": LIVE_CONFIRMATION_PHRASE}
    assert doc["mt5"] == {"data_profile": "live", "profiles": {"live": {"server": "WindsorBrokers1-Real1",
                                                                         "terminal_path": "C:/MT5/terminal64.exe"}}}
    assert doc["ai"] == {"daily_calls_per_pair": 250} and doc["scalp"] == {"enabled": True, "auto_allow_live": True}
    assert new.startswith("# header comment\nai:") and new.count("execution:") == 1 and new.count("mt5:") == 1
    # switching back replaces the blocks in place (one block per section), the phrase goes away
    back = sm.set_blocks(new, sm.blocks_for("demo", "WindsorBrokers1-Real1"))
    doc = yaml.safe_load(back)
    assert doc["execution"] == {"mode": "demo"} and doc["mt5"]["data_profile"] == "demo"
    assert back.count("execution:") == 1 and back.count("mt5:") == 1 and "live_confirmation" not in back
    assert doc["monitor"] == {"diagnose_enabled": False}
    # an empty file
    assert yaml.safe_load(sm.set_blocks("", sm.blocks_for("demo", "X")))["execution"] == {"mode": "demo"}


def test_the_real_config_local_survives_the_editor():
    sm = tool()
    text = (ROOT / "config" / "config.local.yaml").read_text(encoding="utf-8")
    new = sm.set_blocks(text, sm.blocks_for("live", "WindsorBrokers1-Real1"))
    doc = yaml.safe_load(new)
    before = yaml.safe_load(text) or {}
    for key in before:
        if key not in ("execution", "mt5"):
            assert doc[key] == before[key], key
    assert doc["execution"]["mode"] == "live"


def test_the_bat_is_ascii_crlf_and_calls_the_tool():
    raw = (ROOT / "scripts" / "switch_mode.bat").read_bytes()
    assert raw.isascii() and b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    text = raw.decode("ascii")
    assert 'tools\\switch_mode.py %*' in text and "exit /b %RC%" in text
