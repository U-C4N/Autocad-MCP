"""Measurement and analysis.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from backends.capability import capability

if TYPE_CHECKING:
    from backends.base import EntityInfo


class AnalysisContract(ABC):
    # ── boundary tracing (M8 / F3) ───────────────────────────────────────────

    @capability(  # noqa: B027 — @capability supplies the body
        "boundary_trace",
        reason=(
            "ActiveX exposes no BOUNDARY member; the live path would have to drive the "
            "-BOUNDARY command blind, which has not been verified against a live AutoCAD. "
            "Use the headless backend (AUTOCAD_MCP_BACKEND=ezdxf), or run BOUNDARY in "
            "AutoCAD directly."
        ),
    )
    async def boundary_trace(
        self, x: float, y: float, layer: str | None = None, tolerance: float = 1e-9
    ) -> dict:
        """AutoCAD's BOUNDARY/BPOLY: trace the closed loop enclosing a seed point.

        Returns ``{ok, handle, vertices, area, closed, source_handles}``.
        ``vertices`` is a count, ``area`` is unsigned.

        Implementations must not enumerate every loop in the drawing:
        ``edgeminer.find_all_loops`` is O(n!) and was measured at 63.9 s for 40
        edges and a timeout at 60. The seed point picks a starting edge and one
        loop is walked from it.

        A seed with no enclosing loop is refused. Returning an empty boundary
        would be a boundary that bounds nothing, reported as success.
        """
        ...

    @capability(  # noqa: B027 — @capability supplies the body
        "boundary_trace",
        reason=(
            "Chaining arbitrary entities into a loop needs the same edge graph BOUNDARY "
            "does, which ActiveX does not expose. Use the headless backend "
            "(AUTOCAD_MCP_BACKEND=ezdxf)."
        ),
    )
    async def boundary_from_entities(self, handles: list[str], tolerance: float = 1e-9) -> dict:
        """Chain the given entities into one closed polyline.

        Returns ``{ok, handle, vertices, area, closed}``. Refuses a chain that
        does not close, naming the coordinates of the endpoint that dangles —
        "these do not form a loop" is not actionable without the gap.
        """
        ...

    @abstractmethod
    async def analysis_list_properties(self, handle: str) -> dict:
        """AutoCAD's LIST: the full DXF attribute set for one handle.

        Returns ``{ok, handle, type, layer, properties, dxf_attributes}``.
        ``properties`` mirrors what ``entity_get`` reports; ``dxf_attributes``
        is the raw attribute set that ``entity_get`` deliberately does not
        carry.

        Two things the raw dump cannot be handed over as-is. It holds ``Vec3``
        values, so it is not JSON-serialisable and would die at the wire
        boundary; and its coordinates are stored in the entity's own frame, so
        for anything with an extrusion they would contradict every other
        coordinate this server reports. Both are converted here, and
        ``extrusion`` stays in the dump so the original frame is still visible.
        """
        ...

    @abstractmethod
    async def analysis_stats(self) -> dict: ...

    @abstractmethod
    async def analysis_entities_in_region(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
    ) -> list[EntityInfo]: ...

    @abstractmethod
    async def analysis_measure_distance(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
    ) -> float: ...

    @abstractmethod
    async def analysis_measure_area(self, points: list[list[float]]) -> float: ...

    @abstractmethod
    async def entity_measure(self, handle: str, flatten_tolerance: float = 0.001) -> dict:
        """Area and perimeter of ONE existing entity, addressed by handle.

        Returns ``{"handle", "type", "area", "perimeter", "closed",
        "assumed_closed", "method", "exact", "flatten_tolerance", "backend",
        "self_intersecting", "perimeter_exact", "loop_count"}``.

        A HATCH is measured as the area it *fills* — outer loops minus the
        islands inside them — and the headless engine adds ``hatch_style``
        (``normal``/``outer``/``ignore``) so the number can be read against the
        drawing's own island setting. The live backend does not carry that key:
        it reads ActiveX ``.Area``, which **does** apply the island style —
        verified on a live AutoCAD 2026 (2026-08-06): a hatch of r20 with an
        r10 island returns 942.478, which is pi*(400-100).

        ``perimeter`` is ``None`` where ActiveX exposes no length member for the
        type, and ``perimeter_exact`` is then False. The member differs per
        type and was measured rather than assumed: LWPOLYLINE ``.Length``,
        CIRCLE ``.Circumference``, REGION ``.Perimeter``, HATCH none. A
        3DSOLID has no ``.Area`` at all — only ``.Volume`` — and is refused by
        name.
        A missing perimeter is not a reason to discard an area that was read
        successfully — doing exactly that is what made this method raise on
        every ACIS type it advertised.

        ``exact`` and
        ``flatten_tolerance`` are the accuracy disclosure: when one engine can
        answer in closed form and the other has to flatten, the payload says so
        rather than leaving two numbers to quietly disagree.

        Two different refusals, and the difference matters. A type that bounds no
        area at all (LINE, TEXT, a 3D polyline) raises a plain ``RuntimeError``:
        no engine can measure it, so tagging a capability would falsely suggest
        switching backends helps. A type this *engine* cannot evaluate (ACIS
        solids headlessly) raises ``UnsupportedCapabilityError``, which names the
        engine that can.
        """
        ...

    @abstractmethod
    async def analysis_bounding_box(self) -> dict: ...

    @abstractmethod
    async def analysis_select_by_layer(self, layer_name: str) -> list[EntityInfo]: ...

    @abstractmethod
    async def analysis_select_by_type(self, entity_type: str) -> list[EntityInfo]: ...

    @abstractmethod
    async def selection_get(self) -> dict:
        """Read the live viewport's implied ("pickfirst") selection set.

        COM-only / meaningful only with live AutoCAD. Returns a dict::

            {
                "ok": bool,                # True on COM even for an empty pick
                "count": int,
                "handles": list[str],      # entity handles to act on
                "entities": list[EntityInfo],  # _dc()-converted by the server
                "pickfirst": bool | None,  # state of the PICKFIRST sysvar
                "message" / "error": str,  # optional guidance / failure reason
            }

        The ezdxf headless backend has no viewport, so it returns ``ok=False``
        with an empty ``handles`` list (same shape, never raises).
        """
        ...
