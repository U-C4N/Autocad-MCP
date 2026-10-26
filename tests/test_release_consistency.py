"""Release-consistency gates (v1.4.0 Faz 0).

Locks the version string, the README release snapshot, the server.py section
headers, and the Dockerfile file selection to the actual state of the tree so
none of them can drift again:

- pyproject version == CHANGELOG top release == README snapshot major.minor
- version.py fallback parser returns the pyproject version
- README snapshot tool/resource/prompt counts == the generated tool inventory
- every ``SECTION n: ... (N tools)`` header matches the decorators below it
- server.json version == pyproject, and the README carries the mcp-name marker
- every wheel ``only-include`` entry is COPY'd into the Docker image

Two of these used to count the literal text ``@mcp.tool(`` in server.py, which
made that string load-bearing in prose: an example in a docstring inflated the
count. Neither does any more.

* The README snapshot counts now come from ``docs/tool-inventory.json``, which
  ``tests/test_tool_inventory.py`` holds equal to the live component registry —
  so the chain README -> inventory -> registry is closed without this module
  having to import the server.
* The section-header gate stays (a "SECTION n" is a fact about server.py's
  layout that no runtime registry can see) but counts *parsed* decorators
  instead of matched text.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)


def _pyproject_version() -> str:
    return str(_pyproject()["project"]["version"])


def _server_source() -> str:
    return (ROOT / "server.py").read_text(encoding="utf-8")


SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def test_pyproject_version_is_semver() -> None:
    assert SEMVER_RE.match(_pyproject_version()), (
        f"pyproject version {_pyproject_version()!r} is not MAJOR.MINOR.PATCH"
    )


def test_version_py_fallback_matches_pyproject(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pyproject fallback in version.py must resolve the same version.

    The installed-metadata short circuit is forced to miss so the test checks
    our parser (the code path a source checkout without an install uses).
    """
    import version as version_module

    def _raise(_name: str) -> str:
        raise version_module.PackageNotFoundError

    monkeypatch.setattr(version_module, "version", _raise)
    assert version_module.package_version() == _pyproject_version()


def test_changelog_top_release_matches_pyproject() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog, flags=re.MULTILINE)
    assert match, "CHANGELOG.md has no '## [X.Y.Z]' release heading"
    assert match.group(1) == _pyproject_version(), (
        f"CHANGELOG top release {match.group(1)} != pyproject {_pyproject_version()}"
    )


def _readme_snapshot() -> tuple[str, int, int, int]:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    # The snapshot may wrap across blockquote lines; join continuations first.
    flat = re.sub(r"\n>\s*", " ", readme)
    match = re.search(
        r"\*\*v(\d+\.\d+) release snapshot:\*\*\s*(\d+) tools\D+?(\d+) resources"
        r"\D+?(\d+) prompt templates",
        flat,
    )
    assert match, "README.md release-snapshot line not found or malformed"
    return match.group(1), int(match.group(2)), int(match.group(3)), int(match.group(4))


def test_readme_snapshot_matches_pyproject_minor() -> None:
    snapshot_minor, _, _, _ = _readme_snapshot()
    expected = ".".join(_pyproject_version().split(".")[:2])
    assert snapshot_minor == expected, (
        f"README snapshot v{snapshot_minor} != pyproject major.minor v{expected}"
    )


def test_readme_snapshot_counts_match_registrations() -> None:
    """README snapshot == the surface, via the generated inventory.

    The counts used to be a regex tally of ``@mcp.tool(`` / ``@mcp.resource(`` /
    ``@mcp.prompt(`` in server.py's text. They now come from
    ``docs/tool-inventory.json``, which is built from the live component
    registry and gated against it by tests/test_tool_inventory.py — so this
    still fails when a registration is added without updating the README, but
    it can no longer be moved by prose that merely *mentions* a decorator.
    """
    inventory = json.loads((ROOT / "docs" / "tool-inventory.json").read_text(encoding="utf-8"))
    totals = inventory["totals"]
    live = (totals["tools"], totals["resources"], totals["prompts"])
    _, readme_tools, readme_resources, readme_prompts = _readme_snapshot()
    assert (readme_tools, readme_resources, readme_prompts) == live, (
        f"README snapshot says {readme_tools}/{readme_resources}/{readme_prompts} "
        f"(tools/resources/prompts) but the server registers {live[0]}/{live[1]}/{live[2]} "
        "(per docs/tool-inventory.json; regenerate it with "
        "`python scripts/generate_tool_inventory.py` if that is the stale one)"
    )


def _tool_decorator_lines(src: str) -> list[int]:
    """Line number of every ``mcp.tool`` decorator, parsed rather than matched.

    Parsing is what keeps the *string* ``@mcp.tool(`` from being load-bearing:
    an occurrence in a docstring or comment is not a decorator node, so it
    cannot inflate a section's count.
    """
    lines: list[int] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            # Both spellings count: `@mcp.tool` and `@mcp.tool(...)`.
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "tool"
                and isinstance(target.value, ast.Name)
                and target.value.id == "mcp"
            ):
                lines.append(decorator.lineno)
    return sorted(lines)


def test_section_header_counts_match_decorators() -> None:
    """Kept, and not replaceable by the inventory: a section is a source-layout
    fact (which tools sit under which header comment), invisible to the live
    registry, and it does not line up with the tag-derived groups the inventory
    records — SECTION 8 alone spans analysis + batch + templates + validation."""
    src = _server_source()
    lines = src.splitlines()
    decorators = _tool_decorator_lines(src)
    # Segment boundaries: every SECTION header (counted or not) ends a segment.
    boundaries = [
        number
        for number, line in enumerate(lines, start=1)
        if re.search(r"#[^\n]*SECTION [\w]+:", line)
    ]
    boundaries.append(len(lines) + 1)
    headers = [
        (number, match.group(1), match.group(2))
        for number, line in enumerate(lines, start=1)
        if (match := re.search(r"#[^\n]*SECTION ([\w]+):[^\n]*?\((\d+) tools?\)", line))
    ]
    assert headers, "no counted SECTION headers found in server.py"
    mismatches: list[str] = []
    for start, section_id, declared in headers:
        end = min(b for b in boundaries if b > start)
        actual = sum(1 for line in decorators if start <= line < end)
        if actual != int(declared):
            mismatches.append(f"SECTION {section_id}: header says {declared}, actual {actual}")
    assert not mismatches, "; ".join(mismatches)


def test_server_json_versions_match_pyproject() -> None:
    """MCP registry manifest must ship the same version as the package."""
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    expected = _pyproject_version()
    assert manifest["version"] == expected, (
        f"server.json version {manifest['version']} != pyproject {expected}"
    )
    for package in manifest["packages"]:
        assert package["version"] == expected, (
            f"server.json package {package['identifier']} version "
            f"{package['version']} != pyproject {expected}"
        )
        if package["registryType"] == "pypi":
            assert package["identifier"] == _pyproject()["project"]["name"]


def test_uv_lock_records_the_pyproject_version() -> None:
    """`uv sync --locked` refuses a lock that disagrees with pyproject.

    This one is cheap and was learned the expensive way. The v1.5.0 lock was
    generated before the version bump, so it recorded the project's own package
    as 1.4.0 while pyproject said 1.5.0. No dependency differed — the whole
    transitive graph was identical — but `--locked` will not re-resolve, so
    every locked CI lane (lint, test-linux 3.11, test-linux 3.12, test-windows)
    died on `uv sync` before running a single test.

    The rest of this module chains pyproject -> version.py -> CHANGELOG ->
    README -> server.json. The lockfile was the one release artifact carrying a
    version string that nothing checked.
    """
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    name = _pyproject()["project"]["name"]
    expected = _pyproject_version()

    match = re.search(
        rf'^\[\[package\]\]\nname = "{re.escape(name)}"\nversion = "([^"]+)"',
        lock,
        re.MULTILINE,
    )
    assert match, f"uv.lock has no [[package]] entry for {name!r}"
    assert match.group(1) == expected, (
        f"uv.lock records {name} {match.group(1)} but pyproject says {expected} — "
        "run `uv lock` after bumping the version, or every locked CI lane fails "
        "at `uv sync --locked` before a test runs"
    )


def test_readme_contains_mcp_name_marker() -> None:
    """PyPI ownership validation for the MCP registry needs this marker in the
    README (which ships to PyPI as the long description)."""
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"mcp-name: {manifest['name']}" in readme


def test_dockerfile_copies_every_wheel_include() -> None:
    """The Docker image must contain everything the wheel ships (GH bug: the
    image previously missed engineering/ and version.py)."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copy_lines = "\n".join(
        line for line in dockerfile.splitlines() if line.strip().upper().startswith("COPY")
    )
    includes = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["only-include"]
    missing = [name for name in includes if name not in copy_lines]
    assert not missing, f"Dockerfile COPY misses wheel-shipped paths: {missing}"


def test_windows_only_dependencies_carry_a_platform_marker() -> None:
    """`pip install autocad-mcp-pro[full]` must resolve on Linux and macOS.

    pywin32 publishes Windows wheels and **no sdist**, so an unmarked
    `pywin32>=306` in an extra makes that extra uninstallable everywhere else:
    pip fails at resolution with "no matching distribution" before it reaches
    anything else. `[full]` is the extra this project's own README recommends
    to contributors, and CI never caught it because every Linux lane installs
    `[pdf]` or uses uv.lock, whose resolution markers paper over the gap.
    """
    extras = _pyproject()["project"]["optional-dependencies"]
    unmarked: list[str] = []
    for extra, requirements in extras.items():
        for requirement in requirements:
            name = re.split(r"[<>=!~\[; ]", requirement, maxsplit=1)[0].strip().lower()
            if name in {"pywin32"} and "sys_platform" not in requirement:
                unmarked.append(f"{extra}: {requirement}")
    assert not unmarked, (
        'Windows-only distributions need `; sys_platform == "win32"` or the '
        f"extra cannot be installed off Windows: {unmarked}"
    )


def test_document_numbers_match_the_tree() -> None:
    """Every figure the docs quote, recomputed from its source of truth.

    The A/B suite grew 24 -> 26 and three files kept saying 24: `README.md`
    (fixed by hand), `benchmarks/README.md` and `CLAUDE.md` (not). Each was
    edited on its own and nobody re-read the other two, so the repository
    shipped three different answers to one question. That is a mechanical
    failure and it deserves a mechanical gate rather than another careful
    reader.

    `scripts/check_doc_numbers.py` also fails when a pattern stops matching, so
    rewording the sentence around a number cannot silently disarm the check.
    """
    import subprocess

    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_doc_numbers.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_pre_commit_ruff_matches_the_project_pin() -> None:
    """A pre-commit hook that formats differently from CI is a trap, armed.

    `.pre-commit-config.yaml` sat at ruff v0.8.6 while `[dependency-groups] dev`
    pinned 0.16.1, and 0.16 formats Python inside Markdown where 0.8 does not.
    Anyone who ran `pre-commit install` therefore produced commits that failed
    CI's own `ruff format --check .` -- the same class of failure the ruff pin
    was introduced to end.
    """
    config = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    hook = re.search(r"ruff-pre-commit\s*\n\s*rev:\s*v?([\d.]+)", config)
    assert hook, ".pre-commit-config.yaml no longer pins ruff-pre-commit"

    dev_group = _pyproject()["dependency-groups"]["dev"]
    pinned = next((d for d in dev_group if d.startswith("ruff==")), None)
    assert pinned, "pyproject no longer pins ruff in [dependency-groups] dev"

    assert hook.group(1) == pinned.split("==", 1)[1], (
        f"pre-commit runs ruff {hook.group(1)} but the project pins {pinned} — "
        "a contributor's hook would format differently from CI"
    )


def test_the_release_gate_covers_the_platforms_ci_covers() -> None:
    """`release.yml` publishes to PyPI; a lane CI has and it lacks is a hole.

    Its test job was one ubuntu/3.11 run, so a Windows-only defect could reach
    PyPI with no Windows job having executed. v1.5.0 shipped exactly such a
    defect (`except pywintypes.com_error` -> NameError without pywin32).
    """
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    gate = release.split("build:", 1)[0]

    assert "windows-latest" in gate, "the release test gate never runs on Windows"
    assert "3.12" in gate, "the release test gate never runs on Python 3.12"
    assert "uv sync --locked" in gate, "the release gate must install from the lockfile"


# ── 1.7.0 release gate (spec §6 Task 23, B64) ────────────────────────────────
#
# One version in every artifact a release writes, the CI and release workflows
# running the gates the release depends on, the built distributions held to
# what they may carry, and two 1.6.0 changelog claims (triage W4-T21-05 and
# W4-T21-08) corrected to what the code and its tests show.


def _json_file(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _release_versions() -> dict[str, str]:
    """Every version string a release writes, by where it is written."""
    import version as version_module

    found: dict[str, str] = {"version.py": version_module.package_version()}
    server = _json_file("server.json")
    found["server.json version"] = server["version"]
    for package in server["packages"]:
        found[f"server.json package {package['identifier']}"] = package["version"]

    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    name = _pyproject()["project"]["name"]
    own = re.search(
        rf'^\[\[package\]\]\nname = "{re.escape(name)}"\nversion = "([^"]+)"', lock, re.MULTILINE
    )
    found["uv.lock"] = own.group(1) if own else "<missing>"

    plugin = _json_file("plugin/.claude-plugin/plugin.json")
    found["plugin/.claude-plugin/plugin.json"] = plugin["version"]
    for entry in _json_file(".claude-plugin/marketplace.json")["plugins"]:
        found[f"marketplace entry {entry['name']}"] = entry["version"]

    servers = _json_file("plugin/.mcp.json")["mcpServers"]
    pins = [
        pin
        for server_entry in servers.values()
        for arg in server_entry.get("args", [])
        for pin in re.findall(r"==(\d+\.\d+\.\d+)", arg)
    ]
    found["plugin/.mcp.json pin"] = pins[0] if len(pins) == 1 else f"<{len(pins)} pins>"

    readme_pins = re.findall(
        rf"{re.escape(name)}\[[^\]]*\]==(\d+\.\d+\.\d+)",
        (ROOT / "README.md").read_text(encoding="utf-8"),
    )
    found["README plugin pin"] = (
        readme_pins[0] if len(readme_pins) == 1 else f"<{len(readme_pins)} pins>"
    )
    return found


def test_every_release_artifact_carries_the_pyproject_version() -> None:
    """pyproject, version.py, server.json, uv.lock, the plugin, its marketplace
    entry, the MCP pin and the README's copy of it are eight places one release
    number is written; a release that moves only some of them installs the
    previous server from the plugin (the pin), tells a reader to install it
    (the README) or fails every locked CI lane (the lock)."""
    expected = _pyproject_version()
    disagree = {where: got for where, got in _release_versions().items() if got != expected}
    assert not disagree, f"pyproject says {expected}; these disagree: {disagree}"


def _workflow(name: str) -> dict:
    import yaml

    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def _runs(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in job.get("steps", []))


def _jobs_running(workflow: dict, needle: str) -> list[str]:
    return [name for name, job in workflow["jobs"].items() if needle in _runs(job)]


def _needs(job: dict) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_the_plugin_and_marketplace_are_validated_with_a_pinned_claude_code(name: str) -> None:
    """`claude plugin validate --strict` is the only check of the manifests the
    runtime itself reads; a floating CLI version would move the verdict on a
    release nobody chose (the ruff lesson)."""
    workflow = _workflow(name)
    version = str(workflow.get("env", {}).get("CLAUDE_CODE_VERSION", ""))
    assert SEMVER_RE.match(version), f"{name}: CLAUDE_CODE_VERSION {version!r} is not exact"
    jobs = _jobs_running(workflow, "claude plugin validate")
    assert jobs, f"{name}: no job runs `claude plugin validate`"
    runs = "\n".join(_runs(workflow["jobs"][job]) for job in jobs)
    assert "@anthropic-ai/claude-code@${{ env.CLAUDE_CODE_VERSION }}" in runs, (
        f"{name}: the Claude Code CLI is not installed at the pinned version"
    )
    assert re.search(r"claude plugin validate --strict \.(\s|$)", runs), (
        f"{name}: the marketplace (repository root) is not validated with --strict"
    )
    assert "claude plugin validate --strict ./plugin" in runs, (
        f"{name}: the plugin is not validated with --strict"
    )


GENERATED_FILE_GATES = (
    "scripts/build_plugin.py --check",
    "scripts/generate_tool_inventory.py --check",
    "scripts/check_doc_numbers.py",
)


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_the_generated_file_and_document_gates_run_in_ci_and_release(name: str) -> None:
    runs = "\n".join(_runs(job) for job in _workflow(name)["jobs"].values())
    missing = [gate for gate in GENERATED_FILE_GATES if gate not in runs]
    assert not missing, f"{name} never runs {missing}"


def test_the_release_is_gated_on_no_correctness_regression_in_a_headless_lane() -> None:
    """The A/B suite against the previous tag, as a gate (exit 1 on any check
    that passed there and does not pass now), on the headless engine, before
    anything is built - and with the history the baseline worktree needs."""
    release = _workflow("release.yml")
    jobs = [
        name
        for name in _jobs_running(release, "benchmarks/compare_versions.py")
        if "--fail-on-regression" in _runs(release["jobs"][name])
    ]
    assert jobs, "release.yml never runs compare_versions.py --fail-on-regression"
    for name in jobs:
        job = release["jobs"][name]
        assert "ubuntu" in job["runs-on"], f"{name}: the A/B lane is not the headless one"
        env = {**release.get("env", {}), **job.get("env", {})}
        for step in job["steps"]:
            if "compare_versions" in str(step.get("run", "")):
                env.update(step.get("env", {}))
        assert env.get("AUTOCAD_MCP_BACKEND") == "ezdxf", f"{name}: backend not pinned to ezdxf"
        checkout = next(
            s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")
        )
        assert checkout.get("with", {}).get("fetch-depth") == 0, (
            f"{name}: a shallow checkout has no previous tag to build the baseline worktree from"
        )
        assert "uv sync --locked" in _runs(job), f"{name}: the A/B lane is not installed locked"
    build_needs = _needs(release["jobs"]["build"])
    assert set(jobs) <= build_needs, f"build does not wait for the A/B lane {jobs}"
    validate = _jobs_running(release, "claude plugin validate")
    assert set(validate) <= build_needs, f"build does not wait for plugin validation {validate}"


@pytest.mark.parametrize(("name", "job"), [("ci.yml", "package"), ("release.yml", "build")])
def test_built_distributions_are_checked_before_they_leave(name: str, job: str) -> None:
    runs = _runs(_workflow(name)["jobs"][job])
    assert "python -m build" in runs
    assert "scripts/check_dist_contents.py dist" in runs, (
        f"{name} {job}: the wheel and sdist are built but their contents are never checked"
    )
    assert runs.index("python -m build") < runs.index("scripts/check_dist_contents.py")


def _changelog_section(version: str) -> str:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    start = changelog.index(f"## [{version}]")
    end = changelog.find("\n## [", start + 1)
    return changelog[start : end if end != -1 else len(changelog)]


def test_the_top_release_says_what_it_does_not_ship() -> None:
    version = _pyproject_version()
    major_minor = ".".join(version.split(".")[:2])
    section = _changelog_section(version)
    assert re.search(rf"^### Not in {re.escape(major_minor)}(\.\d+)?\s*$", section, re.M), (
        f"CHANGELOG [{version}] has no '### Not in {major_minor}' section"
    )


def test_the_chord_walk_shortfall_quoted_for_a_semicircular_jump_is_the_tested_one() -> None:
    """Triage W4-T21-05. The jump tests/test_pid_graph.py measures is a
    polyline (-20,0) -> (0,0) -bulge 1-> (10,0) -> (30,0): 40 + 5 pi long, and
    the chord walk over its vertices reads 50. "12.5 %" was the v1.5 figure for
    a square's perimeter with a semicircular edge, not this geometry."""
    import math

    true_length = 40 + 5 * math.pi
    chord_walk = 20 + 10 + 20
    quoted = f"{100 * (true_length - chord_walk) / true_length:.2f} %"
    assert quoted == "10.25 %"
    graph = (ROOT / "engineering" / "pid" / "graph.py").read_text(encoding="utf-8")
    flat = re.sub(r"\s*\n\s*#?\s*", " ", graph)
    assert f"{quoted} short on a semicircular jump" in flat
    assert "12.5% short" not in flat and "12.5 % short" not in flat
    section = re.sub(r"\s*\n\s*", " ", _changelog_section("1.6.0"))
    assert f"({quoted} short on a semicircular jump)" in section
    assert "(12.5 % short" not in section


def test_the_changelog_credits_layer_set_intent_mismatch_to_preflight_only() -> None:
    """Triage W4-T21-08. `drawing_preflight` reports the conflict code;
    `drawing_plan` only appends a warning string, which carries no code."""
    from engineering.preflight import pid_plan_warnings, preflight_drawing

    requirements = {"units": "mm", "part_type": "pid", "dimensions": {}, "tolerance_policy": "none"}
    preflight = preflight_drawing("P&ID for the feed section", requirements, layer_set_id="mech")
    assert "LAYER_SET_INTENT_MISMATCH" in [c.code for c in preflight.conflicts]
    warnings = pid_plan_warnings("P&ID for the feed section", "mech")
    assert warnings and not any("LAYER_SET_INTENT_MISMATCH" in w for w in warnings)

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    bullets = [
        re.sub(r"\s*\n\s*", " ", bullet)
        for bullet in re.split(r"\n(?=- |\n)", changelog)
        if "LAYER_SET_INTENT_MISMATCH" in bullet
    ]
    assert bullets, "the changelog no longer mentions LAYER_SET_INTENT_MISMATCH"
    for bullet in bullets:
        assert not re.search(
            r"`drawing_plan`[^.]*\breports?\b[^.]*LAYER_SET_INTENT_MISMATCH", bullet
        ), f"credits drawing_plan with the code: {bullet[:200]}"
        assert "`drawing_preflight` reports `LAYER_SET_INTENT_MISMATCH`" in bullet


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lower-case, punctuation dropped, spaces to '-'."""
    text = re.sub(r"[`*]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def test_every_in_page_link_in_the_readme_lands_on_a_heading() -> None:
    """The nav row links `#whats-new-in-<release>`; renaming the section for a
    release without the link leaves a dead anchor, on PyPI's page too."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    slugs = {_slug(h) for h in re.findall(r"^#{1,6} (.+)$", readme, re.M)}
    anchors = set(re.findall(r"\]\(#([\w\-]+)\)", readme))
    assert anchors, "README has no in-page links"
    assert not sorted(anchors - slugs), f"dead README anchors: {sorted(anchors - slugs)}"


def test_the_readme_install_names_the_marketplace_and_plugin_the_manifests_define() -> None:
    marketplace = _json_file(".claude-plugin/marketplace.json")
    plugin = _json_file("plugin/.claude-plugin/plugin.json")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "claude plugin marketplace add U-C4N/Autocad-MCP" in readme
    assert f"claude plugin install {plugin['name']}@{marketplace['name']}" in readme
