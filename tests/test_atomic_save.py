"""1.8-04 gate - the save protocol: a write is atomic, an interrupted one is resolved.

Every headless DXF write (``drawing_save``, ``drawing_save_as``,
``drawing_export_dxf``, ``document_close(save=True)``, the finalize / deliver
writes and ``drawing_template_save``) goes through
``backends.atomic_write.atomic_write_dxf``: a tmp beside the target, fsync, a
header and entity-count check, a pending marker in ``<STATE_DIR>/pending``,
``os.replace``. The process-death cases run in a real subprocess that exits
with ``os._exit`` at the chosen point - no ``finally`` runs, as under a kill:

* the writer dies half way -> the target is the previous file, the marker is
  there -> the next ``drawing_open`` reports ``rolled_back``;
* the process dies between the fsync and the replace -> the next open puts
  the verified tmp in place: ``finished``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import ezdxf
import pytest
from fastmcp import Client

import server
from backends import atomic_write as aw
from backends import ezdxf_backend as eb
from backends.atomic_write import (
    PENDING_DIR,
    SaveLockedError,
    SaveVerifyError,
    atomic_write_dxf,
    file_sha256,
    resolve_pending,
)

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.asyncio


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if ".acadmcp-" in p.name)


def _markers(state: Path) -> list[Path]:
    folder = state / PENDING_DIR
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


@pytest.fixture
def state() -> Path:
    found = aw.state_dir()
    assert found is not None, "conftest gives every test a usable ACADMCP_STATE_DIR"
    return found


def _run_child(script: str, *args: str) -> int:
    """Run ``script`` in a fresh interpreter with this test's state folder."""
    env = {**os.environ, "PYTHONUTF8": "1"}
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        timeout=120,
    )
    return completed.returncode


_CHILD_PRELUDE = """
import os, sys
sys.path.insert(0, os.getcwd())
import ezdxf
from backends import atomic_write as aw
target = sys.argv[1]
doc = ezdxf.readfile(target)
for i in range(40):
    doc.modelspace().add_circle((i, 0), 1)
"""


# ── the protocol ─────────────────────────────────────────────────────────────


async def test_a_save_leaves_no_tmp_no_marker_and_reports_the_written_sha(backend, tmp_path, state):
    await backend.entity_create_line(0, 0, 10, 0)
    target = tmp_path / "part.dxf"
    result = await backend.drawing_save_as(str(target))
    assert result["ok"] is True and result["path"] == str(target)
    assert result["sha256"] == _sha(target)
    assert _leftovers(tmp_path) == [] and _markers(state) == []
    assert len(ezdxf.readfile(target).modelspace()) == 1


async def test_every_headless_dxf_write_goes_through_the_protocol(backend, tmp_path, monkeypatch):
    seen: list[str] = []
    real = eb.atomic_write_dxf

    def spy(doc, target, *args, **kwargs):
        seen.append(Path(target).name)
        return real(doc, target, *args, **kwargs)

    monkeypatch.setattr(eb, "atomic_write_dxf", spy)
    await backend.entity_create_line(0, 0, 10, 0)
    await backend.drawing_save_as(str(tmp_path / "a.dxf"))
    await backend.drawing_save()
    await backend.drawing_export_dxf(str(tmp_path / "copy.dxf"))
    await backend.drawing_template_save(str(tmp_path / "tpl.dxf"))
    await backend.entity_create_line(0, 5, 10, 5)
    await backend.document_close(save=True)
    assert seen == ["a.dxf", "a.dxf", "copy.dxf", "tpl.dxf", "tpl.dxf"]


async def test_a_writer_failing_half_way_leaves_the_target_as_it_was(
    backend, tmp_path, state, monkeypatch
):
    await backend.entity_create_line(0, 0, 10, 0)
    target = tmp_path / "part.dxf"
    await backend.drawing_save_as(str(target))
    before = _sha(target)
    await backend.entity_create_circle(0, 0, 5)
    doc = backend._doc
    real_write = doc.write

    def half_then_fail(stream, fmt="asc"):
        import io

        buffer = io.StringIO()
        real_write(buffer, fmt)
        text = buffer.getvalue()
        stream.write(text[: len(text) // 2])
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(doc, "write", half_then_fail)
    with pytest.raises(OSError, match="disk full"):
        await backend.drawing_save()
    assert _sha(target) == before
    assert _leftovers(tmp_path) == [] and _markers(state) == []
    assert (await backend.drawing_info()).saved is False, "a failed save is not a save"


async def test_a_tmp_that_fails_its_check_is_never_put_in_place(backend, tmp_path, monkeypatch):
    await backend.entity_create_line(0, 0, 10, 0)
    target = tmp_path / "part.dxf"
    await backend.drawing_save_as(str(target))
    before = _sha(target)
    await backend.entity_create_line(0, 5, 10, 5)
    monkeypatch.setattr(aw, "_expected_entities", lambda doc: 99)
    with pytest.raises(SaveVerifyError, match="ENTITIES section holds 2, the document 99"):
        await backend.drawing_save()
    assert _sha(target) == before and _leftovers(tmp_path) == []


async def test_the_entity_check_counts_polylines_and_attributes_once(tmp_path, state):
    doc = ezdxf.new("R2010")
    block = doc.blocks.new("TAGGED")
    block.add_attdef("TAG", (0, 0))
    msp = doc.modelspace()
    msp.add_polyline2d([(0, 0), (1, 0), (1, 1)])
    msp.add_blockref("TAGGED", (0, 0)).add_auto_attribs({"TAG": "P-101"})
    doc.paperspace().add_line((0, 0), (1, 1))
    written = atomic_write_dxf(doc, tmp_path / "mixed.dxf")
    assert written["entities"] == 3
    assert len(ezdxf.readfile(tmp_path / "mixed.dxf").modelspace()) == 2


async def test_a_hard_linked_target_is_written_into_so_every_name_keeps_the_file(tmp_path):
    target, link = tmp_path / "gear.dxf", tmp_path / "gear_link.dxf"
    atomic_write_dxf(ezdxf.new("R2010"), target)
    os.link(target, link)
    doc = ezdxf.new("R2010")
    doc.modelspace().add_circle((0, 0), 5)
    written = atomic_write_dxf(doc, link)
    assert written["method"] == "in_place"
    assert os.path.samefile(target, link)
    assert _sha(target) == written["sha256"] and _leftovers(tmp_path) == []


async def test_save_backup_keeps_the_previous_file(backend, tmp_path, monkeypatch):
    target = tmp_path / "part.dxf"
    await backend.drawing_save_as(str(target))
    first = target.read_bytes()
    monkeypatch.setenv("SAVE_BACKUP", "1")
    await backend.entity_create_line(0, 0, 10, 0)
    result = await backend.drawing_save()
    assert (tmp_path / "part.bak").read_bytes() == first
    assert result["backup"] == str(tmp_path / "part.bak")


async def test_a_sharing_violation_is_retried_then_refused_by_name(backend, tmp_path, monkeypatch):
    target = tmp_path / "part.dxf"
    await backend.drawing_save_as(str(target))
    before = _sha(target)
    real = os.replace
    calls = {"n": 0}

    def busy_twice(src, dst):
        if str(src).endswith(aw.TMP_SUFFIX):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(13, "The process cannot access the file")
        return real(src, dst)

    monkeypatch.setattr(aw.os, "replace", busy_twice)
    monkeypatch.setattr(aw, "REPLACE_PAUSE_S", 0.0)
    await backend.entity_create_line(0, 0, 10, 0)
    assert (await backend.drawing_save())["ok"] is True and calls["n"] == 3

    def always_busy(src, dst):
        if str(src).endswith(aw.TMP_SUFFIX):
            raise PermissionError(13, "The process cannot access the file")
        return real(src, dst)

    monkeypatch.setattr(aw.os, "replace", always_busy)
    saved = _sha(target)
    assert saved != before
    await backend.entity_create_line(0, 9, 10, 9)
    with pytest.raises(SaveLockedError) as refused:
        await backend.drawing_save()
    assert refused.value.kind == "save_locked" and "3 retries" in str(refused.value)
    assert _sha(target) == saved and _leftovers(tmp_path) == []


# ── a process that dies ──────────────────────────────────────────────────────


async def test_a_writer_killed_half_way_is_rolled_back_at_the_next_open(tmp_path, state):
    target = tmp_path / "part.dxf"
    seed = ezdxf.new("R2010")
    seed.modelspace().add_line((0, 0), (10, 0))
    atomic_write_dxf(seed, target)
    before = _sha(target)
    code = _run_child(
        _CHILD_PRELUDE
        + """
import io
real = doc.write
def dies_half_way(stream, fmt="asc"):
    buffer = io.StringIO()
    real(buffer, fmt)
    text = buffer.getvalue()
    stream.write(text[: len(text) // 2])
    stream.flush()
    os._exit(9)
doc.write = dies_half_way
aw.atomic_write_dxf(doc, target)
""",
        str(target),
    )
    assert code == 9
    assert _sha(target) == before, "the client's file was never touched"
    (marker,) = _markers(state)
    assert json.loads(marker.read_text())["phase"] == "writing"
    assert len(_leftovers(tmp_path)) == 1

    reader = eb.EzdxfBackend()
    await reader.connect()
    try:
        opened = await reader.drawing_open(str(target))
        (row,) = opened["save_recovery"]
        assert row["outcome"] == "rolled_back" and row["target"] == str(target)
        assert len(reader._doc.modelspace()) == 1
    finally:
        await reader.disconnect()
    assert _markers(state) == [] and _leftovers(tmp_path) == []


async def test_a_process_killed_between_fsync_and_replace_is_finished(tmp_path, state):
    target = tmp_path / "part.dxf"
    seed = ezdxf.new("R2010")
    seed.modelspace().add_line((0, 0), (10, 0))
    atomic_write_dxf(seed, target)
    before = _sha(target)
    code = _run_child(
        _CHILD_PRELUDE
        + """
real_replace = os.replace
def dies_before_replace(src, dst):
    if str(src).endswith(aw.TMP_SUFFIX):
        os._exit(9)
    return real_replace(src, dst)
aw.os.replace = dies_before_replace
aw.atomic_write_dxf(doc, target)
""",
        str(target),
    )
    assert code == 9
    assert _sha(target) == before
    (marker,) = _markers(state)
    pending = json.loads(marker.read_text())
    assert pending["phase"] == "verified" and pending["previous_sha256"] == before
    assert file_sha256(pending["tmp"]) == pending["expected_sha256"]

    reader = eb.EzdxfBackend()
    await reader.connect()
    try:
        opened = await reader.drawing_open(str(target))
        (row,) = opened["save_recovery"]
        assert row["outcome"] == "finished"
        assert _sha(target) == pending["expected_sha256"]
        assert len(reader._doc.modelspace()) == 41, "the completed save is what opened"
    finally:
        await reader.disconnect()
    assert _markers(state) == [] and _leftovers(tmp_path) == []


async def test_startup_resolves_every_marker_and_status_reports_it(tmp_path, state):
    target = tmp_path / "part.dxf"
    seed = ezdxf.new("R2010")
    atomic_write_dxf(seed, target)
    code = _run_child(
        _CHILD_PRELUDE
        + """
real_replace = os.replace
def dies_before_replace(src, dst):
    if str(src).endswith(aw.TMP_SUFFIX):
        os._exit(9)
    return real_replace(src, dst)
aw.os.replace = dies_before_replace
aw.atomic_write_dxf(doc, target)
""",
        str(target),
    )
    assert code == 9 and len(_markers(state)) == 1
    async with Client(server.mcp) as client:
        status = (await client.call_tool("system_status", {})).structured_content
    (row,) = status["save_recovery"]
    assert row["outcome"] == "finished" and row["target"] == str(target)
    assert _markers(state) == []
    assert len(ezdxf.readfile(target).modelspace()) == 40


async def test_a_tmp_of_a_dead_process_without_a_marker_is_an_orphan(backend, tmp_path):
    target = tmp_path / "part.dxf"
    await backend.drawing_save_as(str(target))
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait(timeout=60)
    orphan = Path(aw.tmp_path_for(target, gone.pid))
    orphan.write_text("  0\nSECTION\n", encoding="utf-8")
    opened = await backend.drawing_open(str(target))
    assert [row["outcome"] for row in opened["save_recovery"]] == ["orphan_tmp"]
    assert not orphan.exists()


async def test_a_live_owners_marker_is_never_touched(tmp_path, state):
    target = tmp_path / "part.dxf"
    atomic_write_dxf(ezdxf.new("R2010"), target)
    marker = state / PENDING_DIR / "live.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "target": str(target),
                "tmp": aw.tmp_path_for(target),
                "expected_sha256": None,
                "previous_sha256": None,
                "pid": os.getpid(),
            }
        )
    )
    assert resolve_pending() == []
    assert marker.exists()
