# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AutoCAD MCP Pro is a FastMCP 3.0 server that exposes ~110 tools, 5 resources, and 5 prompt templates for AutoCAD automation. (The exact tool count is reported dynamically by `system_status` / `system_about` — never hardcode it.) It runs with a dual-engine architecture: a live COM backend (Windows/AutoCAD required) and a headless ezdxf backend (works anywhere).

## Running the Server

```bash
# STDIO mode (default, for MCP clients like Claude Desktop)
python server.py

# HTTP mode
fastmcp run server.py:mcp --transport http --port 8000

# Force a specific backend
AUTOCAD_MCP_BACKEND=ezdxf python server.py   # headless file ops
AUTOCAD_MCP_BACKEND=com python server.py     # live AutoCAD (Windows only)
AUTOCAD_MCP_BACKEND=auto python server.py    # auto-detect (default)
```

## Installation

```bash
# Core (ezdxf backend only)
pip install -e .

# With COM backend (Windows + AutoCAD)
pip install -e ".[com]"

# With PDF/screenshot support via matplotlib
pip install -e ".[pdf]"

# Everything
pip install -e ".[full]"
```

## Running Tests

```bash
pytest
pytest tests/test_specific.py::test_name   # single test
pytest -x                                   # stop on first failure
```

Uses `pytest-asyncio` for async test support.

## Architecture

### Dual-Backend Pattern

The server uses a strategy pattern with an abstract base:

- `backends/base.py` — `AutoCADBackend` ABC defining the full interface + shared dataclasses (`EntityInfo`, `LayerInfo`, `BlockInfo`, `DrawingInfo`)
- `backends/ezdxf_backend.py` — `EzdxfBackend`: file-based DXF operations using the `ezdxf` library. All sync ezdxf calls are wrapped with `asyncio.to_thread` via `_async()`. Transactions are implemented as full DXF snapshots on an `_undo_stack`.
- `backends/com_backend.py` — `ComBackend`: live AutoCAD control via `pywin32` COM. All COM calls are routed through a `ThreadPoolExecutor` with a single thread to satisfy AutoCAD's STA (Single-Threaded Apartment) COM requirement.

Backend selection at startup (in `server.py::_make_backend`):
1. Check `AUTOCAD_MCP_BACKEND` env var
2. On Windows with `auto`/`com`: try COM first, fall back to ezdxf
3. On non-Windows: always use ezdxf

### FastMCP Server Structure (`server.py`)

The `mcp` FastMCP instance is configured with:
- **Lifespan** (`autocad_lifespan`): initializes the backend singleton; stores it in `ctx.lifespan_context["backend"]`
- **Middleware stack**: `ErrorHandlingMiddleware` → `AuditMiddleware` (custom timing/audit log) → `TimingMiddleware` → `LoggingMiddleware`
- **`_backend(ctx)`** helper: retrieves the backend from lifespan context, raises `ToolError` if not ready

Tools are organized into 12 sections:
1. Drawing Management (11 tools): `drawing_*`
2. Entity Creation (13 tools): `entity_create_*`
3. Dimensions (5 tools): `dimension_*`
4. Entity Modification (10 tools): `entity_move/copy/rotate/scale/mirror/offset/delete/array_*`
5. Entity Query (3 tools): `entity_get`, `entity_list`, `entity_delete_many`
6. Layer Management (12 tools + 2 linetype_*): `layer_*`, `linetype_list`, `linetype_load`
7. Block Operations (7 tools): `block_*`
8. Analysis & Query (8 tools): `analysis_*`
9. View & Screenshot (5 tools — includes `view_zoom_and_screenshot`): `view_*`
10. Transactions (3 tools): `transaction_begin/commit/rollback`
11. System (6 tools): `system_status/get_variable/set_variable/run_command/run_lisp/about`
12. Engineering / Deterministic CAD (7 tools): `gear_draw_*`, `keyway_draw_*`, `titleblock_apply_iso_a3`, `drawing_finalize`

### Adding a New Tool

1. Add the abstract method to `AutoCADBackend` in `backends/base.py`
2. Implement it in `EzdxfBackend` (`backends/ezdxf_backend.py`) using the `_async(func)` wrapper
3. Implement it in `ComBackend` (`backends/com_backend.py`) using `self._com(func)`
4. Register the tool in `server.py` with `@mcp.tool(...)` calling `_backend(ctx).your_method(...)`
5. Use `_dc(result)` to convert dataclass returns to dicts

### Key Conventions

- **Entity handles**: hex strings (e.g. `"1A2B"`). Always returned from create/copy operations; required by all modify/query operations.
- **Coordinates**: drawing units (mm by default); angles in degrees, counter-clockwise from X axis.
- **ACI colors**: 1–255 for specific colors, 256=ByLayer, 0=ByBlock.
- **COM backend only**: `system_run_command`, `system_run_lisp`, and `view_zoom_extents/window` (view ops are no-ops in ezdxf).
- **Screenshot**: COM uses Win32 window capture (Pillow required); ezdxf renders via matplotlib.
- **`_dc(obj)`**: converts dataclasses to dicts recursively for JSON serialization.
- **Engineering layer scaffold**: `drawing_new` auto-bootstraps standard linetypes (CENTER, HIDDEN, PHANTOM) and engineering layers (GEOMETRY, DIM, CENTER, HIDDEN, PHANTOM, HATCH, TEXT, TITLEBLOCK). Pass `bootstrap=False` to opt out.
- **Production drawings**: For real engineering output, use the `engineering/` package primitives via the `gear_*` / `keyway_*` / `titleblock_*` MCP tools — do NOT hand-draw teeth/keyways/sections with raw `entity_create_*` calls. Always end with `drawing_finalize` for the 8-step validator.

### Premium Drawing Rules

These rules are non-negotiable for production engineering output. The full
discipline + workflow templates live in `.claude/skills/autocad-mcp-premium/`.

1. **Plan before draw**: Call `drawing_plan(intent, scale, sheet)` *before* any
   `entity_create_*`. The returned PlanSpec is held by the backend and replayed
   by `drawing_critique` at finalize time.
2. **No coordinate guessing**: Never compute snap points (endpoints, midpoints,
   intersections, perpendicular feet) from memory. Use
   `point_from_snap(handle, "end"|"mid"|"center"|"quad"|"perp"|"near", ref_x, ref_y)`.
3. **Layer discipline**: All geometry on engineering layers (GEOMETRY, HIDDEN,
   CENTER, ...). Construction geometry on `CONSTRUCTION` layer (color 250,
   lightest weight); wipe with `construction_clear()` before finalize.
4. **Lineweights are ISO 128**: 0.13/0.18/0.25/0.35/0.50/0.70/1.00/1.40/2.00 mm
   only. Use `drawing_apply_iso_layers("mech"|"pid"|"iso13567")` to bootstrap
   correct lineweights per layer; never set lineweight manually outside this set.
5. **Corners must be explicit**: Two intersecting lines that should meet at a
   sharp corner must use `entity_trim` (with `keep_x/keep_y`) — leaving overshoot
   is reported as `untrimmed_corner` by `drawing_critique`. Rounded/beveled
   corners use `entity_fillet` / `entity_chamfer`.
6. **Dimensions via auto**: Prefer `dimension_auto(handles, "chain"|"baseline"|
   "ordinate")` over individual `dimension_linear` calls. Manual dimensions only
   for special cases (leader notes, ordinate origins).
7. **Critique-then-finalize**: `drawing_critique(focus=[...])` must return zero
   issues before `drawing_finalize`. Available focuses (closed enum): `iso128`,
   `layer_color`, `dim_overlap`, `untrimmed_corner`, `duplicate_entities`,
   `construction_left`. Pass `focus=None` for all.
8. **Meta-tools over raw**: When a meta-tool exists for a workflow, use it; do
   not reimplement with low-level primitives. Same rule as the engineering-
   primitives line above.

### Standard Premium Workflow (template)

```python
1. plan = drawing_plan(intent, sheet_size="A3", scale=1.0)
2. drawing_apply_iso_layers("mech")        # or "pid", "iso13567"
3. construction_xline(...)                 # scaffolding (optional)
4. entity_create_line / circle / ...       # main geometry on engineering layers
5. entity_trim / fillet / chamfer          # close corners explicitly
6. handles = entity_select_smart({...})    # avoid handle-memorization
7. dimension_auto(handles, style="chain")  # ISO 129 dims
8. issues = drawing_critique(focus=None)   # must be []
9. construction_clear()                    # wipe scaffolding
10. drawing_finalize(save_path=..., screenshot_path=...)
```
