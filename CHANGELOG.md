# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

1.8.0: the server remembers, finishes a job in one call and measures itself
honestly. Nine tools are added (248 -> 257) and two resources (11 -> 13); the
5 prompt templates are unchanged, and `system_about` is the authority for the
counts. This head is the release summary; the three phase blocks below (W1,
W2, W3) keep what each phase shipped, with its own migration notes or Breaking
table. Spec: `docs/superpowers/specs/2026-11-03-v1.8.0-design.md`.

### Breaking at a glance

One line per default that changed, and what restores the 1.7.0 behaviour (the
W1 *Migration notes* and the W2 / W3 *Breaking* tables carry the detail):

- **Server discovery:** `DISCOVERY_MODE` defaults to `search`: a client lists
  `search_tools`, `describe_tool`, `call_tool` and the `DISCOVERY_PINNED` tools
  (`cad_batch`, `drawing_info`, `entity_list`), and every tool stays callable
  by name. Restore: `DISCOVERY_MODE=off`; `DISCOVERY_PINNED=""` pins nothing.
- **Plugin discovery:** `plugin/.mcp.json` passes
  `DISCOVERY_MODE=${AUTOCAD_MCP_DISCOVERY:-search}`. Restore:
  `AUTOCAD_MCP_DISCOVERY=off`.
- **Long descriptions on the wire:** a description over 800 characters is
  listed as its leading paragraphs plus `Details: describe_tool.` (47 of 257
  tools), and `DISCOVERY_MODE=off` lists `describe_tool` too (253 tools). No
  switch: the registered tool and `describe_tool` keep the whole docstring.
- **`meta["cad"]`** carries `summary` and `cost` only. The AutoCAD names and
  synonyms are in `describe_tool` and `docs/tool-inventory.json`.
- **`cad_batch(on_error=)`** defaults to `auto`: a batch that writes is all or
  nothing, and a literal handle that names no entity refuses the whole batch
  (`unknown_handle`) under every mode. Restore: `on_error="stop"` (the handle
  pre-check stays).
- **`entity_delete_many`** deletes all of its handles or none and answers
  `ok: false` whenever it reports an error. No switch: send live handles,
  read `ok`.
- **`system_status.operations`** is a summary, `{count, last,
  unknown_commit}`. Restore: `system_status(detail="operations")` (this
  release only), or `drawing_history`.
- **`strict_critique=True`** (opt-in on `drawing_finalize`, the default of
  `drawing_deliver`) also fails on every `not_checked` lane except
  `domain_absent`, so a default live delivery is `failed_validation`.
  Restore: `strict_critique=False`.
- **Headless `dimstyle_modify`** redraws the dimensions that use the style.
  Restore: `rerender=False`.
- **`LISP_POLICY`** defaults to `read` (the read tier, with the denylist as a
  second pass). Restore: `LISP_POLICY=denylist`.
- **`drawing_topology_check(limit=)`** defaults to `None`: 200 rows per list
  without `summary`, 5 with it. Restore: `limit=200`.
- **A writer's output path** takes only the tool's own extensions and never a
  name AutoCAD or Windows loads (`path_refused`). No switch: name the file
  with the tool's extension.
- **HTTP:** an unknown Origin is 403, a foreign Host 421, a body over 8 MiB
  413. Restore: `HTTP_ALLOWED_ORIGINS` / `HTTP_ALLOWED_HOSTS` (or `*`),
  `MAX_HTTP_BODY_BYTES=0`. A non-loopback bind also needs `ALLOWED_PATHS`
  now, with no switch.
- **Lineweights:** -3 reads as Default, never ByLayer, and a layer refuses
  ByLayer / ByBlock by name. No switch: the old answer was a mislabel.
- **Errors:** every route returns one typed envelope, and a missing handle,
  path, command or file has its own kind. The text content is unchanged.
- **Durable journal:** on by default in `ACADMCP_STATE_DIR`, so a reopened
  file continues its `document_id` and revision, and headless `drawing_undo`
  without `EZDXF_UNDO_DEPTH` steps back to the newest checkpoint, also after a
  restart. Restore: `JOURNAL_DURABLE=off`.
- **COM target space:** a live seat draws in model space until
  `layout_set_current` names a sheet; the tab the operator clicks no longer
  moves it. Restore: `layout_set_current`.
- **COM commands and undo:** `system_run_command` and live undo / redo wait
  for the command and can fail `command_incomplete`. `COMMAND_DEADLINE_S`
  bounds the wait; there is no fire-and-forget mode.
- **COM refresh and bulk jobs:** one display refresh per COM job (`Update()`
  of the entity for a single create, one Regen otherwise), and
  `entity_batch_create` / `entity_delete_many` run as STA jobs sliced at a
  quarter of `COM_CALL_TIMEOUT`, cancellable only between jobs. Restore:
  `COM_REFRESH_MODE=regen` (one Regen per job; there is no per-entity mode);
  no switch for the slicing.
- **Headless `.dwg` open (`dwg_read`):** converted through the ODA File
  Converter, then `accoreconsole`; with neither, a typed `dwg_read` refusal.
  Restore: `ACCORECONSOLE_PATH=off` (and no ODA).
- **`drawing_critique(focus=None)`** reports one `model_drift` `unstamped`
  info issue per part drawn before 1.8.0. Restore: redraw it with
  `mech_part_revise(..., overwrite=True)`.
- **Screenshots:** the image is still `content[0]`; the report is `content[1]`
  and the structured content.

### Gate numbers

Measured on the release tree (`release/1.8.0`, headless), each with its
command:

- **A/B:** `python benchmarks/compare_versions.py v1.7.0 --json ...` - 61 / 83
  (73.5 %) on v1.7.0 against 83 / 83, 22 fixed, 0 regressed
  (`benchmarks/results/published/ab-v1.7.0-vs-v1.8.0.json`, taken at
  `ab07c47`, whose `correctness_suite.py` is the release tree's; the current
  side alone re-run on `78b9b1e`: 83 / 83).
- **Tokens:** `python -m benchmarks.token_suite --budget
  benchmarks/token_budget.json --release` exits 0. Idle, prompt payload / wire
  (ratio/v1 estimates): the 1.8.0 default surface (`search_pinned`, six tools)
  1,026 / 1,300 (ceiling 1,500); `search` 220 / 230 (250; 1.7.0 455 / 503);
  `DISCOVERY_MODE=off` 253 tools 59,138 / 78,391 (65,594; 1.7.0 62,471 /
  89,350); `lean` 16,182 / 21,189 (18,842); `packs_core` 40,722 / 55,169
  (43,997); instructions 199 (250; 1.7.0 461). Result cells: `system_status`
  +18 bytes at 1,000 journal rows (300), a 500-issue critique summary 735
  characters (2,500), finalize's `qa_coverage` 226 tokens (300), the error
  envelope 58 tokens per error (60), `drawing_history(limit=10)` 344 tokens
  (400). Task cells (calls): sketch 1, foreign inspection 1, layer clean-up 2
  (1.7.0 124, the rival 11), plate dimensions 2 (1.7.0 89, the rival 10),
  round-2 value 2. `benchmarks/results/published/token-cost.json` is this run.
  `real_model.plugin_eval` is not checked by `--budget`; its
  `dimension-budget` case was over its 6 calls in 1.8-11's runs (9 and 12).
- **Matrix v9:** `python -m benchmarks.run_competitors --server autocad-mcp-pro
  --backend ezdxf --matrix v9 --no-fetch` - 49 tasks, 47 / 47 attempted pass
  (score 100.0); `lisp_read_tier` `environment_blocked` (AutoLISP runs only in
  AutoCAD), `std_tables_b40` `not_run` (no B40 row); `dwg_headless_roundtrip`
  passed through `accoreconsole` on the run machine. The AgentCAD lane
  (`lizaixi01-agentcad`, no adapter) is `not_run` on all 49.
  `benchmarks/results/published/lanes-v9/`.
- **Full suite:** `AUTOCAD_MCP_BACKEND=ezdxf pytest -q -p no:cacheprovider`
  with no deselection - 7,502 collected, `7501 passed, 1 xfailed` (432 s) at
  `683ba21`, the tree of this release before its version commit.
- **Headless performance:** `perf_compare.py --base v1.7.0 --aa` is measured
  by the coordinator on an idle machine. The W1 gate numbers below are kept as
  history; their A/A spread was 12.3 %, so their bound was 1.25.

### Live evidence

No smoke has a 1.8.0 pass row today: this release's live matrix,
`benchmarks/results/live/1.8.0/matrix.json`, is written by
`scripts/live/run_all.py` on the seat run, and the coordinator fills this
section after it. Until then no document pins a claim to the release (`(AutoCAD
2026, 1.8.0)`, which `scripts/check_doc_numbers.py` would refuse with no
matrix), and the existing claims keep the result files that back them.

**Ran.** `scripts/smoke_accore.py` against the installed
`C:\Program Files\Autodesk\AutoCAD 2026\accoreconsole.exe`, with no AutoCAD
window open: 8 / 8 checks in 3.2 s on 2026-11-04 at `4e98c10` (`dirty`
false), committed as `benchmarks/results/live/accore.json` (the file records
server 1.7.0: the version had not moved yet). It converts the bundled
`templates/iso_a3_mech.dwt` under a `.dwg` name (no DWG can be produced
headlessly without ODA) and compares it with `templates/iso_a3_mech.dxf`;
that template's model space is empty, so the entity gate covers its 34
paper-space entities and the block gate is trivially equal. The ODA route ran
only under monkeypatched tests (ODA is not installed here).

**Written, not run** (AutoCAD 2026 was not running for any of them):
`scripts/smoke_space_com.py`, `scripts/smoke_edit_com.py` and
`scripts/smoke_render_com.py` (W1) with spikes S4 and S5;
`scripts/smoke_settings_com.py`'s `set edits`, `dimension truth` and
`dimension auto` stages, `scripts/smoke_lisp_com.py` and
`scripts/smoke_durability_com.py` (W2); the revise stage and the B40 stage of
`scripts/smoke_mech_com.py`, the `avoid` and `project` stages of
`scripts/smoke_pid_com.py` (the active document plus two DXF sheets; the live
`.dwg` sheet route is covered by a fake-COM test only), and
`benchmarks/perf_com.py` (W3). The 1.8-23 gate also needs
`scripts/smoke_operator_edits_com.py` passing live and `view_screenshot`
showing a new entity after `Update()`. Every COM path of 1.8.0 not named in
`accore.json` is covered by fake-COM tests only.

### Not in 1.8.0

- **No B40 row is transcribed.** The licensed copies of DIN 471/472:2011-04,
  DIN 509, DIN 332-1:1986-04 and ISO 3601-2:2016 were not at hand, so all
  eight tables (DIN 471, DIN 472, DIN 509-E, DIN 509-F, DIN 332-A,
  DIN 332-B, ISO 3601-2, ISO 3601-2-axial) ship empty, refuse every size by
  name and are deferred to 1.8.1 by name - each waiting on its copy (DIN
  471/472 Table 1, DIN 509 Table 1 forms E and F, DIN 332-1 Table 1 forms A
  and B, ISO 3601-2 radial static and axial). Nothing is filled from memory
  or a secondary catalogue; before the first DIN 509 row ships,
  `profile_from_dims` is confirmed against the standard's figure. The v9
  `std_tables_b40` task is `not_run` and the smoke's B40 stage only prints
  its skip.
- **The native .NET worker is deferred to 1.9.0, by a rule fixed before any
  measurement** (`docs/adr/0001-native-worker.md`). 1.9.0 builds it only if
  it is at least 3× faster than the 1.8-23 COM bulk job on the same 1,000
  lines, or if atomic rollback cannot be made reliable with UNDO marks.
  Otherwise it stays deferred: a build and signature per AutoCAD release, the
  SECURELOAD route (`NETLOAD` is on our own denylist), no CI coverage, and a
  second language for one maintainer. The one-day spike was not run, and no
  timing is claimed.
- **1.8.1, from the phases:** batch lease, `confirm_large` and batch budgets;
  `cad_batch` step coalescing; the AutoLISP write tier (`command`, `entmod`,
  `setvar`, `vla-put-*`); `mech_part_revise`'s spec-reconciliation and
  keep-the-hand-edit modes (a part with a detail view, or with two views
  sharing a view tag, is refused by name); `pid_project`'s write side (a
  project-wide tag sequence and a refusal of a tag another sheet holds,
  through `project=`); DWG export and AUDIT through accoreconsole (the
  console runs without `/isolate`, so AutoCAD's own support-path
  `acaddoc.lsp` still loads - noted for 1.8.1); redrawing a dimension after
  move / rotate / mirror / copy / array and inside block definitions, and the
  headless reflex-angle redraw (`dim_render_stale`, `reflex_render`).
- **1.8.1, from the plan:** the headless creation speed lost against v1.4.0
  (B10; 1.8.0 only gates against regressing from 1.7.0); the XDATA chunk /
  case / budget rework (B08); `modified` summaries; a checkpoint per
  operation and a create-only inverse layer; the `ACADMCP_IDENTITY` XRECORD
  stamp; `operation_reconcile`; a COM undo group per operation;
  `history_purge`; `RESULT_VERBOSITY=terse` and dropping `bounding_box` from
  the default `entity_list` row; a run-time batch hint; highlighting,
  `cad_batch(render=True)` and a live viewer; semantic tag replay
  (`if_tag_exists`); inline symbol cutting and symbol move-and-reroute on a
  P&ID; editing an `arch` element; constraint enforcement; the
  `plot_readability` and `text_hygiene` focuses; an `autocad_live` bridge and
  a multi-document `cad_batch`; an `inspect_image` equivalent and a CLI;
  STRETCH / BREAK / JOIN / OVERKILL / draw order and the ISO 273 hole table; a
  jump mark where P&ID lines cross (1.8.1 or later).
- **1.9.0:** IEC 60617 electrical schematics, reusing the P&ID symbol, port
  and graph machinery; extracting a spec from a document; the .NET worker if
  ADR 0001's rule fires.
- **Not planned:** Plant 3D, `run_script`, an HTTP REST `/v1` beside MCP's
  own HTTP transport, and 3D beyond the opt-in live solids.
- **Built narrower than planned:** `_command_context` (1.8-23 point 4) is not
  built, because the 1.8-05 smoke that would justify it never ran; a per-line
  `route="avoid"` inside `pid_from_spec` is refused (only `sheet.routing`),
  its dry run reports no `label_hits`, and `pid_line_draw` does not expose
  the node budget; a lost connection in the middle of a live bulk job loses
  the per-item rows of the jobs already done.

1.8.0, first phase (W1, "the trust foundation"). 1.7.0 made what the server
draws provable; W1 makes what it **says** true. A lineweight of -3 is called
*Default* - never ByLayer - and the critique sees an entity that overrides its
layer. Every error on every route is one typed envelope that says what to do
next, with the 1.7 sentence unchanged beside it. The operation journal
survives the process; a headless save is atomic; a reopened drawing continues
its revision; a damaged DXF can be recovered on request. On a live seat the
server draws where it means to, and a raw command reports what it did. A
polyline is edited in place, and mechanical dimensions carry feature tags. A
region screenshot really shows the region. No tool is added or removed in W1
(`system_about` is the authority for the count). The correctness suite has
67 checks: 61/67 on v1.7.0, 0 regressions. Spec:
`docs/superpowers/specs/2026-11-03-v1.8.0-design.md`; plan:
`docs/superpowers/plans/2026-11-03-v1.8.0-w1-implementation.md`. Spike
evidence is in `docs/analysis/spikes-1.8.0.json`, rows S1-S3, all headless:
- **S1**: with `DISCOVERY_MODE=search`, hidden tools stay directly callable
  by name. Search plus three pinned tools measured 2,730 tokens against the
  plan's 1,500 ceiling, so 1.8-11 re-sets that ceiling.
- **S2**: sqlite WAL with `synchronous=NORMAL` survived 14 process kills
  with no lost or partial commit, at about 12 µs per commit.
- **S3**: the HTTP guard for 1.8-17 wraps `mcp.http_app` beside the
  existing `run_async` wrap.

### Added

- **One typed error envelope (1.8-02).** Every exception a tool call ends in
  is returned as `ToolResult(is_error=True)`. Its structured content is
  `{ok: false, kind, category, retryable, hint, tool, backend, operation_id,
  message, ...the kind's own fields}`. The text content is byte-for-byte what
  a 1.7 client read: the refusal sentence for the 1.7 refusal kinds,
  FastMCP's `Internal error: ...` line for the rest.
  `engineering/errors.py` holds the shared parts:
  - the closed `ERROR_KINDS`: the 17 1.7 batch kinds, then `not_found`,
    `unknown_handle`, `path_refused`, `policy_refused`, `file_not_found`,
    `application_busy`, `command_incomplete`, `internal`,
    `requires_reconcile`, the gate kinds `validation_failed`,
    `critique_gate_failed` and `failed_acceptance`, the 1.8-04 open / save
    kinds `damaged_drawing`, `recover_unsupported`,
    `recovered_source_protected`, `save_locked`, `save_verify_failed` and
    `save_failed`, and the journal's `process_exit`. Every kind a tool can
    put on the wire is declared, with its category, retry default and hint;
  - eight categories (`input`, `spec`, `geometry`, `file`, `policy`,
    `validation`, `dependency`, `internal`);
  - one hint per kind;
  - `classify(exc)`, which types by exception class along the cause chain
    and never by message text.

  The refusal middleware and `cad_batch` both call `classify(exc)`, so a
  direct call and a batch step give one kind, category and `retryable`
  (`tests/test_error_envelope.py` provokes each kind both ways, the 1.8-04
  kinds included). A damaged, DWG or recovered source and a locked target
  are recorded `refused` (nothing was opened or written; `save_locked` is
  the one retryable save kind), `save_verify_failed` / `save_failed` are
  `failed`, and the envelope's message for those kinds is the typed
  sentence that names `recover=True` or `overwrite_source=True`, not the
  parser's or the OS's text. The journal's own refusals -
  `operation_conflict`, `requires_reconcile` and a malformed client id
  (`invalid_args`) - carry the same envelope with the 1.7 text content
  byte for byte, and a bad `detail` on `drawing_finalize` /
  `drawing_deliver` is `invalid_args` (`InvalidArgsError`), not
  `refused`. `retryable` is true only when
  the kind allows a retry **and** the operation record is `refused` or
  `cancelled`, which means nothing was written. `internal` is kept for the
  builtin bug types (`AttributeError`, `NameError`, `UnboundLocalError`,
  `AssertionError`, `RecursionError`). A caller's `TypeError`, `KeyError`,
  `IndexError` or `ZeroDivisionError` stays `failed`, as in 1.7. Typed
  exceptions at the sources: `EntityNotFoundError` (headless
  `_get_entity`, live `_handle_to_object`), `PathRefusedError`,
  `CommandRefusedError` (`verbs`, `symbols`, `alternatives`),
  `ApplicationBusyError` (`RPC_E_CALL_REJECTED` before the job touched the
  document; no revision moves), `CommandIncompleteError` and
  `GateFailure`.
- **The finalize gate returns what it computed.** A failed
  `drawing_finalize` carries `score`, `blocking` (`[{code, severity,
  handles, message}]`), `blocking_count` and `qa_coverage`. By default
  `blocking` holds the first 10 rows with up to 5 handles each (a longer
  list adds `handle_count`). `detail="full"` returns every row, every
  handle and the whole report. `drawing_deliver` adds the same payload as
  `gate_failure` to a `failed_validation` / `failed_acceptance` result. No
  second QA read is needed to see why a gate failed.
- **Refused commands name the typed tools (1.8-02).** A
  `system_run_command` or `system_run_lisp` refusal is `policy_refused`. It
  carries `verbs` (and `symbols` for LISP) and `alternatives`
  (`[{tool, summary}]`, from `discovery.aliases.tools_for_command`).
  `alternatives` lists only tools the active profile, packs and `ENABLE_3D`
  leave callable, never an escape hatch. A permitted command that a typed
  tool also performs carries `typed_alternative` in the same shape.
- **A durable operation journal (B13, 1.8-03).** `DurableJournal`
  (`backends/journal_store.py`) keeps the in-memory journal's API and writes
  every record through to `<ACADMCP_STATE_DIR>/journal.sqlite3` (sqlite WAL,
  `synchronous=NORMAL`, `busy_timeout=5000`). The default folder is
  `%LOCALAPPDATA%\autocad-mcp-pro\state`; off Windows it is
  `$XDG_STATE_HOME/autocad-mcp-pro`.
  - **Replay across restarts:** a client id replays its recorded result
    after a restart. An answer over `JOURNAL_RESULT_MAX_KB`, and every
    answer under `JOURNAL_ARGS=digest` / `off`, keeps only its sha256 and
    replays as `result_truncated: true` with `revision_after`.
  - **A dead owner's rows:** at open, a `running` row whose owner (pid plus
    process start time) is dead becomes `unknown_commit` with
    `error_kind: "process_exit"`; a read-only tool's row becomes
    `cancelled` instead. A retry under that id is `requires_reconcile`. The
    rows of a live second server sharing the file are never touched.
  - **One commit per call:** a mutating call pays one commit, its `running`
    row committed before the call runs. Its final state, and a read's row,
    are flushed in the background 0.1 s later, while a client id's final
    state is committed before the call returns. Measured inside the server:
    about 125 µs over the in-memory journal per call.
  - **Refused folders:** a folder inside a repository checkout (this
    server's tree or any folder with a `.git` above it) or a folder holding
    drawings is refused at startup.
  - **Degraded mode:** a refused folder, a bad setting, an unusable file or
    a lock held past the timeout leaves the journal in memory. It is
    reported in `system_status.state_store` (`mode`, `path`, `bytes`,
    `documents`, `operations`, `checkpoints`, `write_failures`, `degraded`),
    and no tool call ever fails because of its journal.
- **Atomic saves, lineage and recovery (1.8-04, B09).**
  - **Atomic writes:** every headless DXF write goes through
    `backends/atomic_write.py`. That covers `drawing_save`,
    `drawing_save_as`, `drawing_export_dxf`, `document_close(save=True)`,
    and through them the finalize / deliver writes and
    `drawing_template_save`. The protocol: a pending marker under
    `<STATE_DIR>/pending`, `<target>.acadmcp-<pid>.tmp` beside the target,
    flush and fsync, a check (HEADER first, `EOF` last, `$ACADVER`,
    `$HANDSEED` and the ENTITIES count), the marker completed with the
    expected sha256, `SAVE_BACKUP=1` → `<name>.bak`, then `os.replace`. The
    replace is retried 3 × 50 ms on a sharing violation, after which it is
    `save_locked`.
  - **Interrupted saves:** a save a dead process left half done is resolved
    at startup and by a `drawing_open` of the same path, reported as
    `save_recovery` (`completed`, `finished`, `rolled_back`, `orphan_tmp`).
  - **Lineage:** kept in the state folder, never in the DXF. Reopening an
    unchanged file continues its `document_id` and `revision`. A file
    changed outside the server reports `external_change: true` and moves
    the revision by one, with an `external_edit` journal row. The same
    lineage opened under a second path forks (`lineage_parent`), and a
    revision already issued is never issued again.
  - **Recovery:** `drawing_open(path, recover=True)` reads a damaged DXF
    through `ezdxf.recover` and reports
    `recovered: {fixes, errors, messages[:20], entities_read}`, with a
    warning when nothing was read back. The recovered drawing starts a new
    lineage with `recovered_from`. Its source is protected:
    `recovered_source_protected` refuses a save onto it unless
    `drawing_save(overwrite_source=True)`. A DWG is `recover_unsupported`.
    Live, the repair goes into a private temporary DXF that
    `Documents.Open` then opens.
  - **Cost:** measured with the marker files fsynced, the protocol cost
    about 35 ms on a 2,000-entity save, about 10 ms of it those two fsyncs.
    The marker files are therefore written without fsync; the DXF tmp
    still is fsynced, and a power loss resolves it as an orphan.
- **The `property_override` critique focus (1.8-01).** One issue per layer
  and code - `lineweight_not_bylayer`, `color_not_bylayer`,
  `linetype_not_bylayer` - with the count, a value histogram and up to 50
  handles. An explicit lineweight outside ISO 128 is an `error`; -2, -3 or
  an ISO value is a `warning`.
  - **Scope:** the engineering layers plus the layer set applied to the
    drawing. Title-block and construction layers are exempt, and so is every
    member of a block definition, layer 0 included. Any other layer is
    `info` with `in_scope: false`.
  - **Where it runs:** under `focus=None` and inside `drawing_finalize`,
    reading the run's all-spaces snapshot.
  - **Repair:** `drawing_refine` re-reads the layer and sets ByLayer.
- **In-place vertex and label edits (1.8-06).**
  - `entity_edit_geometry` takes `points` and `bulges` for an LWPOLYLINE
    (the vertex count may change), and `x` / `y` for TEXT and MTEXT.
    `entity_edit_text` takes `x` / `y`, and `entity_create_polyline` takes
    `bulges`.
  - An argument the entity's type does not take is refused by name. A
    tilted plane is refused `ocs_tilted_plane`.
  - Behind these is the new contract member `entity_set_vertices` on both
    engines; it is not a tool. Headless it runs through `backends/ocs.py`.
    Live it writes `Coordinates`, `SetBulge` and `Closed` in place and is
    registered in `_MUTATING_METHODS` (98 → 99). When the seat refuses or
    ignores a changed vertex count, the polyline is recreated with its
    layer, colour, linetype, ltscale, lineweight and XDATA. The result then
    reports `replaced: [{old, new}]`.
- **Tagged mechanical dimensions (1.8-06).** `xdata=` reaches
  `dimension_linear`, `dimension_aligned`, `dimension_radius`,
  `dimension_diameter` and `entity_create_table`, written in the same create
  job. `mech_dimension_part` tags each DIMENSION in `ACADMCP_MECH` XDATA
  (`{kind: "feature", part, feature, view, role: "dim"}`) on the dimension
  object itself, not on its anonymous block. The hole table is tagged
  `role: "hole_table"`, and `draw_prims` writes an ordinal `i` on every
  primitive.
- **Region screenshots (1.8-07).** `view_screenshot(region=[x0, y0, x1, y1],
  max_px=1568)` and `view_zoom_and_screenshot(..., max_px=)` draw the window
  they are given on both engines. Beside the image they report `applied`,
  `framing`, `source`, `px` (read from the PNG's IHDR), `region` (the window
  drawn, widened by under a pixel so x and y share one scale),
  `requested_region` and `px_per_unit`.
  - **Headless:** the current space is drawn through
    `backends/region_render.py`.
  - **Live:** a region is the bound document's `Document.Export` snapshot
    drawn by the same renderer (`source: "export_snapshot"`). With no
    region, the result is the window capture (`source: "window"`), with
    `region` and `px_per_unit` null.
  - **Refused by name:** a zero-area region, a `max_px` outside 64-4096,
    and a partial `view_zoom_and_screenshot` window.
- **Lineweight names (1.8-01).** One parser (`backends.base.parse_lineweight`)
  for every lineweight input. It takes an integer (hundredths of a mm),
  a millimetre float, a numeric string, or `"ByLayer"` / `"ByBlock"` /
  `"Default"` in any case. It serves `entity_set_properties`,
  `layer_create`, `layer_modify`, and every row of `entity_batch_create` /
  `entity_batch_modify`. Entity rows (`entity_get`, `entity_list`) carry
  `lineweight_name`. The QA snapshot's entity records gain `lineweight`,
  `color`, `linetype` and `transparency`, and its layers gain `lineweight`,
  `plot`, `on` and `frozen`, on both engines.
- **Six W1 correctness checks.** `benchmarks/correctness_suite.py` gains
  `lineweight_truth` (Entities), `error_envelope_routes_agree`
  (Reliability), `journal_replay_after_kill`,
  `atomic_save_untouched_on_failure`, `lineage_revision_continues`
  (Durability) and `region_render_applied` (Render): 61 → 67 checks. Each
  imports its 1.8.0 modules inside the check, so the suite still runs
  against the v1.7.0 tree; the journal check runs the tree's own
  `server.py` over stdio twice on a private `ACADMCP_STATE_DIR` and kills
  the first with `Popen.kill()`. Against v1.7.0 the six are old fail /
  miss and new pass: 61/67 on v1.7.0, 67/67 here, 0 regressions.
  `tests/test_benchmark_w1_checks.py` also holds that a write straight
  into the target (1.7.0's save) fails the atomic check.
- **Environment variables:**
  - `ACADMCP_STATE_DIR`, the default folder above.
  - `JOURNAL_DURABLE` (`on`).
  - `JOURNAL_ARGS` (`bounded` - canonical JSON up to 4,096 bytes; `digest`
    - sha256 only; `off`).
  - `JOURNAL_SYNC` (`normal`; `full` covers an OS crash at about 30 × the
    cost per commit).
  - `JOURNAL_RETAIN_DAYS` (30).
  - `JOURNAL_RESULT_MAX_KB` (64).
  - `CHECKPOINT_KEEP` (20), `CHECKPOINT_BUDGET_MB` (512) and
    `CHECKPOINT_MAX_DOC_MB` (32). These are read and reported for 1.8-08's
    checkpoints.
  - `SAVE_BACKUP` (off).
  - `COMMAND_DEADLINE_S` (8).

### Changed

- **Error kinds, renamed where 1.7 had only a generic one.** These names
  appear in `cad_batch` step rows, in the envelope and in the journal's
  `error_kind`:
  - a handle that names no entity is `not_found` (was `failed`);
  - a path refusal is `path_refused` (was `refused`);
  - a refused command or LISP expression is `policy_refused` on both routes
    (the direct call was `refused`, a batch step `denied`; `denied` stays
    for a permitted string, because `cad_batch` never runs the escape
    hatches);
  - a missing file is `file_not_found` (was `failed`).
- **A missing handle on a live seat reads like the headless one:**
  `Entity with handle '…' not found.` instead of a raw COM error. A
  caller's handle is resolved through `_handle_to_object` (or a local
  `try` that raises `EntityNotFoundError`), which leaves connection-level
  HRESULTs untouched. Not yet everywhere: `entity_trim`, `entity_extend`,
  `entity_fillet` and `entity_chamfer` still read the caller's handle back
  with a bare `HandleToObject` after the command has run.
- **A retryable refusal is not replayed by its operation id.** Its record
  is kept, but not its answer, so the same request under the same client
  id runs again. A `not_found` or any other non-retryable error still
  replays.
- **Revisions survive a reopen.** `drawing_open` of a file this server saved
  continues its `document_id` and `revision`; the refusal of a file that
  does not parse is `damaged_drawing` and names `recover=True`.
- **COM: the server's target space (1.8-05).**
  - **Model space by default.** The bound document carries a target space:
    model space until `layout_set_current` names a sheet.
    `drawing_new`, `drawing_open` and `document_activate` reset it,
    `layout_rename` follows it and `layout_delete` falls back from it.
  - **Never the operator's tab.** `_msp()` returns the target, never the
    tab the operator has active. A create on a sheet reports its `space`
    on both engines.
  - **Corner operations** pick with the `*x,y` WCS override, with the
    target's tab active, and restore the operator's tab afterwards.
- **`system_run_command` reports what the command did (1.8-05).** The call
  waits until the command finishes: it polls CMDACTIVE up to
  `COMMAND_DEADLINE_S` (default 8 s). It returns `{completed: true,
  created: [{handle, type, space}]` (the first 50), `created_count`,
  `last_prompt`, `target_space}`. A new entity outside the target space
  adds `warning: "created_outside_target_space"`. A command still at a
  prompt has AutoCAD's own `LASTPROMPT` read, is cancelled with ESC × 3,
  and fails with kind `command_incomplete` (`{command, last_prompt}`).
  `drawing_undo`, `drawing_redo` and `transaction_begin`'s `_.UNDO _M` go
  through the same deadline-guarded send.
- **`drawing_undo` / `drawing_redo` report `verified`.** A headless
  snapshot restore is always `true`. Live, `verified` is true when the
  entity total over every layout or the document's edit counter moved,
  with `evidence` holding both.
- **The view tools return the image and a JSON report.**
  `view_screenshot` and `view_zoom_and_screenshot` answer with two content
  blocks, the PNG and the report as JSON text. The report is also the
  structured content.
- **Lineweight descriptions.** No registered schema maps -3 to ByLayer any
  more (`tests/test_lineweight_truth.py` sweeps every tool). A layer's
  lineweight refuses ByLayer and ByBlock, by number or by name, before
  anything is written.
- **The headless hatch is created ByLayer.** ezdxf's `add_hatch` /
  `set_pattern_fill` wrote an explicit ACI 7, which ActiveX `AddHatch`
  does not; the new focus flagged it on `mech_assembly` and
  `arch_roundtrip`.

### Fixed

- **-3 was described as ByLayer** in `entity_set_properties` and
  `layer_create`. DXF has -1 ByLayer, -2 ByBlock and -3 Default
  (LWDEFAULT), so an entity left at -3 was reported as following its layer,
  and `drawing_critique(focus=None)` could not see it (rival claim 4.5).
- **A region was silently ignored headlessly.** `view_zoom_and_screenshot`
  with a window rendered the whole model space and still answered as if it
  had zoomed.
- **A killed headless save could truncate the drawing.** `doc.saveas`
  wrote straight into the target; the atomic protocol above leaves the
  previous file in place.
- **Live, a create could land on a paper layout** when the operator (or a
  DWG saved with a layout active) left a sheet tab current. Nothing is
  drawn outside the target space any more (rival claim 4.6; which of the
  three candidate mechanisms reproduces it on 1.7.0 is spike S5, pending).
- **`system_run_command` returned before the command finished**
  (`{ok, command}` straight after `SendCommand`). A command left at a
  prompt then deadlocked the next COM call.
- **In `DISCOVERY_MODE=search` an error through `call_tool` was wrapped
  twice** (`Error calling tool 'call_tool': ...` around the real refusal).
  It now carries the same text and envelope as the direct call, naming the
  inner tool.

### Migration notes

- **Lineweights:** a lineweight input accepts `"ByLayer"`, `"ByBlock"` and
  `"Default"`. -3 means Default. `layer_create` / `layer_modify` refuse
  -1 / -2. The new `property_override` focus runs inside
  `drawing_finalize`, so a drawing with an explicit non-ISO-128 lineweight
  on an engineering layer now fails the gate. `drawing_refine` repairs it.
- **Error kinds:** a client that branched on `failed` / `refused` /
  `denied` for a missing handle, a path, a command or a missing file reads
  the new kinds above. The text content is unchanged. `kind`, `category`,
  `retryable` and `hint` are in the structured content.
- **Journal folder:** the journal is durable by default and writes to
  `ACADMCP_STATE_DIR`. Point it at a private folder, or set
  `JOURNAL_DURABLE=off` for the 1.7 in-memory journal. A folder inside a
  checkout or beside drawings runs the server in memory, with
  `state_store.degraded`. Test suites should give each run its own folder
  (this repository's `tests/conftest.py` does).
- **Revisions:** a reopen no longer resets `document_id` / `revision`, so
  an `expected_revision` taken by an earlier process is meaningful.
- **Recovered drawings:** a drawing opened with `recover=True` will not
  save over its source without `drawing_save(overwrite_source=True)`.
- **COM sheets:** on a live seat, drawing on a sheet takes
  `layout_set_current`. Clicking a layout tab no longer moves where the
  server draws.
- **COM commands and undo:** `system_run_command` and live undo / redo
  wait up to `COMMAND_DEADLINE_S` and can fail `command_incomplete`. A
  caller that read only `ok` should read `completed`.
- **Screenshots:** a client that took `content[0]` as the whole answer
  still gets the image there. The report is `content[1]` and the
  structured content.

**W1 gate (2026-11-03, `41588a8`).** The full suite green at 6,348
collected tests with the document numbers retaken; ruff, the inventory, the
plugin validation and `scripts/check_doc_numbers.py` green;
`compare_versions.py v1.7.0 --fail-on-regression` 67 / 67 against v1.7.0's
61 / 67, six fixed, zero regressed; `perf_compare.py --base v1.7.0 --aa
--rounds 20` `ok: true` with ratios `create_lines_2k` 1.08, `roundtrip_10k`
1.08, `region_query_10k` 0.98, `premium_pass` 1.00 and
`server_create_lines_2k` 1.11 (the durable journal, about 200 µs per call on
this run). The A/A spread was 12.3 % on that machine, so the bound was 1.25:
the server workload is the number to re-measure on an idle machine, and the
one 1.8-10's change summary must not push further.

**Live evidence pending.** AutoCAD 2026 was not running on 2026-11-03, so
`scripts/smoke_space_com.py`, `scripts/smoke_edit_com.py` and
`scripts/smoke_render_com.py` are written but not yet run. The S4 row
(`Coordinates` with a changed vertex count, undo steps, nested undo marks,
`*D` regen, `%%c<>`, hatch-loop deletion) and the S5 row (which mechanism
puts the rival's circle in paper space on 1.7.0) are not yet run either:
`spikes-1.8.0.json` records both as `pending`. Every COM path above is
covered by fake-COM tests only.

1.8.0, second phase (W2, "call economy, QA truth, security"): all eleven
items, 1.8-08 to 1.8-18. The first documentation pass described 1.8-14,
1.8-16, 1.8-15, 1.8-17, 1.8-18, 1.8-12 and 1.8-13; the second adds 1.8-08
(durable checkpoints), 1.8-09 (the atomic `cad_batch` default), 1.8-10
(`drawing_history`) and 1.8-11 (the search default and the instruction
builder). A layer clean-up and a part's dimensions are now one or two calls
instead of hundreds. Undo survives a restart, a batch that writes is all or
nothing by default, and the value a drawing had in round 2 is two calls away
after a restart. A client sees six tools by default and can still call all of
them. A QA lane that did not run is named, never read as clean. A dimension is
judged by the text the sheet really shows. A server can be narrowed to reads,
a writer cannot leave an AutoCAD autoload file, and the HTTP transport checks
Origin, Host and body size. `system_run_lisp` runs the AutoLISP read tier.
Seven tools are added (`entity_modify_where`, `layer_rename`, `layer_merge`,
`checkpoint_create`, `checkpoint_list`, `checkpoint_restore`,
`drawing_history`) and one resource (`autocad://critique/focuses`);
`system_about` is the authority for the counts. W2 leaves the correctness
suite at 79 checks (W3 adds four more); the published A/B report is still the
W1 run over 67 and is regenerated by the numbers pass.

### W2: Added

- **Set-based edits (1.8-12, core pack).** Three new tools; a layer
  clean-up is one `cad_batch`, not one call per entity.
  - `entity_modify_where(where, set, space="model", dry_run=False,
    expect_count=None)` is AutoCAD's QSELECT + CHPROP. `where` filters by
    `layer` (a name or a list; `*` and `?` are the only wildcards,
    case-insensitive), `type`, `color`, `linetype`, `lineweight`, `region`
    `[x0, y0, x1, y1]` and `block`. `set` writes `layer` (created when
    missing), `color`, `linetype`, `lineweight` (the 1.8-01 parser) and
    `visible`. `space` is `model`, `paper` (every paper layout) or `all`;
    block definitions are never searched. The dry run reports `count`,
    `would_change`, the first 20 `handles`, `truncated`, `skipped_locked` and
    a `preview` (the revision, plus the region fingerprint when `where` has a
    region) that `cad_batch(expected_revision=, preconditions=)` accepts. An
    `expect_count` the drawing no longer matches is refused `stale_revision`
    with nothing changed. Entities on a locked layer are skipped and reported
    in `skipped_locked`, never counted as matched; `changed` counts only
    entities whose value really moved. An empty `where` or `set` is
    `invalid_args` (select everything with `{type: "*"}`).
  - `layer_rename(old, new)` renames the table entry and every reference:
    headless, every layout, block definition, attribute, viewport freeze list
    and the current layer (`entities`, `by_space`, `blocks_touched`); live,
    `AcadLayer.Name` (`entities: null`, because AutoCAD references a layer by
    object id). Refused by name before anything changes: `0`, `DEFPOINTS`, an
    xref-dependent layer, a missing `old`, an existing `new`, a case-only
    change and a DXF-illegal name.
  - `layer_merge(sources, target, delete_sources=True)` is LAYMRG: the
    sources' entities in model space, every paper layout and every block
    definition move to `target`, then the sources are deleted. It names the
    touched block definitions (`blocks_touched`, with a `note`), because an
    edited definition changes every INSERT of that block. The current layer
    moves to the target when it was a source, and a source's viewport freezes
    are dropped. Live, a locked source or target is unlocked for the move and
    the target relocked; a source AutoCAD will not delete is listed in
    `layers_not_deleted`. Refused by name: a missing source or target
    (`layer_create` it first), `0` / `DEFPOINTS` / an xref-dependent source,
    the target among the sources, a duplicate source.
  - All three carry `changes: {added, deleted, counts, complete}` on both
    engines. Costs: `entity_modify_where` and `layer_merge` `destructive`,
    `layer_rename` `mutate`. The alias corpus gains LAYMRG; RENAME
    (`layout_rename` + `layer_rename`) and CHPROP (`entity_set_properties` +
    `entity_modify_where`) become shared commands. Live, the selection is one
    `ssget "X"`-style SelectionSet filter (group codes 0 / 8 / 2 / 410, 62
    only for ACI 1-255) inside one STA job through `_acad_doc()`; ByLayer and
    ByBlock colour, linetype, lineweight and region are checked per entity.
- **`dimension_auto` v2: a part in one call (1.8-13).** No new tool and no
  new capability: `dimension_auto(handles=None, selector=None, style="chain",
  offset=10.0, layer=None, include=None, group_equal=False,
  skip_existing=True, dry_run=False)`. Exactly one of `handles` /
  `selector` (an `entity_select_smart` predicate, e.g. `{"layer":
  "GEOMETRY"}`). A selection of LINEs alone is planned exactly as in 1.7;
  otherwise LWPOLYLINE straight edges are chained per side (an oblique edge
  aligned), CIRCLEs get diameters and ARCs radii with the text outside the
  part box, and the overall width and height are drawn unless an edge
  already measures them (listed under `redundant`). `include` takes
  `linear`, `diameter`, `radius`, `overall` (default: all); `group_equal`
  draws one `n× <>` diameter for equal circles; `skip_existing` binds the
  existing DIMENSIONs through `bind_dimension` and skips a feature already
  dimensioned, so a rerun draws nothing; `dry_run` returns the plan. Every
  entity not dimensioned (ELLIPSE, SPLINE, a bulged segment, anything
  `include` leaves out) is in `omitted: [{handle, type, reason}]`, never
  dropped, and the `dim_overlap` self-check over what was drawn is reported.
  A source carrying a 1.8-06 feature tag gives its dimension a `role: "dim"`
  feature record. Refused by name (`invalid_args`): neither or both of
  `handles` / `selector`, an unknown or empty selector, an unknown or empty
  `include`.
- **QA coverage ledger (1.8-16).** Every critique check writes
  `checked(focus, layout, n)` or `not_checked(focus, layout, reason)`
  (`engineering/qa_coverage.py`). The reasons are a closed vocabulary:
  `domain_absent`, `engine_live_document_template`, `no_viewport_scale`,
  `viewport_scale_deferred`, `font_unresolved` (declared, no emitter until
  1.8.1), `scan_incomplete`, `check_failed`, `check_unreported` (a check that
  wrote no row - a server bug, reported). `drawing_critique(report=True)`
  returns `{issues, coverage: {layouts, complete}}`. `drawing_finalize`,
  `drawing_deliver`, their gate failure and the delivery manifest carry
  `qa_coverage: {complete, records, spaces, reason, not_checked: [{focus,
  layout, reason, lane}], domain_absent}`. On a paper layout (headless):
  nothing but its own viewport is checked; a model viewport onto lettered
  model space is `viewport_scale_deferred` (text at plot scale is 1.8.1); a
  layout with content and no model viewport is `no_viewport_scale`.
- **Compact QA answers (1.8-16).** `drawing_critique`, `validation_check`
  and `drawing_topology_check` take `summary` and `limit` and then return
  `{total, by_focus, by_severity, issues[:limit]}` (`summary=True`: compact
  rows, messages cut to 160 characters, 5 handles plus `handle_count`, 5
  rows unless `limit` says otherwise). `total` and the counts always cover
  every issue; there is no cursor. Without these arguments each tool answers
  as in 1.7. `drawing_topology_check(summary=True)` flattens its findings
  with the `topo_*` severities (near miss error, dangling warning, crossing
  info).
- **The `autocad://critique/focuses` resource (1.8-16).** One row per focus
  in `ALL_CRITIQUE_FOCUSES` order (`{focus, domain, reports, silent?}`), the
  `not_checked` reasons and which of them strict mode exempts.
  `drawing_critique`'s `focus` description no longer carries the
  ~1,000-character enum (now 195 characters); an unknown focus is still a
  warning issue.
- **Dimension truth (1.8-15).**
  - `engineering/dimtext.py` formats a dimension's displayed text the way
    the drawing does: DIMDEC, DIMRND, DIMZIN, DIMDSEP, DIMPOST, DIMLFAC, the
    automatic Ø / R prefix, and DIMADEC / DIMAZIN / DIMAUNIT for angles
    (DIMLFAC never scales an angle). `bind_dimension` uses it and reports
    `display_rounding`: on a DIMDEC 0 sheet a 19.6 mm dimension shows "20"
    with `display_rounding` 0.4, and a shown value within half its last
    unit of the geometry is not a finding.
  - The QA snapshot's DIMENSION records carry `rendered_text` (the plain
    text of the `*D` block), `group42` and the style variables, on both
    engines (live through the same `Document.Export` reader).
  - The new critique focus **`dimension_truth`** binds every DIMENSION in
    every space to its geometry and reports, one issue per dimension and
    code: `dim_override_disagrees` (an override marked NTS is info),
    `dim_dimlfac_scaled`, `dim_render_stale` (the `*D` block shows another
    text than the style formats), `dim_group42_stale`, `dim_double_prefix`
    (a warning until the live ØØ check), `dim_value_hidden` (an empty or
    digitless override, DIMTXT 0, 90 % or more transparency, the text in
    its own fill colour, a layer off, frozen or not plotted) and
    `dim_unbound` (info: not checked, never a pass). It runs under
    `focus=None` and inside `drawing_finalize`. Only `dim_render_stale` and
    `dim_group42_stale` are repairable: `drawing_refine` redraws them
    through the new backend contract method `dimension_rerender(handles,
    force)` (not a tool); every other code is `manual_required`.
  - `entity_edit_geometry` edits a linear or aligned DIMENSION's measured
    points (`x1` / `y1` / `x2` / `y2`) and redraws it; any other dimension
    type or argument is refused by name. `entity_scale` redraws a
    DIMENSION's number and group 42 (the other transforms follow in 1.8.1).
    Old `*D` blocks are purged; a text its author placed keeps its position
    (`text_kept`) unless `dimstyle_modify(force=True)`.
- **Tool policy (1.8-17).** `READ_ONLY=true` runs only `read` tools and
  writes no file. `TOOL_ALLOWLIST` / `TOOL_DENYLIST` take comma-separated
  names or globs; an entry naming no tool is warned about at startup and
  listed under `system_about.policy.unmatched`. `MAX_TOOL_COST=read|safe|
  mutate|destructive|escape` caps the `@cad_tool` cost a call may carry. A
  refused tool is not advertised, and a call to it - directly, through the
  search proxy's `call_tool`, or as any `cad_batch` step - is refused before
  anything runs with kind `policy_refused` `{policy, tool, allowed_cost,
  cost}`. `cad_batch` is judged per step: one refused step refuses the whole
  batch, `cad_batch refused all N steps - nothing was executed`. A tool
  hidden by the profile is still refused by name, never "unknown tool". The
  defaults are 1.7's: every tool runs. `system_status` and `system_about`
  report `policy` (`read_only`, `max_tool_cost`, `ceiling`, `allowlist`,
  `denylist`; `system_about` adds `unmatched` and an `http` block).
- **Write-path policy (1.8-17).** `security.validate_path(path, *,
  allow_write=False, kinds=None)`: every writer passes its extensions -
  `drawing_save` / `drawing_save_as` / `drawing_finalize(save_path)`
  `.dxf/.dwg/.dwt`; `drawing_export_dxf` `.dxf`; `drawing_export_pdf` and
  each `batch_plot` file `.pdf`; `drawing_export_dwg` `.dwg`;
  `drawing_template_save` `.dxf/.dwt`; `data_extract`, `pipe_takeoff`,
  `cable_takeoff` `.csv/.xlsx`; the P&ID lists' `csv_path` `.csv`;
  `drawing_finalize(screenshot_path)` `.png/.jpg`;
  `drawing_verify_requirements(report_path)` `.json/.md`; `drawing_deliver`
  and `batch_plot` take a directory. A file write is `mutate`-class whatever
  its tool's label, so under a ceiling below `mutate` (`READ_ONLY`,
  `MAX_TOOL_COST=read|safe`, a read-only token) every writer is refused
  `policy_refused` with cost `mutate`.
- **HTTP transport guard (1.8-17, `http_guard.py`).** A request whose Origin
  is not in `HTTP_ALLOWED_ORIGINS` is refused 403 (default: none listed); a
  Host that is not the bound host:port (loopback names always) or in
  `HTTP_ALLOWED_HOSTS` 421; a body over `MAX_HTTP_BODY_BYTES` (8 MiB; `0`
  disables) 413, by Content-Length and counted on a chunked body; two
  different Origin or Host headers 400. `*` in either list allows all. The
  refusal body is `{ok: false, error, message}`. Bearer tokens are compared
  in constant time against every configured token. `MCP_AUTH_TOKEN_READONLY`
  issues a token with only the `mcp:read` scope: its requests run under the
  `read` ceiling and its `tools/list` shows only what that ceiling allows.
  stdio is unaffected.
- **The AutoLISP read tier (1.8-18, F08).** `system_run_lisp` reads its
  expression (`security_lisp.py`: escaped strings, `;` and `;| |;`
  comments, quote, numbers, symbols, dotted pairs) and walks it. The read
  tier: arithmetic and geometry (`angle`, `distance`, `polar`, `inters`,
  `trans`), lists and control (`car` / `cdr`, `cons`, `list`, `mapcar`,
  `foreach`, `if`, `cond`, `progn`, `lambda`, `setq`), strings (`strcat`,
  `substr`, `rtos`, `vl-string-*`) and database reads (`entget`,
  `entnext`, `entlast`, `handent`, `tblsearch`, `tblnext`, `getvar`,
  `dictsearch`, `namedobjdict`, `sslength`, `ssname`, `ssget` with a literal
  `_X` / `_W` / `_C` / `_CP` mode, `vlax-ename->vla-object`, `vla-get-*`,
  `vlax-get-property`, `vlax-curve-get*`). All of these were refused by the
  1.7 denylist. `while`, `repeat` and `defun` are outside the tier, so a
  read-tier form always terminates; a higher-order function takes only a
  literal function. The docstring example is now
  `(vla-get-Area (vlax-ename->vla-object (handent "2A")))`. COM only: the
  headless engine still answers that it has no LISP.
- **Session metrics (1.8-14).** `system_status(metrics=True)` adds
  `metrics.session` - this connection's `calls`, `by_tool`, `result_bytes`,
  `errors_by_kind`, `p50_ms`, `p95_ms` - and, on the live engine,
  `metrics.com_run`: each COM job's queue / guard / work / refresh /
  counter ms. In memory and bounded (64 connections, 2,048 latencies). Off
  by default, so the status payload and the idle catalog are unchanged; no
  tool is added.
- **Token and call budgets (1.8-14, 1.8-13).** `benchmarks/token_budget.json`
  fixes the 1.8.0 ceilings, written before W2, each with a gate (`ci` or
  `release`) and an owner. `python -m benchmarks.token_suite --budget
  benchmarks/token_budget.json` exits 1 on a breached `ci` ceiling or an idle
  row grown more than 5 % over `benchmarks/results/published/token-cost.json`;
  `--release` also fails a release-gated cell over its ceiling or
  unmeasured. token_suite has a fourth lane, `task`: five scripted jobs
  (`sketch_20_entities`, `layer_cleanup_300`, `dimension_plate`,
  `inspect_foreign`, `history_value_lookup`), each run naive (one tool per
  entity, labelled as such) and batched (the least-call oracle in the new
  `benchmarks/call_budget.py`), with calls, characters and the modelled
  session input cached and uncached. An oracle whose verb is not on the tree
  reports `pending_tool`, never a pass. A new CI job, `token-budget`, runs
  the idle and task lanes against the budget and `python -m
  benchmarks.call_budget` on every PR. The H4 (`layer_cleanup_300`), H5
  (`dimension_plate`) and H1 (`history_value_lookup`, 1.8-10: 2 calls,
  `drawing_open` + `drawing_history(handle=, detail="args")`) oracles are
  measured and gated in CI.
- **Durable checkpoints (1.8-08, `backends/checkpoints.py`).** Every save
  keeps the bytes it just wrote, gzip level 1, at
  `<ACADMCP_STATE_DIR>/docs/<lineage_id>/cp/cp-<seq>.dxf.gz` - no second
  serialisation. `drawing_new` / `drawing_open` (S0, taken after the
  engineering bootstrap; a never-saved drawing writes nothing until its first
  checkpoint), every rollback-mode `cad_batch` / transaction boundary and the
  new `checkpoint_create(label)` add one; a checkpoint is never taken per
  call. Three new tools (core pack, `transaction` group, not in lean):
  - `checkpoint_create(label=None)` (cost `safe`) stores the drawing as it is
    now (`granularity: "label"` for the undo that lands on it) and returns
    `{cp, seq, tool, rev, kb, label}`;
  - `checkpoint_list(limit=10)` (`read`, 1-200) lists the active drawing's
    checkpoints newest first, across restarts:
    `{cp, seq, tool, rev, kb, label, evicted}`;
  - `checkpoint_restore(checkpoint, expected_revision=None,
    mode="in_place"|"new_document")` (`destructive`) takes an id (`cp-3`), a
    seq or a label (the newest with it). `in_place` first stores the current
    drawing (`checkpoint_before`), so the restore is itself undoable by
    `drawing_undo`; `new_document` opens the checkpoint as `<stem>-cp<seq>`
    and makes it the active document.

  Refused by kind: `checkpoint_not_found` (category `input`; the message
  names the newest ones), `checkpoint_evicted` (category `file`),
  `stale_revision`, and capability `checkpoints` (unsupported) when there is
  no state folder. Two capability keys in both maps: `checkpoints` (ezdxf
  mode `state_dir`) and `checkpoint_restore_in_place` (ezdxf mode
  `snapshot`). Live, a checkpoint is a `Document.Export`, taken at
  `checkpoint_create`, at `transaction_begin` (every rollback boundary and the
  `*_from_spec` transactions) and before `entity_delete_many`,
  `drawing_purge` and `text_find_replace`; `mode="new_document"` opens
  `<stem>-cp<seq>.dxf` and binds it, and `mode="in_place"` is refused with
  capability `checkpoint_restore_in_place` (ActiveX cannot replace a
  database's contents). Journal rows carry `checkpoint_before` /
  `checkpoint_after`, omitted when there is none.
- **A literal-handle pre-check (1.8-09, `engineering/changes/precheck.py`).**
  `cad_batch`, `entity_delete_many` and `entity_batch_modify` read every
  literal handle in a handle parameter with one `entity_fingerprints` call
  before anything runs. A handle that names no entity refuses the whole call
  with kind `unknown_handle` (`missing`: `[{i, tool, handle}]` per batch step,
  `[{index, handle}]` per list item; `executed` / `deleted` / `modified` 0),
  under every `on_error`, before any checkpoint opens. A literal handle after
  a step that opens, closes, switches or restores a document (`drawing_new`,
  `drawing_open`, `drawing_close`, `document_activate`, `document_close`,
  `checkpoint_restore`, `drawing_undo`, `drawing_redo`,
  `transaction_rollback`) is checked when its step runs (`not_found`). A dry
  run marks such a step invalid with kind `unknown_handle`.
- **Value history (1.8-10).** A new read tool, `drawing_history(limit=10,
  since_seq=None, tool=None, query=None, handle=None, label=None,
  detail="row"|"args"|"changes")` (core pack, `transaction` group, not in
  lean), reads the durable journal of the open drawing's lineage, including
  the calls of a server that has since restarted (reopen the file first).
  Rows are `{seq, op, tool, state, rev, cp, label, t}`; `args` adds the
  stored bounded arguments, `changes` each call's change summary and, for
  the first three rows, the checkpoint diff around it. `query` is
  case-insensitive over the tool, the label, the arguments and the
  `ACADMCP_*` feature tokens. A label no row carries is refused
  `invalid_args`, naming the labels there are. With it:
  - **labels:** `_meta.label` on any call, `cad_batch(label=)` and
    `checkpoint_create(label)` (1-200 printable characters; anything else is
    refused `invalid_args`, on a batch before anything runs);
  - **change summaries:** every mutating journal row and every `cad_batch`
    result carries `changes: {added, deleted, counts, complete}`, plus
    `features` (up to 20 `key=value` tokens of the `ACADMCP_MECH` / `ARCH` /
    `PID` payloads it drew). Headless it is a handle-set diff per layout and
    named block, once per call; live it is built from the handles the call
    returned (`complete: false`). A failed batch reports `partial_changes`,
    a rolled-back one `changes: {rolled_back: true}`;
  - **`drawing_diff(old_checkpoint=, new_checkpoint=)`** compares checkpoints
    by id, seq or label, and every diff now reports `feature` (a JSON-path
    diff of `ACADMCP_MECH` / `ARCH` / `PID` payloads, e.g.
    `{"features[2].d": {"from": 12, "to": 16}}`) and, for a DIMENSION,
    `measurement` from -> to. An exact geometric match whose payload moved
    counts as changed.

  The plugin eval case `history-round2` is written (not yet run with a real
  model).
- **Search by default, pinned tools, one instruction builder (1.8-11).**
  - `DISCOVERY_PINNED` (comma list, default `cad_batch,drawing_info,
    entity_list`; empty pins nothing): in search mode these are listed beside
    `search_tools` / `call_tool` / `describe_tool` as shortened cards (the full
    input schema, the first paragraph of the description plus "Full
    description: describe_tool."). A pin the profile or packs hide is not
    listed; an unregistered name is warned about at startup.
  - `describe_tool(names=[...])` returns up to 25 tool cards in one call.
  - `discovery/instructions.py` builds the server instructions per surface
    (`server._server_instructions(mode, profile, packs)`, at most 250 tokens,
    batch first). They name only tools the surface advertises - in search
    mode the three synthetic tools and the pins it lists.
  - The canonical skill gains a "Call budget" section (more than three
    consecutive calls to one tool is a missed batch tool, and a task -> one
    call table).
  - Two recipes, `autocad://recipes/layer_cleanup` and
    `autocad://recipes/dimension_part`, each one `cad_batch` (seven recipes).
  - Two plugin eval cases, `layer-cleanup-budget` and `dimension-budget`,
    which fail above 6 server calls after their setup.
- **Matrix v9 (registered, not the default).** `benchmarks/tasks_v9.py` adds
  eight W2 tasks to v8's 31 in the one `MATRICES` registry; `DEFAULT_MATRIX`
  stays `v8`: `dimension_lie_detect`, `policy_read_only`, `lisp_read_tier`
  (COM; headless `environment_blocked`), `layer_cleanup_budget`,
  `call_count_dimension_part`, `undo_across_restart` (draw, save, move, save,
  kill; a new process's one `drawing_undo` puts the line back; unsaved twin:
  the file untouched and `lost_since_save.count` 1), `atomic_batch_default`
  (move + delete of a missing handle with no `on_error`: `unknown_handle`,
  nothing executed; run-time twin rolled back, verified) and
  `history_value_lookup` (the round-2 diameter in at most two calls after a
  restart). In search mode the reference adapter's in-process MCP-client
  tasks reach every non-synthetic tool through `call_tool`, and the adapter
  reports `discovery_mode` and `proxied_calls` in its metadata, so a
  search-mode pass is visibly not vacuous.
- **Twelve W2 correctness checks** (each imports its 1.8.0 modules inside the
  check): `qa_coverage_not_checked`, `dimension_truth_focus` and
  `property_override_focus` (Quality gate), `dimension_display_truth` and
  `dimension_auto_one_call_part` (Dimensions), `policy_read_only_batch` and
  `write_path_autoload_refused` (Security),
  `search_default_lists_pins_and_batch_first` (Discovery), and
  `undo_after_restart`, `atomic_batch_default`, `delete_many_all_or_nothing`
  and `history_round2_value` (Durability): 67 → 79 checks.
  `property_override_focus` is the plan's section-8 check W1 did not add: a
  line on GEOMETRY given colour 1 and lineweight 30 is named by exactly two
  issues listing only its handle (`lineweight_not_bylayer` error,
  `color_not_bylayer` warning), a clean drawing yields none, and
  `drawing_refine` applies one repair per issue and leaves the line at
  colour 256 and lineweight ByLayer. The five later ones: the default
  surface lists exactly the three synthetic tools and the three pins, a tool
  it does not list still runs on a direct call, `describe_tool(names=)`
  answers two cards and the instructions teach `cad_batch` and name nothing
  unlisted; undo after a killed and restarted server lands on the saved state
  with `granularity: "save"`, and a killed unsaved session reports
  `lost_since_save.count == 1`; `[entity_move(L), entity_delete("DEAD")]`
  with no `on_error` is `unknown_handle` with L unchanged, and its run-time
  twin is rolled back, verified; `entity_delete_many([A, "DEAD"])` keeps A,
  and a delete that fails mid-call leaves A, B and C in the drawing; the
  round-2 diameter (12) is read back after a restart in two calls, labelled
  or from the checkpoint diff. All twelve pass here; eleven were run against
  a v1.7.0 tree by their items and fail or miss there, and the A/B report
  the numbers pass regenerates covers all of them.
- **Environment variables:** `READ_ONLY` (false), `TOOL_ALLOWLIST` /
  `TOOL_DENYLIST` (empty), `MAX_TOOL_COST` (`escape`), `LISP_POLICY`
  (`read`; an unknown value falls back to `read` with a warning),
  `HTTP_ALLOWED_ORIGINS` / `HTTP_ALLOWED_HOSTS` (empty),
  `MAX_HTTP_BODY_BYTES` (8 MiB), `MCP_AUTH_TOKEN_READONLY` (empty),
  `DISCOVERY_PINNED` (`cad_batch,drawing_info,entity_list`).

### W2: Changed

- **A lane the critique did not check is named (1.8-16).** The live
  engine's dimension-legibility lane reports `not_checked`
  (`engine_live_document_template`) instead of a silent `[]` that read as
  checked and clean. A check that writes no ledger row is recorded
  `check_unreported`.
- **A dimension is read as the sheet shows it (1.8-15).** A count prefix
  (`4x Ø13.5`, `4x M8`) is not the claimed size; `40x20` stays a size. A
  dimension measured to a point on an edge (not at its end) binds to that
  edge, which changes requirement verification only for dimensions that
  were `unknown`. A dimension text's MTEXT codes (`<>\PTYP`, `<>\XTYP`,
  `{\H0.7x;<>}`) are formatting: the displayed text is read through the same
  reader as the `*D` block. A headless redraw is all or nothing: a DIMPOST
  without `<>` renders as a suffix, as AutoCAD shows it, and a dimension the
  renderer cannot redraw is refused (kind `refused`) with the style, the
  scale or the edit put back and the revision unmoved.
- **`dimstyle_modify(rerender=True, force=False)` (1.8-15).** Headless, it
  redraws every dimension using the style (`rerendered`, `text_kept`). Live,
  each such dimension is `Update()`d and its `*D` text read back
  (`rerendered`, `already_current`, `stale`); the read costs two
  `Document.Export` reads per modify of a style in use. `rerender_required`
  now means a dimension still shows the old style: `rerender=False`, or a
  live redraw the read-back did not confirm.
- **`LISP_POLICY=read` (1.8-18).** A form outside the read tier goes to the
  1.7 denylist unchanged, and the denylist also re-scans every read-tier
  verdict: a match outside a symbol the reader approved is refused
  `denylist_second_pass`. Refused whatever the tier: interactive `get*`
  (all but `getvar`, `getenv`, `getcfg`, `getpropertyvalue`), `entsel`,
  `grread`, a mode-less or interactive `ssget` (they hold AutoCAD's single
  COM thread), `load` / `autoload` / `arxload`, `startapp`, `dos_*`,
  `vl-file-*`, `read`, a non-literal `eval` / `apply` / `mapcar` function,
  `vlax-create-object`, `vla-SendCommand`, `vlr-*`, `defun`, `c:*`, and
  unreadable text: unbalanced parentheses, an unterminated string or
  comment, a trailing `;` comment, a comment holding a parenthesis or a
  quote, a control character other than tab / CR / LF, nesting deeper than
  200. A refusal is `policy_refused` with `policy` (`lisp_allowlist`, or
  `lisp_denylist` under `LISP_POLICY=denylist`), `symbol`, `line` and
  `column` (1-based), `tier_needed` (`write` / `none` / `unclassified`;
  `read` when only the denylist mode refused a read form), `reason`
  (`never_runs`, `unreadable`, `outside_read_tier`, `denylist_second_pass`,
  `denylist`), `symbols`, `verbs` and `alternatives`. Refused symbols map to
  typed tools: `entdel` / `vla-delete` → ERASE, `entmod` / `vla-put-*` /
  `vlax-put-property` → PROPERTIES, `setvar` → SETVAR, `getpoint` /
  `getcorner` → ID (`user_pick_point`), `entsel` / `nentsel` / `ssget` →
  SELECT (`user_select`). The write tier is 1.8.1.
- **`TOOL_PROFILE=lean` gains `entity_modify_where`:** 67 tools (57 with
  `TOOL_PACKS=core`). `layer_rename` and `layer_merge` are full-profile, and
  so are the four W2 history tools.
- **Headless `drawing_undo` works without `EZDXF_UNDO_DEPTH` and after a
  restart (1.8-08).** It restores the newest checkpoint below the current
  revision and reports `granularity` (`save` / `batch` / `label`) and
  `checkpoint`; repeated undos walk back, never forward onto a state an undo
  replaced. With `EZDXF_UNDO_DEPTH` > 0 the in-memory per-call steps are used
  first, then the store. `drawing_redo` restores what an undo replaced -
  across the memory / store boundary too, within the session; redo across a
  restart is not kept. Capability `undo_history` on ezdxf is supported with
  mode `checkpoint`. Without a state folder (`JOURNAL_DURABLE=off`) and
  without `EZDXF_UNDO_DEPTH` the 1.7 `undo_history` refusal stays.
- **`drawing_open` reports `lost_since_save` (1.8-08)** `{count,
  last_operation}`: the committed calls a reopened file no longer holds
  because the server ended before they were saved (headless; a killed server
  loses nothing of a live document). `CHECKPOINT_KEEP` (20),
  `CHECKPOINT_BUDGET_MB` (512) and `CHECKPOINT_MAX_DOC_MB` (32), read but
  unused in W1, are enforced: per lineage the newest `CHECKPOINT_KEEP` rows
  besides S0 and at most `CHECKPOINT_MAX_DOC_MB`; across lineages the least
  recently written first, never the row just written, so the disk stays at
  or below the budget plus one checkpoint. An evicted row stays listed with
  `evicted: true`.
- **`cad_batch` defaults to `on_error="auto"` (1.8-09).** A batch with a
  mutating step and no document switch runs as `rollback` (headless: a
  document snapshot; live: an UNDO judged by the before/after fingerprints
  while CMDACTIVE is 0); a read-only batch, and one with a
  `transaction_begin` / `transaction_commit` / `transaction_rollback` step or
  a document switch, runs as `stop` and opens no checkpoint. If auto cannot
  open its checkpoint it runs as `stop` and says why (`atomicity.note`); it
  never refuses a batch 1.7 ran. The response's `on_error` echoes `auto`;
  `atomicity.mode` is what ran, with the new keys `default_applied`,
  `cmdactive`, `handles_checked` and `handles_changed`. Headless rollback
  evidence checks the pre-checked handles' presence; live it compares them
  exactly, and an AutoCAD that is busy or whose CMDACTIVE cannot be read
  gives `verified: None` with a "ROLLBACK NOT VERIFIED" note, never a claim.
  An explicit `on_error="rollback"` is unchanged.
- **`entity_delete_many` is all or nothing (1.8-09).** Every handle is
  pre-checked; the deletes run inside `transaction_begin` (live, that begin
  also takes the 1.8-08 checkpoint, so no second export); a failure rolls the
  others back and answers `ok: false`, `deleted: 0`, `rolled_back`,
  `verified`. A handle listed twice is deleted once (`duplicates_ignored`).
  Inside a transaction the caller opened on a live seat (the begin is
  declined) it answers `transaction: false` and a note that the deletes that
  ran stay applied. `entity_batch_modify` gets the pre-check only and stays
  non-transactional.
- **`system_status` is thin (1.8-10).** `operations` is `{count, last: {op,
  tool, state, rev}, unknown_commit}`: with 1,000 journal rows the output is
  at most 300 bytes larger than with none (a test holds it).
  `system_status(detail="operations")` returns the 1.7 list of the last 20
  records, for this release only. The durable journal pages its history
  reads newest first (no full-lineage read). `drawing_history(limit=10)`
  stays under 400 tokens (a test holds it).
- **`DISCOVERY_MODE` defaults to `search` (1.8-11).** A client lists
  `search_tools`, `call_tool`, `describe_tool` and the `DISCOVERY_PINNED`
  tools, and every tool stays directly callable by name (spike S1); the v8
  matrix passed 31 / 31 under the new default with its MCP-client tasks going
  through `call_tool`. `DISCOVERY_MODE=off` restores the 1.7.0 full catalog.
  The plugin starts the server with
  `DISCOVERY_MODE=${AUTOCAD_MCP_DISCOVERY:-search}`.
- **The server instructions are built per surface (1.8-11).** On the full
  catalog they teach batching first (`cad_batch` with `$name` references,
  `entity_batch_create`, `entity_modify_where`, `dimension_auto`, the
  `*_from_spec` tools); in search mode they point the client at those tools
  through `search_tools`. They name only tools the client can see and no
  longer the stale `titleblock_apply_iso_a3`. The
  three search tools' texts are one line each, 922 characters together, with
  lean wire schemas. The `system_status` docstring says operator object
  edits are counted.
- **Tests that read the whole catalog take the `full_catalog` fixture**
  (`tests/conftest.py`, `DISCOVERY_MODE=off` for that test, applied to both
  imported server modules); the rest of the suite runs under the search
  default.
- **token_suite's idle headline is the prompt payload (1.8-14)** - the tools
  array a client forwards to the model. The tools/list wire payload, the
  basis of 1.7.0's 89,226 and 503, is the `wire` column beside it. The idle
  lane gains `packs_core` and `instructions` rows (report schema
  `token/2.0`). `docs/tool-inventory.json` is schema 2 and carries `acad` /
  `synonyms` per row.
- **`perf_compare.py --base` defaults to `auto` (1.8-14):** the previous
  release tag (`git describe --tags --abbrev=0`, from `HEAD^` when a tag
  points at HEAD), refused by name when no tag is reachable, instead of a
  fixed v1.6.0.
- **The wheel and the Docker image ship `policy.py`, `http_guard.py` and
  `security_lisp.py`.**

### W2: Fixed

- **`data_extract` checked no extension (1.8-17).** It, and `drawing_save_as`,
  could write `acaddoc.lsp` or `acad.lsp`. No tool writes a file AutoCAD or
  Windows loads or runs - `*.lsp` (`acad*.lsp`, `acaddoc*.lsp`), `.fas`,
  `.vlx`, `.mnl`, `.scr`, `.arx`, `.dbx`, `.dll`, `.exe`, `.bat`, `.cmd`,
  `.ps1`, `.vbs`, `.js` and `acad.pgp` - even inside `ALLOWED_PATHS`
  (`path_refused`, reason `executable_name`); trailing dots and spaces are
  stripped first, and a `:` in the name (an NTFS alternate data stream) is
  refused. Reads of these names are not affected.
- **The LISP denylist could be escaped (1.8-18).** In 1.7, `1)) ERASE ALL
  ((` broke out of the server's `progn` wrapper and reached the command
  line, because the denylist knows only LISP symbols; the reader refuses
  unbalanced input. The denylist's `vla[xr]?-` pattern never matched
  `vlr-*`, so 1.7 ran reactors; the read tier refuses them
  (`LISP_POLICY=denylist` keeps the 1.7 gap, as the migration requires).
- **A dimension's reported text was not the sheet's (1.8-15).** On a DIMDEC
  0 sheet a 19.6 mm dimension was reported as showing "19.6" while it shows
  "20", and v1.7.0 scaled an angle by DIMLFAC.
- **No undo survived a restart (1.8-08).** In 1.7 the headless
  `drawing_undo` was refused (`undo_history`) unless `EZDXF_UNDO_DEPTH` was
  set, and its in-memory steps died with the process.
- **A batch that named a missing handle kept its earlier steps (1.8-09).**
  `[entity_move(L), entity_delete("DEAD")]` moved L, failed the delete and
  kept the move under 1.7's default; it is now refused before anything runs.
  `entity_delete_many([A, "DEAD"])` deleted A and answered `ok: true`.
- **`system_status` grew with every call (1.8-10):** it carried the last 20
  full operation records.
- **The server instructions taught one call per entity (1.8-11)** and named
  `titleblock_apply_iso_a3`; they never mentioned batching.

### W2: Breaking - migration table

| Change | 1.7.0 | 1.8.0 | Old behaviour |
|---|---|---|---|
| `strict_critique=True` (`drawing_finalize` opt-in, `drawing_deliver` default) | passes when the critique is empty | also fails on every `not_checked` lane except `domain_absent`, each a gate row with code `not_checked` (`critique_gate_failed`); on the live engine a default delivery is now `failed_validation`, because the legibility lane is `engine_live_document_template` | `strict_critique=False` |
| `drawing_topology_check(limit=)` default | `200` | `None`: still 200 rows per list without `summary`; 5 rows with `summary=True` | `limit=200` |
| `LISP_POLICY` | the 1.7 denylist | `read` (+ the denylist as a second pass) | `LISP_POLICY=denylist`; `LISP_POLICY=off` checks nothing |
| `system_status` | no policy | always carries `policy` (`system_about` too, with `unmatched` and `http`) | ignore the key |
| `meta["cad"]` on each tool | `summary`, `cost`, `acad`, `synonyms` | `summary`, `cost` (full-catalog tools/list 91,683 → 83,486 wire tokens, 1.8-14's measurement) | `describe_tool`, `docs/tool-inventory.json` (schema 2) |
| Headless `dimstyle_modify` | `rerender_required: true`, dimensions left stale | redraws (`rerendered`, `text_kept`); `rerender_required` only when a dimension still shows the old style | `rerender=False` |
| `dimension_auto` tool answer | a list of entity rows; a rerun duplicates | `{ok, created: [handles], skipped_existing, omitted, redundant?, layer, dim_overlap}`; `skip_existing=True` by default (the backend method still returns the list of drawn entities) | `skip_existing=False` for a rerun; `entity_get` on `created` for the rows |
| HTTP bearer tokens | one token, every tool | `MCP_AUTH_TOKEN` carries `mcp:read` + `mcp:write`; `MCP_AUTH_TOKEN_READONLY` only `mcp:read` (the `read` ceiling); if both are the same string that token is read-only, with a warning | leave `MCP_AUTH_TOKEN_READONLY` unset |
| HTTP Origin | not checked | a request with an Origin not in `HTTP_ALLOWED_ORIGINS` is 403 - a browser-based MCP inspector included | `HTTP_ALLOWED_ORIGINS=http://localhost:6274` (its origin), or `*` |
| HTTP Host | not checked | not the bound host:port and not in `HTTP_ALLOWED_HOSTS` → 421; a wildcard bind (`0.0.0.0` / `::`) serves only `HTTP_ALLOWED_HOSTS` and the loopback names | `HTTP_ALLOWED_HOSTS=<host:port>`, or `*` |
| Non-loopback HTTP bind | `ALLOW_REMOTE_HTTP=true` + a token | also requires `ALLOWED_PATHS` | set `ALLOWED_PATHS` |
| A writer's output path | any extension, or none | only the tool's own extensions; none, another or a loadable name → `path_refused` (`extension_not_allowed`, `executable_name`) | name the file with the tool's extension |
| `cad_batch(on_error=)` default (1.8-09) | `stop`: a failure keeps the steps before it | `auto`: a batch with a mutating step is all or nothing (rolled back on a run-time failure); a read-only batch, one with its own `transaction_*` step and one that switches documents run as `stop`; `atomicity.default_applied` says which | `on_error="stop"` |
| A literal handle that names no entity, in `cad_batch` (1.8-09) | the steps before it ran, then that step failed | the whole batch refused `unknown_handle` before any step (`missing`, `executed: 0`), under every `on_error` | fix the handles in `missing` |
| `entity_delete_many` (1.8-09) | deletes what it can; `ok: true` with an `errors` list | all or nothing: a missing handle refuses `unknown_handle` with nothing deleted, a delete that fails rolls the others back; `ok: false` whenever `errors` is non-empty; a duplicate handle is deleted once (`duplicates_ignored`) | read `ok`; send only live handles |
| `entity_batch_modify` with a handle that names no entity (1.8-09) | the other operations applied, an error row for it | refused `unknown_handle`, `modified: 0` | fix the handle |
| Headless `drawing_undo` without `EZDXF_UNDO_DEPTH` (1.8-08) | refused `undo_history` | steps back to the newest checkpoint below the current revision (`granularity`, `checkpoint`), also after a restart | `JOURNAL_DURABLE=off` keeps the refusal |
| `system_status.operations` (1.8-10) | a list of the last 20 records | `{count, last: {op, tool, state, rev}, unknown_commit}`; `drawing_history` lists the calls | `system_status(detail="operations")` (this release only) |
| `drawing_diff(old_path)` (1.8-10) | required | optional: `old_checkpoint=` / `new_checkpoint=` instead (id, seq or label) | pass `old_path` as before |
| Server `DISCOVERY_MODE` default (1.8-11) | `off`: the full catalog | `search`: `search_tools` / `describe_tool` / `call_tool` and the `DISCOVERY_PINNED` tools; every tool still callable by name | `DISCOVERY_MODE=off` |
| Plugin discovery mode (1.8-11) | no env (the server default, `off`) | `DISCOVERY_MODE=${AUTOCAD_MCP_DISCOVERY:-search}` | `AUTOCAD_MCP_DISCOVERY=off` |

### W2: measurements

Measured on this tree (`release/1.8.0` at `1d42556`, headless), unless a
report is named:

- **`python -m benchmarks.call_budget`** (exit 0): `sketch_20_entities` 20 →
  1 call, `layer_cleanup_300` 311 → 2 (ceiling 3; 1.7.0 124, the rival
  11), `dimension_plate` 23 → 2 (ceiling 2; 1.7.0 89, the rival 10),
  `inspect_foreign` 5 → 1; `history_value_lookup` is `pending_tool`
  (`drawing_history`, 1.8-10).
- **`python -m benchmarks.token_suite --lanes idle,task --budget
  benchmarks/token_budget.json`**, prompt basis: default 246 tools 67,230
  tokens (wire 86,021), lean 67 tools 19,629 (wire 24,627), `TOOL_PACKS=core`
  187 tools 46,661 (wire 60,805), search 3 tools 455 (wire 503), server
  instructions 461. Task lane, modelled 30-turn session input:
  `layer_cleanup_300` naive 27,961,921 uncached against batched 204,821
  uncached / 99,344 cached; `dimension_plate` 1,657,975 against 203,297 /
  98,312. **The budget exits 1 on this tree:** `idle.default` (67,230) is
  over its `ci` ceiling of 65,594, and `packs_core` grew 5.24 % over the
  published report (limit 5 %). `lean`, `packs_core`, `search` and
  `instructions` are over their release-gated ceilings (owners W3 and
  1.8-11), and the result-lane cells and `search_pinned` are not yet
  measured. The integration decides the `idle.default` cell.
- **QA answers** (1.8-16's report, `tests/test_qa_coverage.py`): a
  500-issue `drawing_critique(summary=True)` is 747 characters against
  79,096 for the full list (ceiling 2,500); finalize's `qa_coverage` is 136
  tokens on a clean sheet and 216 with three not-checked layouts (ceiling
  300).
- **Dimension truth at 2,000 dimensions** (1.8-15's review): snapshot
  0.68 s, `dimension_truth` 0.72 s, a `dimstyle_modify` redraw of all 2,000
  0.73 s, `drawing_critique(focus=None)` 1.79 s.
- **Correctness:** 74 checks here, each of the seven W2 checks failing or
  missing on a v1.7.0 worktree and passing on this tree. The published
  `ab-v1.7.0-vs-v1.8.0.json` is still over 67 checks.

Measured by the second-pass items on their own branches (their reports),
before the numbers pass retakes the tree:

- **Checkpoints (1.8-08):** on a 2,000-entity drawing the median
  `drawing_save` was 54.0 ms without the store and 53.7 ms with it (30 saves
  each, a new revision before every save); the gzip-1 copy itself is 0.6 ms
  (233 KB DXF -> 23 KB). The review measured a rollback-mode
  `transaction_commit` (290 KB snapshot) at 13.5-14.6 ms with the store on
  against 0.4-0.5 ms off - the item's "about 1 ms" did not reproduce.
- **Change summaries (1.8-10):** `perf_compare --aa` against the branch base
  `ok: true` (A/A spread 0.167, bound 1.335; `server_create_lines_2k`
  1.023), so the handle-set diff stays on every mutating call.
  `call_budget`'s `history_value_lookup` is 2 calls.
- **Search default (1.8-11):** token_suite's idle lane, prompt / wire:
  `search` 220 / 230 tokens (922 characters), `search_pinned` 1,003 / 1,277
  (6 tools), `instructions` 199 tokens (832 characters); the three cells moved
  from `release` to `ci`. The v8 matrix passed 31 / 31 under the new default
  (`proxied_calls` 9) and v9 33 / 33 on that branch (`proxied_calls` 41). The
  plugin eval cases, one real-model run each twice (sonnet, `--no-publish`,
  `--max-cost-usd 3`): `layer-cleanup-budget` 3 and 6 server calls (pass),
  `dimension-budget` 9 and 12 (over its 6 - the model re-read
  `dimension_auto`'s result handle by handle), so `real_model.plugin_eval`
  stays release-gated.

### W2: not in this pass, or not yet

- **1.8-15:** headless, ezdxf 1.4.4 draws a two-line angular dimension's
  reflex angle (315° for 45°); `dimension_truth` reports it
  `dim_render_stale` with `repairable: false` and `reflex_render: true`
  ("redraw it in AutoCAD"); a fix is proposed for 1.8.1. A DIMENSION inside
  a block definition is not checked, and no `not_checked` row says so yet.
  The live `dimstyle_modify` writes the style, then `Update()`s each
  dimension in one job with no rollback if one fails. Redrawing after
  move / rotate / mirror / copy / array, and inside block definitions, is
  1.8.1.
- **1.8-13:** `skip_existing` cannot match an existing DIMENSION that
  `bind_dimension` cannot bind (a 1.7 ordinate dimension to a point on no
  feature); a rerun duplicates such a one.
- **1.8-08:** redo across a restart is not kept (a restore after a restart
  goes through `checkpoint_restore`). Left from the review: the checkpoint
  index has no lock across processes (two servers on one state folder can
  take the same seq); a kill between an eviction's unlink and the index write
  leaves a kept row with no file, which `drawing_undo` refuses
  `checkpoint_evicted` instead of falling back; the budget scan reads every
  lineage on every add; a `new_document` restore's temporary folder is never
  removed; `checkpoint_restore`'s `mode` is a plain string (a bad one is
  `failed`, not a schema error) and its `expected_revision` check is not
  atomic with the restore; a live unsaved document's lineage is per session;
  a failed checkpoint is only logged.
- **1.8-09:** batch lease, `confirm_large` and batch budgets are 1.8.1.
  `system_launch(open_path=)` is neither a pre-check barrier nor a document
  switch for auto; a literal handle nested inside a step's arguments
  (`entity_batch_modify` `operations[].handle` in a batch) is checked by the
  inner tool at run time, so `executed` is not 0 then (auto still rolls the
  earlier steps back); the pre-check runs before `expected_revision`, so a
  stale client naming a deleted handle gets `unknown_handle`;
  `entity_delete_many` does not guard a `transaction_begin` that raises, and
  inside a live auto batch its "stay applied" note is wrong (the outer batch
  rolls them back).
- **1.8-10:** the plugin eval `history-round2` is written, not yet run with a
  real model; `call_budget`'s history oracle counts the two calls in one
  in-process client (the restart half is proven by the v9 task and the
  correctness check, which kill real server processes).
- **1.8-11:** `dimension-budget` measured 9 and 12 real-model calls against
  its ceiling of 6, and the trimmed skill row ("never `entity_get` per
  handle") was not re-evaluated. `describe_tool` refusals carry no typed kind
  (more than 25 names is a raw validation error), and `system_about` does not
  report `discovery_mode` or the pins. The `Details:` split of long
  docstrings was planned for 1.8.1; W3 did it (see *W3: Changed*).

**W2 live evidence pending.** AutoCAD 2026 was not running on 2026-11-03.
Written, not run: the `scripts/smoke_settings_com.py` stages `set edits`
(the ssget group codes, a proxy from a deleted SelectionSet staying
writable, a rename rewriting no entity, a merge reaching a sheet and a block
definition), `dimension truth` (whether a `*D` block is redrawn after DIMDEC
changes and by `Update()`, `entity_scale`, `%%c<>` printing ØØ, the
ExtLine1Point / ExtLine2Point read-back) and `dimension auto` (12 dimensions
in one call, the ellipse omitted, a second call drawing nothing); and
`scripts/smoke_lisp_com.py` (the LISP corpus in AutoCAD, `vla-get-Area`
against `analysis_measure_entity`, `getvar` CLAYER / INSUNITS / LTSCALE
against `system_get_variable`, no call left with CMDACTIVE set, the
`(command "_ERASE" ...)` twin refused). The COM `metrics.com_run`
breakdown, `dimension_rerender`, the live `dimstyle_modify` read-back and the
set-edit COM paths are covered by fake-COM tests only; `READ_ONLY` and a
read-only token on a live seat are unverified; the v9 `lisp_read_tier` COM
lane has not run. Also written and not run: `scripts/smoke_durability_com.py`
(1.8-08: `checkpoint_create` / `checkpoint_list`, `mode="in_place"` refused,
the transaction and pre-destructive checkpoints, a `new_document` restore).
The live `cad_batch` auto rollback (fingerprints while CMDACTIVE is 0), the
pre-check through `Document.Export`, the transactional `entity_delete_many`,
and the COM side of `drawing_history`, `drawing_diff(old_checkpoint=)` and
the live change summaries are covered by fake-COM tests only.

1.8.0, third phase (W3, "the named promises"): 1.8-19 to 1.8-24 and K1. A
drawn part is revised by its parameters in place, keeping its handles, and
refuses over a hand edit. A P&ID line goes round the vessels between its
ports, and a project of several sheet files is checked as one. A DWG opens
headlessly where AutoCAD's console is installed. The live engine refreshes
the display once per job and runs a bulk call as one job where the deadline
allows. The B40 feature tables get their double-entry pipeline but no row,
and the native .NET worker is deferred by a measured rule. Two tools are
added (`mech_part_revise`, `pid_project`) and one resource
(`autocad://pid/project/last`); `system_about` is the authority for the
counts. The correctness suite has 83 checks and matrix v9 had 44 tasks at the
end of W3; 1.8-25 added the last five section-8 rows (49) and made v9 the
default (see *Gate numbers* above).

### W3: Added

- **`mech_part_revise` (1.8-19, pack `mech`).**
  `mech_part_revise(part_id, changes, overwrite=False, dry_run=False,
  expected_revision=None, preconditions=None, verify=True)` changes a drawn
  part's model by path edits (`{"segments[1].length": 50,
  "features.bore.diameter": 24, "features.r1": null, "features+": {...}}`)
  and rebuilds its views in place in one transaction
  (`engineering/mech/revise.py`, through generic contract members only, so
  both engines). Tagged LINE / CIRCLE / ARC keep their handles (edited
  through `entity_edit_geometry`); a changed LWPOLYLINE / TEXT / HATCH is
  redrawn and listed in `handle_map: [{old, new, reason}]`, and a layer,
  colour, linetype or lineweight set by hand is carried onto the new entity
  (`handle_map[].kept`). Dimensions are deleted and placed again with the
  style, threshold and fits recorded on each view, so their handles change;
  a fit that no longer applies is in `dimensions.dropped_fits`, never
  re-applied silently. The hole table is rewritten and a balloon whose
  target was redrawn is re-linked. `verify=True` measures the stored
  `ACADMCP_REQUIREMENTS`; `model_is_current` in the result is read back with
  one drift read after the commit. Before anything is written every tagged
  entity's real geometry is compared with the stored model: a hand edit
  (moved, copied or deleted) refuses the call with
  `hand_edited_tagged_entity` naming the handles, a part drawn before 1.8.0
  (no `gen` stamp) or whose views the engine no longer reproduces with
  `model_drift`; `overwrite=True` replaces both with the model. A layer or
  colour edit is not drift. On a part drawn before 1.8.0, `overwrite=True`
  finds its untagged dimensions by the two points the old model measures and
  the hole table by the box its rows lay out; any other untagged dimension on
  a dimensioned view stays on the sheet and is listed in
  `dimensions.legacy.left`. Refused before anything is written: a bad path
  or value (`invalid_args`), a moved preview (`stale_revision`), and a detail
  view, a repeated view tag, a payload with no view or a stored model that no
  longer builds (kind `refused` with a `reason`). Not in lean.
- **Model history and drift (1.8-19).** Each revision appends `{rev, at,
  operation_id, changes: {path: [old, new]}}` to the part's XRECORD in the
  `ACADMCP_MODEL_HISTORY` dictionary (newest 200 kept), which travels with
  the file. `mech_part_inspect(drift=True)` adds `drift: [{feature, view,
  expected, measured, status}]` and `model_is_current`; `history=True` and
  `param="features.bore.diameter"` read the history (both together). New
  critique focus **`model_drift`** (mech domain, after
  `mech_bom_balloon_mismatch`): silent (`domain_absent`) without a part
  anchor; a part drawn before 1.8.0 is an `unstamped` info issue. New error
  kinds `hand_edited_tagged_entity` (category `geometry`) and `model_drift`
  (category `spec`), neither retryable.
- **Obstacle-aware P&ID routing (1.8-20).** `pid_line_draw(route="avoid",
  clearance=2.0, keep_out=[[xmin, ymin, xmax, ymax], ...],
  on_blocked="refuse"|"cross")` searches an orthogonal route that keeps
  `clearance` from every symbol that is not one of its ends and from every
  `keep_out` box (`engineering/pid/router.py::route_avoiding`, pure: A* over
  a sparse Hanan grid with escape lines beside each stub and a frame around
  the sheet; a bend and a crossing with an existing P&ID line each add cost;
  ties break by coordinate, so the result is deterministic). Two ports on
  one row or column always find a route on an open sheet; a route never runs
  back over its own stub or through its own port, and it goes round its own
  end symbols when it can (they are priced, never refused or reported). No
  route: kind `no_route` naming the tags of the enclosing symbols, or
  `on_blocked="cross"`, which runs along as few blocked grid edges as it can
  (not always the fewest boxes) and lists every box it enters. A search over
  its 50,000-node budget is `routing_budget_exceeded`. Both kinds are
  category `geometry`. `pid_from_spec` takes `sheet.routing: "auto"|"avoid"`
  (default `auto`, so no geometry moves); its dry run routes over the
  catalogue boxes with the same router, so the preview's vertices are the
  applied ones. Every route of either tool reports `obstacle_hits: [{handle,
  tag}]` and `label_hits` for a line-number label that overlaps a symbol
  (reported, never moved; `pid_from_spec` reports it on apply).
- **The `pid_line_through_symbol` focus (1.8-20).** A P&ID line segment that
  enters the box of a symbol which is not its end is an error; it runs under
  `focus=None` and inside `drawing_finalize` and is silent without P&ID
  symbols (`domain_absent`). Seven `pid_*` focuses now.
- **`pid_project` (1.8-21, pack `pid`).** `pid_project(action="check"|"index",
  sheets=None, manifest=None, write=None, include_current=True)` reads every
  sheet of a P&ID project with the `pid_graph` reader and never modifies a
  drawing (manifest `{project, sheets: [{path, number, title}]}`). `check`
  returns `links` per LINK id (`matched` / `orphan` / `direction_mismatch` /
  `duplicate`, each connector with its sheet and handle), cross-sheet
  `duplicate_tags` (a continuation connector's TAG never counts) and
  `line_continuity` (the line number on both sides of every matched off-page
  pair), plus `summary` and `clean`; `index` returns the instrument index,
  line list and equipment list merged with a `sheet` column. Sheet files are
  read on a private, pathless headless engine and cached by sha256; a
  `.dwg` sheet is read only on the live engine (opened read-only, exported,
  closed) and refused headlessly with capability `dwg`; the active document
  is read as it stands and replaces its own file when listed. More than 50
  sheets are refused `invalid_args` by name, and a missing sheet file
  `file_not_found`, before anything is read. `write=` (`.json` / `.csv` /
  `.xlsx`) is the only file written; `.xlsx` without openpyxl is refused with
  `xlsx_write`. Cost `safe`, so `READ_ONLY` hides it. New resource
  **`autocad://pid/project/last`**: the last result of this server process,
  or `available: false`.
- **The B40 double-entry pipeline (1.8-22).** Rows for DIN 471/472, DIN 509
  (forms E / F), DIN 332-1 (A / B) and ISO 3601-2 are typed twice into
  `engineering/mech/standards/data/<std>.a.json` and `.b.json`, and
  `scripts/build_standards.py` generates `engineering/mech/standards/_rows.py`
  from them. The build refuses any disagreement between the entries by path,
  a filled table without two different named people (`TRANSCRIBED_BY` /
  `CHECKED_BY`) or a complete `ROW_SOURCE` (edition, table, page), an
  edition other than the one the module's `SOURCE` names, and a broken
  physical invariant (DIN 471 d2 < d1 with m non-decreasing, DIN 472 d2 > d1,
  DIN 509 depth > 0 and a monotone profile, DIN 332 d2 > d1 and t > 0, O-ring
  h < cord and b > cord); `--check` fails while `_rows.py` and the data
  disagree. **No row is transcribed in 1.8.0** (see *Not in 1.8.0*).
- **Headless DWG read (K1, `backends/dwg_read.py`).** `drawing_open` on a
  `.dwg` converts it to DXF through the ODA File Converter, then AutoCAD's
  `accoreconsole.exe` (`ACCORECONSOLE_PATH`: unset means AutoCAD 2026's
  installed console when it exists, `off` disables the route;
  `ACCORECONSOLE_TIMEOUT_S`, default 60 s, kept inside `EZDXF_CALL_TIMEOUT`).
  The result carries `dwg_read {route, seconds, exit_code, log,
  dxf_version}`; a route that fails falls through to the next, and a success
  after a failed one reports `failed_routes`. The console writes DXF 2018
  (AC1032). It runs on a copy in an ASCII staging folder under
  `<ACADMCP_STATE_DIR>/accore`, from a script of only generated `_`-prefixed
  commands - nothing from the caller enters the script or the command line -
  with its UTF-16LE log read back and its process tree killed on an overrun;
  the folder is removed on every path and stale ones are swept after an
  hour. The opened document keeps its `.dwg` path, so a save onto it is
  still refused (`dwg`); save it as `.dxf`. `recover=True` on a `.dwg` stays
  `recover_unsupported`. Capability `dwg_read` in both maps: ezdxf per call
  `odafc` | `accoreconsole` | unsupported, COM native. New error kind
  `dwg_conversion_failed` (category `dependency`, not retryable, record
  `refused`) with `attempts[]` (route, reason, exit code, `timed_out`, log
  tail).
- **ADR 0001, the native .NET worker (1.8-24).** `docs/adr/` (index in
  `docs/adr/README.md`) holds the architecture decisions;
  `docs/adr/0001-native-worker.md` defers the worker to 1.9.0 under a rule
  fixed before any measurement (see *Not in 1.8.0*). It records no
  measurement.
- **`benchmarks/perf_com.py` (1.8-23).** Measures the live engine against a
  v1.7.0 checkout (`--base`), each side in its own child process, one untimed
  warm-up per workload, on a document seeded with 5,000 lines:
  `create_circle_singles`, `batch_create_200`, `delete_singles_200`,
  `delete_many_200`, `cad_batch_50`, `fillet_50_n5000` and
  `fillet_50_n10000`. Its targets were fixed before the run: the bulk tools
  at least 5× faster than v1.7.0's 200 singles, the single
  `entity_create_circle` median at least 30 % lower, and `entity_fillet` at
  10,000 lines at most 1.2× its time at 5,000.
  `benchmarks/results/live/perf_com.json` is the `--pending` skeleton
  (`status: "pending"`, every verdict `pass: null`) until that run.
- **Matrix v9, W3 tasks:** `pid_project_offpage` (three sheet files: one
  orphan, one direction mismatch, one cross-sheet duplicate tag and one
  line-number mismatch, each once with sheet and handle; a missing sheet and
  51 sheets refused by name), `std_tables_b40` (one size per filled B40
  table, a between-rows size refused by name; with no table filled it is
  `not_run` naming the deferred tables, never a pass),
  `dwg_headless_roundtrip` (the bundled `templates/iso_a3_mech.dwt` under a
  `.dwg` name opened through a converter and saved as DXF with its DXF twin's
  layers, blocks and entity counts; no converter: `environment_blocked`),
  `parametric_revise_keeps_handles` (a dimensioned three-view flange revised
  in one call, in-place handles kept, signatures equal to a fresh draw; its
  twin `revise_hand_edit_refused` is checked inside the task) and
  `pid_routing_obstacles` (zero `pid_line_through_symbol` and dangling ends
  with `sheet.routing: "avoid"`; a walled-in pump refused `no_route` naming
  the four vessels). v9 holds 44 tasks; `pid_roundtrip` (v4) is pinned to
  `sheet.routing: "auto"`.
- **Four W3 correctness checks:** `pid_project_links` and
  `pid_avoid_no_symbol_crossing` (P&ID), `b40_between_rows_refused` and
  `mech_revise_keeps_handles` (Mechanical): 79 → 83 checks.
  `b40_between_rows_refused` proves the pipeline on a synthetic double entry
  (round numbers, not DIN values) and every empty table's refusal by name.
- **Environment variables:** `ACCORECONSOLE_PATH` (unset: AutoCAD 2026's
  console when installed; `off`), `ACCORECONSOLE_TIMEOUT_S` (60),
  `COM_REFRESH_UPDATE_MAX` (1, minimum 0), `COM_REFRESH_MODE` (`auto` |
  `regen`, default `auto`; an unknown value falls back to `auto` with a
  warning).

### W3: Changed

- **Part payloads drawn by 1.8.0 carry `gen` and a per-view `sha`
  (1.8-19)**; `mech_view_add` records its `gap` and `mech_dimension_part`
  each view's `dim` settings, which is what a revision re-places.
- **ISO 3601-2 is two tables (1.8-22),** keyed by cord diameter with their
  own coverage: radial static (`ISO 3601-2`, the unchanged name
  `std_feature_draw` uses) and axial (`ISO 3601-2-axial`);
  `oring_groove_dims(cord, kind)` reads the table of its kind.
  `autocad://standards/coverage` lists the axial table ("no rows
  transcribed"). DIN 509 rows are r, t1, f, g (t2 for form F), and
  `undercuts.profile_from_dims` builds the relief profile from them by a
  documented construction that refuses anything else by name. DIN 471 and
  DIN 472 have their own `SOURCE` lines.
- **One display refresh per COM job (1.8-23).** A live path records a
  refresh with `_regen(<the entity it wrote>)`; the job flushes once in its
  `finally` (`_flush_refresh`, also after a failure): `Update()` on the last
  entity when the job asked at most `COM_REFRESH_UPDATE_MAX` times and every
  request named an entity, otherwise one `Regen(acActiveViewport)`.
  `COM_REFRESH_UPDATE_MAX` defaults to 1 while spike S4 (does `Update()` of
  the last entity paint the earlier ones?) is unmeasured, so a single create
  is one `Update()` and anything more one Regen; `COM_REFRESH_MODE=regen` is
  the fallback for a graphics setup where `Update()` does not paint.
- **`entity_batch_create` and `entity_delete_many` are contract methods
  (1.8-23).** Headless they are the 1.7 server loops (results unchanged; a
  non-string `type` is now an `Unknown type` row instead of a crash). On a
  live seat each runs in as few STA jobs as `COM_CALL_TIMEOUT` allows: a job
  takes items until it has spent a quarter of the deadline (at least one
  item each), with one bound-document guard, one operator-counter begin /
  end and one refresh per job; a list that fits is one job, and the call is
  one revision step (both are in `_MUTATING_METHODS`, 103 -> 105). Each
  item's error row carries its kind (`unknown_handle`, `invalid_args` for a
  spec refused before anything was drawn); a lost connection ends the call.
  A bulk call can be cancelled between jobs, not inside one. The tool-level
  rules are unchanged: `entity_delete_many` is still pre-checked and all or
  nothing (1.8-09), `entity_batch_create` leaves earlier items written when
  one fails (atomicity is `cad_batch`'s).
- **Corner operations read only the target block (1.8-23).** `entity_trim` /
  `entity_extend` / `entity_fillet` / `entity_chamfer` pass `owner=` to
  `_safe_send_command`, which reads that block's count and last handle
  before the command and its new tail after, so their cost no longer grows
  with the drawing; a paper-layout target also reads model space's tail, so
  an entity written there is still `created_outside_target_space`.
  `system_run_command` reads each layout block from its end.
- **Long descriptions are listed short, the whole text one call away (W3
  token ceiling).** `discovery/descriptions.py` lists a tool description over
  800 characters as its leading whole paragraphs plus `Details:
  describe_tool.` (47 of 257 tools) on the full catalog, `TOOL_PROFILE=lean`
  and every `TOOL_PACKS` surface; the registered tool keeps its docstring and
  `describe_tool` returns it whole. `DISCOVERY_MODE=off` now lists
  `describe_tool` as well (`DetailsTransform`), so the pointer leads somewhere
  on every surface; search mode is byte-identical and is indexed on the full
  text. `lean` (16,182 prompt tokens) and `packs_core` (40,722) meet their
  ceilings and flip to `ci`; the default `off` surface is 59,138.
- **`drawing_finalize`'s docstring and the P&ID prompt template name
  `pid_line_through_symbol` (1.8-20);** the prompt's step 5 routes with
  `route="avoid"` and expects `obstacle_hits` empty.

### W3: Breaking - migration table

| Change | 1.7.0 | 1.8.0 | Old behaviour |
|---|---|---|---|
| Headless `drawing_open` of a `.dwg` (K1) | a `ToolError` sentence | converted through ODA, then accoreconsole; no converter: a typed capability refusal (kind `unsupported`, capability `dwg_read`, naming both converters); every route failed: `dwg_conversion_failed` with `attempts[]` | `ACCORECONSOLE_PATH=off` (and no ODA) keeps the refusal |
| Live display refresh (1.8-23) | a Regen after each created entity | one refresh per COM job: `Update()` of the entity for a single create, one `Regen` for anything more | `COM_REFRESH_MODE=regen` (one Regen per job); there is no per-entity mode |
| Live `entity_batch_create` / `entity_delete_many` (1.8-23) | one STA job and one revision step per item, each item its own `COM_CALL_TIMEOUT` | jobs sliced at a quarter of `COM_CALL_TIMEOUT`, one revision step per call, cancellable only between jobs | — |
| `drawing_critique(focus=None)` on a part drawn before 1.8.0 (1.8-19) | no such focus | one `model_drift` info issue (`unstamped`) per part, so "zero issues" needs the part revised | `mech_part_revise(part_id, changes, overwrite=True)` redraws it with the 1.8.0 stamp (`changes` may not be empty; restating a current value is accepted and recorded as a no-op pair) |

## [1.7.0] — 2026-10-26

1.6 taught the server to draw whole sheets and to read other people's
drawings. 1.7.0 makes what it draws **provable** and what it runs **safe**: a
source requirement ("Ø40 bore, six holes on a Ø120 circle") is bound to the
real CAD feature, measured on the geometry - never on a label, a stored model
or the numbers that were typed in - and measured again on the saved file
after it is reopened, with a pass / fail / unknown verdict per requirement and
the evidence behind it; and a cancelled call, a big drawing or a hostile text
cell can no longer produce a silent wrong answer. 247 → 248 registered tools
(one new: `drawing_verify_requirements`), 8 → 11 resources, 5 prompts; the
correctness suite at 61 / 61 against v1.6.0's 48 / 61 with zero regressions;
benchmark matrix v8 at 31 / 31; the live COM harness at 129 / 129 checks
over eight smokes on AutoCAD 2026; and no workload slower than 1.6.0
beyond the measured noise (v1.7.0 / v1.6.0 ratios 0.62 to 1.02 over 20 alternating rounds of
`perf_compare.py` on an idle machine, A/A spread 5.5 %,
`perf-v1.6.0-vs-v1.7.0.json`). Headless creation
throughput against v1.4.0 is still not won back: **Not in 1.7.0**
says where it went. Spec: `docs/superpowers/specs/2026-10-24-v1.7.0-design.md`.

### Added

- **Requirement verification - `drawing_verify_requirements` (SECTION 26,
  4 → 5; pack `core`, in the lean profile).** One engine under
  `engineering/verification/`:
  - *Typed requirements* (`requirements.py`, `types.py`): id, quantity
    (distance, radius, diameter, angle, concentric, coincident, parallel,
    perpendicular, horizontal, vertical, tangent, count, absence), target with
    its unit, feature selectors, provenance, source, datum, and a
    manufacturing tolerance kept apart from the verification epsilon. Every
    bad value is refused by its path; conflicting provenance and two targets
    for one feature are kept and reported, never collapsed. A legacy
    `dimensions` object is carried read-only and verifies nothing.
  - *Observed geometry* (`observe.py`): INSERTs exploded with their
    `block_path` (a circle under a non-uniform scale is an ellipse and says
    so, a mirrored insert keeps an arc's true sweep, every MINSERT cell is
    its own element), HATCH loops with their depth and filled area, DIMENSION
    fields with the geometric measurement, the dimension's own measurement,
    the displayed text and `DIMLFAC` kept apart. `entity_get` reports HATCH
    loops and full DIMENSION fields on both engines (capability
    `hatch_loops`).
  - *Binding* (`binding.py`): by handle, feature tag, architectural element,
    geometric predicate or DIMENSION. Every binding keeps its candidates and
    its reason; two equal candidates are `ambiguous` with both listed, never a
    pick. The mechanical generators tag each entity they draw with an
    `ACADMCP_MECH` feature record in the create call itself
    (`entity_create_line/circle/arc/polyline/text/hatch` take `xdata=` on
    both engines). Held-out evidence on raw-ezdxf plates no fixture uses:
    733 of 760 eligible bindings attempted, all 733 correct, the 27 that had
    to abstain abstained.
  - *Evaluators* (`measure.py`): every quantity measured from CAD-read
    geometry with value, unit, method, residual and status; lengths
    converted with exact factors in the drawing's declared unit (`$INSUNITS`;
    an inferred unit is recorded beside it, never applied); splines and tilted
    planes `unsupported`, degenerate cases `unknown` with the reason.
  - *The acceptance report* (`report.py`): one row per requirement - target,
    measured, delta, tolerance basis, status, handles, source - with
    denominators that always add up, as JSON (schema 1) and Markdown. No
    silent pass: unbound or unmeasured is `unknown`. A DIMENSION whose text or
    `DIMLFAC` disagrees with the geometry is a blocking `dimension_mismatch`
    finding beside the geometry row. Evidence turns stale when the document's
    revision moves or an artifact's sha256 changes.
  - *The saved file* (`artifact.py`): `verify_artifact` reads the saved DXF
    fresh (a DWG on a live seat through a private read-only copy) and
    re-binds every feature - by handle while handles are stable, then by
    feature tag, then by geometry - with the before / after comparison; a
    count or absence is counted afresh on the file.
  - `drawing_plan(persist_requirements=True)` writes the set into the drawing
    (an `ACADMCP_REQUIREMENTS` XRECORD, both engines), so a reopened file
    carries it; `drawing_verify_requirements(use_stored=True)` reads it back.
    `drawing_finalize(requirements=)` and `drawing_deliver(requirements=)`
    add `geometry_acceptance`, `drafting_quality` (the unchanged score),
    `artifact_integrity` and `coverage`: a requirement that does not pass
    blocks them (`failed_acceptance`) whatever the drafting score.
    `drawing_verify_requirements(manifest_path=)` re-hashes a delivery's
    artifacts and lists the stale and missing ones.
- **Document identity and revisions (B11).** Every open document has a
  `document_id` and a content `revision` (`system_status`, `drawing_info`,
  `document_list`). Headless, the revision moves on every mutation, undo,
  redo and rollback, never on a save, export, finalize or delivery; live, on
  each of the 98 content-changing `ComBackend` methods once the call hands
  work to AutoCAD (an argument refusal before that moves nothing, as
  headlessly). The live engine binds the document a call chose, and a call
  made after the operator switched AutoCAD's active document is refused
  `active_document_changed` (naming the bound and the active document) before
  anything is read or written - `view_screenshot` included, checked again
  after the capture - and the QA readers (validator, critique, delivery) pass
  the refusal on typed instead of reporting a failed check;
  `transaction_commit` / `transaction_rollback` address the bound document.
  A document belongs to one running AutoCAD: after a restart even an unsaved
  `Drawing1.dwg` is a new document with a new id (`application_changed`).
  The live revision also moves, once per gap between two server calls, when
  the operator edited database objects of the bound document by hand: a
  transient AutoLISP database reactor counts them in the document's own
  namespace and the server reads it through `VL.Application.16` - no COM
  event sink, no `SendCommand`, nothing saved into the drawing, 0.6-1.2 ms
  per read (median, two live runs) and two reads per guarded call. A preview or precondition check reads
  it first, so a stale `expected_revision` is refused by the revision itself;
  `system_status.operator_edits` reports whether it is counting. Two servers
  bound to one document (two clients on one seat) each keep their own id in
  the shared namespace, so a read by one never moves the other's revision; a
  write by one is an outside edit to the other.
- **Operation records and client ids (B12).** One record per call in a
  bounded in-memory journal (operation id, tool, request hash, state,
  document id, revision before and after, error kind); states `committed`,
  `partial`, `failed`, `refused`, `quarantined`, `cancelled`,
  `unknown_commit`. A client id (`cad_batch(operation_id=)`, or
  `_meta.operation_id` on any tool) replays the recorded result for the same
  request, refuses `operation_conflict` for a different one and
  `requires_reconcile` after an `unknown_commit`. `system_status` lists the
  recent operations.
- **Change previews (B14 residue).** `cad_batch`, `drawing_refine`,
  `text_find_replace` and `pid_from_spec` return an exact `preview` on a dry
  run (entity and region fingerprints over the exported DXF tags); an apply
  with `expected_revision` / `preconditions` runs only if nothing moved and is
  refused `stale_revision` with nothing changed otherwise. The apply reports
  `revision` {checked, after, interleaved}.
- **Task recipes (B51).** Five versioned recipes under `engineering/recipes/`
  - dimensioned part → verify → deliver, foreign P&ID → instrument index /
  line list / equipment list, revision pair → takeoff delta, A3 sheet
  delivery, foreign-drawing audit - each with preconditions, `cad_batch`-ready
  steps, the evidence each step leaves and postconditions a script checks;
  served as `autocad://recipes/index` and `autocad://recipes/{recipe_id}`, and
  each run end to end on ezdxf in a test.
- **Standards coverage manifest (B39).** Every registered table's standard,
  edition, locator, unit, implemented sizes, coverage range and SOURCE, in
  `std_part_list(...).coverage` and the resource `autocad://standards/coverage`.
  A size outside a table is refused with the nearest transcribed sizes.
- **`describe_tool` (B50).** `DISCOVERY_MODE=search` gains a third synthetic
  tool: one tool's own input schema, description and annotations (equal to the
  full-mode `list_tools` entry), its risk, cost, pack, AutoCAD commands and
  synonyms, and only the examples its docstring writes itself.
- **The premium skill, restored and packaged as a Claude Code plugin (B05,
  B54, B55).** The canonical skill is `.claude/skills/autocad-mcp-premium/`
  (a ~2.4k-token `SKILL.md` with a decision table by source type and the
  workflow states inspect → requirements → plan → geometry → verified →
  annotated → drafting-verified → saved → reopened → delivered, eleven
  references, five examples whose numbers come from headless runs).
  `plugin/` is the plugin: a generated copy of the skill
  (`scripts/build_plugin.py --check`), an MCP server pinned to this release
  (`autocad-mcp-pro[com,pdf,office]==1.7.0`, overridable with
  `AUTOCAD_MCP_SOURCE`), and the repository-root marketplace. Install:
  `claude plugin marketplace add U-C4N/Autocad-MCP`, then
  `claude plugin install autocad-mcp-pro@autocad-mcp`. Twenty negative eval
  cases under `plugin/evals/` for `claude plugin eval`, and
  `benchmarks/skill_eval/check_artifacts.py` to re-measure a run's saved
  drawings.
- **The live COM release harness (B63).** `scripts/live/harness.py`: attach
  with `GetActiveObject` only, its own documents closed with `Close(False)` on
  every path, an active-document guard, one shared `RPC_E_CALL_REJECTED`
  retry, a JSON result per smoke and exit codes. The five
  `scripts/smoke_*_com.py` assert on it (the P&ID smoke no longer passes on
  `dangling` alone), and `scripts/smoke_verify_com.py` covers the bound
  document, HATCH and DIMENSION read-back, Export delivery and saved-file
  verification. `scripts/smoke_document_com.py` measures the bound-document
  guard itself on two unsaved documents of its own (the refusal, the
  screenshot refusal, the hand-back after a snapshot, re-binding, the
  application identity), and `scripts/smoke_operator_edits_com.py` measures
  which operator edits move the live revision and what the counter costs.
  The results are committed under
  `benchmarks/results/live/`.
- **Benchmark matrix v8 (B56).** v7 plus ten tasks: `requirements_roundtrip`,
  `cancel_safety`, `qa_large_drawing`, `export_safety`, `stale_preview`, and
  the rivals' ground (`revision_guarded_edit`, `pid_file_roundtrip`,
  `foreign_edit_verified`, `damaged_dxf_refusal`, `dimension_binding`). A
  rival lane whose environment is absent is `environment_blocked`, a task it
  was never given `not_run`; neither enters a score. Every report carries a
  run manifest (server SHA, dirty flag, fixture hashes, evaluator version,
  dependencies). Nine correctness checks for the roadmap scenarios T05,
  T09–T13, T18, T25, T36, and `compare_versions.py --fail-on-regression`
  (exit 1 on any check that passed on the baseline and does not pass now).
- **Release gates (B64).** CI validates the marketplace and the plugin with
  `claude plugin validate --strict` (Claude Code pinned), runs
  `build_plugin.py --check`, the inventory `--check` and the document numbers,
  and checks the built wheel and sdist (`scripts/check_dist_contents.py`: the
  wheel is the server and nothing else - no plugin, no skill - and the sdist
  carries only what git tracks). The release workflow adds a headless lane
  running the A/B suite against the previous tag with
  `--fail-on-regression` before anything is built.
  `tests/test_release_consistency.py` holds one version in all seven places a
  release writes it.
- `point_intersection` reports whether the point lies on both drawn segments
  (`on_first`, `on_second`, `on_both`, `candidates`).
- Environment variables `MAX_QA_ENTITIES` (default 2,000,000; a QA read of a
  larger drawing is refused `scan_incomplete`), `SHUTDOWN_GRACE` (5 s),
  `MAX_SPEC_ITEMS` (2,000) and `MAX_VESSEL_TRAYS` (500).

### Fixed

- **A cancelled call released the document while its worker still wrote
  (B01).** The document lease now follows the worker thread: the next call
  waits for it, with `EZDXF_CALL_TIMEOUT` on and off; a worker still alive at
  the call's deadline is quarantined (`cancelled_then_deadline`). A read-only
  overrun is never quarantined: it is detached, other calls on its document
  are refused `busy` by name while `drawing_open` / `drawing_new` /
  `drawing_close` / `document_activate` / `document_close` and `document_list`
  go through without waiting for it, and no restart is needed (with
  `EZDXF_CALL_TIMEOUT=0` nothing is detached: a runaway call holds every
  call until it ends). A read that computes holds the GIL, so answers stay
  slow until it ends - measured in the README. A call's document is taken
  once it holds the lock, not at entry. A cancelled `transaction_begin` no
  longer leaves its checkpoint open, and a cancelled `cad_batch` checks its
  rollback's answer and fingerprint and closes any checkpoint a step opened
  above its own; a batch that succeeds closes it too and reports
  `atomicity.unwound`, and a rollback that ends in `ROLLBACK NOT VERIFIED`
  or `CHECKPOINT LEFT OPEN` says so. An error that escapes a batch without a
  cancellation is recorded with its own cause, not as a cancellation. A QA
  read that overruns is detached, never quarantined, also when `_async` is
  wrapped (the read-only mark travels on the function). Workers run on
  daemon threads; `call_outcomes` are in `system_status`.
- **QA scans stopped early and failed open (B02).** `drawing_critique`, the
  validator, `drawing_finalize` and `drawing_deliver` read one snapshot of
  every space and every entity (a construction line at position 5,001, a
  duplicate after the 5,000th line or anything on a paper layout used to
  read as clean). A check that raises is an error issue with `detail.code`
  `check_failed`, an incomplete read `scan_incomplete`; `drawing_refine`
  never hands either to a repair handler. `untrimmed_corner` and
  `dim_overlap` run off the event loop on a grid (8.9 s at 5,000 lines, the
  server answering nothing, before). `construction_clear` clears every space
  with no cap, and `entity_delete` deletes from the entity's own layout.
- **Exported text could become a spreadsheet formula (B03).** One cell
  serializer for every CSV and XLSX the server writes: a text cell that
  starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading
  `'` in CSV and is a string cell in XLSX; numbers stay numbers. The parts
  list's item, quantity and mass are numeric XLSX cells. The P&ID list tools
  write a file when given `csv_path`, so they are now `readOnlyHint: false`,
  cost `safe`.
- **Spec inputs (B07).** One set of guards (`engineering/validation.py`)
  refuses NaN, infinite and out-of-range values by their path in the P&ID
  vessels and spec, the P&ID XDATA payloads, the architectural plan spec,
  the part model and `block_define`; `pid_from_spec`'s dry run refuses
  exactly what the real run refuses.
- **`drawing_export_dxf` re-pointed the open document on a live seat.** It now
  writes a copy on both engines (live through `Document.Export`); the
  document's name, path and saved flag are untouched. `drawing_save_as` keeps
  re-pointing, as it should. Delivery parity compares every space and every
  block definition against a fresh serialisation of the document, so a hole
  moved inside the outline of the delivered file fails it. Both sides are
  exports, so parity cannot see content the export itself loses; its record
  says so (`scope`) and names the check that can (`content_check`: the
  requirements measured on the delivered file). The manifest (schema 1.1)
  records the document id and revision, the export method, the requirements
  hash and the report hash.
- Saved-file verification re-bound by handle onto whatever feature now held
  the handle: a partial renumbering that gave a bore's handle to a pin of the
  same type measured the pin. The handle stage now also needs the source's
  geometry, or an in-place edit: no other feature carrying it except an
  unchanged twin that already had it at its own handle (a double-drawn bore
  edited in the file is measured and fails, never passed on the twin); an
  edit beside a new feature drawn at the old geometry is ambiguous, the tag
  stage keeps to the source's space, and a source binding without a handle is
  never reported as "re-bound". An open document that is not shown to be the
  file's source (fewer than half of the entities match) makes the report
  `provenance.source_matches: false` with a blocking
  `artifact_source_mismatch`: nothing is re-bound from it, no comparison is
  made, and a handle of the other drawing names nothing on the file.
- `text_find_replace` on a live seat read a narrowed `IAcadEntity` without
  `TextString` and raised; it also rewrote the measurement text inside a
  DIMENSION's anonymous block on both engines. Anonymous blocks are skipped,
  and the search covers `TEXT_SEARCH_SCOPE` on both engines.
- The 74 backlog rows deferred to 1.7 were re-run first
  (`docs/analysis/backlog-1.7.0-triage.json`): 72 still reproduced, one was
  already fixed and one superseded. 68 are fixed in this release (P&ID tag
  grammar, routing, catalogue and reader, packs, spec inputs, export cells),
  each with a test; the four that need the XDATA rework move to 1.7.1 (B08).
- Two 1.6.0 changelog claims (triage W4-T21-05 and W4-T21-08): a chord walk
  over the tested semicircular jump is 10.25 % short, not 12.5 %; and
  `drawing_preflight` reports `LAYER_SET_INTENT_MISMATCH`, while
  `drawing_plan` only adds a warning.

### Changed

- **A live session no longer follows AutoCAD's active document.** Switching
  tabs between two calls used to move the next write onto whatever drawing
  was active; it is now refused `active_document_changed` until
  `document_activate` (or `drawing_open` / `drawing_new`) chooses a document.
- `drawing_deliver`'s reopen parity compares every space and block definition
  (it compared counts, types, layers and extents of the current space).
- The 1.7 error vocabulary: `active_document_changed`, `stale_revision`,
  `operation_conflict`, `requires_reconcile`, `unknown_commit`,
  `scan_incomplete`, `check_failed` (critique `detail.code`),
  `ambiguous_binding`, `missing_requirement`, `unsupported_measurement`,
  `busy`, beside `unsupported` / `quarantined` / `invalid_args` / `refused` /
  `failed`. Every refusal and step error carries its `operation_id`.
- `TOOL_PROFILE=lean` is 66 tools (56 with `TOOL_PACKS=core`): it gains
  `drawing_verify_requirements`.
- The `prompt_floor_plan` prompt is rewritten around `arch_plan_from_spec` /
  `arch_rooms_detect` / `arch_schedule`; `prompt_electrical_schematic` uses
  current tools only and says electrical schematics are 1.9.0.
- `drawing_refine` previews also when the score already meets `min_score`.
- The plugin carries an icon (`plugin/.claude-plugin/icon.png`, a spur gear
  drawn by `gear_draw_spur_front_view`), and the README's `uvx` pin is one of
  the release numbers the version-drift test holds to pyproject.
- The correctness suite removes the temporary folders its checks write; every
  A/B run used to leave one per check in `%TEMP%`.

### Not in 1.7.0

Recorded in the spec, not dropped:

- **Headless throughput (B10) is 1.7.1.** 1.7.0 is not slower than 1.6.0
  (ratios 0.64 to 0.98), but entity creation is still slower than v1.4.0
  and roadmap criterion 7 stays open.
- **`ezdxf.recover` on open (B09) is 1.7.1.** A damaged DXF is refused by name
  (`damaged_dxf_refusal` in matrix v8), not recovered.
- **The XDATA chunk / case / budget rework (B08) is 1.7.1**, with the four
  triage rows that need it.
- **A durable operation journal and cross-restart idempotency (B13) are
  1.8.0.** The journal is in memory: a restart forgets every operation id.
- **Parametric edit and rebuild (B24–B28), obstacle-aware P&ID routing,
  cross-file P&ID projects and the native .NET worker are 1.8.0.**
- **The DIN 471/472, DIN 509, DIN 332 and ISO 3601-2 rows (B40) are 1.8.0.**
  1.7.0 ships their coverage manifest only; every size is refused by name.
- **Electrical schematics are 1.9.0.** The AutoLISP allowlist (F08) is not in
  this release; the command filter is still a denylist.
- **Plant 3D is not planned.**
- **The live revision sees object edits only.** No COM event sink (one left
  the seat refusing `Documents.Add()`, probed 2026-10-24): a transient
  AutoLISP database reactor read through `VL.Application.16` counts the
  operator's edits to database objects. A view change, a `SetVariable` and an
  edit made during a server call do not move `revision`, and without Visual
  LISP it counts this server's writes only (`system_status.operator_edits`).
- **The skill's A/B evaluation ran one case once** (`saved-not-in-memory`:
  the full plugin 1.0, no plugin 0.0, 0.36 USD). The budgeted run of all
  twenty cases × three arms is the maintainer's, at release time.

### Live COM smoke

`scripts/live/harness.py` on AutoCAD 2026 (`25.1s`, build `W.60.0.0`), Windows 11, CPython 3.13.15, pywin32 312, clean tree each time. 129 of 129 checks passed over eight smokes, all on the close-out commit `13499fe` (10 `RPC_E_CALL_REJECTED` retries). Every smoke worked only in documents it added and closed them with `Close(False)`, and the operator's drawing was neither activated, written, saved nor closed. The JSON results are `benchmarks/results/live/*.json`.

| Smoke | Checks | Time | What it covers |
|---|---:|---:|---|
| `smoke_pid_com.py` | 8 / 8 | 12.5 s | P&ID: placement, classification, node confidence, nodes and edges equal to the headless run of the same spec, the six `pid_*` focuses, the pump's XDATA |
| `smoke_arch_com.py` | 6 / 6 | 43.3 s | architecture: the plan from its spec, both rooms read back within 0.1 % of label and hand-computed floor, the three schedules, zero critique issues |
| `smoke_mech_com.py` | 10 / 10 | 58.5 s | mechanical: part, section, dimensions, an ISO 4014 block with its XDATA, the parts list read off the drawing as a native TABLE, a linked balloon, an xref attached and detached, a real `.dwg`, zero critique issues |
| `smoke_settings_com.py` | 33 / 33 | 8.7 s | environment: styles, a template, the page setup read back from the plotted PDF's `/MediaBox`, layer states, named views, UCS, preferences, document properties |
| `smoke_understand_com.py` | 7 / 7 | 5.6 s | understanding: `Document.Export` snapshots of a DWG pair, the P&ID read as schematic, every pipe run and cable row equal to the truth, a diff naming exactly the moved entity |
| `smoke_verify_com.py` | 27 / 27 | 5.8 s | verification: the bound document and its refusal, HATCH loops and DIMENSION read-back, requirements on the live document, Export delivery (not re-pointed), the delivered DXF and a DWG re-measured, an edited copy failing at 22 |
| `smoke_document_com.py` | 22 / 22 | 5.1 s | the bound document: the active-tab refusal with nothing written and no revision moved, the screenshot refusal, the hand-back after a snapshot, re-binding under the known ids, the application identity on a second proxy and after a re-attach |
| `smoke_operator_edits_com.py` | 16 / 16 | 8.6 s | the live revision: a server write and each hand edit (add, move, property, layer property, layer add, delete) move it by one, a hand edit after a preview is refused by the revision, a removed reactor is a gap, another document's edit moves nothing, `DBMOD` stays 0 |

The first `smoke_arch_com.py` run after AutoCAD was restarted stopped in its critique: the harness's active-document guard refused because AutoCAD's active document was not one the run had opened, with nothing written to any other document. Both reruns passed all six checks; the one committed here took 43.3 s.

## [1.6.0] — 2026-09-25

The v1.6 roadmap, shipped as five tracks: 154 → 247 registered tools in 28
groups, the correctness suite at 48 / 48 against v1.5.1's 26 / 48 with zero
regressions, the 21-task benchmark matrix at 21 / 21, and a live AutoCAD 2026
run of every track's COM paths. Two roadmap criteria are not green, and
**Not in 1.6** below says which and why: track D (Plant 3D) was dropped, and
headless creation throughput is still below v1.4.0's. Roadmap:
`docs/superpowers/specs/2026-09-15-v1.6-roadmap-design.md`.

Track A of 1.6: the server becomes a P&ID expert and a P&ID reader. Nine
`pid_*` tools in a new SECTION 17, two generic contracts every engine
implements, six critique focuses that join `drawing_finalize`, and a
`TOOL_PACKS` gate so a mechanical-only client never pays for the P&ID
surface. Spec: `docs/superpowers/specs/2026-09-15-v1.6-pid-design.md`.

Track E of 1.6: the environment a drawing lives in. 38 tools in three new
sections — styles (18), page setup and templates (19), environment (20) —
the `drawing_settings` facade grown, a system-variable catalog, five bundled
templates, several open documents headlessly, portable layer states, named
views and UCS, and the live-only group (launch, preferences, document
properties, operator prompts) refused honestly headlessly. Spec:
`docs/superpowers/specs/2026-09-16-v1.6-settings-design.md`.

Tracks B and G of 1.6: the complete mechanical 2D part drawer and the sheet
standard. 25 tools in four new sections — mechanical parts (21), standard
parts (22), mechanical annotation (23) and sheet and delivery (24) — one part
model and one view engine instead of a zoo of per-part generators, a new
`mech` pack, six `mech_*` critique focuses and four new contract members
(`xref_attach`, `xref_manage`, `image_attach`, `drawing_export_dwg`). Spec:
`docs/superpowers/specs/2026-09-22-v1.6-mech-design.md`.

Track F of 1.6: architectural 2D, the plan model. 12 tools in a new SECTION
25 — a wall network with hosted doors and windows drawn by one engine (L / T /
X junctions resolved, openings cut, poché by material), rooms computed as the
faces the walls enclose and read back from any plan, schedules read off the
drawing, a furniture and sanitary catalogue, the structural grid and plan
symbols, exterior dimension chains and the whole plan in one transaction — a
new `arch` pack, three `arch_*` critique focuses, and English or Turkish
labels. No new contract member. Spec:
`docs/superpowers/specs/2026-09-23-v1.6-arch-design.md`.

Track H of 1.6: understanding drawings somebody else made. Six tools in two
new sections — understanding and QA (26: a one-call report, a scale check
between a P&ID and its layout, a revision diff, topology checks) and plant
takeoffs (27: pipe and cable quantities whose topology comes from the P&ID
and whose lengths come from the layout) — one DXF snapshot per drawing (a
live document through `Document.Export`, never renamed), a vocabulary in five
languages, three `topo_*` critique focuses, a new `plant` pack and the
`office` extra. Read-only by default; nothing invented. No new contract
member. Spec: `docs/superpowers/specs/2026-09-24-v1.6-understand-design.md`.

### Added

- **Styles — 10 tools, pack `core`.** `dimstyle_list/create/modify/set_current`,
  `textstyle_list/create/set_current`, `mleaderstyle_list/create`,
  `drawing_apply_standard`. ISO-25 and ANSI (metric) presets are authored data
  pinned by `tests/test_standards_presets.py` against ISO 129-1 / ASME Y14.2
  (DIMTXT 2.5 / 3.0, DIMASZ 2.5 / 3.0, DIMEXO 0.625 / 1.5, DIMDSEP `,` / `.`,
  DIMTAD 1 / 0 …). `dimstyle_modify` accepts the 33-name whitelist only (the
  17 preset variables plus 16 more — `DIM_VARIABLE_WHITELIST`) and
  refuses an out-of-range value by name before writing; `changed` lists what
  moved. A font neither bundled nor on the engine's font path is written and
  reported `font_resolved: false`, never refused. `mleaderstyle_create` works
  on both engines: ActiveX exposes no MLeaderStyle *collection*, but the
  `ACAD_MLEADERSTYLE` dictionary holds full `IAcadMLeaderStyle` objects, so
  the live engine creates the style through `AddObject` and the property
  writes — no `mleaderstyle` capability key exists.
- **Page setup & templates — 6 tools, pack `core`.** `page_setup_list/apply`
  (`PAPER_SIZES` ISO A0–A4 / ANSI A–E, `CTB_CATALOG`, `SCALES`), `plot_style_list`
  (catalog + installed), `batch_plot` — `mediabox_mm` parsed from each PDF's
  `/MediaBox`, so `ISO_A3` landscape is 420 × 297 because the file says so —
  `drawing_template_list`, `drawing_template_save` (`.dwt` refused headlessly,
  capability `dwt_write`). Five bundled templates built by
  `scripts/build_templates.py` through the server's own methods (`--check`
  proves the DXFs byte-reproducible); their `.dwt` twins were built once on
  AutoCAD 2026 by the smoke. `drawing_new(template="iso_a3_mech")` resolves the
  bundled file and reports `{name, source}`.
- **Environment — 22 tools, pack `settings`.** `document_list/activate/close`
  on both engines (the headless backend keeps a document registry;
  `drawing_close` is the active-document case); `layer_state_save/restore/
  list/delete` as portable XRECORD snapshots — restore applies only the
  requested `properties`, reports `missing_layers` (skipped, never created)
  and `new_layers` (untouched), and the state survives save/reopen;
  `view_named_save/restore/list`; `ucs_list/set/restore` (non-orthogonal axes
  refused with the measured angle; tool coordinates stay WCS);
  `system_variable_describe` over a 90-entry catalog (`known: false` + nearest
  names on a miss); `drawing_properties_get/set` (custom properties on both
  engines, summary fields COM-only: `dwgprops`); `system_launch`
  (`live_application`), `system_preferences_get/set` (whitelisted keys with
  ranges, read-only keys refused by name, `old`/`new` reported:
  `preferences`), `user_pick_point/select` and `system_prompt_message`
  (`interactive_prompt`; a cancelled pick is `{cancelled: true}`, a
  `COM_CALL_TIMEOUT` overrun is `timed_out`).
- **`drawing_settings` grows** by `limits`, `grid`/`grid_spacing`,
  `snap`/`snap_spacing`, `ortho`, `polar`/`polar_angle`, `psltscale`,
  `annotation_scale`, `linear_units`, `angular_units`, `dimstyle`,
  `textstyle`; `system_set_variable` refuses an out-of-range value for a
  catalogued name before writing.
- **Benchmark evidence.** `benchmarks/tasks_v4.py` adds `page_setup_truth`;
  the correctness suite grows to 32 with `settings_dimstyle_iso25_values`,
  `settings_layer_state_roundtrip`, `settings_pdf_mediabox_a3`. A/B against
  `v1.5.1`: **26 / 32 → 32 / 32, six `miss → pass`, zero regressed**.
  `scripts/smoke_settings_com.py` runs the track once against the live seat
  (record below).
- **Benchmark evidence for tracks B and G.** `benchmarks/tasks_v5.py` (18
  tasks, the default matrix; v4 / v3 / v2 stay addressable) adds
  `mech_assembly`: a flange-coupling A3 sheet built through the code the tools
  call — frame and title block, a hub with a full section and chain
  dimensions, a flange with its bolt-circle end view, ISO 4014 bolts and ISO
  4032 nuts, the parts list read off the drawing and linked balloons — gated on
  `drawing_critique(focus=None)` returning zero issues and a finalize score of
  at least 90 (it scores 100); a test drops one balloon and watches the gate
  fail by name. The correctness suite grows to 39 with `mech_part_roundtrip`,
  `mech_section_hatch_area` (1200 mm² read back out of the drawing),
  `mech_iso286_on_dimension`, `mech_thread_unrepresented_is_caught`,
  `std_part_iso4014_m12`, `sheet_frame_iso5457_a3` and `bom_balloon_link`.
  A/B against `v1.5.1`: **26 / 39 → 39 / 39, thirteen `miss → pass`, zero
  regressed**. The token suite measures a `TOOL_PACKS=core,mech` row: 193
  tools / 61,673 tokens against 224 / 72,163 by default.
- **Live COM smoke.** `scripts/smoke_mech_com.py` executed once on AutoCAD
  2026 (`25.1s (LMS Tech)`): the A3 frame and ISO 7200 title block, a two-step
  shaft with a full section and 12 chain dimensions, an ISO 4014 M12x60 block
  carrying 1 ACADMCP_MECH chunk, an ISO 7573 parts list read off the drawing
  with its linked balloon, an xref attached and detached, and a real
  31687-byte `.dwg` written by ActiveX `SaveAs` — `drawing_critique(focus=None)`
  returned 0 issues (record below).
- **Architecture — 12 tools, pack `arch`.** `arch_wall`, `arch_opening`,
  `arch_stair`, `arch_room`, `arch_rooms_detect`, `arch_schedule`,
  `arch_grid`, `arch_symbol`, `arch_catalogue_list`, `arch_catalogue_insert`,
  `arch_dimension_chains`, `arch_plan_from_spec`. The plan travels as
  `ACADMCP_ARCH` XDATA through the mechanical codec, generalised to take an
  application id; `draw_prims` takes a role map. The `arch` layer set is
  ISO 13567-style with ISO 128 lineweights; wall poché follows
  `engineering/arch/materials.py`, whose rows each name their source. Areas
  are measured, never typed. Lean gains four and reaches 64.
- **Benchmark evidence for track F.** `benchmarks/tasks_v6.py` (19 tasks, the
  default matrix; v5 to v2 stay addressable) adds `arch_roundtrip`: the example
  plan drawn by `arch_plan_from_spec` and read back by `arch_rooms_detect`
  within 0.1 % of each label and of the hand-computed net floor, the schedules
  listing the drawn tags, gated on a zero-issue `drawing_critique(focus=None)`
  and a finalize score of at least 90; a test removes one room label and
  watches the gate fail by name. The correctness suite grows to 43 with
  `arch_junction_l_t_x`, `arch_room_area_net`, `arch_opening_cuts_wall` and
  `arch_rooms_detect_foreign`. A/B against `v1.5.1`: **26 / 43 → 43 / 43,
  seventeen `miss → pass`, zero regressed**. The token suite measures a
  `TOOL_PACKS=core,arch` row: 191 tools / 60,568 tokens against 236 / 78,772
  by default.
- **Live COM smoke.** `scripts/smoke_arch_com.py` executed once on AutoCAD
  2026 (`25.1s (LMS Tech)`): the example plan drawn in one transaction, both
  rooms read back within 0.1 % of their labels and of the hand-computed net
  floor, and `drawing_critique(focus=None)` returned 0 issues (record below).
- **Understanding foreign drawings — 4 tools, pack `core`; plant takeoffs — 2
  tools, pack `plant`.** `drawing_understand`, `drawing_scale_check`,
  `drawing_diff`, `drawing_topology_check`, `pipe_takeoff`, `cable_takeoff`.
  Every one reads a snapshot of frozen WCS records
  (`engineering/understand/snapshot.py`); labels are read by one parser
  (`%%c`, `\U+2205`, the U+2300 sign, `ø` and MTEXT format runs decoded,
  Cyrillic look-alike tags folded to Latin) and layer names and texts by one
  EN / TR / RU / NL / DE vocabulary. The takeoff workbook is written as CSV
  always and as XLSX with the new `office` extra (`openpyxl`, also in `full`).
  Lean gains `drawing_understand` and reaches 65.
- **Benchmark evidence for track H.** `benchmarks/tasks_v7.py` (21 tasks, the
  default matrix; v6 to v2 stay addressable) adds `takeoff_roundtrip` and
  `understand_foreign`, both on the synthetic plant pair of
  `tests/fixtures/plant_pair.py`, whose truth is computed from the positions
  it placed; each gate is shown to fail on a broken input. The correctness
  suite grows to 48 with `scale_check_detects_schematic`,
  `pipe_takeoff_rmst_exact`, `cable_takeoff_roundup`,
  `diff_detects_known_edits` and `topology_known_defects`. A/B against
  `v1.5.1`: **26 / 48 → 48 / 48, twenty-two `miss → pass`, zero regressed**.
- **`scripts/field_test_takeoff.py`** runs both takeoffs on a user's own P&ID
  and layout and writes the report next to them; it refuses an output folder
  inside the repository and prints no quantity unless `--show` is given.
- **Live COM smoke.** `scripts/smoke_understand_com.py` executed once on
  AutoCAD 2026 (`25.1s (LMS Tech)`): the live snapshots matched
  their files type for type in model space, the DWG route read the same
  P&ID, both takeoffs matched the truth and the diff reported exactly the
  one entity moved (record below).

- **P&ID — 9 tools, pack `pid`.**
  - `pid_symbol_list` / `pid_symbol_insert`: a 43-symbol ISO 10628-2 /
    ISA-5.1 catalogue authored in code (11 valve bodies × 6 actuators, 6
    rotating machines, 4 heat exchangers, 6 parametric vessels with nozzle
    ports, 7 inline items, the instrument bubble in 4 types × 5 locations,
    5 line markers, 3 connectors), placed as real blocks with a `TAG`
    ATTRIB, named ports, and an `ACADMCP_PID` XDATA payload. Ports are
    transformed through the INSERT's real rotation, scale and mirror; a
    stretched symbol (`|x_scale| ≠ |y_scale|`) has no circle for a radial
    port and is refused rather than answered with a wrong point.
  - `pid_line_draw`: port-to-port orthogonal routing (`auto` picks from seven
    candidates by fewest bends, then length; `direct`; or waypoints), eight
    ISA-5.1 line classes mapped to layer / linetype / marker blocks, and line
    numbers filled from a format (default
    `{size}-{service}-{seq}-{spec}-{insulation}`; an empty field drops out
    with its separator). A route that cannot honour the `stub` on both
    ports is refused by name (`route((0,0), 0°, (3,8), 180°, stub=5)`
    raises) rather than drawn short; a non-axis port direction, a NaN
    coordinate and an unfillable format token are refused before anything
    is written. Lines it crosses are **counted and
    reported** (`crossings`), never silently drawn over — and never refused.
  - `pid_tag_parse`: the ISA-5.1 identification-letter grammar (Table 4.1,
    pinned by `tests/data/isa51_tags.json`), with the equipment-prefix map.
  - `pid_graph`: the reader. Every symbol INSERT becomes a node with a
    `source` and a `confidence` — catalogue name 1.0, `ACADMCP_PID` payload
    0.95, `TAG`-style attribute or keyword block name 0.6 with inferred
    ports, an INSERT a plain line merely touches 0.3. Confidence is the
    minimum of its inputs, never raised by agreement between heuristics.
    Each line end resolves to a port, a junction on another line, or
    `dangling` with the nearest connectable port as a hint, always within
    its own space (`scope="all"` never joins two layouts). Edge `length` is
    the bulge-aware length the engine reports, not a chord walk (10.25 %
    short on a semicircular jump).
  - `pid_instrument_index` / `pid_line_list` / `pid_equipment_list`: derived
    from the graph only, optionally written as CSV. An instrument tapping a
    pipe reports the pipe's line number, never its own signal wire.
  - `pid_from_spec`: a whole sheet from one JSON spec in one transaction —
    rolled back entirely on the first refusal — returning the handles, the
    graph and the critique; `dry_run=True` routes in memory and touches
    nothing.
- **`block_define(name, entities, attdefs)`** on both engines: a block from
  typed primitive specs (line, circle, arc, polyline, text, solid) and
  ATTDEFs, no source entities needed; `overwrite=True` replaces the contents
  so existing INSERTs keep the name. Validation runs before any write: a
  DXF-illegal layer name, a duplicate or trailing-newline ATTDEF tag, a
  multi-line or trailing-caret string are `TypeError`/`ValueError` naming
  `entities[i]` / `attdefs[i]`, and nothing is created.
- **`entity_get_xdata` / `entity_set_xdata`** on both engines, typed by DXF
  group code. The two limits AutoCAD enforces silently are enforced loudly
  and before the write: a string over **255 characters** and an entity
  budget of **16 KB across every application** (three 15 KB apps on one
  entity used to pass headlessly and would have failed `SetXData` live).
  LF/CR in a string is refused — it split a DXF tag, the saved file failed
  to reopen and, because every undo snapshot is a DXF save,
  `transaction_rollback` broke after one such write. `nan`/`inf` are refused
  rather than written as literal text that reads back as `null`.
- **Six critique focuses** — `pid_dangling_line`, `pid_duplicate_tag`,
  `pid_incompatible_connection`, `pid_untagged_instrument`,
  `pid_illegal_tag`, `pid_unconnected_equipment` — run under `focus=None`
  and inside `drawing_finalize`, over one cached graph. They score
  evidence only: a drawing without a recognised P&ID node returns `[]`, a
  0.3 touch guess can neither create nor suppress a finding, and a line
  ending on a sheet frame (an INSERT the drawing does not vouch for) is
  dangling. `drawing_refine` hints name only ports `pid_line_draw` accepts.
- **Two resources:** `autocad://pid/symbols` (the catalogue with ports and
  parameter schemas) and `autocad://standards/isa51` (the letter tables and
  the grammar's disambiguation rules).
- **Benchmark evidence.** `benchmarks/tasks_v4.py` adds `pid_roundtrip`
  (the example sheet drawn through `pid_from_spec`, read back by a graph
  builder that never sees the spec); the correctness suite grows to 29 with
  three P&ID checks. A/B against `v1.5.1`: **26 / 29 → 29 / 29, three
  `miss → pass`, zero regressed** (`ab-v1.5.1-vs-v1.6.0-dev.json`).
- `scripts/smoke_pid_com.py` runs the track once against the live AutoCAD
  on the machine, in a new document; `scripts/render_pid_catalog.py` and
  `scripts/render_readme_pid.py` rebuild the two README images.

### Fixed

- **Every tool call raised `RecursionError` after a few hundred in-process
  sessions.** fastmcp 3's `enable()` / `disable()` append a Visibility
  transform per call, and a tool lookup walks the whole transform stack as
  nested calls. `_apply_tool_profile` runs in the lifespan, once per client
  connection, so each session stacked two more layers. The first Linux CI run
  of the 1.6 tree found it: on CPython 3.11, which counts C frames against the
  recursion limit, `system_status` itself failed from about 90 % of the suite
  on. The profile's transforms now replace the previous ones instead of
  stacking on them, the idiom `_apply_discovery_mode` already used, and a test
  switches the profile 600 times and then calls a tool.
- The same CI run failed 21 fake-ActiveX tests off Windows (they build a real
  pywin32 VARIANT and now skip without pywin32, like the suite's other
  VARIANT-dependent COM tests; the Windows lane runs them), and a test that
  spelled a path through a directory it never created, which only Windows
  normalises away.
- **Found by the 1.6.0 README renders** (each with a test that fails on the
  old code):
  - The exterior dimension chains of `arch_dimension_chains` and
    `arch_plan_from_spec` were drawn at the dimension style's DIMSCALE 1, so a
    1:50 chain's 2.5 text was 2.5 drawing units high — 0.05 mm on the sheet —
    and the plan render showed empty boxes where the numbers belong. Every
    other plan annotation already followed the plot scale, and now the chains
    do too: DIMSCALE is the plan's scale while they are drawn (reported as
    `dimscale`, replacing the old advice to set it by hand), and the drawing's
    own DIMSCALE is put back afterwards. Headlessly each chain dimension
    carries the scale as its override. The live engine reaches the same
    DIMSCALE through `SetVariable`; that combination has not been re-run on a
    live seat since this change.
  - A headless TABLE drew its title as the first column's cell, so a title
    such as `DOOR SCHEDULE` wrapped inside that column and ran over the header
    row. The title row is now merged across the table, as AutoCAD's TABLE
    draws it.
  - A headless TABLE's cell margin was a flat 1.0 drawing unit — 0.02 mm on a
    1:50 schedule, where the text sat on the cell line and `LEFT` read as
    `FT`. The margin now scales with the lettering (0.4 × text height, at most
    0.15 × row height), which leaves every table at the default 2.5 text as
    it was.
  - `drawing_new` left the Standard text style on ezdxf's default `txt`,
    which has no superscript two: a room label's `m²` read `m?` when AutoCAD
    opened the DXF and rendered as a box headlessly. Standard is now
    `arial.ttf`, as AutoCAD 2026's `acadiso.dwt` ships it, so both engines
    begin from the same Standard; the five bundled templates are rebuilt on it
    and `scripts/build_templates.py --check` reproduces them.
- **Hatch islands on a live seat.** `hatch_add_boundary` was refused on COM
  (`hatch_edge_paths`), so a transverse section of any hollow part - a
  bushing, a flange bore - could not be drawn in AutoCAD. Each typed edge now
  becomes a temporary LINE, true ARC or ELLIPSE appended with
  `AppendInnerLoop` and deleted after `Evaluate`; a chain of lines is one
  polyline. Measured on AutoCAD 2026: a two-arc island leaves exactly
  10000 - 100*pi of a 100 x 100 hatch, the OD40/ID20 sleeve's section B-B hatches
  942.2864 mm2 (the view engine's declared 180-gon), and an open loop is
  refused with nothing left behind.
- **`block_find_references` found nothing on a live seat.** ActiveX names a
  block reference `AcDbBlockReference`, so `entity_list(type_filter="INSERT")`
  matched nothing. The COM type filter now lets the DXF name find it (`INSERT`,
  `LWPOLYLINE`, and every `*DIMENSION` class for `DIMENSION`); the reported type
  is unchanged.

- **Found by the tracks B + G evidence** (each with a test that fails on the
  old code):
  - `dimension_diameter` put its text beyond the *second* chord point
    headlessly and beyond the *first* on AutoCAD (`AddDimDiametric`'s
    ChordPoint, measured: a ⌀50 from (x, 170) to (x, 220) annotated at
    (x, 160)). The two engines annotated opposite sides of the same part, and
    headlessly every front-view diameter of `mech_part_draw` stood on its own
    chain dimension — `dim_overlap` fired on any single-step disk. The
    headless engine now follows ActiveX.
  - On a live seat `mech_part_draw` died after drawing every view: its anchor
    POINT goes on `MECH`, and ActiveX refuses `entity.Layer` on a missing
    layer (`Key not found`) where ezdxf creates it. Every mechanical draw
    path now ensures its target layers before the first entity.
  - `bom_extract` returned nothing live: ActiveX names a block reference
    `BLOCKREFERENCE`, and the parts list filtered on `INSERT` only. Both
    names are read.
  - The live `_entity_info` reported no `text_position` for a dimension (the
    entity arrives narrowed to `IAcadEntity`, which has no `TextPosition`),
    so `dim_overlap` compared bounding-box centres — a diameter and its bore
    read "0.00 mm apart". It is read through the concrete class now.
  - `pid_graph` raised `KeyError: 'start'` inside `drawing_critique(focus=None)`
    on a LINE whose point read AutoCAD had refused while busy; such a line is
    left out instead.
  - The validator's `fake_dimension_text` read a standard-part designation
    (`ISO 4014 - M12x60`) as a hand-typed dimension, so every parts list
    drawn as text cost its sheet a warning. The designation is exempt; a
    measurement beside it still flags.
- **Track A hardening** (`tests/test_track_a_hardening.py`; the remaining
  review findings are filed with a disposition each in
  `docs/analysis/track-a-review-backlog.md`):
  - `entity_create_block_ref` of an undefined block name is refused before
    writing (it returned a handle to a dangling INSERT).
  - `block_explode` keeps ATTRIB text: each ATTRIB becomes a TEXT entity on
    both engines instead of being dropped.
  - `pid_from_spec` refuses a NaN coordinate before the transaction opens and
    rolls back on `BaseException`, not only `Exception`.
  - A signal line no longer receives a fabricated line number and label; the
    reader reports `line_number: null` for it.
  - A bubble's exit point honours the first waypoint instead of the port
    direction.
  - A foreign or hand-edited `ACADMCP_PID` payload is read tolerantly, and an
    existing polyline with a bulge is tested as its arc, not its chord, so a
    line through a crossing jump counts as a crossing.
  - `block_insert` attribute values are validated as strings; an int or None
    is a `TypeError` naming the tag, not `str()`-coerced.
  - `block_define` refuses a NaN base point and a layer that does not exist
    in the drawing (it wrote both and `drawing_audit` repaired them later).
- **Headless `block_insert(attributes=…)` lost its ATTRIBs.** The ezdxf path
  used `add_auto_blockref`, which wraps the INSERT in an anonymous `*U`
  block: the handle it returned reported `block_name` `*U1`, and
  `block_get_attributes` on it returned `{}`. Reproduce on v1.5.1 with
  `block_create_from_entities` of a block with one ATTDEF, then
  `block_insert(name, attributes={"TAG": "P-101"})` and
  `block_get_attributes(handle)`. It now inserts the named block with
  `add_auto_attribs`, so the INSERT carries the name and the ATTRIBs, on
  both engines alike. `block_insert` of an undefined name is refused before
  writing (it used to return a handle whose dangling INSERT `drawing_audit`
  later deleted).
- Live LWPOLYLINEs report as `POLYLINE` through ActiveX (`AcDbPolyline`),
  which the P&ID line reader's type filter excluded: on AutoCAD every
  auto-built line number restarted at seq 1 and `crossings` was always 0.
  Both spellings are accepted; the COM `_entity_info` polyline branch now
  returns `points` (and per-vertex `bulges`) for a live LWPOLYLINE.
- Polyline `bulges` now leave both engines in WCS: a mirrored polyline's
  arcs were handed out in the entity frame next to WCS points, so a reader
  tested them on the wrong side of the chord.
- INSERT properties gain `geometry_bbox` (drawn geometry only, ATTRIBs
  excluded) and `mirrored` on both engines. The COM box is exact at right
  angles and measured off them; a member ActiveX cannot measure is carried
  by its corners and the box says `approximate: true`.
- **`entity_list` lost every type-specific property on a live seat with a
  makepy cache.** `ModelSpace.Item` (and `SelectionSet.Item`) is declared
  `IAcadEntity*`, so once `win32com.client.gencache` had run against AutoCAD
  every listed entity arrived as the gen_py `IAcadEntity` wrapper: measured on
  AutoCAD 2026, `item.StartPoint` on an AcDbLine raised `AttributeError`, and a
  LINE row kept only its bounding box (no `start` / `end`, no polyline
  `points`, no text). The room reader therefore saw 0 wall segments and
  `arch_plan_from_spec` refused its first room label live, while every
  headless test passed. `_entity_info` now re-dispatches exactly that wrapper
  through its runtime class (`_com_concrete_entity`); a concrete wrapper from
  `HandleToObject` is left alone. The same narrowing strips `start` from
  every listed LINE, so it would equally explain the `pid_graph`
  `KeyError: 'start'` recorded above as a busy-refused point read (not
  re-measured).

- **Track H, found after the groups landed.** Six defects in the readers,
  each with a test that failed before its fix:
  - A truncated DXF (an interrupted save, a partial copy) made ezdxf raise
    `StopIteration`, which `asyncio.to_thread` cannot hand back to an
    awaiting caller, so every track H tool reading one would have hung. The
    snapshot now refuses it as "not a readable DXF file".
  - `take_snapshot` of a DWG the operator already had open opened it again
    read-only and closed "it" unsaved - possibly their copy, with their
    unsaved work. An open document is now looked up first, exported as it
    stands and left open; only a document the snapshot opened is closed.
  - `drawing_diff`: a LINE drawn back to front and moved was reported as
    reshaped; an INSERT carrying one attribute tag twice could lose an edit
    of the first value (a repeated tag is now compared as the list of its
    values); the cluster list is capped like the other lists
    (`truncated["clusters"]`, the markup still clouds every cluster); and a
    whole-drawing move keeps only the 32 nearest candidates per entity.
  - A line number was read as an equipment tag: `100-P-001` yielded the pump
    `P-001`. A token that is the tail of a word beginning with a digit is no
    longer a tag.
  - The pipe network merged ends and dropped stubs within `tol = 1.0`
    drawing units - a metre on a metre P&ID. The tolerance now defaults to
    1 mm in the P&ID's inferred unit, and a dropped stub is counted and said.
  - A wiring callout was counted as a place its panel is written:
    `Wiring to CP-2` made `CP-2` ambiguous in `drawing_scale_check` and
    `drawing_understand`, and `Wiring to CP1` a phantom tag `CP1`. On the
    synthetic pair the check matched 10 tags and 45 pairs (13.3 % within
    ±10 %) where the generator placed 11 and 55 (21.8 %). A callout is no
    longer a tag occurrence - the rule the line-network reader already kept.
- **Track H, found by the field test** (the takeoffs run on a real P&ID pair
  that stays outside the repository). Each fix has a test that failed first:
  - The pipe network took every piping or electrical layer, so valve, pump
    and annotation layers (sizes, materials, numbers) became pipe runs.
    `vocab.is_network_layer` is now the one rule for the network, the
    topology check and the `topo_*` focuses: piping or electrical at 0.9, and
    naming neither equipment nor annotation.
  - Measured on the P&ID, a run still needed equipment tags at both ends, so
    a field P&ID whose pipes rarely end on a tagged outline gave an empty
    table. On the P&ID each piece now goes to the room it lies in, with its
    diameter kept piece by piece. A layout takeoff that cannot route every run
    now says how many, why, and what the P&ID alternative is.
  - TEXT caret notation (`^J`, a line break) was left in place, so a room
    label's number read as `6^J...`. `plain()` now decodes it.
  - A label that only mentions a room was read as a room: a control cabinet
    naming the room it serves, or a note pointing into a room. An unnumbered
    label naming equipment is no room. Without room faces, when the drawing
    numbers its rooms, only numbered labels take pieces.
  - The bare Russian "to" before a panel (`к CP1`, `к щиту CP-2`) is a
    wiring callout.
- **Track H, what the field test asked for** (each with a synthetic test):
  - **Rooms drawn as frames.** A room label no wall face holds takes the
    smallest closed, straight-sided polyline around it that holds no other
    room number and is at least ten label heights a side (`label+frame`,
    confidence 0.8). This is how a P&ID draws its rooms, so P&ID lengths are
    cut at the room frames.
  - **The nearest panel, flagged.** `cable_takeoff(panel_rule="nearest")`,
    the default: a load that no callout places, whose tag is on the layout,
    takes the nearest panel there. Only names with a panel prefix
    (`is_panel_name`: CP, JB, DB, MCC ...) or names a callout gives are
    candidates. The row is flagged `panel_inferred`, and an inferred panel
    never makes a package. `panel_rule="stated"` leaves such a load open. The
    benchmark and the correctness check read stated panels only.
  - **Possible packages and detail copies.** `pipe_takeoff` reports
    `detail_copies`: compact groups of tags drawn twice (`repeated`), and
    compact groups of one equipment's own parts, such as M150B, M150C and
    M150D of M150 (`family`). A pasteurizer drawn in detail, whose internal
    pipes its vendor supplies, shows up this way. Nothing is removed until
    `exclude_regions=["D2"]` names it. The excluded runs go to the control
    rows as `excluded_region` and are totalled in `excluded_m`.

### Changed

- **Release evidence re-measured for 1.6.0.** The A/B gate against v1.5.1
  (`ab-v1.5.1-vs-v1.6.0.json`: 26 / 48 → 48 / 48, 22 fixed, 0 regressed); the
  reference adapter on matrix v7 (`autocad-mcp-pro.json`: 21 / 21, replacing
  the v3 run of 1.5); the 100-point rubric re-scored per category with the
  evidence for every point withheld (93; the competitor rows are their July
  snapshots, not re-reviewed); the perf lane as medians of five alternating
  runs, with the v1.4.0 comparison published beside it
  (`perf-v1.4.0-vs-v1.6.0.json`). The four charts are regenerated from those
  files, and the README is rewritten around them, with the mechanical and
  architectural sheets rendered by `scripts/render_readme_mech.py` and
  `scripts/render_readme_arch.py`.
- `ruff` skips `docs/superpowers`: ruff 0.16 formats Markdown code blocks,
  and reflowing a plan's quoted code would change what the document says was
  written.
- **`MAX_DXF_BYTES` defaults to 512 MB** (was 50 MB): a real plant layout read
  for track H was a 188 MB DXF. The snapshot's refusal names the file's size,
  the limit and the variable; `0` still disables the check.
- Three tool groups appear in `system_about` and `docs/tool-inventory.json`:
  `styles` (10), `page_setup` (6), `environment` (22) — 204 tools in 23 groups;
  `LEAN_TOOL_NAMES` 55; capability keys `dwgprops`, `dwt_write`,
  `live_application`, `preferences`, `interactive_prompt`, `registry_sysvar`
  and the `documents`, `named_views`, `ucs` and `layer_states`
  (`mode: "xrecord"`) features in both maps.
- **`TOOL_PACKS=all|core,pid,settings`** (default `all`) advertises only the
  vertical packs a client uses; `core` is always on and unknown names are
  ignored with a warning. It composes with `TOOL_PROFILE=lean` (which now
  carries `pid_symbol_insert`, `pid_line_draw` and `pid_graph`, and the five
  settings essentials: 55 tools, 52 with `TOOL_PACKS=core`) and `ENABLE_3D`;
  `system_about` reports `tool_packs`. Measured idle cost: 199 tools /
  59,315 tokens by default, 190 / 55,593 with `TOOL_PACKS=core,settings`,
  168 / 48,825 with `TOOL_PACKS=core`, 55 / 16,360 lean, 2 / 356 in
  discovery mode.
- `PID_LAYERS` (`drawing_apply_iso_layers("pid")`) gains `PROCESS-LINE-TEXT`;
  `drawing_preflight` reports `LAYER_SET_INTENT_MISMATCH` when a P&ID
  intent is planned onto a non-`pid` layer set, and `drawing_plan` adds a
  warning saying so (a message, without the code).
- The `prompt_pid_diagram` prompt template is rewritten onto the tools (`pid_from_spec`,
  `pid_symbol_insert`, `pid_line_draw`, `pid_graph`) instead of describing
  primitives.
- `CritiqueFocus` grows by the six `pid_*` values; benchmark matrix v4
  (`--matrix v3` / `v2` reproduce the earlier sets); correctness suite 29.
- README release snapshot: 204 tools · 8 resources · 5 prompts · 3284
  collected tests (this machine; it was 166 · 2423 after track A). CI has
  not run on this tree yet, and the pre-existing 1369-vs-1363 drift between
  the README and Linux collection predates both tracks and is superseded by
  the new figure.

### Not in 1.6

- **Track D — Plant 3D, read-only — is dropped**, and roadmap criterion 5 is
  withdrawn, not met. The maintainer does not use Plant 3D, and a reader
  tested against nobody's real projects would be a guess. `TOOL_PACKS` keeps
  no `plant3d` pack; the `plant` pack is track H's takeoffs.
- **Roadmap criterion 7 is not met: headless creation is slower than
  v1.4.0.** Medians of five alternating runs, one CPython 3.11.15 and one
  ezdxf over each tree's own code: 2,000 line creates 244.5 → 306.1 ms
  (1.25×), the 10,000-line build / export / reopen 1,847.9 → 2,553.5 ms
  (1.38×); with `EZDXF_CALL_TIMEOUT=0` still 1.17× and 1.20×. The premium
  quality pass went the other way, 150.6 → 65.3 ms. A cProfile of the
  event-loop thread over the 2,000 creates counts 491,071 function calls on
  1.6.0 against 319,038 on v1.4.0. The timeout is part of the cost, not all
  of it, and earlier notes that switching it off restored v1.4.0's numbers
  were wrong. Carried to 1.7.
- **Four mechanical feature tables ship without rows**: DIN 471/472, DIN 509,
  DIN 332 and ISO 3601-2. Their drawing code and provenance tests are in;
  every size is refused by name until rows verified against the standard's
  own table are transcribed.

### Live COM smoke

`scripts/smoke_pid_com.py`, AutoCAD 2026 (`25.1s (LMS Tech)`), Windows 11,
2026-09-16, in a new document (`Drawing2.dwg`) — `block_define` →
`pid_symbol_insert` → `entity_set_xdata` → `pid_line_draw` → `pid_graph` →
six focuses → `entity_get_xdata`, all through the COM engine, exit 0:

```json
{
  "autocad": "25.1s (LMS Tech)",
  "active_document": "Drawing2.dwg",
  "handles": {"P-101": "98", "V-201": "A8", "FCV-101": "B8", "FIC-101": "C5", "OP-1": "CF"},
  "graph_stats": {
    "nodes_by_kind": {"connector": 1, "equipment": 2, "instrument": 1, "valve": 1},
    "edges_by_class": {"electric": 1, "process_major": 3},
    "dangling": 0,
    "unclassified": 0,
    "confidence_min": 1.0,
    "entities_scanned": 16
  },
  "crossings_total": 0,
  "critique_issues": 0,
  "critique": [],
  "xdata_chunks": 2
}
```

The headless engine reports the identical `graph_stats`, `crossings_total`,
`critique_issues` and `xdata_chunks` on the same spec. The first attempt
exited 2 on this machine because of a stale `win32com` `gen_py` cache for the
AutoCAD type library (a package directory holding only a `__pycache__`) — an
environment fault, not a backend one; deleting `%TEMP%\gen_py\3.11` cleared
it and no backend change was needed.

### Live COM smoke — settings (track E)

`scripts/smoke_settings_com.py --build-dwt`, AutoCAD 2026 (`25.1s (LMS Tech)`),
Windows 11, 2026-09-22, in a new document (`Drawing27.dwg`) — styles →
page setup → `batch_plot` (the PDF's own `/MediaBox`) → the five `.dwt` twins
built and `iso_a3_mech.dwt` reopened → layer state round trip → named view →
UCS → preferences (one reversible set) → SummaryInfo → prompt line, all
through the COM engine, exit 0:

```json
{
  "autocad": "25.1s (LMS Tech)",
  "system_launch": {
    "launched": false,
    "attached": true,
    "version": "25.1s (LMS Tech)",
    "document": "Drawing9.dwg",
    "visible": true,
    "progid": "AutoCAD.Application",
    "backend": "com"
  },
  "dwt_built": [
    {
      "name": "iso_a3_mech",
      "path": "templates\\iso_a3_mech.dwt",
      "bytes": 18240,
      "magic": "AC1032",
      "is_dwg_container": true,
      "format": "dwt"
    },
    {
      "name": "iso_a1_arch",
      "path": "templates\\iso_a1_arch.dwt",
      "bytes": 17281,
      "magic": "AC1032",
      "is_dwg_container": true,
      "format": "dwt"
    },
    {
      "name": "iso_a3_pid",
      "path": "templates\\iso_a3_pid.dwt",
      "bytes": 18528,
      "magic": "AC1032",
      "is_dwg_container": true,
      "format": "dwt"
    },
    {
      "name": "ansi_b_mech",
      "path": "templates\\ansi_b_mech.dwt",
      "bytes": 17250,
      "magic": "AC1032",
      "is_dwg_container": true,
      "format": "dwt"
    },
    {
      "name": "ansi_d_arch",
      "path": "templates\\ansi_d_arch.dwt",
      "bytes": 17278,
      "magic": "AC1032",
      "is_dwg_container": true,
      "format": "dwt"
    }
  ],
  "document": "Drawing27.dwg",
  "styles": {
    "dimstyle": {
      "ok": true,
      "name": "ISO-25-SMOKE",
      "values": {
        "DIMTXT": 2.5,
        "DIMASZ": 2.5,
        "DIMEXE": 1.25,
        "DIMEXO": 0.625,
        "DIMGAP": 0.625,
        "DIMTAD": 1,
        "DIMTIH": 0,
        "DIMTOH": 0,
        "DIMDEC": 2,
        "DIMDSEP": ",",
        "DIMLUNIT": 2,
        "DIMZIN": 8,
        "DIMBLK": "",
        "DIMTXSTY": "ISOCP",
        "DIMLWD": -2,
        "DIMLWE": -2,
        "DIMSCALE": 1.0
      },
      "written": [
        "DIMASZ",
        "DIMBLK",
        "DIMDEC",
        "DIMDSEP",
        "DIMEXE",
        "DIMEXO",
        "DIMGAP",
        "DIMLUNIT",
        "DIMLWD",
        "DIMLWE",
        "DIMSCALE",
        "DIMTAD",
        "DIMTIH",
        "DIMTOH",
        "DIMTXSTY",
        "DIMTXT",
        "DIMZIN"
      ],
      "current": true,
      "textstyle_created": true
    },
    "textstyle": {
      "ok": true,
      "name": "ISOCP-SMOKE",
      "font": "isocp.shx",
      "font_resolved": true,
      "current": false
    },
    "mleaderstyle": {
      "ok": true,
      "name": "SMOKE",
      "values": {
        "arrow_size": 2.5,
        "landing_gap": 1.0,
        "text_style": "ISOCP",
        "text_height": 2.5
      },
      "textstyle_created": false
    },
    "mleaderstyles_on_seat": [
      "SMOKE",
      "Standard"
    ]
  },
  "page_setup": {
    "applied": {
      "paper": "ISO_A3",
      "orientation": "landscape",
      "size_mm": [
        420.0,
        297.0
      ],
      "canonical_media_name": "ISO_A3_(420.00_x_297.00_MM)",
      "plot_style": "monochrome.ctb",
      "plot_style_known": true,
      "scale": "1:1",
      "scale_factor": 1.0,
      "scale_ratio": [
        1.0,
        1.0
      ],
      "dxf_standard_scale_type": 16,
      "activex_standard_scale": 16,
      "plot_area": "layout",
      "plot_type": 5,
      "device": "DWG To PDF.pc3",
      "margins_mm": null,
      "center": null,
      "paper_units": "mm"
    },
    "changed": {
      "paper": [
        "",
        "ISO_A3"
      ],
      "canonical_media_name": [
        "",
        "ISO_A3_(420.00_x_297.00_MM)"
      ],
      "size_mm": [
        [
          0.0,
          0.0
        ],
        [
          420.0,
          297.0
        ]
      ],
      "plot_style": [
        "",
        "monochrome.ctb"
      ],
      "device": [
        "Microsoft Print to PDF",
        "DWG To PDF.pc3"
      ],
      "margins_mm": [
        [
          0.0,
          0.0,
          0.0,
          0.0
        ],
        [
          17.0,
          17.0,
          5.0,
          5.0
        ]
      ],
      "paper_units": [
        "inches",
        "mm"
      ]
    },
    "pdf": {
      "layout": "Layout1",
      "ok": true,
      "path": "C:\\Users\\VECTOR\\AppData\\Local\\Temp\\acadmcp_settings_smoke_sapms54v\\Drawing27-Layout1.pdf",
      "bytes": 2774,
      "mediabox_mm": [
        420.158,
        297.039
      ],
      "paper": "ISO_A3"
    },
    "installed_plot_styles": [
      "Autodesk-Color.stb",
      "Autodesk-MONO.stb",
      "DWF Virtual Pens.ctb",
      "Fill Patterns.ctb",
      "Grayscale.ctb",
      "Screening 100%.ctb",
      "Screening 25%.ctb",
      "Screening 50%.ctb",
      "Screening 75%.ctb",
      "acad.ctb",
      "acad.stb",
      "monochrome.ctb",
      "monochrome.stb"
    ]
  },
  "template": {
    "path": "C:\\Users\\VECTOR\\Documents\\GitHub\\Autocad-MCP\\templates\\iso_a3_mech.dwt",
    "source": "bundled",
    "document": "Drawing28.dwg",
    "dimstyle_current": [
      "ISO-25"
    ],
    "papers": [
      "ISO_A3"
    ]
  },
  "documents": [
    {
      "name": "Drawing9.dwg",
      "path": null,
      "active": false,
      "saved": false,
      "entity_count": 0
    },
    {
      "name": "Drawing27.dwg",
      "path": null,
      "active": true,
      "saved": false,
      "entity_count": 0
    }
  ],
  "layer_state": {
    "saved": {
      "ok": true,
      "name": "SMOKE",
      "layer_count": 3,
      "replaced": false,
      "chunks": 2,
      "backend": "com"
    },
    "restored": {
      "ok": true,
      "name": "SMOKE",
      "applied": {
        "layers": 3,
        "properties": [
          "on",
          "frozen",
          "locked",
          "color",
          "linetype",
          "lineweight",
          "plot",
          "current"
        ],
        "current_layer": "0"
      },
      "missing_layers": [],
      "new_layers": [],
      "backend": "com"
    },
    "deleted": {
      "ok": true,
      "deleted": "SMOKE",
      "backend": "com"
    }
  },
  "view": {
    "saved": {
      "ok": true,
      "name": "SMOKE_VIEW",
      "center": [
        100.0,
        50.0
      ],
      "height": 80.0,
      "width": 160.9964412811388,
      "replaced": false,
      "backend": "com"
    },
    "restored": {
      "ok": true,
      "name": "SMOKE_VIEW",
      "center": [
        100.0,
        50.0
      ],
      "height": 80.0,
      "width": 160.9964412811388,
      "applied": "zoom_window",
      "backend": "com",
      "viewctr": [
        100.0,
        50.0
      ],
      "viewsize": 80.0
    }
  },
  "ucs": {
    "set": {
      "ok": true,
      "name": "SMOKE_UCS",
      "origin": [
        10.0,
        20.0,
        0.0
      ],
      "x_axis": [
        1.0,
        0.0,
        0.0
      ],
      "y_axis": [
        0.0,
        1.0,
        0.0
      ],
      "replaced": false,
      "current": true,
      "backend": "com"
    },
    "world": {
      "ok": true,
      "name": "world",
      "current": true,
      "backend": "com"
    }
  },
  "preferences": {
    "cursor_size": 5,
    "read_only": [
      "Files.SupportPath",
      "Files.TemplateDwgPath",
      "Files.PrinterStyleSheetPath",
      "Files.PrinterConfigPath"
    ],
    "template_dwg_path": "C:\\Users\\VECTOR\\AppData\\Local\\Autodesk\\AutoCAD 2026\\R25.1\\enu\\Template"
  },
  "properties": {
    "set": {
      "ok": true,
      "summary_written": [
        "author",
        "title"
      ],
      "custom_written": [
        "ACADMCP_SMOKE"
      ],
      "custom_deleted": [],
      "backend": "com"
    },
    "get": {
      "summary": {
        "title": "ACADMCP settings smoke",
        "subject": "",
        "author": "smoke_settings_com.py",
        "keywords": "",
        "comments": ""
      },
      "summary_available": true,
      "custom": {
        "ACADMCP_SMOKE": "1"
      },
      "backend": "com"
    }
  },
  "prompt_message": {
    "ok": true,
    "text": "ACADMCP settings smoke: done",
    "backend": "com"
  },
  "failures": []
}
```

The headless engine reports the identical `mediabox_mm` (to 1e-9: matplotlib
writes an exact 420 × 297; AutoCAD's PDF driver writes 420.158 × 297.039,
inside spec §9's ±0.5 mm), `layer_state.restored` and `properties.get.custom`
on the same steps; `summary_available` is `false` headlessly and the five
summary fields are `null` (capability `dwgprops`).

What the run refused before the green one, each fixed in the backend and
pinned by a fake-ActiveX test:

- Three runs died with `RPC_E_CALL_REJECTED` (-2147418111) on the read that
  follows a document switch — `Documents.Count` straight after
  `Documents.Open`, after `Close(False)` inside `document_close`, and in
  `_ensure_document_state` — AutoCAD's own message filter refusing the call
  while it still switches documents. pywin32 has no
  `CoRegisterMessageFilter`, so the backend now waits that window out itself
  (`_wait_out_rejected_call`, read-only callables only, 0.25 s → 1 s pauses
  inside a 20 s budget) at those sites; any other error is not retried.
- `layer_list` on the reopened template raised `AttributeError: 'IAcadLayer'
  object has no attribute 'Color'. Did you mean: 'color'?`. The type library
  spells `IAcadEntity` / `IAcadLayer` `color` and `Lineweight`; late-bound
  dispatch resolved `.Color` / `.LineWeight` case-insensitively, but once a
  makepy cache exists (this machine's was regenerated on 2026-09-17) every
  object a method returns is a generated wrapper and the lookup is exact. The
  backend now spells both members as the type library does (works on both
  wrappers) and the fakes model the measured names.
- `layer_state_restore` raised `'IAcadObject' object has no attribute
  'Count'`: `Dictionaries.Item` and `IAcadDictionary.Item` are declared
  `IAcadObject*` (the narrowing `_com_unnarrow` already handled for MLEADER
  styles); the save before it went through the typed `Dictionaries.Add`, so
  only the reopen paths failed. Both now un-narrow.
- A state saved with layer `0` current came back with two warnings and `0`
  unapplied: the `Freeze` put on layer `0` raises 'Invalid layer' whatever
  the value, even `False` while thawed (measured on a scratch document;
  `color` / `Linetype` / `Lineweight` / `Plottable` / `LayerOn` / `Lock` /
  `ActiveLayer` all fine on it). `Freeze` is now written only when it would
  change the layer.
- Two of the smoke's own expectations predated the merged tree and were
  corrected to it: `mleaderstyle_create` is implemented on COM through the
  `ACAD_MLEADERSTYLE` dictionary (spec §4: no `mleaderstyle` key exists), so
  the smoke now asserts the created style is listed with `arrow_size` 2.5;
  and `plot_style_list` marks catalogue rows with `installed: true` (the
  `source: "installed"` rows are files the catalogue does not know), so the
  `monochrome.ctb` check reads that flag.

`--interactive` (`user_pick_point`) was not run in this session: the seat is
shared with other agents and nobody was confirmed at the keyboard, and an
unanswered `Utility.GetPoint` holds AutoCAD's command line — every COM client
gets `RPC_E_CALL_REJECTED` — until someone presses Esc, past the 60 s timeout.
The pick path was executed live earlier in the track (Task 23's measurement:
`GetPoint` answers in WCS while `GetEntity` answers in the current UCS). Each
plot also dropped AutoCAD's `plot.log` into the repo root; it is ignored now.

### Live COM smoke — mechanical parts and the sheet (tracks B + G)

`scripts/smoke_mech_com.py`, AutoCAD 2026 (`25.1s (LMS Tech)`), Windows 11,
2026-09-23, in new documents only (an xref source saved and closed first, then
the sheet) — `apply_layer_set("mech")` → `apply_titleblock` (A3, zones, marks)
→ `draw_part` → `add_view(section)` → `dimension_part` → `insert_std_part` →
`weld_symbol_prims` → `extract_records` → `draw_bom_table` → `add_balloon` →
`xref_attach` / `xref_manage("list")` / `xref_manage("detach")` →
`run_critique(focus=None)` → `drawing_export_dwg("R2018")`, all through the COM
engine, exit 0, every scratch document closed afterwards:

```json
{
  "autocad": "25.1s (LMS Tech)",
  "part_id": "103",
  "views": 2,
  "section_omitted": [],
  "dimensions": 12,
  "bolt_handle": "2F7",
  "xdata_chunks": 1,
  "parts_list_rows": 1,
  "parts_list_representation": "native",
  "balloon": "31A",
  "xref_attached": "XREF_SOURCE",
  "xref_listed": 1,
  "xref_detached": true,
  "xrefs_after_detach": 0,
  "dwg": {
    "path": "%TEMP%\\mech_smoke_*\\mech_smoke.dwg",
    "exists": true,
    "bytes": 31687,
    "magic": "AC1032"
  },
  "critique_issues": 0,
  "critique": []
}
```

The first runs found the four live defects listed under **Fixed** (the `MECH`
layer, `BLOCKREFERENCE`, the dimension `text_position`, the refused LINE read)
and one harness fault: the first call after `Documents.Add` can be refused with
`RPC_E_CALL_REJECTED` while the new document initialises, and a refused
cleanup then left the scratch document open. The smoke retries refused calls
for up to 90 s (a refused call was not executed) and tracks its documents by
the document list rather than by an assumed name, because `SaveAs` renames the
document it writes.

### Live COM smoke — architecture (track F)

`scripts/smoke_arch_com.py`, AutoCAD 2026, Windows 11, in a new document only —
`draw_plan_from_spec(EXAMPLE_SPEC)` (walls, openings, stair, two room labels,
chains, three schedules in one transaction) → `read_plan` → `rooms_detect` →
`run_critique(focus=None)`, all through the COM engine, exit 0, the scratch
document closed afterwards:

```json
{
  "autocad": "25.1s (LMS Tech)",
  "walls": [
    "EXT",
    "INT"
  ],
  "openings": [
    "D1",
    "D2",
    "W1",
    "W2"
  ],
  "omitted": [],
  "stairs": 1,
  "schedules": [
    {
      "kind": "doors",
      "representation": "native",
      "rows": 2
    },
    {
      "kind": "windows",
      "representation": "native",
      "rows": 2
    },
    {
      "kind": "rooms",
      "representation": "native",
      "rows": 3
    }
  ],
  "rooms": {
    "01": {
      "label_mm2": 27743750.0,
      "detected_mm2": 27743750.0,
      "hand_mm2": 27743750.0,
      "agrees": true
    },
    "02": {
      "label_mm2": 21993750.0,
      "detected_mm2": 21993750.0,
      "hand_mm2": 21993750.0,
      "agrees": true
    }
  },
  "faces_detected": 2,
  "confidence_min": 1.0,
  "critique_issues": 0,
  "critique": []
}
```

The first run exited 1: the first room label was refused because the room
reader found 0 wall segments on `A-WALL-E-N` — the `entity_list` narrowing
listed under **Fixed**. The run above is the one after the fix.

### Live COM smoke — understanding (track H)

`scripts/smoke_understand_com.py`, AutoCAD 2026, Windows 11, on the synthetic
plant pair only — `take_snapshot` of `pid.dxf` opened live, the same P&ID
saved as DWG and read back through the read-only DWG route, `take_snapshot`
of `layout.dxf`, `pipe_rows` / `cable_rows` on the live snapshots against the
pair's truth, then one tag moved 10 mm and `diff_snapshots` of the layout
before and after, all through the COM engine, exit 0, every document closed:

```json
{
  "autocad": "25.1s (LMS Tech)",
  "export_s": 0.104,
  "live_pid_source": "live:pid.dxf",
  "dwg_pid_source": "live:pid.dwg",
  "model_space": {
    "pid_file": {
      "ARC": 1,
      "INSERT": 15,
      "LINE": 8,
      "LWPOLYLINE": 8,
      "MTEXT": 8,
      "TEXT": 31
    },
    "pid_live": {
      "ARC": 1,
      "INSERT": 15,
      "LINE": 8,
      "LWPOLYLINE": 8,
      "MTEXT": 8,
      "TEXT": 31
    },
    "pid_dwg": {
      "ARC": 1,
      "INSERT": 15,
      "LINE": 8,
      "LWPOLYLINE": 8,
      "MTEXT": 8,
      "TEXT": 31
    },
    "layout_file": {
      "CIRCLE": 18,
      "LINE": 25,
      "LWPOLYLINE": 4,
      "MULTILEADER": 8,
      "TEXT": 26
    },
    "layout_live": {
      "CIRCLE": 18,
      "LINE": 25,
      "LWPOLYLINE": 4,
      "MULTILEADER": 8,
      "TEXT": 26
    }
  },
  "paper_space_layouts": [],
  "takeoffs": {
    "scale_verdict": "schematic",
    "within_10pct": 0.21818181818181817,
    "runs_checked": 9,
    "pipe_mismatch": [],
    "loads_checked": 4,
    "cable_mismatch": []
  },
  "diff": {
    "moved": "44",
    "added": 0,
    "removed": 0,
    "changed": 1,
    "moved_is_the_change": true
  }
}
```

The smoke works only in documents it brought in from its own temporary
folder, known by full path; each live snapshot, the DWG save and the move
first check, in the same COM-thread call, that the active document is the
smoke's own, and on the way out it re-activates the document that was active
when it started.

## [1.5.1] — 2026-08-06

A patch for three defects found *after* 1.5.0 went to PyPI, two of them by the
adversarial pass over the rewritten README. 1.5.0 is yanked: its diameter and
radius callouts put wrong numbers on drawings at default settings, and that is
not something to leave installable.

### Fixed

- **The HTTP bind guard failed open on the launch path it was written for.**
  `_guarded_run_async` read the bind address from the `host` kwarg and treated
  an absent one as loopback. It is not: `fastmcp run` forwards that kwarg only
  when `--host` was passed, and otherwise fastmcp resolves the host from
  `fastmcp.settings.host`, which is environment-backed. So
  `FASTMCP_HOST=0.0.0.0 fastmcp run server.py:mcp --transport http` bound every
  interface — 149 tools including file open/save and AutoCAD command execution,
  with no token — while the guard inspected the literal string `"127.0.0.1"`
  and waved it through. v1.4.0's changelog claimed the guard "now fires on
  every launch path"; it fired, and then looked at the wrong host. It now
  resolves the effective host, and says so in a warning if it cannot.
- **`[com]` and `[full]` could not be installed off Windows.** Both extras
  listed `pywin32>=306` with no environment marker, and pywin32 publishes
  Windows wheels and *no sdist* — so `pip install autocad-mcp-pro[full]` died
  at resolution on Linux and macOS, and `[full]` is the extra this project's
  own README recommended to contributors. CI never caught it because every
  Linux lane installs `[pdf]` or syncs from `uv.lock`, whose resolution markers
  hide the gap. Both entries now carry `; sys_platform == "win32"`, and a
  release-consistency test fails if a Windows-only distribution loses its
  marker again.
- **`drawing_settings({"dimscale": ...})` reported success and changed nothing
  headlessly.** It writes the `$DIMSCALE` header variable, which is what
  AutoCAD consults when it draws a dimension — so the call worked on the live
  backend. ezdxf renders from the dimstyle table entry and never reads the
  header, so the same call produced two different drawings on the two engines
  while both said `ok`. The header value is now folded into the per-dimension
  override on all five dimension tools, including `dimension_aligned` and
  `dimension_angular`, which do not share the tolerance-override path and were
  missed by the obvious fix. DIMSCALE 1.0 writes no override.

### Changed

- README: the "What you get" table qualified every engine-conditional family
  (boundary tracing, WIPEOUT/REVCLOUD/MTEXT masks, CHSPACE, typed hatch edge
  paths, headless undo) instead of listing them as if both engines had them,
  and the checkout install line no longer recommends an extra that cannot
  resolve on the reader's platform.

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
