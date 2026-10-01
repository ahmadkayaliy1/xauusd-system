"""Desks. Shadow (D-049 / B14): a pair with ``pairs.<PAIR>.desk.mode == "shadow"`` is analysed, gated and scored but never
traded. The executor checks the pair before any backend call (``Executor.handle``, ``_handle``, the management and
model-action steps); this module is the second, independent layer: :class:`ShadowGuard` wraps the execution backend
the executor uses and raises :class:`ShadowViolation` when a mutating call (place / modify / close / cancel) resolves
to a shadow pair, so a coding slip anywhere else still cannot reach the account.

Reads (account, quotes, specs, legs) pass through untouched — the shadow path needs the live account to gate an idea
in full, and reading changes nothing.

Live (D-052): a pair with ``desk.mode == "live"`` trades through the ordinary path; the end of this module holds what
the desk adds (the weekend flat rule, the review calls before a release and before the flat).
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from ..core.settings import Settings

log = logging.getLogger("executor")

# methods that change the account: ``place`` adds risk (an unresolvable pair is refused); the others only ever reduce
# or tighten what exists (an unresolvable target passes — a shadow pair never has anything to reduce)
ADDING = frozenset({"place"})
REDUCING = frozenset({"cancel_decision", "modify_leg", "close_legs", "modify_sl", "modify_tp", "close_position",
                      "cancel_order"})
MUTATING = ADDING | REDUCING

# what marks a stored decision as a shadow idea (execution_detail JSON key) — the candidate query skips those
SHADOW_KEY = "shadow"


class ShadowViolation(RuntimeError):
    """A shadow pair's order would have reached the backend — never raised on a working path."""


def shadow_pairs(s: Settings) -> frozenset[str]:
    """Every configured pair (enabled in this system or not) whose desk is in shadow mode."""
    return frozenset(name for name, p in s.pairs.items() if p.desk is not None and p.desk.mode == "shadow")


class ShadowGuard:
    """Transparent wrapper of a backend (``PaperBackend`` / ``MT5Backend``). ``pair_of(method, args, kwargs)`` resolves
    the pair a mutating call acts on (None = cannot tell)."""

    def __init__(self, backend: Any, pairs: frozenset[str], pair_of: Callable[[str, tuple, dict], str | None]) -> None:
        object.__setattr__(self, "_backend", backend)
        object.__setattr__(self, "_pairs", frozenset(pairs))
        object.__setattr__(self, "_pair_of", pair_of)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._backend, name)
        if name not in MUTATING or not callable(attr):
            return attr

        def guarded(*args: Any, **kwargs: Any) -> Any:
            self._check(name, args, kwargs)
            return attr(*args, **kwargs)

        return guarded

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._backend, name, value)

    def _check(self, name: str, args: tuple, kwargs: dict) -> None:
        try:
            pair = self._pair_of(name, args, kwargs)
        except Exception:  # noqa: BLE001 — an unresolvable call is judged by its kind below
            pair = None
        if pair is not None and pair in self._pairs:
            log.error("shadow guard: %s on shadow pair %s refused", name, pair)
            raise ShadowViolation(f"{name} on shadow pair {pair} refused: a desk in shadow never touches the account")
        if pair is None and name in ADDING:
            log.error("shadow guard: %s with an unresolvable pair refused", name)
            raise ShadowViolation(f"{name} refused: its pair could not be resolved, so it cannot be shown not to "
                                  "belong to a shadow desk")

    @property
    def wrapped(self) -> Any:
        return self._backend


def assert_not_shadow(pairs: frozenset[str], pair: str, what: str) -> None:
    """The executor's own check before any backend-touching helper acts on ``pair``."""
    if pair in pairs:
        raise ShadowViolation(f"{what} on shadow pair {pair} refused: a desk in shadow never touches the account")


# ---------------------------------------------------------------------------------------------- live desk (D-052)
# A desk in ``mode: live`` trades through the ordinary path (none of the above applies to it). What it adds: the weekend
# flat (a system rule appended to every open trade of the pair from desk.flat_before_weekend on) and two review calls
# while it holds a trade - before a fomc/high release and before the flat - as executor events the engine wakes on.
WEEKEND_RULE_NOTE = "desk: flat before the weekend (D-052)"
REVIEW_EVENT = "desk_review"


def live_desk_pairs(s: Settings) -> frozenset[str]:
    """Every configured pair whose desk is live."""
    return frozenset(name for name, p in s.pairs.items() if p.desk is not None and p.desk.mode == "live")


def with_weekend_flat(decision: dict, flat_ms: int) -> dict:
    """``decision`` with the weekend flat appended to its management rules (after the trade's own, so their indices -
    the keys of their stored states - stay): ``close_all`` at ``flat_ms`` (the internal ``at_time`` trigger: met by a
    position or a pending order). A copy; the stored decision is not changed."""
    from .management import rules_of
    rule = {"action": "close_all", "trigger": "at_time", "value": int(flat_ms), "params": {}, "note": WEEKEND_RULE_NOTE}
    det = dict(decision.get("execution_detail") or {})
    det["executed_management"] = [*rules_of(decision), rule]
    return {**decision, "execution_detail": det}


def reviews_due(pcfg: Any, shared_dir: Any, now_ms: int) -> list[tuple[str, str, str]]:
    """The live desk's review calls due at ``now`` for a pair that holds a trade: ``(kind, key, text)`` — ``news``
    once per fomc/high release within ``desk.pre_news_review_min`` (from the stored calendar; a stale one gives none),
    ``weekend`` once in the ``WEEKEND_REVIEW_MIN`` before the weekend flat. ``key`` identifies the occasion (the caller
    sends each one once). Never raises."""
    from ..analysis import news
    from ..analysis.desk_windows import WEEKEND_REVIEW_MIN, friday_flat_ms
    from ..core.timeutil import MS_PER_MINUTE, iso
    desk = pcfg.desk
    out: list[tuple[str, str, str]] = []
    if desk is None or desk.mode != "live":
        return out
    try:
        if desk.pre_news_review_min > 0 and pcfg.news_blackout.enabled:
            st = news.state_now(pcfg.news_blackout, shared_dir, now_ms)
            for tier, ev in st.upcoming if st.fresh else ():
                left = ev.time_ms - now_ms
                if tier in ("fomc", "high") and 0 < left <= desk.pre_news_review_min * MS_PER_MINUTE:
                    out.append(("news", str(ev.time_ms), f"{tier} release '{ev.title}' at {iso(ev.time_ms)} in "
                                f"{-(-left // MS_PER_MINUTE)} min: review the open trade before it (D-052)"))
                    break
        at = friday_flat_ms(desk, now_ms)
        if at is not None and at - WEEKEND_REVIEW_MIN * MS_PER_MINUTE <= now_ms < at:
            out.append(("weekend", str(at), f"the desk closes every open trade at {iso(at)} before the weekend (in "
                        f"{-(-(at - now_ms) // MS_PER_MINUTE)} min): review the open trade (D-052)"))
    except Exception:  # noqa: BLE001 — a review call is a courtesy; the weekend flat itself does not depend on it
        log.exception("desk reviews of %s could not be checked", pcfg)
    return out
