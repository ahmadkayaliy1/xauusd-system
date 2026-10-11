"""D-058: where the scalp job keeps its files (scalp/files.py) — the brief id and its regex, the path mapping, the
newest-first listing (regular files of the pattern only), atomic writes with ``latest.json`` following the newest
record, and ids that would be paths rejected."""
import json
import os
from pathlib import Path

import pytest

from tradingsystem.core.settings import PathsCfg, load_settings
from tradingsystem.scalp import files

HOUR = 3_600_000
T0 = 1_760_000_000_000 // HOUR * HOUR                   # 2025-10-09 08:00:00 UTC


@pytest.fixture
def s(tmp_path):
    base = load_settings(env_path=Path("nope.env"), extra_env={"EXECUTION_MODE": "paper"})
    return base.model_copy(update={"paths": PathsCfg(data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"))})


def test_brief_id_and_its_regex():
    assert files.brief_id(T0) == "20251009T0800Z" and files.brief_id(T0 + 20_000) == "20251009T0800Z"
    assert files.id_ms("20251009T0800Z") == T0
    assert files.ID_RE.fullmatch("20251009T0800Z") and not files.ID_RE.fullmatch("20251009T0800")
    for bad in ("", "..", "20251009T0800Z/..", "../20251009T0800Z", "20251009t0800z", "2025-10-09T08:00Z"):
        assert not files.ID_RE.fullmatch(bad), bad
        with pytest.raises(ValueError):
            files.id_ms(bad)


def test_path_mapping_and_traversal_ids_rejected(s):
    assert files.scalp_dir(s) == s.paths.data() / "scalp"
    assert files.path_of(s, "20251009T0800Z", "json") == s.paths.data() / "scalp" / "briefs" / "20251009" / "0800Z.json"
    assert files.path_of(s, "20251009T0800Z", "md").name == "0800Z.md"
    for bad in ("..", "../x", "20251009T0800Z/../../latest", "latest.json"):
        with pytest.raises(ValueError):
            files.path_of(s, bad, "json")


def test_listing_is_newest_first_and_regular_pattern_files_only(s):
    root = files.scalp_dir(s) / "briefs"
    assert files.list_ids(s) == []                                   # no folder yet
    for day, hhmm in (("20251008", "1500Z"), ("20251009", "0700Z"), ("20251009", "0900Z"), ("20251009", "0800Z")):
        (root / day).mkdir(parents=True, exist_ok=True)
        (root / day / f"{hhmm}.json").write_text("{}", encoding="utf-8")
    (root / "20251009" / "0800Z.md").write_text("md", encoding="utf-8")            # the markdown is not an id
    (root / "20251009" / "0800Z.json.123.tmp").write_text("{", encoding="utf-8")   # a write in progress
    (root / "20251009" / "1000Z.json").mkdir()                                     # a directory, not a file
    (root / "notes").mkdir()
    (root / "20251010").mkdir()                                                    # an empty day
    (root / "latest.json").write_text("{}", encoding="utf-8")
    assert files.list_ids(s) == ["20251009T0900Z", "20251009T0800Z", "20251009T0700Z", "20251008T1500Z"]
    assert files.list_ids(s, 2) == ["20251009T0900Z", "20251009T0800Z"]
    assert files.list_ids(s, 3) == ["20251009T0900Z", "20251009T0800Z", "20251009T0700Z"]   # the limit crosses a day


def test_write_record_is_atomic_and_latest_follows_the_newest(s):
    sdir = files.scalp_dir(s)
    rec = {"id": "20251009T0800Z", "as_of_ms": T0, "brief": {"picture": "صورة"}}
    p = files.write_record(s, rec, "# md\n")
    assert p == files.path_of(s, "20251009T0800Z", "json") and p.is_file()
    assert files.path_of(s, "20251009T0800Z", "md").read_text(encoding="utf-8") == "# md\n"
    assert files.read_record(s, "20251009T0800Z") == rec
    assert json.loads((sdir / "latest.json").read_text(encoding="utf-8")) == rec
    assert not [f for f in os.listdir(p.parent) if f.endswith(".tmp")]           # nothing half-written left
    assert "صورة" in p.read_text(encoding="utf-8")                               # ensure_ascii off: readable
    assert b"\r\n" not in p.read_bytes()
    # an older record stored later (a re-score, a replay) never moves latest.json back
    older = {"id": "20251009T0700Z", "as_of_ms": T0 - HOUR}
    files.write_record(s, older)
    assert json.loads((sdir / "latest.json").read_text(encoding="utf-8"))["id"] == "20251009T0800Z"
    assert not files.path_of(s, "20251009T0700Z", "md").exists()                 # md optional
    newer = {"id": "20251009T0900Z", "as_of_ms": T0 + HOUR}
    files.write_record(s, newer, latest=False)                                    # the caller may keep latest
    assert json.loads((sdir / "latest.json").read_text(encoding="utf-8"))["id"] == "20251009T0800Z"
    files.write_record(s, newer)
    assert json.loads((sdir / "latest.json").read_text(encoding="utf-8"))["id"] == "20251009T0900Z"
    assert [r["id"] for r in files.recent_records(s, 2)] == ["20251009T0900Z", "20251009T0800Z"]
    with pytest.raises(ValueError):
        files.write_record(s, {"id": "../x"})


def test_read_json_and_read_record_tolerate_bad_files(s, tmp_path):
    assert files.read_json(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert files.read_json(bad) is None
    files.path_of(s, "20251009T0800Z", "json").parent.mkdir(parents=True)
    files.path_of(s, "20251009T0800Z", "json").write_text("[1, 2]", encoding="utf-8")
    assert files.read_record(s, "20251009T0800Z") is None                          # a record is a dict
    assert files.read_record(s, "20251009T0900Z") is None                          # missing
    files.write_json_atomic(tmp_path / "deep" / "x.json", {"a": 1})               # the parents are made
    assert (tmp_path / "deep" / "x.json").is_file()
    files.write_text_atomic(tmp_path / "deep" / "t.md", "a\nb")
    assert (tmp_path / "deep" / "t.md").read_bytes() == b"a\nb"
