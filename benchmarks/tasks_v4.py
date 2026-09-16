"""The v4 task matrix: v3 plus the P&ID round trip (v1.6, track A).

``pid_roundtrip`` draws a five-item P&ID through the same code the tools call
and reads it back: the edge count, the absence of dangling ends and the
instrument index are each derived independently of the drawing code. The
published competitor reports were never asked this question; the chart shows
``not_run`` for them, not zero.
"""

from __future__ import annotations

from benchmarks.tasks_v2 import TASKS_V2, TaskSpec
from benchmarks.tasks_v3 import TASKS_V3

#: Added in v1.6.0. The reader can fail this one on its own.
NEW_TASKS_V4: tuple[TaskSpec, ...] = (
    TaskSpec(
        "pid_roundtrip",
        "pid",
        "Draw a P&ID from a spec and read back a matching graph, index and line list",
        1.0,
    ),
)

TASKS_V4: tuple[TaskSpec, ...] = TASKS_V3 + NEW_TASKS_V4

#: Runner-selectable task sets. v2 and v3 stay addressable so an earlier
#: report can be reproduced exactly rather than only described.
MATRICES: dict[str, tuple[TaskSpec, ...]] = {"v2": TASKS_V2, "v3": TASKS_V3, "v4": TASKS_V4}

DEFAULT_MATRIX = "v4"


def task_by_id(task_id: str, matrix: str = DEFAULT_MATRIX) -> TaskSpec:
    for task in MATRICES[matrix]:
        if task.task_id == task_id:
            return task
    raise KeyError(f"{task_id} is not in matrix {matrix}")
