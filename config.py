"""Centralized configuration for AutoCAD MCP Pro."""

from __future__ import annotations

import os
from pathlib import Path

#: ProgID the live backend attaches to when ``CAD_PROGID`` is unset. The only
#: CAD this project is developed and tested against.
DEFAULT_CAD_PROGID = "AutoCAD.Application"

#: Default ``MAX_DXF_BYTES``: 512 MB.
DEFAULT_MAX_DXF_BYTES = 512 * 1024 * 1024

#: Default ``MAX_HTTP_BODY_BYTES``: 8 MiB (1.8-17).
DEFAULT_MAX_HTTP_BODY_BYTES = 8 * 1024 * 1024

#: ``LISP_POLICY`` values (1.8-18, ``security_lisp.py``); the first is the default.
LISP_POLICIES: tuple[str, ...] = ("read", "denylist", "off")

#: ``COM_REFRESH_MODE`` values (1.8-23); the first is the default.
COM_REFRESH_MODES: tuple[str, ...] = ("auto", "regen")


def parse_lisp_policy(raw: str | None) -> str:
    """A ``LISP_POLICY`` value; an unknown one falls back to ``read`` with a warning."""
    value = str(raw or "").strip().lower()
    if not value:
        return LISP_POLICIES[0]
    if value not in LISP_POLICIES:
        import logging

        logging.getLogger("autocad_mcp.config").warning(
            "Unknown LISP_POLICY %r - falling back to %r. Valid values: %s.",
            raw,
            LISP_POLICIES[0],
            ", ".join(LISP_POLICIES),
        )
        return LISP_POLICIES[0]
    return value


class Settings:
    """Server configuration loaded from environment variables."""

    @property
    def backend(self) -> str:
        """Which engine to run: ``ezdxf``, ``com`` or ``auto``.

        Read live rather than captured in ``__init__``, and deliberately
        read-only. ``_make_backend`` used to read ``os.environ`` itself while
        this attribute had no production reader at all, so
        ``monkeypatch.setattr(settings, "backend", "ezdxf")`` looked like a
        guard and was a no-op — on a machine with AutoCAD open that meant a test
        attaching to the operator's live drawing. Late binding is what keeps
        ``monkeypatch.setenv`` working for the call sites that pin the headless
        backend correctly; the missing setter is what makes the broken spelling
        fail loudly instead of silently.
        """
        return os.environ.get("AUTOCAD_MCP_BACKEND", "auto").lower().strip()

    @property
    def cad_progid(self) -> str:
        """COM ProgID the live backend attaches to.

        Read live and deliberately read-only for the same reasons as
        ``backend``: late binding keeps ``monkeypatch.setenv`` working, and the
        missing setter makes a wrong spelling fail loudly rather than look like
        a guard that does nothing.

        Stripped but **not** lowercased — the value is echoed back in error
        messages and ``system_status``, where ``gstarcad.application`` reads
        like a typo we introduced. Never validated against what is actually
        registered on the machine: an unrecognised ProgID is not "invalid", it
        is unverifiable by us, and only AutoCAD is tested.
        """
        return os.environ.get("CAD_PROGID", "").strip() or DEFAULT_CAD_PROGID

    def __init__(self):
        self.log_level: str = os.environ.get("LOG_LEVEL", "INFO").upper().strip()

        raw_paths = os.environ.get("ALLOWED_PATHS", "").strip()
        self.allowed_paths: list[Path] = []
        if raw_paths:
            for p in raw_paths.split(","):
                p = p.strip()
                if p:
                    self.allowed_paths.append(Path(p).resolve())

        self.max_undo_stack: int = int(os.environ.get("MAX_UNDO_STACK", "5"))
        self.dangerous_commands_enabled: bool = (
            os.environ.get("DANGEROUS_COMMANDS_ENABLED", "false").lower().strip() == "true"
        )

        # Reject opening DXF files larger than this many bytes (default 512 MB).
        # Set to 0 to disable the check. It was 50 MB until v1.6's track H: a
        # real plant layout read on 2026-09-24 was a 188 MB DXF, and the
        # understanding tools exist for exactly such drawings. The refusal
        # names the file's size and this variable.
        self.max_dxf_bytes: int = int(os.environ.get("MAX_DXF_BYTES", str(DEFAULT_MAX_DXF_BYTES)))

        # Ceiling on one QA read (drawing_critique, the validator, drawing_finalize,
        # drawing_deliver): the entities of every space are counted first and a
        # drawing holding more is refused with `scan_incomplete` rather than
        # read in part. 0 disables the check; a negative value is refused here
        # (it used to disable the check silently, because the ceiling is only
        # applied when positive).
        self.max_qa_entities: int = int(os.environ.get("MAX_QA_ENTITIES", "2000000"))
        if self.max_qa_entities < 0:
            raise ValueError(
                f"MAX_QA_ENTITIES={self.max_qa_entities} is negative. Set the most entities "
                "one QA read may scan (default 2000000), or 0 to disable the check."
            )

        # Hard cap on list/select limit params to keep tool responses bounded.
        self.max_list_limit: int = int(os.environ.get("MAX_LIST_LIMIT", "5000"))

        # Per-call COM timeout. AutoCAD hangs (modal dialogs, long Regen) would
        # otherwise block the single-thread STA executor forever.
        self.com_call_timeout: float = float(os.environ.get("COM_CALL_TIMEOUT", "60"))

        # 1.8-05: how long a command sent to AutoCAD's command line (a raw
        # system_run_command, drawing_undo / drawing_redo, a corner op, the
        # transaction mark) may stay active before it is cancelled with ESC x3
        # and reported as command_incomplete with AutoCAD's LASTPROMPT. Well
        # inside COM_CALL_TIMEOUT, so the STA job always comes back.
        self.command_deadline_s: float = max(0.1, float(os.environ.get("COMMAND_DEADLINE_S", "8")))

        # 1.8-23: the live engine refreshes the display once per COM job, not
        # once per entity. A job that asked for at most COM_REFRESH_UPDATE_MAX
        # refreshes, each naming its entity, calls Update() on the last one;
        # a larger job (or one whose refresh names no entity) does one
        # Regen(acActiveViewport). The default 1 is the safe reading while
        # spike S4 (does Update() of the last entity paint the earlier ones?)
        # is unmeasured: a single create is one Update(), anything more is one
        # Regen. COM_REFRESH_MODE=regen is the plan's fallback for a graphics
        # configuration where Update() does not paint: one Regen per job.
        self.com_refresh_update_max: int = max(
            0, int(os.environ.get("COM_REFRESH_UPDATE_MAX", "1"))
        )
        self.com_refresh_mode: str = os.environ.get("COM_REFRESH_MODE", "auto").strip().lower()
        if self.com_refresh_mode not in COM_REFRESH_MODES:
            import logging

            logging.getLogger("autocad_mcp.config").warning(
                "Unknown COM_REFRESH_MODE %r - falling back to 'auto'. Valid values: %s.",
                self.com_refresh_mode,
                ", ".join(COM_REFRESH_MODES),
            )
            self.com_refresh_mode = "auto"

        # Per-call timeout for the headless ezdxf backend. Every ezdxf method
        # runs under one document lock, so a single hung call (pathological DXF,
        # a render that never returns) would otherwise hold that lock forever
        # and deadlock every later tool call. Higher than com_call_timeout
        # because matplotlib PDF/PNG rendering of a large drawing is legitimately
        # slow. Set to 0 to disable the check.
        self.ezdxf_call_timeout: float = float(os.environ.get("EZDXF_CALL_TIMEOUT", "120"))

        # How long disconnect() (server shutdown) waits for a worker thread that
        # is still inside a call, in seconds. Nothing can stop a worker thread:
        # with EZDXF_CALL_TIMEOUT=0 a cancelled call keeps the document lock
        # until its worker ends and nothing is ever quarantined, and at shutdown
        # a worker still running after this grace is logged and left behind on a
        # daemon thread, so the process still exits. 0 = do not wait.
        self.shutdown_grace: float = max(0.0, float(os.environ.get("SHUTDOWN_GRACE", "5")))

        # How many undo steps the headless backend keeps. 0 = off, which is the
        # default because it is not free: ezdxf has no journal, so a step is a
        # full DXF snapshot. Measured — 10 entities 3.1 ms / 19 KB, 1000
        # entities 28 ms / 146 KB, 5000 entities 130 ms / 658 KB — and the cost
        # is paid on every mutating call, so switching it on for a large drawing
        # is a real trade the user should make deliberately rather than inherit.
        self.ezdxf_undo_depth: int = max(0, int(os.environ.get("EZDXF_UNDO_DEPTH", "0")))

        # Spec budgets (B07): a count above one of these is refused by path,
        # naming the variable, before anything is drawn; 0 disables a budget.
        # MAX_SPEC_ITEMS bounds each list of a pid_from_spec / arch_plan_from_spec
        # document (measured 2026-10-24: 200 P&ID items place in 451 ms headless,
        # so 2,000 is about 4.5 s there and far longer on a live seat).
        # MAX_VESSEL_TRAYS bounds a column's trays (10,000 trays build in 3.2 ms;
        # the budget bounds the block definition, not the time).
        self.max_spec_items: int = int(os.environ.get("MAX_SPEC_ITEMS", "2000"))
        self.max_vessel_trays: int = int(os.environ.get("MAX_VESSEL_TRAYS", "500"))

        # HTTP transport: refuse non-loopback bind unless explicitly opted in.
        self.allow_remote_http: bool = (
            os.environ.get("ALLOW_REMOTE_HTTP", "false").lower().strip() == "true"
        )
        self.mcp_auth_token: str = os.environ.get("MCP_AUTH_TOKEN", "").strip()

        # 1.8-17 (policy.py): which tools a client may reach. The defaults are
        # 1.7's behaviour - every tool, every cost. READ_ONLY runs only `read`
        # tools and writes no file; TOOL_ALLOWLIST / TOOL_DENYLIST take comma-
        # separated names or globs (`entity_*`), an entry naming no tool is
        # warned about at startup; MAX_TOOL_COST is the highest @cad_tool cost
        # a call may carry (read|safe|mutate|destructive|escape).
        from policy import parse_cost, parse_patterns

        self.read_only: bool = os.environ.get("READ_ONLY", "false").lower().strip() == "true"
        self.tool_allowlist: tuple[str, ...] = parse_patterns(os.environ.get("TOOL_ALLOWLIST"))
        self.tool_denylist: tuple[str, ...] = parse_patterns(os.environ.get("TOOL_DENYLIST"))
        self.max_tool_cost: str = parse_cost(os.environ.get("MAX_TOOL_COST"))

        # 1.8-18 (security_lisp.py): how system_run_lisp checks an expression.
        # "read" (default) runs what passes the AutoLISP read tier and sends the
        # rest to the 1.7 denylist unchanged; "denylist" is 1.7's behaviour;
        # "off" checks nothing. An unknown value falls back to "read".
        self.lisp_policy: str = parse_lisp_policy(os.environ.get("LISP_POLICY"))

        # 1.8-17 (http_guard.py): the HTTP transport's request guard. A request
        # carrying an Origin header must name one of HTTP_ALLOWED_ORIGINS
        # (default none: every browser origin is refused 403); its Host must be
        # the bound host:port or one of HTTP_ALLOWED_HOSTS (421); a body over
        # MAX_HTTP_BODY_BYTES (8 MiB; 0 disables) is refused 413. A request
        # authenticated with MCP_AUTH_TOKEN_READONLY gets only the mcp:read
        # scope and runs under the `read` ceiling. stdio is unaffected.
        self.http_allowed_origins: tuple[str, ...] = tuple(
            origin.strip().rstrip("/").lower()
            for origin in os.environ.get("HTTP_ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        )
        self.http_allowed_hosts: tuple[str, ...] = tuple(
            host.strip().lower()
            for host in os.environ.get("HTTP_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        )
        self.max_http_body_bytes: int = max(
            0, int(os.environ.get("MAX_HTTP_BODY_BYTES", str(DEFAULT_MAX_HTTP_BODY_BYTES)))
        )
        self.mcp_auth_token_readonly: str = os.environ.get("MCP_AUTH_TOKEN_READONLY", "").strip()

        # Exposed tool profile: "full" (default, everything) or "lean" (a
        # curated 67-tool drafting/inspection core for clients with tight tool
        # caps). Invalid values fall back to "full" with a warning at startup;
        # so does the "core" profile removed in v1.5.0 (see
        # server.REMOVED_TOOL_PROFILES).
        self.tool_profile: str = os.environ.get("TOOL_PROFILE", "full").lower().strip()

        # Tool packs: "all" (default) advertises every vertical pack; a comma
        # list ("core,pid,settings") advertises only those. core cannot be disabled.
        self.tool_packs: str = os.environ.get("TOOL_PACKS", "all").lower().strip()

        # Tool discovery: "search" (default since 1.8.0) advertises search_tools
        # + call_tool + describe_tool and the DISCOVERY_PINNED tools; "off"
        # advertises the full catalog (see discovery/transform.py and
        # server._apply_discovery_mode).
        #
        # Flipped in 1.8-11 on the plan's condition (section 7-2): spike S1
        # measured that a hidden tool stays directly callable (no per-write
        # call_tool wrapper is forced), and the v8 matrix passed 31/31 with its
        # MCP-client tasks going through call_tool. A breaking wire-level
        # change: DISCOVERY_MODE=off restores the 1.7.0 catalog. Invalid values
        # fall back to "off" with a warning at startup.
        self.discovery_mode: str = os.environ.get("DISCOVERY_MODE", "search").lower().strip()

        # 1.8-11: tools search mode keeps listed beside search_tools / call_tool
        # / describe_tool (comma list; empty pins nothing). A pin the active
        # profile or packs hide is simply not listed; an unregistered name is
        # warned about at startup. Static on purpose: a pin set that moved
        # between sessions would break every client's prompt-cache prefix.
        self.discovery_pinned: tuple[str, ...] = tuple(
            dict.fromkeys(
                name.strip()
                for name in os.environ.get(
                    "DISCOVERY_PINNED", "cad_batch,drawing_info,entity_list"
                ).split(",")
                if name.strip()
            )
        )

        # Opt-in 3D solids (COM backend only). Disabled by default because the
        # headless backend cannot produce ACIS solids and live 3D modifies real
        # documents; enable deliberately.
        self.enable_3d: bool = os.environ.get("ENABLE_3D", "false").lower().strip() == "true"


settings = Settings()
