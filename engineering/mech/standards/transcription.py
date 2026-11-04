"""Double-entry transcription of the B40 feature tables (plan item 1.8-22).

DIN 471 / DIN 472 retaining-ring grooves, DIN 509 relief grooves, DIN 332-1
centre holes and ISO 3601-2 O-ring housings are **transcribed from the licensed
printed standard**, never derived, never recalled and never filled from a
secondary catalogue. This module is the pipeline that makes that checkable:

* every row is typed twice, independently, into
  ``engineering/mech/standards/data/<std>.a.json`` and ``<std>.b.json`` - two
  people, or one person on two days without looking at the first file;
* :func:`build_tables` parses both entries (every refusal names the file and the
  path inside it), demands they are equal, demands the two ``entered_by`` are
  named and different whenever a table has rows, demands a complete
  ``row_source`` (edition, table, page) whose edition is the one the module's
  SOURCE line names, and runs the physical invariants below;
* :func:`render` writes ``engineering/mech/standards/_rows.py``, the only place
  the table modules read rows from - so the Python dicts are *generated* from
  the double entry, and ``scripts/build_standards.py --check`` (plus the test
  of the same name) fails the moment the two disagree.

The invariants are physical sanity checks on transcribed values, never a source
of values: DIN 471 ``d2 < d1`` and ``m`` does not decrease with ``d1``; DIN 472
``d2 > d1``; DIN 509 a relief depth > 0 and a profile that runs monotonically
(built by :func:`engineering.mech.standards.undercuts.profile_from_dims`);
DIN 332-1 ``d2 > d1``, ``t > 0`` (form B ``d3 > d2``); ISO 3601-2 ``h < d2``
(cord) and ``b > d2``. A table whose licensed copy is not at hand stays empty
and is reported as deferred to :data:`DEFERRED_RELEASE` by name.

Maintainer procedure (one table)
--------------------------------
1. Open the licensed copy; note edition, table and page.
2. Entry A: fill ``row_source`` and ``rows`` of ``data/<std>.a.json`` and put
   your name in ``entered_by``.
3. Entry B: a second person (or you, on another day, without opening entry A)
   fills ``data/<std>.b.json`` the same way under their own ``entered_by``.
4. ``uv run --frozen python scripts/build_standards.py`` - every difference
   between A and B is listed by path; fix against the printed table, never by
   copying one file onto the other. On success ``_rows.py`` is rewritten.
5. ``uv run --frozen pytest tests/test_mech_standards_coverage.py`` and commit
   the two JSON files with the regenerated ``_rows.py``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
GENERATED_PATH = Path(__file__).resolve().parent / "_rows.py"

SCHEMA_VERSION = 1
ENTRIES: tuple[str, ...] = ("a", "b")

#: Where a table without a transcription from the licensed copy goes, by name.
DEFERRED_RELEASE = "1.8.1"

_TOP_KEYS = ("schema", "standard", "entry", "entered_by", "tables")
_TABLE_KEYS = ("row_source", "rows")
ROW_SOURCE_KEYS: tuple[str, ...] = ("edition", "table", "page")


class TranscriptionError(ValueError):
    """The double entry cannot be built; ``problems`` lists every reason by path."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__(
            "standards transcription refused:\n" + "\n".join(f"  - {p}" for p in self.problems)
        )


@dataclass(frozen=True)
class TableSpec:
    """One table inside a data file.

    ``key`` is its key under ``tables`` in the JSON, ``registry`` the name the
    standards registry knows it by, ``size`` what a row key measures,
    ``required`` / ``optional`` the numeric columns (every one finite and > 0),
    ``text`` optional free-text columns (a designation, for traceability), and
    ``edition`` the edition the module's SOURCE line names - a transcription
    from another edition is refused until SOURCE and the coverage locator are
    updated with it.
    """

    key: str
    registry: str
    size: str
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    text: tuple[str, ...] = ()
    edition: str = ""


@dataclass(frozen=True)
class DataFileSpec:
    stem: str
    standard: str
    module: str
    tables: tuple[TableSpec, ...]


DATA_FILES: tuple[DataFileSpec, ...] = (
    DataFileSpec(
        "din471",
        "DIN 471",
        "rings.py",
        (TableSpec("shaft", "DIN 471", "d1", ("m", "d2"), ("n",), (), "2011-04"),),
    ),
    DataFileSpec(
        "din472",
        "DIN 472",
        "rings.py",
        (TableSpec("bore", "DIN 472", "d1", ("m", "d2"), ("n",), (), "2011-04"),),
    ),
    DataFileSpec(
        "din509",
        "DIN 509",
        "undercuts.py",
        (
            TableSpec(
                "E", "DIN 509-E", "d", ("r", "t1", "f", "g"), ("t2",), ("designation",), "1998-06"
            ),
            TableSpec(
                "F", "DIN 509-F", "d", ("r", "t1", "f", "g", "t2"), (), ("designation",), "1998-06"
            ),
        ),
    ),
    DataFileSpec(
        "din332_1",
        "DIN 332-1",
        "centres.py",
        (
            TableSpec("A", "DIN 332-A", "d1", ("d2", "t"), (), (), "1986-04"),
            TableSpec("B", "DIN 332-B", "d1", ("d2", "d3", "t"), (), (), "1986-04"),
        ),
    ),
    DataFileSpec(
        "iso3601_2",
        "ISO 3601-2",
        "orings.py",
        (
            TableSpec("radial_static", "ISO 3601-2", "cord d2", ("b", "h"), (), (), "2016"),
            TableSpec("axial", "ISO 3601-2-axial", "cord d2", ("b", "h"), (), (), "2016"),
        ),
    ),
)

#: Every registry name this pipeline fills, in data-file order.
TABLE_NAMES: tuple[str, ...] = tuple(t.registry for f in DATA_FILES for t in f.tables)


def data_file(stem: str) -> DataFileSpec:
    for spec in DATA_FILES:
        if spec.stem == stem:
            return spec
    raise ValueError(f"standards transcription: unknown data file {stem!r}")


def table_spec(registry: str) -> tuple[DataFileSpec, TableSpec]:
    for spec in DATA_FILES:
        for table in spec.tables:
            if table.registry == registry:
                return spec, table
    raise ValueError(f"standards transcription: {registry!r} is not a B40 table")


def entry_path(stem: str, entry: str, data_dir: Path | None = None) -> Path:
    return Path(data_dir or DATA_DIR) / f"{stem}.{entry}.json"


def skeleton(stem: str, entry: str) -> dict:
    """The empty entry file for ``stem`` - exactly what the build ships today."""
    spec = data_file(stem)
    return {
        "schema": SCHEMA_VERSION,
        "standard": spec.standard,
        "entry": entry,
        "entered_by": None,
        "tables": {
            table.key: {"row_source": dict.fromkeys(ROW_SOURCE_KEYS), "rows": {}}
            for table in spec.tables
        },
    }


# ── parsing: every refusal names the file and the path inside it ─────────────


def _number(value, where: str, problems: list[str]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        problems.append(f"{where}: must be a number, got {value!r}")
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        problems.append(f"{where}: must be finite and > 0, got {value!r}")
        return None
    return number


def _size_key(text, where: str, problems: list[str]) -> float | None:
    try:
        number = float(text)
    except (TypeError, ValueError):
        problems.append(f"{where}: a row key must be a size in millimetres, got {text!r}")
        return None
    if not math.isfinite(number) or number <= 0.0:
        problems.append(f"{where}: a row key must be finite and > 0, got {text!r}")
        return None
    return number


def _keys(obj: dict, expected: tuple[str, ...], where: str, problems: list[str]) -> bool:
    if not isinstance(obj, dict):
        problems.append(f"{where}: must be an object, got {type(obj).__name__}")
        return False
    missing = [k for k in expected if k not in obj]
    unknown = sorted(set(obj) - set(expected))
    if missing:
        problems.append(f"{where}: missing {', '.join(missing)}")
    if unknown:
        problems.append(f"{where}: unknown {', '.join(unknown)}")
    return not missing and not unknown


def _row_source(obj, where: str, problems: list[str]) -> dict:
    if not _keys(obj, ROW_SOURCE_KEYS, where, problems):
        return dict.fromkeys(ROW_SOURCE_KEYS)
    out = {}
    for key in ROW_SOURCE_KEYS:
        value = obj[key]
        if value is None:
            out[key] = None
        elif isinstance(value, str) and value.strip():
            out[key] = value.strip()
        elif key == "page" and isinstance(value, int) and not isinstance(value, bool) and value > 0:
            out[key] = value
        else:
            problems.append(f"{where}.{key}: must be null or a non-empty text, got {value!r}")
            out[key] = None
    return out


def _row(obj, table: TableSpec, where: str, problems: list[str]) -> dict:
    if not isinstance(obj, dict):
        problems.append(f"{where}: a row must be an object, got {type(obj).__name__}")
        return {}
    allowed = (*table.required, *table.optional, *table.text)
    missing = [k for k in table.required if k not in obj]
    unknown = sorted(set(obj) - set(allowed))
    if missing:
        problems.append(f"{where}: missing {', '.join(missing)} ({table.registry} columns)")
    if unknown:
        problems.append(
            f"{where}: unknown {', '.join(unknown)}; {table.registry} takes {', '.join(allowed)}"
        )
    row: dict = {}
    for key in (*table.required, *table.optional):
        if key in obj:
            number = _number(obj[key], f"{where}.{key}", problems)
            if number is not None:
                row[key] = number
    for key in table.text:
        if key in obj:
            value = obj[key]
            if isinstance(value, str) and value.strip():
                row[key] = value.strip()
            else:
                problems.append(f"{where}.{key}: must be a non-empty text, got {value!r}")
    return row


def parse_entry(stem: str, entry: str, data_dir: Path | None = None) -> dict:
    """One entry file as ``{"entered_by", "tables": {registry: {...}}}``, or a refusal."""
    spec = data_file(stem)
    path = entry_path(stem, entry, data_dir)
    label = path.name
    problems: list[str] = []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TranscriptionError(
            [f"{label}: missing (both entries of {stem} must exist)"]
        ) from None
    except json.JSONDecodeError as exc:
        raise TranscriptionError([f"{label}: not JSON ({exc})"]) from None
    if not _keys(raw, _TOP_KEYS, label, problems):
        raise TranscriptionError(problems)
    if raw["schema"] != SCHEMA_VERSION:
        problems.append(f"{label}: schema must be {SCHEMA_VERSION}, got {raw['schema']!r}")
    if raw["standard"] != spec.standard:
        problems.append(f"{label}: standard must be {spec.standard!r}, got {raw['standard']!r}")
    if raw["entry"] != entry:
        problems.append(f"{label}: entry must be {entry!r}, got {raw['entry']!r}")
    who = raw["entered_by"]
    if who is not None and not (isinstance(who, str) and who.strip()):
        problems.append(f"{label}: entered_by must be null or a name, got {who!r}")
        who = None
    tables_raw = raw["tables"]
    if not _keys(tables_raw, tuple(t.key for t in spec.tables), f"{label}: tables", problems):
        raise TranscriptionError(problems)
    tables: dict[str, dict] = {}
    for table in spec.tables:
        where = f"{label}: tables.{table.key}"
        body = tables_raw[table.key]
        if not _keys(body, _TABLE_KEYS, where, problems):
            continue
        source = _row_source(body["row_source"], f"{where}.row_source", problems)
        rows_raw = body["rows"]
        rows: dict[float, dict] = {}
        if not isinstance(rows_raw, dict):
            problems.append(f"{where}.rows: must be an object keyed by {table.size}")
            rows_raw = {}
        for text, value in rows_raw.items():
            key = _size_key(text, f"{where}.rows.{text}", problems)
            if key is None:
                continue
            if key in rows:
                problems.append(f"{where}.rows.{text}: the size {key:g} is entered twice")
                continue
            rows[key] = _row(value, table, f"{where}.rows.{text}", problems)
        if rows:
            blank = [k for k in ROW_SOURCE_KEYS if source[k] is None]
            if blank:
                problems.append(
                    f"{where}.row_source: {', '.join(blank)} must be filled from the licensed "
                    "copy once the table has rows"
                )
            elif str(source["edition"]) != table.edition:
                problems.append(
                    f"{where}.row_source.edition: {source['edition']!r} is not the edition "
                    f"{table.edition!r} that {spec.module}'s SOURCE line names; update SOURCE, "
                    "the coverage locator and TableSpec.edition together, or transcribe from "
                    f"the {table.edition} copy"
                )
        tables[table.registry] = {"row_source": source, "rows": dict(sorted(rows.items()))}
    if problems:
        raise TranscriptionError(problems)
    return {"entered_by": who.strip() if isinstance(who, str) else None, "tables": tables}


def compare_entries(stem: str, a: dict, b: dict) -> list[str]:
    """Every place entry A and entry B disagree, by path. ``entered_by`` is not compared."""
    spec = data_file(stem)
    out: list[str] = []
    for table in spec.tables:
        ta, tb = a["tables"][table.registry], b["tables"][table.registry]
        where = f"{stem}: tables.{table.key}"
        for key in ROW_SOURCE_KEYS:
            if ta["row_source"][key] != tb["row_source"][key]:
                out.append(
                    f"{where}.row_source.{key}: entry a {ta['row_source'][key]!r} != "
                    f"entry b {tb['row_source'][key]!r}"
                )
        for size in sorted(set(ta["rows"]) | set(tb["rows"])):
            label = f"{where}.rows.{size:g}"
            if size not in tb["rows"]:
                out.append(f"{label}: only in entry a")
            elif size not in ta["rows"]:
                out.append(f"{label}: only in entry b")
            else:
                ra, rb = ta["rows"][size], tb["rows"][size]
                for column in sorted(set(ra) | set(rb)):
                    if ra.get(column) != rb.get(column):
                        out.append(
                            f"{label}.{column}: entry a {ra.get(column)!r} != "
                            f"entry b {rb.get(column)!r}"
                        )
    return out


# ── physical invariants: sanity checks on transcribed values, never a source ──


def _invariants_471(rows: dict) -> list[str]:
    out = [
        f"d1 {d1:g}: d2 {row['d2']:g} is not below d1 (a shaft groove)"
        for d1, row in rows.items()
        if not row["d2"] < d1
    ]
    previous = None
    for d1, row in sorted(rows.items()):
        if previous is not None and row["m"] < previous[1]:
            out.append(
                f"d1 {d1:g}: m {row['m']:g} is smaller than m {previous[1]:g} at d1 "
                f"{previous[0]:g} (m does not decrease with d1)"
            )
        previous = (d1, row["m"])
    return out


def _invariants_472(rows: dict) -> list[str]:
    return [
        f"d1 {d1:g}: d2 {row['d2']:g} is not above d1 (a bore groove)"
        for d1, row in rows.items()
        if not row["d2"] > d1
    ]


def _invariants_509(form: str):
    def check(rows: dict) -> list[str]:
        from engineering.mech.standards.undercuts import profile_from_dims

        out = []
        for d, row in rows.items():
            depth = max(row["t1"], row.get("t2") or 0.0)
            if not depth > 0.0:
                out.append(f"d {d:g}: the relief depth is not > 0")
            if row["t1"] >= d / 2.0:
                out.append(f"d {d:g}: t1 {row['t1']:g} reaches the axis")
            try:
                profile = profile_from_dims(
                    form, r=row["r"], t1=row["t1"], f=row["f"], g=row["g"], t2=row.get("t2")
                )
            except ValueError as exc:
                out.append(f"d {d:g}: {exc}")
                continue
            alongs = [a for a, _ in profile]
            if any(b < a for a, b in zip(alongs, alongs[1:], strict=False)):
                out.append(f"d {d:g}: the profile does not run monotonically")
            if any(depth < 0.0 for _, depth in profile):
                out.append(f"d {d:g}: the profile leaves the cylinder")
        return out

    return check


def _invariants_332(form: str):
    def check(rows: dict) -> list[str]:
        out = []
        for d1, row in rows.items():
            if not row["d2"] > d1:
                out.append(f"d1 {d1:g}: d2 {row['d2']:g} is not above d1")
            if not row["t"] > 0.0:
                out.append(f"d1 {d1:g}: t is not > 0")
            if form == "B" and not row["d3"] > row["d2"]:
                out.append(f"d1 {d1:g}: d3 {row['d3']:g} is not above d2 {row['d2']:g}")
        return out

    return check


def _invariants_oring(rows: dict) -> list[str]:
    out = []
    for cord, row in rows.items():
        if not row["h"] < cord:
            out.append(f"cord {cord:g}: h {row['h']:g} is not below the cord (no squeeze)")
        if not row["b"] > cord:
            out.append(f"cord {cord:g}: b {row['b']:g} is not wider than the cord")
    return out


INVARIANTS = {
    "DIN 471": _invariants_471,
    "DIN 472": _invariants_472,
    "DIN 509-E": _invariants_509("E"),
    "DIN 509-F": _invariants_509("F"),
    "DIN 332-A": _invariants_332("A"),
    "DIN 332-B": _invariants_332("B"),
    "ISO 3601-2": _invariants_oring,
    "ISO 3601-2-axial": _invariants_oring,
}


def invariant_violations(registry: str, rows: dict) -> list[str]:
    """The physical invariants of ``registry`` over ``rows`` (``{size: row}``)."""
    if not rows:
        return []
    return [f"{registry}: {problem}" for problem in INVARIANTS[registry](rows)]


# ── build and render ─────────────────────────────────────────────────────────


def build_tables(data_dir: Path | None = None) -> dict[str, dict]:
    """``{registry: {data_file, row_source, transcribed_by, checked_by, rows}}``.

    Refused with :class:`TranscriptionError` listing every problem: a parse
    error, a disagreement between the entries, a filled table without two
    different named people, or a broken invariant.
    """
    problems: list[str] = []
    out: dict[str, dict] = {}
    for spec in DATA_FILES:
        entries = {}
        for entry in ENTRIES:
            try:
                entries[entry] = parse_entry(spec.stem, entry, data_dir)
            except TranscriptionError as exc:
                problems.extend(exc.problems)
        if len(entries) != len(ENTRIES):
            continue
        a, b = entries["a"], entries["b"]
        differences = compare_entries(spec.stem, a, b)
        problems.extend(differences)
        filled = any(a["tables"][t.registry]["rows"] for t in spec.tables)
        if filled:
            if not a["entered_by"] or not b["entered_by"]:
                problems.append(
                    f"{spec.stem}: entered_by must name the person behind each entry once a "
                    "table has rows"
                )
            elif a["entered_by"].casefold() == b["entered_by"].casefold():
                problems.append(
                    f"{spec.stem}: both entries are by {a['entered_by']!r}; the double entry "
                    "needs two independent entries"
                )
        if differences:
            continue
        for table in spec.tables:
            rows = a["tables"][table.registry]["rows"]
            problems.extend(invariant_violations(table.registry, rows))
            out[table.registry] = {
                "data_file": spec.stem,
                "row_source": dict(a["tables"][table.registry]["row_source"]),
                "transcribed_by": a["entered_by"] if rows else None,
                "checked_by": b["entered_by"] if rows else None,
                "rows": rows,
            }
    if problems:
        raise TranscriptionError(problems)
    return out


def _literal(value, indent: int) -> str:
    pad = "    " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        for key, item in value.items():
            lines.append(f"{pad}    {_literal(key, 0)}: {_literal(item, indent + 1)},")
        lines.append(f"{pad}}}")
        return "\n".join(lines)
    if value is None or isinstance(value, bool):
        return repr(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError(f"render: cannot write {value!r}")


_HEADER = '''"""GENERATED by scripts/build_standards.py from engineering/mech/standards/data/*.json.

Do not edit. Change both entries of the data file (see the docstring of
``engineering/mech/standards/transcription.py``), then run
``uv run --frozen python scripts/build_standards.py``; ``--check`` fails while
this file and the double entry disagree.
"""

from __future__ import annotations

'''


def render(tables: dict[str, dict]) -> str:
    """The text of ``_rows.py`` for ``tables`` - deterministic and ruff-format stable."""
    ordered = {name: tables[name] for name in sorted(tables)}
    body = {
        name: {
            "data_file": table["data_file"],
            "row_source": {k: table["row_source"].get(k) for k in ROW_SOURCE_KEYS},
            "transcribed_by": table["transcribed_by"],
            "checked_by": table["checked_by"],
            "rows": {float(k): dict(v) for k, v in sorted(table["rows"].items())},
        }
        for name, table in ordered.items()
    }
    return _HEADER + "TABLES: dict[str, dict] = " + _literal(body, 0) + "\n"


def check(data_dir: Path | None = None, generated: Path | None = None) -> list[str]:
    """Every reason ``--check`` fails: a refused build, or ``_rows.py`` out of date."""
    try:
        tables = build_tables(data_dir)
    except TranscriptionError as exc:
        return list(exc.problems)
    path = Path(generated or GENERATED_PATH)
    current = path.read_text(encoding="utf-8") if path.is_file() else None
    if current != render(tables):
        return [
            f"{path.name} is not the rendering of the double entry; run "
            "scripts/build_standards.py (never edit it by hand)"
        ]
    return []


def deferred_tables(tables: dict[str, dict]) -> tuple[str, ...]:
    """Registry names with no transcribed row - deferred to :data:`DEFERRED_RELEASE`."""
    return tuple(name for name in TABLE_NAMES if not tables.get(name, {}).get("rows"))
