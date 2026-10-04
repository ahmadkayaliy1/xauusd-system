"""P12.7 backtest (docs/plans/gold_live_and_backtest.md B2): the run's own data root and the market replay.

A run lives in ``<data>/backtests/<run>/``: ``data/`` (``hot/``, ``cold/``, ``instances/``, ``shared/``), ``logs/``,
``config.yaml``, the answers and the report. The real data root is only ever READ (SQLite ``mode=ro``, the backup API,
hard links of finished Parquet day files) — a backtest never writes to it.

* :func:`prepare` builds the run's market as it was just BEFORE ``start``: every instrument's hot store is copied (the
  SQLite backup API from a read-only connection — a consistent snapshot while the live system writes), every row at
  or after ``start`` deleted, the forming table emptied; the cold-archive day files of the days before ``start``'s UTC
  day are hard-linked (no extra space), and the rows of ``start``'s day before ``start`` are put into the hot store
  (the reader takes the cold archive up to the first hot row, so the two never overlap wrongly).
* :class:`MarketReplay` then writes, as the simulated clock advances, every row that had reached the live system by
  then: a tick at its time, a closed candle at its close + ``BAR_DELAY_MS`` (the live MT5 poller stores a closed bar
  1-3 s after its close, P1.8), a Binance derivatives row at its own time (the snapshot applies the publication lags
  itself), and the forming 5m / 15m bar of the analysis instrument (from the closed 1m bars and the ticks since; MT5
  bars are bid bars). Nothing after the clock is ever in the run's stores, so the system cannot see the future.

Only real recorded market data is replayed — nothing is synthesised (spec: no mock data on a decision path).
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..core.instruments import Instrument, InstrumentRegistry
from ..core.settings import Settings
from ..core.timeframes import Timeframe
from ..core.timeutil import MS_PER_DAY, MS_PER_MINUTE
from ..storage.parquet_store import ParquetColdStore
from ..storage.reader import InstrumentReader
from ..storage.sqlite_store import SQLiteHotStore
from ..storage.tablespec import FORMING, TableSpec, spec_for, table_specs

log = logging.getLogger("backtest")

BAR_DELAY_MS = 2_000                      # a closed bar reaches the live hot store this long after its close
REPLAYED = ("ticks", "candles", "funding", "open_interest", "metrics", "mark_price")   # what the system reads
FORMING_TFS = (Timeframe.M5, Timeframe.M15)                                         # snapshot.FORMING_TFS
LOAD_MS = 6 * 3_600_000                   # source rows are loaded in windows of this length (one read per window)


def day_start(ms: int) -> int:
    return ms - ms % MS_PER_DAY


def utc_ms(day) -> int:
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp() * 1000)


def lag_of(spec: TableSpec) -> int:
    """How long after its row time a row reached the live hot store: a candle at its close + BAR_DELAY_MS, the rest
    at their own time."""
    return spec.timeframe.ms + BAR_DELAY_MS if spec.datatype == "candles" and spec.timeframe else 0


# ------------------------------------------------------------------------------------------------ the run's root
def prepare(s: Settings, real_data: Path, run_data: Path, start_ms: int) -> dict:
    """Build ``run_data`` as the market stood just before ``start_ms``. Returns a summary (rows kept per table,
    cold files linked). Refuses an existing run root (a run is never prepared twice)."""
    real_data, run_data = Path(real_data).resolve(), Path(run_data).resolve()
    if run_data == real_data or run_data in real_data.parents:
        raise ValueError("the run's data root must not be the real one (or contain it)")
    if (run_data / "hot").exists():
        raise FileExistsError(f"{run_data} is prepared already")
    reg = InstrumentRegistry.from_settings(s)
    out: dict = {"hot": {}, "cold_linked": 0}
    d0 = day_start(start_ms)
    for inst in reg.all():
        src = inst.hot_db_path(real_data)
        if not src.exists():
            continue
        dst = inst.hot_db_path(run_data)
        dst.parent.mkdir(parents=True, exist_ok=True)
        ro = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
        rw = sqlite3.connect(str(dst))
        try:
            ro.backup(rw)
        finally:
            ro.close()
        try:
            names = {r[0] for r in rw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for spec in table_specs(inst):          # keep only what had reached the live system at start
                if spec.name in names:
                    rw.execute(f"DELETE FROM {spec.name} WHERE {spec.time_col} > ?", (start_ms - lag_of(spec),))
            if FORMING.name in names:
                rw.execute(f"DELETE FROM {FORMING.name}")
            rw.commit()
            rw.execute("PRAGMA journal_mode=WAL")
            out["hot"][inst.key] = {spec.name: rw.execute(f"SELECT count(*) FROM {spec.name}").fetchone()[0]
                                    for spec in table_specs(inst) if spec.name in names}
        finally:
            rw.close()
        # the cold archive: finished day files before start's day, hard-linked; start's day before start → hot rows
        cold_src, cold_dst = ParquetColdStore(real_data / "cold"), ParquetColdStore(run_data / "cold")
        rd = InstrumentReader(inst, real_data)
        hot = SQLiteHotStore(dst)
        try:
            for spec in table_specs(inst):
                days = cold_src.days(inst, spec)
                if not days:
                    continue
                for day in days:
                    if utc_ms(day) >= d0:
                        continue
                    a, b = cold_src.day_path(inst, spec, day), cold_dst.day_path(inst, spec, day)
                    b.parent.mkdir(parents=True, exist_ok=True)
                    _link(a, b)
                    out["cold_linked"] += 1
                if spec.datatype in REPLAYED:
                    c = rd.read_range(spec, d0, start_ms + 1, list(spec.column_names))
                    keep = c[spec.time_col].astype(np.int64) + lag_of(spec) <= start_ms
                    c = {k: v[keep] for k, v in c.items()}
                    rows = _rows(c, spec)
                    if rows:
                        hot.ensure_tables([spec])
                        hot.upsert(spec, rows)
        finally:
            hot.close()
            rd.close()
    for sub in ("instances", "shared"):
        (run_data / sub).mkdir(parents=True, exist_ok=True)
    return out


def _link(a: Path, b: Path) -> None:
    """A hard link (same volume: no extra space); a copy where linking is not possible."""
    try:
        os.link(a, b)
    except OSError:
        shutil.copy2(a, b)


def _rows(cols: dict[str, np.ndarray], spec: TableSpec, sel=None) -> list[tuple]:
    names = spec.column_names
    if not len(cols[names[0]]):
        return []
    arrays = [cols[n] if sel is None else cols[n][sel] for n in names]
    return list(zip(*(a.tolist() for a in arrays)))


# ------------------------------------------------------------------------------------------------ the replay
@dataclass
class _Feed:
    inst: Instrument
    spec: TableSpec
    lag: int                                  # release = row time + lag
    cols: dict[str, np.ndarray] = field(default_factory=dict)
    release: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.int64))
    loaded_to: int = 0                        # rows released before this are loaded (window end)


class MarketReplay:
    """Writes the real market into the run's hot stores as the simulated clock advances (:meth:`advance`)."""

    def __init__(self, s: Settings, real_data: Path, run_data: Path, start_ms: int) -> None:
        self.reg = InstrumentRegistry.from_settings(s)
        self.real_data, self.run_data = Path(real_data), Path(run_data)
        self.start = self.cursor = start_ms
        self.src: dict[str, InstrumentReader] = {}
        self.dst: dict[str, SQLiteHotStore] = {}
        self.feeds: list[_Feed] = []
        for inst in self.reg.all():
            if not inst.hot_db_path(self.real_data).exists():
                continue
            self.src[inst.key] = InstrumentReader(inst, self.real_data)
            self.dst[inst.key] = SQLiteHotStore(inst.hot_db_path(self.run_data))
            for spec in table_specs(inst):
                if spec.datatype not in REPLAYED:
                    continue
                self.dst[inst.key].ensure_tables([spec])
                self.feeds.append(_Feed(inst, spec, lag_of(spec), loaded_to=start_ms))
        self.primary = {p: self.reg.primary(p) for p in s.enabled_pairs()}
        for inst in self.primary.values():
            if inst.key in self.dst:
                self.dst[inst.key].ensure_tables([FORMING])
        self.written = 0

    def _load(self, f: _Feed, upto: int) -> None:
        """Make sure every row released up to ``upto`` is loaded: windows [a, b) of ``LOAD_MS`` in release time, read
        once each from the real store; a row released at or before the run's start is the prepared store's."""
        while f.loaded_to <= upto:
            a, b = f.loaded_to, f.loaded_to + LOAD_MS
            c = self.src[f.inst.key].read_range(f.spec, a - f.lag, b - f.lag + 1, list(f.spec.column_names))
            t = c[f.spec.time_col].astype(np.int64) + f.lag
            keep = (t >= a) & (t < b) & (t > self.start)
            if keep.any():
                order = np.argsort(t[keep], kind="stable")
                new = {k: v[keep][order] for k, v in c.items()}
                if f.cols:
                    f.cols = {k: np.concatenate([f.cols[k], new[k]]) for k in new}
                    f.release = np.concatenate([f.release, t[keep][order]])
                else:
                    f.cols, f.release = new, t[keep][order]
            f.loaded_to = b

    def advance(self, to_ms: int) -> int:
        """Release every row known in (cursor, to_ms] into the run's stores and refresh the forming bars. Returns the
        number of rows written. The clock never goes back."""
        if to_ms <= self.cursor:
            return 0
        n = 0
        for f in self.feeds:
            self._load(f, to_ms)
            if not f.release.size:
                continue
            lo = int(np.searchsorted(f.release, self.cursor, side="right"))
            hi = int(np.searchsorted(f.release, to_ms, side="right"))
            if hi > lo:
                rows = _rows(f.cols, f.spec, slice(lo, hi))
                self.dst[f.inst.key].upsert(f.spec, rows)
                n += len(rows)
            if lo > 50_000:                               # drop what is behind the clock (memory)
                f.cols = {k: v[lo:] for k, v in f.cols.items()}
                f.release = f.release[lo:]
        self.cursor = to_ms
        self._forming(to_ms)
        self.written += n
        return n

    def _forming(self, now: int) -> None:
        """The forming 5m / 15m bar of each analysis instrument at ``now`` (from the run's own stores: what had
        arrived by then), written like the live MT5 ingest (``forming_candles``, ``updated_ms`` = now)."""
        for inst in self.primary.values():
            if inst.venue != "mt5" or inst.key not in self.dst:
                continue
            hot = self.dst[inst.key]
            m1 = spec_for(inst, "candles", Timeframe.M1)
            ticks = spec_for(inst, "ticks") if "ticks" in inst.datatypes else None
            rows = []
            for tf in FORMING_TFS:
                op = tf.floor(now)
                bars = hot.read_range(m1, op, now, ["open_time", "open", "high", "low", "close", "tick_volume"])
                closed = bars["open_time"] + MS_PER_MINUTE <= now
                o = h = lo = c = None
                vol = 0.0
                if closed.any():
                    o = float(bars["open"][closed][0])
                    h, lo = float(bars["high"][closed].max()), float(bars["low"][closed].min())
                    c = float(bars["close"][closed][-1])
                    vol = float(bars["tick_volume"][closed].sum())
                    since = int(bars["open_time"][closed][-1]) + MS_PER_MINUTE
                else:
                    since = op
                if ticks is not None:
                    tk = hot.read_range(ticks, since, now + 1, ["time_msc", "bid"])
                    if len(tk["bid"]):
                        b = tk["bid"].astype(float)
                        o = float(b[0]) if o is None else o
                        h = float(b.max()) if h is None else max(h, float(b.max()))
                        lo = float(b.min()) if lo is None else min(lo, float(b.min()))
                        c = float(b[-1])
                        vol += float(len(b))
                if o is not None:
                    rows.append((tf.value, op, o, h, lo, c, vol, now))
            if rows:
                hot.replace(FORMING, rows)

    def close(self) -> None:
        for r in self.src.values():
            r.close()
        for h in self.dst.values():
            h.close()

    def max_written_time(self) -> dict[str, int | None]:
        """The newest row time per replayed table of the run (the causality check: never after the clock)."""
        out = {}
        for f in self.feeds:
            out[f"{f.inst.key}:{f.spec.name}"] = self.dst[f.inst.key].last_time(f.spec)
        return out
