"""Text style presets: the four fonts the standards data refers to by name.

``TEXT_PRESETS`` maps a style name to ``(font file, width factor, oblique
degrees)``. ISOCP is AutoCAD's ISO 3098 type B lettering as an SHX shape
file; ISOCPEUR is the TrueType twin with accented characters; ARIAL is the
general-purpose TrueType face; ROMANS is AutoCAD's simplex roman, the face
ASME Y14.2 Gothic lettering is usually drawn with. A DXF stores only the font
*name*, so an unknown file is never refused here: ``resolve_font`` says
whether it is a bundled preset, the backend adds whether its font path has
it, and the caller sees ``font_resolved``.
"""

from __future__ import annotations

from pathlib import PurePath

__all__ = [
    "MAX_OBLIQUE_DEG",
    "TEXT_PRESETS",
    "resolve_font",
    "validate_textstyle",
]

TEXT_PRESETS: dict[str, tuple[str, float, float]] = {
    "ISOCP": ("isocp.shx", 1.0, 0.0),
    "ISOCPEUR": ("isocpeur.ttf", 1.0, 0.0),
    "ARIAL": ("arial.ttf", 1.0, 0.0),
    "ROMANS": ("romans.shx", 1.0, 0.0),
}

#: AutoCAD's own limit for the STYLE command's obliquing angle.
MAX_OBLIQUE_DEG = 85.0

_ILLEGAL_NAME_CHARS = frozenset('<>/\\":;?*|,=`')


def resolve_font(font: str) -> tuple[str, bool]:
    """``(font_file, known)``: a preset name or file resolves to the bundled
    file with ``known=True``; anything else is passed through with ``False``.

    Accepts the style name (``ISOCP``), the file (``isocp.shx``) or the stem
    (``isocp``), case-insensitively. Refuses an empty string or control
    characters — the only inputs a DXF cannot hold.
    """
    if not isinstance(font, str):
        raise TypeError(f"font: expected a string, got {type(font).__name__}")
    text = font.strip()
    if not text:
        raise ValueError("font: a font file name cannot be empty")
    if any(ord(ch) < 32 for ch in text):
        raise ValueError("font: control characters are refused")
    upper = text.upper()
    if upper in TEXT_PRESETS:
        return TEXT_PRESETS[upper][0], True
    for font_file, _width, _oblique in TEXT_PRESETS.values():
        if upper in (font_file.upper(), PurePath(font_file).stem.upper()):
            return font_file, True
    return text, False


def validate_textstyle(
    name: str,
    font: str,
    height: float = 0.0,
    width_factor: float = 1.0,
    oblique_deg: float = 0.0,
) -> dict:
    """The typed request both engines write, or a ``ValueError``/``TypeError``
    naming the field. ``height`` 0 is AutoCAD's "prompt per text" and is legal;
    the width factor must be positive; the oblique angle is AutoCAD's ±85°."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name: a text style name cannot be empty")
    clean = name.strip()
    bad = sorted(set(clean) & _ILLEGAL_NAME_CHARS)
    if bad:
        raise ValueError(f"name: {clean!r} contains characters DXF forbids in a name: {bad}")
    font_file, known = resolve_font(font)
    for key, value in (
        ("height", height),
        ("width_factor", width_factor),
        ("oblique_deg", oblique_deg),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{key}: expected a number, got {type(value).__name__}")
    if float(height) < 0.0:
        raise ValueError(f"height: {height!r} is negative; 0 means 'ask per text'")
    if float(width_factor) <= 0.0:
        raise ValueError(f"width_factor: {width_factor!r} must be greater than 0")
    if abs(float(oblique_deg)) > MAX_OBLIQUE_DEG:
        raise ValueError(
            f"oblique_deg: {oblique_deg!r} is outside AutoCAD's +/-{MAX_OBLIQUE_DEG:g} degrees"
        )
    return {
        "name": clean,
        "font_file": font_file,
        "known": known,
        "height": float(height),
        "width_factor": float(width_factor),
        "oblique_deg": float(oblique_deg),
    }
