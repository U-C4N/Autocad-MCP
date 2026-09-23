"""The v5 task matrix: v4 plus what tracks B and G added.

``mech_assembly`` (tracks B + G) is roadmap criterion 3: a flange-coupling
sheet built from the tools — two parts with a section, a bolt circle of ISO
4014 bolts and ISO 4032 nuts, ISO 129 dimensions, an ISO 5457 A3 frame, an
ISO 7200 title block and an ISO 7573 parts list with balloons. The gate is two
numbers, not an impression: ``drawing_critique(focus=None)`` returns zero
issues and the finalize score is at least 90. Either one slipping fails the
task outright.

The published competitor reports were never asked this question; the chart
shows ``not_run`` for them, not zero.
"""

from __future__ import annotations

from benchmarks.tasks_v2 import TASKS_V2
from benchmarks.tasks_v3 import TASKS_V3
from benchmarks.tasks_v4 import TASKS_V4, TaskSpec

#: Added in v1.6.0 by tracks B + G.
NEW_TASKS_V5: tuple[TaskSpec, ...] = (
    TaskSpec(
        "mech_assembly",
        "mech",
        "Draw a flange-coupling sheet from the part model and the sheet standard, "
        "with a clean critique and a finalize score of at least 90",
        1.0,
    ),
)

TASKS_V5: tuple[TaskSpec, ...] = TASKS_V4 + NEW_TASKS_V5

#: Runner-selectable task sets. v2, v3 and v4 stay addressable so an earlier
#: report can be reproduced exactly rather than only described.
MATRICES: dict[str, tuple[TaskSpec, ...]] = {
    "v2": TASKS_V2,
    "v3": TASKS_V3,
    "v4": TASKS_V4,
    "v5": TASKS_V5,
}

DEFAULT_MATRIX = "v5"


def task_by_id(task_id: str, matrix: str = DEFAULT_MATRIX) -> TaskSpec:
    for task in MATRICES[matrix]:
        if task.task_id == task_id:
            return task
    raise KeyError(f"{task_id} is not in matrix {matrix}")
