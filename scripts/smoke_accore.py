"""Exercise the K1 DWG reader once against the real accoreconsole on this machine.

Written on the live harness's conventions (scripts/live/harness.py): the same
JSON schema, checks, exit codes and home-scrubbed paths. It never attaches to a
running AutoCAD and never touches an operator's drawing: accoreconsole is
AutoCAD's console engine, started headless by the code under test
(``backends/dwg_read.py``) on a copy of a bundled template, in a state folder
of its own that is removed at the end.

    templates/iso_a3_mech.dwt copied as <tmp>/iso_a3_mech.dwg (a DWT is a DWG container)
    EzdxfBackend.drawing_open(<tmp>.dwg)             -> dwg_read.route "accoreconsole"
    the opened drawing vs templates/iso_a3_mech.dxf  -> same layers, blocks, entity counts
    drawing_save_as(<tmp>/roundtrip.dxf)             -> the file reads back the same
    ACCORECONSOLE_TIMEOUT_S=0.05 on the same DWG     -> dwg_conversion_failed, timed_out,
                                                        no accoreconsole process left behind

Checks also hold the script the console ran to the module's own generated
lines (every command ``_``-prefixed, nothing from the caller) and the staging
folder to an ASCII path that is gone afterwards.

Exit codes are the harness's: 0 every check passed, 1 one failed or raised,
2 no accoreconsole on this machine (``unavailable``).

    uv run --frozen python scripts/smoke_accore.py [--json PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.live.harness import LiveHarness, scrub  # noqa: E402

TEMPLATE_DWT = ROOT / "templates" / "iso_a3_mech.dwt"
TEMPLATE_DXF = ROOT / "templates" / "iso_a3_mech.dxf"


def _counts(doc) -> dict:
    return {
        "layers": sorted(layer.dxf.name for layer in doc.layers),
        "blocks": sorted(block.name for block in doc.blocks),
        "entities": {layout.name: len(layout) for layout in doc.layouts},
    }


def _console_pids() -> set[int]:
    """Process ids of every accoreconsole.exe running now (tasklist)."""
    if sys.platform != "win32":
        return set()
    done = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq accoreconsole.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    pids = set()
    for row in csv.reader(io.StringIO(done.stdout)):
        if len(row) > 1 and row[0].lower() == "accoreconsole.exe":
            pids.add(int(row[1]))
    return pids


def _version(log: list[str]) -> str | None:
    for line in log:
        if line.startswith("Version Number:"):
            return line.split(":", 1)[1].strip()
    return None


async def body(h: LiveHarness, exe: str, work: Path) -> dict:
    import ezdxf

    from backends import dwg_read
    from backends.ezdxf_backend import EzdxfBackend

    scripts: list[str] = []
    original_build = dwg_read.build_script

    def recording_build(out_dxf: str) -> str:
        text = original_build(out_dxf)
        scripts.append(text)
        return text

    dwg_read.build_script = recording_build
    backend = EzdxfBackend()
    await backend.connect()
    try:
        h.stage("open")
        dwg = work / "iso_a3_mech.dwg"
        shutil.copyfile(TEMPLATE_DWT, dwg)
        before = _console_pids()
        opened = await backend.drawing_open(str(dwg))
        report = opened.get("dwg_read") or {}
        if h.autocad is not None:
            h.autocad["version"] = _version(report.get("log") or [])
        truth = _counts(ezdxf.readfile(TEMPLATE_DXF))
        observed = _counts(backend._doc)

        h.stage("roundtrip")
        target = work / "roundtrip.dxf"
        await backend.drawing_save_as(str(target), "dxf")
        saved = _counts(ezdxf.readfile(target))

        h.stage("timeout")
        os.environ["ACCORECONSOLE_TIMEOUT_S"] = "0.05"
        try:
            await backend.drawing_open(str(dwg))
            timeout_outcome = {"raised": None}
        except dwg_read.DwgConversionError as exc:
            timeout_outcome = {
                "raised": type(exc).__name__,
                "kind": exc.kind,
                "timed_out": exc.attempts[-1].get("timed_out"),
                "reason": exc.attempts[-1].get("reason"),
            }
        finally:
            os.environ.pop("ACCORECONSOLE_TIMEOUT_S", None)
        await asyncio.sleep(1.0)
        left_running = sorted(_console_pids() - before)
        root, _why = dwg_read.staging_root()
        staged_left = sorted(p.name for p in root.iterdir()) if root is not None else None
    finally:
        dwg_read.build_script = original_build
        await backend.disconnect()

    result = {
        "exe": exe,
        "dwg_read": report,
        "counts": {"truth": truth, "opened": observed, "saved": saved},
        "scripts": scripts,
        "timeout": timeout_outcome,
        "left_running": left_running,
        "staging_root_is_ascii": str(root).isascii() if root is not None else None,
        "staged_left": staged_left,
    }
    h.stage("checks")
    assess(h, result)
    return result


def assess(h: LiveHarness, result: dict) -> None:
    report, counts = result["dwg_read"], result["counts"]
    h.check(
        "drawing_open converts the DWG through accoreconsole",
        report.get("route") == "accoreconsole" and report.get("timed_out") is False,
        expected={"route": "accoreconsole", "timed_out": False},
        observed={k: report.get(k) for k in ("route", "timed_out", "exit_code", "seconds")},
    )
    h.check(
        "the console's UTF-16LE log is read back",
        any("_SAVEAS" in line for line in report.get("log") or []),
        expected="a log line holding _SAVEAS",
        observed={"log_lines": report.get("log_lines"), "tail": (report.get("log") or [])[-3:]},
    )
    h.check(
        "the opened drawing holds the DXF twin's layers, blocks and entity counts",
        counts["opened"] == counts["truth"],
        expected=counts["truth"],
        observed=counts["opened"],
    )
    h.check(
        "the drawing saved as DXF reads back the same",
        counts["saved"] == counts["truth"],
        expected=counts["truth"],
        observed=counts["saved"],
    )
    scripts = result["scripts"]
    lines = [line for text in scripts for line in text.split("\r\n") if line]
    commands = [line for line in lines if not line.startswith('"') and not line.isdigit()]
    h.check(
        "the script holds only the module's _-prefixed commands and its own staging path",
        bool(scripts)
        and all(line.startswith("_") for line in commands)
        and all(text.isascii() for text in scripts),
        expected='_SAVEAS _DXF _V _2018 16 "<staging>\\out.dxf"',
        observed=scripts[:1],
    )
    timeout = result["timeout"]
    h.check(
        "an overrun is killed and reported dwg_conversion_failed / timed_out",
        timeout.get("kind") == "dwg_conversion_failed" and timeout.get("timed_out") is True,
        expected={"kind": "dwg_conversion_failed", "timed_out": True},
        observed=timeout,
    )
    h.check(
        "no accoreconsole process is left running",
        result["left_running"] == [],
        expected=[],
        observed=result["left_running"],
    )
    h.check(
        "the staging folder is ASCII and nothing is left in it",
        result["staging_root_is_ascii"] is True and result["staged_left"] == [],
        expected={"ascii": True, "left": []},
        observed={"ascii": result["staging_root_is_ascii"], "left": result["staged_left"]},
    )


async def run(json_path: str | None = None, stream=None) -> int:
    from backends import dwg_read

    h = LiveHarness("accore")
    state = Path(tempfile.mkdtemp(prefix="acadmcp_smoke_accore_state_"))
    work = Path(tempfile.mkdtemp(prefix="acadmcp_smoke_accore_"))
    saved_state = os.environ.get("ACADMCP_STATE_DIR")
    os.environ["ACADMCP_STATE_DIR"] = str(state)
    try:
        exe, why = dwg_read.accoreconsole_exe()
        if exe is None:
            h.unavailable(f"no accoreconsole on this machine: {why}")
        else:
            h.autocad = {"name": "AutoCAD Core Console", "version": None, "build": None,
                         "progid": None, "path": exe}  # fmt: skip
            h.report.update(await body(h, exe, work))
    except Exception as exc:
        h.error(exc)
    finally:
        if saved_state is None:
            os.environ.pop("ACADMCP_STATE_DIR", None)
        else:
            os.environ["ACADMCP_STATE_DIR"] = saved_state
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(state, ignore_errors=True)
    result = scrub(json.loads(json.dumps(h.result(), default=str)))
    text = json.dumps(result, indent=2, ensure_ascii=False)
    print(text, file=stream or sys.stdout)
    if json_path:
        target = Path(json_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
    return result["exit_code"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", metavar="PATH", help="also write the JSON result to PATH")
    args = parser.parse_args(argv)
    return asyncio.run(run(json_path=args.json))


if __name__ == "__main__":
    raise SystemExit(main())
