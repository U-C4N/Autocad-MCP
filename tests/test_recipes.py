"""Task recipes (v1.7.0, B51): versioned records the server serves and this file runs.

A recipe is data under ``engineering/recipes/``: preconditions (backends,
capabilities, packs, profiles), declared inputs, ordered steps in ``cad_batch``'s
own ``{tool, args, bind}`` shape, postconditions a script checks and the
evidence each step leaves. What is pinned here:

* every tool a recipe names is registered, and visible with exactly the packs
  and profiles the recipe declares (and each declared pack is needed);
* every argument template validates against the tool's real JSON Schema, by the
  same validator ``cad_batch`` uses, and instantiates into a batch ``cad_batch``
  accepts on a dry run;
* every recipe runs end to end on the headless engine on a synthetic fixture
  with its postconditions holding - and the dimensioned-part recipe's
  verification fails once the test moves a hole;
* the two resources serve the catalog without a backend;
* the two stale prompts name only registered tools.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import ezdxf
import pytest
import pytest_asyncio
from fastmcp import Client

import config
import server
from backends.com_backend import ComBackend
from backends.ezdxf_backend import EzdxfBackend
from discovery.aliases import aliases_for
from engineering.recipes import (
    PACKS,
    RECIPE_IDS,
    RecipeError,
    check_postconditions,
    get_recipe,
    instantiate,
    list_recipes,
    load_catalog,
    recipe_index,
    run_recipe,
    validate_recipe,
)
from tests.fixtures.plant_pair import build_plant_pair
from tests.fixtures.revisions import build_revision_pair

EXPECTED_IDS = (
    "dimensioned_part_delivery",
    "foreign_pid_lists",
    "revision_takeoff_delta",
    "a3_sheet_delivery",
    "foreign_drawing_audit",
)

# ── synthetic fixtures ──────────────────────────────────────────────────────

PLATE = {
    "kind": "prismatic",
    "name": "plate",
    "material": "aluminium",
    "thickness": 8.0,
    "outline": [[0.0, 0.0], [80.0, 0.0], [80.0, 50.0], [0.0, 50.0]],
    "features": [
        {"kind": "hole", "id": "h1", "x": 20.0, "y": 25.0, "diameter": 10.0},
        {"kind": "hole", "id": "h2", "x": 60.0, "y": 25.0, "diameter": 10.0},
    ],
}


def _hole(feature: str) -> dict:
    return {
        "kind": "feature_tag",
        "part": "$part.part_id",
        "feature": feature,
        "view": "front",
        "type": "circle",
    }


#: Ø10 on h1 and 40 mm between the two hole centres, as a client writes them.
#: The part id is not known until the part is drawn: ``$part.part_id`` is the
#: reference the recipe resolves once its ``part`` step has run.
REQUIREMENTS = {
    "units": "mm",
    "items": [
        {
            "id": "D1",
            "quantity": "diameter",
            "target": {"nominal": 10.0, "unit": "mm"},
            "features": [_hole("h1")],
            "provenance": "explicit",
            "source": {"document": "synthetic-sheet", "label": "D1"},
        },
        {
            "id": "P1",
            "quantity": "distance",
            "target": {"nominal": 40.0, "unit": "mm"},
            "features": [_hole("h1"), _hole("h2")],
            "provenance": "explicit",
            "source": {"document": "synthetic-sheet", "label": "P1"},
        },
    ],
}

#: The foreign P&ID's truth: tags placed here, read back by the heuristics.
PID_INSTRUMENTS = {"FIC-101", "LT-201"}
PID_EQUIPMENT = {"P-101", "V-201"}


def _foreign_pid(path: Path) -> str:
    """A P&ID somebody else drew: plain blocks with a TAG attribute, plain lines."""
    doc = ezdxf.new("R2010", units=4)
    msp = doc.modelspace()

    def block(name, draw):
        definition = doc.blocks.new(name)
        draw(definition)
        definition.add_attdef("TAG", (0.0, -12.0), dxfattribs={"height": 2.5})

    block("FEED_PUMP", lambda b: b.add_circle((0.0, 0.0), 8.0))
    block(
        "STORAGE_TANK",
        lambda b: b.add_lwpolyline([(-10, -15), (10, -15), (10, 15), (-10, 15)], close=True),
    )
    block("INST_BUBBLE", lambda b: b.add_circle((0.0, 0.0), 5.0))
    for name, at, tag in (
        ("STORAGE_TANK", (0.0, 0.0), "V-201"),
        ("FEED_PUMP", (100.0, -10.0), "P-101"),
        ("INST_BUBBLE", (150.0, 30.0), "FIC-101"),
        ("INST_BUBBLE", (60.0, 40.0), "LT-201"),
    ):
        msp.add_blockref(name, at).add_auto_attribs({"TAG": tag})
    msp.add_line((10.0, -10.0), (92.0, -10.0))
    msp.add_line((108.0, -10.0), (200.0, -10.0))
    doc.saveas(path)
    return str(path)


def _plain_part(path: Path) -> str:
    """A 120 x 60 plate with a Ø24 bore, in mm, on GEOMETRY - nothing else."""
    doc = ezdxf.new("R2010", units=4)
    doc.layers.add("GEOMETRY")
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (120, 0), (120, 60), (0, 60)], close=True, dxfattribs={"layer": "GEOMETRY"}
    )
    msp.add_circle((60.0, 30.0), 12.0, dxfattribs={"layer": "GEOMETRY"})
    doc.saveas(path)
    return str(path)


# ── the client and the runner's call ────────────────────────────────────────


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "recipes run headless here, never live"
        yield connected


def _caller(client):
    async def call(tool: str, args: dict):
        return (await client.call_tool(tool, args)).structured_content

    return call


def _failed(run: dict) -> list:
    return [row for row in run["steps"] if not row["ok"]] + [
        row for row in run["postconditions"] if not row["ok"]
    ]


# ── the catalog ─────────────────────────────────────────────────────────────


def test_the_catalog_holds_the_five_recipes_each_versioned():
    assert RECIPE_IDS == EXPECTED_IDS
    assert [recipe["id"] for recipe in list_recipes()] == list(EXPECTED_IDS)
    for recipe in list_recipes():
        assert re.fullmatch(r"\d+\.\d+\.\d+", recipe["version"]), recipe["id"]
        assert recipe["schema_version"] == 1
        assert recipe["steps"] and recipe["postconditions"], recipe["id"]
        for step in recipe["steps"]:
            assert step["purpose"].strip() and step["evidence"].strip(), (recipe["id"], step)


def test_the_catalog_files_load_and_validate_from_the_package_itself():
    catalog = load_catalog()
    assert tuple(catalog) == EXPECTED_IDS
    data = Path(__import__("engineering.recipes").recipes.__file__).parent / "data"
    assert sorted(p.stem for p in data.glob("*.json")) == sorted(EXPECTED_IDS)
    for recipe in catalog.values():
        validate_recipe(recipe)


def test_a_copy_returned_by_the_catalog_cannot_edit_the_catalog():
    first = get_recipe("foreign_pid_lists")
    first["steps"].clear()
    assert get_recipe("foreign_pid_lists")["steps"]


def test_an_unknown_recipe_is_refused_with_the_known_ids():
    with pytest.raises(RecipeError, match="unknown recipe 'nope'.*dimensioned_part_delivery"):
        get_recipe("nope")


@pytest.mark.parametrize(
    ("edit", "pattern"),
    [
        (lambda r: r.update(colour="red"), r"unknown key 'colour'"),
        (lambda r: r.update(id="index"), r"id: 'index' is reserved"),
        (lambda r: r.update(version="1.0"), r"version: '1\.0' is not x\.y\.z"),
        (lambda r: r["preconditions"].update(backends=["acad"]), r"backends.*'acad'"),
        (lambda r: r["preconditions"].update(packs=["vr"]), r"packs.*'vr'"),
        (lambda r: r["preconditions"].update(profiles=["core"]), r"profiles.*'core'"),
        (lambda r: r["steps"][1].update(bind="open"), r"steps\[1\]\.bind: 'open' is bound twice"),
        (lambda r: r["steps"][0].update(bind="inputs"), r"steps\[0\]\.bind: 'inputs' is reserved"),
        (
            lambda r: r["steps"][1]["args"].update(tolerance="$later.x"),
            r"steps\[1\]\.args\.tolerance: '\$later\.x' references 'later'",
        ),
        (
            lambda r: r["steps"][0]["args"].update(path="$inputs.nothing"),
            r"steps\[0\]\.args\.path: '\$inputs\.nothing' names no declared input",
        ),
        (lambda r: r["inputs"].update(spare={"type": "string", "description": "x"}), "spare"),
        (lambda r: r["postconditions"][0].update(op="roughly"), r"op: 'roughly'"),
        (
            lambda r: r["postconditions"][0].update(op="eq", value=["$graph.stats"]),
            r"postconditions\[0\]\.value: only a whole value may be a reference",
        ),
        (lambda r: r["steps"][0].pop("evidence"), r"steps\[0\]: missing 'evidence'"),
    ],
)
def test_a_malformed_recipe_is_refused_by_path(edit, pattern):
    recipe = get_recipe("foreign_pid_lists")
    edit(recipe)
    with pytest.raises(RecipeError, match=pattern):
        validate_recipe(recipe)


# ── preconditions: registry, packs, profiles, capabilities ─────────────────


def _tools(recipe) -> set[str]:
    return {step["tool"] for step in recipe["steps"]}


@pytest.mark.asyncio
async def test_every_recipe_tool_is_registered():
    registered = {tool.name for tool in await server._registered_tools()}
    for recipe in list_recipes():
        assert not (_tools(recipe) - registered), recipe["id"]


@pytest.mark.asyncio
async def test_every_tool_is_visible_with_exactly_the_declared_packs_and_profiles(monkeypatch):
    registered = {tool.name for tool in await server._registered_tools()}
    for recipe in list_recipes():
        pre = recipe["preconditions"]
        assert "core" in pre["packs"] and set(pre["packs"]) <= set(server.TOOL_PACK_NAMES)
        monkeypatch.setattr(config.settings, "tool_packs", ",".join(pre["packs"]))
        packs, ignored = server._enabled_packs()
        assert ignored == []
        for profile in pre["profiles"]:
            visible = server._profile_enabled_names(profile, registered, packs)
            hidden = _tools(recipe) - visible
            assert not hidden, f"{recipe['id']} under {profile}: {sorted(hidden)} hidden"
        # Each declared vertical pack is needed: without it a tool disappears.
        for pack in set(pre["packs"]) - {"core"}:
            fewer = set(pre["packs"]) - {pack}
            visible = server._profile_enabled_names("full", registered, fewer)
            assert _tools(recipe) - visible, f"{recipe['id']} declares {pack!r} it never uses"
        # `lean` is declared exactly when every tool is lean.
        all_lean = _tools(recipe) <= server.LEAN_TOOL_NAMES
        assert ("lean" in pre["profiles"]) is all_lean, recipe["id"]
        assert "full" in pre["profiles"]


def test_every_capability_is_declared_and_supported_on_each_named_backend():
    maps = {
        "ezdxf": EzdxfBackend().capabilities().to_dict()["features"],
        "com": ComBackend().capabilities().to_dict()["features"],
    }
    for recipe in list_recipes():
        pre = recipe["preconditions"]
        assert pre["backends"] and set(pre["backends"]) <= set(maps)
        for key in pre["capabilities"]:
            for name, features in maps.items():
                assert key in features, f"{recipe['id']}: {key!r} is not a capability key"
                if name in pre["backends"]:
                    assert features[key]["supported"], (recipe["id"], name, key)


# ── argument templates against the real schemas ────────────────────────────


@pytest.mark.asyncio
async def test_every_argument_template_validates_against_the_real_schema():
    tools = {tool.name: tool for tool in await server._registered_tools()}
    for recipe in list_recipes():
        for index, step in enumerate(recipe["steps"]):
            problem = server._validate_batch_args(tools[step["tool"]].parameters, step["args"])
            assert problem is None, f"{recipe['id']} steps[{index}] {step['tool']}: {problem}"


@pytest.mark.asyncio
async def test_the_schema_check_bites_on_a_wrong_template():
    tools = {tool.name: tool for tool in await server._registered_tools()}
    recipe = get_recipe("foreign_pid_lists")
    step = next(s for s in recipe["steps"] if s["tool"] == "pid_line_list")
    broken = {**step["args"], "tolerence": 0.5}
    assert "tolerence" in server._validate_batch_args(tools["pid_line_list"].parameters, broken)
    wrong_type = {**step["args"], "tolerance": "half a millimetre"}
    assert server._validate_batch_args(tools["pid_line_list"].parameters, wrong_type)


def _sample_inputs(recipe_id: str, tmp_path: Path) -> dict:
    return {
        "dimensioned_part_delivery": {
            "spec": PLATE,
            "requirements": REQUIREMENTS,
            "output_dir": str(tmp_path / "bundle"),
        },
        "foreign_pid_lists": {
            "drawing": str(tmp_path / "pid.dxf"),
            "instrument_csv": str(tmp_path / "instruments.csv"),
            "line_csv": str(tmp_path / "lines.csv"),
            "equipment_csv": str(tmp_path / "equipment.csv"),
        },
        "revision_takeoff_delta": {
            "old_pid": str(tmp_path / "rev_a.dxf"),
            "new_pid": str(tmp_path / "rev_b.dxf"),
            "layout": str(tmp_path / "layout.dxf"),
            "old_workbook": str(tmp_path / "old.xlsx"),
            "new_workbook": str(tmp_path / "new.xlsx"),
        },
        "a3_sheet_delivery": {
            "drawing": str(tmp_path / "part.dxf"),
            "title": "BRACKET",
            "drawing_no": "SYN-001",
            "view_center_x": 60.0,
            "view_center_y": 30.0,
            "plot_dir": str(tmp_path / "plot"),
            "delivery_dir": str(tmp_path / "bundle"),
        },
        "foreign_drawing_audit": {"drawing": str(tmp_path / "foreign.dxf")},
    }[recipe_id]


@pytest.mark.asyncio
async def test_every_recipe_instantiates_into_a_batch_cad_batch_accepts(client, tmp_path):
    for recipe_id in EXPECTED_IDS:
        steps = instantiate(get_recipe(recipe_id), _sample_inputs(recipe_id, tmp_path))
        assert all(set(step) == {"tool", "args", "bind"} for step in steps)
        result = (
            await client.call_tool("cad_batch", {"steps": steps, "dry_run": True})
        ).structured_content
        assert result["ok"] is True, (recipe_id, result.get("results"))


def test_instantiate_refuses_a_missing_or_unknown_input(tmp_path):
    recipe = get_recipe("foreign_drawing_audit")
    with pytest.raises(RecipeError, match="missing input 'drawing'"):
        instantiate(recipe, {})
    with pytest.raises(RecipeError, match="unknown input 'colour'"):
        instantiate(recipe, {"drawing": "x.dxf", "colour": 1})
    with pytest.raises(RecipeError, match="input 'drawing'.*string"):
        instantiate(recipe, {"drawing": 7})


def test_postcondition_operators_read_bindings_and_compare_by_value():
    recipe = {
        "postconditions": [
            {"id": "a", "ref": "$x.n", "op": "eq", "value": 3, "says": "."},
            {"id": "b", "ref": "$x.n", "op": "ge", "value": "$y.min", "says": "."},
            {
                "id": "c",
                "ref": "$x.box",
                "op": "approx",
                "value": [420, 297],
                "tol": 0.5,
                "says": ".",
            },
            {"id": "d", "ref": "$x.rows", "op": "len_eq", "value": 0, "says": "."},
            {"id": "e", "ref": "$x.missing", "op": "exists", "says": "."},
            {"id": "f", "ref": "$x.n", "op": "lt", "value": 3, "says": "."},
        ]
    }
    bindings = {"x": {"n": 3, "box": [420.2, 296.9], "rows": []}, "y": {"min": 2}}
    rows = {row["id"]: row for row in check_postconditions(recipe, bindings)}
    assert [rows[k]["ok"] for k in "abcdef"] == [True, True, True, True, False, False]
    assert "x.missing" in rows["e"]["reason"]
    assert rows["f"]["actual"] == 3 and rows["f"]["expected"] == 3


# ── end to end on the headless engine ───────────────────────────────────────


@pytest.mark.asyncio
async def test_dimensioned_part_is_verified_delivered_and_fails_once_a_hole_moves(client, tmp_path):
    recipe = get_recipe("dimensioned_part_delivery")
    run = await run_recipe(recipe, _sample_inputs(recipe["id"], tmp_path), _caller(client))
    assert run["ok"] is True, _failed(run)
    verify = run["bindings"]["verify"]
    assert (verify["status"], verify["counts"]["pass"]) == ("pass", 2)
    assert {row["id"]: row["measured"]["value"] for row in verify["rows"]} == {
        "D1": 10.0,
        "P1": 40.0,
    }
    delivery = run["bindings"]["delivery"]
    assert delivery["status"] == "success" and delivery["geometry_acceptance"]["accepted"]
    assert Path(delivery["manifest_path"]).is_file()

    # The test moves h2 by 2 mm: the same requirements now fail on P1 alone.
    (p1,) = [row for row in verify["rows"] if row["id"] == "P1"]
    h2 = p1["handles"][1]
    await client.call_tool("entity_move", {"handle": h2, "dx": 2.0, "dy": 0.0})
    requirements = instantiate(recipe, _sample_inputs(recipe["id"], tmp_path))
    verify_step = next(s for s in requirements if s["bind"] == "verify")
    args = json.loads(
        json.dumps(verify_step["args"]).replace("$part.part_id", run["bindings"]["part"]["part_id"])
    )
    moved = (await client.call_tool("drawing_verify_requirements", args)).structured_content
    assert moved["status"] == "fail" and moved["blocking"] == ["fail: P1"]
    (row,) = [r for r in moved["rows"] if r["id"] == "P1"]
    assert abs(row["measured"]["value"] - 42.0) <= 1e-9
    # ...and the recipe's own postconditions say so, read over the new result.
    again = check_postconditions(recipe, {**run["bindings"], "verify": moved})
    assert not next(r for r in again if r["id"] == "requirements_pass")["ok"]


@pytest.mark.asyncio
async def test_foreign_pid_gives_the_three_lists_without_touching_the_drawing(client, tmp_path):
    _foreign_pid(tmp_path / "pid.dxf")
    run = await run_recipe(
        get_recipe("foreign_pid_lists"),
        _sample_inputs("foreign_pid_lists", tmp_path),
        _caller(client),
    )
    assert run["ok"] is True, _failed(run)
    b = run["bindings"]
    assert {row["tag"] for row in b["instruments"]["rows"]} == PID_INSTRUMENTS
    assert {row["tag"] for row in b["equipment"]["rows"]} == PID_EQUIPMENT
    assert any((row["from_tag"], row["to_tag"]) == ("V-201", "P-101") for row in b["lines"]["rows"])
    assert b["graph"]["stats"]["confidence_min"] == 0.6, "a foreign drawing is heuristic"
    for name in ("instruments.csv", "lines.csv", "equipment.csv"):
        assert (tmp_path / name).is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("edits", [True, False])
async def test_revision_pair_takeoff_delta_is_the_difference_of_the_two_takeoffs(
    client, tmp_path, edits
):
    pair = build_revision_pair(tmp_path / "pair", edits=edits)
    inputs = _sample_inputs("revision_takeoff_delta", tmp_path)
    inputs.update(
        old_pid=pair["old"],
        new_pid=pair["new"],
        layout=str(tmp_path / "pair" / "plant" / "layout.dxf"),
    )
    run = await run_recipe(get_recipe("revision_takeoff_delta"), inputs, _caller(client))
    assert run["ok"] is True, _failed(run)
    old = run["bindings"]["old_takeoff"]["totals"]
    new = run["bindings"]["new_takeoff"]["totals"]
    outputs = run["outputs"]
    assert outputs["total_m_delta"] == pytest.approx(new["total_m"] - old["total_m"])
    assert outputs["net_m_delta"] == pytest.approx(new["net_m"] - old["net_m"])
    summary = run["bindings"]["diff"]["summary"]
    if edits:
        # The edited revision renames a tag, so a run loses its end: the delta is real.
        assert outputs["total_m_delta"] != 0.0
        assert summary["changed"] + summary["added"] + summary["removed"] > 0
    else:
        assert outputs["total_m_delta"] == 0.0 and outputs["net_m_delta"] == 0.0
        assert summary["changed"] + summary["added"] + summary["removed"] == 0


@pytest.mark.asyncio
async def test_a3_sheet_is_plotted_on_a3_and_delivered(client, tmp_path):
    pytest.importorskip("matplotlib", reason="the headless PDF needs the [pdf] extra")
    source = _plain_part(tmp_path / "part.dxf")
    before = Path(source).read_bytes()
    run = await run_recipe(
        get_recipe("a3_sheet_delivery"),
        _sample_inputs("a3_sheet_delivery", tmp_path),
        _caller(client),
    )
    assert run["ok"] is True, _failed(run)
    (sheet,) = run["bindings"]["plot"]["sheets"]
    assert sheet["mediabox_mm"] == [420.0, 297.0], "read back from the PDF's /MediaBox"
    assert Path(sheet["path"]).is_file()
    assert run["bindings"]["delivery"]["status"] == "success"
    assert Path(source).read_bytes() == before, "delivery exports a copy, never the source"


@pytest.mark.asyncio
async def test_foreign_drawing_audit_reports_what_the_drawing_hides_and_writes_nothing(
    client, tmp_path
):
    truth = build_plant_pair(tmp_path / "plant")
    inputs = {"drawing": truth["layout"]}
    before = Path(truth["layout"]).read_bytes()
    run = await run_recipe(get_recipe("foreign_drawing_audit"), inputs, _caller(client))
    assert run["ok"] is True, _failed(run)
    understand = run["bindings"]["understand"]
    # The synthetic layout declares inches over millimetre geometry, and one
    # stray line 5,000 km away: the audit must see both.
    assert understand["units"]["agree"] is False
    assert understand["units"]["inferred"] == truth["layout_true_unit"]
    outliers = {row["handle"] for row in understand["extents"]["outliers"]}
    assert truth["outlier_handle"] in outliers
    assert Path(truth["layout"]).read_bytes() == before


@pytest.mark.asyncio
async def test_a_failing_step_stops_the_run_and_is_reported(client, tmp_path):
    run = await run_recipe(
        get_recipe("foreign_drawing_audit"),
        {"drawing": str(tmp_path / "absent.dxf")},
        _caller(client),
    )
    assert run["ok"] is False
    first = run["steps"][0]
    assert (first["id"], first["ok"]) == ("open", False) and first["error"]
    assert len(run["steps"]) == 1, "nothing runs after a failed step"


# ── the resources and the prompts ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_resources_serve_the_catalog_without_a_backend():
    index = json.loads(await server.resource_recipes_index())
    assert index == recipe_index()
    assert [row["id"] for row in index["recipes"]] == list(EXPECTED_IDS)
    for row in index["recipes"]:
        assert row["uri"] == f"autocad://recipes/{row['id']}"
        assert row["tools"] == [step["tool"] for step in get_recipe(row["id"])["steps"]]
    one = json.loads(await server.resource_recipe("a3_sheet_delivery"))
    assert one == get_recipe("a3_sheet_delivery")
    result = await server.mcp.read_resource("autocad://recipes/index")
    assert json.loads(result.contents[0].content)["recipes"][0]["id"] == EXPECTED_IDS[0]


@pytest.mark.asyncio
async def test_the_resources_are_listed_and_read_through_a_client(client):
    uris = {str(r.uri) for r in await client.list_resources()}
    assert "autocad://recipes/index" in uris
    templates = {t.uriTemplate for t in await client.list_resource_templates()}
    assert "autocad://recipes/{recipe_id}" in templates
    text = (await client.read_resource("autocad://recipes/foreign_pid_lists"))[0].text
    assert json.loads(text) == get_recipe("foreign_pid_lists")
    index = json.loads((await client.read_resource("autocad://recipes/index"))[0].text)
    assert index["recipes"][1]["id"] == "foreign_pid_lists"
    with pytest.raises(Exception, match="unknown recipe 'nope'"):
        await client.read_resource("autocad://recipes/nope")


def test_the_inventory_and_the_readme_count_the_two_resources():
    root = Path(__file__).resolve().parents[1]
    inventory = json.loads((root / "docs" / "tool-inventory.json").read_text(encoding="utf-8"))
    uris = {row["uri"] for row in inventory["resources"]}
    assert {"autocad://recipes/index", "autocad://recipes/{recipe_id}"} <= uris
    assert inventory["totals"]["resources"] == len(inventory["resources"]) == 11
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "· 11 resources ·" in readme and "recipes/index" in readme


_TOOL_CALL = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\(")


async def _prompt_text(client, name, arguments=None) -> str:
    prompt = await client.get_prompt(name, arguments or {})
    return "\n".join(message.content.text for message in prompt.messages)


@pytest.mark.asyncio
async def test_every_prompt_names_only_registered_tools(client):
    registered = {tool.name for tool in await server._registered_tools()}
    for prompt in await client.list_prompts():
        required = {a.name: "x" for a in prompt.arguments or [] if a.required}
        text = await _prompt_text(client, prompt.name, required)
        named = set(_TOOL_CALL.findall(text))
        assert named, prompt.name
        assert not (named - registered), f"{prompt.name}: {sorted(named - registered)}"


@pytest.mark.asyncio
async def test_the_floor_plan_prompt_is_the_plan_model_not_lines(client):
    text = await _prompt_text(
        client, "prompt_floor_plan", {"building_name": "Block 9", "scale": "1:50"}
    )
    for needle in ("arch_plan_from_spec", "arch_rooms_detect", "arch_schedule", "Block 9"):
        assert needle in text, needle
    assert '"scale": 50' in text, "the plan scale is a denominator"
    for stale in ("entity_create_rectangle", "entity_create_arc", "dimension_aligned"):
        assert stale not in text, stale


@pytest.mark.asyncio
async def test_the_floor_plan_prompt_s_starting_spec_draws_and_its_rooms_read_back(client):
    """The spec the prompt hands out is a real one: it draws, and its rooms come back."""
    text = await _prompt_text(client, "prompt_floor_plan", {"scale": "1:50"})
    start = text.index("A starting spec:\n") + len("A starting spec:\n")
    spec = json.loads(text[start : text.index("\n   Stairs:")])
    await client.call_tool("drawing_new", {})
    drawn = (await client.call_tool("arch_plan_from_spec", {"spec": spec})).structured_content
    assert drawn["omitted"] == [] and drawn["critique"] == []
    rooms = (await client.call_tool("arch_rooms_detect", {})).structured_content
    areas = {room["label"]["name"]: room["area"] for room in rooms["rooms"]}
    # Net floor between the wall faces: (5000 - 50 - 125) x (6000 - 2 x 125) and
    # (9000 - 125 - 5000 - 50) x the same, in mm^2 - read back, never typed.
    assert areas == {"LIVING": 4825.0 * 5750.0, "HALL": 3825.0 * 5750.0}


@pytest.mark.asyncio
async def test_a_floor_plan_scale_that_is_not_1_to_n_is_named(client):
    text = await _prompt_text(client, "prompt_floor_plan", {"scale": "half"})
    assert "scale 'half' is not 1:N; 1:100 is used" in text and '"scale": 100' in text
    for odd in ("1:inf", "1:0", "1:12.5"):
        text = await _prompt_text(client, "prompt_floor_plan", {"scale": odd})
        assert f"scale {odd!r} is not 1:N with a whole N" in text, odd
    text = await _prompt_text(client, "prompt_floor_plan", {"units": "inch"})
    assert "convert every dimension to mm" in text


def test_the_catalog_s_pack_names_are_the_server_s():
    assert PACKS == server.TOOL_PACK_NAMES


@pytest.mark.asyncio
async def test_the_electrical_prompt_says_schematics_are_a_1_9_item(client):
    text = await _prompt_text(client, "prompt_electrical_schematic", {"circuit_name": "MDP-2"})
    assert "1.9.0" in text and "MDP-2" in text
    assert "block_define" in text and "drawing_critique" in text
    assert "rectangle with diagonal line" not in text, "no hand-drawn symbol recipes"


def test_cad_batch_is_found_by_asking_for_a_recipe():
    record = aliases_for("cad_batch")
    assert record is not None and "task recipe" in record.synonyms
