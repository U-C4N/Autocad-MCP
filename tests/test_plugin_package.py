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
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

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
    """Snake-case names the skill uses that are not tools: critique focuses.

    ``layer_color`` and ``construction_left`` share a prefix with real tool
    groups (``layer_*``, ``construction_*``) but are values of
    ``drawing_critique(focus=...)``; the code's own list says so.
    """
    from engineering.plan_spec import ALL_CRITIQUE_FOCUSES

    return set(ALL_CRITIQUE_FOCUSES)


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


def test_canonical_skill_is_present() -> None:
    expected = {
        "SKILL.md",
        "references/corner-ops.md",
        "references/iso-standards.md",
        "references/meta-planning.md",
        "references/premium-workflow.md",
        "examples/premium-shaft.md",
        "examples/pid-simple-loop.md",
    }
    present = {path.relative_to(SKILL).as_posix() for path in _skill_files(SKILL)}
    assert expected <= present, f"missing from the skill: {sorted(expected - present)}"


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
        for mention in _tool_mentions(path.read_text(encoding="utf-8"), prefixes, ignore):
            seen.add(mention)
            if mention.endswith("_*"):
                ok = any(name.startswith(mention[:-1]) for name in names)
            else:
                ok = mention in names
            if not ok:
                missing.append(f"{path.relative_to(SKILL).as_posix()}: {mention}")
    assert {"drawing_plan", "entity_trim", "point_from_snap"} <= seen, (
        "the tool-name scan found too little — its patterns no longer match the skill"
    )
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

    (copy / "references" / "corner-ops.md").write_text("edited\n", encoding="utf-8")
    (copy / "stray.md").write_text("not in the skill\n", encoding="utf-8")
    (copy / "examples" / "premium-shaft.md").unlink()
    drift = build_plugin.differences(copy)
    assert any("corner-ops.md" in line for line in drift)
    assert any("stray.md" in line for line in drift)
    assert any("premium-shaft.md" in line for line in drift)
