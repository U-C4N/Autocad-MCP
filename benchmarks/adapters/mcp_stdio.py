"""Generic MCP-stdio benchmark adapter.

Drives a *competitor* MCP server as a black box over the standard stdio
transport (fastmcp ``Client``), exactly the way an agent host would. No
competitor code is imported; verification never trusts the competitor's
response payload — every task that claims to have produced geometry must
save a DXF which the harness re-opens with ezdxf and inspects itself.

Subclasses provide the launch runtime (via ``benchmarks.competitors_env``)
and per-task playbooks named ``_task_<task_id>``; an unmapped v2 task is
reported as ``unsupported`` (the v1.4 verdict: no documented equivalent), an
unmapped later task as ``not_run`` (never asked), and a lane whose environment
is absent as ``environment_blocked`` - neither of the last two is ever a zero.
"""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import ezdxf
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from benchmarks.adapters.base import BenchmarkAdapter, EnvironmentBlocked, TaskResult
from benchmarks.competitors_env import CompetitorRuntime, CompetitorSpec, ensure_competitor
from benchmarks.tasks_v2 import TASKS_V2, TaskSpec

CALL_TIMEOUT_SECONDS = 15.0
WARMUP_TIMEOUT_SECONDS = 90.0

#: The ten tasks the v1.4 competitor runs were asked. A missing playbook for one
#: of them is the v1.4 verdict, ``unsupported`` (no documented equivalent); a
#: missing playbook for anything later is ``not_run``.
_V2_TASK_IDS = frozenset(task.task_id for task in TASKS_V2)


class ToolCallFailed(RuntimeError):
    """A competitor tool call returned an error result."""


def read_dxf(path: Path):
    return ezdxf.readfile(str(path))


def modelspace_types(doc) -> list[str]:
    return [entity.dxftype() for entity in doc.modelspace()]


def find_dxf(artifact_dir: Path, preferred: Path | None = None) -> Path | None:
    """Locate the DXF a competitor produced.

    Servers with managed output roots may relocate writes, so fall back to
    the newest ``*.dxf`` anywhere under the artifact directory.
    """
    if preferred is not None and preferred.is_file():
        return preferred
    candidates = sorted(
        artifact_dir.rglob("*.dxf"), key=lambda item: item.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


class McpStdioAdapter(BenchmarkAdapter):
    """Black-box adapter base for competitor MCP servers."""

    name = "mcp-stdio"
    spec: CompetitorSpec  # set by subclasses

    def __init__(self, *, backend: str = "ezdxf"):
        if backend != "ezdxf":
            raise ValueError(
                f"{type(self).__name__} only benchmarks the headless ezdxf lane; "
                f"got backend={backend!r}. COM lanes run locally, not in CI."
            )
        self.backend_name = backend
        self.artifact_dir = Path()
        self.runtime: CompetitorRuntime | None = None
        #: where the pinned sources and venv are cached (None: the default)
        self.cache_root: Path | None = None
        self._client: Client | None = None
        self.tool_names: set[str] = set()

    # -- lifecycle ----------------------------------------------------------

    def extra_launch_env(self) -> dict[str, str]:
        """Environment computed at setup time (``self.artifact_dir`` is set)."""
        return {}

    async def setup(self, artifact_dir: Path) -> None:
        self.artifact_dir = Path(artifact_dir)
        try:
            self.runtime = ensure_competitor(self.spec, self.cache_root, fetch=self.fetch)
        except EnvironmentBlocked:
            raise
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            # a clone or an install that cannot run here (no git, no network, a
            # build that fails) is an absent environment, not a server failure
            raise EnvironmentBlocked(
                f"{self.spec.competitor_id}: the environment could not be prepared ({exc})"
            ) from exc
        env = {**self.runtime.env, **self.extra_launch_env()}
        env.setdefault("PYTHONIOENCODING", "utf-8")
        transport = StdioTransport(
            command=str(self.runtime.python_exe),
            args=list(self.runtime.entry_args),
            env=env,
            cwd=str(self.runtime.repo_dir),
        )
        self._client = Client(transport)
        await self._client.__aenter__()
        self.tool_names = {tool.name for tool in await self._client.list_tools()}
        await self._warmup()

    def warmup_calls(self) -> tuple[tuple[str, dict[str, Any]], ...]:
        """Calls issued once after connect to absorb the competitor's lazy
        backend initialization (which can exceed the per-call timeout)."""
        return ()

    async def _warmup(self) -> None:
        for tool, arguments in self.warmup_calls():
            if tool not in self.tool_names:
                continue
            try:
                await self._client.call_tool(
                    tool, arguments, timeout=WARMUP_TIMEOUT_SECONDS, raise_on_error=False
                )
            except Exception:  # cold-start absorption is best effort
                continue

    async def cleanup(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    def metadata(self) -> dict:
        return {
            "backend": self.backend_name,
            "kind": "mcp-stdio-blackbox",
            "repo": self.spec.repo_url,
            "pinned_sha": self.spec.pinned_sha,
            "tools_discovered": sorted(self.tool_names),
        }

    # -- call helpers ---------------------------------------------------------

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Call one tool; raise ``ToolCallFailed`` on an error result.

        Competitor servers in this family report failures *inside* the payload
        (``CommandResult(ok=False, error=...)`` serialized as text) rather than
        via the MCP ``isError`` flag, so the payload is inspected as well.
        """
        if tool not in self.tool_names:
            raise ToolCallFailed(f"tool {tool!r} is not exposed by {self.name}")
        result = await self._client.call_tool(
            tool, arguments, timeout=CALL_TIMEOUT_SECONDS, raise_on_error=False
        )
        if getattr(result, "is_error", False):
            raise ToolCallFailed(f"{tool}({arguments}) -> {_result_text(result)}")
        data = getattr(result, "data", None)
        payload = data if data is not None else _result_text(result)
        payload = _normalize_payload(payload)
        error = _payload_error(payload)
        if error:
            raise ToolCallFailed(f"{tool}({arguments}) -> {error}")
        return payload

    async def call_op(
        self,
        tool: str,
        operation: str,
        data: dict[str, Any] | None = None,
        *,
        shapes: tuple[dict[str, Any], ...] | None = None,
    ) -> Any:
        """Call a consolidated ``{tool}(operation=..., ...)`` style tool.

        Competitors disagree on whether payload fields live under ``data`` or
        at the top level, and on field spellings. ``shapes`` supplies explicit
        argument dicts to try in order; otherwise both common layouts of
        ``data`` are attempted. The first non-error result wins.
        """
        attempts: list[dict[str, Any]] = []
        if shapes:
            attempts.extend({"operation": operation, **shape} for shape in shapes)
        else:
            payload = data or {}
            attempts.append({"operation": operation, "data": payload})
            if payload:
                attempts.append({"operation": operation, **payload})
        last_error: Exception | None = None
        for arguments in attempts:
            try:
                return await self.call(tool, arguments)
            except ToolCallFailed as exc:  # try the next argument shape
                last_error = exc
        raise ToolCallFailed(str(last_error))

    # -- task dispatch --------------------------------------------------------

    async def run_task(self, task: TaskSpec) -> TaskResult:
        handler = getattr(self, f"_task_{task.task_id}", None)
        if handler is None and task.task_id not in _V2_TASK_IDS:
            # Tasks added after v2 were never put to the competitors: no
            # playbook was written from their documentation, so there is no
            # finding to report - not_run, never a zero (spec §6 Task 20).
            return TaskResult(
                task.task_id,
                "not_run",
                0.0,
                f"{self.name} was never asked this task: no playbook exists for it",
            )
        if handler is None:
            return TaskResult(
                task.task_id,
                "unsupported",
                0.0,
                f"{self.name} has no documented equivalent for this task",
            )
        try:
            return await handler()
        except ToolCallFailed as exc:
            return TaskResult(task.task_id, "fail", 0.0, str(exc)[:500])

    # -- verification ---------------------------------------------------------

    def save_target(self, filename: str) -> Path:
        return self.artifact_dir / filename

    def verify_dxf(self, preferred: Path | None = None):
        located = find_dxf(self.artifact_dir, preferred)
        if located is None:
            raise ToolCallFailed("no DXF artifact was produced")
        return located, read_dxf(located)


def _result_text(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
    return " | ".join(parts)[:1000] if parts else "<no text content>"


def _normalize_payload(payload: Any) -> Any:
    """Unwrap fastmcp's dynamic output wrappers and JSON-in-text payloads.

    Legacy servers return plain text; the fastmcp client may deserialize it
    into a generated *dataclass* (e.g. ``entityOutput(result='<json>')``) or a
    pydantic model. Normalize down to dict / list / str so verification and
    handle extraction see real data.
    """
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        payload = dataclasses.asdict(payload)
    elif hasattr(payload, "model_dump"):
        payload = payload.model_dump()
    elif not isinstance(payload, (dict, list, str)) and hasattr(payload, "result"):
        payload = payload.result
    if isinstance(payload, dict) and set(payload) == {"result"}:
        payload = payload["result"]
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            payload = parsed
    return payload


_TEXT_ERROR_MARKERS = ('"ok": false', "'ok': false", '"ok":false', "ok=false")


def _payload_error(payload: Any) -> str | None:
    """Detect failure reported inside a tool payload instead of MCP isError."""
    if isinstance(payload, dict):
        if payload.get("ok") is False or payload.get("success") is False:
            return str(payload.get("error") or payload.get("message") or "ok=false")[:300]
        error = payload.get("error")
        if isinstance(error, str) and error.strip():
            return error[:300]
        return None
    if isinstance(payload, str):
        lowered = payload.lower()
        if lowered.startswith("error") or any(m in lowered for m in _TEXT_ERROR_MARKERS):
            return payload[:300]
    return None


def wrapped_shapes(payload: dict[str, Any], **top_level: Any) -> tuple[dict[str, Any], ...]:
    """Produce the two common consolidated-tool argument layouts for a payload:
    nested under ``data`` and flattened at the top level."""
    return (
        {**top_level, "data": payload},
        {**top_level, **payload},
    )


_HANDLE_KEYS = ("handle", "entity_id", "entity_handle", "id", "object_id")
_HANDLE_RE = re.compile(
    r'"(?:handle|entity_id|entity_handle|object_id)"\s*:\s*"?([0-9A-Fa-f*][\w*-]*)"?'
)


def extract_handle(payload: Any) -> str | None:
    """Best-effort extraction of a created-entity handle from a black-box
    tool response (structured dict, list, or raw text)."""
    if isinstance(payload, dict):
        for key in _HANDLE_KEYS:
            value = payload.get(key)
            if isinstance(value, (str, int)) and str(value):
                return str(value)
        for value in payload.values():
            found = extract_handle(value)
            if found:
                return found
        return None
    if isinstance(payload, (list, tuple)):
        for item in payload:
            found = extract_handle(item)
            if found:
                return found
        return None
    if isinstance(payload, str):
        match = _HANDLE_RE.search(payload)
        return match.group(1) if match else None
    return None
