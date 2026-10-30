"""Pinned AgentCAD playbooks, exposed through the standard competitor runner.

The evaluator-side playbooks are loaded from the measured public commit, not
from an installed AgentCAD module. Credited CAD work uses MCP stdio; the one
internal cancellation fixture runs in the separate product interpreter. See
``benchmarks/agentcad.md`` for the oracle and evidence boundaries.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType

from benchmarks.adapters.base import BenchmarkAdapter, EnvironmentBlocked, TaskResult
from benchmarks.competitors_env import CompetitorSpec, ensure_competitor
from benchmarks.tasks_v2 import TaskSpec

PINNED_SHA = "39fa4b55899233ab7ac5cbb52714a4767ca164c6"
REPO_URL = "https://github.com/lizaixi01/AgentCAD"
PLAYBOOK_FILES = (
    "examples/autocad_benchmark/run.py",
    "examples/autocad_benchmark/cancel_worker.py",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, timeout=30).strip()


def _load_playbook(repo: Path) -> tuple[ModuleType, dict[str, str]]:
    """Refuse a changed pin or tracked edit before executing any playbook.

    Git's LF blob is the portable identity; Windows CRLF checkout conversion
    is allowed. Execute the checked source directly, avoiding stale bytecode.
    """
    if _git(repo, "rev-parse", "HEAD") != PINNED_SHA:
        raise EnvironmentBlocked("agentcad: cached checkout does not match the pinned commit")
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise EnvironmentBlocked("agentcad: cached checkout is dirty; use a clean pinned checkout")
    sources: dict[str, bytes] = {}
    for relative in PLAYBOOK_FILES:
        source = subprocess.check_output(
            ["git", "-C", str(repo), "show", f"{PINNED_SHA}:{relative}"], timeout=30
        )
        checked_out = (repo / relative).read_bytes().replace(b"\r\n", b"\n")
        if source != checked_out:
            raise EnvironmentBlocked(f"agentcad: playbook differs from pinned blob: {relative}")
        sources[relative] = source
    module = ModuleType("_agentcad_benchmark_playbook")
    module.__file__ = str(repo / PLAYBOOK_FILES[0])
    exec(compile(sources[PLAYBOOK_FILES[0]], module.__file__, "exec"), module.__dict__)
    return module, {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}


class AgentCADAdapter(BenchmarkAdapter):
    name = "agentcad"
    spec = CompetitorSpec(
        competitor_id=name,
        repo_url=f"{REPO_URL}.git",
        pinned_sha=PINNED_SHA,
        entry_candidates=(("-m", "agentcad.mcp_server"),),
        pip_installs=(("-e", ".", "ezdxf==1.4.5", "mcp==1.30.0", "pypdf==6.21.4"),),
    )

    def __init__(self, *, backend: str = "ezdxf"):
        if backend != "ezdxf":
            raise ValueError("AgentCAD's submitted lane is headless ezdxf only")
        self.cache_root: Path | None = None
        self._delegate = None
        self._playbook_hashes: dict[str, str] = {}

    async def setup(self, artifact_dir: Path) -> None:
        if any(Path(artifact_dir).iterdir()):
            raise EnvironmentBlocked(
                "agentcad: choose a new artifact directory; evidence is not overwritten"
            )
        missing = [
            name
            for name in ("pypdf", "matplotlib", "openpyxl")
            if importlib.util.find_spec(name) is None
        ]
        if missing:
            raise EnvironmentBlocked(
                f"agentcad: evaluator dependencies missing: {', '.join(missing)}; "
                'install ".[pdf,office]" and pypdf==6.21.4'
            )
        try:
            runtime = ensure_competitor(self.spec, self.cache_root, fetch=self.fetch)
            module, self._playbook_hashes = _load_playbook(runtime.repo_dir)
            self._delegate = module.AgentCADAdapter(runtime.python_exe)
        except EnvironmentBlocked:
            raise
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise EnvironmentBlocked(
                f"agentcad: could not prepare pinned environment ({exc})"
            ) from exc
        await self._delegate.setup(Path(artifact_dir))

    async def run_task(self, task: TaskSpec) -> TaskResult:
        if self._delegate is None:
            raise RuntimeError("AgentCAD adapter has not been set up")
        # The archived playbook prints progress; --json must remain valid JSON.
        with redirect_stdout(sys.stderr):
            return await self._delegate.run_task(task)

    async def cleanup(self) -> None:
        if self._delegate is not None:
            await self._delegate.cleanup()

    def metadata(self) -> dict:
        metadata = self._delegate.metadata() if self._delegate is not None else {}
        # Product identity is separate from the runner's own manifest.server_sha.
        metadata.update(
            repo=REPO_URL,
            pinned_sha=PINNED_SHA,
            kind="mcp-stdio-with-documented-internal-cancellation-fixture",
            playbook_git_blob_sha256=self._playbook_hashes,
            evidence_boundary="Contributor submission; subject to upstream review. Runtime tie, not a source-review score.",
        )
        return metadata
