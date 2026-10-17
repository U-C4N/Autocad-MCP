"""Document lifecycle.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backends.base import DrawingInfo


class DrawingContract(ABC):
    @abstractmethod
    async def drawing_info(self) -> DrawingInfo: ...

    @abstractmethod
    async def drawing_new(self, template: str | None = None) -> dict: ...

    @abstractmethod
    async def drawing_open(self, path: str) -> dict: ...

    @abstractmethod
    async def drawing_save(self, path: str | None = None) -> dict: ...

    @abstractmethod
    async def drawing_save_as(self, path: str, fmt: str = "dwg") -> dict: ...

    @abstractmethod
    async def drawing_export_dxf(self, path: str, version: str | None = None) -> dict:
        """Write a DXF of the drawing; ``version`` picks the DXF release (R12 … R2018)."""
        ...

    @abstractmethod
    async def drawing_export_pdf(self, path: str, layout: str | None = None) -> dict: ...

    @abstractmethod
    async def drawing_purge(self) -> dict: ...


#: DXF releases a caller can ask for → ``$ACADVER``. R12 is the format older
#: cutters, plotters and CNC front ends still read.
DXF_VERSIONS: dict[str, str] = {
    "R12": "AC1009",
    "R2000": "AC1015",
    "R2004": "AC1018",
    "R2007": "AC1021",
    "R2010": "AC1024",
    "R2013": "AC1027",
    "R2018": "AC1032",
}


def resolve_dxf_version(version) -> tuple[str, str]:
    """``("R2000", "AC1015")`` for ``"R2000"`` / ``"2000"`` / ``"AC1015"`` — or ``ValueError``."""
    text = str(version or "").strip().upper()
    if text in DXF_VERSIONS.values():
        label = next(k for k, v in DXF_VERSIONS.items() if v == text)
        return label, text
    if not text.startswith("R"):
        text = "R" + text
    if text not in DXF_VERSIONS:
        raise ValueError(
            f"dxf_version: {version!r} is not a DXF release this server writes; use one of "
            f"{', '.join(DXF_VERSIONS)}"
        )
    return text, DXF_VERSIONS[text]


def read_acadver(path) -> str | None:
    """The ``$ACADVER`` a DXF file declares (ASCII DXF only), or None."""
    try:
        with open(path, encoding="ascii", errors="ignore") as fp:
            lines = [fp.readline().strip() for _ in range(400)]
    except OSError:
        return None
    for i, line in enumerate(lines):
        if line == "$ACADVER" and i + 2 < len(lines):
            return lines[i + 2] or None
    return None
