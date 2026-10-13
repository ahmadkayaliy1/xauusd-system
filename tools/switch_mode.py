"""Switch the XAUUSD system between the DEMO and the REAL account on the SAME MetaTrader terminal (D-064, owner):

    scripts\\switch_mode.bat live                 # the owner has logged the terminal into the real account
    scripts\\switch_mode.bat demo
    scripts\\switch_mode.bat live --adopt-server  # the first time: take the terminal's real server name into config.local
    python tools\\switch_mode.py --check         # the checks only, nothing changed

The terminal is one: the owner logs it into the account he wants, then runs this. The tool refuses to switch while a
desk position / pending order or a scalp auto-trade is open, while the terminal is not on an account of the target
kind (demo / real) and server (``mt5.profiles.<target>.server``), and - for ``live`` - without the typed word LIVE.
Then: stop the system and the scalp loop, write ``execution.mode`` (+ the live confirmation phrase), ``mt5.data_profile``
(prices come from the same terminal, so the data profile follows) into ``config\\config.local.yaml`` (the existing
``execution:`` / ``mt5:`` blocks are replaced, one block per section), check the config loads, start both, wait for the
executor to report the target mode, and send a notification. Exit 0 switched (or already there), 2 refused, 1 failed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradingsystem.core.settings import LIVE_CONFIRMATION_PHRASE, LOCAL_CONFIG, load_settings  # noqa: E402

MODES = ("demo", "live")
PY = ROOT / ".venv" / "Scripts" / "python.exe"
TRADE_MODE = {"demo": 0, "real": 2}
WAIT_STOP_S, WAIT_START_S = 120, 180


# ------------------------------------------------------------------------------------------------- pure parts
def precheck(current: str, target: str, executor: dict | None, scalp_open: list, account: dict | None,
             profile_server: str, profile_kind: str) -> list[str]:
    """The reasons NOT to switch (empty = go). ``executor`` = the executor status row's detail (None when the system
    is not running), ``scalp_open`` = the scalp auto-trades still tracked, ``account`` = the terminal's account
    (``server``, ``trade_mode``, ``login``; None = no terminal)."""
    out = []
    if target not in MODES:
        out.append(f"target must be demo or live (got {target!r})")
        return out
    if executor:
        if int(executor.get("open_positions") or 0) or int(executor.get("open_orders") or 0):
            out.append(f"the desk holds {executor.get('open_positions')} position(s) / {executor.get('open_orders')} "
                       f"pending order(s) on the {current} account - close them first")
    if scalp_open:
        out.append(f"{len(scalp_open)} scalp auto-trade(s) still tracked on the {current} account - close / let them "
                   "expire first (data\\scalp\\auto_state.json)")
    if account is None:
        out.append("the MT5 terminal is not running or not reachable - open it and log into the target account")
        return out
    want = TRADE_MODE[profile_kind]
    if int(account.get("trade_mode", -1)) != want:
        out.append(f"the terminal is logged into a {'demo' if account.get('trade_mode') == 0 else 'real' if account.get('trade_mode') == 2 else 'contest'} "
                   f"account ({account.get('server')} / {account.get('login')}); log it into the {profile_kind} account first")
    elif account.get("server") != profile_server:
        out.append(f"the terminal is on server {account.get('server')!r} but mt5.profiles.{target}.server is "
                   f"{profile_server!r}: run again with --adopt-server to take the terminal's name into config.local, "
                   "or fix the config")
    return out


def set_blocks(text: str, blocks: dict[str, dict]) -> str:
    """Replace (or append) the given TOP-LEVEL YAML blocks of ``config.local.yaml`` with ours, keeping every other
    block and its comments. One block per section (the loader refuses a repeated key)."""
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln and not ln[0].isspace() and not ln.startswith("#") and ":" in ln]
    spans: dict[str, tuple[int, int]] = {}
    for n, i in enumerate(starts):
        key = lines[i].split(":", 1)[0].strip()
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        spans[key] = (i, end)

    def render(key: str, body: dict) -> list[str]:
        out = [f"{key}:"]

        def walk(d: dict, depth: int) -> None:
            for k, v in d.items():
                pad = "  " * depth
                if isinstance(v, dict):
                    out.append(f"{pad}{k}:")
                    walk(v, depth + 1)
                else:
                    out.append(f"{pad}{k}: {json.dumps(v) if isinstance(v, str) else json.dumps(v)}")
        walk(body, 1)
        return out

    for key, body in blocks.items():
        new = render(key, body)
        if key in spans:
            a, b = spans[key]
            # keep trailing blank lines of the old block out of the replacement
            while b > a + 1 and not lines[b - 1].strip():
                b -= 1
            lines[a:b] = new
            # recompute spans after the edit
            starts = [i for i, ln in enumerate(lines) if ln and not ln[0].isspace() and not ln.startswith("#") and ":" in ln]
            spans = {}
            for n, i in enumerate(starts):
                k2 = lines[i].split(":", 1)[0].strip()
                spans[k2] = (i, starts[n + 1] if n + 1 < len(starts) else len(lines))
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines += new
            spans[key] = (len(lines) - len(new), len(lines))
    return "\n".join(lines).rstrip("\n") + "\n"


def blocks_for(target: str, live_server: str) -> dict[str, dict]:
    ex = {"mode": target}
    if target == "live":
        ex["live_confirmation"] = LIVE_CONFIRMATION_PHRASE
    return {"execution": ex, "mt5": {"data_profile": target, "profiles": {"live": {"server": live_server}}}}


# ------------------------------------------------------------------------------------------------- IO
def executor_detail(s) -> dict | None:
    db = s.paths.state() / "app.db"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            row = con.execute("SELECT detail, updated_ms FROM collector_status WHERE collector='executor'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        det = json.loads(row[0] or "{}")
    except ValueError:
        return None
    det["_updated_ms"] = row[1]
    return det if isinstance(det, dict) else None


def scalp_open(s) -> list:
    try:
        from tradingsystem.scalp.autotrade import read_state
        return read_state(s)["open"]
    except Exception:  # noqa: BLE001
        return []


def terminal_account(s, profile) -> dict | None:
    """The terminal's account as it is now (no server check: that is the precheck's job). Attach only."""
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return None
    from tradingsystem.ingest.mt5.terminal import NO_LAUNCH_MIN_AGE_S, terminal_running
    if not terminal_running(profile.terminal_path, min_age_s=NO_LAUNCH_MIN_AGE_S):
        return None
    if not mt5.initialize(path=profile.terminal_path):
        return None
    try:
        a = mt5.account_info()
        if a is None:
            return None
        return {"server": a.server, "trade_mode": int(a.trade_mode), "login": int(a.login), "balance": float(a.balance),
                "equity": float(a.equity), "currency": a.currency}
    finally:
        mt5.shutdown()


def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()[-800:]


def wait_until(pred, timeout_s: float, every_s: float = 3.0) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(every_s)
    return bool(pred())


def notify(s, level: str, title: str, text: str) -> None:
    try:
        from tradingsystem.core import notify as nt
        nt.notify(s, level, title, text, key=f"switch_mode:{int(time.time() // 60)}", pair=s.scalp.pair)
        nt.flush(20.0)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------------------------------------- the switch
def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("target", nargs="?", choices=MODES, help="demo | live")
    ap.add_argument("--adopt-server", action="store_true",
                    help="take the terminal's (real) server name into mt5.profiles.live.server (config.local)")
    ap.add_argument("--check", action="store_true", help="the checks only; nothing is changed")
    ap.add_argument("--yes", action="store_true", help="(demo only) no question")
    a = ap.parse_args(argv)
    s = load_settings()
    pair = next(iter(s.instances)) if s.instances else "XAUUSD"
    current = s.execution.mode
    target = a.target or current
    pname = s.mt5.execution_profile_by_mode.get(target, target)
    profile = s.mt5.profiles[pname]
    ex = executor_detail(s)
    running = bool(ex and ex.get("_updated_ms") and time.time() * 1000 - ex["_updated_ms"] < 120_000)
    acct = terminal_account(s, profile)
    print(f"current mode: {current} ({'running' if running else 'not running'}); target: {target}")
    if acct:
        print(f"terminal: {acct['server']} / {acct['login']} ({'demo' if acct['trade_mode'] == 0 else 'REAL' if acct['trade_mode'] == 2 else 'contest'}), "
              f"balance {acct['balance']:.2f} {acct['currency']}")
    else:
        print("terminal: not reachable")
    live_server = s.mt5.profiles["live"].server
    if a.adopt_server and target == "live" and acct and acct["trade_mode"] == 2:
        live_server = acct["server"]
    problems = precheck(current, target, ex if running else None, scalp_open(s), acct, live_server, profile.account_type)
    if target == current and not a.check:
        print(f"already in {current} mode" + (" (the terminal matches)" if not problems else ""))
        for p in problems:
            print(" !", p)
        return 0 if not problems else 2
    for p in problems:
        print(" !", p)
    if a.check:
        return 0 if not problems else 2
    if problems:
        print("refused - nothing changed")
        return 2
    if target == "live":
        word = input("Type LIVE to switch the system to REAL-MONEY trading on this account: ").strip()
        if word != "LIVE":
            print("not confirmed - nothing changed")
            return 2
    elif not a.yes:
        word = input(f"Switch to {target}? [y/N] ").strip().lower()
        if word not in ("y", "yes"):
            print("not confirmed - nothing changed")
            return 2
    # 1. stop
    print("stopping the system and the scalp loop ...")
    rc, out = _run([str(PY), "-m", "tradingsystem", "run", "--stop", "--instance", pair], WAIT_STOP_S + 30)
    print(out[-300:])
    stop_file = s.paths.data() / "scalp" / "STOP"
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text("stop", encoding="utf-8")
    time.sleep(20)
    # 2. config.local
    text = LOCAL_CONFIG.read_text(encoding="utf-8") if LOCAL_CONFIG.exists() else ""
    new = set_blocks(text, blocks_for(target, live_server))
    backup = LOCAL_CONFIG.with_name(f"config.local.before_switch_{int(time.time())}.yaml")
    if text:
        backup.write_text(text, encoding="utf-8", newline="\n")
    LOCAL_CONFIG.write_text(new, encoding="utf-8", newline="\n")
    rc, out = _run([str(PY), "-m", "tradingsystem", "config"], 120)
    if rc != 0:
        print("config check FAILED - restoring the previous config.local:", out[-400:])
        LOCAL_CONFIG.write_text(text, encoding="utf-8", newline="\n")
        return 1
    print(f"config.local: execution.mode {target}, mt5.data_profile {target}, live server {live_server!r} "
          f"(backup {backup.name})")
    # 3. start
    print("starting ...")
    rc, out = _run([str(PY), "-m", "tradingsystem", "run", "all", "--detach", "--instance", pair], 120)
    print(out[-300:])
    stop_file.unlink(missing_ok=True)
    _run([str(PY), str(ROOT / "tools" / "scalp_brief.py"), "--start-loop"], 120)
    s2 = load_settings()

    def reported() -> bool:
        d = executor_detail(s2)
        return bool(d and d.get("mode") == target and d.get("_updated_ms") and time.time() * 1000 - d["_updated_ms"] < 60_000)
    ok = wait_until(reported, WAIT_START_S)
    d = executor_detail(s2) or {}
    if ok:
        msg = (f"mode {target.upper()} - executor live on {acct['server'] if acct else '?'} / {acct['login'] if acct else '?'}, "
               f"equity {d.get('equity')}")
        print("switched:", msg)
        notify(s2, "warn" if target == "live" else "info", f"System switched to {target.upper()}", msg)
        return 0
    print("the executor did not report the new mode within the wait - check scripts\\status_all.bat and logs\\XAUUSD\\executor.jsonl")
    notify(s2, "warn", f"Switch to {target} not confirmed", "the executor did not report the new mode - check the logs")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
