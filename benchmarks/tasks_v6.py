"""The v6 task matrix: v5 plus what track F added.

``arch_roundtrip`` (track F) is the roadmap evidence for the plan model: a
two-room plan - an entrance door, an interior door, two windows and a stair -
drawn by ``arch_plan_from_spec``, then read back by ``arch_rooms_detect``,
which never sees the spec. Each room's detected area has to agree with the area
its label carries within 0.1 %, and both with the net floor computed by hand;
the door and window schedules have to list the drawn tags; and the sheet is
gated on the same two numbers as ``mech_assembly``: ``drawing_critique
(focus=None)`` returns zero issues and the finalize score is at least 90.

The published competitor reports were never asked this question; the chart
shows ``not_run`` for them, not zero.
"""

from __future__ import annotations

from benchmarks.tasks_v2 import TASKS_V2
from benchmarks.tasks_v3 import TASKS_V3
from benchmarks.tasks_v4 import TASKS_V4
from benchmarks.tasks_v5 import TASKS_V5, TaskSpec

#: Added in v1.6.0 by track F.
NEW_TASKS_V6: tuple[TaskSpec, ...] = (
    TaskSpec(
        "arch_roundtrip",
        "arch",
        "Draw a two-room plan from one spec, read its rooms back from the drawing "
        "within 0.1 %, schedule its tags, with a clean critique and a finalize score "
        "of at least 90",
        1.0,
    ),
)

TASKS_V6: tuple[TaskSpec, ...] = TASKS_V5 + NEW_TASKS_V6

#: Runner-selectable task sets. v2 to v5 stay addressable so an earlier report
#: can be reproduced exactly rather than only described.
MATRICES: dict[str, tuple[TaskSpec, ...]] = {
    "v2": TASKS_V2,
    "v3": TASKS_V3,
    "v4": TASKS_V4,
    "v5": TASKS_V5,
    "v6": TASKS_V6,
}

DEFAULT_MATRIX = "v6"


def task_by_id(task_id: str, matrix: str = DEFAULT_MATRIX) -> TaskSpec:
    for task in MATRICES[matrix]:
        if task.task_id == task_id:
            return task
    raise KeyError(f"{task_id} is not in matrix {matrix}")
