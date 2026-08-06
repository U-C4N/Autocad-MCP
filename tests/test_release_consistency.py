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
