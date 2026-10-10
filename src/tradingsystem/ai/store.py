"""Decision persistence (P8.7, spec §8.3) in ``app.db``.

For every AI decision the chain is stored and linked: payload (zlib-compressed JSON, keyed by payload hash) →
prompt hash / provider / model / config hash / git SHA → raw model text → validated recommendation (or the
validation errors) → execution state → outcome (filled by the execution layer).
Sub-agent outputs (timeframe analysts, risk reviewer, consensus members) are stored alongside.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.settings import PROJECT_ROOT
from ..core.timeutil import MS_PER_DAY, now_ms
from ..storage.sqlite_store import connect

_DDL = [
    """CREATE TABLE IF NOT EXISTS ai_payloads (
        payload_hash TEXT PRIMARY KEY, ts INTEGER NOT NULL, pair TEXT NOT NULL, payload_z BLOB NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ai_decisions (
        id TEXT PRIMARY KEY, ts INTEGER NOT NULL, pair TEXT NOT NULL, mode TEXT NOT NULL, trigger TEXT,
        provider TEXT, model TEXT, prompt_hash TEXT, payload_hash TEXT, config_hash TEXT, git_sha TEXT,
        status TEXT NOT NULL, decision TEXT, order_type TEXT, confidence INTEGER, rr_computed REAL,
        valid_until INTEGER, recommendation TEXT, raw_text TEXT, errors TEXT, cost_usd REAL, latency_ms INTEGER,
        input_tokens INTEGER, output_tokens INTEGER,
        execution_state TEXT NOT NULL DEFAULT 'not_executed', execution_detail TEXT,
        outcome TEXT, outcome_pnl_usd REAL, outcome_pnl_pct REAL, outcome_pips REAL, outcome_ts INTEGER,
        virtual_outcome TEXT, virtual_r REAL)""",
    "CREATE INDEX IF NOT EXISTS ai_decisions_pair_ts ON ai_decisions(pair, ts)",
    "CREATE INDEX IF NOT EXISTS ai_decisions_exec ON ai_decisions(execution_state)",
    """CREATE TABLE IF NOT EXISTS ai_sub_outputs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL, role TEXT NOT NULL, label TEXT,
        provider TEXT, model TEXT, prompt_hash TEXT, ok INTEGER, output TEXT, errors TEXT, cost_usd REAL)""",
    "CREATE INDEX IF NOT EXISTS ai_sub_outputs_decision ON ai_sub_outputs(decision_id)",
    # the engine's small persistent state per pair (last setup signature, price at the last call, last event id)
    "CREATE TABLE IF NOT EXISTS engine_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_ms INTEGER NOT NULL)",
    # Phase 4 (§3.8): what happened after a decision, measured on real bars; the prompt versions in force; tuning
    """CREATE TABLE IF NOT EXISTS decision_metrics (decision_id TEXT PRIMARY KEY, computed_ms INTEGER NOT NULL,
        mfe_r REAL, mae_r REAL, tp1_hit INTEGER, tp2_hit INTEGER, tp3_hit INTEGER, minutes_to_resolve INTEGER,
        exit_reason TEXT, slippage REAL, spread_at_gate REAL, commission REAL, swap REAL,
        rejected_but_virtual_win INTEGER, no_trade_counterfactual_atr REAL, detail TEXT)""",
    # keyed on the system prompt AND the library: an edit of a user template (instructions.md) leaves prompt_hash
    # unchanged but changes library_hash, and is a new version of what the model saw
    """CREATE TABLE IF NOT EXISTS prompt_versions (prompt_hash TEXT NOT NULL, role TEXT NOT NULL,
        library_hash TEXT NOT NULL, versions TEXT NOT NULL, git_sha TEXT, first_seen_ms INTEGER NOT NULL,
        PRIMARY KEY (prompt_hash, library_hash))""",
    """CREATE TABLE IF NOT EXISTS tuning_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
        pair TEXT NOT NULL, key TEXT NOT NULL, old_value TEXT, new_value TEXT, reason TEXT, evidence TEXT,
        window_hours INTEGER, expires_ms INTEGER, review_id TEXT, actor TEXT, reverted_ms INTEGER)""",
    "CREATE INDEX IF NOT EXISTS tuning_changes_pair_ts ON tuning_changes(pair, ts)",
]

# Phase 3 columns (added in place on existing databases; old code ignores them)
EXTRA_COLUMNS = (("actions_state", "TEXT"), ("library_hash", "TEXT"), ("trigger_strength", "TEXT"),
                 ("setup_strength", "TEXT"),
                 # Phase 4 attribution (at record time) and the broker's outcome split (at settlement)
                 ("setup_kinds", "TEXT"), ("session", "TEXT"), ("regime", "TEXT"), ("htf_bias", "TEXT"),
                 ("data_warnings", "TEXT"), ("playbook_hash", "TEXT"), ("adaptive_hash", "TEXT"),
                 ("outcome_detail", "TEXT"))
ACTION_STATES = ("pending", "done")
METRIC_COLUMNS = ("decision_id", "computed_ms", "mfe_r", "mae_r", "tp1_hit", "tp2_hit", "tp3_hit", "minutes_to_resolve",
                  "exit_reason", "slippage", "spread_at_gate", "commission", "swap", "rejected_but_virtual_win",
                  "no_trade_counterfactual_atr", "detail")

EXECUTION_STATES = ("not_executed", "queued", "executing", "executed", "rejected", "expired", "cancelled")
# 'interrupted' (Phase 5 A5): no answer because the machine slept during the cycle — like 'error' it consumes nothing
# (setup, events, review stay unseen), unlike it the engine does not back off for it
STATUSES = ("valid", "invalid", "refused", "budget_blocked", "error", "skipped", "interrupted")
# D-055, the desk's trade journal: the window, at most this many trades, the reason text kept, the lessons shown
JOURNAL_HOURS, JOURNAL_TRADES, JOURNAL_REASON_CHARS, JOURNAL_LESSONS = 24, 6, 140, 5


def git_sha() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT, capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


@dataclass
class DecisionRecord:
    pair: str
    mode: str
    trigger: str
    status: str
    provider: str | None = None
    model: str | None = None
    prompt_hash: str | None = None
    payload_hash: str | None = None
    recommendation: dict | None = None
    raw_text: str | None = None
    errors: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    rr_computed: float | None = None
    sub_outputs: list[dict] = field(default_factory=list)
    trigger_strength: str | None = None      # strong | weak | review | event | idle | close | manual
    library_hash: str | None = None          # the whole prompt library's hash (prompt versions in force)
    actions_state: str | None = None         # 'pending' while position_actions await the executor
    setup_strength: str | None = None        # a setup that would have called by itself (also under an event label)
    # Phase 4 attribution: what the market looked like when the decision was made (from its payload)
    setup_kinds: list[str] | None = None     # kinds of the setup reasons on screen (structure, location, pattern, flow)
    session: str | None = None               # killzone or active session
    regime: str | None = None                # decision-TF regime
    htf_bias: str | None = None              # confluence bias
    data_warnings: list[str] | None = None
    playbook_hash: str | None = None         # the pair's playbook in force (adaptive overlay)
    adaptive_hash: str | None = None         # the pair's adaptive values in force
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: int = field(default_factory=now_ms)
    # not stored: the pair's effective tunables the prompts were rendered with (core/tunables.py) — attribution
    # takes the hashes from it, not from a later read (the overlay may change during a long model call)
    tunables: Any = field(default=None, repr=False, compare=False)


class DecisionStore:
    def __init__(self, app_db: Path, config_hash: str = "") -> None:
        self._con = connect(app_db, cache_mb=4)
        self._lock = threading.Lock()
        self.config_hash = config_hash
        self.git_sha = git_sha()
        # (prompt hash, library hash) pairs registered by this process (prompt_versions)
        self._prompts_seen: set[tuple[str, str]] = set()
        with self._lock:
            for s in _DDL:
                self._con.execute(s)
            have = {r[1] for r in self._con.execute("PRAGMA table_info(ai_decisions)")}
            for col, typ in EXTRA_COLUMNS:          # several services open the store concurrently
                if col not in have:
                    try:
                        self._con.execute(f"ALTER TABLE ai_decisions ADD COLUMN {col} {typ}")
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc):
                            raise

    def save_payload(self, payload_hash: str, pair: str, payload: dict) -> None:
        blob = zlib.compress(json.dumps(payload, separators=(",", ":"), default=str).encode(), 6)
        with self._lock:
            self._con.execute("INSERT OR IGNORE INTO ai_payloads VALUES (?,?,?,?)", (payload_hash, now_ms(), pair, blob))

    def load_payload(self, payload_hash: str) -> dict | None:
        with self._lock:
            r = self._con.execute("SELECT payload_z FROM ai_payloads WHERE payload_hash=?", (payload_hash,)).fetchone()
        return json.loads(zlib.decompress(r[0])) if r else None

    def save(self, rec: DecisionRecord) -> str:
        r = rec.recommendation or {}
        vu = r.get("valid_until")
        valid_until = None
        if vu:
            from ..core.timeutil import parse_date_spec
            valid_until = parse_date_spec(vu)
        with self._lock:
            self._con.execute(
                """INSERT INTO ai_decisions(id, ts, pair, mode, trigger, provider, model, prompt_hash, payload_hash,
                   config_hash, git_sha, status, decision, order_type, confidence, rr_computed, valid_until,
                   recommendation, raw_text, errors, cost_usd, latency_ms, input_tokens, output_tokens,
                   trigger_strength, library_hash, actions_state, setup_strength, setup_kinds, session, regime,
                   htf_bias, data_warnings, playbook_hash, adaptive_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rec.id, rec.ts, rec.pair, rec.mode, rec.trigger, rec.provider, rec.model, rec.prompt_hash,
                 rec.payload_hash, self.config_hash, self.git_sha, rec.status, r.get("decision"), r.get("order_type"),
                 r.get("confidence"), rec.rr_computed, valid_until,
                 json.dumps(r, default=str) if r else None, rec.raw_text, json.dumps(rec.errors) if rec.errors else None,
                 rec.cost_usd, rec.latency_ms, rec.input_tokens, rec.output_tokens, rec.trigger_strength,
                 rec.library_hash, rec.actions_state, rec.setup_strength,
                 json.dumps(rec.setup_kinds) if rec.setup_kinds is not None else None, rec.session, rec.regime,
                 rec.htf_bias, json.dumps(rec.data_warnings) if rec.data_warnings is not None else None,
                 rec.playbook_hash, rec.adaptive_hash))
            for s in rec.sub_outputs:
                self._con.execute(
                    "INSERT INTO ai_sub_outputs(decision_id, role, label, provider, model, prompt_hash, ok, output, errors, cost_usd) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (rec.id, s.get("role"), s.get("label"), s.get("provider"), s.get("model"), s.get("prompt_hash"),
                     int(bool(s.get("ok"))), json.dumps(s.get("output"), default=str) if s.get("output") is not None else None,
                     json.dumps(s.get("errors")) if s.get("errors") else None, s.get("cost_usd", 0.0)))
        return rec.id

    def set_execution_state(self, decision_id: str, state: str, detail: dict | None = None) -> None:
        if state not in EXECUTION_STATES:
            raise ValueError(state)
        with self._lock:
            self._con.execute("UPDATE ai_decisions SET execution_state=?, execution_detail=? WHERE id=?",
                              (state, json.dumps(detail, default=str) if detail else None, decision_id))

    # ------------------------------------------------------------------ engine state (Phase 3)
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            r = self._con.execute("SELECT value FROM engine_kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._con.execute("INSERT INTO engine_kv(key, value, updated_ms) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                              "SET value=excluded.value, updated_ms=excluded.updated_ms",
                              (key, json.dumps(value, default=str), now_ms()))

    def losing_trades_since(self, pair: str, since_ms: int) -> int:
        """D-052: the pair's executed decisions settled ``closed_loss`` since ``since_ms`` (the day's loss stop)."""
        with self._lock:
            r = self._con.execute("SELECT count(*) FROM ai_decisions WHERE pair=? AND outcome='closed_loss' AND "
                                  "outcome_ts>=?", (pair, since_ms)).fetchone()
        return int(r[0] or 0)

    def events_after(self, after_id: int, kinds: tuple[str, ...], collector: str = "executor",
                     limit: int = 200) -> list[dict[str, Any]]:
        """``ingestion_events`` rows of ``collector`` newer than ``after_id`` (the executor's fills, closes, outcomes,
        actions — they wake the model). Empty when the table does not exist yet."""
        try:
            with self._lock:
                rows = self._con.execute(
                    f"SELECT id, ts, event, detail FROM ingestion_events WHERE collector=? AND id>? AND event IN "
                    f"({','.join('?' * len(kinds))}) ORDER BY id LIMIT ?", (collector, after_id, *kinds, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [{"id": r[0], "ts": r[1], "event": r[2], "detail": r[3]} for r in rows]

    def last_event_id(self) -> int:
        try:
            with self._lock:
                r = self._con.execute("SELECT COALESCE(max(id), 0) FROM ingestion_events").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(r[0])

    def count_strength_since(self, pair: str, strength: str, since_ms: int, *, without_setup: bool = False) -> int:
        """Calls of ``pair`` with this trigger strength since ``since_ms``; ``without_setup``: only those no setup
        would have made by itself (an event call that co-fired with a due setup is not an extra call)."""
        q = "SELECT count(*) FROM ai_decisions WHERE pair=? AND trigger_strength=? AND ts>=?"
        if without_setup:
            q += " AND setup_strength IS NULL"
        with self._lock:
            return int(self._con.execute(q, (pair, strength, since_ms)).fetchone()[0])

    def pending_actions(self, pair: str, since_ms: int) -> list[dict[str, Any]]:
        """Valid decisions of ``pair`` whose ``position_actions`` the executor has not handled yet."""
        with self._lock:
            rows = self._con.execute(
                "SELECT id, ts, recommendation FROM ai_decisions WHERE pair=? AND status='valid' "
                "AND actions_state='pending' AND ts>=? ORDER BY ts", (pair, since_ms)).fetchall()
        return [{"id": r[0], "ts": r[1], "rec": json.loads(r[2]) if r[2] else {}} for r in rows]

    def set_actions_state(self, decision_id: str, state: str) -> None:
        if state not in ACTION_STATES:
            raise ValueError(state)
        with self._lock:
            self._con.execute("UPDATE ai_decisions SET actions_state=? WHERE id=?", (state, decision_id))

    def set_outcome(self, decision_id: str, outcome: str, pnl_usd: float | None, pnl_pct: float | None,
                    pips: float | None, detail: dict | None = None) -> None:
        """The executed decision's real result. ``outcome_ts`` is the settlement time (not the close time);
        ``detail`` (Phase 4, ``outcome_detail``) is the venue's split of it — commission / swap / fee, open and close
        times, how each leg ended — which only the settlement sees (see :mod:`..execution.metrics`)."""
        with self._lock:
            self._con.execute("UPDATE ai_decisions SET outcome=?, outcome_pnl_usd=?, outcome_pnl_pct=?, outcome_pips=?, "
                              "outcome_ts=?, outcome_detail=? WHERE id=?",
                              (outcome, pnl_usd, pnl_pct, pips, now_ms(),
                               json.dumps(detail, default=str) if detail else None, decision_id))

    # ------------------------------------------------------------------ Phase 4: decision metrics, prompt registry
    def pending_metrics(self, limit: int = 20, *, no_trade_before_ms: int,
                        pairs: list[str] | tuple[str, ...] | None = None,
                        exclude: list[str] | tuple[str, ...] = (),
                        unsettled_before_ms: int | None = None) -> list[dict[str, Any]]:
        """Valid decisions whose ``decision_metrics`` row is due, oldest first:

        * executed trades once the venue settled them (``outcome``) — again when a later settlement (``outcome_ts``)
          is newer than the row (an idea first scored virtually, executed afterwards from the manual queue; an
          executed trade first recorded as unsettled);
        * executed trades recorded before ``unsettled_before_ms`` that never settled (Phase 5 A7; None: not asked),
          once — the settlement, if it ever comes, is newer than that row;
        * trade ideas not executed once their virtual outcome is known;
        * NO_TRADE answers recorded before ``no_trade_before_ms`` (their counterfactual needs the next bars).

        ``computed_ms`` is the existing row's time (None when there is none); ``exclude``: ids the caller will not
        score now (waiting for bars)."""
        unsettled = ("" if unsettled_before_ms is None else """
            OR (d.decision IN ('BUY','SELL') AND d.execution_state='executed' AND d.outcome IS NULL AND d.ts<=?
                AND m.decision_id IS NULL)""")
        where = ["d.status='valid'", f"""(
            (d.decision IN ('BUY','SELL') AND d.execution_state='executed' AND d.outcome IS NOT NULL
             AND (m.decision_id IS NULL OR COALESCE(d.outcome_ts, 0) > m.computed_ms))
            OR (d.decision IN ('BUY','SELL') AND d.execution_state!='executed' AND d.virtual_outcome IS NOT NULL
                AND m.decision_id IS NULL)
            OR (d.decision='NO_TRADE' AND d.ts<=? AND m.decision_id IS NULL){unsettled})"""]
        args: list[Any] = [int(no_trade_before_ms)]
        if unsettled_before_ms is not None:
            args.append(int(unsettled_before_ms))
        if pairs is not None:
            if not pairs:
                return []
            where.append(f"d.pair IN ({','.join('?' * len(pairs))})")
            args += list(pairs)
        if exclude:
            where.append(f"d.id NOT IN ({','.join('?' * len(exclude))})")
            args += list(exclude)
        with self._lock:
            rows = self._con.execute(
                "SELECT d.id, d.ts, d.pair, d.decision, d.recommendation, d.execution_state, d.execution_detail, "
                "d.outcome, d.outcome_ts, d.outcome_detail, d.virtual_outcome, d.virtual_r, d.payload_hash, "
                "m.computed_ms FROM ai_decisions d LEFT JOIN decision_metrics m ON m.decision_id = d.id "
                f"WHERE {' AND '.join(where)} ORDER BY d.ts, d.id LIMIT ?", (*args, int(limit))).fetchall()
        out = []
        for r in rows:
            out.append({"id": r[0], "ts": r[1], "pair": r[2], "decision": r[3], "recommendation": _loads(r[4]) or {},
                        "execution_state": r[5], "execution_detail": _loads(r[6]), "outcome": r[7],
                        "outcome_ts": r[8], "outcome_detail": _loads(r[9]), "virtual_outcome": r[10],
                        "virtual_r": r[11], "payload_hash": r[12], "computed_ms": r[13]})
        return out

    def save_metrics(self, row: dict[str, Any]) -> None:
        """Insert or replace one ``decision_metrics`` row (``decision_id`` required; ``computed_ms`` defaults to
        now; ``detail`` is stored as JSON)."""
        vals = {**row, "computed_ms": int(row.get("computed_ms") or now_ms())}
        det = vals.get("detail")
        vals["detail"] = json.dumps(det, default=str) if det is not None and not isinstance(det, str) else det
        cols = METRIC_COLUMNS
        with self._lock:
            self._con.execute(f"INSERT OR REPLACE INTO decision_metrics ({', '.join(cols)}) "
                              f"VALUES ({','.join('?' * len(cols))})", [vals.get(c) for c in cols])

    def metrics_of(self, decision_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._con.execute(f"SELECT {', '.join(METRIC_COLUMNS)} FROM decision_metrics WHERE decision_id=?",
                                  (decision_id,)).fetchone()
        if not r:
            return None
        out = dict(zip(METRIC_COLUMNS, r))
        out["detail"] = _loads(out["detail"])
        return out

    def prompt_known(self, prompt_hash: str, library_hash: str) -> bool:
        """Whether this process already registered ``prompt_hash`` under ``library_hash`` (no database read)."""
        return (prompt_hash, library_hash) in self._prompts_seen

    def register_prompt(self, prompt_hash: str, role: str, library_hash: str, versions: dict[str, int]) -> None:
        """``prompt_versions``: the first time a system prompt is seen with a prompt library, with the versions of the
        files the render used (INSERT OR IGNORE keeps the first sighting; the engine and the executor may race). A
        user-template edit keeps the system prompt's hash but changes the library's, so it gets its own row."""
        with self._lock:
            self._con.execute("INSERT OR IGNORE INTO prompt_versions (prompt_hash, role, library_hash, versions, "
                              "git_sha, first_seen_ms) VALUES (?,?,?,?,?,?)",
                              (prompt_hash, role, library_hash, json.dumps(versions, sort_keys=True), self.git_sha,
                               now_ms()))
            self._prompts_seen.add((prompt_hash, library_hash))

    def recent(self, pair: str, limit: int = 5) -> list[dict[str, Any]]:
        """Compact history for the snapshot (spec rule 10: consistency with recent decisions)."""
        with self._lock:     # 'skipped' rows never reached the model (data gate) — not part of its history
            rows = self._con.execute(
                "SELECT ts, status, decision, order_type, confidence, recommendation, execution_state, outcome, "
                "outcome_pnl_pct, virtual_outcome, execution_detail FROM ai_decisions WHERE pair=? AND status!='skipped' "
                "ORDER BY ts DESC LIMIT ?", (pair, limit)).fetchall()
        out = []
        for ts, status, dec, ot, conf, rec, ex, outc, pnl, vo, det in rows:
            r = json.loads(rec) if rec else {}
            h = {"time": _iso(ts), "status": status, "decision": dec, "order_type": ot, "confidence": conf,
                 "entry": r.get("entry"), "stop_loss": r.get("stop_loss"),
                 "take_profits": [tp.get("price") for tp in r.get("take_profits", [])],
                 "summary": (r.get("market_summary") or "")[:200], "execution_state": ex, "outcome": outc,
                 "outcome_pnl_pct": pnl, "virtual_outcome": vo}
            if ex == "rejected" and det:
                by, why = _rejection(det)
                h["rejected_by"] = by
                if why:
                    h["gate_reason" if by == "gate" else "reject_reason"] = why[:160]
            elif ex == "not_executed" and det:
                h.update(_shadow_verdict(det))      # D-049: a shadow desk's idea carries the gate's verdict too
            out.append(h)
        return out

    def memory(self, pair: str) -> dict[str, Any]:
        """The model's own notes from its latest valid decision on ``pair`` (Phase 1 operator memory)."""
        with self._lock:     # the latest valid decision that carries notes (a later one without notes keeps them)
            r = self._con.execute(
                "SELECT ts, decision, execution_state, recommendation FROM ai_decisions WHERE pair=? AND status='valid' "
                "AND COALESCE(json_extract(recommendation, '$.operator_notes'), '') != '' ORDER BY ts DESC LIMIT 1",
                (pair,)).fetchone()
        if not r or not r[3]:
            return {}
        notes = (json.loads(r[3]).get("operator_notes") or "").strip()
        return {"time": _iso(r[0]), "decision": r[1], "execution_state": r[2], "notes": notes} if notes else {}

    def performance(self, pair: str, days: int = 30, now: int | None = None) -> dict[str, Any]:
        """The model's record on ``pair`` over ``days``: cycles, answers, trade ideas, gate results, broker and
        virtual outcomes (every idea is scored on real prices, executed or not)."""
        since = (now or now_ms()) - days * MS_PER_DAY
        with self._lock:
            rows = self._con.execute(
                "SELECT status, decision, execution_state, outcome, outcome_pnl_usd, virtual_outcome, virtual_r, "
                "execution_detail FROM ai_decisions WHERE pair=? AND ts>=? AND status!='skipped'", (pair, since)).fetchall()
        if not rows:
            return {}
        ideas = [r for r in rows if r[0] == "valid" and r[1] in ("BUY", "SELL")]
        vo = [r for r in ideas if r[5]]
        closed = [r for r in ideas if r[3] in ("closed_profit", "closed_loss", "closed_breakeven")]
        rs = [r[6] for r in vo if r[5] in ("tp1_first", "sl_first") and r[6] is not None]
        return {
            "days": days, "cycles": len(rows), "answered": sum(r[0] in ("valid", "invalid", "refused") for r in rows),
            "no_trade": sum(r[0] == "valid" and r[1] == "NO_TRADE" for r in rows), "trade_ideas": len(ideas),
            "gate_rejected": sum(r[2] == "rejected" and _rejection(r[7])[0] == "gate" for r in ideas),
            "not_placed": sum(r[2] == "rejected" and _rejection(r[7])[0] != "gate" for r in ideas),
            "expired": sum(r[2] == "expired" for r in ideas), "executed": sum(r[2] == "executed" for r in ideas),
            "virtual": {k: sum(r[5] == k for r in vo) for k in ("tp1_first", "sl_first", "not_triggered", "unresolved_24h")}
            | ({"mean_r": round(sum(rs) / len(rs), 2)} if rs else {}),
            "broker": {"closed": len(closed), "pnl_usd": round(sum(r[4] or 0.0 for r in closed), 2)} if closed else {},
        }

    def journal(self, pair: str, as_of: int, hours: int = JOURNAL_HOURS) -> dict[str, Any]:
        """D-055, the desk's trade journal for the snapshot: the executed trades of ``pair`` placed in the last
        ``hours`` up to ``as_of`` (at most JOURNAL_TRADES, oldest first) - fill, original stop and target, every applied
        stop change and close (time, from → to, by the model or a rule, why), how it ended (``close_ms`` lets the
        snapshot add the price after it), the model's own ``trade_review`` of it; today's (UTC) closed trades and
        lessons. Prices are the execution instrument's. ``{}`` when nothing traded."""
        with self._lock:
            return journal_from(self._con, pair, as_of, hours)

    def last_decision(self, pair: str) -> dict | None:
        with self._lock:
            r = self._con.execute("SELECT id, ts, recommendation FROM ai_decisions WHERE pair=? AND status='valid' "
                                  "ORDER BY ts DESC LIMIT 1", (pair,)).fetchone()
        if not r:
            return None
        return {"id": r[0], "ts": r[1], "recommendation": json.loads(r[2]) if r[2] else None}

    def last_attempt_ts(self, pair: str, *, answered: bool = False) -> int | None:
        """Time of the latest stored cycle for ``pair`` in any status (spacing survives restarts and failed cycles
        count — F1); ``answered``: only cycles where the model answered (valid / invalid / refused) — those consume
        a pending review, a failed call or a data-gate skip does not."""
        q = "SELECT max(ts) FROM ai_decisions WHERE pair=?" + (
            " AND status IN ('valid','invalid','refused')" if answered else "")
        with self._lock:
            r = self._con.execute(q, (pair,)).fetchone()
        return int(r[0]) if r and r[0] is not None else None

    def realised_since(self, since_ms: int) -> tuple[float, int]:
        """(realised PnL USD, closed trades) since ``since_ms`` — the Cost Governor's ROI input (D-004, F13)."""
        with self._lock:
            r = self._con.execute(
                "SELECT COALESCE(sum(outcome_pnl_usd),0), count(*) FROM ai_decisions "
                "WHERE outcome IN ('closed_profit','closed_loss','closed_breakeven') AND outcome_ts>=?",
                (since_ms,)).fetchone()
        return float(r[0]), int(r[1])

    def close(self) -> None:
        with self._lock:
            self._con.close()


def _rejection(detail_json: str | None) -> tuple[str, str]:
    """(who rejected — gate / broker / system, why) from an execution_detail: the risk gate records its check list
    and never reaches the backend; a broker refusal carries the backend's result."""
    try:
        d = json.loads(detail_json) if detail_json else {}
    except ValueError:
        return "system", ""
    if not isinstance(d, dict):
        return "system", ""
    if d.get("backend"):
        return "broker", str((d["backend"] or {}).get("reason") or d.get("reason") or "")
    if d.get("gate"):
        return "gate", str(d.get("reason") or "")
    return "system", str(d.get("reason") or "")


def _shadow_verdict(detail_json: str | None) -> dict[str, Any]:
    """For a shadow desk's idea (D-049, ``execution_detail.shadow``): ``shadow`` 1, ``desk_ok`` 1/0 and the failed
    checks the desk is held to as ``gate_reason`` (the account-size checks are waived) - the model's feedback that a
    rejected idea gets as ``gate_reason``. Empty for any other ``not_executed`` row (an idea not picked up yet)."""
    d = _loads(detail_json)
    if not isinstance(d, dict) or not d.get("shadow"):
        return {}
    failed = set(d.get("desk_failed") or [])
    why = "; ".join(f"{g.get('check')}: {g.get('detail')}" for g in d.get("gate") or []
                    if isinstance(g, dict) and not g.get("ok") and g.get("check") in failed)
    why = why or str(d.get("reason") or d.get("error") or "")
    return {"shadow": 1, "desk_ok": int(bool(d.get("desk_ok"))), **({"gate_reason": why[:160]} if why else {})}



def journal_from(con: sqlite3.Connection, pair: str, as_of: int, hours: int = JOURNAL_HOURS,
                 max_trades: int = JOURNAL_TRADES, max_lessons: int = JOURNAL_LESSONS) -> dict[str, Any]:
    """:meth:`DecisionStore.journal` on any connection - the snapshot's limits by default; the dashboard reads
    it read-only over more hours and trades."""
    since = as_of - hours * 3_600_000
    day0 = as_of - as_of % MS_PER_DAY
    rows = con.execute(
        "SELECT id, ts, decision, order_type, execution_detail, outcome, outcome_ts, outcome_pnl_usd, "
        "outcome_detail FROM ai_decisions WHERE pair=? AND execution_state='executed' AND ts>=? AND ts<=? "
        "ORDER BY ts DESC LIMIT ?", (pair, since, as_of, max_trades)).fetchall()[::-1]
    if not rows:
        return {}
    reviews = con.execute(
        "SELECT json_extract(recommendation, '$.trade_review') FROM ai_decisions WHERE pair=? AND "
        "status='valid' AND ts>=? AND ts<=? AND json_extract(recommendation, '$.trade_review') IS NOT NULL "
        "ORDER BY ts", (pair, since, as_of)).fetchall()
    ids = [r[0] for r in rows]
    q = ",".join("?" * len(ids))
    try:
        acts = con.execute(
            f"SELECT ts, source, source_decision, target_decision, action, requested, status FROM "
            f"position_actions WHERE ts<=? AND (target_decision IN ({q}) OR (source='rule' AND "
            f"source_decision IN ({q}))) ORDER BY ts, id", (as_of, *ids, *ids)).fetchall()
    except sqlite3.OperationalError:                     # no executor has written to this database yet
        acts = []
    try:                                                 # the paper account's fills (backtests, paper mode)
        fills = dict(con.execute(
            f"SELECT decision_id, min(fill_price) FROM paper_legs WHERE decision_id IN ({q}) AND "
            f"fill_price IS NOT NULL AND fill_ms<=? GROUP BY decision_id", (*ids, as_of)).fetchall())
    except sqlite3.OperationalError:
        fills = {}
    review: dict[str, dict] = {}
    for (rv,) in reviews:                                    # the latest review of a trade wins
        r = _loads(rv)
        if isinstance(r, dict) and r.get("decision"):
            review[str(r["decision"])[:8]] = {k: r[k] for k in ("cause", "lesson", "idea_alive", "re_entry")
                                              if r.get(k) not in (None, "")}
    trades, closed_today = [], []
    for did, ts, side, ot, ex_j, outc, out_ts, pnl, od_j in rows:
        ex, od = _loads(ex_j) or {}, _loads(od_j) or {}
        lv = ex.get("executed_levels") or {}
        stop0, tps = lv.get("stop_loss"), lv.get("take_profits") or []
        target = tps[0] if tps else None
        legs = [x for x in (od.get("legs") or []) if isinstance(x, dict)]
        bk = ex.get("backend") if isinstance(ex.get("backend"), dict) else {}
        placed = [x for x in (bk.get("placed") or []) if isinstance(x, dict)]
        fill = next((x["fill_price"] for x in legs if x.get("fill_price") is not None), None)
        if fill is None:
            fill = fills.get(did)
        if fill is None and ot == "MARKET" and placed:
            fill = placed[0].get("price")
        t: dict[str, Any] = {"decision": did[:8], "side": side, "order_type": ot, "placed": _iso(ts),
                             "entry": fill if fill is not None else lv.get("entry"), "filled": int(fill is not None),
                             "stop": stop0, "target": target}
        sl_now, changes, refused = stop0, [], 0
        for a_ts, src, sd, td, act, req_j, st in acts:
            if (sd if src == "rule" else td) != did:
                continue
            rq = _loads(req_j) or {}
            why = (rq.get("reason") or rq.get("rule") or "")[:JOURNAL_REASON_CHARS]
            if act in ("modify_sl", "set_sl"):
                if st == "rejected" and src == "model":
                    refused += 1
                elif st == "applied" and rq.get("value") is not None:
                    changes.append({"at": _iso(a_ts), "stop": [sl_now, rq["value"]], "by": src, "why": why})
                    sl_now = rq["value"]
            elif st == "applied":
                changes.append({"at": _iso(a_ts), "action": act, "by": src, "why": why})
        if changes:
            t["changes"] = changes
        if refused:
            t["stop_changes_refused"] = refused
        if out_ts is not None and int(out_ts) > as_of:       # settled after the cycle time: not known yet
            outc = None
        if outc in ("closed_profit", "closed_loss", "closed_breakeven"):
            reason = next((x["reason"] for x in legs if x.get("reason")), None) or "other"
            exit_px = next((x["close_price"] for x in legs if x.get("close_price") is not None), None)
            if exit_px is None:                              # settled before the close price was recorded
                exit_px = sl_now if reason == "sl" else target if reason == "tp" else None
            close_ms = int(od.get("close_ms") or out_ts or ts)
            t.update({"status": "closed", "closed": _iso(close_ms), "close_ms": close_ms, "exit": reason,
                      "exit_price": exit_px, "pnl_usd": pnl})
            if fill is not None and stop0 is not None and exit_px is not None and abs(fill - stop0) > 0:
                t["r"] = round(((exit_px - fill) if side == "BUY" else (fill - exit_px)) / abs(fill - stop0), 2)
            if close_ms >= day0:
                closed_today.append(t)
        else:
            t["status"] = outc or ("open" if fill is not None else "pending")
        if did[:8] in review:
            t["review"] = review[did[:8]]
        trades.append(t)
    out: dict[str, Any] = {"trades": trades}
    if closed_today:
        out["today"] = {"closed": len(closed_today),
                        "losses": sum((x.get("pnl_usd") or 0.0) < 0 for x in closed_today),
                        "pnl_usd": round(sum(x.get("pnl_usd") or 0.0 for x in closed_today), 2)}
    lessons = [v["lesson"] for v in review.values() if v.get("lesson")]
    if lessons:
        out["lessons"] = lessons[-max_lessons:]
    return out

def _loads(text: str | None) -> Any:
    """JSON column → value; None for NULL or unreadable text (a bad row never breaks a reader)."""
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _iso(ms: int) -> str:
    from ..core.timeutil import iso
    return iso(ms)
