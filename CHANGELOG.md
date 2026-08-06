# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] — 2026-08-06

### Added

- **Selection filters — 3 tools.** `selection_window` (AutoCAD's ssget
  window/crossing), `selection_polygon` and `selection_filter` (QSELECT).
  Window returns only entities wholly inside; crossing also returns the ones
  straddling the edge — the modes are reported back and `window` is the
  default, because the over-selecting mode must never be the implicit one. A
  zero-area box is refused rather than answered with an empty list, which would
  read as "nothing there".
  `selection_filter` takes named parameters rather than a query string
  deliberately: ezdxf's query language answers an unknown attribute with an
  empty result, so `LINE[nosuchattr=='x']` is indistinguishable from "no
  matches". Here a typo is a `TypeError` at the boundary, and `filtered_by`
  reports which filters actually ran. `min_area` excludes shapes that have no
  area at all rather than treating them as zero, and a negative `min_area` is
  refused — no area is negative.
- **Boundary tracing — 2 tools, and five deliberately not built.**
  `boundary_trace` is AutoCAD's BOUNDARY/BPOLY: a seed point in, the nearest
  enclosing closed polyline out. It never calls `edgeminer.find_all_loops` —
  measured at **63.9 s for 40 edges and a TimeoutError at 60**, because the
  docstring's O(n!) is real — and walks one loop per seed-adjacent edge
  instead, which is 0.0057 s at the same size. Straight edges are split where
  they cross, so a construction line drawn across a shape actually divides it;
  without that the line shares no endpoint with anything and is invisible to
  the edge graph. Curved edges are not split, and the capability says so.
  Two things the obvious implementation gets silently wrong, both now tested:
  `edgesmith.lwpolyline_from_chain` emits n+1 points with the last duplicating
  the first and `closed` left False — a polyline that draws like a square and
  behaves like an open path to hatch association, area queries and
  `entity_offset`; and `edgesmith.loop_area` works off edge endpoints, so two
  semicircular arcs closing into a circle measure **0.0** against a true
  314.159. The area now comes from the flattened curve and each curved edge
  keeps its bulge.
  **Not built: every REGION and 2D-boolean tool the plan listed.** ezdxf cannot
  author a REGION — `add_region()` returns one with zero ACIS bytes — and the
  substitute, `greiner_hormann`, takes flat vertex lists. Measured on a square
  with one edge bulged into a semicircle, routing it through that boolean loses
  **28.2% of the area and 12.5% of the perimeter**. That is the same magnitude
  as the `analysis_measure_area` bug this release fixed, and shipping it would
  have re-introduced the class.
- **Hatch depth — 3 tools.** `hatch_set_gradient`, `hatch_edit` and
  `hatch_add_boundary`. `hatch_edit` leaves omitted parameters alone — a
  partial edit that resets the rest is data loss — and `changed` reports the
  attributes that actually *moved*, so re-setting a value to what it already
  was comes back empty rather than as a false positive. `hatch_add_boundary`
  takes typed edges (line/arc/ellipse) because a boundary API that only accepts
  vertex lists silently straightens every curve it is given; every edge is
  validated before any is written, so a malformed list refuses instead of
  leaving a half-built path. On COM it refuses with
  `capability: "hatch_edge_paths"`: ActiveX appends boundary loops as existing
  objects, not as typed edges, so the fidelity this tool exists for cannot be
  expressed there.
- **`analysis_list_properties` — AutoCAD's LIST.** The raw DXF attribute set
  for one handle, which `entity_get` deliberately does not carry. It cannot be
  handed over as-is for two reasons, both fixed here: `dxfattribs()` holds
  `Vec3` values and is not JSON-serialisable, and its coordinates are in the
  entity's own frame, so for anything with an extrusion they would contradict
  every other coordinate this server reports.
- **Annotation objects — 4 tools.** `entity_create_wipeout` (mask what is
  behind a closed polygon), `entity_create_revcloud`, `text_set_background`
  (MTEXT background mask) and `text_find_replace`. Each carries a refusal that
  was measured rather than assumed. A wipeout of fewer than three points
  encloses no area, so it is refused instead of returning a handle to a mask
  that masks nothing. A revision cloud *is* its arcs — `ezdxf.revcloud`'s own
  helper stamps the `RevcloudProps` xdata and takes the bulge sign from the
  winding direction, so the result satisfies `is_revcloud()` — and a
  `segment_length` longer than the shortest edge produces no arcs at all, which
  would be a plain rectangle reported as a cloud; that case is refused.
  `text_set_background` refuses TEXT (only MTEXT has a background-fill
  attribute, so setting one on TEXT would report success and change nothing)
  and refuses a scale below 1.0 (a box smaller than its text is a stripe
  through the text).
  `text_find_replace` searches TEXT, MTEXT, ATTRIB **and** ATTDEF, including
  inside block definitions — leaving the ATTDEF behind would fix the drawing
  and reintroduce the old text on the next insert. It returns
  `searched_types`, because "no matches" and "that type was never searched" are
  different answers and a caller cannot otherwise tell them apart. DIMENSION
  text is deliberately out of scope: its text field holds the `<>` override
  placeholder rather than the measurement, so editing it would either do
  nothing or detach the dimension from what it measures — the response says so.
  On COM, `entity_create_revcloud` refuses with `capability: "revcloud"`:
  ActiveX has no revision-cloud member and driving the command blind against a
  live drawing is unverified.
- **The rest of the paper-space lifecycle — 8 tools.** v1.4 could make a sheet
  and never unmake it. Added `layout_delete`, `layout_rename`, `layout_copy`,
  `viewport_list`, `viewport_set_scale`, `viewport_lock`, `viewport_delete` and
  `entity_change_space` (CHSPACE). Verified headless (ezdxf); the COM paths for
  the first seven are implemented against the ActiveX reference but **never
  executed against a live AutoCAD**, and each is marked so in the source.
  Most of the work went into refusals, because ezdxf's own guards stop short of
  where a tool boundary has to: `layouts.delete("")` deletes the *first* sheet
  rather than refusing, `layouts.get("")` resolves to whichever is active, and
  `layouts.rename()` skips the name validation `new()` performs, so `Bad/Name`
  and even `""` land in the table and the file still audits clean. Deleting or
  renaming the current tab used to leave three different answers to "where does
  my next line go?" — `layout_list` said one thing, `$TILEMODE` another, and
  `_msp()` quietly wrote to model space; all three now agree. A handle from a
  deleted sheet refuses instead of dying with `AttributeError` (`entitydb.get`
  is documented not to filter destroyed entities, and `_get_entity` believed
  it).
  `layout_copy` is a construction — ezdxf has no layout-copy API and
  `xref.load_paperspace` is cross-document only — and every way it can go wrong
  is invisible to the Auditor, which reports zero errors on a duplicated tab
  order, two main viewports, and a hatch whose boundary still points into the
  layout it was copied from and dangles the moment that layout is deleted. All
  three are prevented structurally; `skipped` and `associativity_dropped` report
  what could not be carried over rather than letting a bare `ok: true` imply a
  complete copy.
  `viewport_list` reports the layout's own main viewport with `is_main: true`
  rather than hiding it — it is what remains after every drafting viewport is
  deleted — and refuses to report a scale on R12, where `view_height` and
  `flags` are dropped on export and `get_scale()` then returns a fabricated
  number (150.0 measured for a viewport storing no scale at all). The two
  writers refuse on R12 outright rather than reporting a success the file
  cannot keep, and `viewport_lock` read-modify-writes the flags bitfield, which
  also carries the UCS-icon and grid bits a bare assignment would silently drop.
- **`entity_change_space` (CHSPACE), headless only.** AutoCAD moves objects
  *through* a viewport, rescaling them so they look identical on screen.
  ezdxf's `move_to_layout` applies no geometric change whatsoever, so the
  obvious implementation reports success, turns a 100 mm feature into 100 mm of
  paper inside a 1:2 viewport, and writes a perfectly clean file. The transform
  is therefore the tool, and it uses `viewport.get_transformation_matrix()`
  rather than a hand-rolled one: the hand-rolled version ignores
  `view_target_point` and misplaced geometry by 25 mm in testing while
  `is_top_view` was still true and the twist angle still zero — past every guard
  one would naturally write. A regression test pins the result against what
  ezdxf's own rendering pipeline projects.
  It refuses rather than guessing: non-plan and twisted viewports (ezdxf returns
  a meaningless matrix for the first and rotates about the paper origin for the
  second), dimensions unless `freeze_dimensions=true` bakes the measurement in
  first (`transform` halves `get_measurement()` while the baked block text keeps
  the old value, so one of the two is always lying), ACIS bodies (`transform`
  is accepted and moves nothing), tables and proxies (`transform` raises, and
  `hasattr(e, "transform")` is `True` for all of them), viewports themselves,
  and entities already in the target space. Geometry that lands outside the
  viewport or off the sheet is moved and **flagged** (`inside_viewport`,
  `on_sheet`), not refused — AutoCAD allows it too.
  On the COM backend this refuses with `capability: "chspace"`: ActiveX has no
  change-space member, so the alternatives are `CopyObjects` + delete or driving
  the command blind, and neither could be verified against a live seat.
- **`CAD_PROGID` — the COM ProgID is configurable.** `"AutoCAD.Application"`
  was hardcoded at both connection sites, so AutoCAD-compatible applications
  (BricsCAD, ZWCAD, GstarCAD) were unreachable even though they expose the same
  ActiveX object model. The setting is honoured on *both* paths deliberately:
  `Dispatch` is not a passive probe — it COM-launches the application and sets
  `Visible = True` — so honouring the setting on `GetActiveObject` and then
  falling back would start the very AutoCAD the operator said they were not
  using. An unrecognised ProgID fails loudly for the same reason, and the error
  names both the ProgID it tried and the variable to change. `system_status`
  now reports `cad_progid`, without which a non-AutoCAD operator reads an
  `autocad_version` key naming a product they do not own.
  **Caveat, stated everywhere it appears:** `CAD_PROGID` only changes which COM
  application the backend attaches to — every tool beyond the connection itself
  is developed and tested against AutoCAD alone, and clone compatibility is
  unverified, so treat a non-default ProgID as experimental. The candidate
  ProgIDs in `.env.example` are commented out and marked UNVERIFIED: none has
  been tested against a running seat.

- **`analysis_measure_entity` — measure the drawing, not your memory of it.**
  Neither backend could answer "what is the area of the polyline I just drew":
  `analysis_measure_area` shoelaced points the *caller* typed in, and the live
  COM backend did the same despite having the document, a `HandleToObject`
  lookup it already used fourteen times, and an ActiveX `Area` property. The
  only available workflow was also silently wrong — `entity_get` read LWPOLYLINE
  vertices through the default format and kept x and y, dropping the **bulge**
  that makes an edge an arc. Measured on a 100×100 square with one edge bulged
  into a semicircle: 10000 reported against a true 13926.9908, **28.2% low**,
  perimeter **12.5% low**, nothing in the response saying so. And
  `props["length"] = ent.length()` was dead code — `LWPolyline` has no
  `length()`, so the key never appeared at all.
  Bulge geometry is always circular, so the new `engineering/measure.py` is
  exact rather than approximate: shoelace plus each arc's signed circular
  segment, and arc length for perimeter. `entity_get` now reports `area` and
  `length` from the same maths. The payload states its own accuracy — `exact`,
  `flatten_tolerance`, `assumed_closed` (an open boundary is closed the way
  AutoCAD's AREA does), and `self_intersecting`, because the shoelace *cancels*
  crossed lobes and a bowtie measures 0.0. REGION/3DSOLID refuse with
  `capability: "measure_area_acis"` naming the live backend; LINE/TEXT/INSERT
  raise a plain error, since no engine can give them an area and a capability
  tag would falsely suggest switching helps. `AREA`/`MEASUREGEOM` now route here
  rather than at the points-based tool — a drafter typing AREA picks objects,
  they do not retype coordinates.
- **Handle grounding: `view_screenshot(overlay_handles=true)`.** A screenshot
  showed the model what the drawing looks like and nothing it could act on —
  every modify tool takes a handle, and nothing connected "the circle at the
  top-left" to a hex string. Each entity is now labelled with its handle at its
  own bounding-box centre (taken from the renderer's extents, so the label lands
  where the entity is *drawn* even in a rotated frame). Off by default, because
  labels are ink on the drawing; past 40 entities the render is capped and the
  image itself says "handles shown for N of M" rather than letting a labelled
  subset read as the whole sheet. The live backend captures the AutoCAD window
  rather than rendering, so there is nothing to label there — it refuses with
  `capability: "handle_overlay"` instead of quietly returning an unlabelled
  image, since producing the overlay would mean creating and deleting entities
  in the user's open drawing.
- **`block_create_from_entities` works on the live backend.** It refused
  outright and pointed at `system_run_command` with `_BLOCK`, pushing callers
  through the free-text escape hatch for something ActiveX does directly
  (`Blocks.Add` + `Document.CopyObjects`). The tool's own docstring said the
  opposite — "works by using AutoCAD's BLOCK command (COM backend only)" — so
  the one engine that could not do it was advertised as the one that could.
  Both engines now also report `skipped` handles instead of swallowing them, and
  a call where nothing resolves fails rather than leaving an empty definition
  behind. COM path unit-tested against a fake ActiveX object, not
  hardware-verified.
- **Undo and redo on the headless backend (`EZDXF_UNDO_DEPTH`, default 0).**
  The plan called this "add redo". The larger half was that **undo never worked
  either**: `_undo_stack` was declared, read and cleaned up, but nothing ever
  appended to it, so `drawing_undo` could only answer "Nothing to undo" — while
  the tool summary said "Step back one operation" and the alias corpus routed
  `UNDO` and "take that back" straight at it. The old test asserted
  `isinstance(result, dict)`, which a permanent failure satisfies.
  It is opt-in because it is not free. ezdxf keeps no journal, so a history step
  is a whole DXF snapshot: measured 3.1 ms / 19 KB at 10 entities but 130 ms /
  658 KB at 5000, and switching it on costs **37×** on entity creation (0.18 →
  6.65 ms per call) even on a small drawing. Off, it costs nothing; on, you get
  real multi-step undo/redo. Both tools refuse with
  `capability: "undo_history"` naming the setting rather than pretending. The
  live COM backend uses AutoCAD's own undo and ignores it. Drawing after an undo
  discards the redo branch, as in AutoCAD — otherwise redo would restore a state
  that never existed, with geometry you had removed reappearing beside geometry
  you drew afterwards.
- **BREAKING (behavior): geometry now goes into the layout you selected.**
  `layout_set_current("A3-Sheet")` answered `{"ok": true, "current":
  "A3-Sheet"}` and then every entity created afterwards landed in **model
  space** anyway, on both engines — `_msp()` returned model space
  unconditionally, so the tool reported a state change that changed nothing.
  Proven: a line drawn straight after selecting a layout was in modelspace and
  not in the layout. That was also why a title block could not go on a sheet, so
  `titleblock_apply_iso_a3` gains a `layout` parameter — where a title block
  belongs, since the border frames the printed sheet rather than the model — and
  restores your current space afterwards rather than leaving you on the sheet.
  Existing model-space workflows are unaffected: a fresh drawing starts in model
  space, and a refused layout switch leaves the current space where it was.
- **ISO 286 transition and interference holes (K, M, N, P).** `fit="P7"` used to
  raise by name. They are now derived by the standard's delta rule,
  `ES = -ei + Δ` with `Δ = IT(n) − IT(n−1)` — a *rule* over the shaft and IT
  tables already in the module, not new table data, which is why it can be
  pinned against published values (K7/M7/N7/P7 and K6/N6/P6 cross-checked by
  hand) instead of merely trusted. Unlocks the hole-basis pairs K7/h6, M7/h6,
  N7/h6, P7/h6.
- **ISO 286 interference shafts (r, s, t, u).** `fit="s6"` and `fit="u6"` — the
  standard press and drive fits — used to raise. Unlike K/M/N/P these *are* new
  table data, sub-stepped more finely than the main size steps (ISO 286 splits
  18–30 into 18–24/24–30, 30–50 into 30–40/40–50, and every step above 50 mm in
  two or three; flattening that is a silent error of tens of microns at exactly
  the sizes press fits are used at). Because it is data rather than a rule, its
  **provenance is tested**: every value is cross-checked against ISO 286-1's own
  derivation formulae, an independent source. `r` reproduces as the geometric
  mean of `p` and `s` on **24/24** steps — with `p` pinned in this module before
  `r` existed — and `s`/`t`/`u` track their IT7-based formulae within 1 µm on
  every step from 18 mm up. The five places where the standard's rounding puts a
  published value 2–3 µm off its own formula are listed by name in the tests, so
  a reader holding ISO 286 checks five numbers rather than ninety-five. Shaft `t`
  is undefined below 24 mm in the standard and refuses there rather than being
  extrapolated. Unlocks H7/r6, H7/s6, H7/t6 and H7/u6.
- **Headless performance lane** (`benchmarks/perf_suite.py` +
  `render_perf_chart.py`): fixed wall-time workloads (2k individual creates,
  10k build/export/reopen roundtrip, 10k region query, full premium quality
  pass) measured through the same backend methods the MCP tools call;
  machine fingerprint recorded, self-measurement only.
- **Task-matrix chart** (`benchmarks/render_matrix_chart.py`): tasks × servers
  status heatmap rendered from the published live-run reports, so coverage
  gaps are visible per capability instead of one folded score.
- **Tool discovery** (`DISCOVERY_MODE=search`, default `off`): replaces the
  advertised catalog with `search_tools` + `call_tool`, so a client sees two
  tools instead of 127. Ranking layers an AutoCAD command / synonym corpus
  (`discovery/aliases.py`, 132 tools · 148 command tokens · 583 synonyms) over
  fastmcp's BM25 index, and hits are returned as one compact line per tool
  rather than full JSON schemas. Measured on a 66-query golden set with a
  15-query holdout: **top-1 62/66 vs 23/66** for stock BM25, top-3 64/66 vs
  33/66 (holdout top-3 14/15 vs 9/15); stock returns *nothing at all* for 21 of
  the 66, and this transform for none of them — that gap is the corpus, not the
  ranking, and no BM25 parameter closes it. (Both sides moved slightly during
  the release as tool docstrings were rewritten: better prose feeds *both*
  indexes, which is why the headline is the top-1 gap rather than a ratio
  between two scores over a shared corpus.) A `risk` ceiling
  keeps destructive tools out of read-only answers and says when it hid one.
  Off by default — flipping it changes what every connected client sees on
  `list_tools`, which needs its own migration note.
- **`@cad_tool` — a discovery-metadata channel next to the tool contract.**
  A decorator written *above* an `mcp.tool` registration attaches a `cad` key
  to that tool's MCP `meta`: a one-line `summary` for a search hit's preview,
  a `cost` from a closed set (`read`, `safe`, `mutate`, `destructive`,
  `escape`) for clients that want to gate or colour-code the surface before
  calling, and the tool's AutoCAD command names and synonyms — imported from
  `discovery/aliases.py`, never restated, so the corpus stays the one edit
  site. `tags=` and `annotations=` deliberately stay on the `mcp.tool` call, so
  the channel cannot perturb `_tool_groups()` or the registration-counting
  release gate. Misuse fails at import rather than silently no-opping: an
  unknown cost raises, and a decorator written below the registration (or on a
  bare function) raises with the ordering rule in the message. Applied to the
  three `transaction_*` tools so far; every other tool still falls back to the
  first line of its docstring in search results, which is why the rollout can
  be section-by-section instead of a 131-tool patch.
- **`EZDXF_CALL_TIMEOUT`** (default `120`s, `0` disables): per-call timeout for
  the headless backend, mirroring `COM_CALL_TIMEOUT`. Every ezdxf method runs
  under one document lock, so a single hung call previously deadlocked every
  later tool call.
- **`uv.lock` — the dependency graph CI actually installs.** 95 packages,
  resolved universally over `requires-python >=3.11`, so one file serves the
  Linux 3.11/3.12 matrix and the Windows leg (platform-only packages like
  pywin32 carry markers). Every lint and test lane now installs with
  `uv sync --locked`, which *fails* rather than re-resolving when the lock and
  `pyproject.toml` disagree — a fresh CI run can no longer pick up an upstream
  release nobody chose, and the locked set is the set the suite is green on.
  The `package` job stays unlocked on purpose: it is the only job that proves
  the published dependency *ranges* still resolve for a plain
  `pip install autocad-mcp-pro`. The `uv.lock` line in `.gitignore` (added when
  the committed lockfile was stale) is gone with it — a lockfile git ignores is
  not a lockfile.
- **Scheduled floating-dependency job** (`.github/workflows/deps-floating.yml`):
  weekly and on demand, it ignores the lock, resolves the declared ranges fresh
  on 3.11/3.12 and re-runs the suite. Locked lanes cannot be broken by an
  upstream release, which also means they cannot *notice* one; this is where a
  range ceiling that needs moving shows up. It never gates a pull request.
- **Benchmark matrix v3 — five tasks that can fail.** The v2 matrix scored this
  server 10/10, which was not evidence of anything: every task in it exercised
  something the server was built around, so full marks were the only available
  outcome. `benchmarks/tasks_v3.py` keeps the v2 ten byte-for-byte (their ids
  and weights key the published v1.4 reports) and adds `tool_discovery`,
  `token_budget`, `hatch_islands`, `selection_filter` and
  `measure_from_handle`. Each verifies against a value derived independently of
  the code under test — a closed-form area, entities placed on purpose, a token
  ceiling fixed before the measurement. **Three of the five failed while being
  written**, and the defects they found are in *Fixed* below.
  Two are renamed from the release plan, and the renames are the point rather
  than bookkeeping. `tool_discovery_bilingual` lost its "bilingual": the
  Turkish query normalizer was written and then removed, because this
  repository is English throughout and the Turkish was inferred from the
  author's chat language, not required by the product — a task named
  "bilingual" would benchmark a capability that does not exist.
  `measurement_massprops` became `measure_from_handle`: no centroid or
  moment-of-inertia tool ships, since F2 was cut to `entity_measure`.
  The competitor reports are **not** re-scored. Those servers were pinned and
  run against v2 at v1.4 and have never been asked these five questions; the
  matrix chart renders the five rows blank with a `not run` legend entry rather
  than zero, because an invented zero is indistinguishable from a measured one.
  `run_competitors` grows `--matrix v2|v3` (v2 stays addressable so a v1.4
  report can be reproduced rather than only described), records which matrix
  ran in the report, and grows `--publish`, which writes the artifact-path-free
  file that `results/published/` holds — that sanitisation used to be a manual
  edit.
- A capability refusal in the benchmark runner is now recorded `unsupported`
  instead of `fail`. The result enum has carried `unsupported` since v2 for
  exactly this, but a refusal is still an exception and was being caught by the
  generic handler — "your engine cannot reach this" and "your server got this
  wrong" are different findings about a competitor.
- Five checks added to `correctness_suite.py` (26, was 21), and the two kinds
  read differently in the A/B table. Three measurement checks are new
  capability — v1.4.0 misses them by not having the method (`miss → pass`) —
  and are there because all three broke during this release. Two dimension
  checks are repaired defects: v1.4.0 has the methods and gets them wrong
  (`fail → pass`). Result against v1.4.0: 21/26 → 26/26, five fixed, zero
  regressed.

### Changed

- **The backend contract is 19 modules instead of one 1854-line file, and
  optional methods are declarative.** `backends/base.py` held a single ABC with
  112 abstract methods; adding one made *both* backends fail at instantiation
  until both were written, and two test modules construct `ComBackend` at
  import time, so a half-finished contract edit took the entire suite red. The
  interface now lives in `backends/contracts/`, one module per domain, split
  along the section markers that were already in the file — the seams were not
  re-decided. `AutoCADBackend` is the composition of all 19 and stays the
  import surface, so `from backends.base import AutoCADBackend` and every
  `isinstance` check are unchanged, and the abstract-method count is identical
  before and after.
  The split surfaced something the file's name hid: roughly 740 of those lines
  are shared *concrete* implementation (premium meta-tools, GD&T, the settings
  facade), not interface. Those slices are mixins and are labelled as such
  rather than presented as pure contracts.
  New `backends/capability.py` carries `@capability(key, reason=...)`, which
  marks a contract method optional and supplies a default. The default
  **raises** `UnsupportedCapabilityError` rather than returning a quiet
  `{"ok": False}` — a method nobody implemented has to be indistinguishable
  from a deliberate refusal, not from a soft failure, which is the distinction
  this release spent a milestone restoring. A key used by the decorator must be
  declared in *both* capability maps or `tests/test_capability_contract.py`
  fails; without that gate the decorator would just be a way to ship refusals
  `system_capabilities` never mentions, which is worse than the build break it
  replaces. Its first user is COM's `entity_change_space`, which now inherits
  the refusal instead of hand-rolling it. The refusal vocabulary moved into
  this module as well (the contracts need it and `base` imports the contracts);
  `backends.base` re-exports it, so there is still exactly one exception class
  object in play.
- **BREAKING (env): the `core` tool profile was removed.** `TOOL_PROFILE` is
  now `lean` or `full`; the discovery layer above solves the crowded-surface
  problem `core` existed for. `TOOL_PROFILE=core` still boots — it falls back
  to `full` with a warning naming the removal, and `system_about` reports the
  requested profile alongside the applied one.

- **BREAKING (install): `ezdxf>=1.4` is now the floor** (was `>=1.3`). The 1.4
  line is the only one this release is developed and tested against; 1.3 is no
  longer exercised by CI and is unsupported. Upgrade with
  `pip install -U "ezdxf>=1.4"`.
- **Runtime pins narrowed.** A published library cannot pin exactly and stay
  co-installable, so what a user's `pip install` gets is still a range — and a
  range is the only guard that install has. They are therefore deliberately
  narrow. (`uv.lock`, above, is the exact guard on *our* side; the two are not
  substitutes for each other.)
  - `fastmcp>=3.0` → `fastmcp>=3.4.5,<3.5`. `fastmcp` is now a thin
    meta-package (`Requires-Dist: fastmcp-slim[client,server]`) and 3.x moves
    fast enough that a new minor can reshape the API the server is built on.
    Tested against 3.4.5; the ceiling moves only after the suite is re-run.
  - `ezdxf>=1.3` → `ezdxf>=1.4,<2` (see the BREAKING note above).
  - `pydantic>=2.0` → `pydantic>=2.0,<3`, so a future pydantic 3 cannot be
    pulled into an install of this release.
- **Rendering requires an extra — documented, not changed.**
  `ezdxf.addons.drawing.__init__` imports `.frontend`, which does an
  unconditional top-level `import PIL.Image`. Pillow is therefore a hard
  requirement of *every* ezdxf render path (PNG screenshot and PDF export
  alike), not just of the COM window-capture path it is listed under. A bare
  `pip install autocad-mcp-pro` resolves 77 packages with neither Pillow nor
  matplotlib, so it cannot render at all; `[pdf]` (or `[full]`) is required,
  and satisfies Pillow transitively via matplotlib. Note that `[com]` alone
  installs Pillow but not matplotlib, so the ezdxf backend still cannot render
  under it. Pillow deliberately stays out of the core dependencies for now.
- **CI lint pinned to `ruff==0.16.1`** (was an unpinned `pip install ruff` /
  `ruff>=0.8`). The version is now written in exactly one place —
  `[dependency-groups] dev` — and frozen there by `uv.lock`; the workflows
  install ruff *from* the lock instead of restating the pin, so the three
  copies that had to be bumped together no longer exist.
- **Dev toolchain moved to a PEP 735 dependency group.** `[tool.uv]
  dev-dependencies`, which uv now warns is deprecated, became
  `[dependency-groups] dev` — same four tools, byte-identical resolution. The
  standard spelling is also understood by pip >=25.1, so `pip install --group
  dev` gives contributors who do not use uv the same list without a second
  copy of it.
- **CI and the release gate install from the lock.** `lint` (dev group only —
  ruff without building the project), `test-linux`, `test-windows` and the
  `release.yml` test job now run `uv sync --locked` + `uv run --no-sync`, with
  uv itself pinned in the workflow env for the same reason ruff is. `package`,
  `docker` and `registry-validate` are unchanged. `benchmark.yml` is locked
  too: that lane publishes evidence, and an ezdxf release moving our own
  numbers between runs would make the diff unattributable.
- README rebuilt: PyPI-first install, a four-lane benchmark section with
  Python-generated visuals (scores, task matrix, performance, source review),
  v1.4 feature table, updated client/config/roadmap sections.
- `render_live_chart` now skips non-task-matrix reports so multiple lanes can
  share `benchmarks/results/published/`.

### Fixed

- **The COM paths were run against a live AutoCAD 2026, and four of them were
  broken.** Every COM implementation added in this release was written against
  the ActiveX reference and shipped labelled unverified. Executing them against
  a real seat — a fresh instance, a throwaway drawing, nothing saved — found
  four defects that no amount of reading would have caught, one of them
  pre-existing:
  * **`AcadPViewport` has no `ViewCenter` member.** `viewport_list` died on it
    outright. Worse, `viewport_create` has carried the same line since v1.4
    wrapped in a bare `except: pass`, so it raised on every single call and
    said nothing — every COM viewport has been looking at the model origin
    instead of the `view_center_x/y` the caller asked for. The member is
    `Target`, and the failure is no longer swallowed.
  * **`ModelSpace.AddWipeout` does not exist.** ActiveX has no wipeout
    constructor at all; WIPEOUT is a command. Now a typed refusal
    (`capability: "wipeout"`) instead of an `AttributeError`.
  * **`AcDbMText.BackgroundFillColor` does not exist** — it raises on read as
    well as on write, with an int and with an `AcCmColor` object alike.
    `BackgroundFill` (the on/off flag) does work, so `text_set_background`
    enables and disables the mask on COM and refuses a *colour* request with
    `capability: "mtext_background_color"`, rather than switching the fill on
    in the wrong colour and reporting success.
  * `analysis_list_properties` called a helper that does not exist on the class.
  After the fixes, all 25 verification calls behave correctly: 18 succeed, 1
  refuses per-argument (deleting model space), and 6 refuse with the capability
  keys they declare. The remaining COM refusals
  (`chspace`, `revcloud`, `boundary_trace`, `hatch_edge_paths`) were confirmed
  to hold rather than leak a result. The source comments now say VERIFIED with
  the date instead of UNVERIFIED.
- **The headless renderer *does* project model content through viewports.
  v1.4 said it did not.** `viewport_render` was declared unsupported on ezdxf,
  every `drawing_export_pdf(layout=...)` response carried the note "projecting
  model content through viewports is COM-only", and the README table agreed —
  so a caller with no AutoCAD was told to go get one for something that already
  worked. Measured on ezdxf 1.4.4: a sheet holding *nothing but a viewport*
  renders the model geometry that viewport looks at, two viewports at 1:1 and
  1:2 render the same circle at two sizes in the right places, and geometry
  outside a viewport's window is correctly left out. Understating a capability
  is the same class of untruth as overstating one, and it is the more expensive
  one for the user. What is genuinely missing is the viewport *border*, which
  the headless renderer does not draw — the capability now says exactly that
  (`reason: viewport_borders_not_drawn`), and the old note's claim that
  "viewport frames are rendered" was wrong in the other direction. Two tests
  that pinned the false claim were corrected rather than deleted, and the
  replacement proves the behaviour by subtraction: the sheet renders less ink
  once the model geometry is gone.
- **Layout names are matched case-insensitively, the way the engine underneath
  already matched them.** ezdxf resolves layout names case-insensitively in
  `get`, `new`, `rename`, `delete` and `set_active_layout` — but `names()`
  returns the *stored* spelling, so every guard written as
  `name in doc.layouts.names()` disagreed with the code it was guarding, in both
  directions. `layout_create("SHEET")` next to an existing `Sheet` walked past
  its own duplicate check and died on a raw `DXFValueError` from inside ezdxf;
  `layout_set_current("SHEET")` reported "Layout not found" for a tab that was
  right there. Both shipped in v1.4. One resolver now backs
  create/set_current/delete/rename/copy and `viewport_list` on both engines, and
  the current space is stored in the canonical spelling. Re-spelling a layout's
  own name (`Sheet` → `SHEET`) is a rename onto itself rather than a collision;
  ezdxf's `rename` refuses that outright, so it goes through a staging name.
- **`entity_change_space` refuses an entity that lives on another sheet instead
  of half-applying.** `move_to_layout` raises `DXFValueError` from deep inside
  ezdxf when the entity is not in the source layout — and it raises *after* the
  transform has already been applied, so the failed call left the line rescaled
  and translated by a viewport it never moved through, still sitting on the
  wrong sheet. Membership is now checked before anything is written, and the
  refusal names the space the entity is actually in. One bad handle in a batch
  no longer aborts the good ones.
- **BREAKING (behavior): coordinates crossing the boundary are now WCS. They were
  not.** DXF stores CIRCLE, ARC, LWPOLYLINE, TEXT, INSERT and a dimension's text
  position in a per-entity frame derived from the entity's `extrusion` vector —
  and the string "extrusion" appeared *nowhere* in this repository, so every such
  read returned a raw attribute rather than a world coordinate. Mirroring flips
  extrusion to `(0,0,-1)` and leaves the stored x unnegated, so after the
  server's own `entity_mirror` across the Y axis a circle drawn at (30, 20)
  reported `center [30.0, 20.0]` while the **same response's** `bounding_box`
  said `min [-35, 15] / max [-25, 25]`: the payload contradicted itself by 60 mm,
  silently, with no error anywhere. `point_from_snap` returned points on the
  wrong side of the axis, `entity_select_smart` found the entity where it wasn't,
  and `entity_offset` placed the new circle 60 mm away *and* grew it when asked
  to shrink. Reads are now normalised at the single `_entity_info_dxf` funnel, so
  all 38 call sites and every consumer of `EntityInfo.properties` are fixed with
  them. The write paths (`entity_edit_geometry`, `entity_offset`) got the
  matching inverse — mandatory, not cosmetic: read and write were *equally*
  wrong, which made a read-then-write round trip an accidental no-op, and fixing
  reads alone would have turned that no-op into a real 60 mm move.
  - **Not touched, deliberately:** LINE, MTEXT, ELLIPSE, SPLINE and POINT carry
    an `extrusion` attribute too and were always correct (ezdxf makes their
    `ocs()` a pass-through); `entity_rotate` was never affected — it preserves
    extrusion `(0,0,1)`. The trigger was `entity_mirror` and foreign DXF files.
    A dimension's `defpoint` is DXF group code 10 and WCS already; only
    `text_midpoint` (code 11) is in the entity frame.
  - **Tilted planes.** An entity whose plane is not parallel to WCS XY cannot be
    described by an xy pair — a tilted circle of radius 5.0 has a 5.0 × 3.99
    footprint. `entity_get` reports the WCS centre plus `plane_normal` and
    **omits** `radius` / `start_angle` / `end_angle` rather than reporting a
    number that is not true in the frame the rest of the dict uses, and
    `entity_edit_geometry` refuses with `capability: "ocs_tilted_plane"`,
    pointing at `entity_move` (which transforms the whole entity and preserves
    its plane). New capability keys `ocs_normalized` and `ocs_tilted_plane` on
    both backends make the boundary pre-checkable via `system_capabilities`.
  - **Named residuals** (in the `ocs_normalized` reason rather than left silent):
    the OCS z component is not reported, HATCH boundaries are not reported at all
    (silence, not a wrong number), and TEXT `rotation` stays in the entity frame
    because a mirrored TEXT is mirror-imaged and no single scalar angle expresses
    that — measured 29.999 reported against a true WCS 150.0.
  - **COM:** ActiveX returns WCS natively for every point property *except*
    `AcDbLWPolyline.Coordinates`, which is OCS; that one read is now translated.
    Verified only against a fake COM object — this machine has a live AutoCAD and
    connecting to it during development is forbidden, so the path ships
    unit-tested, not hardware-verified. `AcDb2dPolyline` is deliberately out of
    scope: its `Coordinates` may be 3D triples, and changing the stride on a
    documentation reading alone would risk a path that works today.
- **`validate_path` handed out working handles to `C:\Windows`.** Windows names
  the same file through several anchors that compare unequal. The backslash
  spellings were rejected, but `pathlib` normalises the *forward-slash* forms
  into them only after `.resolve()` — so `//?/C:/Windows/win.ini` slipped past
  the pattern check and then failed to match `C:/Windows` in the
  system-directory loop. Proven: it returned a handle that `os.path.samefile`'d
  the real file. Non-local anchors are now rejected in every spelling. This
  removes no capability anyone had: the equivalent backslash paths were already
  refused, so it only makes one policy consistent across spellings.
- **The AutoLISP guard read drawing text as code.** The symbol denylist scanned
  inside double-quoted literals, so an engineering server refused its own
  vocabulary: `(setq desc "Max load 500 kg")` matched the `load` pattern and
  `(setq layer "C:CENTER")` matched the custom-command pattern. Literals are now
  blanked before scanning. Every known-dangerous vector still blocks, including
  `(load "malicious.lsp")` and `(vl-catch-all-apply (quote command) ...)`, whose
  dangerous token is outside the quotes.
- **The command denylist matched characters, not commands.** A word-boundary
  regex over the whole string refused `MTEXT 0,0 10,10 Do not erase this part`
  — annotation text — while missing spellings AutoCAD accepts. SendCommand takes
  a *macro*: segments split on newline or `;`, and only a segment's first token
  is a verb. Matching is now verb-scoped with decorators (`_`, `.`, `-`, `'`)
  stripped, the set gained the load/run channels it was missing (`ARX`,
  `VBASTMT`, `VBAIDE`, `CUILOAD`, `SCR`, `SAVE`, `EXPORT`, `ETRANSMIT`, …), and
  the refusal now names the offending verb — previously a user whose annotation
  was refused had no way to tell why. Single-letter aliases stay out on purpose:
  `E` is the ERASE alias but also ZOOM's Extents option, so blocking it would
  refuse `_ZOOM\nE\n`, the canonical zoom-extents macro.
- **README no longer claims the denylist is a security boundary.** It said
  "blocked by default, with regression tests for known bypass patterns"; the
  bypasses above had no tests, and no denylist over a command language that
  loadable modules extend can be complete. It now says what the guard is — a
  guardrail against an agent accidentally issuing `ERASE ALL` — and names the
  actual boundaries: `ALLOWED_PATHS`, not exposing the server over the network,
  and the typed tools. The same sentence is in the two tool docstrings, so it
  reaches the model that calls them. *Not* changed: blocking `INSERT`/`SAVEAS`
  as command strings while `block_insert`/`drawing_save_as` exist was flagged as
  incoherent and is not — the policy is scoped to the free-text channel, and the
  typed tools take validated arguments where the raw commands take file paths.
  The reasoning is recorded in `security.py` so it is not re-opened.
- **`config.settings.backend` was a control that controlled nothing.**
  `_make_backend` read `os.environ["AUTOCAD_MCP_BACKEND"]` directly, so the
  setting had no production reader at all and
  `monkeypatch.setattr(config.settings, "backend", "ezdxf")` — which two tests
  used — read like a safety guard while doing nothing. On a developer machine
  with AutoCAD open that meant `auto`, i.e. attaching to the operator's live
  unsaved drawing and creating entities in it. `Settings.backend` is now a
  read-only property that reads the environment live, `_make_backend` goes
  through it, and the test suite pins the headless backend for every test via an
  autouse fixture (opt out with `@pytest.mark.live_backend`). Assigning to
  `config.settings.backend` now raises `AttributeError` instead of silently
  protecting nothing. `monkeypatch.setenv` — what the correct call sites already
  used — is unaffected.
- **A capability refusal now survives the trip to the client.** The backends
  always knew exactly why they could not do a thing — they raise
  `UnsupportedCapabilityError(capability, message)`, where `capability` is a key
  of the capability map, so `system_capabilities` answers the same question
  up-front. None of that reached anyone: `FastMCP.call_tool` rewraps a
  non-`FastMCPError` as `ToolError(f"Error calling tool {name!r}: {e}") from e`,
  and while the `from e` keeps the cause alive in-process (which is why
  `cad_batch` could classify refusals), `__cause__` does not serialise. A remote
  client got an English sentence, and telling "this backend cannot" from "your
  arguments were wrong" meant substring-matching prose. A refusal now returns
  `is_error: true` with
  `{"ok": false, "kind": "unsupported", "capability", "error", "tool",
  "backend"}` in `structured_content`, produced by one new
  `CapabilityRefusalMiddleware` — no tool signature changed. *Not* done by
  rebasing the exception on `FastMCPError`: that was measured and changes
  nothing a client can observe, since neither the payload nor the error flag
  travels with the exception class. Non-capability errors are byte-for-byte
  unchanged. `UnsupportedCapabilityError` moved to `backends/base.py` (still
  importable from `backends.ezdxf_backend`, and the *same* class object) so both
  backends raise one type, and the five headless `solid_*` methods now raise it
  instead of returning `{"ok": false}` — same payload, but `is_error` stops
  being quietly `false` on a call that did nothing.
- **BREAKING (behavior): `cad_batch` counted a step that declined *by value* as
  a success.** A tool that returns `{"ok": false, ...}` rather than raising was
  recorded `status: "ok"` and folded into `succeeded`, so a batch could report
  `succeeded: 12` over work that never happened — the same class of lie as the
  DWG bytes and the unverified rollback. Such a step is now `status: "error"`
  with kind `unsupported` (when it names a capability) or `refused`, counts
  toward `failed`, and honours `on_error` like any other failure. Batches that
  relied on, say, a no-op `drawing_redo` passing silently will now stop or roll
  back; that is the point. **Read-only tools are excluded on purpose**:
  `validation_check` / `drawing_critique` / `drawing_preflight` answer
  `{"ok": len(issues) == 0}`, so their `ok: false` means *I found something*,
  not *I could not run* — reading the two the same way would let a read-only
  check trigger a rollback that destroys the batch's own geometry. The
  discriminator is the `readOnlyHint` annotation each tool already publishes.
- **BREAKING (behavior): `drawing_audit` said "0 errors" about documents it had
  just silently rewritten — and then threw the repairs away.** `Drawing.audit()`
  is not a read: it fixes every fixable problem in place, so `auditor.fixes` is
  a log of mutations already applied. The tool discarded that list entirely,
  never marked the document dirty — so `drawing_close(save=True)` saw a "clean"
  document, skipped the save, and the repairs it had just made were lost — and
  rendered the errors it *did* report through `str()` on an `ErrorEntry` that
  defines no `__str__`, i.e. as `<ezdxf.audit.ErrorEntry object at 0x...>`.
  The tool now returns `repaired` / `fixes` / `fix_count` alongside
  `errors` / `error_count`, both as `{code, name, message, handle}` dicts, and
  marks the document dirty when it repaired something. **The behavioral break:
  an audit that repairs now leaves unsaved changes, so a following
  `drawing_close(save=True)` rewrites the file where it previously did not.**
  That is the repair finally persisting; the tool was already annotated
  `readOnlyHint: false` / `cost: mutate`. **Audit can delete entities** — an
  entity whose owner handle points nowhere is removed, not patched — and with
  the dirty flag fixed, that deletion now reaches disk on the next save where
  before it was accidentally discarded. ezdxf reports those particular fixes
  without the entity attached, so `handle` is `null` for them; the message names
  the type and handle in prose. Read `fixes` before saving. On the COM backend `_AUDIT Y` likewise
  repairs, but AutoCAD returns nothing machine-readable over `SendCommand`, so
  the counts come back as `null` with `detail: "unavailable"` and
  `capability: "audit_detail"` — never `0`, which would claim nothing was wrong
  — plus the path of the `.adt` log. That backend also no longer leaves the
  user's `AUDITCTL` permanently set to 1.
- **BREAKING (behavior): the ezdxf backend no longer writes DXF bytes into a
  `.dwg` name.** `drawing_save("part.dwg")` previously called `doc.saveas()`
  and produced a file whose contents did not match its extension — AutoCAD
  cannot open it, and nothing in the response said so. Both save paths now
  refuse, driven by the extension, so `drawing_save_as("x.dwg", fmt="dxf")` is
  refused too. Refusals carry a machine-readable
  `{"ok": false, "capability": "dwg"}`, and `dwg` is now a declared capability
  in both backends (ezdxf: unsupported; COM: native). Save as `.dxf`, or use
  the COM backend for DWG.
- **`drawing_open` no longer pretends it can read DWG.** ezdxf detects format
  by content, so a DXF renamed `.dwg` (exactly what the bug above produced)
  opened "successfully" as a document that does not exist in that format,
  while a genuine DWG died with a raw `OSError` blaming the file rather than
  the backend. The tool now refuses `.dwg` unless the active backend declares
  the capability, and its parameter text no longer advertises DWG
  unconditionally. Workflows holding files produced by the old `drawing_save`
  bug must rename them to `.dxf`; the error message says so.
- **`TOOL_PROFILE` was a silent no-op whenever a tool transform was attached.**
  `_registered_tools()` read the post-transform tool list, so with a transform
  in place it saw 2 tools instead of 131 — the lean profile computed 0
  disabled tools and `system_about` reported a collapsed inventory. It now
  reads the underlying registry.
- **`ruff format --check .` failed on unmodified `main`.** Ruff 0.16 began
  formatting Python code blocks inside Markdown, so the committed
  `docs/superpowers/plans/2026-07-17-repository-cleanup-readme-benchmark.md`
  started failing the format gate with no source change. Reformatted the file
  and pinned the CI ruff version so a future release cannot re-break the gate
  the same way.
- `release.yml` now runs `ruff format --check .` alongside `ruff check .`,
  matching the CI lint job — the release gate previously skipped formatting.

- **Every headless diameter and radius dimension reported the wrong number, at
  default settings.** `add_diameter_dim` / `add_radius_dim` measure to the point
  they are given; `leader_length` is a *text placement*. Passing
  `centre + (radius + leader)` as that point made the leader part of the
  measurement, so a true ⌀40 bore was dimensioned **60** with the default
  `leader_length=10`, and **108** at 34. Radius callouts were `leader_length`
  too large the same way. This is the worst class of defect for this project —
  a wrong number, silently, on the one part of a drawing that exists to carry
  numbers — and it shipped in v1.4.0 and earlier. Now the size comes from
  `radius` and `location` only places the text.
  The live COM backend was never affected: ActiveX's `AddDimDiametric` takes
  the two chord points and the leader length as separate arguments, so the two
  engines had been disagreeing by `2 × leader_length` on the same call.
  Found while building the sheet in the README's hero image, whose own bore
  callout was wrong. Two checks added to `correctness_suite.py` (26, was 24);
  both report `fail → pass` against v1.4.0 rather than `miss → pass`, which is
  what distinguishes a repaired defect from new capability in that lane.
- **A HATCH could not be measured, and the refusal listed HATCH as
  measurable.** `entity_measure` had no HATCH branch, so a hatch fell through
  to the generic error — whose own text named HATCH among the measurable types.
  Whichever half was right, the pair could not both be. It now walks the
  boundary paths and reports the area the hatch actually *fills*: outer loops
  minus the islands inside them, 300 rather than 400 on a 20×20 square with a
  10×10 island. Nesting depth is computed by containment rather than read off
  the boundary flags, because DXF marks a path external or outermost and never
  "two levels down" — and depth is exactly where AutoCAD's three island styles
  stop agreeing. `hatch_style` comes back with the number so it can be read
  against the drawing's own setting. On a curved edge `flatten_tolerance` is
  not the accuracy knob it looks like: ezdxf hands boundaries over as cubic
  Beziers, so tightening it converges on the Bezier's area, ~0.028% above the
  circle it stands for, which is why `exact` goes False and stays False.
- **`boundary_trace` and `entity_measure` disagreed about the same shape.**
  The boundary tools reported an area taken from a copy of the loop flattened
  at a fixed 0.01 sag while `entity_measure` read the stored bulges
  analytically: 139.2177 against 139.2699 for one figure, with nothing in
  either payload marking one as the approximation. The polyline the boundary
  tools store carries exact bulges, so the exact answer was always available;
  both now read it and round to the same six decimals. The flattened figure
  survives only where it is harmless — ranking candidate loops, where a 0.01%
  error cannot change which is smallest.
- **A circle stored as two bulged vertices measured 0.0.** Two semicircular
  arcs joined into a circle store as a *two*-vertex closed LWPOLYLINE with both
  bulges 1.0 — what AutoCAD writes for a JOINed pair of arcs, and what
  `boundary_from_entities` builds from any two-arc loop. `polygon_area_perimeter`
  treated fewer than three vertices as degenerate and returned zero, when the
  shoelace over two points is genuinely 0 and the two circular segments are the
  entire shape. Only zero or one vertex bounds nothing now.
- **The v1.4 → v1.5 performance comparison was measured, and it is not
  flattering.** The published perf report was recorded on CPython 3.14 and the
  new one on 3.11.15, which alone accounts for nearly all of an apparent 2–6×
  "speedup". Re-running v1.4.0 in a worktree on the same interpreter with the
  same suite file gives the real picture: entity creation is **28% slower**
  (245 → 315 ms for 2,000 lines) and the 10,000-line roundtrip **22% slower**.
  Fully attributed — `EZDXF_CALL_TIMEOUT=0` returns v1.5.0 to 244.8 ms and
  1,679 ms, v1.4.0's numbers exactly. The whole cost is the per-call
  `asyncio.wait_for` this release wrapped around every headless call so that
  one hung ezdxf call can no longer wedge a server whose document lock is a
  single `asyncio.Lock`. Deliberate trade, documented knob, and on the 1.6
  roadmap to win back.

_The five items carried into this release from 1.4 are all done: screenshot
overlay + handle grounding, ezdxf undo/redo, COM `block_create_from_entities`,
ISO 286 transition/interference holes, and titleblock on paper-space layouts.
ISO 286 shafts r/s/t/u are deliberately **not** done — they need table data
nothing here can derive, and the refusal says so rather than guessing._

## [1.4.0] — 2026-07-23

Release infrastructure + the roadmap features shipped together. **474 tests,
Ruff lint- and format-clean. Tool count: 122 → 131.**

### Added

- **CI restored and extended** (`.github/workflows/ci.yml`): ruff lint/format,
  Linux test matrix (3.11/3.12) with coverage, a **Windows leg** running the
  mocked-COM suite on the platform the COM backend targets, a package job
  (build + twine check + clean-venv wheel smoke incl. `autocad-mcp --help`),
  a Docker build/smoke job, and MCP `server.json` schema validation.
- **Release pipeline** (`.github/workflows/release.yml`): tag `v*` → test gate
  → tag/version match check → build → **PyPI trusted publishing** (OIDC) →
  GitHub Release with artifacts.
- **MCP registry manifest** `server.json` (schema 2025-12-11, name
  `io.github.u-c4n/autocad-mcp`, PyPI package) plus the `mcp-name` ownership
  marker in the README and an `autocad-mcp-pro` console alias so
  `uvx autocad-mcp-pro` works directly.
- **Live competitor benchmark lane**: a generic black-box MCP-stdio driver
  (`benchmarks/adapters/mcp_stdio.py`), reproducible pinned checkouts
  (`benchmarks/competitors_env.py`), and adapters for
  **puran-water/autocad-mcp** and **beiming183-cloud/AutoCAD-MCP** that run
  the same 10-task matrix over stdio with harness-side DXF verification.
  Published results (`benchmarks/results/published/`): autocad-mcp-pro
  **100.0**, beiming183 **50.0**, puran-water **45.0**; rendered to
  `docs/assets/autocad-mcp-livebench.svg`. Weekly/dispatch CI workflow.
- **Tool profiles** (`TOOL_PROFILE=lean|core|full`): capability-aware
  discovery applied in the server lifespan — `lean` is a curated ~46-tool
  drafting core, `core` hides raw escape hatches and long-tail tools, `full`
  (default) exposes everything. Reported by `system_about`.
- **Paper space / layouts**: `layout_list`, `layout_create`,
  `layout_set_current`, `viewport_create` (scaled model viewports) on both
  backends, and `drawing_export_pdf(layout=...)` for plotting a layout.
  Headless limitation is explicit: viewport model-content projection is
  COM-only (`viewport_render` capability).
- **ISO 286 limits and fits** (`engineering/fits.py`): authored table data
  (IT4–IT11, sizes 1–500 mm; shafts d/e/f/g/h/js/k/m/n/p, holes D/E/F/G/H/JS)
  with `fit_lookup("H7", 20.0)`; `dimension_linear/radius/diameter` gained a
  `fit` parameter that resolves deviations from the measured nominal and
  appends the fit code to the dimension text. Out-of-scope letters raise a
  clear error naming the supported set.
- **Opt-in native 3D solids** (`ENABLE_3D=true`, COM only): `solid_box`,
  `solid_cylinder`, `solid_extrude`, `solid_revolve`, `solid_boolean`.
  Hidden from discovery and rejected while disabled; ezdxf reports the honest
  `solid_3d` capability boundary (ACIS cannot be generated headlessly).
- **Release-consistency test suite** (`tests/test_release_consistency.py`):
  pyproject ↔ version.py ↔ CHANGELOG ↔ README snapshot ↔ server.json versions,
  README tool/resource/prompt counts vs live registrations, per-section header
  counts vs decorators, Dockerfile COPY vs wheel `only-include`, and the
  README `mcp-name` marker.

### Changed

- **pyproject metadata completed** for PyPI: SPDX license + license file,
  authors, keywords, classifiers, project URLs.
- **`build_dim_override`**: `deviation`/`limit` modes now pass DIMTM signed
  (`-lower`) instead of `abs()` — double-positive ISO 286 fits (e.g. p6
  +0.035/+0.022) render correctly; the legacy "positive magnitude = minus
  deviation" contract is unchanged.
- **Capability maps**: `paper_space` is now native on both backends;
  new `viewport_render` feature (COM native / ezdxf unsupported);
  `solid_3d` reflects the ENABLE_3D gate on COM.
- README rebuilt around the live-run benchmark lane; CI/PyPI badges added;
  `ruff format` applied repo-wide and enforced in CI.

### Fixed

- **Dockerfile shipped a broken image**: `engineering/` and `version.py` were
  never copied, so engineering tools failed in containers. Both are now
  copied (plus README/LICENSE for metadata) and the healthcheck imports the
  engineering package.
- `server.py` section-header tool counts and the CLAUDE.md inventory were
  stale (six sections drifted); both now match the live surface and are
  locked by tests. Removed the reference to the non-existent
  `.claude/skills/` directory.

## [1.3.0] — 2026-07-17

Closed-loop quality, production annotation, and auditable delivery. **415 tests,
Ruff lint-clean. Tool count: 116 → 122.**

### Added

- **`drawing_preflight`** normalizes production requirements, reports missing or
  conflicting facts before geometry starts, and emits a deterministic SHA-256
  spec hash that `drawing_plan` can enforce.
- **`drawing_refine`** runs a bounded `critique → repair → re-critique` loop.
  Each repair round is isolated in its own transaction and rolls back if score
  regresses or hard-error count increases. Construction, duplicate, layer
  color/lineweight, untrimmed endpoint, and dimension-overlap repairs are
  supported; undefined GD&T datums remain explicitly manual.
- **TABLE and MLEADER semantics** via `entity_create_table` and
  `leader_create_mleader`: native ActiveX entities on COM and deterministic,
  portable LINE/LWPOLYLINE/MTEXT composites on ezdxf. Composite creation
  returns child handles and a logical ID; after DXF reopen, inspection uses the
  persisted standard child entities rather than a native TABLE/MLEADER object.
- **`system_capabilities`** and shared typed backend capability maps distinguish
  native, rendered, composite, snapshot, shared, and unsupported features.
- **`drawing_deliver`** creates DXF/PDF/PNG bundles, runs validator + critique,
  re-opens the canonical DXF for entity/type/layer/bounds parity, hashes every
  artifact, and writes `manifest.json` plus `validation.json`. Failed gates keep
  artifacts for diagnosis but never report delivery success.
- **Benchmark v2**: ten fixed vendor-neutral tasks, an adapter interface, an
  ezdxf/COM reference adapter, cooperative per-task timeout reporting,
  fixed-matrix coverage-aware scoring, machine-readable statuses,
  runtime/environment/capability metadata, and artifact SHA-256.

### Changed

- ezdxf transaction snapshots are isolated from user-facing undo history, so a
  refiner rollback cannot consume an unrelated undo entry.
- Package metadata is the canonical version source used by `system_about` and
  delivery manifests.
- `benchmarks/correctness_suite.py` now shows usage for argless/`--help` calls
  instead of raising a traceback.

### Fixed
- **Install from source** — `pip install -e ".[full]"` failed at
  "Preparing editable metadata" because the flat layout has no directory
  matching the project name, so hatchling could not infer which files to ship.
  `pyproject.toml` now declares the wheel file selection explicitly
  (`server.py`, `config.py`, `security.py`, `backends/`, `engineering/`). (#2)
- **`dimension_linear` on the COM backend** — always crashed with
  `<unknown>.AddDimLinear`: the AutoCAD ActiveX API has no `AddDimLinear`
  method. Linear dimensions are now created via `AddDimRotated` (identical
  argument order), with a mocked regression test. (#3)

## [1.2.0] — 2026-07-07

Production-ISO parity + a measurable quality moat. **360 tests, ruff-clean.** Tool count: 111 → 116.

### Added
- **`drawing_settings`** — read or change common AutoCAD drawing settings by
  friendly name (units mm/cm/m/inch/feet, linear/angular precision, LTSCALE,
  DIMSCALE, text size, point mode/size, OSMODE, fillet radius) without
  memorising system-variable names. Call with no argument for a full snapshot.
  A convenience facade over `system_get_variable` / `system_set_variable`;
  cross-backend.
- **In-place editing** — `entity_edit_text` re-labels or resizes an existing
  TEXT/MTEXT entity, and `entity_edit_geometry` re-drives a CIRCLE/LINE/ARC
  (center, radius, endpoints, arc angles) — both **preserve the entity handle**,
  so the user can adjust a drawing without delete-and-recreate. Cross-backend.
- **2D GD&T (ISO 1101 / ASME Y14.5)** — `gd_frame` draws a feature control frame
  (all 14 geometric characteristics, ⌀ zone prefix, Ⓜ/Ⓛ/Ⓢ material modifiers,
  multi-datum references) and `datum_feature` places a datum symbol. Frames are
  composed from LINE + TEXT so they render identically on **both** the COM and
  ezdxf backends (ezdxf's native TOLERANCE entity renders blank via matplotlib).
  No competing CAD MCP or surveyed text-to-CAD product ships 2D GD&T authoring.
- **GD&T datum-consistency gate** — a new `gdt` critique focus fails
  `drawing_finalize` when a feature control frame references a datum with no
  matching datum feature (a meaningless FCF per ISO 1101).
- **ISO 129 dimension tolerances** — `dimension_linear` / `dimension_radius` /
  `dimension_diameter` gain `tol_upper` / `tol_lower` / `tol_mode`
  (`symmetric` ±, `deviation` +a/-b, `limit` stacked, `basic` boxed) and a
  `text_override` (e.g. `⌀20 H7`). Toleranced production dimensions are now
  possible — previously the single biggest functional gap.
- **Scalar drawing-score + invalidity ratio** (`engineering/scoring.py`) — the
  `drawing_finalize` payload now carries a 0-100 `score`, an `invalidity_ratio`,
  and an A-F `grade` over the union of the structural validator and the premium
  critique, so drawing quality is regression-trackable (MUSE / CadBench grade an
  Invalidity Ratio, not shape).

### Fixed
- **Honesty**: the COM backend advertised a false `all_entity_types` capability
  (it authors 2D entities only, no 3D solids) — corrected to `entities_2d`.

## [1.1.1] — 2026-06-20

### Added
- **`selection_get`** (Entity Query, COM backend): reads AutoCAD's implied
  ("pickfirst") viewport selection — the entities the user highlighted with
  grips before invoking the AI — and returns their handles + `EntityInfo`.
  This lets the AI scope work to exactly what the user picked
  (`dimension_auto(selection_get()["handles"])`) instead of acting on the whole
  drawing. Resolves [#1](https://github.com/U-C4N/Autocad-MCP/issues/1) — the
  layer-juggling workaround is no longer needed. Surfaces the `PICKFIRST` sysvar
  state so an empty selection is self-explanatory. The ezdxf headless backend
  has no viewport, so it returns `ok=False` with an empty handle list (same
  shape, never raises). Tool count: 110 → 111.

## [1.1.0] — 2026-06-19

Correctness, cross-backend parity, and an **enforced** quality gate, landed across four audited
sprints (see `docs/analysis/`). 318 tests, ruff-clean.

### Added
- **Premium drafting workflow** (shared across both backends): `drawing_plan`, `drawing_critique`
  (ISO-128 focuses), `point_from_snap`, `drawing_apply_iso_layers`, `dimension_auto`,
  `entity_select_smart`, `construction_xline` / `construction_clear`.
- **Deterministic geometry** for exact OSNAP coordinates: `point_intersection`
  (line/line, line/circle, circle/circle) and `point_tangent` (external point → circle).
- **Engineering / deterministic CAD layer**: involute gear front view + section A-A,
  DIN 6885 keyed bore, ISO A3 title block, and the 8-step `drawing_finalize` validator.
- **HTTP bearer-token auth**: `StaticTokenVerifier` wired into FastMCP when `MCP_AUTH_TOKEN` is set.
- Mocked-COM test harness (`tests/test_com_backend.py` + Sprint-3/4 suites) — COM logic is now
  regression-tested on Linux CI without a live AutoCAD.
- `entity_create_mtext` rotation parameter; `dimension_auto` layer override; `bounding_box` /
  ARC `length` / TEXT-MTEXT `rotation`+`char_height` on both backends; `BlockInfo.description`.
- Security module (`security.py`), centralized config (`config.py`), `.env.example`, path
  validation, command/LISP sanitization, ruff config, pre-commit hooks, pytest-cov.

### Changed
- **`drawing_finalize` now enforces the premium critique** in addition to the structural validator:
  leftover construction geometry, non-ISO-128 lineweights, untrimmed corners, duplicate entities,
  and dimension overlap block the gate (was advisory-only). `strict_critique=True` fails on warnings too.
- **`drawing_save_as` derives the on-disk format from the file extension** — `part.dxf` writes DXF,
  not DWG. ezdxf refuses to mislabel a `.dwg` file.
- Dimension and construction layers resolve from the active layer set (iso13567 → `M-DIMEN-T-N` /
  `M-CONST-E-N`, not the hardcoded `DIM` / `CONSTRUCTION`).
- Premium meta-tools lifted into the shared `AutoCADBackend` base class (single source of truth).
- Dead code removed (`section.py`, `generate_tooth_profile`, `CommandResult`, `set_layer_active`, …).

### Fixed
- ezdxf `dimension_aligned` / `dimension_angular` raised `TypeError` (wrong ezdxf 1.4 args) — fixed.
- Full-circle polar array placed a duplicate copy over the original — fixed (divisor = count).
- `entity_offset` ignored `side_x`/`side_y` on both backends — now honored (and COM no longer leaks extras).
- COM `entity_create_hatch` built an associative hatch then deleted its boundary — now non-associative.
- Lineweight mm-vs-hundredths truncation wiped ISO-128 weights — fixed via `normalize_lineweight`.
- **`view_screenshot` / `drawing_export_pdf` could SIGSEGV** (matplotlib GUI backend in a worker thread)
  — now render headless via `Agg` (`Figure` + `FigureCanvasAgg`).
- `entity_select_smart` `length_range` silently rejected all ARCs (no `length`) — fixed.
- Gear tooth profile self-overlapped for high tooth counts (z ≥ ~42); section view drew duplicate
  bore lines; validator keyway heuristic was a permanent false-negative — all fixed.
- COM `system_run_lisp` always reported `"nil"`; `system_set_variable` didn't coerce numeric sysvars
  — fixed. `system_about` tool groups / `_registered_tool_count` no longer drift or surface `-1`.
- `drawing_new` bootstrap failures now surface as `degraded` instead of reporting success.

### Security
- HTTP remote-bind guard now fires on **every** launch path (including `fastmcp run server.py:mcp`),
  not only the `__main__` block — closing an anonymous-remote-bind gap.
- AutoLISP allowlist bypass-vector regression tests (newline injection, symbol aliasing, `vla*`/`acet-*`,
  `c:` custom commands).
- COM apartment leak bounded (`CoUninitialize` on teardown); transaction commit/rollback and
  `system_run_lisp` now respect the CMDACTIVE guard.

## [1.0.0] — 2026-03-01

### Added
- Initial release with 67 tools, 6 resources, 5 prompt templates
- Dual-engine architecture: COM backend (live AutoCAD) + ezdxf backend (headless)
- FastMCP 3.0 server with middleware stack (error handling, audit, timing, logging)
- Drawing management: new, open, save, save-as, export DXF/PDF, purge, audit, undo/redo
- Entity creation: line, circle, arc, polyline, rectangle, text, mtext, hatch, spline, ellipse, point, block reference
- Dimensions: linear, aligned, angular, radius, diameter
- Entity modification: move, copy, rotate, scale, mirror, offset, delete, rectangular/polar array
- Layer management: create, delete, set current, modify, freeze/thaw, lock/unlock, hide/show, isolate
- Block operations: list, insert, explode, attributes, create from entities, find references
- Analysis: entity stats, region search, distance/area measurement, bounding box, select by layer/type
- View control: zoom extents/window, screenshot
- Transaction support: begin, commit, rollback
- System tools: status, variables, command execution, AutoLISP evaluation
- 5 prompt templates: floor plan, P&ID, electrical schematic, mechanical drawing, quick drawing
