# Track A (v1.6 P&ID) — review backlog

**Source:** the plan-compliance and adversarial review rounds of Track A (2026-09-15/16),
waves 1–4. Blocking findings were fixed inside Track A (`fix(task-N)` commits) and are
not listed. Every *minor* finding is here, de-duplicated, with one disposition:

- **fixed-in-task-N** — Track E group H (`docs/superpowers/plans/2026-09-16-v1.6-settings-implementation.md`, Tasks 1–7)
- **fixed-in-track-A** — resolved on main by a later Track A commit (evidence cited)
- **accepted-limitation** — kept as is, with the reason
- **deferred-1.7** — a real item for the 1.7 roadmap

Reading key: `W1-T1-03` = wave 1, Track A Task 1, finding 3. Files are repository-relative.

## Wave 1

#### Track A Task 1 — `block_insert(attributes=…)` (ezdxf)

| ID | File | Finding | Disposition |
|---|---|---|---|
| W1-T1-01 | backends/ezdxf_backend.py | The undefined-block gate (`6f4bda5`) was added without being listed as a deviation. | accepted-limitation (reporting gap; the gate is kept and shared by Task 1) |
| W1-T1-02 | plan | Branch forked before the docs commits; no code diverged. | accepted-limitation (process; merged clean) |
| W1-T1-03 | backends/ezdxf_backend.py | `attributes` was coerced after `add_blockref`, so a non-mapping failed after the write and skipped `_mark_dirty`. | fixed-in-task-3 |
| W1-T1-04 | backends/ezdxf_backend.py | `str(v)` wrote Python reprs (`None`, `True`, a dict) into ATTRIBs on both engines. | fixed-in-task-3 |
| W1-T1-05 | backends/ezdxf_backend.py | Tag matching is exact-case; `{"tag": …}` silently drops against ATTDEF `TAG`. | deferred-1.7 (normalise keys to upper case; the server's own ATTDEF tags are upper-case by `ATTDEF_TAG_RE`) |
| W1-T1-06 | backends/ezdxf_backend.py | `*Model_Space` / `*Paper_Space` pass the existence check and insert a reference cycle. | fixed-in-task-1 |
| W1-T1-07 | backends/ezdxf_backend.py | `entity_create_block_ref` still accepts an undefined block; `doc.audit()` deletes the INSERT later. | fixed-in-task-1 |
| W1-T1-08 | tests/test_block_insert_attributes.py | `attributes=None` yields no ATTRIBs headlessly while COM's `InsertBlock` instantiates ATTDEF defaults; refusal types diverged (ValueError vs RuntimeError). | refusal type: fixed-in-task-1 (COM raises the same ValueError); default ATTRIBs: deferred-1.7 (engine divergence on ATTDEF defaults) |
| W1-T1-09 | backends/ezdxf_backend.py | `block_explode` drops the ATTRIB text. | fixed-in-task-2 |

#### Track A Task 2 — `backends/block_specs.py`

| ID | File | Finding | Disposition |
|---|---|---|---|
| W1-T2-01 | security.py | `illegal_symbol_name_chars` added outside the task's file list, unreported. | accepted-limitation (reporting gap; harmless, tested) |
| W1-T2-02 | backends/block_specs.py | `fullmatch`, layer-character gate and `_one_line` are stricter than the plan stated. | accepted-limitation (all serve the validate-before-write rule; pinned by tests) |
| W1-T2-03 | plan / spec | Spec §4.7 per-entity `color`/`linetype`/`lineweight` overrides were dropped by the plan; unknown keys are silently ignored. | deferred-1.7 (carry the three keys through or refuse unknown keys by index) |
| W1-T2-04 | backends/block_specs.py | `_scalar` let `10**400` escape as `OverflowError` instead of the named `TypeError`. | fixed-in-task-4 |
| W1-T2-05 | backends/block_specs.py | A non-list `entities` (dict, str, generator) is refused as "empty". | deferred-1.7 |
| W1-T2-06 | backends/block_specs.py | `_layer` gates characters but not the 255-char length rule. | deferred-1.7 |
| W1-T2-07 | backends/block_specs.py | Refusal messages echo the caller's value unbounded (`got {value!r}`; measured 2 MB messages). | deferred-1.7 (truncate with reprlib) |
| W1-T2-08 | backends/block_specs.py | Same as W1-T2-03 (unknown keys such as `rotation` silently dropped). | deferred-1.7 (with W1-T2-03) |

#### Track A Task 6 — ISA-5.1 tag grammar

| ID | File | Finding | Disposition |
|---|---|---|---|
| W1-T6-01 | engineering/pid/tags.py | Three grammar changes (X user-defined, no output after V, O/C only after ZS) unreported. | accepted-limitation (the fixture is the contract; documented in `describe_tables()['rules']`) |
| W1-T6-02 | engineering/pid/tags.py | Two of those go past the spec §8 wording. | accepted-limitation (spec text to be reconciled by the 1.7 docs pass) |
| W1-T6-03 | spec | Spec example meanings (`Flow rate`/`Indicate`/`Control`) differ from the tables (`Flow`/`Indicator`/`Controller`). | accepted-limitation (keys match; consumers pin the tables) |
| W1-T6-04 | engineering/pid/tags.py | The "unexpected letter" error names the first trailing letter, not the offender (`LAHX-1` → reports H). | deferred-1.7 |
| W1-T6-05 | engineering/pid/tags.py | `equipment_prefixes={}` falls back to the default map (`or` instead of `is None`); non-string values pass through. | deferred-1.7 (also W4-T19-05) |
| W1-T6-06 | tests/data/isa51_tags.json | No fixture row for K (rate of change / control station), M trailing, O readout, B/N/X user-defined readouts. | deferred-1.7 (≈5 rows) |
| W1-T6-07 | engineering/pid/tags.py | O/C trailing narrowed to Z-first tags beyond the spec text. | accepted-limitation (documented resolution of a spec/fixture conflict) |

#### Track A Task 12 — `engineering/pid/lines.py`

| ID | File | Finding | Disposition |
|---|---|---|---|
| W1-T12-01 | engineering/pid/lines.py | `axis_direction`, the `string.Formatter` parser and finiteness checks were added unreported. | accepted-limitation (reporting gap; pinned by tests) |
| W1-T12-02 | engineering/pid/__init__.py | Created outside the task's file list (branch predated Task 5). | accepted-limitation (byte-identical to Task 5's) |
| W1-T12-03 | engineering/pid/lines.py | `direct` and waypoint modes accept a negative / NaN `stub`. | deferred-1.7 (hoist the stub check above the mode branch) |
| W1-T12-04 | engineering/pid/lines.py | A malformed waypoint (`{"x":…}`, a flat list) escapes as `KeyError` / `TypeError` instead of `ValueError` naming `waypoints[i]`. | deferred-1.7 (also W3-T13-11) |
| W1-T12-05 | engineering/pid/lines.py | `count_crossings` undercounts a crossing that lands exactly on the other polyline's vertex. | deferred-1.7 (reported metric; orthogonal auto routes cannot produce it) |
| W1-T12-06 | engineering/pid/lines.py | A format spec containing `-` (`{seq:-05d}`) is refused with a misleading message. | accepted-limitation (a refusal, not a wrong number; hyphen inside a field spec is unsupported) |
| W1-T12-07 | engineering/pid/lines.py | `label_placement([(0,0)])` raises an unnamed error; a closed-loop waypoint route is accepted; `{{seq}}` is copied through. | accepted-limitation (unreachable through the tools; cosmetic) |

## Wave 2

#### Track A Task 3 — `block_define`

| ID | File | Finding | Disposition |
|---|---|---|---|
| W2-T3-01 | README.md | Snapshot bumped while lines 23/30/106 still said 154. | fixed-in-track-A (Task 21 README rewrite: "166 tools in 20 groups") |
| W2-T3-02 | tests/test_tool_registry.py | Frozen snapshot edited outside the file list. | accepted-limitation (required by the gate; the precedent every later task follows) |
| W2-T3-03 | backends/ezdxf_backend.py | `base_x`/`base_y` only `float()`-cast; NaN written into the BLOCK record. | fixed-in-task-4 |
| W2-T3-04 | backends/ezdxf_backend.py | A primitive layer is written without a layer-table entry; COM would fail mid-loop. | fixed-in-task-4 |
| W2-T3-05 | README.md | Same as W2-T3-01. | fixed-in-track-A |
| W2-T3-06 | backends/ezdxf_backend.py | ATTDEF linetype stays BYLAYER on ezdxf vs ByBlock on COM; neither sets lineweight ByBlock. | deferred-1.7 (cosmetic; layer-0 members resolve to the INSERT's layer) |
| W2-T3-07 | backends/ezdxf_backend.py | `attdefs or []` turns `{}`/`''` into "no ATTDEFs"; a non-str `name` raises AttributeError. | deferred-1.7 |
| W2-T3-08 | backends/ezdxf_backend.py | `block_insert` without `attributes` carries none of the ATTDEF defaults (COM does). | deferred-1.7 (with W1-T1-08) |

#### Track A Task 4 — XDATA

| ID | File | Finding | Disposition |
|---|---|---|---|
| W2-T4-01 | discovery/aliases.py | `entity_get_xdata` ships `XDLIST` only (XDATA would break the single-route gate). | accepted-limitation (still discoverable; reporting gap) |
| W2-T4-02 | tests/test_tool_registry.py | Snapshot edited outside the file list. | accepted-limitation (gate precedent) |
| W2-T4-03 | backends/ezdxf_backend.py | APPID names are case-sensitive headlessly; `APP_A` and `app_a` become two groups and double-count the budget. | deferred-1.7 |
| W2-T4-04 | backends/com_backend.py | A no-op removal registers the APPID on COM but not on ezdxf. | deferred-1.7 |
| W2-T4-05 | backends/xdata_specs.py | 1004 binary chunks decode to the repr of a bytes object. | deferred-1.7 (hex string) |
| W2-T4-06 | backends/xdata_specs.py | The reserved-app rule blocks reading `ACAD` by name while the unfiltered read returns it; other AutoCAD-owned apps are writable. | deferred-1.7 |
| W2-T4-07 | README.md | Snapshot paragraph contradicted itself; collected-tests drift. | fixed-in-track-A (Task 21) |

#### Track A Task 5 — `engineering/pid/xdata.py`

| ID | File | Finding | Disposition |
|---|---|---|---|
| W2-T5-01 | engineering/pid/__init__.py | Listed as "create" but already existed. | accepted-limitation (nothing missing) |
| W2-T5-02 | tests/test_pid_xdata.py | Sync tests under a module-level `pytest.mark.asyncio` emit PytestWarnings. | deferred-1.7 (hygiene sweep across the pid test modules) |
| W2-T5-03 | spec | Spec payload (`actuator`, 3-element ports, no `seq`) vs plan (`variant`, 5-element ports, `seq`). | accepted-limitation (plan governs; spec text to reconcile) |
| W2-T5-04 | engineering/pid/xdata.py | `encode_payload` uses `allow_nan=True`; a NaN would be serialised as a non-RFC literal. | deferred-1.7 (`allow_nan=False`; no current caller can deliver one after Task 5) |
| W2-T5-05 | engineering/pid/xdata.py | `symbol_payload(params={})` is replaced by the spec's params (truthiness). | deferred-1.7 |
| W2-T5-06 | engineering/pid/xdata.py | Duplicate port names collapse silently (last wins). | deferred-1.7 (refuse in `make_spec`) |
| W2-T5-07 | engineering/pid/xdata.py | A non-dict payload is written and can never be read back. | deferred-1.7 |
| W2-T5-08 | tests/test_pid_xdata.py | No multi-chunk payload round trip through a backend. | deferred-1.7 (test gap; the path was verified by hand) |

#### Track A Task 7 — symbol framework

| ID | File | Finding | Disposition |
|---|---|---|---|
| W2-T7-01 | engineering/pid/symbols.py | Per-type outlines (no circle for computer/plc) deviate from the plan snippet, unreported. | accepted-limitation (the spec's ISA table governs; tests added) |
| W2-T7-02 | tests/test_pid_symbols.py | Unused import dropped, `math` added. | accepted-limitation (no functional effect) |
| W2-T7-03 | engineering/pid/symbols.py | Rear-location dashed lines stop up to 1 mm short on the right. | accepted-limitation (cosmetic; no port, bbox or gate depends on it) |
| W2-T7-04 | engineering/pid/symbols.py | A single radial `signal` port cannot describe the hexagon/square outlines (0.67 mm gap on vertical lines into `computer`). | accepted-limitation (spec §4.4 model; a per-axis reach is a 1.7 item) |
| W2-T7-05 | engineering/pid/symbols.py | `all_specs()` swallows builder ValueErrors and nothing pins the catalogue size. | deferred-1.7 (assert `len(all_specs())`) |
| W2-T7-06 | engineering/pid/symbols.py | `register()` overwrites silently; `list_symbols()` returns variants by reference. | deferred-1.7 |
| W2-T7-07 | engineering/pid/geometry.py | `bbox_of` samples arcs at 1° and can under-report by 3.8e-5·r. | accepted-limitation (sub-lineweight) |

#### Track A Task 8 — valves

| ID | File | Finding | Disposition |
|---|---|---|---|
| W2-T8-01 | plan | The plan file was edited to match the shipped geometry, unreported. | accepted-limitation (the original geometry floated the dome; evidence in the commit) |
| W2-T8-02 | engineering/pid/symbols_valves.py | `check`/`relief` advertise actuator variants they refuse. | deferred-1.7 (variants `["none"]` for those bodies) |
| W2-T8-03 | engineering/pid/symbols_valves.py | The diaphragm *body* dome floats 1 mm above the bow-tie. | deferred-1.7 (cosmetic) |
| W2-T8-04 | engineering/pid/symbols_valves.py | The `signal` port and the TAG/DESC text share x = 0 above the actuator; a signal line crosses the tag. | accepted-limitation (spec §4.7 placement; a tag/line overlap critique is a 1.7 item, with W2-T9-05) |

#### Track A Task 9 — equipment

| ID | File | Finding | Disposition |
|---|---|---|---|
| W2-T9-01 | engineering/pid/symbols_equipment.py | `pd_pump` ports are `in`/`out`, spec says `suction`/`discharge`. | accepted-limitation (plan governs; spec text to reconcile; `pid_from_spec` names ports explicitly) |
| W2-T9-02 | engineering/pid/symbols_equipment.py | `spectacle_blind` cites ISO 10628-2 instead of PIP PIC001; no spec names its figure/table. | deferred-1.7 (provenance strings) |
| W2-T9-03 | engineering/pid/symbols_equipment.py | Same as W2-T9-01. | accepted-limitation |
| W2-T9-04 | engineering/pid/symbols_equipment.py | `build_equipment` ignores `shape` for non-reducers and its signature is narrower than `**options`. | accepted-limitation (`resolve()` validates options; builders are internal) |
| W2-T9-05 | engineering/pid/symbols_equipment.py | TAG/DESC sit on the outward ray of every +Y port. | accepted-limitation (with W2-T8-04) |
| W2-T9-06 | engineering/pid/symbols_equipment.py | `air_cooler` fan cross pokes 0.046 mm outside its circle. | deferred-1.7 (cosmetic, ±1.75) |
| W2-T9-07 | engineering/pid/symbols_equipment.py | Seven origins are not the bbox centre (spec §4.2). | accepted-limitation (deliberate mounting points; spec text to reconcile) |

#### Track A Task 10 — vessels

| ID | File | Finding | Disposition |
|---|---|---|---|
| W2-T10-01 | engineering/pid/geometry.py | `scan_hits` added outside the file list, unreported. | accepted-limitation (additive, tested) |
| W2-T10-02 | engineering/pid/symbols_vessels.py | Stricter size rules and outline-based stubs, unreported. | accepted-limitation (evidence in the commit; plan tests unchanged) |
| W2-T10-03 | spec | `PID_VESSEL_VERTICAL_` vs `PID_VESSEL_VERTICAL_VESSEL_`. | accepted-limitation (plan's general rule governs) |
| W2-T10-04 | engineering/pid/symbols_vessels.py | Non-finite sizes/fractions pass `validate_vessel_params` (NaN spec, `RuntimeError` for inf). | deferred-1.7 (`math.isfinite` next to `> 0`) |
| W2-T10-05 | engineering/pid/symbols_vessels.py | Column tray lines poke through dished heads (up to 1.8 mm). | deferred-1.7 (clip to the outline with `scan_hits`) |
| W2-T10-06 | engineering/pid/symbols_vessels.py | Nozzle fractions near 0/1 on curved/sloped sides give degenerate long stubs from a corner. | deferred-1.7 |
| W2-T10-07 | engineering/pid/symbols_vessels.py | `trays` is unbounded (1 000 000 trays build in 6.3 s / 272 MB). | deferred-1.7 (cap by spacing ≥ 1 mm) |
| W2-T10-08 | engineering/pid/symbols_vessels.py | Two nozzles at the same side+fraction duplicate the stub; `-0.0` hashes to a different block name. | deferred-1.7 |

## Waves 3–4

#### Track A Task 11 — `pid_symbol_list` / `pid_symbol_insert`

| ID | File | Finding | Disposition |
|---|---|---|---|
| W3-T11-01 | engineering/layers.py | `PROCESS-LINE-TEXT` inserted at position 9; spec said "16th entry". | accepted-limitation (count matches; ordinal wording only) |

#### Track A Task 13 — `pid_line_draw`

| ID | File | Finding | Disposition |
|---|---|---|---|
| W3-T13-01 | tests/test_tool_registry.py | Snapshot edited outside the file list. | accepted-limitation (gate precedent) |
| W3-T13-02 | discovery/aliases.py | `PLINE` added to `SHARED_ACAD_COMMANDS`. | accepted-limitation (required by the single-route gate; documented in the allowlist comment) |
| W3-T13-03 | backends/com_backend.py | `e924b5d` touched three files outside the list, unreported (COM `AcDbPolyline` naming, mirrored ports). | accepted-limitation (both were defects; tests added) |
| W3-T13-04 | server.py | `>=` for `≥` in a docstring. | accepted-limitation (cosmetic) |
| W3-T13-05 | engineering/pid/drawlines.py | Line sequence is per current space, not per document (spec §6.3). | deferred-1.7 (document-wide scan of existing lines) |
| W3-T13-06 | engineering/pid/drawlines.py | Every signal line got a fabricated number and label and consumed the process sequence. | fixed-in-task-6 |
| W3-T13-07 | engineering/pid/drawlines.py | A bubble's exit aimed at the far end even with waypoints, so the first segment cut through the bubble. | fixed-in-task-6 |
| W3-T13-08 | engineering/pid/drawlines.py | A foreign/hand-edited payload with a non-dict `from`/`to` raised `TypeError` out of `draw_line`. | fixed-in-task-7 |
| W3-T13-09 | engineering/pid/drawlines.py | (a) crossings tested against chords, not arcs; (b) COM heavy 2DPOLYLINE `Coordinates` read with stride 2. | (a) fixed-in-task-7; (b) deferred-1.7 (heavy polylines the tool never creates) |
| W3-T13-10 | README.md | Snapshot line edited without the collected-tests figure. | fixed-in-track-A (Task 21) |
| W3-T13-11 | server.py | An unknown endpoint handle and a flat waypoint list surface as "Internal error" rather than a named refusal. | deferred-1.7 (with W1-T12-04) |

#### Track A Task 14 — `pid_graph`

| ID | File | Finding | Disposition |
|---|---|---|---|
| W3-T14-01 | engineering/pid/graph.py | Block-name keywords match whole tokens, not substrings (spec §9.2); `GATEVALVE` no longer classifies. | accepted-limitation (the plan's own test demands it; documented in code) |
| W3-T14-02 | backends/ezdxf_backend.py | A public `bulges` key was added to every LWPOLYLINE `entity_get`, unreported. | accepted-limitation (additive; spec §9.4 "geometry from the drawing"; now consumed by Task 7) |
| W3-T14-03 | tests/test_tool_registry.py | Snapshot edited outside the file list. | accepted-limitation (gate precedent) |
| W3-T14-04 | engineering/pid/graph.py | Edge dict carries `length_approximate` and (with geometry) `bulges` beyond the spec. | accepted-limitation (additive) |
| W3-T14-05 | tests/test_pid_graph.py | Three sync tests under the asyncio mark warn. | deferred-1.7 (hygiene, with W2-T5-02) |
| W3-T14-06 | engineering/pid/graph.py | `_Edge.segments` omits a closed polyline's closing edge while `length` includes it. | deferred-1.7 |
| W3-T14-07 | engineering/pid/graph.py | The dangling `nearest` hint never names a radial (bubble) port. | deferred-1.7 |
| W3-T14-08 | engineering/pid/graph.py | Text-tag fallback searched a circle, not "above the box". | fixed-in-track-A (`d17a261`, `6ad3734`: tags above the box, `LINE_NUMBER_RE` excluded) |
| W3-T14-09 | engineering/pid/graph.py | `scope="all"` restores `current` but not the paper-space layout binding. | deferred-1.7 |
| W3-T14-10 | engineering/pid/graph.py | A −Z-extruded catalogue INSERT reported mirror-image ports at confidence 1.0. | fixed-in-track-A (`6ad3734`: both engines report `mirrored`; `transform_port(mirrored=)`) |
| W3-T14-11 | engineering/pid/graph.py | A `PID_*` INSERT whose XDATA was stripped falls to the 0.6 heuristic with no ports (spec says catalogue 1.0). | deferred-1.7 |
| W3-T14-12 | README.md | Release-consistency gate red on the collected-tests figure. | fixed-in-track-A (Task 21) |
| W3-T14-13 | engineering/pid/symbols.py | `6ad3734` touched five files outside the list; `transform_port` grew `mirrored`/`strict`. | accepted-limitation (justified; interface kept by keyword defaults) |
| W3-T14-14 | engineering/pid/graph.py | `_text_near` accepts a baseline half a height inside the box and excludes line-number texts — beyond the spec wording. | accepted-limitation (documented, tested) |
| W3-T14-15 | engineering/pid/graph.py | Node dicts gained `y_scale`, `mirrored`, `notes`, `space`, `description`; junctions `space`. | accepted-limitation (additive) |
| W3-T14-16 | docs (plan/spec) | `ruff format --check` was red on the plan and spec Markdown. | fixed-in-track-A (Task 21 formatted them) |
| W3-T14-17 | spec | §9.1 model not extended for the added node/junction keys. | deferred-1.7 (docs) |
| W3-T14-18 | engineering/pid/symbols.py | `strict=False` keeps a diagonal port's local angle under non-uniform scale. | accepted-limitation (unreachable: all 243 catalogue ports are axis-aligned) |
| W3-T14-19 | backends/ezdxf_backend.py | `mirrored` is `True` for a tilted INSERT with a negative-z normal (COM twin too). | deferred-1.7 (`flat and z < 0`) |

#### Track A Task 15 — deliverables

| ID | File | Finding | Disposition |
|---|---|---|---|
| W3-T15-01 | tests/test_pid_deliverables.py | Four plan deviations (line-list rows, `_far_ends`, CSV refusal, `deliverable()` ValueError) unreported. | accepted-limitation (each stronger than the plan; the plan's own numbers were wrong) |
| W3-T15-02 | tests/test_tool_registry.py | Snapshot edited outside the file list. | accepted-limitation (gate precedent) |
| W3-T15-03 | tests/_probe_task15.py | A stray probe file appeared during review. | fixed-in-track-A (not in the tree) |
| W3-T15-04 | engineering/pid/deliverables.py | `natural_key` crashes on `str.isdigit()` runs that are not `\d` (①, ²). | deferred-1.7 (`isdecimal`) |
| W3-T15-05 | engineering/pid/deliverables.py | `_far_ends` resolves junctions at the ends of the instrument's line only, not on it. | deferred-1.7 |
| W3-T15-06 | engineering/pid/deliverables.py | `_tag_of` writes the internal junction id into a tag column; untagged node ends are `None` vs handle in the index. | deferred-1.7 |
| W3-T15-07 | engineering/pid/deliverables.py | `write_csv` edge cases: 0-byte file for zero rows, `csv_path=''` treated as None, directory path escapes as PermissionError, dead `mkdir`, late validation. | deferred-1.7 |
| W3-T15-08 | engineering/pid/deliverables.py | CSV formula injection: cells starting with `=+-@` are written verbatim. | deferred-1.7 (first item of the 1.7 deliverables pass) |
| W3-T15-09 | server.py | The three deliverable tools expose only `tolerance`/`scope`, not `label_search`/`include_foreign`. | deferred-1.7 |
| W3-T15-10 | engineering/pid/deliverables.py | `signal_lines` counts every edge on a bubble regardless of class. | deferred-1.7 |

#### Track A Task 16 — critique focuses

| ID | File | Finding | Disposition |
|---|---|---|---|
| W3-T16-01 | tests/test_pid_critique.py | The refine test calls `refine_drawing` (no backend `drawing_refine` exists), unreported. | accepted-limitation (the plan's call could not run; stricter test) |
| W3-T16-02 | engineering/pid/critique.py | `has_pid_content` guard instead of `not graph['nodes']`, unreported. | accepted-limitation (spec intent; pinned by tests) |
| W3-T16-03 | engineering/pid/critique.py | `_incompatible` skips inferred ports; `_dangling` reports unknown_block ends — beyond the plan. | accepted-limitation (spec §9.4 / §11.1; tested) |
| W3-T16-04 | tests/test_pid_critique.py | Two sync tests warn under the asyncio mark. | deferred-1.7 (hygiene) |
| W3-T16-05 | engineering/pid/critique.py | `_untagged` tests the composed tag, not FUNC/LOOP; an empty LOOP files as `pid_illegal_tag`. | deferred-1.7 |
| W3-T16-06 | engineering/pid/critique.py | A mechanical block whose name holds a keyword token (`OIL_FILTER`) counts as P&ID content and can fail finalize. | accepted-limitation (spec §9.2 mandates the heuristic; README "Known limitations" states it) |
| W3-T16-07 | engineering/pid/critique.py | `_duplicate_tags` scores a text-derived (guessed) tag as a gate-failing error. | deferred-1.7 (warning for `tag_source: text`) |

#### Track A Task 17 — `pid_from_spec`

| ID | File | Finding | Disposition |
|---|---|---|---|
| W4-T17-01 | tests/test_pid_from_spec.py | Four reported deviations verified legitimate (save_path, score dict, async test, snapshot). | accepted-limitation |
| W4-T17-02 | engineering/pid/spec.py | The module is a refactor of the plan's listing; interfaces preserved. | accepted-limitation |
| W4-T17-03 | README.md | `pid-showcase.png` rendered but not referenced. | fixed-in-track-A (README line 20 embeds it) |
| W4-T17-04 | tests/test_release_consistency.py | Collected-tests drift. | fixed-in-track-A (Task 21) |
| W4-T17-05 | engineering/pid/spec.py | `_num` accepts NaN/inf; an INSERT lands at `[nan, 0]` and `build_graph` crashes. | fixed-in-task-5 |
| W4-T17-06 | engineering/pid/spec.py | `run_spec` catches `Exception` only; a cancellation leaves an open transaction. | fixed-in-task-5 |
| W4-T17-07 | engineering/pid/spec.py | `dry_run` skips `place_symbol`'s option checks (`scale=-1` approved; wrong item blamed). | deferred-1.7 (`validate_spec` owns scale/fail/actuator rules) |
| W4-T17-08 | engineering/pid/spec.py | `dry_run` crossings count the spec's own lines only, unlabelled. | deferred-1.7 (`crossings_scope` field) |
| W4-T17-09 | engineering/pid/spec.py | Nested transactions: ezdxf nests, COM refuses; `drawing_plan` is set before the refusal. | deferred-1.7 |
| W4-T17-10 | engineering/pid/spec.py | The `sheet` block is not validated (unknown layer set / sheet size / keys accepted). | deferred-1.7 |
| W4-T17-11 | engineering/pid/spec.py | String-typed optional keys unvalidated (`arrow: "no"` truthy; `number: 123` bare TypeError; `id` with a dot). | deferred-1.7 |
| W4-T17-12 | README.md | README showcase half incomplete; gate red. | fixed-in-track-A (Task 21) |

#### Track A Task 19 — tag parse, resources, prompt

| ID | File | Finding | Disposition |
|---|---|---|---|
| W4-T19-01 | tests/test_tool_registry.py | Snapshot edited outside the file list. | accepted-limitation (gate precedent) |
| W4-T19-02 | scripts/render_pid_catalog.py | Renders through its own matplotlib path instead of `view_screenshot`, unreported. | accepted-limitation (byte-reproducible output; interface kept) |
| W4-T19-03 | README.md | Lines 23/106 still said 154 / 6 resources. | fixed-in-track-A (Task 21) |
| W4-T19-04 | server.py | The prompt's OFF-PAGE example emits a tag warning every time it is followed (`TO P&ID-002` parsed as ISA-5.1). | deferred-1.7 (skip `parse_tag` for connectors at insert) |
| W4-T19-05 | engineering/pid/tags.py | `equipment_prefixes={}` discarded; non-string values pass. | deferred-1.7 (with W1-T6-05) |
| W4-T19-06 | README.md | Release gate red on the collected-tests figure. | fixed-in-track-A (Task 21) |

#### Track A Task 18 — `TOOL_PACKS`

| ID | File | Finding | Disposition |
|---|---|---|---|
| W4-T18-01 | tests/test_tool_packs.py | A sync test warns under the asyncio mark. | deferred-1.7 (hygiene) |
| W4-T18-02 | README.md / config.py / .env.example | Token table and prose said 47 while lean has 50. | README: fixed-in-track-A (Task 21); `config.py:99` / `.env.example:52` "~47": deferred-1.7 (docs) |
| W4-T18-03 | server.py | `TOOL_PACKS=all,mech3000` drops the unknown entry without a warning or `ignored`. | deferred-1.7 |
| W4-T18-04 | server.py | `_enabled_packs()` runs twice per apply; unknown entries warn twice and duplicate in `ignored`. | deferred-1.7 |
| W4-T18-05 | tests/test_tool_packs.py | The pack test compares against a hand-copied set, never the registry. | deferred-1.7 (assert against `_registered_tools()` tags) |
| W4-T18-06 | server.py | Under `TOOL_PACKS=core` the P&ID prompt and resources are still advertised. | accepted-limitation (spec §12 scopes packs to tools) |

#### Track A Task 20 — benchmark v4

| ID | File | Finding | Disposition |
|---|---|---|---|
| W4-T20-01 | scripts/check_doc_numbers.py | The gate short-circuited on the 29-vs-26 A/B drift. | fixed-in-track-A (Task 21 regenerated the A/B report) |
| W4-T20-02 | CLAUDE.md | An extra clause beyond the dictated sentence. | accepted-limitation (accurate) |
| W4-T20-03 | benchmarks/README.md | "16/16 on the v4 matrix" has no published artifact behind it. | deferred-1.7 (publish a v4 reference report at the 1.6 release) |
| W4-T20-04 | tests/test_benchmark_v4.py | No test executes `_task_pid_roundtrip`. | deferred-1.7 (Task 6 of the hardening plan runs it by hand) |
| W4-T20-05 | benchmarks/README.md | A/B rows said 29 checks over a 26-check report. | fixed-in-track-A (Task 21) |

#### Track A Task 21 — docs and smoke

| ID | File | Finding | Disposition |
|---|---|---|---|
| W4-T21-01 | scripts/smoke_pid_com.py | Not re-run live during review (no AutoCAD running). | accepted-limitation (the implementer's live run is recorded in the CHANGELOG) |
| W4-T21-02 | README.md | "Known limitations of the P&ID track" was not a plan item. | accepted-limitation (claims verified against the code) |
| W4-T21-03 | docs (plan) | The plan file was ruff-formatted outside the file list. | accepted-limitation (mechanical) |
| W4-T21-04 | benchmarks/README.md | Tables moved for the new gate labels. | accepted-limitation (required by `check_doc_numbers.py`) |
| W4-T21-05 | CHANGELOG.md | "12.5 % short on a semicircular jump" is the v1.5 square figure; the tested geometry is 10.25 %. | deferred to the final docs task of Track E (CHANGELOG `Unreleased` edit) |
| W4-T21-06 | README.md | The snapshot is labelled v1.5 while quoting the 1.6-dev surface. | accepted-limitation (pinned to pyproject's minor until the 1.6 bump) |
| W4-T21-07 | scripts/smoke_pid_com.py | Exit code checks `dangling` only; critique/confidence/xdata are printed, not asserted. | deferred-1.7 |
| W4-T21-08 | CHANGELOG.md | `LAYER_SET_INTENT_MISMATCH` is claimed for `drawing_plan`, which only appends a warning string. | deferred to the final docs task of Track E |

## Totals

165 rows. Fixed in this track (Tasks 1–7): 15 · fixed inside Track A: 17 · accepted with a reason: 62 · deferred (1.7 or the Track E docs task): 71.

## 1.7.0 triage

**Date:** 2026-10-24, on `release/1.7.0` (v1.7.0 Task 0, roadmap B06). Every row above
whose disposition says `deferred-1.7` (72: 69 pure, 3 mixed — W1-T1-08, W3-T13-09 and
W4-T18-02, where only the deferred half is triaged) and the two rows deferred to the
Track E docs task (W4-T21-05, W4-T21-08) were re-run on this tree. A row is closed
only by a test or commit that settles it, never by an old plan. Owners follow the
file tables of the three 1.7.0 plans (`docs/superpowers/plans/2026-10-24-v1.7.0-w*`);
`task-8` takes a small row whose file no task owns; XDATA rows go to 1.7.1 (B08).
The machine-readable copy, with the finding, the old disposition and the reason for
each owner, is `docs/analysis/backlog-1.7.0-triage.json`.

Dispositions: still_reproduces 72 · fixed 1 · superseded 1. Owners: task-2 1 · task-3 7 · task-4 20 · task-8 33 · task-12 1 · task-13 2 · task-20 1 · task-21 1 · task-23 2 · 1.7.1 4 · none 2.

| ID | Disposition | Owner | Evidence |
|---|---|---|---|
| W1-T1-05 | still_reproduces | task-4 | block_insert('B1', attributes={'tag': 'X1'}) over ATTDEF TAG -> block_get_attributes {'TAG': 'DEF'}: the lower-case key is dropped silently |
| W1-T1-08 | still_reproduces | task-8 | refusal part settled by fixed-in-task-1; block_insert('B1') without attributes -> block_get_attributes {} on ezdxf while COM InsertBlock instantiates the ATTDEF defaults |
| W1-T2-03 | still_reproduces | task-4 | validate_entity_specs([{line ..., 'rotation': 45, 'color': 1}]) -> accepted, both keys dropped from the normalised spec |
| W1-T2-05 | still_reproduces | task-4 | validate_entity_specs({'type': 'line'}) / ('abc') / (generator) -> ValueError "'entities' is empty" for all three |
| W1-T2-06 | still_reproduces | task-4 | validate_entity_specs([{line ..., 'layer': 'L' * 300}]) -> accepted with the 300-char layer |
| W1-T2-07 | still_reproduces | task-4 | validate_entity_specs([{line, 'x1': 'x' * 2_000_000, ...}]) -> TypeError message of 2,000,042 characters |
| W1-T2-08 | still_reproduces | task-4 | same repro as W1-T2-03: 'rotation' accepted and dropped |
| W1-T6-04 | still_reproduces | task-8 | parse_tag('LAHX-1') -> errors ['LAHX: unexpected letter H at position 3']; the offender is X |
| W1-T6-05 | still_reproduces | task-8 | parse_tag('P-101', 'equipment', equipment_prefixes={}) -> valid pump (default map used); prefixes={'P': 5} -> equipment_kind 5 |
| W1-T6-06 | still_reproduces | task-8 | tests/data/isa51_tags.json valid rows: none with K first, none with a trailing M, no B/N/X readout; O readout only ZSO-701 |
| W1-T12-03 | still_reproduces | task-8 | route((0,0), None, (10,0), None, stub=-1, mode='direct') and stub=nan -> accepted; waypoint mode with stub=-1 -> accepted |
| W1-T12-04 | still_reproduces | task-8 | route(..., mode=[{'x': 0}]) -> KeyError 0; mode=[0, 10] -> TypeError 'int' object is not subscriptable |
| W1-T12-05 | still_reproduces | task-8 | count_crossings([(0,0),(10,0)], [[(5,-5),(5,0),(5,5)]]) -> 0; the same crossing without the vertex at (5,0) -> 1 |
| W2-T3-06 | still_reproduces | task-8 | block_define with an ATTDEF on ezdxf -> ATTDEF linetype unset (BYLAYER), lineweight unset; com_backend block_define styles it ByBlock through _style() |
| W2-T3-07 | still_reproduces | task-4 | block_define('B2', [...], attdefs={}) -> accepted as no ATTDEFs; block_define(123, [...]) -> AttributeError 'int' object has no attribute 'strip' |
| W2-T3-08 | still_reproduces | task-8 | same repro as W1-T1-08: no ATTRIBs without attributes on ezdxf |
| W2-T4-03 | still_reproduces | 1.7.1 | entity_set_xdata(h, 'APP_A', ['x']) then (h, 'app_a', ['y']) -> entity_get_xdata keys ['APP_A', 'app_a'] |
| W2-T4-04 | still_reproduces | 1.7.1 | ezdxf entity_set_xdata(h, 'NEWAPP', []) -> 'NEWAPP' not in doc.appids; com_backend.entity_set_xdata calls RegisteredApplications.Add before the empty case |
| W2-T4-05 | still_reproduces | 1.7.1 | decode_values([(1004, b'\x01\x02')]) -> the string "b'\x01\x02'" (the repr of a bytes object) |
| W2-T4-06 | still_reproduces | 1.7.1 | entity_get_xdata(h, 'ACAD') -> ValueError reserved while the unfiltered read returns ACAD; entity_set_xdata(h, 'AcDbAttr', ['z']) -> written |
| W2-T5-02 | still_reproduces | task-8 | pytest tests/test_pid_xdata.py -> 4 PytestWarnings 'marked with @pytest.mark.asyncio but it is not an async function' |
| W2-T5-04 | still_reproduces | task-4 | encode_payload({'a': float('nan')}) -> ['json', '{"a":NaN}'] |
| W2-T5-05 | still_reproduces | task-4 | symbol_payload(column_spec, params={})['params'] -> the spec's full params, not {} |
| W2-T5-06 | still_reproduces | task-4 | make_spec(..., ports=[shaft, shaft]) -> two ports named 'shaft'; symbol_payload keeps one |
| W2-T5-07 | still_reproduces | task-4 | encode_payload([1, 2]) -> ['json', '[1,2]']; decode_payload of it -> None (written, never readable) |
| W2-T5-08 | fixed | none | tests/test_pid_insert.py::test_xdata_payload_carries_ports_and_params (4b6abf7) round-trips a vertical_vessel payload through the ezdxf backend; measured 3 chunks after the marker |
| W2-T7-05 | still_reproduces | task-8 | len(all_specs()) == 109 today; the only pin is tests/test_pid_resources_prompt.py '>= 100' and all_specs() still swallows builder ValueErrors |
| W2-T7-06 | still_reproduces | task-8 | appending to list_symbols()[i]['variants'][k] mutates _REGISTRY (True); register() overwrites an existing symbol without a word |
| W2-T8-02 | still_reproduces | task-8 | symbols_valves.py registers every body with variants {'actuator': ACTUATORS}, check and relief included, while build_valve refuses an actuator on them |
| W2-T8-03 | still_reproduces | task-8 | diaphragm body: arc(0, 2, r=2) ends at (+-2, 2) while the bow-tie edge at x = +-2 is at y = 1: the dome floats 1 mm (file unchanged since 352ce21) |
| W2-T9-02 | still_reproduces | task-8 | symbols_equipment.py SOURCE = 'ISO 10628-2:2012' for spectacle_blind too |
| W2-T9-06 | still_reproduces | task-8 | air_cooler fan cross ends at hypot(1.8, 1.8) = 2.5456 against the 2.5 circle: 0.046 mm outside |
| W2-T10-04 | still_reproduces | task-4 | validate_vessel_params('vertical_vessel', {'width': nan}) -> width nan accepted; build_vessel(height=inf) -> RuntimeError "nozzle 'N1': x=0.0 meets no outline" |
| W2-T10-05 | still_reproduces | task-4 | build_vessel('column', width 16, height 20, trays 9) -> tray lines up to 3.254 mm beyond the dished head |
| W2-T10-06 | still_reproduces | task-4 | vertical_vessel nozzle side=left fraction=0 -> stub from (0, -20) to (-13, -20): a 13 mm stub from the bottom pole |
| W2-T10-07 | still_reproduces | task-4 | validate_vessel_params('column', {'trays': 10 ** 9}) -> accepted, no cap |
| W2-T10-08 | still_reproduces | task-4 | two top nozzles at fraction 0.5 -> 2 identical stubs; fraction 0.0 and -0.0 -> block names ..._72E67B86 and ..._B19D56A8 |
| W3-T13-05 | still_reproduces | task-8 | pid_line_draw in Model -> number 1; layout_set_current('Layout1') and a second line -> number 1 again (existing_pid_lines reads entity_list of the current space) |
| W3-T13-09 | still_reproduces | task-13 | (a) fixed-in-task-7; (b) live AutoCAD 2026 (25.1) probe: ModelSpace.AddPolyline of 3 vertices -> AcDb2dPolyline.Coordinates has 9 values (x, y, z triples) while com_backend entity_get reads stride 2 |
| W3-T13-11 | still_reproduces | task-8 | in-memory Client: pid_line_draw(from_={'handle': 'FFFF'}) -> 'Internal error: ... Entity with handle FFFF not found'; route=[10, 20] -> "'int' object is not subscriptable" |
| W3-T14-05 | still_reproduces | task-8 | pytest tests/test_pid_graph.py -> 7 PytestWarnings 'marked with @pytest.mark.asyncio but it is not an async function' |
| W3-T14-06 | still_reproduces | task-8 | _Edge has no closed flag; _Edge(vertices=[(0,0),(10,0),(10,10)]).segments() -> 2 segments, a closed polyline needs 3 |
| W3-T14-07 | still_reproduces | task-8 | instrument bubble (signal port radius 5) and a signal line ending 2 mm off the circle -> dangling nearest None (radial ports never enter the port grid) |
| W3-T14-09 | still_reproduces | task-2 | active paper layout Layout1, current Model, layout SHEET2 added -> build_graph(scope='all') -> doc.layouts.active_layout() is SHEET2 afterwards |
| W3-T14-11 | still_reproduces | task-8 | pid_symbol_insert('centrifugal_pump'), XDATA cleared -> build_graph node source 'heuristic', confidence 0.6, ports ['p1'] (catalogue ports lost) |
| W3-T14-17 | still_reproduces | task-8 | spec 2026-09-15-v1.6-pid-design.md section 9.1 and the pid_graph docstring list none of y_scale, mirrored, notes, description |
| W3-T14-19 | still_reproduces | task-13 | ezdxf entity_get INSERT: props['mirrored'] = extrusion[2] < 0 (com_backend: Normal[2] < 0), so a tilted normal (0.5, 0, -0.866) reads as mirrored |
| W3-T15-04 | still_reproduces | task-3 | natural_key('FT-1²') -> ValueError invalid literal for int() with base 10: '²' |
| W3-T15-05 | still_reproduces | task-3 | _far_ends reads only edge['from'] / edge['to'] of the instrument's own line (unchanged since 346eac4) |
| W3-T15-06 | still_reproduces | task-3 | _tag_of returns ref.get('junction') as the tag of a junction end |
| W3-T15-07 | still_reproduces | task-3 | write_csv([], p) -> 0-byte file; write_csv(rows, path of a directory) -> PermissionError; deliverable(csv_path='') -> csv_path None |
| W3-T15-08 | still_reproduces | task-3 | write_csv([{'tag': '=1+1'}, {'tag': '@x'}]) -> lines 'tag', '=1+1', '@x' |
| W3-T15-09 | still_reproduces | task-3 | in-memory Client: pid_instrument_index / pid_line_list / pid_equipment_list inputSchema -> csv_path, scope, tolerance only |
| W3-T15-10 | still_reproduces | task-3 | instrument_index counts signal_lines += 1 for every edge on a bubble port, whatever its class |
| W3-T16-04 | still_reproduces | task-8 | pytest tests/test_pid_critique.py -> 2 PytestWarnings 'marked with @pytest.mark.asyncio but it is not an async function' |
| W3-T16-05 | still_reproduces | task-8 | pid/critique.py _untagged tests not n.get('tag') (the composed tag), not FUNC / LOOP |
| W3-T16-07 | still_reproduces | task-8 | _duplicate_tags emits severity 'error' for every node with a tag, tag_source 'text' included |
| W4-T17-07 | still_reproduces | task-4 | run_spec(spec with equipment[0].scale = -1, dry_run=True) -> approved; the real run -> ValueError 'equipment[0]: scale must be > 0' |
| W4-T17-08 | still_reproduces | task-12 | run_spec(dry_run=True) -> keys crossings_total, dry_run, items, lines; no crossings_scope |
| W4-T17-09 | still_reproduces | task-4 | ezdxf: transaction_begin() then run_spec -> accepted (nests); run_spec calls drawing_plan before transaction_begin, and COM transaction_begin returns 'A transaction is already active' |
| W4-T17-10 | still_reproduces | task-4 | validate_spec(sheet={'layer_set': 'nonsense', 'sheet_size': 'Z9', 'bogus': 1, 'scale': 'abc'}) -> accepted; run_spec -> ValueError could not convert string to float: 'abc' |
| W4-T17-11 | still_reproduces | task-4 | validate_spec(lines[0] arrow='no', number=123) -> accepted; equipment id 'P.1' -> accepted |
| W4-T19-04 | still_reproduces | task-8 | place_symbol('offpage', tag='TO P&ID-002', direction='out') -> tag_warnings ["'TO P&ID-002': not in [area-]LETTERS[-]loop[suffix] format"] |
| W4-T19-05 | still_reproduces | task-8 | same repro as W1-T6-05 |
| W4-T18-01 | still_reproduces | task-8 | pytest tests/test_tool_packs.py -> PytestWarning on test_pack_registry_names_real_tools_and_only_them |
| W4-T18-02 | still_reproduces | task-8 | README part settled by fixed-in-track-A; config.py:105, .env.example:52 and server.py:864 still say ~47 / 47 while lean is 65 |
| W4-T18-03 | still_reproduces | task-8 | TOOL_PACKS='all,mech3000' -> _enabled_packs() returns every pack and ignored [], no warning |
| W4-T18-04 | still_reproduces | task-8 | TOOL_PACKS='core,foo,foo' -> one _apply_tool_profile('full') logs 4 warnings (_profile_enabled_names and the apply each call _enabled_packs), ignored ['foo', 'foo'] |
| W4-T18-05 | still_reproduces | task-8 | tests/test_tool_packs.py compares PACK_TOOL_NAMES with hand-copied sets; test_every_pack_member_is_a_registered_tool (70afd57) checks one direction only, no test compares the registered tools' pack tags |
| W4-T20-03 | superseded | none | benchmarks/README.md no longer claims 16/16; benchmarks/results/published/autocad-mcp-pro.json is matrix v7, 21/21 at 554daff, which contains the v4 tasks |
| W4-T20-04 | still_reproduces | task-20 | no test calls _task_pid_roundtrip (tests/test_benchmark_v4.py checks ids, category and adapter coverage only; test_benchmark_v7 reads the published JSON) |
| W4-T21-05 | still_reproduces | task-23 | CHANGELOG.md:221 ([1.6.0]) and engineering/pid/graph.py:154 still say 12.5 %; the tested jump (tests/test_pid_graph.py:421) is 50 against 40 + 5 pi: 10.25 % short |
| W4-T21-07 | still_reproduces | task-21 | scripts/smoke_pid_com.py:80 -> return 0 if stats['dangling'] == 0 else 1 |
| W4-T21-08 | still_reproduces | task-23 | CHANGELOG.md:515 claims drawing_plan reports LAYER_SET_INTENT_MISMATCH; drawing_plan('P&ID ...', layer_set_id='mech') returns only warnings ['intent reads as a P&ID but the layer set is ...'] (drawing_preflight does emit the code) |
