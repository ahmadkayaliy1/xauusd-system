# One folder per pair (D-051)

Since 2026-10-01 every pair is its own, fully independent project folder:

| Folder | Pair | Dashboard | MT5 magic | Task Scheduler folder | Daily / weekly review (UTC) | Backup (UTC) |
|---|---|---|---|---|---|---|
| `C:\TradingSystems\XAUUSD` | XAUUSD (shadow desk, D-049) | 127.0.0.1:8768 | 26092504 | `\TradingSystems\XAUUSD\` | 04:30 / Sun 06:00 | 03:30 |
| `C:\TradingSystems\BTCUSDT` | BTCUSDT | 127.0.0.1:8766 | 26092502 | `\TradingSystems\BTCUSDT\` | 04:55 / Sun 06:45 | 03:40 |
| `C:\TradingSystems\ETHUSDT` | ETHUSDT | 127.0.0.1:8767 | 26092503 | `\TradingSystems\ETHUSDT\` | 05:20 / Sun 07:30 | 03:50 |

Each folder has its own code (its own git repository; the history up to the split is copied, there is no remote), its
own `.venv`, `config\config.yaml` (its one pair) and `config\config.local.yaml`, `.env`, `data\` (market data, state,
AI ledger, account peak, locks, news calendar, reviews), `logs\`, `backups\`, its own scheduled tasks and its own
Claude CLI work folder (`%TEMP%\tradingsystem-claude-code-<PAIR>-<hash>`). No folder reads, writes, stops or registers
anything of another folder. What the three still have in common is outside the folders: the laptop, the one MT5
terminal and the one Windsor demo account, the Binance IP limit and the Claude subscription.

## What is the same as before

Everything that decides a trade: the pair's instruments, prompts, payload, charts, screening, risk numbers, AI
settings and limits, the magic number (so the open trades of a pair are still its own), the dashboard port, the
monitor rules. `tests/unit/test_split_pairs.py` compares each folder's config with the three-pair config it came from
(`tests/fixtures/config_three_pairs.yaml`): only the D-051 keys may differ.

## What the split changes (the owner's choices, 2026-10-01)

- **Each system sees only its own trades.** The correlated-exposure cap (4 %) counts only this system's positions;
  open positions (3), the daily-loss limits (10 %) and the same-direction guard were per pair already. The account
  drawdown stop (25 %) still compares the account's equity, read from the broker, with its peak - each folder keeps its
  own peak file (there is no per-pair balance).
- **Every folder's locks are its own.** Two folders may start a Claude CLI at the same moment (a rare OAuth refresh
  race is retried after `ai.transient_retry_s`), and two crypto orders in the same second could pass their caps
  separately (gold never sends orders).
- **BTC and ETH keep each other as the `market.cross` sibling (B6)**: each system records the other's candles itself
  (`core.instruments.sibling_feeds`, live ingest only; the history was copied at the switch). The sibling's pair entry
  in the config is `enabled: false` - it is never analysed or traded there.
- **Gold stays apart** from the crypto pairs: `risk.correlated_groups: []`, no crypto data in its folder.
- The Binance REST budget is 1000 weight/min per folder (the old 3000 shared out by three systems).
- The price recorder (P1.12) records only its folder's pair; the monitor, the backups and the review sessions are per
  folder (three reviews a day instead of one, spaced so two never run at once).
- A review session cannot engage its folder's only kill switch (`tools/kill_switch.py` refuses to stop the last pair
  still trading - unchanged); the monitor and the owner can.

## The switch (H33)

With the old three-pair checkout `C:\the_claude_new` still running, run in each pair folder, in any order:

```
C:\TradingSystems\XAUUSD\scripts\first_start.bat C:\the_claude_new
C:\TradingSystems\BTCUSDT\scripts\first_start.bat C:\the_claude_new
C:\TradingSystems\ETHUSDT\scripts\first_start.bat C:\the_claude_new
```

Each one: checks that its folder runs nothing yet; stops only its pair in the old checkout (the old checkout's own
`run --stop --instance <PAIR>`, run from there - never the old `stop_all.bat`, whose command-line search would also find
a pair folder that already switched); deletes the old keep-alive task `\TradingSystem-<PAIR>` and the old operator
tasks `\TradingSystemOps-*`; writes the old recorder's STOP file; imports the pair's data
(`tools\import_from_old.py`: hot stores copied with the SQLite backup API, the cold Parquet archive hard-linked - no
extra disk space - the AI ledger with this pair's and the operator sessions' rows only, the account peak, the pair's
state, reviews, logs, the recorder files filtered to this pair, `config.local.yaml` and `.env`); registers the folder's
own tasks (`install_autostart.ps1 -AllPairs`, `install_operator_tasks.ps1`) and starts the system and its recorder.

Then, per folder: `scripts\status_all.bat` and `scripts\check_ops.bat`. The old `\TradingSystem-MT5` task stays (deleting
it may close the running terminal); remove it later with MT5 closed: `schtasks /Delete /TN "\TradingSystem-MT5" /F`.
The old checkout is then an archive: never start it again next to the pair folders (same magic numbers).

Day to day, in a pair folder: `scripts\start_all.bat`, `stop_all.bat`, `status_all.bat`, `restart_all.bat` act on its
one pair (or `start.bat <PAIR>` etc.). A plain `start.bat` without a pair would start an all-pairs system there - not
used.

## Rollback

Soon after the switch: in each pair folder `scripts\stop_all.bat`, `scripts\uninstall_autostart.bat`,
`scripts\install_operator_tasks.bat -Uninstall`; then in `C:\the_claude_new`: delete
`data\research\price_matching\STOP`, `scripts\install_autostart.bat -AllPairs`, `scripts\install_operator_tasks.bat`,
`scripts\start_all.bat`, `scripts\start_recorder.bat`. The old checkout's data was never written by the import; what
the pair folders recorded after the switch (decisions, trades, ledger rows) stays in their folders.
