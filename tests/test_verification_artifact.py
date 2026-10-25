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
