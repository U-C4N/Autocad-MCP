"""Task 16: drawing_deliver(requirements=) and verify_manifest.

A requirement that does not pass blocks delivery whatever the drafting score
(status failed_acceptance), and verify_manifest recomputes every hash the
manifest recorded: an artifact edited after the manifest was written is stale
(spec §8.2 item 9). Ø40 is a circle of r = 20, Ø42 one of r = 21.
"""

from __future__ import annotations

import hashlib
import json

import ezdxf
import pytest

from engineering.delivery import deliver_drawing, verify_manifest
from engineering.layers import ensure_engineering_layers, ensure_standard_linetypes
from engineering.verification.types import FeatureSelector, Requirement, RequirementSet


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _diameter(rid, handle, nominal=40.0):
    return Requirement(
        id=rid,
        quantity="diameter",
        target=(("nominal", nominal), ("unit", "mm")),
        features=(FeatureSelector(kind="handle", value=(("handle", handle),)),),
        provenance="explicit",
        source=(("document", "synthetic-sheet"),),
    )


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


async def _clean(backend):
    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    await backend.entity_create_line(0, 0, 40, 0, layer="GEOMETRY")


@pytest.mark.asyncio
async def test_a_failed_requirement_blocks_delivery_whatever_the_score(backend, tmp_path):
    handles = await _open_part(backend, tmp_path)
    rs = RequirementSet(
        requirements=(_diameter("R1", handles["bore"]), _diameter("R2", handles["wide"])),
        units="mm",
    )
    bundle = tmp_path / "bundle"
    result = await deliver_drawing(
        backend, bundle, formats=["dxf"], min_score=0, strict_critique=False, requirements=rs
    )
    assert result.status == "failed_acceptance"
    data = result.to_dict()
    assert data["drafting_quality"] == data["score"]
    assert data["geometry_acceptance"]["status"] == "fail"
    assert data["geometry_acceptance"]["counts"] == {
        "required": 2,
        "pass": 1,
        "fail": 1,
        "unknown": 0,
        "unsupported": 0,
        "ambiguous": 0,
    }
    assert data["coverage"] == {"required": 2, "measured": 2, "ratio": 1.0}
    assert "fail: R2" in data["blocking"]
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed_acceptance"
    assert manifest["acceptance"]["report"]["sha256"] == _sha(bundle / "acceptance.json")
    report = json.loads((bundle / "acceptance.json").read_text(encoding="utf-8"))
    assert [row["status"] for row in report["rows"]] == ["pass", "fail"]
    assert abs(report["rows"][1]["measured"]["value"] - 42.0) <= 1e-9  # 2 x 21
    assert (bundle / "acceptance.md").read_text(encoding="utf-8").startswith("# Acceptance report")


@pytest.mark.asyncio
async def test_requirements_that_pass_deliver_success(backend, tmp_path):
    handles = await _open_part(backend, tmp_path)
    rs = RequirementSet(requirements=(_diameter("R1", handles["bore"]),), units="mm")
    result = await deliver_drawing(
        backend,
        tmp_path / "bundle",
        formats=["dxf"],
        min_score=0,
        strict_critique=False,
        requirements=rs,
    )
    assert result.status == "success"
    data = result.to_dict()
    assert data["geometry_acceptance"]["accepted"] is True
    assert data["artifact_integrity"]["ok"] is True
    assert data["blocking"] == []


@pytest.mark.asyncio
async def test_without_requirements_the_result_is_unchanged(backend, tmp_path):
    await _clean(backend)
    bundle = tmp_path / "bundle"
    result = await deliver_drawing(
        backend, bundle, formats=["dxf"], min_score=0, strict_critique=False
    )
    assert set(result.to_dict()) == {
        "status",
        "output_dir",
        "manifest_path",
        "artifacts",
        "score",
        "parity",
    }
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert "acceptance" not in manifest
    assert not (bundle / "acceptance.json").exists()


@pytest.mark.asyncio
async def test_validation_failure_comes_first_and_every_reason_is_listed(backend, tmp_path):
    handles = await _open_part(backend, tmp_path)
    await backend.construction_xline(0, 0, 45)  # construction_left: a critique error
    rs = RequirementSet(requirements=(_diameter("R2", handles["wide"]),), units="mm")
    result = await deliver_drawing(
        backend, tmp_path / "bundle", formats=["dxf"], min_score=0, requirements=rs
    )
    assert result.status == "failed_validation"
    blocking = result.to_dict()["blocking"]
    assert any(reason.startswith("critique:") for reason in blocking)
    assert "fail: R2" in blocking


@pytest.mark.asyncio
async def test_verify_manifest_rehashes_and_reports_stale_and_missing(backend, tmp_path):
    await _clean(backend)
    bundle = tmp_path / "bundle"
    await deliver_drawing(backend, bundle, formats=["dxf"], min_score=0, strict_critique=False)
    clean = verify_manifest(str(bundle / "manifest.json"))
    assert clean["ok"] is True
    assert (clean["stale"], clean["missing"], clean["inconsistent"]) == ([], [], [])
    assert {row["filename"] for row in clean["checked"]} == {"drawing.dxf", "validation.json"}
    with (bundle / "drawing.dxf").open("ab") as stream:
        stream.write(b"999\nedited after the manifest was written\n")
    edited = verify_manifest(str(bundle / "manifest.json"))
    assert edited["ok"] is False
    assert [row["filename"] for row in edited["stale"]] == ["drawing.dxf"]
    assert edited["stale"][0]["sha256"] == _sha(bundle / "drawing.dxf")
    (bundle / "validation.json").unlink()
    gone = verify_manifest(str(bundle / "manifest.json"))
    assert [row["filename"] for row in gone["missing"]] == ["validation.json"]


def test_verify_manifest_refuses_a_file_that_is_not_a_manifest(tmp_path):
    text = tmp_path / "manifest.json"
    text.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not a readable delivery manifest"):
        verify_manifest(str(text))
    text.write_text(json.dumps({"status": "success"}), encoding="utf-8")
    with pytest.raises(ValueError, match="no artifacts list"):
        verify_manifest(str(text))


def test_verify_manifest_never_reads_outside_its_folder(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (tmp_path / "secret.dxf").write_bytes(b"outside")
    manifest = {
        "status": "success",
        "artifacts": [{"format": "dxf", "filename": "../secret.dxf", "sha256": "0" * 64}],
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = verify_manifest(str(bundle / "manifest.json"))
    assert result["ok"] is False
    assert result["checked"] == []
    assert [row["field"] for row in result["inconsistent"]] == ["filename"]
