"""What the scored briefs say (D-058): the hit-rate over the last N scored briefs - the "code confidence" the page
shows next to the model's own - and the compact journal the model reads before the next brief, so its confidences
can drift toward the measured rates. Pure functions over stored records (``files.write_record``); a plan a hard check
rejected (``checks[idx].rejected``) and a plan / level the ticks could not decide (``unknown``) never enter a rate."""
from __future__ import annotations

import datetime as dt
from collections import Counter

BUCKETS = ("<50", "50-69", ">=70")
MIN_OBS_N = 8                  # plans behind an observation line before it is written at all
STOP_SMALL_ATR = 0.6           # "a stop below this x ATR5m" observation


def bucket(confidence: int | float | None) -> str:
    c = float(confidence or 0)
    return "<50" if c < 50 else "50-69" if c < 70 else ">=70"


def _rate(hit: int, miss: int) -> float | None:
    return round(hit / (hit + miss), 3) if hit + miss else None


def _scored(records: list[dict]) -> list[dict]:
    """The scored records, newest first (whatever order they came in)."""
    recs = [r for r in records if isinstance(r, dict) and isinstance(r.get("score"), dict)]
    return sorted(recs, key=lambda r: int(r.get("as_of_ms") or 0), reverse=True)


def _plan_rows(rec: dict):
    """(plan as the model wrote it, its score row) for every plan the stats may count."""
    brief = rec.get("brief") or {}
    plans = brief.get("plans") or []
    checks = rec.get("checks") or {}
    for ps in (rec.get("score") or {}).get("plans") or []:
        idx = ps.get("idx")
        if idx is None or idx >= len(plans):
            continue
        if (checks.get(str(idx)) or {}).get("rejected") or ps.get("outcome") in ("rejected", "unknown"):
            continue
        yield plans[idx], ps


class _Tally:
    def __init__(self) -> None:
        self.n = self.triggered = self.target = self.stop = self.expired = 0
        self.sum_r = 0.0

    def add(self, ps: dict) -> None:
        self.n += 1
        if ps.get("outcome") == "not_triggered":
            return
        self.triggered += 1
        self.sum_r += float(ps.get("r") or 0.0)
        if ps["outcome"] == "target":
            self.target += 1
        elif ps["outcome"] == "stop":
            self.stop += 1
        elif ps["outcome"] == "expired":
            self.expired += 1

    def out(self) -> dict:
        return {"n": self.n, "triggered": self.triggered, "target_first": self.target, "stop_first": self.stop,
                "expired": self.expired, "hit_rate": _rate(self.target, self.stop),
                "avg_r": round(self.sum_r / self.triggered, 3) if self.triggered else None,
                "sum_r": round(self.sum_r, 3)}


def _group_out(groups: dict[str, _Tally]) -> dict:
    return {k: {"n": g.target + g.stop, "plans": g.n, "triggered": g.triggered, "hit_rate": _rate(g.target, g.stop)}
            for k, g in groups.items()}


def stats(records: list[dict], n: int = 20) -> dict:
    """The hit-rate over the last ``n`` scored briefs. ``by_confidence[bucket].n`` is the decided sample (target or
    stop first) behind that bucket's rate - the page shows the rate only from 5 on."""
    recs = _scored(records)[:n]
    lv_total = lv_touched = lv_held = 0
    plans = _Tally()
    by_conf = {b: _Tally() for b in BUCKETS}
    by_side: dict[str, _Tally] = {}
    by_entry: dict[str, _Tally] = {}
    manual: dict = {}
    for rec in recs:
        for ls in rec["score"].get("levels") or []:
            if ls.get("outcome") == "unknown":
                continue
            lv_total += 1
            if ls.get("outcome") != "untouched":
                lv_touched += 1
            if ls.get("outcome") == "held":
                lv_held += 1
        for plan, ps in _plan_rows(rec):
            plans.add(ps)
            by_conf[bucket(plan.get("confidence"))].add(ps)
            by_side.setdefault(str(plan.get("side")), _Tally()).add(ps)
            by_entry.setdefault(str(plan.get("entry_type")), _Tally()).add(ps)
        for tr in rec.get("manual") or []:
            key = tr.get("position_id") or (tr.get("open_ms"), tr.get("entry"))
            manual.setdefault(key, tr)
    closed = [t for t in manual.values() if not t.get("open")]
    return {"n_scored": len(recs),
            "levels": {"touched": lv_touched, "total": lv_total, "held": lv_held,
                       "touch_rate": _rate(lv_touched, lv_total - lv_touched),
                       "held_rate": _rate(lv_held, lv_touched - lv_held)},
            "plans": plans.out(),
            "by_confidence": _group_out(by_conf), "by_side": _group_out(by_side), "by_entry_type": _group_out(by_entry),
            "manual": {"n": len(manual), "wins": sum(1 for t in closed if float(t.get("pnl_usd") or 0) > 0),
                       "pnl_usd": round(sum(float(t.get("pnl_usd") or 0) for t in closed), 2),
                       "followed_plan": sum(1 for t in manual.values() if t.get("followed_plan"))}}


# ------------------------------------------------------------------------------------------------- the prompt text
def _hm(ms: int | None) -> str:
    if not ms:
        return "?"
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).strftime("%m-%d %H:%M")


def _r(x: float | None) -> str:
    return "?" if x is None else f"{x:+.1f}R"


def _plan_phrase(plan: dict, ps: dict, rejected: bool) -> str:
    head = f"{plan.get('side')} {float(plan.get('entry') or 0):.1f} c{int(plan.get('confidence') or 0)}"
    if rejected:
        return f"{head} → rejected"
    o = ps.get("outcome")
    if o == "not_triggered":
        return f"{head} → not triggered"
    if o in ("target", "stop", "expired"):
        mins = ps.get("minutes")
        tail = f" in {int(round(mins))}m" if mins is not None else ""
        return f"{head} → {o} {_r(ps.get('r'))}{tail}"
    return f"{head} → {o or 'unknown'}"


def _brief_line(rec: dict) -> str:
    brief, score = rec.get("brief") or {}, rec.get("score") or {}
    checks = rec.get("checks") or {}
    lv = score.get("levels") or []
    touched = sum(1 for x in lv if x.get("outcome") not in ("untouched", "unknown"))
    held = sum(1 for x in lv if x.get("outcome") == "held")
    plans = brief.get("plans") or []
    parts = [_hm(rec.get("as_of_ms")),
             f"{brief.get('regime')}/{brief.get('bias')} {int(brief.get('bias_confidence') or 0)}",
             f"levels {len(lv)}: touched {touched}, held {held}"]
    phrases = []
    for ps in score.get("plans") or []:
        idx = ps.get("idx")
        if idx is None or idx >= len(plans):
            continue
        phrases.append(_plan_phrase(plans[idx], ps, bool((checks.get(str(idx)) or {}).get("rejected"))))
    parts.append("plans: " + ("; ".join(phrases) if phrases else "none"))
    man = [f"{t.get('side')} {float(t.get('entry') or 0):.1f} {float(t.get('pnl_usd') or 0):+.2f}"
           + (" (open)" if t.get("open") else "") for t in rec.get("manual") or []]
    if man:
        parts.append("manual: " + "; ".join(man))
    return " | ".join(parts)


def _manual_line(t: dict) -> str:
    s = f"{_hm(t.get('open_ms'))} {t.get('side')} {float(t.get('entry') or 0):.1f}"
    if t.get("open"):
        return s + " (still open)"
    s += f" → {float(t.get('exit') or 0):.1f} {float(t.get('pnl_usd') or 0):+.2f}"
    return s + (" (plan followed)" if t.get("followed_plan") else "")


def _observations(recs: list[dict]) -> list[str]:
    """Up to three code-derived facts with their thresholds; only from ``MIN_OBS_N`` counted plans on."""
    rows = [(rec, plan, ps) for rec in recs for plan, ps in _plan_rows(rec)]
    decided = [(rec, plan, ps) for rec, plan, ps in rows if ps.get("outcome") in ("target", "stop")]
    if len(rows) < MIN_OBS_N:
        return []
    out = []
    small = [ps for rec, plan, ps in decided if rec.get("atr5m")
             and abs(float(plan["entry"]) - float(plan["stop"])) < STOP_SMALL_ATR * float(rec["atr5m"])]
    if small:
        out.append(f"plans with stop < {STOP_SMALL_ATR}×ATR5m: stop first "
                   f"{sum(1 for ps in small if ps['outcome'] == 'stop')}/{len(small)}")
    kinds: Counter = Counter()
    held: Counter = Counter()
    for rec in recs:
        levels = (rec.get("brief") or {}).get("levels") or []
        for i, ls in enumerate((rec.get("score") or {}).get("levels") or []):
            if i >= len(levels) or ls.get("outcome") in ("untouched", "unknown"):
                continue
            kinds[levels[i].get("kind")] += 1
            held[levels[i].get("kind")] += ls.get("outcome") == "held"
    if kinds:
        k, c = kinds.most_common(1)[0]
        out.append(f"levels of kind {k} held {held[k]}/{c}")
    against = [ps for rec, plan, ps in decided
               if (plan.get("side"), (rec.get("brief") or {}).get("bias")) in {("BUY", "bearish"), ("SELL", "bullish")}]
    if against:
        out.append(f"plans against bias hit {sum(1 for ps in against if ps['outcome'] == 'target')}/{len(against)}")
    return out[:3]


def journal_text(records: list[dict], manual: list[dict], *, n_briefs: int, max_chars: int = 2400) -> str:
    """The journal the model reads (English, numbers only): one stats line, one line per recent scored brief, the
    last manual trades, then the observations. Inserted into the prompt as a VALUE, never re-parsed."""
    recs = _scored(records)
    if not recs and not manual:
        return "No scored briefs yet (the first briefs are scored one hour after they are written)."
    st = stats(records)
    p = st["plans"]
    bc = st["by_confidence"]
    lines = [f"Last {st['n_scored']} scored briefs: levels touched {st['levels']['touched']}/{st['levels']['total']}, "
             f"held {st['levels']['held']}/{st['levels']['touched']}; plans {p['n']}, triggered {p['triggered']}, "
             f"target first {p['target_first']}, stop first {p['stop_first']}, expired {p['expired']}, "
             f"hit-rate {_pct(p['hit_rate'])}, avg {_r(p['avg_r'])}, sum {_r(p['sum_r'])}.",
             "Hit-rate by your confidence: "
             + ", ".join(f"{b}: {_pct(bc[b]['hit_rate'])} (n={bc[b]['n']})" for b in BUCKETS)]
    if n_briefs > 0 and recs:
        lines.append("Recent briefs (time UTC | regime/bias conf | levels | plans | your manual trades):")
        lines.extend(_brief_line(r) for r in recs[:n_briefs])
    closed = sorted((t for t in manual if isinstance(t, dict)), key=lambda t: int(t.get("open_ms") or 0),
                    reverse=True)[:5]
    if closed:
        lines.append("Last manual trades:")
        lines.extend(_manual_line(t) for t in closed)
    obs = _observations(recs[:20])
    if obs:
        lines.append("Observations: " + "; ".join(obs) + ".")
    return _clip("\n".join(lines), max_chars)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _clip(text: str, max_chars: int) -> str:
    """Whole lines only, never a cut in the middle of a number."""
    if len(text) <= max_chars:
        return text
    out: list[str] = []
    size = 0
    for line in text.split("\n"):
        if size + len(line) + 1 > max_chars - 2:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out) + "\n…"
