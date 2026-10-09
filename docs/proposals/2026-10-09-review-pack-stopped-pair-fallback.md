# Proposal: Daily review pack reads the empty legacy all-pairs db when the pair is stopped by the user

| | |
|---|---|
| id | `2026-10-09-review-pack-stopped-pair-fallback` |
| created | 2026-10-09T04:31:16.498Z by operator-session:20261009T043003Z_daily (review `20261009T043003Z_daily`) |
| pair | XAUUSD |
| branch | `proposal/2026-10-09-review-pack-stopped-pair-fallback` |
| base | `main` (d01310d) |
| commits not in main | 1 (this document's commit only) |
| status | awaiting the owner |

## Problem
When the owner stops XAUUSD (data/instances/XAUUSD/run/manual_stop), tools/health_report.py systems() drops the pair from the expected set and, with no supervisor running, falls back to the all-pairs base (data/app.db). That database has no collector_status or ai_decisions table (D-042/D-051), so the scheduled daily pack shows nothing but errors and the review cannot see the window.

## Numbers
Daily pack 20261009T043003Z_daily (24 h): systems all-pairs, health report failed with OperationalError no such table collector_status, XAUUSD no ai_decisions table, funnel 0 screen closes, 0 triggers, AI usage only 1 diagnose call. Same window with --pair XAUUSD: 51 screen closes (15m 18, 5m 33), 8 triggers, 8 decision calls (7 valid NO_TRADE, 1 error claude_code no answer within 180s), 215,268 input tokens, 12 resolved virtual outcomes all time. Stop: logs/XAUUSD/supervisor-ctl.jsonl 2026-10-08T15:16:56Z stop requested, 15:17:06Z stopped by the user.

## Proposed change
In tools/health_report.py systems(): when pairs is empty (nothing running, nothing expected), return every configured instance that has its own data/instances/PAIR/app.db (stopped ones included, with a note: PAIR stopped by the user) before falling back to base; fall back to base only when no instance has an app.db. review_pack.choose_systems inherits the fix. Optionally the pack prints that the system is stopped by the user since the manual_stop mtime.

## Risk
Reporting only, no effect on trading. A stopped pair is reported with stale heartbeats; the note makes clear it is an owner stop, not a crash, so the monitor and sessions do not misread it.

## Test plan
Unit test of systems() with a temp data dir: one instance with app.db and manual_stop, no supervisors running, expects that instance (not base) plus the stopped note. Then run tools/review_pack.py --hours 24 --print with XAUUSD stopped and check the funnel matches the --pair XAUUSD output.

---
Accept: merge `proposal/2026-10-09-review-pack-stopped-pair-fallback` (this document then stands as a pending task in PROJECT_STATUS.md). Reject: `git worktree remove C:\TradingSystems\XAUUSD_wt\proposal-2026-10-09-review-pack-stopped-pair-fallback` and `git branch -D proposal/2026-10-09-review-pack-stopped-pair-fallback`.
