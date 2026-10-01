"""Gold desk call windows (Phase 5 B15, D-049): when a desk pair may be asked for an ENTRY idea.

Pure functions of the desk config, an instant and the execution venue's session calendar — the engine uses them to
suppress entry calls, ``market.gold_clock.desk_window`` (B16) reports the same state to the model.

Closed reasons (in the order they are tested): ``market_closed`` (the calendar), ``reopen_grace`` (within
``desk.reopen_grace_min`` minutes of the weekly reopen — the calendar's closed → open edge after a break longer than
the daily one, never a hardcoded clock time), ``friday_cutoff`` (Friday from ``desk.friday_cutoff`` in its zone),
``outside_window`` (in none of ``desk.windows``, each read in its own zone so DST moves the UTC hours), and inside a
window ``news_blackout`` / ``news_stale`` (B8: the caller passes :func:`desk_news_reason` of the stored news
calendar). Open = ``(True, "<window name>")``; with ``desk.windows: []`` (D-052) the desk is open whenever none of the
other reasons holds = ``(True, "all_day")``.

D-052 (a live desk): :func:`weekend_flat_ms` — this week's ``desk.flat_before_weekend`` instant while it applies.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from ..core.sessions import SessionCalendar
from ..core.settings import DeskCfg
from ..core.timeutil import MS_PER_HOUR, MS_PER_MINUTE

UTC = dt.timezone.utc
WEEKLY_BREAK_MS = 2 * MS_PER_HOUR          # a closed stretch this long before a reopen = the weekly one, not the daily break


def desk_news_reason(news_state) -> str | None:
    """B8 for a desk (D-049): ``news_blackout`` inside a release's window, ``news_stale`` while the calendar is not fresh
    (no money is at stake in shadow, so the desk fails closed on calls), else None. ``news_state`` is an
    :class:`.news.NewsState` or None (the pair has no blackout configured)."""
    if news_state is None:
        return None
    if not news_state.fresh:
        return "news_stale"
    return "news_blackout" if news_state.blackout is not None else None


def _hm(text: str) -> tuple[int, int]:
    h, m = text.split(":")
    return int(h), int(m)


def _local(now_ms: int, tz: str) -> dt.datetime:
    return dt.datetime.fromtimestamp(now_ms / 1000, tz=UTC).astimezone(ZoneInfo(tz))


def _reopen_at(cal: SessionCalendar, now_ms: int, grace_min: int) -> int | None:
    """The instant (minute) of the weekly reopen when ``now`` is open and it lies within ``grace_min`` minutes back,
    else None. Found by walking the calendar back minute by minute — no clock time is assumed."""
    if grace_min <= 0 or not cal.is_open(now_ms):
        return None
    t = now_ms - now_ms % MS_PER_MINUTE
    for back in range(0, grace_min + 1):
        m = t - back * MS_PER_MINUTE
        if not cal.is_open(m):
            reopen = m + MS_PER_MINUTE
            return reopen if not cal.is_open(reopen - WEEKLY_BREAK_MS) else None   # daily break: not the Sunday one
    return None


def desk_window_state(cfg: DeskCfg, now_ms: int, calendar: SessionCalendar, pair: str = "",
                      news: str | None = None) -> tuple[bool, str]:
    """``(True, window name)`` while entry calls are allowed, else ``(False, reason)`` (see the module text); ``news`` =
    :func:`desk_news_reason` at ``now_ms`` (None = no news block)."""
    if not calendar.is_open(now_ms):
        return False, "market_closed"
    if _reopen_at(calendar, now_ms, cfg.reopen_grace_min) is not None:
        return False, "reopen_grace"
    cut = _local(now_ms, cfg.friday_cutoff.tz)
    if cut.weekday() == 4 and (cut.hour, cut.minute) >= _hm(cfg.friday_cutoff.time):
        return False, "friday_cutoff"
    if not cfg.windows:                                  # D-052: no windows = all day (the session is the model's)
        return (False, news) if news else (True, "all_day")
    for w in cfg.windows:
        t = _local(now_ms, w.tz)
        if _hm(w.start) <= (t.hour, t.minute) < _hm(w.end):
            return (False, news) if news else (True, w.name)
    return False, "outside_window"


def desk_state_text(cfg: DeskCfg, now_ms: int, calendar: SessionCalendar, pair: str = "",
                    news: str | None = None) -> str:
    """The state as one word for the model / status: ``open`` or ``closed:<reason>`` (B16 reuses this)."""
    ok, why = desk_window_state(cfg, now_ms, calendar, pair, news)
    return "open" if ok else f"closed:{why}"


def last_window_end(cfg: DeskCfg, now_ms: int) -> int:
    """The UTC end (ms) of the latest window occurrence that ended at or before ``now`` (searched back 8 days; 0 when
    none) — the identity of the gap ``now`` lies in, so one event covers one gap between windows. Without windows
    (all day, D-052) the UTC day's start: one event per closed reason and day."""
    if not cfg.windows:
        return now_ms - now_ms % (24 * MS_PER_HOUR)
    best = 0
    for w in cfg.windows:
        z = ZoneInfo(w.tz)
        day = _local(now_ms, w.tz).date()
        h, m = _hm(w.end)
        for back in range(0, 9):
            d = day - dt.timedelta(days=back)
            end = int(dt.datetime(d.year, d.month, d.day, h, m, tzinfo=z).timestamp() * 1000)
            if end <= now_ms:
                best = max(best, end)
                break
    return best


WEEKEND_FLAT_SPAN_MS = 12 * MS_PER_HOUR     # the flat applies from its Friday time for this long (the rest of Friday)
WEEKEND_REVIEW_MIN = 30                     # the review call before the flat


def friday_flat_ms(cfg: DeskCfg, now_ms: int) -> int | None:
    """D-052: the UTC instant of ``desk.flat_before_weekend`` on the Friday of ``now``'s week (Monday-Sunday in the
    rule's zone, so DST moves it with New York); None without the rule or for a desk that is not live."""
    fb = cfg.flat_before_weekend
    if fb is None or cfg.mode != "live":
        return None
    loc = _local(now_ms, fb.tz)
    fri = loc.date() + dt.timedelta(days=4 - loc.weekday())
    h, m = _hm(fb.time)
    return int(dt.datetime(fri.year, fri.month, fri.day, h, m, tzinfo=ZoneInfo(fb.tz)).timestamp() * 1000)


def weekend_flat_ms(cfg: DeskCfg, now_ms: int) -> int | None:
    """This Friday's flat instant while ``now`` lies in [it, + WEEKEND_FLAT_SPAN_MS) — the rest of the Friday session —
    else None. A system that is back only after the weekend reopen does not close what it holds then: the gap the
    rule protects against is behind it."""
    at = friday_flat_ms(cfg, now_ms)
    return at if at is not None and at <= now_ms < at + WEEKEND_FLAT_SPAN_MS else None
