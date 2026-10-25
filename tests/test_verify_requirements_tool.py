"""Task 16: drawing_verify_requirements, and drawing_finalize / drawing_deliver with
requirements, on the headless engine. Ø40 is a circle of r = 20, Ø42 one of r = 21.

The raw requirement JSON below is Task 11's raw shape written as a client sends
it - `{"units", "items": [...]}` (`parse_requirements` reads `items`, not
`requirements`); `_raw` / `_diameter_row` are the one place to change if Task 11
landed another spelling (the landed parser wins).
"""

from __future__ import annotations

import hashlib
import json

import ezdxf
import pytest
from fastmcp.exceptions import ToolError

import server
from discovery.aliases import aliases_for
from engineering.delivery import deliver_drawing
from engineering.layers import ensure_engineering_layers, ensure_standard_linetypes
from engineering.verification.requirements import store_requirements
from engineering.verification.types import FeatureSelector, Requirement, RequirementSet


class _FakeCtx:
    """What the tools touch on a Context: the backend, info and warning."""

    def __init__(self, backend):
        self.lifespan_context = {"backend": backend}

    async def info(self, message):
        pass

    async def warning(self, message):
        pass


def _raw(*rows) -> dict:
    return {"units": "mm", "items": list(rows)}


def _diameter_row(rid, handle, nominal=40.0) -> dict:
    return {
        "id": rid,
        "quantity": "diameter",
        "target": {"nominal": nominal, "unit": "mm"},
        "features": [{"kind": "handle", "handle": handle}],
        "provenance": "explicit",
        "source": {"document": "synthetic-sheet", "label": rid},
    }


async def _open_part(backend, tmp_path):
    """A 200 x 100 frame, a Ø40 bore (r = 20) and a Ø42 bore (r = 21), in mm."""
    base = tmp_path / "base.dxf"
    ezdxf.new("R2010", units=4).saveas(base)
    await backend.drawing_open(str(base))
    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    frame = await backend.entity_create_polyline(
        [[0, 0], [200, 0], [200, 100], [0, 100]], closed=True, layer="GEOMETRY"
    )
    bore = await backend.entity_create_circle(50, 50, 20, layer="GEOMETRY")
    wide = await backend.entity_create_circle(150, 50, 21, layer="GEOMETRY")
    return {"frame": frame.handle, "bore": bore.handle, "wide": wide.handle}


@pytest.mark.asyncio
async def test_requirements_are_verified_on_the_current_document(backend, tmp_path):
    handles = await _open_part(backend, tmp_path)
    report = await server.drawing_verify_requirements(
        requirements=_raw(
            _diameter_row("R1", handles["bore"]), _diameter_row("R2", handles["wide"])
        ),
        ctx=_FakeCtx(backend),
    )
    assert report["mode"] == "document"
    assert report["status"] == "fail"
    rows = {row["id"]: row for row in report["rows"]}
    assert abs(rows["R1"]["measured"]["value"] - 40.0) <= 1e-9  # 2 x 20
    assert abs(rows["R2"]["measured"]["value"] - 42.0) <= 1e-9  # 2 x 21
    assert abs(rows["R2"]["delta"] - 2.0) <= 1e-9


@pytest.mark.asyncio
async def test_use_stored_reads_the_requirements_the_drawing_carries(backend, tmp_path):
    handles = await _open_part(backend, tmp_path)
    rs = RequirementSet(
        requirements=(
            Requirement(
                id="R1",
                quantity="diameter",
                target=(("nominal", 40.0), ("unit", "mm")),
                features=(FeatureSelector(kind="handle", value=(("handle", handles["bore"]),)),),
                provenance="explicit",
                source=(("document", "synthetic-sheet"),),
            ),
        ),
        units="mm",
    )
    await store_requirements(backend, rs)
    report = await server.drawing_verify_requirements(use_stored=True, ctx=_FakeCtx(backend))
    assert (report["status"], report["accepted"]) == ("pass", True)
    assert report["requirements_digest"] == rs.digest()


@pytest.mark.asyncio
async def test_the_refusals_come_before_any_work(backend, tmp_path):
    handles = await _open_part(backend, tmp_path)
    row = _diameter_row("R1", handles["bore"])
    cases = [
        ({"requirements": _raw(row), "use_stored": True}, "invalid_args: pass requirements or"),
        ({}, "missing_requirement: nothing to verify"),
        ({"use_stored": True}, "missing_requirement: this drawing stores no requirements"),
        ({"requirements": _raw(row), "binding_policy": "fuzzy"}, "binding_policy"),
        (
            {"requirements": _raw(row), "report_path": str(tmp_path / "report.txt")},
            r"\.json or \.md",
        ),
        (
            {"manifest_path": str(tmp_path / "manifest.json"), "use_stored": True},
            "manifest_path is its own mode",
        ),
        (
            {"requirements": _raw(row), "artifact_path": str(tmp_path / "absent.dxf")},
            "artifact_path: no such file",
        ),
        ({"manifest_path": str(tmp_path / "absent.json")}, "manifest_path: no such file"),
    ]
    for kwargs, pattern in cases:
        with pytest.raises(ToolError, match=pattern):
            await server.drawing_verify_requirements(ctx=_FakeCtx(backend), **kwargs)
    assert not (tmp_path / "report.txt").exists()


@pytest.mark.asyncio
async def test_report_path_writes_json_or_markdown(backend, tmp_path):
    handles = await _open_part(backend, tmp_path)
    requirements = _raw(_diameter_row("R1", handles["bore"]))
    as_json = await server.drawing_verify_requirements(
        requirements=requirements,
        report_path=str(tmp_path / "acceptance.json"),
        ctx=_FakeCtx(backend),
    )
    written = json.loads((tmp_path / "acceptance.json").read_text(encoding="utf-8"))
    assert written["rows"] == as_json["rows"]
    assert as_json["report_path"] == str((tmp_path / "acceptance.json").resolve())
    await server.drawing_verify_requirements(
        requirements=requirements,
        report_path=str(tmp_path / "acceptance.md"),
        ctx=_FakeCtx(backend),
    )
    assert (
        (tmp_path / "acceptance.md").read_text(encoding="utf-8").startswith("# Acceptance report")
    )


@pytest.mark.asyncio
async def test_manifest_mode_reports_an_artifact_edited_after_delivery(backend, tmp_path):
    await _open_part(backend, tmp_path)
    bundle = tmp_path / "bundle"
    await deliver_drawing(backend, bundle, formats=["dxf"], min_score=0, strict_critique=False)
    clean = await server.drawing_verify_requirements(
        manifest_path=str(bundle / "manifest.json"), ctx=_FakeCtx(backend)
    )
    assert (clean["mode"], clean["ok"]) == ("manifest", True)
    with (bundle / "drawing.dxf").open("ab") as stream:
        stream.write(b"999\nedited\n")
    edited = await server.drawing_verify_requirements(
        manifest_path=str(bundle / "manifest.json"), ctx=_FakeCtx(backend)
    )
    assert edited["ok"] is False
    assert [row["filename"] for row in edited["stale"]] == ["drawing.dxf"]


@pytest.mark.asyncio
async def test_finalize_with_requirements_adds_the_acceptance_fields(backend, tmp_path):
    handles = await _open_part(backend, tmp_path)
    saved = tmp_path / "part.dxf"
    payload = await server.drawing_finalize(
        save_path=str(saved),
        requirements=_raw(_diameter_row("R1", handles["bore"])),
        ctx=_FakeCtx(backend),
    )
    assert payload["geometry_acceptance"]["accepted"] is True
    assert payload["drafting_quality"] == payload["score"]
    assert payload["coverage"] == {"required": 1, "measured": 1, "ratio": 1.0}
    integrity = payload["artifact_integrity"]
    assert integrity["sha256"] == hashlib.sha256(saved.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_finalize_fails_acceptance_whatever_the_score(backend, tmp_path):
    handles = await _open_part(backend, tmp_path)
    with pytest.raises(ToolError, match="failed_acceptance") as raised:
        await server.drawing_finalize(
            save_path=str(tmp_path / "part.dxf"),
            requirements=_raw(_diameter_row("R2", handles["wide"])),
            ctx=_FakeCtx(backend),
        )
    assert "fail: R2" in str(raised.value)


@pytest.mark.asyncio
async def test_finalize_without_requirements_is_unchanged(backend, tmp_path):
    await _open_part(backend, tmp_path)
    payload = await server.drawing_finalize(
        save_path=str(tmp_path / "part.dxf"), ctx=_FakeCtx(backend)
    )
    assert "score" in payload
    assert "geometry_acceptance" not in payload and "coverage" not in payload


@pytest.mark.asyncio
async def test_finalize_refuses_requirements_that_do_not_parse_before_saving(backend, tmp_path):
    await _open_part(backend, tmp_path)
    saved = tmp_path / "part.dxf"
    with pytest.raises(ToolError, match="invalid_args: requirements"):
        await server.drawing_finalize(
            save_path=str(saved),
            requirements={"units": "mm", "items": [{"id": "R1"}]},
            ctx=_FakeCtx(backend),
        )
    assert not saved.exists()


@pytest.mark.asyncio
async def test_drawing_deliver_tool_reports_failed_acceptance(backend, tmp_path):
    handles = await _open_part(backend, tmp_path)
    result = await server.drawing_deliver(
        output_dir=str(tmp_path / "bundle"),
        formats=["dxf"],
        min_score=0,
        strict_critique=False,
        requirements=_raw(_diameter_row("R2", handles["wide"])),
        ctx=_FakeCtx(backend),
    )
    assert result["status"] == "failed_acceptance"
    assert result["geometry_acceptance"]["counts"]["fail"] == 1


@pytest.mark.asyncio
async def test_the_tool_is_registered_core_lean_and_in_section_26():
    tools = {tool.name: tool for tool in await server._registered_tools()}
    tool = tools["drawing_verify_requirements"]
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is False
    assert tool.meta["cad"]["cost"] == "safe"
    assert "drawing_verify_requirements" in server.LEAN_TOOL_NAMES
    assert not any(
        "drawing_verify_requirements" in names for names in server.PACK_TOOL_NAMES.values()
    )
    groups = await server._tool_groups()
    assert "drawing_verify_requirements" in groups["understanding"]
    record = aliases_for("drawing_verify_requirements")
    assert record is not None and "verify requirements" in record.synonyms
