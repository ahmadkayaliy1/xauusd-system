"""D-058: the hit-rate statistics and the journal the model reads (scalp/journal.py) - buckets and rates, rejected
plans excluded, the text's size, the empty case, and a ``$PDH`` in the text surviving the prompt filler."""
from tradingsystem.ai.prompts import _fill
from tradingsystem.scalp.journal import bucket, journal_text, stats

T0 = 1_760_000_000_000
H = 3_600_000
MIN = 60_000


def plan(side="BUY", conf=60, entry=4200.0, stop=4197.0, entry_type="limit"):
    tgt = entry + 6.0 if side == "BUY" else entry - 6.0
    return {"level_price": entry, "side": side, "entry_type": entry_type, "entry": entry, "stop": stop, "target": tgt,
            "rr": 2.0, "max_hold_min": 30, "confidence": conf}


def ps(idx, outcome, r=None, minutes=None):
    return {"idx": idx, "outcome": outcome, "fill_ms": None, "exit_ms": None, "r": r, "mfe_r": None, "mae_r": None,
            "minutes": minutes}


def rec(i, plans, scores, *, levels=None, level_scores=None, checks=None, manual=None, bias="neutral", score=True,
        atr5m=4.0, levels_kind="support"):
    levels = levels or [{"price": 4200.0, "kind": levels_kind}, {"price": 4210.0, "kind": "pdh"}]
    level_scores = level_scores or [{"price": 4200.0, "outcome": "held", "touched_ms": T0},
                                    {"price": 4210.0, "outcome": "untouched"}]
    return {"id": f"B{i}", "as_of_ms": T0 + i * H, "atr5m": atr5m, "tol": 1.0,
            "brief": {"regime": "range", "bias": bias, "bias_confidence": 55, "levels": levels, "plans": plans},
            "checks": checks or {}, "manual": manual or [],
            "score": {"levels": level_scores, "plans": scores,
                      "summary": {}} if score else None, "scored_ms": T0 + i * H + 2 * H if score else None}


def test_stats_buckets_rates_and_rejected_excluded():
    recs = [
        rec(0, [plan(conf=70), plan("SELL", conf=45, entry=4210.0, stop=4213.0)],
            [ps(0, "target", 2.0, 12.0), ps(1, "stop", -1.0, 18.0)],
            manual=[{"position_id": 1, "side": "SELL", "entry": 4196.1, "exit": 4192.9, "pnl_usd": 3.2, "open": False,
                     "followed_plan": True, "open_ms": T0 + 40 * MIN}]),
        rec(1, [plan(conf=80), plan(conf=55, entry_type="stop"), plan(conf=90)],
            [ps(0, "stop", -1.0, 5.0), ps(1, "expired", 0.4, 30.0), ps(2, "target", 2.0, 9.0)],
            checks={"2": {"rejected": ["desk_ok"]}},
            level_scores=[{"price": 4200.0, "outcome": "broken"}, {"price": 4210.0, "outcome": "unknown"}],
            manual=[{"position_id": 1, "side": "SELL", "entry": 4196.1, "exit": 4192.9, "pnl_usd": 3.2, "open": False,
                     "followed_plan": True, "open_ms": T0 + 40 * MIN},
                    {"position_id": 2, "side": "BUY", "entry": 4181.5, "exit": 4180.0, "pnl_usd": -1.5, "open": False},
                    {"position_id": 3, "side": "BUY", "entry": 4190.0, "exit": None, "pnl_usd": 0.0, "open": True}]),
        rec(2, [plan(conf=50)], [ps(0, "not_triggered")]),
        rec(3, [plan(conf=65)], [ps(0, "unknown")]),
        rec(4, [plan(conf=99)], [], score=False),                                 # not scored yet
    ]
    st = stats(recs)
    assert st["n_scored"] == 4
    # 4 scored records x 2 levels, one of them unknown: held x3 + broken touched, the unknown one is not counted
    assert st["levels"] == {"touched": 4, "total": 7, "held": 3, "touch_rate": 0.571, "held_rate": 0.75}
    p = st["plans"]
    assert p == {"n": 5, "triggered": 4, "target_first": 1, "stop_first": 2, "expired": 1, "hit_rate": 0.333,
                 "avg_r": 0.1, "sum_r": 0.4}
    bc = st["by_confidence"]
    assert bc["<50"] == {"n": 1, "plans": 1, "triggered": 1, "hit_rate": 0.0}
    assert bc["50-69"] == {"n": 0, "plans": 2, "triggered": 1, "hit_rate": None}
    assert bc[">=70"] == {"n": 2, "plans": 2, "triggered": 2, "hit_rate": 0.5}           # the rejected c90 is out
    assert st["by_side"]["BUY"]["plans"] == 4 and st["by_side"]["SELL"]["n"] == 1
    assert st["by_entry_type"]["stop"] == {"n": 0, "plans": 1, "triggered": 1, "hit_rate": None}
    assert st["manual"] == {"n": 3, "wins": 1, "pnl_usd": 1.7, "followed_plan": 1}   # the same position once
    assert stats(recs, n=1)["n_scored"] == 1 and stats(recs, n=1)["plans"]["n"] == 0   # newest scored = B3 (unknown)
    assert stats(recs, n=2)["plans"]["n"] == 1                                         # + B2 (not triggered)
    assert stats([]) == {"n_scored": 0, "levels": {"touched": 0, "total": 0, "held": 0, "touch_rate": None,
                                                    "held_rate": None},
                         "plans": {"n": 0, "triggered": 0, "target_first": 0, "stop_first": 0, "expired": 0,
                                   "hit_rate": None, "avg_r": None, "sum_r": 0.0},
                         "by_confidence": {b: {"n": 0, "plans": 0, "triggered": 0, "hit_rate": None}
                                           for b in ("<50", "50-69", ">=70")},
                         "by_side": {}, "by_entry_type": {}, "manual": {"n": 0, "wins": 0, "pnl_usd": 0.0,
                                                                        "followed_plan": 0}}


def test_bucket():
    assert [bucket(x) for x in (0, 49, 50, 69, 70, 100, None)] == \
        ["<50", "<50", "50-69", "50-69", ">=70", ">=70", "<50"]


def test_journal_text_lines_and_size():
    recs = [rec(i, [plan(conf=70 if i % 2 else 45), plan("SELL", conf=55, entry=4210.0, stop=4213.0)],
                [ps(0, "target" if i % 3 else "stop", 2.0 if i % 3 else -1.0, 12.0), ps(1, "not_triggered")],
                manual=[{"position_id": i, "side": "SELL", "entry": 4196.1, "exit": 4192.9, "pnl_usd": 3.2,
                         "open": False, "followed_plan": True, "open_ms": T0 + i * H + 40 * MIN}] if i < 3 else [],
                bias="bearish")                                                       # every BUY plan is against it
            for i in range(20)]
    manual = [{"position_id": i, "side": "SELL", "entry": 4196.1, "exit": 4192.9, "pnl_usd": 3.2, "open": False,
               "followed_plan": i == 6, "open_ms": T0 + i * H + 42 * MIN} for i in range(7)]
    text = journal_text(recs, manual, n_briefs=6)
    assert len(text) <= 2400
    lines = text.split("\n")
    assert lines[0].startswith("Last 20 scored briefs:") and lines[1].startswith("Hit-rate by your confidence:")
    briefs = [ln for ln in lines if ln.startswith("10-") or ln[2:3] == "-"]
    head = "Recent briefs (time UTC | regime/bias conf | levels | plans | your manual trades):"
    recent = lines[lines.index(head) + 1:]
    assert recent[0].endswith("| manual: SELL 4196.1 +3.20") or "| plans: " in recent[0]
    assert "range/bearish 55 | levels 2: touched 1, held 1 | plans: BUY 4200.0 c70 → target +2.0R in 12m; " \
           "SELL 4210.0 c55 → not triggered" in recent[0]
    assert sum(1 for ln in recent if "| plans: " in ln) == 6                         # journal_n briefs
    man = lines[lines.index("Last manual trades:") + 1:]
    assert len([ln for ln in man if " → " in ln and "SELL 4196.1" in ln]) == 5
    assert any(ln.endswith("+3.20 (plan followed)") for ln in man) and briefs is not None
    assert lines[-1].startswith("Observations: ") and "plans against bias hit" in lines[-1]
    assert "levels of kind support held" in lines[-1]
    # a smaller cap cuts whole lines only
    small = journal_text(recs, manual, n_briefs=6, max_chars=400)
    assert len(small) <= 400 and small.endswith("…") and all(len(ln) < 400 for ln in small.split("\n"))


def test_journal_text_empty_and_dollar_sign_survive_fill():
    assert journal_text([], [], n_briefs=6) == \
        "No scored briefs yet (the first briefs are scored one hour after they are written)."
    recs = [rec(0, [plan(conf=70)], [ps(0, "stop", -1.0, 3.0)],
                levels=[{"price": 4200.0, "kind": "pdh"}, {"price": 4210.0, "kind": "pdh"}])]
    text = journal_text(recs, [], n_briefs=6) + "\nnote: $PDH at 4210"
    assert "Observations" not in text                                               # under 8 counted plans
    filled = _fill("journal:\n$journal\nend", {"journal": text}, "test")
    assert "$PDH at 4210" in filled and filled.endswith("\nend")


def test_journal_text_rejected_plan_and_open_manual():
    recs = [rec(0, [plan(conf=70)], [ps(0, "rejected")], checks={"0": {"rejected": ["news_ok"]}},
                manual=[{"position_id": 5, "side": "BUY", "entry": 4190.0, "exit": None, "pnl_usd": 0.0, "open": True,
                         "open_ms": T0 + 5 * MIN}])]
    text = journal_text(recs, [{"position_id": 5, "side": "BUY", "entry": 4190.0, "open": True, "open_ms": T0}],
                        n_briefs=6)
    assert "BUY 4200.0 c70 → rejected" in text and "manual: BUY 4190.0 +0.00 (open)" in text
    assert "BUY 4190.0 (still open)" in text
    assert stats(recs)["plans"]["n"] == 0
