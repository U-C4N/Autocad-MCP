"""``arch_plan_from_spec``: validation by path, one transaction, the shielded rollback.

Headless ezdxf against a real document. The areas are computed by hand from
``EXAMPLE_SPEC``: the exterior ring runs on axis (0,0)-(9000,6000) at 250 mm,
so its inner faces are x = 125 / 8875 and y = 125 / 5875; the interior wall
runs on x = 5000 at 100 mm, faces x = 4950 / 5050. Room 01 is therefore
(4950 - 125) x (5875 - 125) = 4825 x 5750 = 27 743 750 mm², room 02
(8875 - 5050) x 5750 = 3825 x 5750 = 21 993 750 mm² - the net floor, not the
axis area.
"""

from __future__ import annotations

import copy

import anyio
import pytest

from engineering.arch.spec import EXAMPLE_SPEC, draw_plan_from_spec, validate_spec

ROOM_01 = 4825.0 * 5750.0
ROOM_02 = 3825.0 * 5750.0


def _spec(**over) -> dict:
    spec = copy.deepcopy(EXAMPLE_SPEC)
    spec.update(over)
    return spec


async def _count(backend) -> int:
    return len(await backend.entity_list(limit=5000))


# -- validation, before anything is drawn ---------------------------------------


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"colour": "red"}, r"^spec: unknown key\(s\) colour; keys are sheet"),
        ({"walls": []}, r"^walls: name at least one wall"),
        ({"lang": "de"}, r"^lang: unknown language 'de'; languages are en, tr"),
        ({"sheet": {"scale": 0}}, r"^sheet\.scale: must be a finite number greater than zero"),
        ({"chains": {"sides": ["north"]}}, r"^chains\.sides: \['north'\]"),
        (
            {"schedules": [{"kind": "stairs", "at": [0, 0]}]},
            r"^schedules\[0\]\.kind: 'stairs' is not a schedule",
        ),
    ],
)
@pytest.mark.asyncio
async def test_the_document_is_refused_by_path(backend, over, message):
    with pytest.raises(ValueError, match=message):
        await draw_plan_from_spec(backend, _spec(**over))
    assert await _count(backend) == 0


@pytest.mark.asyncio
async def test_a_bad_wall_is_named_by_its_index(backend):
    spec = _spec()
    spec["walls"][1]["thickness"] = -100
    with pytest.raises(ValueError, match=r"^walls\[1\]\.thickness: must be greater than zero"):
        await draw_plan_from_spec(backend, spec)
    assert await _count(backend) == 0


@pytest.mark.asyncio
async def test_two_overlapping_openings_are_refused_naming_both(backend):
    spec = _spec()
    spec["openings"][0]["offset"] = 1000
    spec["openings"].append(
        {"id": "D9", "wall": "EXT", "kind": "door", "offset": 1500, "width": 900}
    )
    with pytest.raises(ValueError, match=r"openings D1 and D9 overlap on wall EXT"):
        await draw_plan_from_spec(backend, spec)
    assert await _count(backend) == 0


@pytest.mark.asyncio
async def test_a_typed_room_area_is_refused(backend):
    spec = _spec()
    spec["rooms"][0]["area"] = 27.0e6
    with pytest.raises(ValueError, match=r"^rooms\[0\]\.area: an area is measured"):
        await draw_plan_from_spec(backend, spec)
    assert await _count(backend) == 0


def test_validate_spec_turns_dicts_into_the_model():
    norm = validate_spec(EXAMPLE_SPEC)
    assert [w.id for w in norm["walls"]] == ["EXT", "INT"]
    assert [o.id for o in norm["openings"]] == ["D1", "D2", "W1", "W2"]
    assert norm["scale"] == 50.0 and norm["lang"] == "en"
    assert norm["chains"] == ("bottom", "left")
    assert [s["kind"] for s in norm["schedules"]] == ["doors", "windows", "rooms"]


# -- the whole plan --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_example_plan_draws_in_one_transaction_and_measures_its_rooms(backend):
    from engineering.arch.draw import read_plan

    result = await draw_plan_from_spec(backend, EXAMPLE_SPEC)
    assert result["walls"] == ["EXT", "INT"]
    assert result["openings"] == ["D1", "D2", "W1", "W2"]
    assert result["omitted"] == []
    assert len(result["rooms"]) == 2 and len(result["schedules"]) == 3
    assert result["critique"] == []
    plan = await read_plan(backend)
    areas = {room.number: room.area for room in plan["rooms"]}
    assert areas["01"] == pytest.approx(ROOM_01, abs=0.5)
    assert areas["02"] == pytest.approx(ROOM_02, abs=0.5)
    assert backend.get_plan_spec()["layer_set_id"] == "arch"


@pytest.mark.asyncio
async def test_a_room_outside_every_face_rolls_the_whole_plan_back(backend):
    spec = _spec()
    spec["rooms"][1]["at"] = [20000, 20000]
    with pytest.raises(ValueError, match=r"^rooms\[1\]: "):
        await draw_plan_from_spec(backend, spec)
    assert await _count(backend) == 0
    begun = await backend.transaction_begin()
    assert begun["ok"] is True, "the rollback must have closed the transaction"
    await backend.transaction_rollback()


@pytest.mark.asyncio
async def test_a_cancelled_request_still_rolls_back(backend, monkeypatch):
    """The MCP SDK cancels a request by cancelling an anyio scope; the rollback
    runs under a shield, so the cancelled plan leaves nothing behind."""
    from engineering.arch import rooms

    async def hang(*_args, **_kwargs):
        await anyio.sleep_forever()

    monkeypatch.setattr(rooms, "label_room", hang)
    with anyio.move_on_after(0.5):
        await draw_plan_from_spec(backend, EXAMPLE_SPEC)
    assert await _count(backend) == 0
    begun = await backend.transaction_begin()
    assert begun["ok"] is True
    await backend.transaction_rollback()


@pytest.mark.asyncio
async def test_the_tool_is_registered_and_refuses_through_tool_error():
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    import server

    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        result = (
            await client.call_tool("arch_plan_from_spec", {"spec": EXAMPLE_SPEC})
        ).structured_content
        assert result["critique"] == []
        with pytest.raises(ToolError, match=r"walls: name at least one wall"):
            await client.call_tool("arch_plan_from_spec", {"spec": {"walls": []}})
