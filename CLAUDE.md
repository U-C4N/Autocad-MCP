# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AutoCAD MCP Pro is a FastMCP 3.0 server that exposes ~248 tools, 11 resources, and 5 prompt templates for AutoCAD automation. (The exact tool count is reported dynamically by `system_status` / `system_about` — never hardcode it.) It runs with a dual-engine architecture: a live COM backend (Windows/AutoCAD required) and a headless ezdxf backend (works anywhere).

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

# XLSX workbooks (takeoffs, data_extract) via openpyxl
pip install -e ".[office]"

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
5. Entity Query (9 tools): `entity_get`, `entity_list`, `entity_delete_many`, `selection_get`, `selection_window/polygon/filter`, and `entity_get_xdata` / `entity_set_xdata` (typed by group code)
6. Layer Management (14 tools incl. 2 linetype_*): `layer_*`, `linetype_list`, `linetype_load`
7. Block Operations (8 tools): `block_*` — includes `block_define` (a block from typed primitive specs + ATTDEFs, no source entities needed)
8. Analysis & Query (8 tools): `analysis_*` — plus Batch (2), Templates (2), Validation (1)
9. View & Screenshot (4 tools — includes `view_zoom_and_screenshot`): `view_*`
10. Transactions (3 tools): `transaction_begin/commit/rollback`
11. System (8 tools): `system_status/get_variable/set_variable/run_command/run_lisp/about/capabilities`, plus `drawing_settings` (friendly units/scale/precision/osnap facade over system variables; the facade now also covers limits, grid/snap/ortho/polar, PSLTSCALE, annotation scale (`"1:50"`), linear/angular unit formats and the current dimstyle/textstyle, with unknown keys refused and every key reported on read)
12. Engineering / Deterministic CAD (8 tools): `gear_draw_*`, `keyway_draw_*`, `titleblock_apply_iso_a3`, `drawing_finalize`
13. Premium meta-tools (12): `drawing_preflight`, `drawing_plan`, `drawing_critique`, `drawing_refine`, `drawing_deliver`, `point_from_snap/intersection/tangent`, `construction_*`, `drawing_apply_iso_layers`, `dimension_auto`, `entity_select_smart`
14. GD&T (ISO 1101 / ASME Y14.5): `gd_frame` (feature control frames), `datum_feature` — enforced by the `gdt` critique focus
15. Layouts & Paper Space (12 tools): tab lifecycle `layout_list/create/set_current/delete/rename/copy`, viewports `viewport_create/list/set_scale/lock/delete`, and `entity_change_space` (CHSPACE — tagged `layout` *and* `modify`, and `layout` wins the group priority, so it files here). `drawing_export_pdf` takes an optional `layout` param and **does** project model content through viewports on both engines (`viewport_render` — v1.4 declared this COM-only and was wrong; the headless renderer only omits the viewport borders). `entity_change_space` is the mirror case — ezdxf-only (`chspace`), because ActiveX exposes no change-space member and the COM route is unverified.
16. 3D Solids (5 tools, opt-in via `ENABLE_3D=true`, COM only): `solid_box/cylinder/extrude/revolve/boolean` — hidden from discovery and rejected while disabled; ezdxf reports `solid_3d` as unsupported (no headless ACIS).
17. P&ID (9 tools, pack `pid`): `pid_symbol_list/insert` (ISO 10628-2 + ISA-5.1 catalogue authored in code, real blocks with TAG attributes, named ports, ACADMCP_PID XDATA), `pid_line_draw` (port-to-port orthogonal routing, ISA-5.1 line classes → layer/linetype/marker blocks, line numbers), `pid_tag_parse` (ISA-5.1 grammar), the reader `pid_graph` and its deliverables `pid_instrument_index/line_list/equipment_list`, and `pid_from_spec` (whole sheet, one transaction). Six `pid_*` critique focuses (`pid_dangling_line`, `pid_duplicate_tag`, `pid_incompatible_connection`, `pid_untagged_instrument`, `pid_illegal_tag`, `pid_unconnected_equipment`) run under `focus=None` and inside `drawing_finalize`; they return nothing on a drawing without P&ID nodes. Two resources (`autocad://pid/symbols`, `autocad://standards/isa51`) carry the catalogue and the ISA-5.1 letter tables. Both engines implement every P&ID contract; the COM paths were executed live once by `scripts/smoke_pid_com.py` (AutoCAD 2026).
18. Styles (10 tools, pack `core`): `dimstyle_list/create/modify/set_current` (authored ISO-25 / ANSI presets from `engineering/standards/dimstyles.py`; `dimstyle_modify` accepts only `DIM_VARIABLE_WHITELIST` names (33) and refuses out-of-range values by name before writing; `changed` lists only variables that moved, and `rerender_required: true` says a headless DIMENSION keeps its rendered block until redrawn), `textstyle_list/create/set_current` (`font_resolved: false` is reported, never refused — DXF stores only the name), `mleaderstyle_list/create` (both engines: ezdxf on `doc.mleader_styles`, live through the `ACAD_MLEADERSTYLE` dictionary — ActiveX has no MLeaderStyle *collection*, which is not an engine boundary, so no `mleaderstyle` capability key exists), and `drawing_apply_standard("iso"|"ansi")` — dimstyle + text style + units + the `mech` layer set in one call, each item reporting `created` or already present.
19. Page setup & templates (6 tools, pack `core`): `page_setup_list/apply` (ISO 216 / ANSI Y14.1 sizes from `PAPER_SIZES`; an unknown ctb is written and reported `plot_style_known: false`), `plot_style_list` (catalog + the files actually installed on a live seat), `batch_plot` (every paper layout through `drawing_export_pdf(layout=)`; `mediabox_mm` is **read back from the PDF's own `/MediaBox`**, so the sheet size is verified, not assumed), `drawing_template_list` (the five bundled templates: `iso_a3_mech`, `iso_a1_arch`, `iso_a3_pid`, `ansi_b_mech`, `ansi_d_arch` — DXF headless, `.dwt` twin on COM, `scripts/build_templates.py --check` proves the DXFs reproducible), `drawing_template_save` (a `.dwt` request headlessly is refused with capability `dwt_write` naming the DXF route). `drawing_new(template=)` resolves a catalog name to the bundled file and reports `template: {name, source}`.
20. Environment (22 tools, pack `settings`): documents `document_list/activate/close` (both engines — the headless backend keeps a document registry; `document_close` refuses to discard unsaved work unless `discard=True`), layer states `layer_state_save/restore/list/delete` (portable: JSON chunks in an `ACADMCP_LAYERSTATES` XRECORD that travels with the file and **does not appear in AutoCAD's Layer States Manager** — `system_capabilities.layer_states` says so), named views `view_named_save/restore/list` (headless restore writes `$VIEWCTR/$VIEWSIZE` and reports `applied: "header_only"`), UCS `ucs_list/set/restore` (non-orthogonal axes refused with the measured angle; **tool coordinates stay WCS** — a UCS is stored for the operator, no tool interprets input in it), `system_variable_describe` (the 90-entry `SYSVAR_CATALOG`; an unknown name answers `known: false` plus nearest names, never an error; `system_set_variable` now refuses an out-of-range value for a catalogued name before writing), `drawing_properties_get/set` (custom properties on both engines through `$CUSTOMPROPERTYTAG`; the five summary fields are COM-only, capability `dwgprops`), and the live-only group `system_launch` (`live_application`), `system_preferences_get/set` (`preferences`; a whitelist of `Preferences.*` keys with ranges, read-only keys refused by name, writes report `old`/`new`), `user_pick_point/select`, `system_prompt_message` (`interactive_prompt`; a cancelled pick returns `{cancelled: true}`, a `COM_CALL_TIMEOUT` overrun reports `timed_out`). The COM paths were executed live once by `scripts/smoke_settings_com.py` (AutoCAD 2026), which also built the `.dwt` twins.
21. Mechanical parts (6 tools, pack `mech`): `mech_part_draw`, `mech_view_add`, `mech_dimension_part`, `mech_hole_pattern`, `mech_part_from_spec`, `mech_part_inspect`. One part model — a profile (`RevolvedPart` segments or a `PrismaticPart` outline) plus typed features — and one view engine that turns it into front / side / top / section / detail views with hidden lines, cut faces and ISO 129 dimensions. The model is written onto the drawing as `ACADMCP_MECH` XDATA, so `mech_view_add` can add a view months later without the caller re-describing the part. A feature that cannot be projected into a view is listed in `omitted: [{feature, reason}]` — never dropped.
22. Standard parts (3 tools, pack `mech`): `std_part_list`, `std_part_insert`, `std_feature_draw`. ISO 4014 / 4017 / 4032 / 7089 / 4762 fasteners, ISO 15 bearings with ISO 8826-1 representation, DIN 471/472 retaining-ring grooves, DIN 509 undercuts, DIN 332 centre holes and ISO 3601-2 O-ring grooves — authored tables, inserted as real blocks with attributes and `ACADMCP_MECH` XDATA, which is what makes the parts list a *read* of the drawing. A size outside a table's transcribed coverage is refused by name, never interpolated. The four feature tables (DIN 471/472, DIN 509, DIN 332, ISO 3601-2) ship with **no rows** in 1.6: their drawing code and provenance tests exist, every size is refused by name, and filling one is a data edit described in its module docstring.
23. Mechanical annotation (5 tools, pack `mech`): `surface_texture` (ISO 21920-1, ISO 1302's older Ra form accepted), `weld_symbol` (ISO 2553), `centre_marks` (ISO 128-23), `section_line` (ISO 128-40 — it returns the plane `mech_view_add(kind="section")` consumes, so the label and the view cannot disagree), `hatch_material` (the ISO 128-50 map; an unknown material is refused with the list, never hatched as steel).
24. Sheet and delivery (11 tools, pack `core`): `sheet_frame` (ISO 5457 A4-A0 — 20 mm filing margin, 10 mm elsewhere on A3, zone grid, centring and trimming marks), `titleblock_apply` (ISO 7200 for every size; `titleblock_apply_iso_a3` is now a thin alias), `revision_add`, `bom_extract` (never modifies the drawing), `bom_table` (ISO 7573), `balloon_add` (ISO 6433, linked by XDATA so a rerun renumbers instead of duplicating), `data_extract` (CSV always; XLSX refused with `xlsx_write` when `openpyxl` is absent), `xref_attach`, `xref_manage`, `image_attach`, `drawing_export_dwg` (COM `SaveAs` natively; headless only with the ODA File Converter, otherwise `dwg_write` refuses and names the install route). The COM paths of SECTIONS 21-24 were executed live once by `scripts/smoke_mech_com.py` (AutoCAD 2026): part, section, dimensions, an ISO 4014 block with its XDATA, the parts list read off the drawing, a linked balloon, an xref attached and detached, a real `.dwg`, and `drawing_critique(focus=None)` clean.
25. Architecture (12 tools, pack `arch`): `arch_wall`, `arch_opening`, `arch_stair`, `arch_room`, `arch_rooms_detect`, `arch_schedule`, `arch_grid`, `arch_symbol`, `arch_catalogue_list`, `arch_catalogue_insert`, `arch_dimension_chains`, `arch_plan_from_spec`. One plan model — a wall network (axis polyline, thickness, justification, material) with hosted doors and windows — and one engine: L / T / X junctions resolved, openings cut, cut faces hatched by material (`engineering/arch/materials.py`: ISO 128-50 general hatching for brick, the JIS A 0150 RC / stone / wood symbols, AutoCAD's AR-CONC / AR-SAND, AAC as a declared convention), and anything it cannot place listed in `omitted: [{element, reason}]`, never dropped. Rooms are *computed*: a label's area is the planar face its point lies in, and `arch_rooms_detect` reads rooms from any plan (confidence 1.0 for our walls, 0.6 for plain lines) without modifying it. Schedules are a read of the `ACADMCP_ARCH` records, drawn as real TABLEs. Labels and schedules speak English, or Turkish with `lang="tr"` (decimal comma). The COM paths were executed live once by `scripts/smoke_arch_com.py` (AutoCAD 2026).
26. Understanding & QA (5 tools, pack `core`): `drawing_verify_requirements` (new in 1.7: typed source requirements bound to the real features and measured on the geometry, on the document or on a saved file; see *Verification* below), `drawing_understand` (one read-only report of a drawing somebody else made — declared vs inferred units with the evidence, declared vs robust extents and the outlier handles behind them, clusters (sheets, plan copies), every layer's likely discipline and service from an EN / TR / RU / NL / DE vocabulary with a confidence and the deciding keyword, equipment tags, rooms, text languages, blocks, layouts, title-block attributes), `drawing_scale_check` (a drawing against a reference: tags unique within a cluster are matched — a tag seen twice is `ambiguous`, never picked — pairs at least 2 m apart, the median ratio, IQR and the share within ±10 %: `to_scale` at 80 % or more, with its factor; `schematic` at 50 % or less; `partly` between), `drawing_diff` (two revisions matched by signature, then handle, then type + layer + nearest position; `markup=True` draws revision clouds on the newer one — its only write) and `drawing_topology_check` (dangling ends, near misses, interior crossings; the three `topo_*` critique focuses run the same engine under `focus=None` on layers no semantic focus covers). Every one reads one snapshot per drawing (`engineering/understand/snapshot.py`: `ezdxf` directly, or on a live seat `Document.Export(<tmp>/snap, "DXF", ...)`, which never renames the document and carries model space, paper space and block definitions — measured).
27. Plant takeoffs (2 tools, pack `plant`): `pipe_takeoff` (topology from the P&ID — segments merged at their ends, split at T-junctions, continued through inline fittings, a double-drawn segment counted once; service from the layer overridden by supply / return words; sizes kept as drawn — `Ø51`, `SMS51`, `DN20` are never equated — direct, by continuity or `unassigned`; lengths from the layout: the rectilinear MST of the tags a run connects, labelled an estimate, the P&ID length kept beside it as a comparison; statuses A / B / C) and `cable_takeoff` (one row per load: power from the P&ID's electrical texts, never invented; the panel from the layout's `Wiring to CPn` callouts; the Manhattan length plus the allowance rounded up to the metre; a section only from a caller's `section_rules`). Both write a workbook (Metraj / Özet / Kontrol / Metodoloji, `lang` tr / en / ru, XLSX with the `office` extra, CSV always) and never the drawing. `scripts/field_test_takeoff.py` runs both on a user's own pair and writes next to it, never into this repository. The COM paths were executed live once by `scripts/smoke_understand_com.py` (AutoCAD 2026).

**Tool profiles:** `TOOL_PROFILE=lean|full` (default `full`) controls the advertised surface — `lean` = 66 curated drafting tools (56 with `TOOL_PACKS=core`, which drops the three `pid_*`, the three `mech_*` and the four `arch_*` lean members, and keeps `drawing_understand` and `drawing_verify_requirements`; the five settings essentials — `drawing_apply_standard`, `dimstyle_set_current`, `textstyle_set_current`, `page_setup_apply`, `batch_plot` — are core-pack tools and stay; including `cad_batch`, since a client with a tight tool cap is exactly the client paying most per turn). Applied in the lifespan; reported by `system_about`. The `core` profile was removed in v1.5.0 (the discovery layer replaced it); `TOOL_PROFILE=core` falls back to `full` with a warning.

**Tool packs:** `TOOL_PACKS=all|core,pid,settings,mech,arch,plant` (default `all`) advertises only the vertical packs a client uses; `core` is always on and is everything no other pack claims; `settings` is the 22 SECTION 20 environment tools, `pid` the nine P&ID tools, `mech` the 14 SECTION 21-23 mechanical tools, `arch` the 12 SECTION 25 architectural tools, `plant` the two SECTION 27 takeoffs (track D's Plant 3D readers join it); SECTION 18/19/24/26 are core; unknown names are ignored with a warning; a `lean` profile intersects with the enabled packs. `system_about` reports `tool_packs`.

**Tool discovery:** `DISCOVERY_MODE=off|search` (default `off`). `search` replaces the advertised catalog with `search_tools` + `call_tool` + `describe_tool` (one tool's full input schema, risk, pack and its docstring's own examples), ranking hits over an AutoCAD command / synonym corpus (`discovery/aliases.py`) layered on fastmcp's BM25 index.

**ISO 129 tolerances:** `dimension_linear` / `dimension_radius` / `dimension_diameter` take `tol_upper` / `tol_lower` / `tol_mode` (`symmetric` ± / `deviation` +a/-b / `limit` / `basic`) and `text_override` (e.g. `⌀20 H7`). Prefer these over hand-drawn tolerance text.

**ISO 286 fits:** the same dimension tools take `fit="H7"` (mutually exclusive with `tol_*`) — deviations are resolved from authored ISO 286 tables (`engineering/fits.py`; shafts d/e/f/g/h/js/k/m/n/p and interference r/s/t/u, holes D/E/F/G/H/JS and K/M/N/P by the ISO 286 delta rule, sizes 1–500 mm) against the measured nominal and the fit code is appended to the dimension text.

**Drawing score:** `drawing_finalize` returns `payload["score"]` — a 0-100 scalar + `invalidity_ratio` + A-F `grade` over the validator + critique union. Use it as the objective quality metric.

**Measuring:** never read vertices back and shoelace them — that loses 28.2% of the area on a semicircular edge, silently. `analysis_measure_entity(handle)` reads the real geometry, and its payload states its own accuracy: `exact`, `flatten_tolerance`, `assumed_closed`, `self_intersecting`. A HATCH reports the area it *fills* (outer loops minus islands) plus `hatch_style`; on a curved hatch edge `flatten_tolerance` is not the accuracy knob, because ezdxf hands boundaries over as cubic Beziers whose ~0.028% circle error is already baked in. `boundary_trace` / `boundary_from_entities` report the same number `analysis_measure_entity` gives for the polyline they just drew — if those ever diverge, one of them is approximating and neither says so.

**Benchmark matrix:** `benchmarks/tasks_v8.py` is the current set (31 tasks; `--matrix v7` / `v6` / `v5` / `v4` / `v3` / `v2` reproduce the earlier sets). The published competitor reports are pinned v1.4 runs against v2 and are **never** back-filled for the five v3 tasks, the v4 `pid_roundtrip` / `page_setup_truth`, the v5 `mech_assembly` (a flange-coupling sheet gated on a zero-issue `drawing_critique(focus=None)` and a finalize score of at least 90) or the v6 `arch_roundtrip` (a two-room plan drawn by `arch_plan_from_spec`, its rooms read back by `arch_rooms_detect` within 0.1 % of the label and of the hand-computed net floor, the schedules listing the drawn tags, gated like `mech_assembly`) or the v7 `takeoff_roundtrip` / `understand_foreign` (the synthetic plant pair of `tests/fixtures/plant_pair.py` read without its truth: the P&ID called schematic, every routable run's layout length equal to the rectilinear MST of its tags, the cable metres rounded up after the allowance; units, the outlier, both plan copies and the service layers found by one `describe`) — the chart shows them `not run`, not zero. The v8 tasks (1.7.0) add what 1.7.0 made provable and safe - `requirements_roundtrip` (a part verified on the document, its bore moved inside the saved file, exactly that requirement failing on the reopened file), `cancel_safety`, `qa_large_drawing`, `export_safety`, `stale_preview` - and the rivals' ground: `revision_guarded_edit` (beiming), `pid_file_roundtrip` (puran), `foreign_edit_verified`, `damaged_dxf_refusal` (1.7.0 refuses by name; `recover` is 1.7.1) and `dimension_binding` (best-cad, COM-only with no adapter). A task a rival was never asked is `not_run`, a lane whose environment is absent `environment_blocked` (`run_competitors --no-fetch`), and neither enters a score; every report carries a run manifest (server SHA, dirty flag, fixture hashes, evaluator version, dependencies). `benchmarks/correctness_suite.py` (61 checks) is the release A/B gate, its nine `verify_t*` checks the roadmap scenarios T05, T09-T13, T18, T25, T36: `python benchmarks/compare_versions.py v1.6.0 --fail-on-regression` (exit 1 on any regression; the published report is `benchmarks/results/published/ab-v1.6.0-vs-v1.7.0.json`; `scripts/check_doc_numbers.py` names it in `AB_REPORT`).

### Identity, operations, previews and verification (1.7)

**Document identity and revision.** Every open document has a `document_id` (uuid4, kept across our own `SaveAs`) and a content `revision` (`document_identity()` on both engines; reported by `system_status`, `drawing_info`, `document_list`). Headless, the revision moves wherever `dirty` is set — every mutation, undo, redo, rollback, snapshot restore — and never on a save, export, finalize or delivery. Live, the `_mutating` decorator on the content-changing `ComBackend` methods bumps it (a test holds that set to the headless engine's dirty-setting methods), so a new mutating COM method must carry `@_mutating`. The live revision counts this server's writes only: AutoCAD's document events are not usable (a `WithEvents` sink left the seat refusing `Documents.Add()`), so never attach one; live staleness rests on the preview fingerprints.

**The bound document (COM).** The live engine binds `(Name, FullName)` and the id after `drawing_new` / `drawing_open` / `document_activate`, after `document_close` only when it closed the bound document, after `system_launch` only when it opened `open_path`, and at the first call that touches a document. `_acad_doc()` runs the guard inside each executor job before any document work: an active document that is not the bound one is refused with `ActiveDocumentChangedError` (kind `active_document_changed`, naming `bound` and `active`) and nothing is read or written. Never route new COM code around `_acad_doc()`, and never read `ActiveDocument` directly.

**Operations.** `backends/operations.py` keeps one record per call in a bounded in-memory journal (operation id, tool, request hash, state, document id, revision before/after, error kind; states `committed` / `partial` / `failed` / `refused` / `quarantined` / `cancelled` / `unknown_commit`). `AuditMiddleware` mints the id into `CURRENT_OPERATION`; `cad_batch` mints one per step; `ComBackend._run` carries it into the STA job. A client id (`cad_batch(operation_id=)`, `_meta.operation_id`) replays the recorded result for the same request hash, refuses `operation_conflict` for another and `requires_reconcile` after an `unknown_commit`. The journal is not durable (B13, 1.8.0). Every refusal and step error carries `operation_id`.

**Previews.** `engineering/changes/preview.py`: a dry run of `cad_batch` / `drawing_refine` / `text_find_replace` / `pid_from_spec` returns an exact `preview` (entity and region fingerprints: sha256 of the exported DXF tags, read the same way at preview and apply); an apply with `expected_revision` / `preconditions` runs only if nothing moved, else it is refused `stale_revision` with nothing changed, and it reports `revision` {checked, after, interleaved}.

**Verification** (`engineering/verification/`): `requirements.py` parses typed items (refusing by path), `observe.py` builds the observed geometry (INSERTs exploded with `block_path`, MINSERT cells, HATCH loops, DIMENSION fields), `binding.py` binds by handle / feature tag / arch element / predicate / DIMENSION — two equal candidates are `ambiguous` with both, never a pick — `measure.py` evaluates from CAD-read geometry only, `report.py` writes one row per requirement with denominators that add up, and `artifact.py` re-binds and re-measures on a saved file read fresh. Rules: a requirement is never measured on a dimension's text, a stored model or typed numbers; unbound or unmeasured is `unknown`, never a pass; a DIMENSION whose text or `DIMLFAC` disagrees with the geometry is a blocking `dimension_mismatch` finding beside the geometry row; lengths use the drawing's **declared** unit (`$INSUNITS`; an inferred unit is recorded, never applied — unlike the track-H readers); evidence is stale once the revision or an artifact's sha256 moves. The mechanical generators write an `ACADMCP_MECH` feature record on every entity in its create call (`xdata=` on `entity_create_*`): a new generator must tag its features, or binding falls back to geometry. `drawing_plan(persist_requirements=True)` stores the set as the `ACADMCP_REQUIREMENTS` XRECORD. `drawing_finalize` / `drawing_deliver` with `requirements=` block on a requirement that does not pass (`failed_acceptance`), whatever the drafting score.

**Exports never re-point.** `drawing_export_dxf` writes a copy on both engines (live: `Document.Export` into a private folder, renamed onto the target); only `drawing_save_as` re-points the document. Delivery parity is every space and every block definition against a fresh serialisation.

**QA reads are complete.** `engineering/qa_scan.py` takes one all-spaces snapshot per run (headless `readonly=True`; live through `Document.Export`, each layout's `Block.Count` compared); a check that raises is an issue with `detail.code` `check_failed`, an incomplete read `scan_incomplete` (`MAX_QA_ENTITIES`), and `drawing_refine` never repairs either. A new critique check reads the run's snapshot, never `entity_list(limit=...)`.

**The skill and the plugin.** The canonical premium skill is `.claude/skills/autocad-mcp-premium/` (`SKILL.md`, `references/`, `examples/`); `plugin/skills/premium/` is generated from it by `scripts/build_plugin.py` (`--check` fails on any difference — edit the canonical folder, never the copy). `plugin/.mcp.json` pins `autocad-mcp-pro[com,pdf,office]==<version>` (overridable with `AUTOCAD_MCP_SOURCE`), the repository-root `.claude-plugin/marketplace.json` lists the plugin, and `plugin/evals/` holds the `claude plugin eval` cases (run them only with `--no-publish` and a `--max-cost-usd`). A tool name the skill mentions must exist in `docs/tool-inventory.json` unless it sits under a "not yet" heading.

**Recipes.** `engineering/recipes/` holds versioned task recipes (`cad_batch`-shaped steps, postconditions a script checks), served as `autocad://recipes/index` / `autocad://recipes/{recipe_id}` and each run end to end in `tests/test_recipes.py`.

**Release gates.** One version in seven places (pyproject, `version.py`, `server.json`, `uv.lock`, `plugin.json`, the marketplace entry, the `.mcp.json` pin), held by `tests/test_release_consistency.py`; bump them together and run `uv lock`. CI runs `claude plugin validate --strict` (`.` and `./plugin`), the generated-file gates and `scripts/check_dist_contents.py dist` (no plugin or skill in the wheel; nothing untracked in the sdist); the release workflow adds `compare_versions.py <previous tag> --fail-on-regression` on the headless engine. The live COM paths are checked by `scripts/smoke_*_com.py` on `scripts/live/harness.py` (attach with `GetActiveObject` only, own `Documents.Add()` documents closed with `Close(False)`), results in `benchmarks/results/live/`.

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
- **Generic contracts added in 1.6:** `block_define(name, entities, attdefs)` (typed primitives + ATTDEFs, both engines; `overwrite` replaces contents so INSERTs keep the name) and `entity_get/set_xdata` (typed by group code; 255-char strings, 16 KB per entity, enforced before writing).
- **P&ID reader rules:** `pid_graph` never modifies the drawing, reads geometry from the drawing (an INSERT's real rotation/scale) rather than the catalogue, and reports `source`/`confidence` per node (catalog 1.0, xdata 0.95, heuristic 0.6, inferred 0.3) — never raised by agreement between heuristics. Read `stats.confidence_min` before trusting a foreign drawing.
- **`TOOL_PACKS=all|core,pid,settings,mech,arch,plant`** advertises only the packs a client uses; `core` is always on; `system_about` reports `tool_packs`.
- **Layer states are ours, not AutoCAD's.** `layer_state_*` stores a portable snapshot in an `ACADMCP_LAYERSTATES` XRECORD; it survives save/reopen on both engines and travels with the file, and it is not listed in AutoCAD's Layer States Manager. Said in the tool docstring, in `system_capabilities.layer_states` (`mode: "xrecord"`, reason `portable_acadmcp_xrecord;not_listed_in_autocad_layer_states_manager`) and in the README.
- **Page-setup evidence is the PDF.** `batch_plot` reports `mediabox_mm` parsed from the file's `/MediaBox` (`engineering/standards/pdfinfo.py`); a page setup is verified by what a printer would get, not by the setter's return value. A style or setting write reports `changed` — re-setting a value to itself is not a change.
- **Nothing interprets coordinates in UCS.** `ucs_set` / `ucs_restore` store and activate a UCS for the operator's benefit; every tool input and output stays WCS (the repository rule above).
- **The part model, not primitives**: a machine part is `mech_part_draw` with a segment list or an outline plus typed features — never hand-drawn lines. The same rule as gears and keyways, for the same reason.
- **Standards tables are transcribed, never derived**: every row under `engineering/mech/standards/` carries a `SOURCE` naming standard, edition and table, and a size outside the coverage is refused by name. `engineering/mech/standards/materials.py` states which of its rows are ISO 128-50, which are ASME Y14.2M via `acadiso.pat`, and which (concrete, wood) have no mechanical standard at all.
- **Six `mech_*` critique focuses** (`mech_missing_centreline`, `mech_unhatched_section`, `mech_view_misaligned`, `mech_duplicate_dimension`, `mech_thread_unrepresented`, `mech_bom_balloon_mismatch`) run under `focus=None` and inside `drawing_finalize`; they return nothing on a drawing with no `ACADMCP_MECH` payload. `mech_view_misaligned` checks the projection axis and not the quadrant, and `mech_duplicate_dimension` catches two dimensions in the same place rather than a semantic restatement — both narrowings are in the module docstring, because the payload carries no projection angle and no engine exposes what a dimension measured.
- **Live ActiveX will not create a layer on assignment** (`entity.Layer = "X"` on a missing layer raises `Key not found`; ezdxf creates it silently). Every mechanical/annotation draw path ensures its target layers before the first entity — a new path must do the same, or it passes every headless test and dies live.
- **A diameter's text stands beyond the FIRST chord point** on both engines (the ActiveX `AddDimDiametric` rule, measured); `dimension_diameter(x1, y1, x2, y2)` annotates on the `(x1, y1)` side.
- **The plan model, not lines**: a wall is `arch_wall` (an axis, a thickness, a justification, a material) and a door or window is hosted in it by `arch_opening` — never hand-drawn lines. The engine resolves the junctions and cuts the openings; what it cannot place is in `omitted`.
- **Areas are measured, never typed**: a room's area is the planar face its label point lies in, written into the label and the `room` record by `arch_room`; no tool accepts an area from its caller, and `arch_plan_from_spec` refuses a typed `area` by path.
- **Plan text follows the plot scale**: `scale` is a denominator (50 = 1:50) and a paper height `h` is drawn at `h × scale` drawing units — no annotative objects. `lang="tr"` switches words and the decimal separator; any other language is refused with the list.
- **Three `arch_*` critique focuses** (`arch_room_unlabelled`, `arch_wall_gap` — two wall ends within 50 mm that do not join, `arch_opening_clash`) run under `focus=None` and inside `drawing_finalize`; they return nothing on a drawing with no `ACADMCP_ARCH` record.
- **Nominal, not standard**: the furniture and sanitary blocks carry nominal catalogue dimensions and say so; the stair tool reports the Blondel relation (2R + G within 600–650 mm) as a design rule, never as a building code; the `arch` layer colours are a repository convention (ISO 13567 fixes names, not colours).
- **Read-only by default (track H)**: `drawing_understand`, `drawing_scale_check`, `drawing_topology_check` and both takeoffs never modify the drawing; `drawing_diff` writes only with `markup=True`, and a takeoff writes a workbook file, never the drawing.
- **Lengths come from the inferred unit**: a reader measures in the unit the geometry implies (text heights, wall and door sizes, overall size) and reports metres; a disagreement with `INSUNITS` is a warning with the numbers — a layout declaring inches over millimetre geometry is measured in millimetres, never ×25.4.
- **Scale check before a P&ID length**: a P&ID is a length source only when `drawing_scale_check` calls it `to_scale`; otherwise a run's length is the orthogonal estimate on the layout and the drawn length stays a comparison column (`length_source="pid"` on a schematic P&ID is refused unless `force=True`, quoting the statistics).
- **Nothing is invented (track H)**: a power the P&ID does not state stays empty and becomes an open item, an unreadable size stays `unassigned`, and no cable-section table is authored — a section comes only from the caller's `section_rules`.
- **Client drawings never enter the repository**: every track-H fixture is the synthetic plant pair (`tests/fixtures/plant_pair.py`), whose truth is computed from the positions it placed; `scripts/field_test_takeoff.py` refuses an output folder inside the repository and prints no quantity unless `--show` is given.
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
