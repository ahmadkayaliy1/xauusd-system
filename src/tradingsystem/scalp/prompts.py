"""The scalp brief's own prompt pair (D-058): ``scalp/prompts/system.md`` + ``instructions.md``, kept OUTSIDE the
prompt library (``ai/prompts``) so the desk's ``library_hash()`` and its registered versions never move because of
the brief. The renderer reuses the library's helpers (the same ``$name`` filling, the same version header) and
appends the library's payload legend and the gold field notes verbatim, read-only, after the system text - the
model reads the same payload the desk reads, so it needs the same legend."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..ai.prompts import PromptError, _fill, _read_versioned

DIR = Path(__file__).parent / "prompts"
APPENDIX = ("shared/payload_legend", "desks/xauusd_fields")       # library files appended verbatim (no placeholder)
_HEADER = re.compile(r"^<!-- prompt: .*? -->\s*", re.M)
_VERSION = re.compile(r"^<!-- prompt: (\S+) · version (\d+) -->", re.M)
SYSTEM_VARS = ("pair", "price_reference", "output_language", "min_stop_rule", "min_rr", "spread_mult", "max_plans",
               "news_windows", "hours_text")
USER_VARS = ("now_utc", "reason", "desk", "manual_recent", "journal", "charts_note", "payload")


def _read(name: str) -> tuple[str, str, int | None]:
    """(body without its header, ``scalp/<name>``, version) of one of this package's prompt files."""
    raw = (DIR / f"{name}.md").read_text(encoding="utf-8")
    m = _VERSION.search(raw)
    return _HEADER.sub("", raw).strip(), (m.group(1) if m else f"scalp/{name}"), (int(m.group(2)) if m else None)


def render_scalp(system_vars: dict[str, object], user_vars: dict[str, object]) -> tuple[str, str, dict[str, int]]:
    """``(system, user, versions)``. ``system_vars`` must be the same from run to run (the CLI caches the system
    prompt; its hash identifies the version); everything of the hour goes in ``user_vars``. Values are inserted
    verbatim - a ``$PDH`` inside the journal or the payload is never re-parsed."""
    missing = [k for k in SYSTEM_VARS if k not in system_vars]
    if missing:
        raise PromptError(f"scalp/system: missing system vars {missing}")
    body, name, version = _read("system")
    system = _fill(body, system_vars, "scalp/system")
    versions: dict[str, int] = {}
    if version is not None:
        versions[name] = version
    for rel in APPENDIX:
        text, lib_name, lib_version = _read_versioned(f"{rel}.md")
        system = system + "\n\n" + text
        if lib_version is not None:
            versions[lib_name] = lib_version
    body, name, version = _read("instructions")
    user = _fill(body, {**system_vars, **user_vars}, "scalp/instructions")
    if version is not None:
        versions[name] = version
    return system, user, versions


def prompt_hash(system: str) -> str:
    """The system prompt's hash (``prompt_hash`` of the stored record), as the library computes it."""
    return hashlib.sha256(system.encode()).hexdigest()[:16]
