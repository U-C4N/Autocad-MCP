"""Paper sizes, plot-style catalogue and plot scales — authored data.

Sources: ISO 216 (A series), ASME/ANSI Y14.1 (A–E, metric millimetre values as
AutoCAD's ``ANSI_*`` media measure, inch values as the media are *named*),
the ctb files AutoCAD ships in its Plot Styles folder, and the standard-scale
tables of two enums that do not agree with each other:

* DXF group code 75 (``standard_scale_type``, what ezdxf writes): 16=1:1,
  17=1:2, 20=1:10, 22=1:20, 25=1:50, 26=1:100, 27=2:1, 30=10:1 — no 1:5.
* ActiveX ``AcPlotScale`` (``Layout.StandardScale``, AutoCAD 2026 typelib):
  ac1_1=16, ac1_2=17, ac1_5=19, ac1_10=21, ac1_20=23, ac1_50=26, ac1_100=27,
  ac2_1=28, ac10_1=31, acScaleToFit=0.

A scale without a code on an engine goes through the custom numerator /
denominator on that engine, and the read-back reports the same label either way.
"""

from __future__ import annotations

import math
import re

__all__ = [
    "ACTIVEX_PLOT_SCALE",
    "CTB_CATALOG",
    "DXF_STANDARD_SCALE_TYPE",
    "ORIENTATIONS",
    "PAPER_INCHES",
    "PAPER_SIZES",
    "PLOT_AREAS",
    "PLOT_TYPE_NAMES",
    "REQUIRED_SETUP_KEYS",
    "SCALES",
    "activex_scale_label",
    "canonical_media_name",
    "dxf_scale_label",
    "paper_from_size",
    "paper_size_mm",
    "plot_style_known",
    "require_page_setup",
    "resolve_page_setup",
    "scale_label",
    "scale_ratio",
]

#: Portrait width × height in millimetres. Landscape swaps.
PAPER_SIZES: dict[str, tuple[int, int]] = {
    "ISO_A0": (841, 1189),
    "ISO_A1": (594, 841),
    "ISO_A2": (420, 594),
    "ISO_A3": (297, 420),
    "ISO_A4": (210, 297),
    "ANSI_A": (216, 279),
    "ANSI_B": (279, 432),
    "ANSI_C": (432, 559),
    "ANSI_D": (559, 864),
    "ANSI_E": (864, 1118),
}

#: ANSI media are *named* in inches by AutoCAD's DWG To PDF.pc3
#: (``ANSI_B_(17.00_x_11.00_Inches)``); the millimetre table above is what the
#: sheet measures. Portrait width × height.
PAPER_INCHES: dict[str, tuple[float, float]] = {
    "ANSI_A": (8.5, 11.0),
    "ANSI_B": (11.0, 17.0),
    "ANSI_C": (17.0, 22.0),
    "ANSI_D": (22.0, 34.0),
    "ANSI_E": (34.0, 44.0),
}

#: Spellings resolve_page_setup accepts, mapped to the canonical key.
_PAPER_ALIASES: dict[str, str] = {
    **{name.lower(): name for name in PAPER_SIZES},
    **{name.split("_", 1)[1].lower(): name for name in PAPER_SIZES if name.startswith("ISO_")},
    **{f"ansi {name[-1].lower()}": name for name in PAPER_SIZES if name.startswith("ANSI_")},
    **{f"ansi-{name[-1].lower()}": name for name in PAPER_SIZES if name.startswith("ANSI_")},
}

#: Plot style tables AutoCAD ships (``<install>/…/Plot Styles``).
CTB_CATALOG: tuple[str, ...] = (
    "monochrome.ctb",
    "acad.ctb",
    "Grayscale.ctb",
    "Screening 100%.ctb",
    "Screening 75%.ctb",
    "Screening 50%.ctb",
    "Screening 25%.ctb",
    "Fill Patterns.ctb",
    "DWF Virtual Pens.ctb",
)

#: Label → paper:drawing factor (``None`` = scaled to fit).
SCALES: dict[str, float | None] = {
    "fit": None,
    "1:1": 1.0,
    "1:2": 0.5,
    "1:5": 0.2,
    "1:10": 0.1,
    "1:20": 0.05,
    "1:50": 0.02,
    "1:100": 0.01,
    "2:1": 2.0,
    "5:1": 5.0,
    "10:1": 10.0,
}

#: DXF group code 75. Labels missing here use the custom numerator/denominator.
DXF_STANDARD_SCALE_TYPE: dict[str, int] = {
    "fit": 0,
    "1:1": 16,
    "1:2": 17,
    "1:10": 20,
    "1:20": 22,
    "1:50": 25,
    "1:100": 26,
    "2:1": 27,
    "10:1": 30,
}
_DXF_SCALE_LABEL = {code: label for label, code in DXF_STANDARD_SCALE_TYPE.items()}

#: ActiveX ``AcPlotScale`` (AutoCAD 2026 typelib). Labels missing here use SetCustomScale.
ACTIVEX_PLOT_SCALE: dict[str, int] = {
    "fit": 0,
    "1:1": 16,
    "1:2": 17,
    "1:5": 19,
    "1:10": 21,
    "1:20": 23,
    "1:50": 26,
    "1:100": 27,
    "2:1": 28,
    "10:1": 31,
}
_ACTIVEX_SCALE_LABEL = {code: label for label, code in ACTIVEX_PLOT_SCALE.items()}

#: Plot area → DXF/ActiveX plot type (``AcPlotType``: acExtents=1, acLayout=5).
PLOT_AREAS: dict[str, int] = {"layout": 5, "extents": 1}
PLOT_TYPE_NAMES: dict[int, str] = {
    0: "display",
    1: "extents",
    2: "limits",
    3: "view",
    4: "window",
    5: "layout",
}

ORIENTATIONS: tuple[str, ...] = ("landscape", "portrait")

_SCALE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*$")


def _paper_key(paper) -> str:
    key = str(paper or "").strip().lower().replace(" ", "_")
    resolved = _PAPER_ALIASES.get(key) or _PAPER_ALIASES.get(key.replace("_", " "))
    if resolved is None:
        raise ValueError(
            f"paper: unknown paper {paper!r}. Valid papers: {', '.join(PAPER_SIZES)} "
            "(short forms A0–A4 and ANSI_A–ANSI_E are accepted)"
        )
    return resolved


def _orientation(orientation) -> str:
    if orientation not in ORIENTATIONS:
        raise ValueError(f"orientation: must be one of {ORIENTATIONS}, got {orientation!r}")
    return orientation


def paper_size_mm(paper: str, orientation: str = "landscape") -> tuple[float, float]:
    """(width, height) in millimetres for ``paper`` in ``orientation``."""
    name = _paper_key(paper)
    short, long = PAPER_SIZES[name]
    if _orientation(orientation) == "landscape":
        return float(long), float(short)
    return float(short), float(long)


def canonical_media_name(paper: str, orientation: str = "landscape") -> str:
    """AutoCAD's spelling of the media: ``ISO_A3_(420.00_x_297.00_MM)`` for
    landscape A3, ``ANSI_B_(17.00_x_11.00_Inches)`` for landscape ANSI B."""
    name = _paper_key(paper)
    if name in PAPER_INCHES:
        short, long = PAPER_INCHES[name]
        unit = "Inches"
    else:
        short, long = PAPER_SIZES[name]
        unit = "MM"
    width, height = (long, short) if _orientation(orientation) == "landscape" else (short, long)
    return f"{name}_({width:.2f}_x_{height:.2f}_{unit})"


def paper_from_size(width_mm, height_mm, tolerance: float = 0.5) -> str | None:
    """The catalogue paper whose size (either orientation) is within ``tolerance`` mm."""
    try:
        w, h = float(width_mm), float(height_mm)
    except (TypeError, ValueError):
        return None
    for name, (short, long) in PAPER_SIZES.items():
        if (abs(w - long) <= tolerance and abs(h - short) <= tolerance) or (
            abs(w - short) <= tolerance and abs(h - long) <= tolerance
        ):
            return name
    return None


def scale_ratio(label: str) -> tuple[float, float]:
    """``"1:50"`` → ``(1.0, 50.0)`` (paper units : drawing units). ``fit`` has no ratio."""
    match = _SCALE_RE.match(str(label or ""))
    if match is None:
        raise ValueError(f"scale: {label!r} is not an N:M ratio")
    numerator, denominator = float(match.group(1)), float(match.group(2))
    if numerator <= 0 or denominator <= 0:
        raise ValueError(f"scale: {label!r} must have positive terms")
    return numerator, denominator


def scale_label(numerator, denominator) -> str:
    """The ``N:M`` label for a ratio: a catalogue label when the factor matches
    one, otherwise the reduced integer ratio, otherwise ``N:M`` with the floats."""
    num, den = float(numerator), float(denominator)
    if num <= 0 or den <= 0:
        return f"{num:g}:{den:g}"
    for label, factor in SCALES.items():
        if factor is not None and math.isclose(num / den, factor, rel_tol=1e-9):
            return label
    if num.is_integer() and den.is_integer():
        divisor = math.gcd(int(num), int(den))
        return f"{int(num) // divisor}:{int(den) // divisor}"
    return f"{num:g}:{den:g}"


def dxf_scale_label(code, numerator, denominator) -> str:
    """Read-back for the headless engine: the code-75 label, else the custom ratio."""
    return _DXF_SCALE_LABEL.get(int(code)) or scale_label(numerator, denominator)


def activex_scale_label(code) -> str:
    """Read-back for the live engine: the AcPlotScale label, else ``standard:<code>``."""
    return _ACTIVEX_SCALE_LABEL.get(int(code), f"standard:{int(code)}")


def plot_style_known(name) -> bool:
    return str(name or "").strip().lower() in {entry.lower() for entry in CTB_CATALOG}


def resolve_page_setup(
    paper: str,
    orientation: str = "landscape",
    plot_style: str = "monochrome.ctb",
    scale: str = "fit",
    plot_area: str = "layout",
    device: str = "DWG To PDF.pc3",
    margins_mm=None,
    center: bool = True,
) -> dict:
    """Validate a page setup request and resolve it to what both engines write.

    Raises ``ValueError`` naming the offending field; nothing here touches a
    drawing. ``margins_mm`` is ``[top, bottom, left, right]``.
    """
    name = _paper_key(paper)
    width, height = paper_size_mm(name, _orientation(orientation))

    style = str(plot_style or "").strip()
    if not style:
        raise ValueError("plot_style: must name a .ctb/.stb file (e.g. monochrome.ctb)")

    label = str(scale or "").strip()
    if label not in SCALES:
        raise ValueError(f"scale: must be one of {', '.join(SCALES)}, got {scale!r}")
    ratio = (1.0, 1.0) if label == "fit" else scale_ratio(label)

    if plot_area not in PLOT_AREAS:
        raise ValueError(f"plot_area: must be one of {', '.join(PLOT_AREAS)}, got {plot_area!r}")

    device_name = str(device or "").strip()
    if not device_name:
        raise ValueError("device: must name a plotter configuration (e.g. 'DWG To PDF.pc3')")

    margins = None
    if margins_mm is not None:
        try:
            values = [float(v) for v in margins_mm]
        except (TypeError, ValueError):
            raise ValueError(
                "margins_mm: must be four numbers [top, bottom, left, right]"
            ) from None
        if len(values) != 4:
            raise ValueError(
                f"margins_mm: expected 4 values [top, bottom, left, right], got {len(values)}"
            )
        if any(v < 0 for v in values):
            raise ValueError("margins_mm: margins cannot be negative")
        top, bottom, left, right = values
        if top + bottom >= height or left + right >= width:
            raise ValueError(
                f"margins_mm: {values} leave no printable area on a {width:g} x {height:g} mm sheet"
            )
        margins = values

    if not isinstance(center, bool):
        raise ValueError(f"center: must be true or false, got {center!r}")

    return {
        "paper": name,
        "orientation": orientation,
        "size_mm": [width, height],
        "canonical_media_name": canonical_media_name(name, orientation),
        "plot_style": style,
        "plot_style_known": plot_style_known(style),
        "scale": label,
        "scale_factor": SCALES[label],
        "scale_ratio": [ratio[0], ratio[1]],
        "dxf_standard_scale_type": DXF_STANDARD_SCALE_TYPE.get(label),
        "activex_standard_scale": ACTIVEX_PLOT_SCALE.get(label),
        "plot_area": plot_area,
        "plot_type": PLOT_AREAS[plot_area],
        "device": device_name,
        "margins_mm": margins,
        "center": center,
    }


#: Every key resolve_page_setup emits that a backend reads. A backend refuses
#: a dict missing any of them before it writes.
REQUIRED_SETUP_KEYS: tuple[str, ...] = (
    "paper",
    "orientation",
    "size_mm",
    "canonical_media_name",
    "plot_style",
    "plot_style_known",
    "scale",
    "scale_ratio",
    "dxf_standard_scale_type",
    "activex_standard_scale",
    "plot_area",
    "plot_type",
    "device",
    "margins_mm",
    "center",
)


def require_page_setup(setup) -> dict:
    """The backend-side check: ``setup`` is a complete resolve_page_setup dict."""
    if not isinstance(setup, dict):
        raise ValueError("page_setup_apply: setup must be the dict resolve_page_setup returns")
    missing = [key for key in REQUIRED_SETUP_KEYS if key not in setup]
    if missing:
        raise ValueError(
            f"page_setup_apply: setup is missing {', '.join(missing)} — build it with "
            "engineering.standards.papers.resolve_page_setup"
        )
    return setup
