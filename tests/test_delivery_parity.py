"""Task 17 (B21 / B23 residue): delivery parity over every space, the delivery
context of the validator, the manifest's new fields, verify_manifest's
consistency checks and the build stamp.

Parity expectations come from engineering/understand/diff.py's documented
rules, worked by hand below: the default move tolerance is 2 % of the robust
diagonal of the model-space anchors, and a pure translation is ``moved_by``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import ezdxf
import pytest

from engineering import delivery
from engineering.delivery import compare_artifacts, deliver_drawing, verify_manifest
from engineering.layers import ensure_engineering_layers, ensure_standard_linetypes
from engineering.understand.diff import block_signatures, read_revision
from engineering.understand.snapshot import records_from_doc
from engineering.validator import DrawingValidator
from engineering.verification.report import document_identity_of
from engineering.verification.types import FeatureSelector, Requirement, RequirementSet
from version import __version__


def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sheet():
    """Model space: a 200 x 100 frame, a hole r = 10 at (50, 50), a BOLT insert at
    (150, 50); Layout1: one line (10, 10)-(60, 10)."""
    doc = ezdxf.new("R2010", units=4)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True)
    hole = msp.add_circle((50, 50), 10)
    doc.blocks.new("BOLT").add_circle((0, 0), 3)
    msp.add_blockref("BOLT", (150, 50))
    note = doc.layouts.get("Layout1").add_line((10, 10), (60, 10))
    return doc, hole, note


def _revision(doc):
    return records_from_doc(doc, source="test", insert_boxes=False), block_signatures(doc, tol=1e-6)


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


def test_an_identical_read_is_parity_over_every_space():
    doc, _hole, _note = _sheet()
    result = compare_artifacts(_revision(doc), _revision(doc))
    assert result["ok"] is True and result["differences"] == []
    summary = result["summary"]
    assert (summary["changed"], summary["added"], summary["removed"]) == (0, 0, 0)
    assert result["counts"]["source"] == {"Layout1": 1, "Model": 3}
    assert result["blocks"]["compared"] is True


def test_a_hole_moved_inside_the_outline_breaks_parity():
    doc, hole, _note = _sheet()
    source = _revision(doc)
    hole.dxf.center = (50.5, 50.0, 0.0)
    result = compare_artifacts(source, _revision(doc))
    assert result["ok"] is False
    # model-space anchors x: 50 / 50.5, 100, 150 on both sides -> robust box
    # x 50..150, y 50..50, diagonal 100, move_tol = 0.02 x 100 = 2.0 >= 0.5,
    # so the hole is paired and reported as moved, not as removed + added
    assert result["move_tol"] == 2.0
    summary = result["summary"]
    assert (summary["changed"], summary["added"], summary["removed"]) == (1, 0, 0)
    (row,) = next(item for item in result["differences"] if item["field"] == "changed")["rows"]
    assert row["changes"] == {"moved_by": [0.5, 0.0]}


def test_a_change_on_a_paper_layout_breaks_parity():
    doc, _hole, note = _sheet()
    source = _revision(doc)
    note.dxf.end = (60.0, 12.0, 0.0)
    result = compare_artifacts(source, _revision(doc))
    assert result["ok"] is False
    (row,) = next(item for item in result["differences"] if item["field"] == "changed")["rows"]
    assert row["space"] == "Layout1"


def test_a_redrawn_block_definition_breaks_parity():
    doc, _hole, _note = _sheet()
    source = _revision(doc)
    doc.blocks.get("BOLT").query("CIRCLE")[0].dxf.radius = 4.0
    result = compare_artifacts(source, _revision(doc))
    assert result["ok"] is False
    assert result["summary"]["changed"] == 0, "every INSERT record is unchanged"
    assert {"field": "blocks_changed", "names": ["BOLT"]} in result["differences"]


def test_read_revision_can_skip_insert_boxes(tmp_path):
    doc, _hole, _note = _sheet()
    path = tmp_path / "sheet.dxf"
    doc.saveas(path)
    snap, blocks = read_revision(str(path), tol=1e-6, insert_boxes=False)
    (insert,) = [record for record in snap.records if record.type == "INSERT"]
    assert insert.bbox is None and snap.insert_boxes is False
    assert set(blocks) == {"BOLT"}


@pytest.mark.asyncio
async def test_the_saved_step_checks_the_delivered_dxf(backend, tmp_path):
    await backend.entity_create_line(0, 0, 10, 0)
    delivered = tmp_path / "drawing.dxf"
    ezdxf.new("R2010").saveas(delivered)
    result = await DrawingValidator().run(backend, delivered_path=str(delivered))
    codes = {finding.code for finding in result.findings}
    assert "not_saved" not in codes and "no_path" not in codes
    missing = await DrawingValidator().run(backend, delivered_path=str(tmp_path / "absent.dxf"))
    assert "delivery_artifact_missing" in {finding.code for finding in missing.findings}
    empty = tmp_path / "empty.dxf"
    empty.write_bytes(b"")
    unreadable = await DrawingValidator().run(backend, delivered_path=str(empty))
    assert "delivery_artifact_unreadable" in {finding.code for finding in unreadable.findings}
    legacy = await DrawingValidator().run(backend)
    assert "not_saved" in {finding.code for finding in legacy.findings}


def test_the_git_sha_is_the_server_checkout_not_the_working_directory(tmp_path, monkeypatch):
    root = Path(delivery.__file__).resolve().parents[1]
    expected = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.chdir(tmp_path)  # not a git checkout
    assert delivery._git_sha() == expected


def test_the_git_sha_is_none_outside_a_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "_PACKAGE_ROOT", tmp_path)
    assert delivery._git_sha() is None


@pytest.mark.asyncio
async def test_delivery_never_re_points_the_document_and_records_where_it_was(backend, tmp_path):
    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    await backend.entity_create_line(0, 0, 40, 0, layer="GEOMETRY")
    identity = await document_identity_of(backend)
    bundle = tmp_path / "bundle"
    result = await deliver_drawing(
        backend, bundle, formats=["dxf"], min_score=0, strict_critique=False
    )
    assert result.status == "success"
    info = await backend.drawing_info()
    assert (info.full_path, info.saved) == ("", False)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1.1"
    assert manifest["source_document"] == {"name": "untitled.dxf", "path": None, "saved": False}
    assert manifest["dxf_export"] == {
        "method": "ezdxf.write",
        "dxf_version": "AC1024",
        "repointed": False,
    }
    assert manifest["document"]["document_id"] == identity["document_id"]
    assert manifest["document"]["revision"] == identity["revision"]
    assert manifest["build"] == {"version": __version__, "git_sha": manifest["git_sha"]}
    assert manifest["parity"]["ok"] is True
    assert manifest["parity"]["counts"]["source"] == {"Model": 1}
    assert manifest["parity"]["summary"]["unchanged"] == 1
    assert (manifest["requirements_hash"], manifest["report_hash"]) == (None, None)
    codes = {finding["code"] for finding in manifest["validator"]["findings"]}
    assert "not_saved" not in codes and "no_path" not in codes


@pytest.mark.asyncio
async def test_the_requirements_are_measured_on_the_delivered_dxf(backend, tmp_path):
    handles = await _open_part(backend, tmp_path)
    rs = RequirementSet(requirements=(_diameter("R1", handles["bore"]),), units="mm")
    bundle = tmp_path / "bundle"
    result = await deliver_drawing(
        backend, bundle, formats=["dxf"], min_score=0, strict_critique=False, requirements=rs
    )
    assert result.status == "success"
    report = json.loads((bundle / "acceptance.json").read_text(encoding="utf-8"))
    assert Path(report["artifact"]["path"]).resolve() == (bundle / "drawing.dxf").resolve()
    assert report["artifact"]["sha256"] == _sha(bundle / "drawing.dxf")
    assert report["rebinding"]["handles_stable"] is True
    assert [row["method"] for row in report["rebinding"]["rows"]] == ["handle"]
    assert report["comparison"] == [
        {
            "id": "R1",
            "before": {"status": "pass", "value": 40.0},
            "after": {"status": "pass", "value": 40.0},
            "changed": False,
        }
    ]
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["requirements_hash"] == rs.digest()
    assert manifest["report_hash"] == _sha(bundle / "acceptance.json")
    assert manifest["source_document"]["path"] == str(tmp_path / "base.dxf")
    info = await backend.drawing_info()
    assert info.full_path == str(tmp_path / "base.dxf"), "the delivery re-pointed the document"


@pytest.mark.asyncio
async def test_verify_manifest_checks_the_report_and_requirements_hashes(backend, tmp_path):
    handles = await _open_part(backend, tmp_path)
    rs = RequirementSet(requirements=(_diameter("R1", handles["bore"]),), units="mm")
    bundle = tmp_path / "bundle"
    await deliver_drawing(
        backend, bundle, formats=["dxf"], min_score=0, strict_critique=False, requirements=rs
    )
    assert verify_manifest(str(bundle / "manifest.json"))["ok"] is True
    report_path = bundle / "acceptance.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["requirements_digest"] = "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")
    checked = verify_manifest(str(bundle / "manifest.json"))
    assert checked["ok"] is False
    assert [row["filename"] for row in checked["stale"]] == ["acceptance.json"]
    assert [row["field"] for row in checked["inconsistent"]] == ["requirements_hash"]
