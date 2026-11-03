"""Reading a DXF, and recovering a damaged one (B09, 1.8-04).

``read_dxf`` is the plain read ``drawing_open`` always did; a file ezdxf
cannot parse is refused with ``DrawingParseError`` (kind ``damaged_drawing``)
that names the way out: ``drawing_open(path, recover=True)``. That call goes
through ``recover_dxf`` - ``ezdxf.recover.readfile``, which repairs what it can
- and reports ``recovered: {fixes, errors, messages[:20], entities_read}``,
because the recover reader can drop entities without a fix entry (a truncated
file reads back with zero entities and zero fixes, measured on ezdxf 1.4.4);
the counts are the only honest account of what came back.

A recovered document stays bound to its source path, and a save onto that
source is refused (``RecoveredSourceError``, kind
``recovered_source_protected``) unless the caller passes
``overwrite_source=True``: the damaged file may be the only copy of what the
reader could not read.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "MESSAGES_SHOWN",
    "DrawingParseError",
    "RecoverUnsupportedError",
    "RecoveredSourceError",
    "read_dxf",
    "recover_dxf",
    "recovered_source_refusal",
]

MESSAGES_SHOWN = 20


class DrawingParseError(ValueError):
    """A file that is not a readable DXF; nothing was opened."""

    kind = "damaged_drawing"


class RecoveredSourceError(ValueError):
    """A save onto the damaged source of a recovered document, without consent."""

    kind = "recovered_source_protected"


class RecoverUnsupportedError(ValueError):
    """``recover=True`` on a file the recover reader cannot take (a DWG)."""

    kind = "recover_unsupported"


def _fault(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def read_dxf(path: str):
    """``ezdxf.readfile(path)``; a parse failure is a ``DrawingParseError`` naming recover=True."""
    import ezdxf
    from ezdxf.lldxf.const import DXFError

    try:
        return ezdxf.readfile(path)
    except (FileNotFoundError, PermissionError, IsADirectoryError):
        raise
    except (DXFError, OSError, UnicodeDecodeError, ValueError, IndexError) as exc:
        if isinstance(exc, OSError) and not os.path.isfile(path):
            raise
        raise DrawingParseError(
            f"drawing_open: {Path(path).name} could not be read as a DXF file ({_fault(exc)}). "
            "Nothing was opened and the open document is unchanged. "
            "drawing_open(path, recover=True) reads it with ezdxf's recover reader, which "
            "repairs what it can and reports every fix and the entities it read back; the "
            "recovered drawing will not save over this file without overwrite_source=True."
        ) from exc


def recover_dxf(path: str):
    """``(doc, recovered)`` from ``ezdxf.recover.readfile``; refused when even that fails."""
    from ezdxf import recover
    from ezdxf.lldxf.const import DXFError

    if Path(path).suffix.lower() == ".dwg":
        raise RecoverUnsupportedError(
            f"drawing_open(recover=True): {Path(path).name} is a DWG; the recover reader "
            "repairs DXF only. Use AutoCAD's own RECOVER command on a DWG."
        )
    if not os.path.isfile(path):
        raise FileNotFoundError(f"drawing_open(recover=True): {path} does not exist")
    try:
        doc, auditor = recover.readfile(path)
    except (DXFError, OSError, UnicodeDecodeError, ValueError, IndexError) as exc:
        raise DrawingParseError(
            f"drawing_open(recover=True): {Path(path).name} could not be recovered either "
            f"({_fault(exc)}): it is not a DXF file the recover reader can repair. Nothing "
            "was opened and the open document is unchanged."
        ) from exc
    entries = list(auditor.fixes) + list(auditor.errors)
    messages = [f"{getattr(e, 'code', '')} {getattr(e, 'message', e)}".strip() for e in entries]
    entities = sum(len(layout) for layout in doc.layouts)
    recovered = {
        "fixes": len(auditor.fixes),
        "errors": len(auditor.errors),
        "messages": messages[:MESSAGES_SHOWN],
        "entities_read": entities,
    }
    if len(messages) > MESSAGES_SHOWN:
        recovered["messages_total"] = len(messages)
    if entities == 0:
        recovered["warning"] = (
            "no entity was read back: the recovered drawing is empty. The recover reader "
            "drops what it cannot parse, not always with a fix entry."
        )
    return doc, recovered


def recovered_source_refusal(op: str, label: str, source: str) -> RecoveredSourceError:
    return RecoveredSourceError(
        f"{op}: {label!r} was opened with drawing_open(recover=True) from {source}; saving onto "
        "that damaged source would replace the only copy of what the recover reader could not "
        "read. Nothing was written. Pass overwrite_source=True to drawing_save to replace it, or "
        "save to another path with drawing_save_as."
    )
