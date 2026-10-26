"""Task 17 (B21): requirements measured again on the saved file, read fresh.

Spec §8.2 item 9: a save that renumbers handles is re-bound by feature tag,
then by geometry; a copied feature tag makes the binding ambiguous, which is
`unknown` - never a pick. Ø40 is a circle of r = 20, a pin of r = 5 is Ø10.
"""

from __future__ import annotations

from pathlib import Path

import ezdxf
import pytest

import server
from backends.base import UnsupportedCapabilityError
from engineering.layers import ensure_engineering_layers, ensure_standard_linetypes
from engineering.mech.xdata import encode
from engineering.understand.diff import diff_snapshots
from engineering.understand.snapshot import read_snapshot
from engineering.verification import artifact
from engineering.verification.artifact import ArtifactReadError, verify_artifact
from engineering.verification.report import verify_requirements
from engineering.verification.types import FeatureSelector, Requirement, RequirementSet

TAG = {"kind": "feature", "part": "P1", "feature": "bore", "view": "front"}


class _FakeCtx:
    def __init__(self, backend):
        self.lifespan_context = {"backend": backend}

    async def info(self, message):
        pass

    async def warning(self, message):
        pass


def _diameter(rid, handle, nominal=40.0):
    return Requirement(
        id=rid,
        quantity="diameter",
        target=(("nominal", nominal), ("unit", "mm")),
        features=(FeatureSelector(kind="handle", value=(("handle", handle),)),),
        provenance="explicit",
        source=(("document", "synthetic-sheet"),),
    )


def _raw_diameter(rid, handle, nominal=40.0) -> dict:
    return {
        "units": "mm",
        "items": [
            {
                "id": rid,
                "quantity": "diameter",
                "target": {"nominal": nominal, "unit": "mm"},
                "features": [{"kind": "handle", "handle": handle}],
                "provenance": "explicit",
                "source": {"document": "synthetic-sheet"},
            }
        ],
    }


def _write(path, circles) -> dict:
    """A 200 x 100 frame, then ``circles`` in creation order: (name, cx, cy, r, tagged).
    Returns ``{name: handle}``; the frame is always created first."""
    doc = ezdxf.new("R2010", units=4)
    doc.appids.add("ACADMCP_MECH")
    msp = doc.modelspace()
    handles = {
        "frame": msp.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True).dxf.handle
    }
    for name, cx, cy, radius, tagged in circles:
        circle = msp.add_circle((cx, cy), radius)
        if tagged:
            rows = encode(TAG, kinds=("feature",))
            circle.set_xdata("ACADMCP_MECH", [row for row in rows if row[0] != 1001])
        handles[name] = circle.dxf.handle
    doc.saveas(path)
    return handles


@pytest.mark.asyncio
async def test_a_save_that_renumbers_handles_is_re_bound_by_geometry(backend, tmp_path):
    source = _write(
        tmp_path / "a.dxf",
        [("bore", 50, 50, 20, False), ("pin", 150, 50, 5, False)],
    )
    saved = _write(
        tmp_path / "b.dxf",
        [("pin", 150, 50, 5, False), ("bore", 50, 50, 20, False)],
    )
    assert saved["pin"] == source["bore"], "the save gave the bore's handle to the pin"
    await backend.drawing_open(str(tmp_path / "a.dxf"))
    rs = RequirementSet(requirements=(_diameter("R1", source["bore"]),), units="mm")
    report = await verify_artifact(str(tmp_path / "b.dxf"), rs, backend=backend)
    (row,) = report["rows"]
    assert row["status"] == "pass"
    assert abs(row["measured"]["value"] - 40.0) <= 1e-9  # the bore, 2 x 20 - not the pin
    assert row["handles"] == [saved["bore"]]
    assert report["rebinding"]["handles_stable"] is False
    assert [item["method"] for item in report["rebinding"]["rows"]] == ["geometry"]
    assert report["comparison"] == [
        {
            "id": "R1",
            "before": {"status": "pass", "value": 40.0},
            "after": {"status": "pass", "value": 40.0},
            "changed": False,
        }
    ]
    # the stability evidence, by hand: frame, bore and pin match exactly and only
    # the frame kept its handle - 1 of 3 is less than half
    diff = diff_snapshots(
        read_snapshot(str(tmp_path / "a.dxf")), read_snapshot(str(tmp_path / "b.dxf"))
    )
    assert diff["handle_evidence"] == {"exact_matches": 3, "kept_handle": 1}


@pytest.mark.asyncio
async def test_without_the_source_the_handle_is_bound_afresh(backend, tmp_path):
    source = _write(
        tmp_path / "a.dxf",
        [("bore", 50, 50, 20, False), ("pin", 150, 50, 5, False)],
    )
    _write(tmp_path / "b.dxf", [("pin", 150, 50, 5, False), ("bore", 50, 50, 20, False)])
    rs = RequirementSet(requirements=(_diameter("R1", source["bore"]),), units="mm")
    report = await verify_artifact(str(tmp_path / "b.dxf"), rs)
    (row,) = report["rows"]
    assert abs(row["measured"]["value"] - 10.0) <= 1e-9  # the handle names the pin: 2 x 5
    assert row["status"] == "fail"
    assert [item["method"] for item in report["rebinding"]["rows"]] == ["fresh"]
    assert report["comparison"] is None


@pytest.mark.asyncio
async def test_a_copied_feature_tag_is_unknown_never_a_pick(backend, tmp_path):
    source = _write(
        tmp_path / "a.dxf",
        [("bore", 50, 50, 20, True), ("pin", 150, 80, 5, False)],
    )
    saved = _write(
        tmp_path / "b.dxf",
        [("copy", 150, 50, 20, True), ("bore", 50, 50, 20, True), ("pin", 150, 80, 5, False)],
    )
    assert saved["copy"] == source["bore"]
    await backend.drawing_open(str(tmp_path / "a.dxf"))
    rs = RequirementSet(requirements=(_diameter("R1", source["bore"]),), units="mm")
    report = await verify_artifact(str(tmp_path / "b.dxf"), rs, backend=backend)
    (row,) = report["rows"]
    assert (row["status"], row["kind"]) == ("unknown", "missing_requirement")
    assert len(row["binding"]["candidates"]) == 2
    assert report["rebinding"]["handles_stable"] is False
    assert [item["method"] for item in report["rebinding"]["rows"]] == ["tag"]
    assert report["status"] == "incomplete"
    assert report["accepted"] is False


@pytest.mark.asyncio
async def test_verify_requirements_on_an_artifact_goes_through_verify_artifact(backend, tmp_path):
    source = _write(tmp_path / "a.dxf", [("bore", 50, 50, 20, False), ("pin", 150, 50, 5, False)])
    await backend.drawing_open(str(tmp_path / "a.dxf"))
    rs = RequirementSet(requirements=(_diameter("R1", source["bore"]),), units="mm")
    report = await verify_requirements(backend, rs, artifact_path=str(tmp_path / "a.dxf"))
    assert report["rebinding"]["handles_stable"] is True
    assert report["source_document"]["path"] == str(tmp_path / "a.dxf")


@pytest.mark.asyncio
async def test_a_dwg_on_the_headless_engine_is_refused(backend, tmp_path):
    dwg = tmp_path / "part.dwg"
    dwg.write_bytes(b"AC1032 synthetic")
    rs = RequirementSet(requirements=(_diameter("R1", "2F"),), units="mm")
    with pytest.raises(UnsupportedCapabilityError) as raised:
        await verify_artifact(str(dwg), rs, backend=backend)
    assert raised.value.capability == "dwg"


class _ComLike:
    name = "com"


@pytest.mark.asyncio
async def test_a_live_dwg_is_read_from_a_private_copy(tmp_path, monkeypatch):
    saved = tmp_path / "part.dwg"
    saved.write_bytes(b"AC1032 synthetic")
    fresh = tmp_path / "fresh.dxf"
    _write(fresh, [("bore", 50, 50, 20, False)])
    seen: dict = {}

    async def fake_take_snapshot(backend, path=None, *, block_defs=False):
        seen["path"] = Path(path)
        seen["bytes"] = Path(path).read_bytes()
        seen["block_defs"] = block_defs
        return read_snapshot(str(fresh), block_defs=block_defs)

    monkeypatch.setattr(artifact, "take_snapshot", fake_take_snapshot)
    snap, method = await artifact._fresh_snapshot(saved, _ComLike())
    assert seen["path"].name == "part.dwg"
    assert seen["path"].parent != saved.parent, "AutoCAD opens a copy, never the saved file"
    assert seen["bytes"] == b"AC1032 synthetic"
    assert seen["block_defs"] is True, "observe() needs the block definitions to explode INSERTs"
    assert not seen["path"].exists(), "the private copy is deleted after the read"
    assert method == "private copy, Documents.Open read-only + Document.Export"
    assert len(snap.records) == 2


@pytest.mark.asyncio
async def test_a_dwg_that_cannot_be_copied_is_a_named_refusal(tmp_path, monkeypatch):
    saved = tmp_path / "part.dwg"
    saved.write_bytes(b"AC1032 synthetic")

    def locked(src, dst):
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(artifact.shutil, "copy2", locked)
    with pytest.raises(ArtifactReadError, match="private copy"):
        await artifact._fresh_snapshot(saved, _ComLike())


@pytest.mark.asyncio
async def test_finalize_measures_the_saved_file_again(backend, tmp_path):
    base = tmp_path / "base.dxf"
    ezdxf.new("R2010", units=4).saveas(base)
    await backend.drawing_open(str(base))
    await ensure_standard_linetypes(backend)
    await ensure_engineering_layers(backend)
    await backend.entity_create_polyline(
        [[0, 0], [200, 0], [200, 100], [0, 100]], closed=True, layer="GEOMETRY"
    )
    bore = await backend.entity_create_circle(50, 50, 20, layer="GEOMETRY")  # Ø40
    await backend.entity_create_circle(150, 50, 5, layer="GEOMETRY")
    payload = await server.drawing_finalize(
        save_path=str(tmp_path / "part.dxf"),
        requirements=_raw_diameter("R1", bore.handle),
        ctx=_FakeCtx(backend),
    )
    reverified = payload["artifact_integrity"]["reverified"]
    assert (reverified["status"], reverified["accepted"]) == ("pass", True)
    assert reverified["comparison"] == [
        {
            "id": "R1",
            "before": {"status": "pass", "value": 40.0},
            "after": {"status": "pass", "value": 40.0},
            "changed": False,
        }
    ]


# -- review round: what a selector defines is resolved again on the file ----------


def _holes(path, extra=()) -> None:
    """A 200 x 100 frame and four r = 5 holes at x = 20 / 60 / 100 / 140, then ``extra``."""
    doc = ezdxf.new("R2010", units=4)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True)
    for x in (20, 60, 100, 140):
        msp.add_circle((x, 50), 5)
    for centre, radius in extra:
        msp.add_circle(centre, radius)
    doc.saveas(path)


def _raw_predicate(rid, quantity, target, **crit) -> dict:
    return {
        "units": "mm",
        "items": [
            {
                "id": rid,
                "quantity": quantity,
                "target": target,
                "features": [{"kind": "predicate", **crit}],
                "provenance": "explicit",
                "source": {"label": "4 x hole"},
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quantity", "target", "crit", "measured"),
    [
        ("count", {"exact": 4}, {"type": "circle", "radius_max": 6}, 5.0),
        ("absence", {}, {"type": "circle", "radius_min": 30}, 1.0),
    ],
)
async def test_a_count_is_counted_again_on_the_saved_file(
    backend, tmp_path, quantity, target, crit, measured
):
    # the file gained a fifth hole and an r = 35 circle; the document has neither
    _holes(tmp_path / "doc.dxf")
    _holes(tmp_path / "saved.dxf", [((180, 50), 5), ((100, 80), 35)])
    await backend.drawing_open(str(tmp_path / "doc.dxf"))
    report = await server.drawing_verify_requirements(
        requirements=_raw_predicate("N", quantity, target, **crit),
        artifact_path=str(tmp_path / "saved.dxf"),
        ctx=_FakeCtx(backend),
    )
    (row,) = report["rows"]
    assert (report["status"], report["accepted"]) == ("fail", False)
    assert row["measured"]["value"] == measured, "counted on the file, not the document"
    (rebound,) = report["rebinding"]["rows"]
    assert rebound["method"] == "fresh"
    assert "the source document counted" in rebound["reason"]
    (compared,) = report["comparison"]
    assert compared["before"]["status"] == "pass"
    assert compared["changed"] is True


@pytest.mark.asyncio
async def test_a_count_by_handle_on_a_renumbered_file_is_unknown(backend, tmp_path):
    source = _write(tmp_path / "a.dxf", [("bore", 50, 50, 20, False), ("pin", 150, 50, 5, False)])
    _write(tmp_path / "b.dxf", [("pin", 150, 50, 5, False), ("bore", 50, 50, 20, False)])
    await backend.drawing_open(str(tmp_path / "a.dxf"))
    rs = RequirementSet(
        requirements=(
            Requirement(
                id="C1",
                quantity="count",
                target=(("exact", 1),),
                features=(FeatureSelector(kind="handle", value=(("handle", source["bore"]),)),),
                provenance="explicit",
                source=(("document", "synthetic-sheet"),),
            ),
        ),
        units="mm",
    )
    report = await verify_artifact(str(tmp_path / "b.dxf"), rs, backend=backend)
    assert report["rebinding"]["handles_stable"] is False
    (row,) = report["rows"]
    assert row["status"] == "unknown"
    assert "renumbered handles" in report["rebinding"]["rows"][0]["reason"]
    assert report["accepted"] is False


@pytest.mark.asyncio
async def test_a_predicate_that_resolves_differently_on_the_file_is_unknown(backend, tmp_path):
    # one r = 35 circle in the document; the file adds a second inside the predicate
    _holes(tmp_path / "doc.dxf", [((100, 50), 35)])
    _holes(tmp_path / "saved.dxf", [((100, 50), 35), ((100, 50.5), 35)])
    await backend.drawing_open(str(tmp_path / "doc.dxf"))
    raw = _raw_predicate("R", "radius", {"nominal": 35, "unit": "mm"}, type="circle", radius_min=30)
    report = await server.drawing_verify_requirements(
        requirements=raw, artifact_path=str(tmp_path / "saved.dxf"), ctx=_FakeCtx(backend)
    )
    (row,) = report["rows"]
    assert row["status"] == "unknown"
    assert "disagrees with the source binding" in report["rebinding"]["rows"][0]["reason"]
    assert report["accepted"] is False


def _dimensioned(path, order, *, text="45", dimension=True) -> dict:
    """A frame, a bore r = 20 at (50, 50) with a Standard-style (DIMLFAC 1) diameter
    DIMENSION showing ``text`` and a pin r = 5, created in ``order``. Returns
    ``{name: handle}``."""
    doc = ezdxf.new("R2010", units=4, setup=True)
    msp = doc.modelspace()
    handles = {}
    for name in order:
        if name == "frame":
            frame = msp.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True)
            handles["frame"] = frame.dxf.handle
        elif name == "bore":
            handles["bore"] = msp.add_circle((50, 50), 20).dxf.handle
            if dimension:
                dim = msp.add_diameter_dim(
                    center=(50, 50), radius=20, angle=45, text=text, dimstyle="Standard"
                )
                dim.render()
                handles["dim"] = dim.dimension.dxf.handle
        else:
            handles["pin"] = msp.add_circle((150, 50), 5).dxf.handle
    doc.saveas(path)
    return handles


def _by_dimension(handle) -> RequirementSet:
    return RequirementSet(
        requirements=(
            Requirement(
                id="RD",
                quantity="diameter",
                target=(("nominal", 40.0), ("unit", "mm")),
                features=(FeatureSelector(kind="dimension", value=(("handle", handle),)),),
                provenance="explicit",
                source=(("document", "synthetic-sheet"),),
            ),
        ),
        units="mm",
    )


@pytest.mark.asyncio
async def test_a_dimension_mismatch_survives_a_save_that_renumbers_handles(backend, tmp_path):
    source = _dimensioned(tmp_path / "a.dxf", ["frame", "bore", "pin"])
    saved = _dimensioned(tmp_path / "b.dxf", ["pin", "frame", "bore"])
    assert saved["bore"] == source["dim"], "the save gave the DIMENSION's handle to the bore"
    await backend.drawing_open(str(tmp_path / "a.dxf"))
    rs = _by_dimension(source["dim"])
    on_document = await verify_requirements(backend, rs)
    assert [f["kind"] for f in on_document["findings"]] == ["dimension_mismatch"]
    report = await verify_artifact(str(tmp_path / "b.dxf"), rs, backend=backend)
    assert report["rebinding"]["handles_stable"] is False
    assert report["accepted"] is False
    assert [f["kind"] for f in report["findings"]] == ["dimension_mismatch"]
    (row,) = report["rows"]
    assert row["dimension"]["handle"] == saved["dim"]
    assert row["dimension"]["source_handle"] == source["dim"]
    assert row["dimension"]["displayed_text"] == "45"


@pytest.mark.asyncio
async def test_an_edited_dimension_text_is_judged_on_the_file(backend, tmp_path):
    # the file's DIMENSION was corrected to show its measurement: found by its
    # defpoints, judged clean on the file
    source = _dimensioned(tmp_path / "a.dxf", ["frame", "bore", "pin"])
    saved = _dimensioned(tmp_path / "b.dxf", ["pin", "frame", "bore"], text="<>")
    await backend.drawing_open(str(tmp_path / "a.dxf"))
    rs = _by_dimension(source["dim"])
    report = await verify_artifact(str(tmp_path / "b.dxf"), rs, backend=backend)
    (row,) = report["rows"]
    assert row["dimension"]["handle"] == saved["dim"]
    assert row["dimension"]["mismatch"] is False
    assert (report["status"], report["accepted"], report["findings"]) == ("pass", True, [])


@pytest.mark.asyncio
async def test_a_dimension_gone_from_the_file_blocks_acceptance(backend, tmp_path):
    source = _dimensioned(tmp_path / "a.dxf", ["frame", "bore", "pin"], text="<>")
    _dimensioned(tmp_path / "b.dxf", ["pin", "frame", "bore"], dimension=False)
    await backend.drawing_open(str(tmp_path / "a.dxf"))
    rs = _by_dimension(source["dim"])
    assert (await verify_requirements(backend, rs))["accepted"] is True
    report = await verify_artifact(str(tmp_path / "b.dxf"), rs, backend=backend)
    assert report["accepted"] is False
    assert [f["kind"] for f in report["findings"]] == ["dimension_unverified"]
    (row,) = report["rows"]
    assert row["dimension"]["status"] == "unknown"


# -- review minors 14 / 15: what the handle and tag stages accept, which document --


def _lined(path, circles) -> dict:
    """A frame and four lines (created first, so their handles stay put), then
    ``circles`` as in ``_write``."""
    doc = ezdxf.new("R2010", units=4)
    msp = doc.modelspace()
    handles = {
        "frame": msp.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True).dxf.handle
    }
    for k in range(4):
        msp.add_line((10, 10 + 20 * k), (190, 10 + 20 * k))
    for name, cx, cy, radius in circles:
        handles[name] = msp.add_circle((cx, cy), radius).dxf.handle
    doc.saveas(path)
    return handles


@pytest.mark.asyncio
async def test_a_partial_renumbering_never_measures_the_feature_at_the_old_handle(
    backend, tmp_path
):
    # frame and lines keep their handles (handles stable), but the save gave the
    # bore's handle to an untagged pin of the same type: the bore is re-bound by
    # its geometry, never measured as the pin
    source = _lined(tmp_path / "a.dxf", [("bore", 50, 50, 20), ("pin", 150, 50, 5)])
    saved = _lined(tmp_path / "b.dxf", [("pin", 150, 50, 5), ("bore", 50, 50, 20)])
    assert saved["pin"] == source["bore"]
    await backend.drawing_open(str(tmp_path / "a.dxf"))
    rs = RequirementSet(requirements=(_diameter("R1", source["bore"]),), units="mm")
    report = await verify_artifact(str(tmp_path / "b.dxf"), rs, backend=backend)
    assert report["rebinding"]["handles_stable"] is True
    (row,) = report["rows"]
    assert row["status"] == "pass"
    assert abs(row["measured"]["value"] - 40.0) <= 1e-9
    assert row["handles"] == [saved["bore"]]
    assert [item["method"] for item in report["rebinding"]["rows"]] == ["geometry"]


@pytest.mark.asyncio
async def test_a_feature_edited_in_place_is_still_measured_at_its_handle(backend, tmp_path):
    source = _lined(tmp_path / "a.dxf", [("bore", 50, 50, 20), ("pin", 150, 50, 5)])
    saved = _lined(tmp_path / "b.dxf", [("bore", 50, 50, 21), ("pin", 150, 50, 5)])
    assert saved["bore"] == source["bore"]
    await backend.drawing_open(str(tmp_path / "a.dxf"))
    rs = RequirementSet(requirements=(_diameter("R1", source["bore"]),), units="mm")
    report = await verify_artifact(str(tmp_path / "b.dxf"), rs, backend=backend)
    (row,) = report["rows"]
    assert row["status"] == "fail"
    assert abs(row["measured"]["value"] - 42.0) <= 1e-9
    assert [item["method"] for item in report["rebinding"]["rows"]] == ["handle"]


@pytest.mark.asyncio
async def test_the_tag_stage_keeps_to_the_source_space(backend, tmp_path):
    # the file renumbered handles and carries the bore's tag a second time on
    # Layout1: the model-space bore is still the only candidate in its space
    source = _write(tmp_path / "a.dxf", [("bore", 50, 50, 20, True), ("pin", 150, 80, 5, False)])
    doc = ezdxf.new("R2010", units=4)
    doc.appids.add("ACADMCP_MECH")
    msp = doc.modelspace()
    msp.add_circle((150, 80), 5)
    msp.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True)
    rows = [row for row in encode(TAG, kinds=("feature",)) if row[0] != 1001]
    bore = msp.add_circle((50, 50), 20)
    bore.set_xdata("ACADMCP_MECH", rows)
    paper = doc.layouts.get("Layout1").add_circle((30, 30), 12)
    paper.set_xdata("ACADMCP_MECH", rows)
    doc.saveas(tmp_path / "b.dxf")
    await backend.drawing_open(str(tmp_path / "a.dxf"))
    rs = RequirementSet(requirements=(_diameter("R1", source["bore"]),), units="mm")
    report = await verify_artifact(str(tmp_path / "b.dxf"), rs, backend=backend)
    assert report["rebinding"]["handles_stable"] is False
    (row,) = report["rows"]
    assert row["status"] == "pass"
    assert row["handles"] == [bore.dxf.handle]
    assert [item["method"] for item in report["rebinding"]["rows"]] == ["tag"]


@pytest.mark.asyncio
async def test_a_source_binding_without_a_handle_is_never_reported_as_re_bound(backend, tmp_path):
    import dataclasses

    from engineering.verification.report import measure_requirements
    from engineering.verification.types import Binding

    source = _write(tmp_path / "a.dxf", [("bore", 50, 50, 20, False), ("pin", 150, 50, 5, False)])
    await backend.drawing_open(str(tmp_path / "a.dxf"))
    rs = RequirementSet(requirements=(_diameter("R1", source["bore"]),), units="mm")
    run = await measure_requirements(backend, rs)
    hollow = Binding(requirement_id="R1", status="bound", refs=(), candidates=(), reason="x")
    run = dataclasses.replace(run, bindings=(hollow,))
    _bindings, rows, _stable = artifact.rebind(rs, run, run.snapshot, run.observed)
    (rebound,) = rows
    assert rebound["method"] == "fresh"
    assert "names no feature handle" in rebound["reason"]
    assert not rebound["reason"].rstrip().endswith(" by")


def _unrelated(path) -> None:
    """Another drawing altogether: three lines, nothing in common with ``_write``'s."""
    doc = ezdxf.new("R2010", units=4)
    msp = doc.modelspace()
    for k in range(3):
        msp.add_line((0, 300 + 10 * k), (80, 300 + 10 * k))
    doc.saveas(path)


@pytest.mark.asyncio
async def test_an_artifact_of_another_drawing_is_never_accepted(backend, tmp_path):
    # before the fix the open document could not bind the handle, so it was
    # "bound afresh on the file" - and the unrelated artifact passed
    source = _write(tmp_path / "a.dxf", [("bore", 50, 50, 20, False), ("pin", 150, 50, 5, False)])
    _unrelated(tmp_path / "other.dxf")
    await backend.drawing_open(str(tmp_path / "other.dxf"))
    rs = RequirementSet(requirements=(_diameter("R1", source["bore"]),), units="mm")
    report = await verify_artifact(str(tmp_path / "a.dxf"), rs, backend=backend)
    assert report["provenance"]["source_matches"] is False
    assert report["provenance"]["matched"] == 0
    assert report["accepted"] is False
    assert "artifact_source_mismatch" in [f["kind"] for f in report["findings"]]
    (row,) = report["rows"]
    assert row["status"] == "unknown", "a handle of another drawing names nothing here"
    assert report["comparison"] is None
    assert report["source_document"]["path"] == str(tmp_path / "other.dxf")


@pytest.mark.asyncio
async def test_a_handle_free_requirement_on_another_drawing_is_measured_but_not_accepted(
    backend, tmp_path
):
    _holes(tmp_path / "saved.dxf")
    _unrelated(tmp_path / "other.dxf")
    await backend.drawing_open(str(tmp_path / "other.dxf"))
    raw = _raw_predicate("N", "count", {"exact": 4}, type="circle", radius_max=6)
    report = await server.drawing_verify_requirements(
        requirements=raw, artifact_path=str(tmp_path / "saved.dxf"), ctx=_FakeCtx(backend)
    )
    (row,) = report["rows"]
    assert (row["status"], row["measured"]["value"]) == ("pass", 4.0)
    assert report["status"] == "pass"
    assert report["accepted"] is False
    assert any(item.startswith("artifact_source_mismatch") for item in report["blocking"])


@pytest.mark.asyncio
async def test_the_artifact_of_the_open_document_matches_its_source(backend, tmp_path):
    source = _write(tmp_path / "a.dxf", [("bore", 50, 50, 20, False), ("pin", 150, 50, 5, False)])
    await backend.drawing_open(str(tmp_path / "a.dxf"))
    rs = RequirementSet(requirements=(_diameter("R1", source["bore"]),), units="mm")
    report = await verify_artifact(str(tmp_path / "a.dxf"), rs, backend=backend)
    provenance = report["provenance"]
    assert provenance["source_matches"] is True
    assert (provenance["matched"], provenance["source_entities"]) == (3, 3)
    assert provenance["artifact_entities"] == 3
    assert report["accepted"] is True
    # without the source document nothing is claimed about it
    alone = await verify_artifact(str(tmp_path / "a.dxf"), rs)
    assert alone["provenance"] is None
