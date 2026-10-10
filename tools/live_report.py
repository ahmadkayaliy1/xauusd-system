"""The live desk's daily report in Arabic (the owner reads it), from this system's own database - no model call:

    python tools/live_report.py                      # yesterday (UTC), written to data/reviews/<stamp>_live.md
    python tools/live_report.py --day 2026-10-12 --print

The day's trades (entry, stop, target, how each ended, R and USD, every stop change, the model's own review), the
ideas the gate refused and why, the calls, the outages (sleeps, disconnects, restarts), the equity now and the running
total since D-055 (2026-10-09 09:46 UTC). The file name is one the dashboard's Reviews tab lists (kind ``live``)."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradingsystem.ai.store import journal_from  # noqa: E402
from tradingsystem.core.settings import load_settings  # noqa: E402

PAIR = "XAUUSD"
D055_MS = int(dt.datetime(2026, 10, 9, 9, 46, tzinfo=dt.timezone.utc).timestamp() * 1000)   # H (D-055) live
DAY_MS = 86_400_000
EXIT_AR = {"sl": "الوقف", "tp": "الهدف", "manual": "إغلاق يدوي", "close": "إغلاق من النموذج", "flat": "إغلاق قبل العطلة",
           "other": "غير ذلك"}
SIDE_AR = {"BUY": "شراء", "SELL": "بيع"}


def _ms(day: dt.date) -> int:
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _hm(iso_or_ms) -> str:
    if isinstance(iso_or_ms, (int, float)):
        return dt.datetime.fromtimestamp(iso_or_ms / 1000, dt.timezone.utc).strftime("%H:%M")
    return str(iso_or_ms or "")[11:16]


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)


def report(db: Path, day: dt.date) -> str:
    t0, t1 = _ms(day), _ms(day) + DAY_MS
    con = _ro(db)
    try:
        j = journal_from(con, PAIR, t1 - 1, 24, 50, 20)
        trades = [t for t in j.get("trades") or [] if t0 <= _ms(dt.date.fromisoformat(t["placed"][:10])) < t1]
        calls = con.execute("SELECT status, decision, latency_ms, trigger_strength FROM ai_decisions WHERE pair=? AND "
                            "ts>=? AND ts<?", (PAIR, t0, t1)).fetchall()
        refused = con.execute("SELECT ts, decision, order_type, execution_detail FROM ai_decisions WHERE pair=? AND "
                              "ts>=? AND ts<? AND execution_state='rejected' ORDER BY ts", (PAIR, t0, t1)).fetchall()
        try:
            events = Counter(r[0] for r in con.execute("SELECT event FROM ingestion_events WHERE ts>=? AND ts<?",
                                                       (t0, t1)))
            sleeps = con.execute("SELECT ts, detail FROM ingestion_events WHERE ts>=? AND ts<? AND "
                                 "event='system_suspend'", (t0, t1)).fetchall()
        except sqlite3.OperationalError:           # a database without the ingestion tables (tests, a backtest run)
            events, sleeps = Counter(), []
        since = con.execute("SELECT count(*), sum(outcome='closed_profit'), round(coalesce(sum(outcome_pnl_usd), 0), 2) "
                            "FROM ai_decisions WHERE pair=? AND execution_state='executed' AND outcome LIKE 'closed%' "
                            "AND ts>=?", (PAIR, D055_MS)).fetchone()
        try:
            ex = json.loads(con.execute("SELECT detail FROM collector_status WHERE collector='executor'").fetchone()[0])
        except (TypeError, ValueError, sqlite3.OperationalError):
            ex = {}
    finally:
        con.close()
    closed = [t for t in trades if t.get("status") == "closed"]
    pnl = round(sum(t.get("pnl_usd") or 0.0 for t in closed), 2)
    wins = sum((t.get("pnl_usd") or 0) > 0 for t in closed)
    out = [f"# تقرير الحي اليومي - الدهب XAUUSD - {day.isoformat()}", "",
           "## الخلاصة",
           f"- الصفقات اللي انفتحت اليوم: **{len(trades)}** (تسكّرت {len(closed)}، ربحت {wins}، خسرت {len(closed) - wins}).",
           f"- ربح/خسارة الصفقات المسكّرة: **{pnl:+.2f} $**.",
           f"- الرصيد هلق: **{ex.get('equity', '—')} $** (مفتوح: {ex.get('open_positions', '—')} صفقة، "
           f"{ex.get('open_orders', '—')} أمر معلّق).",
           f"- من بداية النسخة H (9/10): {since[0] or 0} صفقة مسكّرة، ربحت {since[1] or 0}، المجموع **{since[2] or 0:+.2f} $**.",
           ""]
    out += ["## الصفقات", ""]
    if not trades:
        out.append("ما في صفقات اليوم.")
    else:
        out += ["| الوقت | الاتجاه | الدخول | الوقف / الهدف | النتيجة | R | $ | تحريك الوقف |", "|---|---|---|---|---|---|---|---|"]
        for t in trades:
            res = (f"{EXIT_AR.get(t.get('exit'), t.get('exit'))} {_hm(t.get('closed'))}" if t.get("status") == "closed"
                   else {"open": "لسا مفتوحة", "pending": "أمر معلّق"}.get(t.get("status"), t.get("status")))
            moves = "; ".join(f"{_hm(c['at'])} {c['stop'][0]}→{c['stop'][1]}" for c in t.get("changes") or [] if c.get("stop"))
            out.append(f"| {_hm(t['placed'])} | {SIDE_AR.get(t['side'], t['side'])} {t.get('order_type')} | {t.get('entry')} | "
                       f"{t.get('stop')} / {t.get('target')} | {res} | {t.get('r', '—')} | "
                       f"{'' if t.get('pnl_usd') is None else format(t['pnl_usd'], '+.2f')} | {moves or '—'} |")
        reviews = [t for t in trades if t.get("review")]
        if reviews:
            out += ["", "### مراجعة النموذج لكل صفقة"]
            for t in reviews:
                rv = t["review"]
                out.append(f"- **{_hm(t['placed'])} {SIDE_AR.get(t['side'], t['side'])}**: السبب `{rv.get('cause', '—')}`"
                           + (f" - الدرس: {rv['lesson']}" if rv.get("lesson") else "")
                           + (f" - الفكرة لسا صالحة، رجوع عند: {rv.get('re_entry')}" if rv.get("idea_alive") else ""))
    out += ["", "## أفكار رفضها نظام المخاطر", ""]
    if not refused:
        out.append("ولا فكرة انرفضت.")
    for ts, side, ot, det in refused:
        try:
            gate = [g for g in (json.loads(det or "{}").get("gate") or []) if not g.get("ok")]
        except ValueError:
            gate = []
        why = "; ".join(f"{g.get('check')} ({g.get('detail')})" for g in gate) or "—"
        out.append(f"- {_hm(ts)} {SIDE_AR.get(side, side)} {ot}: {why}")
    lat = [r[2] for r in calls if r[2]]
    by = Counter(r[1] for r in calls if r[0] == "valid")
    out += ["", "## الأسئلة للذكاء",
            f"- عدد الأسئلة: **{len(calls)}** (صالحة {sum(r[0] == 'valid' for r in calls)}، أخطاء "
            f"{sum(r[0] in ('error', 'interrupted') for r in calls)}).",
            f"- الأجوبة: لا صفقة {by.get('NO_TRADE', 0)}، شراء {by.get('BUY', 0)}، بيع {by.get('SELL', 0)}"
            + (f"، إدارة {sum(v for k, v in by.items() if k not in ('NO_TRADE', 'BUY', 'SELL'))}" if
               any(k not in ('NO_TRADE', 'BUY', 'SELL') for k in by) else "") + ".",
            f"- متوسط وقت الجواب: {round(sum(lat) / len(lat) / 1000) if lat else '—'} ثانية.",
            "", "## الانقطاعات",
            f"- نوم الجهاز: {len(sleeps)} مرة" + (" (" + ", ".join(f"{_hm(ts)}" for ts, _ in sleeps) + ")" if sleeps else "") + ".",
            f"- انقطاع اتصال بمصادر البيانات (بيرجع لحالو): {events.get('disconnect', 0)} مرة.",
            ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--day", help="UTC day YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--print", action="store_true", help="print only, write no file")
    ap.add_argument("--db", help="another app.db (read only; e.g. a backtest run's)")
    a = ap.parse_args(argv)
    day = dt.date.fromisoformat(a.day) if a.day else (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).date()
    s = load_settings(extra_env={"TS_INSTANCE": PAIR})
    db = Path(a.db) if a.db else s.paths.state() / "app.db"
    if not db.exists():
        print(f"live_report: no database at {db}", file=sys.stderr)
        return 1
    text = report(db, day)
    if a.print:
        print(text)
        return 0
    out = s.paths.data() / "reviews"
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"{day.strftime('%Y%m%d')}T235959Z_live.md"
    f.write_text(text + "\n", encoding="utf-8", newline="\n")
    print(f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
