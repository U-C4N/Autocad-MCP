"""server.py must never report something it does not have.

Three families of that rule live here.

**The registry must be read untransformed.** ``_registered_tools()`` backs three
things that all need the *real* inventory:

  * ``system_status`` / ``system_about`` tool counts,
  * ``_tool_groups()`` (the system_about per-group breakdown),
  * ``_apply_tool_profile()`` (which tools get ``mcp.disable()``d).

FastMCP applies :class:`~fastmcp.server.transforms.Transform` chains at two
levels, and only one of them reaches ``mcp._list_tools()``. Shipped search mode
(``_apply_discovery_mode("search")``) adds a *server-level* transform: it swaps
the catalog for ``search_tools``/``call_tool`` at the wire, while
``mcp._list_tools()`` upstream of it still lists everything — so search mode on
its own moves none of the three. A *provider-level* transform is the one that
rewrites ``mcp._list_tools()`` itself, and that is what ``_TruncatingTransform``
below installs. So the property pinned here is the stronger, layer-agnostic one:
registry-truth is read below every transform, and no catalog rewrite — today's,
or a provider-level one someone mounts tomorrow — can make the server report a
surface it does not have or silently turn TOOL_PROFILE into a no-op.

**A tool must not report a document it did not open** (T0.2) — the read-side
twin of the DWG write refusal, and the reason a ``.dwg`` path is refused on a
backend whose ``dwg`` capability is false.

**Discovery metadata must not cost anything.** ``@cad_tool`` writes summary /
cost / alias metadata into the MCP ``meta`` channel; ``tags`` are load-bearing
(``_tool_groups()`` buckets every tool by them for system_about), so the
grouping is pinned to the tags literally written in server.py's source. The
channel is also gated for *coverage*: every registered tool must carry a card,
and no card may contradict the tool's own annotations.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.transforms import Transform

import server
from backends.base import CapabilityMap, FeatureCapability
from discovery.aliases import aliases_for
from discovery.serialize import MAX_SUMMARY_CHARS

pytestmark = pytest.mark.asyncio


class _TruncatingTransform(Transform):
    """Stand-in for a catalog-replacing transform: collapses the list to two tools.

    Mirrors the shape of the problem rather than any shipped transform — a
    transform that replaces the tool list wholesale instead of decorating it,
    mounted (see the fixture) at the level that reaches ``mcp._list_tools()``.
    """

    def __init__(self, keep: int = 2):
        self.keep = keep

    async def list_tools(self, tools):
        return list(tools)[: self.keep]


@pytest.fixture
def truncating_transform():
    """Install the transform where FastMCP actually applies it, then remove it.

    ``mcp._list_tools()`` aggregates ``provider.list_tools()``, and
    ``Provider.list_tools`` is what runs the provider's transforms — so a
    provider-level transform is exactly what rewrites that call's output.
    """
    transform = _TruncatingTransform()
    server.mcp.local_provider.add_transform(transform)
    try:
        yield transform
    finally:
        server.mcp.local_provider._transforms.remove(transform)


@pytest_asyncio.fixture(autouse=True)
async def _restore_full_profile():
    """These tests call _apply_tool_profile, which mutates global enablement."""
    yield
    await server._apply_tool_profile("full")


async def test_transform_really_rewrites_the_public_listing(truncating_transform):
    """Guard the guard: prove the fixture actually breaks the public accessor.

    Without this the other tests would pass even if the transform silently did
    nothing.
    """
    assert len(await server.mcp._list_tools()) == 2


async def test_registered_tools_unaffected_by_a_transform(truncating_transform):
    names = {t.name for t in await server._registered_tools()}
    assert len(names) > 100
    assert "system_about" in names
    assert "gear_draw_spur_front_view" in names


async def test_registered_tool_count_unaffected_by_a_transform(truncating_transform):
    count = await server._registered_tool_count()
    assert count is not None
    assert count > 100


async def test_tool_groups_unaffected_by_a_transform(truncating_transform):
    groups = await server._tool_groups()
    # The transform collapses the catalog to two drawing tools, which would
    # leave a single "drawing" bucket.
    assert len(groups) > 5
    assert groups["transactions"] == [
        "transaction_begin",
        "transaction_commit",
        "transaction_rollback",
    ]
    assert "gear_draw_spur_front_view" in groups["engineering"]


async def test_tool_profile_still_disables_under_a_transform(truncating_transform):
    """TOOL_PROFILE must keep working when a catalog-rewriting transform is mounted.

    Reading the transformed catalog would make ``registered`` tiny, so
    ``registered - enabled`` collapses and ``mcp.disable()`` never fires.
    """
    info = await server._apply_tool_profile("lean")
    assert info["registered_count"] > 100
    assert info["enabled_count"] == len(server.LEAN_TOOL_NAMES)
    assert info["disabled_count"] > 50
    # The escape hatches really are hidden, not merely counted as hidden.
    assert "system_run_command" in info["disabled_tools"]


async def test_registered_tools_returns_empty_when_registry_unknown(monkeypatch):
    """Contract: never raises, and reports [] / None rather than a bogus count."""

    class _Broken:
        @property
        def _components(self):
            raise RuntimeError("registry layout changed")

        async def _list_tools(self):
            raise RuntimeError("registry layout changed")

    async def _boom():
        raise RuntimeError("public accessor gone")

    monkeypatch.setattr(server.mcp, "_local_provider", _Broken(), raising=False)
    monkeypatch.setattr(server.mcp, "_list_tools", _boom, raising=False)

    assert await server._registered_tools() == []
    assert await server._registered_tool_count() is None


# ── T0.2 — drawing_open must not report a document it did not open ──────────


class _Ctx:
    """Minimal async context for calling a tool function directly."""

    def __init__(self, backend):
        self.lifespan_context = {"backend": backend}

    async def info(self, message):
        pass

    async def warning(self, message):
        pass

    async def report_progress(self, progress, total):
        pass


async def _saved_dxf(backend, tmp_path):
    dxf = tmp_path / "part.dxf"
    await backend.drawing_save_as(str(dxf), "dxf")
    return dxf


async def test_drawing_open_opens_a_dxf(backend, tmp_path):
    """Baseline: the honest path keeps working."""
    dxf = await _saved_dxf(backend, tmp_path)
    result = await server.drawing_open(str(dxf), ctx=_Ctx(backend))
    assert result["ok"] is True
    assert result["name"] == "part.dxf"


async def test_drawing_open_refuses_a_dwg_the_backend_cannot_read(backend, tmp_path):
    """DXF bytes wearing a .dwg name must not be reported as an opened DWG.

    ezdxf sniffs content, not extensions, so it happily parses a mislabelled
    file (exactly what pre-1.5.0 drawing_save produced) and the tool then
    answers ``{"ok": True, "name": "part.dwg"}`` — a document that does not
    exist in that format.
    """
    dxf = await _saved_dxf(backend, tmp_path)
    disguised = tmp_path / "part.dwg"
    shutil.copy(dxf, disguised)

    with pytest.raises(ToolError) as excinfo:
        await server.drawing_open(str(disguised), ctx=_Ctx(backend))

    message = str(excinfo.value).lower()
    assert "dwg" in message
    # Blames the backend's missing capability and names the escape hatch,
    # rather than ezdxf's bare "is not a DXF file".
    assert "ezdxf" in message
    assert "com" in message


async def test_drawing_open_refuses_a_real_dwg_without_leaking_a_parser_error(backend, tmp_path):
    real_dwg = tmp_path / "real.dwg"
    real_dwg.write_bytes(b"AC1032" + b"\x00" * 256)

    with pytest.raises(ToolError) as excinfo:
        await server.drawing_open(str(real_dwg), ctx=_Ctx(backend))

    assert "is not a DXF file" not in str(excinfo.value)


async def test_drawing_open_refusal_leaves_the_current_document_untouched(backend, tmp_path):
    dxf = await _saved_dxf(backend, tmp_path)
    ctx = _Ctx(backend)
    await server.drawing_open(str(dxf), ctx=ctx)

    disguised = tmp_path / "other.dwg"
    shutil.copy(dxf, disguised)
    with pytest.raises(ToolError):
        await server.drawing_open(str(disguised), ctx=ctx)

    info = await backend.drawing_info()
    assert info.name == "part.dxf"


class _DwgCapableBackend:
    """Stand-in for the live COM backend, which reads DWG natively."""

    name = "com"

    def __init__(self):
        self.opened: list[str] = []

    def capabilities(self) -> CapabilityMap:
        return CapabilityMap(
            backend="com",
            features={"dwg": FeatureCapability(True, "native")},
        )

    async def drawing_open(self, path: str) -> dict:
        self.opened.append(path)
        return {"ok": True, "name": "part.dwg", "path": path}


async def test_drawing_open_allows_dwg_when_the_backend_supports_it(tmp_path):
    """The refusal is capability-driven, not an ezdxf-shaped hardcode."""
    fake = _DwgCapableBackend()
    result = await server.drawing_open(str(tmp_path / "part.dwg"), ctx=_Ctx(fake))
    assert result["ok"] is True
    assert len(fake.opened) == 1


async def test_drawing_open_does_not_advertise_unconditional_dwg():
    """The lie was also in the docs the model reads before calling."""
    tool = next(t for t in await server._registered_tools() if t.name == "drawing_open")
    text = (tool.description or "").lower()
    for param in (tool.parameters or {}).get("properties", {}).values():
        text += " " + str(param.get("description", "")).lower()
    assert "dwg" in text, "DWG support should still be discoverable"
    assert "com" in text, "but qualified by the backend that actually has it"


# ── @cad_tool — the discovery metadata channel ──────────────────────────────

CAD_TOOL_SECTION = ("transaction_begin", "transaction_commit", "transaction_rollback")


def _component(name: str):
    for tool in server._local_tool_components():
        if tool.name == name:
            return tool
    raise AssertionError(f"{name} is not registered")


@pytest.mark.parametrize("name", CAD_TOOL_SECTION)
async def test_cad_tool_publishes_discovery_meta(name):
    cad = (_component(name).meta or {})["cad"]
    assert cad["summary"] and "\n" not in cad["summary"]
    assert cad["cost"] in server.CAD_TOOL_COSTS


@pytest.mark.parametrize("name", CAD_TOOL_SECTION)
async def test_cad_tool_pulls_aliases_from_the_discovery_corpus(name):
    """The corpus is the single source of truth; server.py must not restate it."""
    record = aliases_for(name)
    cad = (_component(name).meta or {})["cad"]
    assert cad["acad"] == list(record.acad)
    assert cad["synonyms"] == list(record.synonyms)
    assert cad["synonyms"], "the transaction tools do carry synonyms"


async def test_cad_tool_meta_reaches_the_wire(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    cad = tools["transaction_rollback"].meta["cad"]
    assert cad["cost"] == "destructive"
    assert "rollback" in cad["synonyms"]


async def test_cad_tool_rejects_an_unknown_cost():
    with pytest.raises(ValueError, match="cost"):
        server.cad_tool(summary="x", cost="somewhat-dangerous")


async def test_cad_tool_requires_the_registration_below_it():
    """A misordered decorator must fail loudly at import, not silently no-op."""
    decorator = server.cad_tool(summary="x", cost="read")

    async def never_registered(ctx=None) -> dict:
        return {}

    with pytest.raises(RuntimeError, match="not registered"):
        decorator(never_registered)


# ── @cad_tool coverage: the whole surface, not a sample ─────────────────────
#
# The twin of the alias-corpus gate in tests/test_discovery_aliases.py. That one
# proves every registered tool has an alias record; this one proves every
# registered tool has a discovery card, and that the card agrees with the
# annotations the same tool already publishes. Both derive their subject from
# the live registry, so a tool added without the metadata fails here instead of
# shipping with a summary silently scraped off its docstring.


def _cad(tool) -> dict | None:
    cad = (tool.meta or {}).get("cad")
    return cad if isinstance(cad, dict) else None


async def test_every_registered_tool_carries_cad_meta():
    """Registering a tool without @cad_tool must fail this gate."""
    missing = sorted(t.name for t in await server._registered_tools() if _cad(t) is None)
    assert not missing, f"tools with no @cad_tool metadata: {missing}"


async def test_every_cad_summary_is_a_single_non_empty_line():
    """A card is one line; a summary with a newline in it would break the format."""
    bad: list[str] = []
    for tool in await server._registered_tools():
        summary = (_cad(tool) or {}).get("summary")
        if not isinstance(summary, str) or not summary.strip() or "\n" in summary:
            bad.append(f"{tool.name}:{summary!r}")
    assert not bad, f"summaries that are not one usable line: {bad}"


async def test_no_authored_summary_is_long_enough_to_be_truncated():
    """A hand-written summary that overruns the budget would ship rendered as "...".

    The serializer's truncation exists for the docstring fallback, not as a
    licence to write a paragraph into @cad_tool.
    """
    overlong = sorted(
        (t.name, len((_cad(t) or {}).get("summary") or ""))
        for t in await server._registered_tools()
        if len((_cad(t) or {}).get("summary") or "") > MAX_SUMMARY_CHARS
    )
    assert not overlong, f"summaries the serializer would truncate: {overlong}"


async def test_every_cad_cost_is_a_known_value():
    bad = sorted(
        f"{t.name}:{(_cad(t) or {}).get('cost')!r}"
        for t in await server._registered_tools()
        if (_cad(t) or {}).get("cost") not in server.CAD_TOOL_COSTS
    )
    assert not bad, f"costs outside CAD_TOOL_COSTS: {bad}"


async def test_a_read_only_tool_is_costed_read():
    """``readOnlyHint`` is what a client gates on; the cost must not contradict it."""
    bad: list[str] = []
    for tool in await server._registered_tools():
        annotations = tool.annotations
        if annotations is not None and annotations.readOnlyHint:
            cost = (_cad(tool) or {}).get("cost")
            if cost != "read":
                bad.append(f"{tool.name}:{cost!r}")
    assert not bad, f"readOnlyHint tools not costed 'read': {bad}"


async def test_a_destructive_tool_is_never_costed_read_or_safe():
    """The other half of the agreement: an erase must not look harmless."""
    bad: list[str] = []
    for tool in await server._registered_tools():
        annotations = tool.annotations
        if annotations is not None and annotations.destructiveHint:
            cost = (_cad(tool) or {}).get("cost")
            if cost in ("read", "safe"):
                bad.append(f"{tool.name}:{cost!r}")
    assert not bad, f"destructiveHint tools costed as harmless: {bad}"


async def test_only_the_raw_passthroughs_are_costed_escape():
    """'escape' means 'this hands your string to AutoCAD', and nothing else."""
    escapes = sorted(
        t.name for t in await server._registered_tools() if (_cad(t) or {}).get("cost") == "escape"
    )
    assert escapes == ["system_run_command", "system_run_lisp"]


async def test_no_tool_restates_its_aliases_in_server_py():
    """@cad_tool copies the corpus; server.py must never hand-write acad/synonyms."""
    bad: list[str] = []
    for tool in await server._registered_tools():
        cad = _cad(tool) or {}
        record = aliases_for(tool.name)
        expected_acad = list(record.acad) if record else []
        expected_synonyms = list(record.synonyms) if record else []
        if cad.get("acad") != expected_acad or cad.get("synonyms") != expected_synonyms:
            bad.append(tool.name)
    assert not bad, f"cad metadata out of step with discovery.aliases: {bad}"


# ── tags are load-bearing: _tool_groups() feeds system_about ────────────────


def _tags_declared_in_source() -> dict[str, set[str]]:
    """Every tool's tags as literally written in the @mcp.tool(...) call.

    An independent derivation of the input to _tool_groups(): if @cad_tool (or
    anything else) rewrote a registered tool's tags, the live grouping would
    stop matching what server.py declares.
    """
    tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
    declared: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "tool"
                and isinstance(func.value, ast.Name)
                and func.value.id == "mcp"
            ):
                continue
            name = node.name
            tags: set[str] = set()
            for keyword in decorator.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    name = keyword.value.value
                if keyword.arg == "tags" and isinstance(keyword.value, ast.Set):
                    tags = {
                        element.value
                        for element in keyword.value.elts
                        if isinstance(element, ast.Constant)
                    }
            declared[name] = tags
    return declared


async def test_registered_tags_are_exactly_the_ones_written_in_source():
    declared = _tags_declared_in_source()
    live = {tool.name: set(tool.tags) for tool in await server._registered_tools()}
    assert live == declared


async def test_tool_groups_is_byte_identical_to_the_source_declared_grouping():
    """_tool_groups() must be reproducible from server.py's own `tags={...}`."""
    expected: dict[str, list[str]] = {}
    for name, tags in _tags_declared_in_source().items():
        label = "other"
        for tag in server._GROUP_TAG_PRIORITY:
            if tag in tags:
                label = server._GROUP_TAG_LABELS[tag]
                break
        expected.setdefault(label, []).append(name)
    for names in expected.values():
        names.sort()

    assert await server._tool_groups() == dict(sorted(expected.items()))


async def test_tool_group_sizes_are_unchanged():
    """Frozen snapshot of the surface (154 tools, 19 groups).

    Taken before @cad_tool landed at 131 tools; `batch` moved 2 -> 3 when
    v1.5.0's `cad_batch` joined `entity_batch_create`/`entity_batch_modify`, and
    `layouts` moved 4 -> 12 with M6's layout/viewport lifecycle. That group
    gained `entity_change_space` too: it is tagged `layout` *and* `modify`, and
    `_GROUP_TAG_PRIORITY` ranks `layout` higher, so CHSPACE files under layouts
    rather than splitting a ninth tool off into entity_modification. Every other
    number here has been unchanged since the snapshot was taken.
    """
    sizes = {label: len(names) for label, names in (await server._tool_groups()).items()}
    assert sizes == {
        "analysis": 12,
        "batch": 3,
        "blocks": 8,
        "corner_ops": 4,
        "dimensions": 5,
        "drawing": 11,
        "engineering": 10,
        "entity_creation": 18,
        "entity_modification": 15,
        "entity_query": 8,
        "layers": 14,
        "layouts": 12,
        "premium": 12,
        "solids": 5,
        "system": 7,
        "templates": 2,
        "transactions": 3,
        "validation": 1,
        "view": 4,
    }
    assert sum(sizes.values()) == 154
