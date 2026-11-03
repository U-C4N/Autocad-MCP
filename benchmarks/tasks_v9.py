"""The v9 task matrix (1.8.0): v8 plus what 1.8.0 makes provable.

Each 1.8.0 plan item appends its own task at the END of ``NEW_TASKS_V9`` (the
coordinator merges the parallel branches); ``DEFAULT_MATRIX`` stays ``v8``
until plan item 1.8-25 publishes the v9 matrix.

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
)

TASKS_V9: tuple[TaskSpec, ...] = TASKS_V8 + NEW_TASKS_V9

#: v9 tasks that drive the real server in-process on the headless engine; on
#: the live lane they are ``not_run`` (the live evidence is the COM smoke
#: scripts), never a zero.
HEADLESS_ONLY_V9: frozenset[str] = frozenset({"layer_cleanup_budget", "call_count_dimension_part"})

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
