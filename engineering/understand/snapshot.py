"""One bulk read of a drawing into frozen records - the input of every track H reader.

Reading a 35,000-entity P&ID one COM call at a time takes minutes; reading its
DXF takes about a second. So every analysis in ``engineering/understand/`` runs
on a :class:`Snapshot`: the drawing read once, into small frozen
:class:`EntityRecord` s, and never touched again.

Where the snapshot comes from:

* **A DXF file** - :func:`read_snapshot`, ``ezdxf.readfile`` after the size
  check (``MAX_DXF_BYTES``, 512 MB by default; the refusal names the size and
  the variable).
* **The headless engine's current document** - :func:`records_from_doc` on the
  in-memory ``ezdxf`` document, inside the backend's own lock.
* **The live engine's current document** - ``Document.Export(<tmp>/snap, "DXF",
  <selection set of all>)``, then :func:`read_snapshot` on the file, which is
  deleted afterwards. Measured on AutoCAD 2026 (``25.1s (LMS Tech)``) on
  2026-09-24, in a new scratch document closed afterwards:

  - the extension argument must be ``"DXF"``; ``".dxf"`` fails with *Invalid
    argument*; the file is written as ``snap.DXF`` (upper-case extension);
  - the document keeps its name (``Export`` is not ``SaveAs``), and an unsaved
    document exports;
  - **model space, every paper-space layout and every block definition reach
    the snapshot** - a LINE in Layout1 was read back in Layout1, and an unused
    block definition was in the BLOCKS section;
  - **the selection set does not narrow a DXF export**: an empty selection set
    wrote the same model space, paper space and blocks as the set of all. The
    set of all is passed anyway, because the ActiveX signature requires one.
  - xrefs were not measured, so nothing is claimed for the live engine;
    headless, ezdxf reads the host file only. Either way an xref's own
    entities are not read in 1.6 - the host drawing is - and
    ``drawing_understand``'s docstring says so.

  A DWG path on the live engine is opened read-only, exported the same way and
  closed without saving.

Records hold what the readers need and nothing else: WCS points (OCS is
translated here with ``backends/ocs.py``, so a mirrored ARC or LWPOLYLINE is
where it looks), plain text (``labels.plain``: AutoCAD codes and MTEXT format
runs decoded), block names and attributes, a WCS box. Block definitions are not
exploded: an INSERT is one record with its block name, attributes and box.
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import ezdxf
from ezdxf import bbox as ezbbox

import config
from backends.capability import UnsupportedCapabilityError
from backends.ocs import to_wcs_2d, wcs_arc_angles, wcs_bulge
from engineering.measure import polygon_area_perimeter
from engineering.understand.labels import plain

__all__ = [
    "EntityRecord",
    "Pt",
    "Snapshot",
    "length_of",
    "read_snapshot",
    "records_from_doc",
    "take_snapshot",
]

Pt = tuple[float, float]

#: ezdxf's placeholder for an extents header nobody computed.
_UNSET_EXTENT = 1e19
#: The live export's base name inside its temporary folder.
_SNAP_BASE = "snap"


@dataclass(frozen=True)
class EntityRecord:
    """One entity of a layout, in WCS, as the readers need it."""

    handle: str
    type: str
    layer: str
    space: str
    points: tuple[Pt, ...] = ()
    bulges: tuple[float, ...] = ()
    closed: bool = False
    radius: float | None = None
    angles: tuple[float, float] | None = None
    text: str | None = None
    height: float | None = None
    block: str | None = None
    attribs: tuple[tuple[str, str], ...] = ()
    bbox: tuple[float, float, float, float] | None = None
    rotation: float = 0.0
    scale: tuple[float, float] = (1.0, 1.0)


@dataclass(frozen=True)
class Snapshot:
    """A whole drawing, read once."""

    source: str
    insunits: int
    extmin: Pt | None
    extmax: Pt | None
    layers: dict[str, dict]
    layouts: tuple[str, ...]
    records: tuple[EntityRecord, ...]


def _xy(point) -> Pt:
    return (float(point[0]), float(point[1]))


def _box(entity) -> tuple[float, float, float, float] | None:
    """The entity's WCS box. An INSERT's box is its block geometry only: the
    ATTRIB texts are left out, because a TAG lettered beside a tank would push
    the tank's box past its body (the rule `engineering/pid/graph.py` keeps)."""
    try:
        if entity.dxftype() == "INSERT":
            box = ezbbox.extents(entity.virtual_entities(), fast=True)
        else:
            box = ezbbox.extents([entity], fast=True)
    except Exception:  # a proxy or a malformed entity has no box; the record stays
        return None
    if not box.has_data:
        return None
    return (
        float(box.extmin.x),
        float(box.extmin.y),
        float(box.extmax.x),
        float(box.extmax.y),
    )


def _header_point(doc, name: str) -> Pt | None:
    try:
        value = doc.header.get(name)
    except Exception:
        return None
    if value is None or abs(float(value[0])) >= _UNSET_EXTENT:
        return None
    return _xy(value)


def _extrusion(entity):
    """The entity's extrusion, or +Z for a type that has none (MULTILEADER, ...)."""
    if entity.dxf.is_supported("extrusion"):
        return entity.dxf.get("extrusion", (0.0, 0.0, 1.0))
    return (0.0, 0.0, 1.0)


def _polyline(entity) -> tuple[tuple[Pt, ...], tuple[float, ...], bool]:
    extrusion = _extrusion(entity)
    if entity.dxftype() == "LWPOLYLINE":
        elevation = float(entity.dxf.get("elevation", 0.0))
        rows = [(float(x), float(y), float(b)) for x, y, b in entity.get_points("xyb")]
        points = tuple(tuple(to_wcs_2d(extrusion, x, y, elevation)) for x, y, _ in rows)
        bulges = tuple(wcs_bulge(extrusion, b) for _, _, b in rows)
        return points, bulges, bool(entity.closed)
    vertices = list(entity.vertices)
    if entity.is_2d_polyline:
        points = tuple(
            tuple(to_wcs_2d(extrusion, v.dxf.location.x, v.dxf.location.y, v.dxf.location.z))
            for v in vertices
        )
        bulges = tuple(wcs_bulge(extrusion, v.dxf.get("bulge", 0.0)) for v in vertices)
    else:  # a 3D polyline is WCS already; its z is dropped
        points = tuple(_xy(v.dxf.location) for v in vertices)
        bulges = tuple(0.0 for _ in vertices)
    return points, bulges, bool(entity.is_closed)


def _record(entity, space: str) -> EntityRecord | None:
    kind = entity.dxftype()
    dxf = entity.dxf
    base = {
        "handle": str(dxf.handle),
        "type": kind,
        "layer": str(dxf.get("layer", "0")),
        "space": space,
        "bbox": _box(entity),
    }
    extrusion = _extrusion(entity)
    if kind == "LINE":
        return EntityRecord(**base, points=(_xy(dxf.start), _xy(dxf.end)))
    if kind in ("LWPOLYLINE", "POLYLINE"):
        if kind == "POLYLINE" and (entity.is_poly_face_mesh or entity.is_polygon_mesh):
            return EntityRecord(**base)
        points, bulges, closed = _polyline(entity)
        return EntityRecord(**base, points=points, bulges=bulges, closed=closed)
    if kind in ("ARC", "CIRCLE"):
        centre = tuple(to_wcs_2d(extrusion, dxf.center.x, dxf.center.y, dxf.center.z))
        angles = None
        if kind == "ARC":
            angles = wcs_arc_angles(
                extrusion, dxf.center, dxf.radius, dxf.start_angle, dxf.end_angle
            )
        return EntityRecord(**base, points=(centre,), radius=float(dxf.radius), angles=angles)
    if kind == "TEXT":
        insert = dxf.insert
        return EntityRecord(
            **base,
            points=(tuple(to_wcs_2d(extrusion, insert.x, insert.y, insert.z)),),
            text=plain(dxf.get("text", "")),
            height=float(dxf.get("height", 0.0)),
            rotation=float(dxf.get("rotation", 0.0)),
        )
    if kind == "MTEXT":
        return EntityRecord(
            **base,
            points=(_xy(dxf.insert),),
            text=plain(entity.text),
            height=float(dxf.get("char_height", 0.0)),
            rotation=float(entity.get_rotation()),
        )
    if kind == "INSERT":
        insert = dxf.insert
        attribs = tuple((str(a.dxf.tag), plain(a.dxf.get("text", ""))) for a in entity.attribs)
        return EntityRecord(
            **base,
            points=(tuple(to_wcs_2d(extrusion, insert.x, insert.y, insert.z)),),
            block=str(dxf.name),
            attribs=attribs,
            rotation=float(dxf.get("rotation", 0.0)),
            scale=(float(dxf.get("xscale", 1.0)), float(dxf.get("yscale", 1.0))),
        )
    if kind == "MULTILEADER":
        context = entity.context
        points: list[Pt] = []
        for leader in context.leaders:
            for line in leader.lines:
                points.extend(_xy(v) for v in line.vertices)
            points.append(_xy(leader.last_leader_point))
        text = None
        height = None
        if context.mtext is not None:
            text = plain(context.mtext.default_content)
            points.append(_xy(context.mtext.insert))
            height = float(context.char_height)
        return EntityRecord(**base, points=tuple(points), text=text, height=height)
    if kind == "DIMENSION":
        override = str(dxf.get("text", "") or "")
        text = plain(override) if override not in ("", "<>") else None
        middle = dxf.get("text_midpoint", dxf.defpoint)
        points = (_xy(dxf.defpoint), tuple(to_wcs_2d(extrusion, middle.x, middle.y, middle.z)))
        return EntityRecord(**base, points=points, text=text)
    if kind == "LEADER":
        return EntityRecord(**base, points=tuple(_xy(v) for v in entity.vertices))
    if kind == "POINT":
        return EntityRecord(**base, points=(_xy(dxf.location),))
    if kind == "SPLINE":
        return EntityRecord(**base, points=tuple(_xy(p) for p in entity.control_points))
    if kind == "ELLIPSE":
        return EntityRecord(**base, points=(_xy(dxf.center),))
    return EntityRecord(**base)


def records_from_doc(doc, *, source: str) -> Snapshot:
    """Every entity of every layout of an ``ezdxf`` document, as records.

    Model space first, then each paper layout in tab order. ``insunits`` is
    ``$INSUNITS`` (0 when absent); ``extmin`` / ``extmax`` are the declared
    ``$EXTMIN`` / ``$EXTMAX``, None when the header holds ezdxf's "never
    computed" placeholder. Nothing in ``doc`` is modified.
    """
    records: list[EntityRecord] = []
    layouts = tuple(doc.layouts.names_in_taborder())
    for name in layouts:
        for entity in doc.layouts.get(name):
            record = _record(entity, name)
            if record is not None:
                records.append(record)
    layers = {
        str(layer.dxf.name): {
            "color": int(layer.dxf.get("color", 7)),
            "linetype": str(layer.dxf.get("linetype", "Continuous")),
        }
        for layer in doc.layers
    }
    return Snapshot(
        source=source,
        insunits=int(doc.header.get("$INSUNITS", 0) or 0),
        extmin=_header_point(doc, "$EXTMIN"),
        extmax=_header_point(doc, "$EXTMAX"),
        layers=layers,
        layouts=layouts,
        records=tuple(records),
    )


def _read(path: str, *, source: str) -> Snapshot:
    try:
        doc = ezdxf.readfile(path)
    except (OSError, ezdxf.DXFError) as exc:
        raise ValueError(f"{path} is not a readable DXF file: {exc}") from exc
    return records_from_doc(doc, source=source)


def read_snapshot(path: str) -> Snapshot:
    """The snapshot of a DXF file on disk, after the size check.

    Refuses a missing file (``FileNotFoundError``) and a file larger than
    ``MAX_DXF_BYTES`` (``ValueError`` naming the size, the limit and the
    variable; 0 disables the check) before reading a byte of it.
    """
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"{path}: no such file")
    limit = int(config.settings.max_dxf_bytes)
    size = target.stat().st_size
    if limit > 0 and size > limit:
        raise ValueError(
            f"{path} is {size:,} bytes ({size / 2**20:.1f} MB), over the MAX_DXF_BYTES "
            f"limit of {limit:,} bytes ({limit / 2**20:.0f} MB). Raise MAX_DXF_BYTES "
            "(0 disables the check) to read it."
        )
    return _read(str(target), source=str(target))


def _com_export(backend, path: str | None) -> tuple[str, str, str]:
    """(snapshot file, its temporary folder, source) - runs on the COM thread."""
    from backends import com_backend as com

    app = com._acad_app()
    opened = None
    if path is None:
        doc = com._acad_doc()
    else:
        opened = app.Documents.Open(str(path), True)  # read-only
        doc = opened
    folder = tempfile.mkdtemp(prefix="acadmcp_snap_")
    try:
        name = str(backend._wait_out_rejected_call(lambda: doc.Name))
        selection = doc.SelectionSets.Add(f"_SNAP_{uuid.uuid4().hex[:8]}")
        try:
            selection.Select(com._AC_SELECTION_SET_ALL)
            # "DXF", never ".dxf": the dotted spelling is refused with
            # "Invalid argument" (measured, AutoCAD 2026).
            doc.Export(os.path.join(folder, _SNAP_BASE), "DXF", selection)
        finally:
            try:
                selection.Delete()
            except Exception:  # a selection set left behind is harmless; the export is not
                pass
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        if opened is not None:
            opened.Close(False)
    written = sorted(Path(folder).glob(f"{_SNAP_BASE}.*"))
    if not written:
        shutil.rmtree(folder, ignore_errors=True)
        raise RuntimeError(f"AutoCAD reported the export of {name} done but wrote no file")
    return str(written[0]), folder, f"live:{name}"


async def take_snapshot(backend, path: str | None = None) -> Snapshot:
    """The snapshot of ``path``, or of the backend's current document.

    ``path`` given: a ``.dxf`` is read directly (on either engine); a ``.dwg``
    needs the live engine, which opens it read-only, exports it and closes it -
    headlessly it is refused with the ``dwg`` capability. ``path`` None: the
    headless engine reads its in-memory document under its own lock; the live
    engine exports the active document to a temporary DXF (see the module
    docstring for what was measured) and deletes it afterwards. The snapshot of
    a live document is not size-checked: it is already in AutoCAD's memory.
    """
    if path is not None:
        suffix = Path(path).suffix.lower()
        if suffix == ".dxf":
            return await asyncio.to_thread(read_snapshot, str(path))
        if suffix != ".dwg":
            raise ValueError(f"{path}: a snapshot reads a .dxf or a .dwg, not {suffix or 'this'}")
        if not Path(path).is_file():
            raise FileNotFoundError(f"{path}: no such file")
        if backend.name != "com":
            raise UnsupportedCapabilityError(
                "dwg",
                f"{path} is a DWG, which the headless engine cannot read. Export it to "
                "DXF (DXFOUT or SAVEAS in AutoCAD) and pass the .dxf, or run the server "
                "on the live engine (AUTOCAD_MCP_BACKEND=com).",
            )
    if backend.name == "com":
        written, folder, source = await backend._run(_com_export, backend, path)
        if path is not None:
            await backend._ensure_document_state()
        try:
            return await asyncio.to_thread(_read, written, source=source)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    source = backend._doc_path or "memory:untitled"
    return await backend._async(lambda: records_from_doc(backend._doc, source=source))


def length_of(rec: EntityRecord) -> float:
    """Drawn length in drawing units: LINE, LWPOLYLINE / POLYLINE with their
    bulges (``engineering.measure``, the same arithmetic ``analysis_measure_entity``
    reports), ARC, CIRCLE. Every other type is 0.0.
    """
    if rec.type == "LINE" and len(rec.points) == 2:
        return math.dist(rec.points[0], rec.points[1])
    if rec.type in ("LWPOLYLINE", "POLYLINE") and len(rec.points) >= 2:
        bulges = rec.bulges or tuple(0.0 for _ in rec.points)
        vertices = [(x, y, b) for (x, y), b in zip(rec.points, bulges, strict=False)]
        return polygon_area_perimeter(vertices, rec.closed)[1]
    if rec.type == "ARC" and rec.radius is not None and rec.angles is not None:
        sweep = (rec.angles[1] - rec.angles[0]) % 360.0
        return rec.radius * math.radians(sweep)
    if rec.type == "CIRCLE" and rec.radius is not None:
        return 2.0 * math.pi * rec.radius
    return 0.0
