"""Previews and guarded applies on the four tools (spec section 5, Task 12).

``cad_batch``, ``drawing_refine``, ``text_find_replace`` and ``pid_from_spec``
return a ``preview`` on ``dry_run=True`` and, given that preview's ``revision``
(``expected_revision``) and/or ``preconditions``, apply only if nothing moved:
otherwise the call is refused with kind ``stale_revision`` and nothing changes.

Review Focus 5: an entity edited between preview and apply - through the
server (the revision moves) and directly in the document (only the exact
fingerprint moves). Both are refused, and the drawing is left as it was.

Expected fingerprints are recomputed here from ezdxf's ``TagCollector``
(``_oracle``); the routing region is computed by hand from the catalogue box of
the ``dcs`` instrument, (-5, -5, 5, 5).
"""

from __future__ import annotations

import hashlib

import pytest
import pytest_asyncio
from ezdxf.lldxf.tagwriter import TagCollector
from fastmcp import Client, Context

import server
from backends.ezdxf_backend import EzdxfBackend
from engineering.validation import SpecError

pytestmark = pytest.mark.asyncio


def _oracle(entity, dxfversion: str) -> str:
    collector = TagCollector(dxfversion=dxfversion)
    entity.export_dxf(collector)
    digest = hashlib.sha256()
    for tag in collector.tags:
        digest.update(f"{tag[0]}\x1f{tag[1]!r}\x1e".encode())
    return digest.hexdigest()


def _fingerprint(backend, handle: str) -> str:
    doc = backend._doc
    return _oracle(doc.entitydb[handle], doc.dxfversion)


def _revision(backend) -> int:
    return backend.document_identity()["revision"]


@pytest_asyncio.fixture
async def served(monkeypatch):
    """An in-memory client whose lifespan serves THIS headless backend, so a test
    can edit the document directly, the way an operator or a second program would."""
    backend = EzdxfBackend()
    await backend.connect()
    await backend.drawing_new()

    async def _make():
        return backend

    monkeypatch.setattr(server, "_make_backend", _make)
    async with Client(server.mcp) as client:
        yield client, backend


async def _payload(client, tool: str, args: dict) -> dict:
    return (await client.call_tool(tool, args)).structured_content


async def _refused(client, tool: str, args: dict) -> dict:
    result = await client.call_tool(tool, args, raise_on_error=False)
    assert result.is_error is True, "a stale apply is an error result, never a quiet pass"
    payload = result.structured_content
    assert payload["ok"] is False and payload["kind"] == "stale_revision"
    assert payload["tool"] == tool
    return payload


def _move(handle: str, dx: float = 5.0) -> dict:
    return {"tool": "entity_move", "args": {"handle": handle, "dx": dx, "dy": 0.0}}


def _start(backend, handle: str) -> tuple[float, float]:
    start = backend._doc.entitydb[handle].dxf.start
    return (start.x, start.y)


# -- cad_batch ----------------------------------------------------------------------


async def test_a_batch_dry_run_fingerprints_only_the_literal_handles(served):
    client, backend = served
    line = await backend.entity_create_line(0, 0, 10, 0)
    circle = await backend.entity_create_circle(50, 50, 5)
    steps = [
        _move(line.handle),
        {"tool": "entity_create_line", "args": {"x1": 0, "y1": 9, "x2": 9, "y2": 9}, "bind": "new"},
        {"tool": "entity_move", "args": {"handle": "$new", "dx": 1, "dy": 0}},
        {
            "tool": "point_intersection",
            "args": {"handle1": line.handle.lower(), "handle2": circle.handle},
        },
    ]

    result = await _payload(client, "cad_batch", {"steps": steps, "dry_run": True})

    preview = result["preview"]
    assert result["executed"] == 0
    assert preview["revision"] == _revision(backend)
    assert preview["document_id"] == backend.document_identity()["document_id"]
    assert preview["preconditions"] == [
        {"handle": h, "fingerprint": _fingerprint(backend, h)}
        for h in sorted([line.handle, circle.handle])
    ], "a $reference and an entity a step creates have no precondition"
    assert [row["i"] for row in preview["planned"]] == [0, 1, 2, 3]


async def test_a_batch_applies_its_preview_when_nothing_moved(served):
    client, backend = served
    line = await backend.entity_create_line(0, 0, 10, 0)
    steps = [_move(line.handle)]
    preview = (await _payload(client, "cad_batch", {"steps": steps, "dry_run": True}))["preview"]

    result = await _payload(
        client,
        "cad_batch",
        {
            "steps": steps,
            "expected_revision": preview["revision"],
            "preconditions": preview["preconditions"],
        },
    )

    assert result["ok"] is True and result["succeeded"] == 1
    assert _start(backend, line.handle) == (5.0, 0.0)
    assert result["revision"]["checked"] == preview["revision"]
    assert result["revision"]["after"] == _revision(backend)
    assert result["revision"]["interleaved"] == []


async def test_review_focus_5_a_server_edit_refuses_the_batch(served):
    client, backend = served
    line = await backend.entity_create_line(0, 0, 10, 0)
    steps = [_move(line.handle)]
    preview = (await _payload(client, "cad_batch", {"steps": steps, "dry_run": True}))["preview"]

    await client.call_tool("entity_set_properties", {"handle": line.handle, "color": 1})
    revision = _revision(backend)

    payload = await _refused(
        client,
        "cad_batch",
        {
            "steps": steps,
            "expected_revision": preview["revision"],
            "preconditions": preview["preconditions"],
        },
    )

    assert payload["expected_revision"] == preview["revision"]
    assert payload["actual_revision"] == revision != preview["revision"]
    assert [row["handle"] for row in payload["changed"]] == [line.handle]
    assert _start(backend, line.handle) == (0.0, 0.0), "no step ran"
    assert _revision(backend) == revision, "the refusal changed nothing"


async def test_review_focus_5_a_direct_edit_refuses_the_batch(served):
    """The revision cannot see an edit that bypassed the server; the exact
    fingerprint does. A rollback batch is refused before its checkpoint opens."""
    client, backend = served
    line = await backend.entity_create_line(0, 0, 10, 0)
    steps = [_move(line.handle)]
    preview = (await _payload(client, "cad_batch", {"steps": steps, "dry_run": True}))["preview"]

    backend._doc.entitydb[line.handle].dxf.color = 5

    payload = await _refused(
        client,
        "cad_batch",
        {
            "steps": steps,
            "on_error": "rollback",
            "expected_revision": preview["revision"],
            "preconditions": preview["preconditions"],
        },
    )

    assert payload["expected_revision"] == payload["actual_revision"] == preview["revision"]
    (row,) = payload["changed"]
    assert row == {
        "handle": line.handle,
        "expected": preview["preconditions"][0]["fingerprint"],
        "actual": _fingerprint(backend, line.handle),
    }
    assert _start(backend, line.handle) == (0.0, 0.0)
    assert (await backend.system_status())["transaction_depth"] == 0, "no checkpoint opened"


async def test_a_change_between_two_steps_is_reported_as_interleaved(served, monkeypatch):
    """Each step is its own locked call. ``cad_batch`` awaits
    ``ctx.report_progress`` right before each step, so a call that lands there
    is exactly another client's change between two of ours."""
    client, backend = served
    steps = [
        {"tool": "entity_create_line", "args": {"x1": 0, "y1": 0, "x2": 1, "y2": 0}},
        {"tool": "entity_create_line", "args": {"x1": 0, "y1": 1, "x2": 1, "y2": 1}},
    ]
    preview = (await _payload(client, "cad_batch", {"steps": steps, "dry_run": True}))["preview"]
    original = Context.report_progress

    async def _between(self, progress, total=None, message=None):
        if progress == 1:
            await backend.entity_create_circle(500, 500, 1)  # somebody else's call
        return await original(self, progress, total, message)

    monkeypatch.setattr(Context, "report_progress", _between)

    result = await _payload(
        client, "cad_batch", {"steps": steps, "expected_revision": preview["revision"]}
    )

    assert result["ok"] is True and result["succeeded"] == 2
    (row,) = result["revision"]["interleaved"]
    assert row["before"] == "step 1 (entity_create_line)"
    assert row["actual"] > row["expected"]
    assert result["revision"]["after"] == _revision(backend)


# -- drawing_refine -----------------------------------------------------------------


async def test_a_refine_dry_run_previews_even_when_the_threshold_is_met(backend):
    """It returned ``threshold_met`` before the dry-run branch, so a clean
    drawing never produced a preview to apply by."""
    from engineering.refiner import refine_drawing

    await backend.entity_create_line(0, 0, 20, 0)

    payload = (await refine_drawing(backend, dry_run=True)).to_dict()

    assert payload["status"] == "dry_run"
    assert payload["threshold_met"] is True
    assert payload["preview"]["revision"] == _revision(backend)


async def test_a_refine_preview_fingerprints_every_handle_a_repair_touches(backend):
    from engineering.refiner import refine_drawing

    first = await backend.entity_create_line(0, 0, 20, 0)
    second = await backend.entity_create_line(0, 0, 20, 0)

    payload = (await refine_drawing(backend, focus=["duplicate_entities"], dry_run=True)).to_dict()

    assert payload["threshold_met"] is False
    assert payload["preview"]["preconditions"] == [
        {"handle": h, "fingerprint": _fingerprint(backend, h)}
        for h in sorted([first.handle, second.handle])
    ]
    (planned,) = payload["preview"]["planned"]
    assert planned["focus"] == "duplicate_entities" and planned["status"] == "planned"


async def test_review_focus_5_refine_refuses_a_stale_preview(served):
    client, backend = served
    first = await backend.entity_create_line(0, 0, 20, 0)
    await backend.entity_create_line(0, 0, 20, 0)
    dry = await _payload(
        client, "drawing_refine", {"focus": ["duplicate_entities"], "dry_run": True}
    )

    backend._doc.entitydb[first.handle].dxf.color = 2

    payload = await _refused(
        client,
        "drawing_refine",
        {
            "focus": ["duplicate_entities"],
            "expected_revision": dry["preview"]["revision"],
            "preconditions": dry["preview"]["preconditions"],
        },
    )

    assert [row["handle"] for row in payload["changed"]] == [first.handle]
    assert len(await backend.entity_list(type_filter="LINE")) == 2, "nothing was repaired"


async def test_a_guarded_refine_repairs_and_reports_its_revision(backend):
    from engineering.refiner import refine_drawing

    await backend.entity_create_line(0, 0, 20, 0)
    await backend.entity_create_line(0, 0, 20, 0)
    dry = (await refine_drawing(backend, focus=["duplicate_entities"], dry_run=True)).to_dict()

    result = await refine_drawing(
        backend,
        focus=["duplicate_entities"],
        expected_revision=dry["preview"]["revision"],
        preconditions=dry["preview"]["preconditions"],
    )

    assert result.status == "threshold_met"
    assert len(await backend.entity_list(type_filter="LINE")) == 1
    assert result.revision["checked"] == dry["preview"]["revision"]
    assert result.revision["interleaved"] == []


# -- text_find_replace --------------------------------------------------------------


async def test_a_find_replace_dry_run_previews_its_rows(served):
    client, backend = served
    text = await backend.entity_create_text("OLD one", 0, 0)
    mtext = await backend.entity_create_mtext("OLD two", 0, 10)

    result = await _payload(
        client, "text_find_replace", {"find": "OLD", "replace": "NEW", "dry_run": True}
    )

    preview = result["preview"]
    assert preview["revision"] == _revision(backend)
    assert preview["preconditions"] == [
        {"handle": h, "fingerprint": _fingerprint(backend, h)} for h in (text.handle, mtext.handle)
    ]
    assert preview["planned"] == result["entities"]
    assert backend._doc.entitydb[text.handle].dxf.text == "OLD one"


async def test_review_focus_5_find_replace_refuses_after_a_server_edit(served):
    client, backend = served
    text = await backend.entity_create_text("OLD", 0, 0)
    dry = await _payload(
        client, "text_find_replace", {"find": "OLD", "replace": "NEW", "dry_run": True}
    )
    await client.call_tool("entity_set_properties", {"handle": text.handle, "color": 4})

    await _refused(
        client,
        "text_find_replace",
        {
            "find": "OLD",
            "replace": "NEW",
            "expected_revision": dry["preview"]["revision"],
            "preconditions": dry["preview"]["preconditions"],
        },
    )

    assert backend._doc.entitydb[text.handle].dxf.text == "OLD"


async def test_a_new_match_the_preview_did_not_list_refuses_the_apply(served):
    """A precondition vouches only for the entities it names; a text added since
    the preview would otherwise be rewritten unseen."""
    client, backend = served
    text = await backend.entity_create_text("OLD", 0, 0)
    dry = await _payload(
        client, "text_find_replace", {"find": "OLD", "replace": "NEW", "dry_run": True}
    )
    added = await _payload(client, "entity_create_text", {"text": "OLD too", "x": 0, "y": 5})

    payload = await _refused(
        client,
        "text_find_replace",
        {"find": "OLD", "replace": "NEW", "preconditions": dry["preview"]["preconditions"]},
    )

    assert payload["changed"] == [
        {"handle": added["handle"], "expected": "not in the preview", "actual": "matches"}
    ]
    assert backend._doc.entitydb[text.handle].dxf.text == "OLD"
    assert backend._doc.entitydb[added["handle"]].dxf.text == "OLD too"


async def test_a_guarded_find_replace_applies_and_reports_its_revision(served):
    client, backend = served
    text = await backend.entity_create_text("OLD", 0, 0)
    dry = await _payload(
        client, "text_find_replace", {"find": "OLD", "replace": "NEW", "dry_run": True}
    )

    result = await _payload(
        client,
        "text_find_replace",
        {
            "find": "OLD",
            "replace": "NEW",
            "expected_revision": dry["preview"]["revision"],
            "preconditions": dry["preview"]["preconditions"],
        },
    )

    assert result["replaced"] == 1
    assert backend._doc.entitydb[text.handle].dxf.text == "NEW"
    assert result["revision"]["checked"] == dry["preview"]["revision"]
    assert result["revision"]["interleaved"] == []


async def test_a_stale_step_inside_a_batch_is_typed_stale_revision(served):
    client, backend = served
    text = await backend.entity_create_text("OLD", 0, 0)
    dry = await _payload(
        client, "text_find_replace", {"find": "OLD", "replace": "NEW", "dry_run": True}
    )
    backend._doc.entitydb[text.handle].dxf.color = 3

    result = await _payload(
        client,
        "cad_batch",
        {
            "steps": [
                {
                    "tool": "text_find_replace",
                    "args": {
                        "find": "OLD",
                        "replace": "NEW",
                        "preconditions": dry["preview"]["preconditions"],
                    },
                }
            ]
        },
    )

    assert result["ok"] is False
    (row,) = result["results"]
    assert row["error"]["kind"] == "stale_revision"
    assert row["error"]["changed"][0]["handle"] == text.handle
    assert "stale_revision" in server.BATCH_ERROR_KINDS
    assert backend._doc.entitydb[text.handle].dxf.text == "OLD"


# -- pid_from_spec ------------------------------------------------------------------

SPEC = {
    "instruments": [
        {"id": "I1", "type": "dcs", "location": "primary", "x": 0, "y": 0},
        {"id": "I2", "type": "dcs", "location": "primary", "x": 100, "y": 0},
    ],
    "lines": [{"from": "I1", "to": "I2", "class": "electric", "route": "direct", "label": False}],
}
#: The dcs instrument's catalogue box is (-5, -5, 5, 5): placed at (0, 0) and
#: (100, 0) it spans x -5..105, y -5..5; the line (5, 0)-(95, 0) lies inside.
REGION = [-5.0, -5.0, 105.0, 5.0]


async def test_a_spec_dry_run_previews_its_routing_region(served):
    client, backend = served

    result = await _payload(client, "pid_from_spec", {"spec": SPEC, "dry_run": True})

    assert result["region"] == REGION
    preview = result["preview"]
    assert preview["revision"] == _revision(backend)
    (row,) = preview["preconditions"]
    assert row["region"] == REGION and len(row["fingerprint"]) == 64
    assert preview["planned"] == [
        {"where": "instruments[0]", "id": "I1"},
        {"where": "instruments[1]", "id": "I2"},
        {"where": "lines[0]", "from": "I1", "to": "I2", "class": "electric"},
    ]


async def test_review_focus_5_an_entity_added_in_the_region_refuses_the_sheet(served):
    client, backend = served
    dry = await _payload(client, "pid_from_spec", {"spec": SPEC, "dry_run": True})
    await client.call_tool("entity_create_circle", {"cx": 50, "cy": 0, "radius": 1})

    payload = await _refused(
        client,
        "pid_from_spec",
        {"spec": SPEC, "preconditions": dry["preview"]["preconditions"]},
    )

    assert payload["changed"][0]["region"] == REGION
    assert await backend.entity_list(type_filter="INSERT") == [], "nothing was drawn"
    assert (await backend.system_status())["transaction_depth"] == 0


async def test_an_entity_added_outside_the_region_does_not_refuse(served):
    client, backend = served
    dry = await _payload(client, "pid_from_spec", {"spec": SPEC, "dry_run": True})
    await client.call_tool("entity_create_circle", {"cx": 500, "cy": 500, "radius": 1})

    result = await _payload(
        client, "pid_from_spec", {"spec": SPEC, "preconditions": dry["preview"]["preconditions"]}
    )

    assert set(result["handles"]) == {"I1", "I2"}
    assert result["revision"]["interleaved"] == []


async def test_a_region_precondition_from_another_spec_is_refused_by_path(backend):
    from engineering.changes.preview import region_fingerprint
    from engineering.pid.spec import run_spec

    other = [0.0, 0.0, 10.0, 10.0]
    row = {"region": other, "fingerprint": await region_fingerprint(backend, tuple(other))}

    with pytest.raises(SpecError, match=r"^preconditions\[0\]\.region: "):
        await run_spec(backend, SPEC, preconditions=[row])
    assert await backend.entity_list(type_filter="INSERT") == []
