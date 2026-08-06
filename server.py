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
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        try:
            return await call_next(context)
        except Exception as exc:
            capability = _batch_capability_of(exc)
            if capability is None:
                raise  # not ours — leave it byte-for-byte as it was
            message = _refusal_message(exc)
            return ToolResult(
                content=message,  # prose-only clients still get a full sentence
                structured_content={
                    "ok": False,
                    "kind": "unsupported",
                    "capability": capability,
                    "error": message,
                    "tool": context.message.name,
                    "backend": _backend_name_or_none(context),
                },
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
#   lean — a curated ~46-tool drafting/inspection core
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
    }
)

SOLID_TOOL_NAMES = frozenset(
    {"solid_box", "solid_cylinder", "solid_extrude", "solid_revolve", "solid_boolean"}
)

_active_tool_profile: dict | None = None


def _profile_enabled_names(profile: str, registered: set[str]) -> set[str]:
    if profile == "lean":
        enabled = registered & LEAN_TOOL_NAMES
    else:
        enabled = set(registered)
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
    summary="Start a blank drawing, pre-seeded with the standard engineering layers.",
    cost="mutate",
)
@mcp.tool(
    annotations={"title": "New Drawing", "destructiveHint": False},
    tags={"drawing"},
)
async def drawing_new(
    template: Annotated[str | None, "Optional path to .dwt template file"] = None,
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
    """Create a new empty drawing, optionally from a template (.dwt).

    When ``bootstrap=True`` (default), the drawing is also seeded with the
    standard engineering linetypes (CENTER/HIDDEN/PHANTOM) and layers
    (GEOMETRY, DIM, CENTER, HIDDEN, PHANTOM, HATCH, TEXT, TITLEBLOCK).
    """
    if template is not None:
        validated_template = validate_path(template, allow_write=False)
        template = str(validated_template)
    await ctx.info(f"Creating new drawing (template={template}, bootstrap={bootstrap})")
    backend = _backend(ctx)
    raw = await backend.drawing_new(template)
    result = dict(raw) if isinstance(raw, dict) else {"result": raw}
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
    """Close the current drawing. If save is True (default), the drawing is
    saved to its current path before closing. After this call, you must call
    drawing_new or drawing_open before any other tool."""
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
    """Insert a block reference (instance of an existing block definition)."""
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
    """Add one boundary path built from typed edges.

    Typed edges exist because a boundary that only accepts vertex lists
    silently straightens every curve it is given. Every edge is validated
    before any is written, so a malformed list refuses instead of leaving a
    half-built path.
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
    measurement, so editing it would break the association.
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
# ── SECTION 5: Entity Query (7 tools) ───────────────────────────────────────
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
    raw form can deadlock on the FILEDIA file-picker dialog and on the
    -LINETYPE option-menu prompt. This tool sets FILEDIA=0 around the call,
    picks the right .lin file from MEASUREMENT, and verifies the linetype
    actually loaded.
    """
    return await _backend(ctx).linetype_load(name, file)


# ---------------------------------------------------------------------------
# ── SECTION 7: Block Operations (7 tools) ───────────────────────────────────
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
    """Insert a block and optionally set attribute values."""
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
    """Explode a block reference into its individual component entities."""
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
    """Get all attribute values from a block reference as {TAG: value} dict."""
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
    """Update attribute values in a block reference."""
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
    "unknown_tool",  # not registered, or hidden by TOOL_PROFILE / ENABLE_3D
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
        "Rollback ends the AutoCAD undo mark and sends '_UNDO B' to the command line. "
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
                    "the active TOOL_PROFILE / ENABLE_3D gate).",
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

    COM: Undoes all operations back to the last undo mark.
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
    """Set an AutoCAD system variable (e.g. DIMSCALE, LTSCALE, MEASUREMENT)."""
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
                "dimscale, text_size, point_mode, point_size, osmode, fillet_radius. "
                'Example: {"units": "mm", "dimscale": 1.0, "linear_precision": 2}.'
            ),
        ),
    ] = None,
    ctx: Context = None,
) -> dict:
    """Read or change common AutoCAD drawing settings by friendly name.

    A convenience facade over the system variables (INSUNITS, LUPREC, LTSCALE,
    DIMSCALE, TEXTSIZE, OSMODE, …) so the user can say "set units to mm and
    dimension scale to 1" without memorising sysvar names. Call with no argument
    to get a full snapshot of the current settings.
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
    (iso128, layer_color, dim_overlap, untrimmed_corner, duplicate_entities, construction_left),
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
    return _dc(plan)


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
            "duplicate_entities, construction_left. None = run all.",
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
            description="Layer set: mech (DIN/ISO mechanical), pid (P&ID), iso13567 (CAD layer naming).",
        ),
    ] = "mech",
    ctx: Context = None,
) -> dict:
    """Bootstrap a full ISO-conformant layer set with correct colors and lineweights.
    Idempotent — existing layers are not modified.
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
    return f"""You are creating a P&ID for '{project_name}' ({revision}).

LAYER SETUP:
  - PROCESS_LINES   color=7  lineweight=50  (main process piping)
  - UTILITY_LINES   color=3  lineweight=25  (utility services)
  - INSTRUMENTS     color=2  lineweight=25  (instrument circles)
  - EQUIPMENT       color=5  lineweight=50  (vessels, pumps, HX)
  - VALVES          color=4  lineweight=25  (valve symbols)
  - TAGS            color=7  lineweight=13  (tag numbers)
  - ANNOTATIONS     color=8  lineweight=13  (notes)
  - BORDER          color=7  lineweight=100 (drawing border)

STANDARD SYMBOLS (draw as entities):
  - Vessels: rectangle with domed ends
  - Pumps: circle with triangle (impeller)
  - Heat Exchangers: two overlapping rectangles
  - Valves: two triangles point-to-point
  - Control valves: valve symbol + circle above
  - Instruments: circle with tag number

INSTRUMENT TAG FORMAT: [Function][Loop Number][Suffix]
  Examples: FT-101 (flow transmitter), FIC-101 (flow indicator controller)

WORKFLOW:
1. layer_create() for all layers
2. entity_create_rectangle() for drawing border
3. Place major equipment first (vessels, columns)
4. Draw process lines (polylines) connecting equipment
5. Place valve symbols at control points
6. Add instrument bubbles (circles + text)
7. Add line numbers and stream labels
8. Add title block text
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


async def _guarded_run_async(transport=None, *args, **kwargs):
    if (transport in _HTTP_TRANSPORTS) or (kwargs.get("transport") in _HTTP_TRANSPORTS):
        _validate_http_bind(kwargs.get("host", "127.0.0.1"))
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
