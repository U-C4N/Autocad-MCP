"""Reading a DWG on the headless engine (K1, 1.8.0): convert it to DXF first.

ezdxf reads DXF only. ``drawing_open`` on a ``.dwg`` path (the extension is
authoritative, N2) therefore converts, through the first route that works:

1. **odafc** - the ODA File Converter through ``ezdxf.addons.odafc`` (the
   route ``drawing_export_dwg`` already writes with), when it is installed;
2. **accoreconsole** - AutoCAD's own console engine, ``accoreconsole.exe``
   (``ACCORECONSOLE_PATH``; unset means AutoCAD 2026's installed copy when it
   exists, ``off`` disables it), running headless without the AutoCAD GUI;
3. otherwise a refusal with capability ``dwg_read`` naming both routes.

A route that is available but fails does not end the attempt: the next one is
tried, and when every available route failed the open is refused
``dwg_conversion_failed`` with each attempt (route, reason, exit code, the log's
last lines). Nothing is opened and the open document is unchanged either way.

The accoreconsole rules (security - the process is a full AutoCAD engine):

* **A private ASCII staging folder** ``<ACADMCP_STATE_DIR>/accore/<12 hex>``:
  the caller's drawing is *copied in* as ``in.dwg``, so neither its path nor
  its name reaches the command line or the script, and no ``acaddoc.lsp``
  beside the caller's drawing is ever on the console's search path. A state
  folder whose path is not ASCII is used through its Windows short (8.3) name;
  without one the route is unavailable and says so.
* **The script holds only what this module generates**: ``_SAVEAS _DXF _V
  _2018 16 "<staging>\\out.dxf"`` - every command and keyword ``_``-prefixed
  (language-independent), the two values a number and the staging path.
  Nothing from the caller is ever written into it.
* **The log is read back**: the console writes UTF-16LE to stdout, captured to
  ``accore.log`` in the staging folder and reported (its last
  :data:`LOG_LINES_SHOWN` lines) on success and on failure.
* **A deadline**: ``ACCORECONSOLE_TIMEOUT_S`` (default 60, never more than
  ``EZDXF_CALL_TIMEOUT`` minus 5 s); an overrun kills the whole process tree
  (``taskkill /F /T``) and is reported ``timed_out``.
* The staging folder is removed on every path; one a killed server left behind
  is swept on the next conversion once it is an hour old.

Export to DWG and AUDIT through accoreconsole are 1.8.1, not here.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import config

from .base import FeatureCapability, UnsupportedCapabilityError

log = logging.getLogger(__name__)

__all__ = [
    "ACCORE_DEFAULT_PATH",
    "DXF_VERSION",
    "LOG_LINES_SHOWN",
    "DwgConversionError",
    "accoreconsole_exe",
    "build_script",
    "capability",
    "convert_with_accoreconsole",
    "decode_log",
    "effective_timeout",
    "read_dwg",
    "refusal",
    "staging_root",
]

#: Where AutoCAD 2026 installs its console engine (measured on this machine).
ACCORE_DEFAULT_PATH = r"C:\Program Files\Autodesk\AutoCAD 2026\accoreconsole.exe"

#: ``ACCORECONSOLE_PATH`` values that switch the route off.
_DISABLED = frozenset({"off", "none", "false", "0", "no"})

#: The DXF release the console writes (AutoCAD 2018 = AC1032, ezdxf reads it).
DXF_VERSION = "2018"

#: How many log lines a result or a refusal carries.
LOG_LINES_SHOWN = 40

#: A staging folder this old is a killed server's leftover, swept on the next run.
_STALE_STAGING_S = 3600.0

#: Seconds left to the ezdxf call deadline once the conversion is over.
_CALL_MARGIN_S = 5.0

_ODA_URL = "https://www.opendesign.com/guestfiles/oda_file_converter"


class DwgConversionError(RuntimeError):
    """Every available DWG -> DXF route failed; nothing was opened.

    ``attempts`` lists ``{route, reason, ...}`` per route tried, with the
    console's exit code, ``timed_out`` and its log's last lines.
    """

    kind = "dwg_conversion_failed"

    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = [dict(row) for row in attempts]
        self.details = {"attempts": self.attempts}


# ── where the console is ──────────────────────────────────────────────────────


def accoreconsole_exe() -> tuple[str | None, str]:
    """``(exe path, "")`` when the console route is usable, else ``(None, why)``."""
    raw = config.settings.accoreconsole_path
    if raw.lower() in _DISABLED:
        return None, "disabled by ACCORECONSOLE_PATH=off"
    if not raw:
        if Path(ACCORE_DEFAULT_PATH).is_file():
            return ACCORE_DEFAULT_PATH, ""
        return None, "ACCORECONSOLE_PATH is unset and AutoCAD 2026's accoreconsole.exe is absent"
    if not Path(raw).is_file():
        return None, f"ACCORECONSOLE_PATH={raw} is not a file"
    return raw, ""


def effective_timeout() -> float:
    """``ACCORECONSOLE_TIMEOUT_S``, kept inside the ezdxf call deadline."""
    timeout = config.settings.accoreconsole_timeout_s
    call = float(config.settings.ezdxf_call_timeout or 0)
    if call > 0:
        timeout = min(timeout, max(1.0, call - _CALL_MARGIN_S))
    return timeout


def _short_path(path: str) -> str | None:
    """The Windows 8.3 form of an existing ``path``, or None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        size = ctypes.windll.kernel32.GetShortPathNameW(str(path), None, 0)
        if not size:
            return None
        buffer = ctypes.create_unicode_buffer(size)
        if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, size):
            return None
        return buffer.value
    except (OSError, AttributeError, ValueError):
        return None


def staging_root() -> tuple[Path | None, str]:
    """``(<state dir>/accore, "")`` as an ASCII path, or ``(None, why)``."""
    from .atomic_write import state_dir

    state = state_dir()
    if state is None:
        return None, "ACADMCP_STATE_DIR is refused or unusable (see system_status.journal)"
    root = Path(state) / "accore"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"cannot create the staging folder under ACADMCP_STATE_DIR ({exc})"
    text = str(root)
    if not text.isascii():
        short = _short_path(text)
        if short is None or not short.isascii():
            return None, (
                "the staging folder under ACADMCP_STATE_DIR is not an ASCII path and has no "
                "ASCII short name; point ACADMCP_STATE_DIR at an ASCII folder"
            )
        text = short
    return Path(text), ""


# ── the capability and the refusal ───────────────────────────────────────────


def capability(odafc: bool) -> FeatureCapability:
    """The ezdxf map's ``dwg_read`` row, re-evaluated per call."""
    if odafc:
        return FeatureCapability(True, "odafc", reason="oda_file_converter_installed")
    exe, _why = accoreconsole_exe()
    if exe is not None:
        return FeatureCapability(True, "accoreconsole", reason="autocad_core_console_dwg_to_dxf")
    return FeatureCapability(
        False,
        reason="requires_oda_file_converter_or_accoreconsole:set_ACCORECONSOLE_PATH",
    )


def refusal(path: str, backend: str = "ezdxf") -> UnsupportedCapabilityError:
    """``dwg_read``: no converter on this machine; names both routes."""
    _exe, why = accoreconsole_exe()
    return UnsupportedCapabilityError(
        "dwg_read",
        f"drawing_open: the headless {backend} backend cannot read '{path}': a DWG is read by "
        "converting it to DXF and no converter is available here (ODA File Converter: not "
        f"installed; accoreconsole: {why or 'unavailable'}). Nothing was opened and the open "
        f"document is unchanged. Install the ODA File Converter ({_ODA_URL}), or point "
        "ACCORECONSOLE_PATH at AutoCAD's accoreconsole.exe, or switch to the live COM backend "
        "(AUTOCAD_MCP_BACKEND=com, needs Windows + AutoCAD), or open the .dxf instead.",
    )


# ── the script and the log ───────────────────────────────────────────────────


def build_script(out_dxf: str) -> str:
    """The whole console script: SAVEAS to ``out_dxf`` as a DXF 2018, nothing else.

    ``out_dxf`` is this module's own staging path; a non-ASCII path or one
    holding a quote is refused rather than written.
    """
    out_dxf = str(out_dxf)
    if not out_dxf.isascii() or '"' in out_dxf or "\n" in out_dxf or "\r" in out_dxf:
        raise ValueError(f"accoreconsole script: refusing the output path {out_dxf!r}")
    lines = ["_SAVEAS", "_DXF", "_V", f"_{DXF_VERSION}", "16", f'"{out_dxf}"']
    return "\r\n".join(lines) + "\r\n"


def decode_log(raw: bytes) -> list[str]:
    """The console's stdout as lines: UTF-16LE (with or without a BOM), else UTF-8."""
    if raw.startswith(b"\xff\xfe"):
        text = raw[2:].decode("utf-16-le", errors="replace")
    elif len(raw) >= 2 and raw[1::2].count(0) * 2 >= len(raw) // 2:
        text = raw.decode("utf-16-le", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")
    text = text.replace("\x00", "")
    return [line.rstrip() for line in text.splitlines() if line.strip()]


# ── running the console ──────────────────────────────────────────────────────


def _argv(exe: str, dwg: str, script: str) -> list[str]:
    """The command line: the input drawing and the script, nothing else."""
    return [exe, "/i", dwg, "/s", script]


def _kill_tree(proc: subprocess.Popen) -> None:
    """End ``proc`` and every process it started (taskkill /T on Windows)."""
    if sys.platform == "win32":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("taskkill of accoreconsole pid %s failed: %s", proc.pid, exc)
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        log.warning("accoreconsole pid %s did not exit after kill", proc.pid)


def _sweep_stale(root: Path) -> None:
    now = time.time()
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for child in children:
        try:
            if child.is_dir() and now - child.stat().st_mtime > _STALE_STAGING_S:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def convert_with_accoreconsole(source: str, read) -> tuple[object, dict]:
    """Convert ``source`` (a DWG) to DXF with the console and ``read(dxf_path)`` it.

    Returns ``(read's result, report)``; raises :class:`DwgConversionError`
    with one attempt row when the console route is unavailable or fails.
    """
    exe, why = accoreconsole_exe()
    if exe is None:
        raise DwgConversionError(why, [{"route": "accoreconsole", "reason": why}])
    root, why = staging_root()
    if root is None:
        raise DwgConversionError(why, [{"route": "accoreconsole", "reason": why}])
    _sweep_stale(root)
    timeout = effective_timeout()
    folder = root / uuid.uuid4().hex[:12]
    folder.mkdir()
    attempt: dict = {"route": "accoreconsole", "timeout_s": timeout}
    started = time.monotonic()
    try:
        dwg = folder / "in.dwg"
        out = folder / "out.dxf"
        script = folder / "convert.scr"
        shutil.copyfile(source, dwg)
        script.write_bytes(build_script(str(out)).encode("ascii"))
        log_path = folder / "accore.log"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with open(log_path, "wb") as log_handle:
            try:
                proc = subprocess.Popen(
                    _argv(exe, str(dwg), str(script)),
                    cwd=str(folder),
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=flags,
                )
            except OSError as exc:
                attempt["reason"] = f"{exe} could not be started ({type(exc).__name__}: {exc})"
                raise DwgConversionError(attempt["reason"], [attempt]) from exc
            try:
                attempt["exit_code"] = proc.wait(timeout=timeout)
                attempt["timed_out"] = False
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                attempt["exit_code"] = proc.returncode
                attempt["timed_out"] = True
        attempt["seconds"] = round(time.monotonic() - started, 3)
        lines = decode_log(log_path.read_bytes())
        attempt["log"] = lines[-LOG_LINES_SHOWN:]
        attempt["log_lines"] = len(lines)
        if attempt["timed_out"]:
            attempt["reason"] = (
                f"timed_out after {timeout:g} s; the process tree was killed "
                "(raise ACCORECONSOLE_TIMEOUT_S for a large drawing)"
            )
            raise DwgConversionError(attempt["reason"], [attempt])
        if not out.is_file() or out.stat().st_size == 0:
            attempt["reason"] = "the console wrote no DXF (see log)"
            raise DwgConversionError(attempt["reason"], [attempt])
        try:
            result = read(str(out))
        except Exception as exc:  # noqa: BLE001 - reported as the attempt's reason
            attempt["reason"] = f"the DXF it wrote did not read back ({type(exc).__name__}: {exc})"
            raise DwgConversionError(attempt["reason"], [attempt]) from exc
        attempt.pop("reason", None)
        return result, attempt
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _read_with_odafc(source: str):
    from ezdxf.addons import odafc

    return odafc.readfile(source)


def read_dwg(path: str, *, odafc: bool, read) -> tuple[object, dict]:
    """``(document, dwg_read report)`` for the DWG at ``path``.

    ``odafc`` says whether the ODA File Converter is installed (the engine's
    own probe); ``read(dxf_path)`` reads a converted DXF (the engine's DXF
    reader). Refused ``dwg_read`` when no route is available, raises
    :class:`DwgConversionError` when every available route failed.
    """
    exe, _why = accoreconsole_exe()
    if not odafc and exe is None:
        raise refusal(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"drawing_open: {path} does not exist")
    attempts: list[dict] = []
    if odafc:
        started = time.monotonic()
        try:
            doc = _read_with_odafc(path)
        except Exception as exc:  # noqa: BLE001 - the next route is tried
            attempts.append(
                {
                    "route": "odafc",
                    "reason": f"{type(exc).__name__}: {' '.join(str(exc).split())}"[:400],
                    "seconds": round(time.monotonic() - started, 3),
                }
            )
        else:
            report = {"route": "odafc", "seconds": round(time.monotonic() - started, 3)}
            report["dxf_version"] = getattr(doc, "dxfversion", None)
            return doc, report
    if exe is not None:
        try:
            doc, report = convert_with_accoreconsole(path, read)
        except DwgConversionError as exc:
            attempts.extend(exc.attempts)
        else:
            report["dxf_version"] = getattr(doc, "dxfversion", None)
            if attempts:
                report["failed_routes"] = attempts
            return doc, report
    reasons = "; ".join(f"{row['route']}: {row.get('reason')}" for row in attempts)
    raise DwgConversionError(
        f"drawing_open: {Path(path).name} could not be converted to DXF ({reasons}). Nothing "
        "was opened and the open document is unchanged. The attempts carry each route's log; "
        "the live COM backend (AUTOCAD_MCP_BACKEND=com) reads a DWG natively.",
        attempts,
    )
