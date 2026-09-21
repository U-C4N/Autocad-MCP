"""Dimension, text and multileader styles (track E, group S).

``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.

Names are matched case-insensitively — AutoCAD's rule for every symbol
table — and reported in the table's own spelling. Every refusal is raised
before a table is touched: ``ValueError`` naming the key (a missing style, a
clash, a variable outside ``engineering.standards.dimstyles.DIM_VARIABLE_RANGES``),
``TypeError`` for a wrong type.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class StylesContract(ABC):
    @abstractmethod
    async def dimstyle_list(self) -> list[dict]:
        """Every DIMSTYLE entry: ``[{name, current, values, values_available}]``.

        ``values`` holds the seventeen preset variables
        (``engineering.standards.dimstyles.PRESET_VARIABLES``) with ``DIMDSEP``
        as the character. Headless every row carries them (the DXF's stored
        value, else the schema default AutoCAD applies to an omitted code).
        On the live engine ActiveX has no per-style getters — ``AcadDimStyle``
        exposes ``Name`` and ``CopyFrom`` only — so ``values`` is read for the
        *current* style and the other rows carry ``values: None`` with
        ``values_available: False`` rather than cycling the active style
        behind the operator's back.
        """
        ...

    @abstractmethod
    async def dimstyle_create(self, name: str, values: dict, set_current: bool = False) -> dict:
        """Create a dimension style from a fully resolved ``values`` dict
        (``engineering.standards.dimstyles.resolve_dimstyle(preset, overrides)``).

        Returns ``{ok, name, values, written, current, textstyle_created}``.
        ``DIMTXSTY`` must name a text style that exists or a bundled preset
        (ISOCP / ISOCPEUR / ARIAL / ROMANS), which is created first and
        reported in ``textstyle_created``; anything else is refused by name.
        A name clash is refused (``dimstyle_modify`` changes an existing
        style). With ``set_current`` the style becomes ``$DIMSTYLE`` *and* its
        values become the current ``DIM*`` settings — what a live seat does —
        so the next dimension carries them on both engines.
        """
        ...

    @abstractmethod
    async def dimstyle_modify(self, name: str, values: dict) -> dict:
        """Change variables on an existing style.

        Returns ``{ok, name, changed: {VAR: [old, new]}, dimensions_using_style,
        rerender_required}``. ``changed`` names only the variables that moved
        (the ``hatch_edit`` rule: re-setting a value to itself is not a
        change). Dimensions already drawn with the style are listed by handle;
        ``rerender_required`` is ``True`` headlessly when any exist and
        something changed, because a headless DIMENSION carries its rendered
        block and shows the old style until it is redrawn — AutoCAD re-renders
        on the next regen, so the live engine reports ``False``. Modifying the
        current style also updates the ``DIM*`` current settings.
        """
        ...

    @abstractmethod
    async def dimstyle_set_current(self, name: str) -> dict:
        """Make ``name`` the current dimension style.

        Returns ``{ok, current, previous, changed}``. Sets ``$DIMSTYLE`` and
        loads the style's stored variables into the current ``DIM*`` settings
        (AutoCAD's ``-DIMSTYLE Restore``; on the live engine unsaved overrides
        of the previous style are discarded, which is AutoCAD's own rule).
        The dimension tools then create dimensions with it; the per-dimension
        override path (tolerances, fits, text override) is unchanged.
        """
        ...

    @abstractmethod
    async def textstyle_list(self) -> list[dict]:
        """``[{name, font, height, width_factor, oblique_deg, current}]``."""
        ...

    @abstractmethod
    async def textstyle_create(
        self,
        name: str,
        font: str,
        height: float = 0.0,
        width_factor: float = 1.0,
        oblique_deg: float = 0.0,
        set_current: bool = False,
    ) -> dict:
        """Create a text style. Returns ``{ok, name, font, font_resolved, current}``.

        ``font`` is a preset (``ISOCP`` / ``isocp.shx`` / ``isocp``, any case)
        or any file name. A file that is neither a preset nor found on the
        engine's font path is *written* and reported ``font_resolved: False``
        — a DXF stores only the name and AutoCAD substitutes at open — never
        refused. Refuses a clash, ``width_factor <= 0``, ``height < 0``, an
        oblique angle outside ±85°, an empty font.
        """
        ...

    @abstractmethod
    async def textstyle_set_current(self, name: str) -> dict:
        """``$TEXTSTYLE``. Returns ``{ok, current, previous, changed}``; new
        TEXT/MTEXT then carry the style on both engines."""
        ...

    @abstractmethod
    async def mleaderstyle_list(self) -> list[dict]:
        """``[{name, arrow_size, landing_gap, text_style, text_height, values_available}]``.

        Both engines report the four values (``values_available: True``).
        Headless they are read from the MLEADERSTYLE object (schema defaults
        for an unset field); live from the ``AcadMLeaderStyle`` objects the
        ``ACAD_MLEADERSTYLE`` dictionary holds (``ArrowSize`` / ``LandingGap``
        / ``TextHeight`` in drawing units, ``TextStyle`` a name) — ActiveX
        exposes no *collection* property for them, but the dictionary items
        are full ``IAcadMLeaderStyle`` objects.
        """
        ...

    @abstractmethod
    async def mleaderstyle_create(self, name: str, values: dict) -> dict:
        """Create a multileader style from a resolved ``values`` dict
        (``engineering.standards.mleaderstyles.resolve_mleaderstyle``).
        Both engines: ezdxf on ``doc.mleader_styles``; live through
        ``Dictionaries.Item("ACAD_MLEADERSTYLE").AddObject(name,
        "AcDbMLeaderStyle")`` and the object's property writes. Returns
        ``{ok, name, values, textstyle_created}``; the text style follows the
        ``dimstyle_create`` rule (exists or preset, else refused by name); a
        clash is refused — before anything is written."""
        ...
