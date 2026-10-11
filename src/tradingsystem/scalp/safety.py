"""The same-account safety text (D-058): what a manual scalp does to the automatic desk on the shared MT5 account.
Shown on the Scalp tab and written into every brief; the risk cap is a recommendation, never enforced."""
from __future__ import annotations

SAFETY_TEXT: list[dict[str, str]] = [
    {"ar": "المراقب بيقرأ رصيد الحساب كلّو: تحذير عند نزلة 15%، وkill switch عام عند 35% بين فحصين (كل 15 دقيقة). "
           "خسارة يدوية بهالحجم بتوقّف النظام الآلي.",
     "en": "The monitor reads the WHOLE account: warn at -15 %, a global KILL SWITCH at -35 % between two 15-minute "
           "samples (tools/monitor.py). A manual loss of that size stops the automatic desk."},
    {"ar": "النظام الآلي ما بيشوف صفقاتك اليدوية (magic 0): ممكن يفتح عكسها أو فوقها. ما تعاكس صفقتو المفتوحة.",
     "en": "The desk cannot see manual positions (magic 0): it may open the opposite side or stack on yours. Never "
           "trade against its open position."},
    {"ar": "إقفال الويكند والعطل بيسكّر صفقات النظام بس: سكّر صفقاتك اليدوية بإيدك قبل الجمعة 16:30 نيويورك.",
     "en": "The weekend / holiday flat closes the desk's trades only: flatten manual trades by hand before Friday "
           "16:30 New York."},
    {"ar": "الهامش مشترك وما في فحص إلو بالبوابة: صفقتك اليدوية ممكن تخلّي أمر النظام يفشل (NO_MONEY)، وخسارتك العائمة "
           "بتصغّر الرصيد اللي بيحجّم النظام عليه (تحت ~75 $ الدهب بينرفض).",
     "en": "Margin is shared and not gate-checked: a manual position can make the desk's order fail (NO_MONEY); a "
           "floating manual loss shrinks the equity the desk sizes from (below ~$75 gold is refused)."},
    {"ar": "ولا تعليق أمر يدوي يبلّش بـ ts: (علامة تسوية النظام)، والمراقب ما بيراقب صفقة يدوية بلا وقف.",
     "en": "Never give a manual order a comment starting with ts: (the desk's settlement tag); the no-SL monitor does "
           "not watch manual positions."},
]


def manual_risk_cap(equity: float | None, cap_pct: float = 5.0) -> dict:
    """The recommended manual risk (a recommendation on the page; the owner's numbers stay the owner's)."""
    eq = float(equity or 0.0)
    return {"max_risk_usd": round(eq * cap_pct / 100, 2) if eq else None, "max_risk_pct": cap_pct,
            "max_open": 1, "daily_cap_pct": 10.0, "daily_cap_usd": round(eq * 0.10, 2) if eq else None,
            "rule_ar": f"≤ {cap_pct:g}% من الرصيد بالسكالب الواحد، صفقة يدوية وحدة بالمرة، وقف يومي يدوي 10%، "
                       "ما تعاكس النظام، وسكّر قبل إقفال الويكند.",
            "rule_en": f"<= {cap_pct:g} % of equity per scalp, one manual position at a time, a 10 % daily manual "
                       "loss stop, never against the desk, flat before the desk's flat."}
