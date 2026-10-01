"""Bring THIS pair folder's data over from the old three-pair checkout (D-051), once, at the switch:

    python tools/import_from_old.py --from C:\\the_claude_new [--dry-run] [--overwrite]

Every pair is its own project folder (``C:\\TradingSystems\\<PAIR>``) and shares no file with another one. This tool
copies what belongs to this folder's one pair (config ``instances:``) from the old checkout, which must be stopped
(``C:\\the_claude_new\\scripts\\stop_all.bat`` and the recorder's STOP file); it never writes there:

* the pair's market data — every instrument of the pair (also gold's ``cross_context`` EURUSD@/XAGUSD@): the hot
  SQLite stores as consistent copies (SQLite backup API, the WAL included) and the cold Parquet archive as NTFS hard
  links (no extra disk space; a day file is only ever replaced by an atomic tmp->rename, never written in place, so the
  two folders can never change each other's files — deleting the old checkout later just frees its links);
* the correlated sibling's candles only (BTC <-> ETH, ``sibling_feeds``): its candle tables and their known gaps, into
  this folder's own file that this system's ingest keeps up to date from now on;
* the pair's state: ``data/instances/<PAIR>/app.db`` (consistent copy), its charts, its KILL_SWITCH if engaged (and the
  global one), the adaptive overlay ``data/adaptive/<PAIR>``;
* ``data/shared``: the AI ledger with this pair's rows and the operator sessions' rows (no other pair's), the account
  high-water mark (+ ``.bak``), the CLI capability file, the monitor/notify state and the proposals log, and the news
  calendar when this pair has a news blackout;
* the operator reviews (``data/reviews``), the monitoring notes, the price recorder's files filtered to this pair's
  symbols (P1.12/P7.1: spot/USD-M book tickers and MT5 ticks; the clock and event streams whole), ``logs/<PAIR>``,
  and ``config/config.local.yaml``. ``.env`` is copied as a file, never read or printed.

Not copied: another pair's anything, the old all-pairs ``data/app.db``, run-time files (``run/``, STOP_ALL, locks),
the old backups and research benches. Refused when a target already exists (``--overwrite`` replaces the pair's
files), when the old checkout or this folder runs a system or a recorder, or when this folder has not exactly one pair.
``--dry-run`` lists what it would do and writes nothing. A summary is written to ``data/IMPORTED_FROM.json``.
Exit codes: 0 done (or dry run), 1 failed, 2 refused.
"""
# No ``from __future__ import annotations``: tools are loaded by file path without a sys.modules entry (tests).
import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradingsystem.core.instruments import Instrument, InstrumentRegistry, sibling_feeds  # noqa: E402
from tradingsystem.core.settings import INSTANCE_ENV, Settings, load_settings  # noqa: E402
from tradingsystem.supervisor import procs  # noqa: E402

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
SHARED_FILES = ("account_peak.json", "account_peak.json.bak", "cli_capabilities.json", "monitor_state.json",
                "notify_state.json", "proposals.jsonl")
RECORDER_PAIR_STREAMS = ("spot_book", "usdm_book", "mt5_ticks")       # filtered by symbol; every other stream whole
SUMMARY = "IMPORTED_FROM.json"


class Refused(RuntimeError):
    pass


def ro_uri(path: Path) -> str:
    return "file:" + quote(Path(path).as_posix(), safe="/:") + "?mode=ro"


def data_root(s: Settings, root: Path) -> Path:
    p = Path(s.paths.data_dir)
    return p if p.is_absolute() else Path(root) / p


def logs_root(s: Settings, root: Path) -> Path:
    p = Path(s.paths.logs_dir)
    return p if p.is_absolute() else Path(root) / p


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else 0


# --------------------------------------------------------------------------------------------- checks
def running_here(root: Path) -> list[tuple[str, int, str | None]]:
    """``(kind, pid, pair)`` of the supervisors and recorders whose working folder or executable lies in ``root``
    (pair None = a recorder or the all-pairs system)."""
    found = []
    for p in psutil.process_iter(["name"]):
        if not (p.info.get("name") or "").lower().startswith("python"):
            continue
        try:
            cmd = p.cmdline()
            kind = ("supervisor" if procs.is_supervisor_cmd(cmd) else
                    "recorder" if any(a.replace("\\", "/").lower().endswith("price_matching/recorder.py")
                                      for a in cmd[1:]) else None)
            if kind and procs.in_project(p, root):
                found.append((kind, p.pid, procs.cmd_instance(cmd) if kind == "supervisor" else None))
        except (psutil.Error, OSError):
            continue
    return found


def describe(found: list[tuple[str, int, str | None]]) -> str:
    return ", ".join(f"{k} pid {pid}" + (f" ({pair})" if pair else "") for k, pid, pair in found)


def the_pair(s: Settings) -> str:
    if len(s.instances) != 1:
        raise Refused(f"this folder must configure exactly one pair in config 'instances:' (it has "
                      f"{sorted(s.instances) or 'none'}) - a pair folder is one system")
    return next(iter(s.instances))


# --------------------------------------------------------------------------------------------- copies
class Importer:
    def __init__(self, s: Settings, old_root: Path, new_root: Path, *, dry_run: bool, overwrite: bool,
                 old_settings: Settings | None = None) -> None:
        self.s, self.dry, self.overwrite = s, dry_run, overwrite
        self.old_root, self.new_root = Path(old_root), Path(new_root)
        self.pair = the_pair(s)
        old_s = old_settings or s
        self.old, self.new = data_root(old_s, self.old_root), data_root(s, self.new_root)
        self.log: list[dict] = []

    def say(self, what: str, src: Path | None, dst: Path, note: str = "") -> None:
        row = {"what": what, "from": str(src) if src else None, "to": str(dst), "note": note}
        self.log.append(row)
        print(f"{'would ' if self.dry else ''}{what:<12} {dst}" + (f"  ({note})" if note else ""))

    def target_free(self, dst: Path) -> None:
        if dst.exists() and not self.overwrite:
            raise Refused(f"{dst} exists already - nothing was imported over it (--overwrite replaces this pair's files)")

    # ---- SQLite
    def copy_db(self, src: Path, dst: Path, what: str) -> None:
        self.target_free(dst)
        self.say(what, src, dst, f"{size_of(src) / 2**20:,.0f} MB, consistent copy")
        if self.dry:
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".import.tmp")
        tmp.unlink(missing_ok=True)
        s_con = sqlite3.connect(ro_uri(src), uri=True, timeout=30)
        d_con = sqlite3.connect(tmp)
        try:
            s_con.backup(d_con)
        finally:
            d_con.close()
            s_con.close()
        for side in ("-wal", "-shm"):
            Path(str(dst) + side).unlink(missing_ok=True)
        os.replace(tmp, dst)

    def copy_candles(self, feed: Instrument) -> None:
        """The sibling's candle tables (and their known gaps) into this folder's own file for that instrument."""
        src, dst = feed.hot_db_path(self.old), feed.hot_db_path(self.new)
        if not src.exists():
            self.say("skip", src, dst, "the sibling has no hot store in the old checkout - the live ingest fills it")
            return
        self.target_free(dst)
        tables = [feed.table("candles", tf) for tf in feed.timeframes]
        self.say("candles", src, dst, f"sibling {feed.pair}: {', '.join(tables)}")
        if self.dry:
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".import.tmp")
        tmp.unlink(missing_ok=True)
        con = sqlite3.connect("file:" + quote(tmp.as_posix(), safe="/:"), uri=True)   # URI mode: ATTACH reads ?mode=ro
        try:
            con.execute("ATTACH DATABASE ? AS old", (ro_uri(src),))
            have = {r[0]: r[1] for r in con.execute("SELECT name, sql FROM old.sqlite_master WHERE type='table'")}
            for name in [t for t in tables if t in have] + [n for n in ("known_gaps",) if n in have]:
                con.execute(have[name])
                rows = (f"SELECT * FROM old.{name} WHERE table_name IN ({','.join('?' * len(tables))})", tables) \
                    if name == "known_gaps" else (f"SELECT * FROM old.{name}", ())
                con.execute(f"INSERT INTO main.{name} {rows[0]}", rows[1])
            for name, sql in con.execute("SELECT name, sql FROM old.sqlite_master WHERE type='index' AND sql IS NOT NULL"
                                         ).fetchall():
                if any(name.startswith(t) for t in tables):
                    con.execute(sql)
            con.commit()
            con.execute("DETACH DATABASE old")
        finally:
            con.close()
        for side in ("-wal", "-shm"):
            Path(str(dst) + side).unlink(missing_ok=True)
        os.replace(tmp, dst)

    def ledger(self) -> None:
        src, dst = self.old / "shared" / "ai_usage.db", self.new / "shared" / "ai_usage.db"
        if not src.exists():
            return
        self.target_free(dst)
        self.say("ai ledger", src, dst, f"rows of {self.pair} and of the operator sessions only")
        if self.dry:
            return
        self.copy_db_quiet(src, dst)
        con = sqlite3.connect(dst)
        try:
            con.execute("DELETE FROM ai_usage WHERE pair IS NOT NULL AND pair != ?", (self.pair,))
            con.commit()
            con.execute("VACUUM")
        finally:
            con.close()

    def copy_db_quiet(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        s_con = sqlite3.connect(ro_uri(src), uri=True, timeout=30)
        d_con = sqlite3.connect(dst)
        try:
            s_con.backup(d_con)
        finally:
            d_con.close()
            s_con.close()

    # ---- files
    def copy_file(self, src: Path, dst: Path, what: str) -> None:
        if not src.exists():
            return
        self.target_free(dst)
        self.say(what, src, dst)
        if not self.dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    def copy_tree(self, src: Path, dst: Path, what: str) -> None:
        if not src.is_dir():
            return
        files = [f for f in src.rglob("*") if f.is_file() and not f.name.endswith((".lock", ".tmp"))]
        self.say(what, src, dst, f"{len(files)} files, {sum(f.stat().st_size for f in files) / 2**20:,.1f} MB")
        if self.dry:
            return
        for f in files:
            out = dst / f.relative_to(src)
            if out.exists() and not self.overwrite:
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)

    def link_tree(self, src: Path, dst: Path) -> None:
        """The cold archive of one instrument as hard links (same volume) - no second copy on the disk."""
        if not src.is_dir():
            return
        files = [f for f in src.rglob("*") if f.is_file() and not f.name.endswith((".tmp", ".lock"))]
        self.say("cold links", src, dst, f"{len(files)} files, {sum(f.stat().st_size for f in files) / 2**30:,.2f} GB "
                                         "as hard links (no extra space)")
        if self.dry:
            return
        for f in files:
            out = dst / f.relative_to(src)
            if out.exists():
                if not self.overwrite:
                    continue
                out.unlink()
            out.parent.mkdir(parents=True, exist_ok=True)
            os.link(f, out)

    def recorder_files(self, symbols: dict[str, list[str]]) -> None:
        src, dst = self.old / "research" / "price_matching", self.new / "research" / "price_matching"
        if not src.is_dir():
            return
        files = sorted(f for f in src.rglob("*.parquet"))
        self.say("recorder", src, dst, f"{len(files)} files filtered to {sorted(sum(symbols.values(), []))}")
        if self.dry:
            return
        import pyarrow as pa
        import pyarrow.compute as pc
        import pyarrow.parquet as pq
        for f in files:
            rel = f.relative_to(src)
            out = dst / rel
            if out.exists() and not self.overwrite:
                continue
            stream = rel.parts[0]
            out.parent.mkdir(parents=True, exist_ok=True)
            if stream not in RECORDER_PAIR_STREAMS:
                shutil.copy2(f, out)
                continue
            try:
                table = pq.read_table(f)
            except Exception as exc:  # noqa: BLE001 - a file the old recorder is still writing: skipped
                print(f"  skipped {rel} ({type(exc).__name__})")
                continue
            keep = [x.upper() for x in symbols[stream]]         # book tickers say BTCUSDT, MT5 ticks BTCUSD@
            if not keep or "symbol" not in table.column_names:
                continue
            mask = pc.is_in(pc.utf8_upper(table["symbol"]), value_set=pa.array(keep))
            part = table.filter(mask)
            if part.num_rows:
                pq.write_table(part, out, compression="zstd")

    # ---- the whole import
    def run(self, recorded: dict[str, list[str]] | None = None) -> None:
        if self.old_root.resolve() == self.new_root.resolve():
            raise Refused("--from is this folder itself")
        if os.path.splitdrive(self.old.resolve())[0].lower() != os.path.splitdrive(self.new.resolve())[0].lower():
            raise Refused("the old checkout is on another drive: the cold archive is linked, not copied (no room for "
                          "a second copy) - both folders must be on the same drive")
        if not (self.old / "instances" / self.pair).is_dir():
            raise Refused(f"{self.old / 'instances' / self.pair} not found - is --from the old three-pair checkout?")
        reg = InstrumentRegistry.from_settings(self.s)
        # 1. market data of the pair's own instruments
        for inst in reg.for_pair(self.pair):
            src = inst.hot_db_path(self.old)
            if src.exists():
                self.copy_db(src, inst.hot_db_path(self.new), "hot store")
            self.link_tree(self.old / "cold" / inst.venue / inst.file_stem, self.new / "cold" / inst.venue / inst.file_stem)
        # 2. the sibling's candles only
        for feed in sibling_feeds(self.s):
            self.copy_candles(feed)
        # 3. the pair's state
        st_old, st_new = self.old / "instances" / self.pair, self.new / "instances" / self.pair
        self.copy_db(st_old / "app.db", st_new / "app.db", "state")
        self.copy_tree(st_old / "charts", st_new / "charts", "charts")
        self.copy_file(st_old / "KILL_SWITCH", st_new / "KILL_SWITCH", "KILL SWITCH")
        self.copy_file(self.old / "KILL_SWITCH", self.new / "KILL_SWITCH", "KILL SWITCH")
        self.copy_tree(self.old / "adaptive" / self.pair, self.new / "adaptive" / self.pair, "adaptive")
        # 4. shared state that becomes this folder's own
        self.ledger()
        for name in SHARED_FILES:
            self.copy_file(self.old / "shared" / name, self.new / "shared" / name, "shared")
        if self.s.pairs[self.pair].news_blackout.enabled:
            self.copy_file(self.old / "shared" / "news_calendar.json", self.new / "shared" / "news_calendar.json",
                           "shared")
        # 5. history: reviews, monitoring notes, recorder files, logs, local config, .env
        self.copy_tree(self.old / "reviews", self.new / "reviews", "reviews")
        self.copy_tree(self.old / "monitoring", self.new / "monitoring", "monitoring")
        if recorded is not None:
            self.recorder_files(recorded)
        self.copy_tree(logs_root(self.s, self.old_root) / self.pair, logs_root(self.s, self.new_root) / self.pair, "logs")
        self.copy_file(self.old_root / "config" / "config.local.yaml", self.new_root / "config" / "config.local.yaml",
                       "local config")
        env_old, env_new = self.old_root / ".env", self.new_root / ".env"
        if env_old.exists():
            self.target_free(env_new)
            self.say(".env", env_old, env_new, "copied as a file, not read")
            if not self.dry:
                shutil.copy2(env_old, env_new)
        if not self.dry:
            (self.new / SUMMARY).write_text(json.dumps({
                "from": str(self.old_root), "pair": self.pair,
                "at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "steps": self.log},
                indent=1), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="import_from_old.py", description=__doc__.splitlines()[0])
    ap.add_argument("--from", dest="old", required=True, help=r"the old three-pair checkout, e.g. C:\the_claude_new")
    ap.add_argument("--dry-run", action="store_true", help="list what would be copied, write nothing")
    ap.add_argument("--overwrite", action="store_true", help="replace this pair's files that exist already")
    ap.add_argument("--preflight", action="store_true",
                    help="only check that this folder runs nothing yet (scripts\\first_start.bat asks before it stops "
                         "this pair's old system): exit 0 = go on, 2 = refused")
    a = ap.parse_args(argv)
    t0 = time.monotonic()
    try:
        s = load_settings()
        old_root = Path(a.old).resolve()
        if not old_root.is_dir():
            raise Refused(f"{old_root} is not a folder")
        pair = the_pair(s)
        busy = running_here(ROOT)
        if busy:
            raise Refused(f"this folder already runs ({describe(busy)}) - its data is imported only once, before its "
                          "first start (stop it with scripts\\stop_all.bat to import again with --overwrite)")
        if a.preflight:
            print(f"preflight ok: this folder ({pair}) runs nothing")
            return EXIT_OK
        old = running_here(old_root)
        mine = [x for x in old if x[0] == "supervisor" and x[2] in (pair, None)]
        if mine and a.dry_run:
            print(f"note: the old checkout still runs {pair} ({describe(mine)}) - the import itself refuses until it "
                  "is stopped")
        elif mine:
            raise Refused(f"the old checkout still runs {pair} ({describe(mine)}): stop that system first - "
                          rf"{old_root}\.venv\Scripts\python.exe -m tradingsystem run --stop --instance {pair} "
                          r"(scripts\first_start.bat does it)")
        if any(x[0] == "recorder" for x in old):
            print(rf"note: the old price recorder still runs (it stops at its next flush once "
                  rf"{old_root}\data\research\price_matching\STOP exists) - a file it is writing is skipped")
        os.environ.pop(INSTANCE_ENV, None)
        import importlib.util
        spec = importlib.util.spec_from_file_location("ts_recorder", ROOT / "research" / "price_matching" / "recorder.py")
        rec = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rec)
        imp = Importer(s, old_root, ROOT, dry_run=a.dry_run, overwrite=a.overwrite)
        print(f"{'DRY RUN - ' if a.dry_run else ''}importing {imp.pair} from {old_root} into {ROOT}")
        imp.run(rec.recorded_symbols(s))
    except Refused as exc:
        print(f"refused: {exc}")
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return EXIT_FAILED
    print(f"{'dry run done' if a.dry_run else 'done'} in {time.monotonic() - t0:.0f} s"
          + ("" if a.dry_run else f" - summary in {imp.new / SUMMARY}"))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
