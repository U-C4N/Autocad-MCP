"""The v9 task matrix (1.8.0): v8 plus what 1.8.0 makes provable.

Each 1.8.0 plan item appends its own task at the END of ``NEW_TASKS_V9`` (the
coordinator merges the parallel branches); ``DEFAULT_MATRIX`` stays ``v8``
until plan item 1.8-25 publishes the v9 matrix.

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
    "MATRICES",
    "NEW_TASKS_V9",
    "TASKS_V9",
    "TaskSpec",
    "task_by_id",
]

#: Added in v1.8.0, one plan item at a time.
NEW_TASKS_V9: tuple[TaskSpec, ...] = (
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
)

TASKS_V9: tuple[TaskSpec, ...] = TASKS_V8 + NEW_TASKS_V9

#: Runner-selectable task sets: every earlier matrix plus v9. One registry: v9
#: is added to the v8 module's dict itself, so every reader of either module
#: (the runner, the chart, the tests) sees the same object.
MATRICES: dict[str, tuple[TaskSpec, ...]] = _MATRICES_V8
MATRICES["v9"] = TASKS_V9


def task_by_id(task_id: str, matrix: str = DEFAULT_MATRIX) -> TaskSpec:
    for task in MATRICES[matrix]:
        if task.task_id == task_id:
            return task
    raise KeyError(f"{task_id} is not in matrix {matrix}")
