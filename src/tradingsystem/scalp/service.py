"""The scalp job's runs (D-058): the gates, one run (``--once``), the scoring of the stored briefs, the hourly loop
and its detached start. Separate from the supervisor on purpose: every run is a short-lived process that frees its
RAM when it ends, and the loop itself imports nothing heavy. Nothing here places an order.

A run, in order: the gates (enabled, market open, the hours for an hourly run, the daily cap, the desk not in the
middle of a call, the operator-session lock, free RAM - each ``wait`` retried every 30 s up to ``gate_retry_min``),
the run lock, the scoring of every stored brief that matured (free: the real ticks), ``stats.json``, the owner's
manual deals (only when the terminal runs), the inputs, the model, the code checks, the record + markdown, the
notification, ``status.json``. Exit codes: 0 ok, 1 failed, 2 skipped (as the operator runner).

The ``--no-model --fixture`` path writes a full record from a fixture through the same store / notify path and
leaves no ledger row (the owner's dry run); it bypasses the model gates (the desk, the cap, the operator lock)."""
from __future__ import annotations

import asyncio
import datetime as dt
import importlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from ..core.filelock import FileLock, locks_dir
from ..core.instruments import InstrumentRegistry
from ..core.notify import flush, notify
from ..core.sessions import calendar_for
from ..core.settings import PROJECT_ROOT, ScalpCfg, Settings
from ..core.timeutil import MS_PER_DAY, MS_PER_HOUR, MS_PER_MINUTE, now_ms
from .account import EXECUTOR_FRESH_MS, desk_block, ro_connect
from .contract import ScalpBrief
from .files import list_ids, read_json, read_record, recent_records, scalp_dir, write_json_atomic, write_record

log = logging.getLogger(__name__)

EXIT_OK, EXIT_FAILED, EXIT_SKIPPED = 0, 1, 2
ENGINE_FRESH_S = 60.0              # an engine row older than this = no desk call can be in flight
GATE_POLL_S = 30.0                 # a ``wait`` gate is looked at again this often
LOOP_POLL_S = 15.0                 # the loop reads STOP this often
MANAGE_EVERY_MS = 60_000           # D-061: the loop looks after the auto-trades this often
STATUS_EVERY_MS = 60_000           # the loop rewrites status.json this often while idle
OPERATOR_LOCK = "operator_session.lock"
REASONS = ("hourly", "dashboard", "manual")
STATUS_KEYS = ("pid", "loop_started_ms", "last_run_ms", "last_outcome", "next_run_ms", "last_error", "last_id")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------- probes (patched by the tests)
def _free_ram_mb() -> float:
    import psutil
    return psutil.virtual_memory().available / 2**20


def _clock() -> int:
    return now_ms()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _mod(name: str):
    """A sibling module, looked up at call time (``sys.modules`` first: the tests substitute it)."""
    return importlib.import_module(f"{__package__}.{name}")


def status_rows(app_db: Path, now: int) -> tuple[dict | None, float | None, float | None]:
    """(engine detail, engine age s, executor age s) from the desk's ``collector_status``; None = no row / no db."""
    if not app_db.exists():
        return None, None, None
    try:
        con = ro_connect(app_db)
    except sqlite3.Error:
        return None, None, None
    try:
        rows = con.execute("SELECT collector, detail, updated_ms FROM collector_status "
                           "WHERE collector IN ('engine', 'executor')").fetchall()
    except sqlite3.Error:
        return None, None, None
    finally:
        con.close()
    engine: dict | None = None
    eng_age = exe_age = None
    for r in rows:
        age = (now - int(r["updated_ms"])) / 1000 if r["updated_ms"] is not None else None
        if r["collector"] == "engine":
            try:
                engine = json.loads(r["detail"]) if r["detail"] else {}
            except ValueError:
                engine = {}
            eng_age = age
        else:
            exe_age = age
    return engine, eng_age, exe_age


# --------------------------------------------------------------------------- the pure decisions
def gate(now: int, cfg: ScalpCfg, *, engine_detail: dict | None, engine_age_s: float | None,
         executor_age_s: float | None, free_ram_mb: float | None, used_today: int, market_open: bool,
         hourly: bool) -> tuple[str, str]:
    """One look at the gates: ``("run", note)`` | ``("wait", why)`` (looked at again) | ``("skip", why)`` (this
    run is over). ``hourly`` runs keep to ``hours_utc``; an on-demand run ignores the hours."""
    if not cfg.enabled:
        return "skip", "disabled"
    if not market_open:
        return "skip", "market_closed"
    hour = dt.datetime.fromtimestamp(now / 1000, dt.timezone.utc).hour
    if hourly and not (cfg.hours_utc[0] <= hour < cfg.hours_utc[1]):
        return "skip", "outside_hours"
    if used_today >= cfg.max_per_day:
        return "skip", f"daily_cap ({used_today}/{cfg.max_per_day})"
    notes = []
    if engine_detail is None or engine_age_s is None or engine_age_s > ENGINE_FRESH_S:
        notes.append("engine not reporting (nothing to collide with)")
    elif engine_detail.get("cycles_inflight"):
        return "wait", "desk_call_in_flight"
    if executor_age_s is None or executor_age_s > EXECUTOR_FRESH_MS / 1000:
        notes.append("executor not reporting (positions unknown)")
    if free_ram_mb is not None and free_ram_mb < cfg.min_free_ram_mb:
        return "wait", f"low_ram ({free_ram_mb:.0f} MB < {cfg.min_free_ram_mb})"
    return "run", "; ".join(notes) or "ok"


def next_slot(now: int, cfg: ScalpCfg) -> int:
    """The next hourly run after ``now``: the first full hour in ``hours_utc`` plus ``run_offset_s`` (UTC ms)."""
    h0, h1 = cfg.hours_utc
    off = cfg.run_offset_s * 1000
    day0 = now // MS_PER_DAY * MS_PER_DAY
    for d in range(0, 3):
        for h in range(h0, h1):
            t = day0 + d * MS_PER_DAY + h * MS_PER_HOUR + off
            if t > now:
                return t
    raise ValueError("scalp.hours_utc has no hour")      # unreachable: the config validator needs start < end


def horizon_min(rec: dict, cfg: ScalpCfg) -> int:
    """How far after the brief the scorer needs ticks: the trigger window plus the longest hold, or the level
    window plus the hold-decision window, whichever is later."""
    plans = (rec.get("brief") or {}).get("plans") or []
    longest = max((int(p.get("max_hold_min") or 0) for p in plans), default=0)
    return max(cfg.trigger_window_min + longest, cfg.level_horizon_min + cfg.hold_window_min)


# --------------------------------------------------------------------------- status.json
def update_status(s: Settings, **fields: Any) -> dict:
    """Merge ``fields`` into ``status.json`` (the dashboard's and ``--status``'s view)."""
    path = scalp_dir(s) / "status.json"
    doc = read_json(path)
    doc = doc if isinstance(doc, dict) else {}
    doc = {k: doc.get(k) for k in STATUS_KEYS} | fields | {"updated_ms": now_ms()}
    write_json_atomic(path, doc)
    return doc


def status_doc(s: Settings) -> dict:
    """``--status``: status.json plus what can be probed now (the loop lock, STOP, the newest brief)."""
    sdir = scalp_dir(s)
    doc = read_json(sdir / "status.json")
    doc = doc if isinstance(doc, dict) else {}
    lock = FileLock(sdir / "loop.lock")
    running = not lock.acquire(0)
    if not running:
        lock.release()
    ids = list_ids(s, 1)
    return {**doc, "loop_running": running, "stop_file": (sdir / "STOP").exists(), "latest_id": ids[0] if ids else None,
            "enabled": s.scalp.enabled, "hours_utc": list(s.scalp.hours_utc)}


# --------------------------------------------------------------------------- one run
def run_once(s: Settings, *, reason: str = "manual", no_model: bool = False, fixture: Path | str | None = None,
             now: int | None = None) -> int:
    """The whole run (module docstring). ``now`` fixes the brief's time (tests / replays); None = the wall clock."""
    if reason not in REASONS:
        raise ValueError(f"reason {reason!r}: one of {', '.join(REASONS)}")
    if no_model and not fixture:
        raise ValueError("--no-model needs --fixture <brief.json>")
    cfg = s.scalp
    pair = cfg.pair
    if pair not in s.pairs:
        log.error("scalp.pair %s is not a configured pair", pair)
        return EXIT_FAILED
    clock: Callable[[], int] = (lambda: now) if now is not None else _clock
    reg = InstrumentRegistry.from_settings(s)
    exe = reg.with_role(pair, "execution")[0]
    pcfg = s.pairs[pair]
    cal = calendar_for(exe.venue, exe.symbol, pcfg.asset_class)
    sdir = scalp_dir(s)
    run_lock = FileLock(sdir / "run.lock")
    if not run_lock.acquire(0):
        log.warning("scalp: a run is already in progress - this one ends")
        update_status(s, last_outcome="skip:run_in_progress")
        return EXIT_SKIPPED
    op_lock = FileLock(locks_dir(s) / OPERATOR_LOCK)
    usage = None
    try:
        if not no_model:
            from ..ai.budget import UsageStore, usage_db
            from ..ai.providers.base import quota_day_start
            from .brief import provider_name
            usage = UsageStore(usage_db(s))
            tz = s.ai.providers[provider_name(s)].quota_reset_tz
            deadline = time.monotonic() + cfg.gate_retry_min * 60
            while True:
                t = clock()
                eng, eng_age, exe_age = status_rows(s.paths.state() / "app.db", t)
                used = usage.count_role_since("scalp", quota_day_start(t, tz))
                kind, why = gate(t, cfg, engine_detail=eng, engine_age_s=eng_age, executor_age_s=exe_age,
                                 free_ram_mb=_free_ram_mb(), used_today=used, market_open=cal.is_open(t),
                                 hourly=reason == "hourly")
                if kind == "run" and not op_lock.acquire(0):
                    kind, why = "wait", "operator_session"
                if kind == "run":
                    log.info("scalp %s: gates passed (%s)", reason, why)
                    break
                if kind == "skip" or time.monotonic() >= deadline:
                    outcome = f"skip:{why}" if kind == "skip" else f"skip:{why} (after {cfg.gate_retry_min} min)"
                    log.info("scalp %s: %s", reason, outcome)
                    update_status(s, last_run_ms=clock(), last_outcome=outcome)
                    return EXIT_SKIPPED
                log.info("scalp %s: waiting - %s", reason, why)
                _sleep(GATE_POLL_S)
        return _run_body(s, reg, exe, usage, reason=reason, no_model=no_model, fixture=fixture, as_of=clock())
    finally:
        if op_lock.held:
            op_lock.release()
        run_lock.release()
        if usage is not None:
            usage.close()


def _fail(s: Settings, stage: str, err: Any, as_of: int) -> int:
    msg = f"{stage}: {str(err)[:300]}"
    log.error("scalp brief failed - %s", msg)
    if s.scalp.notify:
        day = dt.datetime.fromtimestamp(as_of / 1000, dt.timezone.utc).strftime("%Y%m%d")
        notify(s, "warn", "Scalp brief failed", msg, key=f"scalp:fail:{day}", pair=s.scalp.pair)
        flush(15.0)
    update_status(s, last_run_ms=now_ms(), last_outcome=f"failed:{stage}", last_error=msg)
    return EXIT_FAILED


def manual_text(trades: list[dict], n: int = 5) -> str:
    """The owner's last manual trades as the prompt reads them (one line each, newest first)."""
    out = []
    for t in sorted(trades, key=lambda x: x.get("open_ms") or 0, reverse=True)[:n]:
        when = dt.datetime.fromtimestamp((t.get("open_ms") or 0) / 1000, dt.timezone.utc).strftime("%m-%d %H:%M")
        line = f"{when} {t.get('side')} {t.get('entry')}" + (" (auto)" if t.get("auto") else "")
        line += f" -> {t.get('exit')}" if t.get("exit") is not None else " (open)"
        if isinstance(t.get("pnl_usd"), (int, float)):
            line += f" {t['pnl_usd']:+.2f} USD"
        if t.get("brief_id"):
            line += " (plan followed)" if t.get("followed_plan") else " (own idea)"
        out.append(line)
    return "\n".join(out)


def _run_body(s: Settings, reg: InstrumentRegistry, exe, usage, *, reason: str, no_model: bool,
              fixture: Path | str | None, as_of: int) -> int:
    from . import brief as briefmod
    cfg = s.scalp
    pair = cfg.pair
    try:
        manual = score_pending(s, reg, exe, now=as_of)
    except Exception:  # noqa: BLE001 - the scoring is free; its failure never costs the hour's brief
        log.exception("scalp: scoring the stored briefs failed (the brief goes on)")
        manual = _stored_manual(s)
    try:
        inputs = briefmod.build_inputs(s, reg, pair, as_of)
    except Exception as exc:  # noqa: BLE001
        return _fail(s, "inputs", exc, as_of)
    desk = desk_block(inputs.account)
    scored = [r for r in recent_records(s, cfg.stats_n) if r.get("score")]
    try:
        jtext = _mod("journal").journal_text(scored, manual, n_briefs=cfg.journal_n)
    except Exception:  # noqa: BLE001
        log.exception("scalp: the journal text failed - the model gets none this hour")
        jtext = ""
    system, user, versions = briefmod.compose(s, reg, inputs, reason=reason, desk=desk, manual_text=manual_text(manual),
                                              journal_text=jtext)
    gen = None
    if no_model:
        try:
            brief = ScalpBrief.model_validate(json.loads(Path(fixture).read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            return _fail(s, "fixture", exc, as_of)
        log.info("scalp %s: no model call - the brief comes from %s", reason, fixture)
    else:
        try:
            gen = asyncio.run(briefmod.call_model(s, usage, system, user, inputs.images))
        except Exception as exc:  # noqa: BLE001
            return _fail(s, "model", exc, as_of)
        if not gen.ok or gen.value is None:
            why = gen.provider_error or "; ".join(gen.errors) or ("refused" if gen.refused else "no valid answer")
            return _fail(s, "model", ("budget: " if gen.budget_blocked else "") + why, as_of)
        brief = gen.value
    ck = _mod("checks")
    tol = float(ck.tolerance(inputs.atr5m, inputs.spread_median))
    levels = [lv.model_dump() for lv in brief.levels]
    checks = {str(i): ck.check_plan(p.model_dump(), atr5m=inputs.atr5m, spread=inputs.spread_now,
                                    blackout=inputs.blackout, next_events=inputs.next_events,
                                    news_windows=inputs.news_windows, desk_sides=list(desk.get("sides") or []),
                                    levels=levels, tol=tol, as_of=as_of, trigger_window_min=cfg.trigger_window_min)
              for i, p in enumerate(brief.plans)}
    rec = briefmod.to_record(s, inputs=inputs, reason=reason, brief=brief, checks=checks, desk=desk, tol=tol, gen=gen,
                             system=system, versions=versions)
    md = briefmod.render_md(rec, cap_pct=cfg.manual_risk_cap_pct)
    path = write_record(s, rec, md)
    log.info("scalp %s: brief %s stored (%s; %d levels, %d plans, no_scalp=%s)", reason, rec["id"], path,
             len(brief.levels), len(brief.plans), brief.no_scalp_now)
    if cfg.notify:
        title, text = briefmod.notify_text(rec, md)
        key = f"scalp:{rec['id']}" + ("" if reason == "hourly" else ":manual")
        notify(s, cfg.notify_level, title, text, key=key, pair=pair)
        flush(15.0)
    try:                                               # D-061: THE trade placed when the owner switched auto on
        pcfg = s.pairs[pair]
        cal = calendar_for(exe.venue, exe.symbol, pcfg.asset_class)
        ex = _mod("autotrade").execute(s, rec, symbol=exe.symbol, equity=desk.get("equity"),
                                       market_open=cal.is_open(as_of), now=as_of)
        rec["execution"] = ex
        write_record(s, rec, None)
        if cfg.notify:
            flush(15.0)
    except Exception:  # noqa: BLE001 - the brief stands; the order is the smaller thing
        log.exception("scalp: auto execution failed (the brief is stored)")
    update_status(s, last_run_ms=now_ms(), last_outcome="ok", last_error=None, last_id=rec["id"])
    return EXIT_OK


# --------------------------------------------------------------------------- scoring (free: the stored ticks)
def _stored_manual(s: Settings) -> list[dict]:
    doc = read_json(scalp_dir(s) / "manual_trades.json")
    return [t for t in doc if isinstance(t, dict)] if isinstance(doc, list) else []


def score_pending(s: Settings, reg: InstrumentRegistry, exe, *, now: int) -> list[dict]:
    """Score every stored brief that matured (``scorer.is_mature``) on the real ticks, write ``stats.json`` over the
    last ``stats_n`` scored briefs, refresh the owner's manual trades (when configured and the terminal runs) and
    attach them to their briefs. Returns the manual trades (newest first)."""
    cfg = s.scalp
    sc = _mod("scorer")
    records = [r for bid in list_ids(s, max(2 * cfg.stats_n, 40)) if (r := read_record(s, bid)) is not None]
    pending = [r for r in records if r.get("score") is None and sc.is_mature(r, now, cfg)]
    if pending:
        from ..analysis.snapshot import SnapshotBuilder
        builder = SnapshotBuilder(s, reg)
        try:
            reader = builder.reader(exe)
            for rec in pending:
                t0 = int(rec["as_of_ms"])
                try:
                    t, bid, ask = sc.read_ticks(reader, exe, t0, t0 + horizon_min(rec, cfg) * MS_PER_MINUTE)
                    rec["score"] = sc.score_record(rec, t, bid, ask, now=now, cfg=cfg)
                    rec["scored_ms"] = now
                    write_record(s, rec, None)
                    log.info("scalp: brief %s scored: %s", rec["id"], (rec["score"] or {}).get("summary"))
                except Exception:  # noqa: BLE001 - one brief's ticks must not stop the others
                    log.exception("scalp: scoring %s failed", rec.get("id"))
        finally:
            builder.close()
    scored = [r for r in records if r.get("score")]
    write_json_atomic(scalp_dir(s) / "stats.json", _mod("journal").stats(scored, n=cfg.stats_n))
    if cfg.manual_lookback_days <= 0:
        return _stored_manual(s)
    return _refresh_manual(s, exe, records, now)


def _refresh_manual(s: Settings, exe, records: list[dict], now: int) -> list[dict]:
    """The owner's deals (magic 0) of the last ``manual_lookback_days`` from the RUNNING terminal (never started),
    attributed to their briefs; the stored list when the terminal is not there."""
    from ..ingest.mt5.servertime import ServerTimeModel
    from ..ingest.mt5.terminal import terminal_running
    cfg = s.scalp
    prof = s.mt5_data_profile()
    if not terminal_running(prof.terminal_path, min_age_s=30):
        log.info("scalp: the MT5 terminal is not running - manual trades not refreshed")
        return _stored_manual(s)
    sc = _mod("scorer")
    t0 = now - cfg.manual_lookback_days * MS_PER_DAY
    try:
        deals = sc.fetch_deals(s, t0, now)
    except Exception as exc:  # noqa: BLE001 - a terminal that refuses the attach: the stored list stands
        log.warning("scalp: manual deals not read (%s)", exc)
        return _stored_manual(s)
    model = ServerTimeModel()
    trades = sc.manual_trades(deals, symbol=exe.symbol, t0=t0, t1=now,
                              server_to_utc=lambda ms: model.server_to_utc(ms, prefer="earlier"),
                              auto_magic=_mod("autotrade").scalp_magic(s))
    tol = next((float(r["tol"]) for r in records if isinstance(r.get("tol"), (int, float))), 1.0)
    sc.attribute(trades, records, tol)
    trades.sort(key=lambda t: t.get("open_ms") or 0, reverse=True)
    write_json_atomic(scalp_dir(s) / "manual_trades.json", trades)
    by: dict[str, list[dict]] = {}
    for t in trades:
        if t.get("brief_id"):
            by.setdefault(str(t["brief_id"]), []).append(t)
    for rec in records:
        mine = by.get(rec["id"], [])
        if mine != (rec.get("manual") or []):
            rec["manual"] = mine
            write_record(s, rec, None)
    return trades


def score_only(s: Settings) -> int:
    """``--score-only``: the scoring step alone (no gates but the run lock, no model)."""
    pair = s.scalp.pair
    reg = InstrumentRegistry.from_settings(s)
    exe = reg.with_role(pair, "execution")[0]
    lock = FileLock(scalp_dir(s) / "run.lock")
    if not lock.acquire(0):
        log.warning("scalp: a run is in progress - score later")
        return EXIT_SKIPPED
    try:
        score_pending(s, reg, exe, now=_clock())
    finally:
        lock.release()
    return EXIT_OK


# --------------------------------------------------------------------------- the loop and its start
def child_timeout_s(cfg: ScalpCfg) -> int:
    """How long the loop lets one hourly child live: its gate wait (``gate_retry_min``) plus the run itself
    (``run_timeout_s``)."""
    return int(cfg.gate_retry_min) * 60 + int(cfg.run_timeout_s)


def spawn_once(s: Settings, *, timeout_s: int, python: str | None = None) -> int | None:
    """One hourly child (``--once --quiet --reason hourly``), waited for; killed with its tree after ``timeout_s``."""
    cmd = [python or sys.executable, str(PROJECT_ROOT / "tools" / "scalp_brief.py"), "--once", "--quiet",
           "--reason", "hourly"]
    env = {**os.environ, "TS_INSTANCE": s.paths.instance or "", "TS_MT5_NO_LAUNCH": "1"}
    p = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    try:
        return p.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        from ..supervisor.procs import kill_tree
        log.error("scalp: the hourly run exceeded %d s - killed (pid %d)", timeout_s, p.pid)
        kill_tree(p.pid)
        return None


def _manage_tick(s: Settings) -> None:
    """D-061: the auto-trades' minute tick (nothing happens while nothing is tracked - no MT5 attach)."""
    try:
        _mod("autotrade").manage(s)
    except Exception:  # noqa: BLE001 - never the loop's end
        log.exception("scalp: the auto-trade tick failed")


def loop(s: Settings, *, clock: Callable[[], int] = now_ms, sleep: Callable[[float], None] = time.sleep,
         spawn: Callable[[], Any] | None = None, market_open: Callable[[int], bool] | None = None,
         poll_s: float = LOOP_POLL_S, manage: Callable[[], Any] | None = None) -> int:
    """The tiny scheduler: at every slot of ``hours_utc`` (+ ``run_offset_s``) one ``--once`` child, ``status.json``
    every minute, the end when ``STOP`` appears. Never two loops (``loop.lock``). A slot while the market is closed
    (the weekend) starts nothing."""
    cfg = s.scalp
    sdir = scalp_dir(s)
    sdir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(sdir / "loop.lock")
    if not lock.acquire(0):
        log.warning("scalp: a loop already runs - this one ends")
        return EXIT_SKIPPED
    if market_open is None:
        pcfg = s.pairs[cfg.pair]
        exe = InstrumentRegistry.from_settings(s).with_role(cfg.pair, "execution")[0]
        market_open = calendar_for(exe.venue, exe.symbol, pcfg.asset_class).is_open
    # the child may wait up to gate_retry_min for the desk's call / free RAM BEFORE its run: the kill timeout covers
    # both (2026-10-12: a 600 s timeout killed every hourly run while it was still politely waiting on low RAM)
    run = spawn or (lambda: spawn_once(s, timeout_s=child_timeout_s(cfg)))
    tick = manage or (lambda: _manage_tick(s))
    managed = 0
    try:
        started = clock()
        nxt = next_slot(started, cfg)
        update_status(s, pid=os.getpid(), loop_started_ms=started, next_run_ms=nxt)
        log.info("scalp loop started (pid %d): hours %s UTC, next run at %s", os.getpid(), cfg.hours_utc,
                 dt.datetime.fromtimestamp(nxt / 1000, dt.timezone.utc).isoformat(timespec="seconds"))
        written = started
        while not (sdir / "STOP").exists():
            t = clock()
            if t >= nxt:
                if market_open(t):
                    code = run()
                    log.info("scalp: hourly run ended with %s", code)
                else:
                    log.info("scalp: slot skipped - the market is closed")
                nxt = next_slot(clock(), cfg)
                written = clock()
                update_status(s, pid=os.getpid(), next_run_ms=nxt)
            elif t - written >= STATUS_EVERY_MS:
                written = t
                update_status(s, pid=os.getpid(), next_run_ms=nxt)
            if t - managed >= MANAGE_EVERY_MS:              # D-061: the open scalp order / position
                managed = t
                tick()
            sleep(max(0.0, min(poll_s, (nxt - clock()) / 1000)))
        log.info("scalp loop: STOP found - the loop ends")
        update_status(s, pid=None, next_run_ms=None)
        return EXIT_OK
    finally:
        lock.release()


def start_loop_detached(s: Settings) -> tuple[bool, int | None]:
    """``(started, pid)``: a hidden, detached loop (``pythonw tools/scalp_brief.py --loop --quiet``) unless one runs
    (then its pid from status.json)."""
    from ..supervisor import winops
    sdir = scalp_dir(s)
    lock = FileLock(sdir / "loop.lock")
    if not lock.acquire(0):
        doc = read_json(sdir / "status.json")
        return False, (doc.get("pid") if isinstance(doc, dict) else None)
    lock.release()
    pyw = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    exe = str(pyw) if pyw.exists() else sys.executable
    p = winops.spawn_outside([exe, str(PROJECT_ROOT / "tools" / "scalp_brief.py"), "--loop", "--quiet"],
                             cwd=PROJECT_ROOT, console=False,
                             env={**os.environ, "TS_INSTANCE": s.paths.instance or "", "TS_MT5_NO_LAUNCH": "1"})
    return True, p.pid
