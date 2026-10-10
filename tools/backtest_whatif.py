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
D. Stop moves only to breakeven or into profit (D-055 candidate): every stop change of the run (the model's and the
   rules') replayed in time order, a change that leaves the stop on the losing side of the fill skipped; after the
   trade's real close the declared breakeven rule still applies (trailing is not replayed after it).
E. A MARKET idea the gate refused only because the touch was outside its entry zone on the BETTER side (a SELL above
   the zone, a BUY below it): filled at that touch, its stop and first target, its declared breakeven rule.
F. A wider entry stop in a volatile market: a trade filled while the 1h ATR percentile was >= 0.8 or the 15m ADX
   was > 40 (its decision's payload) gets a stop at least K x the 15m ATR from the fill (K = 1, 1.25, 1.5; never
   past the 15 % gate at the run's equity), replayed as run (the run's own stop changes and closes at their times,
   a change applied only when tighter) and as a plain bracket; the same widening on EVERY trade as the control.
G. The entry stop beyond the nearest round number: the first multiple of STEP (5, 10, 50 USD) on the risk side of the
   stop (or within BUF of it) moved BUF beyond it, when that is within one 15m ATR of the stop (and inside the gate) -
   replayed as run and as a plain bracket.
``--only D,E`` (or F, G) prints those sections only.

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


BE_BUFFER = 0.3            # the breakeven stop: the fill +- about the spread (the position manager's buffer)


def _db(run: Path) -> sqlite3.Connection | None:
    db = run / "data" / "instances" / "XAUUSD" / "app.db"
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True) if db.exists() else None


def _stop_events(run: Path) -> dict[str, list[tuple[int, str, float | None]]]:
    """Every applied stop change or close of the run by trade (8-char id): (ts, 'sl' | 'close', value)."""
    out: dict[str, list[tuple[int, str, float | None]]] = {}
    c = _db(run)
    if c is None:
        return out
    try:
        for ts, src, sd, act, req in c.execute("select ts, source, source_decision, action, requested from "
                                               "position_actions where status='applied' order by ts"):
            rq = json.loads(req or "{}")
            tgt = (sd or "")[:8] if src == "rule" else ((rq.get("target") or {}).get("decision") or "")[:8]
            if act in ("modify_sl", "set_sl") and rq.get("value") is not None:
                out.setdefault(tgt, []).append((ts, "sl", float(rq["value"])))
            elif act == "close":
                out.setdefault(tgt, []).append((ts, "close", None))
    finally:
        c.close()
    return out


def _declared_be(run: Path) -> dict[str, float]:
    """The R multiple of each executed decision's own breakeven rule (8-char id → R)."""
    out: dict[str, float] = {}
    c = _db(run)
    if c is None:
        return out
    try:
        for did, det in c.execute("select id, execution_detail from ai_decisions where execution_state='executed'"):
            for m in (json.loads(det or "{}").get("executed_management") or []):
                if m.get("action") == "move_sl_to_breakeven" and m.get("trigger") == "r_multiple":
                    out[did[:8]] = float(m.get("value") or 1.0)
    finally:
        c.close()
    return out


def replay(ticks, side: str, entry: float, stop: float, target: float | None, start_ms: int,
           events: list[tuple[int, str, float | None]], *, profit_only: bool, after_ms: int | None = None,
           be_r: float | None = None, horizon_ms: int = HORIZON_MS) -> tuple[str, float, int, int]:
    """The trade from its fill with its recorded stop changes / closes applied at their times (``profit_only``: a
    change that leaves the stop on the losing side of the fill is skipped); from ``after_ms`` (the real close) on, the
    declared breakeven rule ``be_r`` still fires. → (tp | sl | close | open, USD per oz, at ms, changes skipped)."""
    sell = side == "SELL"
    risk = abs(entry - stop)
    evs = sorted(events)
    i = skipped = 0
    last = None
    be_level = entry - BE_BUFFER if sell else entry + BE_BUFFER
    for ts, bid, ask in ticks.walk(start_ms, start_ms + horizon_ms):
        while i < len(evs) and evs[i][0] <= ts:
            _, kind, v = evs[i]
            i += 1
            if kind == "close":
                return "close", round((entry - ask) if sell else (bid - entry), 2), ts, skipped
            if profit_only and ((v > entry) if sell else (v < entry)):
                skipped += 1
                continue
            if (v < stop) if sell else (v > stop):
                stop = v
        px = ask if sell else bid
        last = (ts, px)
        if (px >= stop) if sell else (px <= stop):
            return "sl", round((entry - stop) if sell else (stop - entry), 2), ts, skipped
        if target is not None and ((px <= target) if sell else (px >= target)):
            return "tp", round((entry - target) if sell else (target - entry), 2), ts, skipped
        if (be_r and after_ms is not None and ts >= after_ms and risk > 0
                and ((entry - px) if sell else (px - entry)) >= be_r * risk
                and ((be_level < stop) if sell else (be_level > stop))):
            stop = be_level
    if last is None:
        return "open", 0.0, start_ms, skipped
    return "open", round((entry - last[1]) if sell else (last[1] - entry), 2), last[0], skipped


def section_d(ticks, runs: list[Path]) -> None:
    print("## D. Stop moves only to breakeven or into profit (a loss-side stop change skipped)\n")
    print("| run | trade | filled | side | skipped moves | actual USD | replay as run | profit-only: result | USD | diff |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    by_month: dict[str, list[float]] = {}
    for run in runs:
        rep = json.loads((run / "report.json").read_text(encoding="utf-8"))
        evs, bes = _stop_events(run), _declared_be(run)
        for t in rep["trades"]:
            if not t.get("filled") or t.get("open"):
                continue
            side, entry, sl0 = t["side"], t["fill"], t["sl"]
            tp = (t.get("tps") or [None])[0]
            mine = evs.get(t["id"], [])
            loss_side = [e for e in mine if e[1] == "sl" and ((e[2] > entry) if side == "SELL" else (e[2] < entry))]
            month = t["fill_time"][:7]
            row = by_month.setdefault(month, [0.0, 0.0, 0, 0])
            row[0] += t["pnl_usd"]
            if not loss_side:
                row[1] += t["pnl_usd"]
                continue
            t_in, t_out = _ms(t["fill_time"]), _ms(t["close_time"])
            same = replay(ticks, side, entry, sl0, tp, t_in, mine, profit_only=False)
            res, usd, _, sk = replay(ticks, side, entry, sl0, tp, t_in, mine, profit_only=True, after_ms=t_out,
                                     be_r=bes.get(t["id"]))
            row[1] += usd
            row[2] += 1
            row[3] += res == "tp"
            print(f"| {run.name} | {t['id']} | {t['fill_time'][5:16]} | {side} | {sk} | {t['pnl_usd']:+.2f} | "
                  f"{same[0]} {same[1]:+.2f} | {res} | {usd:+.2f} | {usd - t['pnl_usd']:+.2f} |")
    print("\n| month | trades changed | of which reach the target | actual USD | profit-only USD | diff |")
    print("|---|---|---|---|---|---|")
    for m, (a, w, n, tp_n) in sorted(by_month.items()):
        print(f"| {m} | {n} | {tp_n} | {a:+.2f} | {w:+.2f} | {w - a:+.2f} |")
    print()


def section_e(ticks, runs: list[Path]) -> None:
    print("## E. MARKET ideas refused only for a touch outside the zone on the better side, filled at that touch\n")
    print("| run | idea | at | side | zone | touch | beyond | stop | target | result | USD |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    worse = 0
    by_month: dict[str, list[float]] = {}
    for run in runs:
        c = _db(run)
        if c is None:
            continue
        try:
            rows = c.execute("select id, ts, latency_ms, recommendation, execution_detail from ai_decisions "
                             "where execution_state='rejected' order by ts").fetchall()
        finally:
            c.close()
        for did, ts, lat, rec_j, det_j in rows:
            gate = (json.loads(det_j or "{}").get("gate")) or []
            failed = [g for g in gate if not g.get("ok")]
            if [g["check"] for g in failed] != ["market_in_zone"]:
                continue
            m = re.search(r"touch (-?[\d.]+) vs zone (-?[\d.]+)-(-?[\d.]+)", failed[0].get("detail") or "")
            if not m:
                continue
            px, lo, hi = (float(x) for x in m.groups())
            rec = json.loads(rec_j or "{}")
            side = rec.get("decision")
            better = px > hi if side == "SELL" else px < lo
            if not better:
                worse += 1
                continue
            sl = float(rec["stop_loss"])
            tp = (rec.get("take_profits") or [{}])[0].get("price")
            be_r = next((float(x.get("value") or 1.0) for x in rec.get("management") or []
                         if x.get("action") == "move_sl_to_breakeven" and x.get("trigger") == "r_multiple"), None)
            start = int(ts) + int(lat or 0)
            res, usd, _, _ = replay(ticks, side, px, sl, tp, start, [], profit_only=False, after_ms=start, be_r=be_r)
            month = datetime.fromtimestamp(start / 1000, timezone.utc).strftime("%Y-%m")
            row = by_month.setdefault(month, [0, 0, 0.0])
            row[0] += 1
            row[1] += res == "tp"
            row[2] += usd
            print(f"| {run.name} | {did[:8]} | {datetime.fromtimestamp(start / 1000, timezone.utc):%m-%d %H:%M} | "
                  f"{side} | {lo}-{hi} | {px} | {abs(px - (hi if side == 'SELL' else lo)):.2f} | {sl} | {tp} | "
                  f"{res} | {usd:+.2f} |")
    print("\n| month | ideas | reach the target | USD |")
    print("|---|---|---|---|")
    for mo, (n, w, u) in sorted(by_month.items()):
        print(f"| {mo} | {n} | {w} | {u:+.2f} |")
    print(f"\n(refused for a touch outside the zone on the WORSE side, left refused: {worse})\n")


GATE_PCT = 15.0           # D-052: the risk per trade the gate allows (0.01 lot = 1 oz: USD per oz = USD of the trade)
VOL_K = (1.0, 1.25, 1.5)
ROUND = ((5.0, 0.5), (10.0, 0.5), (10.0, 1.0), (50.0, 0.5))      # (step, buffer)


def _entry_context(run: Path) -> dict[str, dict]:
    """By trade (8-char id): what its decision's payload said at the call - the 15m ATR / ADX, the 1h ATR percentile."""
    import zlib
    out: dict[str, dict] = {}
    c = _db(run)
    if c is None:
        return out
    try:
        for did, pz in c.execute("select d.id, p.payload_z from ai_decisions d join ai_payloads p on "
                                 "p.payload_hash = d.payload_hash where d.decision in ('BUY', 'SELL')"):
            try:
                tfs = json.loads(zlib.decompress(pz)).get("timeframes") or {}
            except (zlib.error, ValueError, TypeError):
                continue
            m15, h1 = tfs.get("15m") or {}, tfs.get("1h") or {}
            out[did[:8]] = {"atr15": (m15.get("indicators") or {}).get("atr14"),
                            "adx15": (m15.get("indicators") or {}).get("adx14"),
                            "pct1h": (h1.get("regime") or {}).get("atr_percentile")}
    finally:
        c.close()
    return out


def _group(name: str) -> str:
    for pre, g in (("hl_", "H live days"), ("h_", "H / G"), ("g_", "H / G"), ("b2_", "B fixed runner"),
                   ("claude_", "B Sept-Oct"), ("old_", "B August"), ("d053_", "D-053"), ("e_", "E")):
        if name.startswith(pre):
            return g
    return "other"


def _fair_runs(data: Path, args: list[str]) -> list[Path]:
    runs = [Path(a) if Path(a).is_dir() else data / "backtests" / a for a in args] if args else \
        sorted(p for p in (data / "backtests").iterdir() if p.is_dir())
    keep = []
    for r in runs:
        f = r / "report.json"
        if not f.exists() or "interrupted" in r.name or r.name.endswith("_A") or "_v1" in r.name:
            continue
        rep = json.loads(f.read_text(encoding="utf-8"))
        if rep["meta"].get("answers") == "claude" and not (rep["meta"].get("result") or {}).get("interrupted"):
            keep.append(r)
    return keep


def _variant_rows(ticks, runs: list[Path], stop_of) -> dict:
    """Every closed trade of ``runs`` replayed as run and as a plain bracket with its entry stop and with
    ``stop_of(trade, ctx, eq0)`` (None = unchanged) → {group: [n changed, as-run base, as-run variant, bracket base,
    bracket variant, actual USD, wins base, wins variant, refused by the gate]} plus "all"."""
    tot: dict[str, list[float]] = {}
    for run in runs:
        rep = json.loads((run / "report.json").read_text(encoding="utf-8"))
        eq0 = rep["meta"].get("equity") or 107.38
        evs, ctxs = _stop_events(run), _entry_context(run)
        g = _group(run.name)
        for t in rep["trades"]:
            if not t.get("filled") or t.get("open"):
                continue
            side, entry, sl0 = t["side"], t["fill"], t["sl"]
            tp = (t.get("tps") or [None])[0]
            t_in = _ms(t["fill_time"])
            new = stop_of(t, ctxs.get(t["id"]) or {}, eq0)
            for key in (g, "all"):
                row = tot.setdefault(key, [0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, 0])
                row[5] += t["pnl_usd"]
            if new == "gate":
                for key in (g, "all"):
                    tot[key][8] += 1
                new = None
            mine = evs.get(t["id"], [])
            base = replay(ticks, side, entry, sl0, tp, t_in, mine, profit_only=False)
            bb = bracket(ticks, side, entry, sl0, tp, t_in)
            if new is None or abs(new - sl0) < 0.005:
                var, bv = base, bb
            else:
                var = replay(ticks, side, entry, new, tp, t_in, mine, profit_only=False)
                bv = bracket(ticks, side, entry, new, tp, t_in)
            for key in (g, "all"):
                row = tot[key]
                row[0] += var is not base
                row[1] += base[1]
                row[2] += var[1]
                row[3] += bb[1]
                row[4] += bv[1]
                row[6] += base[1] > 0
                row[7] += var[1] > 0
    return tot


def _print_variant(title: str, tot: dict) -> None:
    print(f"### {title}\n")
    print("| group | trades changed | actual USD | as run: base | as run: variant | diff | bracket: base | "
          "bracket: variant | diff | wins base → variant | gate kept |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for g in sorted(tot, key=lambda k: (k == "all", k)):
        n, a, av, b, bv, act, wb, wv, gate = tot[g]
        print(f"| {g} | {int(n)} | {act:+.2f} | {a:+.2f} | {av:+.2f} | **{av - a:+.2f}** | {b:+.2f} | {bv:+.2f} | "
              f"{bv - b:+.2f} | {int(wb)} → {int(wv)} | {int(gate)} |")
    print()


def _gate_ok(entry: float, stop: float, eq0: float) -> bool:
    return abs(entry - stop) / eq0 * 100 <= GATE_PCT


def section_f(ticks, runs: list[Path]) -> None:
    print("## F. A wider entry stop in a volatile market (1h ATR percentile >= 0.8 or 15m ADX > 40)\n")
    for only_vol in (True, False):
        for k in VOL_K:
            def stop_of(t, c, eq0, k=k, only_vol=only_vol):
                atr = c.get("atr15")
                if not atr:
                    return None
                vol = (c.get("pct1h") or 0) >= 0.8 or (c.get("adx15") or 0) > 40
                if only_vol and not vol:
                    return None
                sell, entry, sl0 = t["side"] == "SELL", t["fill"], t["sl"]
                if abs(entry - sl0) >= k * atr:
                    return None
                new = entry + k * atr if sell else entry - k * atr
                return round(new, 2) if _gate_ok(entry, new, eq0) else "gate"
            _print_variant(f"stop >= {k:g} x 15m ATR - {'volatile trades only' if only_vol else 'EVERY trade (control)'}",
                           _variant_rows(ticks, runs, stop_of))


def section_g(ticks, runs: list[Path]) -> None:
    import math
    print("## G. The entry stop beyond the nearest round number (within one 15m ATR of the stop)\n")
    for step, buf in ROUND:
        def stop_of(t, c, eq0, step=step, buf=buf):
            atr = c.get("atr15")
            sell, entry, sl0 = t["side"] == "SELL", t["fill"], t["sl"]
            if sell:
                r = math.ceil((sl0 - buf) / step) * step
                new = r + buf
            else:
                r = math.floor((sl0 + buf) / step) * step
                new = r - buf
            if not atr or abs(new - sl0) > atr or abs(new - sl0) < 0.005:
                return None
            if (new < sl0) if sell else (new > sl0):     # never tighter than the model's stop
                return None
            return round(new, 2) if _gate_ok(entry, new, eq0) else "gate"
        _print_variant(f"round step {step:g} USD, {buf:g} beyond", _variant_rows(ticks, runs, stop_of))


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    data = ROOT / "data"
    only = None
    while argv[:1] in (["--data"], ["--only"]):
        if argv[0] == "--data":
            data = Path(argv[1])
        else:
            only = set(argv[1].upper().split(","))
        argv = argv[2:]
    ticks = Ticks(data)
    if only:
        runs = [Path(a) if Path(a).is_dir() else data / "backtests" / a for a in argv]
        runs = [r for r in runs if (r / "report.json").exists()]
        if only & {"F", "G"}:                # no run named: every finished, uninterrupted claude run of the folder
            fair = _fair_runs(data, argv)
            print(f"(runs: {len(fair)} - {', '.join(r.name for r in fair)})\n")
            if "F" in only:
                section_f(ticks, fair)
            if "G" in only:
                section_g(ticks, fair)
        if "D" in only:
            section_d(ticks, runs)
        if "E" in only:
            section_e(ticks, runs)
        return 0
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
