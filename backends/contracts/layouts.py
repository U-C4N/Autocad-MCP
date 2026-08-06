"""Layouts, viewports and change-space.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from backends.capability import capability


class LayoutContract(ABC):
    @abstractmethod
    async def layout_list(self) -> dict:
        """Return {ok, layouts: [names], current: name} incl. 'Model'."""
        ...

    @abstractmethod
    async def layout_create(self, name: str) -> dict:
        """Create a new paper-space layout."""
        ...

    @abstractmethod
    async def layout_set_current(self, name: str) -> dict:
        """Activate a layout tab ('Model' or a paper-space layout name)."""
        ...

    @abstractmethod
    async def layout_delete(self, name: str) -> dict:
        """Delete a paper-space layout and everything drawn on it.

        Returns ``{ok, deleted, entities_destroyed, current}``. ``current`` is
        the space geometry goes into afterwards, which the implementation must
        keep consistent with ``layout_list()`` — deleting the active tab
        otherwise leaves the reported current layout, the DXF tile mode and the
        space entities actually land in disagreeing with each other.

        Refuses ``'Model'``, a blank name (which ezdxf resolves to the *first*
        paper-space layout), a name that does not exist, and the last remaining
        paper-space layout.
        """
        ...

    @abstractmethod
    async def layout_rename(self, old_name: str, new_name: str) -> dict:
        """Rename a paper-space layout, keeping its entities and their handles.

        Returns ``{ok, old, new, current}``. Refuses ``'Model'`` on either side,
        a blank or otherwise invalid new name, and a name already in use.
        """
        ...

    @abstractmethod
    async def layout_copy(self, source: str, new_name: str) -> dict:
        """Copy a paper-space layout, its page setup and its geometry.

        Returns ``{ok, source, layout, entities_copied, skipped,
        associativity_remapped, associativity_dropped}``. ``skipped`` names the
        DXF types that could not be cloned — a copy missing geometry must never
        report a bare success. Associative boundaries (hatches) are re-pointed
        at the cloned entities where possible and cleared otherwise, because a
        boundary still referencing the source layout dangles as soon as that
        layout is deleted and no auditor reports it.
        """
        ...

    @abstractmethod
    async def viewport_create(
        self,
        layout: str,
        center_x: float,
        center_y: float,
        width: float,
        height: float,
        view_center_x: float,
        view_center_y: float,
        scale: float = 1.0,
    ) -> dict:
        """Place a scaled model-space viewport on a paper-space layout.

        ``scale`` is paper:model (1.0 = 1:1, 0.5 = 1:2). The viewport shows
        the model window centered at (view_center_x, view_center_y) with
        view height = height / scale.
        """
        ...

    @abstractmethod
    async def viewport_list(self, layout: str | None = None) -> dict:
        """List the viewports on one paper-space layout, or on all of them.

        Returns ``{ok, viewports: [...], count}``. Each row carries ``handle``,
        ``layout``, ``center``, ``width``, ``height``, ``view_center``,
        ``view_height``, ``scale``, ``locked``, ``status``, ``id`` and
        ``is_main``.

        The layout's own main viewport is reported with ``is_main: True`` rather
        than filtered out — it is what remains after deleting every drafting
        viewport, and a caller who cannot see it cannot explain that. ``scale``
        and ``locked`` are ``None`` on documents that cannot store them (R12),
        never a fabricated number. Viewports are addressed by handle; ``id`` is
        metadata only.
        """
        ...

    @abstractmethod
    async def viewport_set_scale(self, handle: str, scale: float) -> dict:
        """Set a viewport's paper:model scale. Returns ``{ok, handle, scale,
        view_height}`` with ``scale`` read back from the entity.

        Refuses the main viewport (whose view height is the tab's own pan/zoom
        state, not a drafting scale) and documents that cannot persist it.
        Geometric scale only: annotative text and dimensions do not resize.
        """
        ...

    @abstractmethod
    async def viewport_lock(self, handle: str, locked: bool = True) -> dict:
        """Lock or unlock a viewport's display scale. Returns ``{ok, handle,
        locked}`` with ``locked`` read back from the entity."""
        ...

    # B027 (empty method, no @abstractmethod) is exactly what this pattern
    # looks like from the outside: the decorator replaces the `...` body with
    # the refusing default, so the source really is empty on purpose.
    @capability(  # noqa: B027
        "chspace",
        reason=(
            "ActiveX exposes no change-space member, so a live backend would have to recreate "
            "geometry through CopyObjects or drive the CHSPACE command blind — neither has been "
            "verified against a live AutoCAD. Use the headless backend "
            "(AUTOCAD_MCP_BACKEND=ezdxf), or run CHSPACE in AutoCAD directly."
        ),
    )
    async def entity_change_space(
        self,
        handles: list[str],
        viewport_handle: str,
        direction: str = "to_paper",
        freeze_dimensions: bool = False,
    ) -> dict:
        """Move entities between model space and a sheet, *through* a viewport.

        Returns ``{ok, moved, refused, scale, viewport}``. Each ``moved`` row
        carries ``handle``, ``from``, ``to``, ``inside_viewport`` and
        ``on_sheet``; each ``refused`` row carries ``handle`` and ``reason``.

        The geometric transform is the whole point: a move without it leaves a
        100 mm feature as 100 mm of paper inside a 1:2 viewport, produces a file
        that audits clean, and is indistinguishable from success. Backends that
        cannot apply the viewport transform must refuse rather than move.

        Refuses per entity for dimensions (unless ``freeze_dimensions`` bakes
        the pre-transform measurement into the text first), ACIS bodies, tables
        and proxies, viewports, and entities already in the target space; and
        refuses the whole call for a twisted or non-top-view viewport.
        """
        ...

    @abstractmethod
    async def viewport_delete(self, handle: str, force: bool = False) -> dict:
        """Delete a viewport. Returns ``{ok, handle, layout, was_main}``.

        The layout's main viewport needs ``force=True`` and the implementation
        must repair the layout's current-viewport pointer afterwards; a dangling
        pointer is invisible to every headless audit and shows up only in CAD.
        """
        ...
