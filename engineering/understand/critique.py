"""The three topology critique focuses (spec §11).

``topo_dangling_endpoint`` (warning), ``topo_near_miss`` (error) and
``topo_interior_crossing`` (info) run ``topology.topology_findings`` - the
engine ``drawing_topology_check`` runs - once per critique run, shared through
``run_critique``'s context the way the P&ID, mechanical and architectural
focuses share theirs.

Which layers: the network layers ``vocab.classify_layer`` calls piping or
electrical with confidence >= 0.9, **minus every layer a semantic focus
already reads**: the P&ID layer set and line layers (``pid_*``) and the
architectural layer set (``arch_*``). One defect is never scored twice, and a
drawing made entirely by this server - a ``pid_from_spec`` sheet, an
``arch_plan_from_spec`` plan, a mechanical part - gets none of these issues.

Cost: the layer table is read first; a drawing with no candidate layer (every
drawing this server makes) is never snapshotted, so ``focus=None`` stays cheap.
A snapshot that did not cover the drawing is reported as one ``error`` issue
per focus with ``detail.code`` ``scan_incomplete``, never as a clean result.

Severities are this module's decision: a near miss is an ``error`` (a network
that looks connected and is not - a takeoff counts two runs), a dangling end a
``warning`` (it may be a real open end), a crossing ``info`` (often drawn on
purpose).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engineering.arch.layers import ARCH_LAYER_ROLES, ARCH_LAYERS
from engineering.layers import PID_LAYERS
from engineering.pid.lines import PID_LINE_LAYERS
from engineering.plan_spec import Issue
from engineering.qa_scan import shared_index
from engineering.understand.topology import (
    network_layers,
    topology_findings,
)
from engineering.understand.vocab import is_network_layer

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

__all__ = [
    "SEMANTIC_LAYERS",
    "TOPO_DISPATCH",
    "TOPO_FOCUSES",
    "build_index",
    "focus_layers",
    "issues_for",
]

TOPO_FOCUSES = ("topo_dangling_endpoint", "topo_near_miss", "topo_interior_crossing")
_KEY = "topo_index"

#: Layers the pid_* and arch_* focuses already read. Layer "0" is nobody's.
SEMANTIC_LAYERS: frozenset[str] = frozenset(
    {name for name, *_rest in PID_LAYERS}
    | set(PID_LINE_LAYERS)
    | {name for name, *_rest in ARCH_LAYERS}
    | set(ARCH_LAYER_ROLES.values())
) - {"0"}


def _is_candidate(name: str) -> bool:
    return name not in SEMANTIC_LAYERS and is_network_layer(name)


def focus_layers(snap) -> list[str]:
    """The network layers of ``snap`` no semantic focus covers."""
    return [row["layer"] for row in network_layers(snap) if row["layer"] not in SEMANTIC_LAYERS]


async def build_index(backend: AutoCADBackend, shared: dict | None = None) -> dict:
    """One read of the drawing for the three focuses:
    ``{"layers": [...], "findings": dict | None, "error": str | None}``.

    The drawing is the run's QA snapshot (``engineering/qa_scan.py``), so a
    finalize that already read it for the validator does not read it again.
    A layer table that cannot be read raises (``run_critique`` reports
    ``check_failed``); a snapshot that did not cover the drawing sets
    ``error`` and each focus reports ``scan_incomplete``.
    """
    from engineering.qa_scan import qa_snapshot

    names = [layer.name for layer in await backend.layer_list()]
    if not any(_is_candidate(name) for name in names):
        return {"layers": [], "findings": None, "error": None}
    snap, coverage = await qa_snapshot(backend, {} if shared is None else shared)
    if not coverage.complete:
        return {
            "layers": sorted(n for n in names if _is_candidate(n)),
            "findings": None,
            "error": coverage.reason,
        }
    layers = focus_layers(snap)
    if not layers:
        return {"layers": [], "findings": None, "error": None}
    return {"layers": layers, "findings": topology_findings(snap, layers=layers), "error": None}


def _dangling(findings: dict) -> list[Issue]:
    return [
        Issue(
            "warning",
            "topo_dangling_endpoint",
            f"{row['handle']} on layer {row['layer']} ends at ({row['at'][0]:.3f}, "
            f"{row['at'][1]:.3f}) touching nothing - no line, fitting or equipment is at that "
            "end. Connect it, or confirm it is a real open end.",
            [row["handle"]],
            {"layer": row["layer"], "end": row["end"], "at": row["at"], "space": row["space"]},
        )
        for row in findings["dangling"]
    ]


def _near_miss(findings: dict) -> list[Issue]:
    out = []
    for row in findings["near_miss"]:
        first, second = row["handles"]
        what = "the end of" if row["kind"] == "end_end" else "the body of"
        out.append(
            Issue(
                "error",
                "topo_near_miss",
                f"{first} on layer {row['layers'][0]} stops {row['gap']:.4g} short of {what} "
                f"{second} without joining it: the network reads as two pieces there. Extend or "
                "trim it onto the other line.",
                sorted({first, second}),
                {
                    "kind": row["kind"],
                    "gap": row["gap"],
                    "at": row["at"],
                    "to": row["to"],
                    "layers": row["layers"],
                    "space": row["space"],
                },
            )
        )
    return out


def _crossing(findings: dict) -> list[Issue]:
    return [
        Issue(
            "info",
            "topo_interior_crossing",
            f"{row['handles'][0]} and {row['handles'][1]} on layer {row['layer']} cross at "
            f"({row['at'][0]:.3f}, {row['at'][1]:.3f}) away from their ends with no junction. "
            "If they connect, split both at the crossing; if not, draw the crossing with a break.",
            sorted(set(row["handles"])),
            {"layer": row["layer"], "at": row["at"], "space": row["space"]},
        )
        for row in findings["crossing"]
    ]


_CHECKS = {
    "topo_dangling_endpoint": _dangling,
    "topo_near_miss": _near_miss,
    "topo_interior_crossing": _crossing,
}


def issues_for(focus: str, index: dict) -> list[Issue]:
    if index.get("error"):
        return [
            Issue(
                "error",
                focus,
                f"Topology not checked on {', '.join(index['layers'])}: the drawing could not be "
                f"read in full ({index['error']}).",
                [],
                {
                    "code": "scan_incomplete",
                    "reason": index["error"],
                    "layers": list(index["layers"]),
                },
            )
        ]
    findings = index.get("findings")
    if not findings:
        return []
    return _CHECKS[focus](findings)


def _make(focus: str):
    async def check(backend: AutoCADBackend, shared: dict) -> list[Issue]:
        async def build(b):
            return await build_index(b, shared)

        index = await shared_index(shared, _KEY, build, backend)
        return issues_for(focus, index)

    check.needs_shared = True  # type: ignore[attr-defined]
    check.__name__ = f"check_{focus}"
    return check


TOPO_DISPATCH = {focus: _make(focus) for focus in TOPO_FOCUSES}
