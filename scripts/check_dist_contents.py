"""Hold the built distributions to what they are meant to carry.

    python scripts/check_dist_contents.py dist/            # exit 1 on any problem
    python scripts/check_dist_contents.py dist/ --no-git   # skip the tracked-file rule

Runs in CI's ``package`` job and in the release ``build`` job, after
``python -m build`` and before anything is uploaded.

**The wheel is the server.** It holds exactly the paths of
``[tool.hatch.build.targets.wheel] only-include`` plus its ``.dist-info``, each
of them present. The premium skill (``.claude/skills/``) and the Claude Code
plugin (``plugin/``) are not in it: they reach a user through ``claude plugin
install``, never through pip, and a copy in site-packages would be a second,
unversioned skill. ``tests/``, ``benchmarks/``, ``scripts/`` and ``docs/`` in
the wheel are packaging accidents; so is a compiled ``.pyc``.

**The sdist is the source of that wheel.** It holds ``PKG-INFO``,
``pyproject.toml``, ``README.md``, ``LICENSE`` and every ``only-include`` path,
and - when git can say - nothing git does not track. hatch's sdist honours
``.gitignore``, not git's index, so a local build sweeps in every untracked
file of the checkout; a maintainer's checkout holds files (client drawings,
local notes) that must never be published. The plugin and skill sources may be
in the sdist: they are tracked, and the sdist is the source tree.

Both archives must carry the pyproject version in their file names.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Never in the wheel, each with the reason a problem names.
WHEEL_FORBIDDEN = {
    "plugin/": "the Claude Code plugin ships through `claude plugin install`, not pip",
    ".claude/": "the premium skill ships in the plugin, not in the wheel",
    "tests/": "the test suite is not part of the installed server",
    "benchmarks/": "the benchmarks are not part of the installed server",
    "scripts/": "repository scripts are not part of the installed server",
    "docs/": "the documentation is not part of the installed server",
}

#: Never at the top of either archive (``.pyc`` and ``__pycache__/`` never anywhere).
NEVER_TOP = ("out/", ".venv/", "dist/", "build/")

#: The sdist's own build inputs besides the wheel's paths.
SDIST_REQUIRED = ("PKG-INFO", "pyproject.toml", "README.md", "LICENSE")

#: Files the sdist backend writes itself; git never tracks them.
SDIST_GENERATED = {"PKG-INFO"}


def _pyproject(root: Path) -> dict:
    with (root / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)


def tracked_files(root: Path) -> set[str] | None:
    """Every path git tracks under ``root`` (forward slashes), or None without git."""
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {path for path in listed.stdout.decode("utf-8").split("\0") if path}


def _covers(include: str, member: str) -> bool:
    return member == include or member.startswith(include.rstrip("/") + "/")


def _never(member: str) -> str | None:
    """The NEVER marker ``member`` hits: compiled files and caches anywhere, the
    build / venv / scratch folders at the top of the tree."""
    if member.endswith(".pyc"):
        return ".pyc"
    if member.startswith("__pycache__/") or "/__pycache__/" in member:
        return "__pycache__/"
    return next((top for top in NEVER_TOP if member.startswith(top)), None)


def wheel_problems(path: Path, includes: list[str]) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if not name.endswith("/")]
    problems: list[str] = []
    for member in members:
        top = member.split("/", 1)[0]
        if top.endswith(".dist-info"):
            continue
        marker = _never(member)
        if marker:
            problems.append(f"wheel {path.name}: {member} must not ship ({marker})")
            continue
        forbidden = next((p for p in WHEEL_FORBIDDEN if member.startswith(p)), None)
        if forbidden:
            problems.append(f"wheel {path.name}: {member} - {WHEEL_FORBIDDEN[forbidden]}")
        elif not any(_covers(include, member) for include in includes):
            problems.append(f"wheel {path.name}: {member} is outside only-include {includes}")
    for include in includes:
        if not any(_covers(include, member) for member in members):
            problems.append(f"wheel {path.name}: {include} is missing")
    return problems


def sdist_problems(
    path: Path, includes: list[str], stem: str, tracked: set[str] | None
) -> list[str]:
    with tarfile.open(path) as archive:
        names = [member.name for member in archive.getmembers() if member.isfile()]
    problems: list[str] = []
    members: list[str] = []
    for name in names:
        if not name.startswith(stem + "/"):
            problems.append(f"sdist {path.name}: {name} is outside {stem}/")
            continue
        members.append(name[len(stem) + 1 :])
    for member in members:
        marker = _never(member)
        if marker:
            problems.append(f"sdist {path.name}: {member} must not ship ({marker})")
        elif tracked is not None and member not in SDIST_GENERATED and member not in tracked:
            problems.append(
                f"sdist {path.name}: {member} is not tracked by git - a local file the "
                "build swept in; only tracked files may be published"
            )
    for required in (*SDIST_REQUIRED, *includes):
        if not any(_covers(required, member) for member in members):
            problems.append(f"sdist {path.name}: {required} is missing")
    return problems


def check(dist: Path, tracked: set[str] | None, root: Path = ROOT) -> list[str]:
    """Every problem with the wheel and sdist in ``dist`` (empty when clean)."""
    project = _pyproject(root)
    version = project["project"]["version"]
    includes = list(project["tool"]["hatch"]["build"]["targets"]["wheel"]["only-include"])
    stem = f"{project['project']['name'].replace('-', '_')}-{version}"

    problems: list[str] = []
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    for archive in (*wheels, *sdists):
        if not archive.name.startswith(stem + "-") and archive.name != f"{stem}.tar.gz":
            problems.append(f"{archive.name} is not version {version} ({stem})")
    wheels = [w for w in wheels if w.name.startswith(stem + "-")]
    sdists = [s for s in sdists if s.name == f"{stem}.tar.gz"]
    if len(wheels) != 1:
        problems.append(f"expected one wheel {stem}-*.whl in {dist}, found {len(wheels)}")
    if len(sdists) != 1:
        problems.append(f"expected one sdist {stem}.tar.gz in {dist}, found {len(sdists)}")
    for wheel in wheels:
        problems += wheel_problems(wheel, includes)
    for sdist in sdists:
        problems += sdist_problems(sdist, includes, stem, tracked)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dist", type=Path, help="the folder `python -m build` wrote")
    parser.add_argument(
        "--no-git", action="store_true", help="do not hold the sdist to git's tracked files"
    )
    args = parser.parse_args(argv)

    tracked = None if args.no_git else tracked_files(ROOT)
    if tracked is None and not args.no_git:
        print("note: git is unavailable; the sdist's tracked-file rule is not checked")
    problems = check(args.dist, tracked)
    for problem in problems:
        print(f"DIST  {problem}")
    if problems:
        print(f"\n{len(problems)} problem(s) in {args.dist}.")
        return 1
    print(f"dist ok: {', '.join(sorted(p.name for p in args.dist.iterdir() if p.is_file()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
