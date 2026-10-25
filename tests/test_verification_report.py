"""Task 16 (B22, F02): the acceptance report and verify_requirements.

Every expected number is hand geometry written here, never read back from the
module under test: a circle of radius r has diameter 2r, so a bore drawn at
r = 20 is Ø40 and one drawn at r = 21 is Ø42 (delta +2 against a nominal 40).
"""

from __future__ import annotations

import hashlib

import ezdxf
import pytest

from engineering.layers import ensure_engineering_layers, ensure_standard_linetypes
from engineering.verification.report import (
    SCHEMA_VERSION,
    attach_dimension_evidence,
    build_report,
    document_identity_of,
    mark_stale,
    render_markdown,
    summarize,
    verify_requirements,
)
from engineering.verification.types import (
    Binding,
    FeatureRef,
    FeatureSelector,
    Measurement,
    ObservedGeometry,
    Requirement,
    RequirementSet,
)

OBSERVED = ObservedGeometry(
    features=(),
    declared_unit="mm",
    inferred_unit="mm",
    document_id="d1",
    revision=3,
    source="memory:untitled",
)
DOCUMENT = {
    "document_id": "d1",
    "revision": 3,
    "name": "untitled.dxf",
    "path": None,
    "backend": "ezdxf",
}
DIAMETER_40 = (("nominal", 40.0), ("unit", "mm"))


def _req(rid, handle, *, quantity="diameter", target=DIAMETER_40, tolerance=None):
    return Requirement(
        id=rid,
        quantity=quantity,
        target=target,
        features=(FeatureSelector(kind="handle", value=(("handle", handle),)),),
        provenance="explicit",
        source=(("document", "sheet-1"),),
        manufacturing_tolerance=tolerance,
    )


def _ref(handle):
    return FeatureRef(
        document_id="d1",
        revision=3,
        space="Model",
        block_path=(),
        handles=(handle,),
        feature=None,
    )


def _bound(rid, handle):
    return Binding(
        requirement_id=rid,
        status="bound",
        refs=(_ref(handle),),
        candidates=(),
        reason=f"handle {handle}",
    )


def _unbound(rid, reason):
    return Binding(requirement_id=rid, status="unknown", refs=(), candidates=(), reason=reason)


def _measured(rid, value, status, method="circle_diameter"):
    return Measurement(
        requirement_id=rid, value=value, unit="mm", method=method, residual=None, status=status
    )


def _seven():
    """Seven requirements, one per way a row can end (statuses assigned by hand)."""
    rs = RequirementSet(
        requirements=(
            _req("R1", "2A"),  # Ø40 measured 40: pass
            _req("R2", "2B"),  # Ø40 measured 42: fail, delta +2
            _req("R3", "FFFF0", quantity="distance", target=(("nominal", 100.0), ("unit", "mm"))),
            _req("R4", "2C", quantity="radius", target=(("nominal", 5.0), ("unit", "mm"))),
            _req("R5", "2D", quantity="count", target=(("exact", 6),)),
            _req("R6", "2E"),  # nothing was produced for it at all
            _req("R7", "2F"),  # unbound, yet an evaluator said pass
        ),
        units="mm",
    )
    bindings = (
        _bound("R1", "2A"),
        _bound("R2", "2B"),
        _unbound("R3", "no entity has handle FFFF0"),
        _bound("R4", "2C"),
        Binding(
            requirement_id="R5",
            status="ambiguous",
            refs=(),
            candidates=(_ref("2D"), _ref("3D")),
            reason="two equal candidates",
        ),
        _unbound("R7", "no entity has handle 2F"),
    )
    measurements = (
        _measured("R1", 40.0, "pass"),
        _measured("R2", 42.0, "fail"),
        _measured("R3", None, "unknown", method="binding"),
        _measured("R4", None, "unsupported", method="spline"),
        _measured("R5", None, "unknown", method="count"),
        _measured("R7", 40.0, "pass"),
    )
    return rs, bindings, measurements


def _rows(report):
    return {row["id"]: row for row in report["rows"]}


def test_every_requirement_gets_one_row_and_the_statuses_add_up():
    rs, bindings, measurements = _seven()
    report = build_report(rs, bindings, measurements, observed=OBSERVED, document=DOCUMENT)
    assert report["schema_version"] == SCHEMA_VERSION == 1
    assert [row["id"] for row in report["rows"]] == ["R1", "R2", "R3", "R4", "R5", "R6", "R7"]
    assert report["counts"] == {
        "required": 7,
        "pass": 1,
        "fail": 1,
        "unknown": 3,
        "unsupported": 1,
        "ambiguous": 1,
    }
    statuses = ("pass", "fail", "unknown", "unsupported", "ambiguous")
    assert sum(report["counts"][status] for status in statuses) == 7
    assert report["status"] == "fail"
    assert report["accepted"] is False
    assert report["coverage"] == {"required": 7, "measured": 2, "ratio": 2 / 7}
    assert report["requirements_digest"] == rs.digest()
    assert report["evidence"]["document_id"] == "d1"
    assert report["evidence"]["revision"] == 3


def test_no_silent_pass():
    rs, bindings, measurements = _seven()
    rows = _rows(build_report(rs, bindings, measurements, observed=OBSERVED, document=DOCUMENT))
    assert (rows["R3"]["status"], rows["R3"]["kind"]) == ("unknown", "missing_requirement")
    assert (rows["R4"]["status"], rows["R4"]["kind"]) == ("unsupported", "unsupported_measurement")
    assert (rows["R5"]["status"], rows["R5"]["kind"]) == ("ambiguous", "ambiguous_binding")
    assert len(rows["R5"]["binding"]["candidates"]) == 2
    assert (rows["R6"]["status"], rows["R6"]["kind"]) == ("unknown", "missing_requirement")
    assert rows["R6"]["measured"] is None and rows["R6"]["binding"] is None
    assert (rows["R7"]["status"], rows["R7"]["kind"]) == ("unknown", "missing_requirement"), (
        "an evaluator's pass on an unbound requirement is not a pass"
    )
    assert rows["R7"]["delta"] is None


def test_a_row_carries_target_measured_delta_basis_handles_and_source():
    rs, bindings, measurements = _seven()
    rows = _rows(build_report(rs, bindings, measurements, observed=OBSERVED, document=DOCUMENT))
    r2 = rows["R2"]
    assert r2["target"] == {"nominal": 40.0, "unit": "mm"}
    assert r2["measured"]["value"] == 42.0
    assert r2["measured"]["unit"] == "mm"
    assert abs(r2["delta"] - 2.0) <= 1e-9  # 42 - 40
    assert r2["tolerance_basis"] == "verification_epsilon"
    assert r2["handles"] == ["2B"]
    assert r2["source"] == {"document": "sheet-1"}
    assert r2["provenance"] == "explicit"
    assert rows["R1"]["delta"] == 0.0  # 40 - 40


def test_a_manufacturing_tolerance_with_its_source_is_the_basis():
    tolerance = (("lower", -0.1), ("source", "drawing note 3"), ("upper", 0.1))
    rs = RequirementSet(requirements=(_req("R1", "2A", tolerance=tolerance),), units="mm")
    report = build_report(
        rs,
        (_bound("R1", "2A"),),
        (_measured("R1", 40.0, "pass"),),
        observed=OBSERVED,
        document=DOCUMENT,
    )
    row = report["rows"][0]
    assert row["tolerance_basis"] == "manufacturing_tolerance"
    assert row["tolerance"] == {
        "manufacturing_tolerance": {"lower": -0.1, "source": "drawing note 3", "upper": 0.1},
        "verification_epsilon": None,
    }


def test_an_empty_set_verifies_nothing_and_carries_the_legacy_dimensions():
    rs = RequirementSet(requirements=(), units="mm", legacy=(("dimensions", (("bore", 40.0),)),))
    report = build_report(rs, (), (), observed=OBSERVED, document=DOCUMENT)
    assert report["status"] == "no_requirements"
    assert report["accepted"] is False
    assert report["counts"]["required"] == 0
    assert report["coverage"]["ratio"] is None
    assert report["legacy"] == {"dimensions": [["bore", 40.0]]}


def test_every_requirement_passing_is_accepted():
    rs = RequirementSet(requirements=(_req("R1", "2A"),), units="mm")
    report = build_report(
        rs,
        (_bound("R1", "2A"),),
        (_measured("R1", 40.0, "pass"),),
        observed=OBSERVED,
        document=DOCUMENT,
    )
    assert (report["status"], report["accepted"], report["blocking"]) == ("pass", True, [])


def test_the_evidence_goes_stale_when_the_revision_moves():
    rs = RequirementSet(requirements=(_req("R1", "2A"),), units="mm")
    report = build_report(
        rs,
        (_bound("R1", "2A"),),
        (_measured("R1", 40.0, "pass"),),
        observed=OBSERVED,
        document=DOCUMENT,
    )
    fresh = mark_stale(report, DOCUMENT)
    assert (fresh["stale"], fresh["accepted"]) == (False, True)
    moved = mark_stale(report, {**DOCUMENT, "revision": 4})
    assert moved["stale"] is True and moved["accepted"] is False
    assert "3" in moved["stale_reason"] and "4" in moved["stale_reason"]
    assert all(row["stale"] for row in moved["rows"])
    other = mark_stale(report, {**DOCUMENT, "document_id": "d2"})
    assert other["stale"] is True and "d2" in other["stale_reason"]


def test_the_evidence_goes_stale_when_the_artifact_changes(tmp_path):
    artifact = tmp_path / "part.dxf"
    artifact.write_bytes(b"0\nEOF\n")
    record = {
        "path": str(artifact),
        "sha256": hashlib.sha256(b"0\nEOF\n").hexdigest(),
        "size_bytes": 6,
    }
    rs = RequirementSet(requirements=(_req("R1", "2A"),), units="mm")
    report = build_report(
        rs,
        (_bound("R1", "2A"),),
        (_measured("R1", 40.0, "pass"),),
        observed=OBSERVED,
        document={"document_id": None, "revision": None, "name": "part.dxf", "path": str(artifact)},
        artifact=record,
    )
    assert mark_stale(report, None)["stale"] is False
    artifact.write_bytes(b"0\nSECTION\n0\nEOF\n")
    changed = mark_stale(report, None)
    assert changed["stale"] is True and "changed after it was measured" in changed["stale_reason"]
    artifact.unlink()
    assert "is gone" in mark_stale(report, None)["stale_reason"]


def test_a_dimension_that_disagrees_with_its_geometry_is_a_blocking_finding():
    rs = RequirementSet(requirements=(_req("R1", "2A"),), units="mm")
    report = build_report(
        rs,
        (_bound("R1", "2A"),),
        (_measured("R1", 40.0, "pass"),),
        observed=OBSERVED,
        document=DOCUMENT,
    )
    agreeing = attach_dimension_evidence(
        report,
        {
            "R1": {
                "handle": "3A",
                "refs": (),
                "geometric_value": 40.0,
                "dimension_measurement": 40.0,
                "displayed_text": "40",
                "mismatch": False,
            }
        },
    )
    assert agreeing["findings"] == [] and agreeing["accepted"] is True
    lying = attach_dimension_evidence(
        report,
        {
            "R1": {
                "handle": "3A",
                "refs": (),
                "geometric_value": 42.0,
                "dimension_measurement": 42.0,
                "displayed_text": "40",
                "mismatch": True,
            }
        },
    )
    (finding,) = lying["findings"]
    assert (finding["kind"], finding["severity"], finding["requirement"]) == (
        "dimension_mismatch",
        "error",
        "R1",
    )
    assert lying["rows"][0]["dimension"]["displayed_text"] == "40"
    assert lying["accepted"] is False
    assert "dimension_mismatch: R1" in lying["blocking"]


def test_markdown_lists_every_row_and_the_denominators():
    rs, bindings, measurements = _seven()
    text = render_markdown(
        build_report(rs, bindings, measurements, observed=OBSERVED, document=DOCUMENT)
    )
    assert text.startswith("# Acceptance report\n")
    assert "| 7 | 1 | 1 | 3 | 1 | 1 |" in text
    assert (
        "| R1 | diameter | 40 mm | 40 | +0 | verification_epsilon | pass | 2A | document=sheet-1 |"
        in text
    )
    assert (
        "| R2 | diameter | 40 mm | 42 | +2 | verification_epsilon | fail | 2B | document=sheet-1 |"
        in text
    )
    assert (
        "| R5 | count | exactly 6 | - | - | verification_epsilon | ambiguous | - | document=sheet-1 |"
        in text
    )
    assert "## Not accepted because" in text


def test_summarize_keeps_the_verdict_and_drops_the_rows():
    rs, bindings, measurements = _seven()
    summary = summarize(
        build_report(rs, bindings, measurements, observed=OBSERVED, document=DOCUMENT)
    )
    assert set(summary) == {
        "status",
        "accepted",
        "stale",
        "stale_reason",
        "counts",
        "coverage",
        "blocking",
        "requirements_digest",
    }
    assert summary["status"] == "fail"


# -- on real CAD geometry (headless engine) ------------------------------------------


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
async def test_verify_requirements_measures_the_real_circles(backend, tmp_path):
    handles = await _open_part(backend, tmp_path)
    rs = RequirementSet(
        requirements=(
            _req("R1", handles["bore"]),
            _req("R2", handles["wide"]),
            _req("R3", "FFFF0"),
        ),
        units="mm",
    )
    report = await verify_requirements(backend, rs)
    rows = _rows(report)
    assert rows["R1"]["status"] == "pass"
    assert abs(rows["R1"]["measured"]["value"] - 40.0) <= 1e-9  # 2 x 20
    assert rows["R2"]["status"] == "fail"
    assert abs(rows["R2"]["measured"]["value"] - 42.0) <= 1e-9  # 2 x 21
    assert abs(rows["R2"]["delta"] - 2.0) <= 1e-9
    assert rows["R2"]["handles"] == [handles["wide"]]
    assert (rows["R3"]["status"], rows["R3"]["kind"]) == ("unknown", "missing_requirement")
    assert report["counts"] == {
        "required": 3,
        "pass": 1,
        "fail": 1,
        "unknown": 1,
        "unsupported": 0,
        "ambiguous": 0,
    }
    identity = await document_identity_of(backend)
    assert report["evidence"]["document_id"] == identity["document_id"]
    assert report["evidence"]["revision"] == identity["revision"]
    assert report["stale"] is False


@pytest.mark.asyncio
async def test_the_evidence_goes_stale_when_the_drawing_changes(backend, tmp_path):
    handles = await _open_part(backend, tmp_path)
    rs = RequirementSet(requirements=(_req("R1", handles["bore"]),), units="mm")
    report = await verify_requirements(backend, rs)
    assert report["accepted"] is True
    await backend.entity_create_line(0, 0, 10, 0, layer="GEOMETRY")
    later = mark_stale(report, await document_identity_of(backend))
    assert later["stale"] is True
    assert later["accepted"] is False


@pytest.mark.asyncio
async def test_a_dimension_that_says_40_over_42_fails_and_is_a_finding(backend, tmp_path):
    """Spec §8.2 item 8: the text says 40, the geometry measures 42, DIMLFAC is 2."""
    doc = ezdxf.new("R2010", setup=True, units=4)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True)
    msp.add_circle((50, 50), 21)  # measures Ø42
    dim = msp.add_diameter_dim(
        center=(50, 50),
        radius=21,
        angle=0,  # defpoints on the 0 and 180 degree quadrants: (71, 50) and (29, 50)
        text="40",
        override={"dimlfac": 2.0},
        dimstyle="EZ_RADIUS",
    )
    dim.render()
    path = tmp_path / "dim.dxf"
    doc.saveas(path)
    await backend.drawing_open(str(path))
    rs = RequirementSet(
        requirements=(
            Requirement(
                id="RD",
                quantity="diameter",
                target=DIAMETER_40,
                features=(
                    FeatureSelector(
                        kind="dimension", value=(("handle", dim.dimension.dxf.handle),)
                    ),
                ),
                provenance="explicit",
                source=(("label", "Ø40"),),
            ),
        ),
        units="mm",
    )
    report = await verify_requirements(backend, rs)
    (row,) = report["rows"]
    assert row["status"] == "fail"
    assert abs(row["measured"]["value"] - 42.0) <= 1e-9  # the geometry: 2 x 21
    assert abs(row["delta"] - 2.0) <= 1e-9
    assert row["dimension"]["displayed_text"] == "40"
    assert abs(row["dimension"]["geometric_value"] - 42.0) <= 1e-9
    assert row["dimension"]["dimension_measurement"] is not None
    assert row["dimension"]["mismatch"] is True
    assert [finding["kind"] for finding in report["findings"]] == ["dimension_mismatch"]
    assert report["accepted"] is False


@pytest.mark.asyncio
async def test_an_artifact_is_measured_on_the_file_not_on_the_document(backend, tmp_path):
    handles = await _open_part(backend, tmp_path)
    artifact = tmp_path / "artifact.dxf"
    await backend.drawing_export_dxf(str(artifact))
    edited = ezdxf.readfile(artifact)
    edited.entitydb[handles["bore"]].dxf.radius = 21.0  # the saved bore is Ø42 now
    edited.saveas(artifact)
    rs = RequirementSet(requirements=(_req("R1", handles["bore"]),), units="mm")
    on_document = await verify_requirements(backend, rs)
    on_file = await verify_requirements(backend, rs, artifact_path=str(artifact))
    assert _rows(on_document)["R1"]["status"] == "pass"
    row = _rows(on_file)["R1"]
    assert row["status"] == "fail"
    assert abs(row["measured"]["value"] - 42.0) <= 1e-9  # 2 x 21
    assert on_file["artifact"]["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert on_file["document"]["document_id"] is None


@pytest.mark.asyncio
async def test_a_drawing_with_a_block_reference_is_measured_not_refused(backend, tmp_path):
    """Task 13's observe() refuses a snapshot holding an INSERT without its block
    definitions; the document and the artifact are both read with block_defs=True."""
    doc = ezdxf.new("R2010", units=4)
    mark = doc.blocks.new("MARK")
    mark.add_circle((0, 0), 5)
    msp = doc.modelspace()
    bore = msp.add_circle((50, 50), 20)  # Ø40: 2 x 20
    msp.add_blockref("MARK", (150, 50))
    path = tmp_path / "with_block.dxf"
    doc.saveas(path)
    await backend.drawing_open(str(path))
    rs = RequirementSet(requirements=(_req("R1", bore.dxf.handle),), units="mm")
    on_document = await verify_requirements(backend, rs)
    on_file = await verify_requirements(backend, rs, artifact_path=str(path))
    assert _rows(on_document)["R1"]["status"] == "pass"
    assert _rows(on_file)["R1"]["status"] == "pass"
