"""D-051: every pair is its own project folder (``C:\\TradingSystems\\<PAIR>``) and shares nothing with another one —
code, venv, data, logs, Task Scheduler tasks, locks, the CLI work folder, the AI ledger and the account peak are each
folder's own. What the split may change and nothing more: a system sees only its own trades (the owner's choice), BTC
and ETH each record the other's candles themselves (their ``market.cross`` sibling block stays), gold stays apart from
the crypto pairs, and the folder's tasks live in its own Task Scheduler folder with spaced review times."""
from __future__ import annotations

import importlib.util
import re
import types
from pathlib import Path

import pytest

from tradingsystem import cli
from tradingsystem.analysis import cross
from tradingsystem.core import settings as settings_mod
from tradingsystem.core.instruments import InstrumentRegistry, sibling_feeds
from tradingsystem.core.settings import INSTANCE_ENV, SupervisorCfg, cli_workdir, load_settings
from tradingsystem.execution.executor import magics
from tradingsystem.supervisor import procs

ROOT = Path(__file__).resolve().parents[2]
THREE = ROOT / "tests" / "fixtures" / "config_three_pairs.yaml"
FOLDER = ROOT / "config" / "config.yaml"
NO_LOCAL = ROOT / "tests" / "fixtures" / "no_config.local.yaml"
NO_ENV = ROOT / "tests" / "fixtures" / "no.env"
SIBLING = {"BTCUSDT": "ETHUSDT", "ETHUSDT": "BTCUSDT", "XAUUSD": None}


def three(inst: str | None = None):
    return load_settings(THREE, local_path=NO_LOCAL, env_path=NO_ENV, extra_env={INSTANCE_ENV: inst or ""})


def folder_like(pair: str):
    """What a pair folder's config gives: its own instance, its pair, its sibling (disabled), the groups among them."""
    s = three(pair)
    keep = {pair, SIBLING[pair]} - {None}
    pairs = {k: v.model_copy(update={"enabled": k == pair}) for k, v in s.pairs.items() if k in keep}
    groups = [g for g in s.risk.correlated_groups if set(g) <= keep]
    return s.model_copy(update={"pairs": pairs, "instances": {pair: s.instances[pair]},
                                "risk": s.risk.model_copy(update={"correlated_groups": groups})})


# ------------------------------------------------------------------------------------------- sibling candle feed
def test_a_sibling_feed_exists_only_when_no_system_of_this_folder_records_the_sibling():
    assert sibling_feeds(three("BTCUSDT")) == []          # three pairs in one folder: ETH's own system records it
    assert sibling_feeds(three()) == []                   # the all-pairs system records every pair itself
    assert sibling_feeds(folder_like("XAUUSD")) == []     # gold has no sibling
    for pair, other in (("BTCUSDT", "ETHUSDT"), ("ETHUSDT", "BTCUSDT")):
        s = folder_like(pair)
        (feed,) = sibling_feeds(s)
        primary = cross.sibling_instrument(s, other)
        assert feed.pair == other and feed.key == primary.key == f"binance_spot:{other}"
        assert feed.datatypes == ("candles",) and feed.timeframes == primary.timeframes
        assert feed.start_specs == {"candles": primary.start_specs["candles"]}
        assert feed.hot_db_path(Path("d")) == primary.hot_db_path(Path("d"))    # the file market.cross reads
        assert feed.key not in {i.key for i in InstrumentRegistry.from_settings(s).all()}   # never traded here
        assert set(s.enabled_pairs()) == {pair}


def test_the_binance_ingest_records_the_sibling_feed_next_to_the_pairs_own_instruments(monkeypatch, tmp_path):
    from tradingsystem.ingest import main as ingest_main
    from tradingsystem.ingest.binance import service

    s = folder_like("BTCUSDT")
    s = s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(tmp_path / "data"),
                                                                  "logs_dir": str(tmp_path / "logs")})})
    seen = []

    class Svc:
        def __init__(self, settings, registry, appdb):
            seen.append(sorted(i.key for i in registry.all()))
            self.stop = types.SimpleNamespace(set=lambda: None)

        async def run(self):
            return None

    monkeypatch.setattr(service, "BinanceLiveService", Svc)
    monkeypatch.setattr(ingest_main, "_settings", lambda data_dir: s)
    ingest_main.run_binance(backfill=False)
    assert seen == [["binance_spot:BTCUSDT", "binance_spot:ETHUSDT", "binance_usdm:BTCUSDT", "mt5:BTCUSD@"]]


# ------------------------------------------------------------------------------------------- own trades only
def test_a_pair_folder_counts_only_its_own_magic_family():
    """The owner's choice: each system sees only its own trades - no other pair's magic is in its family any more, so
    no other system's position counts towards its correlated-exposure cap."""
    for pair, offset in (("BTCUSDT", 1), ("ETHUSDT", 2), ("XAUUSD", 3)):
        own, base, family = magics(folder_like(pair))
        assert own == base + offset and family == {base, own}
        assert magics(three(pair))[0] == own                  # the same magic as before the split: its open trades


# ------------------------------------------------------------------------------------------- the price recorder
def test_the_recorder_records_only_this_folders_pairs():
    spec = importlib.util.spec_from_file_location("ts_recorder_split", ROOT / "research" / "price_matching" / "recorder.py")
    rec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rec)
    assert rec.recorded_symbols(three()) == {"spot_book": ["btcusdt", "ethusdt"],              # the P1.12 set
                                             "usdm_book": ["btcusdt", "ethusdt", "xauusdt"],
                                             "mt5_ticks": ["BTCUSD@", "ETHUSD@", "XAUUSD@"]}
    assert rec.recorded_symbols(folder_like("BTCUSDT")) == {"spot_book": ["btcusdt"], "usdm_book": ["btcusdt"],
                                                            "mt5_ticks": ["BTCUSD@"]}
    assert rec.recorded_symbols(folder_like("XAUUSD")) == {"spot_book": [], "usdm_book": ["xauusdt"],
                                                           "mt5_ticks": ["XAUUSD@"]}
    assert rec.stream_url(rec.USDM_WS, ["btcusdt", "xauusdt"]) == \
        "wss://fstream.binance.com/stream?streams=btcusdt@bookTicker/xauusdt@bookTicker"


# ------------------------------------------------------------------------------------------- processes and files
class Proc:
    def __init__(self, cwd=None, exe=None):
        self._cwd, self._exe = cwd, exe

    def cwd(self):
        if self._cwd is None:
            raise procs.psutil.AccessDenied(1)
        return self._cwd

    def exe(self):
        if self._exe is None:
            raise procs.psutil.AccessDenied(1)
        return self._exe


def test_a_process_belongs_to_the_project_folder_its_working_folder_or_executable_lies_in(tmp_path):
    root = tmp_path / "BTCUSDT"
    assert procs.in_project(Proc(cwd=str(root)), root)
    assert procs.in_project(Proc(cwd=str(root / "scripts")), root)
    assert procs.in_project(Proc(exe=str(root / ".venv" / "Scripts" / "python.exe")), root)
    assert not procs.in_project(Proc(cwd=str(tmp_path / "ETHUSDT")), root)
    assert not procs.in_project(Proc(cwd=str(tmp_path / "BTCUSDT_wt")), root)              # a sibling path
    assert not procs.in_project(Proc(exe=r"C:\Program Files\Python312\python.exe"), root)
    assert not procs.in_project(Proc(), root)                                               # unreadable: not ours


def test_another_folders_supervisor_is_never_found(monkeypatch, tmp_path):
    cmd = ["python.exe", "-m", "tradingsystem", "run", "all", "--instance", "BTCUSDT"]

    class P(Proc):
        def __init__(self, pid, cwd):
            super().__init__(cwd=cwd)
            self.pid, self.info = pid, {"name": "python.exe"}

        def cmdline(self):
            return cmd

        def ppid(self):
            return 1

        def create_time(self):
            return 0.0

        def environ(self):
            return {}

    fakes = [P(10, str(tmp_path / "old_checkout")), P(20, str(procs.PROJECT_ROOT))]
    monkeypatch.setattr(procs.psutil, "process_iter", lambda attrs=None: iter(fakes))
    assert procs.running_supervisors() == {20: "BTCUSDT"}
    assert procs.other_supervisors(instance="BTCUSDT", scope="exact") == [20]       # what stop.bat may kill


def test_the_cli_work_folder_is_this_folders_own_and_outside_it(monkeypatch, tmp_path):
    here = cli_workdir()
    assert settings_mod.PROJECT_ROOT not in here.parents and here.name.startswith("tradingsystem-claude-code-")
    monkeypatch.setattr(settings_mod, "PROJECT_ROOT", tmp_path / "ETHUSDT")
    other = cli_workdir()
    assert other != here and "ETHUSDT" in other.name


# ------------------------------------------------------------------------------------------- Task Scheduler
def test_the_task_folder_setting():
    assert SupervisorCfg().task_folder == "\\" and SupervisorCfg().mt5_task_path() == "TradingSystem-MT5"
    cfg = SupervisorCfg(task_folder="\\TradingSystems\\BTCUSDT\\")
    assert cfg.mt5_task_path() == "\\TradingSystems\\BTCUSDT\\TradingSystem-MT5"
    assert SupervisorCfg(task_folder="\\X\\", mt5_task="\\Y\\T").mt5_task_path() == "\\Y\\T"
    for bad in ("TradingSystems\\BTCUSDT\\", "\\TradingSystems\\BTCUSDT", "\\a\\\\b\\", "/a/"):
        with pytest.raises(ValueError):
            SupervisorCfg(task_folder=bad)


def test_config_tasks_prints_the_folder_and_the_times(capsys):
    assert cli.main(["config", "--tasks", "--config", str(THREE)]) == 0
    lines = dict(ln.split("=", 1) for ln in capsys.readouterr().out.splitlines())
    assert lines == {"task_folder": "\\", "mt5_task": "TradingSystem-MT5", "daily_utc": "04:30", "weekly_utc": "06:00",
                     "weekly_day": "Sunday", "backup_utc": "03:30"}


TASK_SCRIPTS = ("install_autostart.ps1", "uninstall_autostart.ps1", "install_operator_tasks.ps1", "check_ops.ps1")


@pytest.mark.parametrize("name", TASK_SCRIPTS)
def test_every_task_script_works_only_inside_this_systems_task_folder(name):
    raw = (ROOT / "scripts" / name).read_bytes()
    assert raw.isascii() and b"\n" not in raw.replace(b"\r\n", b"")        # Windows PowerShell 5.1: ASCII + CRLF
    text = raw.decode("ascii")
    assert "config --tasks" in text and "function Split-TaskLine" in text
    for m in re.finditer(r"schtasks /(Query|Delete|Run) /TN (\S+)", text):
        assert m.group(2).startswith('"$taskFolder'), m.group(0)
    registers = [ln for ln in text.splitlines() if ln.lstrip().startswith("Register-ScheduledTask -TaskName")]
    assert all("-TaskPath $taskFolder" in ln for ln in registers), registers
    assert bool(registers) == name.startswith("install_")
    listings = text.count("schtasks /Query /FO CSV")
    loops = text.split("foreach ($line in (schtasks /Query /FO CSV /NH 2>$null)) {")[1:]
    assert len(loops) == listings - text.count('"schtasks /Query /FO CSV /NH": its first field')     # minus the comment
    assert all("Split-TaskLine $line" in s[:120] and "-ne $taskFolder" in s[:220] for s in loops)


# ------------------------------------------------------------------------------------------- this folder's config
def test_this_folders_config_is_one_pair_and_otherwise_the_three_pair_config_unchanged():
    """In a pair folder (one instance): its pair, its sibling (disabled) and nothing else; every setting outside the
    D-051 ones equals the three-pair config's for that pair. The three-pair checkout itself is the fixture."""
    probe = load_settings(FOLDER, local_path=NO_LOCAL, env_path=NO_ENV, extra_env={INSTANCE_ENV: ""})
    if len(probe.instances) != 1:
        assert FOLDER.read_text(encoding="utf-8") in THREE.read_text(encoding="utf-8")   # the fixture = this config
        pytest.skip("the three-pair checkout (not a pair folder)")
    (pair,) = probe.instances
    sib = SIBLING[pair]
    new = load_settings(FOLDER, local_path=NO_LOCAL, env_path=NO_ENV, extra_env={INSTANCE_ENV: pair})
    old = three(pair)
    assert set(new.pairs) == {pair, sib} - {None} and set(new.enabled_pairs()) == {pair}
    assert not probe.pairs[sib].enabled if sib else True
    assert new.supervisor.task_folder == f"\\TradingSystems\\{pair}\\"
    assert new.execution.magic == old.execution.magic and new.api.port == old.api.port
    assert new.binance.rest_weight_budget_per_min == old.binance.rest_weight_budget_per_min == 1000
    assert [i.key for i in InstrumentRegistry.from_settings(new).all()] == \
        [i.key for i in InstrumentRegistry.from_settings(old).all()]
    assert [f.key for f in sibling_feeds(new)] == ([f"binance_spot:{sib}"] if sib else [])
    assert new.risk.correlated_groups == (old.risk.correlated_groups if sib else [])

    def view(s):
        d = s.model_dump(mode="json")
        for k in ("config_hash", "instances", "supervisor", "operator", "backup"):
            d.pop(k)
        d["risk"].pop("correlated_groups")
        d["execution"].pop("venue_by_pair")
        d["pairs"] = d["pairs"][pair]
        return d

    assert view(new) == view(old)
    times = {"XAUUSD": ("04:30", "06:00", "03:30"), "BTCUSDT": ("04:55", "06:45", "03:40"),
             "ETHUSDT": ("05:20", "07:30", "03:50")}[pair]
    assert (new.operator.daily_utc, new.operator.weekly_utc, new.backup.daily_utc) == times


# ------------------------------------------------------------------------------------------- the one-time import
def _db(path: Path, *stmts: str) -> None:
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    for st in stmts:
        con.execute(st)
    con.commit()
    con.close()


def _rows(path: Path, sql: str) -> list:
    import sqlite3
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _old_checkout(old: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    d = old / "data"
    candles = "CREATE TABLE {p}_candles_15m (open_time INTEGER PRIMARY KEY, close REAL NOT NULL)"
    gaps = "CREATE TABLE known_gaps (table_name TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL)"
    _db(d / "hot" / "binance_spot" / "BTCUSDT.db", candles.format(p="btcusdt"), "INSERT INTO btcusdt_candles_15m VALUES (1, 2.0)")
    _db(d / "hot" / "binance_spot" / "ETHUSDT.db", candles.format(p="ethusdt"), "INSERT INTO ethusdt_candles_15m VALUES (7, 8.0)",
        "CREATE TABLE ethusdt_agg_trades (agg_id INTEGER PRIMARY KEY, price REAL)", "INSERT INTO ethusdt_agg_trades VALUES (1, 1.0)",
        gaps, "INSERT INTO known_gaps VALUES ('ethusdt_candles_15m', 1, 2)", "INSERT INTO known_gaps VALUES ('ethusdt_agg_trades', 3, 4)")
    _db(d / "hot" / "binance_usdm" / "BTCUSDT.db", "CREATE TABLE t (x)")
    _db(d / "hot" / "mt5" / "BTCUSD.db", "CREATE TABLE t (x)")
    _db(d / "hot" / "mt5" / "XAUUSD.db", "CREATE TABLE t (x)")
    cold = d / "cold" / "binance_spot" / "BTCUSDT" / "btcusdt_agg_trades" / "2026-09-30.parquet"
    cold.parent.mkdir(parents=True)
    cold.write_bytes(b"PAR1-day")
    for pair in ("BTCUSDT", "ETHUSDT"):
        _db(d / "instances" / pair / "app.db", "CREATE TABLE status (pair TEXT)", f"INSERT INTO status VALUES ('{pair}')")
        (d / "instances" / pair / "run").mkdir()
        (d / "instances" / pair / "run" / "supervisor.json").write_text("{}", encoding="utf-8")
    (d / "instances" / "BTCUSDT" / "charts" / "BTCUSDT").mkdir(parents=True)
    (d / "instances" / "BTCUSDT" / "charts" / "BTCUSDT" / "15m.png").write_bytes(b"png")
    _db(d / "shared" / "ai_usage.db", "CREATE TABLE ai_usage (id INTEGER PRIMARY KEY, pair TEXT)",
        "INSERT INTO ai_usage (pair) VALUES ('BTCUSDT'), ('ETHUSDT'), ('XAUUSD'), (NULL)")
    (d / "shared" / "account_peak.json").write_text('{"mt5:x:1": {"peak": 100}}', encoding="utf-8")
    (d / "shared" / "news_calendar.json").write_text("[]", encoding="utf-8")
    for stream, syms in (("spot_book", ["BTCUSDT", "ETHUSDT"]), ("mt5_ticks", ["BTCUSD@", "XAUUSD@"]), ("clock", ["binance"])):
        f = d / "research" / "price_matching" / stream / "2026-09-30" / "000000.parquet"
        f.parent.mkdir(parents=True)
        pq.write_table(pa.table({"recv_ms": list(range(len(syms))), "symbol": syms}), f)
    (old / "logs" / "BTCUSDT").mkdir(parents=True)
    (old / "logs" / "BTCUSDT" / "engine.jsonl").write_text("{}\n", encoding="utf-8")
    (old / "config").mkdir()
    (old / "config" / "config.local.yaml").write_text("ai: {}\n", encoding="utf-8")
    (old / ".env").write_text("SECRET=x\n", encoding="utf-8")


def test_the_import_copies_this_pairs_data_and_nothing_of_another_pair(tmp_path):
    import os
    spec = importlib.util.spec_from_file_location("ts_import_split", ROOT / "tools" / "import_from_old.py")
    imp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(imp)
    old, new = tmp_path / "the_claude_new", tmp_path / "TradingSystems" / "BTCUSDT"
    _old_checkout(old)
    s = folder_like("BTCUSDT")
    recorded = {"spot_book": ["btcusdt"], "usdm_book": ["btcusdt"], "mt5_ticks": ["BTCUSD@"]}
    imp.Importer(s, old, new, dry_run=True, overwrite=False).run(recorded)
    assert not new.exists()                                                   # a dry run writes nothing
    imp.Importer(s, old, new, dry_run=False, overwrite=False).run(recorded)
    d = new / "data"
    assert _rows(d / "hot" / "binance_spot" / "BTCUSDT.db", "SELECT * FROM btcusdt_candles_15m") == [(1, 2.0)]
    sib = d / "hot" / "binance_spot" / "ETHUSDT.db"                           # the sibling: candles + their gaps only
    assert sorted(r[0] for r in _rows(sib, "SELECT name FROM sqlite_master WHERE type='table'")) == \
        ["ethusdt_candles_15m", "known_gaps"]
    assert _rows(sib, "SELECT * FROM ethusdt_candles_15m") == [(7, 8.0)]
    assert _rows(sib, "SELECT table_name FROM known_gaps") == [("ethusdt_candles_15m",)]
    cold = d / "cold" / "binance_spot" / "BTCUSDT" / "btcusdt_agg_trades" / "2026-09-30.parquet"
    assert cold.read_bytes() == b"PAR1-day" and os.stat(cold).st_nlink == 2   # a hard link, not a second copy
    assert not (d / "hot" / "mt5" / "XAUUSD.db").exists() and not (d / "instances" / "ETHUSDT").exists()
    assert _rows(d / "instances" / "BTCUSDT" / "app.db", "SELECT * FROM status") == [("BTCUSDT",)]
    assert not (d / "instances" / "BTCUSDT" / "run").exists()                 # run-time state is never carried over
    assert (d / "instances" / "BTCUSDT" / "charts" / "BTCUSDT" / "15m.png").exists()
    assert sorted(_rows(d / "shared" / "ai_usage.db", "SELECT pair FROM ai_usage"), key=str) == [("BTCUSDT",), (None,)]
    assert (d / "shared" / "account_peak.json").exists() and not (d / "shared" / "news_calendar.json").exists()
    import pyarrow.parquet as pq
    rp = d / "research" / "price_matching"
    assert pq.read_table(rp / "spot_book" / "2026-09-30" / "000000.parquet")["symbol"].to_pylist() == ["BTCUSDT"]
    assert pq.read_table(rp / "mt5_ticks" / "2026-09-30" / "000000.parquet")["symbol"].to_pylist() == ["BTCUSD@"]
    assert pq.read_table(rp / "clock" / "2026-09-30" / "000000.parquet")["symbol"].to_pylist() == ["binance"]
    assert (new / "logs" / "BTCUSDT" / "engine.jsonl").exists() and (new / "config" / "config.local.yaml").exists()
    assert (new / ".env").read_text(encoding="utf-8") == "SECRET=x\n"
    assert (d / "IMPORTED_FROM.json").exists()
    with pytest.raises(imp.Refused):                                          # never over an existing system
        imp.Importer(s, old, new, dry_run=False, overwrite=False).run(recorded)
    imp.Importer(s, old, new, dry_run=False, overwrite=True).run(recorded)
    with pytest.raises(imp.Refused):
        imp.Importer(three("BTCUSDT"), old, new, dry_run=True, overwrite=False)   # three pairs = not a pair folder


def test_first_start_stops_only_its_own_pair_in_the_old_checkout_after_the_preflight():
    raw = (ROOT / "scripts" / "first_start.bat").read_bytes()
    assert raw.isascii() and b"\n" not in raw.replace(b"\r\n", b"")
    text = raw.decode("ascii")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lower().startswith("rem "))
    assert "stop_all" not in code                                           # its search would find switched folders
    pre, stop, imp = (code.index("--preflight"), code.index("run --stop --instance %PAIR%"),
                      code.index('import_from_old.py --from "%OLD%"\n'))
    assert pre < stop < imp < code.index("install_autostart.ps1\" -AllPairs") < code.index("start_all.bat")
    deleted = re.findall(r'schtasks /Delete /TN "([^"]+)"', code)
    assert deleted == [r"\TradingSystem-%PAIR%", r"\TradingSystemOps-%%T"]  # the old root-folder tasks only
    assert "TradingSystem-MT5" not in code
