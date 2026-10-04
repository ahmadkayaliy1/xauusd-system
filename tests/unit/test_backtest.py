"""P12.7 tick-level backtest: the simulated clock, the run's market (prepared as it stood just before the start, the
real root never written), the replay's causality (no row in the run's stores is ever after the clock, a candle appears
at its close + BAR_DELAY_MS, the forming bar is the clock's), when a stored answer reaches the executor, the swap
nights of the report and the run's paper-only settings.

The source market here is hand-built (labelled pure-logic stand-ins: a price walk on a regular grid) - these tests
check the release times and the bookkeeping, not prices. The replay on the real recorded market is checked by the
calibration run against the real system's own decisions (PROJECT_STATUS P12.7)."""
from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import pytest

from tradingsystem.backtest import market as mk
from tradingsystem.backtest.report import rollovers, summary
from tradingsystem.backtest.runner import RunConfig, answer_due, run_settings
from tradingsystem.core import timeutil
from tradingsystem.core.instruments import InstrumentRegistry
from tradingsystem.core.settings import load_settings
from tradingsystem.core.timeframes import Timeframe
from tradingsystem.core.timeutil import MS_PER_MINUTE, iso, now_ms
from tradingsystem.storage.sqlite_store import SQLiteHotStore
from tradingsystem.storage.tablespec import FORMING, spec_for, table_specs

UTC = dt.timezone.utc
T0 = int(dt.datetime(2026, 9, 23, 6, 0, tzinfo=UTC).timestamp() * 1000)      # a Wednesday, gold open
START = T0 + 7 * MS_PER_MINUTE + 30_000                                         # 06:07:30


def xau():
    s = load_settings(env_path=Path("nope.env"), extra_env={"TS_INSTANCE": "XAUUSD"})
    return s, InstrumentRegistry.from_settings(s).get("mt5:XAUUSD@")


def build_source(root: Path):
    """A hand-built XAUUSD@ hot store: 1m / 15m bars from T0-3h to T0+1h, a tick every 15 s from T0-2h (stand-ins)."""
    s, inst = xau()
    hot = SQLiteHotStore(inst.hot_db_path(root))
    hot.ensure_tables(table_specs(inst))
    for tf in (Timeframe.M1, Timeframe.M15):
        spec = spec_for(inst, "candles", tf)
        rows = []
        for i, ot in enumerate(range(T0 - 3 * 3_600_000, T0 + 3_600_000, tf.ms)):
            p = 4000 + i * 0.1
            rows.append((ot, ot + 3 * 3_600_000, p, p + 1, p - 1, p + 0.5, 10, 20, None))
        hot.upsert(spec, rows)
    spec = spec_for(inst, "ticks")
    rows = []
    for i, t in enumerate(range(T0 - 2 * 3_600_000, T0 + 3_600_000, 15_000)):
        b = 4000 + i * 0.01
        rows.append((t * 1000, t, t + 3 * 3_600_000, b, b + 0.2, None, None, 0))
    hot.upsert(spec, rows)
    hot.close()
    return s, inst


def digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------------------ the clock
def test_the_simulated_clock_is_everyones_now_and_is_removed_again():
    assert abs(now_ms() - timeutil.time.time_ns() // 1_000_000) < 5_000
    timeutil.set_sim_clock(lambda: T0)
    try:
        assert now_ms() == T0
        from tradingsystem.execution import executor
        assert executor.now_ms() == T0                       # every module imported the one function
    finally:
        timeutil.set_sim_clock(None)
    assert now_ms() > T0 + 86_400_000


# ------------------------------------------------------------------------------------------------ the run's market
def test_prepare_keeps_only_what_had_arrived_at_the_start_and_never_writes_the_real_root(tmp_path):
    real, run = tmp_path / "real" / "data", tmp_path / "run" / "data"
    s, inst = build_source(real)
    src = inst.hot_db_path(real)
    before = digest(src)
    mk.prepare(s, real, run, START)
    assert digest(src) == before                                           # the real store untouched
    hot = SQLiteHotStore(inst.hot_db_path(run), readonly=True)
    try:
        m1 = hot.read_range(spec_for(inst, "candles", Timeframe.M1))["open_time"]
        m15 = hot.read_range(spec_for(inst, "candles", Timeframe.M15))["open_time"]
        tk = hot.read_range(spec_for(inst, "ticks"))["time_msc"]
    finally:
        hot.close()
    assert iso(int(m1.max())) == "2026-09-23T06:06:00.000Z"                 # closed 06:07 (+2 s) - 06:07 still open
    assert iso(int(m15.max())) == "2026-09-23T05:45:00.000Z"                # the 06:00 bar closes only at 06:15
    assert int(tk.max()) <= START and int(tk.max()) > START - 15_000
    with pytest.raises(FileExistsError):
        mk.prepare(s, real, run, START)                                    # a run is prepared once
    with pytest.raises(ValueError):
        mk.prepare(s, real, real, START)                                   # never the real root


def test_the_replay_never_writes_a_row_after_the_clock_and_releases_a_bar_at_its_close(tmp_path):
    real, run = tmp_path / "real" / "data", tmp_path / "run" / "data"
    s, inst = build_source(real)
    mk.prepare(s, real, run, START)
    rep = mk.MarketReplay(s, real, run, START)
    m15 = spec_for(inst, "candles", Timeframe.M15)
    try:
        clock, seen_0600 = START, None
        while clock < START + 20 * MS_PER_MINUTE:
            clock += 5_000
            rep.advance(clock)
            hot = rep.dst[inst.key]
            for spec in table_specs(inst):
                if spec.datatype not in mk.REPLAYED:
                    continue
                c = hot.read_range(spec, None, None, [spec.time_col])[spec.time_col]
                if len(c):
                    assert int(c.max()) + mk.lag_of(spec) <= clock, (spec.name, iso(clock))
            if seen_0600 is None and T0 in hot.read_range(m15, T0, T0 + 1)["open_time"].tolist():
                seen_0600 = clock
            f = hot.read_range(FORMING, columns=list(FORMING.column_names))
            assert set(f["tf"].tolist()) == {"5m", "15m"} and set(f["updated_ms"].tolist()) == {clock}
        assert seen_0600 == T0 + 15 * MS_PER_MINUTE + 5_000                 # first step at or after 06:15:02
        fifteen = rep.dst[inst.key].read_range(FORMING, columns=list(FORMING.column_names))
        i = fifteen["tf"].tolist().index("15m")
        assert int(fifteen["open_time"][i]) == T0 + 15 * MS_PER_MINUTE      # the bar in progress at 06:27:30
    finally:
        rep.close()


# ------------------------------------------------------------------------------------------------ the runner
def test_an_answer_reaches_the_executor_when_it_arrived():
    rec = {"timestamp": "2026-09-30T08:50:06.000Z"}
    asked = int(dt.datetime(2026, 9, 30, 8, 50, 6, tzinfo=UTC).timestamp() * 1000)
    real_row = asked + 133_000                                             # the real system: stored at the answer
    assert answer_due(real_row, rec, 114_635) == real_row
    assert answer_due(asked, rec, 114_635) == asked + 114_635              # a backtest run: stored at the call's start
    assert answer_due(asked, {}, 50_000) == asked + 50_000                 # no timestamp: the later (conservative)


def test_a_run_is_paper_only_inside_its_own_root(tmp_path):
    three = Path(__file__).resolve().parents[1] / "fixtures" / "config_three_pairs.yaml"
    rc = RunConfig(start_ms=START, end_ms=START + 3_600_000)
    s = run_settings(three, tmp_path / "run", rc)
    assert s.execution.mode == "paper" and s.execution.trigger == "auto" and s.execution.paper_equity == 107.38
    assert s.paths.data() == (tmp_path / "run" / "data").resolve() or s.paths.data() == tmp_path / "run" / "data"
    assert s.paths.state() == s.paths.data() / "instances" / "XAUUSD"
    assert not s.notify.enabled and not s.pairs["XAUUSD"].news_blackout.enabled
    assert s.ai.daily_calls_per_pair == 60 and s.ai.min_minutes_between_calls == 15


# ------------------------------------------------------------------------------------------------ the report
def test_swap_nights_follow_the_server_midnight_and_wednesday_counts_three():
    def u(d, h, mi=0):
        return int(dt.datetime(2026, 9, d, h, mi, tzinfo=UTC).timestamp() * 1000)
    assert rollovers(u(23, 6), u(23, 20), 2) == 0                          # inside one server day (UTC+3)
    assert rollovers(u(22, 20), u(22, 21, 30), 2) == 1                     # Tuesday 23:00 → 00:30 server: one night
    assert rollovers(u(23, 20), u(23, 21, 30), 2) == 3                     # the Wednesday rollover: triple
    assert rollovers(u(22, 6), u(24, 6), 2) == 4                           # Tue + Wed nights


def test_the_summary_books_swap_and_measures_the_drawdown_on_closed_trades():
    tr = [{"filled": True, "open": False, "close_time": "2026-09-23T07:00:00Z", "pnl_usd": -10.0, "swap_usd": -0.4,
           "r": -1.04, "session": "london"},
          {"filled": True, "open": False, "close_time": "2026-09-23T09:00:00Z", "pnl_usd": 20.0, "swap_usd": 0.0,
           "r": 2.0, "session": "london"}]
    sm = summary(tr, 100.0)
    assert sm["net_usd"] == 9.6 and sm["max_drawdown_pct"] == 10.4 and sm["win_rate"] == 0.5
    assert sm["expectancy_r"] == 0.48 and sm["by_session_r"]["london"]["n"] == 2
