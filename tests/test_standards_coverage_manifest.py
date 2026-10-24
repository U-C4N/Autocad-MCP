"""The standards coverage manifest (B39): what is transcribed, from where, and what is not.

One record per registered table - standard, edition, table locator, unit, the
sizes actually implemented, the coverage range, the SOURCE locator, transcriber,
reviewer and a redistribution note. The four feature standards that ship with
no rows (DIN 509, DIN 471/472, DIN 332-1, ISO 3601-2) say "no rows transcribed"
instead of a 0-0 range. ``std_part_list`` returns the manifest under
``coverage`` and the static resource ``autocad://standards/coverage`` serves the
same document without a backend. A request outside a table is refused with the
nearest transcribed sizes listed - never substituted, never interpolated.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from fastmcp import Client

import engineering.mech.standards as standards_mod
import server
from engineering.mech.standards import Coverage, coverage_of, lookup, register, source_of
from engineering.mech.standards.coverage import TableCoverage, coverage_manifest
from engineering.mech.standards.threads import pitch_for

STANDARDS_DIR = Path(standards_mod.__file__).parent

#: Every table this build registers, pinned so a lost import is a failure.
TRANSCRIBED = {
    "ISO 15": 33,
    "ISO 261": 28,
    "ISO 4014": 10,
    "ISO 4017": 10,
    "ISO 4032": 10,
    "ISO 4762": 10,
    "ISO 7089": 10,
}
EMPTY = {
    "DIN 332-A": "DIN 332-1",
    "DIN 332-B": "DIN 332-1",
    "DIN 471": "DIN 471",
    "DIN 472": "DIN 472",
    "DIN 509-E": "DIN 509",
    "DIN 509-F": "DIN 509",
    "ISO 3601-2": "ISO 3601-2",
}
FIELDS = (
    "standard",
    "edition",
    "table",
    "unit",
    "implemented",
    "coverage",
    "source",
    "transcriber",
    "reviewer",
    "redistribution",
)


def _records() -> dict[str, dict]:
    return {row["standard"]: row for row in coverage_manifest()["tables"]}


# -- (1) one record per registered table ------------------------------------


def test_the_manifest_has_one_record_per_registered_table():
    manifest = coverage_manifest()
    assert manifest["schema_version"] == 1
    names = [row["standard"] for row in manifest["tables"]]
    assert len(names) == len(set(names))
    assert set(names) == set(standards_mod.standards())
    real = {name for name in names if not name.startswith("TEST")}
    assert real == set(TRANSCRIBED) | set(EMPTY)


def test_every_table_module_is_loaded_by_the_manifest_itself():
    """A table module coverage.py forgets to import would vanish from the manifest."""
    tree = ast.parse((STANDARDS_DIR / "coverage.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.rsplit(".", 1)[-1])
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
    registering = {
        path.stem
        for path in STANDARDS_DIR.glob("*.py")
        if path.stem not in ("__init__", "coverage")
        and "register(" in path.read_text(encoding="utf-8")
    }
    assert registering == {
        "bearings",
        "centres",
        "fasteners",
        "orings",
        "rings",
        "threads",
        "undercuts",
    }
    assert registering <= imported


def test_every_record_carries_every_field_of_the_contract():
    assert tuple(TableCoverage.__dataclass_fields__) == FIELDS
    for row in coverage_manifest()["tables"]:
        assert tuple(row) == FIELDS, row["standard"]
        assert isinstance(row["implemented"], list)
        assert row["unit"] == "mm"
        assert row["redistribution"].strip()


def test_edition_and_table_locator_are_quoted_from_the_tables_own_source():
    records = _records()
    for name in (*TRANSCRIBED, *EMPTY):
        row = records[name]
        assert row["source"] == source_of(name)
        assert row["edition"] and row["edition"] in row["source"], name
        assert row["table"].lower().startswith("table"), name
        assert row["table"] in row["source"], name
    assert records["ISO 4014"]["edition"] == "2011"
    assert records["ISO 15"]["table"] == "Table 4"
    assert records["DIN 471"]["table"] == "Table 1 (shafts)"
    assert records["DIN 472"]["table"] == "Table 1 (bores)"
    assert records["DIN 509-E"]["edition"] == "1998-06"


def test_transcribed_tables_list_their_sizes_and_their_range():
    records = _records()
    for name, count in TRANSCRIBED.items():
        row = records[name]
        assert len(row["implemented"]) == count, name
        assert row["coverage"]["status"] == "transcribed"
        assert row["coverage"]["rows"] == count
        declared = coverage_of(name)
        assert (row["coverage"]["low"], row["coverage"]["high"]) == (declared.low, declared.high)
        assert row["coverage"]["measure"]
    assert records["ISO 4014"]["implemented"][:3] == ["M5", "M6", "M8"]
    assert "6205" in records["ISO 15"]["implemented"]
    assert records["ISO 261"]["implemented"][0] == "1.6"
    assert "64" in records["ISO 261"]["implemented"]


def test_the_four_empty_standards_say_no_rows_transcribed_with_their_source_locator():
    records = _records()
    for name, document in EMPTY.items():
        row = records[name]
        assert row["implemented"] == [], name
        assert row["coverage"]["status"] == "no rows transcribed", name
        assert row["coverage"]["rows"] == 0
        # The 0-0 placeholder range is not a coverage and is never reported as one.
        assert row["coverage"]["low"] is None and row["coverage"]["high"] is None
        assert document in row["source"] and "Table" in row["source"]
        assert "no values" in row["redistribution"].lower()
        assert f"from {document} " in row["redistribution"], name
    assert {document.split("-")[0] for document in EMPTY.values()} == {
        "DIN 332",
        "DIN 471",
        "DIN 472",
        "DIN 509",
        "ISO 3601",
    }


def test_transcriber_and_reviewer_are_null_never_invented():
    manifest = coverage_manifest()
    for row in manifest["tables"]:
        assert row["transcriber"] is None and row["reviewer"] is None
    assert "not recorded" in manifest["notes"]["transcriber"]


def test_a_table_registered_later_gets_its_own_record():
    saved = dict(standards_mod._REGISTRY)
    try:
        register(
            "TEST 9001",
            {4.0: {"w": 1.0}},
            Coverage(4.0, 4.0),
            "TEST 9001:2026, Table 7 - synthetic fixture, not a standard",
        )
        row = _records()["TEST 9001"]
        assert row["implemented"] == ["4"]
        assert row["edition"] == "2026" and row["table"] == "Table 7"
        assert row["coverage"]["measure"] == "not stated"
    finally:
        standards_mod._REGISTRY.clear()
        standards_mod._REGISTRY.update(saved)


def test_the_counts_add_up():
    manifest = coverage_manifest()
    counts = manifest["counts"]
    tables = manifest["tables"]
    assert counts["tables"] == len(tables)
    assert counts["transcribed"] + counts["empty"] == counts["tables"]
    assert counts["rows"] == sum(row["coverage"]["rows"] for row in tables)
    assert counts["empty"] >= len(EMPTY)


def test_the_manifest_is_plain_json():
    manifest = coverage_manifest()
    assert json.loads(json.dumps(manifest)) == manifest


# -- (2) a request outside the coverage: the nearest sizes, never a substitute ----


def test_a_size_beyond_the_coverage_lists_the_nearest_transcribed_sizes():
    with pytest.raises(ValueError) as excinfo:
        lookup("ISO 261", 80)
    message = str(excinfo.value)
    assert "outside the table" in message
    assert "nearest transcribed sizes: 64, 60, 56" in message
    assert "nothing is substituted" in message.lower()


def test_a_designation_beyond_the_coverage_is_refused_against_the_coverage():
    with pytest.raises(ValueError) as excinfo:
        lookup("ISO 4014", "M42")
    message = str(excinfo.value)
    assert "ISO 4014 covers 5-36 mm" in message
    assert "nearest transcribed sizes: M36, M30, M24" in message


def test_a_gap_inside_the_coverage_lists_the_neighbours_and_is_never_interpolated():
    with pytest.raises(ValueError) as excinfo:
        lookup("ISO 261", 15)
    message = str(excinfo.value)
    assert "no row for 15" in message
    assert "nearest transcribed sizes: 14, 16" in message


@pytest.mark.parametrize("name", sorted(EMPTY))
def test_an_empty_table_says_no_rows_transcribed_not_a_zero_range(name):
    with pytest.raises(ValueError) as excinfo:
        lookup(name, 30)
    message = str(excinfo.value)
    assert "no rows transcribed" in message
    assert source_of(name).split(" - ")[0] in message
    assert "0-0" not in message


def test_a_thread_diameter_outside_iso_261_lists_the_nearest_diameters():
    with pytest.raises(ValueError) as excinfo:
        pitch_for(70.0)
    assert "Nearest transcribed diameters: M64, M60, M56" in str(excinfo.value)


@pytest.mark.asyncio
async def test_an_out_of_coverage_part_is_refused_without_drawing_a_substitute(backend):
    from engineering.mech.stdparts import insert_std_part

    entities = len(await backend.entity_list())
    blocks = {block.name for block in await backend.block_list()}
    with pytest.raises(ValueError) as excinfo:
        await insert_std_part(backend, "ISO 4014 - M42x200", at=(0.0, 0.0))
    message = str(excinfo.value)
    assert "Nearest entries: ISO 4014 - M36" in message
    assert "M5" in message and "M36" in message  # the available sizes are listed
    assert len(await backend.entity_list()) == entities
    assert {block.name for block in await backend.block_list()} == blocks


# -- (3) the resource and std_part_list serve the same manifest ---------------


@pytest.mark.asyncio
async def test_the_resource_serves_the_manifest_without_a_backend():
    result = await server.mcp.read_resource("autocad://standards/coverage")
    assert json.loads(result.contents[0].content) == coverage_manifest()
    assert json.loads(await server.resource_standards_coverage()) == coverage_manifest()


@pytest.mark.asyncio
async def test_the_resource_is_listed_and_std_part_list_returns_the_same_manifest():
    async with Client(server.mcp) as client:
        uris = {str(r.uri) for r in await client.list_resources()}
        assert "autocad://standards/coverage" in uris
        text = (await client.read_resource("autocad://standards/coverage"))[0].text
        assert json.loads(text) == coverage_manifest()
        out = (await client.call_tool("std_part_list", {"query": "M12"})).structured_content
        assert out["coverage"] == coverage_manifest()
        assert out["count"] == 5


# -- (4) counts ---------------------------------------------------------------


def test_the_inventory_and_the_readme_count_the_new_resource():
    root = Path(server.__file__).parent
    inventory = json.loads((root / "docs" / "tool-inventory.json").read_text(encoding="utf-8"))
    uris = {row["uri"] for row in inventory["resources"]}
    assert "autocad://standards/coverage" in uris
    assert inventory["totals"]["resources"] == len(inventory["resources"]) == 9
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "· 9 resources ·" in readme
    assert "<code>standards/coverage</code>" in readme
