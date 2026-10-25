"""The v8 task matrix: v7 plus what 1.7.0 made provable, and the rivals' ground.

1.7.0 (spec §6 Task 20, B56) made two promises: what the server draws is
*measured* - a source requirement bound to the real CAD feature and measured
again on the saved file - and what it runs is *safe* - a cancelled call, a big
drawing or a hostile text cell can no longer produce a silent wrong answer.
Each promise gets tasks that can fail:

* ``requirements_roundtrip`` - a plate drawn from its part model passes four
  requirements on the document; the inner bore is then moved 3 mm inside the
  outline of the saved file (same counts, same extents - what 1.6's delivery
  parity compared) and the reopened file must fail exactly the one requirement
  that moved, both when the artifact is re-bound from the document and when it
  is opened on its own;
* ``cancel_safety`` - a call cancelled while its worker still holds the
  drawing: the next call waits for that worker, never runs beside it;
* ``qa_large_drawing`` - 10,000 lines with offenders at the 5,001st position,
  after the 10,000th and on a paper layout: every one found by handle, no
  ``scan_incomplete`` and no ``check_failed``;
* ``export_safety`` - text read off a drawing (P&ID line numbers, block
  attributes) that starts like a formula leaves as text in CSV and XLSX, and a
  genuine ``-12.5`` stays a number;
* ``stale_preview`` - a ``cad_batch`` preview, an edit behind the server's
  back, and the apply refused ``stale_revision`` with nothing changed.

And tasks on the rivals' ground (``RIVAL_GROUND``), where another server makes
the claim:

* ``revision_guarded_edit`` (beiming183's ``expected_revision`` discipline):
  each edit moves the revision by one; an apply against an old revision is
  refused and changes nothing;
* ``pid_file_roundtrip`` (puran-water's P&ID tools): a sheet drawn from a spec,
  saved, reopened by a fresh engine and read back identically - tags, edges,
  line numbers, instrument index;
* ``foreign_edit_verified``: a drawing somebody else made (no record of ours)
  is measured against requirements, repaired under a fingerprint precondition
  and accepted on the saved file, every other entity unchanged;
* ``damaged_dxf_refusal``: a truncated DXF and a file that is not a DXF at
  all are refused by name and the open document is kept. 1.7.0 refuses;
  ``recover`` arrives in 1.7.1, and this task is named for what 1.7.0 does;
* ``dimension_binding`` (best-cad-mcp's claim): a DIMENSION bound to the
  features it measures, its displayed text and DIMLFAC judged against the
  geometry - best-cad is COM-only with no adapter, so its lane is ``not_run``.

A rival lane never reports zero for a task it was not asked: no playbook is
``not_run``, a lane whose environment is absent is ``environment_blocked``,
and neither enters a score (``benchmarks/run_competitors.py``).
"""

from __future__ import annotations

from benchmarks.tasks_v2 import TASKS_V2
from benchmarks.tasks_v3 import TASKS_V3
from benchmarks.tasks_v4 import TASKS_V4
from benchmarks.tasks_v5 import TASKS_V5
from benchmarks.tasks_v6 import TASKS_V6
from benchmarks.tasks_v7 import TASKS_V7, TaskSpec

#: Added in v1.7.0 (B56).
NEW_TASKS_V8: tuple[TaskSpec, ...] = (
    TaskSpec(
        "requirements_roundtrip",
        "verify",
        "Draw a part from its model, verify four requirements on the document, move the "
        "inner bore inside the saved file's outline and fail exactly that requirement on "
        "the reopened file",
        1.0,
    ),
    TaskSpec(
        "cancel_safety",
        "reliability",
        "Cancel a call while its worker holds the drawing; the next call waits for the "
        "worker and never runs beside it",
        1.0,
    ),
    TaskSpec(
        "qa_large_drawing",
        "reliability",
        "Find every offender in a 10,000-line drawing - at the 5,001st position, after the "
        "10,000th and on a paper layout - with no incomplete scan",
        1.0,
    ),
    TaskSpec(
        "export_safety",
        "reliability",
        "Export drawing text that starts like a formula as text in CSV and XLSX, and a "
        "genuine negative number as a number",
        1.0,
    ),
    TaskSpec(
        "stale_preview",
        "preview",
        "Preview a batch, edit an entity behind the server's back, and have the apply "
        "refused stale_revision with nothing changed",
        1.0,
    ),
    TaskSpec(
        "revision_guarded_edit",
        "preview",
        "Edit under expected_revision: one revision per edit, an apply against an old "
        "revision refused and nothing changed",
        1.0,
    ),
    TaskSpec(
        "pid_file_roundtrip",
        "pid",
        "Draw a P&ID from a spec, save it, reopen it in a fresh engine and read the same "
        "tags, edges, line numbers and instrument index back",
        1.0,
    ),
    TaskSpec(
        "foreign_edit_verified",
        "verify",
        "Measure a foreign drawing against requirements, repair the one failing hole under "
        "a fingerprint precondition, accept it on the saved file with nothing else changed",
        1.0,
    ),
    TaskSpec(
        "damaged_dxf_refusal",
        "reliability",
        "Open a truncated DXF and a non-DXF: refused by name, the open document kept "
        "(recover arrives in 1.7.1)",
        1.0,
    ),
    TaskSpec(
        "dimension_binding",
        "verify",
        "Bind DIMENSIONs to the features they measure and judge their displayed text and "
        "DIMLFAC against the geometry",
        1.0,
    ),
)

TASKS_V8: tuple[TaskSpec, ...] = TASKS_V7 + NEW_TASKS_V8

#: The rival whose ground a v8 task stands on. Recorded so a lane report can say
#: whose claim was tested; it never changes how a task is scored.
RIVAL_GROUND: dict[str, str] = {
    "revision_guarded_edit": "beiming183-autocad-mcp",
    "pid_file_roundtrip": "puran-water-autocad-mcp",
    "dimension_binding": "best-cad-mcp",
}

#: Bumped whenever a task's gate or its fixture changes, so two reports with the
#: same evaluator version measured the same thing (the run manifest carries it).
EVALUATOR_VERSION = "v8.1"

#: Repository files that generate the fixtures the matrix runs on; the run
#: manifest records their sha256 so a report names the inputs it measured.
FIXTURE_SOURCES: tuple[str, ...] = (
    "benchmarks/adapters/autocad_mcp_pro.py",
    "engineering/arch/spec.py",
    "engineering/pid/spec.py",
    "tests/fixtures/plant_pair.py",
)

#: Runner-selectable task sets. v2 to v7 stay addressable so an earlier report
#: can be reproduced exactly rather than only described.
MATRICES: dict[str, tuple[TaskSpec, ...]] = {
    "v2": TASKS_V2,
    "v3": TASKS_V3,
    "v4": TASKS_V4,
    "v5": TASKS_V5,
    "v6": TASKS_V6,
    "v7": TASKS_V7,
    "v8": TASKS_V8,
}

DEFAULT_MATRIX = "v8"


def task_by_id(task_id: str, matrix: str = DEFAULT_MATRIX) -> TaskSpec:
    for task in MATRICES[matrix]:
        if task.task_id == task_id:
            return task
    raise KeyError(f"{task_id} is not in matrix {matrix}")
