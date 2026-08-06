"""The v3 task matrix: the v2 ten, plus five the v2 ten could not see.

v2 scored this server 10/10, and that number was structurally uninformative:
every task in it exercised something the server was already built around. A
matrix that cannot fail is a matrix that measures nothing, so the five added
here were picked to cover capability the v2 set never touches — discovery,
idle token cost, hatch islands, selection semantics, and measuring an entity
that is already in the drawing.

Two of the five are renamed from the release plan, and the renames are the
point rather than bookkeeping:

``tool_discovery_bilingual`` -> ``tool_discovery``
    The plan assumed a Turkish query normalizer. One was written and then
    removed: this repository is English throughout, and the Turkish was an
    inference from the author's chat language, not a product requirement. A
    task named "bilingual" would advertise a capability that does not exist,
    which is the class of claim this release spent four milestones deleting.

``measurement_massprops`` -> ``measure_from_handle``
    No mass properties ship. The plan's eight-tool measurement family (F2) was
    cut to one after measurement showed the repo already carried eight
    ``analysis_*`` tools; centroids and moments of inertia were not among the
    survivors. What did ship is measuring a *handle* instead of a caller's
    remembered coordinates, so that is what the task is called.

The v2 ids and weights are untouched. The published competitor reports were
run against the v2 matrix at v1.4 and are not re-scored here — those servers
have never been asked these five questions, and inventing a result for them
would be worse than the gap. The task-matrix chart renders them ``not_run``.
"""

from __future__ import annotations

from benchmarks.tasks_v2 import TASKS_V2, TaskSpec

#: Added in v1.5.0. Every one of these can fail on this server.
NEW_TASKS_V3: tuple[TaskSpec, ...] = (
    TaskSpec(
        "tool_discovery",
        "discovery",
        "Resolve AutoCAD command names to tools through the search layer",
        1.0,
    ),
    TaskSpec(
        "token_budget",
        "efficiency",
        "Keep the idle catalog under a fixed token ceiling",
        1.0,
    ),
    TaskSpec(
        "hatch_islands",
        "hatch",
        "Hatch an area with an island and measure what it fills",
        1.0,
    ),
    TaskSpec(
        "selection_filter",
        "selection",
        "Separate window from crossing, and a polygon from its bounding box",
        1.0,
    ),
    TaskSpec(
        "measure_from_handle",
        "measurement",
        "Measure a curved boundary from its handle, bulges included",
        1.0,
    ),
)

TASKS_V3: tuple[TaskSpec, ...] = TASKS_V2 + NEW_TASKS_V3

#: Runner-selectable task sets. v2 stays addressable so a v2 report can be
#: reproduced exactly rather than only described.
MATRICES: dict[str, tuple[TaskSpec, ...]] = {"v2": TASKS_V2, "v3": TASKS_V3}

DEFAULT_MATRIX = "v3"


def task_by_id(task_id: str, matrix: str = DEFAULT_MATRIX) -> TaskSpec:
    for task in MATRICES[matrix]:
        if task.task_id == task_id:
            return task
    raise KeyError(f"Unknown benchmark task: {task_id}")
