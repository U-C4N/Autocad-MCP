"""AutoCAD MCP Pro – FastMCP 3.0 server.

Dual engine: pywin32 COM API (live AutoCAD) + ezdxf (headless file ops).
The exact tool count is reported dynamically via system_status / system_about.

Usage:
    python server.py                          # STDIO (default)
    fastmcp run server.py:mcp --transport http --port 8000
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from dataclasses import asdict
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import StaticTokenVerifier
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.logging import LoggingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware
from fastmcp.tools.tool import ToolResult
from pydantic import Field

import config
from backends.base import BlockInfo, EntityInfo, LayerInfo
from discovery.aliases import aliases_for
from engineering.fits import fit_lookup
from engineering.measure import is_self_intersecting, polygon_area_perimeter
from security import sanitize_command, sanitize_lisp, validate_path
from version import __version__

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("autocad_mcp")

# ---------------------------------------------------------------------------
# Backend auto-detection
# ---------------------------------------------------------------------------

_WIN32 = sys.platform == "win32"


def _detect_autocad_running() -> bool:
    if not _WIN32:
        return False
    try:
        import win32gui

        result = {"found": False}

        def _cb(hwnd, _):
            if "AutoCAD" in win32gui.GetWindowText(hwnd) and win32gui.IsWindowVisible(hwnd):
                result["found"] = True
                return False
            return True

        win32gui.EnumWindows(_cb, None)
        return result["found"]
    except Exception as exc:
        log.debug("AutoCAD detection failed: %s", exc)
        return False


async def _make_backend():
    """Create the best available backend."""
    # Through the setting, not straight off os.environ: reading the environment
    # here is what let config.settings.backend drift into a control that
    # controlled nothing, so a test believing it had pinned the headless engine
    # could reach a live AutoCAD instead. The property reads the environment
    # live, so nothing about startup ordering changes.
    backend_env = config.settings.backend

    if backend_env == "ezdxf":
        from backends.ezdxf_backend import EzdxfBackend

        b = EzdxfBackend()
        await b.connect()
        return b

    if backend_env in ("auto", "com"):
        if _WIN32:
            try:
                from backends.com_backend import ComBackend

                b = ComBackend()
                await b.connect()
                log.info("Using COM backend (live AutoCAD control)")
                return b
            except Exception as exc:
                log.warning("COM backend init failed (%s)", exc)
                if backend_env == "com":
                    raise RuntimeError(f"COM backend requested but failed: {exc}") from exc
        else:
            log.warning("COM backend requires Windows; falling back to ezdxf")

    from backends.ezdxf_backend import EzdxfBackend

    b = EzdxfBackend()
    await b.connect()
    log.info("Using ezdxf backend (headless mode)")
    return b


# ---------------------------------------------------------------------------
# Lifespan – backend singleton
# ---------------------------------------------------------------------------


@lifespan
async def autocad_lifespan(server):
    """Initialize AutoCAD backend on server start, clean up on stop."""
    log.info("Initializing AutoCAD MCP Pro...")
    if config.settings.dangerous_commands_enabled:
        log.warning(
            "⚠ DANGEROUS_COMMANDS_ENABLED=true — command and LISP sanitization is "
            "DISABLED. The server will execute any AutoCAD command or LISP expression "
            "the client sends. Do NOT enable this on a network-reachable instance."
        )
    try:
        await _apply_tool_profile()
    except Exception as exc:  # profile trouble must never block startup
        log.warning("Tool profile could not be applied: %s", exc)
    try:
        backend = await _make_backend()
        log.info("Backend ready: %s", backend.name)
        yield {"backend": backend}
    except Exception as exc:
        log.error("Backend initialization failed: %s", exc)
        yield {"backend": None, "init_error": str(exc)}
    finally:
        log.info("AutoCAD MCP Pro shutting down")


# ---------------------------------------------------------------------------
# Middleware: audit log for destructive operations
# ---------------------------------------------------------------------------


class AuditMiddleware(Middleware):
    """Log all tool calls with timing for audit trail."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool_name = context.message.name
        start = time.monotonic()
        try:
            result = await call_next(context)
            elapsed = (time.monotonic() - start) * 1000
            # A refusal comes back as a *returned* error result, not a raise
            # (see CapabilityRefusalMiddleware), so control flow alone would
            # log it as TOOL OK.
            if getattr(result, "is_error", False):
                log.warning("TOOL ERR %-40s %6.1fms  (refused)", tool_name, elapsed)
            else:
                log.info("TOOL OK  %-40s %6.1fms", tool_name, elapsed)
            return result
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            log.warning("TOOL ERR %-40s %6.1fms  %s", tool_name, elapsed, exc)
            raise


def _backend_name_or_none(context) -> str | None:
    """Which engine refused, for the refusal payload. Must never raise."""
    try:
        backend = context.fastmcp_context.lifespan_context.get("backend")
        return getattr(backend, "name", None)
    except Exception:  # a label is never worth breaking a refusal over
        return None


class CapabilityRefusalMiddleware(Middleware):
    """Carry a typed capability refusal across the JSON-RPC boundary.

    ``FastMCP.call_tool`` rewraps a non-``FastMCPError`` as
    ``ToolError(f"Error calling tool {name!r}: {e}") from e``. The ``from e``
    keeps the cause alive in-process — which is the only reason ``cad_batch``
    can classify a refusal — but ``__cause__`` does not serialise, so a remote
    client used to get an English sentence and had to substring-match it to tell
    "this backend cannot" from "your arguments were wrong".

    Middleware runs outside that wrap with the cause chain still intact, so this
    is the last place the type is knowable. ``ToolResult(is_error=True)`` is the
    only channel that carries both a machine-readable payload *and* the error
    flag; rebasing the exception on ``FastMCPError`` carries neither, which is
    why no tool function changes here.

    R32 added a sibling kind. A quarantined document is *not* a capability
    boundary — the engine can draw a circle perfectly well, the document it would
    draw into is untrusted — so it gets ``kind: "quarantined"`` and carries the
    abandoned-call record instead of a capability key.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        try:
            return await call_next(context)
        except Exception as exc:
            capability = _batch_capability_of(exc)
            quarantine = None if capability else _batch_quarantine_of(exc)
            if capability is None and quarantine is None:
                raise  # not ours — leave it byte-for-byte as it was
            message = _refusal_message(exc)
            payload = {
                "ok": False,
                "error": message,
                "tool": context.message.name,
                "backend": _backend_name_or_none(context),
            }
            if capability is not None:
                payload["kind"] = "unsupported"
                payload["capability"] = capability
            else:
                payload["kind"] = "quarantined"
                payload["quarantine"] = quarantine
            return ToolResult(
                content=message,  # prose-only clients still get a full sentence
                structured_content=payload,
                is_error=True,
            )


# ---------------------------------------------------------------------------
# HTTP auth
# ---------------------------------------------------------------------------

_auth = (
    StaticTokenVerifier(
        tokens={
            config.settings.mcp_auth_token: {
                "client_id": "mcp-http-client",
                "scopes": ["mcp:read", "mcp:write"],
            }
        }
    )
    if config.settings.mcp_auth_token
    else None
)

# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="AutoCAD MCP Pro",
    auth=_auth,
    instructions="""
AutoCAD MCP Pro provides complete AutoCAD automation through a large tool surface.

DUAL ENGINE:
  - COM backend: Live AutoCAD control (requires AutoCAD running on Windows)
  - ezdxf backend: Headless DXF file operations (no AutoCAD needed)

WORKFLOW:
  1. drawing_open / drawing_new  → open or create a drawing
  2. layer_create               → set up layers
  3. entity_create_*            → draw geometry
  4. analysis_entity_stats      → inspect drawing
  5. view_screenshot            → see current state
  6. drawing_save               → save your work

ENGINEERING WORKFLOW (parts requiring real production drawings):
  1. drawing_new           → auto-bootstraps engineering layers + linetypes
  2. titleblock_apply_iso_a3 → standard ISO 7200 border + title (NEVER hand-draw the title)
  3. gear_draw_helical_front_view / gear_draw_spur_front_view
                           → parametric gear front view (NEVER hand-draw teeth or invent the title)
  4. gear_draw_section_aa  → parametric cross-section
  5. dimension_*           → real DIMENSION entities (NEVER use text+leader fakes)
  6. drawing_finalize      → 8-step validator + save + screenshot;
                             cannot complete without this; raises if invalid.

CRITICAL: For engineering drawings, NEVER manually draw gear teeth, keyways,
or section hatches with primitive line/circle calls. Use the gear_*, keyway_*,
and titleblock_* tools — they are deterministic and produce production-quality
output. The title text is whatever you pass to titleblock_apply_iso_a3 — do
not invent variants like "HELICAL SPUR GEAR".

HANDLES: Every entity has a unique handle (hex string). Use it for
entity_get, entity_move, entity_delete, entity_set_properties, etc.

TRANSACTIONS: Use transaction_begin / transaction_commit / transaction_rollback
to group operations with undo support.

Set AUTOCAD_MCP_BACKEND=com|ezdxf|auto environment variable to force a backend.
""",
    lifespan=autocad_lifespan,
)

mcp.add_middleware(ErrorHandlingMiddleware())
mcp.add_middleware(AuditMiddleware())
mcp.add_middleware(TimingMiddleware())
mcp.add_middleware(LoggingMiddleware())
# Added last on purpose: first-added is outermost, so this sits innermost —
# closest to the call_tool wrap, where the refusal's __cause__ chain is still
# intact. Anywhere further out and AuditMiddleware would log the raise before
# it becomes a result.
mcp.add_middleware(CapabilityRefusalMiddleware())


# ---------------------------------------------------------------------------
# Tool discovery mode (DISCOVERY_MODE)
# ---------------------------------------------------------------------------
# "off"    the whole catalog is advertised (default, backwards compatible)
# "search" list_tools returns a search tool + a call_tool proxy instead, and
#          the catalog is reached through discovery/transform.py — which
#          indexes each tool's AutoCAD command aliases as well as its
#          description, so BPOLY/QSELECT/MATCHPROP resolve at all.
#
# Tool *counts* are unaffected: system_status / system_about / _tool_groups /
# _apply_tool_profile all read the untransformed registry (_registered_tools).

DISCOVERY_MODES = ("off", "search")

_discovery_transform = None


def _apply_discovery_mode(mode: str | None = None) -> str:
    """Attach or detach the tool-search transform. Returns the mode applied.

    Idempotent: re-applying removes the previous transform first, so a mode
    switch never stacks two search interfaces on one server.
    """
    global _discovery_transform
    selected = (mode or config.settings.discovery_mode or "off").lower().strip()
    if selected not in DISCOVERY_MODES:
        log.warning(
            "Unknown DISCOVERY_MODE %r - falling back to 'off'. Valid modes: %s.",
            selected,
            ", ".join(DISCOVERY_MODES),
        )
        selected = "off"
    if _discovery_transform is not None:
        try:
            mcp._transforms.remove(_discovery_transform)
        except (AttributeError, ValueError) as exc:  # pragma: no cover - layout change
            log.debug("Could not detach the discovery transform: %s", exc)
        _discovery_transform = None
    if selected == "search":
        # Imported lazily so "off" never pays for it, matching how the backend
        # modules are kept out of import time.
        from discovery.transform import CadSearchTransform

        _discovery_transform = CadSearchTransform()
        mcp.add_transform(_discovery_transform)
        log.info("Tool discovery: search transform attached (clients see search_tools/call_tool)")
    return selected


_apply_discovery_mode()


# ---------------------------------------------------------------------------
# Backend access helper
# ---------------------------------------------------------------------------


def _backend(ctx: Context):
    """Get the backend from lifespan context, raising ToolError if not ready."""
    b = ctx.lifespan_context.get("backend")
    if b is None:
        err = ctx.lifespan_context.get("init_error", "Backend not initialized")
        raise ToolError(f"AutoCAD backend unavailable: {err}")
    return b


def _backend_supports(backend, capability: str) -> bool:
    """True unless `backend` explicitly reports `capability` as unsupported.

    Unknown keys and probe failures answer True: a capability map that has not
    heard of a feature is not evidence the feature is missing, and refusing on
    that basis would break any backend older than the key.
    """
    try:
        feature = backend.capabilities().features.get(capability)
    except Exception as exc:  # a capability probe must never break a tool call
        log.debug("capability probe for %r failed: %s", capability, exc)
        return True
    return feature is None or bool(feature.supported)


def _is_dwg_path(path: str) -> bool:
    """True when `path` names a DWG file. The extension is authoritative (N2)."""
    from pathlib import Path as _P

    return _P(path).suffix.lower() == ".dwg"


# T0.2 — the read-side twin of the backend's DWG write refusal. ezdxf detects
# format by content, so a DXF saved under a .dwg name (what pre-1.5.0
# drawing_save produced) opens fine and gets reported as an opened DWG, while a
# genuine DWG dies with ezdxf's "is not a DXF file" — which blames the file
# instead of the backend. Refuse both, in the backend's own vocabulary.
_DWG_READ_REFUSAL = (
    "drawing_open: backend '{backend}' has no DWG support (capability 'dwg' is "
    "unsupported), so '{path}' is refused rather than opened as something else: "
    "a DXF saved under a .dwg name would be parsed and reported back as a DWG. "
    "Open the .dxf instead, or switch to the live COM backend "
    "(AUTOCAD_MCP_BACKEND=com, needs Windows + AutoCAD) to read real DWG."
)


def _dc(obj) -> dict:
    """Convert dataclass to dict (recursively)."""
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, list):
        return [_dc(i) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Result shaping — field projection + compact rows
# ---------------------------------------------------------------------------
#
# Discovery cost is only half the token bill; this is the other half. A default
# ``entity_list(limit=100)`` over a 300-entity drawing is ~24 kB of JSON the
# model has to read, and roughly a third of it is the ``bounding_box`` inside
# ``properties`` that nobody asked for.
#
# ONE mechanism, two parameters, spelled identically on every collection tool
# (:data:`SHAPED_RESULT_TOOLS`) — a per-tool parameter set would rot, so
# tests/test_result_projection.py gates the registry against that list in both
# directions and compares the two parameter schemas across every carrier:
#
#   ``fields=[...]``  projection. Column subset, caller's order preserved,
#                     validated against the row's dataclass. A typo is an error
#                     naming the valid fields — a projection that answered
#                     ``{"handel": null}`` a hundred times would read as "this
#                     drawing has no handles", which is worse than no feature.
#   ``compact=True``  columnar envelope instead of a list of dicts, so the key
#                     names are paid for once rather than once per row. It is
#                     also the only shape with somewhere to put ``total`` /
#                     ``truncated`` / ``next_offset``.
#
# Omit both and the value is byte-identical to what 1.4.0 returned. That is the
# compatibility contract, and every pre-existing test in the suite is its gate;
# it is also why the honest-truncation metadata could not simply be bolted onto
# the default list return.
#
# `_dc()` stays the only dataclass→dict step: shaping runs *after* it, over
# plain dicts, so the convention is untouched and a backend that grows a field
# gets it projectable for free.

#: Separator for reaching one key inside a row's nested ``properties`` payload.
FIELD_PATH_SEP = "."

#: The two parameters, in the order they appear in every shaped signature.
RESULT_SHAPE_PARAMS = ("fields", "compact")

#: Every tool carrying the mechanism. Closed on purpose and gated both ways:
#: a shaped tool missing from here fails the gate, and a name here that is not
#: a registered carrier fails it too.
SHAPED_RESULT_TOOLS = (
    "analysis_find_in_region",
    "analysis_select_by_layer",
    "analysis_select_by_type",
    "block_find_references",
    "block_list",
    "entity_array_polar",
    "entity_array_rectangular",
    "entity_list",
    "entity_select_smart",
    "layer_list",
    "selection_get",
)

# Both descriptions are paid once per tool in every uncached catalog, so they
# are written to the shortest length that still prevents a wasted round trip.
_FIELDS_DESC = (
    "Project to these fields, in this order (e.g. ['handle','type','layer']); "
    "'properties.<key>' reaches one nested value. Omit for the full record; an "
    "unknown name errors and lists the valid ones."
)
_COMPACT_DESC = (
    "Return a columnar {fields, rows, count, offset, total, truncated, next_offset} "
    "envelope instead of dicts: much cheaper per row, and the only shape that "
    "reports truncation."
)

#: Shared annotations so the two parameters cannot drift tool to tool.
ResultFields = Annotated[list[str] | None, Field(default=None, description=_FIELDS_DESC)]
ResultCompact = Annotated[bool, Field(default=False, description=_COMPACT_DESC)]


def _nested_keys(rows: list[dict], root: str) -> list[str]:
    """Every sub-key present under `root` anywhere in `rows`."""
    keys: set[str] = set()
    for row in rows:
        value = row.get(root)
        if isinstance(value, dict):
            keys.update(value)
    return sorted(keys)


def _resolve_fields(fields: list[str], rows: list[dict], spec: type, tool: str) -> list[str]:
    """Validate a projection against `spec`'s fields; return it de-duplicated.

    Validation is the whole point: an unvalidated projection turns a typo into a
    column of nulls, and a column of nulls reads like data.

    Nested sub-keys are checked against the keys this result actually carries,
    which is data-dependent by nature (a CIRCLE has ``radius``, a LINE does
    not). A sub-key present on *some* rows is legitimate and yields null on the
    rest; one present on *none* is the typo case and raises. An empty result is
    exempt — there is no column there to misread.
    """
    roots = tuple(spec.__dataclass_fields__)
    valid = ", ".join(roots)
    resolved: list[str] = []
    for raw in fields:
        name = str(raw).strip()
        root, sep, sub = name.partition(FIELD_PATH_SEP)
        if root not in roots:
            raise ToolError(
                f"{tool}: unknown field {name!r}. Valid fields ({spec.__name__}): {valid}."
            )
        if sep:
            if not sub:
                raise ToolError(
                    f"{tool}: field {name!r} names no sub-key. "
                    f"Write '{root}.<key>', or just '{root}' for the whole object."
                )
            available = _nested_keys(rows, root)
            if rows and sub not in available:
                raise ToolError(
                    f"{tool}: {root!r} carries no {sub!r} in this result. "
                    f"Available {root} keys here: {', '.join(available) or '(none)'}. "
                    f"Valid top-level fields: {valid}."
                )
        if name not in resolved:
            resolved.append(name)
    if not resolved:
        raise ToolError(
            f"{tool}: fields=[] would project nothing. Omit the parameter for the "
            f"full record, or name fields: {valid}."
        )
    return resolved


def _pluck(row: dict, name: str):
    """One projected cell. Missing nested keys are null, never a KeyError."""
    root, sep, sub = name.partition(FIELD_PATH_SEP)
    value = row.get(root)
    if not sep:
        return value
    return value.get(sub) if isinstance(value, dict) else None


def _shape_rows(
    rows,
    *,
    spec: type,
    fields: list[str] | None,
    compact: bool,
    tool: str,
    total: int | None = None,
    offset: int = 0,
) -> list[dict] | dict:
    """Apply the shared projection/compaction to one collection result.

    `rows` are backend dataclasses (or anything ``_dc`` handles); `spec` is the
    dataclass they are, which is what makes the field validation possible on an
    empty result — the valid names come from the type, not from the data.

    `total` is the size of the matching set *before* paging or capping, so the
    envelope can state ``truncated`` as a fact. Passing ``None`` (unknown) makes
    ``truncated`` null rather than false: not knowing is not the same as knowing
    there is nothing more, and only one of those is safe to read as complete.
    """
    dicts = [_dc(row) for row in rows]
    # `is None` rather than falsiness: an explicit `fields=[]` is a caller
    # mistake worth an error, not a silent fall-through to the full record.
    if fields is None and not compact:
        return dicts  # the 1.4.0 value, byte for byte
    names = (
        list(spec.__dataclass_fields__)
        if fields is None
        else _resolve_fields(fields, dicts, spec, tool)
    )
    projected = [{name: _pluck(row, name) for name in names} for row in dicts]
    if not compact:
        return projected
    count = len(projected)
    truncated = None if total is None else (offset + count) < total
    return {
        "fields": names,
        "rows": [[row[name] for name in names] for row in projected],
        "count": count,
        "offset": offset,
        "total": total,
        "truncated": truncated,
        "next_offset": offset + count if truncated else None,
    }


def _local_tool_components() -> list:
    """Tool components straight out of the local registry, synchronously.

    The untransformed source of truth for the whole file: ``_registered_tools``
    reads it, and ``cad_tool`` needs it at import time (before any event loop
    exists) to attach metadata to a just-registered tool.
    """
    components = mcp._local_provider._components
    return [value for key, value in components.items() if key.startswith("tool:")]


async def _registered_tools() -> list:
    """Return the FunctionTool objects registered on the server, *untransformed*.

    This deliberately reads the local component registry rather than any
    ``list_tools`` accessor, so that "what is registered" stays independent of
    every transform layer FastMCP can put in front of the catalog. There are two
    such layers and they surface at different accessors:

      * *server-level* — ``mcp.add_transform(...)``, which is what
        ``_apply_discovery_mode("search")`` installs — is applied by the public
        ``mcp.list_tools()``, i.e. at the wire, where a client sees
        ``search_tools``/``call_tool`` in place of the catalog.
        ``mcp._list_tools()`` sits upstream of that chain and still lists the
        full inventory, so search mode as shipped does not move these counts.
      * *provider-level* — ``provider.add_transform(...)`` — is applied inside
        ``Provider.list_tools()``, which ``mcp._list_tools()`` aggregates. That
        one *does* rewrite ``_list_tools()`` wholesale: a transform returning a
        couple of synthetic entries makes it report a couple of tools.

    The component registry sits below both, which is what every caller here
    needs. Over a rewritten catalog, ``system_status`` / ``system_about`` would
    report the advertised surface instead of the inventory, ``_tool_groups()``
    would bucket only what survived the rewrite, and ``_apply_tool_profile()``
    would compute a collapsed ``registered - enabled`` set, never call
    ``mcp.disable()``, and silently turn TOOL_PROFILE into a no-op. Reading
    pre-transform is what keeps "registered" and "advertised" two separate,
    stable numbers under any transform layer, present or future.

    Ordered fallbacks, both untransformed, then ``mcp._list_tools()`` as a last
    resort should a future FastMCP rename the private layout — last precisely
    because it is the one accessor here a provider-level transform can reach.
    Never raises; returns [] when the registry is genuinely unknown so callers
    can label/omit rather than surface a bogus count.
    """
    try:
        tools = _local_tool_components()
        if tools:
            return tools
    except Exception as exc:
        log.debug("private tool registry unavailable, falling back: %s", exc)
    try:
        # Same source, different accessor: LocalProvider._list_tools() is the
        # pre-transform base that Provider.list_tools() decorates.
        tools = await mcp._local_provider._list_tools()
        if tools:
            return list(tools)
    except Exception as exc:
        log.debug("private local listing unavailable, falling back: %s", exc)
    try:  # pragma: no cover - only reachable if both private paths break
        return list(await mcp._list_tools())
    except Exception as exc:
        log.debug("public _list_tools() failed: %s", exc)
        return []


async def _registered_tool_count() -> int | None:
    """Number of @mcp.tool registrations currently on the server.

    Backed by the untransformed registry (see ``_registered_tools``), so a
    search transform cannot shrink the reported inventory. Returns ``None``
    (never -1) when the count is genuinely unknown so system_status /
    system_about can omit/label it rather than report a fake value.
    """
    tools = await _registered_tools()
    return len(tools) if tools else None


# Priority order used to bucket each registered tool into exactly one group
# for the system_about breakdown (R15). The first matching tag wins, so more
# specific tags (engineering/premium/corner) take precedence over the broad
# entity/drawing tags. Tools whose tags match none fall into "other".
_GROUP_TAG_PRIORITY = (
    "pid",
    "mech",
    "sheet",
    "style",
    "pagesetup",
    "environment",
    "engineering",
    "premium",
    "corner",
    "batch",
    "template",
    "dimension",
    "transaction",
    "layout",
    "solid",
    "view",
    "validation",
    "analysis",
    "block",
    "layer",
    "linetype",
    "create",
    "modify",
    "query",
    "drawing",
    "system",
)

# Map the winning tag to a human-readable group label for the breakdown.
_GROUP_TAG_LABELS = {
    "pid": "pid",
    "mech": "mechanical",
    "sheet": "sheet",
    "style": "styles",
    "pagesetup": "page_setup",
    "environment": "environment",
    "engineering": "engineering",
    "premium": "premium",
    "corner": "corner_ops",
    "batch": "batch",
    "template": "templates",
    "dimension": "dimensions",
    "transaction": "transactions",
    "layout": "layouts",
    "solid": "solids",
    "view": "view",
    "analysis": "analysis",
    "validation": "validation",
    "block": "blocks",
    "layer": "layers",
    "linetype": "layers",
    "create": "entity_creation",
    "modify": "entity_modification",
    "query": "entity_query",
    "drawing": "drawing",
    "system": "system",
}


async def _tool_groups() -> dict:
    """Derive the system_about per-group tool breakdown dynamically from each
    registered tool's tags (R15).

    Replaces the hand-maintained static dict that omitted ~22 tools (all
    engineering/premium/corner-ops + drawing_close) and misfiled
    entity_delete_many under entity_creation. Because this reads the live
    ``tags={...}`` on each @mcp.tool, the breakdown can never drift again.
    """
    groups: dict[str, list[str]] = {}
    for tool in await _registered_tools():
        name = getattr(tool, "name", None)
        if not name:
            continue
        tags = getattr(tool, "tags", None) or set()
        label = "other"
        for tag in _GROUP_TAG_PRIORITY:
            if tag in tags:
                label = _GROUP_TAG_LABELS[tag]
                break
        groups.setdefault(label, []).append(name)
    for names in groups.values():
        names.sort()
    return dict(sorted(groups.items()))


# ---------------------------------------------------------------------------
# Discovery metadata channel (@cad_tool)
# ---------------------------------------------------------------------------

# How much damage a call can do, for a client that wants to gate or colour-code
# the surface before calling it:
#   read        pure query, no document mutation
#   safe        mutates, trivially reversible (settings, current layer, view)
#   mutate      creates or edits geometry
#   destructive deletes or overwrites geometry/files
#   escape      raw AutoCAD command / LISP passthrough
CAD_TOOL_COSTS = ("read", "safe", "mutate", "destructive", "escape")


def cad_tool(*, summary: str, cost: str):
    """Attach discovery metadata to the ``mcp.tool`` registration below it.

    Usage — this decorator sits **above** the registration::

        @cad_tool(summary="Open a rollback checkpoint.", cost="mutate")
        @mcp.tool          # ... with its usual annotations= / tags= arguments
        async def transaction_begin(ctx: Context = None) -> dict: ...

    (The example spells the decorator without its argument list only because
    the release-consistency gate counts literal registrations by scanning this
    file's text, and a docstring example would inflate the count.)

    The ordering is deliberate. ``tags=`` and ``annotations=`` stay on the
    ``mcp.tool`` call, so this wrapper never receives them and cannot perturb
    ``_tool_groups()`` — whose frozen 20-tag priority list feeds system_about.
    It also leaves the registration itself literally intact for that gate.

    Writes a single ``cad`` key into the tool's MCP ``meta`` channel:

      ``summary``   one compact line, for a search hit's preview
      ``cost``      one of :data:`CAD_TOOL_COSTS`
      ``acad``      AutoCAD command names, from ``discovery.aliases``
      ``synonyms``  natural-language phrasings, from ``discovery.aliases``

    The alias corpus is imported, never restated here: one edit site, and the
    coverage gate over all registered tools already lives with the corpus.
    """
    if cost not in CAD_TOOL_COSTS:
        raise ValueError(f"cad_tool cost must be one of {CAD_TOOL_COSTS}, got {cost!r}")

    def decorate(target):
        # decorator_mode="function" (the default) hands back the function and
        # registers the component; "object" hands back the component itself.
        name = getattr(target, "name", None) or getattr(target, "__name__", "")
        fastmcp_meta = getattr(target, "__fastmcp__", None)
        name = getattr(fastmcp_meta, "name", None) or name
        component = target if hasattr(target, "meta") else None
        if component is None:
            component = next((t for t in _local_tool_components() if t.name == name), None)
        if component is None:
            raise RuntimeError(
                f"@cad_tool({name!r}): tool is not registered. cad_tool must be written "
                "directly above the mcp.tool decorator, never below it or on a bare function."
            )
        record = aliases_for(name)
        if record is None:
            log.warning("No discovery aliases for tool %r; searches will miss it", name)
        meta = dict(component.meta or {})
        meta["cad"] = {
            "summary": summary,
            "cost": cost,
            "acad": list(record.acad) if record else [],
            "synonyms": list(record.synonyms) if record else [],
        }
        component.meta = meta
        return target

    return decorate


# ---------------------------------------------------------------------------
# Tool profiles (capability-aware discovery)
# ---------------------------------------------------------------------------
# Some MCP clients degrade (or hard-cap) when a server exposes 100+ tools.
# TOOL_PROFILE selects how much of the surface is advertised:
#   full — everything (default, backwards compatible)
#   lean — a curated 47-tool drafting/inspection core
# Disabled tools stay registered (system_about still reports the full
# inventory) but are hidden from MCP list_tools and rejected if called.

TOOL_PROFILES = ("lean", "full")

# Profiles that used to exist, mapped to why they went away. Kept so an old
# TOOL_PROFILE value still boots the server (falling back to "full") with a
# warning that names the removed profile instead of a generic "unknown value".
REMOVED_TOOL_PROFILES = {
    "core": (
        "the discovery layer (tool search + AutoCAD command aliases) now solves "
        "the crowded-surface problem it existed for"
    ),
}

LEAN_TOOL_NAMES = frozenset(
    {
        # drawing management
        "drawing_new",
        "drawing_open",
        "drawing_save",
        "drawing_save_as",
        "drawing_export_dxf",
        "drawing_export_pdf",
        "drawing_info",
        "drawing_audit",
        "drawing_close",
        # entity creation
        "entity_create_line",
        "entity_create_circle",
        "entity_create_arc",
        "entity_create_rectangle",
        "entity_create_polyline",
        "entity_create_text",
        "entity_create_mtext",
        # entity modification
        "entity_move",
        "entity_copy",
        "entity_rotate",
        "entity_scale",
        "entity_mirror",
        "entity_offset",
        "entity_delete",
        "entity_set_properties",
        "entity_trim",
        "entity_fillet",
        # query
        "entity_get",
        "entity_list",
        "entity_delete_many",
        # layers
        "layer_create",
        "layer_list",
        "layer_set_current",
        "layer_modify",
        "layer_delete",
        # dimensions
        "dimension_linear",
        "dimension_aligned",
        "dimension_radius",
        "dimension_diameter",
        # view
        "view_zoom_extents",
        "view_screenshot",
        # transactions
        "transaction_begin",
        "transaction_commit",
        "transaction_rollback",
        # system
        "system_status",
        "system_about",
        "system_capabilities",
        # batching — the profile exists for clients with tight tool caps, which
        # are exactly the clients paying the most per turn. Measured, the big
        # lever is turn elimination (9.07x), so a lean surface without cad_batch
        # withholds the saving from the callers who need it most.
        "cad_batch",
        # P&ID — the three tools a lean client needs to draw and read a P&ID
        # (symbol insertion, line drawing, the reader). Still subject to
        # TOOL_PACKS: lean intersects with the enabled packs, so
        # TOOL_PACKS=core hides them from a lean surface too.
        "pid_symbol_insert",
        "pid_line_draw",
        "pid_graph",
        # Styles (track E) — a lean client *uses* a style far more often than
        # it authors one; drawing_apply_standard authors the whole set in one
        # call when it must.
        "dimstyle_set_current",
        "textstyle_set_current",
        "drawing_apply_standard",
        # Page setup (track E) — the two a lean client needs to set the
        # sheet and plot it. Core-pack tools, so TOOL_PACKS=core leaves
        # them on the lean surface; with S's three above, lean is 55.
        "page_setup_apply",
        "batch_plot",
        # Mechanical + sheet (tracks B/G) — the five a lean client needs to
        # draw a part and put it on a sheet. The catalogue, the annotation
        # symbols and the delivery tools stay full-profile: a lean client
        # draws the part and leaves the paperwork to a full one. The first
        # three are `mech`-pack tools, so TOOL_PACKS=core hides them from a
        # lean surface too; sheet_frame and titleblock_apply are core.
        "mech_part_draw",
        "mech_view_add",
        "mech_dimension_part",
        "sheet_frame",
        "titleblock_apply",
    }
)

SOLID_TOOL_NAMES = frozenset(
    {"solid_box", "solid_cylinder", "solid_extrude", "solid_revolve", "solid_boolean"}
)

# Tool packs: vertical domains a client can opt out of advertising. `core` is
# everything not claimed by another pack and is always on. ~300 tools would
# push the full catalog to ~80k idle tokens; a client that only drafts
# mechanically should not pay for the P&ID surface, and a client that never
# talks to a live seat should not pay for the environment surface. Styles and
# page setup (SECTION 18/19) are drafting essentials and stay in core.
TOOL_PACK_NAMES = ("core", "pid", "settings", "mech")
PACK_TOOL_NAMES: dict[str, frozenset[str]] = {
    "pid": frozenset(
        {
            "pid_symbol_list",
            "pid_symbol_insert",
            "pid_line_draw",
            "pid_tag_parse",
            "pid_graph",
            "pid_instrument_index",
            "pid_line_list",
            "pid_equipment_list",
            "pid_from_spec",
        }
    ),
    # SECTION 20 — the 22 environment tools (spec §7, §8.2).
    "settings": frozenset(
        {
            "document_list",
            "document_activate",
            "document_close",
            "layer_state_save",
            "layer_state_restore",
            "layer_state_list",
            "layer_state_delete",
            "view_named_save",
            "view_named_restore",
            "view_named_list",
            "ucs_list",
            "ucs_set",
            "ucs_restore",
            "system_launch",
            "system_preferences_get",
            "system_preferences_set",
            "drawing_properties_get",
            "drawing_properties_set",
            "system_variable_describe",
            "user_pick_point",
            "user_select",
            "system_prompt_message",
        }
    ),
    # SECTIONS 21-23 — the mechanical part drawer, the standard-parts catalogue
    # and the ISO annotation symbols (spec §11). SECTION 24 (the sheet, the
    # parts list, xrefs and DWG) stays in `core`: a frame, a title block, a
    # parts list and an xref are universal drafting, not a vertical.
    "mech": frozenset(
        {
            "mech_part_draw",
            "mech_view_add",
            "mech_dimension_part",
            "mech_hole_pattern",
            "mech_part_from_spec",
            "mech_part_inspect",
            "std_part_list",
            "std_part_insert",
            "std_feature_draw",
            "surface_texture",
            "weld_symbol",
            "centre_marks",
            "section_line",
            "hatch_material",
        }
    ),
}

_active_tool_profile: dict | None = None


def _enabled_packs() -> tuple[set[str], list[str]]:
    """Resolve TOOL_PACKS into (enabled packs, ignored unknown entries).

    ``all`` (or an empty value) enables every pack; a comma list enables the
    named packs on top of ``core``, which can never be dropped. Unknown names
    are ignored with a warning rather than failing startup.
    """
    raw = (config.settings.tool_packs or "all").lower()
    requested = [p.strip() for p in raw.split(",") if p.strip()]
    if not requested or "all" in requested:
        return set(TOOL_PACK_NAMES), []
    enabled = {"core"}
    ignored: list[str] = []
    for pack in requested:
        if pack in TOOL_PACK_NAMES:
            enabled.add(pack)
        else:
            ignored.append(pack)
            log.warning(
                "Unknown TOOL_PACKS entry %r ignored. Valid packs: %s.",
                pack,
                ", ".join(TOOL_PACK_NAMES),
            )
    return enabled, ignored


def _profile_enabled_names(profile: str, registered: set[str]) -> set[str]:
    packs, _ignored = _enabled_packs()
    hidden_by_pack: set[str] = set().union(
        *(names for pack, names in PACK_TOOL_NAMES.items() if pack not in packs)
    )
    enabled = set(registered) - hidden_by_pack
    if profile == "lean":
        enabled &= LEAN_TOOL_NAMES
    if not config.settings.enable_3d:
        # Capability-aware discovery: don't advertise opt-in 3D tools that
        # would only reject the call.
        enabled -= SOLID_TOOL_NAMES
    return enabled


async def _apply_tool_profile(profile: str | None = None) -> dict:
    """Enable/disable registered tools according to the selected profile.

    Runs in the server lifespan (and directly from tests). Re-applying a
    different profile re-enables previously hidden tools first, so profile
    switches are idempotent.
    """
    global _active_tool_profile
    requested = (profile or config.settings.tool_profile or "full").lower().strip()
    selected = requested
    if selected not in TOOL_PROFILES:
        if selected in REMOVED_TOOL_PROFILES:
            log.warning(
                "TOOL_PROFILE %r was removed (%s) - falling back to 'full'. Valid profiles: %s.",
                selected,
                REMOVED_TOOL_PROFILES[selected],
                ", ".join(TOOL_PROFILES),
            )
        else:
            log.warning(
                "Unknown TOOL_PROFILE %r - falling back to 'full'. Valid profiles: %s.",
                selected,
                ", ".join(TOOL_PROFILES),
            )
        selected = "full"
    registered = {tool.name for tool in await _registered_tools() if getattr(tool, "name", None)}
    enabled = _profile_enabled_names(selected, registered)
    disabled = sorted(registered - enabled)
    if enabled:
        mcp.enable(names=set(enabled))
    if disabled:
        mcp.disable(names=set(disabled))
    _active_tool_profile = {
        "profile": selected,
        "registered_count": len(registered),
        "enabled_count": len(enabled),
        "disabled_count": len(disabled),
        "disabled_tools": disabled,
    }
    packs, ignored = _enabled_packs()
    _active_tool_profile["tool_packs"] = {
        "enabled": sorted(packs),
        "available": list(TOOL_PACK_NAMES),
        "ignored": ignored,
    }
    if requested != selected:
        # Surface the fallback where an MCP client can actually see it: a
        # warning on a STDIO server's stderr is routinely invisible.
        _active_tool_profile["requested"] = requested
    log.info(
        "Tool profile '%s': %d enabled, %d hidden",
        selected,
        len(enabled),
        len(disabled),
    )
    return _active_tool_profile


# ---------------------------------------------------------------------------
# ── SECTION 1: Drawing Management (12 tools) ────────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(summary="Read the current drawing's name, path, extents and object counts.", cost="read")
@mcp.tool(
    annotations={"title": "Drawing Info", "readOnlyHint": True},
    tags={"drawing", "query"},
)
async def drawing_info(ctx: Context) -> dict:
    """Get comprehensive metadata for the current drawing.

    Returns: name, path, entity_count, layer_count, block_count,
    extents (min/max), units, version, backend name.
    """
    await ctx.info("Fetching drawing info")
    result = await _backend(ctx).drawing_info()
    return _dc(result)


@cad_tool(
    summary="Start a drawing: blank with the engineering layers, or from a bundled/own template.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "New Drawing", "destructiveHint": False},
    tags={"drawing"},
)
async def drawing_new(
    template: Annotated[
        str | None,
        "Bundled template name (drawing_template_list: iso_a3_mech, iso_a1_arch, iso_a3_pid, "
        "ansi_b_mech, ansi_d_arch) or a path to a .dwt/.dxf template file.",
    ] = None,
    bootstrap: Annotated[
        bool,
        Field(
            description="Auto-load CENTER/HIDDEN/PHANTOM linetypes and create standard "
            "engineering layers (GEOMETRY, DIM, CENTER, HIDDEN, PHANTOM, "
            "HATCH, TEXT, TITLEBLOCK). Disable for vanilla DXF.",
        ),
    ] = True,
    ctx: Context = None,
) -> dict:
    """Create a new drawing, optionally from a template.

    A bundled name resolves to `templates/<name>.dxf` headlessly and
    `templates/<name>.dwt` on the live engine (falling back to the DXF with
    `source: "bundled_dxf"` when no `.dwt` twin is committed); a path is used
    as given. The result carries `template: {name|path, path, source}`.

    On the live engine only a genuine `.dwt` reaches `Documents.Add` —
    measured on AutoCAD 2026, `Add(<file.dxf>)` silently returns the default
    drawing — so a `.dxf` template (bundled or a path) is first converted
    through AutoCAD into a cached `.dwt` and `template.dwt` names it
    (`template.dwt_cached` says whether the conversion was reused). The
    drawing then really carries the template's layers, tabs and page setup.

    Refused before anything is replaced: a bare name that is not in the
    catalogue (the message lists the five), a template path that does not
    exist, and a path outside the allowed directories. With `bootstrap=True`
    (default) the standard engineering linetypes and layers are ensured
    afterwards — idempotent on a template that already has them.
    """
    from pathlib import Path as _P

    from engineering.standards.templates import resolve_template

    backend = _backend(ctx)
    template_info: dict | None = None
    if template is not None:
        try:
            resolved, source = resolve_template(template, backend.name)
        except ValueError as exc:
            raise ToolError(f"drawing_new: {exc}") from exc
        if source == "path":
            resolved = str(validate_path(resolved, allow_write=False))
            if not _P(resolved).is_file():
                raise ToolError(f"drawing_new: template file not found: {resolved}")
            template_info = {"path": resolved, "source": source}
        else:
            template_info = {"name": template.strip().lower(), "path": resolved, "source": source}
        template = resolved
    await ctx.info(f"Creating new drawing (template={template}, bootstrap={bootstrap})")
    raw = await backend.drawing_new(template)
    result = dict(raw) if isinstance(raw, dict) else {"result": raw}
    converted = result.pop("template_dwt", None)
    if template_info is not None:
        if converted:  # the live engine built a .dwt from the .dxf (see ComBackend.drawing_new)
            template_info["dwt"] = converted["path"]
            template_info["dwt_cached"] = bool(converted.get("cached"))
        result["template"] = template_info
    if bootstrap:
        try:
            from engineering import (
                ensure_engineering_layers,
                ensure_standard_linetypes,
            )

            lt_status = await ensure_standard_linetypes(backend)
            layer_status = await ensure_engineering_layers(backend)
            result["bootstrap"] = {"ok": True, "linetypes": lt_status, "layers": layer_status}
        except Exception as exc:
            await ctx.warning(f"Engineering bootstrap failed: {exc}")
            result["bootstrap"] = {"ok": False, "error": str(exc)}
            result["status"] = "degraded"
    return result


@cad_tool(
    summary="Load an existing DXF from disk (DWG only on the live AutoCAD backend).",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Open Drawing"},
    tags={"drawing"},
)
async def drawing_open(
    path: Annotated[
        str,
        "Full path to the .dxf file. .dwg needs a backend that can read it "
        "(the live COM backend); the headless ezdxf backend refuses it.",
    ],
    ctx: Context = None,
) -> dict:
    """Open an existing DXF drawing file (DWG too, on the live COM backend).

    T0.2: a .dwg path is refused up front when the active backend has no `dwg`
    capability. ezdxf sniffs content rather than extensions, so it would parse a
    mislabelled DXF-in-a-.dwg and this tool would answer with a document that
    does not exist in that format.
    """
    validated = validate_path(path, allow_write=False)
    backend = _backend(ctx)
    if _is_dwg_path(str(validated)) and not _backend_supports(backend, "dwg"):
        raise ToolError(_DWG_READ_REFUSAL.format(backend=backend.name, path=validated))
    await ctx.info(f"Opening drawing: {validated}")
    await ctx.report_progress(0, 100)
    result = await backend.drawing_open(str(validated))
    await ctx.report_progress(100, 100)
    return result


@cad_tool(summary="Write the drawing back to its file, or to a path you give.", cost="mutate")
@mcp.tool(
    annotations={"title": "Save Drawing"},
    tags={"drawing"},
)
async def drawing_save(
    path: Annotated[str | None, "Optional save path; uses current path if omitted"] = None,
    ctx: Context = None,
) -> dict:
    """Save the current drawing. Optionally specify a new path."""
    if path is not None:
        validated = validate_path(path, allow_write=True)
        path = str(validated)
    await ctx.info("Saving drawing")
    return await _backend(ctx).drawing_save(path)


@cad_tool(
    summary="Save a copy under a new name; the extension picks DXF, DWG or DWT.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Save As"},
    tags={"drawing"},
)
async def drawing_save_as(
    path: Annotated[str, "Full destination path including extension"],
    format: Annotated[
        str,
        "Output format override: dwg, dxf, dwt. Default: derive "
        "from the path extension (the extension is authoritative).",
    ] = "",
    ctx: Context = None,
) -> dict:
    """Save current drawing to a new path/format (DWG, DXF, or DWT template).

    The on-disk format is derived from the file extension so the bytes always
    match the name (N2) — e.g. 'part.dxf' writes DXF, not DWG. `format` overrides
    only when the path has no recognised extension.
    """
    validated = validate_path(path, allow_write=True)
    from pathlib import Path as _P

    ext = _P(str(validated)).suffix.lstrip(".").lower()
    fmt = ext if ext in ("dxf", "dwg", "dwt") else (format.lower() or "dxf")
    await ctx.info(f"Saving as {fmt}: {validated}")
    return await _backend(ctx).drawing_save_as(str(validated), fmt)


@cad_tool(summary="Write a DXF interchange copy of the drawing.", cost="mutate")
@mcp.tool(
    annotations={"title": "Export DXF"},
    tags={"drawing", "export"},
)
async def drawing_export_dxf(
    path: Annotated[str, "Output .dxf file path"],
    ctx: Context = None,
) -> dict:
    """Export the current drawing as a DXF file."""
    validated = validate_path(path, allow_write=True)
    await ctx.info(f"Exporting DXF: {validated}")
    return await _backend(ctx).drawing_export_dxf(str(validated))


@cad_tool(summary="Plot the drawing, or one paper-space layout, to PDF.", cost="mutate")
@mcp.tool(
    annotations={"title": "Export PDF"},
    tags={"drawing", "export"},
)
async def drawing_export_pdf(
    path: Annotated[str, "Output .pdf file path"],
    layout: Annotated[
        str | None,
        "Paper-space layout to plot (default: model space). COM plots the layout "
        "natively incl. viewport content; ezdxf renders the layout's own entities.",
    ] = None,
    ctx: Context = None,
) -> dict:
    """Export the current drawing (or a paper-space layout) to PDF."""
    validated = validate_path(path, allow_write=True)
    await ctx.info(f"Exporting PDF: {validated}")
    await ctx.report_progress(0, 100)
    result = await _backend(ctx).drawing_export_pdf(str(validated), layout=layout)
    await ctx.report_progress(100, 100)
    return result


@cad_tool(
    summary="Strip unused layers, blocks, linetypes and styles out of the file.",
    cost="destructive",
)
@mcp.tool(
    annotations={"title": "Purge Drawing"},
    tags={"drawing", "cleanup"},
)
async def drawing_purge(ctx: Context = None) -> dict:
    """Purge all unused objects (layers, blocks, linetypes, styles) from the drawing."""
    await ctx.info("Purging drawing")
    return await _backend(ctx).drawing_purge()


@cad_tool(summary="Scan the drawing for structural errors and repair what it finds.", cost="mutate")
@mcp.tool(
    annotations={"title": "Audit Drawing", "readOnlyHint": False},
    tags={"drawing", "cleanup"},
)
async def drawing_audit(ctx: Context = None) -> dict:
    """Audit the drawing: repair every fixable structural problem, and report it.

    This mutates the drawing. `fixes` lists repairs that have ALREADY been
    applied, so save afterwards to keep them; `errors` lists problems that could
    not be repaired. On the live COM backend AutoCAD applies repairs but hands
    back no counts, so they arrive as null with `detail: "unavailable"` rather
    than as zero.
    """
    await ctx.info("Auditing drawing")
    return await _backend(ctx).drawing_audit()


@cad_tool(summary="Close the drawing, saving first unless you say otherwise.", cost="destructive")
@mcp.tool(
    annotations={"title": "Close Drawing", "destructiveHint": True},
    tags={"drawing"},
)
async def drawing_close(
    save: Annotated[bool, "Save the drawing before closing"] = True,
    ctx: Context = None,
) -> dict:
    """Close the active document. If save is True (default), the drawing is
    saved to its current path before closing; an untitled document is closed
    and `warning` says its changes were discarded (headless) or the call is
    refused (live, where the alternative is a modal Save dialog). The
    active-document case of `document_close`; after the last document is
    closed, call drawing_new or drawing_open before any other tool."""
    await ctx.info(f"Closing drawing (save={save})")
    return await _backend(ctx).drawing_close(save)


@cad_tool(summary="Step back one operation.", cost="safe")
@mcp.tool(
    annotations={"title": "Undo", "destructiveHint": False, "idempotentHint": False},
    tags={"drawing", "undo"},
)
async def drawing_undo(ctx: Context = None) -> dict:
    """Undo the last drawing operation.

    On the live COM backend this is AutoCAD's own undo. The headless backend has
    no journal, so a step is a full DXF snapshot and history is **off by
    default** — set `EZDXF_UNDO_DEPTH` to the number of steps you want. Measured
    cost of switching it on: 37x on entity creation (0.18 -> 6.65 ms per call).
    For a single checkpoint around a risky sequence, `transaction_begin` /
    `transaction_rollback` is far cheaper.

    Drawing something after an undo discards the redo branch, as in AutoCAD.
    """
    return await _backend(ctx).drawing_undo()


@cad_tool(summary="Reapply the operation you just undid.", cost="safe")
@mcp.tool(
    annotations={"title": "Redo", "destructiveHint": False},
    tags={"drawing", "undo"},
)
async def drawing_redo(ctx: Context = None) -> dict:
    """Reapply the operation you just undid.

    Same history as `drawing_undo`, so the headless backend needs
    `EZDXF_UNDO_DEPTH` set. Anything drawn after an undo discards the redo
    branch — otherwise redo would restore a state that never existed, with
    geometry you had removed reappearing beside geometry you drew afterwards.
    """
    return await _backend(ctx).drawing_redo()


# ---------------------------------------------------------------------------
# ── SECTION 2: Entity Creation (19 tools) ───────────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(summary="Draw a straight line between two points.", cost="mutate")
@mcp.tool(
    annotations={"title": "Create Line", "readOnlyHint": False},
    tags={"entity", "create"},
)
async def entity_create_line(
    x1: Annotated[float, "Start X coordinate"],
    y1: Annotated[float, "Start Y coordinate"],
    x2: Annotated[float, "End X coordinate"],
    y2: Annotated[float, "End Y coordinate"],
    z1: Annotated[float, "Start Z coordinate (default 0)"] = 0.0,
    z2: Annotated[float, "End Z coordinate (default 0)"] = 0.0,
    layer: Annotated[str | None, "Layer name (default: current layer)"] = None,
    color: Annotated[int | None, "ACI color code 1-255, 256=ByLayer, 0=ByBlock"] = None,
    linetype: Annotated[str | None, "Linetype name (e.g. 'DASHED', 'CENTER')"] = None,
    ctx: Context = None,
) -> dict:
    """Create a line from (x1,y1) to (x2,y2). Returns entity info with handle."""
    await ctx.debug(f"Creating line ({x1},{y1}) → ({x2},{y2})")
    result = await _backend(ctx).entity_create_line(x1, y1, x2, y2, z1, z2, layer, color, linetype)
    return _dc(result)


@cad_tool(summary="Draw a circle: a hole, a bore, a pitch circle.", cost="mutate")
@mcp.tool(
    annotations={"title": "Create Circle", "readOnlyHint": False},
    tags={"entity", "create"},
)
async def entity_create_circle(
    cx: Annotated[float, "Center X"],
    cy: Annotated[float, "Center Y"],
    radius: Annotated[float, Field(description="Circle radius", gt=0)],
    layer: Annotated[str | None, "Layer name"] = None,
    color: Annotated[int | None, "ACI color code"] = None,
    ctx: Context = None,
) -> dict:
    """Create a circle at (cx, cy) with given radius."""
    await ctx.debug(f"Creating circle center=({cx},{cy}) r={radius}")
    result = await _backend(ctx).entity_create_circle(cx, cy, radius, layer, color)
    return _dc(result)


@cad_tool(summary="Draw a circular arc from a centre, a radius and two angles.", cost="mutate")
@mcp.tool(
    annotations={"title": "Create Arc", "readOnlyHint": False},
    tags={"entity", "create"},
)
async def entity_create_arc(
    cx: Annotated[float, "Center X"],
    cy: Annotated[float, "Center Y"],
    radius: Annotated[float, Field(description="Arc radius", gt=0)],
    start_angle: Annotated[float, "Start angle in degrees (0 = right, CCW positive)"],
    end_angle: Annotated[float, "End angle in degrees"],
    layer: Annotated[str | None, "Layer name"] = None,
    color: Annotated[int | None, "ACI color code"] = None,
    ctx: Context = None,
) -> dict:
    """Create a circular arc. Angles are in degrees, measured counter-clockwise from the positive X axis."""
    await ctx.debug(f"Creating arc center=({cx},{cy}) r={radius} {start_angle}°→{end_angle}°")
    result = await _backend(ctx).entity_create_arc(
        cx, cy, radius, start_angle, end_angle, layer, color
    )
    return _dc(result)


@cad_tool(
    summary="Draw a connected outline or closed profile through a list of points.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Create Polyline", "readOnlyHint": False},
    tags={"entity", "create"},
)
async def entity_create_polyline(
    points: Annotated[list[list[float]], "List of [x, y] coordinate pairs"],
    closed: Annotated[bool, "Whether to close the polyline"] = False,
    layer: Annotated[str | None, "Layer name"] = None,
    color: Annotated[int | None, "ACI color code"] = None,
    ctx: Context = None,
) -> dict:
    """Create a lightweight 2D polyline through the given points.

    Example: points=[[0,0],[100,0],[100,100],[0,100]], closed=true → rectangle
    """
    await ctx.debug(f"Creating polyline with {len(points)} points, closed={closed}")
    result = await _backend(ctx).entity_create_polyline(points, closed, layer, color)
    return _dc(result)


@cad_tool(summary="Draw a closed rectangle from two opposite corners.", cost="mutate")
@mcp.tool(
    annotations={"title": "Create Rectangle", "readOnlyHint": False, "idempotentHint": False},
    tags={"entity", "create"},
)
async def entity_create_rectangle(
    x1: Annotated[float, "First corner X"],
    y1: Annotated[float, "First corner Y"],
    x2: Annotated[float, "Opposite corner X"],
    y2: Annotated[float, "Opposite corner Y"],
    layer: Annotated[str | None, "Layer name"] = None,
    color: Annotated[int | None, "ACI color code"] = None,
    ctx: Context = None,
) -> dict:
    """Create a closed rectangular polyline between two corner points.

    Convenience wrapper around entity_create_polyline.
    """
    await ctx.debug(f"Creating rectangle ({x1},{y1}) - ({x2},{y2})")
    pts = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
    result = await _backend(ctx).entity_create_polyline(pts, closed=True, layer=layer, color=color)
    return _dc(result)


@cad_tool(summary="Place a single-line text label.", cost="mutate")
@mcp.tool(
    annotations={"title": "Create Text", "readOnlyHint": False},
    tags={"entity", "create", "annotation"},
)
async def entity_create_text(
    text: Annotated[str, "Text content to display"],
    x: Annotated[float, "Insertion point X"],
    y: Annotated[float, "Insertion point Y"],
    height: Annotated[float, Field(description="Text height in drawing units", gt=0)] = 2.5,
    rotation: Annotated[float, "Rotation angle in degrees"] = 0.0,
    layer: Annotated[str | None, "Layer name"] = None,
    color: Annotated[int | None, "ACI color code"] = None,
    ctx: Context = None,
) -> dict:
    """Create a single-line text entity (DTEXT/TEXT)."""
    await ctx.debug(f"Creating text: '{text[:30]}' at ({x},{y})")
    result = await _backend(ctx).entity_create_text(text, x, y, height, rotation, layer, color)
    return _dc(result)


@cad_tool(summary="Place a wrapped paragraph note in a text box of a given width.", cost="mutate")
@mcp.tool(
    annotations={"title": "Create MText", "readOnlyHint": False},
    tags={"entity", "create", "annotation"},
)
async def entity_create_mtext(
    text: Annotated[
        str, "Text content (supports \\P for paragraph breaks, {\\H...;} for formatting)"
    ],
    x: Annotated[float, "Insertion point X"],
    y: Annotated[float, "Insertion point Y"],
    width: Annotated[float, "Text box width in drawing units"] = 100.0,
    height: Annotated[float, "Character height in drawing units"] = 2.5,
    rotation: Annotated[float, "Rotation in degrees, CCW from +X"] = 0.0,
    layer: Annotated[str | None, "Layer name"] = None,
    color: Annotated[int | None, "ACI color code"] = None,
    ctx: Context = None,
) -> dict:
    """Create a multi-line text entity (MTEXT) with word-wrap at the specified width."""
    await ctx.debug(f"Creating mtext at ({x},{y}) w={width}")
    result = await _backend(ctx).entity_create_mtext(
        text, x, y, width, height, rotation, layer, color
    )
    return _dc(result)


@cad_tool(
    summary="Place a parts list, BOM or schedule as a table with headers and rows.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Create Table", "readOnlyHint": False},
    tags={"entity", "create", "annotation", "table"},
)
async def entity_create_table(
    x: Annotated[float, "Top-left X coordinate"],
    y: Annotated[float, "Top-left Y coordinate"],
    rows: Annotated[list[list[str]], "Data rows; every row must have the same length"],
    headers: Annotated[list[str] | None, "Optional column header row"] = None,
    column_widths: Annotated[list[float] | None, "Optional explicit widths per column"] = None,
    row_height: Annotated[float, Field(default=7.0, gt=0)] = 7.0,
    text_height: Annotated[float, Field(default=2.5, gt=0)] = 2.5,
    title: Annotated[str | None, "Optional title row"] = None,
    layer: Annotated[str, "Target layer"] = "TEXT",
    ctx: Context = None,
) -> dict:
    """Create a native COM table or an explicitly-labelled ezdxf composite table."""
    result = await _backend(ctx).entity_create_table(
        x, y, rows, headers, column_widths, row_height, text_height, title, layer
    )
    return _dc(result)


@cad_tool(summary="Add a callout: an arrow with a note on the end.", cost="mutate")
@mcp.tool(
    annotations={"title": "Create Multileader", "readOnlyHint": False},
    tags={"entity", "create", "annotation", "leader"},
)
async def leader_create_mleader(
    points: Annotated[list[list[float]], "Leader vertices as [x, y] pairs"],
    text: Annotated[str, "Leader annotation text"],
    text_height: Annotated[float, Field(default=2.5, gt=0)] = 2.5,
    landing_gap: Annotated[float, Field(default=1.0, ge=0)] = 1.0,
    arrow_size: Annotated[float, Field(default=2.5, gt=0)] = 2.5,
    layer: Annotated[str, "Target layer"] = "DIM",
    ctx: Context = None,
) -> dict:
    """Create a native COM MLeader or an explicitly-labelled ezdxf composite."""
    result = await _backend(ctx).leader_create_mleader(
        points, text, text_height, landing_gap, arrow_size, layer
    )
    return _dc(result)


@cad_tool(
    summary="Fill a closed boundary with a section pattern such as ANSI31 or SOLID.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Create Hatch", "readOnlyHint": False},
    tags={"entity", "create"},
)
async def entity_create_hatch(
    pattern: Annotated[str, "Hatch pattern name: SOLID, ANSI31, ANSI32, STEEL, GRAVEL, etc."],
    boundary_points: Annotated[list[list[float]], "Closed boundary as list of [x, y] points"],
    scale: Annotated[float, Field(description="Pattern scale factor", gt=0)] = 1.0,
    angle: Annotated[float, "Pattern rotation angle in degrees"] = 0.0,
    layer: Annotated[str | None, "Layer name"] = None,
    color: Annotated[int | None, "ACI color code"] = None,
    ctx: Context = None,
) -> dict:
    """Create a hatch fill pattern inside a closed boundary polygon."""
    await ctx.debug(f"Creating hatch pattern={pattern} scale={scale}")
    result = await _backend(ctx).entity_create_hatch(
        pattern, boundary_points, scale, angle, layer, color
    )
    return _dc(result)


@cad_tool(summary="Draw a smooth NURBS curve through fit points.", cost="mutate")
@mcp.tool(
    annotations={"title": "Create Spline", "readOnlyHint": False},
    tags={"entity", "create"},
)
async def entity_create_spline(
    fit_points: Annotated[list[list[float]], "List of [x, y] fit points the spline passes through"],
    layer: Annotated[str | None, "Layer name"] = None,
    color: Annotated[int | None, "ACI color code"] = None,
    ctx: Context = None,
) -> dict:
    """Create a NURBS spline curve passing through the specified fit points."""
    await ctx.debug(f"Creating spline with {len(fit_points)} fit points")
    result = await _backend(ctx).entity_create_spline(fit_points, layer, color)
    return _dc(result)


@cad_tool(
    summary="Draw an ellipse from its centre, major axis vector and axis ratio.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Create Ellipse", "readOnlyHint": False},
    tags={"entity", "create"},
)
async def entity_create_ellipse(
    cx: Annotated[float, "Center X"],
    cy: Annotated[float, "Center Y"],
    major_x: Annotated[float, "Major axis endpoint X (relative to center)"],
    major_y: Annotated[float, "Major axis endpoint Y (relative to center)"],
    ratio: Annotated[
        float, Field(description="Minor-to-major axis ratio (0 < ratio ≤ 1)", gt=0, le=1)
    ] = 0.5,
    layer: Annotated[str | None, "Layer name"] = None,
    color: Annotated[int | None, "ACI color code"] = None,
    ctx: Context = None,
) -> dict:
    """Create an ellipse. major_x/major_y define the major axis vector from the center."""
    await ctx.debug(
        f"Creating ellipse center=({cx},{cy}) major=({major_x},{major_y}) ratio={ratio}"
    )
    result = await _backend(ctx).entity_create_ellipse(
        cx, cy, major_x, major_y, ratio, layer, color
    )
    return _dc(result)


@cad_tool(summary="Place a point marker (node) at a coordinate.", cost="mutate")
@mcp.tool(
    annotations={"title": "Create Point", "readOnlyHint": False},
    tags={"entity", "create"},
)
async def entity_create_point(
    x: Annotated[float, "Point X coordinate"],
    y: Annotated[float, "Point Y coordinate"],
    layer: Annotated[str | None, "Layer name"] = None,
    color: Annotated[int | None, "ACI color code"] = None,
    ctx: Context = None,
) -> dict:
    """Create a point marker entity at (x, y)."""
    result = await _backend(ctx).entity_create_point(x, y, layer, color)
    return _dc(result)


@cad_tool(
    summary="Drop an instance of an existing block definition into the drawing.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Insert Block Reference", "readOnlyHint": False},
    tags={"entity", "create", "block"},
)
async def entity_create_block_ref(
    name: Annotated[str, "Block definition name (must exist in drawing)"],
    x: Annotated[float, "Insertion X"],
    y: Annotated[float, "Insertion Y"],
    scale_x: Annotated[float, "X scale factor"] = 1.0,
    scale_y: Annotated[float, "Y scale factor"] = 1.0,
    rotation: Annotated[float, "Rotation angle in degrees"] = 0.0,
    layer: Annotated[str | None, "Layer name"] = None,
    ctx: Context = None,
) -> dict:
    """Insert a block reference (instance of an existing block definition).

    Refused before any write, on both engines: a name that is not a block
    definition in this drawing (`block 'X' is not defined` — check
    `block_list`), a layout block (`*Model_Space` / `*Paper_Space`, a
    reference cycle) and a non-string name. The same gate as `block_insert`;
    this tool is the no-attributes form.
    """
    await ctx.debug(f"Inserting block '{name}' at ({x},{y})")
    result = await _backend(ctx).entity_create_block_ref(
        name, x, y, scale_x, scale_y, rotation, layer
    )
    return _dc(result)


@cad_tool(summary="Replace a hatch's fill with a two-colour gradient.", cost="mutate")
@mcp.tool(
    annotations={"title": "Set Hatch Gradient", "destructiveHint": False},
    tags={"entity", "create"},
)
async def hatch_set_gradient(
    handle: Annotated[str, "Handle of an existing HATCH."],
    color1: Annotated[list[int], "Start colour as [r, g, b]."],
    color2: Annotated[list[int], "End colour as [r, g, b]."],
    rotation: Annotated[float, "Gradient angle in degrees."] = 0.0,
    centered: Annotated[float, "0 = one-sided, 1 = centred."] = 0.0,
    one_color: Annotated[bool, "Blend color1 towards the background instead of color2."] = False,
    tint: Annotated[float, "Tint value used with one_color (0-1)."] = 0.0,
    name: Annotated[str, "Gradient name: LINEAR, CYLINDER, CURVED, SPHERICAL, HEMISPHERICAL."] = (
        "LINEAR"
    ),
    ctx: Context = None,
) -> dict:
    """Fill a hatch with a gradient instead of a pattern."""
    return await _backend(ctx).hatch_set_gradient(
        handle, color1, color2, rotation, centered, one_color, tint, name
    )


@cad_tool(summary="Change a hatch's pattern, scale, angle, colour or island style.", cost="mutate")
@mcp.tool(
    annotations={"title": "Edit Hatch", "destructiveHint": False},
    tags={"entity", "create"},
)
async def hatch_edit(
    handle: Annotated[str, "Handle of an existing HATCH."],
    pattern: Annotated[str, "New pattern name. Empty leaves it alone."] = "",
    scale: Annotated[float | None, "New pattern scale (> 0)."] = None,
    angle: Annotated[float | None, "New pattern angle in degrees."] = None,
    color: Annotated[int | None, "New ACI colour."] = None,
    style: Annotated[str, "Island style: normal, outer or ignore. Empty leaves it alone."] = "",
    ctx: Context = None,
) -> dict:
    """Edit an existing hatch in place.

    Omitted parameters are left alone — a partial edit that resets the rest is
    data loss. `changed` reports which attributes actually moved, so re-setting
    a value to what it already was comes back as an empty list rather than a
    false positive.
    """
    return await _backend(ctx).hatch_edit(handle, pattern, scale, angle, color, style)


@cad_tool(summary="Add a boundary path to a hatch, arcs and ellipses included.", cost="mutate")
@mcp.tool(
    annotations={"title": "Add Hatch Boundary", "destructiveHint": False},
    tags={"entity", "create"},
)
async def hatch_add_boundary(
    handle: Annotated[str, "Handle of an existing HATCH."],
    edges: Annotated[
        list[dict],
        "Typed edges: {'type':'line','start':[x,y],'end':[x,y]} | "
        "{'type':'arc','center':[x,y],'radius':r,'start_angle':a,'end_angle':b,'ccw':true} | "
        "{'type':'ellipse','center':[x,y],'major_axis':[x,y],'ratio':r}",
    ],
    ctx: Context = None,
) -> dict:
    """Add one boundary path built from typed edges - an island inside the hatch.

    Typed edges exist because a boundary that only accepts vertex lists
    silently straightens every curve it is given. Every edge is validated
    before any is written, so a malformed list refuses instead of leaving a
    half-built path. Both engines: headlessly the edges become an edge path;
    on a live seat each becomes a temporary LINE, true ARC or ELLIPSE appended
    as an inner loop and then deleted (a chain of lines becomes one polyline).
    A loop that does not close is refused by AutoCAD and nothing is added.
    """
    return await _backend(ctx).hatch_add_boundary(handle, edges)


@cad_tool(summary="Mask whatever is behind a closed polygon on the sheet.", cost="mutate")
@mcp.tool(
    annotations={"title": "Create Wipeout", "destructiveHint": False},
    tags={"entity", "create"},
)
async def entity_create_wipeout(
    points: Annotated[list[list[float]], "Closed polygon as [[x, y], ...]; at least 3 points."],
    layer: Annotated[str, "Target layer. Empty uses the current layer."] = "",
    ctx: Context = None,
) -> dict:
    """Create a WIPEOUT that hides drawing content behind its outline.

    Refuses fewer than three points: a zero-area mask hides nothing while
    reporting success.
    """
    return await _backend(ctx).entity_create_wipeout(points, layer or None)


@cad_tool(summary="Draw a revision cloud around an area.", cost="mutate")
@mcp.tool(
    annotations={"title": "Create Revision Cloud", "destructiveHint": False},
    tags={"entity", "create"},
)
async def entity_create_revcloud(
    points: Annotated[list[list[float]], "Path corners as [[x, y], ...]."],
    segment_length: Annotated[
        float,
        Field(gt=0, description="Approximate arc length of each cloud bump, in drawing units."),
    ],
    layer: Annotated[str, "Target layer. Empty uses the current layer."] = "",
    closed: Annotated[bool, "Close the path back to the first point."] = True,
    ctx: Context = None,
) -> dict:
    """Draw a revision cloud: a polyline whose every segment carries an arc.

    A `segment_length` longer than the shortest edge is refused — the result
    would carry no arcs at all and would be a plain polyline reported as a
    cloud.
    """
    return await _backend(ctx).entity_create_revcloud(points, segment_length, layer or None, closed)


# ---------------------------------------------------------------------------
# ── SECTION 3: Dimensions (5 tools) ─────────────────────────────────────────
# ---------------------------------------------------------------------------


def _fit_to_tolerances(
    fit: str | None,
    nominal: float,
    tol_upper: float | None,
    tol_lower: float | None,
    tol_mode: str,
    text_override: str | None,
) -> tuple[float | None, float | None, str, str | None]:
    """Resolve an ISO 286 fit code (e.g. 'H7') into the tolerance contract.

    Mutually exclusive with explicit tol_* values. Returns
    (tol_upper, tol_lower, tol_mode, text_override) where tol_lower follows
    the build_dim_override convention (positive = minus deviation).
    """
    if not fit:
        return tol_upper, tol_lower, tol_mode, text_override
    if tol_upper is not None or tol_lower is not None or (tol_mode or "none") != "none":
        raise ToolError("Pass either fit=<ISO 286 code> or explicit tol_* values, not both.")
    try:
        deviation = fit_lookup(fit, nominal)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return (
        deviation.upper_mm,
        -deviation.lower_mm,
        "deviation",
        text_override or f"<> {deviation.code}",
    )


@cad_tool(
    summary="Dimension a horizontal or vertical size, with an ISO 129 tolerance or ISO 286 fit.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Linear Dimension", "readOnlyHint": False},
    tags={"annotation", "dimension"},
)
async def dimension_linear(
    x1: Annotated[float, "First extension line origin X"],
    y1: Annotated[float, "First extension line origin Y"],
    x2: Annotated[float, "Second extension line origin X"],
    y2: Annotated[float, "Second extension line origin Y"],
    dim_x: Annotated[float, "Dimension line position X"],
    dim_y: Annotated[float, "Dimension line position Y"],
    rotation: Annotated[float, "Angle of the measured dimension (0=horizontal, 90=vertical)"] = 0.0,
    layer: Annotated[str | None, "Layer name"] = None,
    tol_upper: Annotated[float | None, "Upper deviation (mm), e.g. 0.02 for +0.02"] = None,
    tol_lower: Annotated[float | None, "Lower deviation (mm), e.g. 0.01 for -0.01"] = None,
    tol_mode: Annotated[
        str,
        Field(
            default="none",
            description="ISO 129 tolerance display: none | symmetric (±tol_upper) | "
            "deviation (+tol_upper/-tol_lower) | limit (stacked limits) | basic (boxed).",
        ),
    ] = "none",
    text_override: Annotated[
        str | None, "Replace the measured text ('<>' keeps the measurement)"
    ] = None,
    fit: Annotated[
        str | None,
        "ISO 286 fit code (e.g. 'H7', 'g6', 'js9'); resolves deviations from the "
        "authored tables for the measured nominal. Mutually exclusive with tol_*.",
    ] = None,
    ctx: Context = None,
) -> dict:
    """Create a linear dimension, optionally toleranced (ISO 129 or ISO 286 fit)."""
    angle = math.radians(rotation)
    nominal = abs((x2 - x1) * math.cos(angle) + (y2 - y1) * math.sin(angle))
    tol_upper, tol_lower, tol_mode, text_override = _fit_to_tolerances(
        fit, nominal, tol_upper, tol_lower, tol_mode, text_override
    )
    result = await _backend(ctx).dimension_linear(
        x1,
        y1,
        x2,
        y2,
        dim_x,
        dim_y,
        rotation,
        layer,
        tol_upper,
        tol_lower,
        tol_mode,
        text_override,
    )
    return _dc(result)


@cad_tool(summary="Dimension the true distance between two points, along a slope.", cost="mutate")
@mcp.tool(
    annotations={"title": "Aligned Dimension", "readOnlyHint": False},
    tags={"annotation", "dimension"},
)
async def dimension_aligned(
    x1: Annotated[float, "First point X"],
    y1: Annotated[float, "First point Y"],
    x2: Annotated[float, "Second point X"],
    y2: Annotated[float, "Second point Y"],
    dim_x: Annotated[float, "Dimension line position X"],
    dim_y: Annotated[float, "Dimension line position Y"],
    layer: Annotated[str | None, "Layer name"] = None,
    ctx: Context = None,
) -> dict:
    """Create an aligned dimension that measures the true distance between two points."""
    result = await _backend(ctx).dimension_aligned(x1, y1, x2, y2, dim_x, dim_y, layer)
    return _dc(result)


@cad_tool(summary="Dimension the included angle between two rays from a vertex.", cost="mutate")
@mcp.tool(
    annotations={"title": "Angular Dimension", "readOnlyHint": False},
    tags={"annotation", "dimension"},
)
async def dimension_angular(
    vertex_x: Annotated[float, "Angle vertex X"],
    vertex_y: Annotated[float, "Angle vertex Y"],
    x1: Annotated[float, "First ray endpoint X"],
    y1: Annotated[float, "First ray endpoint Y"],
    x2: Annotated[float, "Second ray endpoint X"],
    y2: Annotated[float, "Second ray endpoint Y"],
    text_x: Annotated[float, "Dimension text position X"],
    text_y: Annotated[float, "Dimension text position Y"],
    layer: Annotated[str | None, "Layer name"] = None,
    ctx: Context = None,
) -> dict:
    """Create an angular dimension measuring the angle between two lines from a vertex."""
    result = await _backend(ctx).dimension_angular(
        vertex_x, vertex_y, x1, y1, x2, y2, text_x, text_y, layer
    )
    return _dc(result)


@cad_tool(summary="Call out the radius of a circle or arc, optionally toleranced.", cost="mutate")
@mcp.tool(
    annotations={"title": "Radius Dimension", "readOnlyHint": False},
    tags={"annotation", "dimension"},
)
async def dimension_radius(
    center_x: Annotated[float, "Circle/arc center X"],
    center_y: Annotated[float, "Circle/arc center Y"],
    chord_x: Annotated[float, "Point on the circle/arc X (determines angle)"],
    chord_y: Annotated[float, "Point on the circle/arc Y"],
    leader_length: Annotated[float, "Length of the leader line"] = 10.0,
    layer: Annotated[str | None, "Layer name"] = None,
    tol_upper: Annotated[float | None, "Upper deviation (mm)"] = None,
    tol_lower: Annotated[float | None, "Lower deviation (mm)"] = None,
    tol_mode: Annotated[
        str,
        Field(
            default="none",
            description="ISO 129 tolerance display: none | symmetric | deviation | limit | basic.",
        ),
    ] = "none",
    text_override: Annotated[
        str | None, "Replace the measured text ('<>' keeps the measurement)"
    ] = None,
    fit: Annotated[
        str | None,
        "ISO 286 fit code resolved for the measured radius value. Mutually exclusive with tol_*.",
    ] = None,
    ctx: Context = None,
) -> dict:
    """Create a radius dimension for a circle or arc, optionally toleranced."""
    nominal = math.dist((center_x, center_y), (chord_x, chord_y))
    tol_upper, tol_lower, tol_mode, text_override = _fit_to_tolerances(
        fit, nominal, tol_upper, tol_lower, tol_mode, text_override
    )
    result = await _backend(ctx).dimension_radius(
        center_x,
        center_y,
        chord_x,
        chord_y,
        leader_length,
        layer,
        tol_upper,
        tol_lower,
        tol_mode,
        text_override,
    )
    return _dc(result)


@cad_tool(
    summary="Call out a hole or shaft diameter, optionally to a fit such as H7.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Diameter Dimension", "readOnlyHint": False},
    tags={"annotation", "dimension"},
)
async def dimension_diameter(
    x1: Annotated[float, "First point on diameter X"],
    y1: Annotated[float, "First point on diameter Y"],
    x2: Annotated[float, "Second point on diameter (opposite side) X"],
    y2: Annotated[float, "Second point on diameter Y"],
    leader_length: Annotated[float, "Leader line length"] = 10.0,
    layer: Annotated[str | None, "Layer name"] = None,
    tol_upper: Annotated[float | None, "Upper deviation (mm)"] = None,
    tol_lower: Annotated[float | None, "Lower deviation (mm)"] = None,
    tol_mode: Annotated[
        str,
        Field(
            default="none",
            description="ISO 129 tolerance display: none | symmetric | deviation | limit | basic.",
        ),
    ] = "none",
    text_override: Annotated[
        str | None, "Replace the measured text ('<>' keeps the measurement)"
    ] = None,
    fit: Annotated[
        str | None,
        "ISO 286 fit code (e.g. 'H7' hole / 'g6' shaft) resolved for the measured "
        "diameter. Mutually exclusive with tol_*.",
    ] = None,
    ctx: Context = None,
) -> dict:
    """Create a diameter dimension for a circle, optionally toleranced (e.g. ⌀20 H7)."""
    nominal = math.dist((x1, y1), (x2, y2))
    tol_upper, tol_lower, tol_mode, text_override = _fit_to_tolerances(
        fit, nominal, tol_upper, tol_lower, tol_mode, text_override
    )
    result = await _backend(ctx).dimension_diameter(
        x1,
        y1,
        x2,
        y2,
        leader_length,
        layer,
        tol_upper,
        tol_lower,
        tol_mode,
        text_override,
    )
    return _dc(result)


# ---------------------------------------------------------------------------
# ── SECTION 4: Entity Modification (18 tools) ───────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(summary="Shift an entity by a displacement vector.", cost="mutate")
@mcp.tool(
    annotations={"title": "Move Entity", "readOnlyHint": False, "destructiveHint": False},
    tags={"entity", "modify"},
)
async def entity_move(
    handle: Annotated[str, "Entity handle (hex string from entity_list or entity_create_*)"],
    dx: Annotated[float, "X displacement"],
    dy: Annotated[float, "Y displacement"],
    dz: Annotated[float, "Z displacement"] = 0.0,
    ctx: Context = None,
) -> dict:
    """Move an entity by the specified displacement vector (dx, dy, dz)."""
    await ctx.debug(f"Moving entity {handle} by ({dx},{dy},{dz})")
    return await _backend(ctx).entity_move(handle, dx, dy, dz)


@cad_tool(summary="Duplicate an entity and offset the copy.", cost="mutate")
@mcp.tool(
    annotations={"title": "Copy Entity", "readOnlyHint": False, "destructiveHint": False},
    tags={"entity", "modify"},
)
async def entity_copy(
    handle: Annotated[str, "Entity handle to copy"],
    dx: Annotated[float, "X displacement for the copy"],
    dy: Annotated[float, "Y displacement for the copy"],
    dz: Annotated[float, "Z displacement"] = 0.0,
    ctx: Context = None,
) -> dict:
    """Copy an entity and move the copy by (dx, dy, dz). Returns info of the new copy."""
    await ctx.debug(f"Copying entity {handle}")
    result = await _backend(ctx).entity_copy(handle, dx, dy, dz)
    return _dc(result)


@cad_tool(summary="Turn an entity about a base point by an angle in degrees.", cost="mutate")
@mcp.tool(
    annotations={"title": "Rotate Entity", "readOnlyHint": False, "destructiveHint": False},
    tags={"entity", "modify"},
)
async def entity_rotate(
    handle: Annotated[str, "Entity handle"],
    base_x: Annotated[float, "Rotation base point X"],
    base_y: Annotated[float, "Rotation base point Y"],
    angle_deg: Annotated[float, "Rotation angle in degrees (positive = counter-clockwise)"],
    ctx: Context = None,
) -> dict:
    """Rotate an entity around a base point by the specified angle."""
    await ctx.debug(f"Rotating entity {handle} by {angle_deg}° around ({base_x},{base_y})")
    return await _backend(ctx).entity_rotate(handle, base_x, base_y, angle_deg)


@cad_tool(summary="Resize an entity uniformly about a base point.", cost="mutate")
@mcp.tool(
    annotations={"title": "Scale Entity", "readOnlyHint": False, "destructiveHint": False},
    tags={"entity", "modify"},
)
async def entity_scale(
    handle: Annotated[str, "Entity handle"],
    base_x: Annotated[float, "Scale base point X"],
    base_y: Annotated[float, "Scale base point Y"],
    factor: Annotated[float, Field(description="Scale factor (>1 enlarges, <1 shrinks)", gt=0)],
    ctx: Context = None,
) -> dict:
    """Scale an entity uniformly from a base point."""
    return await _backend(ctx).entity_scale(handle, base_x, base_y, factor)


@cad_tool(
    summary="Reflect an entity across a mirror line, keeping or dropping the original.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Mirror Entity", "readOnlyHint": False, "destructiveHint": False},
    tags={"entity", "modify"},
)
async def entity_mirror(
    handle: Annotated[str, "Entity handle"],
    x1: Annotated[float, "Mirror line first point X"],
    y1: Annotated[float, "Mirror line first point Y"],
    x2: Annotated[float, "Mirror line second point X"],
    y2: Annotated[float, "Mirror line second point Y"],
    delete_original: Annotated[bool, "Delete original after mirroring"] = False,
    ctx: Context = None,
) -> dict:
    """Mirror an entity across a line defined by two points. Returns the mirrored copy."""
    result = await _backend(ctx).entity_mirror(handle, x1, y1, x2, y2, delete_original)
    return _dc(result)


@cad_tool(
    summary="Make a parallel copy of a line, circle or polyline at a set distance.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Offset Entity", "readOnlyHint": False, "destructiveHint": False},
    tags={"entity", "modify"},
)
async def entity_offset(
    handle: Annotated[str, "Entity handle (line, circle, or polyline)"],
    distance: Annotated[float, "Offset distance (positive = outward/right)"],
    side_x: Annotated[float | None, "X coordinate of a point on the offset side (optional)"] = None,
    side_y: Annotated[float | None, "Y coordinate of a point on the offset side (optional)"] = None,
    ctx: Context = None,
) -> dict:
    """Create a parallel copy of a line, circle, or polyline at the given distance."""
    result = await _backend(ctx).entity_offset(handle, distance, side_x, side_y)
    return _dc(result)


# ── corner operations (trim/extend/fillet/chamfer) ──────────────────────────


@cad_tool(
    summary="Cut a line back to where another crosses it, keeping the side you point at.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Trim Entity", "readOnlyHint": False, "destructiveHint": False},
    tags={"entity", "modify", "corner"},
)
async def entity_trim(
    target_handle: Annotated[str, "Handle of the line being trimmed"],
    cutter_handle: Annotated[str, "Handle of the cutting line"],
    keep_x: Annotated[float, "X of a point on the side of the target to KEEP"],
    keep_y: Annotated[float, "Y of a point on the side of the target to KEEP"],
    ctx: Context = None,
) -> dict:
    """Trim `target` against `cutter`, keeping the segment containing (keep_x, keep_y).

    V1 supports LINE+LINE only. Cutter is treated as an infinite ray (AutoCAD's
    default 'implied extend' trim mode). Raises if the lines are parallel.
    """
    result = await _backend(ctx).entity_trim(target_handle, cutter_handle, keep_x, keep_y)
    return _dc(result)


@cad_tool(summary="Lengthen a line until it meets a boundary line.", cost="mutate")
@mcp.tool(
    annotations={"title": "Extend Entity", "readOnlyHint": False, "destructiveHint": False},
    tags={"entity", "modify", "corner"},
)
async def entity_extend(
    target_handle: Annotated[str, "Handle of the line being extended"],
    boundary_handle: Annotated[str, "Handle of the boundary line"],
    end_x: Annotated[float | None, "X of a point near the endpoint to extend (None = auto)"] = None,
    end_y: Annotated[float | None, "Y of a point near the endpoint to extend (None = auto)"] = None,
    ctx: Context = None,
) -> dict:
    """Extend `target` to meet `boundary`. If end_x/end_y is None, the target
    endpoint nearest the boundary is auto-selected.

    V1 supports LINE+LINE only. Raises if the lines are parallel.
    """
    result = await _backend(ctx).entity_extend(target_handle, boundary_handle, end_x, end_y)
    return _dc(result)


@cad_tool(summary="Round a corner between two lines with a tangent arc.", cost="mutate")
@mcp.tool(
    annotations={"title": "Fillet Two Entities", "readOnlyHint": False, "destructiveHint": False},
    tags={"entity", "modify", "corner"},
)
async def entity_fillet(
    handle1: Annotated[str, "First entity handle"],
    handle2: Annotated[str, "Second entity handle"],
    radius: Annotated[
        float, Field(description="Fillet radius (>= 0; 0 = sharp corner / corner-merge)", ge=0.0)
    ],
    trim: Annotated[
        bool, "If true, trim source entities to the tangent points (AutoCAD default)"
    ] = True,
    ctx: Context = None,
) -> dict:
    """Round a corner with a tangent arc. Returns info on the new ARC entity
    (or the first source line for radius=0). V1 supports LINE+LINE only."""
    result = await _backend(ctx).entity_fillet(handle1, handle2, radius, trim)
    return _dc(result)


@cad_tool(summary="Bevel a corner between two lines by two setback distances.", cost="mutate")
@mcp.tool(
    annotations={"title": "Chamfer Two Entities", "readOnlyHint": False, "destructiveHint": False},
    tags={"entity", "modify", "corner"},
)
async def entity_chamfer(
    handle1: Annotated[str, "First entity handle"],
    handle2: Annotated[str, "Second entity handle"],
    dist1: Annotated[float, Field(description="Chamfer distance along first line", gt=0.0)],
    dist2: Annotated[
        float | None, "Chamfer distance along second line (None = symmetric, dist2=dist1)"
    ] = None,
    trim: Annotated[
        bool, "If true, trim source entities to the tangent points (AutoCAD default)"
    ] = True,
    ctx: Context = None,
) -> dict:
    """Bevel a corner with a chamfer line. Returns info on the new chamfer LINE.
    V1 supports LINE+LINE only."""
    result = await _backend(ctx).entity_chamfer(handle1, handle2, dist1, dist2, trim)
    return _dc(result)


@cad_tool(summary="Erase one entity by handle.", cost="destructive")
@mcp.tool(
    annotations={"title": "Delete Entity", "readOnlyHint": False, "destructiveHint": True},
    tags={"entity", "modify"},
)
async def entity_delete(
    handle: Annotated[str, "Entity handle to delete"],
    ctx: Context = None,
) -> dict:
    """Permanently delete an entity by its handle."""
    await ctx.warning(f"Deleting entity {handle}")
    return await _backend(ctx).entity_delete(handle)


@cad_tool(summary="Repeat an entity in a grid of rows and columns.", cost="mutate")
@mcp.tool(
    annotations={"title": "Rectangular Array", "readOnlyHint": False},
    tags={"entity", "modify", "array"},
)
async def entity_array_rectangular(
    handle: Annotated[str, "Entity handle to array"],
    rows: Annotated[int, Field(description="Number of rows", ge=1)],
    cols: Annotated[int, Field(description="Number of columns", ge=1)],
    row_spacing: Annotated[float, "Spacing between rows (Y direction)"],
    col_spacing: Annotated[float, "Spacing between columns (X direction)"],
    fields: ResultFields = None,
    compact: ResultCompact = False,
    ctx: Context = None,
) -> list[dict] | dict:
    """Create a rectangular array of copies. Returns info of all created copies.

    rows x cols is unbounded, so this is a result-heavy tool despite being a
    create: a 40x40 grid hands back 1600 full records. fields=["handle"] is
    usually all a caller needs from it.
    """
    await ctx.info(f"Creating {rows}×{cols} rectangular array of entity {handle}")
    made = rows * cols
    await ctx.report_progress(0, made)
    result = await _backend(ctx).entity_array_rectangular(
        handle, rows, cols, row_spacing, col_spacing
    )
    await ctx.report_progress(made, made)
    return _shape_rows(
        result,
        spec=EntityInfo,
        fields=fields,
        compact=compact,
        tool="entity_array_rectangular",
        total=len(result),
    )


@cad_tool(
    summary="Repeat an entity around a centre: a bolt circle or radial pattern.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Polar Array", "readOnlyHint": False},
    tags={"entity", "modify", "array"},
)
async def entity_array_polar(
    handle: Annotated[str, "Entity handle to array"],
    count: Annotated[int, Field(description="Total number of items in the array", ge=2)],
    fill_angle: Annotated[float, "Total angle to fill in degrees (360 for full circle)"],
    center_x: Annotated[float, "Array center X"],
    center_y: Annotated[float, "Array center Y"],
    fields: ResultFields = None,
    compact: ResultCompact = False,
    ctx: Context = None,
) -> list[dict] | dict:
    """Create a polar (circular) array of copies around a center point.

    `count` is unbounded, so the same result-shaping applies as for the
    rectangular array: fields=["handle"] when the geometry is already known.
    """
    await ctx.info(f"Creating polar array of {count} items around ({center_x},{center_y})")
    result = await _backend(ctx).entity_array_polar(handle, count, fill_angle, center_x, center_y)
    return _shape_rows(
        result,
        spec=EntityInfo,
        fields=fields,
        compact=compact,
        tool="entity_array_polar",
        total=len(result),
    )


@cad_tool(
    summary="Change an entity's layer, colour, linetype, lineweight or visibility.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Set Entity Properties", "readOnlyHint": False, "destructiveHint": False},
    tags={"entity", "modify"},
)
async def entity_set_properties(
    handle: Annotated[str, "Entity handle"],
    layer: Annotated[str | None, "New layer name"] = None,
    color: Annotated[int | None, "New ACI color (256=ByLayer, 0=ByBlock, 1-255=specific)"] = None,
    linetype: Annotated[
        str | None, "New linetype name (e.g. 'DASHED', 'CENTER', 'ByLayer')"
    ] = None,
    lineweight: Annotated[int | None, "Lineweight in 0.01mm units (-3=ByLayer, -2=ByBlock)"] = None,
    visible: Annotated[bool | None, "Set entity visibility"] = None,
    ctx: Context = None,
) -> dict:
    """Change one or more properties of an entity (layer, color, linetype, lineweight, visibility)."""
    await ctx.debug(f"Setting properties for entity {handle}")
    return await _backend(ctx).entity_set_properties(
        handle, layer, color, linetype, lineweight, visible
    )


@cad_tool(
    summary="Reword a label, or change its height or rotation, keeping its handle.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Edit Text", "readOnlyHint": False, "destructiveHint": False},
    tags={"entity", "modify"},
)
async def entity_edit_text(
    handle: Annotated[str, "Handle of an existing TEXT or MTEXT entity"],
    text: Annotated[str | None, "New text content (unchanged if omitted)"] = None,
    height: Annotated[float | None, "New text height (unchanged if omitted)"] = None,
    rotation: Annotated[float | None, "New rotation in degrees (unchanged if omitted)"] = None,
    ctx: Context = None,
) -> dict:
    """Edit an existing text label in place — change its content, height, or rotation.

    Use this to rename/relabel without deleting and recreating (which would lose
    the handle). Works on both TEXT and MTEXT.
    """
    await ctx.debug(f"Editing text entity {handle}")
    result = await _backend(ctx).entity_edit_text(handle, text, height, rotation)
    return _dc(result)


@cad_tool(summary="Put an opaque background box behind an MTEXT.", cost="mutate")
@mcp.tool(
    annotations={"title": "Set Text Background", "destructiveHint": False},
    tags={"entity", "modify"},
)
async def text_set_background(
    handle: Annotated[str, "Handle of an MTEXT entity."],
    enabled: Annotated[bool, "False removes the background box."] = True,
    color: Annotated[int, "ACI background colour (1-255). 0 uses the drawing background."] = 0,
    scale: Annotated[
        float,
        Field(ge=1.0, description="Box size as a multiple of the text box; must be >= 1."),
    ] = 1.5,
    ctx: Context = None,
) -> dict:
    """Mask what is behind an MTEXT so it stays readable over hatch or geometry.

    MTEXT only: TEXT has no background-fill attribute, so setting one on it
    would report success and change nothing.
    """
    return await _backend(ctx).text_set_background(handle, enabled, color or None, scale)


@cad_tool(summary="Find and replace text across the drawing.", cost="mutate")
@mcp.tool(
    annotations={"title": "Find and Replace Text", "destructiveHint": False},
    tags={"entity", "modify"},
)
async def text_find_replace(
    find: Annotated[str, "Literal text to search for (not a regex)."],
    replace: Annotated[str, "Replacement text."],
    layer: Annotated[str, "Restrict to one layer. Empty searches every layer."] = "",
    match_case: Annotated[bool, "Case-sensitive search."] = True,
    dry_run: Annotated[bool, "Report what would change without changing it."] = False,
    ctx: Context = None,
) -> dict:
    """Replace text in TEXT, MTEXT and block attributes (ATTRIB and ATTDEF).

    `searched_types` is on the response because "no matches" and "that type was
    never searched" are different answers. Block *definitions* are included, so
    the next insert does not reintroduce the old text. DIMENSION text is out of
    scope: its text field holds the `<>` override placeholder rather than the
    measurement, so editing it would break the association. A multi-line
    attribute is matched and rewritten as the whole value (`Line1\\PLine2`),
    the same value `block_get_attributes` reports and `block_explode` bursts.
    """
    await ctx.info(f"Replacing {find!r} with {replace!r}{' (dry run)' if dry_run else ''}")
    return await _backend(ctx).text_find_replace(find, replace, layer or None, match_case, dry_run)


@cad_tool(
    summary="Change a circle's radius, a line's endpoints or an arc's angles in place.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Edit Geometry", "readOnlyHint": False, "destructiveHint": False},
    tags={"entity", "modify"},
)
async def entity_edit_geometry(
    handle: Annotated[str, "Handle of an existing CIRCLE, LINE, or ARC"],
    cx: Annotated[float | None, "New center X (CIRCLE/ARC)"] = None,
    cy: Annotated[float | None, "New center Y (CIRCLE/ARC)"] = None,
    radius: Annotated[float | None, "New radius (CIRCLE/ARC)"] = None,
    x1: Annotated[float | None, "New start X (LINE)"] = None,
    y1: Annotated[float | None, "New start Y (LINE)"] = None,
    x2: Annotated[float | None, "New end X (LINE)"] = None,
    y2: Annotated[float | None, "New end Y (LINE)"] = None,
    start_angle: Annotated[float | None, "New start angle in degrees (ARC)"] = None,
    end_angle: Annotated[float | None, "New end angle in degrees (ARC)"] = None,
    ctx: Context = None,
) -> dict:
    """Edit the defining geometry of an existing entity in place (no delete/recreate).

    CIRCLE: cx/cy/radius · LINE: x1/y1/x2/y2 · ARC: cx/cy/radius/start_angle/end_angle.
    Any argument left out is unchanged; the handle is preserved.
    """
    await ctx.debug(f"Editing geometry of entity {handle}")
    result = await _backend(ctx).entity_edit_geometry(
        handle,
        cx,
        cy,
        radius,
        x1,
        y1,
        x2,
        y2,
        start_angle,
        end_angle,
    )
    return _dc(result)


# ---------------------------------------------------------------------------
# ── SECTION 5: Entity Query (9 tools) ───────────────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(summary="Select entities inside or crossing a rectangle.", cost="read")
@mcp.tool(
    annotations={"title": "Select by Window", "readOnlyHint": True},
    tags={"entity", "query"},
)
async def selection_window(
    x1: Annotated[float, "First corner X."],
    y1: Annotated[float, "First corner Y."],
    x2: Annotated[float, "Opposite corner X."],
    y2: Annotated[float, "Opposite corner Y."],
    mode: Annotated[
        str,
        Field(
            default="window",
            description="window = wholly inside only; crossing = also entities straddling the "
            "edge.",
        ),
    ] = "window",
    entity_type: Annotated[str, "Restrict to a DXF type, e.g. CIRCLE. Empty means any."] = "",
    layer: Annotated[str, "Restrict to a layer. Empty means any."] = "",
    ctx: Context = None,
) -> dict:
    """AutoCAD's ssget window/crossing selection.

    Corners may be given in any order. Selection is by *drawn* position, so an
    entity in a mirrored frame is found where `entity_get` reports it. A
    zero-area box is refused rather than answered with an empty list.
    """
    return await _backend(ctx).selection_window(x1, y1, x2, y2, mode, entity_type, layer)


@cad_tool(summary="Select entities inside or crossing an arbitrary polygon.", cost="read")
@mcp.tool(
    annotations={"title": "Select by Polygon", "readOnlyHint": True},
    tags={"entity", "query"},
)
async def selection_polygon(
    points: Annotated[list[list[float]], "Polygon vertices as [[x, y], ...]; at least 3."],
    mode: Annotated[
        str,
        Field(default="window", description="window = wholly inside only; crossing = touching."),
    ] = "window",
    entity_type: Annotated[str, "Restrict to a DXF type. Empty means any."] = "",
    layer: Annotated[str, "Restrict to a layer. Empty means any."] = "",
    ctx: Context = None,
) -> dict:
    """Window or crossing selection against a polygon rather than a rectangle."""
    return await _backend(ctx).selection_polygon(points, mode, entity_type, layer)


@cad_tool(summary="Select entities by layer, type, colour, linetype or minimum area.", cost="read")
@mcp.tool(
    annotations={"title": "Select by Properties", "readOnlyHint": True},
    tags={"entity", "query"},
)
async def selection_filter(
    entity_type: Annotated[str, "DXF type, e.g. LWPOLYLINE. Empty means any."] = "",
    layer: Annotated[str, "Layer name. Empty means any."] = "",
    color: Annotated[int | None, "ACI colour to match."] = None,
    linetype: Annotated[str, "Linetype name. Empty means any."] = "",
    min_area: Annotated[float | None, "Keep only closed shapes with at least this area."] = None,
    ctx: Context = None,
) -> dict:
    """AutoCAD's QSELECT: filter the drawing by properties.

    Named parameters rather than a query string, deliberately — a mistyped
    attribute name in a query language comes back as an empty result, which is
    indistinguishable from "no matches". `filtered_by` reports which filters
    actually ran.
    """
    return await _backend(ctx).selection_filter(entity_type, layer, color, linetype, min_area)


@cad_tool(summary="Inspect one entity by handle: type, layer, colour and geometry.", cost="read")
@mcp.tool(
    annotations={"title": "Get Entity", "readOnlyHint": True},
    tags={"entity", "query"},
)
async def entity_get(
    handle: Annotated[str, "Entity handle"],
    ctx: Context = None,
) -> dict:
    """Get all properties of a specific entity by its handle."""
    result = await _backend(ctx).entity_get(handle)
    return _dc(result)


@cad_tool(
    summary="Browse the drawing's entities and their handles, filtered by type or layer.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "List Entities", "readOnlyHint": True},
    tags={"entity", "query"},
)
async def entity_list(
    type_filter: Annotated[
        str | None,
        "Filter by entity type: LINE, CIRCLE, ARC, LWPOLYLINE, TEXT, MTEXT, INSERT, HATCH, etc.",
    ] = None,
    layer_filter: Annotated[str | None, "Filter by layer name"] = None,
    limit: Annotated[int, Field(description="Maximum entities to return", ge=1, le=1000)] = 100,
    offset: Annotated[int, Field(description="Number of entities to skip", ge=0)] = 0,
    fields: ResultFields = None,
    compact: ResultCompact = False,
    ctx: Context = None,
) -> list[dict] | dict:
    """List entities in the drawing with optional type and layer filters.

    Returns handle, type, layer, color, and type-specific properties.
    Use handles with entity_get, entity_move, entity_delete, etc.

    This is the most expensive result on the server — the full record runs
    ~250 characters per entity, and `properties.bounding_box` alone is about a
    third of it. When all you need is handles, say so::

        entity_list(layer_filter="GEOMETRY", fields=["handle", "type"], compact=True)

    Paging honesty: a plain list has nowhere to say that more entities followed
    the page, so `compact=True` is the only mode that reports `total`,
    `truncated` and `next_offset` — all measured against the same filters.
    """
    capped = min(int(limit), config.settings.max_list_limit)
    if capped < limit:
        await ctx.warning(
            f"limit {limit} exceeds MAX_LIST_LIMIT={config.settings.max_list_limit}; capped"
        )
    await ctx.info(f"Listing entities type={type_filter} layer={layer_filter} limit={capped}")
    b = _backend(ctx)
    result = await b.entity_list(type_filter, layer_filter, capped, offset)
    total = None
    if compact:
        # Only counted when there is a field to report it in; the default path
        # must not grow a second full pass over the drawing.
        total = await b.entity_count(type_filter, layer_filter)
    elif len(result) >= capped:
        # A full page is the one case where the plain list is indistinguishable
        # from a complete answer. The value cannot say so without breaking the
        # compatibility contract, so say it in the log and name the mode that can.
        await ctx.warning(
            f"entity_list returned a full page of {capped}; more entities may follow. "
            "Re-run with compact=True for total/truncated/next_offset, or page with "
            f"offset={offset + len(result)}."
        )
    return _shape_rows(
        result,
        spec=EntityInfo,
        fields=fields,
        compact=compact,
        tool="entity_list",
        total=total,
        offset=offset,
    )


@cad_tool(summary="Erase a whole list of entities in one call.", cost="destructive")
@mcp.tool(
    annotations={
        "title": "Delete Multiple Entities",
        "readOnlyHint": False,
        "destructiveHint": True,
    },
    tags={"entity", "modify"},
)
async def entity_delete_many(
    handles: Annotated[list[str], "List of entity handles to delete"],
    ctx: Context = None,
) -> dict:
    """Delete multiple entities in one call. Returns count of deleted entities."""
    await ctx.info(f"Deleting {len(handles)} entities")
    b = _backend(ctx)
    deleted = 0
    errors = []
    for i, h in enumerate(handles):
        await ctx.report_progress(i, len(handles))
        try:
            await b.entity_delete(h)
            deleted += 1
        except Exception as exc:
            errors.append({"handle": h, "error": str(exc)})
    await ctx.report_progress(len(handles), len(handles))
    return {"ok": True, "deleted": deleted, "errors": errors}


@cad_tool(
    summary="Read what the user already picked in the AutoCAD viewport (COM backend).",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Get Viewport Selection", "readOnlyHint": True},
    tags={"entity", "query"},
)
async def selection_get(
    fields: ResultFields = None,
    compact: ResultCompact = False,
    ctx: Context = None,
) -> dict:
    """Read the entities the user pre-selected in the AutoCAD viewport (COM backend only).

    Returns the implied "pickfirst" selection — the entities highlighted with
    grips before invoking the AI — so work can be scoped to exactly those
    entities instead of the whole drawing. Typical use::

        sel = selection_get()
        dimension_auto(sel["handles"], style="chain")

    Result keys:
        ok        — True on the COM backend (even for an empty selection)
        count     — number of selected entities
        handles   — list of entity handles (hex strings) to act on
        entities  — full per-entity info (type, layer, color, ...)
        pickfirst — state of the PICKFIRST sysvar (None if unknown)
        message   — guidance when nothing is selected

    On the ezdxf headless backend there is no viewport, so this returns
    ok=False with an empty handles list.

    `fields` / `compact` shape the "entities" collection — this tool already
    returns an object, so the columnar envelope lands *under* that key rather
    than replacing the result. `handles` is unaffected, so a caller that only
    wants handles can pass fields=["handle"] and still read `handles` directly.
    """
    result = await _backend(ctx).selection_get()
    if result.get("ok"):
        await ctx.info(f"Viewport selection: {result.get('count', 0)} entit(y/ies)")
    else:
        await ctx.warning(result.get("error", "selection_get returned not-ok"))
    # The backend places EntityInfo dataclasses under "entities"; serialize them.
    result = dict(result)
    entities = result.get("entities", [])
    result["entities"] = _shape_rows(
        entities,
        spec=EntityInfo,
        fields=fields,
        compact=compact,
        tool="selection_get",
        total=len(entities),
    )
    return result


@cad_tool(summary="Read an entity's extended data (XDATA) by application name.", cost="read")
@mcp.tool(
    annotations={"title": "Get Entity XDATA", "readOnlyHint": True},
    tags={"entity", "query"},
)
async def entity_get_xdata(
    handle: Annotated[str, "Entity handle"],
    app_name: Annotated[str | None, "Registered application name; omit for every app"] = None,
    ctx: Context = None,
) -> dict:
    """Extended entity data as {app: [values]}. An app with no XDATA is simply absent."""
    return await _backend(ctx).entity_get_xdata(handle, app_name)


@cad_tool(summary="Attach or replace extended data (XDATA) on an entity.", cost="safe")
@mcp.tool(
    annotations={"title": "Set Entity XDATA", "readOnlyHint": False},
    tags={"entity", "modify"},
)
async def entity_set_xdata(
    handle: Annotated[str, "Entity handle"],
    app_name: Annotated[str, "Registered application name (created if missing)"],
    values: Annotated[
        list,
        "Values typed by kind: strings (≤255 chars) → 1000, ints → 1071, floats → 1040, [x,y] → 1010",
    ],
    ctx: Context = None,
) -> dict:
    """Replace the named app's XDATA on one entity; an empty list removes it.

    Limits are AutoCAD's and are enforced before writing: 255 characters per
    string and 16 KB per entity.
    """
    return await _backend(ctx).entity_set_xdata(handle, app_name, values)


# ---------------------------------------------------------------------------
# ── SECTION 6: Layer Management (14 tools) ──────────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(summary="List every layer with its colour, linetype and visibility state.", cost="read")
@mcp.tool(
    annotations={"title": "List Layers", "readOnlyHint": True},
    tags={"layer", "query"},
)
async def layer_list(
    fields: ResultFields = None,
    compact: ResultCompact = False,
    ctx: Context = None,
) -> list[dict] | dict:
    """List all layers with their properties (color, linetype, frozen, locked, visibility).

    Never truncated — a drawing's whole layer table is returned — so a compact
    envelope here always reports truncated=false.
    """
    result = await _backend(ctx).layer_list()
    return _shape_rows(
        result,
        spec=LayerInfo,
        fields=fields,
        compact=compact,
        tool="layer_list",
        total=len(result),
    )


@cad_tool(summary="Add a layer with a colour, linetype and lineweight.", cost="mutate")
@mcp.tool(
    annotations={"title": "Create Layer", "readOnlyHint": False},
    tags={"layer"},
)
async def layer_create(
    name: Annotated[str, "New layer name"],
    color: Annotated[int, "ACI color code (1=Red, 2=Yellow, 3=Green, 4=Cyan, 5=Blue, 7=White)"] = 7,
    linetype: Annotated[str, "Linetype name"] = "Continuous",
    lineweight: Annotated[
        int, "Lineweight (-3=ByLayer, 0=0.00mm, 13=0.13mm, 25=0.25mm, 50=0.50mm)"
    ] = -3,
    ctx: Context = None,
) -> dict:
    """Create a new layer with specified properties."""
    await ctx.info(f"Creating layer '{name}' color={color}")
    result = await _backend(ctx).layer_create(name, color, linetype, lineweight)
    return _dc(result)


@cad_tool(summary="Remove an empty layer from the drawing.", cost="destructive")
@mcp.tool(
    annotations={"title": "Delete Layer", "readOnlyHint": False, "destructiveHint": True},
    tags={"layer"},
)
async def layer_delete(
    name: Annotated[str, "Layer name to delete (layer must be empty)"],
    ctx: Context = None,
) -> dict:
    """Delete a layer. The layer must have no entities. Layer '0' cannot be deleted."""
    await ctx.warning(f"Deleting layer '{name}'")
    return await _backend(ctx).layer_delete(name)


@cad_tool(summary="Choose the layer that new geometry lands on.", cost="safe")
@mcp.tool(
    annotations={"title": "Set Current Layer", "readOnlyHint": False, "destructiveHint": False},
    tags={"layer"},
)
async def layer_set_current(
    name: Annotated[str, "Layer name to set as current"],
    ctx: Context = None,
) -> dict:
    """Set the active/current layer for new entities."""
    await ctx.info(f"Setting current layer to '{name}'")
    return await _backend(ctx).layer_set_current(name)


@cad_tool(summary="Change an existing layer's colour, linetype or lineweight.", cost="mutate")
@mcp.tool(
    annotations={"title": "Modify Layer", "readOnlyHint": False, "destructiveHint": False},
    tags={"layer"},
)
async def layer_modify(
    name: Annotated[str, "Layer name to modify"],
    color: Annotated[int | None, "New ACI color code"] = None,
    linetype: Annotated[str | None, "New linetype name"] = None,
    lineweight: Annotated[int | None, "New lineweight value"] = None,
    ctx: Context = None,
) -> dict:
    """Modify an existing layer's color, linetype, and/or lineweight."""
    result = await _backend(ctx).layer_modify(name, color, linetype, lineweight)
    return _dc(result)


@cad_tool(summary="Freeze a layer so it stops drawing and regenerating.", cost="safe")
@mcp.tool(annotations={"title": "Freeze Layer"}, tags={"layer"})
async def layer_freeze(
    name: Annotated[str, "Layer name to freeze"],
    ctx: Context = None,
) -> dict:
    """Freeze a layer (makes it invisible and unselectable, faster regeneration)."""
    return await _backend(ctx).layer_freeze(name)


@cad_tool(summary="Bring a frozen layer back into view.", cost="safe")
@mcp.tool(annotations={"title": "Thaw Layer"}, tags={"layer"})
async def layer_thaw(
    name: Annotated[str, "Layer name to thaw"],
    ctx: Context = None,
) -> dict:
    """Thaw a frozen layer, making it visible and selectable again."""
    return await _backend(ctx).layer_thaw(name)


@cad_tool(summary="Lock a layer so its entities can be seen but not edited.", cost="safe")
@mcp.tool(annotations={"title": "Lock Layer"}, tags={"layer"})
async def layer_lock(
    name: Annotated[str, "Layer name to lock"],
    ctx: Context = None,
) -> dict:
    """Lock a layer (entities visible but cannot be selected or modified)."""
    return await _backend(ctx).layer_lock(name)


@cad_tool(summary="Unlock a layer so its entities can be edited again.", cost="safe")
@mcp.tool(annotations={"title": "Unlock Layer"}, tags={"layer"})
async def layer_unlock(
    name: Annotated[str, "Layer name to unlock"],
    ctx: Context = None,
) -> dict:
    """Unlock a layer to allow entity selection and modification."""
    return await _backend(ctx).layer_unlock(name)


@cad_tool(summary="Turn a layer off so its entities stop showing.", cost="safe")
@mcp.tool(annotations={"title": "Hide Layer"}, tags={"layer"})
async def layer_hide(
    name: Annotated[str, "Layer name to turn off"],
    ctx: Context = None,
) -> dict:
    """Turn off a layer (entities invisible but still processed in regeneration)."""
    return await _backend(ctx).layer_hide(name)


@cad_tool(summary="Turn a layer that was switched off back on.", cost="safe")
@mcp.tool(annotations={"title": "Show Layer"}, tags={"layer"})
async def layer_show(
    name: Annotated[str, "Layer name to turn on"],
    ctx: Context = None,
) -> dict:
    """Turn on a layer that was previously turned off."""
    return await _backend(ctx).layer_show(name)


@cad_tool(summary="Hide every layer except one, to work on it alone.", cost="safe")
@mcp.tool(
    annotations={"title": "Isolate Layer", "readOnlyHint": False},
    tags={"layer"},
)
async def layer_isolate(
    name: Annotated[str, "Layer name to keep visible (all others will be hidden)"],
    ctx: Context = None,
) -> dict:
    """Hide all layers except the specified one (layer isolation)."""
    await ctx.info(f"Isolating layer '{name}'")
    b = _backend(ctx)
    layers = await b.layer_list()
    hidden = []
    for lyr in layers:
        if lyr.name != name and lyr.name != "0":
            await b.layer_hide(lyr.name)
            hidden.append(lyr.name)
    return {"ok": True, "isolated": name, "hidden_count": len(hidden), "hidden_layers": hidden}


@cad_tool(summary="List the dash patterns currently loaded in the drawing.", cost="read")
@mcp.tool(
    annotations={"title": "List Loaded Linetypes", "readOnlyHint": True},
    tags={"layer", "linetype"},
)
async def linetype_list(ctx: Context = None) -> list[str]:
    """Return the names of all linetypes currently loaded in the active drawing."""
    return await _backend(ctx).linetype_list()


@cad_tool(
    summary="Load a linetype such as CENTER or HIDDEN, without the FILEDIA dialog trap.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Load Linetype", "readOnlyHint": False},
    tags={"layer", "linetype"},
)
async def linetype_load(
    name: Annotated[str, "Linetype name to load (e.g. 'CENTER', 'DASHED', 'HIDDEN')"],
    file: Annotated[
        str | None,
        "Optional .lin file. Defaults to acadiso.lin (metric) or acad.lin "
        "(imperial), chosen from MEASUREMENT. Ignored by ezdxf backend.",
    ] = None,
    ctx: Context = None,
) -> dict:
    """Load a single linetype safely.

    Use this instead of `system_run_command('_-LINETYPE _LOAD ...')` — that
    raw form deadlocks on the FILEDIA file-picker dialog and on the
    -LINETYPE option-menu prompt (measured on AutoCAD 2026: it loads nothing
    either way). This tool loads through the ActiveX `Linetypes.Load` member,
    picks the right .lin file from MEASUREMENT, and verifies the linetype
    actually loaded. Refuses a name that is not a valid symbol name, a file
    outside the allowed paths, and reports a name the file does not carry
    or a file AutoCAD cannot find as an error naming both.
    """
    return await _backend(ctx).linetype_load(name, file)


# ---------------------------------------------------------------------------
# ── SECTION 7: Block Operations (8 tools) ───────────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(summary="List the block definitions the drawing already carries.", cost="read")
@mcp.tool(
    annotations={"title": "List Blocks", "readOnlyHint": True},
    tags={"block", "query"},
)
async def block_list(
    fields: ResultFields = None,
    compact: ResultCompact = False,
    ctx: Context = None,
) -> list[dict] | dict:
    """List all block definitions in the drawing (name, origin, attribute count, entity count).

    Never truncated — the whole block table is returned — so a compact envelope
    here always reports truncated=false.
    """
    result = await _backend(ctx).block_list()
    return _shape_rows(
        result,
        spec=BlockInfo,
        fields=fields,
        compact=compact,
        tool="block_list",
        total=len(result),
    )


@cad_tool(summary="Place a block, filling in its attribute values as you go.", cost="mutate")
@mcp.tool(
    annotations={"title": "Insert Block", "readOnlyHint": False},
    tags={"block"},
)
async def block_insert(
    name: Annotated[str, "Block definition name"],
    x: Annotated[float, "Insertion X"],
    y: Annotated[float, "Insertion Y"],
    scale_x: Annotated[float, "X scale factor"] = 1.0,
    scale_y: Annotated[float, "Y scale factor"] = 1.0,
    rotation: Annotated[float, "Rotation angle in degrees"] = 0.0,
    attributes: Annotated[dict | None, "Attribute values: {TAG: value}"] = None,
    layer: Annotated[str | None, "Layer name"] = None,
    ctx: Context = None,
) -> dict:
    """Insert a block and optionally set attribute values.

    Refused before any write, on both engines: an undefined or layout block
    name, and an attribute value that is not text — a string is written as
    is (one line), an int or finite float as its plain digits (`101` →
    `"101"`), while `null`, booleans, lists and objects are refused naming
    the tag (they used to be written as their Python repr). Values for tags
    the block does not define are ignored.
    """
    await ctx.info(f"Inserting block '{name}' at ({x},{y})")
    result = await _backend(ctx).block_insert(
        name, x, y, scale_x, scale_y, rotation, attributes, layer
    )
    return _dc(result)


@cad_tool(summary="Break a block reference apart into its component entities.", cost="destructive")
@mcp.tool(
    annotations={"title": "Explode Block", "readOnlyHint": False, "destructiveHint": True},
    tags={"block"},
)
async def block_explode(
    handle: Annotated[str, "Block reference (INSERT) entity handle"],
    ctx: Context = None,
) -> dict:
    """Explode a block reference into its component entities, keeping its
    attribute text (the Express Tools BURST rule, on both engines).

    Every attached ATTRIB becomes a TEXT with the same value, placement,
    height, rotation and layer (an invisible attribute becomes an invisible
    TEXT; a multi-line attribute becomes an MTEXT carrying every line), and
    a constant attribute (which has no ATTRIB) becomes a TEXT of its value
    too; AutoCAD's plain EXPLODE would keep only the tag-name placeholders
    and drop the values. Everything lands in the layout that owns the
    reference (model space or its paper-space sheet), whichever tab is
    current. Returns `inserted_handles` (the geometry), `attribute_texts`
    (one TEXT or MTEXT handle per attribute), `exploded_handle` and
    `backend`. Refused, with nothing written: a handle that is not a block
    reference, a reference nested inside a block definition (explode the
    outer reference instead), an external reference (bind it first), a
    MINSERT grid, and -- headless only, capability `explode_opaque_members`
    -- a block holding a member ezdxf cannot transform (an OLE2FRAME logo, a
    VIEWPORT, a proxy entity without proxy graphics), named by member; the
    live engine hands those to AutoCAD. Not undoable except through
    `drawing_undo` / a transaction.
    """
    await ctx.warning(f"Exploding block reference {handle}")
    return await _backend(ctx).block_explode(handle)


@cad_tool(summary="Read the tag/value pairs off a block reference.", cost="read")
@mcp.tool(
    annotations={"title": "Get Block Attributes", "readOnlyHint": True},
    tags={"block", "query"},
)
async def block_get_attributes(
    handle: Annotated[str, "Block reference (INSERT) entity handle"],
    ctx: Context = None,
) -> dict:
    """Get all attribute values from a block reference as {TAG: value} dict.

    A multi-line attribute reports its whole content with `\\P` between the
    lines (AutoCAD's `TextString`), on both engines.
    """
    return await _backend(ctx).block_get_attributes(handle)


@cad_tool(
    summary="Fill in a block reference's attributes, such as title-block fields.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Set Block Attributes", "readOnlyHint": False},
    tags={"block"},
)
async def block_set_attributes(
    handle: Annotated[str, "Block reference (INSERT) entity handle"],
    attributes: Annotated[dict, "Attribute values to update: {TAG: new_value}"],
    ctx: Context = None,
) -> dict:
    """Update attribute values in a block reference.

    Same value rule as `block_insert`: strings and numbers are written,
    `null` / booleans / lists / objects are refused naming the tag before
    anything changes. `updated_tags` lists the tags that exist on the
    reference and were written; unknown tags are ignored.

    A multi-line attribute takes `Line1\\PLine2` and keeps every line; the
    value written is the one `block_get_attributes` reads back and
    `block_explode` bursts, on both engines.
    """
    return await _backend(ctx).block_set_attributes(handle, attributes)


@cad_tool(
    summary="Turn existing entities into a reusable block definition.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Create Block From Entities", "readOnlyHint": False},
    tags={"block"},
)
async def block_create_from_entities(
    name: Annotated[str, "New block definition name"],
    handles: Annotated[list[str], "List of entity handles to include in the block"],
    base_x: Annotated[float, "Block base point X"] = 0.0,
    base_y: Annotated[float, "Block base point Y"] = 0.0,
    ctx: Context = None,
) -> dict:
    """Create a new block definition from existing entities in the drawing.

    Works on both engines. The originals stay in model space — this defines a
    reusable block from them rather than consuming them the way AutoCAD's BLOCK
    command does; use `block_insert` to place copies, and delete the originals
    yourself if you want the command's behaviour.

    Handles that do not resolve are listed in `skipped` rather than silently
    dropped, and a call where none resolve fails instead of leaving an empty
    definition behind.
    """
    await ctx.info(f"Creating block '{name}' from {len(handles)} entities")
    return await _backend(ctx).block_create_from_entities(name, handles, base_x, base_y)


@cad_tool(
    summary="Define a block from typed primitives and attribute definitions, on both engines.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Define Block", "readOnlyHint": False},
    tags={"block", "create"},
)
async def block_define(
    name: Annotated[str, "New block definition name"],
    entities: Annotated[
        list[dict],
        "Primitives in block-local coordinates. type=line {x1,y1,x2,y2} | circle {cx,cy,r} | "
        "arc {cx,cy,r,start_deg,end_deg} | polyline {points,closed,bulges?} | "
        "text {text,x,y,height,rotation_deg?,align?} | solid {points (3-4)}. Optional layer (default 0).",
    ],
    attdefs: Annotated[
        list[dict] | None,
        "Attribute definitions: {tag, x, y, height, prompt?, default?, rotation_deg?, align?, invisible?}",
    ] = None,
    base_x: Annotated[float, "Block base point X"] = 0.0,
    base_y: Annotated[float, "Block base point Y"] = 0.0,
    overwrite: Annotated[
        bool, "Replace the contents of an existing definition of this name"
    ] = False,
    create_layers: Annotated[
        bool, "Create primitive layers that do not exist yet (default: refuse and name them)"
    ] = False,
    ctx: Context = None,
) -> dict:
    """Create a block definition with attribute definitions from typed specs.

    The whole request is validated before anything is written: one malformed
    entry refuses the call and leaves no definition behind. Refusals: a
    malformed primitive or ATTDEF (names `entities[i]`/`attdefs[i]` and the
    key), a non-finite base point (names `base_x`/`base_y`), a name clash
    without `overwrite`, an anonymous `*` name, and a primitive `layer` that
    is not in the drawing's layer table (names the missing layers) unless
    `create_layers=true`, which creates them and lists them in
    `layers_created`. `overwrite=true` replaces the *contents* of an existing
    block so its INSERTs keep pointing at the name and show the new geometry;
    `replaced` reports it. Geometry inside the block is ByBlock so the
    INSERT's layer supplies colour and lineweight. Insert with
    `block_insert(attributes={TAG: value})`.
    """
    await ctx.info(f"Defining block '{name}' from {len(entities)} primitives")
    return await _backend(ctx).block_define(
        name, entities, attdefs, base_x, base_y, overwrite, create_layers
    )


@cad_tool(summary="Find every place a given block is inserted.", cost="read")
@mcp.tool(
    annotations={"title": "Find Blocks By Name", "readOnlyHint": True},
    tags={"block", "query"},
)
async def block_find_references(
    name: Annotated[str, "Block definition name to search for"],
    fields: ResultFields = None,
    compact: ResultCompact = False,
    ctx: Context = None,
) -> list[dict] | dict:
    """Find all insert references to a specific block definition.

    Bounded by the backend's own default entity_list page (200 INSERTs scanned),
    which is a pre-existing limit, not a new one: the compact envelope's `total`
    counts the references found within that scan.
    """
    await ctx.info(f"Finding all references to block '{name}'")
    result = await _backend(ctx).entity_list(type_filter="INSERT")
    refs = [e for e in result if e.properties.get("block_name") == name]
    return _shape_rows(
        refs,
        spec=EntityInfo,
        fields=fields,
        compact=compact,
        tool="block_find_references",
        total=len(refs),
    )


# ---------------------------------------------------------------------------
# ── SECTION 8: Analysis & Query (12 tools) ──────────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(summary="Trace the closed boundary around a point, like BOUNDARY.", cost="mutate")
@mcp.tool(
    annotations={"title": "Trace Boundary", "destructiveHint": False},
    tags={"analysis"},
)
async def boundary_trace(
    x: Annotated[float, "Seed point X, inside the region to trace."],
    y: Annotated[float, "Seed point Y, inside the region to trace."],
    layer: Annotated[str, "Only consider edges on this layer. Empty considers all."] = "",
    tolerance: Annotated[float, "Gap tolerance for joining edges."] = 1e-9,
    ctx: Context = None,
) -> dict:
    """AutoCAD's BOUNDARY/BPOLY: create a closed polyline around a seed point.

    Returns the *nearest enclosing* loop, so a seed inside an island gives the
    island rather than the outer region. Straight edges are split where they
    cross, so a line drawn across a shape divides it the way it looks like it
    should. A seed with no enclosing loop is refused, and the error names the
    gap when the edges nearly close.
    """
    await ctx.info(f"Tracing the boundary around ({x}, {y})")
    return await _backend(ctx).boundary_trace(x, y, layer or None, tolerance)


@cad_tool(summary="Join named entities into one closed boundary polyline.", cost="mutate")
@mcp.tool(
    annotations={"title": "Boundary from Entities", "destructiveHint": False},
    tags={"analysis"},
)
async def boundary_from_entities(
    handles: Annotated[list[str], "Handles of the 2D entities that form the loop."],
    tolerance: Annotated[float, "Gap tolerance for joining endpoints."] = 1e-9,
    ctx: Context = None,
) -> dict:
    """Chain the given entities into one closed polyline.

    The handles may arrive in any order — putting them in chain order is the
    tool's job. A chain that does not close is refused, and the error names the
    coordinates of the gap.
    """
    return await _backend(ctx).boundary_from_entities(handles, tolerance)


@cad_tool(summary="Dump every DXF property of one entity, like LIST.", cost="read")
@mcp.tool(
    annotations={"title": "List Entity Properties", "readOnlyHint": True},
    tags={"analysis", "query"},
)
async def analysis_list_properties(
    handle: Annotated[str, "Handle of the entity to dump."],
    ctx: Context = None,
) -> dict:
    """AutoCAD's LIST: the full DXF attribute set for one handle.

    `dxf_attributes` is the raw attribute set `entity_get` deliberately does
    not carry. Coordinates in it are WCS, like everywhere else in this server,
    and `extrusion` is reported so the entity's own frame is still visible.
    """
    return await _backend(ctx).analysis_list_properties(handle)


@cad_tool(summary="Count the drawing's objects, broken down by type and by layer.", cost="read")
@mcp.tool(
    annotations={"title": "Entity Statistics", "readOnlyHint": True},
    tags={"analysis", "query"},
)
async def analysis_entity_stats(ctx: Context = None) -> dict:
    """Analyze the drawing and return entity counts grouped by type and by layer.

    Returns: total_entities, by_type (sorted by count), by_layer (sorted by count).
    This is unique to AutoCAD MCP Pro – no other MCP server provides this!
    """
    await ctx.info("Analyzing drawing statistics")
    return await _backend(ctx).analysis_stats()


@cad_tool(summary="List everything that falls inside a rectangular window.", cost="read")
@mcp.tool(
    annotations={"title": "Find Entities in Region", "readOnlyHint": True},
    tags={"analysis", "query"},
)
async def analysis_find_in_region(
    x1: Annotated[float, "Region minimum X"],
    y1: Annotated[float, "Region minimum Y"],
    x2: Annotated[float, "Region maximum X"],
    y2: Annotated[float, "Region maximum Y"],
    fields: ResultFields = None,
    compact: ResultCompact = False,
    ctx: Context = None,
) -> list[dict] | dict:
    """Find all entities within a rectangular region (crossing selection).

    Uncapped: a window over a busy drawing returns every hit. Project with
    `fields` and/or `compact` before widening the window.
    """
    await ctx.info(f"Finding entities in region ({x1},{y1}) → ({x2},{y2})")
    result = await _backend(ctx).analysis_entities_in_region(x1, y1, x2, y2)
    return _shape_rows(
        result,
        spec=EntityInfo,
        fields=fields,
        compact=compact,
        tool="analysis_find_in_region",
        total=len(result),
    )


@cad_tool(summary="Measure the gap between two points: distance, dx/dy and angle.", cost="read")
@mcp.tool(
    annotations={"title": "Measure Distance", "readOnlyHint": True, "idempotentHint": True},
    tags={"analysis", "measure"},
)
async def analysis_measure_distance(
    x1: Annotated[float, "Point 1 X"],
    y1: Annotated[float, "Point 1 Y"],
    x2: Annotated[float, "Point 2 X"],
    y2: Annotated[float, "Point 2 Y"],
    ctx: Context = None,
) -> dict:
    """Measure the Euclidean distance between two points."""
    dist = await _backend(ctx).analysis_measure_distance(x1, y1, x2, y2)
    dx = x2 - x1
    dy = y2 - y1
    angle = math.degrees(math.atan2(dy, dx))
    return {
        "distance": round(dist, 6),
        "dx": round(dx, 6),
        "dy": round(dy, 6),
        "angle_degrees": round(angle, 4),
    }


@cad_tool(summary="Measure a polygon's area and perimeter from its vertices.", cost="read")
@mcp.tool(
    annotations={"title": "Measure Area", "readOnlyHint": True, "idempotentHint": True},
    tags={"analysis", "measure"},
)
async def analysis_measure_area(
    points: Annotated[
        list[list[float]],
        "Polygon vertices, min 3. Each is [x, y] or [x, y, bulge] — the bulge "
        "(DXF convention) makes the edge leaving that vertex a circular arc.",
    ],
    ctx: Context = None,
) -> dict:
    """Area and perimeter of a polygon you supply the vertices for.

    This measures the numbers in the call, NOT the drawing. To measure something
    that exists, use `analysis_measure_entity(handle)` — it reads the real
    geometry, including curvature this tool can only see if you pass it.

    Straight-edged polygons are exact. Pass a third `bulge` element per vertex
    for arc edges; omitting it on curved geometry under-reports (28% on a
    semicircular edge), which is why `assumes` says what was taken on faith.
    """
    if len(points) < 3:
        raise ToolError("At least 3 points are required to calculate area.")
    vertices = [(float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0) for p in points]
    area, perimeter = polygon_area_perimeter(vertices, closed=True)
    curved = any(abs(v[2]) > 1e-12 for v in vertices)
    return {
        "area": round(area, 6),
        "perimeter": round(perimeter, 6),
        "vertex_count": len(points),
        "exact": True,
        "assumes": (
            "the vertices given are the whole boundary and it is closed"
            if curved
            else "every edge is straight, and the vertices given are the whole "
            "closed boundary — pass [x, y, bulge] if any edge is an arc"
        ),
        "self_intersecting": is_self_intersecting([(v[0], v[1]) for v in vertices]),
    }


@cad_tool(
    summary="Area and perimeter of an existing entity, by handle.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Measure Entity", "readOnlyHint": True, "idempotentHint": True},
    tags={"analysis", "measure"},
)
async def analysis_measure_entity(
    handle: Annotated[str, "Entity handle (hex string) from create/list/select"],
    flatten_tolerance: Annotated[
        float, "Max chord deviation when geometry has no closed form (splines, partial ellipses)"
    ] = 0.001,
    ctx: Context = None,
) -> dict:
    """Measure something already in the drawing, by handle.

    Reads the real geometry, so polyline bulges (arc edges) are included —
    reading vertices back and shoelacing them yourself loses 28% of the area on
    a semicircular edge, silently.

    Measurable: LWPOLYLINE, 2D POLYLINE, CIRCLE, ELLIPSE, SPLINE, HATCH, SOLID,
    TRACE, 3DFACE. REGION and 3DSOLID need the live COM backend (their area is
    in ACIS data ezdxf cannot evaluate) and refuse with
    `capability: "measure_area_acis"`. LINE/TEXT/INSERT bound no area on any
    engine and are a plain error, not a capability gap.

    The payload states its own accuracy: `exact` is false when the shape had to
    be flattened (then `flatten_tolerance` says how finely), `assumed_closed` is
    true when an open boundary was closed the way AutoCAD's AREA does, and
    `self_intersecting` warns when the shoelace cancelled crossed lobes — a
    bowtie measures 0.0 and that number is worse than useless unflagged.
    """
    await ctx.debug(f"Measuring entity {handle}")
    return await _backend(ctx).entity_measure(handle, flatten_tolerance)


@cad_tool(summary="Get the overall extents of everything drawn.", cost="read")
@mcp.tool(
    annotations={"title": "Drawing Bounding Box", "readOnlyHint": True},
    tags={"analysis", "query"},
)
async def analysis_bounding_box(ctx: Context = None) -> dict:
    """Get the bounding box (extents) of all entities in the drawing."""
    return await _backend(ctx).analysis_bounding_box()


@cad_tool(summary="Grab the handles of everything sitting on one layer.", cost="read")
@mcp.tool(
    annotations={"title": "Select Entities By Layer", "readOnlyHint": True},
    tags={"analysis", "query"},
)
async def analysis_select_by_layer(
    layer_name: Annotated[str, "Layer name to select entities from"],
    fields: ResultFields = None,
    compact: ResultCompact = False,
    ctx: Context = None,
) -> list[dict] | dict:
    """Get all entities on a specific layer. Returns entity list with handles.

    Capped at MAX_LIST_LIMIT (default 5000). The plain list cannot say it was
    capped — the warning goes to the log stream, which most clients never show
    the model — so use `compact=True` when the count matters: its `total` is the
    layer's real population and `truncated` states whether the cap fired.
    """
    await ctx.info(f"Selecting all entities on layer '{layer_name}'")
    result = await _backend(ctx).analysis_select_by_layer(layer_name)
    total = len(result)
    cap = config.settings.max_list_limit
    if total > cap:
        await ctx.warning(f"Layer has {total} entities; truncated to {cap}")
        result = result[:cap]
    return _shape_rows(
        result,
        spec=EntityInfo,
        fields=fields,
        compact=compact,
        tool="analysis_select_by_layer",
        total=total,
    )


@cad_tool(
    summary="Grab the handles of every entity of one type: all the circles, all the lines.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Select Entities By Type", "readOnlyHint": True},
    tags={"analysis", "query"},
)
async def analysis_select_by_type(
    entity_type: Annotated[
        str,
        "Entity type: LINE, CIRCLE, ARC, LWPOLYLINE, TEXT, MTEXT, INSERT, HATCH, SPLINE, ELLIPSE",
    ],
    fields: ResultFields = None,
    compact: ResultCompact = False,
    ctx: Context = None,
) -> list[dict] | dict:
    """Get all entities of a specific type. Returns entity list with handles.

    Capped at MAX_LIST_LIMIT (default 5000); as with analysis_select_by_layer,
    `compact=True` is the only shape that reports `total` and `truncated`.
    """
    await ctx.info(f"Selecting all {entity_type} entities")
    result = await _backend(ctx).analysis_select_by_type(entity_type)
    total = len(result)
    cap = config.settings.max_list_limit
    if total > cap:
        await ctx.warning(f"Found {total} entities of type {entity_type}; truncated to {cap}")
        result = result[:cap]
    return _shape_rows(
        result,
        spec=EntityInfo,
        fields=fields,
        compact=compact,
        tool="analysis_select_by_type",
        total=total,
    )


@cad_tool(summary="Report how many entities, and which types, sit on each layer.", cost="read")
@mcp.tool(
    annotations={"title": "Layer Statistics", "readOnlyHint": True},
    tags={"analysis", "query", "layer"},
)
async def analysis_layer_stats(ctx: Context = None) -> dict:
    """Return detailed statistics for each layer: entity count, types present."""
    await ctx.info("Computing layer statistics")
    b = _backend(ctx)
    await ctx.report_progress(0, 100)
    layers = await b.layer_list()
    await ctx.report_progress(20, 100)
    all_entities = await b.entity_list(limit=50000)
    await ctx.report_progress(80, 100)
    layer_data: dict[str, dict] = {
        lyr.name: {"layer": _dc(lyr), "count": 0, "types": {}} for lyr in layers
    }
    for ent in all_entities:
        lyr_name = ent.layer
        if lyr_name not in layer_data:
            layer_data[lyr_name] = {"layer": {"name": lyr_name}, "count": 0, "types": {}}
        layer_data[lyr_name]["count"] += 1
        t = ent.type
        layer_data[lyr_name]["types"][t] = layer_data[lyr_name]["types"].get(t, 0) + 1
    await ctx.report_progress(100, 100)
    return {
        "layers": list(layer_data.values()),
        "total_layers": len(layer_data),
    }


# ---------------------------------------------------------------------------
# ── SECTION 8b: Batch Operations (3 tools) ───────────────────────────────
# ---------------------------------------------------------------------------
#
# Three tools, two jobs, no overlap:
#
#   cad_batch            ordered, heterogeneous, any tool, with binding between
#                        steps. The general executor.
#   entity_batch_create  one dense dict per entity ({"type": "line", ...}) with
#   entity_batch_modify  no per-step tool name and no binding. Roughly 30 fewer
#                        characters per entity than the equivalent cad_batch
#                        step, which is real money at a few hundred entities.
#
# They compose rather than compete: a cad_batch step may call
# entity_batch_create for the bulk half of a drawing. What must never happen is
# a second general executor — hence cad_batch is on its own deny set and cannot
# nest inside itself.


# ── cad_batch: the ordered multi-tool executor ──────────────────────────────

#: What ``on_error`` accepts. ``stop`` is the default because it is the only
#: mode whose guarantee is identical on both backends (see
#: :func:`_batch_rollback_guarantee`).
BATCH_ON_ERROR_MODES = ("stop", "continue", "rollback")

#: Every ``error.kind`` a step row can carry. Closed on purpose: a client that
#: branches on the taxonomy needs it to be enumerable.
BATCH_ERROR_KINDS = (
    "denied",  # on cad_batch's deny set; never executed
    "malformed_step",  # not a {tool, args, bind} object
    "unknown_tool",  # not registered, or hidden by TOOL_PROFILE / TOOL_PACKS / ENABLE_3D
    "invalid_args",  # rejected by the tool's own JSON Schema
    "unresolved_ref",  # a $reference that no earlier step successfully bound
    "unsupported",  # this backend cannot do it (carries `capability`)
    "refused",  # the tool declined on purpose (path/command validation, ...)
    "failed",  # attempted and broke
)

#: What a rollback is actually worth, per backend transaction implementation.
BATCH_ROLLBACK_GUARANTEES = ("snapshot", "best_effort_undo", "unverified", "none")

#: A hard ceiling on one batch. Steps run sequentially inside a single tool
#: call that no client can cancel midway, so an unbounded list is a foot-gun
#: rather than a feature.
MAX_BATCH_STEPS = 250

#: Reference sigil. ``$name`` / ``$name.path.0``; ``$$`` escapes a literal ``$``.
_BATCH_REF_SIGIL = "$"

#: Stands in for an unresolved reference while a step is validated against the
#: real JSON Schema. A bound value's *type* is only knowable at run time, so
#: errors reported against this exact scalar are dropped and everything else
#: (required keys, unknown keys, the types of every literal argument) still
#: applies. NUL-delimited so no real argument can collide with it.
_BATCH_REF_SENTINEL = "\x00cad_batch:unresolved-reference\x00"

#: Step-row messages are compacted to this many characters. A pydantic failure
#: prints four paragraphs; re-emitting that per step would hand back the tokens
#: the batch just saved.
_BATCH_MESSAGE_CHARS = 240

_MISSING = object()


class BatchReferenceError(ValueError):
    """A ``$reference`` that cannot be resolved against the current bindings."""


def _batch_denied_tools() -> frozenset[str]:
    """Tools ``cad_batch`` will never call, derived from the live registry.

    A single ``cad_batch`` grant would otherwise reach *every* tool, including
    ``system_run_command`` / ``system_run_lisp`` — defeating a client's per-tool
    allowlist and routing around the point of having the raw escape hatches be
    separately opt-in in the first place. The set is derived from the
    ``@cad_tool(cost="escape")`` cards rather than hand-listed, so a future
    escape hatch is denied the day it is registered, not the day someone
    remembers this function exists.

    The denial is unconditional. ``DANGEROUS_COMMANDS_ENABLED`` governs whether
    :mod:`security` will *sanitize* a command string; it says nothing about
    which tool a client may reach, and reusing it here would let one env var
    silently re-open the escalation path. The escape hatches stay fully
    available — called directly, where their own gate applies.

    ``cad_batch`` denies itself too: no recursion, and no laundering a denied
    step through a nested executor.
    """
    denied = {"cad_batch"}
    for tool in _local_tool_components():
        cad = (getattr(tool, "meta", None) or {}).get("cad") or {}
        if cad.get("cost") == "escape":
            denied.add(tool.name)
    return frozenset(denied)


def _batch_read_only_tools() -> frozenset[str]:
    """Tools whose ``ok: False`` is a *finding*, not a failure to act.

    ``validation_check`` / ``drawing_critique`` / ``drawing_preflight`` are
    ``readOnlyHint=True`` and answer ``{"ok": len(issues) == 0}``, so on any
    drawing with something to report they return ``ok: False`` having worked
    perfectly. A mutator answering ``ok: False`` means the opposite — it did not
    mutate. Reading both the same way made a read-only check trigger a rollback
    that destroyed the geometry the batch had just drawn, so the discriminator
    is the annotation the tool already publishes.
    """
    return frozenset(
        tool.name
        for tool in _local_tool_components()
        if getattr(getattr(tool, "annotations", None), "readOnlyHint", None) is True
    )


# ── error typing ────────────────────────────────────────────────────────────
#
# `ctx.fastmcp.call_tool` re-raises FastMCP's own exceptions untouched but wraps
# everything else as `ToolError(f"Error calling tool {name!r}: {e}") from e`. The
# `from e` is what makes a typed taxonomy possible: the real exception is still
# on the `__cause__` chain, so nothing here ever has to read message text. It
# must not, either — "this backend cannot write DWG" and a RuntimeError that
# happens to say the same words are different facts, and only the type knows.

_BATCH_CAUSE_CHAIN_LIMIT = 8

_capability_error_types: tuple[type, ...] | None = None


def _capability_refusal_types() -> tuple[type, ...]:
    """Backend exception classes meaning "this engine cannot do that".

    Imported lazily, exactly as the backends themselves are: server.py must
    still import on a box where a backend's dependencies are missing.
    """
    global _capability_error_types
    if _capability_error_types is None:
        found: list[type] = []
        try:
            from backends.ezdxf_backend import UnsupportedCapabilityError

            found.append(UnsupportedCapabilityError)
        except Exception as exc:  # pragma: no cover - ezdxf is a core dependency
            log.debug("capability refusal type unavailable: %s", exc)
        _capability_error_types = tuple(found)
    return _capability_error_types


def _batch_cause_chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and its ``__cause__`` ancestors, outermost first, bounded."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(chain) < _BATCH_CAUSE_CHAIN_LIMIT:
        if id(current) in seen:
            break
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__
    return chain


def _batch_capability_of(exc: BaseException) -> str | None:
    """The backend capability ``exc`` refuses, or None.

    Recognised by type, and — because a backend may ship its own refusal class —
    also by the contract every such refusal implements: a non-empty
    ``capability`` string plus the ``to_dict()`` that carries the
    ``{ok, error, capability}`` payload. Structural, not textual.
    """
    types = _capability_refusal_types()
    for error in _batch_cause_chain(exc):
        if types and isinstance(error, types):
            return str(getattr(error, "capability", "") or "unknown")
        capability = getattr(error, "capability", None)
        if isinstance(capability, str) and capability and callable(getattr(error, "to_dict", None)):
            return capability
    return None


_quarantine_error_type: type | None = None


def _quarantine_refusal_type() -> type | None:
    """``DocumentQuarantineError``, imported lazily like the capability types.

    It lives in ``backends.quarantine``, which deliberately imports no ezdxf, so
    this resolves even on a box where a backend's dependencies are missing.
    """
    global _quarantine_error_type
    if _quarantine_error_type is None:
        from backends.quarantine import DocumentQuarantineError

        _quarantine_error_type = DocumentQuarantineError
    return _quarantine_error_type


def _batch_quarantine_of(exc: BaseException) -> dict | None:
    """The abandoned-call record ``exc`` refuses on behalf of, or None.

    Recognised by type, not structurally: ``DocumentQuarantineError``
    deliberately has no ``capability`` attribute, because
    ``_batch_capability_of`` would then report it to the client as
    ``kind: "unsupported"`` — telling them the ezdxf engine cannot draw a circle
    when the engine is fine and the document is not.
    """
    try:
        quarantine_type = _quarantine_refusal_type()
    except Exception as exc_import:  # pragma: no cover - core module
        log.debug("quarantine refusal type unavailable: %s", exc_import)
        return None
    for error in _batch_cause_chain(exc):
        if isinstance(error, quarantine_type):
            return getattr(error, "quarantine", None) or {}
    return None


def _refusal_message(exc: BaseException) -> str:
    """The most specific message on the cause chain, in full.

    Deliberately not ``_batch_message``: that truncates to
    ``_BATCH_MESSAGE_CHARS`` for a compact step row, and a refusal that gets cut
    off mid-word ("...or switc") loses the escape hatch it was pointing at.
    """
    for error in reversed(_batch_cause_chain(exc)):
        text = " ".join(str(error).split())
        if text:
            return text
    return type(exc).__name__


def _compact_batch_message(text: str) -> str:
    """One short line, for a step row that sits alongside dozens of others."""
    text = " ".join(text.split())
    if len(text) > _BATCH_MESSAGE_CHARS:
        text = text[: _BATCH_MESSAGE_CHARS - 3].rstrip() + "..."
    return text


def _batch_message(exc: BaseException) -> str:
    """The most specific message on the chain, collapsed onto one short line."""
    chain = _batch_cause_chain(exc)
    text = ""
    for error in reversed(chain):
        text = str(error).strip()
        if text:
            break
    return _compact_batch_message(text) or type(chain[0]).__name__


def _classify_batch_error(exc: BaseException) -> dict:
    """Turn an exception from a nested tool call into a typed step error."""
    from fastmcp.exceptions import NotFoundError
    from fastmcp.exceptions import ValidationError as FastMCPValidationError
    from pydantic import ValidationError as PydanticValidationError

    capability = _batch_capability_of(exc)
    if capability:
        return {
            "kind": "unsupported",
            "capability": capability,
            "message": _batch_message(exc),
        }
    quarantine = _batch_quarantine_of(exc)
    if quarantine is not None:
        # R32: its own kind. Filing it under "unsupported" would tell the batch
        # reader the engine lacks a feature; filing it under "failed" would hide
        # that every remaining step will refuse for the same reason.
        return {
            "kind": "quarantined",
            "quarantine": quarantine,
            "message": _batch_message(exc),
        }
    chain = _batch_cause_chain(exc)
    if any(isinstance(error, NotFoundError) for error in chain):
        return {"kind": "unknown_tool", "message": _batch_message(exc)}
    if any(isinstance(error, (FastMCPValidationError, PydanticValidationError)) for error in chain):
        return {"kind": "invalid_args", "message": _batch_message(exc)}
    if isinstance(chain[-1], ToolError):
        # call_tool re-raises a FastMCPError untouched, so an *unwrapped*
        # ToolError is the tool itself declining on purpose: a rejected path, a
        # sanitized command, an unavailable backend.
        return {"kind": "refused", "message": _batch_message(exc)}
    return {"kind": "failed", "message": _batch_message(exc)}


# ── $references ─────────────────────────────────────────────────────────────


def _batch_ref_name(value: Any) -> str | None:
    """The binding name a value references, or None.

    Only a string that is *entirely* a reference counts. Partial interpolation
    ("part-$edge") is deliberately unsupported: it cannot be told apart from a
    text string that happens to contain a dollar sign, and silently rewriting
    drawing text would be worse than not offering the feature.
    """
    if not isinstance(value, str) or not value.startswith(_BATCH_REF_SIGIL):
        return None
    if value.startswith(_BATCH_REF_SIGIL * 2):  # "$$" escapes a literal "$"
        return None
    body = value[1:]
    if not body:
        return None
    return body.split(".", 1)[0]


def _resolve_batch_ref(value: str, bindings: dict[str, Any]) -> Any:
    """Resolve one whole-string ``$reference`` against ``bindings``."""
    name, _, path = value[1:].partition(".")
    if name not in bindings:
        raise BatchReferenceError(
            f"'{value}' references an unbound name: no earlier step bound {name!r}. "
            f"Bound so far: {sorted(bindings) or 'nothing'}."
        )
    current = bindings[name]
    if not path:
        if isinstance(current, dict):
            handle = current.get("handle", _MISSING)
            if handle is _MISSING:
                raise BatchReferenceError(
                    f"'{value}' asks for the handle of step bound as {name!r}, but that "
                    f"step returned no 'handle' (keys: {sorted(current)}). Reference a "
                    f"field instead, e.g. '{value}.{sorted(current)[0]}'."
                    if current
                    else f"'{value}' asks for a handle, but step {name!r} returned nothing."
                )
            return handle
        return current
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
            continue
        if isinstance(current, (list, tuple)) and segment.lstrip("-").isdigit():
            index = int(segment)
            if -len(current) <= index < len(current):
                current = current[index]
                continue
        raise BatchReferenceError(
            f"'{value}' cannot be resolved: {segment!r} is not present in the result "
            f"bound as {name!r}."
        )
    return current


def _substitute_batch_refs(value: Any, bindings: dict[str, Any]) -> Any:
    """Replace every whole-string reference inside an argument tree."""
    if isinstance(value, str):
        if value.startswith(_BATCH_REF_SIGIL * 2):
            return value[1:]
        if _batch_ref_name(value) is not None:
            return _resolve_batch_ref(value, bindings)
        return value
    if isinstance(value, dict):
        return {key: _substitute_batch_refs(item, bindings) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute_batch_refs(item, bindings) for item in value]
    return value


def _batch_refs_in(value: Any, found: set[str] | None = None) -> set[str]:
    """Every binding name referenced anywhere in an argument tree."""
    found = set() if found is None else found
    name = _batch_ref_name(value)
    if name is not None:
        found.add(name)
    elif isinstance(value, dict):
        for item in value.values():
            _batch_refs_in(item, found)
    elif isinstance(value, list):
        for item in value:
            _batch_refs_in(item, found)
    return found


def _batch_sentinel_refs(value: Any) -> Any:
    """Replace references with the validation sentinel, leaving the rest intact."""
    if isinstance(value, str):
        if value.startswith(_BATCH_REF_SIGIL * 2):
            return value[1:]
        return _BATCH_REF_SENTINEL if _batch_ref_name(value) is not None else value
    if isinstance(value, dict):
        return {key: _batch_sentinel_refs(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_batch_sentinel_refs(item) for item in value]
    return value


# ── schema validation (dry_run, and the pre-flight of every real run) ───────


def _validate_batch_args(schema: dict, args: dict) -> str | None:
    """Validate ``args`` against a tool's real JSON Schema. Returns a problem, or None.

    ``jsonschema`` ships with the ``mcp`` package this server already depends on,
    so it is always importable in practice; a missing one degrades to "no schema
    check" rather than to a bogus pass, and says so.

    References are validated as a wildcard: errors reported against the sentinel
    scalar are dropped, because a bound value's type is genuinely unknown until
    the step that produces it has run. Everything else still applies — required
    keys, unknown keys, and the type of every literal argument.
    """
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema arrives with `mcp`
        return None
    instance = _batch_sentinel_refs(args)
    try:
        validator = jsonschema.Draft202012Validator(schema)
        problems = [
            error
            for error in validator.iter_errors(instance)
            if error.instance != _BATCH_REF_SENTINEL
        ]
    except Exception as exc:  # a malformed schema must not take the batch down
        log.debug("schema validation skipped: %s", exc)
        return None
    if not problems:
        return None
    problems.sort(key=lambda error: list(error.absolute_path))
    first = problems[0]
    where = "/".join(str(part) for part in first.absolute_path)
    detail = " ".join(str(first.message).split())
    text = f"{where}: {detail}" if where else detail
    if len(problems) > 1:
        text = f"{text} (+{len(problems) - 1} more)"
    return text[:_BATCH_MESSAGE_CHARS]


# ── atomicity ───────────────────────────────────────────────────────────────

_BATCH_GUARANTEE_NOTES = {
    "snapshot": (
        "Rollback restores a full document snapshot taken before step 1; the restore "
        "is a synchronous document replacement that raises if it fails, and the "
        "before/after fingerprint below is checked. Cost: the checkpoint writes the "
        "whole document to a temporary DXF, so it scales with drawing size."
    ),
    "best_effort_undo": (
        "Rollback ends the AutoCAD undo group and sends '_UNDO _B' (back to the UNDO "
        "Mark transaction_begin set) to the command line. "
        "AutoCAD executes that asynchronously and does not confirm it landed, so this "
        "is best-effort, NOT atomic. The before/after fingerprint below is the only "
        "evidence available; treat a mismatch as 'the undo has not landed (yet)' and "
        "check the drawing."
    ),
    "unverified": (
        "This backend reports a transaction implementation this server does not "
        "recognise, so no claim is made about what rollback restores. The before/after "
        "fingerprint below is the only evidence."
    ),
    "none": (
        "This backend does not support transactions, so on_error='rollback' cannot be "
        "honoured. Use on_error='stop' and read `results` for what landed."
    ),
}

_BATCH_NO_CLAIM_NOTE = "on_error={mode!r}: whatever ran stays applied; `results` is the record."


def _batch_rollback_guarantee(backend) -> tuple[str, str]:
    """What a rollback on this backend is actually worth, and the honest note.

    Keyed off the backend's own ``transactions`` capability *mode* rather than
    its name, so a third backend gets classified by what it declares it does.
    """
    try:
        feature = backend.capabilities().features.get("transactions")
    except Exception as exc:  # a capability probe must never break a tool call
        log.debug("transaction capability probe failed: %s", exc)
        return "unverified", _BATCH_GUARANTEE_NOTES["unverified"]
    if feature is None or not feature.supported:
        return "none", _BATCH_GUARANTEE_NOTES["none"]
    guarantee = {"snapshot": "snapshot", "undo_mark": "best_effort_undo"}.get(
        feature.mode or "", "unverified"
    )
    return guarantee, _BATCH_GUARANTEE_NOTES[guarantee]


def _batch_transaction_ok(outcome) -> tuple[bool, str | None]:
    """Did the backend actually perform the commit/rollback we asked for?

    Both backends answer with a dict carrying ``ok``; the headless one returns
    ``{"ok": False, "error": "No active transaction to rollback"}`` when the
    checkpoint stack is empty. Returns ``(performed, declined_reason)`` so the
    payload can report the refusal instead of silently claiming the happy path.
    A non-dict answer is treated as success — that is the pre-existing contract
    for backends that return nothing meaningful.
    """
    if not isinstance(outcome, dict):
        return True, None
    if outcome.get("ok", True):
        return True, None
    return False, str(outcome.get("error") or "backend declined")


async def _batch_fingerprint(backend) -> dict | None:
    """A cheap, backend-neutral summary of document state.

    Counts plus extents: enough to *evidence* that a rollback landed rather than
    merely assert it. Necessary, not sufficient — an in-place edit that changes
    neither a count nor the extents would pass — which the payload says.
    """
    try:
        info = _dc(await backend.drawing_info())
    except Exception as exc:
        log.debug("batch fingerprint unavailable: %s", exc)
        return None
    return {
        "entity_count": info.get("entity_count"),
        "layer_count": info.get("layer_count"),
        "block_count": info.get("block_count"),
        "extents_min": [round(float(v), 6) for v in (info.get("extents_min") or ())],
        "extents_max": [round(float(v), 6) for v in (info.get("extents_max") or ())],
    }


# ── result compaction ───────────────────────────────────────────────────────


def _compact_batch_result(result: Any, verbose: bool) -> Any:
    """Trim a step result to what is not recoverable from the drawing.

    A dict carrying a ``handle`` is an EntityInfo (or an insert/copy that
    returns one): every other field of it is one ``entity_get`` away, and
    re-emitting all of them per step gives back exactly the tokens batching
    just saved. Anything without a handle — a score, a report, a measured
    value, a point — is kept whole, because nothing else can produce it.
    """
    if verbose or not isinstance(result, dict):
        return result
    handle = result.get("handle", _MISSING)
    if handle is _MISSING:
        return result
    return {"handle": handle}


_BATCH_CHECK_NOTE = (
    "document fingerprint (entity/layer/block counts + extents) captured before "
    "step 1 and re-read after the rollback. Equality is evidence, not proof: an "
    "edit changing neither a count nor the extents would pass it."
)


def _batch_atomicity(*, transaction: bool = False, **fields) -> dict:
    """The atomicity block.

    ``mode`` / ``guarantee`` / ``transaction`` / ``rolled_back`` are always
    present — those are what a caller branches on. The evidence keys
    (``committed``, ``verified``, ``before``, ``after``, ``check``) appear only
    when a checkpoint was actually opened: on a ``stop``/``continue`` batch they
    would be four nulls and two paragraphs describing a rollback that was never
    on the table, and that boilerplate measured 61% of a short batch's whole
    reply — a token bill for saying nothing.
    """
    block: dict[str, Any] = {
        "mode": "stop",
        "guarantee": "none",
        "transaction": transaction,
        "rolled_back": False,
        "note": "",
    }
    if transaction:
        block.update(
            {"committed": False, "verified": None, "before": None, "after": None},
        )
        block["check"] = _BATCH_CHECK_NOTE
    block.update(fields)
    return block


@cad_tool(
    summary="Run an ordered list of tool calls in one round trip, binding results between steps.",
    cost="destructive",
)
@mcp.tool(
    annotations={"title": "Batch: Run Tools", "readOnlyHint": False, "destructiveHint": True},
    tags={"batch"},
)
async def cad_batch(
    steps: Annotated[
        list[dict],
        Field(
            description=(
                "Ordered steps. Each: {'tool': <tool name>, 'args': {...}, "
                "'bind': <optional name>}. 'bind' names this step's result so a later "
                "step can reference it as '$name' (its handle), '$name.field' or "
                "'$name.list.0'. '$$' is a literal dollar sign."
            )
        ),
    ],
    on_error: Annotated[
        str,
        Field(
            description=(
                "stop (default): halt at the first failure, keep what already ran. "
                "continue: run every step. rollback: open a checkpoint first and undo "
                "on failure - read the returned `atomicity` block for what that is "
                "worth on this backend."
            )
        ),
    ] = "stop",
    dry_run: Annotated[
        bool,
        "Validate every step against its tool's real JSON Schema and execute nothing.",
    ] = False,
    verbose: Annotated[
        bool,
        "Return each step's full result instead of just its handle.",
    ] = False,
    ctx: Context = None,
) -> dict:
    """Execute an ordered list of tool calls in ONE round trip.

    N calls collapse into one request/response pair, and `bind` lets a later
    step reference an earlier step's result so handles never have to be echoed
    back through the model.

        steps=[
          {"tool": "entity_create_line",  "args": {...}, "bind": "edge"},
          {"tool": "point_from_snap",     "args": {"handle": "$edge", "snap": "mid"},
                                          "bind": "mid"},
          {"tool": "entity_create_circle","args": {"cx": "$mid.x", "cy": "$mid.y",
                                                   "radius": 4}},
        ]

    Successful steps report only their handle; pass verbose=True for the full
    result. Anything without a handle is returned whole.

    VALIDATION runs first, always: an unknown tool, a schema-invalid argument or
    a reference no earlier step binds refuses the whole batch before anything
    executes. `on_error` governs run-time failures only. dry_run=True returns
    that validation report and executes nothing.

    ERRORS are typed, never text: each failed step carries error.kind - one of
    unsupported (with the backend `capability`), invalid_args, refused, failed,
    unknown_tool, unresolved_ref, denied, malformed_step.

    ATOMICITY is reported, not assumed. Read the `atomicity` block: on the
    headless backend rollback restores a full document snapshot; on live AutoCAD
    it sends an UNDO whose landing AutoCAD never confirms. The default
    on_error="stop" claims nothing and is exact on both.

    NOT CALLABLE from a batch: the raw command/LISP escape hatches, and
    cad_batch itself. Call those directly.

    For a few hundred entities of the same kind, entity_batch_create is denser
    still (no per-step tool name) - and it can be one step of a cad_batch.
    """
    mode = (on_error or "stop").lower().strip()
    if mode not in BATCH_ON_ERROR_MODES:
        raise ToolError(
            f"cad_batch: on_error must be one of {BATCH_ON_ERROR_MODES}, got {on_error!r}."
        )
    if not steps:
        raise ToolError("cad_batch: `steps` is empty; there is nothing to run.")
    if len(steps) > MAX_BATCH_STEPS:
        raise ToolError(
            f"cad_batch: {len(steps)} steps exceeds the {MAX_BATCH_STEPS}-step ceiling. "
            "Steps run sequentially inside one uncancellable call - split the work."
        )

    denied = _batch_denied_tools()
    read_only = _batch_read_only_tools()
    plans: list[dict] = []
    bound_names: set[str] = set()
    problems: list[str] = []

    for index, step in enumerate(steps):
        row: dict = {"i": index, "tool": None, "status": "valid"}

        def _invalid(kind: str, message: str, row: dict = row, index: int = index) -> None:
            row["status"] = "invalid"
            row["error"] = {"kind": kind, "message": message}
            problems.append(f"step {index}: {message}")

        if not isinstance(step, dict):
            _invalid("malformed_step", f"step {index} is {type(step).__name__}, not an object")
            plans.append(row)
            continue
        name = step.get("tool")
        args = step.get("args") if step.get("args") is not None else {}
        bind = step.get("bind")
        row["tool"] = name if isinstance(name, str) else None
        unknown_keys = sorted(set(step) - {"tool", "args", "bind"})
        if not isinstance(name, str) or not name:
            _invalid("malformed_step", f"step {index} has no 'tool' name")
        elif not isinstance(args, dict):
            _invalid("malformed_step", f"step {index} 'args' is not an object")
        elif bind is not None and (not isinstance(bind, str) or not bind):
            _invalid("malformed_step", f"step {index} 'bind' must be a non-empty string")
        elif unknown_keys:
            _invalid(
                "malformed_step",
                f"step {index} has unexpected key(s) {unknown_keys}; a step is "
                "{'tool', 'args', 'bind'}",
            )
        elif name in denied:
            _invalid(
                "denied",
                f"{name!r} is denied inside cad_batch: one batch grant must not reach "
                "the raw escape hatches (or another batch executor). Call it directly, "
                "where its own gate applies.",
            )
        else:
            tool = await ctx.fastmcp.get_tool(name)
            card = (getattr(tool, "meta", None) or {}).get("cad") or {}
            if tool is None:
                _invalid(
                    "unknown_tool",
                    f"{name!r} is not a callable tool here (unregistered, or hidden by "
                    "the active TOOL_PROFILE / TOOL_PACKS / ENABLE_3D gate).",
                )
            elif card.get("cost") == "escape":
                # The deny set is computed from the local registry; this repeats
                # the check against the *resolved* tool, which is what actually
                # runs. A mounted or transformed provider can supply a tool the
                # local snapshot never saw.
                _invalid(
                    "denied",
                    f"{name!r} is an escape-hatch tool (cost='escape') and is denied "
                    "inside cad_batch. Call it directly, where its own gate applies.",
                )
            elif not card:
                # Fail closed on unknown provenance. This is not a nicety: under
                # DISCOVERY_MODE=search the advertised surface is search_tools +
                # `call_tool`, and `call_tool` invokes any tool by name. It is a
                # general-purpose proxy living in the same server as this
                # denylist, and being card-less is the ONLY thing that stops
                # cad_batch -> call_tool -> system_run_command. The deny set
                # above cannot see it: it is transform-supplied, not a local
                # component with a cost card.
                #
                # So: giving `call_tool` a @cad_tool card - reasonable-sounding,
                # since it would make the proxy costed and discoverable -
                # reopens the escalation path. Do that only alongside an
                # explicit denial for it. tests/test_cad_batch.py pins this.
                _invalid(
                    "denied",
                    f"{name!r} carries no @cad_tool discovery card, so cad_batch cannot "
                    "tell what it costs and will not run it blind. Call it directly.",
                )
            else:
                missing = sorted(_batch_refs_in(args) - bound_names)
                if missing:
                    _invalid(
                        "unresolved_ref",
                        f"step {index} references {missing} which no earlier step binds "
                        f"(bound by step {index}: {sorted(bound_names) or 'nothing'}).",
                    )
                else:
                    detail = _validate_batch_args(tool.parameters or {}, args)
                    if detail:
                        _invalid("invalid_args", f"{name}: {detail}")
                    else:
                        row["_args"] = args
                        row["_bind"] = bind
                        if bind:
                            bound_names.add(bind)
        plans.append(row)

    if dry_run:
        invalid = sum(1 for row in plans if row["status"] == "invalid")
        guarantee, note = ("none", _BATCH_NO_CLAIM_NOTE.format(mode=mode))
        if mode == "rollback":
            guarantee, note = _batch_rollback_guarantee(_backend(ctx))
            note = f"Planned only - a dry run opens no checkpoint. {note}"
        return {
            "ok": invalid == 0,
            "dry_run": True,
            "on_error": mode,
            "steps": len(steps),
            "executed": 0,
            "valid": len(plans) - invalid,
            "invalid": invalid,
            "bindings": sorted(bound_names),
            "results": [{k: v for k, v in row.items() if not k.startswith("_")} for row in plans],
            "atomicity": _batch_atomicity(mode=mode, guarantee=guarantee, note=note),
            "note": (
                "Validated against each tool's real JSON Schema; nothing was executed. "
                "A '$reference' validates as a wildcard because its type is only known "
                "once the step that binds it has run."
            ),
        }

    if problems:
        raise ToolError(
            f"cad_batch refused all {len(steps)} steps - nothing was executed. "
            + "; ".join(problems[:5])
            + ("" if len(problems) <= 5 else f" (+{len(problems) - 5} more)")
        )

    backend = _backend(ctx)
    guarantee, note = "none", _BATCH_NO_CLAIM_NOTE.format(mode=mode)
    transaction = False
    before = None
    if mode == "rollback":
        guarantee, note = _batch_rollback_guarantee(backend)
        if guarantee == "none":
            raise ToolError(f"cad_batch: on_error='rollback' is unavailable here. {note}")
        before = await _batch_fingerprint(backend)
        opened = await backend.transaction_begin()
        if not (isinstance(opened, dict) and opened.get("ok")):
            reason = (opened or {}).get("error", "transaction_begin declined")
            return {
                "ok": False,
                "dry_run": False,
                "on_error": mode,
                "steps": len(steps),
                "executed": 0,
                "succeeded": 0,
                "failed": 0,
                "skipped": len(steps),
                "bindings": [],
                "results": [
                    {"i": row["i"], "tool": row["tool"], "status": "skipped", "reason": reason}
                    for row in plans
                ],
                "atomicity": _batch_atomicity(
                    mode=mode,
                    guarantee=guarantee,
                    note=(
                        f"No checkpoint was opened ({reason}), so nothing ran: a "
                        "transaction this call did not open is not one it can roll back "
                        f"to. {note}"
                    ),
                ),
            }
        transaction = True

    bindings: dict[str, Any] = {}
    results: list[dict] = []
    succeeded = failed = executed = 0
    total = len(plans)
    await ctx.info(f"cad_batch: {total} step(s), on_error={mode}")

    for position, plan in enumerate(plans):
        await ctx.report_progress(position, total)
        name = plan["tool"]
        row: dict = {"i": plan["i"], "tool": name}
        if failed and mode == "stop":
            row["status"] = "skipped"
            row["reason"] = "an earlier step failed (on_error='stop')"
            results.append(row)
            continue
        try:
            args = _substitute_batch_refs(plan["_args"], bindings)
        except BatchReferenceError as exc:
            failed += 1
            row["status"] = "error"
            row["error"] = {"kind": "unresolved_ref", "message": _batch_message(exc)}
            log.warning("BATCH %d/%d %-38s unresolved_ref", position + 1, total, name)
            results.append(row)
            continue
        try:
            # run_middleware=False: the cad_batch call itself is already through
            # the audit/timing chain, and skipping it keeps the error one wrap
            # deep instead of three, which is what makes the taxonomy above
            # readable. Each step is logged here instead, tagged as a batch step.
            outcome = await ctx.fastmcp.call_tool(name, args, run_middleware=False)
        except Exception as exc:
            failed += 1
            executed += 1
            row["status"] = "error"
            row["error"] = _classify_batch_error(exc)
            log.warning("BATCH %d/%d %-38s %s", position + 1, total, name, row["error"]["kind"])
            results.append(row)
            if mode == "rollback":
                break
            continue
        executed += 1
        structured = getattr(outcome, "structured_content", None)
        # A tool may decline *by value* instead of raising: an `is_error` result
        # from the refusal middleware, or an `{"ok": False, ...}` payload. Both
        # mean the step did not do its work, and counting either as succeeded is
        # the same class of lie as the DWG bytes and the unverified rollback —
        # the batch would report succeeded:1 over work that never happened.
        #
        # `ok: False` only carries that meaning for a tool that was supposed to
        # ACT. On a read-only checker it means "I looked and found something",
        # and treating that as a refusal is how a `validation_check` step came
        # to trigger a rollback that destroyed the batch's own geometry.
        refused = bool(getattr(outcome, "is_error", False)) or (
            name not in read_only and isinstance(structured, dict) and structured.get("ok") is False
        )
        if refused:
            failed += 1
            declined_payload = structured if isinstance(structured, dict) else {}
            capability = declined_payload.get("capability")
            row["status"] = "error"
            row["error"] = {
                "kind": "unsupported" if capability else "refused",
                **({"capability": capability} if capability else {}),
                "message": _compact_batch_message(
                    str(declined_payload.get("error") or "the tool declined")
                ),
            }
            log.warning("BATCH %d/%d %-38s %s", position + 1, total, name, row["error"]["kind"])
            results.append(row)
            # `failed` is what on_error='stop' reads at the top of the loop, so
            # incrementing it above is the whole stop mechanism.
            if mode == "rollback":
                break
            continue
        succeeded += 1
        if plan["_bind"]:
            bindings[plan["_bind"]] = structured
            row["bind"] = plan["_bind"]
        row["status"] = "ok"
        row["result"] = _compact_batch_result(structured, verbose)
        log.info("BATCH %d/%d %-38s ok", position + 1, total, name)
        results.append(row)

    await ctx.report_progress(total, total)

    committed = rolled_back = False
    verified = None
    after = None
    declined = None
    if transaction:
        if failed:
            await ctx.warning("cad_batch: rolling back")
            # Read the backend's answer instead of asserting success from
            # control flow. A step is free to call transaction_commit and pop
            # the checkpoint out from under us, in which case the headless
            # backend returns {"ok": False, "error": "No active transaction to
            # rollback"} and the caller's work is still there. Reporting
            # rolled_back=True off the bare await would be the same class of
            # lie as writing DXF bytes into a .dwg.
            rolled_back, declined = _batch_transaction_ok(await backend.transaction_rollback())
            after = await _batch_fingerprint(backend)
            verified = None if (before is None or after is None) else before == after
        else:
            committed, declined = _batch_transaction_ok(await backend.transaction_commit())

    ok = failed == 0
    atomicity = _batch_atomicity(
        mode=mode, guarantee=guarantee, transaction=transaction, rolled_back=rolled_back, note=note
    )
    if transaction:
        atomicity.update(
            {"committed": committed, "verified": verified, "before": before, "after": after}
        )
    payload = {
        "ok": ok,
        "dry_run": False,
        "on_error": mode,
        "steps": total,
        "executed": executed,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": sum(1 for row in results if row["status"] == "skipped"),
        "bindings": sorted(bindings),
        "results": results,
        "atomicity": atomicity,
    }
    if declined:
        payload["atomicity"]["note"] = (
            f"BACKEND DECLINED THE {'ROLLBACK' if failed else 'COMMIT'} ({declined}). "
            "The checkpoint was gone before we reached it - most likely a step called "
            "transaction_commit or transaction_rollback itself. Nothing was undone; "
            f"the steps that succeeded are still in the document. {note}"
        )
    elif rolled_back and verified is False:
        payload["atomicity"]["note"] = (
            "ROLLBACK NOT VERIFIED - the document fingerprint differs from the one "
            f"taken before step 1. {note}"
        )
    return payload


@cad_tool(summary="Draw many entities in one call instead of one round trip each.", cost="mutate")
@mcp.tool(
    annotations={"title": "Batch Create Entities", "readOnlyHint": False},
    tags={"entity", "create", "batch"},
)
async def entity_batch_create(
    entities: Annotated[
        list[dict],
        "List of entity definitions. Each dict must have 'type' and type-specific params. Types: line, circle, arc, polyline, rectangle, text, point",
    ],
    ctx: Context = None,
) -> dict:
    """Create multiple entities in a single call for better performance.

    Each entity dict must have a 'type' key and the parameters for that type.
    Example: [{"type": "line", "x1": 0, "y1": 0, "x2": 100, "y2": 0}, {"type": "circle", "cx": 50, "cy": 50, "radius": 25}]

    Denser than cad_batch for many entities of the same kind (no per-step tool
    name), and usable as one step *of* a cad_batch. Use cad_batch when the calls
    differ, must be ordered, or must feed each other.
    """
    b = _backend(ctx)
    results = []
    errors = []
    total = len(entities)
    await ctx.info(f"Batch creating {total} entities")

    create_map = {
        "line": b.entity_create_line,
        "circle": b.entity_create_circle,
        "arc": b.entity_create_arc,
        "polyline": b.entity_create_polyline,
        "text": b.entity_create_text,
        "mtext": b.entity_create_mtext,
        "point": b.entity_create_point,
        "hatch": b.entity_create_hatch,
        "spline": b.entity_create_spline,
        "ellipse": b.entity_create_ellipse,
    }

    for i, ent_def in enumerate(entities):
        await ctx.report_progress(i, total)
        ent_type = ent_def.pop("type", None)
        if not ent_type:
            errors.append({"index": i, "error": "Missing 'type' key"})
            continue
        creator = create_map.get(ent_type.lower())
        if not creator:
            errors.append({"index": i, "error": f"Unknown type: {ent_type}"})
            continue
        try:
            info = await creator(**ent_def)
            results.append(_dc(info))
        except Exception as exc:
            errors.append({"index": i, "type": ent_type, "error": str(exc)})

    await ctx.report_progress(total, total)
    return {"created": len(results), "errors": errors, "entities": results}


@cad_tool(
    summary="Move, rotate, scale, restyle or delete many entities in one call.",
    cost="destructive",
)
@mcp.tool(
    annotations={"title": "Batch Modify Entities", "readOnlyHint": False},
    tags={"entity", "modify", "batch"},
)
async def entity_batch_modify(
    operations: Annotated[
        list[dict],
        "List of operations. Each dict: {handle, action, ...params}. Actions: move(dx,dy), rotate(base_x,base_y,angle_deg), scale(base_x,base_y,factor), delete, set_properties(layer,color,...)",
    ],
    ctx: Context = None,
) -> dict:
    """Apply multiple modifications in a single call.

    Example: [{"handle": "1A", "action": "move", "dx": 10, "dy": 20}, {"handle": "2B", "action": "delete"}]

    Covers move/rotate/scale/delete/set_properties only. For anything else, for
    ordering, or to feed one step's result into the next, use cad_batch.
    """
    b = _backend(ctx)
    results = []
    errors = []
    total = len(operations)
    await ctx.info(f"Batch modifying {total} entities")

    for i, op in enumerate(operations):
        await ctx.report_progress(i, total)
        handle = op.get("handle")
        action = op.get("action", "").lower()
        if not handle or not action:
            errors.append({"index": i, "error": "Missing 'handle' or 'action'"})
            continue
        try:
            if action == "move":
                await b.entity_move(handle, op.get("dx", 0), op.get("dy", 0), op.get("dz", 0))
                results.append({"handle": handle, "action": "move", "ok": True})
            elif action == "rotate":
                await b.entity_rotate(handle, op["base_x"], op["base_y"], op["angle_deg"])
                results.append({"handle": handle, "action": "rotate", "ok": True})
            elif action == "scale":
                await b.entity_scale(handle, op["base_x"], op["base_y"], op["factor"])
                results.append({"handle": handle, "action": "scale", "ok": True})
            elif action == "delete":
                await b.entity_delete(handle)
                results.append({"handle": handle, "action": "delete", "ok": True})
            elif action == "set_properties":
                await b.entity_set_properties(
                    handle,
                    layer=op.get("layer"),
                    color=op.get("color"),
                    linetype=op.get("linetype"),
                    lineweight=op.get("lineweight"),
                    visible=op.get("visible"),
                )
                results.append({"handle": handle, "action": "set_properties", "ok": True})
            else:
                errors.append({"index": i, "error": f"Unknown action: {action}"})
        except Exception as exc:
            errors.append({"index": i, "handle": handle, "action": action, "error": str(exc)})

    await ctx.report_progress(total, total)
    return {"modified": len(results), "errors": errors, "results": results}


# ---------------------------------------------------------------------------
# ── SECTION 8c: Templates (2 tools) ──────────────────────────────────────
# ---------------------------------------------------------------------------

_LAYER_TEMPLATES = {
    "architectural": [
        {"name": "WALLS", "color": 7, "linetype": "Continuous", "lineweight": 50},
        {"name": "DOORS", "color": 3, "linetype": "Continuous", "lineweight": 25},
        {"name": "WINDOWS", "color": 4, "linetype": "Continuous", "lineweight": 25},
        {"name": "FURNITURE", "color": 8, "linetype": "Continuous", "lineweight": 13},
        {"name": "DIMENSIONS", "color": 2, "linetype": "Continuous", "lineweight": 13},
        {"name": "TEXT", "color": 7, "linetype": "Continuous", "lineweight": 13},
        {"name": "GRID", "color": 9, "linetype": "Continuous", "lineweight": 13},
        {"name": "HATCHING", "color": 8, "linetype": "Continuous", "lineweight": 13},
    ],
    "mechanical": [
        {"name": "VISIBLE", "color": 7, "linetype": "Continuous", "lineweight": 50},
        {"name": "HIDDEN", "color": 1, "linetype": "Continuous", "lineweight": 25},
        {"name": "CENTER", "color": 3, "linetype": "Continuous", "lineweight": 13},
        {"name": "DIMENSIONS", "color": 2, "linetype": "Continuous", "lineweight": 13},
        {"name": "SECTION", "color": 5, "linetype": "Continuous", "lineweight": 50},
        {"name": "HATCHING", "color": 8, "linetype": "Continuous", "lineweight": 13},
        {"name": "PHANTOM", "color": 4, "linetype": "Continuous", "lineweight": 13},
        {"name": "ANNOTATIONS", "color": 7, "linetype": "Continuous", "lineweight": 13},
        {"name": "BORDER", "color": 7, "linetype": "Continuous", "lineweight": 100},
    ],
    "electrical": [
        {"name": "POWER_LINES", "color": 7, "linetype": "Continuous", "lineweight": 50},
        {"name": "CONTROL_LINES", "color": 3, "linetype": "Continuous", "lineweight": 25},
        {"name": "COMPONENTS", "color": 2, "linetype": "Continuous", "lineweight": 25},
        {"name": "TERMINALS", "color": 4, "linetype": "Continuous", "lineweight": 25},
        {"name": "WIRE_NUMBERS", "color": 7, "linetype": "Continuous", "lineweight": 13},
        {"name": "COMPONENT_TAGS", "color": 8, "linetype": "Continuous", "lineweight": 13},
        {"name": "BORDER", "color": 7, "linetype": "Continuous", "lineweight": 100},
    ],
    "piping": [
        {"name": "PROCESS_LINES", "color": 7, "linetype": "Continuous", "lineweight": 50},
        {"name": "UTILITY_LINES", "color": 3, "linetype": "Continuous", "lineweight": 25},
        {"name": "INSTRUMENTS", "color": 2, "linetype": "Continuous", "lineweight": 25},
        {"name": "EQUIPMENT", "color": 5, "linetype": "Continuous", "lineweight": 50},
        {"name": "VALVES", "color": 4, "linetype": "Continuous", "lineweight": 25},
        {"name": "TAGS", "color": 7, "linetype": "Continuous", "lineweight": 13},
        {"name": "ANNOTATIONS", "color": 8, "linetype": "Continuous", "lineweight": 13},
        {"name": "BORDER", "color": 7, "linetype": "Continuous", "lineweight": 100},
    ],
}


@cad_tool(
    summary="Create a standard layer set: architectural, mechanical, electrical or piping.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Apply Layer Template", "readOnlyHint": False},
    tags={"template", "layer"},
)
async def template_apply_layers(
    template: Annotated[str, "Template name: architectural, mechanical, electrical, piping"],
    ctx: Context = None,
) -> dict:
    """Apply a standard layer set from a predefined template.

    Available templates: architectural, mechanical, electrical, piping.
    Creates all layers defined in the template with standard colors and lineweights.
    """
    template_key = template.lower().strip()
    if template_key not in _LAYER_TEMPLATES:
        available = ", ".join(_LAYER_TEMPLATES.keys())
        raise ToolError(f"Unknown template '{template}'. Available: {available}")

    b = _backend(ctx)
    layers_def = _LAYER_TEMPLATES[template_key]
    created = []
    await ctx.info(f"Applying '{template_key}' layer template ({len(layers_def)} layers)")

    for ldef in layers_def:
        await b.layer_create(
            ldef["name"],
            color=ldef["color"],
            linetype=ldef["linetype"],
            lineweight=ldef["lineweight"],
        )
        created.append(ldef["name"])

    return {"ok": True, "template": template_key, "layers_created": created, "count": len(created)}


@cad_tool(summary="Show the available layer templates and the layers in each.", cost="read")
@mcp.tool(
    annotations={"title": "List Available Templates", "readOnlyHint": True},
    tags={"template", "query"},
)
async def template_list(ctx: Context = None) -> dict:
    """List all available layer templates and their contents."""
    result = {}
    for name, layers in _LAYER_TEMPLATES.items():
        result[name] = {
            "layer_count": len(layers),
            "layers": [ldef["name"] for ldef in layers],
        }
    return {"templates": result}


# ---------------------------------------------------------------------------
# ── SECTION 8d: Validation (1 tool) ──────────────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(
    summary="Sanity-check the drawing for empty layers, zero-length lines and duplicates.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Validate Drawing", "readOnlyHint": True},
    tags={"analysis", "validation"},
)
async def validation_check(
    checks: Annotated[
        list[str], "List of checks: empty_layers, zero_length, duplicate_entities"
    ] = None,
    ctx: Context = None,
) -> dict:
    """Run quality checks on the current drawing.

    Available checks:
    - empty_layers: Find layers with no entities
    - zero_length: Find zero-length lines
    - duplicate_entities: Find entities at the same position
    """
    if checks is None:
        checks = ["empty_layers", "zero_length"]

    b = _backend(ctx)
    await ctx.info(f"Running validation checks: {', '.join(checks)}")
    issues = []

    if "empty_layers" in checks:
        layers = await b.layer_list()
        all_entities = await b.entity_list(limit=50000)
        used_layers = {e.layer for e in all_entities}
        for lyr in layers:
            if lyr.name != "0" and lyr.name not in used_layers:
                issues.append(
                    {
                        "check": "empty_layers",
                        "severity": "info",
                        "message": f"Layer '{lyr.name}' has no entities",
                        "layer": lyr.name,
                    }
                )

    if "zero_length" in checks:
        lines = await b.entity_list(type_filter="LINE", limit=10000)
        for line in lines:
            props = line.properties or {}
            start = props.get("start", [])
            end = props.get("end", [])
            if start and end and len(start) >= 2 and len(end) >= 2:
                dx = end[0] - start[0]
                dy = end[1] - start[1]
                length = (dx * dx + dy * dy) ** 0.5
                if length < 0.001:
                    issues.append(
                        {
                            "check": "zero_length",
                            "severity": "warning",
                            "message": f"Zero-length line at ({start[0]:.1f}, {start[1]:.1f})",
                            "handle": line.handle,
                        }
                    )

    return {
        "ok": len(issues) == 0,
        "total_issues": len(issues),
        "issues": issues,
        "checks_run": checks,
    }


# ---------------------------------------------------------------------------
# ── SECTION 9: View & Screenshot (4 tools) ──────────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(summary="Zoom out until every entity fits on screen.", cost="safe")
@mcp.tool(
    annotations={"title": "Zoom Extents", "readOnlyHint": False, "destructiveHint": False},
    tags={"view"},
)
async def view_zoom_extents(ctx: Context = None) -> dict:
    """Zoom to show all entities in the drawing (fit drawing in viewport)."""
    return await _backend(ctx).view_zoom_extents()


@cad_tool(summary="Zoom in on a rectangular region of the drawing.", cost="safe")
@mcp.tool(
    annotations={"title": "Zoom Window", "readOnlyHint": False, "destructiveHint": False},
    tags={"view"},
)
async def view_zoom_window(
    x1: Annotated[float, "Window corner 1 X"],
    y1: Annotated[float, "Window corner 1 Y"],
    x2: Annotated[float, "Window corner 2 X"],
    y2: Annotated[float, "Window corner 2 Y"],
    ctx: Context = None,
) -> dict:
    """Zoom to display the specified rectangular window region."""
    return await _backend(ctx).view_zoom_window(x1, y1, x2, y2)


@cad_tool(summary="Capture a PNG picture of the drawing as it currently looks.", cost="read")
@mcp.tool(
    annotations={"title": "Screenshot", "readOnlyHint": True},
    tags={"view", "screenshot"},
)
async def view_screenshot(
    overlay_handles: Annotated[
        bool,
        "Label each entity with its handle, so what you see maps to what you "
        "can modify. Headless backend only.",
    ] = False,
    ctx: Context = None,
):
    """Capture a screenshot of the current drawing view.

    COM backend: captures live AutoCAD window at current view.
    ezdxf backend: renders via matplotlib to PNG.

    With `overlay_handles`, each entity is labelled with its handle at its own
    centre — every modify tool takes a handle, and without the labels there is
    nothing connecting "the circle at the top-left" to a hex string you can act
    on. Crowded drawings are capped and the image says how many of how many were
    labelled. Live AutoCAD captures its own window, so there is no render to
    label there; it refuses with `capability: "handle_overlay"`.

    Returns an Image content block with the PNG data.
    """
    from fastmcp.utilities.types import Image

    await ctx.info("Capturing drawing screenshot")
    await ctx.report_progress(0, 100)

    b = _backend(ctx)
    png_bytes = await b.view_screenshot(overlay_handles=overlay_handles)

    await ctx.report_progress(100, 100)

    if png_bytes is None:
        raise ToolError(
            "Screenshot not available. "
            "For COM backend: ensure AutoCAD window is visible. "
            "For ezdxf backend: install matplotlib (pip install matplotlib)."
        )

    return Image(data=png_bytes, format="png")


@cad_tool(
    summary="Fit the drawing on screen, then capture it: the quickest visual check.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Zoom and Screenshot", "readOnlyHint": True},
    tags={"view", "screenshot"},
)
async def view_zoom_and_screenshot(
    x1: Annotated[float | None, "Optional: zoom to this window corner X1"] = None,
    y1: Annotated[float | None, "Optional: zoom to window corner Y1"] = None,
    x2: Annotated[float | None, "Optional: zoom to window corner X2"] = None,
    y2: Annotated[float | None, "Optional: zoom to window corner Y2"] = None,
    ctx: Context = None,
):
    """Zoom to extents (or window if coordinates given), then capture a screenshot.

    The most useful tool for visually inspecting drawing state.
    """
    from fastmcp.utilities.types import Image

    await ctx.info("Zooming and capturing screenshot")
    b = _backend(ctx)

    await ctx.report_progress(10, 100)
    if x1 is not None and y1 is not None and x2 is not None and y2 is not None:
        await b.view_zoom_window(x1, y1, x2, y2)
    else:
        await b.view_zoom_extents()

    await ctx.report_progress(50, 100)
    png_bytes = await b.view_screenshot()
    await ctx.report_progress(100, 100)

    if png_bytes is None:
        raise ToolError("Screenshot unavailable. Check backend capabilities.")

    return Image(data=png_bytes, format="png")


# ---------------------------------------------------------------------------
# ── SECTION 10: Transactions (3 tools) ──────────────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(summary="Open a rollback checkpoint before a risky edit.", cost="mutate")
@mcp.tool(
    annotations={"title": "Begin Transaction", "readOnlyHint": False, "destructiveHint": False},
    tags={"transaction"},
)
async def transaction_begin(ctx: Context = None) -> dict:
    """Begin a transaction (undo mark).

    COM backend: Sets AutoCAD undo mark. All subsequent operations can be
    rolled back to this point with transaction_rollback.

    ezdxf backend: Saves a DXF snapshot. Rollback restores the full document
    state to this point.

    Always pair with transaction_commit or transaction_rollback.
    """
    await ctx.info("Beginning transaction")
    return await _backend(ctx).transaction_begin()


@cad_tool(summary="Keep the changes and drop the checkpoint.", cost="safe")
@mcp.tool(
    annotations={"title": "Commit Transaction", "readOnlyHint": False, "destructiveHint": False},
    tags={"transaction"},
)
async def transaction_commit(ctx: Context = None) -> dict:
    """Commit the current transaction.

    COM: Ends the undo mark (changes are permanent but still undoable via drawing_undo).
    ezdxf: Discards the rollback snapshot (changes are kept).
    """
    await ctx.info("Committing transaction")
    return await _backend(ctx).transaction_commit()


@cad_tool(
    summary="Throw away every change made since transaction_begin.",
    cost="destructive",
)
@mcp.tool(
    annotations={"title": "Rollback Transaction", "readOnlyHint": False, "destructiveHint": True},
    tags={"transaction"},
)
async def transaction_rollback(ctx: Context = None) -> dict:
    """Rollback the current transaction to the point of transaction_begin.

    COM: Undoes all operations back to the UNDO Mark transaction_begin set;
    refused (`ok: false`) when no transaction is active.
    ezdxf: Restores the document from the saved DXF snapshot.

    WARNING: This is destructive – all changes since transaction_begin are lost.
    """
    await ctx.warning("Rolling back transaction")
    return await _backend(ctx).transaction_rollback()


# ---------------------------------------------------------------------------
# ── SECTION 11: System (8 tools) ────────────────────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(
    summary="Check the connection: which engine is live and what document is open.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Server Status", "readOnlyHint": True},
    tags={"system"},
)
async def system_status(ctx: Context = None) -> dict:
    """Get full status of the AutoCAD MCP Pro server and backend connection.

    Returns backend name, connection status, capabilities, document info.
    """
    b = ctx.lifespan_context.get("backend")
    # R20: tool_count may be None when the registry is unavailable; omit the
    # key rather than surface a bogus value (e.g. the old -1).
    tool_count = await _registered_tool_count()
    unsafe = config.settings.dangerous_commands_enabled
    if b is None:
        out = {
            "server": "AutoCAD MCP Pro",
            "backend": "none",
            "connected": False,
            "unsafe_mode": unsafe,
            "error": ctx.lifespan_context.get("init_error"),
            "hint": "Set AUTOCAD_MCP_BACKEND=ezdxf to use headless mode, or start AutoCAD for COM mode.",
        }
        if tool_count is not None:
            out["tool_count"] = tool_count
        return out
    status = await b.system_status()
    status["server"] = "AutoCAD MCP Pro"
    if tool_count is not None:
        status["tool_count"] = tool_count
    status["unsafe_mode"] = unsafe
    if unsafe:
        status["unsafe_mode_warning"] = (
            "DANGEROUS_COMMANDS_ENABLED=true — command/LISP sanitization disabled."
        )
    return status


@cad_tool(summary="Ask the active backend which features it really supports.", cost="read")
@mcp.tool(
    annotations={"title": "Backend Capabilities", "readOnlyHint": True},
    tags={"system", "query"},
)
async def system_capabilities(ctx: Context = None) -> dict:
    """Return machine-readable support modes for the active backend."""
    return _backend(ctx).capabilities().to_dict()


@cad_tool(summary="Read one AutoCAD system variable, such as DIMSCALE or LTSCALE.", cost="read")
@mcp.tool(
    annotations={"title": "Get System Variable", "readOnlyHint": True},
    tags={"system"},
)
async def system_get_variable(
    name: Annotated[
        str, "System variable name (e.g. DIMSCALE, LTSCALE, INSUNITS, CLAYER, MEASUREMENT)"
    ],
    ctx: Context = None,
) -> dict:
    """Get an AutoCAD system variable value."""
    value = await _backend(ctx).system_get_variable(name)
    return {"variable": name, "value": value}


@cad_tool(summary="Set one AutoCAD system variable by name.", cost="safe")
@mcp.tool(
    annotations={"title": "Set System Variable", "readOnlyHint": False},
    tags={"system"},
)
async def system_set_variable(
    name: Annotated[str, "System variable name"],
    value: Annotated[Any, "New variable value"],
    ctx: Context = None,
) -> dict:
    """Set an AutoCAD system variable (e.g. DIMSCALE, LTSCALE, MEASUREMENT).

    Refusals, before anything is written: a value outside the catalogued
    range or enum of a known variable, or a write to a read-only variable
    (DIMSTYLE, CANNOSCALEVALUE, DWGNAME, …), is refused with the catalogue's
    message — `system_variable_describe(name)` shows the range. A variable the
    catalogue does not know is passed through unchanged. The headless engine
    additionally refuses registry-saved variables (`capability:
    registry_sysvar`) because a file has nowhere to keep them, and names that
    ezdxf has no header slot for (a `ValueError` naming the variable).
    """
    from engineering.standards.sysvars import check_sysvar_value

    message = check_sysvar_value(name, value)
    if message:
        raise ToolError(f"system_set_variable refused: {message}")
    return await _backend(ctx).system_set_variable(name, value)


@cad_tool(
    summary="Read or change drawing units, precision, linetype and dimension scale by name.",
    cost="safe",
)
@mcp.tool(
    annotations={
        "title": "Drawing Settings (read / change)",
        "readOnlyHint": False,
        "destructiveHint": False,
    },
    tags={"system"},
)
async def drawing_settings(
    settings: Annotated[
        dict | None,
        Field(
            default=None,
            description=(
                "Omit to READ every setting; pass a dict to CHANGE them. Friendly keys: "
                "units (mm/cm/m/inch/feet), linear_precision, angular_precision, ltscale, "
                "dimscale, dim_text_height, dim_arrow_size, dim_decimals, "
                'decimal_separator ("." or ","), zero_suppression, text_size, point_mode, '
                "point_size, osmode, fillet_radius, limits ([[xmin,ymin],[xmax,ymax]]), "
                "grid (bool), grid_spacing, snap (bool), snap_spacing, ortho (bool), "
                "polar (bool), polar_angle (degrees), psltscale (bool), annotation_scale "
                '("1:50"), linear_units (decimal|engineering|architectural|fractional|'
                "scientific), angular_units (degrees|dms|grads|radians|surveyor), "
                "dimstyle, textstyle (current style names). "
                'Example: {"units": "mm", "limits": [[0, 0], [420, 297]], "grid": true}.'
            ),
        ),
    ] = None,
    ctx: Context = None,
) -> dict:
    """Read or change common AutoCAD drawing settings by friendly name.

    A convenience facade over the system variables (INSUNITS, LUPREC, LTSCALE,
    DIMSCALE, DIMTXT, DIMASZ, DIMDEC, DIMDSEP, DIMZIN, TEXTSIZE, OSMODE, LIMMIN/
    LIMMAX, GRIDMODE/GRIDUNIT, SNAPMODE/SNAPUNIT, ORTHOMODE, AUTOSNAP/POLARANG,
    PSLTSCALE, CANNOSCALE, LUNITS, AUNITS, DIMSTYLE, TEXTSTYLE) so the user can
    say "set units to mm, limits to A3 and the grid on" without memorising
    sysvar names. Call with no argument for a full snapshot; a write returns
    `applied`, `changed` ({key: [old, new]} — only what moved) and `errors`.

    `dim_text_height` / `dim_arrow_size` / `dim_decimals` / `decimal_separator`
    / `zero_suppression` shape the *dimension* — `text_size` is TEXTSIZE, the
    height of a standalone TEXT entity, and does not touch dimensions.

    Refusals, per key, nothing else rolled back: an unknown key; a value outside
    its range (grid/snap spacing > 0, polar_angle 0–360, precision 0–8, …); a
    malformed `limits` or `annotation_scale`; `osmode` / `polar` / `polar_angle`
    on the headless engine (`capability: registry_sysvar` — AutoCAD keeps them in
    the registry, a file cannot, and a headless snapshot reports them as `None`);
    `dimstyle` / `textstyle` when the backend has no styles contract. On the
    live engine `annotation_scale` must name a scale in the drawing's scale list
    (SCALELISTEDIT) and AutoCAD refuses the write on a
    paper-space layout with no active viewport; the headless engine adds the
    scale, seeding AutoCAD's default list first when the drawing has none,
    and refuses it on an R12 file (no OBJECTS section, so the value would
    vanish at save — save as R2000 or newer first; limits/grid/ortho survive).
    Headlessly, grid/snap are stored on the active VPORT and the annotation
    scale in the variable dictionary — the places AutoCAD reads them from.
    """
    if settings:
        await ctx.info(f"Applying drawing settings: {', '.join(settings)}")
    return await _backend(ctx).drawing_settings(settings)


@cad_tool(
    summary="Send a raw command string to the AutoCAD command line (COM only).",
    cost="escape",
)
@mcp.tool(
    annotations={"title": "Run AutoCAD Command", "readOnlyHint": False},
    tags={"system"},
)
async def system_run_command(
    command: Annotated[str, "AutoCAD command string (e.g. '_ZOOM E', '_REGEN', '_EXPLODE')"],
    ctx: Context = None,
) -> dict:
    """Execute an AutoCAD command string directly (COM backend only).

    Append \\n for Enter. Example: '_LINE 0,0 100,0 \\n'.

    IMPORTANT: commands that finish at an option menu (e.g. -LINETYPE, -LAYER,
    -STYLE return to '[?/Create/Load/Set]:' after their action) need an EXTRA
    blank line or '_X\\n' to exit, otherwise AutoCAD stays at a prompt and the
    next COM call will deadlock. Example: '_-LINETYPE _LOAD CENTER acad.lin\\n\\n'.

    A verb denylist refuses obviously destructive commands, but it is a guardrail
    against issuing `ERASE ALL` by accident, NOT a security boundary — AutoCAD
    accepts hundreds of commands and any loaded ARX/LISP adds more. Prefer the
    typed tools (entity_delete, drawing_save_as, block_insert, drawing_purge):
    they validate their arguments, which a free-text command string cannot.
    """
    sanitize_command(command)
    await ctx.warning(f"Running command: {command}")
    return await _backend(ctx).system_run_command(command)


@cad_tool(summary="Evaluate an AutoLISP expression inside AutoCAD (COM only).", cost="escape")
@mcp.tool(
    annotations={"title": "Execute AutoLISP", "readOnlyHint": False},
    tags={"system"},
)
async def system_run_lisp(
    expression: Annotated[str, 'AutoLISP expression to evaluate (e.g. \'(command "ZOOM" "E")\')'],
    ctx: Context = None,
) -> dict:
    """Execute an AutoLISP expression (COM backend only).

    Example: '(setvar \"DIMSCALE\" 1.0)'

    A symbol denylist refuses the known code-execution and file-I/O channels;
    text inside double quotes is treated as data, so drawing notes are not
    mistaken for code. It is a guardrail, NOT a security boundary — AutoLISP has
    more write channels than any denylist enumerates. Prefer the typed tools.
    """
    sanitize_lisp(expression)
    await ctx.warning(f"Running LISP: {expression[:80]}")
    return await _backend(ctx).system_run_lisp(expression)


@cad_tool(
    summary="See what this server can do: version, tool groups and active profile.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Backend Info", "readOnlyHint": True},
    tags={"system"},
)
async def system_about(ctx: Context = None) -> dict:
    """Get detailed information about AutoCAD MCP Pro capabilities and available tools."""
    b = ctx.lifespan_context.get("backend")
    backend_name = b.name if b else "none"
    # R15: derive the per-group breakdown dynamically from each tool's tags so
    # it can never drift from the registered surface (the old hand-maintained
    # dict omitted all engineering/premium/corner-ops tools + drawing_close and
    # misfiled entity_delete_many under entity_creation).
    tool_count = await _registered_tool_count()
    tool_packs = (_active_tool_profile or {}).get("tool_packs")
    if tool_packs is None:
        # Not applied yet (system_about called outside the lifespan): resolve
        # once rather than twice so an unknown pack warns once.
        packs, ignored = _enabled_packs()
        tool_packs = {
            "enabled": sorted(packs),
            "available": list(TOOL_PACK_NAMES),
            "ignored": ignored,
        }
    out = {
        "name": "AutoCAD MCP Pro",
        "version": __version__,
        "description": "Production-grade AutoCAD MCP server with dual COM+ezdxf engine",
        "active_backend": backend_name,
        "tool_groups": await _tool_groups(),
        "unsafe_mode": config.settings.dangerous_commands_enabled,
        "capabilities": b.capabilities().to_dict()["features"] if b else {},
        "tool_profile": _active_tool_profile
        or {"profile": config.settings.tool_profile, "applied": False},
        "tool_packs": tool_packs,
    }
    # R20: omit total_tools when unknown rather than reporting a fake -1.
    if tool_count is not None:
        out["total_tools"] = tool_count
    return out


# ---------------------------------------------------------------------------
# ── SECTION 12: Engineering Tools (deterministic CAD generators) ────────────
# ---------------------------------------------------------------------------


@cad_tool(
    summary="Draw a helical gear front view: true involute teeth, circles, bore and keyway.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Gear: Helical Front View", "destructiveHint": False},
    tags={"engineering", "gear"},
)
async def gear_draw_helical_front_view(
    module: Annotated[
        float, Field(gt=0, description="Module (mm). Pitch radius = module*teeth/2.")
    ],
    teeth: Annotated[int, Field(ge=6, description="Number of teeth.")],
    helix_angle: Annotated[float, Field(ge=0, lt=45, description="Helix angle in degrees.")],
    pressure_angle: Annotated[
        float, Field(default=20.0, gt=0, lt=45, description="Pressure angle (deg). Standard: 20.")
    ] = 20.0,
    hand: Annotated[
        str, Field(default="RH", description="Helix hand: 'RH' (right) or 'LH' (left).")
    ] = "RH",
    center_x: Annotated[float, Field(default=0.0)] = 0.0,
    center_y: Annotated[float, Field(default=0.0)] = 0.0,
    bore_diameter: Annotated[
        float | None,
        Field(default=None, gt=0, description="Optional bore diameter (mm). Adds a centered hole."),
    ] = None,
    keyway_width: Annotated[
        float | None,
        Field(
            default=None,
            gt=0,
            description="Keyway width (b). Auto from DIN 6885 if bore set and this is None.",
        ),
    ] = None,
    keyway_depth: Annotated[
        float | None, Field(default=None, gt=0, description="Keyway depth into hub (t2).")
    ] = None,
    ctx: Context = None,
) -> dict:
    """Deterministic helical gear front view: full involute outline (40 pts/flank),
    pitch/base/outer/root circles, helix symbol, optional bore + keyway.

    Returns a handle bundle plus 'metadata' for downstream gear_draw_section_aa.
    """
    from engineering import draw_helical_gear_front_view

    backend = _backend(ctx)
    await ctx.info(
        f"Drawing helical gear: m={module}, z={teeth}, beta={helix_angle} deg, hand={hand}"
    )
    return await draw_helical_gear_front_view(
        backend,
        module=module,
        teeth=teeth,
        helix_angle=helix_angle,
        pressure_angle=pressure_angle,
        hand=hand,
        center=(center_x, center_y),
        bore_diameter=bore_diameter,
        keyway_width=keyway_width,
        keyway_depth=keyway_depth,
    )


@cad_tool(
    summary="Draw a straight-tooth spur gear front view with true involute teeth.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Gear: Spur Front View", "destructiveHint": False},
    tags={"engineering", "gear"},
)
async def gear_draw_spur_front_view(
    module: Annotated[float, Field(gt=0)],
    teeth: Annotated[int, Field(ge=6)],
    pressure_angle: Annotated[float, Field(default=20.0, gt=0, lt=45)] = 20.0,
    center_x: Annotated[float, Field(default=0.0)] = 0.0,
    center_y: Annotated[float, Field(default=0.0)] = 0.0,
    bore_diameter: Annotated[float | None, Field(default=None, gt=0)] = None,
    keyway_width: Annotated[float | None, Field(default=None, gt=0)] = None,
    keyway_depth: Annotated[float | None, Field(default=None, gt=0)] = None,
    ctx: Context = None,
) -> dict:
    """Deterministic spur gear front view (no helix symbol)."""
    from engineering import draw_spur_gear_front_view

    backend = _backend(ctx)
    await ctx.info(f"Drawing spur gear: m={module}, z={teeth}")
    return await draw_spur_gear_front_view(
        backend,
        module=module,
        teeth=teeth,
        pressure_angle=pressure_angle,
        center=(center_x, center_y),
        bore_diameter=bore_diameter,
        keyway_width=keyway_width,
        keyway_depth=keyway_depth,
    )


@cad_tool(
    summary="Draw the hatched side cross-section A-A of a gear you already drew.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Gear: Section A-A View", "destructiveHint": False},
    tags={"engineering", "gear"},
)
async def gear_draw_section_aa(
    gear_metadata: Annotated[
        dict,
        Field(
            description="The 'metadata' dict returned by gear_draw_helical_front_view or gear_draw_spur_front_view."
        ),
    ],
    x_offset: Annotated[float, Field(description="X position to place the section view.")],
    face_width: Annotated[float, Field(gt=0, description="Gear face width (mm).")],
    ctx: Context = None,
) -> dict:
    """Deterministic side cross-section of a gear created by gear_draw_*_front_view.
    Includes top/bottom/left/right boundaries, bore lines, keyway notch, ANSI31 hatch.
    """
    from engineering import draw_gear_section_aa

    backend = _backend(ctx)
    await ctx.info(f"Drawing section A-A at x={x_offset}, face_width={face_width}")
    return await draw_gear_section_aa(
        backend,
        gear_metadata=gear_metadata,
        x_offset=x_offset,
        face_width=face_width,
    )


@cad_tool(
    summary="Draw a bore with a DIN 6885 keyway, auto-sized from the bore diameter.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Keyway: Keyed Bore (front view)", "destructiveHint": False},
    tags={"engineering", "keyway"},
)
async def keyway_draw_keyed_bore(
    center_x: Annotated[float, Field()],
    center_y: Annotated[float, Field()],
    bore_diameter: Annotated[float, Field(gt=0)],
    keyway_width: Annotated[float | None, Field(default=None, gt=0)] = None,
    keyway_depth: Annotated[float | None, Field(default=None, gt=0)] = None,
    layer: Annotated[str, Field(default="GEOMETRY")] = "GEOMETRY",
    ctx: Context = None,
) -> dict:
    """Bore + DIN 6885 keyway in front view. Auto-sizes keyway from bore if width/depth omitted."""
    from engineering import draw_keyed_bore

    backend = _backend(ctx)
    return await draw_keyed_bore(
        backend,
        center=(center_x, center_y),
        bore_diameter=bore_diameter,
        keyway_width=keyway_width,
        keyway_depth=keyway_depth,
        layer=layer,
    )


@cad_tool(summary="Draw the side cross-section of a keyed bore.", cost="mutate")
@mcp.tool(
    annotations={"title": "Keyway: Side Section", "destructiveHint": False},
    tags={"engineering", "keyway"},
)
async def keyway_draw_section(
    center_x: Annotated[float, Field()],
    center_y: Annotated[float, Field()],
    bore_diameter: Annotated[float, Field(gt=0)],
    face_width: Annotated[float, Field(gt=0)],
    keyway_width: Annotated[float | None, Field(default=None, gt=0)] = None,
    keyway_depth: Annotated[float | None, Field(default=None, gt=0)] = None,
    ctx: Context = None,
) -> dict:
    """Side cross-section view of a keyed bore."""
    from engineering import draw_keyway_section

    backend = _backend(ctx)
    return await draw_keyway_section(
        backend,
        center=(center_x, center_y),
        bore_diameter=bore_diameter,
        face_width=face_width,
        keyway_width=keyway_width,
        keyway_depth=keyway_depth,
    )


@cad_tool(
    summary="Stamp an ISO 7200 title block and sheet frame onto an A3 drawing.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "TitleBlock: ISO A3", "destructiveHint": False},
    tags={"engineering", "titleblock"},
)
async def titleblock_apply_iso_a3(
    title: Annotated[str, Field(description="Drawing title (verbatim, no LLM transformation).")],
    drawing_no: Annotated[str, Field(description="Drawing number (e.g. 'AM-2026-001').")],
    part_no: Annotated[str, Field(default="")] = "",
    material: Annotated[str, Field(default="")] = "",
    scale: Annotated[str, Field(default="1:1")] = "1:1",
    units: Annotated[str, Field(default="mm")] = "mm",
    drawn_by: Annotated[str, Field(default="")] = "",
    checked_by: Annotated[str, Field(default="")] = "",
    date: Annotated[str, Field(default="")] = "",
    sheet: Annotated[str, Field(default="1/1")] = "1/1",
    revision: Annotated[str, Field(default="A")] = "A",
    company: Annotated[str, Field(default="Anka-Makine")] = "Anka-Makine",
    origin_x: Annotated[float, Field(default=0.0)] = 0.0,
    origin_y: Annotated[float, Field(default=0.0)] = 0.0,
    layout: Annotated[
        str,
        "Paper-space layout to draw the sheet on (create it with layout_create). "
        "Empty draws in the current space, as before.",
    ] = "",
    ctx: Context = None,
) -> dict:
    """ISO 7200 / A3 (420x297 mm) title block. Title text is used verbatim.

    Pass `layout` to put the sheet on a paper-space layout, which is where a
    title block belongs — the border frames the printed sheet, not the model.
    Your current space is restored afterwards, so asking for a border does not
    move you onto the sheet.
    """
    from engineering import TitleBlockMetadata, apply_iso_a3_titleblock

    backend = _backend(ctx)
    metadata = TitleBlockMetadata(
        title=title,
        drawing_no=drawing_no,
        part_no=part_no,
        material=material,
        scale=scale,
        units=units,
        drawn_by=drawn_by,
        checked_by=checked_by,
        date=date,
        sheet=sheet,
        revision=revision,
        company=company,
    )
    return await apply_iso_a3_titleblock(
        backend,
        metadata=metadata,
        origin=(origin_x, origin_y),
        layout=layout or None,
    )


@cad_tool(
    summary="Finish the drawing: validate, critique, save, screenshot and score it.",
    cost="destructive",
)
@mcp.tool(
    annotations={
        "title": "Drawing: Finalize (validate + save + screenshot)",
        "destructiveHint": True,
    },
    tags={"engineering", "drawing", "validation"},
)
async def drawing_finalize(
    save_path: Annotated[
        str | None,
        Field(
            default=None,
            description="If given, save drawing here before validation. Pass full path including extension.",
        ),
    ] = None,
    screenshot_path: Annotated[
        str | None, Field(default=None, description="If given, write PNG screenshot to this path.")
    ] = None,
    expected: Annotated[
        dict | None,
        Field(
            default=None,
            description="Optional contract: {'part_type', 'helix_angle', 'must_have_bore', 'must_have_keyway'}",
        ),
    ] = None,
    strict_critique: Annotated[
        bool,
        Field(
            default=False,
            description="If true, ANY critique issue (including warnings) fails the gate — the full "
            "premium discipline. Default false: only critique 'error' issues (e.g. leftover "
            "construction geometry) fail; warnings are surfaced in the payload.",
        ),
    ] = False,
    ctx: Context = None,
) -> dict:
    """Premium completion gate: runs BOTH the 8-step validator AND the premium critique focuses
    (iso128, layer_color, dim_overlap, untrimmed_corner, duplicate_entities, construction_left,
    gdt, plus the P&ID focuses pid_dangling_line, pid_duplicate_tag,
    pid_incompatible_connection, pid_untagged_instrument, pid_illegal_tag,
    pid_unconnected_equipment — silent on a sheet with no P&ID symbols, plus the
    mechanical focuses mech_missing_centreline, mech_unhatched_section,
    mech_view_misaligned, mech_duplicate_dimension, mech_thread_unrepresented,
    mech_bom_balloon_mismatch — silent on a sheet with no mechanical part),
    then saves to disk, exports a screenshot, and returns the DWG path.

    Raises ToolError if any validator 'error' finding is present, or if critique reports an
    'error' (or, with strict_critique=True, any critique issue). Critique warnings are surfaced
    under payload['critique'] without failing the gate by default.
    """
    from engineering import DrawingValidator

    backend = _backend(ctx)

    if save_path:
        validated = validate_path(save_path, allow_write=True)
        from pathlib import Path as _P

        _fmt = _P(str(validated)).suffix.lstrip(".").lower() or "dxf"
        await backend.drawing_save_as(str(validated), fmt=_fmt)

    if screenshot_path:
        validated_shot = validate_path(screenshot_path, allow_write=True)
        try:
            shot = await backend.view_screenshot()
            if shot:
                from pathlib import Path as _P

                _P(str(validated_shot)).write_bytes(shot)
        except Exception as exc:
            if ctx is not None:
                await ctx.warning(f"Screenshot export failed: {exc}")

    result = await DrawingValidator().run(backend, expected=expected or {})
    payload = result.to_dict()

    # I15 — the premium critique focuses are part of the finalize gate, not merely advisory.
    critique_issues = await backend.drawing_critique(focus=None)
    crit_summary = {"error": 0, "warning": 0, "info": 0}
    for issue in critique_issues:
        crit_summary[issue.severity] = crit_summary.get(issue.severity, 0) + 1
    payload["critique"] = [issue.to_dict() for issue in critique_issues]
    payload["critique_summary"] = crit_summary

    # I4 — a single regression-trackable scalar over the union of the structural
    # validator and the premium critique (MUSE/CadBench grade an Invalidity Ratio,
    # not shape). 100 = clean; errors dominate the penalty.
    from engineering.scoring import combine

    payload["score"] = combine(result.summary, crit_summary)

    info = await backend.drawing_info()
    payload["dwg_path"] = getattr(info, "full_path", "")

    if ctx is not None:
        for issue in critique_issues:
            if issue.severity == "warning":
                await ctx.warning(f"critique[{issue.focus}]: {issue.message}")

    if not result.ok:
        raise ToolError(
            f"drawing_finalize: validation failed with {result.summary['error']} error(s). "
            f"First: {result.findings[0].code}: {result.findings[0].message}"
        )

    gate_failures = [
        i
        for i in critique_issues
        if i.severity == "error" or (strict_critique and i.severity != "info")
    ]
    if gate_failures:
        first = gate_failures[0]
        raise ToolError(
            f"drawing_finalize: critique gate failed with {len(gate_failures)} blocking issue(s). "
            f"First: {first.focus}: {first.message}"
        )
    return payload


@cad_tool(
    summary="Hand the drawing off: a hashed, validated bundle of files plus a manifest.",
    cost="destructive",
)
@mcp.tool(
    annotations={
        "title": "Drawing: Deliver Auditable Bundle",
        "destructiveHint": True,
    },
    tags={"engineering", "drawing", "validation", "delivery"},
)
async def drawing_deliver(
    output_dir: Annotated[
        str,
        Field(description="Output directory for drawing artifacts and manifest.json."),
    ],
    formats: Annotated[
        list[str] | None,
        Field(default=None, description="Requested formats: dxf, pdf, png."),
    ] = None,
    min_score: Annotated[
        float,
        Field(default=95.0, ge=0.0, le=100.0, description="Minimum accepted quality score."),
    ] = 95.0,
    strict_critique: Annotated[
        bool,
        Field(default=True, description="Block delivery on every non-info critique issue."),
    ] = True,
    expected: Annotated[
        dict | None,
        Field(default=None, description="Optional validator expectations for the drawing."),
    ] = None,
    ctx: Context = None,
) -> dict:
    """Create a hashed, validated delivery bundle and verify DXF save/reopen parity.

    The result status is ``success``, ``failed_validation`` or ``failed_export``.
    Failure intentionally keeps all generated artifacts for diagnosis.
    """
    from engineering import deliver_drawing

    destination = validate_path(output_dir, allow_write=True)
    result = await deliver_drawing(
        _backend(ctx),
        destination,
        formats=formats,
        min_score=min_score,
        strict_critique=strict_critique,
        expected=expected,
    )
    return result.to_dict()


# ---------------------------------------------------------------------------
# ── SECTION 13: Premium Drafting Meta-Tools (5x quality multiplier) ─────────
# ---------------------------------------------------------------------------
# These tools wrap the entity primitives in a quality-first workflow:
#   plan → snap-aware geometry → critique → finalize.
# See `.claude/skills/autocad-mcp-premium/` for the full discipline.


@cad_tool(
    summary="Check the brief before you draw: units, part type, tolerances, missing inputs.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Drawing: Preflight", "readOnlyHint": True},
    tags={"premium", "planning", "validation"},
)
async def drawing_preflight(
    intent: Annotated[str, "One-line description of what this drawing represents."],
    requirements: Annotated[
        dict | None,
        "Units, part_type, dimensions, tolerance_policy and optional constraints.",
    ] = None,
    sheet_size: Annotated[str, Field(default="A3", description="A4 / A3 / A2 / A1 / A0.")] = "A3",
    scale: Annotated[float, Field(default=1.0, gt=0)] = 1.0,
    layer_set_id: Annotated[
        str, Field(default="mech", description="mech / pid / iso13567.")
    ] = "mech",
    view_count: Annotated[int, Field(default=1, ge=1)] = 1,
    dim_style: Annotated[
        str, Field(default="chain", description="chain / baseline / ordinate / mixed.")
    ] = "chain",
    allow_assumptions: Annotated[
        bool, "Allow documented defaults for units and tolerance policy."
    ] = False,
    ctx: Context = None,
) -> dict:
    """Validate and normalize requirements before committing a drawing plan."""
    result = await _backend(ctx).drawing_preflight(
        intent,
        requirements,
        sheet_size,
        scale,
        layer_set_id,
        view_count,
        dim_style,
        allow_assumptions,
    )
    return result.to_dict()


@cad_tool(
    summary="Commit sheet size, scale, layer set and dimension style before any geometry.",
    cost="safe",
)
@mcp.tool(
    annotations={"title": "Drawing: Plan (commit intent before drawing)", "destructiveHint": False},
    tags={"premium", "planning"},
)
async def drawing_plan(
    intent: Annotated[str, "One-line description of what this drawing represents."],
    sheet_size: Annotated[
        str, Field(default="A3", description="Paper size: A4 / A3 / A2 / A1 / A0.")
    ] = "A3",
    scale: Annotated[
        float, Field(default=1.0, gt=0, description="Drawing scale (1.0 = 1:1, 0.1 = 1:10, etc).")
    ] = 1.0,
    layer_set_id: Annotated[
        str, Field(default="mech", description="Layer set to bootstrap: mech / pid / iso13567.")
    ] = "mech",
    view_count: Annotated[int, Field(default=1, ge=1)] = 1,
    dim_style: Annotated[
        str,
        Field(
            default="chain",
            description="Default dimensioning style: chain / baseline / ordinate / mixed.",
        ),
    ] = "chain",
    notes: Annotated[list[str] | None, "Free-form constraint notes."] = None,
    requirements: Annotated[dict | None, "Normalized preflight requirements."] = None,
    spec_hash: Annotated[str | None, "Hash returned by the latest ready drawing_preflight."] = None,
    ctx: Context = None,
) -> dict:
    """Commit a PlanSpec before any geometry is created.

    The PlanSpec is stored on the backend and surfaced for reference during
    the workflow (it is not replayed as a critique). Always call this FIRST
    in a premium workflow.
    """
    plan = await _backend(ctx).drawing_plan(
        intent,
        sheet_size,
        scale,
        layer_set_id,
        view_count,
        dim_style,
        notes,
        requirements,
        spec_hash,
    )
    from engineering.preflight import pid_plan_warnings

    out = _dc(plan)
    warnings = pid_plan_warnings(intent, layer_set_id)
    if warnings:
        out["warnings"] = warnings
    return out


@cad_tool(
    summary="Review the drawing for drafting mistakes: must come back empty before finalize.",
    cost="read",
)
@mcp.tool(
    annotations={
        "title": "Drawing: Critique (premium quality checks)",
        "destructiveHint": False,
        "readOnlyHint": True,
    },
    tags={"premium", "validation"},
)
async def drawing_critique(
    focus: Annotated[
        list[str] | None,
        Field(
            default=None,
            description="Subset of: iso128, layer_color, dim_overlap, untrimmed_corner, "
            "duplicate_entities, construction_left, gdt, the P&ID focuses "
            "pid_dangling_line, pid_duplicate_tag, pid_incompatible_connection, "
            "pid_untagged_instrument, pid_illegal_tag, pid_unconnected_equipment "
            "(silent on a drawing with no P&ID symbols), and the mechanical focuses "
            "mech_missing_centreline, mech_unhatched_section, mech_view_misaligned, "
            "mech_duplicate_dimension, mech_thread_unrepresented, "
            "mech_bom_balloon_mismatch (silent on a drawing with no mechanical part). "
            "None = run all.",
        ),
    ] = None,
    ctx: Context = None,
) -> list[dict]:
    """Run premium-quality checks. Returns zero issues for a clean drawing.

    Standard production gate: must return [] before `drawing_finalize`.
    """
    issues = await _backend(ctx).drawing_critique(focus)
    return [_dc(i) for i in issues]


@cad_tool(
    summary="Auto-repair what the critique found, then re-check, up to three rounds.",
    cost="destructive",
)
@mcp.tool(
    annotations={"title": "Drawing: Refine", "destructiveHint": True},
    tags={"premium", "validation", "modify"},
)
async def drawing_refine(
    max_rounds: Annotated[int, Field(default=3, ge=1, le=3)] = 3,
    min_score: Annotated[float, Field(default=95.0, ge=0, le=100)] = 95.0,
    focus: Annotated[list[str] | None, "Critique focuses to repair; None runs all."] = None,
    allowed_repairs: Annotated[
        list[str] | None, "Optional allowlist of repair focus names."
    ] = None,
    dry_run: Annotated[bool, "Return the repair plan without modifying the drawing."] = False,
    ctx: Context = None,
) -> dict:
    """Run a bounded, transaction-safe critique/repair/re-critique loop."""
    from engineering.refiner import refine_drawing

    result = await refine_drawing(
        _backend(ctx),
        max_rounds=max_rounds,
        min_score=min_score,
        focus=focus,
        allowed_repairs=allowed_repairs,
        dry_run=dry_run,
    )
    return result.to_dict()


@cad_tool(
    summary="Get an exact endpoint, midpoint, centre, quadrant or perpendicular foot.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Point: Snap (deterministic OSNAP)", "readOnlyHint": True},
    tags={"premium", "snap"},
)
async def point_from_snap(
    handle: Annotated[str, "Entity handle to snap onto"],
    snap: Annotated[str, Field(description="Snap type: end | mid | center | quad | perp | near")],
    ref_x: Annotated[
        float | None, "Reference X (required for perp/near; disambiguates end/quad)"
    ] = None,
    ref_y: Annotated[float | None, "Reference Y"] = None,
    ctx: Context = None,
) -> dict:
    """Compute a deterministic snap point on an entity. Use this INSTEAD OF
    guessing coordinates — eliminates the most common LLM drawing error.
    """
    pt = await _backend(ctx).point_from_snap(handle, snap, ref_x, ref_y)
    return {"x": float(pt[0]), "y": float(pt[1])}


@cad_tool(summary="Find where two lines or circles cross, exactly.", cost="read")
@mcp.tool(
    annotations={"title": "Point: Intersection (deterministic)", "readOnlyHint": True},
    tags={"premium", "snap"},
)
async def point_intersection(
    handle1: Annotated[str, "First entity handle (LINE or CIRCLE)"],
    handle2: Annotated[str, "Second entity handle (LINE or CIRCLE)"],
    ref_x: Annotated[
        float | None, "Reference X to pick nearest candidate when multiple exist"
    ] = None,
    ref_y: Annotated[float | None, "Reference Y"] = None,
    ctx: Context = None,
) -> dict:
    """Compute the intersection of two geometry entities (LINE-LINE, LINE-CIRCLE,
    CIRCLE-CIRCLE). When two candidates exist, ref_x/ref_y selects the nearest.
    Returns {x, y}.
    """
    pt = await _backend(ctx).point_intersection(handle1, handle2, ref_x, ref_y)
    return {"x": float(pt[0]), "y": float(pt[1])}


@cad_tool(summary="Find where a line from an outside point touches a circle.", cost="read")
@mcp.tool(
    annotations={"title": "Point: Tangent from external point", "readOnlyHint": True},
    tags={"premium", "snap"},
)
async def point_tangent(
    circle_handle: Annotated[str, "Handle of the CIRCLE entity"],
    from_x: Annotated[float, "X of the external point"],
    from_y: Annotated[float, "Y of the external point"],
    ref_x: Annotated[
        float | None, "Reference X to pick nearest tangent point when two exist"
    ] = None,
    ref_y: Annotated[float | None, "Reference Y"] = None,
    ctx: Context = None,
) -> dict:
    """Compute the tangent point on a circle from an external point.
    Returns {x, y}. Raises if the from-point is inside the circle.
    """
    pt = await _backend(ctx).point_tangent(circle_handle, from_x, from_y, ref_x, ref_y)
    return {"x": float(pt[0]), "y": float(pt[1])}


@cad_tool(summary="Lay down an infinite guide line to build geometry against.", cost="mutate")
@mcp.tool(
    annotations={"title": "Construction: XLine (infinite reference)", "destructiveHint": False},
    tags={"premium", "construction"},
)
async def construction_xline(
    x: Annotated[float, "Base point X"],
    y: Annotated[float, "Base point Y"],
    angle_deg: Annotated[float, "Angle in degrees (0=horizontal, 90=vertical)"],
    layer: Annotated[str, "Layer for the construction line"] = "CONSTRUCTION",
    ctx: Context = None,
) -> dict:
    """Create an infinite construction line on the CONSTRUCTION layer.
    Use as scaffolding; call `construction_clear()` before finalize.
    """
    result = await _backend(ctx).construction_xline(x, y, angle_deg, layer)
    return _dc(result)


@cad_tool(
    summary="Wipe the construction scaffolding off the drawing before finalize.",
    cost="destructive",
)
@mcp.tool(
    annotations={"title": "Construction: Clear (delete scaffold)", "destructiveHint": True},
    tags={"premium", "construction"},
)
async def construction_clear(
    layer: Annotated[str, "Layer to clear"] = "CONSTRUCTION",
    ctx: Context = None,
) -> dict:
    """Delete every entity on the CONSTRUCTION layer. Idempotent.
    Must be called before `drawing_finalize` to satisfy `construction_left` critique.
    """
    return await _backend(ctx).construction_clear(layer)


@cad_tool(
    summary="Set up a standard ISO layer set with the right colours and lineweights.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Drawing: Apply ISO Layer Set (bootstrap)", "destructiveHint": False},
    tags={"premium", "layers"},
)
async def drawing_apply_iso_layers(
    standard: Annotated[
        str,
        Field(
            default="mech",
            description=(
                "Layer set: mech (DIN/ISO mechanical), pid (P&ID), iso13567 (CAD layer naming), "
                "arch (ISO 13567-style architectural: walls, poche, doors, windows, stairs, "
                "furniture, sanitary, grid, rooms, dimensions, symbols, overhead)."
            ),
        ),
    ] = "mech",
    ctx: Context = None,
) -> dict:
    """Bootstrap a full ISO-conformant layer set with correct colors and lineweights.
    Idempotent — existing layers are not modified.

    Refused by name: a standard that is not one of mech, pid, iso13567, arch.
    """
    return await _backend(ctx).drawing_apply_iso_layers(standard)


@cad_tool(
    summary="Dimension a set of entities at once, as a chain, baseline or ordinate run.",
    cost="mutate",
)
@mcp.tool(
    annotations={
        "title": "Dimension: Auto (chain / baseline / ordinate)",
        "destructiveHint": False,
    },
    tags={"premium", "dimension"},
)
async def dimension_auto(
    handles: Annotated[list[str], "List of entity handles to dimension"],
    style: Annotated[
        str, Field(default="chain", description="chain | baseline | ordinate")
    ] = "chain",
    offset: Annotated[
        float, Field(default=10.0, gt=0, description="Dimension-line offset from the geometry (mm)")
    ] = 10.0,
    ctx: Context = None,
) -> list[dict]:
    """Generate ISO 129 dimensions across the listed entities in the chosen style.
    V1 supports LINE entities only.
    """
    result = await _backend(ctx).dimension_auto(handles, style, offset)
    return [_dc(e) for e in result]


@cad_tool(
    summary="Pick every entity matching a description: type, layer, colour, length, location.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Entity: Smart Select (semantic predicate)", "readOnlyHint": True},
    tags={"premium", "select"},
)
async def entity_select_smart(
    predicate: Annotated[
        dict,
        Field(
            description=(
                "Predicate dict (all keys optional, AND-ed): "
                "type (e.g. 'LINE'), layer (name), near ([x,y,radius]), "
                "length_range ([min,max], LINE/ARC only), color (ACI int)."
            )
        ),
    ],
    fields: ResultFields = None,
    compact: ResultCompact = False,
    ctx: Context = None,
) -> list[dict] | dict:
    """Select entities by semantic predicate instead of memorising handles.

    Uncapped. The usual next step is dimension_auto(handles), so
    fields=["handle"] is normally all this needs to return.
    """
    result = await _backend(ctx).entity_select_smart(predicate)
    return _shape_rows(
        result,
        spec=EntityInfo,
        fields=fields,
        compact=compact,
        tool="entity_select_smart",
        total=len(result),
    )


# ---------------------------------------------------------------------------
# ── SECTION 14: GD&T (ISO 1101 / ASME Y14.5) ────────────────────────────────
# ---------------------------------------------------------------------------
# 2D geometric tolerancing — feature control frames + datum features — composed
# from LINE + TEXT so the same frame renders on COM and ezdxf. The datum-
# consistency rule is enforced by the `gdt` critique focus at finalize time.


@cad_tool(
    summary="Draw an ISO 1101 feature control frame: symbol, tolerance zone and datums.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "GD&T: Feature Control Frame (ISO 1101)", "destructiveHint": False},
    tags={"engineering", "gdt"},
)
async def gd_frame(
    symbol: Annotated[
        str,
        Field(
            description=(
                "Geometric characteristic: straightness, flatness, circularity, "
                "cylindricity, profile_line, profile_surface, angularity, "
                "perpendicularity, parallelism, position, concentricity, symmetry, "
                "circular_runout, total_runout."
            )
        ),
    ],
    tolerance: Annotated[float, "Tolerance zone value (mm)."],
    x: Annotated[float, "Frame bottom-left corner X."],
    y: Annotated[float, "Frame bottom-left corner Y."],
    datums: Annotated[
        list[str] | None,
        Field(
            default=None,
            description="Ordered datum references, e.g. ['A','B']. Required for "
            "orientation/location/runout characteristics.",
        ),
    ] = None,
    height: Annotated[
        float, Field(default=5.0, gt=0, description="Frame height (mm); text scales with it.")
    ] = 5.0,
    diameter: Annotated[
        bool,
        Field(default=False, description="Prefix ⌀ for a cylindrical (diametral) tolerance zone."),
    ] = False,
    modifier: Annotated[
        str | None,
        Field(default=None, description="Material-condition modifier: M (MMC), L (LMC), S (RFS)."),
    ] = None,
    layer: Annotated[str | None, "Layer (defaults to the active DIM layer)."] = None,
    ctx: Context = None,
) -> dict:
    """Draw an ISO 1101 feature control frame from LINE + TEXT primitives.

    Renders identically on COM and ezdxf. Referenced datums are recorded so the
    `gdt` critique focus flags any datum with no matching datum feature.
    """
    return await _backend(ctx).draw_feature_control_frame(
        symbol,
        tolerance,
        x,
        y,
        datums,
        height,
        diameter,
        modifier,
        layer,
    )


@cad_tool(
    summary="Mark a datum on a feature: filled triangle plus the boxed datum letter.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "GD&T: Datum Feature (ISO 1101)", "destructiveHint": False},
    tags={"engineering", "gdt"},
)
async def datum_feature(
    letter: Annotated[str, "Datum letter, e.g. 'A' (avoid I, O, Q per ISO 1101)."],
    x: Annotated[float, "Datum triangle apex X (on the referenced feature)."],
    y: Annotated[float, "Datum triangle apex Y."],
    size: Annotated[float, Field(default=5.0, gt=0, description="Triangle/label size (mm).")] = 5.0,
    layer: Annotated[str | None, "Layer (defaults to the active DIM layer)."] = None,
    ctx: Context = None,
) -> dict:
    """Place a datum feature symbol (filled triangle + boxed letter).

    Establishes the datum so a feature control frame referencing this letter
    passes the `gdt` critique focus.
    """
    return await _backend(ctx).draw_datum_feature(letter, x, y, size, layer)


# ---------------------------------------------------------------------------
# ── SECTION 15: Layouts & Paper Space (12 tools) ────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(summary="List the sheet tabs: Model plus every paper-space layout.", cost="read")
@mcp.tool(
    annotations={"title": "List Layouts", "readOnlyHint": True},
    tags={"layout", "query"},
)
async def layout_list(ctx: Context = None) -> dict:
    """List all layout tabs (Model + paper-space layouts) and the current one."""
    return await _backend(ctx).layout_list()


@cad_tool(summary="Add a new paper-space sheet tab to the drawing.", cost="mutate")
@mcp.tool(
    annotations={"title": "Create Layout", "destructiveHint": False},
    tags={"layout"},
)
async def layout_create(
    name: Annotated[str, "New paper-space layout name (e.g. 'A3-Sheet')."],
    ctx: Context = None,
) -> dict:
    """Create a new paper-space layout tab."""
    await ctx.info(f"Creating layout {name}")
    return await _backend(ctx).layout_create(name)


@cad_tool(summary="Switch to a sheet tab, or back to model space.", cost="safe")
@mcp.tool(
    annotations={"title": "Set Current Layout"},
    tags={"layout"},
)
async def layout_set_current(
    name: Annotated[str, "Layout tab to activate ('Model' or a paper-space layout)."],
    ctx: Context = None,
) -> dict:
    """Activate a layout tab."""
    return await _backend(ctx).layout_set_current(name)


@cad_tool(
    summary="Put a scaled window onto model space on a sheet, at 1:1, 1:2 and so on.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Create Viewport", "destructiveHint": False},
    tags={"layout"},
)
async def viewport_create(
    layout: Annotated[str, "Paper-space layout that receives the viewport."],
    center_x: Annotated[float, "Viewport center X in paper units."],
    center_y: Annotated[float, "Viewport center Y in paper units."],
    width: Annotated[float, Field(gt=0, description="Viewport width in paper units.")],
    height: Annotated[float, Field(gt=0, description="Viewport height in paper units.")],
    view_center_x: Annotated[float, "Model-space X the viewport looks at."],
    view_center_y: Annotated[float, "Model-space Y the viewport looks at."],
    scale: Annotated[
        float,
        Field(gt=0, description="Paper:model scale (1.0 = 1:1, 0.5 = 1:2, 2.0 = 2:1)."),
    ] = 1.0,
    ctx: Context = None,
) -> dict:
    """Place a scaled model-space viewport on a paper-space layout.

    The viewport window shows the model region centered at
    (view_center_x, view_center_y); view height = height / scale.
    """
    await ctx.info(f"Creating viewport on {layout} at {scale}:1")
    return await _backend(ctx).viewport_create(
        layout, center_x, center_y, width, height, view_center_x, view_center_y, scale
    )


@cad_tool(summary="Delete a sheet tab and everything drawn on it.", cost="destructive")
@mcp.tool(
    annotations={"title": "Delete Layout", "destructiveHint": True},
    tags={"layout"},
)
async def layout_delete(
    name: Annotated[str, "Paper-space layout tab to delete (never 'Model')."],
    ctx: Context = None,
) -> dict:
    """Delete a paper-space layout and every entity on it.

    Refuses model space, a blank name, and the last remaining sheet. If the
    deleted tab was the current one, the returned `current` is where geometry
    goes next — and handles from the deleted sheet stop resolving.
    """
    await ctx.info(f"Deleting layout {name}")
    return await _backend(ctx).layout_delete(name)


@cad_tool(summary="Rename a sheet tab, keeping its geometry and handles.", cost="mutate")
@mcp.tool(
    annotations={"title": "Rename Layout", "destructiveHint": False},
    tags={"layout"},
)
async def layout_rename(
    old_name: Annotated[str, "Existing layout tab name."],
    new_name: Annotated[str, "New name; must not be blank or contain / \\ * ? : ; , = `"],
    ctx: Context = None,
) -> dict:
    """Rename a paper-space layout. Entity handles are unaffected."""
    return await _backend(ctx).layout_rename(old_name, new_name)


@cad_tool(summary="Duplicate a sheet with its page setup and geometry.", cost="mutate")
@mcp.tool(
    annotations={"title": "Copy Layout", "destructiveHint": False},
    tags={"layout"},
)
async def layout_copy(
    source: Annotated[str, "Layout tab to copy from (never 'Model')."],
    new_name: Annotated[str, "Name for the new layout tab."],
    ctx: Context = None,
) -> dict:
    """Copy a paper-space layout: page setup, plot settings and all geometry.

    `skipped` names any DXF types that could not be cloned — check it rather
    than trusting `ok` alone. Associative hatch boundaries are re-pointed at the
    cloned entities; `associativity_dropped` counts those that referenced
    something outside the source layout and had to be cleared.
    """
    await ctx.info(f"Copying layout {source} to {new_name}")
    return await _backend(ctx).layout_copy(source, new_name)


@cad_tool(summary="List the viewports on a sheet with their scales and locks.", cost="read")
@mcp.tool(
    annotations={"title": "List Viewports", "readOnlyHint": True},
    tags={"layout", "query"},
)
async def viewport_list(
    layout: Annotated[
        str,
        "Restrict to one paper-space layout. Empty covers every sheet.",
    ] = "",
    ctx: Context = None,
) -> dict:
    """List paper-space viewports: handle, geometry, scale and lock state.

    The layout's own main viewport is included with `is_main: true` — it is the
    tab's pan/zoom state rather than a drafting viewport, and it is what remains
    after every drafting viewport is deleted. `scale` and `locked` are null on
    documents that cannot store them (R12) rather than fabricated.
    """
    return await _backend(ctx).viewport_list(layout or None)


@cad_tool(summary="Set a viewport's scale, e.g. 1:2 or 1:50.", cost="mutate")
@mcp.tool(
    annotations={"title": "Set Viewport Scale", "destructiveHint": False},
    tags={"layout"},
)
async def viewport_set_scale(
    handle: Annotated[str, "Viewport entity handle (from viewport_list)."],
    scale: Annotated[
        float,
        Field(gt=0, description="Paper:model scale (1.0 = 1:1, 0.5 = 1:2, 0.02 = 1:50)."),
    ],
    ctx: Context = None,
) -> dict:
    """Rescale a viewport by adjusting its view height.

    Geometric scale only: annotative text and dimensions do not resize with it.
    Refuses the layout's main viewport, whose view height is the tab's own
    pan/zoom state rather than a drafting scale.
    """
    return await _backend(ctx).viewport_set_scale(handle, scale)


@cad_tool(summary="Lock a viewport so its scale cannot be zoomed away.", cost="safe")
@mcp.tool(
    annotations={"title": "Lock Viewport"},
    tags={"layout"},
)
async def viewport_lock(
    handle: Annotated[str, "Viewport entity handle (from viewport_list)."],
    locked: Annotated[bool, "True to lock the display scale, False to unlock."] = True,
    ctx: Context = None,
) -> dict:
    """Lock or unlock a viewport's display scale."""
    return await _backend(ctx).viewport_lock(handle, locked)


@cad_tool(summary="Remove a viewport from a sheet.", cost="destructive")
@mcp.tool(
    annotations={"title": "Delete Viewport", "destructiveHint": True},
    tags={"layout"},
)
async def viewport_delete(
    handle: Annotated[str, "Viewport entity handle (from viewport_list)."],
    force: Annotated[bool, "Allow deleting the layout's main viewport."] = False,
    ctx: Context = None,
) -> dict:
    """Delete a viewport.

    The layout's main viewport needs `force=true`; deleting it removes the tab's
    own view state, and the layout's current-viewport pointer is repaired so the
    file does not carry a dangling reference that only CAD would notice.
    """
    return await _backend(ctx).viewport_delete(handle, force)


@cad_tool(
    summary="Move entities between model space and a sheet through a viewport.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Change Space", "destructiveHint": False},
    tags={"layout", "modify"},
)
async def entity_change_space(
    handles: Annotated[list[str], "Entity handles to move."],
    viewport_handle: Annotated[str, "Viewport that defines the model-to-paper mapping."],
    direction: Annotated[
        str,
        Field(
            default="to_paper",
            description="to_paper (model -> sheet) or to_model (sheet -> model).",
        ),
    ] = "to_paper",
    freeze_dimensions: Annotated[
        bool,
        "Bake each dimension's current measurement into its text before scaling.",
    ] = False,
    ctx: Context = None,
) -> dict:
    """AutoCAD's CHSPACE: move entities across spaces, rescaled by the viewport.

    Geometry is transformed by the viewport's own matrix so it stays the same
    size on screen — a move without that transform would leave a 100 mm feature
    as 100 mm of paper inside a 1:2 viewport.

    Refused per entity for dimensions (unless `freeze_dimensions`), ACIS solids,
    tables and proxies, viewports, and entities already in the target space;
    refused outright for a twisted or non-plan viewport. Entities that end up
    outside the viewport or off the sheet are moved and flagged, not refused.
    """
    await ctx.info(f"Changing space for {len(handles)} entities ({direction})")
    return await _backend(ctx).entity_change_space(
        handles, viewport_handle, direction, freeze_dimensions
    )


# ---------------------------------------------------------------------------
# ── SECTION 16: 3D Solids (5 tools) — opt-in via ENABLE_3D ──────────────────
# ---------------------------------------------------------------------------


def _require_3d() -> None:
    """3D solids are opt-in: hidden from discovery and rejected when disabled."""
    if not config.settings.enable_3d:
        raise ToolError(
            "3D solids are disabled. Set ENABLE_3D=true (COM backend with live "
            "AutoCAD required; the headless backend cannot generate ACIS solids)."
        )


@cad_tool(summary="Create a 3D solid box (live AutoCAD, opt-in via ENABLE_3D).", cost="mutate")
@mcp.tool(
    annotations={"title": "Solid: Box", "destructiveHint": False},
    tags={"solid"},
)
async def solid_box(
    cx: Annotated[float, "Box center X."],
    cy: Annotated[float, "Box center Y."],
    cz: Annotated[float, "Box center Z."],
    length: Annotated[float, Field(gt=0, description="Length along X (mm).")],
    width: Annotated[float, Field(gt=0, description="Width along Y (mm).")],
    height: Annotated[float, Field(gt=0, description="Height along Z (mm).")],
    ctx: Context = None,
) -> dict:
    """Create a native 3D solid box (COM backend, opt-in)."""
    _require_3d()
    return await _backend(ctx).solid_box(cx, cy, cz, length, width, height)


@cad_tool(
    summary="Create a 3D solid cylinder or shaft (live AutoCAD, opt-in via ENABLE_3D).",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Solid: Cylinder", "destructiveHint": False},
    tags={"solid"},
)
async def solid_cylinder(
    cx: Annotated[float, "Base-center X."],
    cy: Annotated[float, "Base-center Y."],
    cz: Annotated[float, "Center Z (AutoCAD places the cylinder center here)."],
    radius: Annotated[float, Field(gt=0, description="Cylinder radius (mm).")],
    height: Annotated[float, Field(gt=0, description="Cylinder height (mm).")],
    ctx: Context = None,
) -> dict:
    """Create a native 3D solid cylinder (COM backend, opt-in)."""
    _require_3d()
    return await _backend(ctx).solid_cylinder(cx, cy, cz, radius, height)


@cad_tool(
    summary="Pull a closed profile up into a 3D solid (live AutoCAD, opt-in via ENABLE_3D).",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Solid: Extrude", "destructiveHint": False},
    tags={"solid"},
)
async def solid_extrude(
    profile_handle: Annotated[str, "Handle of a closed profile (circle / closed polyline)."],
    height: Annotated[float, "Extrusion height; negative extrudes downward."],
    taper_angle: Annotated[
        float, Field(default=0.0, ge=-45, le=45, description="Taper angle in degrees.")
    ] = 0.0,
    ctx: Context = None,
) -> dict:
    """Extrude a closed profile into a native 3D solid (COM backend, opt-in)."""
    _require_3d()
    return await _backend(ctx).solid_extrude(profile_handle, height, taper_angle)


@cad_tool(
    summary="Spin a closed profile around an axis into a 3D solid (opt-in via ENABLE_3D).",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Solid: Revolve", "destructiveHint": False},
    tags={"solid"},
)
async def solid_revolve(
    profile_handle: Annotated[str, "Handle of a closed profile (circle / closed polyline)."],
    axis_x1: Annotated[float, "Revolution axis start X."],
    axis_y1: Annotated[float, "Revolution axis start Y."],
    axis_x2: Annotated[float, "Revolution axis end X."],
    axis_y2: Annotated[float, "Revolution axis end Y."],
    angle: Annotated[
        float, Field(default=360.0, gt=0, le=360, description="Revolution angle in degrees.")
    ] = 360.0,
    ctx: Context = None,
) -> dict:
    """Revolve a closed profile around an axis into a native 3D solid (COM, opt-in)."""
    _require_3d()
    return await _backend(ctx).solid_revolve(
        profile_handle, axis_x1, axis_y1, axis_x2, axis_y2, angle
    )


@cad_tool(
    summary="Union, subtract or intersect two 3D solids; the tool solid is consumed.",
    cost="destructive",
)
@mcp.tool(
    annotations={"title": "Solid: Boolean", "destructiveHint": True},
    tags={"solid"},
)
async def solid_boolean(
    target_handle: Annotated[str, "Handle of the solid that receives the result."],
    tool_handle: Annotated[str, "Handle of the solid consumed by the operation."],
    operation: Annotated[str, "union | subtract | intersect"],
    ctx: Context = None,
) -> dict:
    """Boolean-combine two native 3D solids (COM backend, opt-in).

    The tool solid is consumed; the target holds the result.
    """
    _require_3d()
    return await _backend(ctx).solid_boolean(target_handle, tool_handle, operation)


# ---------------------------------------------------------------------------
# ── SECTION 17: P&ID (9 tools) ──────────────────────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(
    summary="List the P&ID symbol catalogue: families, variants, ports, parameters.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "P&ID: Symbol Catalogue", "readOnlyHint": True},
    tags={"pid", "query"},
)
async def pid_symbol_list(
    family: Annotated[
        str | None,
        "valve | rotating | vessel | heat | misc | instrument | connector | marker; omit for all",
    ] = None,
    ctx: Context = None,
) -> dict:
    """The symbols `pid_symbol_insert` can place, with their ports and variant options.

    Authored from ISO 10628-2 (equipment, valves) and ISA-5.1 (instrumentation);
    each row's `source` names the figure. Symbol-local sizes are millimetres at
    A3/A1 paper scale (bubble Ø10, valve 8×4); `scale` at insertion rescales.
    """
    from engineering.pid.symbols import CATALOG_VERSION, list_symbols

    return {"catalog_version": CATALOG_VERSION, "symbols": list_symbols(family)}


@cad_tool(
    summary=(
        "Place a P&ID symbol (valve, pump, vessel, instrument bubble, connector) "
        "as a tagged block with ports."
    ),
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "P&ID: Insert Symbol", "readOnlyHint": False},
    tags={"pid", "create"},
)
async def pid_symbol_insert(
    symbol: Annotated[
        str,
        "Catalogue symbol name, e.g. gate, centrifugal_pump, vertical_vessel, instrument, offpage",
    ],
    x: Annotated[float, "Insertion X (WCS)"],
    y: Annotated[float, "Insertion Y (WCS)"],
    rotation: Annotated[float, "Rotation in degrees; 0 = flow left to right"] = 0.0,
    scale: Annotated[float, Field(default=1.0, gt=0, description="Uniform scale")] = 1.0,
    tag: Annotated[
        str | None,
        "Tag: equipment P-101, valve HV-101, instrument FIC-101 (split into FUNC/LOOP)",
    ] = None,
    desc: Annotated[str | None, "Description attribute"] = None,
    actuator: Annotated[
        str | None, "Valves: none | diaphragm | piston | motor | solenoid | hand"
    ] = None,
    fail: Annotated[str | None, "Actuated valves: FO | FC | FL"] = None,
    type: Annotated[str | None, "Instruments: discrete | dcs | computer | plc"] = None,
    location: Annotated[
        str | None,
        "Instruments: field | primary | auxiliary | primary_rear | auxiliary_rear",
    ] = None,
    params: Annotated[
        dict | None,
        "Parametric vessels: {width, height, nozzles: [{name, side, fraction}], roof, trays, jacketed}",
    ] = None,
    shape: Annotated[str | None, "Reducer: concentric | eccentric"] = None,
    direction: Annotated[str | None, "Off-page connector: in | out"] = None,
    link: Annotated[str | None, "Off-page connector link id shared by both ends"] = None,
    layer: Annotated[str | None, "Override the family layer"] = None,
    ctx: Context = None,
) -> dict:
    """Insert a catalogue symbol as a real block with TAG attributes and named ports.

    Defines the block on first use (`defined`), creates a missing family layer
    from the P&ID layer set (`layers_created`), writes an ACADMCP_PID payload
    so readers without the catalogue still see the ports, and returns every
    port in WCS — connect them with `pid_line_draw`. An invalid ISA-5.1 tag is
    written and reported in `tag_warnings`; `drawing_critique` flags it.
    """
    from engineering.pid.insert import place_symbol

    await ctx.info(f"P&ID symbol {symbol} at ({x}, {y})")
    return await place_symbol(
        _backend(ctx),
        symbol,
        x,
        y,
        rotation,
        scale,
        tag,
        desc,
        fail,
        link,
        layer,
        actuator=actuator,
        type=type,
        location=location,
        params=params,
        shape=shape,
        direction=direction,
    )


@cad_tool(
    summary=(
        "Draw a P&ID line port-to-port: orthogonal route, ISA-5.1 class, "
        "line number, markers, arrow."
    ),
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "P&ID: Draw Line", "readOnlyHint": False},
    tags={"pid", "create"},
)
async def pid_line_draw(
    from_: Annotated[dict, "{handle, port} of a placed symbol, or {x, y}"],
    to: Annotated[dict, "{handle, port} of a placed symbol, or {x, y}"],
    line_class: Annotated[
        str,
        "process_major | process_minor | utility | pneumatic | electric | hydraulic | capillary | data",
    ] = "process_major",
    route: Annotated[str | list, "auto (orthogonal) | direct | [[x, y], ...] waypoints"] = "auto",
    stub: Annotated[
        float,
        Field(default=5.0, ge=0, description="Minimum straight run leaving each port (mm)"),
    ] = 5.0,
    line_number: Annotated[
        str | None, "Verbatim line number; omit to build one from the fields"
    ] = None,
    size: Annotated[str | None, "Nominal size, e.g. 100"] = None,
    service: Annotated[str | None, "Service code, e.g. P"] = None,
    spec: Annotated[str | None, "Piping spec, e.g. CS1"] = None,
    insulation: Annotated[str | None, "Insulation code, e.g. IH"] = None,
    number_format: Annotated[
        str | None,
        "Default {size}-{service}-{seq}-{spec}-{insulation}; empty fields drop out",
    ] = None,
    label: Annotated[bool, "Write the line number along the longest segment"] = True,
    arrow: Annotated[
        bool | None, "Flow arrow at the end; default per class (process yes, signal no)"
    ] = None,
    ctx: Context = None,
) -> dict:
    """Connect two ports with one LWPOLYLINE on the class's layer.

    Ports come from `pid_symbol_insert`; a bubble's radial port is entered
    without a name. `auto` picks the shortest orthogonal route that leaves each
    port straight for `stub` mm; when none exists the call refuses and says so
    — pass waypoints. `crossings` counts existing P&ID lines the new one cuts
    (reported, never refused) and `port_reuse` names ports that already had a
    line (branching is legal). Signal classes get ISA-5.1 markers (`//`, `X`,
    `L`, `o`) as small blocks on segments >= 15 mm; electric is dashed by layer.
    A signal line carries no pipe line number: it gets no auto-built number
    and no label, and does not consume the process sequence — a verbatim
    `line_number` is still written and labelled. With waypoints, a bubble's
    exit leaves towards the first waypoint (and the entry arrives from the
    last one); `crossings` follows existing arcs, not their chords.
    """
    from engineering.pid.drawlines import draw_line

    await ctx.info(f"P&ID line {line_class}")
    return await draw_line(
        _backend(ctx),
        from_,
        to,
        line_class,
        route,
        stub,
        line_number,
        size,
        service,
        spec,
        insulation,
        number_format,
        label,
        arrow,
    )


@cad_tool(
    summary=(
        "Read the P&ID back as a graph: symbols, lines, junctions, dangling ends, confidence."
    ),
    cost="read",
)
@mcp.tool(
    annotations={"title": "P&ID: Connectivity Graph", "readOnlyHint": True},
    tags={"pid", "query"},
)
async def pid_graph(
    tolerance: Annotated[
        float, Field(default=0.5, gt=0, description="Endpoint-to-port snap distance (mm)")
    ] = 0.5,
    label_search: Annotated[
        float,
        Field(default=15.0, gt=0, description="How far to look for tag/line-number text (mm)"),
    ] = 15.0,
    scope: Annotated[
        str, "current_space | all (every layout, current restored afterwards)"
    ] = "current_space",
    include_foreign: Annotated[
        bool, "Classify INSERTs not placed by this server (confidence < 1)"
    ] = True,
    include_geometry: Annotated[bool, "Include edge vertices"] = False,
    ctx: Context = None,
) -> dict:
    """Connectivity of the drawing as it *is*, not as it was drawn.

    Nodes: symbol INSERTs classified by catalogue name (confidence 1.0),
    ACADMCP_PID payload (0.95), tag attributes / block-name keywords (0.6) or
    a line touching an unknown INSERT (0.3); every node reports its `source`.
    Ports come from the INSERT's real rotation, scale and mirror, measured
    (a stretched bubble drops the node to 0.6 with a note). Edges: lines and
    polylines; each end resolves to a port, a junction on another line, or
    `dangling` with the nearest port as a hint — always within its own
    space, so `scope="all"` never joins two sheets. `stats.confidence_min`
    is the number to read before trusting a foreign drawing.
    """
    from engineering.pid.graph import build_graph

    return await build_graph(
        _backend(ctx), tolerance, label_search, scope, include_foreign, include_geometry
    )


@cad_tool(
    summary=(
        "Instrument index derived from the P&ID graph (tag, function, loop, "
        "connections); optional CSV."
    ),
    cost="read",
)
@mcp.tool(
    annotations={"title": "P&ID: Instrument Index", "readOnlyHint": True},
    tags={"pid", "query"},
)
async def pid_instrument_index(
    csv_path: Annotated[str | None, "Write the rows as CSV here (inside ALLOWED_PATHS)"] = None,
    tolerance: Annotated[
        float, Field(default=0.5, gt=0, description="Endpoint-to-port snap distance (mm)")
    ] = 0.5,
    scope: Annotated[str, "current_space | all"] = "current_space",
    ctx: Context = None,
) -> dict:
    """Every instrument bubble with its ISA-5.1 reading and what it connects to.

    Rows come from `pid_graph`, never from typed-in lists; the drawing is not
    modified. `csv_path` is the only file this tool writes.
    """
    from engineering.pid.deliverables import deliverable

    return await deliverable(
        _backend(ctx), "instrument_index", csv_path, tolerance=tolerance, scope=scope
    )


@cad_tool(
    summary=(
        "Line list derived from the P&ID graph (number, class, size, spec, from/to); optional CSV."
    ),
    cost="read",
)
@mcp.tool(
    annotations={"title": "P&ID: Line List", "readOnlyHint": True},
    tags={"pid", "query"},
)
async def pid_line_list(
    csv_path: Annotated[str | None, "Write the rows as CSV here (inside ALLOWED_PATHS)"] = None,
    tolerance: Annotated[
        float, Field(default=0.5, gt=0, description="Endpoint-to-port snap distance (mm)")
    ] = 0.5,
    scope: Annotated[str, "current_space | all"] = "current_space",
    ctx: Context = None,
) -> dict:
    """Every P&ID line with its number, class, size/service/spec/insulation and endpoints.

    Rows come from `pid_graph`; untagged lines sort last with `line_number: null`.
    The drawing is not modified; `csv_path` is the only file this tool writes.
    """
    from engineering.pid.deliverables import deliverable

    return await deliverable(_backend(ctx), "line_list", csv_path, tolerance=tolerance, scope=scope)


@cad_tool(
    summary=(
        "Equipment list derived from the P&ID graph (tag, kind, ports, connections); optional CSV."
    ),
    cost="read",
)
@mcp.tool(
    annotations={"title": "P&ID: Equipment List", "readOnlyHint": True},
    tags={"pid", "query"},
)
async def pid_equipment_list(
    csv_path: Annotated[str | None, "Write the rows as CSV here (inside ALLOWED_PATHS)"] = None,
    tolerance: Annotated[
        float, Field(default=0.5, gt=0, description="Endpoint-to-port snap distance (mm)")
    ] = 0.5,
    scope: Annotated[str, "current_space | all"] = "current_space",
    ctx: Context = None,
) -> dict:
    """Every equipment, valve and connector node with its ports and how many lines reach them.

    Rows come from `pid_graph`; the drawing is not modified and `csv_path` is
    the only file this tool writes.
    """
    from engineering.pid.deliverables import deliverable

    return await deliverable(
        _backend(ctx), "equipment_list", csv_path, tolerance=tolerance, scope=scope
    )


@cad_tool(
    summary=(
        "Draw a whole P&ID from one declarative spec in one transaction; returns graph + critique."
    ),
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "P&ID: From Spec", "readOnlyHint": False},
    tags={"pid", "create", "batch"},
)
async def pid_from_spec(
    spec: Annotated[
        dict,
        "{sheet, equipment[], valves[], instruments[], lines[], connectors[]} — see the P&ID prompt",
    ],
    dry_run: Annotated[bool, "Validate and route in memory; draw nothing"] = False,
    ctx: Context = None,
) -> dict:
    """Equipment, valves, instruments and connectors placed, then every line
    drawn port-to-port, inside one transaction: any bad item rolls the whole
    sheet back and the error names it (`lines[2].to`); so does an interruption
    (a client cancellation mid-run leaves no half-drawn sheet and no open
    transaction on either engine — the live one goes back to the UNDO Mark
    `transaction_begin` set, so it never waits at an AutoCAD prompt; should
    that rollback itself fail, the failure is logged, the cancellation is what
    the client sees, and the drawing must be checked before a retry).
    A non-finite coordinate, rotation, scale or stub is refused
    by path before anything is placed. The response carries the graph read
    back from the drawing and the P&ID critique issues, so the caller sees
    dangling ends or duplicate tags in the same round trip. `dry_run` returns
    the planned vertices and crossings without touching the drawing.
    """
    from engineering.pid.spec import run_spec

    await ctx.info("P&ID from spec" + (" (dry run)" if dry_run else ""))
    return await run_spec(_backend(ctx), spec, dry_run)


@cad_tool(
    summary="Parse an ISA-5.1 instrument tag or an equipment tag; explains every letter.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "P&ID: Parse Tag", "readOnlyHint": True},
    tags={"pid", "query"},
)
async def pid_tag_parse(
    tag: Annotated[str, "e.g. FIC-101, 10-PDT-203A, P-101A"],
    kind: Annotated[str, "auto | instrument | equipment"] = "auto",
    equipment_prefixes: Annotated[
        dict | None,
        "Replace the default equipment prefix map, e.g. {'Q': 'quench tower'}",
    ] = None,
    ctx: Context = None,
) -> dict:
    """ISA-5.1-2009 Table 4.1 as a grammar: first letter, modifier, readout and
    output functions, trailing high/low, loop number and suffix — with the
    standard's description ("Flow Indicating Controller") or the first
    offending letter. Equipment prefixes are not standardised, so the default
    map (P pump, V vessel, E exchanger, ...) can be replaced per call.
    """
    from engineering.pid.tags import parse_tag

    return parse_tag(tag, kind, equipment_prefixes)


# ---------------------------------------------------------------------------
# ── SECTION 18: Styles (10 tools) ───────────────────────────────────────────
# ---------------------------------------------------------------------------
#
# Pack: core (drafting essentials). Lean profile: dimstyle_set_current and
# textstyle_set_current only (Task 13) — a lean client needs to *use* a
# style a template already holds far more often than to author one.


@cad_tool(
    summary="List dimension styles with their ISO-25/ANSI variables and the current one.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Styles: List Dimension Styles", "readOnlyHint": True},
    tags={"style", "query"},
)
async def dimstyle_list(ctx: Context = None) -> dict:
    """Every DIMSTYLE table entry with the seventeen preset variables
    (DIMTXT, DIMASZ, DIMEXE, DIMEXO, DIMGAP, DIMTAD, DIMTIH, DIMTOH, DIMDEC,
    DIMDSEP, DIMLUNIT, DIMZIN, DIMBLK, DIMTXSTY, DIMLWD, DIMLWE, DIMSCALE).

    Headless, `values` is what the DXF stores (the schema default where an
    entry omits a code). On the live engine ActiveX has no per-style getters,
    so `values` is read for the *current* style only and the other rows carry
    `values: null`, `values_available: false` — `dimstyle_set_current` a
    style to read it. Never refuses on an open drawing.
    """
    rows = await _backend(ctx).dimstyle_list()
    return {
        "ok": True,
        "styles": rows,
        "count": len(rows),
        "current": next((row["name"] for row in rows if row["current"]), None),
    }


@cad_tool(
    summary="Create a dimension style from the ISO-25 or ANSI preset, with per-variable overrides.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Styles: Create Dimension Style", "readOnlyHint": False},
    tags={"style", "create"},
)
async def dimstyle_create(
    name: Annotated[str, "New dimension style name, e.g. ISO-25, ANSI, MECH-A3"],
    preset: Annotated[
        str | None,
        "iso-25 (ISO 129-1, 2.5 mm, comma) | ansi (ASME Y14.2, 3 mm, point); omit for the ISO-25 base",
    ] = None,
    overrides: Annotated[
        dict | None,
        "DIM* variables applied on top of the preset, e.g. {DIMTXT: 3.5, DIMDSEP: '.', DIMTAD: 0}",
    ] = None,
    set_current: Annotated[bool, "Also make it the current dimension style"] = False,
    ctx: Context = None,
) -> dict:
    """Create a dimension style: preset values first, then `overrides`.

    Refuses, before anything is written: an unknown preset; an override key
    outside the 33-variable whitelist (the 17 preset variables plus DIMTOL,
    DIMTP, DIMTM, DIMTOLJ, DIMTFAC, DIMLFAC, DIMRND, DIMATFIT, DIMTMOVE,
    DIMCLRD/E/T, DIMSAH, DIMBLK1/2, DIMCEN); a value outside its range
    (DIMDEC 0-8, sizes > 0, DIMTAD 0-4, DIMDSEP one character, lineweights an
    AutoCAD code); a name that already exists (use `dimstyle_modify`); a
    user DIMBLK/DIMBLK1/DIMBLK2 naming a block the drawing does not define
    (built-in arrowheads need none); a DIMTXSTY that is neither an existing
    text style nor a bundled preset (ISOCP, ISOCPEUR, ARIAL, ROMANS — those
    are created on both engines, `textstyle_created`; live, a preset whose
    font file the seat lacks is refused the same way, nothing written). On
    the live engine a write ActiveX still refuses after the style is added
    is rolled back — previous style current, the half-made entry deleted.
    With `set_current` the next dimension carries the style on both engines.
    """
    from engineering.standards.dimstyles import resolve_dimstyle

    values = resolve_dimstyle(preset, overrides)
    await ctx.info(f"Dimension style {name!r} from preset {preset or 'iso-25'}")
    return await _backend(ctx).dimstyle_create(name, values, set_current)


@cad_tool(
    summary="Change DIM* variables on an existing dimension style; reports only what moved.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Styles: Modify Dimension Style", "readOnlyHint": False},
    tags={"style", "modify"},
)
async def dimstyle_modify(
    name: Annotated[str, "Existing dimension style name"],
    overrides: Annotated[dict, "DIM* variables to change, e.g. {DIMDEC: 3, DIMTXT: 3.5}"],
    ctx: Context = None,
) -> dict:
    """Change variables on an existing dimension style.

    `changed` names only the variables that actually moved — re-setting a
    value to itself is not a change. `dimensions_using_style` lists the
    dimensions already drawn with it; headlessly `rerender_required: true`
    says they still show the old style until redrawn (AutoCAD re-renders on
    the next regen). Refuses a missing style, an empty `overrides`, a key
    outside the whitelist, a value outside its range, a DIMTXSTY that does
    not exist, a user arrowhead block the drawing does not define — all
    before any write. On the live engine, editing a non-current style makes
    it current for the duration of the call and the previous style is
    restored — also when a write fails midway (its unsaved overrides are
    discarded, which is AutoCAD's own DIMSTYLE rule).
    """
    await ctx.info(f"Modifying dimension style {name!r}")
    return await _backend(ctx).dimstyle_modify(name, overrides)


@cad_tool(summary="Make a dimension style current so new dimensions use it.", cost="safe")
@mcp.tool(
    annotations={"title": "Styles: Set Current Dimension Style", "readOnlyHint": False},
    tags={"style", "modify"},
)
async def dimstyle_set_current(
    name: Annotated[str, "Dimension style to make current (case-insensitive)"],
    ctx: Context = None,
) -> dict:
    """Set `$DIMSTYLE` and load the style's variables as the current DIM*
    settings — what a live seat's DIMSTYLE Restore does — so `dimension_*`
    and `dimension_auto` draw with it on both engines. Per-dimension
    tolerances, fits and text overrides still apply on top. Refuses a style
    the drawing does not hold. Returns `previous` and `changed`.
    """
    return await _backend(ctx).dimstyle_set_current(name)


@cad_tool(summary="List text styles: font file, height, width factor, oblique angle.", cost="read")
@mcp.tool(
    annotations={"title": "Styles: List Text Styles", "readOnlyHint": True},
    tags={"style", "query"},
)
async def textstyle_list(ctx: Context = None) -> dict:
    """Every STYLE table entry and the one new TEXT/MTEXT will use."""
    rows = await _backend(ctx).textstyle_list()
    return {
        "ok": True,
        "styles": rows,
        "count": len(rows),
        "current": next((row["name"] for row in rows if row["current"]), None),
    }


@cad_tool(
    summary="Create a text style from a bundled font (isocp, isocpeur, arial, romans) or any file.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Styles: Create Text Style", "readOnlyHint": False},
    tags={"style", "create"},
)
async def textstyle_create(
    name: Annotated[str, "New text style name, e.g. ISOCP"],
    font: Annotated[
        str,
        "isocp.shx | isocpeur.ttf | arial.ttf | romans.shx (name or file, any case) or any font file",
    ],
    height: Annotated[
        float, Field(default=0.0, ge=0, description="Fixed height; 0 = ask per text")
    ] = 0.0,
    width_factor: Annotated[float, Field(default=1.0, gt=0, description="Width factor")] = 1.0,
    oblique_deg: Annotated[float, "Obliquing angle in degrees, -85..85"] = 0.0,
    set_current: Annotated[bool, "Also make it the current text style"] = False,
    ctx: Context = None,
) -> dict:
    """Create a text style.

    Headlessly a font nobody can find is *written* and reported
    `font_resolved: false` (AutoCAD substitutes at open; the headless
    renderer falls back), because a DXF stores only the name. The live
    engine cannot do that: ActiveX refuses a font AutoCAD cannot open, so a
    file that is neither on its support path nor in the Windows Fonts folder
    is refused there before any write (the message names the folders and the
    presets); a TrueType font is written by its typeface (`SetFont`, what the
    STYLE dialog does — the record carries `arial.ttf`, never a machine
    path), an SHX by the file name. Refuses, before any write on both
    engines: a name that exists, an empty font, `width_factor <= 0`,
    `height < 0`, an oblique angle outside ±85°.
    """
    await ctx.info(f"Text style {name!r} with font {font!r}")
    return await _backend(ctx).textstyle_create(
        name, font, height, width_factor, oblique_deg, set_current
    )


@cad_tool(summary="Make a text style current so new text uses it.", cost="safe")
@mcp.tool(
    annotations={"title": "Styles: Set Current Text Style", "readOnlyHint": False},
    tags={"style", "modify"},
)
async def textstyle_set_current(
    name: Annotated[str, "Text style to make current (case-insensitive)"],
    ctx: Context = None,
) -> dict:
    """Set `$TEXTSTYLE`; `entity_create_text` / `entity_create_mtext` then
    carry the style on both engines. Refuses a style the drawing does not
    hold. Returns `previous` and `changed`."""
    return await _backend(ctx).textstyle_set_current(name)


@cad_tool(
    summary="List multileader styles: arrow size, landing gap, text style and height.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Styles: List Multileader Styles", "readOnlyHint": True},
    tags={"style", "query"},
)
async def mleaderstyle_list(ctx: Context = None) -> dict:
    """Every MLEADERSTYLE with its four values on both engines
    (`values_available: true`). Headless they are read from the MLEADERSTYLE
    object; live from the `AcadMLeaderStyle` objects the `ACAD_MLEADERSTYLE`
    dictionary holds (ActiveX exposes no *collection* property for them, but
    the dictionary items are full objects). `leader_create_mleader` can still
    override any of them per leader."""
    rows = await _backend(ctx).mleaderstyle_list()
    return {"ok": True, "styles": rows, "count": len(rows)}


@cad_tool(
    summary="Create a multileader style from the ISO or ANSI preset, on both engines.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Styles: Create Multileader Style", "readOnlyHint": False},
    tags={"style", "create"},
)
async def mleaderstyle_create(
    name: Annotated[str, "New multileader style name"],
    preset: Annotated[
        str, "iso (2.5 mm arrow, 1.0 landing, ISOCP 2.5) | ansi (3.0, 1.5, ROMANS 3.0)"
    ],
    overrides: Annotated[
        dict | None, "arrow_size | landing_gap | text_style | text_height on top of the preset"
    ] = None,
    ctx: Context = None,
) -> dict:
    """Create a multileader style: preset values first, then `overrides`.

    Headless it lands on `doc.mleader_styles`; live it is added through the
    `ACAD_MLEADERSTYLE` dictionary (`AddObject(name, "AcDbMLeaderStyle")`)
    and the object's properties are written. Refuses, before any write: an
    unknown preset; an override key outside the four; `arrow_size` /
    `text_height <= 0`, `landing_gap < 0`; a name that exists; a `text_style`
    that is neither present nor a bundled preset (ISOCP, ISOCPEUR, ARIAL,
    ROMANS — those are created on both engines, `textstyle_created`; live, a
    preset whose font file the seat lacks is refused, nothing written).
    """
    from engineering.standards.mleaderstyles import resolve_mleaderstyle

    values = resolve_mleaderstyle(preset, overrides)
    await ctx.info(f"Multileader style {name!r} from preset {preset}")
    return await _backend(ctx).mleaderstyle_create(name, values)


@cad_tool(
    summary="Put the drawing on ISO or ANSI in one call: dimstyle, text style, units, mech layers.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Drawing: Apply Drafting Standard", "readOnlyHint": False},
    tags={"style", "drawing"},
)
async def drawing_apply_standard(
    standard: Annotated[
        str, "iso (ISO-25 / ISOCP, decimal comma) | ansi (ANSI / ROMANS, decimal point)"
    ],
    layers: Annotated[bool, "Also bootstrap the mech layer set with ISO 128 lineweights"] = True,
    units: Annotated[
        bool, "Also write INSUNITS mm, LUNITS/AUNITS decimal, LTSCALE 1, DIMSCALE 1"
    ] = True,
    ctx: Context = None,
) -> dict:
    """One call for the common case: the standard's dimension style (created
    if missing, then current), its text style (created if missing, then
    current), the shared units, and with `layers` the `mech` layer set (ANSI
    layer naming is company-specific, so both standards share it).

    Every item reports `created` or already present; `settings.changed`
    names only the variables that moved, so a second call reports nothing.
    An existing ISO-25 / ANSI style is reused as it is, not reset — use
    `dimstyle_modify` to change one. Refuses an unknown standard, or a units
    variable it cannot read back, before any write; the underlying style
    refusals (`dimstyle_create`, `textstyle_create`) apply unchanged. In the
    `lean` profile (Task 13).
    """
    from engineering.standards.apply import apply_standard

    await ctx.info(f"Applying the {standard} drafting standard")
    return await apply_standard(_backend(ctx), standard, layers, units)


# ---------------------------------------------------------------------------
# ── SECTION 19: Page Setup & Templates (6 tools) ────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(
    summary="Read each sheet's page setup: paper, orientation, ctb, scale, device.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "List Page Setups", "readOnlyHint": True},
    tags={"pagesetup", "layout", "plot", "query"},
)
async def page_setup_list(
    layout: Annotated[
        str | None, "One paper-space layout, or omit for every sheet (Model is never listed)."
    ] = None,
    ctx: Context = None,
) -> dict:
    """The page setup stored on each paper-space layout.

    `paper` is the catalogue name (ISO_A3, ANSI_B, …) when the stored size
    matches one within 0.5 mm, otherwise the media name as stored; `size_mm`,
    `orientation`, `plot_style` (ctb), `scale` ("fit", "1:50", or a custom
    ratio), `plot_area`, `device`, `margins_mm` [top, bottom, left, right] and
    `center` are read back from the LAYOUT object (headless) or the AutoCAD
    Layout (live). Refuses an unknown layout name and `Model` (model space has
    no sheet; plot it with `drawing_export_pdf(layout=None)`).
    """
    try:
        rows = await _backend(ctx).page_setup_list(layout)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return {"ok": True, "layouts": rows, "count": len(rows)}


@cad_tool(
    summary="Set a sheet's paper, orientation, ctb, plot scale and device (PAGESETUP).",
    cost="safe",
)
@mcp.tool(
    annotations={"title": "Apply Page Setup", "destructiveHint": False},
    tags={"pagesetup", "layout", "plot"},
)
async def page_setup_apply(
    layout: Annotated[str, "Paper-space layout to set up (never 'Model')."],
    paper: Annotated[str, "ISO_A0…ISO_A4 or ANSI_A…ANSI_E (short forms A3, ansi_b accepted)."],
    orientation: Annotated[str, "landscape | portrait"] = "landscape",
    plot_style: Annotated[
        str, "Plot style table, e.g. monochrome.ctb, acad.ctb, Grayscale.ctb (plot_style_list)."
    ] = "monochrome.ctb",
    scale: Annotated[
        str, "fit | 1:1 | 1:2 | 1:5 | 1:10 | 1:20 | 1:50 | 1:100 | 2:1 | 5:1 | 10:1"
    ] = "fit",
    plot_area: Annotated[str, "layout | extents"] = "layout",
    device: Annotated[str, "Plotter configuration (.pc3) or printer name."] = "DWG To PDF.pc3",
    margins_mm: Annotated[
        list[float] | None,
        "[top, bottom, left, right] in mm. Headless only: AutoCAD takes margins from the .pc3.",
    ] = None,
    center: Annotated[bool, "Centre the plot on the paper."] = True,
    ctx: Context = None,
) -> dict:
    """AutoCAD's PAGESETUP as one call, on both engines.

    Paper sizes are ISO 216 / ANSI Y14.1; the media is written in AutoCAD's own
    spelling (`ISO_A3_(420.00_x_297.00_MM)`). `changed` lists only the values
    that moved — re-applying the same setup reports `{}`. An unknown ctb is
    written and reported `plot_style_known: false` (a missing ctb only matters
    at plot time). Viewports on the sheet are never touched.

    Refused before anything is written: an unknown paper, orientation, scale
    or plot_area, an empty device or plot_style, margins that leave no
    printable area, `Model`, an unknown layout. On the live engine `margins_mm`
    is refused (the .pc3 owns them) and a media the device does not offer is
    refused with the device's names for the same paper.

    The proof is the PDF: `batch_plot` reads each sheet's `/MediaBox` back.
    """
    from engineering.standards.papers import resolve_page_setup

    try:
        setup = resolve_page_setup(
            paper, orientation, plot_style, scale, plot_area, device, margins_mm, center
        )
        await ctx.info(f"Applying page setup {setup['paper']} {orientation} to {layout}")
        return await _backend(ctx).page_setup_apply(layout, setup)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@cad_tool(
    summary="List the plot style tables (ctb): the AutoCAD catalogue, plus what is installed.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "List Plot Styles", "readOnlyHint": True},
    tags={"pagesetup", "layout", "plot", "query"},
)
async def plot_style_list(ctx: Context = None) -> dict:
    """The ctb files AutoCAD ships (`monochrome.ctb`, `acad.ctb`, `Grayscale.ctb`,
    the Screening set, …) and, on the live engine, the files actually present
    in `Preferences.Files.PrinterStyleSheetPath` (`source: "installed"`,
    `installed: true/false` per row). Headlessly `installed` is `null`: there
    is no installation to scan, and the catalogue is not evidence of one.
    Never refuses; an unreadable Preferences object degrades to the catalogue.
    """
    backend = _backend(ctx)
    rows = await backend.plot_style_list()
    return {"ok": True, "styles": rows, "count": len(rows), "backend": backend.name}


@cad_tool(
    summary="Plot every sheet (or the named ones) to PDF; each sheet size is read back from its PDF.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Batch Plot", "destructiveHint": False},
    tags={"pagesetup", "layout", "plot", "export"},
)
async def batch_plot(
    output_dir: Annotated[str, "Folder for the PDFs (created if missing)."],
    layouts: Annotated[
        list[str] | None,
        "Layouts to plot; omit for every paper-space layout. 'Model' only when named.",
    ] = None,
    format: Annotated[str, "pdf (the only format this release plots)."] = "pdf",
    file_pattern: Annotated[
        str, "File name per sheet; {drawing} and {layout} are substituted."
    ] = "{drawing}-{layout}.pdf",
    ctx: Context = None,
) -> dict:
    """AutoCAD's PUBLISH for a PDF set, through `drawing_export_pdf(layout=…)`.

    One row per sheet: `path`, `bytes`, and `mediabox_mm` **parsed from the
    PDF that was written** (`/MediaBox`, points → mm), with `paper` naming the
    catalogue size it matches within 0.5 mm — so the sheet size is verified,
    never assumed. Model space is included only when named.

    Refused before any file is written: a format other than pdf, an empty
    `layouts` list, a layout that does not exist, a `file_pattern` containing a
    path separator or lacking `{layout}` when more than one sheet is plotted,
    and any output path outside the allowed directories. A sheet whose PDF has
    no readable MediaBox is reported with `mediabox_mm: null` and an `error`.
    """
    from engineering.standards.plot import batch_plot as _batch_plot

    if str(format).lower() != "pdf":
        raise ToolError(
            f"batch_plot: format must be 'pdf' (got {format!r}); PDF is the only plot format"
        )
    destination = validate_path(output_dir, allow_write=True)
    await ctx.info(f"Batch plotting {layouts or 'every sheet'} to {destination}")
    try:
        return await _batch_plot(_backend(ctx), layouts, str(destination), file_pattern)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@cad_tool(
    summary="List the five bundled drawing templates: standard, sheet, layers, styles, page setup.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "List Drawing Templates", "readOnlyHint": True},
    tags={"pagesetup", "template", "drawing", "query"},
)
async def drawing_template_list(ctx: Context = None) -> dict:
    """The templates `drawing_new(template=<name>)` can start from.

    Each row: `standard` (ISO/ANSI), `sheet`, `paper`, `layout` (the sheet tab's
    name), `layer_set` (mech / pid / iso13567), `dimstyle` / `textstyle`
    (ISO-25 / ISOCP or ANSI / ROMANS), `scale`, `title_block` (the ISO 5457 A3
    frame, A3 sheets only), the `settings` applied, and `files.dxf` /
    `files.dwt` with `present` flags — the `.dwt` twins exist only once built
    on a live AutoCAD (`scripts/smoke_settings_com.py --build-dwt`). Never
    refuses; it reads a catalogue.
    """
    from engineering.standards.templates import TEMPLATES_DIR, template_rows

    rows = template_rows()
    return {
        "ok": True,
        "templates": rows,
        "count": len(rows),
        "templates_dir": str(TEMPLATES_DIR),
        "engine": _backend(ctx).name,
    }


@cad_tool(
    summary="Save the current drawing as a template: .dwt on live AutoCAD, .dxf headlessly.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Save As Template", "destructiveHint": False},
    tags={"pagesetup", "template", "drawing"},
)
async def drawing_template_save(
    path: Annotated[str, "Destination .dwt (live AutoCAD) or .dxf (either engine)."],
    name: Annotated[str | None, "Template name for the report (default: the file stem)."] = None,
    description: Annotated[
        str | None, "Template description → DWT summary Comments on the live engine."
    ] = None,
    ctx: Context = None,
) -> dict:
    """AutoCAD's SAVEAS → Drawing Template, on both engines.

    Live AutoCAD writes a real `.dwt` (`SaveAs(path, ac2018_Template)`) and
    puts `description` into the template's summary Comments; the active
    document is rebound to the new file, as SAVEAS does. Headlessly a `.dxf`
    template is written — `drawing_new(template=<path>)` opens it — and a
    `.dwt` request is refused with capability `dwt_write` (the message names
    the DXF route and the COM route); a description is reported
    `description_written: false` because DXF has nowhere to keep it. Any other
    suffix, and any path outside the allowed directories, is refused before
    a byte is written.

    Pack: core · lean: no (`drawing_save_as` is not lean either).
    """
    validated = validate_path(path, allow_write=True)
    await ctx.info(f"Saving template: {validated}")
    try:
        return await _backend(ctx).drawing_template_save(str(validated), name, description)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


# ---------------------------------------------------------------------------
# ── SECTION 20: Environment (22 tools) ──────────────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(
    summary="List the open documents: which is active, which have unsaved changes.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "List Documents", "readOnlyHint": True},
    tags={"environment", "drawing"},
)
async def document_list(ctx: Context = None) -> dict:
    """Every open document with `name`, `path`, `active`, `saved` and
    `entity_count`.

    Headless, the server keeps its own registry: `drawing_new` / `drawing_open`
    add an entry (`untitled-N` or the file path) instead of replacing the only
    one, and `document_activate` chooses which one every later tool targets.
    Live, this is AutoCAD's `Documents` collection. No refusals: an empty
    backend answers with an empty list. Pack: settings · lean: no.
    """
    rows = await _backend(ctx).document_list()
    return {
        "documents": rows,
        "count": len(rows),
        "active": next((r["name"] for r in rows if r["active"]), None),
    }


@cad_tool(summary="Switch which open document every later tool call targets.", cost="safe")
@mcp.tool(
    annotations={"title": "Activate Document", "readOnlyHint": False, "destructiveHint": False},
    tags={"environment", "drawing"},
)
async def document_activate(
    name_or_path: Annotated[
        str, "A name from document_list (`gear.dxf`, `untitled-2`) or the full path"
    ],
    ctx: Context = None,
) -> dict:
    """Make one open document the active one; reports `previous`.

    Matches the full path first, then the file name; an ambiguous name is
    refused with the candidates, an unknown one with the list of open
    documents. Headless, a document quarantined after an abandoned call cannot
    be switched away from — `drawing_new`, `drawing_open` or closing it are
    the ways out. Pack: settings · lean: no.
    """
    await ctx.info(f"Activating document {name_or_path!r}")
    return await _backend(ctx).document_activate(name_or_path)


@cad_tool(
    summary="Close one open document by name; never drops unsaved work unless told to.",
    cost="destructive",
)
@mcp.tool(
    annotations={"title": "Close Document", "destructiveHint": True},
    tags={"environment", "drawing"},
)
async def document_close(
    name_or_path: Annotated[
        str | None, "A name from document_list or the full path; omit for the active document"
    ] = None,
    save: Annotated[bool, "Write unsaved changes to the document's own path first"] = False,
    discard: Annotated[bool, "Close even with unsaved changes, dropping them"] = False,
    ctx: Context = None,
) -> dict:
    """Close a document and report `saved`, `discarded_changes` and the new `active`.

    Refusals, all before anything is closed: unsaved changes with neither
    `save` nor `discard`; `save` on a document that has never been saved (no
    path to write — live, `Close(True)` would open AutoCAD's Save dialog and
    block the COM thread); `save` together with `discard`. Closing the last
    document leaves the backend with none open, which every tool already
    reports. `drawing_close` is the same operation for the active document
    with its 1.4 contract kept. Pack: settings · lean: no.
    """
    await ctx.info(
        f"Closing document {name_or_path or '(active)'} (save={save}, discard={discard})"
    )
    return await _backend(ctx).document_close(name_or_path, save, discard)


@cad_tool(
    summary="Save the layer table (on/frozen/locked/colour/linetype/lineweight/plot + current) under a name, in the file.",
    cost="safe",
)
@mcp.tool(
    annotations={"title": "Save Layer State", "readOnlyHint": False, "destructiveHint": False},
    tags={"environment", "layer"},
)
async def layer_state_save(
    name: Annotated[str, "State name, e.g. PLOT-SET or DESIGN"],
    description: Annotated[str | None, "Free text stored with the state"] = None,
    ctx: Context = None,
) -> dict:
    """Snapshot every layer's on/frozen/locked/color/linetype/lineweight/plot
    flags plus the current layer as a named state stored in the drawing.

    These are the server's own portable layer states: JSON chunks in an XRECORD
    under the `ACADMCP_LAYERSTATES` dictionary. They live in the file and
    travel with it, work on both engines, and do **not** appear in AutoCAD's
    Layer States Manager (LAYERSTATE). Same name replaces (`replaced: true`).
    Refusals: an empty or control-character name, a non-string description.
    Pack: settings · lean: no.
    """
    await ctx.info(f"Saving layer state {name!r}")
    return await _backend(ctx).layer_state_save(name, description)


@cad_tool(summary="Restore a saved layer state, all properties or a chosen subset.", cost="safe")
@mcp.tool(
    annotations={"title": "Restore Layer State", "readOnlyHint": False, "destructiveHint": False},
    tags={"environment", "layer"},
)
async def layer_state_restore(
    name: Annotated[str, "A name from layer_state_list"],
    properties: Annotated[
        list[str] | None,
        "Subset of on, frozen, locked, color, linetype, lineweight, plot, current; omit for all",
    ] = None,
    ctx: Context = None,
) -> dict:
    """Apply a saved state to the layers that still exist.

    `missing_layers` (in the state, not in the drawing) are skipped, never
    created; `new_layers` (in the drawing, not in the state) are untouched;
    `applied` says how many layers and which properties moved. Refusals: an
    unknown state name (lists the saved ones), a property outside the eight
    (names the index). Live, a layer AutoCAD refuses to change (freezing the
    active layer, making a frozen layer current) lands in `warnings` instead
    of failing the call; the state's current layer is thawed before it is
    made current when `frozen` is being restored, so save → freeze → restore
    round-trips. Portable server states, not Layer States Manager entries.
    Pack: settings · lean: no.
    """
    await ctx.info(f"Restoring layer state {name!r}")
    return await _backend(ctx).layer_state_restore(name, properties)


@cad_tool(summary="List the layer states saved in this drawing.", cost="read")
@mcp.tool(
    annotations={"title": "List Layer States", "readOnlyHint": True},
    tags={"environment", "layer"},
)
async def layer_state_list(ctx: Context = None) -> dict:
    """Every state under `ACADMCP_LAYERSTATES` with its description and layer
    count. These are the server's portable states, not AutoCAD Layer States
    Manager entries. No refusals. Pack: settings · lean: no."""
    rows = await _backend(ctx).layer_state_list()
    return {"states": rows, "count": len(rows)}


@cad_tool(summary="Delete one saved layer state from the drawing.", cost="destructive")
@mcp.tool(
    annotations={"title": "Delete Layer State", "destructiveHint": True},
    tags={"environment", "layer"},
)
async def layer_state_delete(
    name: Annotated[str, "A name from layer_state_list"],
    ctx: Context = None,
) -> dict:
    """Remove the named state's XRECORD. Layers themselves are untouched.
    Refusal: an unknown name (lists the saved ones). Pack: settings · lean: no."""
    await ctx.info(f"Deleting layer state {name!r}")
    return await _backend(ctx).layer_state_delete(name)


@cad_tool(
    summary="Save a named view: a centre and height (and width) to come back to.", cost="safe"
)
@mcp.tool(
    annotations={"title": "Save Named View", "readOnlyHint": False, "destructiveHint": False},
    tags={"environment", "view"},
)
async def view_named_save(
    name: Annotated[str, "View name, e.g. DETAIL-A"],
    center: Annotated[
        list[float] | None,
        "[x, y] in WCS; default: the current view (live) or the drawing extents (headless)",
    ] = None,
    height: Annotated[float | None, "View height in drawing units; default as for center"] = None,
    width: Annotated[
        float | None, "View width; default: height × the current viewport aspect"
    ] = None,
    ctx: Context = None,
) -> dict:
    """Store a VIEW table entry (AutoCAD's VIEW command). Same name replaces
    (`replaced: true`). Refusals: `height`/`width` ≤ 0 or non-finite, a
    malformed `center`, and headless an empty drawing with no `center`/`height`
    (there are no extents to default to). Pack: settings · lean: no."""
    await ctx.info(f"Saving named view {name!r}")
    return await _backend(ctx).view_named_save(name, center, height, width)


@cad_tool(summary="Restore a named view.", cost="safe")
@mcp.tool(
    annotations={"title": "Restore Named View", "readOnlyHint": False, "destructiveHint": False},
    tags={"environment", "view"},
)
async def view_named_restore(
    name: Annotated[str, "A name from view_named_list"],
    ctx: Context = None,
) -> dict:
    """Live: zooms the active viewport to the saved window the way `-VIEW _R`
    does — VIEWCTR becomes the saved centre and VIEWSIZE the saved height
    (or width / display aspect when the window is wider than the display);
    `applied: "zoom_window"` plus the read-back `viewctr` / `viewsize`.
    Headless there is no display: the saved window is fitted into the
    `*Active` VPORT — the view AutoCAD opens the file on — whose aspect ratio
    is left as the file carries it (AutoCAD reconciles a changed aspect by the
    viewport's lower-left corner, which shifts the centre); the result says
    `applied: "header_only"` with the written `vport_height` and the kept
    `aspect_ratio`, the same reported-no-op rule as `view_zoom_extents`.
    Refusal: an unknown name (lists the saved views). Pack: settings · lean:
    no."""
    return await _backend(ctx).view_named_restore(name)


@cad_tool(summary="List the named views saved in this drawing.", cost="read")
@mcp.tool(
    annotations={"title": "List Named Views", "readOnlyHint": True},
    tags={"environment", "view"},
)
async def view_named_list(ctx: Context = None) -> dict:
    """Every VIEW table entry with centre, height and width. No refusals.
    Pack: settings · lean: no."""
    rows = await _backend(ctx).view_named_list()
    return {"views": rows, "count": len(rows)}


@cad_tool(summary="List the user coordinate systems, with the implicit world one.", cost="read")
@mcp.tool(
    annotations={"title": "List UCS", "readOnlyHint": True},
    tags={"environment", "view"},
)
async def ucs_list(ctx: Context = None) -> dict:
    """The `world` row first, then every UCS table entry with origin and unit
    axes, `current` on the active one. `world` is current only when the frame
    IS the WCS (WORLDUCS live, the `$UCS*` header headless) — an unnamed UCS
    (`UCS Origin` / `3P` without saving) has an empty name too and is
    reported as a trailing row with `name: null` and its origin/axes, so
    `current` is `null` then and never `world`. Tool coordinates stay WCS on
    both engines whatever is current. No refusals. Pack: settings · lean: no."""
    rows = await _backend(ctx).ucs_list()
    active = next((r for r in rows if r["current"]), None)
    return {
        "ucs": rows,
        "count": len(rows),
        "current": active["name"] if active else None,
        "current_unnamed": bool(active and active["name"] is None),
    }


@cad_tool(
    summary="Define (or replace) a named UCS from an origin and two perpendicular axes, and make it current.",
    cost="safe",
)
@mcp.tool(
    annotations={"title": "Set UCS", "readOnlyHint": False, "destructiveHint": False},
    tags={"environment", "view"},
)
async def ucs_set(
    name: Annotated[str, "UCS name"],
    origin: Annotated[list[float], "[x, y, z] in WCS"],
    x_axis: Annotated[list[float], "Direction of the new X axis (any length)"],
    y_axis: Annotated[list[float], "Direction of the new Y axis; must be perpendicular to x_axis"],
    ctx: Context = None,
) -> dict:
    """Store a UCS table entry and make it current (AutoCAD's UCS command).

    Axes are normalised to unit vectors. **Every tool keeps taking and
    returning WCS coordinates** — the repository rule; a UCS is for the
    operator's own drafting, and no tool starts interpreting inputs in it.
    Refusals, before any write: a name of `world` (reserved), a zero-length
    axis, and non-perpendicular axes — the message carries the measured angle
    (tolerance 0.001°). Pack: settings · lean: no.
    """
    await ctx.info(f"Setting UCS {name!r}")
    return await _backend(ctx).ucs_set(name, origin, x_axis, y_axis)


@cad_tool(summary="Make a saved UCS current, or `world` to reset to WCS.", cost="safe")
@mcp.tool(
    annotations={"title": "Restore UCS", "readOnlyHint": False, "destructiveHint": False},
    tags={"environment", "view"},
)
async def ucs_restore(
    name: Annotated[str, "A name from ucs_list, or `world`"],
    ctx: Context = None,
) -> dict:
    """Live: `ActiveUCS` for a named entry; `world` runs `UCS World`, which is
    refused while AutoCAD has an active command (CMDACTIVE — press ESC).
    Headless: the `$UCSNAME/$UCSORG/$UCSXDIR/$UCSYDIR` header variables.
    Refusal: an unknown name (lists the saved ones). Tool coordinates stay
    WCS. Pack: settings · lean: no."""
    return await _backend(ctx).ucs_restore(name)


@cad_tool(
    summary="Attach to the running AutoCAD or start it, optionally opening a file (live only).",
    cost="safe",
)
@mcp.tool(
    annotations={
        "title": "Launch / Attach AutoCAD",
        "readOnlyHint": False,
        "destructiveHint": False,
    },
    tags={"environment", "system"},
)
async def system_launch(
    visible: Annotated[bool, "Show the application window"] = True,
    open_path: Annotated[str | None, "A .dwg/.dxf to open after attaching"] = None,
    ctx: Context = None,
) -> dict:
    """Connect to the application named by `CAD_PROGID` (attach if running,
    launch otherwise) and report `launched`, `attached`, `version` and the
    active `document`. Refusals: headless, `capability: "live_application"`
    (there is no application to launch); an `open_path` that fails path
    validation. Pack: settings · lean: no.
    """
    if open_path is not None:
        open_path = str(validate_path(open_path, allow_write=False))
    await ctx.info(f"Launching or attaching to {config.settings.cad_progid}")
    return await _backend(ctx).system_launch(visible, open_path)


@cad_tool(
    summary="Read AutoCAD preferences from the whitelist (OPTIONS dialog values; live only).",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Get Preferences", "readOnlyHint": True},
    tags={"environment", "system"},
)
async def system_preferences_get(
    keys: Annotated[
        list[str] | None,
        "Subset of OpenSave.SaveAsType, OpenSave.AutoSaveInterval, OpenSave.CreateBackup, "
        "OpenSave.IncrementalSavePercent, Display.CursorSize, Drafting.AutoSnapMarkerSize, "
        "Drafting.AutoSnapTooltip, Selection.PickBoxSize, Output.DefaultPlotStyleTable, "
        "Output.DefaultOutputDevice, Files.SupportPath, Files.TemplateDwgPath, "
        "Files.PrinterStyleSheetPath, Files.PrinterConfigPath; omit for all",
    ] = None,
    ctx: Context = None,
) -> dict:
    """The whitelisted `Preferences.*` values, enum values as names
    (`SaveAsType: "ac2018_dwg"`), with `read_only` naming the four `Files.*`
    paths. Refusals: headless, `capability: "preferences"` (they live in the
    running application, not in a file); a key outside the whitelist.
    Pack: settings · lean: no.
    """
    return await _backend(ctx).preferences_get(keys)


@cad_tool(
    summary="Change one whitelisted AutoCAD preference; reports old and new (live only).",
    cost="safe",
)
@mcp.tool(
    annotations={"title": "Set Preference", "readOnlyHint": False, "destructiveHint": False},
    tags={"environment", "system"},
)
async def system_preferences_set(
    key: Annotated[str, "A writable key from system_preferences_get"],
    value: Annotated[Any, "New value: bool, int within the key's range, string, or an enum name"],
    ctx: Context = None,
) -> dict:
    """Write one preference and report `old`, `new` and `changed`.

    Refusals, all before any write: a read-only key (`Files.*`), an unknown
    key, a value of the wrong type, an int outside the authored range
    (`AutoSaveInterval` 0–600 min, `CursorSize` 1–100, `PickBoxSize` 0–50, …),
    an unknown `SaveAsType` name; headless, `capability: "preferences"`.
    Pack: settings · lean: no.
    """
    await ctx.info(f"Setting preference {key} = {value!r}")
    return await _backend(ctx).preferences_set(key, value)


@cad_tool(
    summary="Ask the operator to pick a point on screen (live only; blocks until they do).",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Ask Operator: Pick Point", "readOnlyHint": True},
    tags={"environment", "system"},
)
async def user_pick_point(
    prompt: Annotated[str, "Shown on the command line, e.g. 'Pick the base point'"],
    ctx: Context = None,
) -> dict:
    """`Utility.GetPoint`: returns the WCS `x`, `y` (`z`) the operator clicks.

    ESC is an answer, not an error: `cancelled: true` with AutoCAD's reason.
    Waiting longer than `COM_CALL_TIMEOUT` returns `timed_out: true` (the
    prompt is abandoned on AutoCAD's side; press ESC there). Refusals: an
    empty prompt; headless, `capability: "interactive_prompt"` (no operator).
    Pack: settings · lean: no.
    """
    await ctx.info(f"Asking the operator to pick a point: {prompt}")
    return await _backend(ctx).user_pick_point(prompt)


@cad_tool(
    summary="Ask the operator to select one entity or a set on screen (live only).",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Ask Operator: Select", "readOnlyHint": True},
    tags={"environment", "system"},
)
async def user_select(
    prompt: Annotated[str, "Shown on the command line"],
    mode: Annotated[str, "single (GetEntity) | multiple (SelectOnScreen)"] = "single",
    ctx: Context = None,
) -> dict:
    """Returns `handles` the operator picked — one with `mode="single"` (plus
    the `picked` point `[x, y]` in **WCS** — AutoCAD reports it in the current
    UCS and the tool translates, so a UCS made current by `ucs_set` never
    shifts it), any number with `mode="multiple"` (Enter with nothing
    selected is `count: 0`, not a cancel). ESC → `cancelled: true`; a wait
    longer than `COM_CALL_TIMEOUT` → `timed_out: true`. Refusals: a mode
    outside the two, an empty prompt; headless, `capability:
    "interactive_prompt"`. Pack: settings · lean: no.
    """
    await ctx.info(f"Asking the operator to select ({mode}): {prompt}")
    return await _backend(ctx).user_select(prompt, mode)


@cad_tool(summary="Print a message on AutoCAD's command line (live only).", cost="safe")
@mcp.tool(
    annotations={"title": "Command-Line Message", "readOnlyHint": False, "destructiveHint": False},
    tags={"environment", "system"},
)
async def system_prompt_message(
    text: Annotated[str, "The message; one line is best"],
    ctx: Context = None,
) -> dict:
    """`Utility.Prompt`: tell the operator something where they are looking.
    Nothing in the drawing changes. Refusals: an empty message; headless,
    `capability: "interactive_prompt"`. Pack: settings · lean: no."""
    return await _backend(ctx).system_prompt_message(text)


@cad_tool(
    summary="Explain a system variable: type, range, default, where it is saved, which engine honours it.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Describe System Variable", "readOnlyHint": True},
    tags={"environment", "system", "settings"},
)
async def system_variable_describe(
    name: Annotated[
        str | None, "System variable name, e.g. LTSCALE (case-insensitive, '$' optional)"
    ] = None,
    search: Annotated[
        str | None, "Free text matched against names and meanings, e.g. 'decimal separator'"
    ] = None,
    ctx: Context = None,
) -> dict:
    """What an AutoCAD system variable means, from an authored catalogue of 90.

    With `name`: `{known, name, type, range|enum, default, meaning, saved_in
    (drawing|registry|not_saved), engines: {ezdxf, com}, friendly_key,
    read_only}` plus `current` when the active backend can read it (omitted,
    with `current_unavailable`, when it cannot — no document open, or a
    registry variable headlessly). An unknown name is never an error: `known:
    false` and the nearest catalogue names come back. With `search`: every row
    whose name or meaning contains all the words. With neither: the index of
    names. `friendly_key` names the `drawing_settings` key that wraps the
    variable, which is the preferred way to set it; `engines.ezdxf: false`
    means the headless engine refuses a write (registry-saved, or no header
    slot in ezdxf).
    """
    from engineering.standards.sysvars import SYSVAR_CATALOG, describe_sysvar, search_sysvars

    if name:
        row = describe_sysvar(name)
        backend = ctx.lifespan_context.get("backend") if ctx is not None else None
        if row["known"] and backend is not None:
            try:
                row["current"] = await backend.system_get_variable(row["name"])
            except Exception as exc:  # no document, or the engine cannot hold it
                row["current_unavailable"] = str(exc)
        return row
    if search:
        return {"query": search, "matches": search_sysvars(search)}
    return {
        "count": len(SYSVAR_CATALOG),
        "names": sorted(SYSVAR_CATALOG),
        "hint": "pass name=<VARIABLE> for one row or search=<words> to filter",
    }


@cad_tool(
    summary="Read the drawing's title, subject, author, keywords, comments and custom properties.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Drawing Properties (read)", "readOnlyHint": True},
    tags={"environment", "drawing", "settings"},
)
async def drawing_properties_get(ctx: Context = None) -> dict:
    """The DWGPROPS dialog as data: `summary` (title, subject, author, keywords,
    comments), `summary_available`, and `custom` ({key: value}).

    On the live engine the summary comes from the document's SummaryInfo. The
    headless engine cannot read that stream, so the five summary fields are
    `null` with `summary_available: false` — not empty strings, which would
    claim the drawing has no title. Custom properties (`$CUSTOMPROPERTYTAG` /
    `$CUSTOMPROPERTY` header pairs) are read on both engines.
    """
    return await _backend(ctx).drawing_properties_get()


@cad_tool(
    summary="Set the drawing's summary fields and add, change or delete custom properties.",
    cost="safe",
)
@mcp.tool(
    annotations={"title": "Drawing Properties (write)", "readOnlyHint": False},
    tags={"environment", "drawing", "settings"},
)
async def drawing_properties_set(
    title: Annotated[str | None, "SummaryInfo Title"] = None,
    subject: Annotated[str | None, "SummaryInfo Subject"] = None,
    author: Annotated[str | None, "SummaryInfo Author"] = None,
    keywords: Annotated[str | None, "SummaryInfo Keywords"] = None,
    comments: Annotated[str | None, "SummaryInfo Comments"] = None,
    custom: Annotated[
        dict | None,
        "Custom properties to write: {key: text}; a null value deletes the key. "
        "Keys not mentioned are left alone.",
    ] = None,
    ctx: Context = None,
) -> dict:
    """Write DWGPROPS fields. Only the arguments given are touched; returns
    `summary_written`, `custom_written` and `custom_deleted` (a key that was
    never there is not reported deleted).

    Refusals, before anything is written: a summary field on the headless
    engine (`capability: dwgprops` — SummaryInfo lives in the DWG, a DXF has
    no slot for it; custom properties still work headlessly), a non-string
    value (`TypeError` naming the field or key — nothing is coerced with
    `str()`), an empty custom key, a custom key to *write* that AutoCAD's
    AddCustomInfo would reject mid-write (measured on AutoCAD 2026: leading or
    trailing whitespace, or any of the thirteen characters
    `" * , / : ; < = > ? \\ ` |` anywhere in the key; internal spaces, tabs
    and unicode are fine — `ValueError` naming the key, on both engines; a
    delete is exempt because RemoveCustomByKey never validates syntax, so a
    key that reached the drawing another way stays removable), two keys in
    one request that AutoCAD would call the same key (its key compare is a
    simple per-character case compare: `Project`/`PROJECT` and `Grün`/`GRÜN`
    are one key, `Straße`/`STRASSE` are two), a line break in a custom key or
    value (it corrupts the DXF on save), and headlessly a document older than
    R2004 (`ValueError` — ezdxf only writes the custom-property header pairs
    for AC1018+, so they would vanish at save; save as R2004 or newer first).
    A key is matched to the drawing by that same rule on both engines and
    keeps its stored spelling when updated. On the live engine an exact
    spelling is changed with SetCustomByKey; otherwise AutoCAD decides —
    AddCustomInfo, and on its 'Duplicate key' SetCustomByKey; a delete is
    RemoveCustomByKey, and its 'Key not found' is reported as not deleted.
    """
    summary = {
        "title": title,
        "subject": subject,
        "author": author,
        "keywords": keywords,
        "comments": comments,
    }
    touched = [k for k, v in summary.items() if v is not None] + sorted(custom or {})
    await ctx.info(f"Setting drawing properties: {', '.join(touched) or 'nothing'}")
    return await _backend(ctx).drawing_properties_set(summary, custom)


# ---------------------------------------------------------------------------
# ── SECTION 21: Mechanical Parts (6 tools) ──────────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(
    summary="Draw a machine part from a segment list or an outline plus typed features.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Mechanical: Draw Part", "destructiveHint": False},
    tags={"mech", "create"},
)
async def mech_part_draw(
    spec: Annotated[
        dict,
        Field(
            description=(
                "The part: {kind: 'revolved'|'prismatic', name, material, "
                "segments:[{length, d_outer, d_inner, taper_to}] | outline:[[x,y]] + thickness, "
                "features:[{kind, id, ...}]}"
            )
        ),
    ],
    at_x: Annotated[
        float,
        Field(
            default=0.0,
            description="WCS X of the first view's lower-left corner (the rotation pivot)",
        ),
    ] = 0.0,
    at_y: Annotated[
        float,
        Field(
            default=0.0,
            description="WCS Y of the first view's lower-left corner (the rotation pivot)",
        ),
    ] = 0.0,
    rotation: Annotated[
        float,
        Field(
            default=0.0,
            description="Degrees CCW; turns every view and its dimensions about (at_x, at_y)",
        ),
    ] = 0.0,
    views: Annotated[
        list[str] | None,
        Field(
            default=None,
            description="Views to draw in order: front | side | top (default ['front'])",
        ),
    ] = None,
    projection: Annotated[
        str, Field(default="first", description="first (ISO 128-30, default) | third")
    ] = "first",
    dimension: Annotated[
        bool, Field(default=True, description="Also dimension the part (ISO 129 + ISO 286 fits)")
    ] = True,
    style: Annotated[
        str,
        Field(default="chain", description="Axial dimension style: chain | baseline | ordinate"),
    ] = "chain",
    ctx: Context = None,
) -> dict:
    """One part model - a turned profile or a plate outline plus typed features - drawn as views.

    The model is written onto the drawing as `ACADMCP_MECH` XDATA on an anchor
    POINT, so `mech_view_add` can add a section months later without the caller
    re-describing the part, and `mech_part_inspect` reads it back.

    Refused **before anything is drawn**, by path: a non-finite or non-positive
    number (`segments[2].d_outer`), a bore that is not smaller than its outside,
    an outline with fewer than three distinct vertices or one that crosses
    itself, an unknown material, a feature whose placement is off the part, two
    features that would remove the same material (both names given), a thread
    outside the transcribed ISO 261 table, a keyway outside DIN 6885's bore
    range, gear teeth whose tip diameter disagrees with their segment. A DIN 509
    undercut, a DIN 471/472 groove, a DIN 332 centre hole and an ISO 3601-2
    O-ring groove need explicit dimensions in this build: those tables are not
    transcribed and the lookup refuses by name rather than interpolate. A
    feature that cannot be projected into a requested view is listed in
    `omitted`, never dropped.
    """
    from engineering.mech.draw import draw_part
    from engineering.mech.part import build_part

    try:
        part = build_part(spec)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    await ctx.info(f"mech_part_draw: {part.name} at ({at_x}, {at_y})")
    try:
        return await draw_part(
            _backend(ctx),
            part,
            at=(at_x, at_y),
            rotation=rotation,
            views=tuple(views or ("front",)),
            projection=projection,
            dimension=dimension,
            style=style,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@cad_tool(
    summary="Add another view of a drawn part: side, top, section (full/half/offset) or detail.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Mechanical: Add View", "destructiveHint": False},
    tags={"mech", "create"},
)
async def mech_view_add(
    part_id: Annotated[
        str | None,
        Field(
            default=None,
            description="Anchor handle from mech_part_draw; omit if the drawing holds one part",
        ),
    ] = None,
    kind: Annotated[
        str, Field(default="section", description="front | side | top | section | detail")
    ] = "section",
    side: Annotated[
        str, Field(default="right", description="Which end an end view looks from: right | left")
    ] = "right",
    style: Annotated[
        str,
        Field(default="full", description="Section style: full | half | offset | revolved"),
    ] = "full",
    plane: Annotated[
        dict | None,
        Field(
            default=None,
            description="Cutting plane {p1:[x,y], p2:[x,y], label, direction:[dx,dy], via:[[x,y]]} - required for a section",
        ),
    ] = None,
    detail: Annotated[
        dict | None,
        Field(
            default=None,
            description="Detail region {center:[x,y], radius, scale, label, of:'front'} - required for a detail",
        ),
    ] = None,
    gap: Annotated[
        float, Field(default=20.0, description="Gap between this view and its parent (mm)")
    ] = 20.0,
    scale: Annotated[
        float,
        Field(
            default=1.0,
            description=(
                "Must be 1.0: views are drawn full size (a detail takes its scale in "
                "detail.scale; a sheet scale is the viewport's)"
            ),
        ),
    ] = 1.0,
    ctx: Context = None,
) -> dict:
    """Another view of a part already on the drawing, laid out on its projection axis.

    The part model comes back out of the drawing's `ACADMCP_MECH` XDATA, so the
    caller never re-describes it, and the placement follows the projection angle
    recorded when the part was drawn (first angle by default: the view from the
    right goes to the left).

    Refused by name: a section without a plane, or with a plane of zero length,
    a plane that misses the part, or one that grazes an outline vertex so its
    crossings cannot be paired; a half or offset section on the wrong kind of
    plane (the message names the style that does apply); a detail without a
    radius or with a scale of zero; an unknown part_id, and `part_id=None` on a
    drawing that holds more than one part.
    """
    from engineering.mech.draw import add_view, read_part

    backend = _backend(ctx)
    try:
        resolved = part_id or (await read_part(backend))["part_id"]
        await ctx.info(f"mech_view_add: {kind} on {resolved}")
        return await add_view(
            backend,
            resolved,
            kind,
            side=side,
            style=style,
            plane=plane,
            detail=detail,
            gap=gap,
            scale=scale,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@cad_tool(
    summary="Dimension a drawn part: ISO 129 dimensions, ISO 286 fits, a hole table.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Mechanical: Dimension Part", "destructiveHint": False},
    tags={"mech", "dimension"},
)
async def mech_dimension_part(
    part_id: Annotated[
        str | None,
        Field(
            default=None,
            description="Anchor handle from mech_part_draw; omit if the drawing holds one part",
        ),
    ] = None,
    style: Annotated[
        str,
        Field(default="chain", description="Axial dimension style: chain | baseline | ordinate"),
    ] = "chain",
    hole_table_threshold: Annotated[
        int,
        Field(default=8, description="Above this many holes the diameters move into a hole table"),
    ] = 8,
    fits: Annotated[
        dict | None,
        Field(
            default=None,
            description="ISO 286 fit per feature id, e.g. {'bearing_seat': 'k6', 'bore': 'H7'}",
        ),
    ] = None,
    views: Annotated[
        list[int] | None,
        Field(
            default=None,
            description=(
                "View indices to dimension, in mech_part_inspect order "
                "(default: every view not yet dimensioned)"
            ),
        ),
    ] = None,
    ctx: Context = None,
) -> dict:
    """Every dimension the part and its features ask for, laid out without overlaps.

    Axial lengths are chained, stacked from a baseline or measured from an
    origin; diameters stack outside the silhouette; fillets get a radius. Each
    measurement appears once (ISO 129-1): every view record carries a
    `dimensioned` flag, so after a `mech_part_draw` that dimensioned the front
    view and a `mech_view_add`, this call dimensions only the new view, and
    `views` naming a view already dimensioned is refused by name. `fits` resolves an ISO 286 code
    against the measured nominal through the repository's authored tables and
    appends the code to the dimension text; an explicit `tol` on a feature's own
    intent does the same through the ISO 129 path.

    Refused by name: a fit together with an explicit tolerance, a fit outside
    the authored ISO 286 range (over 1 mm to 500 mm, IT4-IT11), a fit on an
    intent with no measurable nominal, an unknown style, an unknown part_id, a
    call when every view is already dimensioned (the message lists them), and
    `part_id=None` on a drawing that holds more than one part. An angular intent
    is reported in `skipped` rather than drawn, because a DimIntent carries no
    vertex.
    """
    from engineering.mech.draw import dimension_part, read_part

    backend = _backend(ctx)
    try:
        resolved = part_id or (await read_part(backend))["part_id"]
        await ctx.info(f"mech_dimension_part: {resolved} ({style})")
        return await dimension_part(
            backend,
            resolved,
            style=style,
            hole_table_threshold=hole_table_threshold,
            fits=fits,
            views=views,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@cad_tool(
    summary="Draw a linear, grid or polar hole pattern with centre marks on any drawing.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Mechanical: Hole Pattern", "destructiveHint": False},
    tags={"mech", "create"},
)
async def mech_hole_pattern(
    pattern: Annotated[str, Field(description="linear | grid | polar")],
    x: Annotated[float, Field(description="WCS X of the first hole (polar: the circle centre)")],
    y: Annotated[float, Field(description="WCS Y of the first hole (polar: the circle centre)")],
    diameter: Annotated[float, Field(description="Hole diameter in drawing units")],
    count: Annotated[
        int, Field(default=2, description="Holes along the pattern (polar: around the circle)")
    ] = 2,
    spacing: Annotated[
        float, Field(default=0.0, description="Pitch between holes (linear, grid columns)")
    ] = 0.0,
    count_y: Annotated[int, Field(default=1, description="Rows, for a grid")] = 1,
    spacing_y: Annotated[float, Field(default=0.0, description="Row pitch, for a grid")] = 0.0,
    pcd: Annotated[
        float, Field(default=0.0, description="Pitch-circle diameter, for a polar pattern")
    ] = 0.0,
    start_angle: Annotated[
        float, Field(default=0.0, description="Angle of the first hole (polar), degrees CCW")
    ] = 0.0,
    angle: Annotated[
        float, Field(default=0.0, description="Direction of a linear pattern, degrees CCW")
    ] = 0.0,
    layer: Annotated[
        str | None, Field(default=None, description="Override the GEOMETRY layer for the circles")
    ] = None,
    centre_marks: Annotated[
        bool, Field(default=True, description="Draw ISO 128-23 centre marks")
    ] = True,
    dimension: Annotated[
        bool, Field(default=False, description="Add a '6x ⌀8' diameter callout")
    ] = False,
    ctx: Context = None,
) -> dict:
    """A hole pattern on any drawing - the part model is not required.

    The same expansion `HolePattern` performs inside a part, exposed for a
    drawing that was not built from the model.

    Refused before anything is drawn: an unknown pattern, a count below 1 (below
    2 for polar), a linear pattern with no spacing, a grid with more than one
    column or row and no pitch for it, a polar pattern with no pitch-circle
    diameter, and a non-finite or non-positive diameter.
    """
    from engineering.mech.draw import draw_hole_pattern

    await ctx.info(f"mech_hole_pattern: {pattern} x{count}")
    try:
        return await draw_hole_pattern(
            _backend(ctx),
            pattern=pattern,
            x=x,
            y=y,
            diameter=diameter,
            count=count,
            spacing=spacing,
            count_y=count_y,
            spacing_y=spacing_y,
            pcd=pcd,
            start_angle=start_angle,
            angle=angle,
            layer=layer,
            centre_marks=centre_marks,
            dimension=dimension,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@cad_tool(
    summary="Draw a whole mechanical sheet - several parts and their views - in one transaction.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Mechanical: Sheet From Spec", "destructiveHint": False},
    tags={"mech", "create"},
)
async def mech_part_from_spec(
    spec: Annotated[
        dict,
        Field(
            description=(
                "{sheet: {intent, sheet_size, scale, layer_set}, "
                "parts: [{part: {...}, at: [x, y], rotation, views: ['front','side'], "
                "projection, dimension, style}]}"
            )
        ),
    ],
    ctx: Context = None,
) -> dict:
    """Several parts, their views and their dimensions, drawn inside one transaction.

    Every part is validated before the transaction opens, so a refusal leaves
    the drawing untouched and names the offending path (`parts[1]:
    segments[0].d_outer: ...`). If a draw fails or the request is cancelled
    part-way, the transaction is rolled back under an anyio shield, so a
    cancelled call cannot leave half a sheet inside an open transaction.

    Refused by name: an empty `parts` list, any part refusal from
    `mech_part_draw`, and a transaction that cannot be opened.
    """
    from engineering.mech.draw import draw_from_spec

    await ctx.info(f"mech_part_from_spec: {len((spec or {}).get('parts') or [])} part(s)")
    try:
        return await draw_from_spec(_backend(ctx), spec)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@cad_tool(
    summary="Read the part model back off the drawing: segments, features, views, omissions.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Mechanical: Inspect Part", "readOnlyHint": True},
    tags={"mech", "query"},
)
async def mech_part_inspect(
    part_id: Annotated[
        str | None,
        Field(
            default=None,
            description="Anchor handle from mech_part_draw; omit if the drawing holds one part",
        ),
    ] = None,
    ctx: Context = None,
) -> dict:
    """The `ACADMCP_MECH` payload a drawn part carries, decoded.

    Never modifies the drawing. Returns the part model exactly as
    `mech_part_draw` accepts it (`part`), the views drawn so far with their
    placements, and the payload version.

    Refused by name: an unknown part_id, a handle that carries no
    `ACADMCP_MECH` payload, a corrupt or foreign payload, and `part_id=None` on
    a drawing that holds no part or more than one (the message lists the
    handles).
    """
    from engineering.mech.draw import read_part

    try:
        return await read_part(_backend(ctx), part_id)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


# ---------------------------------------------------------------------------
# ── SECTION 22: Standard Parts (3 tools) ────────────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(
    summary="Search the standard-parts catalogue: ISO bolts, nuts, washers, cap screws, bearings.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Standard Parts: Catalogue", "readOnlyHint": True},
    tags={"mech", "query"},
)
async def std_part_list(
    query: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Free text matched against designation, standard, size and family - "
                "'M12', 'ISO 4014', '6205', 'washer'"
            ),
        ),
    ] = None,
    standard: Annotated[
        str | None,
        Field(
            default=None,
            description="One of ISO 4014, ISO 4017, ISO 4032, ISO 7089, ISO 4762, ISO 15",
        ),
    ] = None,
    ctx: Context = None,
) -> dict:
    """Catalogue rows with the dimensions a drawing needs, and their provenance.

    Every row is transcribed from the named standard's own table and carries
    its `source`. Coverage is deliberately narrow: ISO 261 first-choice sizes
    only (M5-M36, M3-M24 for ISO 4762) and ISO 15 bore numbers 00-10 in the
    600x / 620x / 630x series. A size outside a table is refused by name by
    `std_part_insert` with the nearest entries, never interpolated. An unknown
    `standard` is refused with the list of catalogue standards.
    """
    from engineering.mech.stdparts import FAMILY_STANDARDS, catalogue

    rows = catalogue(query, standard)
    return {"count": len(rows), "standards": list(FAMILY_STANDARDS), "parts": list(rows)}


@cad_tool(
    summary=(
        "Insert an ISO standard part - hex bolt, nut, washer, cap screw or ball bearing "
        "- as a tagged block."
    ),
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Standard Parts: Insert", "readOnlyHint": False},
    tags={"mech", "create"},
)
async def std_part_insert(
    designation: Annotated[
        str,
        Field(
            description=(
                "'ISO 4014 - M12x60', 'ISO 4032 - M12', 'ISO 7089 - M12', "
                "'ISO 4762 - M8x30', 'ISO 15 - 6205' (a bare '6205' also works)"
            )
        ),
    ],
    x: Annotated[float, Field(description="Insertion X (WCS)")],
    y: Annotated[float, Field(description="Insertion Y (WCS)")],
    view: Annotated[
        str,
        Field(
            default="side",
            description=(
                "Fasteners: side | top. Bearings: simplified (ISO 8826-1) | detailed "
                "(ISO 8826-2). The default 'side' falls back to the family's first view."
            ),
        ),
    ] = "side",
    rotation: Annotated[
        float,
        Field(default=0.0, description="Degrees CCW; 0 puts the part's axis along +X"),
    ] = 0.0,
    layer: Annotated[
        str,
        Field(
            default="GEOMETRY",
            description=(
                "Layer for the INSERT; the block's own primitives keep GEOMETRY / CENTER / HIDDEN"
            ),
        ),
    ] = "GEOMETRY",
    material: Annotated[
        str | None,
        Field(
            default=None,
            description="Property class or material for the MAT attribute and the XDATA, e.g. '8.8'",
        ),
    ] = None,
    ctx: Context = None,
) -> dict:
    """Define the part's block once, insert it, and write its `std_part` XDATA.

    The block carries DESIG / STD / SIZE / MAT attributes and an ACADMCP_MECH
    payload, so a parts list is a read of the drawing rather than a naming
    convention. Refusals, all before any write: a designation outside the
    transcribed tables (named, with the nearest catalogue entries - nothing is
    drawn approximately), a malformed designation, a missing or stray nominal
    length, a view the family does not have, an attribute tag the block does
    not carry, and a non-finite coordinate or rotation.
    """
    from engineering.mech.stdparts import insert_std_part

    await ctx.info(f"standard part {designation} ({view}) at ({x}, {y})")
    return await insert_std_part(
        _backend(ctx),
        designation,
        at=(x, y),
        view=view,
        rotation=rotation,
        layer=layer,
        material=material,
    )


@cad_tool(
    summary=(
        "Draw a standard feature - thread, undercut, ring or O-ring groove, centre hole "
        "- onto existing geometry."
    ),
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Standard Parts: Draw Feature", "readOnlyHint": False},
    tags={"mech", "create"},
)
async def std_feature_draw(
    kind: Annotated[
        str,
        Field(description="thread | undercut | ring_groove | centre_hole | oring_groove"),
    ],
    x: Annotated[float, Field(description="Feature origin X (WCS): a point on the axis")],
    y: Annotated[float, Field(description="Feature origin Y (WCS): a point on the axis")],
    params: Annotated[
        dict,
        Field(
            description=(
                "thread: {d, length, size|pitch, internal} (a size must name d's own "
                "thread - 'M20' with d=8 is refused, not drawn); "
                "undercut: {d, form: E|F}; "
                "ring_groove: {d, kind: shaft|bore}; centre_hole: {size, form: A|B}; "
                "oring_groove: {d, cord, kind: shaft|bore}"
            )
        ),
    ],
    rotation: Annotated[
        float,
        Field(
            default=0.0,
            description="Degrees CCW applied to the local +X (along the axis, into the feature)",
        ),
    ] = 0.0,
    layer: Annotated[
        str | None,
        Field(
            default=None,
            description="Override the visible primitives' layer; CENTER and HIDDEN keep theirs",
        ),
    ] = None,
    ctx: Context = None,
) -> dict:
    """Add a standard feature to geometry that already exists - a foreign drawing being finished.

    Local frame: `(x, y)` is on the axis, +X runs along the axis in the
    direction the feature extends and +Y is radially outward. The thread is the
    ISO 6410 representation (minor diameter at 0.8 x major, thread-length limit
    to the major) and its pitch comes from ISO 261 via `size` or from an
    explicit `pitch` - it is never guessed. Every other kind takes its
    dimensions from the registered standards table and is refused by name when
    that table is absent, when the size is outside its coverage, or when the row
    lacks a dimension the feature needs: no value is interpolated and no profile
    is reconstructed. DIN 332 form R is refused - only forms A and B are drawn.
    """
    from engineering.mech.stdparts import draw_std_feature

    await ctx.info(f"standard feature {kind} at ({x}, {y})")
    return await draw_std_feature(
        _backend(ctx), kind, (x, y), params, rotation=rotation, layer=layer
    )


# ---------------------------------------------------------------------------
# ── SECTION 23: Mechanical Annotation (5 tools) ─────────────────────────────
# ---------------------------------------------------------------------------
# ISO annotation symbols composed from LINE / ARC / CIRCLE / LWPOLYLINE / TEXT,
# so the same symbol lands on COM and ezdxf. The standards tables live in
# engineering/mech/annotate.py with their sources; a value outside a
# transcribed table is refused there, before the first entity is written.


@cad_tool(
    summary="Draw an ISO 21920-1 surface-texture symbol: Ra/Rz, process, lay, allowance.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Surface Texture Symbol (ISO 21920-1)", "destructiveHint": False},
    tags={"engineering", "mech"},
)
async def surface_texture(
    x: Annotated[float, "Symbol apex X (WCS) — the point the leader attaches to."],
    y: Annotated[float, "Symbol apex Y (WCS)."],
    ra: Annotated[
        float | None, Field(default=None, description="Ra value in µm, written as 'Ra 3.2'.")
    ] = None,
    rz: Annotated[
        float | None, Field(default=None, description="Rz value in µm, written as 'Rz 12.5'.")
    ] = None,
    machining: Annotated[
        str,
        Field(
            default="any",
            description=(
                "any (basic vee) | required (bar across the vee, material removal "
                "required) | prohibited (circle in the vee, material removal not allowed)."
            ),
        ),
    ] = "any",
    process: Annotated[
        str | None, "Manufacturing method, treatment or coating, e.g. 'milled'."
    ] = None,
    lay: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Direction of lay: parallel, perpendicular, crossed, multidirectional, "
                "circular, radial, particulate."
            ),
        ),
    ] = None,
    allowance: Annotated[
        float | None, "Machining allowance in mm, written below the extension line."
    ] = None,
    all_around: Annotated[bool, "Circle at the kink: the requirement applies all around."] = False,
    leader_to: Annotated[
        list[float] | None, "[x, y] on the surface; draws a leader with an arrowhead there."
    ] = None,
    height: Annotated[
        float,
        Field(
            default=3.5,
            gt=0,
            description="Text height (mm). ISO 1302 tabulates 2.5, 3.5, 5, 7, 10, 14, 20.",
        ),
    ] = 3.5,
    rotation: Annotated[float, "Rotate the whole symbol about its apex (degrees CCW)."] = 0.0,
    standard: Annotated[
        str, "ISO 21920-1 (default) or ISO 1302 for drawings issued under the older designation."
    ] = "ISO 21920-1",
    layer: Annotated[
        str | None, "Override the role layers (DIM for geometry, TEXT for text)."
    ] = None,
    ctx: Context = None,
) -> dict:
    """Draw the 60° surface-texture vee with its annotation in the standard positions.

    Refusals, all before the first entity reaches the drawing: a text height
    outside the seven ISO 1302 rows (2.5/3.5/5/7/10/14/20 mm — never
    interpolated), a `lay` that is not one of the seven ISO 1302 directions of
    lay, a `machining` outside any/required/prohibited, and a `standard` other
    than ISO 21920-1 or ISO 1302.
    """
    from engineering.mech.annotate import draw_surface_texture

    await ctx.debug(f"Surface texture symbol at ({x}, {y})")
    return await draw_surface_texture(
        _backend(ctx),
        at=(x, y),
        leader_to=tuple(leader_to) if leader_to else None,
        ra=ra,
        rz=rz,
        process=process,
        lay=lay,
        machining=machining,
        all_around=all_around,
        allowance=allowance,
        height=height,
        standard=standard,
        rotation=rotation,
        layer=layer,
    )


@cad_tool(
    summary="Draw an ISO 2553 weld symbol: reference line, identification line, size and pitch.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Weld Symbol (ISO 2553)", "destructiveHint": False},
    tags={"engineering", "mech"},
)
async def weld_symbol(
    x: Annotated[float, "Kink X (WCS) — where the arrow line meets the reference line."],
    y: Annotated[float, "Kink Y (WCS)."],
    kind: Annotated[str, "Elementary symbol: square | v | bevel | u | j | fillet."] = "fillet",
    size: Annotated[
        float | str | None,
        Field(
            default=None,
            description=(
                "Written before the symbol. A number on a fillet takes the ISO 2553 "
                "design-throat prefix (5 → 'a5'); pass a string for leg length ('z7')."
            ),
        ),
    ] = None,
    length: Annotated[float | None, "Weld length (mm), written after the symbol."] = None,
    pitch: Annotated[
        float | None, "Pitch (mm) of an intermittent weld, written in brackets after the length."
    ] = None,
    side: Annotated[
        str,
        Field(
            default="arrow",
            description=(
                "arrow (symbol on the reference line) | other (on the dashed "
                "identification line) | both (symmetrical; the identification line is "
                "omitted, per ISO 2553:2019)."
            ),
        ),
    ] = "arrow",
    field_weld: Annotated[bool, "Flag at the kink: weld made on site."] = False,
    all_around: Annotated[bool, "Circle at the kink: weld all around."] = False,
    process: Annotated[str | None, "Tail reference, e.g. an ISO 4063 process number."] = None,
    leader_to: Annotated[
        list[float] | None, "[x, y] on the joint; draws a leader with an arrowhead there."
    ] = None,
    height: Annotated[float, Field(default=3.5, gt=0, description="Text height (mm).")] = 3.5,
    rotation: Annotated[float, "Rotate the whole annotation about the kink (degrees CCW)."] = 0.0,
    layer: Annotated[
        str | None, "Override the role layers (DIM for geometry, TEXT for text)."
    ] = None,
    ctx: Context = None,
) -> dict:
    """Draw an ISO 2553 weld annotation: reference line, identification line, symbol, dimensions.

    Refusals, all before the first entity reaches the drawing: a `kind` outside
    the six elementary symbols transcribed here (square, v, bevel, u, j,
    fillet — the rest of the ISO 2553 table is deliberately not shipped rather
    than guessed), a `side` outside arrow/other/both, a non-positive `height`,
    and a `pitch` given without a `length`.
    """
    from engineering.mech.annotate import draw_weld_symbol

    await ctx.debug(f"Weld symbol {kind} at ({x}, {y})")
    return await draw_weld_symbol(
        _backend(ctx),
        at=(x, y),
        kind=kind,
        leader_to=tuple(leader_to) if leader_to else None,
        size=size,
        length=length,
        pitch=pitch,
        side=side,
        field_weld=field_weld,
        all_around=all_around,
        process=process,
        height=height,
        rotation=rotation,
        layer=layer,
    )


@cad_tool(
    summary="Draw ISO 128-23 centre marks or centre lines on circles, read from the drawing.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Centre Marks (ISO 128-23)", "destructiveHint": False},
    tags={"engineering", "mech"},
)
async def centre_marks(
    handles: Annotated[
        list[str] | None,
        "Handles of CIRCLEs/ARCs; their real centre and radius are read from the drawing.",
    ] = None,
    centers: Annotated[
        list[list[float]] | None,
        "Explicit centres as [x, y] or [x, y, radius]; style='lines' needs the radius.",
    ] = None,
    style: Annotated[
        str,
        Field(
            default="mark",
            description="mark (short cross at the centre) | lines (full centre lines).",
        ),
    ] = "mark",
    extension: Annotated[
        float,
        Field(
            default=3.0,
            gt=0,
            description="Arm length (mark) or overrun past the outline (lines), mm.",
        ),
    ] = 3.0,
    layer: Annotated[str | None, "Override the CENTER role layer."] = None,
    ctx: Context = None,
) -> dict:
    """Centre marks on the CENTER layer, whose linetype carries ISO 128-23's dash pattern.

    Pass `handles` and the radii are read back with `entity_get` rather than
    restated — a radius from memory is the classic silent wrong number.
    Refusals, all before the first entity is written: neither `handles` nor
    `centers` given (or both), a handle that is not a CIRCLE or ARC, a circle
    in a plane tilted out of WCS XY (capability `ocs_tilted_plane`, so it has
    no radius in this frame), a `style` outside mark/lines, a non-positive
    `extension`, and `style='lines'` on a centre given without a radius.
    """
    from engineering.mech.marks import draw_centre_marks

    await ctx.debug(f"Centre marks style={style}")
    return await draw_centre_marks(
        _backend(ctx),
        handles=handles,
        centers=centers,
        style=style,
        extension=extension,
        layer=layer,
    )


@cad_tool(
    summary="Draw an ISO 128-40 cutting-plane line and return the plane the section view uses.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Section Line (ISO 128-40)", "destructiveHint": False},
    tags={"engineering", "mech"},
)
async def section_line(
    x1: Annotated[float, "Cutting plane start X (WCS)."],
    y1: Annotated[float, "Cutting plane start Y (WCS)."],
    x2: Annotated[float, "Cutting plane end X (WCS)."],
    y2: Annotated[float, "Cutting plane end Y (WCS)."],
    label: Annotated[str, "Capital letter drawn at both ends, e.g. 'A'."] = "A",
    direction: Annotated[
        list[float] | None,
        "Viewing direction as [dx, dy]; must not be parallel to the cutting plane.",
    ] = None,
    style: Annotated[
        str, "full | half | offset | revolved — carried into the returned plane."
    ] = "full",
    height: Annotated[float, Field(default=5.0, gt=0, description="Label height (mm).")] = 5.0,
    layer: Annotated[str | None, "Override the role layers (GEOMETRY/CENTER/TEXT)."] = None,
    ctx: Context = None,
) -> dict:
    """Draw the cutting-plane line and hand back the plane a section view consumes.

    Wide segments at the ends on GEOMETRY, the thin long-dash-dot middle on
    CENTER, an arrow at each end whose head sits on the line end and points the
    way the section is viewed, and the same letter beyond both arrows.
    `payload["plane"]` is exactly `{p1, p2, label, direction, style}` — feed it
    to the section view so the label on the plan and the label on the view
    cannot disagree. Refusals, before any entity is written: `p1 == p2`, a zero
    or parallel viewing direction, an empty `label`, a `style` outside
    full/half/offset/revolved, and a non-positive `height`.
    """
    from engineering.mech.marks import draw_section_line

    await ctx.debug(f"Section line {label} ({x1},{y1})-({x2},{y2})")
    return await draw_section_line(
        _backend(ctx),
        (x1, y1),
        (x2, y2),
        label=label,
        direction=tuple(direction) if direction else (0.0, -1.0),
        style=style,
        height=height,
        layer=layer,
    )


@cad_tool(
    summary="Hatch a region with the ISO 128-50 section pattern for a named material.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Material Hatch (ISO 128-50)", "destructiveHint": False},
    tags={"engineering", "mech"},
)
async def hatch_material(
    material: Annotated[str, "Material name; an unknown one is refused with the list."] = "steel",
    boundary: Annotated[
        list[list[float]] | None, "Closed boundary as [[x, y], ...] — at least three points."
    ] = None,
    handles: Annotated[
        list[str] | None, "Entities to chain into one closed loop instead of a boundary."
    ] = None,
    scale: Annotated[
        float,
        Field(default=1.0, gt=0, description="Multiplies the material's own pattern scale."),
    ] = 1.0,
    angle: Annotated[
        float | None, "Replaces the material's own pattern angle (degrees) when given."
    ] = None,
    layer: Annotated[str | None, "Override the HATCH role layer."] = None,
    ctx: Context = None,
) -> dict:
    """Section-hatch a cut face with the pattern ISO 128-50 gives its material.

    Refusals: an unknown material (named, with the full list, before anything
    is drawn), neither `boundary` nor `handles` (or both), a boundary with
    fewer than three points, and a non-positive `scale`. With `handles` the
    loop is chained by `boundary_from_entities`, which the COM engine refuses
    (capability `boundary_trace`) - pass `boundary` on a live seat, or run
    headlessly. The HATCH layer is created first when the drawing lacks it:
    ActiveX refuses an entity on an absent layer and would otherwise leave the
    hatch orphaned on layer 0.

    The payload states its own accuracy, as `analysis_measure_entity` does:
    `area` is the polygon actually hatched, `boundary_area` the exact area of
    the chained loop, and `accuracy` is `"exact"` or `"flatten_tolerance"` -
    an arc edge reaches `entity_create_hatch` as chords, so a curved cut face
    is hatched to within `HATCH_FLATTEN_SAGITTA` of itself rather than being
    replaced by its chord polygon. With `handles`, the outline
    `boundary_from_entities` draws is scaffolding and is deleted; `traced`
    reports its handle and whether the delete succeeded. A loop enclosing no
    area is refused instead of writing a HATCH of area 0.0.
    """
    from engineering.mech.marks import draw_material_hatch

    await ctx.debug(f"Material hatch {material}")
    return await draw_material_hatch(
        _backend(ctx),
        boundary=boundary,
        handles=handles,
        material=material,
        scale=scale,
        angle=angle,
        layer=layer,
    )


# ---------------------------------------------------------------------------
# ── SECTION 24: Sheet & Delivery (11 tools) ─────────────────────────────────
# ---------------------------------------------------------------------------


@cad_tool(
    summary="Draw an ISO 5457 sheet frame with the zone grid and centring marks.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Sheet: ISO 5457 Frame", "destructiveHint": False},
    tags={"engineering", "sheet"},
)
async def sheet_frame(
    size: Annotated[
        str, Field(default="A3", description="ISO 216 sheet: A4, A3, A2, A1 or A0.")
    ] = "A3",
    orientation: Annotated[
        str, Field(default="landscape", description="'landscape' or 'portrait'.")
    ] = "landscape",
    zones: Annotated[
        bool,
        Field(
            default=True,
            description="Draw the ISO 5457 grid reference system: letters down the vertical "
            "edges from the top, numbers along the horizontal edges from the left.",
        ),
    ] = True,
    marks: Annotated[
        bool,
        Field(
            default=True,
            description="Draw the four centring marks and the eight corner trimming rectangles.",
        ),
    ] = True,
    origin_x: Annotated[
        float, Field(default=0.0, description="X of the sheet's lower-left corner (WCS).")
    ] = 0.0,
    origin_y: Annotated[
        float, Field(default=0.0, description="Y of the sheet's lower-left corner (WCS).")
    ] = 0.0,
    layout: Annotated[
        str,
        Field(
            default="",
            description="Paper-space layout to draw the sheet on (create it with "
            "layout_create). Empty draws in the current space.",
        ),
    ] = "",
    ctx: Context = None,
) -> dict:
    """ISO 5457 drawing frame for A4-A0: a 20 mm filing margin, 10 mm on the
    other three edges, the grid reference system and the centring/trimming marks.

    Borders, rules and marks land on TITLEBLOCK; the zone lettering on TEXT.
    Refusals, all before the first entity is drawn: a size outside A4-A0 is
    refused by name and the five covered sizes are listed; an orientation that
    is neither 'landscape' nor 'portrait'; a `layout` that does not exist. The
    zone divisions drawn are equal -- ISO 5457's shorter corner fields are not
    implemented -- while the division counts are the standard's.
    """
    from engineering.sheet.frames import draw_sheet_frame

    await ctx.info(f"Sheet frame {size} {orientation}")
    return await draw_sheet_frame(
        _backend(ctx),
        size,
        orientation=orientation,
        zones=zones,
        marks=marks,
        origin=(origin_x, origin_y),
        layout=layout or None,
    )


@cad_tool(
    summary="Stamp an ISO 7200 title block (and frame) on any sheet from A4 to A0.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Sheet: ISO 7200 Title Block", "destructiveHint": False},
    tags={"engineering", "sheet"},
)
async def titleblock_apply(
    title: Annotated[str, Field(description="Drawing title (verbatim, no transformation).")],
    drawing_no: Annotated[str, Field(description="Identification number, e.g. 'AM-2026-001'.")],
    size: Annotated[
        str, Field(default="A3", description="ISO 216 sheet: A4, A3, A2, A1 or A0.")
    ] = "A3",
    orientation: Annotated[
        str, Field(default="landscape", description="'landscape' or 'portrait'.")
    ] = "landscape",
    projection: Annotated[
        str,
        Field(
            default="first",
            description="Projection-angle symbol: 'first' (ISO/European, the default), "
            "'third' (ANSI), or '' for no symbol.",
        ),
    ] = "first",
    part_no: Annotated[str, Field(default="")] = "",
    material: Annotated[str, Field(default="")] = "",
    scale: Annotated[str, Field(default="1:1")] = "1:1",
    units: Annotated[str, Field(default="mm")] = "mm",
    drawn_by: Annotated[str, Field(default="")] = "",
    checked_by: Annotated[str, Field(default="")] = "",
    date: Annotated[str, Field(default="")] = "",
    sheet: Annotated[str, Field(default="1/1")] = "1/1",
    revision: Annotated[str, Field(default="A")] = "A",
    company: Annotated[str, Field(default="Anka-Makine")] = "Anka-Makine",
    frame: Annotated[
        bool, Field(default=True, description="Also draw the ISO 5457 frame around the block.")
    ] = True,
    zones: Annotated[
        bool, Field(default=False, description="Add the zone grid to that frame.")
    ] = False,
    marks: Annotated[
        bool, Field(default=False, description="Add the centring and trimming marks.")
    ] = False,
    origin_x: Annotated[float, Field(default=0.0)] = 0.0,
    origin_y: Annotated[float, Field(default=0.0)] = 0.0,
    layout: Annotated[
        str,
        Field(
            default="",
            description="Paper-space layout to draw the sheet on; empty draws in the "
            "current space.",
        ),
    ] = "",
    ctx: Context = None,
) -> dict:
    """ISO 7200 title block, 180 mm wide, in the lower-right corner of any
    A4-A0 sheet. The title text is used verbatim.

    Carries the ISO 128-30 projection-angle symbol: 'first' draws the cone on
    the left and the concentric circles on the right, 'third' is its mirror.
    Refusals, all before the first entity: an unknown size (the five covered
    sizes are listed), an unknown orientation, a projection that is not
    'first'/'third'/'', a `layout` that does not exist. `titleblock_apply_iso_a3`
    is this tool with size='A3' and no symbol, kept so no caller breaks.
    """
    from engineering.sheet.titleblock import TitleBlockMetadata, apply_titleblock

    await ctx.info(f"Title block {size} {orientation}: {title}")
    metadata = TitleBlockMetadata(
        title=title,
        drawing_no=drawing_no,
        part_no=part_no,
        material=material,
        scale=scale,
        units=units,
        drawn_by=drawn_by,
        checked_by=checked_by,
        date=date,
        sheet=sheet,
        revision=revision,
        company=company,
    )
    return await apply_titleblock(
        _backend(ctx),
        size=size,
        metadata=metadata,
        origin=(origin_x, origin_y),
        orientation=orientation,
        projection=projection or None,
        frame=frame,
        zones=zones,
        marks=marks,
        layout=layout or None,
    )


@cad_tool(
    summary="Append a revision row, with optional revision clouds and triangular tags.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Sheet: Add Revision", "destructiveHint": False},
    tags={"engineering", "sheet"},
)
async def revision_add(
    rev: Annotated[
        str, Field(description="Revision code: one to three capitals or digits (A, B, 01).")
    ],
    description: Annotated[str, Field(description="What changed, verbatim.")],
    date: Annotated[str, Field(default="", description="Issue date, verbatim.")] = "",
    by: Annotated[str, Field(default="", description="Who issued it.")] = "",
    clouds: Annotated[
        list[list[float]] | None,
        Field(
            default=None,
            description="Regions to cloud, each [x0, y0, x1, y1] in WCS. Refused before "
            "anything is written on an engine without the revcloud capability.",
        ),
    ] = None,
    tags: Annotated[
        list[list[float]] | None,
        Field(default=None, description="Points [x, y] to put a triangular revision tag at."),
    ] = None,
    size: Annotated[
        str, Field(default="A3", description="Sheet the block is placed on: A4-A0.")
    ] = "A3",
    orientation: Annotated[str, Field(default="landscape")] = "landscape",
    origin_x: Annotated[float, Field(default=0.0)] = 0.0,
    origin_y: Annotated[float, Field(default=0.0)] = 0.0,
    cloud_segment: Annotated[
        float, Field(default=8.0, gt=0, description="Revision-cloud arc segment length (mm).")
    ] = 8.0,
    layout: Annotated[str, Field(default="")] = "",
    ctx: Context = None,
) -> dict:
    """Append one row to the revision block, creating the block on first use.

    Idempotent per revision code: a code the drawing already carries returns
    `created=false` and draws nothing, so re-running a revision pass cannot
    stack duplicates. The block defaults to the upper-right corner of the
    drawing frame, growing downwards, so it does not collide with the ISO 7573
    parts list above the title block. Refusals, all before the first entity: a
    malformed code, a cloud region that is not [x0, y0, x1, y1], and any
    request for clouds on a backend whose `revcloud` capability is false (the
    COM engine — REVCLOUD has no ActiveX member) — that one carries
    `capability: "revcloud"`.
    """
    from engineering.sheet.revision import add_revision

    await ctx.info(f"Revision {rev}: {description}")
    return await add_revision(
        _backend(ctx),
        rev=rev,
        description=description,
        date=date,
        by=by,
        clouds=clouds or (),
        tags=tags or (),
        size=size,
        orientation=orientation,
        origin=(origin_x, origin_y),
        cloud_segment=cloud_segment,
        layout=layout or None,
    )


@cad_tool(
    summary="Read the bill of materials out of the drawing's block references.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Sheet: Extract Bill of Materials", "readOnlyHint": True},
    tags={"engineering", "sheet", "query"},
)
async def bom_extract(
    layer: Annotated[
        str, Field(default="", description="Only read INSERTs on this layer; empty reads all.")
    ] = "",
    group_by: Annotated[
        str,
        Field(
            default="designation",
            description="Field to group identical parts by: designation, standard, material, "
            "or '' for one row per insert.",
        ),
    ] = "designation",
    columns: Annotated[
        list[str] | None,
        Field(
            default=None,
            description="Columns to return; default item, qty, designation, standard, material.",
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            default=0,
            ge=0,
            description="Stop after this many item records; 0 (the default) reads the whole "
            "drawing. `truncated` says whether the cap cut the list.",
        ),
    ] = 0,
    ctx: Context = None,
) -> dict:
    """Walk the INSERTs and return the ISO 7573 item rows. Never modifies the
    drawing. `bom_table` is what draws them.

    Each row's source is reported: `xdata` when the ACADMCP_MECH payload
    supplied it (what `std_part_insert` writes), `attributes` when the block's
    own ATTRIBs did. A block with neither a payload nor a DESIGNATION attribute
    is skipped rather than guessed at, and `skipped` counts them.

    The whole drawing is read by default — `entity_list`'s 200-entity page is
    paged through, not taken as a cap, because an item list that stops at the
    200th INSERT prints a wrong QTY rather than a shorter one. Pass `limit` to
    cap it deliberately; `truncated` is then true if and only if the cap cut
    the list. Refusals: an unknown column or group_by field, named with the
    list of the ones there are.
    """
    from engineering.sheet.bom import extract_records, rows_from_records

    backend = _backend(ctx)
    cap = int(limit) or None
    # One record past the cap, so `truncated` is measured rather than guessed:
    # exactly `cap` records on the drawing is not a truncation.
    records = await extract_records(
        backend, layer=layer or None, limit=None if cap is None else cap + 1
    )
    truncated = cap is not None and len(records) > cap
    if truncated:
        records = records[:cap]
    rows = rows_from_records(records, columns=columns, group_by=group_by or None)
    return {
        "ok": True,
        "standard": "ISO 7573",
        "rows": [dict(row) | {"handles": list(row["handles"])} for row in rows],
        "records": len(records),
        "limit": cap,
        "truncated": truncated,
        "group_by": group_by or None,
    }


@cad_tool(summary="Draw the parts list as a real TABLE above the title block.", cost="mutate")
@mcp.tool(
    annotations={"title": "Sheet: Draw Parts List", "destructiveHint": False},
    tags={"engineering", "sheet"},
)
async def bom_table(
    rows: Annotated[
        list[dict] | None,
        Field(
            default=None,
            description="Rows to draw; omit to extract them from the drawing first.",
        ),
    ] = None,
    size: Annotated[str, Field(default="A3", description="Sheet the list is placed on.")] = "A3",
    orientation: Annotated[str, Field(default="landscape")] = "landscape",
    origin_x: Annotated[float, Field(default=0.0)] = 0.0,
    origin_y: Annotated[float, Field(default=0.0)] = 0.0,
    at_x: Annotated[
        float | None,
        Field(default=None, description="Override the anchor X; default is the title block's."),
    ] = None,
    at_y: Annotated[float | None, Field(default=None)] = None,
    direction: Annotated[
        str,
        Field(
            default="up",
            description="'up' (ISO 7573 on a drawing: heading at the bottom, items ascending) "
            "or 'down'.",
        ),
    ] = "up",
    columns: Annotated[list[str] | None, Field(default=None)] = None,
    row_height: Annotated[float, Field(default=7.0, gt=0)] = 7.0,
    layout: Annotated[str, Field(default="")] = "",
    ctx: Context = None,
) -> dict:
    """Draw the ISO 7573 item list as a real TABLE entity, 180 mm wide (the
    title-block width), growing upwards from directly above the title block.

    With `direction="up"` the heading row sits against the title block and item
    1 is the row above it, so the list extends upwards as parts are added —
    which is what ISO 7573 asks for on a drawing. Pass `rows` to draw a list
    you already have, or omit it and the tool runs `bom_extract` first.
    `representation` reports what was drawn: `native` (a real ACAD_TABLE on the
    live engine) or `composite` (rules and MTEXT headlessly, whose `handle` is
    the first child). Refusals: no rows to draw, an unknown column, an unknown
    direction, a `layout` that does not exist.
    """
    from engineering.sheet.bom import (
        draw_bom_table,
        extract_records,
        rows_from_records,
    )
    from engineering.sheet.titleblock import TB_HEIGHT, titleblock_origin

    backend = _backend(ctx)
    if rows is None:
        rows = list(rows_from_records(await extract_records(backend), columns=columns))
    tb_x, tb_y = titleblock_origin(size, orientation=orientation)
    anchor = (
        origin_x + tb_x if at_x is None else at_x,
        origin_y + tb_y + TB_HEIGHT if at_y is None else at_y,
    )
    await ctx.info(f"Parts list: {len(rows)} rows at {anchor}")
    return await draw_bom_table(
        backend,
        rows,
        at=anchor,
        direction=direction,
        columns=columns,
        row_height=row_height,
        layout=layout or None,
    )


@cad_tool(summary="Add an ISO 6433 balloon linked to its item row.", cost="mutate")
@mcp.tool(
    annotations={"title": "Sheet: Add Balloon", "destructiveHint": False},
    tags={"engineering", "sheet"},
)
async def balloon_add(
    item: Annotated[
        int, Field(ge=1, description="Item reference number, as bom_extract numbers it.")
    ],
    x: Annotated[float, Field(description="Balloon centre X (WCS).")],
    y: Annotated[float, Field(description="Balloon centre Y (WCS).")],
    leader_x: Annotated[float, Field(description="X of the dot on the item.")],
    leader_y: Annotated[float, Field(description="Y of the dot on the item.")],
    targets: Annotated[
        list[str] | None,
        Field(
            default=None,
            description="Handles of the INSERTs this item is; the link a rerun renumbers by.",
        ),
    ] = None,
    radius: Annotated[
        float,
        Field(
            default=4.0,
            gt=0,
            description="Balloon radius (mm). The numeral is 1.4x this, so a sheet "
            "dimensioned at 3.5 mm wants 5.0 to satisfy ISO 6433.",
        ),
    ] = 4.0,
    layer: Annotated[str, Field(default="DIM")] = "DIM",
    layout: Annotated[str, Field(default="")] = "",
    ctx: Context = None,
) -> dict:
    """One ISO 6433 item reference: a numbered balloon, a leader, and a dot on
    the item.

    The balloon carries an ACADMCP_MECH payload naming its targets, so running
    the balloon pass again after the bill of materials is regrouped *renumbers*
    the existing balloon (`renumbered: true`) instead of drawing a second one.
    Refusals, all before the first entity: an item number below 1, a leader
    target inside the balloon, and an item number already used by another
    balloon for different targets — named with that balloon's handle.
    """
    from engineering.sheet.bom import add_balloon

    await ctx.info(f"Balloon {item} at ({x}, {y})")
    return await add_balloon(
        _backend(ctx),
        item=item,
        at=(x, y),
        leader_to=(leader_x, leader_y),
        targets=tuple(targets or ()),
        radius=radius,
        layer=layer,
        layout=layout or None,
    )


@cad_tool(
    summary="Attach a DWG/DXF as an external reference (attachment or overlay).", cost="mutate"
)
@mcp.tool(
    annotations={"title": "Sheet: Attach Xref", "destructiveHint": False},
    tags={"sheet", "drawing", "block"},
)
async def xref_attach(
    path: Annotated[str, Field(description="Full path of the drawing to reference.")],
    x: Annotated[float, Field(default=0.0, description="Insertion X (WCS).")] = 0.0,
    y: Annotated[float, Field(default=0.0, description="Insertion Y (WCS).")] = 0.0,
    scale: Annotated[float, Field(default=1.0, gt=0, description="Uniform scale.")] = 1.0,
    rotation: Annotated[
        float, Field(default=0.0, description="Rotation in degrees, CCW from +X.")
    ] = 0.0,
    kind: Annotated[
        str,
        Field(
            default="attach",
            description="'attach' (the reference's own xrefs come with it) or 'overlay' "
            "(they do not, so a circular reference cannot form).",
        ),
    ] = "attach",
    ctx: Context = None,
) -> dict:
    """Attach an external reference and insert it once. Both engines.

    The block takes the referenced file's stem as its name. Refusals, all
    before anything is written: a `kind` that is not 'attach' or 'overlay', a
    file that does not exist, a non-positive or non-finite scale, and a block
    name the drawing already holds — detach it first with
    `xref_manage(action='detach')`.
    """
    validated = validate_path(path)
    await ctx.info(f"Xref {kind}: {validated}")
    return await _backend(ctx).xref_attach(str(validated), (x, y), scale, rotation, kind)


@cad_tool(summary="List, reload, bind, detach or repath an external reference.", cost="mutate")
@mcp.tool(
    annotations={"title": "Sheet: Manage Xrefs", "destructiveHint": True},
    tags={"sheet", "drawing", "block"},
)
async def xref_manage(
    action: Annotated[
        str,
        Field(description="list | reload | bind | detach | path."),
    ],
    name: Annotated[str, Field(default="", description="Xref name; ignored by 'list'.")] = "",
    new_path: Annotated[
        str, Field(default="", description="Required by 'path': the reference's new location.")
    ] = "",
    ctx: Context = None,
) -> dict:
    """One operation on one external reference.

    `list`, `detach` and `path` work on both engines. `reload` and `bind` need
    a live AutoCAD seat's xref manager: headlessly they are refused with
    `capability: "xref_live"` rather than pretended, and the refusal says which
    engine does them. Other refusals: an action outside the five, an unknown
    xref name, a 'path' action with no `new_path`. On the live engine a listed
    row's `inserts` and `kind` are both null — ActiveX's block interface
    carries neither a per-xref insert count nor an overlay indicator, and this
    server does not invent them. Headlessly both are measured: `inserts`
    counts every insert in the drawing, model space and paper space alike, and
    `detach` removes exactly those.
    """
    await ctx.info(f"Xref {action} {name or '(all)'}")
    validated = str(validate_path(new_path)) if new_path else None
    return await _backend(ctx).xref_manage(name, action, validated)


@cad_tool(summary="Attach a raster image (PNG/JPG) as an underlay.", cost="mutate")
@mcp.tool(
    annotations={"title": "Sheet: Attach Image", "destructiveHint": False},
    tags={"sheet", "drawing", "create"},
)
async def image_attach(
    path: Annotated[str, Field(description="Full path of the raster image.")],
    x: Annotated[float, Field(default=0.0, description="Insertion X (WCS).")] = 0.0,
    y: Annotated[float, Field(default=0.0, description="Insertion Y (WCS).")] = 0.0,
    scale: Annotated[
        float,
        Field(
            default=1.0,
            gt=0,
            description="Drawing units per pixel: an 800x400 image at 0.1 is 80x40 mm.",
        ),
    ] = 1.0,
    rotation: Annotated[float, Field(default=0.0, description="Rotation in degrees.")] = 0.0,
    ctx: Context = None,
) -> dict:
    """Attach a raster underlay at the file's own aspect ratio. Both engines.

    `scale` is drawing units per pixel, so the placed size is the image's pixel
    size times `scale` and the payload reports both. Refusals, all before
    anything is written: a file that does not exist, a file no image reader can
    open (named), a non-positive or non-finite scale.
    """
    validated = validate_path(path)
    await ctx.info(f"Image underlay: {validated}")
    return await _backend(ctx).image_attach(str(validated), (x, y), scale, rotation)


@cad_tool(
    summary="Write the bill of materials to a CSV file, or to XLSX when openpyxl is present.",
    cost="safe",
)
@mcp.tool(
    annotations={"title": "Sheet: Extract Data", "destructiveHint": False},
    tags={"engineering", "sheet", "query"},
)
async def data_extract(
    path: Annotated[str, Field(description="File to write, ending .csv or .xlsx.")],
    fmt: Annotated[
        str,
        Field(default="csv", description="'csv' (always available) or 'xlsx' (needs openpyxl)."),
    ] = "csv",
    layer: Annotated[
        str, Field(default="", description="Only read INSERTs on this layer; empty reads all.")
    ] = "",
    group_by: Annotated[
        str,
        Field(default="designation", description="Group identical parts by this field, or ''."),
    ] = "designation",
    columns: Annotated[list[str] | None, Field(default=None)] = None,
    ctx: Context = None,
) -> dict:
    """The same rows `bom_extract` returns, written to a file.

    Refusals: a format that is not 'csv' or 'xlsx'; a drawing with nothing to
    itemise (nothing is written); and `fmt='xlsx'` without openpyxl, which
    carries `capability: "xlsx_write"` and names both the package to install
    and the CSV route that always works.
    """
    from engineering.sheet.bom import extract_records, rows_from_records
    from engineering.sheet.extract import write_rows

    backend = _backend(ctx)
    validated = validate_path(path, allow_write=True)
    records = await extract_records(backend, layer=layer or None)
    rows = rows_from_records(records, columns=columns, group_by=group_by or None)
    await ctx.info(f"Data extract: {len(rows)} rows -> {validated}")
    return write_rows(rows, str(validated), columns=columns, fmt=fmt)


@cad_tool(summary="Write the drawing as a real DWG at a chosen AutoCAD version.", cost="safe")
@mcp.tool(
    annotations={"title": "Drawing: Export DWG", "destructiveHint": False},
    tags={"sheet", "drawing"},
)
async def drawing_export_dwg(
    path: Annotated[str, Field(description="Full path of the .dwg to write.")],
    version: Annotated[
        str,
        Field(
            default="R2018",
            description="R2000 | R2004 | R2007 | R2010 | R2013 | R2018.",
        ),
    ] = "R2018",
    ctx: Context = None,
) -> dict:
    """Write a real DWG — not a DXF under a .dwg name.

    On a live AutoCAD seat this is `Document.SaveAs` with the matching
    AcSaveAsType, which (as AutoCAD's own SaveAs does) leaves the session bound
    to the file it just wrote. Headlessly it needs the ODA File Converter; when
    that is absent the call is refused with `capability: "dwg_write"` and the
    refusal names the install route and `drawing_export_dxf`, which writes a
    DXF AutoCAD opens unchanged. Check `system_capabilities` first: the ezdxf
    flag is re-evaluated at call time, so installing the converter changes it
    without a restart. An unknown version is refused by name with the six.
    """
    validated = validate_path(path, allow_write=True)
    await ctx.info(f"DWG export {version}: {validated}")
    return await _backend(ctx).drawing_export_dwg(str(validated), version)


# ---------------------------------------------------------------------------
# ── SECTION 25: Architecture (4 tools) ──────────────────────────────────────
# ---------------------------------------------------------------------------
# ── arch: catalogue (group C) ──


@cad_tool(
    summary="Search the furniture and sanitary catalogue in English or Turkish.",
    cost="read",
)
@mcp.tool(
    annotations={"title": "Architecture: Catalogue", "readOnlyHint": True},
    tags={"arch", "query"},
)
async def arch_catalogue_list(
    query: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Words matched against the item name, family and its English or Turkish "
                "label, ignoring case and Turkish diacritics - 'bed', 'yatak', 'klozet', "
                "'buzdolabi'. Every word must match."
            ),
        ),
    ] = None,
    family: Annotated[
        str | None,
        Field(default=None, description="furniture | sanitary"),
    ] = None,
    lang: Annotated[
        str,
        Field(default="en", description="en | tr - the language of the 'label' column"),
    ] = "en",
    ctx: Context = None,
) -> dict:
    """Catalogue rows: name, family, both labels, nominal size [w, d] in mm, block name, layer.

    Sizes are nominal catalogue dimensions - what a furniture or appliance
    catalogue lists as a common size - and every row says so in `size_basis`;
    they are not standards values. Twenty items: thirteen furniture, seven
    sanitary. Refusals: a `family` other than furniture/sanitary and a `lang`
    other than en/tr, each with the list.
    """
    from engineering.arch.catalogue import FAMILIES, SIZE_BASIS, catalogue

    try:
        rows = catalogue(query, family, lang=lang)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return {
        "count": len(rows),
        "families": list(FAMILIES),
        "size_basis": SIZE_BASIS,
        "items": list(rows),
    }


@cad_tool(
    summary="Insert a furniture or sanitary item - bed, sofa, WC, basin, bathtub - as a block.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Architecture: Catalogue Insert", "readOnlyHint": False},
    tags={"arch", "create"},
)
async def arch_catalogue_insert(
    name: Annotated[
        str,
        Field(
            description=(
                "Catalogue name from arch_catalogue_list, e.g. 'double_bed', 'sofa_3_seat', "
                "'wc', 'wall_basin', 'kitchen_counter'"
            )
        ),
    ],
    x: Annotated[float, Field(description="X (WCS) of the item's back-left corner")],
    y: Annotated[float, Field(description="Y (WCS) of the item's back-left corner")],
    rotation: Annotated[
        float,
        Field(
            default=0.0,
            description=(
                "Degrees CCW about (x, y); 0 puts the item's back (its wall side) along +X "
                "with the item extending towards +Y"
            ),
        ),
    ] = 0.0,
    ctx: Context = None,
) -> dict:
    """Define the item's block ARCH_<NAME> once, insert it on the furniture or sanitary layer.

    The block carries an invisible ITEM attribute with the catalogue name. Its
    size is a nominal catalogue dimension (reported with `size_basis`), not a
    standards value; the outline is a plan symbol drawn inside that footprint.
    The furniture / sanitary layer is created from the `arch` layer set when
    the drawing lacks it. Refusals, all before any write: an unknown name
    (with the nearest catalogue names) and a non-finite coordinate or rotation.
    """
    from engineering.arch.catalogue import insert_item

    await ctx.info(f"arch catalogue: {name} at ({x}, {y})")
    try:
        return await insert_item(_backend(ctx), name, at=(x, y), rotation=rotation)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@cad_tool(
    summary="Draw a structural grid: axis lines with numbered and lettered bubbles at both ends.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Architecture: Structural Grid", "destructiveHint": False},
    tags={"arch", "create"},
)
async def arch_grid(
    x_axes: Annotated[
        list[float],
        Field(description="X positions (WCS) of the vertical axes; numbered 1, 2, 3 left to right"),
    ],
    y_axes: Annotated[
        list[float],
        Field(
            description="Y positions (WCS) of the horizontal axes; lettered A, B, C bottom to top"
        ),
    ],
    labels: Annotated[
        dict | None,
        Field(
            default=None,
            description=(
                "Override the labels: {'x': [...], 'y': [...]} (either key), one label per "
                "position in the order the positions were given"
            ),
        ),
    ] = None,
    extension: Annotated[
        float,
        Field(
            default=1500.0,
            gt=0,
            description="Drawing units each axis runs past the outermost crossing axis",
        ),
    ] = 1500.0,
    scale: Annotated[
        float,
        Field(
            default=50.0,
            gt=0,
            description="Plot-scale denominator (50 = 1:50): bubbles and labels are paper mm x scale",
        ),
    ] = 50.0,
    ctx: Context = None,
) -> dict:
    """Axis lines on the arch grid layer (its CENTER linetype comes from the layer) and a
    5 mm bubble with its label at both ends of each axis, on the symbol layer.

    Numbers run along x and letters along y (A..Z, then AA, AB - no letter is
    skipped) unless `labels` names them. The bubble and label sizes are this
    server's declared drafting sizes in paper millimetres, not a standards value.
    Refusals, all before any write: an empty axis list in either direction, a
    non-finite or duplicate position, a label list whose length does not match
    its positions, a duplicate or empty label, an unknown `labels` key, and a
    non-positive `extension` or `scale`.
    """
    from engineering.arch.symbols import draw_grid

    await ctx.info(f"arch grid: {len(x_axes)} x {len(y_axes)} axes at 1:{scale:g}")
    try:
        return await draw_grid(
            _backend(ctx), x_axes, y_axes, labels=labels, extension=extension, scale=scale
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@cad_tool(
    summary="Draw a plan symbol: north arrow, section mark, level mark or elevation mark.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "Architecture: Symbol", "destructiveHint": False},
    tags={"arch", "create"},
)
async def arch_symbol(
    kind: Annotated[
        str,
        Field(description="north_arrow | section_mark | level | elevation_mark"),
    ],
    x: Annotated[float, Field(description="Symbol origin X (WCS)")],
    y: Annotated[float, Field(description="Symbol origin Y (WCS)")],
    params: Annotated[
        dict | None,
        Field(
            default=None,
            description=(
                "north_arrow: {rotation} (degrees CCW from +Y; 0 = north up). "
                "section_mark: {p1, p2, label, side} - p1/p2 are [x, y] relative to (x, y), "
                "side left|right is where the section looks, seen from p1 towards p2. "
                "level: {value} in metres (0 -> '±0.00'). "
                "elevation_mark: {label, directions} - directions from up|right|down|left"
            ),
        ),
    ] = None,
    lang: Annotated[
        str,
        Field(default="en", description="en | tr - the north letter and the decimal separator"),
    ] = "en",
    scale: Annotated[
        float,
        Field(
            default=50.0,
            gt=0,
            description="Plot-scale denominator (50 = 1:50): symbol sizes are paper mm x scale",
        ),
    ] = 50.0,
    ctx: Context = None,
) -> dict:
    """Draw one architectural symbol on the arch symbol layer.

    Sizes are this server's declared drafting sizes in paper millimetres times
    `scale` (a 16 mm north-arrow circle, 10 mm section and elevation bubbles),
    not standards values. Refusals, all before any write: an unknown `kind`
    (with the list), a parameter the kind does not take (with the ones it
    does), a section mark without p1/p2, with an empty label, an unknown side
    or a cut line too short for its two arrows, an elevation direction outside
    up/right/down/left or named twice, a non-finite level value, and a `lang`
    other than en/tr.
    """
    from engineering.arch.symbols import draw_symbol

    await ctx.info(f"arch symbol: {kind} at ({x}, {y})")
    try:
        return await draw_symbol(
            _backend(ctx), kind, (x, y), lang=lang, scale=scale, **(params or {})
        )
    except (TypeError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


# ---------------------------------------------------------------------------
# ── RESOURCES ───────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------


@mcp.resource(
    "autocad://drawing/info",
    name="Current Drawing Info",
    description="Metadata for the currently open drawing",
    mime_type="application/json",
    annotations={"readOnlyHint": True},
    tags={"drawing"},
)
async def resource_drawing_info(ctx: Context = None) -> str:
    b = ctx.lifespan_context.get("backend")
    if b is None:
        return json.dumps({"error": "Backend not ready"})
    try:
        info = await b.drawing_info()
        return json.dumps(_dc(info), indent=2)
    except Exception as exc:
        log.debug("Resource error: %s", exc)
        return json.dumps({"error": str(exc)})


@mcp.resource(
    "autocad://layers",
    name="Layer List",
    description="All layers in the current drawing with properties",
    mime_type="application/json",
    annotations={"readOnlyHint": True},
    tags={"layer"},
)
async def resource_layers(ctx: Context = None) -> str:
    b = ctx.lifespan_context.get("backend")
    if b is None:
        return json.dumps({"error": "Backend not ready"})
    try:
        layers = await b.layer_list()
        return json.dumps([_dc(lyr) for lyr in layers], indent=2)
    except Exception as exc:
        log.debug("Resource error: %s", exc)
        return json.dumps({"error": str(exc)})


@mcp.resource(
    "autocad://blocks",
    name="Block Library",
    description="All block definitions in the current drawing",
    mime_type="application/json",
    annotations={"readOnlyHint": True},
    tags={"block"},
)
async def resource_blocks(ctx: Context = None) -> str:
    b = ctx.lifespan_context.get("backend")
    if b is None:
        return json.dumps({"error": "Backend not ready"})
    try:
        blocks = await b.block_list()
        return json.dumps([_dc(blk) for blk in blocks], indent=2)
    except Exception as exc:
        log.debug("Resource error: %s", exc)
        return json.dumps({"error": str(exc)})


@mcp.resource(
    "autocad://entities/stats",
    name="Entity Statistics",
    description="Entity counts by type and layer",
    mime_type="application/json",
    annotations={"readOnlyHint": True},
    tags={"analysis"},
)
async def resource_entity_stats(ctx: Context = None) -> str:
    b = ctx.lifespan_context.get("backend")
    if b is None:
        return json.dumps({"error": "Backend not ready"})
    try:
        stats = await b.analysis_stats()
        return json.dumps(stats, indent=2)
    except Exception as exc:
        log.debug("Resource error: %s", exc)
        return json.dumps({"error": str(exc)})


@mcp.resource(
    "autocad://system/status",
    name="Server Status",
    description="AutoCAD MCP Pro server and backend status",
    mime_type="application/json",
    annotations={"readOnlyHint": True},
    tags={"system"},
)
async def resource_status(ctx: Context = None) -> str:
    b = ctx.lifespan_context.get("backend")
    if b is None:
        return json.dumps({"backend": "none", "connected": False})
    try:
        status = await b.system_status()
        return json.dumps(status, indent=2)
    except Exception as exc:
        log.debug("Resource error: %s", exc)
        return json.dumps({"error": str(exc)})


@mcp.resource(
    "autocad://entities/{layer_name}",
    name="Entities By Layer",
    description="List all entities on a specific layer",
    mime_type="application/json",
    annotations={"readOnlyHint": True},
    tags={"entity", "layer"},
)
async def resource_entities_by_layer(layer_name: str, ctx: Context = None) -> str:
    b = ctx.lifespan_context.get("backend")
    if b is None:
        return json.dumps({"error": "Backend not ready"})
    try:
        entities = await b.analysis_select_by_layer(layer_name)
        return json.dumps([_dc(e) for e in entities], indent=2)
    except Exception as exc:
        log.debug("Resource error: %s", exc)
        return json.dumps({"error": str(exc)})


@mcp.resource(
    "autocad://pid/symbols",
    name="P&ID Symbol Catalogue",
    description="Every P&ID symbol pid_symbol_insert can place: families, variants, ports, parameters",
    mime_type="application/json",
    annotations={"readOnlyHint": True},
    tags={"pid"},
)
async def resource_pid_symbols() -> str:
    from engineering.pid.symbols import CATALOG_VERSION, list_symbols

    return json.dumps({"catalog_version": CATALOG_VERSION, "symbols": list_symbols()}, indent=2)


@mcp.resource(
    "autocad://standards/isa51",
    name="ISA-5.1 Identification Letters",
    description="ISA-5.1-2009 Table 4.1 letter tables, the tag grammar and its disambiguation rules",
    mime_type="application/json",
    annotations={"readOnlyHint": True},
    tags={"pid", "standards"},
)
async def resource_isa51() -> str:
    from engineering.pid.tags import describe_tables

    return json.dumps(describe_tables(), indent=2)


# ---------------------------------------------------------------------------
# ── PROMPTS ──────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------


@mcp.prompt(tags={"template", "architectural"})
def prompt_floor_plan(
    building_name: str = "Building A",
    scale: str = "1:100",
    units: str = "mm",
) -> str:
    """Generate a prompt for creating a floor plan drawing."""
    return f"""You are creating a floor plan for '{building_name}' at scale {scale} in {units}.

LAYER SETUP (create these layers first):
  - WALLS       color=7 (white)  linetype=Continuous  lineweight=50
  - DOORS       color=3 (green)  linetype=Continuous  lineweight=25
  - WINDOWS     color=4 (cyan)   linetype=Continuous  lineweight=25
  - FURNITURE   color=8 (gray)   linetype=Continuous  lineweight=13
  - DIMENSIONS  color=2 (yellow) linetype=Continuous  lineweight=13
  - TEXT        color=7 (white)  linetype=Continuous  lineweight=13
  - GRID        color=9          linetype=DASHED       lineweight=13

WORKFLOW:
1. drawing_new() → create new drawing
2. layer_create() for each layer above
3. entity_create_rectangle() on WALLS layer for outer boundary
4. entity_create_polyline() on WALLS for interior walls (width ~200mm)
5. entity_create_arc() on DOORS for door swings (radius=900mm)
6. entity_create_rectangle() on WINDOWS for window openings
7. dimension_aligned() on DIMENSIONS layer for key measurements
8. entity_create_text() on TEXT layer for room labels
9. view_zoom_and_screenshot() to verify layout

CONVENTIONS:
- Exterior walls: 300mm thick
- Interior walls: 150mm thick
- Door openings: 900mm wide
- Window sills: 900mm from floor (not shown in plan)
- Room labels: centered in each space, height=300mm at 1:100
"""


@mcp.prompt(tags={"template", "pid"})
def prompt_pid_diagram(
    project_name: str = "Process Unit 01",
    revision: str = "Rev A",
) -> str:
    """Generate a prompt for creating a P&ID (Piping and Instrumentation Diagram)."""
    return f"""You are creating a P&ID for '{project_name}' ({revision}) with the server's P&ID tools.
Never hand-draw a symbol from lines and circles: every valve, pump, vessel and instrument is a
catalogue block with ports, and every line is drawn port-to-port.

REFERENCE SIZES (symbol-local mm at A3/A1 paper scale): instrument bubble Ø10, valve body 8×4,
pump Ø8, grid 5. Keep equipment ≥ 40 mm apart so line numbers and tags have room.

WORKFLOW
1. drawing_plan(intent="{project_name} P&ID", sheet_size="A3", layer_set_id="pid")
2. drawing_apply_iso_layers("pid")
3. pid_symbol_list() — read the catalogue once; pid_tag_parse(tag) when unsure about ISA-5.1 letters
4. pid_symbol_insert(symbol, x, y, tag=...) for each item:
     equipment  tag P-101 / V-201 / E-301 (desc="Feed pump")
     valves     symbol=gate|globe|ball|... with actuator=diaphragm|piston|motor|solenoid|hand and fail=FO|FC|FL
     instruments symbol="instrument", type=discrete|dcs|computer|plc, location=field|primary|auxiliary|..., tag="FIC-101"
     vessels    params={{"width", "height", "nozzles": [{{"name": "N1", "side": "top", "fraction": 0.5}}]}}
   Each call returns the block's ports in WCS — use them, never guess coordinates.
5. pid_line_draw(from_={{"handle", "port"}}, to={{"handle", "port"}}, line_class=process_major|process_minor|
   utility|pneumatic|electric|hydraulic|capillary|data, size="100", service="P", spec="CS1")
   A bubble's radial port needs no name. Read `crossings` and `port_reuse` in the response.
6. pid_graph() — `dangling` must be empty and `stats.confidence_min` 1.0 for a drawing you made.
7. drawing_critique(focus=None) — must return [] (pid_dangling_line, pid_duplicate_tag,
   pid_incompatible_connection, pid_untagged_instrument, pid_illegal_tag, pid_unconnected_equipment
   are part of it).
8. pid_instrument_index() / pid_line_list() / pid_equipment_list() for the deliverables.
9. drawing_finalize(save_path=...) — the score is the objective quality metric.

ONE-CALL ALTERNATIVE: pid_from_spec(spec) draws the whole sheet in one transaction from
{{sheet, equipment[], valves[], instruments[], lines[], connectors[]}} and returns the graph and the
critique; use dry_run=true first to see the planned routes and crossings.

OFF-PAGE: pid_symbol_insert("offpage", direction="out"|"in", tag="TO P&ID-002", link="L-17"); two connectors
sharing a link are paired by pid_graph.
"""


@mcp.prompt(tags={"template", "electrical"})
def prompt_electrical_schematic(
    circuit_name: str = "Main Distribution Panel",
    voltage: str = "400V/230V",
) -> str:
    """Generate a prompt for creating an electrical schematic diagram."""
    return f"""You are creating an electrical schematic for '{circuit_name}' at {voltage}.

LAYER SETUP:
  - POWER_LINES     color=7  lineweight=50
  - CONTROL_LINES   color=3  lineweight=25
  - COMPONENTS      color=2  lineweight=25
  - TERMINALS       color=4  lineweight=25
  - WIRE_NUMBERS    color=7  lineweight=13
  - COMPONENT_TAGS  color=8  lineweight=13
  - BORDER          color=7  lineweight=100

STANDARD IEC 60617 SYMBOLS:
  - Circuit breaker: rectangle with diagonal line
  - Contactor: circle with cross
  - Relay coil: rectangle
  - Motor: circle with 'M'
  - Fuse: rectangle with horizontal line
  - Switch NO: two points with gap
  - Switch NC: two points with diagonal slash

LADDER DIAGRAM CONVENTIONS:
  - Power rails: vertical lines on left (L1/L2/L3) and right (N/PE)
  - Rungs: horizontal lines connecting rails
  - Load elements (coils, motors): always on right side of rung
  - Contact elements: always to the left of loads
  - Rung numbers: on left margin

WORKFLOW:
1. layer_create() for all layers
2. entity_create_line() for power rails (vertical)
3. entity_create_polyline() for each circuit rung
4. Place component symbols with entity_create_*
5. Add wire numbers as text entities
6. Add component reference tags
7. view_zoom_and_screenshot() to verify
"""


@mcp.prompt(tags={"template", "mechanical"})
def prompt_mechanical_drawing(
    part_name: str = "Part-001",
    material: str = "Steel",
    scale: str = "1:1",
) -> str:
    """Generate a prompt for creating a mechanical engineering drawing."""
    return f"""You are creating a mechanical drawing for '{part_name}', material: {material}, scale: {scale}.

LAYER SETUP (ISO 128 standards):
  - VISIBLE       color=7  linetype=Continuous  lineweight=50  (visible edges)
  - HIDDEN        color=1  linetype=DASHED       lineweight=25  (hidden edges)
  - CENTER        color=3  linetype=CENTER       lineweight=13  (center lines)
  - DIMENSIONS    color=2  linetype=Continuous  lineweight=13  (dimensions)
  - SECTION       color=5  linetype=Continuous  lineweight=50  (section lines)
  - HATCHING      color=8  linetype=Continuous  lineweight=13  (section hatching)
  - PHANTOM       color=4  linetype=PHANTOM      lineweight=13  (phantom lines)
  - ANNOTATIONS   color=7  linetype=Continuous  lineweight=13  (notes)
  - BORDER        color=7  linetype=Continuous  lineweight=100 (border/title block)

DRAWING STANDARDS:
  - Third-angle projection (ASME) or First-angle (ISO)
  - Center lines extend 3-5mm beyond feature
  - Dimension lines: offset 8-10mm from feature
  - Leader lines: 60° angle preferred
  - Tolerance notation: ±0.1 general, tighter for fits
  - Surface finish: Ra values in µm
  - Title block: part number, revision, scale, material, drawn by, date

VIEW LAYOUT (for standard three-view drawing):
  - Front view: lower-left area
  - Top view: directly above front view
  - Right side view: directly to right of front view
  - Isometric: upper-right (optional)

WORKFLOW:
1. drawing_new() + set units with system_set_variable('INSUNITS', 4)  # mm
2. layer_create() for all layers
3. Draw front view outlines on VISIBLE layer
4. Add hidden lines on HIDDEN layer
5. Add center lines on CENTER layer (use entity_create_line with CENTER linetype)
6. Add dimensions on DIMENSIONS layer
7. Add section hatch on HATCHING layer (ANSI31 pattern)
8. Add title block text on ANNOTATIONS layer
"""


@mcp.prompt(tags={"template", "utility"})
def prompt_quick_drawing(
    description: str,
) -> str:
    """Generate step-by-step instructions for creating a drawing from a description."""
    return f"""Create a CAD drawing based on this description: {description}

SYSTEMATIC APPROACH:

STEP 1 — PLANNING
- What entities are needed? (lines, circles, arcs, polylines, text)
- What layers should be used?
- What are the approximate dimensions?
- Is there any existing drawing to modify?

STEP 2 — SETUP
Use drawing_new() or drawing_open() first.
Create necessary layers with layer_create().
Set the current layer with layer_set_current().

STEP 3 — DRAWING
Create entities in logical order:
- Large shapes first (boundaries, major outlines)
- Details and features next
- Annotations and dimensions last

STEP 4 — VERIFY
Use analysis_entity_stats() to confirm what was created.
Use view_zoom_and_screenshot() to see the current state.
Use entity_list() to check specific entities.

STEP 5 — SAVE
Use drawing_save() or drawing_export_dxf() to save the result.

TIPS:
- Use transaction_begin() before complex operations
- All coordinates are in drawing units (mm by default)
- Angles are in degrees, counter-clockwise from X axis
- Entity handles are hex strings (e.g. '1A2B') — save them for later editing
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _validate_http_bind(host: str) -> None:
    """Refuse non-loopback HTTP bind unless explicitly opted in.

    Without this guard, `--host 0.0.0.0` would expose 80+ tools (including
    arbitrary file open/save and AutoCAD command execution) to the network
    with no authentication — see the audit notes for the threat model.
    """
    if host in _LOOPBACK_HOSTS:
        return
    if not config.settings.allow_remote_http:
        raise SystemExit(
            f"Refusing to bind HTTP on non-loopback host '{host}'. "
            "Set ALLOW_REMOTE_HTTP=true and MCP_AUTH_TOKEN=<token> to opt in. "
            "Without auth, any client on the network can run AutoCAD commands."
        )
    if not config.settings.mcp_auth_token:
        raise SystemExit(
            f"Refusing to bind HTTP on '{host}' without MCP_AUTH_TOKEN. "
            "Set MCP_AUTH_TOKEN=<token> to enable bearer-token auth, or bind "
            "to 127.0.0.1 for local-only access."
        )
    log.warning(
        "⚠ Binding HTTP on non-loopback host '%s'. Auth token required for all "
        "requests. Make sure your firewall and TLS termination are in order.",
        host,
    )


# NEW-AUTH-1 — enforce the bind guard on EVERY launch path, not just __main__.
# The documented `fastmcp run server.py:mcp --transport http` imports this module
# and calls `mcp.run_async(...)` directly, bypassing the __main__ block. Both
# `mcp.run()` (via anyio.run(self.run_async, ...)) and the CLI funnel through
# run_async, so wrapping it on the instance closes the anonymous-remote-bind hole.
_HTTP_TRANSPORTS = {"http", "sse", "streamable-http"}
_orig_run_async = mcp.run_async


def _effective_http_host(kwargs: dict) -> str:
    """The address that will actually be bound — not the one we hope for.

    This used to read ``kwargs.get("host", "127.0.0.1")`` and treat an absent
    kwarg as loopback. It is not: ``fastmcp run`` forwards ``host`` only when
    ``--host`` was given, and otherwise fastmcp resolves it from
    ``fastmcp.settings.host``, which is environment-backed. So
    ``FASTMCP_HOST=0.0.0.0`` bound every interface while this guard inspected
    the literal string ``"127.0.0.1"`` and let it through — the check failed
    *open*, without a token, on the exact launch path it was written for.

    If the setting cannot be resolved we fall back to loopback rather than
    refusing to start, but we say so: a guard that cannot see the host is not
    enforcing anything, and silence is how the original bug survived.
    """
    host = kwargs.get("host")
    if host:
        return str(host)
    try:
        import fastmcp

        resolved = getattr(fastmcp.settings, "host", None)
        if resolved:
            return str(resolved)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(
            "Could not resolve the effective HTTP host from fastmcp settings (%s); "
            "assuming loopback. Pass --host explicitly to be sure of the bind guard.",
            exc,
        )
        return "127.0.0.1"
    log.warning(
        "fastmcp exposes no resolvable host setting; assuming loopback. Pass "
        "--host explicitly to be sure of the bind guard."
    )
    return "127.0.0.1"


async def _guarded_run_async(transport=None, *args, **kwargs):
    if (transport in _HTTP_TRANSPORTS) or (kwargs.get("transport") in _HTTP_TRANSPORTS):
        _validate_http_bind(_effective_http_host(kwargs))
    return await _orig_run_async(transport, *args, **kwargs)


mcp.run_async = _guarded_run_async


def main() -> None:
    """Run the MCP server from the installed ``autocad-mcp`` command."""
    parser = argparse.ArgumentParser(description="Run the AutoCAD MCP server")
    parser.add_argument("--transport", default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run()
    else:
        _validate_http_bind(args.host)
        mcp.run(
            transport=args.transport,
            host=args.host,
            port=args.port,
        )


if __name__ == "__main__":
    main()
