"""The v9 task matrix (1.8.0): v8 plus what 1.8.0 makes provable (plan section 8).

Each 1.8.0 plan item appends its own task at the END of ``NEW_TASKS_V9`` (in
the order the items land; the coordinator merges the parallel branches) and
its ``_task_<id>`` to the reference adapter; ``DEFAULT_MATRIX`` stays ``v8``
until plan item 1.8-25 publishes the v9 matrix, so a run without
``--matrix v9`` measures exactly what 1.7.0 published.

* ``dimension_lie_detect`` (1.8-15, C2): a drawing whose dimensions lie in
  seven ways - a typed override, DIMLFAC, a stale ``*D`` block, a stale
  group 42, a double Ø, a value on a layer that is off, and one drawn over
  nothing - yields exactly one ``dimension_truth`` finding per lie, on the
  dimension that tells it; a 19.6 mm dimension under DIMDEC 0 is reported as
  showing "20". The broken-input twin: DIMDEC 2 -> 0 with ``rerender=False``
  leaves a stale dimension that ``dim_render_stale`` names and
  ``drawing_refine`` clears.
* ``policy_read_only`` (1.8-17, security) - under ``READ_ONLY`` a ``cad_batch``
  with one create step is refused before step 1 with the content revision and
  the entity count unchanged; the broken-input twin sends the write through the
  search proxy (``call_tool("entity_create_line")``) and must be refused
  ``policy_refused`` the same way.
* ``lisp_read_tier`` (1.8-18, security) - three AutoLISP reads run on the
  first try through ``system_run_lisp`` (a circle's ``vla-get-Area`` equal to
  ``analysis_measure_entity``, ``getvar "CLAYER"`` equal to
  ``system_get_variable``, ``(mapcar (function (lambda ...)))``); the
  broken-input twin ``(command "_ERASE" "_L" "")`` is refused with symbol,
  line and the typed ``alternatives``. The reads need AutoCAD, so the headless
  lane is ``environment_blocked`` (never a pass); it still records the policy's
  verdicts in its metrics.
* ``layer_cleanup_budget`` (1.8-12 / 1.8-13, ergonomics, the rival's §4.4 H4) -
  the messy-layers drawing of ``benchmarks/call_budget.py`` (six misnamed
  layers, 300 entities with colour overrides at lineweight -3) brought to ISO
  layer names, ByLayer and no empty layer in at most 3 counted calls, with
  ``drawing_critique(focus=["iso128", "layer_color", "property_override"])``
  empty. Its broken-input twin: an ``expect_count`` the drawing does not hold
  is refused ``stale_revision`` and nothing changes.
* ``call_count_dimension_part`` (1.8-13, ergonomics, §4.4 H5) - the plate
  (a closed LWPOLYLINE, six circles, two arcs) dimensioned in at most 2 counted
  calls (``dimension_auto(selector={"layer": "GEOMETRY"})`` +
  ``drawing_critique``): every dimension bound by
  ``drawing_verify_requirements`` with no ``dimension_mismatch``,
  ``mech_duplicate_dimension`` clean, and a second run draws nothing. Its
  broken-input twin: an ELLIPSE and a SPLINE in the selection come back in
  ``omitted`` with their reasons, never silently dropped.
* ``pid_project_offpage`` (1.8-21, P&ID) - three P&ID sheet files drawn by
  ``pid_from_spec`` and read by ``pid_project(action="check")`` through the
  real server: one orphan link, one direction mismatch, one cross-sheet
  duplicate tag and one line-number mismatch, each reported exactly once with
  the sheet and the handle the drawing gave it (connector TAGs repeated on
  every sheet never count). The broken-input twins: a manifest naming a
  missing sheet file is refused ``file_not_found`` and 51 sheets
  ``invalid_args``, both by name and before anything is read.
* ``std_tables_b40`` (1.8-22, standards, B40) - one size drawn by
  ``std_feature_draw`` from every DIN 471 / DIN 472 / DIN 509 / DIN 332-1 /
  ISO 3601-2 table that has transcribed rows, its dimensions equal to the
  double entry's own row; the broken-input twin draws a size between two rows
  (or past the only one) and must be refused by name with nothing drawn. With
  no table filled - 1.8.0 ships none, the licensed copies were not at hand -
  the task is ``not_run`` naming the tables deferred to 1.8.1, never a vacuous
  pass. It runs through the backend contract, so it is not headless-only.
* ``dwg_headless_roundtrip`` (K1, reliability) - a DWG (the bundled
  ``templates/iso_a3_mech.dwt`` under a ``.dwg`` name: a DWT is a DWG
  container) opened by ``drawing_open`` on the headless engine through a
  converter (ODA, then accoreconsole) and saved as DXF holds the layers,
  blocks and per-layout entity counts of the template's DXF twin. Its
  broken-input twin: with the converters switched off the same open is refused
  ``unsupported`` / capability ``dwg_read`` with the open document unchanged.
  No converter on the machine is ``environment_blocked``, never a pass.
* ``parametric_revise_keeps_handles`` (1.8-19, parametric, B24-B28) - a
  dimensioned flange in three views with a bolt circle and a stored Ø24 bore
  requirement; one ``mech_part_revise`` (bore 20 -> 24, length 60 -> 70, a
  chamfer added, the step radius removed) must keep every LINE / CIRCLE / ARC
  handle it edits in place, leave the drawing's entity signatures equal to a
  fresh draw of the revised part (written out by hand here, not derived), make
  the dimensions read ⌀24 and (70) and the requirement pass. Its broken-input
  twin ``revise_hand_edit_refused``: a tagged line moved by hand refuses the
  same revision ``hand_edited_tagged_entity`` with that handle, every
  fingerprint unchanged.
* ``pid_routing_obstacles`` (1.8-20, P&ID) - a sheet whose fixed ``auto``
  routes run through two vessels, drawn with ``sheet.routing: "avoid"``: zero
  ``pid_line_through_symbol`` findings, zero dangling ends, every route's
  ``obstacle_hits`` empty, the drawn vertices equal to the dry run's and the
  crossings no more than the dry run declared. Its broken-input twin: a pump
  walled in by four vessels is refused ``no_route`` naming exactly those
  vessels' tags, with nothing drawn.
"""

from __future__ import annotations

from benchmarks.tasks_v8 import (
    DEFAULT_MATRIX,
    EVALUATOR_VERSION,
    FIXTURE_SOURCES,
    TASKS_V8,
    TaskSpec,
)
from benchmarks.tasks_v8 import MATRICES as _MATRICES_V8

__all__ = [
    "DEFAULT_MATRIX",
    "EVALUATOR_VERSION",
    "FIXTURE_SOURCES",
    "HEADLESS_ONLY_V9",
    "MATRICES",
    "NEW_TASKS_V9",
    "TASKS_V9",
    "TaskSpec",
    "task_by_id",
]

#: Added in v1.8.0, one plan item at a time.
NEW_TASKS_V9: tuple[TaskSpec, ...] = (
    TaskSpec(
        "dimension_lie_detect",
        "qa",
        "Seven dimensions that each lie one way yield exactly one dimension_truth finding "
        "each; DIMDEC 0 over 19.6 shows '20'; a stale redraw is named and refined away",
        1.0,
    ),
    TaskSpec(
        "policy_read_only",
        "security",
        "Under READ_ONLY, refuse a one-create-step batch before step 1 with the revision "
        "unchanged, and refuse the same write sent through the search proxy's call_tool",
        1.0,
    ),
    TaskSpec(
        "lisp_read_tier",
        "security",
        "Run three AutoLISP reads (vla-get-Area, getvar, a mapcar over a lambda) on the "
        'first try, and refuse (command "_ERASE" ...) naming symbol, line and the typed '
        "alternatives; live engine only (headless: environment_blocked)",
        1.0,
    ),
    TaskSpec(
        "layer_cleanup_budget",
        "ergonomics",
        "Clean six misnamed layers and 300 overridden entities to ISO names, ByLayer and no "
        "empty layer in at most 3 calls, with the iso128 / layer_color / property_override "
        "critique empty; a stale expect_count is refused stale_revision with nothing changed",
        1.0,
    ),
    TaskSpec(
        "call_count_dimension_part",
        "ergonomics",
        "Dimension a plate (outline, six holes, two arcs) in at most 2 calls: every dimension "
        "bound without a dimension_mismatch, no duplicate, a second run draws nothing; an "
        "ELLIPSE and a SPLINE in the selection are omitted with their reasons",
        1.0,
    ),
    TaskSpec(
        "pid_project_offpage",
        "pid",
        "Check three P&ID sheet files: one orphan link, one direction mismatch, one "
        "cross-sheet duplicate tag and one line-number mismatch, each once with sheet and "
        "handle; a missing sheet file and 51 sheets are refused by name",
        1.0,
    ),
    TaskSpec(
        "std_tables_b40",
        "standards",
        "Draw one size from every filled DIN 471/472, DIN 509, DIN 332-1 and ISO 3601-2 "
        "table with the double entry's own dimensions; a size between two rows is refused "
        "by name with nothing drawn; no filled table is not_run naming the deferred tables",
        1.0,
    ),
    TaskSpec(
        "dwg_headless_roundtrip",
        "reliability",
        "Open a DWG on the headless engine through a converter and save it as DXF with the "
        "layers, blocks and entity counts of its DXF twin; with no converter the open is "
        "refused dwg_read (no converter on the machine: environment_blocked)",
        1.0,
    ),
    TaskSpec(
        "parametric_revise_keeps_handles",
        "parametric",
        "Revise a dimensioned three-view flange (bore 20->24, length 60->70, +chamfer, "
        "-radius) in one call: in-place handles kept, signatures equal a fresh draw, dims "
        "read 24 and 70, the bore requirement passes; a hand-moved tagged line is refused "
        "hand_edited_tagged_entity with the fingerprints unchanged",
        1.0,
    ),
    TaskSpec(
        "pid_routing_obstacles",
        "pid",
        "Route a sheet whose auto lines cross two vessels with sheet.routing avoid: zero "
        "pid_line_through_symbol, zero dangling ends, crossings within the dry run's; a "
        "walled-in pump is refused no_route naming the four vessels' tags",
        1.0,
    ),
)

TASKS_V9: tuple[TaskSpec, ...] = TASKS_V8 + NEW_TASKS_V9

#: v9 tasks that reach into the headless engine (its document) or drive the
#: real server in-process on it; on the live lane they are ``not_run`` (the
#: live evidence is the COM smoke scripts), never a zero. ``policy_read_only``
#: runs on both lanes and ``lisp_read_tier`` is the live lane's own task
#: (headless: ``environment_blocked``), so neither is listed here.
HEADLESS_ONLY_V9: frozenset[str] = frozenset(
    {
        "dimension_lie_detect",
        "layer_cleanup_budget",
        "call_count_dimension_part",
        "pid_project_offpage",
        "dwg_headless_roundtrip",
        "parametric_revise_keeps_handles",
        "pid_routing_obstacles",
    }
)

#: Runner-selectable task sets: every earlier matrix plus v9. One registry: v9
#: is added to the v8 module's dict itself, so every reader of either module
#: (the runner, the chart, the tests) sees the same object. ``DEFAULT_MATRIX``
#: is the v8 module's and stays ``"v8"`` until 1.8-25 publishes the v9 report.
MATRICES: dict[str, tuple[TaskSpec, ...]] = _MATRICES_V8
MATRICES["v9"] = TASKS_V9


def task_by_id(task_id: str, matrix: str = DEFAULT_MATRIX) -> TaskSpec:
    for task in MATRICES[matrix]:
        if task.task_id == task_id:
            return task
    raise KeyError(f"{task_id} is not in matrix {matrix}")
