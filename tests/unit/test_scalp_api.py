"""D-058: the Scalp tab's routes (api/app.py) on a seeded throw-away data root — the one-read ``GET /api/scalp``
(empty root, then seeded records → history / latest / stats / manual trades / safety / risk cap, the stats fallback
when ``stats.json`` is missing), one record by id (never a path from the request), the brief's own chart images, and
``POST /api/scalp/run`` — token + Origin like the kill switch, 409 while a run holds ``run.lock``, otherwise one
detached ``--once --reason dashboard`` start (patched) and a notification."""
import inspect
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

import tradingsystem.api.app as app_mod
from tradingsystem.core.filelock import FileLock
from tradingsystem.core.settings import PathsCfg, load_settings
from tradingsystem.core.timeutil import now_ms
from tradingsystem.scalp import files
from tradingsystem.scalp.safety import SAFETY_TEXT

TOKEN = "unit-test-token"
HOUR = 3_600_000
T0 = 1_760_000_000_000 // HOUR * HOUR           # 2025-10-09 08:00 UTC, on the hour


def _settings(tmp_path: Path, instance: str | None = None):
    extra = {"EXECUTION_MODE": "paper", **({"TS_INSTANCE": instance} if instance else {})}
    s = load_settings(env_path=Path("nope.env"), extra_env=extra)
    return s.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"),
                                                  instance=instance)})


def _client(s, monkeypatch) -> TestClient:
    monkeypatch.setenv(s.api.token_env, TOKEN)                  # read once, at create_app
    monkeypatch.setattr(app_mod, "_process_list", lambda: [])
    app_mod._SNAP_CACHE["ts"] = 0
    return TestClient(app_mod.create_app(s), base_url=f"http://127.0.0.1:{s.api.port}")


def _env(tmp_path, monkeypatch, instance=None):
    s = _settings(tmp_path, instance)
    s.paths.state().mkdir(parents=True, exist_ok=True)
    return NS(s=s, db=s.paths.state() / "app.db", client=_client(s, monkeypatch),
              origin=f"http://127.0.0.1:{s.api.port}", data=s.paths.data())


@pytest.fixture
def env(tmp_path, monkeypatch):
    return _env(tmp_path, monkeypatch)


def plan(side="BUY", conf=65, entry=4200.0):
    stop = entry - 3.0 if side == "BUY" else entry + 3.0
    tgt = entry + 6.0 if side == "BUY" else entry - 6.0
    return {"level_price": entry, "side": side, "trigger": "إذا رفض السعر المستوى <b>",
            "entry_type": "limit", "entry": entry, "stop": stop, "target": tgt, "rr": 2.0, "max_hold_min": 30,
            "invalidation": "إغلاق 5m تحت المستوى", "confidence": conf}


def record(as_of: int, *, scored: bool = False, plans=None, checks=None, manual=None, reason="hourly") -> dict:
    """A stored record with every key ``files.write_record`` stores (the shared interface)."""
    plans = plans if plans is not None else [plan(), plan("SELL", 45, 4210.0)]
    score = None
    if scored:
        score = {"levels": [{"price": 4200.0, "outcome": "held", "touched_ms": as_of + 10 * 60_000},
                            {"price": 4210.0, "outcome": "untouched", "touched_ms": None},
                            {"price": 4185.0, "outcome": "broken", "touched_ms": as_of + 20 * 60_000}],
                 "plans": [{"idx": 0, "outcome": "target", "fill_ms": as_of + 5 * 60_000,
                            "exit_ms": as_of + 17 * 60_000, "r": 2.0, "mfe_r": 2.1, "mae_r": -0.3, "minutes": 12.0},
                           {"idx": 1, "outcome": "stop", "fill_ms": as_of + 8 * 60_000,
                            "exit_ms": as_of + 26 * 60_000, "r": -1.0, "mfe_r": 0.4, "mae_r": -1.0, "minutes": 18.0}
                           ][:len(plans)],
                 "summary": {"levels_touched": 2, "levels_held": 1, "plans_n": len(plans), "triggered": len(plans),
                             "target_first": 1, "stop_first": 1 if len(plans) > 1 else 0, "expired": 0,
                             "sum_r": 1.0 if len(plans) > 1 else 2.0}}
    return {"version": 1, "id": files.brief_id(as_of), "as_of_ms": as_of, "reason": reason, "model": "claude-fable-5-1",
            "effort": "medium", "latency_ms": 41_000, "tokens": {"input": 12_000, "cached": 9_000, "output": 1_800},
            "images": 3, "prompt_hash": "abc", "prompt_versions": {"scalp/system": 1}, "payload_hash": "p1",
            "price_at": 4203.4, "atr5m": 4.0, "spread_now": 0.24, "spread_median": 0.24, "tol": 1.0, "blackout": False,
            "next_events": [["2025-10-09T12:30:00Z", "high", "CPI", 270]], "news_windows": {"high": [30, 15]},
            "desk": {"equity": 101.5, "currency": "USD", "legs": [], "sides": [], "kill_switch": False},
            "brief": {"regime": "range", "bias": "neutral", "bias_confidence": 55,
                      "picture": "صورة <script>x</script>", "expect": "نتوقع نطاق",
                      "levels": [{"price": 4200.0, "kind": "support", "timeframe": "15m", "strength": "strong",
                                  "why": "قاع آسيا"},
                                 {"price": 4210.0, "kind": "pdh", "timeframe": "1h", "strength": "medium",
                                  "why": "قمة أمس"},
                                 {"price": 4185.0, "kind": "liquidity_low", "timeframe": "5m", "strength": "weak",
                                  "why": "سيولة"}],
                      "plans": plans, "no_scalp_now": not plans, "no_scalp": ["خبر عالي الأثر"],
                      "news_clock": ["12:30 CPI"], "desk_warning": "النظام ما عندو صفقة", "confidence_note": ""},
            "checks": checks if checks is not None else {
                str(i): {"stop_ok": True, "target_ok": True, "news_ok": True, "desk_ok": True, "level_ok": True,
                         "rr_ok": True, "rejected": [], "notes": []} for i in range(len(plans))},
            "charts": ["1h", "15m", "5m"], "score": score, "scored_ms": as_of + 2 * HOUR if scored else None,
            "manual": manual or []}


def seed(env) -> list[dict]:
    recs = [record(T0 - 3 * HOUR, scored=True),
            record(T0 - 2 * HOUR, scored=True, plans=[plan(conf=80)],
                   checks={"0": {"rejected": ["desk_ok"], "notes": ["x"]}}),
            record(T0 - HOUR, scored=False, plans=[], reason="dashboard",
                   manual=[{"position_id": 7, "side": "SELL", "entry": 4196.1, "exit": 4192.9,
                            "open_ms": T0 - HOUR + 600_000, "close_ms": T0 - HOUR + 900_000, "volume": 0.01,
                            "pnl_usd": 3.2, "open": False, "comment": "", "brief_id": files.brief_id(T0 - HOUR),
                            "followed_plan": False}])]
    for r in recs:
        files.write_record(env.s, r, f"# brief {r['id']}\n<script>alert(1)</script>")
    return recs


# ---------------------------------------------------------------------- GET /api/scalp
def test_scalp_on_an_empty_root_gives_the_config_the_safety_text_and_no_brief(env):
    d = env.client.get("/api/scalp").json()
    assert d["enabled"] is False and d["latest"] is None and d["status"] is None
    assert d["history"] == [] and d["manual_trades"] == [] and d["charts"] == []
    assert d["config"]["model"] == "claude-fable-5-1" and d["config"]["effort"] == "medium"
    assert d["config"]["hours_utc"] == [7, 16] and d["config"]["max_per_day"] == 12
    assert d["config"]["charts"] == ["1h", "15m", "5m"]
    assert d["safety"] == SAFETY_TEXT and len(d["safety"]) == 5 and all(x["ar"] and x["en"] for x in d["safety"])
    cap = d["risk_cap"]
    assert cap["max_risk_usd"] is None and cap["max_risk_pct"] == 5.0 and cap["max_open"] == 1
    assert d["stats"]["n_scored"] == 0 and d["stats"]["plans"]["n"] == 0         # computed: no stats.json yet
    assert d["stats"]["by_confidence"]["<50"]["hit_rate"] is None


def test_scalp_reads_the_jobs_files_and_lists_the_history_newest_first(env):
    recs = seed(env)
    sdir = env.data / "scalp"
    files.write_json_atomic(sdir / "status.json", {"pid": 4321, "loop_started_ms": T0 - 5 * HOUR,
                                                   "last_run_ms": T0 - HOUR, "last_outcome": "ok", "next_run_ms": T0,
                                                   "last_error": None, "last_id": recs[-1]["id"]})
    files.write_json_atomic(sdir / "stats.json", {"n_scored": 2, "plans": {"n": 2, "hit_rate": 0.5},
                                                  "by_confidence": {}})
    files.write_json_atomic(sdir / "manual_trades.json", recs[-1]["manual"])
    (sdir / "charts").mkdir()
    (sdir / "charts" / "15m.png").write_bytes(b"\x89PNG\r\n")
    (sdir / "charts" / "1d.png").write_bytes(b"\x89PNG\r\n")                     # not a configured scalp chart
    d = env.client.get("/api/scalp?limit=2").json()
    assert d["latest"]["id"] == recs[-1]["id"] and d["latest"]["brief"]["picture"] == "صورة <script>x</script>"
    assert [h["id"] for h in d["history"]] == [recs[-1]["id"], recs[-2]["id"]]                   # limit respected
    h = env.client.get("/api/scalp").json()["history"]
    assert [x["id"] for x in h] == [r["id"] for r in reversed(recs)]
    assert h[0] == {"id": recs[-1]["id"], "as_of_ms": T0 - HOUR, "reason": "dashboard", "model": "claude-fable-5-1",
                    "regime": "range", "bias": "neutral", "bias_confidence": 55, "no_scalp_now": True, "n_levels": 3,
                    "n_plans": 0, "n_rejected": 0, "score": None, "scored_ms": None, "n_manual": 1,
                    "latency_ms": 41_000}
    assert h[1]["n_rejected"] == 1 and h[1]["score"]["target_first"] == 1 and h[2]["score"]["sum_r"] == 1.0
    assert d["status"]["pid"] == 4321 and d["status"]["last_id"] == recs[-1]["id"]
    assert d["stats"] == {"n_scored": 2, "plans": {"n": 2, "hit_rate": 0.5}, "by_confidence": {}}   # the file wins
    assert d["manual_trades"][0]["pnl_usd"] == 3.2
    assert d["charts"] == [{"tf": "15m", "updated_ms": d["charts"][0]["updated_ms"]}]
    assert d["risk_cap"]["max_risk_usd"] == 5.08 and d["risk_cap"]["daily_cap_usd"] == 10.15   # the brief's desk equity
    assert d["safety"] == SAFETY_TEXT


def test_scalp_stats_fall_back_to_the_records_and_the_equity_to_the_executor_row(env):
    seed(env)
    with sqlite3.connect(env.db) as con:
        con.execute("CREATE TABLE collector_status (collector TEXT PRIMARY KEY, state TEXT NOT NULL, "
                    "last_data_ms INTEGER, last_error TEXT, last_error_ms INTEGER, detail TEXT, "
                    "updated_ms INTEGER NOT NULL)")
        con.execute("INSERT INTO collector_status VALUES ('executor', 'live', NULL, NULL, NULL, ?, ?)",
                    (json.dumps({"equity": 250.0, "mode": "demo"}), now_ms()))
    d = env.client.get("/api/scalp").json()
    st = d["stats"]
    assert st["n_scored"] == 2 and st["plans"] == {"n": 2, "triggered": 2, "target_first": 1, "stop_first": 1,
                                                     "expired": 0, "hit_rate": 0.5, "avg_r": 0.5, "sum_r": 1.0}
    assert st["levels"]["total"] == 6 and st["manual"]["n"] == 0      # the unscored brief's manual trade not counted
    assert d["risk_cap"]["max_risk_usd"] == 12.5 and d["risk_cap"]["daily_cap_usd"] == 25.0


def test_scalp_tolerates_torn_or_oversized_files(env):
    sdir = env.data / "scalp"
    sdir.mkdir(parents=True)
    (sdir / "latest.json").write_text('{"id": "2025', encoding="utf-8")
    (sdir / "status.json").write_text("[1, 2]", encoding="utf-8")
    (sdir / "manual_trades.json").write_text("x" * (app_mod.MAX_SCALP_BYTES + 1), encoding="utf-8")
    (sdir / "briefs" / "20251009").mkdir(parents=True)
    (sdir / "briefs" / "20251009" / "0700Z.json").write_text("not json", encoding="utf-8")
    d = env.client.get("/api/scalp").json()
    assert d["latest"] is None and d["status"] is None and d["manual_trades"] == [] and d["history"] == []


# ---------------------------------------------------------------------- GET /api/scalp/{id}
def test_one_brief_by_id_and_never_a_path_from_the_request(env):
    recs = seed(env)
    bid = recs[0]["id"]
    r = env.client.get(f"/api/scalp/{bid}").json()
    assert r["id"] == bid and r["record"]["as_of_ms"] == T0 - 3 * HOUR
    assert r["record"]["score"]["summary"]["sum_r"] == 1.0
    assert r["md"] == f"# brief {bid}\n<script>alert(1)</script>"
    for bad in ("20251009T0400Z", "..", "../latest.json", "20251009T0500Z/../x", "latest", "20251009t0500z", bid + "x"):
        assert env.client.get(f"/api/scalp/{bad}").status_code == 404, bad
    files.path_of(env.s, bid, "md").unlink()                                   # the markdown is optional
    assert env.client.get(f"/api/scalp/{bid}").json()["md"] is None


# ---------------------------------------------------------------------- charts
def test_scalp_chart_images_only_for_the_configured_timeframes(env):
    assert env.client.get("/api/scalp/charts/1d").status_code == 404                      # not in scalp.charts
    assert env.client.get("/api/scalp/charts/..%2F5m").status_code == 404           # "../5m" is not a timeframe
    assert env.client.get("/api/scalp/charts/5m.png").status_code == 404
    assert env.client.get("/api/scalp/charts/5m").status_code == 404                      # not rendered yet
    d = env.data / "scalp" / "charts"
    d.mkdir(parents=True)
    (d / "5m.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    r = env.client.get("/api/scalp/charts/5m")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "no-store"
    assert r.content.startswith(b"\x89PNG")


# ---------------------------------------------------------------------- POST /api/scalp/run
def test_run_needs_the_token_and_a_correct_origin_and_starts_nothing_otherwise(env, monkeypatch):
    calls = []
    monkeypatch.setattr(app_mod, "_spawn_scalp_run", lambda s: calls.append(s) or 777)
    assert env.client.post("/api/scalp/run").status_code == 401
    assert env.client.post("/api/scalp/run", headers={"X-Dashboard-Token": "nope"}).status_code == 401
    assert env.client.post("/api/scalp/run", headers={"Origin": env.origin}).status_code == 401
    r = env.client.post("/api/scalp/run", headers={"X-Dashboard-Token": TOKEN, "Origin": "http://evil.example"})
    assert r.status_code == 403
    assert env.client.get("/api/scalp/run").status_code == 404        # "run" is no brief id (the {id} route answers)
    assert calls == []


def test_run_starts_one_detached_once_run_and_notifies(env, monkeypatch):
    calls, sent = [], []
    monkeypatch.setattr(app_mod, "_spawn_scalp_run", lambda s: calls.append(s) or 777)
    monkeypatch.setattr(app_mod, "_notify",
                        lambda s, level, title, text, **kw: sent.append((level, title, kw.get("key"))))
    r = env.client.post("/api/scalp/run", headers={"X-Dashboard-Token": TOKEN, "Origin": env.origin})
    assert r.status_code == 200
    body = r.json()
    assert body["started"] is True and body["pid"] == 777 and "scalp.enabled" in body["note"]
    assert calls == [env.s] and sent == [("info", "Scalp run requested", "scalp:run_requested")]
    # a POST without an Origin (a local script) needs only the token
    assert env.client.post("/api/scalp/run", headers={"X-Dashboard-Token": TOKEN}).status_code == 200
    assert len(calls) == 2
    cmd = app_mod._scalp_run_cmd(env.s)
    assert cmd[0].endswith("pythonw.exe") and cmd[1].endswith("scalp_brief.py")
    assert cmd[2:] == ["--once", "--quiet", "--reason", "dashboard"]


def test_run_is_refused_while_a_run_holds_the_lock(env, monkeypatch):
    calls = []
    monkeypatch.setattr(app_mod, "_spawn_scalp_run", lambda s: calls.append(s) or 777)
    lock = FileLock(env.data / "scalp" / "run.lock")
    assert lock.acquire(0)
    try:
        r = env.client.post("/api/scalp/run", headers={"X-Dashboard-Token": TOKEN, "Origin": env.origin})
        assert r.status_code == 409 and calls == []
    finally:
        lock.release()
    h = {"X-Dashboard-Token": TOKEN, "Origin": env.origin}
    assert env.client.post("/api/scalp/run", headers=h).status_code == 200
    assert len(calls) == 1
    probe = FileLock(env.data / "scalp" / "run.lock")                    # the probe released the lock again
    assert probe.acquire(0)
    probe.release()


def test_run_reports_a_start_failure(env, monkeypatch):
    def boom(s):
        raise OSError("pythonw.exe missing")
    monkeypatch.setattr(app_mod, "_spawn_scalp_run", boom)
    r = env.client.post("/api/scalp/run", headers={"X-Dashboard-Token": TOKEN, "Origin": env.origin})
    assert r.status_code == 500 and "pythonw.exe missing" in r.json()["detail"]


def test_the_spawn_goes_through_winops_outside_the_apis_job_with_the_instance_env(env, monkeypatch):
    seen = {}

    def fake_spawn(cmd, *, cwd, console, env=None, stdout=None):
        seen.update(cmd=cmd, cwd=cwd, console=console, env=env)
        return NS(pid=4242)

    import tradingsystem.supervisor.winops as winops
    monkeypatch.setattr(winops, "spawn_outside", fake_spawn)
    assert app_mod._spawn_scalp_run(env.s) == 4242
    assert seen["cmd"] == app_mod._scalp_run_cmd(env.s) and seen["console"] is False
    assert Path(seen["cwd"]) == app_mod.PROJECT_ROOT
    assert seen["env"]["TS_INSTANCE"] == "" and seen["env"]["TS_MT5_NO_LAUNCH"] == "1"


# ---------------------------------------------------------------------- Health / page
def test_the_scalp_loop_shows_among_our_processes():
    assert '"scalp_brief.py" in cmd' in inspect.getsource(app_mod._process_list)


def test_the_page_has_the_scalp_tab(env):
    html = env.client.get("/").text
    assert 'data-tab="scalp"' in html and 'id="tab-scalp"' in html
    assert 'id="sc-run"' in html and 'id="sc-refresh"' in html and 'id="sc-body"' in html


# ---------------------------------------------------------------------- D-061: POST /api/scalp/settings
def test_settings_are_saved_through_the_guarded_route_and_shown_by_the_get(env, monkeypatch):
    from tradingsystem.scalp import autotrade as at
    sent = []
    monkeypatch.setattr(app_mod, "_notify", lambda s, level, title, text, *, key=None, pair=None: sent.append((level, title, key)))
    body = {"mode": "auto", "lot": 0.02, "min_confidence": 60}
    assert env.client.post("/api/scalp/settings", json=body).status_code == 401
    assert env.client.post("/api/scalp/settings", json=body, headers={"X-Dashboard-Token": "nope"}).status_code == 401
    r = env.client.post("/api/scalp/settings", json=body, headers={"X-Dashboard-Token": TOKEN, "Origin": "http://evil.example"})
    assert r.status_code == 403
    r = env.client.post("/api/scalp/settings", json=body, headers={"X-Dashboard-Token": TOKEN, "Origin": env.origin})
    assert r.status_code == 200 and r.json()["saved"] is True
    assert r.json()["settings"]["mode"] == "auto" and r.json()["settings"]["lot"] == 0.02
    assert at.read_settings(env.s)["min_confidence"] == 60 and sent[-1][1].startswith("Scalp settings: mode auto, lot 0.02")
    d = env.client.get("/api/scalp").json()
    assert d["auto"]["settings"]["mode"] == "auto" and d["auto"]["open"] == [] and d["auto"]["log"] == []
    assert d["auto"]["magic"] == at.scalp_magic(env.s) and d["auto"]["max_lot"] == 0.10 and d["auto"]["risk_cap_pct"] == 5.0
    # a value out of bounds: 400 with the reason, nothing changed
    r = env.client.post("/api/scalp/settings", json={"lot": 0.5}, headers={"X-Dashboard-Token": TOKEN, "Origin": env.origin})
    assert r.status_code == 400 and "lot must be between" in r.json()["detail"]
    assert at.read_settings(env.s)["lot"] == 0.02
    r = env.client.post("/api/scalp/settings", json={"mode": "analyze"}, headers={"X-Dashboard-Token": TOKEN, "Origin": env.origin})
    assert r.status_code == 200 and r.json()["settings"] == {**r.json()["settings"], "mode": "analyze", "lot": 0.02, "min_confidence": 60}
    r = env.client.post("/api/scalp/settings", content=b"not json", headers={"X-Dashboard-Token": TOKEN, "Origin": env.origin,
                                                                           "Content-Type": "application/json"})
    assert r.status_code == 400
    html = env.client.get("/").text
    assert 'id="sc-mode"' in html and 'id="sc-lot"' in html and 'id="sc-minconf"' in html and 'id="sc-save"' in html
