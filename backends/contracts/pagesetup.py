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
        margins_mm: [top, bottom, left, right], paper_units, center}``.
        ``size_mm`` and ``margins_mm`` are the sheet *as plotted*: under
        ``plot_rotation`` 1/3 (AutoCAD's landscape-on-portrait-media
        convention) both are turned together, through the one mapping the
        renderer plots by (``papers.turned_sheet``), so the printable area a
        row implies is the area the same engine plots. ``paper`` is the
        catalogue name when the size matches one within 0.5 mm, otherwise the
        media name as stored. ``paper_units`` (``"mm"`` | ``"inches"`` |
        ``"pixels"``) is what one paper-space unit means at the plot scale;
        sizes and margins are millimetres whatever it says. ``center`` is a
        bool for an extents plot and ``None`` for a layout plot, where
        centring is not applicable (AutoCAD greys it out; ActiveX refuses
        ``CenterPlot = True`` under ``acLayout``). Raises ``ValueError`` for
        an unknown layout.
        ``Model`` is never listed: model space is plotted through
        ``drawing_export_pdf(layout=None)`` and carries no sheet.
        """
        ...

    @abstractmethod
    async def page_setup_apply(self, layout: str, setup: dict) -> dict:
        """Write a page setup resolved by
        ``engineering.standards.papers.resolve_page_setup`` to a layout.

        Returns ``{ok, layout, applied: {...setup}, changed: {key: [old, new]},
        warnings: [str], plot_style_known, viewports_kept}``. ``changed`` is
        computed from the read-back before and after (the rows
        ``page_setup_list`` reports), so re-applying the same setup reports
        ``{}``. The write resets ``plot_rotation`` to 0 (orientation rides on
        the media's width/height); headlessly, with no ``margins_mm`` asked
        for, the turned margins are re-written un-turned so the sheet keeps
        the margins it had, and on the live engine (margins come from the
        .pc3) the ones that moved show in ``changed``. ``setup["paper_units"]``
        (``"mm"`` default, ``"inches"``, or ``None`` to keep the layout's
        units as AutoCAD's own Page Setup does) is written when given; a move
        rescales every paper-space unit by 25.4 at the plot scale and is named
        in ``changed`` *and* ``warnings`` (``papers.page_setup_warnings``).
        Refuses ``Model``, an unknown layout, and a ``setup`` missing a
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
