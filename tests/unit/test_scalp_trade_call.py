"""D-060: the scalp brief names THE trade - the contract fields, the code's choice of the plan, the fallback text."""
from __future__ import annotations

import json
from pathlib import Path

from tradingsystem.scalp import brief as bf
from tradingsystem.scalp.contract import ScalpBrief

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "scalp_brief_sample.json"


def sample(**over) -> dict:
    return {**json.loads(FIXTURE.read_text(encoding="utf-8")), **over}


def test_the_fields_parse_and_a_dangling_best_plan_is_dropped_not_repaired():
    b = ScalpBrief.model_validate(sample())
    assert b.best_plan == 0 and b.trade_call.startswith("بيع limit من 4186.0")
    assert ScalpBrief.model_validate(sample(best_plan=7)).best_plan is None
    assert ScalpBrief.model_validate(sample(plans=[], no_scalp_now=True, best_plan=0)).best_plan is None
    old = sample()
    old.pop("trade_call"), old.pop("best_plan")                       # a brief from before D-060 still parses
    b = ScalpBrief.model_validate(old)
    assert b.trade_call == "" and b.best_plan is None


def test_the_code_picks_the_models_plan_unless_the_checks_dropped_it():
    b = ScalpBrief.model_validate(sample()).model_dump()
    tr = bf.trade_summary(b, {})
    assert tr["idx"] == 0 and tr["text"] == b["trade_call"] and tr["rejected"] == []
    assert tr["line"].startswith("بيع من 4186.00 (limit) · الوقف 4189.00 (3.00 $ للأونصة) · الهدف 4180.00")
    # the model's plan failed a hard check: the best remaining plan, and a code-built sentence that says so
    tr = bf.trade_summary(b, {"0": {"rejected": ["news_ok"]}})
    assert tr["idx"] == 1 and tr["rejected"] == ["news_ok"]
    assert tr["text"].startswith("الصفقة: شراء من 4173.00، الوقف 4170.00، الهدف 4178.50. الشرط: sweep تحت 4172.5")
    assert "سقطت: news_ok" in tr["text"]
    # every plan rejected: no trade, and the reason is visible
    tr = bf.trade_summary(b, {"0": {"rejected": ["news_ok"]}, "1": {"rejected": ["desk_ok"]}})
    assert tr["idx"] is None and tr["text"].startswith("لا صفقة هلق") and "news_ok" in tr["text"]


def test_the_fallback_sentence_when_the_model_left_trade_call_empty():
    b = ScalpBrief.model_validate(sample(trade_call="", best_plan=None)).model_dump()
    tr = bf.trade_summary(b, {})
    assert tr["idx"] == 0                                              # the highest confidence (55 over 48)
    assert tr["text"] == "الصفقة: بيع من 4186.00، الوقف 4189.00، الهدف 4180.00. الشرط: " + b["plans"][0]["trigger"]
    b = ScalpBrief.model_validate(sample(trade_call="", best_plan=None, plans=[], no_scalp_now=True)).model_dump()
    tr = bf.trade_summary(b, {})
    assert tr == {"idx": None, "text": "لا صفقة هلق: " + b["no_scalp"][0], "line": "", "rejected": []}
    b = ScalpBrief.model_validate(sample(trade_call="لا صفقة: السبريد واسع", best_plan=None, plans=[],
                                         no_scalp_now=True)).model_dump()
    assert bf.trade_summary(b, {})["text"] == "لا صفقة: السبريد واسع"   # the model's own words stay


def test_extra_keys_are_ignored_not_repaired_but_a_wrong_field_still_fails():
    import pytest
    doc = sample()
    doc["levels"][0]["strength_note"] = "x"
    doc["plans"][0]["timeframe_note"] = "y"
    doc["extra_top"] = 1
    b = ScalpBrief.model_validate(doc)                               # 2026-10-12: no repair call for these
    assert "strength_note" not in b.levels[0].model_dump() and "extra_top" not in b.model_dump()
    with pytest.raises(ValueError):
        ScalpBrief.model_validate(sample(plans=[{**sample()["plans"][0], "stop": 4100.0}]))   # a SELL with stop < entry


def test_the_record_carries_the_trade_and_the_page_fields_are_escaped():
    js = (Path(__file__).resolve().parents[2] / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "The trade now (الصفقة المقترحة)" in js and "esc(txt(trade.text || b.trade_call" in js
