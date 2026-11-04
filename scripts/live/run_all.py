"""Run every live smoke once and write the release's live matrix (1.8-25, live-8).

    uv run --frozen python scripts/live/run_all.py              # every smoke
    uv run --frozen python scripts/live/run_all.py --only pid   # one (repeatable)
    uv run --frozen python scripts/live/run_all.py --out <dir>  # elsewhere

Every ``scripts/smoke_*_com.py`` and ``scripts/smoke_accore.py`` runs in its own
child process, one after the other (a COM failure in one cannot take the next
with it), with ``--json`` pointed at ``<out>/<release>/<smoke>.json``. The
release is ``version.py``'s; ``<smoke>`` is the name the harness writes into a
result (``smoke_pid_com.py`` -> ``pid``). Then ``<out>/<release>/matrix.json``
gets one row per smoke:

* ``status`` - ``pass`` / ``fail`` / ``environment_blocked``. The harness's
  ``unavailable`` (exit 2: no running AutoCAD to attach to, no pywin32, no
  accoreconsole) is ``environment_blocked``; ``refused`` (the active document
  changed under the run) is a ``fail`` - the seat was there and the run did not
  finish. A child that wrote no result is ``environment_blocked`` on exit 2 and
  ``fail`` otherwise; one that overran ``--timeout`` is killed and ``fail``.
  ``harness_status`` keeps the result's own word.
* ``started_at`` / ``ended_at`` (UTC), ``duration_s``, ``exit_code``, the
  result's ``summary`` and ``reason``;
* ``seat`` - ``environment.autocad`` of the result (name, version, build,
  ``year``), None when nothing was attached.

and the file carries the run's ``server`` (version, commit, dirty flag - the
harness's ``server_build()``, the same three fields as a benchmark run
manifest), the first seat any smoke attached to, a ``summary`` per status and
``disconnects``: every line of a child's stdout / stderr, or error of a result
check, that names a COM disconnection - ``RPC_E_DISCONNECTED`` (0x80010108)
or ``RPC_S_SERVER_UNAVAILABLE`` (0x800706BA), the HRESULTs ``ComBackend``
already treats as "the server went away" - stamped with the time run_all read
it (child lines are timestamped as they arrive, not after the child ends).

Nothing here attaches to AutoCAD itself and nothing launches it: each smoke
attaches with ``GetActiveObject`` only (``scripts/live/harness.py``). With no
seat running, every COM smoke is ``environment_blocked`` and accore still runs
for real (it needs no GUI). Every string is home-scrubbed like a harness
result. ``scripts/check_doc_numbers.py`` reads the matrix: a live claim in
README.md / CLAUDE.md needs a ``pass`` row (or an earlier release's evidence).

Exit codes: 0 no smoke failed (``environment_blocked`` is not a failure), 1 a
smoke failed, 2 ``--require-seat`` was given and a smoke was blocked.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_doc_numbers import seat_year, smoke_name  # noqa: E402
from scripts.live.harness import scrub, server_build  # noqa: E402

SCHEMA = "acadmcp-live-matrix/1"
SCRIPTS = ROOT / "scripts"
DEFAULT_OUT = ROOT / "benchmarks" / "results" / "live"
#: One smoke's wall-clock ceiling; an overrun is killed and recorded ``fail``.
DEFAULT_TIMEOUT_S = 1800.0

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_BLOCKED = 2

#: The harness's exit code for ``unavailable`` / ``refused``.
HARNESS_EXIT_UNAVAILABLE = 2

#: HRESULT -> name of the COM disconnection events recorded in ``disconnects``.
DISCONNECT_HRESULTS: dict[int, str] = {
    -2147417848: "RPC_E_DISCONNECTED",
    -2147023174: "RPC_S_SERVER_UNAVAILABLE",
}

_DISCONNECT = re.compile(
    "|".join(
        [
            *(re.escape(str(code)) for code in DISCONNECT_HRESULTS),
            *(f"0x{code & 0xFFFFFFFF:08x}" for code in DISCONNECT_HRESULTS),
            *DISCONNECT_HRESULTS.values(),
            r"object invoked has disconnected from its clients",
            r"RPC server is unavailable",
        ]
    ),
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def release() -> str:
    """The release the matrix is written for: ``version.py``."""
    from version import __version__

    return str(__version__)


def discover(scripts: Path = SCRIPTS) -> list[Path]:
    """Every ``smoke_*_com.py`` (sorted) and then ``smoke_accore.py``."""
    found = sorted(scripts.glob("smoke_*_com.py"))
    accore = scripts / "smoke_accore.py"
    if accore.is_file():
        found.append(accore)
    return found


def disconnect_event(line: str) -> dict | None:
    """``{hresult, name}`` when ``line`` names a COM disconnection, else None."""
    match = _DISCONNECT.search(line)
    if match is None:
        return None
    token = match.group(0).lower()
    for code, name in DISCONNECT_HRESULTS.items():
        if token in (str(code), f"0x{code & 0xFFFFFFFF:08x}", name.lower()):
            return {"hresult": code, "name": name}
    if "disconnected" in token:
        return {"hresult": -2147417848, "name": "RPC_E_DISCONNECTED"}
    return {"hresult": -2147023174, "name": "RPC_S_SERVER_UNAVAILABLE"}


def _pump(stream, name: str, lines: queue.Queue) -> None:
    try:
        for raw in iter(stream.readline, b""):
            lines.put((_now(), name, raw.decode("utf-8", errors="replace").rstrip("\r\n")))
    except (OSError, ValueError):
        pass
    finally:
        lines.put(None)


def run_one(
    script: Path,
    result_path: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    python: str | None = None,
) -> dict:
    """Run one smoke; its matrix row plus the disconnect events it produced."""
    smoke = smoke_name(script.name)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result_path.unlink()  # never read a previous run's result as this one's
    except FileNotFoundError:
        pass
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    started_at, clock = _now(), time.monotonic()
    proc = subprocess.Popen(
        [python or sys.executable, str(script), "--json", str(result_path)],
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    lines: queue.Queue = queue.Queue()
    readers = [
        threading.Thread(target=_pump, args=(proc.stdout, "stdout", lines), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, "stderr", lines), daemon=True),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait(timeout=60)
    for reader in readers:
        reader.join(timeout=10)
    for stream in (proc.stdout, proc.stderr):
        try:
            stream.close()
        except OSError:
            pass
    ended_at = _now()

    events, tail, open_streams = [], [], 2
    while open_streams:
        try:
            item = lines.get_nowait()
        except queue.Empty:
            break
        if item is None:
            open_streams -= 1
            continue
        at, stream, text = item
        if stream == "stderr":
            tail.append(text)
        found = disconnect_event(text)
        if found:
            events.append({"smoke": smoke, "at": at, "stream": stream, **found, "text": text})

    result: dict[str, Any] | None = None
    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = None
    for check in (result or {}).get("checks") or []:
        found = disconnect_event(str(check.get("error") or ""))
        if found:
            events.append(
                {
                    "smoke": smoke,
                    "at": ended_at,  # a check carries no time of its own
                    "stream": f"check: {check.get('name')}",
                    **found,
                    "text": str(check.get("error")),
                }
            )

    harness_status = None if result is None else result.get("status")
    if timed_out:
        status, reason = "fail", f"overran the {timeout:.0f} s timeout and was killed"
    elif result is None:
        blocked = proc.returncode == HARNESS_EXIT_UNAVAILABLE
        status = "environment_blocked" if blocked else "fail"
        reason = f"wrote no result (exit {proc.returncode}): " + " | ".join(tail[-5:])
    elif harness_status == "pass" and proc.returncode == 0:
        status, reason = "pass", result.get("reason")
    elif harness_status == "unavailable":
        status, reason = "environment_blocked", result.get("reason")
    else:
        status = "fail"
        reason = result.get("reason") or f"harness status {harness_status}, exit {proc.returncode}"

    seat = ((result or {}).get("environment") or {}).get("autocad")
    if isinstance(seat, dict):
        seat = {**seat, "year": seat_year(seat)}
    row = {
        "smoke": smoke,
        "script": script.relative_to(ROOT).as_posix()
        if script.is_relative_to(ROOT)
        else script.name,
        "status": status,
        "harness_status": harness_status,
        "exit_code": proc.returncode,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_s": round(time.monotonic() - clock, 3),
        "seat": seat if isinstance(seat, dict) else None,
        "result": result_path.name if result is not None else None,
        "summary": (result or {}).get("summary"),
        "reason": reason,
    }
    return {"row": row, "disconnects": events}


def run_all(
    scripts: list[Path] | None = None,
    *,
    out: Path = DEFAULT_OUT,
    release_name: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    python: str | None = None,
    stream=None,
) -> dict:
    """Run ``scripts`` (default: ``discover()``) and write ``<out>/<release>/matrix.json``."""
    scripts = discover() if scripts is None else list(scripts)
    name = release_name or release()
    folder = Path(out) / name
    folder.mkdir(parents=True, exist_ok=True)
    started_at = _now()
    rows, disconnects = [], []
    for script in scripts:
        print(f"run_all: {script.name} ...", file=stream or sys.stderr, flush=True)
        done = run_one(
            script, folder / f"{smoke_name(script.name)}.json", timeout=timeout, python=python
        )
        rows.append(done["row"])
        disconnects.extend(done["disconnects"])
        print(
            f"run_all: {script.name} -> {done['row']['status']}",
            file=stream or sys.stderr,
            flush=True,
        )
    seat = next((row["seat"] for row in rows if row["seat"]), None)
    matrix = {
        "schema": SCHEMA,
        "release": name,
        "started_at": started_at,
        "ended_at": _now(),
        "server": server_build(),
        "seat": seat,
        "summary": {
            status: sum(row["status"] == status for row in rows)
            for status in ("pass", "fail", "environment_blocked")
        },
        "rows": rows,
        "disconnects": disconnects,
    }
    matrix = scrub(matrix)
    (folder / "matrix.json").write_text(
        json.dumps(matrix, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    return matrix


def exit_code(matrix: dict, *, require_seat: bool = False) -> int:
    if matrix["summary"]["fail"]:
        return EXIT_FAIL
    if require_seat and matrix["summary"]["environment_blocked"]:
        return EXIT_BLOCKED
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="folder that gets <release>/ (default: %(default)s)",
    )
    parser.add_argument("--release", help="release folder name (default: version.py)")
    parser.add_argument(
        "--only", action="append", metavar="SMOKE", help="run only this smoke (pid, accore, ...)"
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--require-seat", action="store_true", help="exit 2 when any smoke was environment_blocked"
    )
    args = parser.parse_args(argv)
    scripts = discover()
    if args.only:
        wanted = set(args.only)
        unknown = wanted - {smoke_name(path.name) for path in scripts}
        if unknown:
            parser.error(f"no such smoke: {sorted(unknown)}")
        scripts = [path for path in scripts if smoke_name(path.name) in wanted]
    matrix = run_all(scripts, out=args.out, release_name=args.release, timeout=args.timeout)
    print(json.dumps(matrix, indent=2, ensure_ascii=False))
    return exit_code(matrix, require_seat=args.require_seat)


if __name__ == "__main__":
    raise SystemExit(main())
