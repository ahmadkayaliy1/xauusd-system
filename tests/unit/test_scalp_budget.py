"""D-058: the scalp brief's ledger rows (ai/budget.py) — role ``scalp`` with no pair: recorded for the usage gauge,
counted by ``count_role_since`` (the job's own daily cap), but never by ``count_since`` (the provider's ``rpd`` ladder)
nor by a pair's daily cap, so the desk's quota does not move when the brief runs."""
from tradingsystem.ai import budget
from tradingsystem.core.settings import AIProviderCfg


def test_scalp_rows_are_outside_the_desks_quota_but_inside_the_gauge(tmp_path):
    usage = budget.UsageStore(tmp_path / "ai_usage.db")
    cfg = AIProviderCfg(kind="claude_code", model="claude-sonnet-5", rpd=300)
    for _ in range(10):
        usage.record(None, provider="claude_code", model="claude-sonnet-5", purpose="decision", pair="XAUUSD", ok=True,
                     role="decision")
    for _ in range(3):
        usage.record(None, provider="claude_code", model="claude-fable-5-1", purpose="scalp_brief", pair=None, ok=True,
                     role="scalp")
    usage.record(None, provider="claude_code", model="claude-fable-5-1", purpose="scalp_brief", pair=None, ok=False,
                 error="timeout", role="scalp")
    usage.record(None, provider="claude_code", model="opus", purpose="review", pair=None, ok=True, role="review")
    assert usage.count_since("claude_code", 0) == 10                          # scalp (and review) rows excluded
    assert usage.count_since("claude_code", 0, pair="XAUUSD") == 10
    assert usage.count_role_since("scalp", 0) == 4                            # failed attempts count too (the cap)
    assert usage.count_role_since("scalp", 0, pair="XAUUSD") == 0             # the rows carry no pair
    assert usage.count_role_since("review", 0) == 1
    assert usage.tokens_since(0)["calls"] == 15                               # the gauge sees every row
    alone = budget.RateLimiter("claude_code", cfg, usage)
    assert alone.cap() == 300 and alone.remaining_today() == 290
    mine = budget.RateLimiter("claude_code", cfg, usage, instance="XAUUSD", daily_cap=250)
    assert mine.cap() == 250 and mine.remaining_today() == 240 and mine.used_today() == (10, 250)
    rows = usage._con.execute("SELECT role, pair, model, purpose FROM ai_usage WHERE role='scalp'").fetchall()
    assert rows and all(r[1] is None and r[2] == "claude-fable-5-1" and r[3] == "scalp_brief" for r in rows)
    usage.close()


def test_the_exclusion_is_by_role_only():
    """A desk row never carries the scalp role; ``NULL`` roles (old rows) still count toward the ladder."""
    import inspect
    src = inspect.getsource(budget.UsageStore.count_since)
    assert "NOT IN ('review', 'diagnose', 'scalp')" in src and "COALESCE(role, '')" in src


def test_the_scalp_daily_cap_counts_answered_briefs_only(tmp_path):
    """2026-10-14: two 180-s timeouts and a refused sign-in ate three of the owner's twelve daily briefs."""
    from tradingsystem.ai.budget import UsageStore
    st = UsageStore(tmp_path / "ai_usage.db")
    try:
        for ok in (1, 0, 0, 1):
            st.record(None, provider="claude_code", model="claude-fable-5-1", purpose="scalp_brief", pair=None, ok=bool(ok),
                      error=None if ok else "claude_code: no answer within 180s", role="scalp")
        assert st.count_role_since("scalp", 0) == 4 and st.count_role_since("scalp", 0, ok_only=True) == 2
    finally:
        st.close()
