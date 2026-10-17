"""Moving, copying and editing geometry.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import EntityInfo


class EntityModificationContract(ABC):
    @abstractmethod
    async def entity_move(self, handle: str, dx: float, dy: float, dz: float = 0.0) -> dict: ...

    @abstractmethod
    async def entity_copy(
        self, handle: str, dx: float, dy: float, dz: float = 0.0
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_rotate(
        self,
        handle: str,
        base_x: float,
        base_y: float,
        angle_deg: float,
    ) -> dict: ...

    @abstractmethod
    async def entity_scale(
        self,
        handle: str,
        base_x: float,
        base_y: float,
        factor: float,
    ) -> dict: ...

    @abstractmethod
    async def entity_mirror(
        self,
        handle: str,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        delete_original: bool = False,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_offset(
        self,
        handle: str,
        distance: float,
        side_x: float | None = None,
        side_y: float | None = None,
    ) -> EntityInfo: ...

    @abstractmethod
    async def entity_delete(self, handle: str) -> dict: ...

    @abstractmethod
    async def entity_array_rectangular(
        self,
        handle: str,
        rows: int,
        cols: int,
        row_spacing: float,
        col_spacing: float,
    ) -> list[EntityInfo]: ...

    @abstractmethod
    async def entity_array_polar(
        self,
        handle: str,
        count: int,
        fill_angle: float,
        center_x: float,
        center_y: float,
    ) -> list[EntityInfo]: ...

    # ── text annotation (M8 / F15) ───────────────────────────────────────────

    @abstractmethod
    async def text_set_background(
        self,
        handle: str,
        enabled: bool = True,
        color: int | None = None,
        scale: float = 1.5,
    ) -> dict:
        """Put an opaque background box behind an MTEXT. ``{ok, handle, enabled}``.

        MTEXT only — TEXT has no background-fill attribute, so setting one on
        it would report success and change nothing. ``scale`` is a multiple of
        the text box and must be >= 1: a box smaller than its text is a stripe
        through the text, not a mask.
        """
        ...

    @abstractmethod
    async def text_find_replace(
        self,
        find: str,
        replace: str,
        layer: str | None = None,
        match_case: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """Replace text across the drawing.

        Returns ``{ok, replaced, entities, searched_types, note, dry_run}``.
        ``searched_types`` is reported rather than assumed: "no matches" and
        "that type was never searched" are different answers, and an editor
        that silently skips block attributes is the classic version of this
        bug. DIMENSION text is deliberately out of scope — its ``text`` field
        holds the ``<>`` override placeholder rather than the measurement, so
        editing it would either do nothing or break the association.
        """
        ...

    @abstractmethod
    async def entity_set_true_color(self, handle: str, rgb: tuple[int, int, int]) -> dict:
        """Give an entity a 24-bit colour. ``{ok, handle, true_color: "#RRGGBB"}``.

        ACI has 255 colours and no dark warm grey, so a Pantone swatch such as
        PMS 426 C (#25282A) printed as ACI 250 — visibly lighter (report of
        2026-10-18). The true colour overrides the ACI number on every engine,
        as in AutoCAD; setting an ACI ``color`` afterwards removes it again.
        """
        ...


def parse_true_color(value) -> tuple[int, int, int]:
    """``"#25282A"`` / ``"25282A"`` / ``"37,40,42"`` / ``[37, 40, 42]`` → ``(37, 40, 42)``.

    Raises ``ValueError`` naming the accepted spellings for anything else; every
    channel must be an integer 0-255.
    """
    rgb: list[int]
    if isinstance(value, str):
        text = value.strip()
        hex_text = text[1:] if text.startswith("#") else text
        if len(hex_text) == 6 and all(c in "0123456789abcdefABCDEF" for c in hex_text):
            rgb = [int(hex_text[i : i + 2], 16) for i in (0, 2, 4)]
        else:
            try:
                rgb = [int(part.strip()) for part in text.split(",")]
            except ValueError:
                rgb = []
    elif isinstance(value, (list, tuple)):
        try:
            rgb = [int(v) for v in value]
        except (TypeError, ValueError):
            rgb = []
        if any(isinstance(v, float) and not float(v).is_integer() for v in value):
            rgb = []
    else:
        rgb = []
    if len(rgb) != 3 or any(not 0 <= c <= 255 for c in rgb):
        raise ValueError(
            f"true_color: {value!r} is not a colour; use '#RRGGBB', 'R,G,B' or [R, G, B] "
            "with each channel 0-255"
        )
    return rgb[0], rgb[1], rgb[2]


def true_color_hex(rgb) -> str:
    """``(37, 40, 42)`` → ``"#25282A"``."""
    r, g, b = (int(c) for c in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"
