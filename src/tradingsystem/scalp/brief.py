"""One scalp brief (D-058): the desk's own payload and charts as inputs, the scalp prompt pair, ONE validated call to
``ai.models.scalp`` (role ``scalp``, no pair - outside the desk's daily caps), the stored record and the Arabic
markdown the owner reads (the toast / Telegram text is its first 600 characters). Nothing here sends an order,
writes the desk's state or touches ``ai_decisions``; the engine's chart folder is never written (the brief renders
its own three images into ``data/scalp/charts``)."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..ai.budget import CostGovernor, RateLimiter, UsageStore
from ..ai.providers import ImageInput, ProviderError, make_provider
from ..ai.repair import Generation, generate_validated
from ..core.instruments import InstrumentRegistry
from ..core.settings import Settings
from ..core.timeutil import iso
from .contract import RECORD_VERSION, ScalpBrief
from .files import brief_id, scalp_dir
from .prompts import prompt_hash, render_scalp
from .safety import SAFETY_TEXT, manual_risk_cap

log = logging.getLogger(__name__)

NO_CHARTS = "No chart images are attached to this message: work from the payload numbers."
AUTH_WAIT_S = 90.0                 # a cold CLI sign-in check on a busy machine (claude_code.AUTH_CHECK_TIMEOUT_S)
MIN_STOP_USD = 2.0                 # the hard rules the code checks (checks.py) and the prompt states
MIN_STOP_ATR = 0.5
MIN_RR = 1.5
SPREAD_MULT = 5
MAX_PLANS = 5
LANGUAGE = {"ar": "Arabic", "en": "English"}
SIDE_AR = {"BUY": "شراء", "SELL": "بيع"}
REGIME_AR = {"trend_up": "ترند صاعد", "trend_down": "ترند هابط", "range": "تذبذب", "volatile_news": "تقلّب أخبار",
             "mixed": "مختلط"}
BIAS_AR = {"bullish": "صاعد", "bearish": "هابط", "neutral": "محايد"}
STRENGTH_AR = {"strong": "قوي", "medium": "متوسط", "weak": "ضعيف"}
ENTRY_AR = {"limit": "limit", "stop": "stop", "market_after_rejection": "market بعد رفض"}
OUTCOME_AR = {"not_triggered": "ما اتفعّلت", "target": "الهدف", "stop": "الوقف", "expired": "انتهت المدة",
              "unknown": "بلا بيانات", "rejected": "مرفوضة", "untouched": "ما انلمس", "held": "صمد", "broken": "انكسر",
              "undecided": "ما انحسم"}
NOTIFY_TEXT_MAX = 1500             # the Telegram message (the toast shows its first TOAST_TEXT_MAX characters)
NO_TRADE_AR = "لا صفقة هلق"


@dataclass
class Inputs:
    """What one brief is built from (``build_inputs``). Timestamps int64 UTC ms; prices in the analysis instrument's."""
    pair: str
    as_of: int
    payload: dict
    payload_text: str
    images: list[ImageInput] = field(default_factory=list)
    charts_note: str = NO_CHARTS
    charts: list[str] = field(default_factory=list)
    account: dict = field(default_factory=dict)
    journal: dict = field(default_factory=dict)
    atr5m: float | None = None
    spread_now: float | None = None
    spread_median: float | None = None
    price_at: float | None = None
    blackout: bool = False
    next_events: list = field(default_factory=list)
    news_windows: dict[str, list[int]] = field(default_factory=dict)


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def payload_numbers(payload: dict) -> dict:
    """The numbers the checks and the scorer need, read from the payload (None when the block is missing)."""
    tf5 = (((payload.get("timeframes") or {}).get("5m") or {}).get("indicators")) or {}
    ex = ((payload.get("market") or {}).get("execution")) or {}
    costs = ex.get("costs") or {}
    bid, ask = _f(ex.get("bid")), _f(ex.get("ask"))
    price_at = (bid + ask) / 2 if bid is not None and ask is not None else _f(tf5.get("close"))
    news = ((payload.get("market") or {}).get("news")) or {}
    nxt = news.get("next")
    return {"atr5m": _f(tf5.get("atr14")), "spread_now": _f(costs.get("spread_now")),
            "spread_median": _f(costs.get("spread_median_1h")), "price_at": price_at,
            "blackout": isinstance(news.get("blackout"), list),
            "next_events": list(nxt) if isinstance(nxt, list) else []}


def build_inputs(s: Settings, reg: InstrumentRegistry, pair: str, as_of: int) -> Inputs:
    """The desk's snapshot at ``as_of`` (the executor's account row read-only, the desk's trade journal, the engine's
    kv caches read-only) and the brief's own chart images. The heavy modules load here, not at import: the service
    gates first and imports this only for a run that goes ahead."""
    from ..ai.model_view import view
    from ..ai.store import journal_from
    from ..analysis.snapshot import SnapshotBuilder
    from .account import ReadOnlyKV, default_account, desk_account, ro_connect

    pcfg = s.pairs[pair]
    primary = reg.primary(pair)
    app_db = s.paths.state() / "app.db"
    con = ro_connect(app_db) if app_db.exists() else None
    try:
        if con is not None:
            account = desk_account(s, reg, con, pair, as_of)
            try:
                journal = journal_from(con, pair, as_of) or {}
            except Exception as exc:  # noqa: BLE001 - a database without the desk's tables = no journal
                log.warning("%s: the desk's journal could not be read (%s)", pair, exc)
                journal = {}
        else:
            account = {**default_account(s), "equity_source": "configured account size (no app.db); positions unknown"}
            journal = {}
        builder = SnapshotBuilder(s, reg, kv=ReadOnlyKV(con) if con is not None else None)
        try:
            payload = builder.build(pair, as_of, account=account, history=[], memory={}, performance={},
                                    journal=journal)
            images, note, tfs = _charts(s, builder, primary, pcfg, pair, as_of, payload)
        finally:
            builder.close()                     # never wait_background: the daily-profile worker is the engine's
    finally:
        if con is not None:
            con.close()
    nums = payload_numbers(payload)
    return Inputs(pair=pair, as_of=as_of, payload=payload,
                  payload_text=json.dumps(view(payload), separators=(",", ":"), default=str, ensure_ascii=False),
                  images=images, charts_note=note, charts=tfs, account=account, journal=journal,
                  news_windows={t: [int(b), int(a)] for t, (b, a) in pcfg.news_blackout.windows.items()}, **nums)


def _charts(s: Settings, builder, primary, pcfg, pair: str, as_of: int,
            payload: dict) -> tuple[list[ImageInput], str, list[str]]:
    """The brief's chart images (``scalp.charts``) in ``data/scalp/charts``; a failure = a text-only call."""
    from ..analysis.charts import ChartRenderer
    from ..core.sessions import calendar_for
    from ..core.settings import ChartsCfg

    try:
        cfg = ChartsCfg(enabled=True, width=s.ai.charts.width, height=s.ai.charts.height,
                        timeframes=list(s.scalp.charts),
                        bars={tf: s.ai.charts.bars.get(tf, 96) for tf in s.scalp.charts},
                        overlays=list(s.ai.charts.overlays))
        renderer = ChartRenderer(cfg, out_dir=scalp_dir(s) / "charts")
        cal = calendar_for(primary.venue, primary.symbol, pcfg.asset_class)
        imgs = renderer.render_set(builder.reader(primary), primary, as_of, payload, cal)
    except Exception:  # noqa: BLE001
        log.exception("%s: chart rendering failed - text-only brief", pair)
        return [], NO_CHARTS, []
    if not imgs:
        return [], NO_CHARTS, []
    note = (f"Attached: {len(imgs)} charts ({', '.join(c.tf for c in imgs)}), {cfg.width}×{cfg.height}, the last "
            f"closed candles of each timeframe, analysis prices ({primary.key}).")
    return [c.as_input(pair, primary.key) for c in imgs], note, [c.tf for c in imgs]


# ------------------------------------------------------------------------------------------------- the prompt
def system_vars(s: Settings, reg: InstrumentRegistry, pair: str) -> dict:
    """Stable from run to run (the system prompt is cached by the CLI and hashed into the record)."""
    h0, h1 = s.scalp.hours_utc
    windows = s.pairs[pair].news_blackout.windows
    return {"pair": pair, "price_reference": reg.primary(pair).key,
            "output_language": LANGUAGE.get(s.scalp.output_language, s.scalp.output_language),
            "min_stop_rule": f"|entry - stop| >= max({MIN_STOP_ATR:g} x the 5-minute ATR14, {MIN_STOP_USD:g} USD)",
            "min_rr": f"{MIN_RR:g}", "spread_mult": SPREAD_MULT, "max_plans": MAX_PLANS,
            "news_windows": ", ".join(f"{t} {b}/{a}" for t, (b, a) in windows.items()) or "none configured",
            "hours_text": f"every hour from {h0:02d}:00 to {max(h1 - 1, h0):02d}:00 UTC"}


def desk_text(desk: dict) -> str:
    """The desk block as the model reads it (one line per leg)."""
    eq = desk.get("equity")
    head = (f"equity {eq:.2f} {desk.get('currency', 'USD')}" if isinstance(eq, (int, float)) else "equity unknown")
    head += f" ({desk.get('equity_source')})" if desk.get("equity_source") else ""
    if desk.get("today_pnl_pct") is not None:
        head += f"; today {desk['today_pnl_pct']:+.2f} %"
    if desk.get("drawdown_pct") is not None:
        head += f"; drawdown {desk['drawdown_pct']:.2f} %"
    head += f"; kill switch {'ON' if desk.get('kill_switch') else 'off'}"
    lines = [head]
    legs = desk.get("legs") or []
    if not legs:
        lines.append("holds: nothing on this pair (flat, no pending order)")
    for leg in legs:
        since = leg.get("since_ms") or leg.get("expires_ms")
        when = f" since {iso(since)[:16]}" if leg.get("kind") == "position" and since else \
            f" expires {iso(since)[:16]}" if since else ""
        pnl = f" ({leg['profit_usd']:+.2f} USD)" if isinstance(leg.get("profit_usd"), (int, float)) else ""
        kind = f"{leg.get('kind')} {leg.get('side')} {leg.get('order_type') or ''}".replace("  ", " ").strip()
        lines.append(f"holds: {kind} entry {leg.get('entry')} sl {leg.get('sl')} tp {leg.get('tp')} "
                     f"volume {leg.get('volume')}{when}{pnl}")
    if desk.get("note"):
        lines.append(desk["note"])
    return "\n".join(lines)


def compose(s: Settings, reg: InstrumentRegistry, inputs: Inputs, *, reason: str, desk: dict, manual_text: str,
            journal_text: str) -> tuple[str, str, dict[str, int]]:
    """``(system, user, versions)`` of this run."""
    user = {"now_utc": iso(inputs.as_of), "reason": reason, "desk": desk_text(desk),
            "manual_recent": manual_text or "none in the last days",
            "journal": journal_text or "no scored brief yet - calibrate from the rules above",
            "charts_note": inputs.charts_note, "payload": inputs.payload_text}
    return render_scalp(system_vars(s, reg, inputs.pair), user)


# ------------------------------------------------------------------------------------------------- the call
def provider_name(s: Settings) -> str:
    """The claude_code provider: the active one when it is, else the first configured (as the operator tools)."""
    active = s.ai.providers.get(s.ai.active_provider)
    if active is not None and active.kind == "claude_code":
        return s.ai.active_provider
    for name, cfg in s.ai.providers.items():
        if cfg.kind == "claude_code":
            return name
    raise ValueError("no provider of kind claude_code is configured (ai.providers)")


async def call_model(s: Settings, usage: UsageStore, system: str, user: str, images: list[ImageInput],
                     *, wait_s: float = AUTH_WAIT_S) -> Generation[ScalpBrief]:
    """ONE validated call (one repair at most) on ``ai.models.scalp``: ledger role ``scalp``, ``pair`` None, so the
    row counts against neither the provider's ``rpd`` ladder nor the pair's daily cap. The CLI start stagger and the
    sign-in check are the provider's own."""
    name = provider_name(s)
    rm = s.ai.models.scalp
    prov = make_provider(s, name, model=rm.model, effort=rm.effort)
    if hasattr(prov, "caps_dir"):
        prov.caps_dir = s.paths.shared()
    end = time.monotonic() + wait_s
    why = prov.unavailable_reason()
    while getattr(prov, "availability_pending", False) and time.monotonic() < end:
        await asyncio.sleep(0.5)
        why = prov.unavailable_reason()
    if why:
        raise ProviderError(f"scalp provider unavailable: {why}", retryable=False)
    limiter = RateLimiter(name, prov.cfg, usage, instance=None, daily_cap=None)
    governor = CostGovernor(s.ai.budget, usage)
    return await generate_validated(prov, ScalpBrief, system=system, user=user, limiter=limiter, governor=governor,
                                    usage=usage, purpose="scalp_brief", pair=None, max_repairs=1,
                                    est_input_tokens=max(2000, len(user) // 3), images=images or None, role="scalp",
                                    transient_retry_s=s.ai.transient_retry_s)


# ------------------------------------------------------------------------------------------------- the record
def to_record(s: Settings, *, inputs: Inputs, reason: str, brief: ScalpBrief, checks: dict[str, dict], desk: dict,
              tol: float, gen: Generation | None, system: str, versions: dict[str, int]) -> dict:
    """The stored record (``files.write_record``); ``gen`` None = the fixture path (no call, zero tokens)."""
    attempts = list(gen.attempts) if gen is not None else []
    rm = s.ai.models.scalp
    return {"version": RECORD_VERSION, "id": brief_id(inputs.as_of), "as_of_ms": inputs.as_of, "reason": reason,
            "model": attempts[-1].model if attempts else rm.model, "effort": rm.effort,
            "latency_ms": sum(a.latency_ms for a in attempts),
            "tokens": {"input": sum(a.input_tokens for a in attempts),
                       "cached": sum(a.cached_input_tokens for a in attempts),
                       "output": sum(a.output_tokens for a in attempts)},
            "images": gen.images_sent if gen is not None else 0, "prompt_hash": prompt_hash(system),
            "prompt_versions": dict(versions), "payload_hash": (inputs.payload.get("meta") or {}).get("payload_hash"),
            "price_at": inputs.price_at, "atr5m": inputs.atr5m, "spread_now": inputs.spread_now,
            "spread_median": inputs.spread_median, "tol": tol, "blackout": inputs.blackout,
            "next_events": list(inputs.next_events), "news_windows": dict(inputs.news_windows), "desk": desk,
            "brief": brief.model_dump(), "checks": checks, "charts": list(inputs.charts), "score": None,
            "scored_ms": None, "manual": [], "trade": trade_summary(brief.model_dump(), checks)}


def _hm(ms: int | None) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%H:%M") if ms else "—"


def _usd(a: Any, b: Any) -> str:
    return f"{abs(float(a) - float(b)):.2f} $" if isinstance(a, (int, float)) and isinstance(b, (int, float)) else "—"


def trade_summary(brief: dict, checks: dict[str, dict] | None = None) -> dict:
    """D-060: THE trade of the hour - the plan ``best_plan`` names when it passed the code checks, else the
    highest-confidence plan that passed, else none. ``text`` = the model's ``trade_call`` (or a code-built sentence
    when it left it empty), ``line`` = the numbers (entry / stop / target / RR / confidence / hold), ``idx`` = the
    plan's index or None, ``rejected`` = why the model's own choice was dropped (hard checks)."""
    plans = list(brief.get("plans") or [])
    checks = checks or {}

    def rejected(i: int) -> list[str]:
        return list((checks.get(str(i)) or {}).get("rejected") or [])

    idx = brief.get("best_plan")
    dropped: list[str] = []
    if isinstance(idx, int) and 0 <= idx < len(plans) and rejected(idx):
        dropped = rejected(idx)
        idx = None
    if not (isinstance(idx, int) and 0 <= idx < len(plans)):
        ok = [i for i in range(len(plans)) if not rejected(i)]
        idx = max(ok, key=lambda i: (plans[i].get("confidence") or 0, -i)) if ok else None
    text = str(brief.get("trade_call") or "").strip()
    if idx is None:
        line = ""
        if not text or dropped:
            why = (brief.get("no_scalp") or [""])[0] if brief.get("no_scalp") else ""
            text = NO_TRADE_AR + (f": {why}" if why else "") + (
                f" (خطة الموديل سقطت بفحوصات الكود: {', '.join(dropped)})" if dropped else "")
        return {"idx": None, "text": text, "line": line, "rejected": dropped}
    p = plans[idx]
    side = SIDE_AR.get(p.get("side"), p.get("side"))
    line = (f"{side} من {_p(p.get('entry'))} ({ENTRY_AR.get(p.get('entry_type'), p.get('entry_type'))}) · "
            f"الوقف {_p(p.get('stop'))} ({_usd(p.get('entry'), p.get('stop'))} للأونصة) · "
            f"الهدف {_p(p.get('target'))} ({_usd(p.get('target'), p.get('entry'))} للأونصة) · RR {p.get('rr')} · "
            f"ثقة {p.get('confidence')}% · المدة ≤ {p.get('max_hold_min')} د")
    if not text or dropped:
        text = (f"الصفقة: {side} من {_p(p.get('entry'))}، الوقف {_p(p.get('stop'))}، الهدف {_p(p.get('target'))}. "
                f"الشرط: {p.get('trigger', '')}" + (f" (خطة الموديل الأولى سقطت: {', '.join(dropped)})" if dropped else ""))
    return {"idx": idx, "text": text, "line": line, "rejected": dropped}


def _p(x: Any) -> str:
    return f"{x:.2f}" if isinstance(x, (int, float)) else "—"


def _checks_text(c: dict | None) -> str:
    if not c:
        return "—"
    failed = [k for k in ("stop_ok", "target_ok", "news_ok", "desk_ok", "level_ok", "rr_ok") if c.get(k) is False]
    if c.get("rejected"):
        return "مرفوضة: " + ", ".join(c["rejected"])
    return "كلّها ✓" if not failed else "✓ عدا " + ", ".join(failed)


def render_md(rec: dict, *, cap_pct: float = 5.0) -> str:
    """The brief as the owner reads it (Arabic; the levels and plans as tables; the safety text always). The first
    600 characters are the toast / Telegram text, so the headline and the picture come first. ``cap_pct`` =
    ``scalp.manual_risk_cap_pct`` (the same number the Scalp tab shows)."""
    b = rec.get("brief") or {}
    desk = rec.get("desk") or {}
    t = _hm(rec.get("as_of_ms"))
    regime = REGIME_AR.get(b.get("regime"), str(b.get("regime")))
    bias = BIAS_AR.get(b.get("bias"), str(b.get("bias")))
    out = [f"# Scalp {t} UTC — {regime} / {bias} ({b.get('bias_confidence', '—')}%)"]
    if b.get("no_scalp_now"):
        out.append("**لا سكالب هلق (NO SCALP)**")
    legs = desk.get("legs") or []
    if legs:
        out.append("**النظام الآلي ماسك:** " + "؛ ".join(
            f"{SIDE_AR.get(x.get('side'), x.get('side'))} {x.get('kind')} @ {_p(x.get('entry'))} "
            f"(SL {_p(x.get('sl'))} / TP {_p(x.get('tp'))})" for x in legs) + " — ما تعاكسو.")
    tr = rec.get("trade") or trade_summary(b, rec.get("checks") or {})
    out += ["", "## الصفقة المقترحة", f"**{tr.get('text', '')}**"]
    if tr.get("idx") is not None:
        p = (b.get("plans") or [])[int(tr["idx"])]
        out += [f"- الأرقام (خطة {int(tr['idx']) + 1}): {tr.get('line', '')}", f"- الشرط: {p.get('trigger', '')}",
                f"- الإبطال: {p.get('invalidation', '')}"]
    out += ["", "## الصورة", str(b.get("picture", "")), "", "## المتوقع", str(b.get("expect", ""))]
    out += ["", "## المستويات", "| السعر | النوع | الإطار | القوة | ليش |", "|---|---|---|---|---|"]
    for lv in b.get("levels") or []:
        why = str(lv.get("why", "")).replace("|", "/")
        out.append(f"| {_p(lv.get('price'))} | {lv.get('kind')} | {lv.get('timeframe')} | "
                   f"{STRENGTH_AR.get(lv.get('strength'), lv.get('strength'))} | {why} |")
    plans = b.get("plans") or []
    out += ["", "## الخطط" + ("" if plans else " — ما في خطة هالساعة")]
    if plans:
        out += ["| # | الاتجاه | الدخول | الوقف | الهدف | RR | المدة | ثقة الموديل | الفحوصات |",
                "|---|---|---|---|---|---|---|---|---|"]
        checks = rec.get("checks") or {}
        for i, p in enumerate(plans):
            out.append(f"| {i + 1} | {SIDE_AR.get(p.get('side'), p.get('side'))} | {_p(p.get('entry'))} "
                       f"({ENTRY_AR.get(p.get('entry_type'), p.get('entry_type'))}) | {_p(p.get('stop'))} | "
                       f"{_p(p.get('target'))} | {p.get('rr')} | {p.get('max_hold_min')} د | {p.get('confidence')}% | "
                       f"{_checks_text(checks.get(str(i)))} |")
        for i, p in enumerate(plans):
            out += [f"- **خطة {i + 1}** — الشرط: {p.get('trigger', '')}", f"  الإبطال: {p.get('invalidation', '')}"]
    if b.get("no_scalp"):
        out += ["", "## لا سكالب إذا"] + [f"- {x}" for x in b["no_scalp"]]
    if b.get("news_clock"):
        out += ["", "## ساعة الأخبار"] + [f"- {x}" for x in b["news_clock"]]
    out += ["", "## النظام الآلي", str(b.get("desk_warning", ""))]
    if b.get("confidence_note"):
        out += ["", "## معايرة الثقة", str(b["confidence_note"])]
    sc = rec.get("score")
    if sc:
        sm = sc.get("summary") or {}
        out += ["", "## النتيجة (على التكات الحقيقية)",
                f"- المستويات: لُمس {sm.get('levels_touched', 0)}، صمد {sm.get('levels_held', 0)}",
                f"- الخطط: {sm.get('plans_n', 0)}، اتفعّل {sm.get('triggered', 0)}، "
                f"الهدف أول {sm.get('target_first', 0)}، الوقف أول {sm.get('stop_first', 0)}، "
                f"انتهت {sm.get('expired', 0)}، مجموع R {sm.get('sum_r', 0)}"]
        for p in sc.get("plans") or []:
            r = p.get("r")
            out.append(f"- خطة {int(p.get('idx', 0)) + 1}: {OUTCOME_AR.get(p.get('outcome'), p.get('outcome'))}"
                       + (f" ({r:+.2f}R بعد {p.get('minutes')} د)" if isinstance(r, (int, float)) else ""))
    man = rec.get("manual") or []
    if man:
        out += ["", "## صفقاتك اليدوية بهالساعة"]
        for m in man:
            pnl = m.get("pnl_usd")
            out.append(f"- {_hm(m.get('open_ms'))} {SIDE_AR.get(m.get('side'), m.get('side'))} {_p(m.get('entry'))}"
                       + (f" → {_p(m.get('exit'))}" if m.get("exit") is not None else " (مفتوحة)")
                       + (f" {pnl:+.2f} USD" if isinstance(pnl, (int, float)) else "")
                       + (" (تبعت الخطة)" if m.get("followed_plan") else ""))
    cap = manual_risk_cap(desk.get("equity"), cap_pct)
    out += ["", "## السلامة (نفس الحساب)", f"- {cap['rule_ar']}"
            + (f" (≤ {cap['max_risk_usd']:.2f} USD بالصفقة، {cap['daily_cap_usd']:.2f} USD باليوم)"
               if cap.get("max_risk_usd") else "")]
    out += [f"- {x['ar']}" for x in SAFETY_TEXT]
    tok = rec.get("tokens") or {}
    out += ["", f"_{rec.get('model')} · {rec.get('effort')} · {round((rec.get('latency_ms') or 0) / 1000)} s · "
                f"{tok.get('input', 0)} in / {tok.get('output', 0)} out · {rec.get('reason')} · {rec.get('id')}_"]
    return "\n".join(out) + "\n"


def notify_text(rec: dict, md: str | None = None) -> tuple[str, str]:
    """``(title, text)`` of the toast / Telegram message (D-060): the trade first (what the owner acts on - the toast
    shows only the first ``TOAST_TEXT_MAX`` characters), then the desk's holdings, the picture, the levels, the
    no-scalp conditions and the news clock; ``NOTIFY_TEXT_MAX`` characters at most. ``md`` is accepted for
    compatibility (the message is composed from the record, not cut from the markdown)."""
    b = rec.get("brief") or {}
    desk = rec.get("desk") or {}
    regime = REGIME_AR.get(b.get("regime"), str(b.get("regime")))
    bias = BIAS_AR.get(b.get("bias"), str(b.get("bias")))
    title = f"Scalp {_hm(rec.get('as_of_ms'))} UTC — {b.get('regime')} / {b.get('bias')}"
    if b.get("no_scalp_now"):
        title += " — NO SCALP"
    tr = rec.get("trade") or trade_summary(b, rec.get("checks") or {})
    out = [f"{regime} / {bias} ({b.get('bias_confidence', '—')}%)" + (" — لا سكالب هلق" if b.get("no_scalp_now") else ""),
           f"الصفقة: {tr.get('text', '')}"]
    if tr.get("idx") is not None:
        p = (b.get("plans") or [])[int(tr["idx"])]
        out += [f"الأرقام: {tr.get('line', '')}", f"الإبطال: {p.get('invalidation', '')}"]
    legs = desk.get("legs") or []
    if legs:
        out.append("النظام الآلي ماسك: " + "؛ ".join(
            f"{SIDE_AR.get(x.get('side'), x.get('side'))} {x.get('kind')} @ {_p(x.get('entry'))} "
            f"(SL {_p(x.get('sl'))} / TP {_p(x.get('tp'))})" for x in legs) + " — ما تعاكسو.")
    out += ["", f"الصورة: {b.get('picture', '')}", f"المتوقع: {b.get('expect', '')}"]
    lv = b.get("levels") or []
    if lv:
        out.append("المستويات: " + " · ".join(
            f"{_p(x.get('price'))} ({x.get('kind')}, {x.get('timeframe')}, "
            f"{STRENGTH_AR.get(x.get('strength'), x.get('strength'))})" for x in lv))
    others = [i for i in range(len(b.get("plans") or [])) if i != tr.get("idx")]
    if others:
        out.append("خطط تانية: " + " · ".join(
            f"{SIDE_AR.get(p.get('side'), p.get('side'))} {_p(p.get('entry'))} / وقف {_p(p.get('stop'))} / هدف "
            f"{_p(p.get('target'))} ({p.get('confidence')}%)" for p in ((b.get("plans") or [])[i] for i in others)))
    if b.get("no_scalp"):
        out.append("لا تسكالب إذا: " + " · ".join(str(x) for x in b["no_scalp"]))
    if b.get("news_clock"):
        out.append("الأخبار: " + " · ".join(str(x) for x in b["news_clock"]))
    text = "\n".join(out)
    return title, (text if len(text) <= NOTIFY_TEXT_MAX else text[:NOTIFY_TEXT_MAX - 1] + "…")
