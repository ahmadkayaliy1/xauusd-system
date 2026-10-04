"""P12.7 backtest report (plan B8): every trade of a run with its entry / exit ticks, R, USD and the management that
acted; the equity curve, max drawdown, P&L by day and by session; the calls and tokens; and, for a run that replayed
the real system's own decisions (``answers: production``), the comparison with what that system recorded.

Swap: the paper backend books none, so the report adds it per position for every broker rollover held (server
midnight, the server on the EU summer-time rule - Europe/Athens offsets, D-014; Wednesday's counts three times), at the
contract's swap in points (1 point = tick_size; tick value = contract_size x tick_size per lot).
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

from ..core.settings import Settings
from ..core.timeutil import iso

SERVER_TZ = ZoneInfo("Europe/Athens")    # UTC+2 / +3 on the EU rule = the Windsor server clock (D-014)
WEEKDAY = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def _q(db: Path, sql: str, args=()) -> list[tuple]:
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def rollovers(open_ms: int, close_ms: int, triple_weekday: int | None) -> int:
    """Swap nights held between two instants: server midnights crossed, the triple day's counting three."""
    a = dt.datetime.fromtimestamp(open_ms / 1000, SERVER_TZ)
    b = dt.datetime.fromtimestamp(close_ms / 1000, SERVER_TZ)
    n, d = 0, a.date()
    while d < b.date():
        n += 3 if triple_weekday is not None and d.weekday() == triple_weekday else 1
        d += dt.timedelta(days=1)
    return n


def trades(s: Settings, pair: str, app_db: Path) -> list[dict]:
    inst = next(i for i in s.pairs[pair].instruments if "execution" in i.roles)
    c = inst.contract
    tick_value = (c.contract_size * c.tick_size) if c else 1.0
    triple = WEEKDAY.get(c.triple_swap_weekday) if c and c.triple_swap_weekday else None
    decs = {r[0]: r for r in _q(app_db, "SELECT id, ts, decision, order_type, confidence, session, trigger_strength, "
                                        "execution_detail, latency_ms FROM ai_decisions WHERE pair=? AND "
                                        "execution_state='executed' ORDER BY ts", (pair,))}
    legs = defaultdict(list)
    for r in _q(app_db, "SELECT decision_id, side, volume, contract_size, fill_price, fill_ms, sl, tp, close_price, "
                        "close_ms, close_reason, pnl_usd, status, order_type, created_ms FROM paper_legs"):
        legs[r[0]].append(r)
    out = []
    for did, (_, ts, side, otype, conf, sess, strength, det, lat) in decs.items():
        d = json.loads(det) if det else {}
        lv = d.get("executed_levels") or {}
        ls = legs.get(did, [])
        filled = [x for x in ls if x[5] is not None]
        t = {"id": did[:8], "decision_ts": ts, "side": side, "order_type": otype, "confidence": conf, "session": sess,
             "strength": strength, "latency_s": round((lat or 0) / 1000), "entry_plan": lv.get("entry"),
             "sl": lv.get("stop_loss"), "tps": lv.get("take_profits"), "risk_pct_at_entry": d.get("risk_pct"),
             "equity_at_entry": d.get("equity_at_entry"), "filled": bool(filled)}
        if not filled:
            t.update(result="not filled", pnl_usd=0.0, r=0.0, swap_usd=0.0)
            out.append(t)
            continue
        vol = sum(x[2] for x in filled)
        fill = sum(x[4] * x[2] for x in filled) / vol
        fill_ms = min(x[5] for x in filled)
        closed = [x for x in filled if x[9] is not None]
        open_left = [x for x in filled if x[9] is None]
        pnl = sum(x[11] or 0.0 for x in closed)
        close_ms = max((x[9] for x in closed), default=None)
        risk_usd = abs(fill - float(lv.get("stop_loss") or fill)) * vol * filled[0][3]
        swap_pts = (c.swap_long if side == "BUY" else c.swap_short) if c else 0.0
        nights = rollovers(fill_ms, close_ms, triple) if close_ms else 0
        swap = (swap_pts or 0.0) * tick_value * vol * nights
        t.update(fill=round(fill, 2), fill_time=iso(fill_ms), volume=round(vol, 2),
                 close_time=iso(close_ms) if close_ms else None,
                 exit=round(sum((x[8] or 0) * x[2] for x in closed) / max(sum(x[2] for x in closed), 1e-12), 2)
                 if closed else None,
                 reasons=sorted({x[10] or "?" for x in closed}), open=bool(open_left),
                 minutes=round((close_ms - fill_ms) / 60_000) if close_ms else None,
                 pnl_usd=round(pnl, 2), swap_usd=round(swap, 2), nights=nights,
                 risk_usd=round(risk_usd, 2), r=round((pnl + swap) / risk_usd, 2) if risk_usd else None,
                 result="open at the end" if open_left else ("win" if pnl + swap > 0 else "loss"))
        out.append(t)
    return out


def summary(tr: list[dict], equity0: float) -> dict:
    done = [t for t in tr if t.get("filled") and not t.get("open")]
    eq, peak, mdd, curve = equity0, equity0, 0.0, []
    for t in sorted(done, key=lambda x: x["close_time"]):
        eq += t["pnl_usd"] + t["swap_usd"]
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100 if peak else 0.0)
        curve.append((t["close_time"], round(eq, 2)))
    by_day, by_sess = defaultdict(float), defaultdict(list)
    for t in done:
        by_day[t["close_time"][:10]] += t["pnl_usd"] + t["swap_usd"]
        by_sess[t.get("session") or "?"].append(t["r"] or 0.0)
    rs = [t["r"] for t in done if t["r"] is not None]
    wins = [x for x in rs if x > 0]
    return {"trades": len(done), "open_at_end": sum(1 for t in tr if t.get("open")),
            "not_filled": sum(1 for t in tr if not t.get("filled")),
            "wins": len(wins), "win_rate": round(len(wins) / len(rs), 3) if rs else None,
            "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None, "total_r": round(sum(rs), 2),
            "net_usd": round(eq - equity0, 2), "end_equity": round(eq, 2),
            "return_pct": round((eq - equity0) / equity0 * 100, 2), "max_drawdown_pct": round(mdd, 2),
            "by_day_usd": {k: round(v, 2) for k, v in sorted(by_day.items())},
            "by_session_r": {k: {"n": len(v), "avg_r": round(sum(v) / len(v), 3)} for k, v in sorted(by_sess.items())},
            "curve": curve}


def calls(app_db: Path, ledger: Path, pair: str) -> dict:
    dec = _q(app_db, "SELECT status, decision, count(*) FROM ai_decisions WHERE pair=? GROUP BY 1, 2", (pair,))
    rej = defaultdict(int)
    for (det,) in _q(app_db, "SELECT execution_detail FROM ai_decisions WHERE pair=? AND execution_state='rejected'",
                     (pair,)):
        d = json.loads(det) if det else {}
        for g in d.get("gate") or []:
            if not g.get("ok"):
                rej[g.get("check")] += 1
    led = _q(ledger, "SELECT count(*), sum(input_tokens), sum(output_tokens), sum(cached_tokens), avg(latency_ms) "
                     "FROM ai_usage")
    n, tin, tout, tcache, lat = (led[0] if led else (0, 0, 0, 0, None))
    return {"decisions": {f"{st}/{d or '-'}": k for st, d, k in dec}, "gate_rejections": dict(rej),
            "ledger_calls": n or 0, "input_tokens": tin or 0, "output_tokens": tout or 0, "cached_tokens": tcache or 0,
            "avg_latency_s": round((lat or 0) / 1000, 1)}


def calibration(app_db: Path, source_db: Path, pair: str, start: int, end: int) -> list[dict]:
    """For ``answers: production``: each real decision of the period, what the real system recorded (its execution
    state and virtual outcome in R) beside what the run did with the same answer."""
    src = _q(source_db, "SELECT id, ts, decision, execution_state, virtual_outcome, virtual_r FROM ai_decisions "
                        "WHERE pair=? AND status='valid' AND decision IN ('BUY','SELL') AND ts>=? AND ts<? ORDER BY ts",
             (pair, start, end))
    run = {r[0]: r for r in _q(app_db, "SELECT id, execution_state, execution_detail FROM ai_decisions WHERE pair=?",
                                (pair,))}
    out = []
    for did, ts, side, st, vo, vr in src:
        r = run.get(did)
        det = json.loads(r[2]) if r and r[2] else {}
        failed = [g.get("check") for g in det.get("gate") or [] if not g.get("ok")]
        out.append({"id": did[:8], "time": iso(ts), "side": side, "real_state": st, "real_virtual": vo,
                    "real_virtual_r": vr, "run_state": r[1] if r else None, "run_failed": failed})
    return out


def coverage(s: Settings, pair: str, start: int, end: int) -> dict:
    """How much of the period the replay had market data for: the minutes the execution venue's calendar calls open
    and, of those, the minutes with at least one tick of the execution instrument in the run's store (a system outage
    in the real recording is a hole here: no fill, no stop, no screen can happen inside it)."""
    import numpy as np

    from ..core.instruments import InstrumentRegistry
    from ..core.sessions import calendar_for
    from ..storage.sqlite_store import SQLiteHotStore
    from ..storage.tablespec import spec_for
    reg = InstrumentRegistry.from_settings(s)
    exe = reg.with_role(pair, "execution")[0]
    cal = calendar_for(exe.venue, exe.symbol, s.pairs[pair].asset_class)
    open_m = [m for m in range(start - start % 60_000, end, 60_000) if cal.is_open(m)]
    path = exe.hot_db_path(s.paths.data())
    if not path.exists() or "ticks" not in exe.datatypes:
        return {"open_minutes": len(open_m), "minutes_with_ticks": None, "share": None, "holes": []}
    hot = SQLiteHotStore(path, readonly=True)
    try:
        t = hot.read_range(spec_for(exe, "ticks"), start, end, ["time_msc"])["time_msc"].astype(np.int64)
    finally:
        hot.close()
    have = set((t - t % 60_000).tolist())
    holes, first, last = [], None, None
    for m in open_m:                                    # open stretches without a tick, longer than 15 minutes
        if m in have:
            if first is not None and last - first >= 15 * 60_000:
                holes.append([iso(first), iso(last + 60_000)])
            first = None
        else:
            first = m if first is None else first
            last = m
    if first is not None and last - first >= 15 * 60_000:
        holes.append([iso(first), iso(last + 60_000)])
    covered = sum(1 for m in open_m if m in have)
    return {"open_minutes": len(open_m), "minutes_with_ticks": covered,
            "share": round(covered / len(open_m), 3) if open_m else None, "holes": holes}


def render(meta: dict, tr: list[dict], summ: dict, cl: dict, cal: list[dict] | None, cov: dict | None = None) -> str:
    L = [f"# Backtest {meta['run_id']}", "",
         f"Period {meta['from']} → {meta['to']} · pair {meta['pair']} · answers `{meta['answers']}` · start equity "
         f"${meta['equity']:.2f} · brief `{meta.get('brief')}` · news blackout {'on (archived real calendar)' if meta['news'] else 'OFF'} · "
         f"{meta['result'].get('stopped_by') or 'ran to the end'} · {meta['result'].get('real_s')} s real time.", "",
         *([f"**INTERRUPTED - not a clean sample** (computer asleep ≈ {meta['result'].get('suspended_s')} s, "
            f"{meta['result'].get('error_cycles')} cycle(s) without an answer): re-run before using it.", ""]
           if meta['result'].get('interrupted') else []),
         "Paper execution on the real recorded ticks (bid/ask), the production engine, gate and management on a "
         "simulated clock; the answer of a call reaches the executor after its real latency. Swap added per "
         "rollover held. A small sample proves nothing about an edge.", "",
         *([f"Market data: {cov['minutes_with_ticks']} of {cov['open_minutes']} open minutes had ticks "
            f"(share {cov['share']}); holes > 15 min: {cov['holes'] or 'none'}", ""] if cov else []),
         "## Result", ""]
    for k in ("trades", "open_at_end", "not_filled", "wins", "win_rate", "expectancy_r", "total_r", "net_usd",
              "end_equity", "return_pct", "max_drawdown_pct"):
        L.append(f"- {k}: **{summ[k]}**")
    L += ["", f"P&L by day (USD): {summ['by_day_usd']}", "", f"By session (R): {summ['by_session_r']}", "",
          "## Trades", "",
          "| id | decided | side | type | fill | at | exit | at | reasons | min | nights | R | USD | swap |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for t in tr:
        L.append(f"| {t['id']} | {iso(t['decision_ts'])[5:16]} | {t['side']} | {t['order_type']} | {t.get('fill', '-')} "
                 f"| {(t.get('fill_time') or '-')[5:19]} | {t.get('exit', '-')} | {(t.get('close_time') or '-')[5:19]} "
                 f"| {','.join(t.get('reasons') or [t['result']])} | {t.get('minutes', '-')} | {t.get('nights', 0)} "
                 f"| {t.get('r', '-')} | {t.get('pnl_usd', 0)} | {t.get('swap_usd', 0)} |")
    L += ["", "## Calls", "", f"- decisions: {cl['decisions']}", f"- gate rejections: {cl['gate_rejections']}",
          f"- model calls: {cl['ledger_calls']} · input {cl['input_tokens']:,} (cached {cl['cached_tokens']:,}) · "
          f"output {cl['output_tokens']:,} · avg latency {cl['avg_latency_s']} s"]
    if cal is not None:
        L += ["", "## Calibration against the real system's records", "",
              "| id | time | side | real state | real virtual | R | run state | run gate failed |", "|---|---|---|---|---|---|---|---|"]
        for c in cal:
            L.append(f"| {c['id']} | {c['time'][5:16]} | {c['side']} | {c['real_state']} | {c['real_virtual']} | "
                     f"{c['real_virtual_r']} | {c['run_state']} | {','.join(c['run_failed']) or '-'} |")
    return "\n".join(L) + "\n"


def write(run_root: Path, s: Settings, meta: dict, source_db: Path | None = None) -> dict:
    pair = meta["pair"]
    app_db = s.paths.state() / "app.db"
    tr = trades(s, pair, app_db)
    summ = summary(tr, meta["equity"])
    cl = calls(app_db, s.paths.shared() / "ai_usage.db", pair)
    cal = calibration(app_db, source_db, pair, meta["from_ms"], meta["to_ms"]) \
        if meta["answers"] == "production" and source_db else None
    cov = coverage(s, pair, meta["from_ms"], meta["to_ms"])
    doc = {"meta": meta, "coverage": cov, "summary": summ, "trades": tr, "calls": cl, "calibration": cal}
    (run_root / "report.json").write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8", newline="\n")
    (run_root / "report.md").write_text(render(meta, tr, summ, cl, cal, cov), encoding="utf-8", newline="\n")
    return doc
