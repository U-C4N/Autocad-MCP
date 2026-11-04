"""The v9 task matrix (1.8.0): v8 plus what 1.8.0 makes provable (plan section 8).

Each 1.8.0 plan item appended its own task at the END of ``NEW_TASKS_V9`` (in
the order the items landed) and its ``_task_<id>`` to the reference adapter.
Since plan item 1.8-25 v9 is the runner's ``DEFAULT_MATRIX`` (this module's
constant; ``tasks_v8.DEFAULT_MATRIX`` stays ``"v8"`` for its own callers, as
every earlier ``tasks_vN`` module keeps its own), the run manifest carries
``EVALUATOR_VERSION`` ``"v9.0"``, and ``--matrix v8`` still reproduces the
1.7.0 task set.

**The rival's ground.** ``RIVAL_GROUND`` extends v8's with
``lizaixi01-agentcad`` (AgentCAD), on the tasks that answer the behavioural
claims it published against us (plan section 2 / section 8; ``RIVAL_SCENARIO``
names each one): ``history_value_lookup`` (§4.1 H1, the round-2 value after
five revision rounds), ``undo_across_restart`` (§4.2, undo after the server
is killed), ``atomic_batch_default`` (§4.3, a failed batch whose first step
stayed applied), ``layer_cleanup_budget`` (§4.4 H4, 124 calls against its 11),
``call_count_dimension_part`` (§4.4 H5, 89 calls against its 10) and
``parametric_revise_keeps_handles`` (B24-B28, the weaknesses its own
``revise_drawing`` admits: it overwrites a hand-edited tagged entity). No
adapter for AgentCAD exists in ``benchmarks/competitors.yaml``, so none of
these is ever scored for it: a task a rival was never asked is ``not_run``.

**Kill points.** ``KILL_POINTS`` names, per task, where a real ``python
server.py`` is killed with ``Popen.kill()`` (TerminateProcess on Windows); the
run manifest records them with the state-folder policy, so a report says
which deaths it survived. ``benchmarks/fault_inject.py`` is the harness that
generalises those kills (and can kill at a named in-process barrier).

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
* ``undo_across_restart`` (1.8-08, the rival's §4.2 claim): a real
  ``python server.py`` draws a line, saves, moves it, saves and is killed with
  ``Popen.kill()``; a new process opens the file and ``drawing_undo`` puts the
  line back where it was before the move - no undo setting in the
  environment. Its broken twin: a move after the last save, then a kill; the
  file is untouched and the reopen reports ``lost_since_save.count == 1``.
* ``atomic_batch_default`` (1.8-09, the rival's §4.3 probe): ``cad_batch``
  with ``[entity_move(L), entity_delete("DEAD")]`` and no ``on_error`` is
  refused ``unknown_handle`` with ``executed: 0`` and L's summary unchanged.
  Its run-time twin (the second step fails while it runs) is rolled back by
  the default ``on_error="auto"``, headless ``atomicity.verified: true``.
* ``history_value_lookup`` (1.8-10, the rival's H1): a real server draws three
  rounds of a plate with ``mech_part_draw`` (hole 10, 12 - its call labelled
  ``round 2`` - and 16), saving each, and is killed; a new process answers
  "the diameter in round 2" with ``drawing_open`` + ``drawing_history``: 12,
  in at most two calls. Twins: a label nobody gave is refused ``invalid_args``
  naming the labels there are, never guessed; an unlabelled round (one circle
  edited 5 -> 6 -> 8) is read from ``detail="changes"``'s checkpoint diff.
"""

from __future__ import annotations

from benchmarks.tasks_v8 import FIXTURE_SOURCES as _FIXTURE_SOURCES_V8
from benchmarks.tasks_v8 import MATRICES as _MATRICES_V8
from benchmarks.tasks_v8 import RIVAL_GROUND as _RIVAL_GROUND_V8
from benchmarks.tasks_v8 import TASKS_V8, TaskSpec

__all__ = [
    "DEFAULT_MATRIX",
    "EVALUATOR_VERSION",
    "FIXTURE_SOURCES",
    "HEADLESS_ONLY_V9",
    "KILL_POINTS",
    "MATRICES",
    "NEW_TASKS_V9",
    "RIVAL_GROUND",
    "RIVAL_SCENARIO",
    "STATE_DIR_POLICY",
    "TASKS_V9",
    "TaskSpec",
    "task_by_id",
]

#: The runner's matrix since 1.8-25 (``benchmarks/run_competitors.py`` and
#: ``scripts/check_doc_numbers.py`` read this module).
DEFAULT_MATRIX = "v9"

#: v8.1 -> v9.0 (1.8-25): the default matrix is v9 - thirteen new tasks, two new
#: fixture generators (``FIXTURE_SOURCES``) and kill-based gates - and the
#: reference adapter's v8 gates now run on the 1.8.0 engine (``dimension_binding``
#: formats through ``engineering/dimtext.py``). A report from this tree, v8 or
#: v9, was measured by this evaluator; ``tasks_v8.EVALUATOR_VERSION`` stays
#: ``"v8.1"``, the evaluator the published v8 report names.
EVALUATOR_VERSION = "v9.0"

#: v8's generators plus the two the v9 tasks draw from: the messy-layers and
#: plate scenarios of the call budget, and the seven lying dimensions.
FIXTURE_SOURCES: tuple[str, ...] = _FIXTURE_SOURCES_V8 + (
    "benchmarks/call_budget.py",
    "tests/fixtures/lying_dims.py",
)

_AGENTCAD = "lizaixi01-agentcad"

#: The rival whose ground a task stands on (v8's three plus AgentCAD's six).
#: Recorded so a lane report can say whose claim was tested; it never changes
#: how a task is scored.
RIVAL_GROUND: dict[str, str] = {
    **_RIVAL_GROUND_V8,
    "history_value_lookup": _AGENTCAD,
    "undo_across_restart": _AGENTCAD,
    "atomic_batch_default": _AGENTCAD,
    "layer_cleanup_budget": _AGENTCAD,
    "call_count_dimension_part": _AGENTCAD,
    "parametric_revise_keeps_handles": _AGENTCAD,
}

#: The rival's own scenario each AgentCAD task answers, as plan section 2 /
#: section 8 name it (the brief's section 4 claims; nothing else is mapped).
RIVAL_SCENARIO: dict[str, str] = {
    "history_value_lookup": "§4.1 H1: the value of round 2 after five revision rounds",
    "undo_across_restart": "§4.2: undo after the server was killed",
    "atomic_batch_default": "§4.3: a batch with an unknown handle left its first step applied",
    "layer_cleanup_budget": "§4.4 H4: layer cleanup in 124 calls (AgentCAD 11)",
    "call_count_dimension_part": "§4.4 H5: dimensioning a part in 89 calls (AgentCAD 10)",
    "parametric_revise_keeps_handles": (
        "B24-B28: revise_drawing overwrites a hand-edited tagged entity (its own admission)"
    ),
}

#: Where each task kills a real ``python server.py`` with ``Popen.kill()``
#: (TerminateProcess on Windows, no atexit or shutdown hook runs), in order.
#: Every kill so far is between two answered calls; a kill *inside* a call is
#: the named barriers of ``benchmarks/fault_inject.py`` (its own tests).
KILL_POINTS: dict[str, tuple[str, ...]] = {
    "undo_across_restart": (
        "after drawing_save of the moved line (process 1)",
        "after an unsaved entity_move (process 2)",
        "after drawing_open of the untouched file (process 3)",
    ),
    "history_value_lookup": (
        "after the three saved rounds and the saved unlabelled twin (process 1)",
        "after the drawing_history answers and the checkpoint diff (process 2)",
    ),
}

#: The state folder every killed server runs on: a private
#: ``tempfile.mkdtemp`` folder outside the repository, per task, removed after it.
STATE_DIR_POLICY = "private tempfile.mkdtemp per task, outside the repository, removed after it"

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
    TaskSpec(
        "undo_across_restart",
        "durability",
        "Draw, save, move, save, kill the server; a new process opens the file and one "
        "drawing_undo puts the line back before the move. Unsaved twin: the file is "
        "untouched and lost_since_save.count is 1",
        1.0,
    ),
    TaskSpec(
        "atomic_batch_default",
        "durability",
        "Move a line and delete a handle that does not exist in one batch with no on_error: "
        "refused unknown_handle, nothing executed, the line unchanged. Run-time twin: the "
        "default rolls the move back, verified",
        1.0,
    ),
    TaskSpec(
        "history_value_lookup",
        "durability",
        "Three rounds of a plate (hole 10, 12 labelled 'round 2', 16), saved, server killed; "
        "a new process finds the round-2 diameter 12 in at most two calls (drawing_open + "
        "drawing_history). Twins: a label nobody gave is refused by name; an unlabelled "
        "round is read from the checkpoint diff",
        1.0,
    ),
)

TASKS_V9: tuple[TaskSpec, ...] = TASKS_V8 + NEW_TASKS_V9

#: v9 tasks that reach into the headless engine (its document) or drive the
#: real server in-process on it; on the live lane they are ``not_run`` (the
#: live evidence is the COM smoke scripts), never a zero; the 1.8-08 / 09 / 10
#: durability tasks drive the headless engine or a headless server process by
#: construction. ``policy_read_only``
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
        "undo_across_restart",
        "atomic_batch_default",
        "history_value_lookup",
    }
)

#: Runner-selectable task sets: every earlier matrix plus v9. One registry: v9
#: is added to the v8 module's dict itself, so every reader of either module
#: (the runner, the chart, the tests) sees the same object. ``DEFAULT_MATRIX``
#: above is this module's own ("v9"); the v8 module keeps "v8".
MATRICES: dict[str, tuple[TaskSpec, ...]] = _MATRICES_V8
MATRICES["v9"] = TASKS_V9


def task_by_id(task_id: str, matrix: str = DEFAULT_MATRIX) -> TaskSpec:
    for task in MATRICES[matrix]:
        if task.task_id == task_id:
            return task
    raise KeyError(f"{task_id} is not in matrix {matrix}")
