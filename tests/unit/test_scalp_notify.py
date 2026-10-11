"""The scalp brief's notifications (D-058, plan §1.8): the title names the hour, regime and bias (and NO SCALP), the
text is the markdown's first 600 characters (``TOAST_TEXT_MAX``), the dedupe key is one per hour for the loop's
runs and a separate one for on-demand runs, a failure has one key per day. The notifier itself is disabled by the
suite (``TS_NOTIFY_DISABLE``): the real ``notify`` logs only, and that path is exercised once here."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from tradingsystem.core import notify as nt
from tradingsystem.core.notify import TOAST_TEXT_MAX
from tradingsystem.core.settings import INSTANCE_ENV, load_settings
from tradingsystem.scalp import brief as bf
from tradingsystem.scalp import service as sv
from tradingsystem.scalp.account import desk_block
from tradingsystem.scalp.contract import ScalpBrief
from tradingsystem.scalp.files import read_json, scalp_dir

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "scalp_brief_sample.json"
T = int(dt.datetime(2026, 10, 13, 10, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)


def settings(tmp_path: Path, **scalp):
    s = load_settings(extra_env={INSTANCE_ENV: "XAUUSD"})
    paths = s.paths.model_copy(update={"data_dir": str(tmp_path / "data"), "logs_dir": str(tmp_path / "logs")})
    return s.model_copy(update={"paths": paths, "scalp": s.scalp.model_copy(update={"enabled": True, **scalp})})


def record(tmp_path: Path, **brief_over) -> dict:
    s = settings(tmp_path)
    doc = {**json.loads(FIXTURE.read_text(encoding="utf-8")), **brief_over}
    brief = ScalpBrief.model_validate(doc)
    inputs = bf.Inputs(pair="XAUUSD", as_of=T, payload={"meta": {"payload_hash": "h"}}, payload_text="{}",
                       atr5m=3.9, spread_now=0.24, spread_median=0.24, price_at=4181.2)
    leg = {"side": "BUY", "price": 4180.0, "sl": 4172.0, "tp": 4196.0, "volume": 0.01, "profit_usd": 1.2,
           "since_ms": T - 600_000}
    desk = desk_block({"equity": 100.36, "open_positions": [leg]})
    return bf.to_record(s, inputs=inputs, reason="hourly", brief=brief, checks={}, desk=desk, tol=1.0, gen=None,
                        system="sys", versions={"scalp/system": 1})


def test_title_and_text_bounds(tmp_path):
    rec = record(tmp_path)
    title, text = bf.notify_text(rec)
    assert title == "Scalp 10:00 UTC — range / neutral"
    assert len(text) == TOAST_TEXT_MAX == 600 and text == bf.render_md(rec)[:600]
    assert text.startswith("# Scalp 10:00 UTC — تذبذب / محايد (55%)")
    assert "النظام الآلي ماسك" in text and "شراء position @ 4180.00" in text   # the desk's leg is in the first lines
    title, text = bf.notify_text(record(tmp_path, no_scalp_now=True, plans=[]))
    assert title.endswith(" — NO SCALP") and "**لا سكالب هلق (NO SCALP)**" in text
    assert "## الخطط — ما في خطة هالساعة" in bf.render_md(record(tmp_path, no_scalp_now=True, plans=[]))


def test_keys_per_hour_for_the_loop_and_per_run_for_the_button(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(sv, "notify", lambda s, level, title, text, *, key=None, pair=None:
                        sent.append((level, title, key)))
    monkeypatch.setattr(sv, "flush", lambda timeout_s=15.0: None)
    monkeypatch.setattr(sv, "_free_ram_mb", lambda: 4000.0)
    from tradingsystem.ingest.mt5 import terminal
    monkeypatch.setattr(terminal, "terminal_running", lambda path, min_age_s=0.0: False)   # never near the live MT5
    import sys
    import types
    checks = types.ModuleType("tradingsystem.scalp.checks")
    checks.tolerance = lambda a, m: 1.0
    checks.check_plan = lambda plan, **kw: {"stop_ok": True, "target_ok": True, "news_ok": True, "desk_ok": True,
                                            "level_ok": True, "rr_ok": True, "rejected": [], "notes": []}
    scorer = types.ModuleType("tradingsystem.scalp.scorer")
    scorer.is_mature = lambda rec, now, cfg: False
    journal = types.ModuleType("tradingsystem.scalp.journal")
    journal.stats = lambda records, n=20: {}
    journal.journal_text = lambda records, manual, *, n_briefs, max_chars=2400: ""
    for name, mod in (("checks", checks), ("scorer", scorer), ("journal", journal)):
        monkeypatch.setitem(sys.modules, f"tradingsystem.scalp.{name}", mod)
    monkeypatch.setattr(bf, "build_inputs", lambda s, reg, pair, as_of: bf.Inputs(
        pair=pair, as_of=as_of, payload={}, payload_text="{}", account={"equity": 100.0}, atr5m=3.9, spread_now=0.24,
        spread_median=0.24, price_at=4181.0))
    s = settings(tmp_path, notify_level="warn")
    assert sv.run_once(s, reason="hourly", no_model=True, fixture=FIXTURE, now=T) == 0
    assert sv.run_once(s, reason="dashboard", no_model=True, fixture=FIXTURE, now=T + 3_600_000) == 0
    assert sv.run_once(s, reason="manual", no_model=True, fixture=FIXTURE, now=T + 7_200_000) == 0
    assert [k for _, _, k in sent] == ["scalp:20261013T1000Z", "scalp:20261013T1100Z:manual",
                                       "scalp:20261013T1200Z:manual"]
    assert {lvl for lvl, _, _ in sent} == {"warn"}         # notify_level keeps the brief above a raised min_level
    # notify: false → the brief is stored, nothing is sent
    s = settings(tmp_path, notify=False)
    assert sv.run_once(s, reason="hourly", no_model=True, fixture=FIXTURE, now=T + 10_800_000) == 0
    assert len(sent) == 3


def test_failure_key_per_day_and_the_disabled_notifier_logs_only(tmp_path, monkeypatch):
    s = settings(tmp_path)
    monkeypatch.setattr(sv, "flush", lambda timeout_s=15.0: None)
    before = len(nt._DEFAULT.results)
    assert sv._fail(s, "inputs", RuntimeError("no ticks"), T) == 1
    assert sv._fail(s, "model", "provider unavailable", T + 3_600_000) == 1
    res = list(nt._DEFAULT.results)[before:]
    assert [r["key"] for r in res] == ["scalp:fail:20261013", "scalp:fail:20261013"]       # one key a day: deduped
    assert all(r["level"] == "warn" and r["title"] == "Scalp brief failed" for r in res)
    # the suite's guard (TS_NOTIFY_DISABLE): nothing reaches the desktop or Telegram
    assert all(r["toast"] is None and r["telegram"] is None and "log only" in r["why"] for r in res)
    st = read_json(scalp_dir(s) / "status.json")
    assert st["last_outcome"] == "failed:model" and st["last_error"].startswith("model: provider unavailable")
    # notify: false → a failure is logged and written to status.json, never sent
    s = settings(tmp_path, notify=False)
    n = len(nt._DEFAULT.results)
    assert sv._fail(s, "fixture", ValueError("bad"), T) == 1
    assert len(nt._DEFAULT.results) == n
