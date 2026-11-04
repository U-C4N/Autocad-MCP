"""pid_project (plan 1.8-21): a read-only multi-sheet P&ID project.

The gate: three synthetic sheets built here with ``run_spec`` - one orphan
link, one direction mismatch, one cross-sheet duplicate tag and one
line-number mismatch, each reported exactly once with its sheet and handle.
"""

from __future__ import annotations

import csv
import json
import os

import pytest
import pytest_asyncio
from fastmcp import Client

import server
from backends.ezdxf_backend import EzdxfBackend
from engineering.pid import project as project_module
from engineering.pid.project import MAX_SHEETS, clear_cache, run_project
from engineering.pid.spec import run_spec

pytestmark = pytest.mark.asyncio

_SHEET = {"layer_set": "pid", "sheet_size": "A3", "scale": 1.0}


def _pump(item_id: str, x: float, y: float) -> dict:
    return {"id": item_id, "symbol": "centrifugal_pump", "x": x, "y": y, "tag": item_id}


def _out(item_id, link, source, number, x, y, tag="TO NEXT SHEET"):
    return {
        "id": item_id,
        "direction": "out",
        "x": x,
        "y": y,
        "tag": tag,
        "link": link,
        "from": source,
        "number": number,
    }


def _in(item_id, link, target, number, x, y, tag="FROM PREVIOUS SHEET"):
    return {
        "id": item_id,
        "direction": "in",
        "x": x,
        "y": y,
        "tag": tag,
        "link": link,
        "to": target,
        "number": number,
    }


SPECS = {
    # L-1 and L-2 leave here; L-3 arrives here and leaves nowhere (the orphan).
    "P-001": {
        "sheet": _SHEET,
        "equipment": [_pump("P-101", 100, 100), _pump("P-102", 100, 200)],
        "connectors": [
            _out("OP1", "L-1", "P-101.discharge", "100-P-1001", 200, 140),
            _out("OP2", "L-2", "P-102.discharge", "100-P-1002", 200, 240),
            _in("OP3", "L-3", "P-101.suction", "100-P-1003", 20, 100),
        ],
    },
    # L-1 arrives with its number, L-2 arrives under another number (the line
    # mismatch), L-4 leaves.
    "P-002": {
        "sheet": _SHEET,
        "equipment": [_pump("P-201", 100, 100), _pump("P-202", 100, 200)],
        "connectors": [
            _in("IN1", "L-1", "P-201.suction", "100-P-1001", 20, 100),
            _in("IN2", "L-2", "P-202.suction", "100-P-9999", 20, 200),
            _out("OUT4", "L-4", "P-201.discharge", "100-P-1004", 200, 140),
        ],
    },
    # L-4 leaves here too (the direction mismatch); P-101 is P-001's tag (the
    # cross-sheet duplicate). The connector TAG repeats P-001's and never counts.
    "P-003": {
        "sheet": _SHEET,
        "equipment": [_pump("P-101", 100, 100)],
        "connectors": [_out("OUT4B", "L-4", "P-101.discharge", "100-P-1004", 200, 140)],
    },
}


async def _draw(spec: dict, path) -> dict:
    backend = EzdxfBackend()
    await backend.connect()
    try:
        await backend.drawing_new()
        result = await run_spec(backend, spec)
        await backend.drawing_save_as(str(path))
        return result["handles"]
    finally:
        await backend.disconnect()


@pytest_asyncio.fixture
async def project(tmp_path):
    """Three sheet files and the handles each spec id was drawn with."""
    clear_cache()
    paths, handles = {}, {}
    for number, spec in SPECS.items():
        paths[number] = tmp_path / f"{number}.dxf"
        handles[number] = await _draw(spec, paths[number])
    manifest = {
        "project": "Unit 7",
        "sheets": [{"path": str(paths[n]), "number": n, "title": f"Sheet {n}"} for n in SPECS],
    }
    yield {"paths": paths, "handles": handles, "manifest": manifest}
    clear_cache()


def _one(rows, **match):
    found = [row for row in rows if all(row.get(k) == v for k, v in match.items())]
    assert len(found) == 1, (match, rows)
    return found[0]


async def test_each_defect_is_reported_exactly_once_with_sheet_and_handle(backend, project):
    h = project["handles"]
    result = await run_project(
        backend, "check", manifest=project["manifest"], include_current=False
    )

    assert result["project"] == "Unit 7"
    assert [s["sheet"] for s in result["sheets"]] == ["P-001", "P-002", "P-003"]
    assert all(len(s["sha256"]) == 64 and s["source"] == "file" for s in result["sheets"])

    links = {row["link"]: row for row in result["links"]}
    assert {k: v["status"] for k, v in links.items()} == {
        "L-1": "matched",
        "L-2": "matched",
        "L-3": "orphan",
        "L-4": "direction_mismatch",
    }
    assert [(c["sheet"], c["handle"]) for c in links["L-3"]["connectors"]] == [
        ("P-001", h["P-001"]["OP3"])
    ]
    assert [(c["sheet"], c["handle"], c["direction"]) for c in links["L-4"]["connectors"]] == [
        ("P-002", h["P-002"]["OUT4"], "out"),
        ("P-003", h["P-003"]["OUT4B"], "out"),
    ]
    summary = result["summary"]
    assert summary["orphan"] == 1 and summary["direction_mismatch"] == 1
    assert summary["duplicate"] == 0 and summary["matched"] == 2

    (dup,) = result["duplicate_tags"]  # P-101 only: the connector TAGs never count
    assert dup["tag"] == "P-101" and dup["sheets"] == ["P-001", "P-003"]
    assert [(o["sheet"], o["handle"]) for o in dup["occurrences"]] == [
        ("P-001", h["P-001"]["P-101"]),
        ("P-003", h["P-003"]["P-101"]),
    ]

    continuity = {row["link"]: row for row in result["line_continuity"]}
    assert continuity["L-1"]["status"] == "equal"
    bad = _one(result["line_continuity"], status="mismatch")
    assert bad["link"] == "L-2"
    assert bad["out"] == {
        "sheet": "P-001",
        "handle": h["P-001"]["OP2"],
        "line_number": "100-P-1002",
    }
    assert bad["in"] == {"sheet": "P-002", "handle": h["P-002"]["IN2"], "line_number": "100-P-9999"}
    assert summary["line_mismatch"] == 1 and result["clean"] is False


async def test_index_merges_the_three_lists_with_a_sheet_column(backend, project):
    result = await run_project(
        backend, "index", manifest=project["manifest"], include_current=False
    )
    equipment = result["equipment_list"]
    pumps = [(row["tag"], row["sheet"]) for row in equipment if row["kind"] == "equipment"]
    assert pumps == [
        ("P-101", "P-001"),
        ("P-101", "P-003"),
        ("P-102", "P-001"),
        ("P-201", "P-002"),
        ("P-202", "P-002"),
    ]
    assert all(list(row)[0] == "sheet" for row in equipment + result["line_list"])
    numbers = {(row["sheet"], row["line_number"]) for row in result["line_list"]}
    assert ("P-002", "100-P-9999") in numbers and ("P-001", "100-P-1002") in numbers
    assert result["counts"]["line_list"] == len(result["line_list"]) == 7
    assert "links" not in result


async def test_sheet_files_are_read_once_by_sha256(backend, project, monkeypatch):
    first = await run_project(backend, "check", manifest=project["manifest"], include_current=False)
    assert [s["cached"] for s in first["sheets"]] == [False, False, False]
    loads = []
    real = project_module._load_dxf
    monkeypatch.setattr(project_module, "_load_dxf", lambda *a: loads.append(a) or real(*a))
    again = await run_project(backend, "check", manifest=project["manifest"], include_current=False)
    assert [s["cached"] for s in again["sheets"]] == [True, True, True] and loads == []
    assert again["links"] == first["links"]
    # A sheet whose bytes change is read again: the cache is keyed by content.
    await _draw(
        SPECS["P-003"] | {"equipment": [_pump("P-301", 100, 100)], "connectors": []},
        project["paths"]["P-003"],
    )
    third = await run_project(backend, "check", manifest=project["manifest"], include_current=False)
    assert [s["cached"] for s in third["sheets"]] == [True, True, False] and len(loads) == 1
    assert third["duplicate_tags"] == []
    assert {r["link"]: r["status"] for r in third["links"]}["L-4"] == "orphan"


async def test_the_active_document_replaces_its_own_file(project):
    """The open sheet is read as it stands - unsaved edits included."""
    backend = EzdxfBackend()
    await backend.connect()
    try:
        await backend.drawing_open(str(project["paths"]["P-003"]))
        # An unsaved fix: the second L-4 becomes an "in" - in memory only.
        await run_spec(
            backend,
            {
                "sheet": _SHEET,
                "equipment": [_pump("P-399", 300, 100)],
                "connectors": [_in("FIX", "L-5", "P-399.suction", None, 220, 100)],
            },
        )
        result = await run_project(backend, "check", manifest=project["manifest"])
        row = result["sheets"][2]
        assert row["sheet"] == "P-003" and row["source"] == "active_document"
        assert row["sha256"] is None and row["document_id"] and "unsaved" in row["note"]
        assert len(result["sheets"]) == 3, "the open file is not read twice"
        assert {r["link"]: r["status"] for r in result["links"]}["L-5"] == "orphan"
        # Not listed: the active document joins as sheet "current".
        alone = await run_project(backend, "check", sheets=[str(project["paths"]["P-001"])])
        assert [s["sheet"] for s in alone["sheets"]] == ["P-001", "current"]
    finally:
        await backend.disconnect()


async def test_refusals_name_what_is_wrong(backend, project, tmp_path, monkeypatch):
    from backends.capability import UnsupportedCapabilityError
    from engineering.errors import InvalidArgsError
    from security import PathRefusedError

    one = str(project["paths"]["P-001"])
    with pytest.raises(InvalidArgsError, match=f"at most {MAX_SHEETS} sheets"):
        await run_project(backend, sheets=[str(tmp_path / f"s{i}.dxf") for i in range(51)])
    with pytest.raises(InvalidArgsError, match="action must be one of check, index"):
        await run_project(backend, "list", sheets=[one])
    with pytest.raises(InvalidArgsError, match="sheets or manifest, not both"):
        await run_project(backend, sheets=[one], manifest=project["manifest"])
    with pytest.raises(FileNotFoundError, match="sheet missing .*nothing was read"):
        await run_project(backend, sheets=[one, str(tmp_path / "missing.dxf")])
    dwg = tmp_path / "P-009.dwg"
    dwg.write_bytes(b"AC1032 not really")
    with pytest.raises(UnsupportedCapabilityError) as refused:
        await run_project(backend, sheets=[str(dwg)], include_current=False)
    assert refused.value.capability == "dwg"
    with pytest.raises(InvalidArgsError, match="repeats"):
        await run_project(
            backend,
            manifest={"sheets": [{"path": one, "number": "A"}, {"path": str(dwg), "number": "A"}]},
        )
    with pytest.raises(PathRefusedError):
        await run_project(backend, sheets=[one], write=str(tmp_path / "acaddoc.lsp"))
    monkeypatch.setattr("engineering.sheet.extract.xlsx_available", lambda: False)
    with pytest.raises(UnsupportedCapabilityError) as no_xlsx:
        await run_project(backend, sheets=[one], write=str(tmp_path / "index.xlsx"))
    assert no_xlsx.value.capability == "xlsx_write"
    assert not (tmp_path / "index.xlsx").exists()


async def test_the_dwg_route_uses_the_live_snapshot_export(project, tmp_path, monkeypatch):
    """A .dwg on the live engine: opened read-only and exported (faked here)."""
    import engineering.understand.snapshot as snapshot

    dwg = tmp_path / "P-001.dwg"
    dwg.write_bytes(project["paths"]["P-001"].read_bytes())
    exported = tmp_path / "export"
    calls = []

    def fake_export(backend, path):
        calls.append(path)
        exported.mkdir()
        target = exported / "snap.dxf"
        target.write_bytes(project["paths"]["P-001"].read_bytes())
        return str(target), str(exported), "live:P-001.dwg"

    class FakeCom:
        name = "com"

        def document_identity(self):
            return {"document_id": None, "revision": None, "name": None, "path": None}

        async def _run(self, func, *args):
            return func(*args)

        async def _ensure_document_state(self):
            calls.append("ensure")

    monkeypatch.setattr(snapshot, "_com_export", fake_export)
    result = await run_project(FakeCom(), "check", sheets=[str(dwg)], include_current=False)
    assert calls == [str(dwg.resolve()), "ensure"] or calls == [str(dwg), "ensure"]
    assert result["sheets"][0]["source"] == "live_export"
    assert not exported.exists(), "the temporary export folder is removed"
    assert {r["link"] for r in result["links"]} == {"L-1", "L-2", "L-3"}


async def test_the_live_smoke_project_step_holds_on_the_headless_engine(backend, tmp_path):
    """scripts/smoke_pid_com.py's project step, run here with the headless
    engine standing in for the live one (the live run is pending)."""
    from engineering.layers import apply_layer_set
    from engineering.pid.spec import EXAMPLE_SPEC
    from scripts import smoke_pid_com as smoke

    class Harness:
        def __init__(self):
            self.failed = []

        def check(self, name, ok, expected=None, observed=None):
            if not ok:
                self.failed.append((name, expected, observed))

    await apply_layer_set(backend, "pid")
    drawn = await run_spec(backend, EXAMPLE_SPEC)
    files = await smoke.project_files(tmp_path)
    project = await run_project(
        backend, "check", sheets=[files[n]["path"] for n in smoke._PROJECT_SHEETS]
    )
    harness = Harness()
    smoke.assess_project(
        harness, {"handles": drawn["handles"], "project_files": files, "project": project}
    )
    assert harness.failed == []


async def test_the_v9_task_is_registered_headless_only():
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter
    from benchmarks.tasks_v9 import HEADLESS_ONLY_V9, NEW_TASKS_V9

    assert "pid_project_offpage" in {task.task_id for task in NEW_TASKS_V9}
    assert "pid_project_offpage" in HEADLESS_ONLY_V9
    assert callable(getattr(AutoCADMCPProAdapter, "_task_pid_project_offpage", None))


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        yield connected


async def test_tool_writes_csv_json_and_serves_the_last_result(client, project, tmp_path):
    out = tmp_path / "check.csv"
    args = {"manifest": project["manifest"], "include_current": False, "write": str(out)}
    result = (await client.call_tool("pid_project", args)).structured_content
    assert result["written"]["format"] == "csv" and result["summary"]["orphan"] == 1
    with out.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert {r["table"] for r in rows} == {"sheets", "links", "duplicate_tags", "line_continuity"}
    orphan = [r for r in rows if r["table"] == "links" and r["status"] == "orphan"]
    assert [(r["sheet"], r["handle"]) for r in orphan] == [
        ("P-001", project["handles"]["P-001"]["OP3"])
    ]
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]

    index = tmp_path / "index.json"
    args = {**args, "action": "index", "write": str(index)}
    indexed = (await client.call_tool("pid_project", args)).structured_content
    assert json.loads(index.read_text(encoding="utf-8"))["counts"] == indexed["counts"]
    last = json.loads((await client.read_resource("autocad://pid/project/last"))[0].text)
    assert last["available"] is True and last["action"] == "index"
    assert last["counts"] == indexed["counts"]


async def test_tool_refusals_carry_their_kind(client, tmp_path):
    many = [str(tmp_path / f"s{i}.dxf") for i in range(MAX_SHEETS + 1)]
    result = await client.call_tool("pid_project", {"sheets": many}, raise_on_error=False)
    assert result.is_error and result.structured_content["kind"] == "invalid_args"
    missing = await client.call_tool(
        "pid_project",
        {"sheets": [str(tmp_path / "gone.dxf")], "include_current": False},
        raise_on_error=False,
    )
    assert missing.structured_content["kind"] == "file_not_found"
    dwg = tmp_path / "x.dwg"
    dwg.write_bytes(b"x")
    refused = await client.call_tool(
        "pid_project", {"sheets": [str(dwg)], "include_current": False}, raise_on_error=False
    )
    assert refused.structured_content["kind"] == "unsupported"
    assert refused.structured_content["capability"] == "dwg"


async def test_xlsx_writes_one_worksheet_per_table(client, project, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    out = tmp_path / "index.xlsx"
    args = {
        "action": "index",
        "manifest": project["manifest"],
        "include_current": False,
        "write": str(out),
    }
    await client.call_tool("pid_project", args)
    book = openpyxl.load_workbook(out)
    assert book.sheetnames == ["sheets", "instrument_index", "line_list", "equipment_list"]
    assert [c.value for c in book["line_list"][1]][0] == "sheet"
