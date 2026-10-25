"""The change-preview contract: exact fingerprints, region fingerprints, refusals.

Spec section 5, Task 12 (B14 residue). A precondition is an EXACT fingerprint:
the sha256 of the entity's exported DXF tags, read the same way at preview and
at apply. ``engineering/understand/diff.py::signature`` is the counter-example
every test here leans on: it rounds to 0.01 and leaves out colour, linetype,
XDATA, ellipse axes, dimension values and MTEXT formatting, so each edit below
leaves the signature equal and must still move the fingerprint.

Expected fingerprints are recomputed in this file from ezdxf's own
``TagCollector`` (``_oracle``), never read back from the module under test.
"""

from __future__ import annotations

import hashlib
import os
import tempfile

import ezdxf
import pytest
from ezdxf.lldxf.tagwriter import TagCollector

from engineering.understand.diff import signature
from engineering.understand.snapshot import records_from_doc
from engineering.validation import SpecError

pytestmark = pytest.mark.asyncio


def _oracle(entity, dxfversion: str) -> str:
    """sha256 over the exported tags, written independently of the module."""
    collector = TagCollector(dxfversion=dxfversion)
    entity.export_dxf(collector)
    digest = hashlib.sha256()
    for tag in collector.tags:
        digest.update(f"{tag[0]}\x1f{tag[1]!r}\x1e".encode())
    return digest.hexdigest()


def _signature_of(doc, handle: str) -> tuple:
    snap = records_from_doc(doc, source="test")
    (record,) = [r for r in snap.records if r.handle == handle]
    return signature(record)


# -- exact entity fingerprints ----------------------------------------------------


async def test_an_untouched_entity_reads_the_same_fingerprint_twice(backend):
    from engineering.changes.preview import entity_fingerprint

    line = await backend.entity_create_line(0, 0, 10, 0)

    first = await entity_fingerprint(backend, line.handle)
    second = await entity_fingerprint(backend, line.handle.lower())

    raw = backend._doc.entitydb[line.handle]
    assert first == second == _oracle(raw, backend._doc.dxfversion)
    assert len(first) == 64


def _edit_colour(doc, entity):
    entity.dxf.color = 1


def _edit_linetype(doc, entity):
    entity.dxf.linetype = "DASHED"


def _edit_xdata(doc, entity):
    if "PREVIEWTEST" not in doc.appids:
        doc.appids.new("PREVIEWTEST")
    entity.set_xdata("PREVIEWTEST", [(1000, "edited")])


def _edit_far_digit(doc, entity):
    end = entity.dxf.end
    entity.dxf.end = (end.x + 1e-12, end.y, end.z)


@pytest.mark.parametrize(
    "edit",
    [_edit_colour, _edit_linetype, _edit_xdata, _edit_far_digit],
    ids=["colour", "linetype", "xdata", "1e-12 coordinate"],
)
async def test_an_edit_the_signature_cannot_see_moves_a_line_fingerprint(backend, edit):
    from engineering.changes.preview import entity_fingerprint

    line = await backend.entity_create_line(0, 0, 10, 0)
    doc = backend._doc
    raw = doc.entitydb[line.handle]
    before_sig = _signature_of(doc, line.handle)
    before = await entity_fingerprint(backend, line.handle)

    edit(doc, raw)

    assert _signature_of(doc, line.handle) == before_sig, "diff.signature misses this edit"
    after = await entity_fingerprint(backend, line.handle)
    assert after != before
    assert after == _oracle(raw, doc.dxfversion)


async def test_an_ellipse_axis_ratio_moves_the_fingerprint(backend):
    from engineering.changes.preview import entity_fingerprint

    doc = backend._doc
    ellipse = doc.modelspace().add_ellipse((0, 0), major_axis=(10, 0), ratio=0.5)
    before_sig = _signature_of(doc, ellipse.dxf.handle)
    before = await entity_fingerprint(backend, ellipse.dxf.handle)

    ellipse.dxf.ratio = 0.5000001

    assert _signature_of(doc, ellipse.dxf.handle) == before_sig
    assert await entity_fingerprint(backend, ellipse.dxf.handle) != before


async def test_a_dimension_measurement_moves_the_fingerprint(backend):
    from engineering.changes.preview import entity_fingerprint

    doc = backend._doc
    builder = doc.modelspace().add_linear_dim(base=(0, 5), p1=(0, 0), p2=(40, 0))
    builder.render()
    dimension = builder.dimension
    before_sig = _signature_of(doc, dimension.dxf.handle)
    before = await entity_fingerprint(backend, dimension.dxf.handle)

    dimension.dxf.actual_measurement = 42.0

    assert _signature_of(doc, dimension.dxf.handle) == before_sig
    assert await entity_fingerprint(backend, dimension.dxf.handle) != before


async def test_mtext_formatting_moves_the_fingerprint(backend):
    from engineering.changes.preview import entity_fingerprint

    doc = backend._doc
    mtext = doc.modelspace().add_mtext("OLD")
    before_sig = _signature_of(doc, mtext.dxf.handle)
    before = await entity_fingerprint(backend, mtext.dxf.handle)

    mtext.text = "{\\LOLD}"

    assert _signature_of(doc, mtext.dxf.handle) == before_sig, "plain text is still OLD"
    assert await entity_fingerprint(backend, mtext.dxf.handle) != before


async def test_an_attribute_value_moves_its_reference_fingerprint(backend):
    from engineering.changes.preview import entity_fingerprint

    doc = backend._doc
    doc.blocks.new("TAGGED").add_attdef("TAG", (0, 0), text="")
    ref = doc.modelspace().add_blockref("TAGGED", (0, 0))
    ref.add_auto_attribs({"TAG": "P-101"})
    before = await entity_fingerprint(backend, ref.dxf.handle)

    ref.attribs[0].dxf.text = "P-102"

    assert await entity_fingerprint(backend, ref.dxf.handle) != before


async def test_a_handle_the_drawing_does_not_hold_reads_missing(backend):
    from engineering.changes.preview import MISSING, entity_fingerprint, entity_fingerprints

    line = await backend.entity_create_line(0, 0, 10, 0)
    await backend.entity_delete(line.handle)

    assert await entity_fingerprint(backend, line.handle) == MISSING
    assert await entity_fingerprints(backend, ["FFFFF"]) == {"FFFFF": MISSING}
    assert await entity_fingerprints(backend, []) == {}


# -- region fingerprints ----------------------------------------------------------

REGION = (40.0, -5.0, 60.0, 5.0)


async def test_a_region_fingerprint_moves_only_for_entities_that_meet_it(backend):
    from engineering.changes.preview import region_fingerprint

    await backend.entity_create_circle(50, 0, 2)  # box (48, -2, 52, 2): inside
    first = await region_fingerprint(backend, REGION)

    await backend.entity_create_circle(500, 500, 1)  # box (499, 499, 501, 501): outside
    assert await region_fingerprint(backend, REGION) == first

    await backend.entity_create_circle(62, 0, 2)  # box x0 = 60.0 = the region's x1
    touching = await region_fingerprint(backend, REGION)
    assert touching != first, "closed intervals: a box touching the edge meets the region"

    await backend.entity_create_circle(62.5, 0, 2)  # box x0 = 60.5 > 60.0
    assert await region_fingerprint(backend, REGION) == touching


async def test_a_region_fingerprint_is_bound_to_the_current_layout(backend):
    from engineering.changes.preview import region_fingerprint

    model = await region_fingerprint(backend, REGION)
    await backend.layout_set_current("Layout1")

    assert await region_fingerprint(backend, REGION) != model


async def test_a_malformed_region_is_refused_by_path(backend):
    from engineering.changes.preview import region_fingerprint

    with pytest.raises(SpecError, match=r"^bbox: x0 <= x1"):
        await region_fingerprint(backend, (10.0, 0.0, 0.0, 1.0))
    with pytest.raises(SpecError, match=r"^bbox\[2\]: must be finite"):
        await region_fingerprint(backend, (0.0, 0.0, float("nan"), 1.0))


# -- the preview ------------------------------------------------------------------


async def test_a_preview_carries_identity_preconditions_and_plan(backend):
    from engineering.changes.preview import make_preview

    line = await backend.entity_create_line(0, 0, 10, 0)
    await backend.entity_create_circle(50, 0, 2)

    preview = await make_preview(
        backend,
        handles=[line.handle],
        region=REGION,
        planned=[{"i": 0, "tool": "entity_move", "args": {"handle": line.handle, "dx": 5}}],
    )
    payload = preview.to_dict()

    identity = backend.document_identity()
    assert payload["document_id"] == identity["document_id"]
    assert payload["revision"] == identity["revision"]
    raw = backend._doc.entitydb[line.handle]
    assert payload["preconditions"][0] == {
        "handle": line.handle,
        "fingerprint": _oracle(raw, backend._doc.dxfversion),
    }
    assert payload["preconditions"][1]["region"] == list(REGION)
    assert payload["planned"] == [
        {"i": 0, "tool": "entity_move", "args": {"handle": line.handle, "dx": 5}}
    ]
    assert hash(preview) == hash(preview), "a frozen, hashable value"


# -- check_preconditions ----------------------------------------------------------


async def test_unchanged_preconditions_pass(backend):
    from engineering.changes.preview import check_preconditions, make_preview

    line = await backend.entity_create_line(0, 0, 10, 0)
    payload = (await make_preview(backend, handles=[line.handle], region=REGION)).to_dict()

    await check_preconditions(
        backend, expected_revision=payload["revision"], preconditions=payload["preconditions"]
    )


async def test_an_edit_through_the_server_is_a_stale_revision(backend):
    from engineering.changes.preview import StaleRevisionError, check_preconditions, make_preview

    line = await backend.entity_create_line(0, 0, 10, 0)
    payload = (await make_preview(backend, handles=[line.handle])).to_dict()

    await backend.entity_set_properties(line.handle, color=1)

    with pytest.raises(StaleRevisionError) as caught:
        await check_preconditions(
            backend, expected_revision=payload["revision"], preconditions=payload["preconditions"]
        )
    refusal = caught.value.to_dict()
    assert refusal["ok"] is False and refusal["kind"] == "stale_revision"
    assert refusal["expected_revision"] == payload["revision"]
    assert refusal["actual_revision"] == backend.document_identity()["revision"]
    assert refusal["actual_revision"] != payload["revision"]
    assert [row["handle"] for row in refusal["changed"]] == [line.handle]
    assert "nothing was changed" in refusal["error"]


async def test_a_direct_edit_in_the_document_is_caught_by_the_fingerprint(backend):
    """An edit that never passes through the server leaves the revision where
    it was; the exact fingerprint is what refuses the apply."""
    from engineering.changes.preview import StaleRevisionError, check_preconditions, make_preview

    line = await backend.entity_create_line(0, 0, 10, 0)
    payload = (await make_preview(backend, handles=[line.handle])).to_dict()

    backend._doc.entitydb[line.handle].dxf.color = 5

    with pytest.raises(StaleRevisionError) as caught:
        await check_preconditions(
            backend, expected_revision=payload["revision"], preconditions=payload["preconditions"]
        )
    refusal = caught.value.to_dict()
    assert refusal["expected_revision"] == refusal["actual_revision"] == payload["revision"]
    (row,) = refusal["changed"]
    assert row["handle"] == line.handle
    assert row["expected"] == payload["preconditions"][0]["fingerprint"]
    assert row["actual"] == _oracle(backend._doc.entitydb[line.handle], backend._doc.dxfversion)


async def test_an_entity_added_inside_a_region_is_stale(backend):
    from engineering.changes.preview import StaleRevisionError, check_preconditions, make_preview

    payload = (await make_preview(backend, region=REGION)).to_dict()
    backend._doc.modelspace().add_circle((45, 0), 1)

    with pytest.raises(StaleRevisionError) as caught:
        await check_preconditions(backend, preconditions=payload["preconditions"])
    assert caught.value.changed[0]["region"] == list(REGION)


@pytest.mark.parametrize(
    "raw, message",
    [
        ("x", r"^preconditions: expected a list"),
        ([1], r"^preconditions\[0\]: expected an object"),
        ([{"fingerprint": "missing"}], r"^preconditions\[0\]: carries exactly one"),
        ([{"handle": "1A", "fingerprint": "abc"}], r"^preconditions\[0\]\.fingerprint"),
        ([{"handle": "1A", "fingerprint": "missing", "x": 1}], r"^preconditions\[0\]: unknown"),
        ([{"region": [1, 0, 0, 1], "fingerprint": "missing"}], r"^preconditions\[0\]\.region"),
    ],
)
async def test_a_malformed_precondition_is_refused_by_path(backend, raw, message):
    from engineering.changes.preview import check_preconditions

    with pytest.raises(SpecError, match=message):
        await check_preconditions(backend, preconditions=raw)


@pytest.mark.parametrize("bad", [-1, True, 1.5, "3"])
async def test_a_malformed_expected_revision_is_refused(backend, bad):
    from engineering.changes.preview import check_preconditions

    with pytest.raises(SpecError, match=r"^expected_revision:"):
        await check_preconditions(backend, expected_revision=bad)


async def test_a_backend_without_a_revision_cannot_vouch_for_one(backend, monkeypatch):
    """No silent pass: a revision nobody can read is not the expected one."""
    from engineering.changes.preview import StaleRevisionError, check_preconditions

    monkeypatch.setattr(
        backend, "document_identity", lambda: {"document_id": None, "revision": None}
    )
    with pytest.raises(StaleRevisionError):
        await check_preconditions(backend, expected_revision=0)


# -- interleaving -----------------------------------------------------------------


async def test_the_watch_reports_a_change_between_two_of_our_calls(backend):
    from engineering.changes.preview import RevisionWatch

    watch = await RevisionWatch.begin(backend)
    start = backend.document_identity()["revision"]

    assert await watch.before("step 0") is False
    await backend.entity_create_line(0, 0, 1, 0)  # our step
    await watch.after()
    ours = backend.document_identity()["revision"]
    await backend.entity_create_line(0, 1, 1, 1)  # somebody else's call
    interleaved = backend.document_identity()["revision"]

    assert await watch.before("step 1") is True
    assert watch.report() == {
        "checked": start,
        "after": interleaved,
        "interleaved": [{"before": "step 1", "expected": ours, "actual": interleaved}],
    }


# -- the live engine reads its own export ---------------------------------------------


class _LiveBackend:
    """The surface the preview module touches on ComBackend: ``name``,
    ``_run(func, *args)``, ``layout_list()``, ``document_identity()``."""

    name = "com"

    def __init__(self):
        self.ran: list[str] = []

    async def _run(self, func, *args, **kwargs):
        self.ran.append(getattr(func, "__name__", str(func)))
        return func(*args, **kwargs)

    async def layout_list(self):
        return {"ok": True, "layouts": ["Model", "Layout1"], "current": "Model"}

    def document_identity(self):
        return {"document_id": "f" * 32, "revision": 7, "name": "A.dwg", "path": None}


async def test_the_live_engine_fingerprints_its_dxf_export(monkeypatch, tmp_path):
    from engineering.changes import preview

    doc = ezdxf.new("R2018")
    handle = doc.modelspace().add_line((0, 0), (10, 0)).dxf.handle
    folders: list[str] = []

    def _fake_export(backend, path):
        assert path is None, "the active (bound) document, never a file"
        folder = tempfile.mkdtemp(prefix="acadmcp_snap_", dir=str(tmp_path))
        folders.append(folder)
        written = os.path.join(folder, "snap.DXF")
        doc.saveas(written)
        return written, folder, "live:A.dwg"

    monkeypatch.setattr(preview, "_com_export", _fake_export)
    live = _LiveBackend()

    found = await preview.entity_fingerprints(live, [handle])

    assert live.ran == ["_fake_export"]
    assert not os.path.exists(folders[0]), "the export is deleted once read"
    exported = tmp_path / "oracle.dxf"
    doc.saveas(exported)
    oracle = ezdxf.readfile(exported)
    assert found == {handle: _oracle(oracle.entitydb[handle], oracle.dxfversion)}
    payload = (await preview.make_preview(live, handles=[handle])).to_dict()
    assert payload["revision"] == 7 and payload["document_id"] == "f" * 32
