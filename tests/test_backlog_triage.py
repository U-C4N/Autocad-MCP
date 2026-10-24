"""The 1.7.0 backlog triage stays a contract the later tasks can read.

``docs/analysis/backlog-1.7.0-triage.json`` (v1.7.0 Task 0) classifies every
row of ``docs/analysis/track-a-review-backlog.md`` that was deferred to 1.7 —
plus the two rows deferred to Track E's docs task — and names the task that
owns each one that still reproduces. Every later 1.7.0 task fixes the rows it
owns, so a row missing from the JSON, an owner nobody can read, or a markdown
table that disagrees with the JSON would silently drop work.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKLOG = ROOT / "docs" / "analysis" / "track-a-review-backlog.md"
TRIAGE = ROOT / "docs" / "analysis" / "backlog-1.7.0-triage.json"

DISPOSITIONS = {"still_reproduces", "fixed", "accepted", "superseded"}
OWNER_RE = re.compile(r"^(task-(?:[0-9]|1[0-9]|2[0-3])|1\.7\.1|1\.8\.0|none)$")
ROW_RE = re.compile(r"^\| (W[0-9]-T[0-9]+-[0-9]+) \| ([^|]*) \| (.*) \| ([^|]*) \|\s*$")
TRACK_E = "deferred to the final docs task of Track E"
SECTION = "## 1.7.0 triage"


def _deferred_rows() -> dict[str, tuple[str, str, str]]:
    """``id -> (file, finding, old disposition)`` for every deferred backlog row."""
    text = BACKLOG.read_text(encoding="utf-8")
    original = text.split(SECTION, 1)[0]
    rows = {}
    for line in original.splitlines():
        match = ROW_RE.match(line)
        if not match:
            continue
        row_id, file, finding, disposition = (part.strip() for part in match.groups())
        if "deferred-1.7" in disposition or TRACK_E in disposition:
            rows[row_id] = (file, finding, disposition)
    return rows


def _triage() -> dict:
    return json.loads(TRIAGE.read_text(encoding="utf-8"))


def test_the_backlog_still_holds_the_72_deferred_rows_and_the_two_track_e_rows():
    rows = _deferred_rows()
    deferred = [r for r in rows.values() if "deferred-1.7" in r[2]]
    mixed = [r for r in deferred if not r[2].startswith("deferred-1.7")]
    assert len(deferred) == 72
    assert len(mixed) == 3
    assert len(rows) - len(deferred) == 2


def test_every_deferred_row_is_triaged_exactly_once_and_copied_faithfully():
    data = _triage()
    assert data["schema_version"] == 1
    assert data["source"] == "docs/analysis/track-a-review-backlog.md"
    ids = [row["id"] for row in data["rows"]]
    assert len(ids) == len(set(ids)), "a row is triaged twice"
    backlog = _deferred_rows()
    assert set(ids) == set(backlog)
    for row in data["rows"]:
        file, finding, old = backlog[row["id"]]
        assert (row["file"], row["finding"], row["old_disposition"]) == (file, finding, old), row[
            "id"
        ]


def test_every_row_has_a_disposition_evidence_and_a_readable_owner():
    for row in _triage()["rows"]:
        where = row["id"]
        assert set(row) == {
            "id",
            "file",
            "finding",
            "old_disposition",
            "disposition",
            "evidence",
            "owner",
            "reason",
        }, where
        assert row["disposition"] in DISPOSITIONS, where
        assert OWNER_RE.match(row["owner"]), where
        assert row["evidence"].strip(), where
        assert row["reason"].strip(), where
        if row["disposition"] == "still_reproduces":
            assert row["owner"] != "none", f"{where}: a live row needs an owner"
        else:
            assert row["owner"] == "none", f"{where}: a closed row has no owner"


def test_the_markdown_section_agrees_with_the_json():
    text = BACKLOG.read_text(encoding="utf-8")
    assert SECTION in text
    section = text.split(SECTION, 1)[1]
    table = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and re.match(r"^W[0-9]-T[0-9]+-[0-9]+$", cells[0]):
            table[cells[0]] = (cells[1], cells[2])
    rows = {row["id"]: (row["disposition"], row["owner"]) for row in _triage()["rows"]}
    assert table == rows
