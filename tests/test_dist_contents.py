"""The built wheel and sdist carry what they may, and nothing else (spec §6 Task 23).

``scripts/check_dist_contents.py dist/`` runs in CI's ``package`` job and in the
release ``build`` job after ``python -m build``. These tests drive it on
archives written here, laid out as hatchling lays them out (a wheel of the
``only-include`` paths plus ``<name>-<version>.dist-info``; an sdist under
``<name>-<version>/`` with ``PKG-INFO``), so the gate is tested without a build
backend in the test environment. The real archives are checked by CI and by
the release gate.
"""

from __future__ import annotations

import importlib.util
import io
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_dist_contents", ROOT / "scripts" / "check_dist_contents.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()

with (ROOT / "pyproject.toml").open("rb") as _stream:
    _PYPROJECT = tomllib.load(_stream)
VERSION = _PYPROJECT["project"]["version"]
INCLUDES = _PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["only-include"]
STEM = f"autocad_mcp_pro-{VERSION}"


def _one_member_per_include() -> list[str]:
    members = []
    for include in INCLUDES:
        members.append(include if include.endswith(".py") else f"{include}/__init__.py")
    return members


def _write_wheel(
    folder: Path, extra: tuple[str, ...] | list[str] = (), drop: str | None = None
) -> Path:
    path = folder / f"{STEM}-py3-none-any.whl"
    members = [m for m in _one_member_per_include() if m != drop] + list(extra)
    members += [f"{STEM}.dist-info/METADATA", f"{STEM}.dist-info/RECORD"]
    with zipfile.ZipFile(path, "w") as archive:
        for member in members:
            archive.writestr(member, "x")
    return path


def _write_sdist(folder: Path, extra: tuple[str, ...] | list[str] = ()) -> Path:
    path = folder / f"{STEM}.tar.gz"
    members = ["PKG-INFO", "pyproject.toml", "README.md", "LICENSE"]
    members += _one_member_per_include() + list(extra)
    with tarfile.open(path, "w:gz") as archive:
        for member in members:
            data = b"x"
            info = tarfile.TarInfo(f"{STEM}/{member}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def _tracked() -> set[str]:
    return {"pyproject.toml", "README.md", "LICENSE", *_one_member_per_include()} | {
        "plugin/.claude-plugin/plugin.json",
        ".claude/skills/autocad-mcp-premium/SKILL.md",
    }


def _problems(folder: Path, tracked: set[str] | None = None) -> list[str]:
    return checker.check(folder, tracked=_tracked() if tracked is None else tracked)


def test_a_wheel_and_sdist_laid_out_as_hatchling_lays_them_out_pass(tmp_path: Path) -> None:
    _write_wheel(tmp_path)
    _write_sdist(tmp_path)
    assert _problems(tmp_path) == []


@pytest.mark.parametrize(
    "member",
    [
        "plugin/skills/premium/SKILL.md",
        ".claude/skills/autocad-mcp-premium/SKILL.md",
        "tests/test_server.py",
        "benchmarks/correctness_suite.py",
    ],
)
def test_the_plugin_the_skill_and_the_repository_tooling_are_not_in_the_wheel(
    tmp_path: Path, member: str
) -> None:
    _write_wheel(tmp_path, extra=[member])
    _write_sdist(tmp_path)
    problems = _problems(tmp_path)
    assert len(problems) == 1 and member in problems[0], problems


def test_a_wheel_missing_a_shipped_package_fails_by_name(tmp_path: Path) -> None:
    _write_wheel(tmp_path, drop="engineering/__init__.py")
    _write_sdist(tmp_path)
    problems = _problems(tmp_path)
    assert any("engineering" in p and "missing" in p for p in problems), problems


def test_compiled_files_are_not_shipped(tmp_path: Path) -> None:
    _write_wheel(tmp_path, extra=["engineering/__pycache__/gear.cpython-311.pyc"])
    _write_sdist(tmp_path)
    assert any("__pycache__" in p for p in _problems(tmp_path))


def test_a_file_git_does_not_track_is_not_in_the_sdist(tmp_path: Path) -> None:
    """hatch's sdist honours .gitignore, not git's index: a local build sweeps in
    every untracked file of the checkout. A maintainer's checkout holds files
    that must never be published, so only tracked files may ship."""
    _write_wheel(tmp_path)
    _write_sdist(tmp_path, extra=["notes/untracked.md"])
    problems = _problems(tmp_path)
    assert len(problems) == 1 and "notes/untracked.md" in problems[0], problems


def test_the_sdist_may_carry_the_plugin_and_skill_sources(tmp_path: Path) -> None:
    """The sdist is the source tree: tracked plugin and skill files are fine
    there; only the wheel - what pip installs - must not carry them."""
    _write_wheel(tmp_path)
    _write_sdist(
        tmp_path,
        extra=["plugin/.claude-plugin/plugin.json", ".claude/skills/autocad-mcp-premium/SKILL.md"],
    )
    assert _problems(tmp_path) == []


def test_an_sdist_without_the_build_inputs_fails(tmp_path: Path) -> None:
    _write_wheel(tmp_path)
    path = tmp_path / f"{STEM}.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        data = b"x"
        info = tarfile.TarInfo(f"{STEM}/PKG-INFO")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    problems = _problems(tmp_path)
    assert any("pyproject.toml" in p for p in problems)
    assert any("server.py" in p for p in problems)


def test_distributions_of_another_version_fail(tmp_path: Path) -> None:
    other = tmp_path / "autocad_mcp_pro-0.0.1-py3-none-any.whl"
    with zipfile.ZipFile(other, "w") as archive:
        archive.writestr("server.py", "x")
    _write_sdist(tmp_path)
    problems = _problems(tmp_path)
    assert any("0.0.1" in p for p in problems), problems


def test_an_empty_dist_folder_fails(tmp_path: Path) -> None:
    problems = _problems(tmp_path)
    assert any("wheel" in p for p in problems) and any("sdist" in p for p in problems)


def test_the_command_line_exits_one_on_a_problem_and_zero_when_clean(tmp_path: Path) -> None:
    _write_wheel(tmp_path, extra=["plugin/.mcp.json"])
    _write_sdist(tmp_path)
    assert checker.main([str(tmp_path), "--no-git"]) == 1
    (tmp_path / f"{STEM}-py3-none-any.whl").unlink()
    _write_wheel(tmp_path)
    assert checker.main([str(tmp_path), "--no-git"]) == 0


def test_the_tracked_set_is_read_from_git() -> None:
    tracked = checker.tracked_files(ROOT)
    assert tracked is not None
    assert "pyproject.toml" in tracked and "server.py" in tracked
    assert ".claude/skills/autocad-mcp-premium/SKILL.md" in tracked
    assert not any(path.startswith("out/") for path in tracked)
