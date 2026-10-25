"""The premium skill and the Claude Code plugin that ships it (spec §4 W1, B05).

The canonical skill lives in ``.claude/skills/autocad-mcp-premium/``; the plugin
under ``plugin/`` carries a generated copy (``scripts/build_plugin.py``), an MCP
server pinned to the package version, and a manifest the repository-root
marketplace points at. These tests hold the four promises the plugin makes:

- one version everywhere: pyproject, ``version.py``, ``plugin.json``, the
  marketplace entry and the ``==<version>`` pin in ``plugin/.mcp.json``;
- every relative link in the skill resolves, and stays inside the skill folder
  (a link that escapes it breaks in the plugin copy);
- every tool the skill names exists in ``docs/tool-inventory.json`` — a tool that
  no longer exists is a failure, never a skip;
- the plugin copy is exactly what ``build_plugin.py`` would write.

The 1.7.0 skill content (spec §6 Task 19, B54) adds:

- ``SKILL.md`` stays within its token budget (below);
- every reference and example SKILL.md links exists, and every file under
  ``references/`` and ``examples/`` is linked from SKILL.md;
- tool names under a "not yet" heading are exempt from the inventory check
  (they name what a later release brings), everywhere else they are not;
- every fenced ``json`` block in the skill is checked by what it claims to be:
  ``json requirements`` parses with ``engineering/verification/requirements.py``,
  ``json requirements-refused`` is refused by it, ``json tool=<name>`` validates
  against that tool's real JSON Schema (``cad_batch``'s own validator) - and the
  flange and ambiguous-hole examples run end to end on the headless engine with
  the numbers they quote;
- the eval cases under ``plugin/evals/`` load under the CLI 2.1.310 case schema
  and cover the negative scenarios of spec §6 Task 22.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import pytest_asyncio
import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude" / "skills" / "autocad-mcp-premium"
PLUGIN = ROOT / "plugin"
PLUGIN_SKILL = PLUGIN / "skills" / "premium"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"
PLUGIN_MCP = PLUGIN / ".mcp.json"
BUILD_SCRIPT = ROOT / "scripts" / "build_plugin.py"

PLUGIN_NAME = "autocad-mcp-pro"

#: ``[text](target)`` — the markdown inline link form the skill uses.
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
#: Inline code spans (one backtick, not a fence).
_SPAN_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
#: Fenced code blocks.
_FENCE_RE = re.compile(r"```.*?```", re.S)
#: A call inside a fenced block: ``name(`` not preceded by ``.`` (a method).
_CALL_RE = re.compile(r"(?<![.\w])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\s*\(")
#: A tool-shaped inline span: ``name``, ``name(...)``, ``name_*`` or the slash
#: shorthand ``entity_get/list/delete_many`` (``...`` allowed as a last part).
_TOOL_SPAN_RE = re.compile(
    r"^(?P<head>[a-z][a-z0-9]*(?:_[a-z0-9]+)*_(?:[a-z0-9]+|\*))"
    r"(?P<rest>(?:/(?:[a-z0-9_]+|\.\.\.))*)"
    r"(?:\(.*\))?$"
)


def _pyproject_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _skill_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.md"))


def _inventory_names() -> set[str]:
    inventory = _json(ROOT / "docs" / "tool-inventory.json")
    return {tool["name"] for tool in inventory["tools"]}


def _non_tool_vocabulary() -> set[str]:
    """Snake-case names the skill uses that are not tools: critique focuses and
    the acceptance report's finding kinds.

    ``layer_color`` and ``construction_left`` share a prefix with real tool
    groups (``layer_*``, ``construction_*``) but are values of
    ``drawing_critique(focus=...)``; ``dimension_mismatch`` shares one with
    ``dimension_*`` but is a ``kind`` of ``drawing_verify_requirements``. The
    code's own lists say so.
    """
    from engineering.plan_spec import ALL_CRITIQUE_FOCUSES
    from engineering.verification import report

    kinds = {
        report.AMBIGUOUS_BINDING,
        report.DIMENSION_MISMATCH,
        report.DIMENSION_UNVERIFIED,
        report.MISSING_REQUIREMENT,
        report.UNSUPPORTED_MEASUREMENT,
    }
    return set(ALL_CRITIQUE_FOCUSES) | kinds


#: A markdown heading: its level and its text.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def _without_not_yet_sections(text: str) -> str:
    """``text`` minus every section whose heading says "not yet".

    A section runs from its heading to the next heading of the same or a higher
    level. What a later release brings is named there on purpose; the skill may
    not name it anywhere else. Headings inside fenced blocks are not headings.
    """
    kept: list[str] = []
    skip_level: int | None = None
    in_fence = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if skip_level is None and "not yet" in heading.group(2).lower():
                skip_level = level
        if skip_level is None:
            kept.append(line)
    return "".join(kept)


def _expand_span(head: str, rest: str) -> list[str]:
    """``entity_get/list/delete_many`` -> entity_get, entity_list, entity_delete_many."""
    names = [head]
    if rest:
        base = head.rsplit("_", 1)[0] + "_"
        names += [base + part for part in rest.split("/")[1:] if part != "..."]
    return names


def _tool_mentions(text: str, prefixes: set[str], ignore: set[str]) -> set[str]:
    """Every tool name a skill document mentions, wildcards kept as ``prefix_*``."""
    mentions: set[str] = set()
    for fence in _FENCE_RE.findall(text):
        for match in _CALL_RE.finditer(fence):
            mentions.add(match.group(1))
    prose = _FENCE_RE.sub("", text)
    for span in _SPAN_RE.findall(prose):
        match = _TOOL_SPAN_RE.match(span.strip())
        if match:
            mentions.update(_expand_span(match.group("head"), match.group("rest")))
    return {name for name in mentions if name.split("_", 1)[0] in prefixes and name not in ignore}


# ---------------------------------------------------------------------------
# One version everywhere
# ---------------------------------------------------------------------------


def test_versions_agree_across_package_plugin_marketplace_and_pin() -> None:
    import version as version_module

    expected = _pyproject_version()
    plugin = _json(PLUGIN_MANIFEST)
    marketplace = _json(MARKETPLACE)
    entries = [entry for entry in marketplace["plugins"] if entry["name"] == PLUGIN_NAME]
    assert len(entries) == 1, f"marketplace lists {PLUGIN_NAME} {len(entries)} times"
    pins = re.findall(r"==(\d+\.\d+\.\d+)", PLUGIN_MCP.read_text(encoding="utf-8"))

    found = {
        "version.py": version_module.package_version(),
        "plugin.json": plugin["version"],
        "marketplace entry": entries[0]["version"],
    }
    assert pins, "plugin/.mcp.json carries no ==<version> pin"
    for index, pin in enumerate(pins):
        found[f".mcp.json pin {index}"] = pin
    mismatched = {where: value for where, value in found.items() if value != expected}
    assert not mismatched, f"pyproject is {expected}; these disagree: {mismatched}"


def test_marketplace_lists_exactly_the_one_plugin_by_its_owner() -> None:
    marketplace = _json(MARKETPLACE)
    assert marketplace["owner"]["name"] == "U-C4N"
    assert [entry["name"] for entry in marketplace["plugins"]] == [PLUGIN_NAME]
    assert marketplace["plugins"][0]["source"] == "./plugin"
    assert _json(PLUGIN_MANIFEST)["name"] == PLUGIN_NAME


def test_mcp_json_runs_the_pinned_package_with_its_extras() -> None:
    servers = _json(PLUGIN_MCP)["mcpServers"]
    assert list(servers) == ["autocad"], "the plugin starts exactly one server"
    server = servers["autocad"]
    assert server["command"] == "uvx"
    args = " ".join(server["args"])
    version = _pyproject_version()
    assert f"autocad-mcp-pro[com,pdf,office]=={version}" in args
    assert server["args"][-1] == "autocad-mcp"


def test_plugin_readme_documents_the_local_source_override() -> None:
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    mcp_text = PLUGIN_MCP.read_text(encoding="utf-8")
    variable = re.search(r"\$\{(AUTOCAD_MCP_[A-Z_]+):-", mcp_text)
    assert variable, "plugin/.mcp.json has no ${VAR:-default} override for the source"
    assert variable.group(1) in readme, "the README does not name the override variable"


# ---------------------------------------------------------------------------
# The canonical skill: links and tool names
# ---------------------------------------------------------------------------


#: The references and examples spec §6 Task 19 names.
EXPECTED_REFERENCES = {
    "dimensioned-reconstruction",
    "requirement-binding",
    "mechanical-editing",
    "pid-projects",
    "foreign-drawing",
    "architecture",
    "takeoff-evidence",
    "uncertainty-and-questions",
    "delivery-and-roundtrip",
    "backend-capabilities",
    "recovery-and-transactions",
}
EXPECTED_EXAMPLES = {
    "dimensioned-flange",
    "ambiguous-hole-binding",
    "schematic-pid",
    "stale-revision",
    "accepted-delivery-report",
}


def test_canonical_skill_is_present() -> None:
    expected = {"SKILL.md"}
    expected |= {f"references/{name}.md" for name in EXPECTED_REFERENCES}
    expected |= {f"examples/{name}.md" for name in EXPECTED_EXAMPLES}
    present = {path.relative_to(SKILL).as_posix() for path in _skill_files(SKILL)}
    assert expected <= present, f"missing from the skill: {sorted(expected - present)}"


def test_skill_md_links_every_reference_and_example_and_each_exists() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    linked = {
        target.split("#", 1)[0]
        for target in _LINK_RE.findall(text)
        if target.startswith(("references/", "examples/"))
    }
    present = {
        path.relative_to(SKILL).as_posix()
        for folder in ("references", "examples")
        for path in (SKILL / folder).glob("*.md")
    }
    assert linked - present == set(), f"SKILL.md links missing files: {sorted(linked - present)}"
    assert present - linked == set(), f"not linked from SKILL.md: {sorted(present - linked)}"


# ``claude --plugin-dir ./plugin plugin details autocad-mcp-pro`` (CLI 2.1.310)
# prices the skill's on-invoke cost. Measured on this skill: the 1.5-era SKILL.md
# (5,194 characters) ~2.1k tokens, 2.47 characters a token; the first 1.7.0 draft
# (7,591 characters) ~2.8k, 2.71. The committed SKILL.md (6,425 characters):
# ~2.4k on-invoke, ~190 always-on. Dividing by 2.4 - below both measured ratios -
# overestimates the CLI's figure, so the bound errs on the side of a short skill.
SKILL_TOKEN_BUDGET = 3000
CLI_CHARS_PER_TOKEN = 2.4


def test_skill_md_is_within_its_token_budget() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    estimate = math.ceil(len(text) / CLI_CHARS_PER_TOKEN)
    assert estimate <= SKILL_TOKEN_BUDGET, (
        f"SKILL.md is {len(text)} characters, ~{estimate} tokens by the CLI's measured ratio; "
        f"the budget is {SKILL_TOKEN_BUDGET} - move detail into a reference"
    )


def test_skill_md_carries_a_not_yet_section() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    headings = [m.group(2) for m in map(_HEADING_RE.match, text.splitlines()) if m]
    assert any("not yet" in heading.lower() for heading in headings), headings
    assert "1.8.0" in text


@pytest.mark.parametrize("root", [SKILL, PLUGIN_SKILL], ids=["canonical", "plugin-copy"])
def test_every_relative_link_in_the_skill_resolves_inside_it(root: Path) -> None:
    assert (root / "SKILL.md").is_file(), f"{root} has no SKILL.md"
    broken: list[str] = []
    checked = 0
    for path in _skill_files(root):
        for target in _LINK_RE.findall(path.read_text(encoding="utf-8")):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue
            checked += 1
            resolved = (path.parent / target.split("#", 1)[0]).resolve()
            label = f"{path.relative_to(root).as_posix()} -> {target}"
            if not resolved.exists():
                broken.append(f"{label} (missing)")
            elif not resolved.is_relative_to(root.resolve()):
                broken.append(f"{label} (outside the skill folder)")
    assert checked, "no relative link found: the link pattern no longer matches the skill"
    assert not broken, "broken skill links:\n" + "\n".join(broken)


def test_every_tool_the_skill_names_exists_in_the_inventory() -> None:
    names = _inventory_names()
    prefixes = {name.split("_", 1)[0] for name in names}
    ignore = _non_tool_vocabulary()
    missing: list[str] = []
    seen: set[str] = set()
    for path in _skill_files(SKILL):
        text = _without_not_yet_sections(path.read_text(encoding="utf-8"))
        for mention in _tool_mentions(text, prefixes, ignore):
            seen.add(mention)
            if mention.endswith("_*"):
                ok = any(name.startswith(mention[:-1]) for name in names)
            else:
                ok = mention in names
            if not ok:
                missing.append(f"{path.relative_to(SKILL).as_posix()}: {mention}")
    assert {
        "drawing_plan",
        "entity_trim",
        "point_from_snap",
        "drawing_verify_requirements",
    } <= seen, "the tool-name scan found too little — its patterns no longer match the skill"
    assert not missing, "the skill names tools that do not exist:\n" + "\n".join(sorted(missing))


def test_tool_scan_reports_a_tool_that_no_longer_exists() -> None:
    names = {"entity_get", "entity_list", "drawing_plan", "layer_create"}
    prefixes = {name.split("_", 1)[0] for name in names}
    text = (
        "Use `entity_get/list/delete_many` and `drawing_planx(intent)`.\n"
        "```python\nlayer_destroy(name)\nbackend.layer_missing()\n```\n"
        "Focus `layer_color` is a value, `keep_x` an argument.\n"
    )
    mentions = _tool_mentions(text, prefixes, {"layer_color"})
    assert mentions == {
        "entity_get",
        "entity_list",
        "entity_delete_many",
        "drawing_planx",
        "layer_destroy",
    }
    assert sorted(mentions - names) == ["drawing_planx", "entity_delete_many", "layer_destroy"]


def test_not_yet_sections_are_exempt_and_nothing_else_is() -> None:
    text = (
        "# Skill\nUse `drawing_rebuild`.\n"
        "## Not yet (1.8.0)\n`drawing_rebuild` and `layer_journal` arrive later.\n"
        "### Detail\n`layer_detail` is still inside the section.\n"
        "```text\n# not a heading\n```\n"
        "## Back\n`layer_after` is checked again.\n"
    )
    prefixes = {"drawing", "layer"}
    kept = _tool_mentions(_without_not_yet_sections(text), prefixes, set())
    assert kept == {"drawing_rebuild", "layer_after"}
    assert "layer_journal" in _tool_mentions(text, prefixes, set())


# ---------------------------------------------------------------------------
# The fenced JSON blocks: requirement sets, refusals and tool calls
# ---------------------------------------------------------------------------

#: A fenced block's info string and body: ```json <kind> [key=value ...]
_JSON_FENCE_RE = re.compile(r"^```json([^\n]*)\n(.*?)^```", re.S | re.M)


def _json_blocks(text: str) -> list[tuple[str, dict, object]]:
    """``(kind, attributes, parsed JSON)`` for every fenced ``json`` block."""
    blocks = []
    for info, body in _JSON_FENCE_RE.findall(text):
        words = info.split()
        kind = next((word for word in words if "=" not in word), "")
        attrs = dict(word.split("=", 1) for word in words if "=" in word)
        if "tool" in attrs:
            kind = "tool"
        blocks.append((kind, attrs, json.loads(body)))
    return blocks


def _all_json_blocks() -> list[tuple[str, str, dict, object]]:
    return [
        (path.relative_to(SKILL).as_posix(), kind, attrs, data)
        for path in _skill_files(SKILL)
        for kind, attrs, data in _json_blocks(path.read_text(encoding="utf-8"))
    ]


def test_every_json_block_in_the_skill_says_what_it_is() -> None:
    blocks = _all_json_blocks()
    untagged = [where for where, kind, _, _ in blocks if kind not in _BLOCK_KINDS]
    assert not untagged, f"json blocks with no checkable tag: {untagged}"
    kinds = {kind for _, kind, _, _ in blocks}
    assert kinds == set(_BLOCK_KINDS), f"the skill no longer exercises {set(_BLOCK_KINDS) - kinds}"


_BLOCK_KINDS = ("requirements", "requirements-refused", "tool")


def test_every_example_requirement_set_parses_against_the_schema() -> None:
    from engineering.verification.requirements import parse_requirements

    checked = []
    for where, kind, _attrs, data in _all_json_blocks():
        if kind == "requirements":
            parsed = parse_requirements(data)
            assert parsed.requirements, f"{where}: a requirement block with no typed item"
            checked.append(where)
    assert {"references/requirement-binding.md", "examples/dimensioned-flange.md"} <= set(checked)


def test_every_refused_requirement_set_is_refused() -> None:
    from engineering.verification.requirements import RequirementError, parse_requirements

    refused = [(w, d) for w, kind, _, d in _all_json_blocks() if kind == "requirements-refused"]
    assert refused
    for where, data in refused:
        with pytest.raises(RequirementError, match="source"):
            parse_requirements(data)
        assert where.startswith(("references/", "examples/"))


@pytest.mark.asyncio
async def test_every_tool_call_block_validates_against_the_real_schema() -> None:
    import server

    tools = {tool.name: tool for tool in await server._registered_tools()}
    calls = [(w, attrs["tool"], d) for w, kind, attrs, d in _all_json_blocks() if kind == "tool"]
    assert len(calls) >= 5
    for where, name, args in calls:
        assert name in tools, f"{where}: {name} is not a registered tool"
        problem = server._validate_batch_args(tools[name].parameters, args)
        assert problem is None, f"{where} {name}: {problem}"


@pytest.mark.asyncio
async def test_the_schema_check_on_tool_blocks_bites() -> None:
    import server

    tools = {tool.name: tool for tool in await server._registered_tools()}
    schema = tools["drawing_verify_requirements"].parameters
    assert server._validate_batch_args(schema, {"manifest_paht": "x.json"})
    assert server._validate_batch_args(schema, {"use_stored": "yes"})


# ---------------------------------------------------------------------------
# The examples, run: what they quote is what the server does
# ---------------------------------------------------------------------------


def _example_blocks(name: str) -> dict[str, object]:
    """An example's blocks by ``name=`` (requirements) or by tool name."""
    text = (SKILL / "examples" / f"{name}.md").read_text(encoding="utf-8")
    out: dict[str, object] = {}
    for _kind, attrs, data in _json_blocks(text):
        key = attrs.get("name") or attrs.get("tool")
        if key and key not in out:
            out[key] = data
    return out


def _with_part(requirements: dict, part_id: str) -> dict:
    """The requirement set with every feature-tag ``part`` set to the drawn part's id."""
    data = json.loads(json.dumps(requirements))
    for item in data["items"]:
        for selector in item["features"]:
            if selector["kind"] == "feature_tag":
                selector["part"] = part_id
    return data


@pytest_asyncio.fixture
async def headless_client(monkeypatch):
    from fastmcp import Client

    import server

    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "the examples run headless here, never live"
        yield connected


async def _draw_example_part(client, blocks: dict) -> str:
    async def call(tool, args):
        return (await client.call_tool(tool, args)).structured_content

    await call("drawing_new", {})
    await call("drawing_settings", {"settings": {"units": "mm"}})
    drawn = await call("mech_part_draw", blocks["mech_part_draw"])
    assert drawn["omitted"] == []
    return drawn["part_id"]


def _quoted_measurements(name: str, ids) -> dict[str, float]:
    """The ``Measured`` column of an example's acceptance table, by requirement id."""
    text = (SKILL / "examples" / f"{name}.md").read_text(encoding="utf-8")
    out = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0] in ids:
            out[cells[0]] = float(re.search(r"-?\d+(?:\.\d+)?", cells[2]).group())
    return out


@pytest.mark.asyncio
async def test_the_flange_example_passes_with_the_numbers_it_quotes(headless_client) -> None:
    blocks = _example_blocks("dimensioned-flange")
    part_id = await _draw_example_part(headless_client, blocks)
    requirements = _with_part(blocks["flange"], part_id)
    report = (
        await headless_client.call_tool(
            "drawing_verify_requirements", {"requirements": requirements}
        )
    ).structured_content
    assert (report["status"], report["accepted"]) == ("pass", True), report["blocking"]
    measured = {row["id"]: row["measured"]["value"] for row in report["rows"]}
    assert set(measured) == {"C1", "D1", "D2", "N1", "P1"}
    assert _quoted_measurements("dimensioned-flange", measured) == pytest.approx(measured)
    assert part_id == "3E", "the example says a fresh headless drawing's part_id is 3E"


@pytest.mark.asyncio
async def test_the_ambiguous_example_is_ambiguous_then_resolved(headless_client) -> None:
    blocks = _example_blocks("ambiguous-hole-binding")
    part_id = await _draw_example_part(headless_client, blocks)

    async def verify(requirements):
        return (
            await headless_client.call_tool(
                "drawing_verify_requirements", {"requirements": requirements}
            )
        ).structured_content

    before = await verify(blocks["ambiguous"])
    assert (before["status"], before["accepted"]) == ("incomplete", False)
    row = before["rows"][0]
    assert (row["status"], row["kind"], row["handles"]) == ("ambiguous", "ambiguous_binding", [])
    assert "2 equal candidates, none chosen" in row["reason"]

    after = await verify(_with_part(blocks["resolved"], part_id))
    assert (after["status"], after["accepted"]) == ("pass", True)
    assert len(after["rows"][0]["handles"]) == 1


# ---------------------------------------------------------------------------
# The eval cases (spec §6 Task 22's negative scenarios)
# ---------------------------------------------------------------------------

EVALS = PLUGIN / "evals"
#: The plugin's MCP tools as Claude Code names them: mcp__plugin_<plugin>_<server>__<tool>.
MCP_PREFIX = f"mcp__plugin_{PLUGIN_NAME}_autocad__"
MCP_SERVER_RULE = MCP_PREFIX.rstrip("_")

#: ``prompt.md`` frontmatter keys the CLI 2.1.310 case loader accepts (any other
#: key fails the case: "unknown frontmatter key").
PROMPT_KEYS = {
    "schema_version",
    "name",
    "description",
    "tags",
    "plugins",
    "runs",
    "expected_outcome",
    "model",
    "max_turns",
    "timeout_seconds",
    "allowed_tools",
    "disallowed_tools",
    "artifact_publish",
    "growthbook_overrides",
    "append_system_prompt",
    "env",
}
#: Grader keys per type, read off the CLI 2.1.310 schema (``.strict()`` - an
#: unknown key fails the case). ``name`` is the file name; for ``llm`` the body is
#: ``criteria`` and for ``regex`` it is ``pattern`` when the key is absent.
_COMMON = {"type", "weight", "arm"}
GRADER_KEYS = {
    "regex": _COMMON | {"target", "pattern", "flags", "match"},
    "tool_order": _COMMON | {"before", "after"},
    "tool_used": _COMMON | {"tool", "input_match", "min", "max"},
    "file_exists": _COMMON | {"path", "exists"},
    "llm": _COMMON | {"criteria", "focus"},
    "baseline": _COMMON | {"baseline_file", "criteria"},
}
#: The negative scenarios spec §6 Task 22 lists by name.
REQUIRED_SCENARIOS = {
    "wrong-label",
    "stale-xdata",
    "two-equal-holes",
    "mm-inch-header",
    "schematic-metres",
    "entity-5001",
    "stale-preview",
    "modified-artifact",
    "prompt-injection",
}
#: Spec §6 Task 22: "20 cases" - the named scenarios above and the rest of the
#: suite's negative scenarios.
SUITE_SIZE = 20
#: The grader types spec §6 Task 22 names; the suite uses each at least once.
SPEC_GRADER_TYPES = {"tool_used", "tool_order", "regex", "file_exists", "llm"}


def _front_matter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    assert match, f"{path.relative_to(ROOT).as_posix()}: no YAML front matter"
    data = yaml.safe_load(match.group(1)) or {}
    assert isinstance(data, dict), (
        f"{path.relative_to(ROOT).as_posix()}: front matter is not a mapping"
    )
    return data, match.group(2).strip()


def _cases() -> list[Path]:
    return sorted(path.parent for path in EVALS.glob("*/prompt.md"))


def test_eval_cases_cover_the_named_negative_scenarios() -> None:
    names = {case.name for case in _cases()}
    assert REQUIRED_SCENARIOS <= names, f"missing cases: {sorted(REQUIRED_SCENARIOS - names)}"
    for case in _cases():
        meta, _ = _front_matter(case / "prompt.md")
        assert "negative" in meta.get("tags", []), case.name
        assert case.name in meta.get("tags", []), f"{case.name}: tag the case by its scenario"


@pytest.mark.parametrize("case", _cases(), ids=lambda path: path.name)
def test_eval_case_loads_under_the_cli_schema(case: Path) -> None:
    meta, prompt = _front_matter(case / "prompt.md")
    assert set(meta) <= PROMPT_KEYS, f"unknown prompt.md keys: {sorted(set(meta) - PROMPT_KEYS)}"
    assert prompt, "prompt.md has no prompt"
    assert meta.get("expected_outcome"), "say what the expected behaviour is"
    tools = meta.get("allowed_tools", [])
    assert MCP_SERVER_RULE in tools, "the case cannot reach the plugin's server"
    # A shell grant is refused on a machine that cannot sandbox it (measured on
    # this Windows seat, CLI 2.1.310); the cases reach files through the server.
    assert not {"Bash", "PowerShell"} & set(tools), "a shell grant makes the case unrunnable here"

    graders = sorted((case / "graders").glob("*.md"))
    assert graders, "no graders"
    inventory = _inventory_names()
    kinds = []
    for path in graders:
        data, body = _front_matter(path)
        kind = data.get("type")
        assert kind in GRADER_KEYS, f"{path.name}: unknown grader type {kind!r}"
        assert set(data) <= GRADER_KEYS[kind], (
            f"{path.name}: keys {sorted(set(data) - GRADER_KEYS[kind])}"
        )
        if kind == "llm":
            assert data.get("criteria") or body, f"{path.name}: an llm grader needs criteria"
        if kind == "regex":
            re.compile(data.get("pattern") or body)
        if kind == "tool_used":
            _check_tool_ref(path.name, {"tool": data["tool"], **data}, inventory)
        if kind == "tool_order":
            for side in ("before", "after"):
                assert side in data, f"{path.name}: a tool_order grader needs {side!r}"
                ref = data[side] if isinstance(data[side], dict) else {"tool": data[side]}
                assert set(ref) <= {"tool", "input_match"}, f"{path.name}: {side} keys {ref}"
                _check_tool_ref(path.name, ref, inventory)
        if kind == "file_exists":
            target = data.get("path", "")
            assert target and not Path(target).is_absolute() and ".." not in Path(target).parts, (
                f"{path.name}: a file_exists path is relative to the run's folder, got {target!r}"
            )
            assert target in prompt or target.lstrip("./") in prompt, (
                f"{path.name}: the prompt never names {target}"
            )
        kinds.append((path.stem, kind, data))
    fired = [d for stem, kind, d in kinds if kind == "tool_used" and d.get("tool") == "Skill"]
    assert fired and fired[0].get("arm") == "with-only", "the plugin-fired indicator is missing"
    assert any(kind == "llm" for _, kind, _ in kinds), "no grader judges the behaviour"


def _check_tool_ref(grader: str, ref: dict, inventory: set[str]) -> None:
    """A grader's tool: the plugin server's prefix and a tool the server has."""
    tool = ref["tool"]
    if tool.startswith("mcp__"):
        assert tool.startswith(MCP_PREFIX), f"{grader}: {tool} is not the plugin's server"
        assert tool[len(MCP_PREFIX) :] in inventory, f"{grader}: no tool {tool}"
    if ref.get("input_match"):
        re.compile(ref["input_match"])


def test_the_suite_has_twenty_cases() -> None:
    names = [case.name for case in _cases()]
    assert len(names) == SUITE_SIZE, f"{len(names)} cases: {names}"


def test_the_suite_uses_every_grader_type_the_spec_names() -> None:
    used = {
        _front_matter(path)[0].get("type")
        for case in _cases()
        for path in (case / "graders").glob("*.md")
    }
    assert SPEC_GRADER_TYPES <= used, f"never used: {sorted(SPEC_GRADER_TYPES - used)}"


def test_eval_tool_names_follow_the_plugin_naming() -> None:
    manifest = _json(PLUGIN_MANIFEST)
    servers = _json(PLUGIN_MCP)["mcpServers"]
    assert MCP_PREFIX == f"mcp__plugin_{manifest['name']}_{next(iter(servers))}__"


# ---------------------------------------------------------------------------
# The generated copy
# ---------------------------------------------------------------------------


def test_build_plugin_check_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_build_plugin_check_reports_drift(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import build_plugin
    finally:
        sys.path.remove(str(ROOT / "scripts"))

    copy = tmp_path / "premium"
    build_plugin.build(copy)
    assert build_plugin.differences(copy) == []

    (copy / "references" / "requirement-binding.md").write_text("edited\n", encoding="utf-8")
    (copy / "stray.md").write_text("not in the skill\n", encoding="utf-8")
    (copy / "examples" / "dimensioned-flange.md").unlink()
    drift = build_plugin.differences(copy)
    assert any("requirement-binding.md" in line for line in drift)
    assert any("stray.md" in line for line in drift)
    assert any("dimensioned-flange.md" in line for line in drift)
