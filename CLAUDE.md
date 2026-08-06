# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AutoCAD MCP Pro is a FastMCP 3.0 server that exposes ~154 tools, 6 resources, and 5 prompt templates for AutoCAD automation. (The exact tool count is reported dynamically by `system_status` / `system_about` — never hardcode it.) It runs with a dual-engine architecture: a live COM backend (Windows/AutoCAD required) and a headless ezdxf backend (works anywhere).

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

- `backends/contracts/` — the interface, one module per domain (`drawing.py`, `layouts.py`, `entity_creation.py`, …). Split in v1.5.0 out of a single 1854-line ABC; some slices (premium, gdt, settings) also carry shared *concrete* implementations, so they are mixins rather than pure interfaces.
- `backends/capability.py` — the refusal vocabulary (`UnsupportedCapabilityError`) plus `@capability(key, reason=...)`, which marks a contract method optional and gives it a default that **raises** that refusal. Use it only for genuine engine boundaries, never for an unfinished method; `@abstractmethod` stays the default. Every key used must be declared in *both* capability maps — `tests/test_capability_contract.py` fails otherwise, which is what stops the decorator becoming a way to ship undeclared holes.
- `backends/base.py` — shared dataclasses (`EntityInfo`, `LayerInfo`, `BlockInfo`, `DrawingInfo`, `CapabilityMap`) and the `AutoCADBackend` composition of every contract. Import surface is unchanged: `from backends.base import AutoCADBackend, UnsupportedCapabilityError` still works.
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

Tools are organized into sections (counts are indicative — `system_about` is authoritative):
1. Drawing Management (12 tools): `drawing_*` (includes `drawing_redo`)
2. Entity Creation (14 tools): `entity_create_*` (includes `entity_create_table`, `leader_create_mleader`)
3. Dimensions (5 tools): `dimension_*`
4. Entity Modification (16 tools): `entity_move/copy/rotate/scale/mirror/offset/delete/array_*`, corner ops (`entity_trim/extend/fillet/chamfer`), plus in-place `entity_edit_text` (TEXT/MTEXT content/height/rotation) and `entity_edit_geometry` (CIRCLE/LINE/ARC — center/radius/endpoints/angles), both handle-preserving
5. Entity Query (4 tools): `entity_get`, `entity_list`, `entity_delete_many`, `selection_get`
6. Layer Management (14 tools incl. 2 linetype_*): `layer_*`, `linetype_list`, `linetype_load`
7. Block Operations (7 tools): `block_*`
8. Analysis & Query (8 tools): `analysis_*` — plus Batch (2), Templates (2), Validation (1)
9. View & Screenshot (4 tools — includes `view_zoom_and_screenshot`): `view_*`
10. Transactions (3 tools): `transaction_begin/commit/rollback`
11. System (8 tools): `system_status/get_variable/set_variable/run_command/run_lisp/about/capabilities`, plus `drawing_settings` (friendly units/scale/precision/osnap facade over system variables)
12. Engineering / Deterministic CAD (8 tools): `gear_draw_*`, `keyway_draw_*`, `titleblock_apply_iso_a3`, `drawing_finalize`
13. Premium meta-tools (12): `drawing_preflight`, `drawing_plan`, `drawing_critique`, `drawing_refine`, `drawing_deliver`, `point_from_snap/intersection/tangent`, `construction_*`, `drawing_apply_iso_layers`, `dimension_auto`, `entity_select_smart`
14. GD&T (ISO 1101 / ASME Y14.5): `gd_frame` (feature control frames), `datum_feature` — enforced by the `gdt` critique focus
15. Layouts & Paper Space (12 tools): tab lifecycle `layout_list/create/set_current/delete/rename/copy`, viewports `viewport_create/list/set_scale/lock/delete`, and `entity_change_space` (CHSPACE — tagged `layout` *and* `modify`, and `layout` wins the group priority, so it files here). `drawing_export_pdf` takes an optional `layout` param and **does** project model content through viewports on both engines (`viewport_render` — v1.4 declared this COM-only and was wrong; the headless renderer only omits the viewport borders). `entity_change_space` is the mirror case — ezdxf-only (`chspace`), because ActiveX exposes no change-space member and the COM route is unverified.
16. 3D Solids (5 tools, opt-in via `ENABLE_3D=true`, COM only): `solid_box/cylinder/extrude/revolve/boolean` — hidden from discovery and rejected while disabled; ezdxf reports `solid_3d` as unsupported (no headless ACIS).

**Tool profiles:** `TOOL_PROFILE=lean|full` (default `full`) controls the advertised surface — `lean` ≈ 47 curated drafting tools (including `cad_batch`, since a client with a tight tool cap is exactly the client paying most per turn). Applied in the lifespan; reported by `system_about`. The `core` profile was removed in v1.5.0 (the discovery layer replaced it); `TOOL_PROFILE=core` falls back to `full` with a warning.

**Tool discovery:** `DISCOVERY_MODE=off|search` (default `off`). `search` replaces the advertised catalog with `search_tools` + `call_tool`, ranking hits over an AutoCAD command / synonym corpus (`discovery/aliases.py`) layered on fastmcp's BM25 index.

**ISO 129 tolerances:** `dimension_linear` / `dimension_radius` / `dimension_diameter` take `tol_upper` / `tol_lower` / `tol_mode` (`symmetric` ± / `deviation` +a/-b / `limit` / `basic`) and `text_override` (e.g. `⌀20 H7`). Prefer these over hand-drawn tolerance text.

**ISO 286 fits:** the same dimension tools take `fit="H7"` (mutually exclusive with `tol_*`) — deviations are resolved from authored ISO 286 tables (`engineering/fits.py`; shafts d/e/f/g/h/js/k/m/n/p, holes D/E/F/G/H/JS, sizes 1–500 mm) against the measured nominal and the fit code is appended to the dimension text.

**Drawing score:** `drawing_finalize` returns `payload["score"]` — a 0-100 scalar + `invalidity_ratio` + A-F `grade` over the validator + critique union. Use it as the objective quality metric.

**Measuring:** never read vertices back and shoelace them — that loses 28.2% of the area on a semicircular edge, silently. `analysis_measure_entity(handle)` reads the real geometry, and its payload states its own accuracy: `exact`, `flatten_tolerance`, `assumed_closed`, `self_intersecting`. A HATCH reports the area it *fills* (outer loops minus islands) plus `hatch_style`; on a curved hatch edge `flatten_tolerance` is not the accuracy knob, because ezdxf hands boundaries over as cubic Beziers whose ~0.028% circle error is already baked in. `boundary_trace` / `boundary_from_entities` report the same number `analysis_measure_entity` gives for the polyline they just drew — if those ever diverge, one of them is approximating and neither says so.

**Benchmark matrix:** `benchmarks/tasks_v3.py` is the current set (15 tasks; `--matrix v2` reproduces the v1.4 ten). The published competitor reports are pinned v1.4 runs against v2 and are **never** back-filled for the five v3 tasks — the chart shows them `not run`, not zero. `benchmarks/correctness_suite.py` (24 checks) is the release A/B gate: `python benchmarks/compare_versions.py v1.4.0`.

### Adding a New Tool

1. Add the method to the matching module in `backends/contracts/` — `@abstractmethod` if both engines must have it, `@capability("key", reason=...)` only if it is a real engine boundary (and then declare `key` in both capability maps, or the gate fails)
2. Implement it in `EzdxfBackend` (`backends/ezdxf_backend.py`) using the `_async(func)` wrapper
3. Implement it in `ComBackend` (`backends/com_backend.py`) using `self._com(func)`
4. Register the tool in `server.py` with `@mcp.tool(...)` calling `_backend(ctx).your_method(...)`
5. Use `_dc(result)` to convert dataclass returns to dicts

### Key Conventions

- **Entity handles**: hex strings (e.g. `"1A2B"`). Always returned from create/copy operations; required by all modify/query operations.
- **Coordinates**: drawing units (mm by default); angles in degrees, counter-clockwise from X axis.
  **Every coordinate in or out of a tool is WCS on both engines.** DXF stores
  CIRCLE/ARC/LWPOLYLINE/TEXT/INSERT geometry in a per-entity frame (`extrusion`);
  `backends/ocs.py` translates at the backend boundary, so OCS never reaches a
  tool. An entity in a plane tilted out of WCS XY reports `plane_normal` and omits
  the fields xy cannot express; writing an xy into such an entity is refused with
  `capability: "ocs_tilted_plane"` — use `entity_move` instead.
- **ACI colors**: 1–255 for specific colors, 256=ByLayer, 0=ByBlock.
- **COM backend only**: `system_run_command`, `system_run_lisp`, and `view_zoom_extents/window` (view ops are no-ops in ezdxf).
- **Screenshot**: COM uses Win32 window capture (Pillow required); ezdxf renders via matplotlib.
- **`_dc(obj)`**: converts dataclasses to dicts recursively for JSON serialization.
- **Engineering layer scaffold**: `drawing_new` auto-bootstraps standard linetypes (CENTER, HIDDEN, PHANTOM) and engineering layers (GEOMETRY, DIM, CENTER, HIDDEN, PHANTOM, HATCH, TEXT, TITLEBLOCK). Pass `bootstrap=False` to opt out.
- **Production drawings**: For real engineering output, use the `engineering/` package primitives via the `gear_*` / `keyway_*` / `titleblock_*` MCP tools — do NOT hand-draw teeth/keyways/sections with raw `entity_create_*` calls. Always end with `drawing_finalize` for the 8-step validator.

### Premium Drawing Rules

These rules are non-negotiable for production engineering output.

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
