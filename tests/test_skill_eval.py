"""The skill's A/B evaluation kit (spec §6 Task 22, B55).

``plugin/evals/`` holds the cases (``tests/test_plugin_package.py`` loads them
under the CLI schema). This file holds the rest of the kit:

- ``scripts/build_plugin.py --variant`` writes the arms the evaluation compares -
  the full plugin, the plugin with ``SKILL.md`` only, the plugin with the server
  and no skill - outside git, each with an MCP server that runs this checkout on
  the headless engine. ``claude plugin eval`` passes only ``EVAL_*`` variables
  to its runs (read off the CLI 2.1.310 bundle), so neither
  ``AUTOCAD_MCP_SOURCE`` nor ``AUTOCAD_MCP_BACKEND`` would reach the shipped
  ``.mcp.json``: unconfigured, the plugin's server would be the PyPI release on
  ``auto`` - the live AutoCAD on a seat that runs one. The variant writes both
  into its own ``.mcp.json`` instead, and a test starts that server and asks it
  which engine it runs.
- ``benchmarks/skill_eval/check_artifacts.py`` re-measures the drawings a run
  saved with ``verify_artifact`` - what a grader cannot do: open the file.
- ``benchmarks/skill_eval/README.md`` gives the commands; every
  ``claude plugin eval`` in it carries ``--no-publish`` and a cost ceiling.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import ezdxf
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"
EVALS = PLUGIN / "evals"
BUILD_SCRIPT = ROOT / "scripts" / "build_plugin.py"
SKILL_EVAL = ROOT / "benchmarks" / "skill_eval"
CHECKS = SKILL_EVAL / "artifact_checks.json"
CHECK_SCRIPT = SKILL_EVAL / "check_artifacts.py"


def _import(path: Path):
    sys.path.insert(0, str(path.parent))
    try:
        return __import__(path.stem)
    finally:
        sys.path.remove(str(path.parent))


@pytest.fixture(scope="module")
def build_plugin():
    return _import(BUILD_SCRIPT)


@pytest.fixture(scope="module")
def check_artifacts():
    return _import(CHECK_SCRIPT)


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------------------
# The arms: generated variants outside git
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["full", "skill-only", "server-only"])
def test_a_variant_carries_the_manifest_and_the_cases(build_plugin, tmp_path, variant) -> None:
    out = tmp_path / variant
    build_plugin.build_variant(variant, out)
    manifest = json.loads((out / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    shipped = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == shipped["name"], "the tool names graders use depend on it"
    cases = {p.parent.name for p in (out / "evals").glob("*/prompt.md")}
    assert cases == {p.parent.name for p in EVALS.glob("*/prompt.md")}
    assert not (out / "evals" / "results").exists()
    assert not (out / "evals" / "mocks").exists()


def test_the_three_variants_differ_only_in_the_skill(build_plugin, tmp_path) -> None:
    skill = ROOT / ".claude" / "skills" / "autocad-mcp-premium"
    for variant in ("full", "skill-only", "server-only"):
        build_plugin.build_variant(variant, tmp_path / variant)
    full = _files(tmp_path / "full" / "skills" / "premium")
    assert full == _files(skill), "the full arm is the whole canonical skill"
    assert any(name.startswith("references/") for name in full)
    assert _files(tmp_path / "skill-only" / "skills" / "premium") == {"SKILL.md"}
    assert not (tmp_path / "server-only" / "skills").exists()
    assert (tmp_path / "skill-only" / "skills" / "premium" / "SKILL.md").read_bytes() == (
        skill / "SKILL.md"
    ).read_bytes()


def test_a_variant_server_runs_this_checkout_headless(build_plugin, tmp_path) -> None:
    out = tmp_path / "full"
    build_plugin.build_variant("full", out, python="C:/py/python.exe")
    servers = json.loads((out / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    shipped = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert list(servers) == list(shipped), "the server name is part of every tool name"
    (server,) = servers.values()
    assert server["command"] == "C:/py/python.exe"
    assert server["args"] == [(ROOT / "server.py").as_posix()]
    assert server["env"]["AUTOCAD_MCP_BACKEND"] == "ezdxf"


@pytest.mark.asyncio
async def test_the_variant_server_starts_and_reports_the_headless_engine(
    build_plugin, tmp_path
) -> None:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    out = tmp_path / "full"
    build_plugin.build_variant("full", out)
    (server,) = json.loads((out / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"].values()
    # only what the eval passes: the variant's own env, never this process's
    # AUTOCAD_MCP_BACKEND (conftest/monkeypatch would hide the bug otherwise)
    env = {key: value for key, value in __import__("os").environ.items() if key in _BASE_ENV}
    env.update(server["env"])
    transport = StdioTransport(server["command"], server["args"], env=env, cwd=str(tmp_path))
    async with Client(transport) as client:
        status = (await client.call_tool("system_status", {})).structured_content
    assert status["backend"] == "ezdxf"


#: What a Windows child process needs to start Python at all.
_BASE_ENV = {"SYSTEMROOT", "SystemRoot", "PATH", "Path", "TEMP", "TMP", "USERPROFILE", "HOME"}


def test_a_variant_refuses_a_folder_git_would_track(build_plugin) -> None:
    for inside in (ROOT / "plugin", ROOT / "benchmarks" / "skill-only", ROOT):
        with pytest.raises(ValueError, match="outside git"):
            build_plugin.build_variant("skill-only", inside)
    # out/ is ignored by git: the documented place
    assert build_plugin.variant_target_ok(ROOT / "out" / "plugin-skill-only")


def test_a_variant_replaces_only_a_folder_it_wrote(build_plugin, tmp_path) -> None:
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "notes.txt").write_text("keep me\n", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to replace"):
        build_plugin.build_variant("full", precious)
    assert (precious / "notes.txt").is_file()

    arm = tmp_path / "arm"
    build_plugin.build_variant("full", arm)
    build_plugin.build_variant("server-only", arm)  # its own previous output: replaced
    assert not (arm / "skills").exists()
    assert (arm / build_plugin.VARIANT_MARKER).read_text(encoding="utf-8").strip() == "server-only"


def test_an_unknown_variant_is_refused(build_plugin, tmp_path) -> None:
    with pytest.raises(ValueError, match="skill-only"):
        build_plugin.build_variant("half", tmp_path / "x")


def test_the_variant_command_line(tmp_path) -> None:
    out = tmp_path / "plugin-skill-only"
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--variant", "skill-only", "--out", str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (out / "skills" / "premium" / "SKILL.md").is_file()
    missing_out = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--variant", "skill-only"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert missing_out.returncode == 2
    assert "--out" in missing_out.stderr


# ---------------------------------------------------------------------------
# check_artifacts: the saved drawings measured again
# ---------------------------------------------------------------------------


def _front_matter(path: Path) -> tuple[dict, str]:
    match = re.match(r"^---\n(.*?)\n---\n?(.*)$", path.read_text(encoding="utf-8"), re.S)
    assert match, path
    return yaml.safe_load(match.group(1)) or {}, match.group(2)


def test_every_artifact_check_names_a_case_that_saves_that_file(check_artifacts) -> None:
    from engineering.verification.requirements import parse_requirements

    checks = check_artifacts.load_checks()
    assert checks, "no artifact checks"
    for name, check in checks.items():
        case = EVALS / name
        assert (case / "prompt.md").is_file(), f"{name}: no such case"
        _, prompt = _front_matter(case / "prompt.md")
        assert check["path"] in prompt, f"{name}: the prompt never names {check['path']}"
        graders = [_front_matter(p)[0] for p in (case / "graders").glob("*.md")]
        assert any(
            g.get("type") == "file_exists" and g.get("path") == check["path"] for g in graders
        ), f"{name}: no file_exists grader for {check['path']}"
        assert check["expect"] in ("pass", "fail")
        parse_requirements(check["requirements"])


def _bracket(path: Path, h2_x: float) -> Path:
    doc = ezdxf.new("R2010", units=4)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (80, 0), (80, 50), (0, 50)], close=True)
    msp.add_circle((20, 25), 5)
    msp.add_circle((h2_x, 25), 5)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return path


@pytest.mark.asyncio
async def test_a_corrected_saved_bracket_meets_its_check(check_artifacts, tmp_path) -> None:
    _bracket(tmp_path / "bracket.dxf", 60)
    result = await check_artifacts.check_case("saved-not-in-memory", root=tmp_path)
    assert result["status"] == "as_expected", result
    assert result["report"]["status"] == "pass"
    measured = {row["id"]: row["measured"]["value"] for row in result["report"]["rows"]}
    assert measured["S1"] == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_a_bracket_saved_with_the_hole_still_moved_fails(check_artifacts, tmp_path) -> None:
    _bracket(tmp_path / "bracket.dxf", 65)
    result = await check_artifacts.check_case("saved-not-in-memory", root=tmp_path)
    assert result["status"] == "not_as_expected"
    assert result["report"]["status"] == "fail"
    measured = {row["id"]: row["measured"]["value"] for row in result["report"]["rows"]}
    assert measured["S1"] == pytest.approx(45.0)


@pytest.mark.asyncio
async def test_a_missing_artifact_is_reported_never_skipped(check_artifacts, tmp_path) -> None:
    result = await check_artifacts.check_case("saved-not-in-memory", root=tmp_path)
    assert result["status"] == "missing"
    assert "bracket.dxf" in result["reason"]


def test_the_command_line_exit_codes(tmp_path) -> None:
    def run(*args):
        return subprocess.run(
            [sys.executable, str(CHECK_SCRIPT), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )

    good = _bracket(tmp_path / "good" / "bracket.dxf", 60)
    report = tmp_path / "report.json"
    ok = run("--case", "saved-not-in-memory", "--root", str(good.parent), "--json", str(report))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    data = json.loads(report.read_text(encoding="utf-8"))
    assert [row["status"] for row in data["cases"]] == ["as_expected"]

    _bracket(tmp_path / "bad" / "bracket.dxf", 65)
    bad = run("--case", "saved-not-in-memory", "--root", str(tmp_path / "bad"))
    assert bad.returncode == 1, bad.stdout + bad.stderr

    explicit = run("--case", "saved-not-in-memory", "--file", str(good))
    assert explicit.returncode == 0, explicit.stdout + explicit.stderr

    unknown = run("--case", "no-such-case", "--root", str(tmp_path))
    assert unknown.returncode == 2
    assert "no-such-case" in unknown.stderr


# ---------------------------------------------------------------------------
# The README: the commands for the three arms
# ---------------------------------------------------------------------------


def _commands(text: str) -> list[str]:
    """Every shell line of the README's fenced blocks, continuations joined."""
    lines: list[str] = []
    for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.S):
        joined = re.sub(r"\s*[\\^`]\n\s*", " ", block)
        lines += [line.strip() for line in joined.splitlines() if line.strip()]
    return lines


def test_the_readme_gives_every_eval_run_its_guards() -> None:
    text = (SKILL_EVAL / "README.md").read_text(encoding="utf-8")
    runs = [line for line in _commands(text) if line.startswith("claude plugin eval ")]
    assert len(runs) >= 3, "a command per arm"
    for line in runs:
        for flag in ("--no-publish", "--max-cost-usd", "--model", "--allow-real-servers"):
            assert flag in line, f"{flag} missing from: {line}"
        target = line.split()[3]
        assert target.startswith("out/"), (
            f"{target}: the shipped plugin/ would run the PyPI release on the auto engine"
        )
    builds = [line for line in _commands(text) if "build_plugin.py --variant" in line]
    assert {re.search(r"--variant (\S+)", line).group(1) for line in builds} == {
        "full",
        "skill-only",
        "server-only",
    }
    assert "check_artifacts.py" in text
