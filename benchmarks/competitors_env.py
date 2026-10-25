"""Reproducible checkout + venv management for competitor MCP servers.

Every competitor is pinned to an exact commit so a benchmark run is
reproducible: same tasks, same tree, same interpreter isolation. The cache
lives under ``benchmarks/.competitors/<id>/`` (gitignored) as::

    repo/    the pinned git checkout
    venv/    an isolated interpreter with the competitor installed

Nothing here talks MCP — it only prepares a runnable ``python`` + entry args
for :class:`benchmarks.adapters.mcp_stdio.McpStdioAdapter`.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

CACHE_ROOT = Path(__file__).resolve().parent / ".competitors"


@dataclass(frozen=True)
class CompetitorSpec:
    """Everything needed to fetch and launch one competitor server."""

    competitor_id: str
    repo_url: str
    pinned_sha: str
    # Candidate stdio entrypoints, resolved in order against the checkout.
    # ("-m", "pkg") entries are accepted when repo/pkg or repo/src/pkg exists.
    entry_candidates: tuple[tuple[str, ...], ...]
    # pip arguments executed inside the venv, relative to the checkout.
    pip_installs: tuple[tuple[str, ...], ...] = (("-e", "."),)
    # Extra environment applied when launching the server.
    launch_env: dict[str, str] = field(default_factory=dict)
    # Set PYTHONPATH to this repo-relative directory (e.g. "src") if not None.
    pythonpath: str | None = None


@dataclass(frozen=True)
class CompetitorRuntime:
    """A resolved, launchable competitor installation."""

    python_exe: Path
    repo_dir: Path
    entry_args: tuple[str, ...]
    env: dict[str, str]


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 600.0) -> None:
    completed = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({' '.join(cmd)}):\n{completed.stdout}\n{completed.stderr}"
        )


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _ensure_checkout(spec: CompetitorSpec, repo_dir: Path) -> None:
    if not (repo_dir / ".git").exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--quiet", spec.repo_url, str(repo_dir)])
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True
    ).stdout.strip()
    if head != spec.pinned_sha:
        _run(["git", "fetch", "--quiet", "origin"], cwd=repo_dir)
        _run(["git", "checkout", "--quiet", spec.pinned_sha], cwd=repo_dir)


def _ensure_venv(spec: CompetitorSpec, venv_dir: Path, repo_dir: Path) -> Path:
    python_exe = _venv_python(venv_dir)
    marker = venv_dir / ".installed-sha"
    if python_exe.exists() and marker.exists() and marker.read_text().strip() == spec.pinned_sha:
        return python_exe
    _run([sys.executable, "-m", "venv", str(venv_dir)])
    _run([str(python_exe), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    for install in spec.pip_installs:
        _run([str(python_exe), "-m", "pip", "install", "--quiet", *install], cwd=repo_dir)
    marker.write_text(spec.pinned_sha)
    return python_exe


def _resolve_entry(spec: CompetitorSpec, repo_dir: Path) -> tuple[str, ...]:
    for candidate in spec.entry_candidates:
        if candidate[0] == "-m":
            package = candidate[1].replace(".", "/")
            roots = [repo_dir, repo_dir / "src"]
            if any(
                (root / package).is_dir() or (root / f"{package}.py").is_file() for root in roots
            ):
                return candidate
        elif (repo_dir / candidate[0]).is_file():
            return tuple([str(repo_dir / candidate[0]), *candidate[1:]])
    raise RuntimeError(
        f"{spec.competitor_id}: no stdio entrypoint found "
        f"(tried {[c[0] for c in spec.entry_candidates]})"
    )


def _shown(path: Path) -> str:
    """A path as a report may print it: relative to this repository when it is
    inside it, so a published report carries no machine's directory layout."""
    try:
        return path.resolve().relative_to(CACHE_ROOT.parents[1]).as_posix()
    except ValueError:
        return path.name


def _prepared(spec: CompetitorSpec, root: Path) -> str | None:
    """Why the cached environment is not ready to launch, or None when it is."""
    repo_dir = root / "repo"
    if not (repo_dir / ".git").exists():
        return f"no checkout at {_shown(repo_dir)}"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True
    ).stdout.strip()
    if head != spec.pinned_sha:
        return f"the checkout is at {head or 'nothing'}, not the pinned {spec.pinned_sha}"
    venv_dir = root / "venv"
    marker = venv_dir / ".installed-sha"
    if not _venv_python(venv_dir).exists() or not marker.exists():
        return f"no installed venv at {_shown(venv_dir)}"
    if marker.read_text().strip() != spec.pinned_sha:
        return f"the venv at {_shown(venv_dir)} was installed for another commit"
    return None


def ensure_competitor(
    spec: CompetitorSpec, cache_root: Path | None = None, *, fetch: bool = True
) -> CompetitorRuntime:
    """Clone, pin, install and resolve one competitor. Idempotent.

    With ``fetch=False`` nothing is downloaded or installed: an environment
    that is not already cached at the pinned commit raises
    :class:`benchmarks.adapters.base.EnvironmentBlocked` naming what is missing.
    """
    from benchmarks.adapters.base import EnvironmentBlocked

    root = (cache_root or CACHE_ROOT) / spec.competitor_id
    repo_dir = root / "repo"
    if not fetch:
        missing = _prepared(spec, root)
        if missing is not None:
            raise EnvironmentBlocked(
                f"{spec.competitor_id}: environment absent and fetching is off ({missing})"
            )
    _ensure_checkout(spec, repo_dir)
    python_exe = _ensure_venv(spec, root / "venv", repo_dir)
    entry_args = _resolve_entry(spec, repo_dir)

    env = dict(spec.launch_env)
    if spec.pythonpath is not None:
        env["PYTHONPATH"] = str(repo_dir / spec.pythonpath)
    return CompetitorRuntime(
        python_exe=python_exe, repo_dir=repo_dir, entry_args=entry_args, env=env
    )
