"""The server's tool policy (1.8-17): which tools a client may reach at all.

Three settings, all read live from :mod:`config` so a test (or a restarted
server) sees the value it set:

* ``READ_ONLY`` (default false) - only ``read`` tools run, and no file is
  written (``security.validate_path(allow_write=True)`` refuses);
* ``TOOL_ALLOWLIST`` / ``TOOL_DENYLIST`` - comma-separated names or globs
  (``entity_*``); an entry naming no registered tool is warned about at
  startup, never an error;
* ``MAX_TOOL_COST`` - the highest ``@cad_tool`` cost a call may carry:
  ``read < safe < mutate < destructive < escape`` (default ``escape``, which is
  every tool, i.e. 1.7's behaviour). ``READ_ONLY`` implies ``read``.

An HTTP request authenticated with ``MCP_AUTH_TOKEN_READONLY`` carries only the
``mcp:read`` scope; the server sets :data:`REQUEST_CEILING` to ``read`` for that
request, so it runs under the same ceiling as ``READ_ONLY``.

The policy is checked at four points (``check`` is the one rule they share):

1. the profile visibility transform - a refused tool is not advertised;
2. ``AuditMiddleware`` before an operation record is opened, on the tool a
   call really runs (the search proxy's ``call_tool`` target included);
3. ``cad_batch`` at plan time, for every step (steps skip the middleware);
4. ``security.validate_path(allow_write=True)`` - every file write. A file
   write is ``mutate``-class whatever its tool's label, so a writer labelled
   ``read`` or ``safe`` (``drawing_verify_requirements(report_path=)``,
   ``drawing_export_dwg``) is still refused under a ceiling below ``mutate``.

``cad_batch`` is judged per step: its own label (``destructive``, the worst
case it can reach) is not held against the ceiling, every step it plans is.
The discovery proxies (``search_tools``, ``describe_tool``, ``call_tool``) are
never refused themselves - ``call_tool`` is judged by its target.
"""

from __future__ import annotations

import fnmatch
import logging
from contextvars import ContextVar

from fastmcp.exceptions import ToolError

log = logging.getLogger("autocad_mcp.policy")

#: Every ``@cad_tool`` cost, cheapest first. The order is the policy.
COSTS: tuple[str, ...] = ("read", "safe", "mutate", "destructive", "escape")
_RANK = {cost: rank for rank, cost in enumerate(COSTS)}

#: The cost of writing a file, whatever the tool that writes it is labelled.
WRITE_COST = "mutate"

#: Tools whose cost is judged per step, never by their own label.
PER_STEP_TOOLS: frozenset[str] = frozenset({"cad_batch"})

#: The search-mode proxies: never refused by name; ``call_tool``'s target is.
DISCOVERY_TOOLS: frozenset[str] = frozenset({"search_tools", "describe_tool", "call_tool"})

#: The ceiling of the request being served (``read`` for a read-only token),
#: or None. Set by ``AuditMiddleware`` for the length of one tool call.
REQUEST_CEILING: ContextVar[str | None] = ContextVar("acadmcp_request_ceiling", default=None)

#: The tool being served, for a write refusal's ``tool`` field.
CURRENT_TOOL: ContextVar[str | None] = ContextVar("acadmcp_current_tool", default=None)


class PolicyRefusedError(ToolError):
    """The server's tool policy refused a call before anything ran.

    ``policy`` names the rule (``read_only``, ``max_tool_cost``,
    ``token_scope``, ``tool_allowlist``, ``tool_denylist``); ``allowed_cost``
    is the ceiling in force, ``cost`` the tool's own (``mutate`` for a file
    write). A ``ToolError`` so every ``except ToolError`` still holds; the type
    is what ``engineering.errors.classify`` reads as ``policy_refused``.
    """

    kind = "policy_refused"

    def __init__(
        self,
        message: str,
        *,
        policy: str,
        tool: str | None,
        allowed_cost: str,
        cost: str | None = None,
    ):
        super().__init__(message)
        self.policy = policy
        self.tool = tool
        self.allowed_cost = allowed_cost
        self.cost = cost

    def fields(self) -> dict:
        """The refusal's own envelope fields."""
        row = {"policy": self.policy, "allowed_cost": self.allowed_cost}
        if self.tool is not None:
            row["tool"] = self.tool
        if self.cost is not None:
            row["cost"] = self.cost
        return row


def parse_patterns(raw: str | None) -> tuple[str, ...]:
    """``"entity_*, drawing_info"`` -> ``("entity_*", "drawing_info")``, lowercased."""
    return tuple(
        dict.fromkeys(part.strip().lower() for part in str(raw or "").split(",") if part.strip())
    )


def parse_cost(raw: str | None, *, default: str = "escape") -> str:
    """A ``MAX_TOOL_COST`` value; an unknown one falls back to ``default`` with a warning."""
    value = str(raw or "").strip().lower()
    if not value:
        return default
    if value not in _RANK:
        log.warning(
            "Unknown MAX_TOOL_COST %r - falling back to %r. Valid values: %s.",
            raw,
            default,
            ", ".join(COSTS),
        )
        return default
    return value


def _settings():
    # Imported here: config's Settings() parses its values with this module's
    # helpers, so a module-level import would be circular.
    import config

    return config.settings


def read_only() -> bool:
    return bool(getattr(_settings(), "read_only", False))


def allowlist() -> tuple[str, ...]:
    return tuple(getattr(_settings(), "tool_allowlist", ()) or ())


def denylist() -> tuple[str, ...]:
    return tuple(getattr(_settings(), "tool_denylist", ()) or ())


def max_cost() -> str:
    value = getattr(_settings(), "max_tool_cost", "escape") or "escape"
    return value if value in _RANK else "escape"


def server_ceiling() -> str:
    """The server-wide ceiling: ``MAX_TOOL_COST``, lowered to ``read`` by ``READ_ONLY``."""
    return "read" if read_only() else max_cost()


def ceiling() -> str:
    """The ceiling in force now: the server's, lowered by the request's (a read-only token)."""
    current = server_ceiling()
    request = REQUEST_CEILING.get()
    if request in _RANK and _RANK[request] < _RANK[current]:
        return request
    return current


def _binding_rule(cost_ceiling: str) -> str:
    """Which setting set ``cost_ceiling``: the one a refusal names."""
    if read_only() and cost_ceiling == "read":
        return "read_only"
    request = REQUEST_CEILING.get()
    if request == cost_ceiling and _RANK.get(request, 99) < _RANK[server_ceiling()]:
        return "token_scope"
    return "max_tool_cost"


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in patterns)


def refusal(tool: str, cost: str | None) -> PolicyRefusedError | None:
    """The refusal ``tool`` (of ``cost``) meets under the policy now in force, or None.

    A registered tool with no cost card is judged as ``escape`` (fail closed).
    """
    if tool in DISCOVERY_TOOLS:
        return None
    limit = ceiling()
    denied = denylist()
    if denied and _matches(tool, denied):
        return PolicyRefusedError(
            f"policy_refused: {tool!r} matches TOOL_DENYLIST ({', '.join(denied)}); the "
            "server's operator excluded it. Nothing ran.",
            policy="tool_denylist",
            tool=tool,
            allowed_cost=limit,
            cost=cost,
        )
    allowed = allowlist()
    if allowed and not _matches(tool, allowed):
        return PolicyRefusedError(
            f"policy_refused: {tool!r} is not in TOOL_ALLOWLIST ({', '.join(allowed)}); only "
            "the listed tools run on this server. Nothing ran.",
            policy="tool_allowlist",
            tool=tool,
            allowed_cost=limit,
            cost=cost,
        )
    if tool in PER_STEP_TOOLS:
        return None
    effective = cost if cost in _RANK else "escape"
    if _RANK[effective] > _RANK[limit]:
        rule = _binding_rule(limit)
        why = {
            "read_only": "the server runs READ_ONLY",
            "token_scope": "this request's token carries only the mcp:read scope",
            "max_tool_cost": f"MAX_TOOL_COST is {limit!r}",
        }[rule]
        return PolicyRefusedError(
            f"policy_refused: {tool!r} costs {effective!r} and {why}, so only tools up to "
            f"{limit!r} run. Nothing ran.",
            policy=rule,
            tool=tool,
            allowed_cost=limit,
            cost=effective,
        )
    return None


def check(tool: str, cost: str | None) -> None:
    """Raise :class:`PolicyRefusedError` when the policy refuses ``tool``."""
    refused = refusal(tool, cost)
    if refused is not None:
        raise refused


def allowed(tool: str, cost: str | None) -> bool:
    return refusal(tool, cost) is None


def check_write(path) -> None:
    """Refuse a file write under a ceiling below ``mutate`` (READ_ONLY, a read-only token).

    Called by ``security.validate_path(allow_write=True)``, so it covers every
    writer whatever its tool's label - the guard that catches a writer
    labelled ``read`` or ``safe``.
    """
    limit = ceiling()
    if _RANK[limit] >= _RANK[WRITE_COST]:
        return
    rule = _binding_rule(limit)
    raise PolicyRefusedError(
        f"policy_refused: writing {str(path)!r} is a {WRITE_COST!r} operation and only tools "
        f"up to {limit!r} run here ({rule}). Nothing was written.",
        policy=rule,
        tool=CURRENT_TOOL.get(),
        allowed_cost=limit,
        cost=WRITE_COST,
    )


def restrictive() -> bool:
    """True when anything narrows the default (every tool, every cost)."""
    return bool(read_only() or allowlist() or denylist() or max_cost() != "escape")


def unmatched_patterns(registered) -> dict[str, list[str]]:
    """Allow/deny entries that name no registered tool (a typo, a removed tool)."""
    names = [str(name).lower() for name in registered]
    out: dict[str, list[str]] = {}
    for label, patterns in (("allowlist", allowlist()), ("denylist", denylist())):
        missing = [p for p in patterns if not any(fnmatch.fnmatchcase(n, p) for n in names)]
        if missing:
            out[label] = missing
    return out


_warned: set[tuple[str, str]] = set()


def warn_unmatched(registered) -> dict[str, list[str]]:
    """Log each allow/deny entry that names no registered tool, once per process."""
    unmatched = unmatched_patterns(registered)
    for label, patterns in unmatched.items():
        for pattern in patterns:
            if (label, pattern) in _warned:
                continue
            _warned.add((label, pattern))
            log.warning(
                "TOOL_%s entry %r matches no registered tool; it has no effect.",
                label.upper(),
                pattern,
            )
    return unmatched


def describe(registered=None) -> dict:
    """The policy in force, for ``system_about`` / ``system_status``."""
    out = {
        "read_only": read_only(),
        "max_tool_cost": max_cost(),
        "ceiling": server_ceiling(),
        "allowlist": list(allowlist()),
        "denylist": list(denylist()),
    }
    if registered is not None:
        unmatched = unmatched_patterns(registered)
        if unmatched:
            out["unmatched"] = unmatched
    return out
