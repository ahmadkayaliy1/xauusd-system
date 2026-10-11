"""Where the scalp job keeps its files (D-058): ``<data>/scalp/`` - a folder of its own, never the desk's state.

    briefs/<YYYYMMDD>/<HHMM>Z.json   the stored record (the model's brief + the code's checks + the score)
    briefs/<YYYYMMDD>/<HHMM>Z.md     the same as the owner reads it (also the Telegram / toast text)
    latest.json                      the newest record (the dashboard's first read)
    status.json                      the loop / last run (pid, next_run, last_run, outcome)
    stats.json                       the hit-rate over the last N scored briefs (journal.stats)
    manual_trades.json               the owner's manual deals of the last days (scorer.manual_trades)
    charts/<tf>.png                  the brief's own chart images (never the engine's folder)
    STOP, loop.lock, run.lock        the loop's stop file and the single-instance locks

Every timestamp is int64 UTC ms; a brief id is ``YYYYMMDDTHHMMZ`` (the brief's as_of minute)."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any

from ..core.settings import Settings

ID_RE = re.compile(r"\d{8}T\d{4}Z")


def scalp_dir(s: Settings) -> Path:
    return s.paths.data() / "scalp"


def brief_id(as_of_ms: int) -> str:
    return dt.datetime.fromtimestamp(as_of_ms / 1000, dt.timezone.utc).strftime("%Y%m%dT%H%MZ")


def id_ms(brief_id_: str) -> int:
    if not ID_RE.fullmatch(brief_id_):
        raise ValueError(f"not a brief id: {brief_id_!r}")
    return int(dt.datetime.strptime(brief_id_, "%Y%m%dT%H%MZ").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def path_of(s: Settings, brief_id_: str, ext: str) -> Path:
    if not ID_RE.fullmatch(brief_id_):
        raise ValueError(f"not a brief id: {brief_id_!r}")
    return scalp_dir(s) / "briefs" / brief_id_[:8] / f"{brief_id_[9:]}.{ext}"


def list_ids(s: Settings, limit: int = 100) -> list[str]:
    """Stored brief ids, newest first (regular ``.json`` files under ``briefs/<day>/`` only)."""
    root = scalp_dir(s) / "briefs"
    out: list[str] = []
    try:
        days = sorted((d for d in os.scandir(root) if d.is_dir(follow_symlinks=False) and re.fullmatch(r"\d{8}", d.name)),
                      key=lambda d: d.name, reverse=True)
    except OSError:
        return out
    for day in days:
        try:
            names = sorted((f.name for f in os.scandir(day.path)
                            if f.is_file(follow_symlinks=False) and re.fullmatch(r"\d{4}Z\.json", f.name)), reverse=True)
        except OSError:
            continue
        for n in names:
            out.append(f"{day.name}T{n[:-5]}")
            if len(out) >= limit:
                return out
    return out


def write_json_atomic(path: Path, doc: Any) -> None:
    """A whole file or nothing (a reader never sees a half-written JSON): ``.tmp`` + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_record(s: Settings, brief_id_: str) -> dict | None:
    rec = read_json(path_of(s, brief_id_, "json"))
    return rec if isinstance(rec, dict) else None


def write_record(s: Settings, rec: dict, md: str | None = None, *, latest: bool = True) -> Path:
    """Store a record (and its markdown); ``latest.json`` follows when the record is the newest."""
    bid = rec["id"]
    p = path_of(s, bid, "json")
    write_json_atomic(p, rec)
    if md is not None:
        write_text_atomic(path_of(s, bid, "md"), md)
    if latest:
        cur = read_json(scalp_dir(s) / "latest.json")
        if not (isinstance(cur, dict) and str(cur.get("id", "")) > bid):
            write_json_atomic(scalp_dir(s) / "latest.json", rec)
    return p


def recent_records(s: Settings, n: int) -> list[dict]:
    """The newest ``n`` records, newest first."""
    out = []
    for bid in list_ids(s, n):
        rec = read_record(s, bid)
        if rec is not None:
            out.append(rec)
    return out
