"""P12.7 backtest runner (docs/plans/gold_live_and_backtest.md B3/B6): the production engine, risk gate, PAPER execution
and position management on a simulated clock over a chosen period, the market replayed into the run's own data root.

The loop (one process, one pair):

* between two 5-minute screens the clock moves in ``step_ms`` steps while the pair holds a position or a pending order
  (every step: the replay, the executor - paper fills and stops tick by tick, management, the model's actions, the
  desk's duties - and the engine, which wakes on executor events as live), and jumps straight to the next screen
  while it holds nothing;
* at each screen close (+ the engine's settle time) the engine screens and, when it fires, calls the model for real;
  the clock stands still during the call, then moves on by the call's real latency while protective work continues -
  the new decision reaches the executor only then (the price it meets is the price after the answer, as live).

Answers (``answers``):
* ``claude`` - the model is asked (the real provider, the run's own ledger, ``max_calls`` per run);
* ``production`` / ``replay`` - no model call: the valid decisions of the real system (``source`` = its app.db) or of an
  earlier run are stored again at their own time + their latency (calibration of the replay against what really
  happened; execution / management variants without spending the plan).

Safety: the run's settings are forced to ``execution.mode: paper`` (the executor builds no MT5 backend), notifications
off, every path inside the run root; the simulated clock is removed when the run ends.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..ai.store import DecisionRecord
from ..core import timeutil
from ..core.settings import PathsCfg, Settings, load_settings
from ..core.timeframes import Timeframe
from ..core.timeutil import MS_PER_MINUTE, iso, parse_date_spec
from ..supervisor.winops import keep_awake, sample_clock
from .market import MarketReplay, prepare

log = logging.getLogger("backtest")

STEP_MS = 5_000                      # clock step while a position / order is live
HOUSEKEEPING_MS = 60_000             # virtual outcomes + metrics, as the live executor (every minute)
SCREEN_SETTLE_MS = 6_000             # the engine screens this long after a 5-minute close (its SETTLE_MS is 5 s)
MAX_ERROR_CYCLES = 2                 # a slow / failed call happens live too; more than this = not a clean run
SUSPEND_S = 60                       # a wall/monotonic gap larger than this = the computer slept during the run
DUE_KEY = "bt:due:"                  # engine_kv: when a run's answer reached its executor (replay uses it exactly)
PROVIDER_WAIT_S = 120                # real seconds to wait for the provider's sign-in check before the clock starts


@dataclass
class RunConfig:
    start_ms: int
    end_ms: int
    pair: str = "XAUUSD"
    equity: float = 107.38
    answers: str = "claude"                  # claude | production | replay
    source: Path | None = None               # app.db of the real system / of an earlier run (production / replay)
    max_calls: int = 150
    news: bool = False                       # the news blackout: the archived real calendar of each week (B5)
    news_archive: Path | None = None         # <data>/shared/news_archive (news.ARCHIVE_DIR): <NY Sunday>.json
    brief: str | None = None                 # another desk brief for the run (an A/B of the prompt), e.g. desks/xau_live_b
    mgmt: dict = field(default_factory=dict)  # replay only: a management variant of the stored answers (variant())
    ai: dict = field(default_factory=lambda: {"daily_calls_per_pair": 120, "min_minutes_between_calls": 15,
                                              "review_floor_minutes": 10, "event_calls_per_day": 12})
    step_ms: int = STEP_MS


def ny_week(ms: int) -> str:
    """The Sunday (New York date) of the week ``ms`` lies in - the news archive's file name (news.Calendar.week)."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    d = dt.datetime.fromtimestamp(ms / 1000, ZoneInfo("America/New_York")).date()
    return str(d - dt.timedelta(days=(d.weekday() + 1) % 7))


def missing_news_weeks(archive: Path, start_ms: int, end_ms: int) -> list[str]:
    """The New York weeks of [start, end) without an archived calendar (a run with ``news`` refuses them)."""
    weeks = sorted({ny_week(t) for t in range(start_ms, end_ms, 6 * 3_600_000)} | {ny_week(end_ms - 1)})
    return [w for w in weeks if not (Path(archive) / f"{w}.json").is_file()]


def variant(rec: dict, mgmt: dict) -> dict:
    """A management variant of a stored answer (``replay`` / ``production`` runs only - the same decisions, another
    way to manage them): ``be_r`` 0 drops the model's breakeven rules, x moves them to x R (trigger r_multiple);
    ``tp_r`` x replaces the targets by one at x R from the answer's own entry (a MARKET answer without an entry price
    keeps its targets). The model's later answers still saw the original path - an approximation, said in the report."""
    import copy
    r = copy.deepcopy(rec)
    if r.get("decision") not in ("BUY", "SELL"):
        return r
    be = mgmt.get("be_r")
    if be is not None:
        rules = []
        for x in r.get("management") or []:
            if x.get("action") == "move_sl_to_breakeven":
                if not be:
                    continue
                x = {**x, "trigger": "r_multiple", "value": float(be)}
            rules.append(x)
        r["management"] = rules
    tp_r = mgmt.get("tp_r")
    entry = (r.get("entry") or {}).get("price")
    if tp_r and entry is not None and r.get("stop_loss") is not None:
        d = abs(float(entry) - float(r["stop_loss"]))
        tp = float(entry) + (tp_r * d if r["decision"] == "BUY" else -tp_r * d)
        r["take_profits"] = [{"price": round(tp, 2), "close_fraction": 1.0, "label": f"{tp_r:g}R (backtest variant)"}]
        r["management"] = [x for x in r.get("management") or [] if x.get("trigger") != "tp_hit"]
    return r


def answer_due(row_ts: int, rec: dict, latency_ms: int) -> int:
    """When a stored answer reached the executor: the real system stores a decision when its answer arrives (row ts =
    the answer), a backtest run at the call's start (its clock stands still during the call) - so the later of the
    row time and the recommendation's own time (the screen it was asked at) + the call's latency."""
    try:
        asked = parse_date_spec(rec["timestamp"])
    except (KeyError, TypeError, ValueError):
        asked = row_ts
    return max(row_ts, asked + latency_ms)


def run_settings(config: Path, run_root: Path, rc: RunConfig) -> Settings:
    """The folder's configuration with the run's paths, PAPER execution on ``rc.equity``, the run's AI caps, no
    notifications and (unless ``rc.news``) no news blackout."""
    s = load_settings(config, local_path=run_root / "no.local.yaml", env_path=run_root / "no.env",
                      extra_env={"TS_INSTANCE": rc.pair, "EXECUTION_MODE": "paper", "EXECUTION_TRIGGER": "auto"})
    pc = s.pairs[rc.pair]
    upd = {"news_blackout": pc.news_blackout.model_copy(update={"enabled": bool(rc.news)})}
    if rc.brief:
        if pc.desk is None:
            raise ValueError(f"{rc.pair} has no desk: no brief to replace")
        upd["desk"] = pc.desk.model_copy(update={"brief": rc.brief})
    pairs = {**s.pairs, rc.pair: pc.model_copy(update=upd)}
    s = s.model_copy(update={
        "paths": PathsCfg(data_dir=str(run_root / "data"), logs_dir=str(run_root / "logs"), instance=rc.pair),
        "execution": s.execution.model_copy(update={"mode": "paper", "trigger": "auto", "paper_equity": rc.equity}),
        "ai": s.ai.model_copy(update=rc.ai),
        "notify": s.notify.model_copy(update={"enabled": False, "toast": False, "telegram": False}),
        "pairs": pairs})
    if s.execution.mode != "paper":
        raise RuntimeError("a backtest runs on the paper backend only")
    return s


class Backtest:
    def __init__(self, s: Settings, rc: RunConfig, real_data: Path, run_root: Path) -> None:
        self.s, self.rc, self.real_data, self.run_root = s, rc, Path(real_data), Path(run_root)
        self.clock = rc.start_ms
        self.screen_tf = Timeframe.parse(s.ai.screen_timeframe)
        self.calls = 0
        self.stopped_by: str | None = None
        self.inject: list[tuple[int, DecisionRecord]] = []
        self.real_s = 0.0
        self._news_week: str | None = None
        self._news_at = 0
        self._clk0 = sample_clock()          # tick (with sleep) - awake (without): grows only while the computer sleeps
        self.suspended_s = 0.0

    # ------------------------------------------------------------------ setup
    def prepare(self) -> dict:
        return prepare(self.s, self.real_data, self.s.paths.data(), self.rc.start_ms)

    def _load_injections(self) -> None:
        """``production`` / ``replay``: the source's valid decisions of the pair inside the period, due at ts + latency."""
        src = Path(self.rc.source)
        con = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
        try:
            try:                      # an earlier run's own record of when each answer reached its executor (exact)
                due_of = {k[len(DUE_KEY):]: int(json.loads(v)) for k, v in
                          con.execute("SELECT key, value FROM engine_kv WHERE key LIKE ?", (DUE_KEY + "%",))}
            except sqlite3.OperationalError:
                due_of = {}
            rows = con.execute(
                "SELECT id, ts, trigger, recommendation, raw_text, latency_ms, input_tokens, output_tokens, "
                "trigger_strength, session, regime, htf_bias, provider, model FROM ai_decisions WHERE pair=? AND "
                "status='valid' AND ts>=? AND ts<? ORDER BY ts", (self.rc.pair, self.rc.start_ms, self.rc.end_ms)).fetchall()
        finally:
            con.close()
        for (did, ts, trig, rec, raw, lat, tin, tout, strength, sess, regime, bias, prov, model) in rows:
            r = json.loads(rec) if rec else None
            if not r:
                continue
            if self.rc.mgmt:
                r = variant(r, self.rc.mgmt)
            dr = DecisionRecord(self.rc.pair, "agent_per_pair", (trig or "")[:600], "valid", provider=prov, model=model,
                                recommendation=r, raw_text=raw, latency_ms=int(lat or 0), input_tokens=int(tin or 0),
                                output_tokens=int(tout or 0), trigger_strength=strength, session=sess, regime=regime,
                                htf_bias=bias, actions_state="pending" if r.get("position_actions") else None,
                                id=did, ts=int(ts))
            self.inject.append((due_of.get(did) or answer_due(int(ts), r, int(lat or 0)), dr))
        self.inject.sort(key=lambda x: x[0])

    # ------------------------------------------------------------------ the loop
    async def run(self) -> dict:
        from ..analysis.engine import Engine           # imported here: the clock is set before any start-up time read
        from ..execution.executor import Executor
        os.environ["TS_NOTIFY_DISABLE"] = "1"
        os.environ["TS_NEWS_NO_FETCH"] = "1"
        timeutil.set_sim_clock(lambda: self.clock)
        keep_awake(True)                     # no idle sleep while the run goes on (lid / button / battery still can)
        t_real = time.time()
        self.market = MarketReplay(self.s, self.real_data, self.s.paths.data(), self.rc.start_ms)
        self.ex = Executor(self.s)
        self.eng = None
        try:
            if self.rc.answers == "claude":
                self.eng = Engine(self.s)
                for pair, pcfg in self.s.enabled_pairs().items():   # as Engine.run: not the bar before the start
                    tf = pcfg.decision_timeframe
                    self.eng.processed[pair] = tf.floor(self.clock) - tf.ms
                    stf = self.eng.screen_tf(pair)
                    self.eng.processed_screen[pair] = stf.floor(self.clock) - stf.ms
                self.eng._init_event_cursor()
                # the provider's sign-in check runs in the background at start-up: wait for it (real time) so the
                # first screens of the period are not lost to "AI provider not ready"
                t_ready = time.time()
                while not self.eng.ai_ready() and time.time() - t_ready < PROVIDER_WAIT_S:
                    await asyncio.sleep(1.0)
                if not self.eng.ai_ready():
                    raise RuntimeError(f"the AI provider is not ready: {self.eng._ai_problem}")
            else:
                self._load_injections()
            last_hk = self.clock
            while self.clock < self.rc.end_ms and self.stopped_by is None:
                screen = self.screen_tf.floor(self.clock) + self.screen_tf.ms + SCREEN_SETTLE_MS
                while self.clock < min(screen, self.rc.end_ms):
                    tgt = min(screen, self.rc.end_ms)
                    if self._busy():
                        tgt = min(tgt, self.clock + self.rc.step_ms)
                    due = next((t for t, _ in self.inject if t > self.clock), None)
                    if due is not None:
                        tgt = min(tgt, due)
                    self._to(tgt)
                    self._inject_due()
                    last_hk = self._ex_step(last_hk)
                    if self.eng is not None and self._busy():
                        await self._engine_tick()
                if self.clock >= self.rc.end_ms:
                    break
                if self.eng is not None:
                    await self._engine_tick()
        finally:
            self.real_s = time.time() - t_real
            try:
                self._finish()
            finally:
                timeutil.set_sim_clock(None)
                keep_awake(False)
        errors = self._error_cycles()
        interrupted = bool(self.suspended_s) or errors > MAX_ERROR_CYCLES
        return {"calls": self.calls, "stopped_by": self.stopped_by, "end": iso(self.clock), "real_s": round(self.real_s),
                "suspended_s": round(self.suspended_s), "error_cycles": errors, "interrupted": interrupted}

    def _error_cycles(self) -> int:
        """Cycles that ended without an answer (a failed or cut-off call) - a run with any is not a clean sample."""
        try:
            con = sqlite3.connect(f"file:{(self.s.paths.state() / 'app.db').as_posix()}?mode=ro", uri=True)
            try:
                return int(con.execute("SELECT count(*) FROM ai_decisions WHERE status IN ('error','interrupted')")
                           .fetchone()[0])
            finally:
                con.close()
        except sqlite3.Error:
            return 0

    def _check_suspend(self) -> None:
        """A sleep / hibernate of the computer during the run (the supervisor's measure: time since boot with sleep -
        without sleep grew). A call
        in flight across it may have failed - the run is marked interrupted (the report says so; the queue re-runs it)."""
        c = sample_clock()
        gap = (c.tick - c.awake) - (self._clk0.tick - self._clk0.awake)
        if gap > SUSPEND_S and gap > self.suspended_s + 1:
            log.warning("the computer slept for about %.0f s during the run (at %s)", gap - self.suspended_s, iso(self.clock))
            self.suspended_s = gap

    def _to(self, t: int) -> None:
        self._check_suspend()
        if t > self.clock:
            self.clock = t
            self.market.advance(t)
            self._news_sync()

    def _news_sync(self) -> None:
        """B5: with ``news`` the run's calendar file is the archived real calendar of the clock's New York week,
        written as the live engine's hourly download would have (fetched = the clock)."""
        if not self.rc.news:
            return
        week = ny_week(self.clock)
        if week == self._news_week and self.clock - self._news_at < 3_600_000:
            return
        doc = json.loads((Path(self.rc.news_archive) / f"{week}.json").read_text(encoding="utf-8"))
        doc.update(fetched_ms=self.clock, fetched=iso(self.clock))
        dest = self.s.paths.shared() / "news_calendar.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
        self._news_week, self._news_at = week, self.clock

    def _busy(self) -> bool:
        return bool(self.ex.paper.watermarks())        # a pending order or an open position of the run

    def _inject_due(self) -> None:
        while self.inject and self.inject[0][0] <= self.clock:
            _, dr = self.inject.pop(0)
            self.ex.store.save(dr)

    def _ex_step(self, last_hk: int) -> int:
        hk = self.clock - last_hk >= HOUSEKEEPING_MS
        acct = self.ex.step(hk)
        self.ex.appdb.set_status("executor", "live", last_data_ms=self.clock, detail=self.ex.status_detail(acct))
        return self.clock if hk else last_hk

    def _protect(self) -> None:
        """While an answer is 'on its way' (its latency): what the live executor keeps doing - paper fills and stops,
        management, the desk's duties - but no new candidate and none of the model's new actions yet."""
        self.ex.advance_paper()
        for step in (self.ex.manage_positions, self.ex.desk_reviews):
            try:
                step()
            except Exception:  # noqa: BLE001 — as Executor.step
                log.exception("%s failed", step.__name__)

    async def _engine_tick(self) -> None:
        if self.calls >= self.rc.max_calls:
            self.stopped_by = f"max_calls {self.rc.max_calls} reached at {iso(self.clock)}"
            return
        await self.eng.tick()
        if not self.eng.inflight:
            return
        as_of = self.clock
        t0 = time.time()
        tasks = list(set(self.eng.inflight.values()))
        await asyncio.gather(*tasks, return_exceptions=True)
        latency_ms = int((time.time() - t0) * 1000)
        self.calls = self._ledger_calls()
        with self.ex.store._lock:
            ids = [r[0] for r in self.ex.store._con.execute("SELECT id FROM ai_decisions WHERE ts=?", (as_of,))]
        for did in ids:
            self.ex.store.kv_set(f"{DUE_KEY}{did}", as_of + latency_ms)   # when this answer reached the executor
        log.info("call(s) at %s answered after %.0f s (calls so far %d)", iso(as_of), latency_ms / 1000, self.calls)
        end = as_of + latency_ms
        while self.clock < end:                         # the answer is on its way: the market moves on
            self._to(min(end, self.clock + self.rc.step_ms))
            self._protect()

    def _ledger_calls(self) -> int:
        db = self.s.paths.shared() / "ai_usage.db"
        if not db.exists():
            return 0
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            return int(con.execute("SELECT count(*) FROM ai_usage").fetchone()[0])
        finally:
            con.close()

    def _finish(self) -> None:
        for obj in (getattr(self, "market", None),):
            if obj is not None:
                obj.close()
        if self.eng is not None:
            try:
                self.eng.close()
            except Exception:  # noqa: BLE001
                log.exception("engine close failed")
        ex = getattr(self, "ex", None)
        if ex is not None:                    # every handle on the run's stores: the market copy is deleted after
            for close in (getattr(ex.paper, "close", None), ex.store.close, ex.appdb.close,
                          *(r.close for r in getattr(ex, "readers", {}).values())):
                try:
                    if close is not None:
                        close()
                except Exception:  # noqa: BLE001
                    log.exception("executor close failed")


def default_run_root(real_data: Path, run_id: str) -> Path:
    return Path(real_data) / "backtests" / run_id


def run_id_for(rc: RunConfig) -> str:
    return f"{iso(rc.start_ms)[:16].replace(':', '')}_{iso(rc.end_ms)[:16].replace(':', '')}_{rc.answers}_" \
           f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"


__all__ = ["Backtest", "RunConfig", "run_settings", "default_run_root", "run_id_for", "MS_PER_MINUTE"]
