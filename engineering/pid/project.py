"""A multi-sheet P&ID project, read-only: ``pid_project`` (plan 1.8-21).

Off-page connectors were matched only inside one document
(``graph.build_graph`` step 6): a ``LINK`` id that leaves sheet 1 and never
arrives on sheet 2 was invisible. This module reads every sheet of a project
with the same reader ``pid_graph`` uses and checks what only the whole set can
show.

**Reading.** Every sheet that is a file goes through ``build_graph(scope="all")``
on a *private* ``EzdxfBackend`` that is registered without a path, so nothing
it holds can ever be saved onto the sheet; the file's sha256 is taken before
and after the parse, and a sheet whose bytes moved while it was read is
refused by name. A ``.dwg`` sheet is read only on the live engine, through the
snapshot route (``engineering.understand.snapshot._com_export``: opened
read-only, exported to a temporary DXF, closed; a drawing the operator already
has open is exported as it stands and left open); headlessly it is refused
with the ``dwg`` capability. The active document is read through the session
backend (``include_current``); when it is one of the listed files it replaces
that file's reading, unsaved edits included, and the row says so
(``source: "active_document"``). File graphs are cached by sha256 for the
life of the process; the active document never is. More than
:data:`MAX_SHEETS` sheets are refused by name before anything is read.

**Checks** (``action="check"``):

* ``links`` - one row per ``LINK`` id over every sheet: exactly one ``out`` and
  one ``in`` connector is ``matched``; one connector alone is ``orphan``; two
  that are not one ``out`` and one ``in`` (a foreign connector whose direction
  the block name does not say counts as not) is ``direction_mismatch``; three
  or more is ``duplicate``. Every connector names its sheet and handle.
* ``duplicate_tags`` - a tag carried on more than one sheet. Connectors are
  never counted (their TAG is free text such as ``TO P&ID-002``, repeated by
  design on a continuation), nor ``unknown_block`` guesses; a duplicate inside
  one sheet is ``pid_duplicate_tag``'s business, not this one's.
* ``line_continuity`` - for every matched pair, the line number of the line on
  the ``out`` side against the one on the ``in`` side: ``equal``,
  ``mismatch`` or ``unknown`` (a side has no numbered line).

**Index** (``action="index"``): the instrument index, line list and equipment
list of :mod:`engineering.pid.deliverables`, merged over the sheets, each row
led by a ``sheet`` column.

**Write** (``write=``): the only file this tool writes, validated before any
sheet is read through ``security.validate_path(allow_write=True,
kinds=WRITE_KINDS)``. ``.json`` is the whole result; ``.csv`` is the action's
tables in one long file led by a ``table`` column; ``.xlsx`` is one worksheet
per table and needs openpyxl, refused with the ``xlsx_write`` capability when it
is absent (before anything is read). Text that starts like a formula is
prefixed (``engineering.export.cells``).

The write side - a project-wide tag sequence and a refusal of a tag another
sheet holds, through ``project=`` on the drawing tools - is 1.8.1.
"""

from __future__ import annotations

import asyncio
import collections
import copy
import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import config
from backends.capability import UnsupportedCapabilityError
from engineering.errors import InvalidArgsError

from .deliverables import (
    EQUIPMENT_COLUMNS,
    INDEX_COLUMNS,
    LINE_COLUMNS,
    NUMBER_COLUMNS,
    equipment_list,
    instrument_index,
    line_list,
    natural_key,
)
from .graph import build_graph

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

__all__ = [
    "ACTIONS",
    "MAX_SHEETS",
    "WRITE_KINDS",
    "check_links",
    "clear_cache",
    "cross_sheet_duplicate_tags",
    "last_result",
    "line_continuity",
    "merged_index",
    "run_project",
]

ACTIONS = ("check", "index")
#: The most sheets one call reads (plan 1.8-21: "50 pafta üstü isimle ret").
MAX_SHEETS = 50
SHEET_SUFFIXES = (".dxf", ".dwg")
WRITE_KINDS: tuple[str, ...] = (".json", ".csv", ".xlsx")
#: File graphs kept by sha256 (each a few hundred KB at most).
CACHE_SIZE = 128
CURRENT_SHEET = "current"

_CACHE: collections.OrderedDict[str, dict] = collections.OrderedDict()
_LAST: dict | None = None


def clear_cache() -> None:
    """Forget every cached sheet graph (tests; a long-lived server never needs it)."""
    _CACHE.clear()


def last_result() -> dict | None:
    """The previous ``pid_project`` result of this process, or None."""
    return copy.deepcopy(_LAST) if _LAST is not None else None


# ── inputs ───────────────────────────────────────────────────────────────────


def _text_or_none(value, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise InvalidArgsError(f"invalid_args: {where} must be text, got {type(value).__name__}")
    text = str(value).strip()
    return text or None


def parse_sheets(sheets, manifest) -> tuple[str | None, list[dict]]:
    """``(project, [{number, title, path}])`` from ``sheets`` or ``manifest``.

    Exactly one of the two may be given. A sheet's ``number`` defaults to its
    file stem; numbers are unique and so are files (by spelling, case-folded).
    """
    if sheets is not None and manifest is not None:
        raise InvalidArgsError("invalid_args: pass sheets or manifest, not both")
    project = None
    rows: list[dict] = []
    if manifest is not None:
        if not isinstance(manifest, dict):
            raise InvalidArgsError("invalid_args: manifest must be {project, sheets: [...]}")
        unknown = sorted(set(manifest) - {"project", "sheets"})
        if unknown:
            raise InvalidArgsError(
                f"invalid_args: manifest: unknown key(s) {', '.join(unknown)}; "
                "keys are project, sheets"
            )
        project = _text_or_none(manifest.get("project"), "manifest.project")
        items = manifest.get("sheets")
        if not isinstance(items, list):
            raise InvalidArgsError("invalid_args: manifest.sheets must be a list")
        for i, item in enumerate(items):
            where = f"manifest.sheets[{i}]"
            if not isinstance(item, dict):
                raise InvalidArgsError(f"invalid_args: {where} must be {{path, number, title}}")
            bad = sorted(set(item) - {"path", "number", "title"})
            if bad:
                raise InvalidArgsError(
                    f"invalid_args: {where}: unknown key(s) {', '.join(bad)}; "
                    "keys are path, number, title"
                )
            path = item.get("path")
            if not isinstance(path, str) or not path.strip():
                raise InvalidArgsError(f"invalid_args: {where}.path is required")
            rows.append(
                {
                    "path": path,
                    "number": _text_or_none(item.get("number"), f"{where}.number"),
                    "title": _text_or_none(item.get("title"), f"{where}.title"),
                    "where": where,
                }
            )
    elif sheets is not None:
        if not isinstance(sheets, list):
            raise InvalidArgsError("invalid_args: sheets must be a list of file paths")
        for i, path in enumerate(sheets):
            if not isinstance(path, str) or not path.strip():
                raise InvalidArgsError(f"invalid_args: sheets[{i}] must be a file path")
            rows.append({"path": path, "number": None, "title": None, "where": f"sheets[{i}]"})
    if len(rows) > MAX_SHEETS:
        raise InvalidArgsError(
            f"invalid_args: pid_project reads at most {MAX_SHEETS} sheets (MAX_SHEETS); "
            f"{len(rows)} were given. Split the project into sets of {MAX_SHEETS} or fewer."
        )
    seen_numbers: dict[str, str] = {}
    seen_files: dict[str, str] = {}
    for row in rows:
        suffix = Path(row["path"]).suffix.lower()
        if suffix not in SHEET_SUFFIXES:
            raise InvalidArgsError(
                f"invalid_args: {row['where']}: {row['path']!r} is not a .dxf or .dwg sheet"
            )
        row["number"] = row["number"] or Path(row["path"]).stem
        if row["number"] == CURRENT_SHEET:
            raise InvalidArgsError(
                f"invalid_args: {row['where']}.number {CURRENT_SHEET!r} is reserved for the "
                "active document; number the sheet"
            )
        if row["number"] in seen_numbers:
            raise InvalidArgsError(
                f"invalid_args: {row['where']}.number {row['number']!r} repeats "
                f"{seen_numbers[row['number']]}"
            )
        seen_numbers[row["number"]] = row["where"]
        spelled = os.path.normcase(os.path.abspath(row["path"]))
        if spelled in seen_files:
            raise InvalidArgsError(
                f"invalid_args: {row['where']} names the file {seen_files[spelled]} already names"
            )
        seen_files[spelled] = row["where"]
    return project, rows


def _same_file(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# ── reading ──────────────────────────────────────────────────────────────────


def _size_check(path: str, label: str) -> None:
    limit = int(config.settings.max_dxf_bytes)
    size = os.path.getsize(path)
    if limit > 0 and size > limit:
        raise ValueError(
            f"pid_project: sheet {label} ({path}) is {size:,} bytes, over the MAX_DXF_BYTES "
            f"limit of {limit:,}. Raise MAX_DXF_BYTES (0 disables the check) to read it."
        )


def _load_dxf(path: str, label: str, sha: str):
    """The parsed DXF; refused when its bytes moved while it was parsed."""
    from backends.atomic_write import file_sha256
    from backends.dxf_recover import read_dxf

    _size_check(path, label)
    doc = read_dxf(path)
    if file_sha256(path) != sha:
        raise RuntimeError(
            f"pid_project: sheet {label} ({path}) changed while it was read; read the "
            "project again once it is saved"
        )
    return doc


async def _graph_of_doc(doc) -> dict:
    """``build_graph(scope="all")`` on a private, pathless, read-only backend."""
    from backends.ezdxf_backend import EzdxfBackend

    private = EzdxfBackend()
    await private.connect()
    try:
        # No path: the private registry entry can never be saved onto the sheet.
        private._register_document(doc, None)
        return await build_graph(private, scope="all")
    finally:
        await private.disconnect()


async def _read_file_sheet(backend: AutoCADBackend, path: str, label: str) -> tuple[dict, dict]:
    """``(graph, facts)`` for one sheet file; ``facts`` = sha256, cached, source."""
    from backends.atomic_write import file_sha256

    if not os.path.isfile(path):
        raise FileNotFoundError(f"pid_project: sheet {label}: no file at {path}")
    suffix = Path(path).suffix.lower()
    if suffix == ".dwg" and backend.name != "com":
        raise UnsupportedCapabilityError(
            "dwg",
            f"pid_project: sheet {label} ({path}) is a DWG, which the headless engine cannot "
            "read. Export it to DXF (DXFOUT or SAVEAS in AutoCAD) and list the .dxf, or run "
            "the server on the live engine (AUTOCAD_MCP_BACKEND=com).",
        )
    sha = await asyncio.to_thread(file_sha256, path)
    key = f"{suffix}:{sha}"
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key], {"sha256": sha, "cached": True, "source": "file"}
    if suffix == ".dwg":
        from engineering.understand.snapshot import _com_export

        written, folder, _source = await backend._run(_com_export, backend, path)
        try:
            await backend._ensure_document_state()
            exported_sha = await asyncio.to_thread(file_sha256, written)
            doc = await asyncio.to_thread(_load_dxf, written, label, exported_sha)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        source = "live_export"
    else:
        doc = await asyncio.to_thread(_load_dxf, path, label, sha)
        source = "file"
    graph = await _graph_of_doc(doc)
    _CACHE[key] = graph
    while len(_CACHE) > CACHE_SIZE:
        _CACHE.popitem(last=False)
    return graph, {"sha256": sha, "cached": False, "source": source}


# ── checks ───────────────────────────────────────────────────────────────────


def _direction(node: dict) -> str | None:
    name = str(node.get("block_name") or "").upper()
    if name.endswith("_OUT"):
        return "out"
    if name.endswith("_IN"):
        return "in"
    return None


def _line_numbers(node: dict, edges: dict) -> list[str]:
    numbers = []
    for port in (node.get("ports") or {}).values():
        for edge_id in port.get("edges") or ():
            number = (edges.get(edge_id) or {}).get("line_number")
            if number and str(number).strip() not in numbers:
                numbers.append(str(number).strip())
    return sorted(numbers)


def _connectors(sheets: list[tuple[str, dict]]) -> dict[str, list[dict]]:
    """LINK id -> every connector carrying it, over all sheets, in sheet order."""
    out: dict[str, list[dict]] = {}
    for number, graph in sheets:
        nodes = {n["id"]: n for n in graph["nodes"]}
        edges = {e["id"]: e for e in graph["edges"]}
        for link in graph.get("offpage_links") or ():
            for handle in link["nodes"]:
                node = nodes[handle]
                numbers = _line_numbers(node, edges)
                out.setdefault(str(link["link"]), []).append(
                    {
                        "sheet": number,
                        "handle": handle,
                        "direction": _direction(node),
                        "tag": node.get("tag"),
                        "line_number": ", ".join(numbers) or None,
                        "space": node.get("space"),
                    }
                )
    return out


def _link_status(connectors: list[dict]) -> str:
    if len(connectors) == 1:
        return "orphan"
    if len(connectors) > 2:
        return "duplicate"
    if sorted(str(c["direction"]) for c in connectors) == ["in", "out"]:
        return "matched"
    return "direction_mismatch"


def check_links(sheets: list[tuple[str, dict]]) -> list[dict]:
    """One row per LINK id: ``{link, status, connectors: [{sheet, handle, ...}]}``."""
    rows = []
    for link, connectors in sorted(_connectors(sheets).items(), key=lambda kv: natural_key(kv[0])):
        row = {"link": link, "status": _link_status(connectors), "connectors": connectors}
        if row["status"] == "matched" and connectors[0]["sheet"] == connectors[1]["sheet"]:
            row["same_sheet"] = True
        rows.append(row)
    return rows


def line_continuity(links: list[dict]) -> list[dict]:
    """For every matched pair: the line number on both sides, ``equal`` or not."""
    rows = []
    for row in links:
        if row["status"] != "matched":
            continue
        out = next(c for c in row["connectors"] if c["direction"] == "out")
        into = next(c for c in row["connectors"] if c["direction"] == "in")
        a, b = out["line_number"], into["line_number"]
        if not a or not b:
            status = "unknown"
        elif a.upper() == b.upper():
            status = "equal"
        else:
            status = "mismatch"
        side = ("sheet", "handle", "line_number")
        rows.append(
            {
                "link": row["link"],
                "status": status,
                "out": {k: out[k] for k in side},
                "in": {k: into[k] for k in side},
            }
        )
    return rows


def cross_sheet_duplicate_tags(sheets: list[tuple[str, dict]]) -> list[dict]:
    """A tag carried on two or more sheets; connectors and 0.3 guesses never count."""
    seen: dict[str, list[dict]] = {}
    for number, graph in sheets:
        for node in graph["nodes"]:
            tag = (node.get("tag") or "").strip()
            if not tag or node["kind"] in ("connector", "unknown_block"):
                continue
            seen.setdefault(tag.upper(), []).append(
                {
                    "sheet": number,
                    "handle": node["id"],
                    "kind": node["kind"],
                    "tag_source": node.get("tag_source"),
                }
            )
    rows = []
    for tag, occurrences in sorted(seen.items(), key=lambda kv: natural_key(kv[0])):
        on = list(dict.fromkeys(o["sheet"] for o in occurrences))
        if len(on) >= 2:
            rows.append({"tag": tag, "sheets": on, "occurrences": occurrences})
    return rows


def merged_index(sheets: list[tuple[str, dict]]) -> dict[str, list[dict]]:
    """The three deliverables over every sheet, each row led by ``sheet``."""
    out: dict[str, list[dict]] = {}
    for name, builder, key in (
        ("instrument_index", instrument_index, "tag"),
        ("line_list", line_list, "line_number"),
        ("equipment_list", equipment_list, "tag"),
    ):
        rows = [{"sheet": number, **row} for number, graph in sheets for row in builder(graph)]
        order = {number: i for i, (number, _graph) in enumerate(sheets)}
        rows.sort(key=lambda r, k=key: (natural_key(r[k]), order[r["sheet"]]))
        out[name] = rows
    return out


# ── writing ──────────────────────────────────────────────────────────────────

_SHEET_COLUMNS = (
    "sheet",
    "title",
    "path",
    "source",
    "sha256",
    "document_id",
    "revision",
    "nodes",
    "edges",
    "connectors",
    "cached",
)
_LINK_COLUMNS = ("link", "status", "sheet", "handle", "direction", "tag", "line_number")
_DUPLICATE_COLUMNS = ("tag", "sheet", "handle", "kind", "tag_source")
_CONTINUITY_COLUMNS = (
    "link",
    "status",
    "out_sheet",
    "out_handle",
    "out_line_number",
    "in_sheet",
    "in_handle",
    "in_line_number",
)
_TABLE_NUMBERS = NUMBER_COLUMNS | {"revision", "nodes", "edges", "connectors"}


def _tables(result: dict) -> list[tuple[str, tuple[str, ...], list[dict]]]:
    tables = [("sheets", _SHEET_COLUMNS, result["sheets"])]
    if result["action"] == "check":
        tables.append(
            (
                "links",
                _LINK_COLUMNS,
                [
                    {"link": row["link"], "status": row["status"], **c}
                    for row in result["links"]
                    for c in row["connectors"]
                ],
            )
        )
        tables.append(
            (
                "duplicate_tags",
                _DUPLICATE_COLUMNS,
                [
                    {"tag": row["tag"], **o}
                    for row in result["duplicate_tags"]
                    for o in row["occurrences"]
                ],
            )
        )
        tables.append(
            (
                "line_continuity",
                _CONTINUITY_COLUMNS,
                [
                    {
                        "link": row["link"],
                        "status": row["status"],
                        **{f"out_{k}": v for k, v in row["out"].items()},
                        **{f"in_{k}": v for k, v in row["in"].items()},
                    }
                    for row in result["line_continuity"]
                ],
            )
        )
    else:
        tables.append(("instrument_index", ("sheet", *INDEX_COLUMNS), result["instrument_index"]))
        tables.append(("line_list", ("sheet", *LINE_COLUMNS), result["line_list"]))
        tables.append(("equipment_list", ("sheet", *EQUIPMENT_COLUMNS), result["equipment_list"]))
    return tables


def _cell(value):
    return ", ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else value


def _write(result: dict, target: Path) -> dict:
    """Write ``result`` to ``target`` (``.json``/``.csv``/``.xlsx``) through a tmp file."""
    from engineering.export.cells import append_xlsx_row, write_csv

    suffix = target.suffix.lower()
    tmp = target.with_name(f"{target.name}.acadmcp-{os.getpid()}.tmp")
    try:
        if suffix == ".json":
            tmp.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        elif suffix == ".csv":
            columns: list[str] = ["table"]
            tables = _tables(result)
            for _name, cols, _rows in tables:
                columns.extend(c for c in cols if c not in columns)
            kinds = ["number" if c in _TABLE_NUMBERS else "text" for c in columns]
            body = (
                [name if c == "table" else _cell(row.get(c)) for c in columns]
                for name, _cols, rows in tables
                for row in rows
            )
            write_csv(tmp, columns, body, kinds)
        else:
            from openpyxl import Workbook

            book = Workbook()
            book.remove(book.active)
            for name, cols, rows in _tables(result):
                sheet = book.create_sheet(name[:31])
                append_xlsx_row(sheet, list(cols), ["text"] * len(cols))
                kinds = ["number" if c in _TABLE_NUMBERS else "text" for c in cols]
                for row in rows:
                    append_xlsx_row(sheet, [_cell(row.get(c)) for c in cols], kinds)
            book.save(tmp)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    return {"path": str(target), "format": suffix[1:], "bytes": target.stat().st_size}


def _write_target(write: str) -> Path:
    """Validated before anything is read; XLSX without openpyxl is refused here."""
    from engineering.sheet.extract import xlsx_available
    from security import validate_path

    target = validate_path(write, allow_write=True, kinds=WRITE_KINDS)
    if target.suffix.lower() == ".xlsx" and not xlsx_available():
        raise UnsupportedCapabilityError(
            "xlsx_write",
            "pid_project(write='*.xlsx') needs openpyxl, which is not installed. Install it "
            'with: pip install openpyxl (or pip install -e ".[office]"), or write a .csv or '
            ".json, which always work and carry the same rows.",
        )
    return target


# ── the tool ─────────────────────────────────────────────────────────────────


def _sheet_row(number, title, path, graph, facts) -> dict:
    connectors = sum(1 for n in graph["nodes"] if n["kind"] == "connector")
    row = {
        "sheet": number,
        "title": title,
        "path": path,
        "source": facts["source"],
        "sha256": facts.get("sha256"),
        "nodes": len(graph["nodes"]),
        "edges": len(graph["edges"]),
        "connectors": connectors,
        "confidence_min": graph["stats"]["confidence_min"],
        "cached": bool(facts.get("cached", False)),
    }
    for key in ("document_id", "revision", "note"):
        if facts.get(key) is not None:
            row[key] = facts[key]
    return row


async def run_project(
    backend: AutoCADBackend,
    action: str = "check",
    sheets: list[str] | None = None,
    manifest: dict | None = None,
    write: str | None = None,
    include_current: bool = True,
) -> dict:
    """Read the project's sheets and return the ``check`` or ``index`` result."""
    global _LAST
    if action not in ACTIONS:
        raise InvalidArgsError(
            f"invalid_args: action must be one of {', '.join(ACTIONS)}, got {action!r}"
        )
    project, listed = parse_sheets(sheets, manifest)
    current_identity = backend.document_identity() if include_current else None
    current_open = include_current and not (
        backend.name != "com" and current_identity.get("document_id") is None
    )
    current_path = (current_identity or {}).get("path")
    replaced = next((row for row in listed if _same_file(row["path"], current_path)), None)
    total = len(listed) + (1 if current_open and replaced is None else 0)
    if total > MAX_SHEETS:
        raise InvalidArgsError(
            f"invalid_args: pid_project reads at most {MAX_SHEETS} sheets (MAX_SHEETS); with the "
            f"active document this is {total}. Pass include_current=False or fewer sheets."
        )
    if total == 0:
        raise InvalidArgsError(
            "invalid_args: pid_project has no sheet to read: pass sheets or manifest, or open "
            "a drawing and keep include_current=True"
        )
    target = _write_target(write) if write is not None else None

    from security import validate_path

    for row in listed:
        row["resolved"] = str(validate_path(row["path"]))
        if not os.path.isfile(row["resolved"]):
            raise FileNotFoundError(
                f"pid_project: sheet {row['number']} ({row['where']}): no file at "
                f"{row['resolved']}; nothing was read"
            )

    graphs: list[tuple[str, dict]] = []
    rows: list[dict] = []
    if current_open:
        graph = await build_graph(backend, scope="all")
        # Read after the graph: the live engine binds its document at the first
        # call that touches one, so its path is known only now.
        identity = backend.document_identity()
        replaced = next(
            (row for row in listed if _same_file(row["resolved"], identity.get("path"))), None
        )
        facts = {
            "source": "active_document",
            "sha256": None,
            "document_id": identity.get("document_id"),
            "revision": identity.get("revision"),
            "cached": False,
        }
        if replaced is not None:
            number, title, path = replaced["number"], replaced["title"], replaced["resolved"]
            facts["note"] = "read from the open document (unsaved edits included), not the file"
        else:
            number, title, path = CURRENT_SHEET, identity.get("name"), identity.get("path")
        current = (number, _sheet_row(number, title, path, graph, facts), graph)
    else:
        current = None
    for row in listed:
        if replaced is row and current is not None:
            graphs.append((current[0], current[2]))
            rows.append(current[1])
            continue
        graph, facts = await _read_file_sheet(backend, row["resolved"], row["number"])
        graphs.append((row["number"], graph))
        rows.append(_sheet_row(row["number"], row["title"], row["resolved"], graph, facts))
    if current is not None and replaced is None:
        graphs.append((current[0], current[2]))
        rows.append(current[1])

    result: dict = {
        "ok": True,
        "action": action,
        "project": project,
        "sheets": rows,
        "backend": backend.name,
    }
    if action == "check":
        links = check_links(graphs)
        continuity = line_continuity(links)
        duplicates = cross_sheet_duplicate_tags(graphs)
        statuses = collections.Counter(row["status"] for row in links)
        lines = collections.Counter(row["status"] for row in continuity)
        result.update(
            links=links,
            duplicate_tags=duplicates,
            line_continuity=continuity,
            summary={
                "links": len(links),
                "matched": statuses["matched"],
                "orphan": statuses["orphan"],
                "direction_mismatch": statuses["direction_mismatch"],
                "duplicate": statuses["duplicate"],
                "duplicate_tags": len(duplicates),
                "line_mismatch": lines["mismatch"],
                "line_unknown": lines["unknown"],
            },
        )
        result["clean"] = (
            statuses["matched"] == len(links) and not duplicates and not lines["mismatch"]
        )
    else:
        merged = merged_index(graphs)
        result.update(merged)
        result["counts"] = {name: len(rows_) for name, rows_ in merged.items()}
    if target is not None:
        result["written"] = await asyncio.to_thread(_write, result, target)
    _LAST = copy.deepcopy(result)
    return result
