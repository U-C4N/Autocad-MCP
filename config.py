"""Centralized configuration for AutoCAD MCP Pro."""

from __future__ import annotations

import os
from pathlib import Path

#: ProgID the live backend attaches to when ``CAD_PROGID`` is unset. The only
#: CAD this project is developed and tested against.
DEFAULT_CAD_PROGID = "AutoCAD.Application"


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

        # Reject opening DXF files larger than this many bytes (default 50 MB).
        # Set to 0 to disable the check.
        self.max_dxf_bytes: int = int(os.environ.get("MAX_DXF_BYTES", str(50 * 1024 * 1024)))

        # Hard cap on list/select limit params to keep tool responses bounded.
        self.max_list_limit: int = int(os.environ.get("MAX_LIST_LIMIT", "5000"))

        # Per-call COM timeout. AutoCAD hangs (modal dialogs, long Regen) would
        # otherwise block the single-thread STA executor forever.
        self.com_call_timeout: float = float(os.environ.get("COM_CALL_TIMEOUT", "60"))

        # Per-call timeout for the headless ezdxf backend. Every ezdxf method
        # runs under one document lock, so a single hung call (pathological DXF,
        # a render that never returns) would otherwise hold that lock forever
        # and deadlock every later tool call. Higher than com_call_timeout
        # because matplotlib PDF/PNG rendering of a large drawing is legitimately
        # slow. Set to 0 to disable the check.
        self.ezdxf_call_timeout: float = float(os.environ.get("EZDXF_CALL_TIMEOUT", "120"))

        # How many undo steps the headless backend keeps. 0 = off, which is the
        # default because it is not free: ezdxf has no journal, so a step is a
        # full DXF snapshot. Measured — 10 entities 3.1 ms / 19 KB, 1000
        # entities 28 ms / 146 KB, 5000 entities 130 ms / 658 KB — and the cost
        # is paid on every mutating call, so switching it on for a large drawing
        # is a real trade the user should make deliberately rather than inherit.
        self.ezdxf_undo_depth: int = max(0, int(os.environ.get("EZDXF_UNDO_DEPTH", "0")))

        # HTTP transport: refuse non-loopback bind unless explicitly opted in.
        self.allow_remote_http: bool = (
            os.environ.get("ALLOW_REMOTE_HTTP", "false").lower().strip() == "true"
        )
        self.mcp_auth_token: str = os.environ.get("MCP_AUTH_TOKEN", "").strip()

        # Exposed tool profile: "full" (default, everything) or "lean" (a
        # curated ~47-tool drafting/inspection core for clients with tight tool
        # caps). Invalid values fall back to "full" with a warning at startup;
        # so does the "core" profile removed in v1.5.0 (see
        # server.REMOVED_TOOL_PROFILES).
        self.tool_profile: str = os.environ.get("TOOL_PROFILE", "full").lower().strip()

        # Tool discovery: "off" (default) advertises the full catalog; "search"
        # replaces it with a search tool plus a call_tool proxy (see
        # discovery/transform.py and server._apply_discovery_mode).
        #
        # Default "off" deliberately. "search" is the intended v1.x default,
        # but flipping it changes what every connected client sees on
        # list_tools — a breaking wire-level change that needs its own
        # migration note and README pass, neither of which ships in v1.5.0.
        # Invalid values fall back to "off" with a warning at startup.
        self.discovery_mode: str = os.environ.get("DISCOVERY_MODE", "off").lower().strip()

        # Opt-in 3D solids (COM backend only). Disabled by default because the
        # headless backend cannot produce ACIS solids and live 3D modifies real
        # documents; enable deliberately.
        self.enable_3d: bool = os.environ.get("ENABLE_3D", "false").lower().strip() == "true"


settings = Settings()
