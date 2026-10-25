"""Common, vendor-neutral benchmark adapter contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from benchmarks.tasks_v2 import TaskSpec

TaskStatus = Literal[
    "pass", "partial", "unsupported", "fail", "timeout", "not_run", "environment_blocked"
]

#: Statuses that say a task was not attempted at all. They never enter a score:
#: a zero would claim the server was asked and failed. ``not_run``: nobody asked
#: this server the task (no playbook, a published run that predates it, a lane
#: with no adapter). ``environment_blocked``: the lane could not start - the
#: competitor's sources or venv are absent and fetching is off, or the live
#: engine is not reachable.
NOT_ATTEMPTED: frozenset[str] = frozenset({"not_run", "environment_blocked"})


class EnvironmentBlocked(RuntimeError):
    """An adapter's environment is absent, so none of its tasks can run.

    Raised by ``setup``; ``run_tasks`` reports every task ``environment_blocked``
    with this message instead of letting the lane crash or score zero.
    """


@dataclass
class TaskResult:
    task_id: str
    status: TaskStatus
    score: float
    message: str = ""
    duration_ms: float = 0.0
    artifacts: list[str | dict[str, Any]] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    stdout_summary: str = ""
    stderr_summary: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "score": float(self.score),
            "message": self.message,
            "duration_ms": float(self.duration_ms),
            "artifacts": list(self.artifacts),
            "metrics": dict(self.metrics),
            "stdout_summary": self.stdout_summary,
            "stderr_summary": self.stderr_summary,
        }


class BenchmarkAdapter(ABC):
    """Minimum interface every competing MCP adapter must implement."""

    name: str
    #: May ``setup`` download what it needs (a competitor's sources, a venv)?
    #: ``run_competitors --no-fetch`` turns it off; an absent environment is
    #: then ``environment_blocked`` instead of a download.
    fetch: bool = True

    def metadata(self) -> dict:
        """Runtime/backend claims captured alongside every benchmark report."""
        return {}

    @abstractmethod
    async def setup(self, artifact_dir: Path) -> None: ...

    @abstractmethod
    async def run_task(self, task: TaskSpec) -> TaskResult: ...

    @abstractmethod
    async def cleanup(self) -> None: ...
