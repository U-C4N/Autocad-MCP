"""batch_plot: every sheet through drawing_export_pdf, each PDF measured back.

Composes the existing per-layout export; the only thing it adds is the
evidence — ``mediabox_mm`` is parsed from the file each sheet produced, so a
caller sees the size that was plotted, not the size that was asked for.
"""

from __future__ import annotations

import re
from pathlib import Path

from engineering.standards.papers import paper_from_size
from engineering.standards.pdfinfo import read_mediabox

__all__ = ["DEFAULT_FILE_PATTERN", "batch_plot", "safe_filename"]

DEFAULT_FILE_PATTERN = "{drawing}-{layout}.pdf"

_UNSAFE = re.compile(r"[<>:\"/\\|?*\s]+")


def safe_filename(text: str) -> str:
    """A layout or drawing name as a filename fragment: no separators, no
    reserved characters, runs of them collapsed to one underscore."""
    cleaned = _UNSAFE.sub("_", str(text or "").strip()).strip("_")
    return cleaned or "untitled"


async def batch_plot(
    backend,
    layouts: list[str] | None,
    output_dir: str,
    file_pattern: str = DEFAULT_FILE_PATTERN,
) -> dict:
    """Plot layouts to PDF and read each sheet size back from the file.

    ``layouts=None`` plots every paper-space layout; ``Model`` is plotted only
    when named, and is passed to the engine by name so the model row is model
    space whichever tab is current. A sheet the engine cannot plot, or reports
    plotted without a file, is a per-row ``ok: False`` with its ``error``; the
    rows before it stay. Refuses (``ValueError``, before any PDF is written): an empty
    list, a layout that does not exist, a pattern with a path separator, and
    a pattern without ``{layout}`` when more than one sheet would be written.
    The output folder is created, then every output path goes through
    ``security.validate_path(allow_write=True)`` before the first export.
    """
    from security import validate_path

    listing = await backend.layout_list()
    available = [str(name) for name in (listing.get("layouts") or [])]
    lowered = {name.lower(): name for name in available}
    if layouts is None:
        targets = [name for name in available if name.lower() != "model"]
        if not targets:
            raise ValueError("batch_plot: the drawing has no paper-space layout to plot")
    else:
        requested = [str(name).strip() for name in layouts]
        if not requested:
            raise ValueError("batch_plot: layouts is empty — pass null for every sheet")
        unknown = [name for name in requested if name.lower() not in lowered]
        if unknown:
            raise ValueError(
                f"batch_plot: layout(s) not found: {', '.join(unknown)} "
                f"(available: {', '.join(available)})"
            )
        targets = [lowered[name.lower()] for name in requested]

    pattern = str(file_pattern or DEFAULT_FILE_PATTERN)
    if "/" in pattern or "\\" in pattern:
        raise ValueError("batch_plot: file_pattern must be a file name, not a path")
    if "{layout}" not in pattern and len(targets) > 1:
        raise ValueError(
            "batch_plot: file_pattern needs {layout} when more than one sheet is plotted, "
            "or every sheet would overwrite the last"
        )
    try:
        pattern.format(drawing="x", layout="y")
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(
            f"batch_plot: file_pattern {pattern!r} may only use {{drawing}} and {{layout}}"
        ) from exc

    info = await backend.drawing_info()
    drawing = safe_filename(Path(info.name).stem)
    destination = Path(validate_path(output_dir, allow_write=True))
    planned = [
        (name, destination / pattern.format(drawing=drawing, layout=safe_filename(name)))
        for name in targets
    ]
    # validate_path(allow_write=True) requires the parent folder to exist, so
    # the output folder is created before the per-file fence check; an empty
    # folder is the most a refused call leaves behind.
    destination.mkdir(parents=True, exist_ok=True)
    for _name, path in planned:
        validate_path(str(path), allow_write=True)

    sheets: list[dict] = []
    for name, path in planned:
        is_model = name.lower() == "model"
        # The layout is always named — "Model" included — so the engine plots
        # the tab this row is labelled with, never the one that happens to be
        # current. A plot that fails inside the engine (COM raises RuntimeError
        # for an AutoCAD error) or that reports ok without leaving a file is a
        # failed row, not a lost batch: the sheets already plotted are kept.
        try:
            result = await backend.drawing_export_pdf(str(path), layout=name)
        except RuntimeError as exc:
            sheets.append({"layout": name, "ok": False, "path": str(path), "error": str(exc)})
            continue
        if not result.get("ok", False):
            sheets.append(
                {"layout": name, "ok": False, "path": str(path), "error": result.get("error")}
            )
            continue
        if not path.is_file():
            sheets.append(
                {
                    "layout": name,
                    "ok": False,
                    "path": str(path),
                    "error": "the engine reported the plot ok but no file was written",
                }
            )
            continue
        row = {"layout": name, "ok": True, "path": str(path), "bytes": path.stat().st_size}
        try:
            width, height = read_mediabox(str(path))
            row["mediabox_mm"] = [width, height]
            row["paper"] = None if is_model else paper_from_size(width, height)
        except ValueError as exc:
            row["mediabox_mm"] = None
            row["paper"] = None
            row["error"] = str(exc)
        sheets.append(row)

    return {
        "ok": all(row["ok"] for row in sheets),
        "drawing": drawing,
        "output_dir": str(destination),
        "format": "pdf",
        "sheets": sheets,
        "count": len(sheets),
    }
