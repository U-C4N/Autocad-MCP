"""Result shaping — field projection + compact rows on the collection tools.

The release made *discovery* cheap and left the other half of the token bill
untouched: what the tools hand back. On a 300-entity drawing a single default
``entity_list(limit=100)`` is ~24 kB of JSON the model has to read, and roughly
a third of it is a ``properties.bounding_box`` nobody asked for.

ONE mechanism, two parameters, spelled identically on every collection tool
(:data:`server.SHAPED_RESULT_TOOLS`)::

    fields=["handle", "type", "layer"]   # projection, order preserved
    compact=True                         # columnar envelope instead of dicts

What these tests guard is the mechanism, not the byte counts:

* **Backwards compatibility is the hard constraint.** Omitting both parameters
  must return exactly the 1.4.0 value. Every pre-existing test in the suite is
  the real gate; the test here only pins the intent.
* **No per-tool dialect.** ``SHAPED_RESULT_TOOLS`` is checked against the live
  registry in *both* directions, and the two parameters' JSON Schemas are
  compared across every tool that carries them. A tool that grows a ``columns=``
  or a ``brief=`` of its own fails here.
* **A typo is an error, never an empty column.** An unknown field name must come
  back naming the valid fields. This is the difference between a projection and
  a footgun: ``fields=["handel"]`` returning 100 rows of ``{"handel": null}``
  reads as "the drawing has no handles".
* **Truncation honesty.** A silently truncated list read as complete is a
  correctness bug, not a token one. ``entity_list`` pages, and in 1.4.0 the
  return value carried no signal at all that more rows existed; the two
  ``analysis_select_by_*`` tools capped at ``MAX_LIST_LIMIT`` and said so only
  in a log notification most clients never show the model. The compact envelope
  is the mode that can carry ``total``/``truncated``/``next_offset``, and
  ``total`` is measured against the *unfiltered* matching set rather than the
  page, which is why the backends gained ``entity_count``.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError

import config
import server
from backends.base import BlockInfo, EntityInfo, LayerInfo

pytestmark = pytest.mark.asyncio


ENTITY_FIELDS = ("handle", "type", "layer", "color", "linetype", "visible", "properties")


@pytest_asyncio.fixture
async def client(monkeypatch):
    """In-memory client on a fresh headless document.

    The env var is what ``server._make_backend`` reads; setting
    ``config.settings.backend`` would leave a Windows box with AutoCAD open
    talking to the operator's live drawing.
    """
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        await connected.call_tool("drawing_new", {})
        yield connected


async def _draw(client, circles: int = 30, lines: int = 30) -> None:
    """A small mixed drawing: CIRCLEs carry properties LINEs do not."""
    steps = []
    for i in range(circles):
        steps.append(
            {
                "tool": "entity_create_circle",
                "args": {"cx": float(i), "cy": 0.0, "radius": 1.0 + i, "layer": "GEOMETRY"},
            }
        )
    for i in range(lines):
        steps.append(
            {
                "tool": "entity_create_line",
                "args": {
                    "x1": 0.0,
                    "y1": float(i),
                    "x2": 10.0,
                    "y2": float(i),
                    "layer": "HIDDEN",
                },
            }
        )
    result = (await client.call_tool("cad_batch", {"steps": steps})).structured_content
    assert result["failed"] == 0, result


def _rows(result) -> list | dict:
    """Unwrap FastMCP's ``{"result": ...}`` output wrapper."""
    return (result.structured_content or {})["result"]


def dumps(obj) -> str:
    """Compact JSON — how a payload is actually charged for."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# The mechanism is one mechanism
# ---------------------------------------------------------------------------


async def test_every_shaped_tool_carries_both_parameters():
    tools = {t.name: t for t in await server._registered_tools()}
    missing = [name for name in server.SHAPED_RESULT_TOOLS if name not in tools]
    assert not missing, f"SHAPED_RESULT_TOOLS names unregistered tools: {missing}"
    for name in server.SHAPED_RESULT_TOOLS:
        properties = (tools[name].parameters or {}).get("properties", {})
        for param in server.RESULT_SHAPE_PARAMS:
            assert param in properties, f"{name} is listed as shaped but has no {param!r} parameter"


async def test_no_tool_carries_the_parameters_without_being_declared_shaped():
    """The closed list is checked in both directions, so it cannot silently rot."""
    tools = await server._registered_tools()
    carriers = {
        tool.name
        for tool in tools
        if set(server.RESULT_SHAPE_PARAMS) & set((tool.parameters or {}).get("properties", {}))
    }
    assert carriers == set(server.SHAPED_RESULT_TOOLS)


async def test_a_shaped_tool_never_carries_only_half_the_mechanism():
    for tool in await server._registered_tools():
        properties = set((tool.parameters or {}).get("properties", {}))
        present = [param for param in server.RESULT_SHAPE_PARAMS if param in properties]
        assert len(present) in (0, len(server.RESULT_SHAPE_PARAMS)), (
            f"{tool.name} declares {present} — the two parameters ship together or not at all"
        )


async def test_the_parameter_schemas_are_identical_across_every_shaped_tool():
    """One spelling everywhere: no per-tool dialect of the same idea."""
    tools = {t.name: t for t in await server._registered_tools()}
    seen: dict[str, list[tuple[str, str]]] = {param: [] for param in server.RESULT_SHAPE_PARAMS}
    for name in server.SHAPED_RESULT_TOOLS:
        properties = (tools[name].parameters or {})["properties"]
        for param in server.RESULT_SHAPE_PARAMS:
            seen[param].append((name, dumps(properties[param])))
    for param, entries in seen.items():
        distinct = {schema for _, schema in entries}
        assert len(distinct) == 1, f"{param!r} is spelled {len(distinct)} different ways: {entries}"


# ---------------------------------------------------------------------------
# Backwards compatibility
# ---------------------------------------------------------------------------


async def test_omitting_both_parameters_returns_the_untouched_record(client):
    await _draw(client, circles=3, lines=0)
    rows = _rows(await client.call_tool("entity_list", {}))
    assert isinstance(rows, list) and len(rows) == 3
    assert set(rows[0]) == set(ENTITY_FIELDS), "the default record must keep every field"
    assert rows[0]["properties"]["radius"] == pytest.approx(1.0)


async def test_explicit_defaults_are_the_same_value_as_omitting_them(client):
    await _draw(client, circles=4, lines=4)
    omitted = _rows(await client.call_tool("entity_list", {}))
    explicit = _rows(await client.call_tool("entity_list", {"fields": None, "compact": False}))
    assert omitted == explicit


@pytest.mark.parametrize(
    "tool,args,spec",
    [
        ("layer_list", {}, LayerInfo),
        ("block_list", {}, BlockInfo),
        ("analysis_select_by_layer", {"layer_name": "GEOMETRY"}, EntityInfo),
        ("analysis_select_by_type", {"entity_type": "CIRCLE"}, EntityInfo),
        ("analysis_find_in_region", {"x1": -1e6, "y1": -1e6, "x2": 1e6, "y2": 1e6}, EntityInfo),
        ("entity_select_smart", {"predicate": {"type": "CIRCLE"}}, EntityInfo),
    ],
)
async def test_default_shape_is_a_list_of_full_records(client, tool, args, spec):
    await _draw(client, circles=3, lines=3)
    rows = _rows(await client.call_tool(tool, args))
    assert isinstance(rows, list)
    for row in rows:
        assert set(row) == set(spec.__dataclass_fields__)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


async def test_projection_keeps_only_the_named_fields_in_order(client):
    await _draw(client, circles=5, lines=0)
    rows = _rows(await client.call_tool("entity_list", {"fields": ["handle", "type"]}))
    assert all(list(row) == ["handle", "type"] for row in rows)
    assert all(row["type"] == "CIRCLE" for row in rows)


async def test_the_projection_order_is_the_callers_not_the_dataclasses(client):
    await _draw(client, circles=3, lines=0)
    rows = _rows(await client.call_tool("entity_list", {"fields": ["layer", "handle"]}))
    assert all(list(row) == ["layer", "handle"] for row in rows)


async def test_a_repeated_field_is_projected_once(client):
    """Repeats are a caller slip, not an error — but they must not double the column."""
    await _draw(client, circles=2, lines=0)
    rows = _rows(await client.call_tool("entity_list", {"fields": ["handle", "handle", "type"]}))
    assert all(list(row) == ["handle", "type"] for row in rows)


async def test_a_dict_valued_field_can_still_be_taken_whole(client):
    await _draw(client, circles=2, lines=0)
    rows = _rows(await client.call_tool("entity_list", {"fields": ["properties"]}))
    assert all(set(row) == {"properties"} for row in rows)
    assert rows[0]["properties"]["radius"] == pytest.approx(1.0)


async def test_projection_reaches_one_nested_property(client):
    await _draw(client, circles=3, lines=0)
    rows = _rows(await client.call_tool("entity_list", {"fields": ["handle", "properties.radius"]}))
    assert [row["properties.radius"] for row in rows] == [1.0, 2.0, 3.0]


async def test_a_nested_key_absent_from_some_rows_is_null_not_an_error(client):
    """LINEs have no radius; the column exists because CIRCLEs in the set do."""
    await _draw(client, circles=2, lines=2)
    rows = _rows(await client.call_tool("entity_list", {"fields": ["type", "properties.radius"]}))
    by_type = {row["type"]: row["properties.radius"] for row in rows}
    assert by_type["CIRCLE"] is not None
    assert by_type["LINE"] is None


async def test_an_unknown_field_is_an_error_naming_the_valid_fields(client):
    await _draw(client, circles=2, lines=0)
    with pytest.raises(ToolError) as excinfo:
        await client.call_tool("entity_list", {"fields": ["handel"]})
    message = str(excinfo.value)
    assert "handel" in message
    for field in ENTITY_FIELDS:
        assert field in message, f"the error must name {field!r} as a valid field"


async def test_an_unknown_nested_key_is_an_error_naming_the_available_keys(client):
    await _draw(client, circles=2, lines=0)
    with pytest.raises(ToolError) as excinfo:
        await client.call_tool("entity_list", {"fields": ["properties.diameter"]})
    message = str(excinfo.value)
    assert "diameter" in message
    assert "radius" in message, "the error must list the sub-keys this result actually has"


async def test_an_empty_projection_is_an_error_not_an_empty_column(client):
    await _draw(client, circles=2, lines=0)
    with pytest.raises(ToolError) as excinfo:
        await client.call_tool("entity_list", {"fields": []})
    assert "handle" in str(excinfo.value)


async def test_the_valid_field_list_follows_the_row_dataclass_not_entityinfo(client):
    """layer_list rows are LayerInfo, so its error must name LayerInfo's fields."""
    with pytest.raises(ToolError) as excinfo:
        await client.call_tool("layer_list", {"fields": ["handle"]})
    message = str(excinfo.value)
    assert "is_frozen" in message and "lineweight" in message
    assert "linetype" in message


async def test_projection_works_on_every_shaped_collection_tool(client):
    await _draw(client, circles=3, lines=3)
    cases = [
        ("analysis_select_by_layer", {"layer_name": "GEOMETRY"}, ["handle", "type"]),
        ("analysis_select_by_type", {"entity_type": "LINE"}, ["handle", "layer"]),
        (
            "analysis_find_in_region",
            {"x1": -1e6, "y1": -1e6, "x2": 1e6, "y2": 1e6},
            ["handle", "type"],
        ),
        ("entity_select_smart", {"predicate": {"type": "CIRCLE"}}, ["handle"]),
        ("layer_list", {}, ["name", "color"]),
        ("block_list", {}, ["name"]),
    ]
    for tool, args, fields in cases:
        rows = _rows(await client.call_tool(tool, {**args, "fields": fields}))
        assert isinstance(rows, list), tool
        for row in rows:
            assert list(row) == fields, tool


# ---------------------------------------------------------------------------
# Compact envelope
# ---------------------------------------------------------------------------


async def test_compact_returns_a_columnar_envelope(client):
    await _draw(client, circles=4, lines=0)
    envelope = _rows(await client.call_tool("entity_list", {"compact": True}))
    assert isinstance(envelope, dict)
    assert set(envelope) == {
        "fields",
        "rows",
        "count",
        "offset",
        "total",
        "truncated",
        "next_offset",
    }
    assert envelope["fields"] == list(ENTITY_FIELDS)
    assert envelope["count"] == 4
    assert all(len(row) == len(envelope["fields"]) for row in envelope["rows"])


async def test_compact_rows_carry_the_same_values_as_the_dict_form(client):
    await _draw(client, circles=3, lines=2)
    fields = ["handle", "type", "layer"]
    dicts = _rows(await client.call_tool("entity_list", {"fields": fields}))
    envelope = _rows(await client.call_tool("entity_list", {"fields": fields, "compact": True}))
    assert envelope["fields"] == fields
    assert envelope["rows"] == [[row[name] for name in fields] for row in dicts]


async def test_compact_is_much_cheaper_than_the_default_record(client):
    """The point of the exercise, guarded as a ratio rather than a byte count."""
    await _draw(client, circles=60, lines=60)
    default = dumps(_rows(await client.call_tool("entity_list", {"limit": 120})))
    projected = dumps(
        _rows(
            await client.call_tool(
                "entity_list",
                {"limit": 120, "fields": ["handle", "type", "layer"], "compact": True},
            )
        )
    )
    assert len(projected) < len(default) / 5, (
        f"projected+compact was {len(projected)} chars against {len(default)} — "
        "the mechanism is not paying for its schema cost"
    )


async def test_compact_on_a_shaped_tool_that_already_returns_a_dict(client):
    """``selection_get``'s collection is nested, so its envelope lands under it."""
    result = (await client.call_tool("selection_get", {"compact": True})).structured_content
    assert isinstance(result["entities"], dict)
    assert result["entities"]["fields"] == list(ENTITY_FIELDS)
    assert result["entities"]["rows"] == []
    plain = (await client.call_tool("selection_get", {})).structured_content
    assert plain["entities"] == [], "the default must stay a plain list"


# ---------------------------------------------------------------------------
# Truncation honesty
# ---------------------------------------------------------------------------


async def test_a_paged_entity_list_reports_the_total_it_did_not_return(client):
    await _draw(client, circles=40, lines=40)
    envelope = _rows(
        await client.call_tool("entity_list", {"limit": 10, "compact": True, "fields": ["handle"]})
    )
    assert envelope["count"] == 10
    assert envelope["total"] == 80, "total counts the matching set, not the page"
    assert envelope["truncated"] is True
    assert envelope["next_offset"] == 10


async def test_the_last_page_is_not_reported_as_truncated(client):
    await _draw(client, circles=10, lines=0)
    envelope = _rows(
        await client.call_tool(
            "entity_list", {"limit": 10, "offset": 5, "compact": True, "fields": ["handle"]}
        )
    )
    assert (envelope["count"], envelope["offset"], envelope["total"]) == (5, 5, 10)
    assert envelope["truncated"] is False
    assert envelope["next_offset"] is None


async def test_total_honours_the_same_filters_as_the_page(client):
    await _draw(client, circles=20, lines=30)
    envelope = _rows(
        await client.call_tool(
            "entity_list",
            {"type_filter": "LINE", "limit": 5, "compact": True, "fields": ["handle"]},
        )
    )
    assert envelope["total"] == 30, "a filtered page must be measured against the filtered set"
    envelope = _rows(
        await client.call_tool(
            "entity_list",
            {"layer_filter": "GEOMETRY", "limit": 5, "compact": True, "fields": ["handle"]},
        )
    )
    assert envelope["total"] == 20


async def test_the_max_list_limit_cap_is_reported_as_truncation(client, monkeypatch):
    """``analysis_select_by_layer`` capped silently in the value before this."""
    monkeypatch.setattr(config.settings, "max_list_limit", 4)
    await _draw(client, circles=9, lines=0)
    envelope = _rows(
        await client.call_tool(
            "analysis_select_by_layer",
            {"layer_name": "GEOMETRY", "compact": True, "fields": ["handle"]},
        )
    )
    assert envelope["count"] == 4
    assert envelope["total"] == 9
    assert envelope["truncated"] is True
    assert envelope["next_offset"] == 4


async def test_an_untruncated_selection_says_so(client):
    await _draw(client, circles=6, lines=0)
    envelope = _rows(
        await client.call_tool(
            "analysis_select_by_type",
            {"entity_type": "CIRCLE", "compact": True, "fields": ["handle"]},
        )
    )
    assert (envelope["count"], envelope["total"]) == (6, 6)
    assert envelope["truncated"] is False


async def test_default_mode_still_truncates_the_way_it_always_did(client, monkeypatch):
    """The compatibility contract cuts both ways: the old value, cap included."""
    monkeypatch.setattr(config.settings, "max_list_limit", 4)
    await _draw(client, circles=9, lines=0)
    rows = _rows(await client.call_tool("analysis_select_by_layer", {"layer_name": "GEOMETRY"}))
    assert isinstance(rows, list) and len(rows) == 4


# ---------------------------------------------------------------------------
# entity_count — the backend method the honest `total` needs
# ---------------------------------------------------------------------------


async def test_entity_count_matches_a_full_listing(backend):
    for i in range(7):
        await backend.entity_create_circle(float(i), 0.0, 1.0, layer="GEOMETRY")
    for i in range(5):
        await backend.entity_create_line(0.0, float(i), 1.0, float(i), layer="HIDDEN")
    assert await backend.entity_count() == 12
    assert await backend.entity_count(type_filter="CIRCLE") == 7
    assert await backend.entity_count(layer_filter="HIDDEN") == 5
    assert await backend.entity_count(type_filter="LINE", layer_filter="GEOMETRY") == 0


async def test_entity_count_filters_case_insensitively_like_entity_list(backend):
    await backend.entity_create_circle(0.0, 0.0, 1.0, layer="GEOMETRY")
    assert await backend.entity_count(type_filter="circle") == 1
    assert await backend.entity_count(layer_filter="geometry") == 1


async def test_entity_count_is_on_the_backend_contract():
    from backends.base import AutoCADBackend

    assert "entity_count" in AutoCADBackend.__abstractmethods__


async def test_entity_count_never_materialises_the_rows(backend, monkeypatch):
    """A count that paged through entity_list would defeat its own purpose."""

    async def _forbidden(*args, **kwargs):
        raise AssertionError("entity_count must not call entity_list")

    monkeypatch.setattr(backend, "entity_list", _forbidden)
    await backend.entity_create_circle(0.0, 0.0, 1.0)
    assert await backend.entity_count() == 1
