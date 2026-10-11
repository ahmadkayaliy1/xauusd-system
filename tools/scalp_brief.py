"""The owner's manual-scalping brief (D-058) - a job separate from the automatic desk; it never places an order:

    python tools/scalp_brief.py --once [--reason manual|dashboard|hourly]      one brief now (the gates decide)
    python tools/scalp_brief.py --once --no-model --fixture tests/fixtures/scalp_brief_sample.json   a dry run
    python tools/scalp_brief.py --score-only       score the stored briefs on the real ticks, refresh stats.json
    python tools/scalp_brief.py --status           status.json + whether the loop runs (JSON)
    python tools/scalp_brief.py --start-loop       scripts\\start_scalp.bat: a hidden, detached hourly loop
    pythonw tools/scalp_brief.py --loop --quiet    what --start-loop starts (ends when data\\scalp\\STOP appears)

The run is this folder's one pair (``--pair``, default: the folder's ``instances:`` key, else ``scalp.pair``);
``TS_MT5_NO_LAUNCH=1`` for the whole process, so reading the owner's deals can never start a terminal. Log:
``logs/<PAIR>/scalp.jsonl``. Exit codes: 0 ok, 1 failed, 2 skipped (a gate, a lock, a loop already running)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("TS_MT5_NO_LAUNCH", "1")

from tradingsystem.core.logsetup import setup_logging  # noqa: E402
from tradingsystem.core.settings import INSTANCE_ENV, load_settings  # noqa: E402

log = logging.getLogger("scalp_brief")


def default_pair() -> str:
    s0 = load_settings()
    return next(iter(s0.instances)) if len(s0.instances) == 1 else s0.scalp.pair


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="one brief now")
    mode.add_argument("--loop", action="store_true", help="the hourly loop (foreground; --start-loop detaches it)")
    mode.add_argument("--start-loop", action="store_true", help="start the loop hidden and detached (once)")
    mode.add_argument("--score-only", action="store_true", help="score the stored briefs, no model call")
    mode.add_argument("--status", action="store_true", help="print status.json and the loop state")
    ap.add_argument("--quiet", action="store_true", help="no console log (the loop and its children)")
    ap.add_argument("--reason", choices=("hourly", "dashboard", "manual"), default="manual")
    ap.add_argument("--no-model", action="store_true", help="take the brief from --fixture: no CLI call, no ledger row")
    ap.add_argument("--fixture", help="a ScalpBrief JSON for --no-model")
    ap.add_argument("--pair", help="the pair (default: this folder's instance)")
    a = ap.parse_args(argv)
    if a.no_model and not a.fixture:
        ap.error("--no-model needs --fixture <brief.json>")
    pair = a.pair or default_pair()
    s = load_settings(extra_env={INSTANCE_ENV: pair})
    setup_logging("scalp", logs_dir=s.paths.logs(), console=not a.quiet, secret_env_names=s.secret_env_names())
    from tradingsystem.scalp import service
    try:
        if a.status:
            print(json.dumps(service.status_doc(s), indent=1, ensure_ascii=False, default=str))
            return service.EXIT_OK
        if a.start_loop:
            started, pid = service.start_loop_detached(s)
            print(f"started pid {pid}" if started else f"already running pid {pid}")
            return service.EXIT_OK
        if a.loop:
            return service.loop(s)
        if a.score_only:
            return service.score_only(s)
        code = service.run_once(s, reason=a.reason, no_model=a.no_model, fixture=a.fixture)
        if not a.quiet:
            print({service.EXIT_OK: "ok", service.EXIT_SKIPPED: "skipped"}.get(code, "failed")
                  + " - see data\\scalp\\status.json")
        return code
    except Exception:  # noqa: BLE001 - the exit code says it; the log has the trace
        log.exception("scalp_brief failed")
        return service.EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
