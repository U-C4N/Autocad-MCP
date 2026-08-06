"""Security utilities for AutoCAD MCP Pro.

Path traversal protection and command injection prevention.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from fastmcp.exceptions import ToolError

import config

log = logging.getLogger("autocad_mcp.security")

# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------

_BLOCKED_PATH_PATTERNS = re.compile(
    r"(\.\.[\\/])"
    r"|(%2e%2e)"
    r"|(\\\\[?.]\\)"
    r"|(^\\\\)"
    r"|(\x00)",
    re.IGNORECASE,
)


_DRIVE_ANCHOR = re.compile(r"^[A-Za-z]:$")


def _reject_non_local_anchor(original: str, resolved: Path) -> None:
    r"""Every guard below compares against plain ``X:\`` paths.

    Windows accepts three other anchors that name the same files but compare
    unequal: the extended-length prefix (``\\?\C:\...``), UNC (``\\host\share``)
    and the admin shares (``\\localhost\C$``). The backslash spellings are
    already rejected by ``_BLOCKED_PATH_PATTERNS``, but ``pathlib`` normalises
    the *forward-slash* spellings into them only after ``.resolve()`` — so they
    slipped past the pattern check and then failed to match ``C:/Windows`` in
    the system-directory loop. Proven: ``//?/C:/Windows/win.ini`` handed back a
    working handle to ``C:\Windows\win.ini``.

    A hard input rejection, so a plain ``ToolError`` like its siblings — not a
    capability refusal, which means "this backend cannot" and would wrongly
    suggest another engine would accept it.
    """
    drive = resolved.drive
    if _DRIVE_ANCHOR.match(drive):
        return
    log.warning("Blocked non-local path anchor: %s -> %s", original, resolved)
    raise ToolError(
        f"Path rejected: '{original}' resolves to '{resolved}', which is not a "
        f"plain local drive path (anchor '{drive}'). Extended-length (\\\\?\\), "
        "device and UNC (\\\\host\\share, \\\\localhost\\C$) paths bypass the "
        "system-directory and allowed-root checks and are not accepted. Use a "
        "normal 'X:\\folder\\file.dxf' path."
    )


def validate_path(path: str, *, allow_write: bool = False) -> Path:
    """Resolve and validate a file path against security rules.

    Raises ToolError if:
      - Path contains traversal sequences (..)
      - Path is outside ALLOWED_PATHS (when configured)
      - Path targets a system directory
    """
    if not path or not path.strip():
        raise ToolError("File path cannot be empty.")

    if _BLOCKED_PATH_PATTERNS.search(path):
        log.warning("Blocked path traversal attempt: %s", path)
        raise ToolError(
            f"Path rejected: suspicious traversal pattern detected in '{path}'. "
            "Use absolute paths without '..' components."
        )

    resolved = Path(path).resolve()

    # After .resolve() (drive is only meaningful once resolved) and before the
    # system-directory loop, whose comparisons this is protecting. POSIX has no
    # drive component at all, so the check is Windows-only by construction.
    if os.name == "nt":
        _reject_non_local_anchor(path, resolved)

    _SYSTEM_DIRS = [
        Path("C:/Windows").resolve(),
        Path("C:/Program Files").resolve(),
        Path("C:/Program Files (x86)").resolve(),
        Path("/etc").resolve(),
        Path("/usr").resolve(),
        Path("/bin").resolve(),
        Path("/sbin").resolve(),
        Path("/boot").resolve(),
        Path("/sys").resolve(),
        Path("/proc").resolve(),
    ]
    for sys_dir in _SYSTEM_DIRS:
        try:
            if resolved.is_relative_to(sys_dir):
                log.warning("Blocked system directory access: %s", resolved)
                raise ToolError(
                    f"Path rejected: '{resolved}' is inside a system directory. "
                    "File operations in system directories are not allowed."
                )
        except (ValueError, TypeError):
            pass

    if config.settings.allowed_paths:
        allowed = False
        for allowed_dir in config.settings.allowed_paths:
            try:
                if resolved.is_relative_to(allowed_dir):
                    allowed = True
                    break
            except (ValueError, TypeError):
                pass
        if not allowed:
            allowed_str = ", ".join(str(p) for p in config.settings.allowed_paths)
            log.warning("Path outside allowed directories: %s", resolved)
            raise ToolError(
                f"Path rejected: '{resolved}' is not inside any allowed directory. "
                f"Allowed directories: {allowed_str}. "
                "Set ALLOWED_PATHS environment variable to configure."
            )

    if allow_write:
        parent = resolved.parent
        if not parent.exists():
            raise ToolError(
                f"Parent directory does not exist: '{parent}'. "
                "Create the directory first or use a valid path."
            )

    return resolved


# ---------------------------------------------------------------------------
# Command / LISP sanitization
#
# Why it is NOT incoherent to block INSERT/SAVEAS/NEW/OPEN/PURGE/ERASE here
# while `block_insert` / `drawing_save_as` / `drawing_new` / `drawing_open` /
# `drawing_purge` / `entity_delete` are tools — this gets re-raised, so it is
# recorded once:
#
# `sanitize_command` and `sanitize_lisp` each have exactly ONE caller, both of
# them the COM-only free-text escape hatch into SendCommand. The policy is
# scoped to the CHANNEL, not to the operation, which is capability narrowing
# rather than a contradiction:
#
#   * `block_insert(name, x, y, ...)` takes a block ALREADY in the drawing plus
#     typed geometry. The INSERT *command* takes a file path and pulls in an
#     external DWG, which can carry an attached VBA/LISP payload. Same name,
#     different capability.
#   * `entity_delete(handle)` deletes one named entity. `ERASE ALL` does not.
#   * `drawing_save_as` / `_open` / `_new` route their path through
#     validate_path. A free-text `SAVEAS C:\...` routes through nothing.
# ---------------------------------------------------------------------------

# Matched against the extracted VERB, so the "_XXX" duplicates the old list
# carried are gone — command_verbs() strips every decorator AutoCAD accepts.
#
# Single-letter aliases are deliberately absent. `E` is the ERASE alias, but it
# is also the Extents *option* of ZOOM, so blocking it refuses `_ZOOM\nE\n` —
# the canonical zoom-extents macro, and this repo's own safe-command fixture.
# A guardrail that blocks the most common harmless macro gets switched off, and
# then it guards nothing.
_DANGEROUS_COMMANDS = frozenset(
    {
        # destructive to the drawing
        "ERASE",
        "DELETE",
        "PURGE",
        "WBLOCK",
        "INSERT",
        "XREF",
        # ends or overwrites the session / file
        "QUIT",
        "EXIT",
        "CLOSE",
        "CLOSEALL",
        "QSAVE",
        "SAVE",
        "SAVEAS",
        "NEW",
        "OPEN",
        "RECOVER",
        "RECOVERALL",
        # loads or runs code — every channel found while probing
        "SHELL",
        "SCRIPT",
        "SCR",
        "APPLOAD",
        "NETLOAD",
        "ARX",
        "VBARUN",
        "VBALOAD",
        "VBASTMT",
        "VBAIDE",
        "VBAMAN",
        "CUILOAD",
        "MENULOAD",
        "MENU",
        "CUI",
        # writes files outside the tool layer's path validation
        "EXPORT",
        "ETRANSMIT",
        "PUBLISH",
        "SECURITYOPTIONS",
    }
)

# AutoCAD accepts a command with any mix of these leading decorators:
#   _  international (locale-independent) form
#   .  suppress a redefined command
#   -  the command-line (no dialog) variant
#   '  transparent
_COMMAND_DECORATORS = "_.-*'"


def command_verbs(command: str) -> list[str]:
    """Every command verb in a SendCommand string.

    SendCommand takes a whole macro, not one command: segments are separated by
    a newline or ``;``, and only the FIRST token of a segment is a verb — the
    rest is argument data. Scanning the raw string with a word-boundary regex is
    what refused ``MTEXT 0,0 10,10 Do not erase this part``, which is annotation
    text, while still missing spellings AutoCAD accepts.
    """
    verbs: list[str] = []
    for segment in re.split(r"[\n;]+", command):
        segment = segment.strip()
        if not segment:
            continue
        token = segment.split()[0].split(",")[0].lstrip(_COMMAND_DECORATORS)
        if token:
            verbs.append(token.upper())
    return verbs


# Dangerous LISP symbols. We match these as **bare tokens anywhere** in the
# expression — not only when they appear right after `(` — because aliasing
# tricks like `(setq f command)(f "ERASE")` reference the symbol as a value.
_DANGEROUS_LISP_PATTERNS = [
    # File and shell I/O
    r"startapp",
    r"dos_[\w-]*",
    r"vl-file-(?:delete|rename|copy|directory-p)",
    r"vl-mkdir",
    r"vl-directory-files",
    r"read-line",
    r"write-line",
    r"findfile",
    r"getfiled",
    # Command execution (every known channel)
    r"command(?:-s)?",
    r"vl-cmdf",
    # ActiveX/COM family — vla-*, vlax-*, vlr-*; covers vla-SendCommand etc.
    r"vla[xr]?-[\w-]+",
    # AutoCAD Express Tools — acet-sys-shell and friends
    r"acet-[\w-]+",
    # Code loading
    r"vl-acad-(?:de|un)fun",
    r"(?:auto)?load",
    r"arxload",
    # Indirection vectors that bypass head-only matching
    r"eval",
    r"apply",
    r"funcall",
    r"function",
    r"\(\s*quote\s+command\b",
    # Custom command invocation prefix `(c:my-cmd)`
    r"c:[\w-]+",
    # Error handler hijack
    r"\*push-error-using-command\*",
    r"\*push-error-using-stack\*",
]

# A double-quoted literal is DATA — drawing text, a layer name, a note. The
# symbol denylist must not read it. Without this, an engineering CAD server
# refused its own vocabulary: '(setq desc "Max load 500 kg")' matched
# r"(?:auto)?load" and '(setq layer "C:CENTER")' matched r"c:[\w-]+".
_LISP_STRING_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')

_DANGEROUS_LISP_PATTERN = re.compile(
    r"(?:(?<![\w-])|^)(?:" + "|".join(_DANGEROUS_LISP_PATTERNS) + r")(?![\w-])",
    re.IGNORECASE,
)


def sanitize_command(command: str) -> str:
    """Validate an AutoCAD command string for safety.

    Raises ToolError if a dangerous command pattern is detected
    (unless DANGEROUS_COMMANDS_ENABLED is true).
    """
    if not command or not command.strip():
        raise ToolError("Command string cannot be empty.")

    if config.settings.dangerous_commands_enabled:
        log.info("Dangerous commands enabled — bypassing sanitization for: %s", command[:80])
        return command

    blocked = {verb for verb in command_verbs(command) if verb in _DANGEROUS_COMMANDS}
    if blocked:
        log.warning("Blocked dangerous command: %s", command[:120])
        raise ToolError(
            f"Command rejected: {', '.join(sorted(blocked))} "
            f"{'is a restricted command' if len(blocked) == 1 else 'are restricted commands'}. "
            "This denylist is a guardrail against accidental destruction, not a "
            "security boundary — "
            "prefer the typed tools (entity_delete, drawing_save_as, block_insert, "
            "drawing_purge), which validate their arguments. Set "
            "DANGEROUS_COMMANDS_ENABLED=true to allow all commands."
        )

    return command


def sanitize_lisp(expression: str) -> str:
    """Validate an AutoLISP expression for safety.

    Raises ToolError if a dangerous LISP function is detected
    (unless DANGEROUS_COMMANDS_ENABLED is true).
    """
    if not expression or not expression.strip():
        raise ToolError("LISP expression cannot be empty.")

    if config.settings.dangerous_commands_enabled:
        log.info(
            "Dangerous commands enabled — bypassing LISP sanitization for: %s", expression[:80]
        )
        return expression

    # Blank the literals, then scan. The ORIGINAL expression is returned:
    # this decides whether to run it, it does not rewrite it.
    code_only = _LISP_STRING_LITERAL.sub('""', expression)
    if _DANGEROUS_LISP_PATTERN.search(code_only):
        log.warning("Blocked dangerous LISP expression: %s", expression[:120])
        raise ToolError(
            "LISP expression rejected: contains a restricted function. "
            "Restricted functions include startapp, command, load, eval, file I/O, etc. "
            "Set DANGEROUS_COMMANDS_ENABLED=true to allow all LISP expressions."
        )

    return expression
