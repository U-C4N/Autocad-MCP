"""Read a PDF's page size back from the file — no dependency, no rendering.

The evidence rule for page setup (spec §8.3): the sheet size a plot produced is
what the PDF says it is, not what the layout was asked for. ``/MediaBox`` is the
one PDF object that carries it. matplotlib and AutoCAD's DWG To PDF driver both
write it in clear text; a PDF 1.5+ writer may put the page dictionary inside a
FlateDecode object stream, so when the plain scan finds nothing every stream is
inflated and searched. Anything else raises rather than guessing.
"""

from __future__ import annotations

import re
import zlib
from pathlib import Path

__all__ = ["PT_TO_MM", "read_mediabox"]

#: PostScript points to millimetres.
PT_TO_MM = 25.4 / 72.0

_NUMBER = rb"([-+]?\d*\.?\d+)"
_MEDIABOX_RE = re.compile(
    rb"/MediaBox\s*\[\s*"
    + _NUMBER
    + rb"\s+"
    + _NUMBER
    + rb"\s+"
    + _NUMBER
    + rb"\s+"
    + _NUMBER
    + rb"\s*\]"
)
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)


def read_mediabox(path: str) -> tuple[float, float]:
    """Return the first page ``/MediaBox`` as ``(width_mm, height_mm)``.

    Raises ``ValueError`` when the file is not a PDF or carries no MediaBox.
    ``/Rotate`` is not applied: the box is reported as written.
    """
    data = Path(path).read_bytes()
    if not data.startswith(b"%PDF"):
        raise ValueError(f"read_mediabox: {path} is not a PDF (no %PDF header)")
    match = _MEDIABOX_RE.search(data)
    if match is None:
        for stream in _STREAM_RE.finditer(data):
            try:
                inflated = zlib.decompress(stream.group(1))
            except zlib.error:
                continue
            match = _MEDIABOX_RE.search(inflated)
            if match is not None:
                break
    if match is None:
        raise ValueError(f"read_mediabox: no /MediaBox found in {path}")
    x0, y0, x1, y1 = (float(value) for value in match.groups())
    return (round(abs(x1 - x0) * PT_TO_MM, 3), round(abs(y1 - y0) * PT_TO_MM, 3))
