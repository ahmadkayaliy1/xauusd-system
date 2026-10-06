"""Model comparison on identical inputs (P12.7): the same decision moments of finished backtest runs, the same stored
payload and the charts re-drawn as of the same instant, asked to another model - no day is replayed.

    python tools/model_probe.py select RUN [RUN ...]          → data/backtests/probes/moments.json
    python tools/model_probe.py ask --model M --label L [--only trades|all] [--max N]
                                                               → data/backtests/probes/<L>/answers.jsonl (resumable)
    python tools/model_probe.py evaluate [--label L ...]       → markdown on stdout

``select`` takes every BUY/SELL decision of the runs (filled winners, losers, never filled) and a stratified sample of
their NO_TRADE moments (half where the price moved >= MOVE_USD within 2 h after, half random; at most 2 a day).
``ask`` builds the production orchestrator on a probe root (its own market copy, paper only, notifications off) with
the decision model ``M`` and the runs' brief, and asks each moment once with the stored payload (account, history,
memory as they were). ``evaluate`` scores every answer the same way on the recorded ticks - the original model's
answers included: the entry as ordered (MARKET at the first tick after the answer, a LIMIT / STOP only if touched
before valid_until), the stop and first target as given, then the plain bracket and the breakeven-at-2R variant
(24 h horizon), at 0.01 lot; a stop wider than 15 % of the moment's equity or a reward below 1.5 R is refused as
the gate would."""
from __future__ import annotations

import asyncio
import json
import os
import random
import sqlite3
import sys
import time
import zlib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_whatif import Ticks, bracket  # noqa: E402

DATA = Path(os.environ.get("TS_PROBE_DATA", r"C:\TradingSystems\XAUUSD\data"))
PROBES = DATA / "backtests" / "probes"
MOVE_USD = 15.0
PER_DAY = 2
NOT_FILLED = 20                     # a sample of the never-filled orders (they cost a call each, teach less)
SEED = 11
HORIZON_MS = 24 * 3_600_000


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _db(run: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{run / 'data' / 'instances' / 'XAUUSD' / 'app.db'}?mode=ro", uri=True)


# ---------------------------------------------------------------------------------------------------------- select
def select(runs: list[str], *, seed: int = SEED, out_name: str = "moments.json", exclude: set | None = None,
           not_filled: int = NOT_FILLED, n_big: int = 15, n_plain: int = 15, settled: bool = True) -> None:
    """``exclude``: moment ids already used elsewhere (a hold-out set never overlaps them); ``settled`` False
    leaves out the filled trades (winners / losers / open)."""
    exclude = exclude or set()
    ticks = Ticks(DATA)
    trades, quiet = [], []
    for name in runs:
        run = DATA / "backtests" / name
        rep = json.loads((run / "report.json").read_text(encoding="utf-8"))
        result = {t["id"]: t for t in rep["trades"]}
        con = _db(run)
        try:
            rows = con.execute("select id, ts, decision, payload_hash, trigger, trigger_strength, setup_strength, "
                               "recommendation, latency_ms, model from ai_decisions where status='valid' "
                               "order by ts").fetchall()
        finally:
            con.close()
        con = _db(run)
        try:
            eq_of = {}
            for ph, z in con.execute("select payload_hash, payload_z from ai_payloads"):
                try:
                    eq_of[ph] = float((json.loads(zlib.decompress(z)).get("account") or {}).get("equity") or 107.38)
                except (ValueError, zlib.error, TypeError):
                    pass
        finally:
            con.close()
        for did, ts, dec, ph, trig, ts_, ss, rec, lat, model in rows:
            m = {"run": name, "day": rep["meta"]["from"][:10], "id": did, "ts": ts, "payload_hash": ph,
                 "equity": eq_of.get(ph, 107.38),
                 "trigger": trig, "strength": ts_, "setup": ss, "model": model, "latency_ms": lat,
                 "original": json.loads(rec) if rec else None}
            if did in exclude:
                continue
            if dec in ("BUY", "SELL"):
                t = result.get(did[:8])
                m["group"] = ("not filled" if not t or not t.get("filled") else
                              "open at the end" if t.get("open") else ("winner" if t["pnl_usd"] > 0 else "loser"))
                m["actual_usd"] = None if not t or not t.get("filled") or t.get("open") else t["pnl_usd"]
                trades.append(m)
            elif dec == "NO_TRADE":
                quiet.append(m)
    rnd = random.Random(seed)
    if not settled:
        trades = [m for m in trades if m["group"] == "not filled"]
    nf = [m for m in trades if m["group"] == "not filled"]
    rnd.shuffle(nf)
    keep = {m["id"] for m in nf[:not_filled]}
    trades = [m for m in trades if m["group"] != "not filled" or m["id"] in keep]
    rnd.shuffle(quiet)
    big, plain, per_day = [], [], defaultdict(int)
    for m in quiet:
        if per_day[m["day"]] >= PER_DAY:
            continue
        hi = lo = None
        p0 = None
        for _, bid, ask in ticks.walk(m["ts"], m["ts"] + 2 * 3_600_000):
            mid = (bid + ask) / 2
            p0 = mid if p0 is None else p0
            hi = mid if hi is None else max(hi, mid)
            lo = mid if lo is None else min(lo, mid)
        if p0 is None:
            continue
        moved = max(hi - p0, p0 - lo)
        if moved >= MOVE_USD and len(big) < n_big:
            m["group"] = "no trade, big move after"
            big.append(m)
            per_day[m["day"]] += 1
        elif moved < MOVE_USD and len(plain) < n_plain:
            m["group"] = "no trade, random"
            plain.append(m)
            per_day[m["day"]] += 1
        if len(big) >= n_big and len(plain) >= n_plain:
            break
    out = trades + big + plain
    PROBES.mkdir(parents=True, exist_ok=True)
    (PROBES / out_name).write_text(json.dumps(out, indent=1), encoding="utf-8")
    g = defaultdict(int)
    for m in out:
        g[m["group"]] += 1
    print(f"{len(out)} moments → {PROBES / out_name}: {dict(g)}")


# ---------------------------------------------------------------------------------------------------------- ask
def _probe_settings(root: Path, end: int, brief: str, model: str | None, confirmer: str | None = None):
    """The run settings of a probe root: paper, notifications off, the decision model ``model`` and, for a
    confirm run, the escalation step on for every trade idea with ``confirmer``."""
    from tradingsystem.backtest.runner import RunConfig, run_settings
    rc = RunConfig(start_ms=end, end_ms=end + 1, pair="XAUUSD", brief=brief, ai={"daily_calls_per_pair": 5000})
    s = run_settings(ROOT / "config" / "config.yaml", root, rc)
    prov = s.ai.providers["claude_code"].model_copy(update={"rpd": 5000})
    models = s.ai.models
    if model:
        models = models.model_copy(update={"decision": models.decision.model_copy(update={"model": model})})
    upd = {"providers": {**s.ai.providers, "claude_code": prov}, "models": models}
    if confirmer:
        upd["models"] = models.model_copy(update={"escalation": models.escalation.model_copy(
            update={"model": confirmer, "effort": "medium"})})
        upd["escalation"] = s.ai.escalation.model_copy(update={
            "enabled": True, "on_strength": ["strong", "weak", "review", "event", "idle", "close", "manual"],
            "min_confidence": 0, "max_per_day_per_pair": 100000, "on_failure": "withhold", "timeout_s": 300})
    return s.model_copy(update={"ai": s.ai.model_copy(update=upd)})


def _market(s, label: str, last: int, end: int) -> None:
    from tradingsystem.backtest.market import prepare
    hot = s.paths.data() / "hot" / "mt5" / "XAUUSD.db"
    if hot.exists():                                     # a copy that ends before the last moment is no copy
        con = sqlite3.connect(f"file:{hot}?mode=ro", uri=True)
        try:
            top = con.execute("select max(open_time) from xauusd_candles_5m").fetchone()[0] or 0
        finally:
            con.close()
        if top < last - 4 * 86_400_000:
            raise RuntimeError(f"{label}: its market copy ends {top} before the last moment {last} - delete "
                               f"{s.paths.data()} and run again")
    else:
        prepare(s, DATA, s.paths.data(), end)            # one market copy as of the day after the last moment


def _payload(m: dict) -> dict | None:
    con = _db(DATA / "backtests" / m["run"])
    try:
        z = con.execute("select payload_z from ai_payloads where payload_hash=?", (m["payload_hash"],)).fetchone()
    finally:
        con.close()
    return json.loads(zlib.decompress(z[0])) if z else None


async def _engine(s):
    from tradingsystem.analysis.engine import Engine
    from tradingsystem.supervisor.winops import keep_awake
    keep_awake(True)
    os.environ["TS_NOTIFY_DISABLE"] = "1"
    os.environ["TS_NEWS_NO_FETCH"] = "1"
    eng = Engine(s)
    t0 = time.time()
    while not eng.ai_ready() and time.time() - t0 < 180:
        await asyncio.sleep(1.0)
    if not eng.ai_ready():
        raise RuntimeError(f"the AI provider is not ready: {eng._ai_problem}")
    return eng


async def _confirm(confirmer: str, label: str, proposer: str, brief: str, moments_name: str) -> None:
    """The escalation step of production (orchestrator._escalate, the 'escalation' prompt + ``brief``) on the stored
    trade ideas of the ``proposer`` label: ``confirmer`` confirms each unchanged or downgrades it to NO_TRADE. A
    moment where the proposer had no trade is written as its NO_TRADE without a call."""
    from tradingsystem.ai.orchestrator import _dump
    from tradingsystem.ai.store import DecisionRecord
    from tradingsystem.core import timeutil
    from tradingsystem.core.tunables import tunables_of

    moments = json.loads((PROBES / moments_name).read_text(encoding="utf-8"))
    last = max(m["ts"] for m in moments)
    src = PROBES / proposer / "answers.jsonl"
    props = {r["id"]: r for r in (json.loads(x) for x in src.read_text(encoding="utf-8").splitlines() if x.strip())
             if r["status"] == "valid"}
    root = PROBES / label
    root.mkdir(parents=True, exist_ok=True)
    done_f = root / "answers.jsonl"
    done = {json.loads(x)["id"] for x in done_f.read_text(encoding="utf-8").splitlines() if x.strip()} \
        if done_f.exists() else set()
    todo = [m for m in moments if m["id"] in props and m["id"] not in done]
    print(f"{label}: {len(todo)} to review ({len(done)} done) - {proposer} proposes, {confirmer} confirms", flush=True)
    if not todo:
        return
    end = (last // 86_400_000 + 1) * 86_400_000
    s = _probe_settings(root, end, brief, None, confirmer)
    _market(s, label, last, end)
    clock = {"t": todo[0]["ts"]}
    timeutil.set_sim_clock(lambda: clock["t"])
    eng = await _engine(s)
    orch = eng.orch
    for k, m in enumerate(todo, 1):
        while (PROBES / "PAUSE").exists():
            await asyncio.sleep(30)
        prop = props[m["id"]]
        rec0 = prop.get("recommendation") or {}
        row = {"id": m["id"], "label": label, "model": confirmer, "proposer": proposer, "asked_ms": prop["asked_ms"],
               "latency_ms": prop.get("latency_ms")}
        if rec0.get("decision") not in ("BUY", "SELL"):
            row.update(status="valid", recommendation=rec0, verdict="no proposal")
        else:
            payload = _payload(m)
            as_of = _ms(payload["meta"]["as_of"])
            clock["t"] = as_of
            reason = m["trigger"] or "probe"
            tn = tunables_of(orch, "XAUUSD")
            images, note = await orch.charts_for("XAUUSD", as_of, payload)
            uv = orch._user_vars("XAUUSD", as_of, reason, _dump(payload), account=payload.get("account"), tn=tn,
                                 charts_note=note)
            rec = DecisionRecord("XAUUSD", "agent_per_pair", reason, "valid", recommendation=dict(rec0))
            rec.trigger_strength = m["strength"] if m["strength"] in s.ai.escalation.on_strength else "review"
            t1 = time.time()
            await orch._escalate(rec, "XAUUSD", payload, reason, as_of, payload.get("account") or {}, uv, images,
                                 setup=m["setup"], tn=tn)
            errs = " ".join(rec.errors)
            verdict = ("confirm" if "confirmed" in errs else "downgrade" if "downgraded" in errs else
                       "failed: " + errs[:200])
            row.update(status="valid" if not verdict.startswith("failed") else "error",
                       recommendation=rec.recommendation, verdict=verdict,
                       review_s=round(time.time() - t1, 1))
        with done_f.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        print(f"  [{k}/{len(todo)}] {m['day']} {m['group']}: {row['verdict'][:60]}", flush=True)
    timeutil.set_sim_clock(None)


async def _ask(model: str, label: str, only: str, max_n: int | None, brief: str = "desks/xau_live_b",
               moments_name: str = "moments.json") -> None:
    from tradingsystem.ai.orchestrator import CycleRequest
    from tradingsystem.backtest.market import prepare
    from tradingsystem.backtest.runner import RunConfig, run_settings
    from tradingsystem.core import timeutil

    moments = json.loads((PROBES / moments_name).read_text(encoding="utf-8"))
    last = max(m["ts"] for m in moments)                 # the market copy always covers every moment
    if only == "trades":
        moments = [m for m in moments if not m["group"].startswith("no trade")]
    if max_n:
        moments = moments[:max_n]
    root = PROBES / label
    root.mkdir(parents=True, exist_ok=True)
    done_f = root / "answers.jsonl"
    done = {json.loads(x)["id"] for x in done_f.read_text(encoding="utf-8").splitlines() if x.strip()} \
        if done_f.exists() else set()
    todo = [m for m in moments if m["id"] not in done]
    print(f"{label}: {len(todo)} to ask ({len(done)} done) with {model}", flush=True)
    if not todo:
        return
    end = (last // 86_400_000 + 1) * 86_400_000
    rc = RunConfig(start_ms=end, end_ms=end + 1, pair="XAUUSD", brief=brief,
                   ai={"daily_calls_per_pair": 5000})
    s = run_settings(ROOT / "config" / "config.yaml", root, rc)
    prov = s.ai.providers["claude_code"].model_copy(update={"rpd": 5000})
    models = s.ai.models.model_copy(update={"decision": s.ai.models.decision.model_copy(update={"model": model})})
    s = s.model_copy(update={"ai": s.ai.model_copy(update={"providers": {**s.ai.providers, "claude_code": prov},
                                                           "models": models})})
    hot = s.paths.data() / "hot" / "mt5" / "XAUUSD.db"
    if hot.exists():                                     # a copy that ends before the last moment is no copy
        con = sqlite3.connect(f"file:{hot}?mode=ro", uri=True)
        try:
            top = con.execute("select max(open_time) from xauusd_candles_5m").fetchone()[0] or 0
        finally:
            con.close()
        if top < last - 4 * 86_400_000:
            raise RuntimeError(f"{label}: its market copy ends {top} before the last moment {last} - delete "
                               f"{s.paths.data()} and run again")
    else:
        prepare(s, DATA, s.paths.data(), end)            # one market copy as of the day after the last moment
    from tradingsystem.supervisor.winops import keep_awake
    keep_awake(True)                                     # no idle sleep while the probe runs
    clock = {"t": todo[0]["ts"]}
    timeutil.set_sim_clock(lambda: clock["t"])
    os.environ["TS_NOTIFY_DISABLE"] = "1"
    os.environ["TS_NEWS_NO_FETCH"] = "1"
    from tradingsystem.analysis.engine import Engine
    eng = Engine(s)
    t0 = time.time()
    while not eng.ai_ready() and time.time() - t0 < 180:
        await asyncio.sleep(1.0)
    if not eng.ai_ready():
        raise RuntimeError(f"the AI provider is not ready: {eng._ai_problem}")
    for k, m in enumerate(todo, 1):
        while (PROBES / "PAUSE").exists():             # the owner's plan window: no new call while paused
            await asyncio.sleep(30)
        src = DATA / "backtests" / m["run"]
        con = _db(src)
        try:
            z = con.execute("select payload_z from ai_payloads where payload_hash=?", (m["payload_hash"],)).fetchone()
        finally:
            con.close()
        if z is None:
            print(f"  {m['id'][:8]}: no stored payload - skipped", flush=True)
            continue
        payload = json.loads(zlib.decompress(z[0]))
        as_of = _ms(payload["meta"]["as_of"])
        clock["t"] = as_of
        t1 = time.time()
        recs = await eng.orch.run_cycle([CycleRequest("XAUUSD", m["trigger"] or "probe", m["strength"], m["setup"])],
                                        as_of=as_of, account=payload.get("account"), payloads={"XAUUSD": payload})
        r = recs[0] if recs else None
        row = {"id": m["id"], "label": label, "model": model, "asked_ms": as_of,
               "status": getattr(r, "status", None), "recommendation": getattr(r, "recommendation", None),
               "latency_ms": getattr(r, "latency_ms", None) or int((time.time() - t1) * 1000),
               "input_tokens": getattr(r, "input_tokens", None), "output_tokens": getattr(r, "output_tokens", None),
               "served_model": getattr(r, "model", None), "errors": getattr(r, "errors", None)}
        with done_f.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        dec = (row["recommendation"] or {}).get("decision")
        print(f"  [{k}/{len(todo)}] {m['day']} {m['group']}: {row['status']} {dec} "
              f"({row['latency_ms'] / 1000:.0f} s, {row['served_model']})", flush=True)
    timeutil.set_sim_clock(None)


# ---------------------------------------------------------------------------------------------------------- evaluate
def _fill(ticks, rec: dict, start_ms: int, until_ms: int):
    """(fill price, fill ms) of a recommendation's entry, or None (not filled / no entry)."""
    side = rec.get("decision")
    ot = (rec.get("order_type") or "MARKET").upper()
    e = rec.get("entry") or {}
    lo, hi = e.get("range_min"), e.get("range_max")
    px = e.get("price")                  # the executor's rule: a range's near edge (BUY: its top, SELL: its bottom)
    if px is None and lo is not None:
        px = hi if side == "BUY" else lo
    for ts, bid, ask in ticks.walk(start_ms, until_ms):
        if ot == "MARKET":
            p = ask if side == "BUY" else bid
            if lo is not None and hi is not None and not (lo <= p <= hi):
                return None                  # the gate's market_in_zone: the price left the zone while asking
            return p, ts
        if px is None:
            return None
        if ot == "BUY_LIMIT" and ask <= px or ot == "SELL_LIMIT" and bid >= px:
            return float(px), ts
        if ot == "BUY_STOP" and ask >= px or ot == "SELL_STOP" and bid <= px:
            return float(px), ts
    return None


def score(ticks, rec: dict | None, asked_ms: int, latency_ms: int, equity: float) -> dict:
    """One answer on the recorded ticks: no_trade | refused | not filled | (plain, be2r) USD at 0.01 lot."""
    if not rec or rec.get("decision") not in ("BUY", "SELL"):
        return {"kind": "no_trade", "plain": 0.0, "be2r": 0.0}
    side, sl = rec["decision"], rec.get("stop_loss")
    tps = [t.get("price") for t in rec.get("take_profits") or [] if t.get("price") is not None]
    if sl is None or not tps:
        return {"kind": "refused", "why": "no stop or target", "plain": 0.0, "be2r": 0.0}
    try:
        until = _ms(rec.get("valid_until")) if rec.get("valid_until") else asked_ms + 3_600_000
    except ValueError:
        until = asked_ms + 3_600_000
    f = _fill(ticks, rec, asked_ms + int(latency_ms or 0), max(until, asked_ms + int(latency_ms or 0) + 60_000))
    if f is None:
        return {"kind": "not filled", "plain": 0.0, "be2r": 0.0}
    entry, t_in = f
    risk = abs(entry - float(sl))
    rr = abs(float(tps[0]) - entry) / risk if risk else 0.0
    if risk <= 0 or risk / equity * 100 > 15.0:
        return {"kind": "refused", "why": f"risk {risk / equity * 100:.1f} %", "plain": 0.0, "be2r": 0.0}
    if rr < 1.5:
        return {"kind": "refused", "why": f"reward {rr:.2f} R", "plain": 0.0, "be2r": 0.0}
    plain = bracket(ticks, side, entry, float(sl), float(tps[0]), t_in)
    be = bracket(ticks, side, entry, float(sl), float(tps[0]), t_in,
                 be=(2.0, entry - 0.5 if side == "SELL" else entry + 0.5))
    return {"kind": "trade", "side": side, "entry": entry, "plain": plain[1], "be2r": be[1], "res": plain[0]}


def evaluate(labels: list[str], moments_name: str = "moments.json") -> None:
    ticks = Ticks(DATA)
    moments = {m["id"]: m for m in json.loads((PROBES / moments_name).read_text(encoding="utf-8"))}
    answers = {"Sonnet 5 (original)": {mid: {"recommendation": m["original"], "asked_ms": m["ts"],
                                              "latency_ms": m["latency_ms"]} for mid, m in moments.items()}}
    cost = {}
    for lab in labels:
        f = PROBES / lab / "answers.jsonl"
        rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] if f.exists() else []
        answers[lab] = {r["id"]: r for r in rows if r["status"] == "valid"}
        ok = [r for r in rows if r["status"] == "valid"]
        led = PROBES / lab / "data" / "shared" / "ai_usage.db"
        calls = tin = tout = tcache = lat = 0
        served = set()
        if led.exists():
            con = sqlite3.connect(f"file:{led}?mode=ro", uri=True)
            try:
                for n, a, b, c_, l_, mdl in con.execute(
                        "select count(*), sum(input_tokens), sum(output_tokens), sum(cached_tokens), "
                        "sum(latency_ms), model from ai_usage group by model"):
                    calls += n
                    tin += a or 0
                    tout += b or 0
                    tcache += c_ or 0
                    lat += l_ or 0
                    served.add(mdl)
            finally:
                con.close()
        cost[lab] = (len(rows), len(ok), calls, tin / max(1, calls), tout / max(1, calls), tcache / max(1, calls),
                     lat / max(1, calls) / 1000, sorted(x for x in served if x))
    common = set(moments)
    for lab in labels:
        common &= set(answers[lab])
    eqs = {mid: float(moments[mid].get("equity") or 107.38) for mid in common}
    scored = {lab: {mid: score(ticks, answers[lab][mid].get("recommendation"), answers[lab][mid].get("asked_ms")
                               or moments[mid]["ts"], answers[lab][mid].get("latency_ms") or 0, eqs[mid])
                    for mid in common} for lab in answers}
    print(f"# Model comparison on {len(common)} identical moments\n")
    print("| label | answers | valid | model calls (ledger) | avg input tokens | avg output tokens | avg cached | "
          "avg latency s | served |")
    print("|---|---|---|---|---|---|---|---|---|")
    for lab, c in cost.items():
        print(f"| {lab} | {c[0]} | {c[1]} | {c[2]} | {c[3]:,.0f} | {c[4]:,.0f} | {c[5]:,.0f} | {c[6]:.0f} | "
              f"{', '.join(c[7])} |")
    groups = sorted({moments[m]["group"] for m in common})
    combos = {}
    labs = list(answers)
    if len(labs) >= 3:                    # the two-model gates: the first asked model proposes, the second confirms
        a, b = labs[1], labs[2]
        for nm, (prop, conf) in ((f"{a} proposes, {b} confirms", (a, b)), (f"{b} proposes, {a} confirms", (b, a))):
            combos[nm] = {}
            for mid in common:
                sp, sc = scored[prop][mid], scored[conf][mid]
                dp = (answers[prop][mid].get("recommendation") or {}).get("decision")
                dc = (answers[conf][mid].get("recommendation") or {}).get("decision")
                combos[nm][mid] = sp if (sp["kind"] == "trade" and dp == dc) else {"kind": "vetoed", "plain": 0.0,
                                                                                  "be2r": 0.0}
    allv = {**scored, **combos}
    print("\n## By group (USD at 0.01 lot: plain bracket / breakeven at 2 R; trades taken · won)\n")
    head = "| model | " + " | ".join(groups) + " | **total** |"
    print(head)
    print("|" + "---|" * (len(groups) + 2))
    for lab, sc in allv.items():
        cells = []
        for g in groups + ["*"]:
            ids = [m for m in common if g == "*" or moments[m]["group"] == g]
            tr = [sc[m] for m in ids if sc[m]["kind"] == "trade"]
            w = sum(1 for x in tr if x["plain"] > 0)
            cells.append(f"{sum(sc[m]['plain'] for m in ids):+.0f} / {sum(sc[m]['be2r'] for m in ids):+.0f} "
                         f"({len(tr)}·{w})")
        print(f"| {lab} | " + " | ".join(cells) + " |")
    print("\n## Agreement with the original Sonnet 5 decision (same decision: BUY / SELL / NO_TRADE)\n")
    print("| model | same | trade where S5 traded | no trade where S5 traded | trade where S5 did not |")
    print("|---|---|---|---|---|")
    for lab in labels:
        same = sum(1 for m in common if (answers[lab][m].get("recommendation") or {}).get("decision")
                   == (moments[m]["original"] or {}).get("decision"))
        s5t = [m for m in common if (moments[m]["original"] or {}).get("decision") in ("BUY", "SELL")]
        kept = sum(1 for m in s5t if (answers[lab][m].get("recommendation") or {}).get("decision") in ("BUY", "SELL"))
        new = sum(1 for m in common if m not in s5t and
                  (answers[lab][m].get("recommendation") or {}).get("decision") in ("BUY", "SELL"))
        print(f"| {lab} | {same}/{len(common)} | {kept}/{len(s5t)} | {len(s5t) - kept}/{len(s5t)} | {new} |")
    print("\n## Every moment\n")
    print("| day | group | S5 actual run USD | " + " | ".join(allv) + " |")
    print("|" + "---|" * (len(allv) + 3))
    for mid in sorted(common, key=lambda x: moments[x]["ts"]):
        m = moments[mid]
        cells = []
        for lab, sc in allv.items():
            x = sc[mid]
            cells.append(x["kind"] if x["kind"] != "trade" else f"{x['side'][0]} {x['plain']:+.1f}/{x['be2r']:+.1f}")
        au = m.get("actual_usd")
        print(f"| {datetime.fromtimestamp(m['ts'] / 1000, timezone.utc).strftime('%m-%d %H:%M')} | {m['group']} | "
              f"{'-' if au is None else f'{au:+.2f}'} | " + " | ".join(cells) + " |")


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "select":
        kw, runs = {}, []
        while rest:
            k = rest.pop(0)
            if k == "--seed":
                kw["seed"] = int(rest.pop(0))
            elif k == "--out":
                kw["out_name"] = rest.pop(0)
            elif k == "--exclude":
                kw["exclude"] = {m["id"] for f in rest.pop(0).split(",")
                                 for m in json.loads((PROBES / f).read_text(encoding="utf-8"))}
            elif k == "--not-filled":
                kw["not_filled"] = int(rest.pop(0))
            elif k == "--big":
                kw["n_big"] = int(rest.pop(0))
            elif k == "--random":
                kw["n_plain"] = int(rest.pop(0))
            elif k == "--unsettled-only":
                kw["settled"] = False
            else:
                runs.append(k)
        select(runs, **kw)
        return 0
    if cmd == "ask":
        opts = {"--model": None, "--label": None, "--only": "all", "--max": None, "--brief": "desks/xau_live_b",
                "--moments": "moments.json"}
        while rest:
            k = rest.pop(0)
            opts[k] = rest.pop(0)
        asyncio.run(_ask(opts["--model"], opts["--label"], opts["--only"], int(opts["--max"]) if opts["--max"] else None,
                         brief=opts["--brief"], moments_name=opts["--moments"]))
        return 0
    if cmd == "confirm":
        opts = {"--model": None, "--label": None, "--proposer": None, "--brief": "desks/xau_live_b",
                "--moments": "moments.json"}
        while rest:
            k = rest.pop(0)
            opts[k] = rest.pop(0)
        asyncio.run(_confirm(opts["--model"], opts["--label"], opts["--proposer"], opts["--brief"], opts["--moments"]))
        return 0
    if cmd == "evaluate":
        labels = [rest[i + 1] for i in range(0, len(rest), 2) if rest[i] == "--label"]
        mom = next((rest[i + 1] for i in range(0, len(rest), 2) if rest[i] == "--moments"), "moments.json")
        evaluate(labels, mom)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
