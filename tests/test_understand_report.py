"""The takeoff workbook: four sheets, three languages, CSV always, XLSX with openpyxl.

The result written is `pipe_rows` over a hand-built pair: a P&ID with two
runs (T4100-T4101 labelled Ø51, T4101-T4102 unlabelled) and a layout whose
INSUNITS says inches over millimetre geometry, measured on the layout (28 m
and 12 m).
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

import engineering.understand.report as report
from engineering.understand.network import build_network
from engineering.understand.report import LANGS, SHEETS, write_workbook
from engineering.understand.snapshot import EntityRecord, Snapshot
from engineering.understand.takeoff import pipe_rows

PIPE = "PRODUCT"
WALLS = "WALLS"
SCHEMATIC = {
    "verdict": "schematic",
    "within_10pct": 0.25,
    "pair_count": 12,
    "median": 1.3,
    "iqr": 0.8,
    "factor": None,
    "matched": [],
    "ambiguous": [],
}


def rec(handle, kind, layer, points=(), **extra):
    return EntityRecord(
        handle=handle, type=kind, layer=layer, space="Model", points=tuple(points), **extra
    )


def text(handle, value, at, height, layer="TEXT"):
    x, y = at
    return rec(
        handle,
        "TEXT",
        layer,
        (at,),
        text=value,
        height=height,
        bbox=(x, y, x + height * len(value) * 0.6, y + height),
    )


def snap(records, insunits, source):
    layers = {r.layer: {"color": 7, "linetype": "Continuous"} for r in records}
    return Snapshot(
        source=source,
        insunits=insunits,
        extmin=None,
        extmax=None,
        layers=layers,
        layouts=(),
        records=tuple(records),
    )


def tank(handle, tag, x0, y0):
    return [
        rec(
            handle,
            "LWPOLYLINE",
            "EQUIPMENT",
            ((x0, y0), (x0 + 500.0, y0), (x0 + 500.0, y0 + 500.0), (x0, y0 + 500.0)),
            closed=True,
            bbox=(x0, y0, x0 + 500.0, y0 + 500.0),
        ),
        text(handle + "T", tag, (x0 + 150.0, y0 + 225.0), 50.0),
    ]


@pytest.fixture
def result():
    pid = snap(
        [
            *tank("A", "T4100", 0.0, 0.0),
            *tank("B", "T4101", 3000.0, 0.0),
            *tank("C", "T4102", 3000.0, 2000.0),
            rec("L1", "LINE", PIPE, ((500.0, 250.0), (3000.0, 250.0))),
            rec("L2", "LINE", PIPE, ((3250.0, 500.0), (3250.0, 2000.0))),
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
            rec("X", "LINE", WALLS, ((20000, 0), (20000, 20000))),
            text("R1", "ROOM 101", (5000.0, 10000.0), 250.0),
            text("R2", "ROOM 102", (30000.0, 10000.0), 250.0),
            text("T0", "T4100", (2000.0, 3000.0), 250.0),
            text("T1", "T4101", (30000.0, 3000.0), 250.0),
            text("T2", "T4102", (30000.0, 15000.0), 250.0),
        ],
        1,
        "layout.dxf",
    )
    return pipe_rows(build_network(pid, layers=[PIPE]), pid, layout, scale=SCHEMATIC)


METRAJ_HEADERS = {
    "tr": [
        "Mahal",
        "Hizmet",
        "Çap",
        "Doğrudan (m)",
        "Süreklilik (m)",
        "Atanmamış (m)",
        "Net (m)",
        "Pay oranı",
        "Pay (m)",
        "Paylı toplam (m)",
        "Şematik (m)",
        "Durum",
        "Hatlar",
    ],
    "en": [
        "Room",
        "Service",
        "Diameter",
        "Direct (m)",
        "Continuity (m)",
        "Unassigned (m)",
        "Net (m)",
        "Allowance",
        "Allowance (m)",
        "Total incl. allowance (m)",
        "Schematic (m)",
        "Status",
        "Runs",
    ],
    "ru": [
        "Помещение",
        "Среда",
        "Диаметр",
        "Прямо (м)",
        "По непрерывности (м)",
        "Не назначено (м)",
        "Нетто (м)",
        "Запас",
        "Запас (м)",
        "Итого с запасом (м)",
        "По схеме (м)",
        "Статус",
        "Линии",
    ],
}
TOTAL = {"tr": "TOPLAM", "en": "TOTAL", "ru": "ИТОГО"}


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


@pytest.mark.parametrize("lang", LANGS)
def test_csv_sheets_carry_the_headers_of_each_language(result, tmp_path, lang):
    files = write_workbook(result, kind="pipe", lang=lang, path=str(tmp_path / "takeoff.xlsx"))
    names = [Path(p).name for p in files["csv"]]
    assert names == [
        "takeoff_metraj.csv",
        "takeoff_ozet.csv",
        "takeoff_kontrol.csv",
        "takeoff_metodoloji.csv",
    ]
    metraj = read_csv(files["csv"][0])
    assert metraj[0] == METRAJ_HEADERS[lang]
    total = metraj[-1]
    assert total[0] == TOTAL[lang]
    assert float(total[6]) == pytest.approx(40.0)  # 28 m + 12 m net
    assert float(total[9]) == pytest.approx(48.0)  # with the 20 % allowance


def test_the_workbook_has_the_four_sheets_and_numbers_stay_numbers(result, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    files = write_workbook(result, kind="pipe", lang="tr", path=str(tmp_path / "takeoff.xlsx"))
    assert files["refused"] is None
    book = openpyxl.load_workbook(files["xlsx"])
    assert book.sheetnames == list(SHEETS) == ["Metraj", "Özet", "Kontrol", "Metodoloji"]
    rows = list(book["Metraj"].iter_rows(values_only=True))
    assert list(rows[0]) == METRAJ_HEADERS["tr"]
    assert rows[-1][0] == "TOPLAM"
    assert rows[-1][6] == pytest.approx(40.0)
    assert isinstance(rows[1][6], (int, float))  # a number cell, not the text '18.0'
    assert rows[1][6] == pytest.approx(18.0)  # room 101's share of the Ø51 run


def test_the_method_sheet_is_generated_from_the_result(result, tmp_path):
    files = write_workbook(result, kind="pipe", lang="en", path=str(tmp_path / "t.xlsx"))
    method = dict((row[0], row[1]) for row in read_csv(files["csv"][3])[1:] if len(row) == 2)
    assert method["Length source"] == "layout"
    assert method["Scale check: verdict"] == "schematic"
    assert method["Scale check: pairs"] == "12"
    assert method["Scale verified"] == "yes"
    assert method["Units: layout"] == "INSUNITS in → mm"
    assert "x first, then y" in method["Rule: route"]
    warnings = [row[1] for row in read_csv(files["csv"][3]) if row[0] == "Warning"]
    assert any("INSUNITS declares in" in w for w in warnings)


def test_kontrol_lists_the_unassigned_diameter(result, tmp_path):
    files = write_workbook(result, kind="pipe", lang="en", path=str(tmp_path / "t.xlsx"))
    kontrol = read_csv(files["csv"][2])
    assert kontrol[0] == ["Run", "Kind", "Service", "Tags", "Schematic (m)", "Reason"]
    (row,) = kontrol[1:]
    assert row[1] == "Diameter unassigned" and row[3] == "T4101, T4102"
    assert row[5] == "No diameter label"


def test_without_openpyxl_the_csv_is_written_and_xlsx_is_refused(result, tmp_path, monkeypatch):
    monkeypatch.setattr(report, "xlsx_available", lambda: False)
    files = write_workbook(result, kind="pipe", lang="tr", path=str(tmp_path / "takeoff.xlsx"))
    assert files["xlsx"] is None
    assert not (tmp_path / "takeoff.xlsx").exists()
    assert all(Path(p).exists() for p in files["csv"])
    assert files["refused"]["capability"] == "xlsx_write"
    assert files["refused"]["ok"] is False
    assert '".[office]"' in files["refused"]["error"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"kind": "steel"}, "kind: 'steel' unknown; choose from pipe"),
        ({"lang": "de"}, "lang: 'de' unknown; choose from tr, en, ru"),
        ({"path": "takeoff.ods"}, "path: the workbook is written as .xlsx"),
    ],
)
def test_refusals_come_before_any_file(result, tmp_path, kwargs, message):
    arguments = {"kind": "pipe", "lang": "en", "path": str(tmp_path / "t.xlsx"), **kwargs}
    if "path" in kwargs:
        arguments["path"] = str(tmp_path / kwargs["path"])
    with pytest.raises(ValueError, match="^" + message.replace(".", r"\.")):
        write_workbook(result, **arguments)
    assert list(tmp_path.iterdir()) == []
