"""Backtest runs side by side (P12.7): ``python tools/backtest_compare.py RUN [RUN ...]`` (run ids under
data/backtests, or paths) → one markdown table on stdout - period, brief, trades, wins, expectancy R, net %, max
drawdown, model calls and input tokens, and the random-entry baseline at the run's own stops - plus the pooled totals
per brief. Read-only."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(arg: str, data: Path) -> dict | None:
    p = Path(arg)
    run = p if p.is_dir() else data / "backtests" / arg
    f = run / "report.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    data = ROOT / "data"
    if argv[:1] == ["--data"]:
        data, argv = Path(argv[1]), argv[2:]
    rows, pooled = [], defaultdict(lambda: {"trades": 0, "r": 0.0, "wins": 0, "usd": 0.0, "calls": 0, "tin": 0})
    for a in argv:
        d = load(a, data)
        if d is None:
            print(f"(no report: {a})", file=sys.stderr)
            continue
        m, s, c, b = d["meta"], d["summary"], d["calls"], d.get("baseline") or {}
        brief = m.get("brief") or "desks/xau_live"
        rows.append(f"| {m['run_id']} | {m['from'][:10]} | {brief.split('/')[-1]} | {'on' if m.get('news') else 'off'} | "
                    f"{s['trades']} | {s['wins']} | {s['expectancy_r']} | {s['total_r']} | {s['return_pct']} % | "
                    f"{s['max_drawdown_pct']} % | {c['ledger_calls']} | {c['input_tokens'] / 1e6:.2f} M | "
                    f"{b.get('expectancy_r', '-')} |")
        p = pooled[brief]
        p["trades"] += s["trades"]
        p["r"] += s["total_r"] or 0.0
        p["wins"] += s["wins"]
        p["usd"] += s["net_usd"]
        p["calls"] += c["ledger_calls"]
        p["tin"] += c["input_tokens"]
    print("| run | day | brief | news | trades | wins | exp R | total R | net | max DD | calls | input | baseline R |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    print("\n".join(rows))
    print("\n| brief | trades | wins | total R | exp R | net USD | calls | input |\n|---|---|---|---|---|---|---|---|")
    for k, p in pooled.items():
        exp = round(p["r"] / p["trades"], 3) if p["trades"] else None
        print(f"| {k} | {p['trades']} | {p['wins']} | {round(p['r'], 2)} | {exp} | {round(p['usd'], 2)} | {p['calls']} | "
              f"{p['tin'] / 1e6:.2f} M |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
