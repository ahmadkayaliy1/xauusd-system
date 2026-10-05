"""One account across many one-day backtest runs (P12.7): ``python tools/backtest_chain.py [--equity 100] RUN [RUN ...]``
(run ids under data/backtests, or paths; one run per day) → the equity path of a single account that started the first
day with ``--equity`` and took each run's trades in time order, with the D-052 gate re-checked at the running equity
(the 1 oz minimum's risk <= --max-risk % of equity, notional <= --max-leverage x equity, the day's worst case -
realised loss + every open stop + the new stop - <= --daily % of the day's opening equity, no entry after the day's
--losses-th losing trade). Every run trades 0.01 lot (the minimum: the 1 % target is below it at these equities), so a
trade's USD result does not depend on the equity - only whether the gate lets it in. Read-only; markdown on stdout.

Not the same as one continuous run: each day's model saw a fresh account of the run's own equity (107.38) and no
history of the earlier days, a position still open at a run's end is left out, and a trade the run's gate refused
(see each report's gate rejections) never appears here even if this account could have taken it."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def chain(reports: list[dict], equity: float, max_risk: float, max_lev: float, daily: float,
          losses: int) -> dict:
    trades, open_left, refusals = [], [], 0
    for d in reports:
        refusals += sum((d.get("calls") or {}).get("gate_rejections", {}).values()) \
            if isinstance((d.get("calls") or {}).get("gate_rejections"), dict) else 0
        for t in d["trades"]:
            if not t.get("filled"):
                continue
            if t.get("open"):
                open_left.append(f"{d['meta']['run_id']} {t['id']}")
                continue
            trades.append({**t, "run": d["meta"]["run_id"], "t_in": _ms(t["fill_time"]),
                           "t_out": _ms(t["close_time"])})
    events = sorted([(t["t_in"], 1, i) for i, t in enumerate(trades)] +
                    [(t["t_out"], 0, i) for i, t in enumerate(trades)])
    eq, peak, max_dd = equity, equity, 0.0
    day, day_open, day_real, day_losses = None, equity, 0.0, 0
    live: dict[int, dict] = {}
    taken, refused, by_day, curve = [], [], {}, [("start", round(equity, 2))]
    for ts, kind, i in events:
        t = trades[i]
        if _day(ts) != day:
            day, day_open, day_real, day_losses = _day(ts), eq, 0.0, 0
        if kind == 0:                                     # a close
            if i not in live:
                continue
            live.pop(i)
            res = t["pnl_usd"] + t.get("swap_usd", 0.0)
            eq += res
            day_real += res
            day_losses += res < 0
            by_day[_day(ts)] = by_day.get(_day(ts), 0.0) + res
            peak = max(peak, eq)
            max_dd = max(max_dd, (peak - eq) / peak * 100 if peak > 0 else 0.0)
            curve.append((t["close_time"][:16], round(eq, 2)))
            continue
        risk = abs(t["fill"] - t["sl"]) * 100 * t["volume"]
        why = None
        if eq <= 0:
            why = "no equity"
        elif risk / eq * 100 > max_risk:
            why = f"1 oz risks {risk / eq * 100:.1f} % > {max_risk:g} %"
        elif t["fill"] * 100 * t["volume"] / eq > max_lev:
            why = f"leverage {t['fill'] * 100 * t['volume'] / eq:.0f}x > {max_lev:g}x"
        elif day_losses >= losses:
            why = f"day stop ({losses} losing trades)"
        elif (max(0.0, -day_real) + sum(x["risk"] for x in live.values()) + risk) / day_open * 100 > daily:
            why = f"day worst case > {daily:g} %"
        if why:
            refused.append((t["run"], t["id"], t["fill_time"][:16], why, t["pnl_usd"]))
            continue
        live[i] = {"risk": risk}
        taken.append(t)
    wins = sum(1 for t in taken if t["pnl_usd"] + t.get("swap_usd", 0.0) > 0)
    days = sorted(by_day.items())
    return {"start": equity, "end": round(eq, 2), "return_pct": round((eq / equity - 1) * 100, 1),
            "trades": len(taken), "wins": wins, "win_rate": round(wins / len(taken), 3) if taken else None,
            "total_r": round(sum(t["r"] for t in taken), 2), "max_dd_pct": round(max_dd, 1),
            "best_day": max(days, key=lambda x: x[1]) if days else None,
            "worst_day": min(days, key=lambda x: x[1]) if days else None,
            "days": days, "refused": refused, "open_left": open_left, "run_refusals": refusals, "curve": curve}


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    opts = {"--equity": 100.0, "--max-risk": 15.0, "--max-leverage": 60.0, "--daily": 30.0, "--losses": 3}
    data, runs = ROOT / "data", []
    while argv:
        a = argv.pop(0)
        if a == "--data":
            data = Path(argv.pop(0))
        elif a in opts:
            opts[a] = type(opts[a])(argv.pop(0))
        else:
            runs.append(a)
    reports = []
    for a in runs:
        p = Path(a)
        f = (p if p.is_dir() else data / "backtests" / a) / "report.json"
        if not f.exists():
            print(f"(no report: {a})", file=sys.stderr)
            continue
        reports.append(json.loads(f.read_text(encoding="utf-8")))
    reports.sort(key=lambda d: d["meta"]["from_ms"])
    seen = [d["meta"]["from"][:10] for d in reports]
    if len(set(seen)) != len(seen):
        print("two runs of the same day - pass one run per day", file=sys.stderr)
        return 2
    r = chain(reports, opts["--equity"], opts["--max-risk"], opts["--max-leverage"], opts["--daily"],
              opts["--losses"])
    print(f"# One account over {len(reports)} days ({seen[0]} … {seen[-1]})\n")
    print(f"- start {r['start']:.2f} USD → end **{r['end']:.2f} USD** ({r['return_pct']:+.1f} %)")
    print(f"- trades {r['trades']}, won {r['wins']} ({(r['win_rate'] or 0) * 100:.0f} %), total {r['total_r']:+.2f} R")
    print(f"- max drawdown {r['max_dd_pct']} % · best day {r['best_day']} · worst day {r['worst_day']}")
    print(f"- refused here at the running equity: {len(r['refused'])} · left out (open at a run's end): "
          f"{len(r['open_left'])} · refused by the runs' own gates: {r['run_refusals']}\n")
    print("| day | USD | equity after |\n|---|---|---|")
    eq = r["start"]
    by = dict(r["days"])
    for day in seen:
        eq += by.get(day, 0.0)
        print(f"| {day} | {by.get(day, 0.0):+.2f} | {eq:.2f} |")
    for x in r["refused"]:
        print(f"\nrefused: {x[0]} {x[1]} {x[2]} - {x[3]} (its result was {x[4]:+.2f})")
    for x in r["open_left"]:
        print(f"\nleft out: {x}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
