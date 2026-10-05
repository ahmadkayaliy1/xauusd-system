"""What-if on the trades of finished backtest runs, on the real recorded ticks (P12.7): ``python tools/backtest_whatif.py
RUN [RUN ...]`` → two read-only answers in markdown, no model call:

A. The model's own stop changes and closes undone: for each trade the model tightened (``modify_sl``) or closed, the
   same trade from the model's first such action on, with the stop it had just before and the first target, plus the
   run's own breakeven rule if it would have fired - which came first, the target or that stop (24 h horizon)?
   Trailing rules are not replayed here (the result says so).
B. The entry stop wider by Y: every filled trade as a plain bracket (its entry stop + Y, its first target, no
   management), Y = 0, 1, 2, 3, 5 USD per oz - the wins, losses and USD of each, and how many a wider stop would have
   pushed past the 15 % gate at the run's equity.
C. The plain bracket with only a breakeven rule (the stop to entry +- 0.5 once the move reaches 1, 1.5 or 2 R).

A SELL's stop and target trigger on the ask, a BUY's on the bid (the paper backend's rule); the exit price is the
level itself (no slippage)."""
from __future__ import annotations

import bisect
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HORIZON_MS = 24 * 3_600_000
WIDER = (0.0, 1.0, 2.0, 3.0, 5.0)
BE_AT = (1.0, 1.5, 2.0)


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


class Ticks:
    """The recorded ticks of the pair's execution venue, day files loaded on demand (read-only)."""

    def __init__(self, data: Path):
        self.dir = data / "cold" / "mt5" / "XAUUSD" / "xauusd_ticks"
        self.days: dict[str, tuple[list[int], list[float], list[float]]] = {}

    def _day(self, day: str):
        if day not in self.days:
            f = self.dir / day[:4] / f"{day}.parquet"
            if f.exists():
                import pyarrow.parquet as pq
                t = pq.read_table(f, columns=["time_msc", "bid", "ask"]).sort_by("time_msc")
                self.days[day] = (t["time_msc"].to_pylist(), t["bid"].to_pylist(), t["ask"].to_pylist())
            else:
                self.days[day] = ([], [], [])
        return self.days[day]

    def walk(self, start_ms: int, end_ms: int):
        d = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
        while datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000 < end_ms:
            ts, bid, ask = self._day(d.isoformat())
            for k in range(bisect.bisect_left(ts, start_ms), len(ts)):
                if ts[k] > end_ms:
                    return
                yield ts[k], bid[k], ask[k]
            d += timedelta(days=1)


def bracket(ticks, side: str, entry: float, stop: float, target: float | None, start_ms: int,
            be: tuple[float, float] | None = None, horizon_ms: int = HORIZON_MS) -> tuple[str, float, int]:
    """First of the target or the stop from ``start_ms`` (a SELL on the ask, a BUY on the bid); ``be`` = (the R
    multiple, the breakeven stop) moves the stop once the move reaches it. → (tp | sl | open, USD per oz, at ms)."""
    sell = side == "SELL"
    risk = abs(entry - stop)
    last = None
    for ts, bid, ask in ticks.walk(start_ms, start_ms + horizon_ms):
        px = ask if sell else bid
        last = (ts, px)
        if (px >= stop) if sell else (px <= stop):
            return "sl", round((entry - stop) if sell else (stop - entry), 2), ts
        if target is not None and ((px <= target) if sell else (px >= target)):
            return "tp", round((entry - target) if sell else (target - entry), 2), ts
        if be and risk > 0 and ((entry - px) if sell else (px - entry)) >= be[0] * risk:
            better = (be[1] < stop) if sell else (be[1] > stop)
            if better:
                stop = be[1]
            be = None
    if last is None:
        return "open", 0.0, start_ms
    return "open", round((entry - last[1]) if sell else (last[1] - entry), 2), last[0]


def _actions(run: Path) -> dict[str, list[dict]]:
    db = run / "data" / "instances" / "XAUUSD" / "app.db"
    out: dict[str, list[dict]] = {}
    if not db.exists():
        return out
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for ts, src, act, st, req, det in c.execute(
                "select ts, source, action, status, requested, detail from position_actions order by ts"):
            if st != "applied":
                continue
            rq, dt_ = json.loads(req or "{}"), json.loads(det or "{}")
            tgt = ((rq.get("target") or {}).get("decision") or rq.get("decision") or "")[:8] or None
            if src == "rule":
                tgt = None
            cur = None
            for ch in dt_.get("checks") or []:
                m = re.search(r"current (-?[\d.]+)", ch.get("detail") or "") if ch.get("check") == "tighter_only" else None
                if m:
                    cur = float(m.group(1))
            out.setdefault(tgt or "?", []).append({"ts": ts, "src": src, "act": act, "value": rq.get("value"),
                                                    "before": rq.get("sl_before", cur), "rule": rq.get("rule"),
                                                    "trigger_value": rq.get("trigger_value"), "req": rq})
    finally:
        c.close()
    return out


def _rule_actions(run: Path) -> dict[str, list[dict]]:
    """The rule-driven stop moves by trade: position_actions.source_decision names the trade."""
    db = run / "data" / "instances" / "XAUUSD" / "app.db"
    out: dict[str, list[dict]] = {}
    if not db.exists():
        return out
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for ts, sd, req in c.execute("select ts, source_decision, requested from position_actions "
                                     "where source='rule' and status='applied' order by ts"):
            rq = json.loads(req or "{}")
            out.setdefault((sd or "")[:8], []).append({"ts": ts, **rq})
    finally:
        c.close()
    return out


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    data = ROOT / "data"
    if argv[:1] == ["--data"]:
        data, argv = Path(argv[1]), argv[2:]
    ticks = Ticks(data)
    a_rows, b_tot = [], {y: {"tp": 0, "sl": 0, "open": 0, "usd": 0.0, "gate": 0} for y in WIDER}
    c_tot = {r: {"tp": 0, "sl": 0, "open": 0, "usd": 0.0, "be": 0} for r in BE_AT}
    for arg in argv:
        run = Path(arg) if Path(arg).is_dir() else data / "backtests" / arg
        f = run / "report.json"
        if not f.exists():
            print(f"(no report: {arg})", file=sys.stderr)
            continue
        rep = json.loads(f.read_text(encoding="utf-8"))
        eq0 = rep["meta"].get("equity") or 107.38
        model, rules = _actions(run), _rule_actions(run)
        for t in rep["trades"]:
            if not t.get("filled") or t.get("open"):
                continue
            side, entry, sl0 = t["side"], t["fill"], t["sl"]
            tp = (t.get("tps") or [None])[0]
            t_in = _ms(t["fill_time"])
            for y in WIDER:                                          # B: the entry stop wider by y, no management
                stop = sl0 + y if side == "SELL" else sl0 - y
                if abs(entry - stop) / eq0 * 100 > 15.0:
                    b_tot[y]["gate"] += 1
                    continue
                res, usd, _ = bracket(ticks, side, entry, stop, tp, t_in)
                b_tot[y][res] += 1
                b_tot[y]["usd"] += usd
            for r in BE_AT:                                          # C: the plain bracket + breakeven only
                be_stop = entry - 0.5 if side == "SELL" else entry + 0.5
                res, usd, _ = bracket(ticks, side, entry, sl0, tp, t_in, be=(r, be_stop))
                c_tot[r][res] += 1
                c_tot[r]["usd"] += usd
                c_tot[r]["be"] += res == "sl" and abs(usd - 0.5) < 1e-6
            mine = [a for a in model.get(t["id"], []) if a["act"] in ("modify_sl", "close")]
            if not mine:
                continue
            first = mine[0]                                          # A: the model's actions undone
            prior = [r for r in rules.get(t["id"], []) if r["ts"] < first["ts"]]
            stop = first["before"] if first["before"] is not None else (prior[-1]["value"] if prior else sl0)
            later_be = next((r for r in rules.get(t["id"], []) if r["ts"] >= first["ts"]
                             and r.get("rule") == "move_sl_to_breakeven"), None)
            be = (float(later_be.get("trigger_value") or 1.0), float(later_be["value"])) if later_be else None
            res, usd, at = bracket(ticks, side, entry, stop, tp, first["ts"], be=be)
            a_rows.append((rep["meta"]["run_id"], t["id"], t["fill_time"][5:16], side, first["act"],
                           datetime.fromtimestamp(first["ts"] / 1000, timezone.utc).strftime("%H:%M"),
                           t["pnl_usd"], res, usd, t["reasons"]))
    print("## A. The model's stop changes / closes undone (from its first one; trailing rules not replayed)\n")
    print("| run | trade | filled | side | model action | at | actual USD | without: result | USD | diff |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    tot_a, tot_w = 0.0, 0.0
    for r in a_rows:
        tot_a += r[6]
        tot_w += r[8]
        print(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]:+.2f} | {r[7]} | {r[8]:+.2f} | "
              f"{r[8] - r[6]:+.2f} |")
    print(f"\n**{len(a_rows)} trades: actual {tot_a:+.2f} USD, without the model's actions {tot_w:+.2f} USD "
          f"(diff {tot_w - tot_a:+.2f})**\n")
    print("## B. Plain bracket (entry stop + Y, first target, no management)\n")
    print("| Y (USD/oz) | target | stop | open 24 h | refused by the 15 % gate | USD |")
    print("|---|---|---|---|---|---|")
    for y in WIDER:
        b = b_tot[y]
        print(f"| +{y:g} | {b['tp']} | {b['sl']} | {b['open']} | {b['gate']} | {b['usd']:+.2f} |")
    print("\n## C. Plain bracket + breakeven only (stop to entry +- 0.5 at the R multiple)\n")
    print("| breakeven at | target | stop (of which at breakeven) | open 24 h | USD |")
    print("|---|---|---|---|---|")
    for r in BE_AT:
        c = c_tot[r]
        print(f"| {r:g} R | {c['tp']} | {c['sl']} ({c['be']}) | {c['open']} | {c['usd']:+.2f} |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
