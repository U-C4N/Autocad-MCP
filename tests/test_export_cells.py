"""No exported text becomes a spreadsheet formula (B03).

One corpus goes through the serializer (`engineering/export/cells.py`) and then
through every exporter that writes a CSV or an XLSX: the P&ID deliverables
(`pid/deliverables.py::write_csv`), the parts list (`sheet/extract.py::write_rows`)
and the takeoff workbook (`understand/report.py::write_workbook`). Everything is
read back the way a reader of the file would read it — `csv.reader` and
openpyxl's `load_workbook` — never by inspecting what the writer meant to write.

The rule: a *text* cell that begins with ``=``, ``+``, ``-``, ``@``, a tab or a
carriage return gets a leading ``'`` in CSV; in XLSX every text cell is a string
cell (``data_type == "s"``) and needs no prefix. A *number* is a number, and a
negative one is never prefixed.
"""

from __future__ import annotations

import csv
import math

import pytest

from engineering.export.cells import (
    FORMULA_PREFIXES,
    append_xlsx_row,
    csv_row,
    csv_text,
    parse_decimal,
    write_csv,
)

openpyxl = pytest.importorskip("openpyxl")

#: Every leading character the rule exists for, a value spanning two lines,
#: a negative number written as text, and a value nothing should touch.
CORPUS = ("=1+1", "+A", "-B", "@C", "\tD", "\rE", "first line\nsecond line", "-12.5", "PLAIN")


def csv_expected(text: str) -> str:
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def xlsx_expected(text: str) -> str:
    """What openpyxl reads back from a string cell: XML 1.0 §2.11 turns a carriage
    return into a line feed when the sheet is parsed, so `cells.py` writes line
    breaks as ``\\n`` itself (``\\r\\n`` came back as ``\\n\\n`` otherwise —
    measured with openpyxl 3.1.5)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_csv(path, encoding="utf-8"):
    with open(path, newline="", encoding=encoding) as handle:
        return list(csv.reader(handle))


# ── (1) the serializer alone ────────────────────────────────────────────────


def test_the_prefixes_are_the_six_the_spec_names():
    assert FORMULA_PREFIXES == ("=", "+", "-", "@", "\t", "\r")


@pytest.mark.parametrize("text", CORPUS)
def test_csv_text_prefixes_exactly_the_formula_leads(text):
    assert csv_text(text) == csv_expected(text)


def test_csv_text_writes_none_as_an_empty_cell_and_stringifies_the_rest():
    assert csv_text(None) == ""
    assert csv_text(12) == "12"
    assert csv_text(-3) == "'-3", "a number in a text column is text"


def test_a_number_column_keeps_a_genuine_negative_number_unprefixed():
    row = csv_row([-12.5, -7, 0.25, "-12.5", None], ["number", "number", "number", "text", "text"])
    assert row == ["-12.5", "-7", "0.25", "'-12.5", ""]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=1+1", "'=1+1"),  # text in a number column is still text
        # A str is text even when it reads as a number: a column that mixes
        # sentences and numbers (the method sheet) must not retype a sentence.
        ("-12.5", "'-12.5"),
        ("+5", "'+5"),
        (math.nan, "nan"),
        (math.inf, "inf"),
        (True, "True"),  # a bool is not a quantity
        ("—", "—"),
    ],
)
def test_anything_but_a_finite_number_in_a_number_column_follows_the_text_rule(value, expected):
    assert csv_row([value], ["number"]) == [expected]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("-12.5", -12.5),
        ("3", 3),
        ("0.25", 0.25),
        ("+5", "+5"),  # a leading + is a formula lead, not a sign
        ("1e400", "1e400"),  # overflows to inf: not a finite number
        ("1.2 kg", "1.2 kg"),
        ("١٢", "١٢"),  # ASCII digits only
        ("", ""),
        (None, None),
        (4.5, 4.5),
    ],
)
def test_parse_decimal_turns_only_a_plain_decimal_into_a_number(text, expected):
    assert parse_decimal(text) == expected
    assert type(parse_decimal(text)) is type(expected)


def test_a_row_must_match_its_kinds():
    with pytest.raises(ValueError, match="2 values for 3 column kinds"):
        csv_row(["a", "b"], ["text", "text", "text"])
    with pytest.raises(ValueError, match="kinds\\[1\\]: 'date'"):
        csv_row(["a", "b"], ["text", "date"])


@pytest.mark.parametrize("bom", [False, True])
def test_write_csv_reads_back_through_csv_reader(tmp_path, bom):
    target = tmp_path / "corpus.csv"
    rows = [[text, -12.5, text] for text in CORPUS]
    write_csv(target, ["=HEAD", "value", "note"], rows, ["text", "number", "text"], bom=bom)
    read = read_csv(target, "utf-8-sig" if bom else "utf-8")
    assert read[0] == ["'=HEAD", "value", "note"], "a header is text too"
    assert read[1:] == [[csv_expected(t), "-12.5", csv_expected(t)] for t in CORPUS]
    assert target.read_bytes().startswith(b"\xef\xbb\xbf") is bom


def test_write_csv_refuses_a_directory_by_name(tmp_path):
    with pytest.raises(ValueError, match="is a directory"):
        write_csv(tmp_path, ["a"], [["x"]], ["text"])


def test_write_csv_refuses_a_header_that_does_not_match_the_kinds(tmp_path):
    with pytest.raises(ValueError, match="1 header cells for 2 column kinds"):
        write_csv(tmp_path / "x.csv", ["a"], [], ["text", "number"])
    assert not (tmp_path / "x.csv").exists()


def test_xlsx_text_cells_are_string_cells_after_a_save_load_round_trip(tmp_path):
    book = openpyxl.Workbook()
    sheet = book.active
    for text in CORPUS:
        append_xlsx_row(sheet, [text, -12.5, text, None], ["text", "number", "number", "text"])
    target = tmp_path / "corpus.xlsx"
    book.save(target)
    loaded = openpyxl.load_workbook(target).active
    for index, text in enumerate(CORPUS, start=1):
        cell, number, numeric_text = (loaded.cell(index, c) for c in (1, 2, 3))
        assert cell.data_type == "s", f"{text!r} must be a string cell, not {cell.data_type!r}"
        assert cell.value == xlsx_expected(text)
        assert number.data_type == "n" and number.value == -12.5
        assert loaded.cell(index, 4).value is None
        # text in a number column — "-12.5" included — is a string cell
        assert numeric_text.data_type == "s" and numeric_text.value == xlsx_expected(text)


def test_without_the_override_openpyxl_would_have_stored_a_formula():
    """The reason the override exists, kept as a check on the locked openpyxl."""
    sheet = openpyxl.Workbook().active
    sheet.append(["=1+1"])
    assert sheet.cell(1, 1).data_type == "f"


# ── (2) every exporter through the corpus ───────────────────────────────────


def test_the_pid_deliverable_csv(tmp_path):
    from engineering.pid.deliverables import LINE_COLUMNS
    from engineering.pid.deliverables import write_csv as write_pid_csv

    rows = []
    for text in CORPUS:
        row = dict.fromkeys(LINE_COLUMNS)
        row.update(line_number=text, from_tag=text, length_mm=-12.5, handle="4F")
        rows.append(row)
    target = tmp_path / "lines.csv"
    write_pid_csv(rows, str(target), LINE_COLUMNS)
    read = read_csv(target)
    assert read[0] == list(LINE_COLUMNS)
    at = {name: i for i, name in enumerate(LINE_COLUMNS)}
    for line, text in zip(read[1:], CORPUS, strict=True):
        assert line[at["line_number"]] == csv_expected(text)
        assert line[at["from_tag"]] == csv_expected(text)
        assert line[at["length_mm"]] == "-12.5"
        assert line[at["size"]] == ""


def _bom_rows():
    return tuple(
        {
            "item": index,
            "qty": 2,
            "designation": text,
            "standard": "ISO 4014",
            "material": text,
            "mass": "-12.5",  # `extract_records` hands the mass over as text
            "remarks": "-12.5",
            "handles": ("A1",),
        }
        for index, text in enumerate(CORPUS, start=1)
    )


COLUMNS = ("item", "qty", "designation", "material", "mass", "remarks")


def test_the_parts_list_csv(tmp_path):
    from engineering.sheet.extract import write_rows

    target = tmp_path / "bom.csv"
    write_rows(_bom_rows(), str(target), columns=COLUMNS, fmt="csv")
    read = read_csv(target)
    assert read[0] == ["POS", "QTY", "DESIGNATION", "MATERIAL", "MASS", "REMARKS"]
    for index, (line, text) in enumerate(zip(read[1:], CORPUS, strict=True), start=1):
        assert line == [str(index), "2", csv_expected(text), csv_expected(text), "-12.5", "'-12.5"]


def test_the_parts_list_xlsx_keeps_quantities_and_masses_numeric(tmp_path):
    from engineering.sheet.extract import write_rows

    target = tmp_path / "bom.xlsx"
    write_rows(_bom_rows(), str(target), columns=COLUMNS, fmt="xlsx")
    sheet = openpyxl.load_workbook(target).active
    assert [c.value for c in sheet[1]] == [
        "POS",
        "QTY",
        "DESIGNATION",
        "MATERIAL",
        "MASS",
        "REMARKS",
    ]
    for index, text in enumerate(CORPUS, start=2):
        item, qty, designation, material, mass, remarks = sheet[index]
        assert (item.data_type, item.value) == ("n", index - 1)
        assert (qty.data_type, qty.value) == ("n", 2), "a BOM quantity is a number, not '2'"
        assert (mass.data_type, mass.value) == ("n", -12.5)
        for cell in (designation, material):
            assert (cell.data_type, cell.value) == ("s", xlsx_expected(text))
        assert (remarks.data_type, remarks.value) == ("s", "-12.5")


@pytest.fixture
def pipe_result():
    from engineering.understand.network import build_network
    from engineering.understand.takeoff import pipe_rows
    from tests.test_understand_report import PIPE, SCHEMATIC, WALLS, rec, snap, tank, text

    pid = snap(
        [
            *tank("A", "T4100", 0.0, 0.0),
            *tank("B", "T4101", 3000.0, 0.0),
            rec("L1", "LINE", PIPE, ((500.0, 250.0), (3000.0, 250.0))),
            text("D1", "Ø51", (1500.0, 260.0), 50.0),
        ],
        4,
        "pid.dxf",
    )
    layout = snap(
        [
            rec(
                "W",
                "LWPOLYLINE",
                WALLS,
                ((0, 0), (40000, 0), (40000, 20000), (0, 20000)),
                closed=True,
                bbox=(0.0, 0.0, 40000.0, 20000.0),
            ),
            text("R1", "ROOM 101", (5000.0, 10000.0), 250.0),
            text("T0", "T4100", (2000.0, 3000.0), 250.0),
            text("T1", "T4101", (30000.0, 3000.0), 250.0),
        ],
        1,
        "layout.dxf",
    )
    result = pipe_rows(build_network(pid, layers=[PIPE]), pid, layout, scale=SCHEMATIC)
    (base,) = result["rows"]
    result["rows"] = [
        dict(base, room=t, diameter=t, status=t, runs=[t], direct_m=-12.5) for t in CORPUS
    ]
    result["warnings"] = list(CORPUS)
    return result


def test_the_takeoff_csv(pipe_result, tmp_path):
    from engineering.understand.report import write_workbook

    files = write_workbook(pipe_result, kind="pipe", lang="en", path=str(tmp_path / "t.xlsx"))
    metraj = read_csv(files["csv"][0], "utf-8-sig")
    for line, text in zip(metraj[1:], CORPUS, strict=False):
        assert line[0] == line[2] == line[11] == line[12] == csv_expected(text)
        assert line[3] == "-12.5", "a negative length in a length column is a number"
    method = read_csv(files["csv"][3], "utf-8-sig")
    warnings = [row[1] for row in method if row[0] == "Warning"]
    assert warnings == [csv_expected(t) for t in CORPUS]


def test_the_takeoff_xlsx(pipe_result, tmp_path):
    from engineering.understand.report import write_workbook

    files = write_workbook(pipe_result, kind="pipe", lang="en", path=str(tmp_path / "t.xlsx"))
    book = openpyxl.load_workbook(files["xlsx"])
    metraj = book["Metraj"]
    for index, text in enumerate(CORPUS, start=2):
        row = metraj[index]
        for cell in (row[0], row[2], row[11], row[12]):
            assert (cell.data_type, cell.value) == ("s", xlsx_expected(text))
        assert (row[3].data_type, row[3].value) == ("n", -12.5)
        assert row[6].data_type == "n", "net metres stay a number"
    method = [r for r in book["Metodoloji"].iter_rows() if r[0].value == "Warning"]
    assert [(r[1].data_type, r[1].value) for r in method] == [
        ("s", xlsx_expected(t)) for t in CORPUS
    ]


def test_the_cable_takeoff_keeps_kw_numeric_and_a_text_section_text(tmp_path):
    from engineering.understand.report import write_workbook
    from engineering.understand.takeoff import cable_rows
    from tests.test_understand_cable import layout_records, pid_records, snap

    result = cable_rows(snap(pid_records(), 4, "pid.dxf"), snap(layout_records(), 4, "l.dxf"))
    first = result["rows"][0]
    first.update(tag="=1+1", kw=-12.5, section="-12.5")
    files = write_workbook(result, kind="cable", lang="tr", path=str(tmp_path / "c.xlsx"))
    line = read_csv(files["csv"][0], "utf-8-sig")[1]
    assert (line[0], line[2], line[11]) == ("'=1+1", "-12.5", "'-12.5")
    row = openpyxl.load_workbook(files["xlsx"])["Metraj"][2]
    assert (row[0].data_type, row[0].value) == ("s", "=1+1")
    assert (row[2].data_type, row[2].value) == ("n", -12.5)
    assert (row[11].data_type, row[11].value) == ("s", "-12.5")
    assert row[10].data_type == "n", "cable metres stay a number"


# ── (3) the method sheet says so ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("lang", "topic", "words"),
    [
        ("tr", "Kural: CSV hücreleri", ("'", "=", "formül")),
        ("en", "Rule: CSV cells", ("'", "=", "formula")),
        ("ru", "Правило: ячейки CSV", ("'", "=", "формул")),
    ],
)
@pytest.mark.parametrize("kind", ["pipe", "cable"])
def test_the_method_sheet_states_the_csv_rule(pipe_result, tmp_path, lang, topic, words, kind):
    from engineering.understand.report import write_workbook
    from engineering.understand.takeoff import cable_rows
    from tests.test_understand_cable import layout_records, pid_records, snap

    result = (
        pipe_result
        if kind == "pipe"
        else cable_rows(snap(pid_records(), 4, "pid.dxf"), snap(layout_records(), 4, "l.dxf"))
    )
    files = write_workbook(result, kind=kind, lang=lang, path=str(tmp_path / "t.xlsx"))
    method = dict((row[0], row[1]) for row in read_csv(files["csv"][3], "utf-8-sig")[1:])
    assert topic in method, f"no {topic!r} row in {sorted(method)}"
    assert all(word in method[topic] for word in words), method[topic]
