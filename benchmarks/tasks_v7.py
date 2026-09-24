"""The v7 task matrix: v6 plus what track H added.

Track H teaches the server to read drawings somebody else made, so its two
tasks are gated on a drawing pair whose answers are known in advance - the
synthetic plant pair of ``tests/fixtures/plant_pair.py``, which reproduces
every field finding of the design spec with invented names:

* ``takeoff_roundtrip`` - the scale check calls the P&ID ``schematic``; every
  routable pipe run's layout length equals the rectilinear minimum spanning
  tree of its tags exactly; its size, service and status match the generator's
  truth; the cable rows equal the Manhattan lengths from each load to its panel
  with the allowance rounded up to the metre.
* ``understand_foreign`` - one ``describe`` of the layout flags inches declared
  over millimetre geometry, finds the stray entity by handle and separates the
  two plan copies; one ``describe`` of the P&ID classifies its English and
  Dutch service layers.

The published competitor reports were never asked these questions; the chart
shows ``not_run`` for them, not zero.
"""

from __future__ import annotations

from benchmarks.tasks_v2 import TASKS_V2
from benchmarks.tasks_v3 import TASKS_V3
from benchmarks.tasks_v4 import TASKS_V4
from benchmarks.tasks_v5 import TASKS_V5
from benchmarks.tasks_v6 import TASKS_V6, TaskSpec

#: Added in v1.6.0 by track H.
NEW_TASKS_V7: tuple[TaskSpec, ...] = (
    TaskSpec(
        "takeoff_roundtrip",
        "plant",
        "Take pipe and cable quantities off a schematic P&ID and its layout: the P&ID "
        "called schematic, every run's layout length equal to the rectilinear MST of "
        "its tags, sizes and services as drawn, cable metres rounded up after the "
        "allowance",
        1.0,
    ),
    TaskSpec(
        "understand_foreign",
        "understand",
        "Read a foreign layout in one call: inches declared over millimetres flagged, "
        "the stray entity found, both plan copies separated, the service layers of its "
        "P&ID classified",
        1.0,
    ),
)

TASKS_V7: tuple[TaskSpec, ...] = TASKS_V6 + NEW_TASKS_V7

#: Runner-selectable task sets. v2 to v6 stay addressable so an earlier report
#: can be reproduced exactly rather than only described.
MATRICES: dict[str, tuple[TaskSpec, ...]] = {
    "v2": TASKS_V2,
    "v3": TASKS_V3,
    "v4": TASKS_V4,
    "v5": TASKS_V5,
    "v6": TASKS_V6,
    "v7": TASKS_V7,
}

DEFAULT_MATRIX = "v7"


def task_by_id(task_id: str, matrix: str = DEFAULT_MATRIX) -> TaskSpec:
    for task in MATRICES[matrix]:
        if task.task_id == task_id:
            return task
    raise KeyError(f"{task_id} is not in matrix {matrix}")
