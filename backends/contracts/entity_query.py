"""Reading entities back.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import EntityInfo


class EntityQueryContract(ABC):
    # ── selection filters (M8 / F1) ──────────────────────────────────────────

    @abstractmethod
    async def selection_window(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        mode: str = "window",
        entity_type: str = "",
        layer: str = "",
    ) -> dict:
        """AutoCAD's ssget window/crossing. ``{ok, handles, count, mode}``.

        ``window`` returns only entities wholly inside the box; ``crossing``
        also returns the ones that straddle its edge. Getting those backwards
        is silent and wrong, so ``mode`` is reported back and defaults to the
        narrower one. A zero-area box is refused rather than answered with an
        empty list, which would read as "nothing there".

        Selection is by *drawn* position: an entity in a mirrored frame is
        found where ``entity_get`` reports it, not where the file stores it.
        """
        ...

    @abstractmethod
    async def selection_polygon(
        self,
        points: list[list[float]],
        mode: str = "window",
        entity_type: str = "",
        layer: str = "",
    ) -> dict:
        """Window/crossing selection against an arbitrary polygon.

        A polygon is not its bounding box — the difference is the whole point
        of the tool, so the implementation must test against the shape itself.
        """
        ...

    @abstractmethod
    async def selection_filter(
        self,
        entity_type: str = "",
        layer: str = "",
        color: int | None = None,
        linetype: str = "",
        min_area: float | None = None,
    ) -> dict:
        """Property filter. Returns ``{ok, handles, count, filtered_by}``.

        Named parameters rather than a query string, deliberately: ezdxf's
        query language answers an unknown attribute with an empty result, so a
        typo is indistinguishable from "no matches". Here a typo is a
        ``TypeError`` at the boundary, and ``filtered_by`` tells the caller
        which filters actually ran — "I filtered by layer and found none" and
        "I never filtered by layer" are different answers.
        """
        ...

    @abstractmethod
    async def entity_get(self, handle: str) -> EntityInfo: ...

    @abstractmethod
    async def entity_set_properties(
        self,
        handle: str,
        layer: str | None = None,
        color: int | None = None,
        linetype: str | None = None,
        lineweight: float | None = None,
        visible: bool | None = None,
    ) -> dict: ...

    @abstractmethod
    async def entity_edit_text(
        self,
        handle: str,
        text: str | None = None,
        height: float | None = None,
        rotation: float | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> EntityInfo:
        """Edit an existing TEXT or MTEXT entity in place. Any argument left
        None is unchanged. Returns the updated EntityInfo. Raises if the handle
        is not a TEXT/MTEXT entity.

        ``x`` / ``y`` (v1.8.0) move the label so its reported ``insertion``
        lands there (WCS): the whole entity is translated, so an aligned TEXT's
        alignment point moves with it and the handle and XDATA stay. A TEXT in a
        plane tilted out of WCS XY is refused with ``ocs_tilted_plane``."""
        ...

    @abstractmethod
    async def entity_edit_geometry(
        self,
        handle: str,
        cx: float | None = None,
        cy: float | None = None,
        radius: float | None = None,
        x1: float | None = None,
        y1: float | None = None,
        x2: float | None = None,
        y2: float | None = None,
        start_angle: float | None = None,
        end_angle: float | None = None,
        points: list[list[float]] | None = None,
        bulges: list[float] | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> EntityInfo:
        """Edit the defining geometry of an existing entity in place.
        - CIRCLE: cx / cy / radius
        - LINE: x1 / y1 (start), x2 / y2 (end)
        - ARC: cx / cy / radius / start_angle / end_angle (degrees)
        - LWPOLYLINE (v1.8.0): points / bulges, exactly ``entity_set_vertices``
        - TEXT / MTEXT (v1.8.0): x / y, exactly ``entity_edit_text(x=, y=)``
        Any argument left None is unchanged. Raises for unsupported types, and
        for an argument the entity's type does not take - a polyline handed
        ``cx`` or a circle handed ``points`` is refused, never ignored."""
        ...

    @abstractmethod
    async def entity_set_vertices(
        self,
        handle: str,
        points: list[list[float]],
        bulges: list[float] | None = None,
        closed: bool | None = None,
    ) -> EntityInfo:
        """Rewrite an LWPOLYLINE's vertices in place (v1.8.0, plan item 1.8-06).

        ``points`` are WCS ``[x, y]`` (at least two); ``bulges`` one per vertex,
        counter-clockwise positive in WCS - the spelling ``entity_get`` reports.
        Omitted bulges keep the polyline's own when the vertex count is
        unchanged and are straight otherwise; ``closed=None`` keeps the flag.
        The vertex count may change. The handle, layer and XDATA are kept.

        A mirrored polyline (extrusion -Z) is written in its own frame; one in
        a plane tilted out of WCS XY is refused with ``ocs_tilted_plane``.
        Anything else than an LWPOLYLINE raises. Validation happens before the
        first write (``backends/vertices.py``), so a refusal changes nothing.

        Live: ``Coordinates`` + ``SetBulge`` + ``Closed``. If AutoCAD refuses or
        ignores a ``Coordinates`` write with a different vertex count (S4,
        unmeasured), the polyline is recreated in its owner with its layer,
        colour, linetype, linetype scale, lineweight and XDATA and the old one
        deleted; the result then carries ``properties["replaced"] =
        [{"old", "new"}]`` and the new handle - decided at runtime, so the
        contract holds whichever way the seat answers.
        """
        ...

    @abstractmethod
    async def entity_list(
        self,
        type_filter: str | None = None,
        layer_filter: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[EntityInfo]: ...

    @abstractmethod
    async def entity_count(
        self,
        type_filter: str | None = None,
        layer_filter: str | None = None,
    ) -> int:
        """How many entities ``entity_list`` would match, ignoring limit/offset.

        Exists so a *paged* listing can report an honest total without paying
        for the rows it is not going to return. ``entity_list`` stops building
        ``EntityInfo`` objects the moment it has ``limit`` of them, so it can
        never tell a caller whether anything followed the page — and a
        truncated list read as complete is a correctness bug, not a token one.

        Two obligations, both load-bearing:

        * apply **exactly** the filter semantics of ``entity_list`` (same
          case-insensitive type/layer comparison), or ``truncated`` would be
          computed against a different set than the one that was paged;
        * do not build ``EntityInfo`` rows. A count implemented by listing
          would cost more than the listing it is meant to make cheap.
        """
        ...

    # ── extended entity data (XDATA) ─────────────────────────────────────────

    @abstractmethod
    async def entity_get_xdata(self, handle: str, app_name: str | None = None) -> dict:
        """Extended entity data as ``{"handle", "xdata": {app: [values]}, "backend"}``.
        ``app_name=None`` returns every app; an absent app is ``{}``, not an error."""
        ...

    @abstractmethod
    async def entity_set_xdata(self, handle: str, app_name: str, values: list) -> dict:
        """Replace the app's XDATA (registering the APPID if needed). Values are typed
        by ``backends/xdata_specs.py``; an empty list removes the app's XDATA."""
        ...
