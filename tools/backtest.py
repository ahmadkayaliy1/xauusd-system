"""Tick-level backtest of the live gold desk (P12.7, docs/plans/gold_live_and_backtest.md phase B).

    python tools/backtest.py --from 2026-09-30T06:00Z --to 2026-10-01T11:00Z --answers production
    python tools/backtest.py --from 2026-09-29 --to 2026-09-30 --answers claude --max-calls 60
    python tools/backtest.py --from ... --to ... --answers replay --source data/backtests/<run>/data/instances/XAUUSD/app.db

The production engine, risk gate, PAPER execution and management run on a simulated clock over [from, to); the real
recorded market is replayed into the run's own data root under ``data/backtests/<run>/`` (the real stores are only
read). ``--answers claude`` asks the model for real (each call spends the shared plan: about 26 k input tokens; the run
stops at ``--max-calls``); ``production`` stores the real system's own decisions again at their own time + latency (no
call: the calibration of the replay); ``replay`` does the same with an earlier run's decisions (execution / management
variants without calls). The report: ``report.md`` / ``report.json`` in the run folder. ``--keep-market`` keeps the
run's market copy (≈ 0.5 GB); by default it is deleted after the report (decisions, ledger and report stay).

Never a broker order: the run is forced to paper execution. Exit 0 done, 1 error, 3 invalid arguments.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradingsystem.backtest import report  # noqa: E402
from tradingsystem.backtest.runner import (Backtest, RunConfig, missing_news_weeks, run_id_for,  # noqa: E402
                                           run_settings)
from tradingsystem.core.timeutil import iso, parse_date_spec  # noqa: E402

REAL_DATA = ROOT / "data"
CONFIG = ROOT / "config" / "config.yaml"


def baseline_section(s, real: Path, pair: str, trades: list[dict], start: int, end: int) -> dict | None:
    """The random-entry baseline of the period (tools/desk_report.py B19): a BUY / SELL at each of the run's own stop
    sizes from every 5-minute close the desk could enter, the real per-bar spread, target 2R - what a random entry
    at the same stops, hours and costs earned; beside the run's own expectancy."""
    import importlib.util
    import math
    filled = [x for x in trades if x.get("filled") and not x.get("open") and x.get("sl") and x.get("fill")]
    if not filled:
        return None
    spec = importlib.util.spec_from_file_location("ts_tools_desk_report", ROOT / "tools" / "desk_report.py")
    dr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dr)
    s_real = s.model_copy(update={"paths": s.paths.model_copy(update={"data_dir": str(real)})})
    stops = sorted({round(abs(x["fill"] - x["sl"]), 1) for x in filled})
    days = max(1, math.ceil((end - start) / 86_400_000))
    b = dr.baseline(s_real, pair, stops, days, end)
    if b.get("error"):
        return {"error": b["error"]}
    rows = [{"stop": abs(x["fill"] - x["sl"]), "decision": x["side"], "r_net": x["r"]} for x in filled]
    return {"expectancy_r": dr.baseline_for(rows, b), "entries": b.get("entries"), "days": days,
            "by_stop_side": {k: {"n": v["n"], "expectancy_r": v["expectancy_r"], "tp_first_share": v["tp_first_share"]}
                             for k, v in (b.get("by_stop_side") or {}).items()}}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="start", required=True, help="UTC, ISO (2026-09-30 or 2026-09-30T06:00Z)")
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--answers", choices=("claude", "production", "replay"), default="claude")
    ap.add_argument("--source", help="replay: the earlier run's app.db (production: default the real system's)")
    ap.add_argument("--equity", type=float, default=107.38)
    ap.add_argument("--max-calls", type=int, default=150)
    ap.add_argument("--calls-per-day", type=int, default=60)
    ap.add_argument("--news", action="store_true",
                    help="news blackout on, from the archived real calendars (data/shared/news_archive/<NY Sunday>.json)")
    ap.add_argument("--run-id")
    ap.add_argument("--brief", help="another desk brief for this run (prompt A/B), e.g. desks/xau_live_b")
    ap.add_argument("--desk-management", choices=("model", "be_2r"),
                    help="another desk management policy for this run (the pre-D-053 'model' or 'be_2r')")
    ap.add_argument("--mgmt", default="", help="replay/production: a management variant, e.g. be_r=1.5,tp_r=2 (be_r=0: "
                                               "no breakeven)")
    ap.add_argument("--keep-market", action="store_true")
    ap.add_argument("--data", default=str(REAL_DATA), help="the real data root (read only); default this checkout's")
    a = ap.parse_args(argv)
    try:
        start, end = parse_date_spec(a.start), parse_date_spec(a.end)
        if end <= start:
            raise ValueError("--to must be after --from")
        real = Path(a.data)
        if not (real / "hot").is_dir():
            raise ValueError(f"{real} holds no market data")
        source = Path(a.source) if a.source else (real / "instances" / "XAUUSD" / "app.db"
                                                  if a.answers == "production" else None)
        if a.answers != "claude" and (source is None or not source.exists()):
            raise ValueError(f"--answers {a.answers} needs an existing --source app.db")
        archive = real / "shared" / "news_archive"
        mgmt = {k: float(v) for k, v in (kv.split("=") for kv in a.mgmt.split(",") if kv)}
        if mgmt and a.answers == "claude":
            raise ValueError("--mgmt changes stored answers: --answers replay or production")
        if set(mgmt) - {"be_r", "tp_r"}:
            raise ValueError(f"--mgmt: unknown key(s) {sorted(set(mgmt) - {'be_r', 'tp_r'})}")
        if a.news and (miss := missing_news_weeks(archive, start, end)):
            raise ValueError(f"--news: no archived calendar for the New York week(s) {', '.join(miss)} in {archive}")
    except ValueError as exc:
        print(f"backtest: invalid: {exc}", file=sys.stderr)
        return 3
    rc = RunConfig(start_ms=start, end_ms=end, equity=a.equity, answers=a.answers, source=source,
                   max_calls=a.max_calls, news=a.news, news_archive=archive, brief=a.brief, mgmt=mgmt,
                   desk_management=a.desk_management)
    rc.ai = {**rc.ai, "daily_calls_per_pair": a.calls_per_day}
    run_id = a.run_id or run_id_for(rc)
    run_root = real / "backtests" / run_id
    if run_root.exists():
        print(f"backtest: invalid: {run_root} exists", file=sys.stderr)
        return 3
    (run_root / "logs").mkdir(parents=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(run_root / "logs" / "backtest.log", encoding="utf-8")])
    s = run_settings(CONFIG, run_root, rc)
    bt = Backtest(s, rc, real, run_root)
    print(f"backtest {run_id}: {iso(start)} → {iso(end)}, answers {a.answers}, equity {a.equity}, "
          f"max calls {a.max_calls}")
    try:
        prep = bt.prepare()
        print(f"  market prepared: {prep['cold_linked']} cold day files linked")
        result = asyncio.run(bt.run())
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("backtest").exception("run failed")
        print(f"backtest: error {exc!r} (log: {run_root / 'logs' / 'backtest.log'})", file=sys.stderr)
        return 1
    meta = {"run_id": run_id, "pair": rc.pair, "from": iso(start), "to": iso(end), "from_ms": start, "to_ms": end,
            "answers": a.answers, "source": str(source) if source else None, "equity": a.equity, "news": a.news,
            "max_calls": a.max_calls, "brief": s.pairs[rc.pair].desk.brief if s.pairs[rc.pair].desk else None,
            "mgmt": mgmt or None,
            "result": result}
    doc = report.write(run_root, s, meta, source)
    try:
        base = baseline_section(s, real, rc.pair, doc["trades"], start, end)
    except Exception as exc:  # noqa: BLE001 - a reference, never a reason to lose the run's report
        base = {"error": repr(exc)[:200]}
    if base is not None:
        doc["baseline"] = base
        (run_root / "report.json").write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8", newline="\n")
        with (run_root / "report.md").open("a", encoding="utf-8", newline="\n") as f:
            f.write("\n## Random-entry baseline (the run's stops and sides, every 5-min close the desk could enter, "
                    "real spread, target 2R)\n\n")
            f.write(f"- {base.get('error') or ''}" if base.get("error") else
                    f"- baseline expectancy at the run's stops/sides: **{base['expectancy_r']} R** (the run: "
                    f"{doc['summary']['expectancy_r']} R) over {base['entries']} entries in {base['days']} day(s)\n"
                    f"- by stop/side: {base['by_stop_side']}\n")
    if not a.keep_market:                     # the run's market copy (~0.3 GB): never left behind silently
        import gc
        gc.collect()                          # drop stray SQLite connections before Windows lets the files go
        for sub in ("hot", "cold"):
            d = s.paths.data() / sub
            for attempt in range(5):
                shutil.rmtree(d, ignore_errors=True)
                if not d.exists():
                    break
                time.sleep(2)
            if d.exists():
                print(f"  WARNING: the market copy {d} could not be deleted (in use?) - delete it by hand",
                      file=sys.stderr)
    sm = doc["summary"]
    print(f"  done in {result['real_s']} s real time: {sm['trades']} trades, expectancy {sm['expectancy_r']} R, net "
          f"${sm['net_usd']} ({sm['return_pct']} %), max drawdown {sm['max_drawdown_pct']} %, calls {result['calls']}")
    print(f"  report: {run_root / 'report.md'}")
    print(json.dumps({"stopped_by": result.get("stopped_by")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
