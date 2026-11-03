"""The v9 task matrix: v8 plus what 1.8.0 makes provable (plan section 8).

Each 1.8.0 item appends its task to ``NEW_TASKS_V9`` (at the end, in the order
the items land) and its ``_task_<id>`` to the reference adapter; the default
matrix stays ``v8`` until 1.8-25 switches it, so a run without ``--matrix v9``
measures exactly what 1.7.0 published.

* ``dimension_lie_detect`` (1.8-15, C2): a drawing whose dimensions lie in
  seven ways - a typed override, DIMLFAC, a stale ``*D`` block, a stale
  group 42, a double Ø, a value on a layer that is off, and one drawn over
  nothing - yields exactly one ``dimension_truth`` finding per lie, on the
  dimension that tells it; a 19.6 mm dimension under DIMDEC 0 is reported as
  showing "20". The broken-input twin: DIMDEC 2 -> 0 with ``rerender=False``
  leaves a stale dimension that ``dim_render_stale`` names and
  ``drawing_refine`` clears.
"""

from __future__ import annotations

from benchmarks.tasks_v8 import MATRICES as MATRICES_V8
from benchmarks.tasks_v8 import TASKS_V8, TaskSpec

#: Added in v1.8.0.
NEW_TASKS_V9: tuple[TaskSpec, ...] = (
    TaskSpec(
        "dimension_lie_detect",
        "qa",
        "Seven dimensions that each lie one way yield exactly one dimension_truth finding "
        "each; DIMDEC 0 over 19.6 shows '20'; a stale redraw is named and refined away",
        1.0,
    ),
)

TASKS_V9: tuple[TaskSpec, ...] = TASKS_V8 + NEW_TASKS_V9

#: Runner-selectable task sets: every earlier matrix plus v9.
MATRICES: dict[str, tuple[TaskSpec, ...]] = {**MATRICES_V8, "v9": TASKS_V9}

#: Unchanged until 1.8-25 publishes the v9 report.
DEFAULT_MATRIX = "v8"


def task_by_id(task_id: str, matrix: str = DEFAULT_MATRIX) -> TaskSpec:
    for task in MATRICES[matrix]:
        if task.task_id == task_id:
            return task
    raise KeyError(f"{task_id} is not in matrix {matrix}")
