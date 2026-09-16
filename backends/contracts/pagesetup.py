"""Page setup, plot styles and templates (v1.6 track E, group P).

``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class PageSetupContract(ABC):
    @abstractmethod
    async def page_setup_list(self, layout: str | None = None) -> list[dict]:
        """Page setup of one paper-space layout, or of every one.

        Rows: ``{layout, paper, canonical_media_name, size_mm: [w, h],
        orientation, plot_style, scale, plot_area, device,
        margins_mm: [top, bottom, left, right], center}``. ``paper`` is the
        catalogue name when the size matches one within 0.5 mm, otherwise the
        media name as stored. ``center`` is a bool for an extents plot and
        ``None`` for a layout plot, where centring is not applicable (AutoCAD
        greys it out; ActiveX refuses ``CenterPlot = True`` under
        ``acLayout``). Raises ``ValueError`` for an unknown layout.
        ``Model`` is never listed: model space is plotted through
        ``drawing_export_pdf(layout=None)`` and carries no sheet.
        """
        ...

    @abstractmethod
    async def page_setup_apply(self, layout: str, setup: dict) -> dict:
        """Write a page setup resolved by
        ``engineering.standards.papers.resolve_page_setup`` to a layout.

        Returns ``{ok, layout, applied: {...setup}, changed: {key: [old, new]},
        plot_style_known, viewports_kept}``. ``changed`` is computed from the
        read-back before and after, so re-applying the same setup reports
        ``{}``. Refuses ``Model``, an unknown layout, and a ``setup`` missing a
        resolved key, before anything is written. A write the engine refuses
        part-way through is unwound (every landed value put back) before the
        ``ValueError`` names the refused property. Never deletes viewports.
        """
        ...

    @abstractmethod
    async def plot_style_list(self) -> list[dict]:
        """``[{name, source: "catalog"|"installed", installed: bool|None}]`` —
        the authored ctb catalogue plus, on a live engine, the files present
        in the plot style search path. ``installed`` is ``None`` where no
        installation can be scanned."""
        ...

    @abstractmethod
    async def drawing_template_save(
        self, path: str, name: str | None = None, description: str | None = None
    ) -> dict:
        """Save the current drawing as a template.

        ``.dxf`` writes on both engines; ``.dwt`` writes on the live engine
        (``SaveAs(path, ac2018_Template)``) and raises
        ``UnsupportedCapabilityError("dwt_write")`` headlessly. Returns
        ``{ok, path, format: "dxf"|"dwt", backend, name, description_written}``.
        """
        ...
