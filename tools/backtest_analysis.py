"""The full read-out of a set of one-day backtest runs (P12.7): ``python tools/backtest_analysis.py [--equity 100]
[--split YYYY-MM-DD] RUN [RUN ...]`` (one run per day) → markdown on stdout, no model call:

1. every trade (entry, stop, target, R, USD, exit, minutes, best / worst excursion, the management that acted, the
   decision's session / regime / 4h-1d bias / setup) and each loser's class;
2. the results by session, hour, order type, side, with / against the 4h-1d bias, confidence, regime, exit;
3. one account from ``--equity`` USD: always 0.01 lot, 0.01 lot per whole ``--equity`` USD of equity (the same risk
   %), and the ideal fractional version (backtest_chain.chain, the D-052 gate re-checked);
4. what-if variants on the recorded ticks (backtest_whatif.bracket): the plain bracket, breakeven at 1 / 1.5 / 2 R,
   the model's own stop changes undone, fixed targets - and filters that drop trades - each as one account, on the
   whole period and on its two halves (``--split``: the first day of the second half) - a change is only worth
   anything when it helps in both halves;
5. a bootstrap of the days (20,000 months of 21 days, and quarters of 63, from --equity): the chance of a losing
   month, of the account falling below 70 / 50 % of its start, the median and the 5 / 95 % months.

The trades' entries are the runs' own; a variant changes only what happens after the fill (or drops the trade), so
it can not see entries the model would have made differently. Read-only."""
from __future__ import annotations

import json
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_chain import chain  # noqa: E402
from backtest_whatif import Ticks, _actions, _rule_actions, bracket  # noqa: E402

GATE = dict(max_risk=15.0, max_lev=60.0, daily=30.0, losses=3)


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _decisions(run: Path) -> dict[str, dict]:
    db = run / "data" / "instances" / "XAUUSD" / "app.db"
    out: dict[str, dict] = {}
    if not db.exists():
        return out
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for row in c.execute("""select d.id, d.session, d.regime, d.htf_bias, d.setup_kinds, d.trigger_strength,
                                       d.confidence, m.mfe_r, m.mae_r
                                from ai_decisions d left join decision_metrics m on m.decision_id = d.id
                                where d.decision in ('BUY', 'SELL')"""):
            out[row[0][:8]] = dict(zip(("id", "session", "regime", "htf", "setup", "trig", "conf", "mfe", "mae"), row))
    finally:
        c.close()
    return out


def load(data: Path, runs: list[str]) -> tuple[list[dict], list[dict]]:
    reports, trades = [], []
    for arg in runs:
        run = Path(arg) if Path(arg).is_dir() else data / "backtests" / arg
        f = run / "report.json"
        if not f.exists():
            print(f"(no report: {arg})", file=sys.stderr)
            continue
        rep = json.loads(f.read_text(encoding="utf-8"))
        reports.append(rep)
        dec, model, rules = _decisions(run), _actions(run), _rule_actions(run)
        for t in rep["trades"]:
            if not t.get("filled") or t.get("open"):
                continue
            d = dec.get(t["id"], {})
            mine = [a for a in model.get(t["id"], []) if a["act"] in ("modify_sl", "close")]
            trades.append({**t, "run": rep["meta"]["run_id"], "day": rep["meta"]["from"][:10], "dec": d,
                           "model_actions": mine, "rule_actions": rules.get(t["id"], []),
                           "hour": int(t["fill_time"][11:13])})
    reports.sort(key=lambda r: r["meta"]["from_ms"])
    trades.sort(key=lambda t: t["fill_time"])
    return reports, trades


def align(t: dict) -> str:
    b = (t["dec"].get("htf") or "").lower()
    if "bear" in b:
        return "with" if t["side"] == "SELL" else "against"
    if "bull" in b:
        return "with" if t["side"] == "BUY" else "against"
    return "neutral"


def hour_bucket(h: int) -> str:
    for lo, hi, name in ((0, 2, "00-02 Asia open"), (2, 4, "02-04 Asia"), (4, 7, "04-07 late Asia"),
                         (7, 10, "07-10 London open"), (10, 12, "10-12 London"), (12, 16, "12-16 New York"),
                         (16, 21, "16-21 NY afternoon"), (21, 24, "21-24 rollover / late")):
        if lo <= h < hi:
            return name
    return "?"


def loss_class(t: dict) -> str:
    mfe = t["dec"].get("mfe")
    if t["pnl_usd"] > 0:
        return "win (target)" if "tp" in t["reasons"] else "win (stop in profit)"
    if mfe is None:
        return "loss (no excursion data)"
    if mfe < 0.3:
        return "loss - wrong from the start (best < 0.3 R)"
    if mfe < 1.0:
        return "loss - went 0.3-1 R our way, then reversed"
    return "loss - reached >= 1 R, then reversed"


def table(rows: list[tuple], head: tuple) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


def agg(trades: list[dict], key) -> list[tuple]:
    g = defaultdict(list)
    for t in trades:
        g[key(t)].append(t)
    rows = []
    for k in sorted(g, key=str):
        ts = g[k]
        w = sum(1 for t in ts if t["pnl_usd"] > 0)
        rows.append((k, len(ts), w, f"{w / len(ts) * 100:.0f} %", f"{sum(t['r'] for t in ts):+.2f}",
                     f"{sum(t['r'] for t in ts) / len(ts):+.2f}", f"{sum(t['pnl_usd'] for t in ts):+.2f}"))
    return rows


def with_trades(reports: list[dict], new: dict[tuple[str, str], dict | None]) -> list[dict]:
    """The reports with some trades replaced (a dict of the changed fields) or dropped (None)."""
    out = []
    for r in reports:
        tr = []
        for t in r["trades"]:
            k = (r["meta"]["run_id"], t["id"])
            if k in new:
                if new[k] is None:
                    continue
                t = {**t, **new[k]}
            tr.append(t)
        out.append({**r, "trades": tr})
    return out


def account(reports: list[dict], equity: float, sizing: str = "fixed") -> dict:
    return chain(reports, equity, GATE["max_risk"], GATE["max_lev"], GATE["daily"], GATE["losses"],
                 sizing=sizing, base=equity)


def halves(reports: list[dict], split: str | None) -> tuple[list[dict], list[dict]]:
    if not split:
        n = len(reports) // 2
        return reports[:n], reports[n:]
    return [r for r in reports if r["meta"]["from"][:10] < split], [r for r in reports if r["meta"]["from"][:10] >= split]


INSAMPLE: set[str] = set()


def variant_row(name: str, reports: list[dict], change: dict, equity: float, split: str | None) -> tuple:
    rs = with_trades(reports, change)
    a, (h1, h2) = account(rs, equity), halves(rs, split)
    b1, b2 = account(h1, equity), account(h2, equity)
    st = account(rs, equity, "step")
    row = (name, a["trades"], f"{(a['win_rate'] or 0) * 100:.0f} %", f"{a['end']:.2f}", f"{a['max_dd_pct']} %",
           f"{b1['end'] - equity:+.2f}", f"{b2['end'] - equity:+.2f}", f"{st['end']:.2f}")
    if INSAMPLE:
        by = dict(a["days"])
        ins = sum(u for d, u in by.items() if d in INSAMPLE)
        row += (f"{ins:+.2f}", f"{sum(by.values()) - ins:+.2f}")
    return row


def bootstrap(day_usd: list[float], equity: float, days: int, n: int = 20000, seed: int = 7) -> dict:
    rnd = random.Random(seed)
    ends, lows70, lows50, losing = [], 0, 0, 0
    for _ in range(n):
        eq, low = equity, equity
        for _ in range(days):
            eq += rnd.choice(day_usd)
            low = min(low, eq)
        ends.append(eq)
        lows70 += low < 0.7 * equity
        lows50 += low < 0.5 * equity
        losing += eq < equity
    ends.sort()
    return {"median": ends[n // 2], "p5": ends[int(n * 0.05)], "p95": ends[int(n * 0.95)],
            "losing": losing / n, "below70": lows70 / n, "below50": lows50 / n}


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    data, equity, split, runs = ROOT / "data", 100.0, None, []
    while argv:
        a = argv.pop(0)
        if a == "--data":
            data = Path(argv.pop(0))
        elif a == "--equity":
            equity = float(argv.pop(0))
        elif a == "--split":
            split = argv.pop(0)
        elif a == "--insample":
            INSAMPLE.update(argv.pop(0).split(","))
        else:
            runs.append(a)
    reports, trades = load(data, runs)
    days = [r["meta"]["from"][:10] for r in reports]
    print(f"# Backtest read-out - {len(reports)} days ({days[0]} … {days[-1]}), {len(trades)} closed trades\n")

    # 1. every trade
    rows = []
    for t in trades:
        d = t["dec"]
        mg = ",".join(sorted({("BE" if r.get("rule") == "move_sl_to_breakeven" else (r.get("rule") or "rule"))
                              for r in t["rule_actions"]} | {f"model {a['act']}" for a in t["model_actions"]})) or "-"
        rows.append((t["fill_time"][5:16].replace("T", " "), t["side"], t["order_type"].replace("_LIMIT", " LMT"),
                     d.get("conf"), (d.get("regime") or "").split("/")[0], align(t), f"{t['fill']:.2f}",
                     f"{abs(t['fill'] - t['sl']):.2f}", f"{t['r']:+.2f}", f"{t['pnl_usd']:+.2f}",
                     "/".join(t["reasons"]), t.get("minutes"),
                     "-" if d.get("mfe") is None else f"{d['mfe']:.2f}", "-" if d.get("mae") is None else f"{d['mae']:.2f}",
                     mg, loss_class(t).replace("loss - ", "").replace("win ", "")))
    print("## 1. Every trade\n")
    print(table(rows, ("filled UTC", "side", "type", "conf", "regime", "4h/1d", "fill", "stop $", "R", "USD", "exit",
                       "min", "best R", "worst R", "management", "class")))

    # 2. breakdowns
    print("\n## 2. Breakdowns\n")
    head = ("group", "trades", "won", "win %", "R", "R / trade", "USD")
    for title, key in (("Loss / win class", loss_class), ("Session (decision)", lambda t: t["session"]),
                       ("Fill hour UTC", lambda t: hour_bucket(t["hour"])), ("Order type", lambda t: t["order_type"]),
                       ("Side", lambda t: t["side"]), ("With / against the 4h-1d bias", align),
                       ("Confidence", lambda t: "< 60" if (t["dec"].get("conf") or 0) < 60 else
                        ("60-64" if t["dec"].get("conf") < 65 else ">= 65")),
                       ("Regime", lambda t: (t["dec"].get("regime") or "?").split("/")[0]),
                       ("Exit", lambda t: "/".join(t["reasons"])),
                       ("Management that acted", lambda t: ("model changed the stop" if t["model_actions"] else
                                                            ("breakeven / trail rule" if t["rule_actions"] else "none"))),
                       ("Weekday", lambda t: datetime.fromisoformat(t["day"]).strftime("%a"))):
        print(f"### {title}\n")
        print(table(agg(trades, key), head) + "\n")
    wins = [t["pnl_usd"] for t in trades if t["pnl_usd"] > 0]
    losses = [t["pnl_usd"] for t in trades if t["pnl_usd"] <= 0]
    pf = sum(wins) / -sum(losses) if losses and sum(losses) < 0 else float("inf")
    print(f"- average win {statistics.mean(wins):+.2f} USD ({len(wins)}), average loss {statistics.mean(losses):+.2f} USD "
          f"({len(losses)}), profit factor {pf:.2f}")
    streak = best = 0
    for t in trades:
        streak = streak + 1 if t["pnl_usd"] <= 0 else 0
        best = max(best, streak)
    print(f"- longest run of losing trades: {best}\n")

    # 3. one account
    print("## 3. One account from", equity, "USD\n")
    acc = {s: account(reports, equity, s) for s in ("fixed", "step", "cont")}
    print(table([(s, a["trades"], f"{(a['win_rate'] or 0) * 100:.0f} %", f"{a['end']:.2f}", f"{a['return_pct']:+.1f} %",
                  f"{a['max_dd_pct']} %", a["worst_day"], a["best_day"], len(a["refused"]))
                 for s, a in acc.items()],
                ("lot", "trades", "won", "end USD", "return", "max DD", "worst day", "best day", "refused")))
    fx = acc["fixed"]
    eq = equity
    drow = []
    by = dict(fx["days"])
    by_step = dict(acc["step"]["days"])
    eq_s = equity
    for day in days:
        eq += by.get(day, 0.0)
        eq_s += by_step.get(day, 0.0)
        drow.append((day, f"{by.get(day, 0.0):+.2f}", f"{eq:.2f}", f"{by_step.get(day, 0.0):+.2f}", f"{eq_s:.2f}"))
    print("\n" + table(drow, ("day", "USD 0.01 lot", "equity", "USD lot by equity", "equity")) + "\n")
    for x in fx["refused"]:
        print(f"- refused at the running equity (0.01 lot): {x[0]} {x[2]} - {x[3]} (its result {x[4]:+.2f})")
    for x in acc["step"]["refused"]:
        print(f"- refused at the running equity (lot by equity): {x[0]} {x[2]} - {x[3]} (its result {x[4]:+.2f})")

    # 4. what-if variants
    ticks = Ticks(data)
    variants: dict[str, dict] = {k: {} for k in ("plain bracket (SL + first TP, no management)", "breakeven at 1 R only",
                                                  "breakeven at 1.5 R only", "breakeven at 2 R only",
                                                  "actual, the model's stop changes undone", "plain, target 1.5 R",
                                                  "plain, target 2 R", "plain, target 3 R")}
    for t in trades:
        k = (t["run"], t["id"])
        side, entry, sl0 = t["side"], t["fill"], t["sl"]
        tp = (t.get("tps") or [None])[0]
        t_in = _ms(t["fill_time"])
        risk = abs(entry - sl0)
        be_stop = entry - 0.5 if side == "SELL" else entry + 0.5

        def put(name, res):
            variants[name][k] = {"pnl_usd": res[1], "close_time": _iso(res[2]), "swap_usd": 0.0,
                                 "r": round(res[1] / risk, 2) if risk else 0.0}

        put("plain bracket (SL + first TP, no management)", bracket(ticks, side, entry, sl0, tp, t_in))
        for r_, name in ((1.0, "breakeven at 1 R only"), (1.5, "breakeven at 1.5 R only"), (2.0, "breakeven at 2 R only")):
            put(name, bracket(ticks, side, entry, sl0, tp, t_in, be=(r_, be_stop)))
        for m, name in ((1.5, "plain, target 1.5 R"), (2.0, "plain, target 2 R"), (3.0, "plain, target 3 R")):
            put(name, bracket(ticks, side, entry, sl0, entry - m * risk if side == "SELL" else entry + m * risk, t_in))
        if t["model_actions"]:
            first = t["model_actions"][0]
            prior = [r for r in t["rule_actions"] if r["ts"] < first["ts"]]
            stop = first["before"] if first["before"] is not None else (prior[-1]["value"] if prior else sl0)
            later_be = next((r for r in t["rule_actions"] if r["ts"] >= first["ts"]
                             and r.get("rule") == "move_sl_to_breakeven"), None)
            be = (float(later_be.get("trigger_value") or 1.0), float(later_be["value"])) if later_be else None
            put("actual, the model's stop changes undone", bracket(ticks, side, entry, stop, tp, first["ts"], be=be))
    filters = {
        "drop fills 04-07 UTC (late Asia)": lambda t: 4 <= t["hour"] < 7,
        "drop MARKET entries (LIMIT only)": lambda t: t["order_type"] == "MARKET",
        "drop confidence < 60": lambda t: (t["dec"].get("conf") or 0) < 60,
        "drop trades against the 4h-1d bias": lambda t: align(t) == "against",
        "drop fills 12-16 UTC (New York open)": lambda t: 12 <= t["hour"] < 16,
    }
    head = ("variant", "trades", "won", "end USD (0.01)", "max DD", f"1st half USD", "2nd half USD", "end (lot by equity)")
    if INSAMPLE:
        head += ("in-sample days USD", "out-of-sample days USD")
    rows = [variant_row("**actual (what the runs did)**", reports, {}, equity, split)]
    rows += [variant_row(name, reports, ch, equity, split) for name, ch in variants.items()]
    rows += [variant_row(name, reports, {(t["run"], t["id"]): None for t in trades if f(t)}, equity, split)
             for name, f in filters.items()]
    combo = {**variants["breakeven at 2 R only"]}
    rows.append(variant_row("breakeven at 2 R only + drop 04-07 UTC", reports,
                            {**combo, **{(t["run"], t["id"]): None for t in trades if 4 <= t["hour"] < 7}}, equity, split))
    h1, h2 = halves(reports, split)
    print(f"\n## 4. What-if (the runs' own entries; halves: {h1[0]['meta']['from'][:10]} … {h1[-1]['meta']['from'][:10]} "
          f"and {h2[0]['meta']['from'][:10]} … {h2[-1]['meta']['from'][:10]})\n")
    print(table(rows, head))
    diffs = []
    for t in trades:
        k = (t["run"], t["id"])
        for name in ("plain bracket (SL + first TP, no management)", "breakeven at 2 R only"):
            v = variants[name].get(k)
            if v and abs(v["pnl_usd"] - t["pnl_usd"]) >= 3:
                diffs.append((name.split(" (")[0], t["fill_time"][5:16], t["side"], f"{t['pnl_usd']:+.2f}",
                              f"{v['pnl_usd']:+.2f}", f"{v['pnl_usd'] - t['pnl_usd']:+.2f}"))
    print("\nThe trades a management variant changes by >= 3 USD:\n")
    print(table(diffs, ("variant", "filled", "side", "actual", "variant", "diff")))

    # 5. bootstrap
    day_usd = [dict(fx["days"]).get(d, 0.0) for d in days]
    print(f"\n## 5. Bootstrap of the {len(days)} days (0.01 lot, from {equity:g} USD, 20,000 draws)\n")
    brow = []
    for n_days, label in ((21, "a month (21 days)"), (63, "a quarter (63 days)")):
        b = bootstrap(day_usd, equity, n_days)
        brow.append((label, f"{b['median']:.0f}", f"{b['p5']:.0f}", f"{b['p95']:.0f}", f"{b['losing'] * 100:.1f} %",
                     f"{b['below70'] * 100:.1f} %", f"{b['below50'] * 100:.1f} %"))
    print(table(brow, ("period", "median end", "5 % worst end", "5 % best end", "ends below start",
                       "falls below 70 %", "falls below 50 %")))
    if INSAMPLE:
        oos = [u for d, u in zip(days, day_usd) if d not in INSAMPLE]
        b = bootstrap(oos, equity, 21)
        print(f"\nOnly the {len(oos)} out-of-sample days: a month's median {b['median']:.0f}, ends below the start "
              f"{b['losing'] * 100:.1f} %, falls below 70 % {b['below70'] * 100:.1f} %")
        best = with_trades(reports, variants["breakeven at 2 R only"])
        bd = dict(account(best, equity)["days"])
        for label, sel in (("all", days), ("out-of-sample", [d for d in days if d not in INSAMPLE])):
            b = bootstrap([bd.get(d, 0.0) for d in sel], equity, 21)
            print(f"\nBreakeven at 2 R only, {label} days: a month's median {b['median']:.0f}, 5 % worst "
                  f"{b['p5']:.0f}, ends below the start {b['losing'] * 100:.1f} %, falls below 70 % "
                  f"{b['below70'] * 100:.1f} %, below 50 % {b['below50'] * 100:.1f} %")
    wo = sorted(day_usd)[:-1]
    b = bootstrap(wo, equity, 21)
    print(f"\nWithout the single best day ({max(day_usd):+.2f}): a month's median {b['median']:.0f}, "
          f"ends below the start {b['losing'] * 100:.1f} %, falls below 70 % {b['below70'] * 100:.1f} %")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
