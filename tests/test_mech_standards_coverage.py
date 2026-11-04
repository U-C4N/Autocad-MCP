"""The B40 feature tables (plan item 1.8-22): double entry, generated rows, invariants.

DIN 471 / DIN 472, DIN 509 (forms E and F), DIN 332-1 (forms A and B) and
ISO 3601-2 (radial static and axial housings) are transcribed from the licensed
printed standard and nothing else. Their rows live in two independent entries,
``engineering/mech/standards/data/<std>.a.json`` and ``.b.json``, and the
Python dicts the modules read are generated from them into ``_rows.py`` by
``scripts/build_standards.py``. This build ships **no row** of any of them -
the licensed copies were not at hand - so every lookup is refused by name and
every table is reported deferred to 1.8.1.

What is pinned here works for whatever rows exist, so the same tests hold the
day a table is filled: both entries agree, ``_rows.py`` is their rendering, a
filled table names its two people and its edition / table / page, the physical
invariants hold, an in-coverage size resolves and a size between two rows is
refused by name - never interpolated. The pipeline proves itself on a tiny
SYNTHETIC double entry built in ``tmp_path``: its round numbers are not DIN or
ISO values and never enter the shipped data.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest

import engineering.mech.standards as standards_mod
from engineering.mech.standards import (
    coverage_from_rows,
    register,
    row_count,
    standards,
)
from engineering.mech.standards import transcription as tr
from engineering.mech.standards._rows import TABLES
from engineering.mech.standards.centres import (
    CENTRE_ROW_KEYS,
    ROWS_A,
    ROWS_B,
    centre_hole_dims,
)
from engineering.mech.standards.coverage import coverage_manifest
from engineering.mech.standards.feature_tables import (
    deferred_message,
    deferred_tables,
    filled_tables,
    probe,
)
from engineering.mech.standards.orings import (
    COVERAGE_BY_KIND,
    KINDS,
    ORING_ROW_KEYS,
    ROWS_AXIAL,
    ROWS_RADIAL_STATIC,
    TABLE_BY_KIND,
    oring_groove_dims,
)
from engineering.mech.standards.rings import (
    RING_ROW_KEYS,
    ROWS_471,
    ROWS_472,
    ring_groove_dims,
)
from engineering.mech.standards.undercuts import (
    ROWS_E,
    ROWS_F,
    UNDERCUT_ROW_KEYS,
    profile_from_dims,
    undercut_profile,
)

ROOT = Path(__file__).resolve().parents[1]

B40 = (
    "DIN 471",
    "DIN 472",
    "DIN 509-E",
    "DIN 509-F",
    "DIN 332-A",
    "DIN 332-B",
    "ISO 3601-2",
    "ISO 3601-2-axial",
)


def _build_script():
    spec = importlib.util.spec_from_file_location(
        "build_standards", ROOT / "scripts" / "build_standards.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── (1) the shipped data: two entries, one rendering ─────────────────────────


def test_every_b40_table_is_registered_and_filled_only_through_the_pipeline():
    assert tr.TABLE_NAMES == B40
    assert set(B40) <= set(standards())
    assert {spec.stem for spec in tr.DATA_FILES} == {
        "din471",
        "din472",
        "din509",
        "din332_1",
        "iso3601_2",
    }


@pytest.mark.parametrize("stem", [spec.stem for spec in tr.DATA_FILES])
def test_both_entries_exist_parse_and_agree(stem):
    a = tr.parse_entry(stem, "a")
    b = tr.parse_entry(stem, "b")
    assert tr.compare_entries(stem, a, b) == []


def test_the_generated_rows_are_the_rendering_of_the_double_entry():
    tables = tr.build_tables()
    assert tr.GENERATED_PATH.read_text(encoding="utf-8") == tr.render(tables)
    assert tr.check() == []
    assert TABLES == tables


def test_build_standards_check_passes_on_the_committed_tree(capsys):
    assert _build_script().main(["--check"]) == 0
    out = capsys.readouterr().out
    for name in deferred_tables():
        assert name in out
    if deferred_tables():
        assert "deferred to 1.8.1" in out


def test_the_modules_read_their_rows_only_from_the_generated_tables():
    assert ROWS_471 == TABLES["DIN 471"]["rows"]
    assert ROWS_472 == TABLES["DIN 472"]["rows"]
    assert ROWS_RADIAL_STATIC == TABLES["ISO 3601-2"]["rows"]
    assert ROWS_AXIAL == TABLES["ISO 3601-2-axial"]["rows"]
    for rows, name in ((ROWS_A, "DIN 332-A"), (ROWS_B, "DIN 332-B")):
        assert rows == {k: {"d1": k, **v} for k, v in TABLES[name]["rows"].items()}
    for rows, name in ((ROWS_E, "DIN 509-E"), (ROWS_F, "DIN 509-F")):
        assert {k: {c: v for c, v in row.items() if c != "profile"} for k, row in rows.items()} == (
            TABLES[name]["rows"]
        )
    for name in B40:
        assert row_count(name) == len(TABLES[name]["rows"])


@pytest.mark.parametrize("name", B40)
def test_a_filled_table_names_its_source_and_two_people_an_empty_one_is_deferred(name):
    table = TABLES[name]
    record = {row["standard"]: row for row in coverage_manifest()["tables"]}[name]
    if table["rows"]:
        assert all(table["row_source"][k] for k in tr.ROW_SOURCE_KEYS)
        assert table["transcribed_by"] and table["checked_by"]
        assert table["transcribed_by"].casefold() != table["checked_by"].casefold()
        assert record["coverage"]["status"] == "transcribed"
        assert (record["transcriber"], record["reviewer"]) == (
            table["transcribed_by"],
            table["checked_by"],
        )
        assert name not in deferred_tables()
    else:
        assert table["transcribed_by"] is None and table["checked_by"] is None
        assert record["coverage"]["status"] == "no rows transcribed"
        assert record["transcriber"] is None and record["reviewer"] is None
        assert name in deferred_tables()
        assert name in deferred_message() and "1.8.1" in deferred_message()


@pytest.mark.parametrize("name", B40)
def test_the_shipped_rows_hold_the_physical_invariants(name):
    """Vacuous on an empty table; binding the moment a row lands."""
    assert tr.invariant_violations(name, TABLES[name]["rows"]) == []


@pytest.mark.parametrize(
    ("rows", "keys"),
    [
        (ROWS_E, ("profile", "r", "t1", "f", "g")),
        (ROWS_F, ("profile", "r", "t1", "f", "g", "t2")),
        (ROWS_471, ("m", "d2")),
        (ROWS_472, ("m", "d2")),
        (ROWS_A, ("d1", "d2", "t")),
        (ROWS_B, ("d1", "d2", "d3", "t")),
        (ROWS_RADIAL_STATIC, ("b", "h")),
        (ROWS_AXIAL, ("b", "h")),
    ],
)
def test_every_shipped_row_carries_every_key_the_drawing_code_reads(rows, keys):
    for size, row in rows.items():
        missing = [key for key in keys if key not in row]
        assert not missing, f"row {size} is missing {missing}"


def test_the_row_key_contracts_are_the_ones_the_drawing_code_reads():
    assert UNDERCUT_ROW_KEYS == ("profile",)
    assert RING_ROW_KEYS == ("m", "d2")
    assert CENTRE_ROW_KEYS == ("d1", "d2", "d3", "t")
    assert ORING_ROW_KEYS == ("b", "h")
    assert KINDS == ("radial", "axial")
    assert TABLE_BY_KIND == {"radial": "ISO 3601-2", "axial": "ISO 3601-2-axial"}
    assert set(COVERAGE_BY_KIND) == set(KINDS)


# ── (2) an empty table refuses every size by name ────────────────────────────

_LOOKUPS = [
    ("DIN 471", lambda: ring_groove_dims("DIN 471", 30.0), ("DIN 471", "din471", "rings.py")),
    ("DIN 472", lambda: ring_groove_dims("DIN 472", 30.0), ("DIN 472", "din472", "rings.py")),
    ("DIN 509-E", lambda: undercut_profile("E", 40.0), ("form E", "din509", "undercuts.py")),
    ("DIN 509-F", lambda: undercut_profile("F", 40.0), ("form F", "din509", "undercuts.py")),
    ("DIN 332-A", lambda: centre_hole_dims("A", 2.5), ("form A", "din332_1", "centres.py")),
    ("DIN 332-B", lambda: centre_hole_dims("B", 2.5), ("form B", "din332_1", "centres.py")),
    ("ISO 3601-2", lambda: oring_groove_dims(3.53), ("ISO 3601-2", "iso3601_2", "orings.py")),
    (
        "ISO 3601-2-axial",
        lambda: oring_groove_dims(3.53, "axial"),
        ("ISO 3601-2-axial", "iso3601_2", "orings.py"),
    ),
]


@pytest.mark.parametrize(("name", "call", "words"), _LOOKUPS, ids=[row[0] for row in _LOOKUPS])
def test_an_empty_table_refuses_by_name_and_says_what_to_transcribe(name, call, words):
    if row_count(name):
        pytest.skip(f"{name} has transcribed rows; the resolution tests below cover it")
    with pytest.raises(ValueError) as excinfo:
        call()
    message = str(excinfo.value)
    for word in words:
        assert word in message
    assert "no rows are transcribed" in message
    assert "1.8.1" in message
    assert "Nothing is interpolated" in message


def test_unknown_forms_standards_and_kinds_are_refused_before_any_table():
    with pytest.raises(ValueError, match="forms E and F"):
        undercut_profile("G", 40.0)
    with pytest.raises(ValueError, match="DIN 471 or DIN 472"):
        ring_groove_dims("DIN 6799", 30.0)
    with pytest.raises(ValueError, match="'radial' or 'axial'"):
        oring_groove_dims(3.53, "dynamic")
    with pytest.raises(ValueError, match="form A or B"):
        centre_hole_dims("C", 2.5)


def test_centre_hole_form_r_is_refused_outright_not_approximated():
    with pytest.raises(ValueError) as excinfo:
        centre_hole_dims("R", 2.5)
    assert "form R" in str(excinfo.value)
    assert "approximated" in str(excinfo.value)


# ── (3) the DIN 509 profile: a documented construction from r, t1, f, g, t2 ──


def _arc_centre(start, r, t1):
    run = math.sqrt(2.0 * r * t1 - t1 * t1)
    return start + run, t1 - r


def test_form_e_profile_leaves_the_shoulder_line_rounds_into_the_floor_and_runs_out():
    r, t1, f, g = 1.0, 0.4, 5.0, 2.0
    profile = profile_from_dims("E", r=r, t1=t1, f=f, g=g)
    assert profile[0] == (0.0, 0.0)
    assert profile[-1] == (f, 0.0)
    assert profile[-2] == (f - g, t1)  # the floor ends where the run-out of length g starts
    cx, cz = _arc_centre(0.0, r, t1)
    arc = profile[: tr_arc_points()]
    for along, depth in arc:
        assert math.hypot(along - cx, depth - cz) == pytest.approx(r)
    assert arc[-1] == pytest.approx((cx, t1))  # tangent to the floor at its lowest point
    assert max(depth for _, depth in profile) == pytest.approx(t1)
    alongs = [a for a, _ in profile]
    assert alongs == sorted(alongs)
    assert all(depth >= 0.0 for _, depth in profile)


def tr_arc_points() -> int:
    from engineering.mech.standards.undercuts import ARC_SEGMENTS

    return ARC_SEGMENTS + 1


def test_form_f_profile_starts_t2_into_the_shoulder_face():
    r, t1, f, g, t2 = 1.0, 0.5, 4.0, 2.0, 0.3
    profile = profile_from_dims("F", r=r, t1=t1, f=f, g=g, t2=t2)
    assert profile[0] == (-t2, 0.0)
    assert profile[-1] == (f, 0.0)
    cx, cz = _arc_centre(-t2, r, t1)
    for along, depth in profile[: tr_arc_points()]:
        assert math.hypot(along - cx, depth - cz) == pytest.approx(r)
    assert len(profile_from_dims("F", r=r, t1=t1, f=f, g=g, t2=t2, segments=3)) == 3 + 1 + 2


def test_a_radius_equal_to_the_depth_meets_the_shoulder_line_square():
    profile = profile_from_dims("E", r=0.5, t1=0.5, f=3.0, g=1.0)
    assert profile[0] == (0.0, 0.0)
    assert profile[tr_arc_points() - 1] == pytest.approx((0.5, 0.5))


@pytest.mark.parametrize(
    ("form", "dims", "match"),
    [
        ("E", {"r": 0.4, "t1": 0.6, "f": 3.0, "g": 1.0}, "needs t1 <= r"),
        ("E", {"r": 2.0, "t1": 0.5, "f": 2.0, "g": 1.5}, "past f - g"),
        ("E", {"r": 1.0, "t1": 0.5, "f": 4.0, "g": 2.0, "t2": 0.2}, "relieves the cylinder only"),
        ("F", {"r": 1.0, "t1": 0.5, "f": 4.0, "g": 2.0}, "t2 .* is required"),
        ("E", {"r": 0.0, "t1": 0.5, "f": 4.0, "g": 2.0}, "r must be finite and > 0"),
        ("E", {"r": True, "t1": 0.5, "f": 4.0, "g": 2.0}, "r must be a number"),
        ("F", {"r": 1.0, "t1": 0.5, "f": 4.0, "g": 2.0, "t2": -0.1}, "t2 must be finite"),
        ("G", {"r": 1.0, "t1": 0.5, "f": 4.0, "g": 2.0}, "forms E and F"),
    ],
)
def test_a_row_the_construction_cannot_draw_is_refused_not_bent_to_fit(form, dims, match):
    with pytest.raises(ValueError, match=match):
        profile_from_dims(form, **dims)


# ── (4) the pipeline on a SYNTHETIC double entry (not DIN / ISO values) ──────

#: Round numbers chosen to satisfy the invariants - a fixture, never data.
SYNTHETIC = {
    "din471": {"shaft": {"10": {"m": 1.0, "d2": 9.0}, "20": {"m": 1.5, "d2": 18.5}}},
    "din509": {"F": {"30": {"r": 1.0, "t1": 0.5, "f": 4.0, "g": 2.0, "t2": 0.3}}},
    "din332_1": {"B": {"2": {"d2": 4.0, "d3": 5.0, "t": 4.0}}},
    "iso3601_2": {"radial_static": {"2": {"b": 3.0, "h": 1.5}, "3": {"b": 4.0, "h": 2.4}}},
}


def _write(folder: Path, stem: str, entry: str, data: dict) -> None:
    tr.entry_path(stem, entry, folder).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _entry(stem: str, entry: str, who: str | None) -> dict:
    data = tr.skeleton(stem, entry)
    spec = tr.data_file(stem)
    for table_key, rows in SYNTHETIC.get(stem, {}).items():
        edition = next(t.edition for t in spec.tables if t.key == table_key)
        data["tables"][table_key] = {
            "row_source": {"edition": edition, "table": "SYNTHETIC fixture", "page": "0"},
            "rows": json.loads(json.dumps(rows)),
        }
    data["entered_by"] = who if stem in SYNTHETIC else None
    return data


@pytest.fixture
def synthetic_dir(tmp_path) -> Path:
    folder = tmp_path / "data"
    folder.mkdir()
    for spec in tr.DATA_FILES:
        _write(folder, spec.stem, "a", _entry(spec.stem, "a", "fixture-a"))
        _write(folder, spec.stem, "b", _entry(spec.stem, "b", "fixture-b"))
    return folder


def _edit(folder: Path, stem: str, entry: str, change) -> None:
    path = tr.entry_path(stem, entry, folder)
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def _problems(folder: Path) -> list[str]:
    with pytest.raises(tr.TranscriptionError) as excinfo:
        tr.build_tables(folder)
    return excinfo.value.problems


def test_the_pipeline_generates_the_rows_and_their_provenance_from_the_entries(synthetic_dir):
    tables = tr.build_tables(synthetic_dir)
    assert tables["DIN 471"]["rows"] == {10.0: {"m": 1.0, "d2": 9.0}, 20.0: {"m": 1.5, "d2": 18.5}}
    assert tables["DIN 471"]["transcribed_by"] == "fixture-a"
    assert tables["DIN 471"]["checked_by"] == "fixture-b"
    assert tables["DIN 471"]["row_source"] == {
        "edition": "2011-04",
        "table": "SYNTHETIC fixture",
        "page": "0",
    }
    assert tables["DIN 472"]["rows"] == {} and tables["DIN 472"]["transcribed_by"] is None
    assert tr.deferred_tables(tables) == ("DIN 472", "DIN 509-E", "DIN 332-A", "ISO 3601-2-axial")
    namespace: dict = {}
    exec(compile(tr.render(tables), "_rows.py", "exec"), namespace)
    assert namespace["TABLES"] == tables


def test_the_build_script_writes_and_checks_the_generated_module(synthetic_dir, capsys):
    build = _build_script()
    out = synthetic_dir.parent / "_rows.py"
    args = ["--data-dir", str(synthetic_dir), "--out", str(out)]
    assert build.main(args) == 0
    assert build.main([*args, "--check"]) == 0
    assert "DIN 471 (2 rows)" in capsys.readouterr().out
    out.write_text(out.read_text(encoding="utf-8").replace("1.5", "1.6"), encoding="utf-8")
    assert build.main([*args, "--check"]) == 1  # a hand edit of the generated module
    assert build.main(args) == 0
    _edit(synthetic_dir, "din471", "b", lambda d: d["tables"]["shaft"]["rows"]["10"].update(m=1.1))
    assert build.main([*args, "--check"]) == 1  # the entries disagree
    assert "entry a 1.0 != entry b 1.1" in capsys.readouterr().out


def test_every_disagreement_between_the_entries_is_refused_by_path(synthetic_dir):
    def change(data):
        rows = data["tables"]["shaft"]["rows"]
        rows["10"]["d2"] = 9.1
        rows["15"] = {"m": 1.2, "d2": 14.0}
        data["tables"]["shaft"]["row_source"]["page"] = "1"

    _edit(synthetic_dir, "din471", "b", change)
    problems = _problems(synthetic_dir)
    assert "din471: tables.shaft.rows.10.d2: entry a 9.0 != entry b 9.1" in problems
    assert "din471: tables.shaft.rows.15: only in entry b" in problems
    assert "din471: tables.shaft.row_source.page: entry a '0' != entry b '1'" in problems


def test_one_person_twice_or_nobody_is_not_a_double_entry(synthetic_dir):
    _edit(synthetic_dir, "din471", "b", lambda d: d.update(entered_by="FIXTURE-A"))
    assert any("two independent entries" in p for p in _problems(synthetic_dir))
    _edit(synthetic_dir, "din471", "b", lambda d: d.update(entered_by=None))
    assert any("entered_by must name the person" in p for p in _problems(synthetic_dir))


def test_a_filled_table_needs_its_row_source_and_the_edition_its_source_names(synthetic_dir):
    def blank(data):
        data["tables"]["shaft"]["row_source"]["page"] = None

    _edit(synthetic_dir, "din471", "a", blank)
    _edit(synthetic_dir, "din471", "b", blank)
    assert any("page must be filled from the licensed copy" in p for p in _problems(synthetic_dir))

    def other_edition(data):
        data["tables"]["shaft"]["row_source"].update(edition="1988-08", page="0")

    _edit(synthetic_dir, "din471", "a", other_edition)
    _edit(synthetic_dir, "din471", "b", other_edition)
    assert any("is not the edition '2011-04'" in p for p in _problems(synthetic_dir))


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        (lambda r: r["10"].update(x=1.0), "rows.10: unknown x"),
        (lambda r: r["10"].pop("m"), "rows.10: missing m"),
        (lambda r: r["10"].update(m=0.0), "rows.10.m: must be finite and > 0"),
        (lambda r: r["10"].update(m=True), "rows.10.m: must be a number"),
        (lambda r: r.update({"10.0": {"m": 1.0, "d2": 9.0}}), "the size 10 is entered twice"),
        (lambda r: r.update({"M10": {"m": 1.0, "d2": 9.0}}), "a row key must be a size"),
    ],
)
def test_a_malformed_entry_is_refused_naming_the_file_and_path(synthetic_dir, change, fragment):
    for entry in ("a", "b"):
        _edit(synthetic_dir, "din471", entry, lambda d: change(d["tables"]["shaft"]["rows"]))
    problems = _problems(synthetic_dir)
    assert any(p.startswith("din471.a.json: tables.shaft.") and fragment in p for p in problems)


def test_a_missing_or_mislabelled_entry_file_is_refused(synthetic_dir):
    _edit(synthetic_dir, "din472", "a", lambda d: d.update(entry="b"))
    _edit(synthetic_dir, "din509", "b", lambda d: d.update(standard="DIN 510"))
    tr.entry_path("din332_1", "b", synthetic_dir).unlink()
    problems = _problems(synthetic_dir)
    assert "din472.a.json: entry must be 'a', got 'b'" in problems
    assert "din509.b.json: standard must be 'DIN 509', got 'DIN 510'" in problems
    assert any(p.startswith("din332_1.b.json: missing") for p in problems)


# ── (5) the physical invariants (sanity checks, never a source of values) ────


@pytest.mark.parametrize(
    ("name", "rows", "fragment"),
    [
        ("DIN 471", {10.0: {"m": 1.0, "d2": 10.0}}, "is not below d1"),
        (
            "DIN 471",
            {10.0: {"m": 1.5, "d2": 9.0}, 20.0: {"m": 1.0, "d2": 18.0}},
            "m does not decrease with d1",
        ),
        ("DIN 472", {10.0: {"m": 1.0, "d2": 10.0}}, "is not above d1"),
        ("DIN 509-E", {30.0: {"r": 0.4, "t1": 0.6, "f": 3.0, "g": 1.0}}, "needs t1 <= r"),
        ("DIN 509-F", {30.0: {"r": 2.0, "t1": 0.5, "f": 2.0, "g": 1.5, "t2": 0.1}}, "past f - g"),
        ("DIN 509-E", {1.0: {"r": 1.0, "t1": 0.5, "f": 4.0, "g": 2.0}}, "reaches the axis"),
        ("DIN 332-A", {2.0: {"d2": 2.0, "t": 4.0}}, "is not above d1"),
        ("DIN 332-B", {2.0: {"d2": 4.0, "d3": 4.0, "t": 4.0}}, "d3 4 is not above d2 4"),
        ("ISO 3601-2", {2.0: {"b": 3.0, "h": 2.0}}, "is not below the cord"),
        ("ISO 3601-2-axial", {2.0: {"b": 2.0, "h": 1.5}}, "is not wider than the cord"),
    ],
)
def test_each_invariant_names_the_row_that_breaks_it(name, rows, fragment):
    problems = tr.invariant_violations(name, rows)
    assert any(fragment in p for p in problems), problems
    assert all(p.startswith(f"{name}: ") for p in problems)


def test_the_synthetic_rows_hold_every_invariant_and_a_broken_one_blocks_the_build(
    synthetic_dir,
):
    for name, table in tr.build_tables(synthetic_dir).items():
        assert tr.invariant_violations(name, table["rows"]) == []
    for entry in ("a", "b"):
        _edit(
            synthetic_dir,
            "din471",
            entry,
            lambda d: d["tables"]["shaft"]["rows"]["20"].update(d2=21.0),
        )
    assert any("DIN 471: d1 20: d2 21 is not below d1" in p for p in _problems(synthetic_dir))


# ── (6) resolution and refusal through the modules and std_feature_draw ──────


def _registry_rows(name: str, rows: dict) -> dict:
    """The rows a table module registers for ``name`` (what its import does)."""
    if name.startswith("DIN 332"):
        return {k: {"d1": k, **v} for k, v in rows.items()}
    if name.startswith("DIN 509"):
        form = name[-1]
        return {
            k: {**v, "profile": [list(p) for p in profile_from_dims(form, **v)]}
            for k, v in rows.items()
        }
    return {k: dict(v) for k, v in rows.items()}


@pytest.fixture
def synthetic_registry(synthetic_dir):
    """The real registry names, holding the synthetic rows, restored afterwards."""
    saved = dict(standards_mod._REGISTRY)
    tables = tr.build_tables(synthetic_dir)
    for name, table in tables.items():
        rows = _registry_rows(name, table["rows"])
        standards_mod._REGISTRY.pop(name)
        register(name, rows, coverage_from_rows(rows), "SYNTHETIC fixture, Table 0")
    try:
        yield tables
    finally:
        standards_mod._REGISTRY.clear()
        standards_mod._REGISTRY.update(saved)


def test_an_in_coverage_size_resolves_and_a_size_between_rows_is_refused(synthetic_registry):
    assert ring_groove_dims("DIN 471", 10) == {"m": 1.0, "d2": 9.0}
    assert ring_groove_dims("din  471", 20.0) == {"m": 1.5, "d2": 18.5}
    with pytest.raises(ValueError) as excinfo:
        ring_groove_dims("DIN 471", 15)
    message = str(excinfo.value)
    assert message.startswith("DIN 471 has no row for 15.0")
    assert "nearest transcribed sizes: 10, 20" in message
    assert "Nothing is interpolated" in message
    with pytest.raises(ValueError, match=r"DIN 471 covers 10-20 mm; 40 mm is outside the table"):
        ring_groove_dims("DIN 471", 40)
    assert oring_groove_dims(2.0) == {"b": 3.0, "h": 1.5}
    with pytest.raises(ValueError, match=r"ISO 3601-2 has no row for 2.5"):
        oring_groove_dims(2.5)
    with pytest.raises(ValueError, match="ISO 3601-2-axial: no rows are transcribed"):
        oring_groove_dims(2.0, "axial")  # coverage is per kind
    assert centre_hole_dims("B", 2) == {"d1": 2.0, "d2": 4.0, "d3": 5.0, "t": 4.0}
    assert undercut_profile("F", 30) == profile_from_dims("F", r=1.0, t1=0.5, f=4.0, g=2.0, t2=0.3)
    with pytest.raises(ValueError, match="DIN 509-F covers 30-30 mm"):
        undercut_profile("F", 31)


def test_std_feature_draw_draws_the_generated_row_and_refuses_between_rows(synthetic_registry):
    from engineering.mech.stdparts import feature_spec

    groove = feature_spec("ring_groove", {"d": 10.0, "kind": "shaft"})
    assert groove["standard"] == "DIN 471"
    assert groove["dims"] == {"d": 10.0, "m": 1.0, "d2": 9.0, "kind": "shaft"}
    assert groove["prims"][1]["y1"] == 4.5 and groove["prims"][1]["x2"] == 1.0
    with pytest.raises(ValueError, match="ring_groove: DIN 471 - DIN 471 has no row for 15"):
        feature_spec("ring_groove", {"d": 15.0, "kind": "shaft"})
    relief = feature_spec("undercut", {"d": 30.0, "form": "F"})
    profile = profile_from_dims("F", r=1.0, t1=0.5, f=4.0, g=2.0, t2=0.3)
    assert relief["dims"]["profile"] == [list(p) for p in profile]
    oring = feature_spec("oring_groove", {"d": 20.0, "cord": 3.0, "kind": "shaft"})
    assert (oring["dims"]["b"], oring["dims"]["h"]) == (4.0, 2.4)


@pytest.mark.asyncio
async def test_a_refused_size_writes_nothing(synthetic_registry, backend):
    from engineering.mech.stdparts import draw_std_feature

    before = len(await backend.entity_list())
    with pytest.raises(ValueError, match="no row for 15"):
        await draw_std_feature(backend, "ring_groove", (0.0, 0.0), {"d": 15.0, "kind": "shaft"})
    assert len(await backend.entity_list()) == before
    drawn = await draw_std_feature(backend, "ring_groove", (0.0, 0.0), {"d": 10.0, "kind": "shaft"})
    assert len(drawn["handles"]) == 6
    assert len(await backend.entity_list()) == before + 6


def test_the_probe_picks_a_listed_size_and_one_to_refuse(synthetic_registry):
    assert set(filled_tables()) == {"DIN 471", "DIN 509-F", "DIN 332-B", "ISO 3601-2"}
    assert set(deferred_tables()) == {"DIN 472", "DIN 509-E", "DIN 332-A", "ISO 3601-2-axial"}
    ring = probe("DIN 471")
    assert (ring["size"], ring["refused_size"], ring["refusal"]) == (10.0, 15.0, "between_rows")
    assert ring["draw"] == ("ring_groove", {"kind": "shaft", "standard": "DIN 471", "d": 10.0})
    centre = probe("DIN 332-B")
    assert (centre["refused_size"], centre["refusal"]) == (3.0, "outside_coverage")
    with pytest.raises(ValueError, match="deferred to 1.8.1"):
        probe("DIN 472")


# ── (7) the v9 task and the correctness check ────────────────────────────────


def test_v9_carries_std_tables_b40_and_is_the_default():
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter
    from benchmarks.tasks_v9 import DEFAULT_MATRIX, HEADLESS_ONLY_V9, task_by_id

    assert task_by_id("std_tables_b40", "v9").category == "standards"
    assert "std_tables_b40" not in HEADLESS_ONLY_V9  # it runs through the backend contract
    assert DEFAULT_MATRIX == "v9"  # 1.8-25
    assert hasattr(AutoCADMCPProAdapter, "_task_std_tables_b40")


async def _run_v9_task(tmp_path):
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter
    from benchmarks.tasks_v9 import task_by_id

    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        return await adapter.run_task(task_by_id("std_tables_b40", "v9"))
    finally:
        await adapter.cleanup()


@pytest.mark.asyncio
async def test_the_v9_task_is_not_run_naming_every_deferred_table_never_a_vacuous_pass(
    tmp_path,
):
    if filled_tables():
        pytest.skip("a B40 table has rows; the task runs instead")
    result = await _run_v9_task(tmp_path)
    assert result.status == "not_run", result
    assert "deferred to 1.8.1" in result.message
    for name in B40:
        assert name in result.message
    assert result.metrics["deferred_refused_by_name"] == dict.fromkeys(B40, True)


@pytest.mark.asyncio
async def test_the_v9_task_draws_every_filled_table_and_refuses_its_twin(
    synthetic_registry, tmp_path, monkeypatch
):
    import engineering.mech.standards._rows as generated

    monkeypatch.setattr(generated, "TABLES", synthetic_registry)
    result = await _run_v9_task(tmp_path)
    assert result.status == "pass", result
    assert set(result.metrics["drawn"]) == {"DIN 471", "DIN 509-F", "DIN 332-B", "ISO 3601-2"}
    assert all(result.metrics["twins_ok"].values())


@pytest.mark.asyncio
async def test_the_correctness_check_passes_and_leaves_the_registry_as_it_was():
    from benchmarks.correctness_suite import CHECKS, b40_between_rows_refused

    before = {name: row_count(name) for name in standards()}
    assert CHECKS["b40_between_rows_refused"] == (b40_between_rows_refused, "Mechanical")
    assert await b40_between_rows_refused() is True
    assert {name: row_count(name) for name in standards()} == before
