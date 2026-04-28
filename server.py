"""AutoCAD MCP Pro – FastMCP 3.0 server.

Dual engine: pywin32 COM API (live AutoCAD) + ezdxf (headless file ops).
The exact tool count is reported dynamically via system_status / system_about.

Usage:
    python server.py                          # STDIO (default)
    fastmcp run server.py:mcp --transport http --port 8000
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from dataclasses import asdict
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.logging import LoggingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware
from pydantic import Field

import config
from security import sanitize_command, sanitize_lisp, validate_path

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
    backend_env = os.environ.get("AUTOCAD_MCP_BACKEND", "auto").lower().strip()

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
                    raise RuntimeError(
                        f"COM backend requested but failed: {exc}"
                    ) from exc
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
            log.info("TOOL OK  %-40s %6.1fms", tool_name, elapsed)
            return result
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            log.warning("TOOL ERR %-40s %6.1fms  %s", tool_name, elapsed, exc)
            raise


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="AutoCAD MCP Pro",
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


def _dc(obj) -> dict:
    """Convert dataclass to dict (recursively)."""
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, list):
        return [_dc(i) for i in obj]
    return obj


def _registered_tool_count() -> int:
    """Number of @mcp.tool registrations currently on the server.

    Used by system_status / system_about so the count never drifts from reality.
    """
    try:
        components = mcp._local_provider._components
        return sum(1 for k in components if k.startswith("tool:"))
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# ── SECTION 1: Drawing Management (11 tools) ────────────────────────────────
# ---------------------------------------------------------------------------

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


@mcp.tool(
    annotations={"title": "New Drawing", "destructiveHint": False},
    tags={"drawing"},
)
async def drawing_new(
    template: Annotated[str | None, "Optional path to .dwt template file"] = None,
    bootstrap: Annotated[bool, Field(
        description="Auto-load CENTER/HIDDEN/PHANTOM linetypes and create standard "
                    "engineering layers (GEOMETRY, DIM, CENTER, HIDDEN, PHANTOM, "
                    "HATCH, TEXT, TITLEBLOCK). Disable for vanilla DXF.",
    )] = True,
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
            result["bootstrap"] = {"linetypes": lt_status, "layers": layer_status}
        except Exception as exc:
            await ctx.warning(f"Engineering bootstrap skipped: {exc}")
            result["bootstrap"] = {"error": str(exc)}
    return result


@mcp.tool(
    annotations={"title": "Open Drawing"},
    tags={"drawing"},
)
async def drawing_open(
    path: Annotated[str, "Full path to the .dwg or .dxf file"],
    ctx: Context = None,
) -> dict:
    """Open an existing DWG or DXF drawing file."""
    validated = validate_path(path, allow_write=False)
    await ctx.info(f"Opening drawing: {validated}")
    await ctx.report_progress(0, 100)
    result = await _backend(ctx).drawing_open(str(validated))
    await ctx.report_progress(100, 100)
    return result


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


@mcp.tool(
    annotations={"title": "Save As"},
    tags={"drawing"},
)
async def drawing_save_as(
    path: Annotated[str, "Full destination path including extension"],
    format: Annotated[str, "Output format: dwg, dxf, dwt"] = "dwg",
    ctx: Context = None,
) -> dict:
    """Save current drawing to a new path/format (DWG, DXF, or DWT template)."""
    validated = validate_path(path, allow_write=True)
    await ctx.info(f"Saving as {format}: {validated}")
    return await _backend(ctx).drawing_save_as(str(validated), format)


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


@mcp.tool(
    annotations={"title": "Export PDF"},
    tags={"drawing", "export"},
)
async def drawing_export_pdf(
    path: Annotated[str, "Output .pdf file path"],
    ctx: Context = None,
) -> dict:
    """Export the current drawing to PDF."""
    validated = validate_path(path, allow_write=True)
    await ctx.info(f"Exporting PDF: {validated}")
    await ctx.report_progress(0, 100)
    result = await _backend(ctx).drawing_export_pdf(str(validated))
    await ctx.report_progress(100, 100)
    return result


@mcp.tool(
    annotations={"title": "Purge Drawing"},
    tags={"drawing", "cleanup"},
)
async def drawing_purge(ctx: Context = None) -> dict:
    """Purge all unused objects (layers, blocks, linetypes, styles) from the drawing."""
    await ctx.info("Purging drawing")
    return await _backend(ctx).drawing_purge()


@mcp.tool(
    annotations={"title": "Audit Drawing", "readOnlyHint": False},
    tags={"drawing", "cleanup"},
)
async def drawing_audit(ctx: Context = None) -> dict:
    """Run an integrity audit on the drawing to detect and fix errors."""
    await ctx.info("Auditing drawing")
    return await _backend(ctx).drawing_audit()


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


@mcp.tool(
    annotations={"title": "Undo", "destructiveHint": False, "idempotentHint": False},
    tags={"drawing", "undo"},
)
async def drawing_undo(ctx: Context = None) -> dict:
    """Undo the last drawing operation."""
    return await _backend(ctx).drawing_undo()


@mcp.tool(
    annotations={"title": "Redo", "destructiveHint": False},
    tags={"drawing", "undo"},
)
async def drawing_redo(ctx: Context = None) -> dict:
    """Redo the last undone drawing operation."""
    return await _backend(ctx).drawing_redo()


# ---------------------------------------------------------------------------
# ── SECTION 2: Entity Creation (13 tools) ───────────────────────────────────
# ---------------------------------------------------------------------------

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
    result = await _backend(ctx).entity_create_arc(cx, cy, radius, start_angle, end_angle, layer, color)
    return _dc(result)


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


@mcp.tool(
    annotations={"title": "Create MText", "readOnlyHint": False},
    tags={"entity", "create", "annotation"},
)
async def entity_create_mtext(
    text: Annotated[str, "Text content (supports \\P for paragraph breaks, {\\H...;} for formatting)"],
    x: Annotated[float, "Insertion point X"],
    y: Annotated[float, "Insertion point Y"],
    width: Annotated[float, "Text box width in drawing units"] = 100.0,
    height: Annotated[float, "Character height in drawing units"] = 2.5,
    layer: Annotated[str | None, "Layer name"] = None,
    color: Annotated[int | None, "ACI color code"] = None,
    ctx: Context = None,
) -> dict:
    """Create a multi-line text entity (MTEXT) with word-wrap at the specified width."""
    await ctx.debug(f"Creating mtext at ({x},{y}) w={width}")
    result = await _backend(ctx).entity_create_mtext(text, x, y, width, height, layer, color)
    return _dc(result)


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
    result = await _backend(ctx).entity_create_hatch(pattern, boundary_points, scale, angle, layer, color)
    return _dc(result)


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


@mcp.tool(
    annotations={"title": "Create Ellipse", "readOnlyHint": False},
    tags={"entity", "create"},
)
async def entity_create_ellipse(
    cx: Annotated[float, "Center X"],
    cy: Annotated[float, "Center Y"],
    major_x: Annotated[float, "Major axis endpoint X (relative to center)"],
    major_y: Annotated[float, "Major axis endpoint Y (relative to center)"],
    ratio: Annotated[float, Field(description="Minor-to-major axis ratio (0 < ratio ≤ 1)", gt=0, le=1)] = 0.5,
    layer: Annotated[str | None, "Layer name"] = None,
    color: Annotated[int | None, "ACI color code"] = None,
    ctx: Context = None,
) -> dict:
    """Create an ellipse. major_x/major_y define the major axis vector from the center."""
    await ctx.debug(f"Creating ellipse center=({cx},{cy}) major=({major_x},{major_y}) ratio={ratio}")
    result = await _backend(ctx).entity_create_ellipse(cx, cy, major_x, major_y, ratio, layer, color)
    return _dc(result)


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
    result = await _backend(ctx).entity_create_block_ref(name, x, y, scale_x, scale_y, rotation, layer)
    return _dc(result)


# ---------------------------------------------------------------------------
# ── SECTION 3: Dimensions (5 tools) ─────────────────────────────────────────
# ---------------------------------------------------------------------------

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
    ctx: Context = None,
) -> dict:
    """Create a linear dimension measuring horizontal or vertical distance."""
    result = await _backend(ctx).dimension_linear(x1, y1, x2, y2, dim_x, dim_y, rotation, layer)
    return _dc(result)


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
    ctx: Context = None,
) -> dict:
    """Create a radius dimension for a circle or arc."""
    result = await _backend(ctx).dimension_radius(center_x, center_y, chord_x, chord_y, leader_length, layer)
    return _dc(result)


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
    ctx: Context = None,
) -> dict:
    """Create a diameter dimension for a circle (two points on opposite sides)."""
    result = await _backend(ctx).dimension_diameter(x1, y1, x2, y2, leader_length, layer)
    return _dc(result)


# ---------------------------------------------------------------------------
# ── SECTION 4: Entity Modification (10 tools) ───────────────────────────────
# ---------------------------------------------------------------------------

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
    ctx: Context = None,
) -> list[dict]:
    """Create a rectangular array of copies. Returns info of all created copies."""
    await ctx.info(f"Creating {rows}×{cols} rectangular array of entity {handle}")
    total = rows * cols
    await ctx.report_progress(0, total)
    result = await _backend(ctx).entity_array_rectangular(handle, rows, cols, row_spacing, col_spacing)
    await ctx.report_progress(total, total)
    return [_dc(e) for e in result]


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
    ctx: Context = None,
) -> list[dict]:
    """Create a polar (circular) array of copies around a center point."""
    await ctx.info(f"Creating polar array of {count} items around ({center_x},{center_y})")
    result = await _backend(ctx).entity_array_polar(handle, count, fill_angle, center_x, center_y)
    return [_dc(e) for e in result]


@mcp.tool(
    annotations={"title": "Set Entity Properties", "readOnlyHint": False, "destructiveHint": False},
    tags={"entity", "modify"},
)
async def entity_set_properties(
    handle: Annotated[str, "Entity handle"],
    layer: Annotated[str | None, "New layer name"] = None,
    color: Annotated[int | None, "New ACI color (256=ByLayer, 0=ByBlock, 1-255=specific)"] = None,
    linetype: Annotated[str | None, "New linetype name (e.g. 'DASHED', 'CENTER', 'ByLayer')"] = None,
    lineweight: Annotated[int | None, "Lineweight in 0.01mm units (-3=ByLayer, -2=ByBlock)"] = None,
    visible: Annotated[bool | None, "Set entity visibility"] = None,
    ctx: Context = None,
) -> dict:
    """Change one or more properties of an entity (layer, color, linetype, lineweight, visibility)."""
    await ctx.debug(f"Setting properties for entity {handle}")
    return await _backend(ctx).entity_set_properties(handle, layer, color, linetype, lineweight, visible)


# ---------------------------------------------------------------------------
# ── SECTION 5: Entity Query (3 tools) ───────────────────────────────────────
# ---------------------------------------------------------------------------

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


@mcp.tool(
    annotations={"title": "List Entities", "readOnlyHint": True},
    tags={"entity", "query"},
)
async def entity_list(
    type_filter: Annotated[str | None, "Filter by entity type: LINE, CIRCLE, ARC, LWPOLYLINE, TEXT, MTEXT, INSERT, HATCH, etc."] = None,
    layer_filter: Annotated[str | None, "Filter by layer name"] = None,
    limit: Annotated[int, Field(description="Maximum entities to return", ge=1, le=1000)] = 100,
    offset: Annotated[int, Field(description="Number of entities to skip", ge=0)] = 0,
    ctx: Context = None,
) -> list[dict]:
    """List entities in the drawing with optional type and layer filters.

    Returns handle, type, layer, color, and type-specific properties.
    Use handles with entity_get, entity_move, entity_delete, etc.
    """
    capped = min(int(limit), config.settings.max_list_limit)
    if capped < limit:
        await ctx.warning(
            f"limit {limit} exceeds MAX_LIST_LIMIT={config.settings.max_list_limit}; capped"
        )
    await ctx.info(f"Listing entities type={type_filter} layer={layer_filter} limit={capped}")
    result = await _backend(ctx).entity_list(type_filter, layer_filter, capped, offset)
    return [_dc(e) for e in result]


@mcp.tool(
    annotations={"title": "Delete Multiple Entities", "readOnlyHint": False, "destructiveHint": True},
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


# ---------------------------------------------------------------------------
# ── SECTION 6: Layer Management (12 tools) ──────────────────────────────────
# ---------------------------------------------------------------------------

@mcp.tool(
    annotations={"title": "List Layers", "readOnlyHint": True},
    tags={"layer", "query"},
)
async def layer_list(ctx: Context = None) -> list[dict]:
    """List all layers with their properties (color, linetype, frozen, locked, visibility)."""
    result = await _backend(ctx).layer_list()
    return [_dc(lyr) for lyr in result]


@mcp.tool(
    annotations={"title": "Create Layer", "readOnlyHint": False},
    tags={"layer"},
)
async def layer_create(
    name: Annotated[str, "New layer name"],
    color: Annotated[int, "ACI color code (1=Red, 2=Yellow, 3=Green, 4=Cyan, 5=Blue, 7=White)"] = 7,
    linetype: Annotated[str, "Linetype name"] = "Continuous",
    lineweight: Annotated[int, "Lineweight (-3=ByLayer, 0=0.00mm, 13=0.13mm, 25=0.25mm, 50=0.50mm)"] = -3,
    ctx: Context = None,
) -> dict:
    """Create a new layer with specified properties."""
    await ctx.info(f"Creating layer '{name}' color={color}")
    result = await _backend(ctx).layer_create(name, color, linetype, lineweight)
    return _dc(result)


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


@mcp.tool(annotations={"title": "Freeze Layer"}, tags={"layer"})
async def layer_freeze(
    name: Annotated[str, "Layer name to freeze"],
    ctx: Context = None,
) -> dict:
    """Freeze a layer (makes it invisible and unselectable, faster regeneration)."""
    return await _backend(ctx).layer_freeze(name)


@mcp.tool(annotations={"title": "Thaw Layer"}, tags={"layer"})
async def layer_thaw(
    name: Annotated[str, "Layer name to thaw"],
    ctx: Context = None,
) -> dict:
    """Thaw a frozen layer, making it visible and selectable again."""
    return await _backend(ctx).layer_thaw(name)


@mcp.tool(annotations={"title": "Lock Layer"}, tags={"layer"})
async def layer_lock(
    name: Annotated[str, "Layer name to lock"],
    ctx: Context = None,
) -> dict:
    """Lock a layer (entities visible but cannot be selected or modified)."""
    return await _backend(ctx).layer_lock(name)


@mcp.tool(annotations={"title": "Unlock Layer"}, tags={"layer"})
async def layer_unlock(
    name: Annotated[str, "Layer name to unlock"],
    ctx: Context = None,
) -> dict:
    """Unlock a layer to allow entity selection and modification."""
    return await _backend(ctx).layer_unlock(name)


@mcp.tool(annotations={"title": "Hide Layer"}, tags={"layer"})
async def layer_hide(
    name: Annotated[str, "Layer name to turn off"],
    ctx: Context = None,
) -> dict:
    """Turn off a layer (entities invisible but still processed in regeneration)."""
    return await _backend(ctx).layer_hide(name)


@mcp.tool(annotations={"title": "Show Layer"}, tags={"layer"})
async def layer_show(
    name: Annotated[str, "Layer name to turn on"],
    ctx: Context = None,
) -> dict:
    """Turn on a layer that was previously turned off."""
    return await _backend(ctx).layer_show(name)


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


@mcp.tool(
    annotations={"title": "List Loaded Linetypes", "readOnlyHint": True},
    tags={"layer", "linetype"},
)
async def linetype_list(ctx: Context = None) -> list[str]:
    """Return the names of all linetypes currently loaded in the active drawing."""
    return await _backend(ctx).linetype_list()


@mcp.tool(
    annotations={"title": "Load Linetype", "readOnlyHint": False},
    tags={"layer", "linetype"},
)
async def linetype_load(
    name: Annotated[str, "Linetype name to load (e.g. 'CENTER', 'DASHED', 'HIDDEN')"],
    file: Annotated[
        str | None,
        "Optional .lin file. Defaults to acadiso.lin (metric) or acad.lin "
        "(imperial), chosen from MEASUREMENT. Ignored by ezdxf backend."
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

@mcp.tool(
    annotations={"title": "List Blocks", "readOnlyHint": True},
    tags={"block", "query"},
)
async def block_list(ctx: Context = None) -> list[dict]:
    """List all block definitions in the drawing (name, origin, attribute count, entity count)."""
    result = await _backend(ctx).block_list()
    return [_dc(b) for b in result]


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
    result = await _backend(ctx).block_insert(name, x, y, scale_x, scale_y, rotation, attributes, layer)
    return _dc(result)


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

    Note: This tool works by using AutoCAD's BLOCK command (COM backend only).
    For ezdxf backend, entities must be added to a block definition directly.
    """
    await ctx.info(f"Creating block '{name}' from {len(handles)} entities")
    return await _backend(ctx).block_create_from_entities(name, handles, base_x, base_y)


@mcp.tool(
    annotations={"title": "Find Blocks By Name", "readOnlyHint": True},
    tags={"block", "query"},
)
async def block_find_references(
    name: Annotated[str, "Block definition name to search for"],
    ctx: Context = None,
) -> list[dict]:
    """Find all insert references to a specific block definition."""
    await ctx.info(f"Finding all references to block '{name}'")
    result = await _backend(ctx).entity_list(type_filter="INSERT")
    refs = [_dc(e) for e in result if e.properties.get("block_name") == name]
    return refs


# ---------------------------------------------------------------------------
# ── SECTION 8: Analysis & Query (8 tools) ───────────────────────────────────
# ---------------------------------------------------------------------------

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


@mcp.tool(
    annotations={"title": "Find Entities in Region", "readOnlyHint": True},
    tags={"analysis", "query"},
)
async def analysis_find_in_region(
    x1: Annotated[float, "Region minimum X"],
    y1: Annotated[float, "Region minimum Y"],
    x2: Annotated[float, "Region maximum X"],
    y2: Annotated[float, "Region maximum Y"],
    ctx: Context = None,
) -> list[dict]:
    """Find all entities within a rectangular region (crossing selection)."""
    await ctx.info(f"Finding entities in region ({x1},{y1}) → ({x2},{y2})")
    result = await _backend(ctx).analysis_entities_in_region(x1, y1, x2, y2)
    return [_dc(e) for e in result]


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


@mcp.tool(
    annotations={"title": "Measure Area", "readOnlyHint": True, "idempotentHint": True},
    tags={"analysis", "measure"},
)
async def analysis_measure_area(
    points: Annotated[list[list[float]], "Polygon vertices as list of [x, y] points (min 3)"],
    ctx: Context = None,
) -> dict:
    """Calculate the area of a polygon defined by vertices using the shoelace formula."""
    if len(points) < 3:
        raise ToolError("At least 3 points are required to calculate area.")
    area = await _backend(ctx).analysis_measure_area(points)
    # Also compute perimeter
    perimeter = sum(
        math.sqrt((points[(i+1) % len(points)][0] - points[i][0]) ** 2 +
                  (points[(i+1) % len(points)][1] - points[i][1]) ** 2)
        for i in range(len(points))
    )
    return {
        "area": round(area, 6),
        "perimeter": round(perimeter, 6),
        "vertex_count": len(points),
    }


@mcp.tool(
    annotations={"title": "Drawing Bounding Box", "readOnlyHint": True},
    tags={"analysis", "query"},
)
async def analysis_bounding_box(ctx: Context = None) -> dict:
    """Get the bounding box (extents) of all entities in the drawing."""
    return await _backend(ctx).analysis_bounding_box()


@mcp.tool(
    annotations={"title": "Select Entities By Layer", "readOnlyHint": True},
    tags={"analysis", "query"},
)
async def analysis_select_by_layer(
    layer_name: Annotated[str, "Layer name to select entities from"],
    ctx: Context = None,
) -> list[dict]:
    """Get all entities on a specific layer. Returns entity list with handles."""
    await ctx.info(f"Selecting all entities on layer '{layer_name}'")
    result = await _backend(ctx).analysis_select_by_layer(layer_name)
    cap = config.settings.max_list_limit
    if len(result) > cap:
        await ctx.warning(f"Layer has {len(result)} entities; truncated to {cap}")
        result = result[:cap]
    return [_dc(e) for e in result]


@mcp.tool(
    annotations={"title": "Select Entities By Type", "readOnlyHint": True},
    tags={"analysis", "query"},
)
async def analysis_select_by_type(
    entity_type: Annotated[str, "Entity type: LINE, CIRCLE, ARC, LWPOLYLINE, TEXT, MTEXT, INSERT, HATCH, SPLINE, ELLIPSE"],
    ctx: Context = None,
) -> list[dict]:
    """Get all entities of a specific type. Returns entity list with handles."""
    await ctx.info(f"Selecting all {entity_type} entities")
    result = await _backend(ctx).analysis_select_by_type(entity_type)
    cap = config.settings.max_list_limit
    if len(result) > cap:
        await ctx.warning(f"Found {len(result)} entities of type {entity_type}; truncated to {cap}")
        result = result[:cap]
    return [_dc(e) for e in result]


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
    layer_data: dict[str, dict] = {lyr.name: {"layer": _dc(lyr), "count": 0, "types": {}} for lyr in layers}
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
# ── SECTION 8b: Batch Operations (2 tools) ───────────────────────────────
# ---------------------------------------------------------------------------

@mcp.tool(
    annotations={"title": "Batch Create Entities", "readOnlyHint": False},
    tags={"entity", "create", "batch"},
)
async def entity_batch_create(
    entities: Annotated[list[dict], "List of entity definitions. Each dict must have 'type' and type-specific params. Types: line, circle, arc, polyline, rectangle, text, point"],
    ctx: Context = None,
) -> dict:
    """Create multiple entities in a single call for better performance.

    Each entity dict must have a 'type' key and the parameters for that type.
    Example: [{"type": "line", "x1": 0, "y1": 0, "x2": 100, "y2": 0}, {"type": "circle", "cx": 50, "cy": 50, "radius": 25}]
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


@mcp.tool(
    annotations={"title": "Batch Modify Entities", "readOnlyHint": False},
    tags={"entity", "modify", "batch"},
)
async def entity_batch_modify(
    operations: Annotated[list[dict], "List of operations. Each dict: {handle, action, ...params}. Actions: move(dx,dy), rotate(base_x,base_y,angle_deg), scale(base_x,base_y,factor), delete, set_properties(layer,color,...)"],
    ctx: Context = None,
) -> dict:
    """Apply multiple modifications in a single call.

    Example: [{"handle": "1A", "action": "move", "dx": 10, "dy": 20}, {"handle": "2B", "action": "delete"}]
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

@mcp.tool(
    annotations={"title": "Validate Drawing", "readOnlyHint": True},
    tags={"analysis", "validation"},
)
async def validation_check(
    checks: Annotated[list[str], "List of checks: empty_layers, zero_length, duplicate_entities"] = None,
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
                issues.append({
                    "check": "empty_layers",
                    "severity": "info",
                    "message": f"Layer '{lyr.name}' has no entities",
                    "layer": lyr.name,
                })

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
                    issues.append({
                        "check": "zero_length",
                        "severity": "warning",
                        "message": f"Zero-length line at ({start[0]:.1f}, {start[1]:.1f})",
                        "handle": line.handle,
                    })

    return {
        "ok": len(issues) == 0,
        "total_issues": len(issues),
        "issues": issues,
        "checks_run": checks,
    }


# ---------------------------------------------------------------------------
# ── SECTION 9: View & Screenshot (5 tools) ──────────────────────────────────
# ---------------------------------------------------------------------------

@mcp.tool(
    annotations={"title": "Zoom Extents", "readOnlyHint": False, "destructiveHint": False},
    tags={"view"},
)
async def view_zoom_extents(ctx: Context = None) -> dict:
    """Zoom to show all entities in the drawing (fit drawing in viewport)."""
    return await _backend(ctx).view_zoom_extents()


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


@mcp.tool(
    annotations={"title": "Screenshot", "readOnlyHint": True},
    tags={"view", "screenshot"},
)
async def view_screenshot(ctx: Context = None):
    """Capture a screenshot of the current drawing view.

    COM backend: captures live AutoCAD window at current view.
    ezdxf backend: renders via matplotlib to PNG.

    Returns an Image content block with the PNG data.
    """
    from fastmcp.utilities.types import Image

    await ctx.info("Capturing drawing screenshot")
    await ctx.report_progress(0, 100)

    b = _backend(ctx)
    png_bytes = await b.view_screenshot()

    await ctx.report_progress(100, 100)

    if png_bytes is None:
        raise ToolError(
            "Screenshot not available. "
            "For COM backend: ensure AutoCAD window is visible. "
            "For ezdxf backend: install matplotlib (pip install matplotlib)."
        )

    return Image(data=png_bytes, format="png")


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
# ── SECTION 11: System (6 tools) ────────────────────────────────────────────
# ---------------------------------------------------------------------------

@mcp.tool(
    annotations={"title": "Server Status", "readOnlyHint": True},
    tags={"system"},
)
async def system_status(ctx: Context = None) -> dict:
    """Get full status of the AutoCAD MCP Pro server and backend connection.

    Returns backend name, connection status, capabilities, document info.
    """
    b = ctx.lifespan_context.get("backend")
    tool_count = _registered_tool_count()
    unsafe = config.settings.dangerous_commands_enabled
    if b is None:
        return {
            "server": "AutoCAD MCP Pro",
            "backend": "none",
            "connected": False,
            "tool_count": tool_count,
            "unsafe_mode": unsafe,
            "error": ctx.lifespan_context.get("init_error"),
            "hint": "Set AUTOCAD_MCP_BACKEND=ezdxf to use headless mode, or start AutoCAD for COM mode.",
        }
    status = await b.system_status()
    status["server"] = "AutoCAD MCP Pro"
    status["tool_count"] = tool_count
    status["unsafe_mode"] = unsafe
    if unsafe:
        status["unsafe_mode_warning"] = (
            "DANGEROUS_COMMANDS_ENABLED=true — command/LISP sanitization disabled."
        )
    return status


@mcp.tool(
    annotations={"title": "Get System Variable", "readOnlyHint": True},
    tags={"system"},
)
async def system_get_variable(
    name: Annotated[str, "System variable name (e.g. DIMSCALE, LTSCALE, INSUNITS, CLAYER, MEASUREMENT)"],
    ctx: Context = None,
) -> dict:
    """Get an AutoCAD system variable value."""
    value = await _backend(ctx).system_get_variable(name)
    return {"variable": name, "value": value}


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
    """
    sanitize_command(command)
    await ctx.warning(f"Running command: {command}")
    return await _backend(ctx).system_run_command(command)


@mcp.tool(
    annotations={"title": "Execute AutoLISP", "readOnlyHint": False},
    tags={"system"},
)
async def system_run_lisp(
    expression: Annotated[str, "AutoLISP expression to evaluate (e.g. '(command \"ZOOM\" \"E\")')"],
    ctx: Context = None,
) -> dict:
    """Execute an AutoLISP expression (COM backend only).

    Example: '(setvar \"DIMSCALE\" 1.0)'
    """
    sanitize_lisp(expression)
    await ctx.warning(f"Running LISP: {expression[:80]}")
    return await _backend(ctx).system_run_lisp(expression)


@mcp.tool(
    annotations={"title": "Backend Info", "readOnlyHint": True},
    tags={"system"},
)
async def system_about(ctx: Context = None) -> dict:
    """Get detailed information about AutoCAD MCP Pro capabilities and available tools."""
    b = ctx.lifespan_context.get("backend")
    backend_name = b.name if b else "none"
    return {
        "name": "AutoCAD MCP Pro",
        "version": "1.0.0",
        "description": "Production-grade AutoCAD MCP server with dual COM+ezdxf engine",
        "active_backend": backend_name,
        "tool_groups": {
            "drawing": ["drawing_info", "drawing_new", "drawing_open", "drawing_save",
                        "drawing_save_as", "drawing_export_dxf", "drawing_export_pdf",
                        "drawing_purge", "drawing_audit", "drawing_undo", "drawing_redo"],
            "entity_creation": ["entity_create_line", "entity_create_circle", "entity_create_arc",
                                "entity_create_polyline", "entity_create_rectangle",
                                "entity_create_text", "entity_create_mtext", "entity_create_hatch",
                                "entity_create_spline", "entity_create_ellipse",
                                "entity_create_point", "entity_create_block_ref",
                                "entity_delete_many"],
            "dimensions": ["dimension_linear", "dimension_aligned", "dimension_angular",
                           "dimension_radius", "dimension_diameter"],
            "entity_modification": ["entity_move", "entity_copy", "entity_rotate", "entity_scale",
                                    "entity_mirror", "entity_offset", "entity_delete",
                                    "entity_array_rectangular", "entity_array_polar",
                                    "entity_set_properties"],
            "entity_query": ["entity_get", "entity_list"],
            "batch": ["entity_batch_create", "entity_batch_modify"],
            "templates": ["template_apply_layers", "template_list"],
            "layers": ["layer_list", "layer_create", "layer_delete", "layer_set_current",
                       "layer_modify", "layer_freeze", "layer_thaw", "layer_lock",
                       "layer_unlock", "layer_hide", "layer_show", "layer_isolate",
                       "linetype_list", "linetype_load"],
            "blocks": ["block_list", "block_insert", "block_explode",
                       "block_get_attributes", "block_set_attributes",
                       "block_create_from_entities", "block_find_references"],
            "analysis": ["analysis_entity_stats", "analysis_find_in_region",
                         "analysis_measure_distance", "analysis_measure_area",
                         "analysis_bounding_box", "analysis_select_by_layer",
                         "analysis_select_by_type", "analysis_layer_stats"],
            "validation": ["validation_check"],
            "view": ["view_zoom_extents", "view_zoom_window", "view_screenshot",
                     "view_zoom_and_screenshot"],
            "transactions": ["transaction_begin", "transaction_commit", "transaction_rollback"],
            "system": ["system_status", "system_get_variable", "system_set_variable",
                       "system_run_command", "system_run_lisp", "system_about"],
        },
        "total_tools": _registered_tool_count(),
        "unsafe_mode": config.settings.dangerous_commands_enabled,
    }


# ---------------------------------------------------------------------------
# ── SECTION 12: Engineering Tools (deterministic CAD generators) ────────────
# ---------------------------------------------------------------------------

@mcp.tool(
    annotations={"title": "Gear: Helical Front View", "destructiveHint": False},
    tags={"engineering", "gear"},
)
async def gear_draw_helical_front_view(
    module: Annotated[float, Field(gt=0, description="Module (mm). Pitch radius = module*teeth/2.")],
    teeth: Annotated[int, Field(ge=6, description="Number of teeth.")],
    helix_angle: Annotated[float, Field(ge=0, lt=45, description="Helix angle in degrees.")],
    pressure_angle: Annotated[float, Field(default=20.0, gt=0, lt=45,
        description="Pressure angle (deg). Standard: 20.")] = 20.0,
    hand: Annotated[str, Field(default="RH",
        description="Helix hand: 'RH' (right) or 'LH' (left).")] = "RH",
    center_x: Annotated[float, Field(default=0.0)] = 0.0,
    center_y: Annotated[float, Field(default=0.0)] = 0.0,
    bore_diameter: Annotated[float | None, Field(default=None, gt=0,
        description="Optional bore diameter (mm). Adds a centered hole.")] = None,
    keyway_width: Annotated[float | None, Field(default=None, gt=0,
        description="Keyway width (b). Auto from DIN 6885 if bore set and this is None.")] = None,
    keyway_depth: Annotated[float | None, Field(default=None, gt=0,
        description="Keyway depth into hub (t2).")] = None,
    ctx: Context = None,
) -> dict:
    """Deterministic helical gear front view: full involute outline (40 pts/flank),
    pitch/base/outer/root circles, helix symbol, optional bore + keyway.

    Returns a handle bundle plus 'metadata' for downstream gear_draw_section_aa.
    """
    from engineering import draw_helical_gear_front_view
    backend = _backend(ctx)
    await ctx.info(f"Drawing helical gear: m={module}, z={teeth}, beta={helix_angle} deg, hand={hand}")
    return await draw_helical_gear_front_view(
        backend, module=module, teeth=teeth, helix_angle=helix_angle,
        pressure_angle=pressure_angle, hand=hand, center=(center_x, center_y),
        bore_diameter=bore_diameter, keyway_width=keyway_width, keyway_depth=keyway_depth,
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
        backend, module=module, teeth=teeth, pressure_angle=pressure_angle,
        center=(center_x, center_y), bore_diameter=bore_diameter,
        keyway_width=keyway_width, keyway_depth=keyway_depth,
    )


@mcp.tool(
    annotations={"title": "Gear: Section A-A View", "destructiveHint": False},
    tags={"engineering", "gear"},
)
async def gear_draw_section_aa(
    gear_metadata: Annotated[dict, Field(description="The 'metadata' dict returned by gear_draw_helical_front_view or gear_draw_spur_front_view.")],
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
        backend, gear_metadata=gear_metadata, x_offset=x_offset, face_width=face_width,
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
        backend, center=(center_x, center_y), bore_diameter=bore_diameter,
        keyway_width=keyway_width, keyway_depth=keyway_depth, layer=layer,
    )


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
        backend, center=(center_x, center_y), bore_diameter=bore_diameter,
        face_width=face_width, keyway_width=keyway_width, keyway_depth=keyway_depth,
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
    ctx: Context = None,
) -> dict:
    """ISO 7200 / A3 (420x297 mm) title block. Title text is used verbatim."""
    from engineering import apply_iso_a3_titleblock, TitleBlockMetadata
    backend = _backend(ctx)
    metadata = TitleBlockMetadata(
        title=title, drawing_no=drawing_no, part_no=part_no, material=material,
        scale=scale, units=units, drawn_by=drawn_by, checked_by=checked_by,
        date=date, sheet=sheet, revision=revision, company=company,
    )
    return await apply_iso_a3_titleblock(backend, metadata=metadata, origin=(origin_x, origin_y))


@mcp.tool(
    annotations={"title": "Drawing: Finalize (validate + save + screenshot)",
                 "destructiveHint": True},
    tags={"engineering", "drawing", "validation"},
)
async def drawing_finalize(
    save_path: Annotated[str | None, Field(default=None,
        description="If given, save drawing here before validation. Pass full path including extension.")] = None,
    screenshot_path: Annotated[str | None, Field(default=None,
        description="If given, write PNG screenshot to this path.")] = None,
    expected: Annotated[dict | None, Field(default=None,
        description="Optional contract: {'part_type', 'helix_angle', 'must_have_bore', 'must_have_keyway'}")] = None,
    ctx: Context = None,
) -> dict:
    """8-step pre-completion validator: real dimensions, no hidden orphans, linetypes loaded,
    bore+keyway in both views (if expected), title consistency, saved to disk, screenshot exported,
    DWG path returned. Raises ToolError if any 'error' severity finding is present.
    """
    from engineering import DrawingValidator
    backend = _backend(ctx)

    if save_path:
        validated = validate_path(save_path, allow_write=True)
        await backend.drawing_save_as(str(validated))

    if screenshot_path:
        validated_shot = validate_path(screenshot_path, allow_write=True)
        try:
            shot = await backend.view_screenshot()
            if shot:
                from pathlib import Path as _P
                _P(str(validated_shot)).write_bytes(shot)
        except Exception as exc:
            await ctx.warning(f"Screenshot export failed: {exc}")

    result = await DrawingValidator().run(backend, expected=expected or {})
    payload = result.to_dict()
    info = await backend.drawing_info()
    payload["dwg_path"] = getattr(info, "full_path", "")
    if not result.ok:
        raise ToolError(
            f"drawing_finalize: validation failed with {result.summary['error']} error(s). "
            f"First: {result.findings[0].code}: {result.findings[0].message}"
        )
    return payload


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


if __name__ == "__main__":
    transport = "stdio"
    host = "127.0.0.1"
    port = 8000

    args = sys.argv[1:]
    if "--transport" in args:
        idx = args.index("--transport")
        transport = args[idx + 1]
    if "--port" in args:
        idx = args.index("--port")
        port = int(args[idx + 1])
    if "--host" in args:
        idx = args.index("--host")
        host = args[idx + 1]

    if transport == "stdio":
        mcp.run()
    else:
        _validate_http_bind(host)
        mcp.run(transport=transport, host=host, port=port)
