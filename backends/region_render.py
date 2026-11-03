"""Region render: a picture of a window of the drawing, at a stated scale (1.8-07).

``view_screenshot(region=...)`` and ``view_zoom_and_screenshot`` share this
renderer on both engines. Headless it draws the backend's current space; live,
a region request is drawn from the ``Document.Export`` snapshot of the bound
document (``engineering/understand/snapshot.py``), so the same DXF gives the
same pixels whichever engine asked.

What the report promises, and why it is exact:

* ``px`` is read back from the PNG's own IHDR, never assumed.
* The figure is sized to the region's aspect with the long side ``max_px``.
  Rounding a side to whole pixels would leave x and y at two scales, so the
  shorter axis of the region is widened (by under one pixel) until both share
  one ``px_per_unit``; ``region`` is the window actually drawn, and
  ``requested_region`` the one asked for.
* The axes fill the figure (no ``bbox_inches="tight"``, no matplotlib margin),
  so pixel ``(0, 0)`` is ``(x0, y1)`` and a drawing point maps to
  ``((x - x0) * ppu, (y1 - y) * ppu)``.

Without a region the headless engine frames what the renderer drew (its
autoscaled extents) and says ``framing: "extents"``.
"""

from __future__ import annotations

import io
import math
from collections.abc import Callable, Sequence

#: The long side of a model-facing image: larger images are downscaled by the
#: client anyway, and every pixel past this costs tokens and shows nothing.
DEFAULT_MAX_PX = 1568
MIN_MAX_PX = 64
MAX_MAX_PX = 4096
_DPI = 100
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

Region = tuple[float, float, float, float]


def png_size(data: bytes) -> tuple[int, int]:
    """(width, height) out of a PNG's IHDR chunk; ``ValueError`` for anything else."""
    if len(data) < 24 or data[:8] != _PNG_SIGNATURE:
        raise ValueError("not a PNG image")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def check_max_px(max_px) -> int:
    """``max_px`` as an int within [MIN_MAX_PX, MAX_MAX_PX], or ``ValueError`` naming it."""
    if isinstance(max_px, bool) or not isinstance(max_px, int | float) or max_px != int(max_px):
        raise ValueError(f"max_px must be a whole number of pixels, got {max_px!r}")
    value = int(max_px)
    if not MIN_MAX_PX <= value <= MAX_MAX_PX:
        raise ValueError(
            f"max_px must be between {MIN_MAX_PX} and {MAX_MAX_PX} pixels, got {value}"
        )
    return value


def normalize_region(region) -> Region | None:
    """``[x0, y0, x1, y1]`` in any corner order -> (xmin, ymin, xmax, ymax).

    Refuses (``ValueError`` naming ``region``) anything but four finite numbers
    spanning a positive width and height: a zero-area window has no scale.
    """
    if region is None:
        return None
    if isinstance(region, str | bytes) or not isinstance(region, Sequence):
        raise ValueError(f"region must be [x0, y0, x1, y1] in drawing units, got {region!r}")
    if len(region) != 4:
        raise ValueError(
            f"region must be [x0, y0, x1, y1] in drawing units: four numbers, got {len(region)}"
        )
    try:
        values = [float(v) for v in region]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"region must hold four numbers, got {list(region)!r}") from exc
    if not all(math.isfinite(v) for v in values):
        raise ValueError(f"region must hold four finite numbers, got {values!r}")
    x0, x1 = sorted((values[0], values[2]))
    y0, y1 = sorted((values[1], values[3]))
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        raise ValueError(
            f"region {values!r} has zero width or height; a window needs two "
            "corners that differ in both x and y"
        )
    return (x0, y0, x1, y1)


def frame(region: Region, max_px: int) -> tuple[int, int, Region, float]:
    """(width_px, height_px, drawn region, px_per_unit) for ``region``.

    The long side is ``max_px``; the drawn region contains ``region`` and is
    widened on one axis by under a pixel so that x and y share one scale.
    """
    x0, y0, x1, y1 = region
    rw, rh = x1 - x0, y1 - y0
    if rw >= rh:
        width, height = max_px, max(1, round(max_px * rh / rw))
    else:
        width, height = max(1, round(max_px * rw / rh)), max_px
    ppu = min(width / rw, height / rh)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    half_w, half_h = width / ppu / 2.0, height / ppu / 2.0
    return width, height, (cx - half_w, cy - half_h, cx + half_w, cy + half_h), ppu


def _agg_figure():
    """A Figure on the Agg canvas - no pyplot, safe in a worker thread."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(dpi=_DPI)
    FigureCanvasAgg(fig)
    return fig


def _inside(point, region: Region) -> bool:
    x0, y0, x1, y1 = region
    return x0 <= point[0] <= x1 and y0 <= point[1] <= y1


def render_layout(
    doc,
    layout,
    *,
    region: Region | None = None,
    max_px: int = DEFAULT_MAX_PX,
    overlay_handles: bool = False,
    max_labels: int = 40,
    label_anchor: Callable | None = None,
) -> dict:
    """Render ``layout`` of ``doc`` to PNG; ``region`` None frames the extents.

    Raises ``ImportError`` when matplotlib is missing; the caller decides how
    to say so. ``region`` must already be normalised (``normalize_region``).
    """
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    max_px = check_max_px(max_px)
    fig = _agg_figure()
    ax = fig.add_axes((0.0, 0.0, 1.0, 1.0))
    out = MatplotlibBackend(ax, adjust_figure=False)
    Frontend(RenderContext(doc), out).draw_layout(layout, finalize=True)

    if region is None:
        (ex0, ex1), (ey0, ey1) = ax.get_xlim(), ax.get_ylim()
        if not ex1 - ex0 > 0:
            ex0, ex1 = ex0 - 0.5, ex0 + 0.5
        if not ey1 - ey0 > 0:
            ey0, ey1 = ey0 - 0.5, ey0 + 0.5
        target: Region = (ex0, ey0, ex1, ey1)
        framing = "extents"
    else:
        target = region
        framing = "region"

    width, height, drawn, ppu = frame(target, max_px)
    fig.set_size_inches(width / _DPI, height / _DPI)
    ax.set_aspect("auto")
    ax.set_xlim(drawn[0], drawn[2])
    ax.set_ylim(drawn[1], drawn[3])

    labels: dict[str, list[float]] = {}
    total = 0
    truncated = False
    if overlay_handles and label_anchor is not None:
        anchored = []
        for ent in layout:
            anchor = label_anchor(ent)
            if anchor is not None and _inside(anchor, drawn):
                anchored.append((ent, anchor))
        total = len(anchored)
        limit = max(0, int(max_labels))
        truncated = total > limit
        for ent, anchor in anchored[:limit]:
            handle = str(ent.dxf.get("handle", "?"))
            labels[handle] = [anchor[0], anchor[1]]
            ax.annotate(
                handle,
                anchor,
                color="#d62728",
                fontsize=7,
                ha="center",
                va="center",
                bbox={
                    "boxstyle": "round,pad=0.15",
                    "facecolor": "white",
                    "edgecolor": "#d62728",
                    "linewidth": 0.4,
                    "alpha": 0.85,
                },
            )
        if truncated:
            ax.text(
                0.5,
                0.98,
                f"handles shown for {len(labels)} of {total} entities",
                transform=ax.transAxes,
                color="#d62728",
                fontsize=9,
                ha="center",
                va="top",
            )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=_DPI, facecolor=ax.get_facecolor())
    png = buf.getvalue()
    got_w, got_h = png_size(png)
    return {
        "png": png,
        "applied": True,
        "framing": framing,
        "px": [got_w, got_h],
        "region": [float(v) for v in drawn],
        "requested_region": None if region is None else [float(v) for v in region],
        "px_per_unit": got_w / (drawn[2] - drawn[0]),
        "space": str(getattr(layout, "name", "Model")),
        "labels": labels,
        "labelled": len(labels),
        "total": total,
        "truncated": truncated,
    }


def render_dxf_file(path: str, *, region: Region | None, max_px: int = DEFAULT_MAX_PX) -> dict:
    """Render the model space of the DXF at ``path`` (the live engine's snapshot)."""
    import ezdxf

    doc = ezdxf.readfile(path)
    return render_layout(doc, doc.modelspace(), region=region, max_px=max_px)


def downscale_png(png: bytes, max_px: int) -> bytes:
    """``png`` with its long side brought down to ``max_px`` (aspect kept); never enlarged."""
    width, height = png_size(png)
    if max(width, height) <= max_px:
        return png
    from PIL import Image

    scale = max_px / max(width, height)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    image = Image.open(io.BytesIO(png))
    resized = image.resize(size, Image.LANCZOS)
    out = io.BytesIO()
    resized.save(out, format="PNG")
    return out.getvalue()
