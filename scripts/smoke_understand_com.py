"""Exercise track H once against the live AutoCAD on this machine.

Builds the synthetic plant pair (tests/fixtures/plant_pair.py) in a temporary
folder and reads it through the live engine - never an operator's drawing:

    open pid.dxf    -> take_snapshot(backend)            (Document.Export, "DXF")
                    -> drawing_save_as(pid.dwg), close it
                    -> take_snapshot(backend, pid.dwg)   (opened read-only, exported, closed)
    open layout.dxf -> take_snapshot(backend)
    pipe_rows / cable_rows on the two live snapshots, against the pair's truth
    entity_move one tag text by 10 mm -> take_snapshot -> diff_snapshots
                    against the snapshot taken before the move

and prints what came back as JSON. Exit codes:

    0  every live snapshot holds the same model-space entities (by type) as the
       file it came from, the takeoffs match the truth, and the diff reports
       exactly the one moved entity
    1  it ran, but something disagreed (the JSON says what)
    2  no live CAD application reachable (pywin32 missing, or no running
       instance to attach to) -- the COM paths then remain fake-tested only

**The operator's drawings are never touched.** AutoCAD may have other
documents open, with unsaved work in them, while this runs. So:

* the smoke only ever attaches to a running instance (``GetActiveObject``);
  it never creates one, because ``Dispatch`` would launch AutoCAD;
* every document it works in is one it brought in itself from the files it
  just wrote into its own temporary folder, and it knows them by full path -
  never by a name another document could share;
* every call that reads or writes *the current document* (a live snapshot, the
  DWG save, the move) first checks, inside the same COM-thread call, that the
  active document is the smoke's own - and refuses otherwise;
* on the way out (also when a step fails) it closes its own documents with
  ``Close(False)`` and re-activates the document that was active when it
  started; unless ``--keep`` is given, then its documents stay open;
* nothing it prints names a document other than its own.

    AUTOCAD_MCP_BACKEND=com uv run --frozen python scripts/smoke_understand_com.py
    uv run --frozen python scripts/smoke_understand_com.py --keep   # leave them open
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: ``RPC_E_CALL_REJECTED``: AutoCAD's message filter refused the call because
#: it was busy (a document still initialising after ``Documents.Open``). A
#: refused call was not executed.
RPC_E_CALL_REJECTED = -2147418111

#: How long a refused call is retried before the smoke gives up on it.
REJECTED_PATIENCE_S = 90.0

#: Full paths (normalised) of the documents this run opened and has not closed.
_OPENED: list[str] = []

#: The run's state shared with the COM thread: the folder its files live in,
#: the document that must be active for the next current-document call (None:
#: the call does not touch the current document), and the document that was
#: active when the run started (a COM object, used on the COM thread only).
_STATE: dict = {"folder": None, "expect": None, "start": None}


class NotOurDocument(RuntimeError):
    """The active document is not the one the smoke is about to read or write."""


def _key(path) -> str:
    return str(Path(str(path)).resolve()).lower()


def _ours(path) -> bool:
    folder = _STATE["folder"]
    return bool(path) and folder is not None and Path(_key(path)).parent == Path(_key(folder))


def _check_active() -> None:
    """COM thread: refuse unless the active document is the expected own one."""
    expected = _STATE["expect"]
    if expected is None:
        return
    from backends import com_backend as com

    app = com._acad_app()
    active = str(app.ActiveDocument.FullName) if int(app.Documents.Count) else ""
    if not active or _key(active) != expected:
        # Never name the other document: it may be an operator's drawing.
        raise NotOurDocument(
            f"refused: the active document is not the smoke's own {Path(expected).name}"
        )


def _patient(backend) -> None:
    """Retry any call AutoCAD refused with ``RPC_E_CALL_REJECTED``, and run the
    active-document check in the same COM-thread call as the call it guards."""
    run = backend._run

    async def patient(func, *args, **kwargs):
        def guarded(*a, **kw):
            _check_active()
            return func(*a, **kw)

        deadline = time.monotonic() + REJECTED_PATIENCE_S
        pause = 0.5
        while True:
            try:
                return await run(guarded, *args, **kwargs)
            except RuntimeError as exc:
                cause = exc.__cause__
                hr = cause.args[0] if cause is not None and cause.args else None
                if hr != RPC_E_CALL_REJECTED or time.monotonic() + pause > deadline:
                    raise
                await asyncio.sleep(pause)
                pause = min(pause * 2.0, 5.0)

    backend._run = patient


class _Current:
    """``async with _Current(path):`` - calls inside it must find ``path`` active."""

    def __init__(self, path) -> None:
        self.path = _key(path)

    async def __aenter__(self):
        _STATE["expect"] = self.path
        return self

    async def __aexit__(self, *exc):
        _STATE["expect"] = None
        return False


async def _attach(backend) -> str | None:
    """Attach to the running instance only, and remember the active document.

    Returns the application's version, or None when nothing is running (the
    backend's own ``_acad_app`` would fall back to ``Dispatch`` and launch
    AutoCAD - a smoke must never do that)."""
    import win32com.client

    import config
    from backends import com_backend as com

    def _sync():
        try:
            app = win32com.client.GetActiveObject(config.settings.cad_progid)
        except Exception:
            return None
        com._COM_STATE["app"] = app
        _STATE["start"] = app.ActiveDocument if int(app.Documents.Count) else None
        return str(app.Version)

    return await backend._run(_sync)


async def _open_paths(backend) -> set[str]:
    """Full paths of the open documents that are the smoke's own (COM thread)."""

    def _sync():
        from backends import com_backend as com

        app = com._acad_app()
        paths = (str(app.Documents.Item(i).FullName) for i in range(int(app.Documents.Count)))
        return {_key(p) for p in paths if _ours(p)}

    return await backend._run(_sync)


async def _open(backend, path) -> str:
    """``drawing_open`` of one of the smoke's own files; returns its key."""
    if not _ours(path):
        raise NotOurDocument(f"refused: {Path(path).name} is not in the smoke's own folder")
    result = await backend.drawing_open(str(path))
    key = _key(result.get("path") or path)
    if key not in _OPENED:
        _OPENED.append(key)
    return key


async def _close(backend, key: str) -> None:
    """Close one of the smoke's own documents, unsaved (``Close(False)``)."""
    if not _ours(key):
        raise NotOurDocument("refused: not one of the smoke's own documents")
    if key in await _open_paths(backend):
        await backend.document_close(key, discard=True)
    if key in _OPENED:
        _OPENED.remove(key)


async def _close_leftovers(backend) -> None:
    for key in list(_OPENED):
        try:
            await _close(backend, key)
        except Exception as exc:  # best effort on the way out
            print(f"could not close {Path(key).name}: {type(exc).__name__}", file=sys.stderr)
            if key in _OPENED:
                _OPENED.remove(key)


async def _reactivate_start(backend) -> None:
    """Hand the seat back as it was: the starting document active again."""

    def _sync():
        start = _STATE["start"]
        if start is None:
            return
        try:
            start.Activate()
        except Exception:  # it may have been closed by its owner meanwhile
            pass

    try:
        await backend._run(_sync)
    except Exception as exc:
        print(f"could not re-activate the starting document: {type(exc).__name__}", file=sys.stderr)


def _model(snapshot):
    """The snapshot with its model-space records only (AutoCAD adds a paper-space
    viewport when it initialises a layout; the takeoffs read model space)."""
    return dataclasses.replace(
        snapshot, records=tuple(r for r in snapshot.records if r.space == "Model")
    )


def _types(snapshot) -> dict[str, int]:
    return dict(sorted(Counter(r.type for r in _model(snapshot).records).items()))


def _check_takeoffs(truth, pid, layout) -> dict:
    from engineering.understand.network import build_network
    from engineering.understand.scale import scale_check
    from engineering.understand.takeoff import cable_rows, pipe_rows

    scale = scale_check(pid, layout)
    services = tuple(sorted({run["service"] for run in truth["pipe_runs"]}))
    pipe = pipe_rows(build_network(pid), pid, layout, scale=scale, services=services)
    found = {(r["service"], tuple(sorted(r["tags"]))): r for r in pipe["runs"]}
    pipe_mismatch = []
    for run in truth["pipe_runs"]:
        got = found.get((run["service"], tuple(run["tags"])))
        want = run["layout_length_mm"]
        have = None if got is None else got.get("layout_m")
        agrees = got is not None and got["status"] == run["status"]
        if want is None:
            agrees = agrees and have is None
        else:
            agrees = agrees and have is not None and abs(float(have) * 1000.0 - want) <= 1e-6
        if not agrees:
            pipe_mismatch.append({"run": run["id"], "want": want, "have": have})
    rows = {row["tag"]: row for row in cable_rows(pid, layout)["rows"]}
    cable_mismatch = [
        want["tag"]
        for want in truth["cable_rows"]
        if rows.get(want["tag"], {}).get("cable_m") != want["roundup_m"]
        or rows.get(want["tag"], {}).get("panel") != want["panel"]
    ]
    return {
        "scale_verdict": scale["verdict"],
        "within_10pct": scale.get("within_10pct"),
        "runs_checked": len(truth["pipe_runs"]),
        "pipe_mismatch": pipe_mismatch,
        "loads_checked": len(truth["cable_rows"]),
        "cable_mismatch": cable_mismatch,
    }


async def _smoke(backend, folder: Path) -> dict:
    from engineering.understand.diff import diff_snapshots
    from engineering.understand.snapshot import read_snapshot, take_snapshot
    from tests.fixtures.plant_pair import build_plant_pair

    truth = build_plant_pair(folder)

    pid_key = await _open(backend, truth["pid"])
    async with _Current(pid_key):
        started = time.perf_counter()
        live_pid = await take_snapshot(backend)
        export_s = time.perf_counter() - started
        dwg = folder / "pid.dwg"
        await backend.drawing_save_as(str(dwg), "dwg")
    # SaveAs renamed the document: it is now pid.dwg, and still the smoke's own.
    dwg_key = _key(dwg)
    _OPENED.append(dwg_key)
    for key in (pid_key, dwg_key):
        await _close(backend, key)
    dwg_pid = await take_snapshot(backend, str(dwg))

    layout_key = await _open(backend, truth["layout"])
    async with _Current(layout_key):
        live_layout = await take_snapshot(backend)

    takeoffs = _check_takeoffs(truth, _model(live_pid), _model(live_layout))

    tag = next(
        r
        for r in live_layout.records
        if r.type == "TEXT" and r.layer == "TAGS" and r.space == "Model"
    )
    async with _Current(layout_key):
        await backend.entity_move(tag.handle, 10.0, 0.0)
        after = await take_snapshot(backend)
    diff = diff_snapshots(_model(live_layout), _model(after))
    await _close(backend, layout_key)

    file_pid, file_layout = read_snapshot(truth["pid"]), read_snapshot(truth["layout"])
    return {
        "export_s": round(export_s, 3),
        "live_pid_source": live_pid.source,
        "dwg_pid_source": dwg_pid.source,
        "model_space": {
            "pid_file": _types(file_pid),
            "pid_live": _types(live_pid),
            "pid_dwg": _types(dwg_pid),
            "layout_file": _types(file_layout),
            "layout_live": _types(live_layout),
        },
        "paper_space_layouts": sorted({r.space for r in live_layout.records} - {"Model"}),
        "takeoffs": takeoffs,
        "diff": {
            "moved": tag.handle,
            "added": len(diff["added"]),
            "removed": len(diff["removed"]),
            "changed": len(diff["changed"]),
            "moved_is_the_change": f'"{tag.handle}"' in json.dumps(diff["changed"]),
        },
    }


async def main(keep_document: bool = False) -> int:
    try:
        from backends.com_backend import ComBackend
    except ImportError as exc:
        print(f"pywin32 not available: {exc}")
        return 2
    backend = ComBackend()
    try:
        await backend.connect()
    except Exception as exc:
        print(f"no live AutoCAD reachable: {exc}")
        return 2
    _patient(backend)
    folder = Path(tempfile.mkdtemp(prefix="acadmcp_understand_smoke_"))
    _STATE["folder"] = str(folder)
    try:
        version = await _attach(backend)
        if version is None:
            print("no live AutoCAD reachable: no running instance to attach to")
            return 2
        report = {"autocad": version, **await _smoke(backend, folder)}
        print(json.dumps(report, indent=2, ensure_ascii=False))
        spaces = report["model_space"]
        ok = (
            spaces["pid_live"] == spaces["pid_file"] == spaces["pid_dwg"]
            and spaces["layout_live"] == spaces["layout_file"]
            and report["takeoffs"]["scale_verdict"] == "schematic"
            and not report["takeoffs"]["pipe_mismatch"]
            and not report["takeoffs"]["cable_mismatch"]
            and report["diff"]["changed"] == 1
            and report["diff"]["added"] == report["diff"]["removed"] == 0
            and report["diff"]["moved_is_the_change"]
        )
        return 0 if ok else 1
    finally:
        _STATE["expect"] = None
        if not keep_document:
            await _close_leftovers(backend)
        await _reactivate_start(backend)
        _STATE["start"] = None
        await backend.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(keep_document="--keep" in sys.argv[1:])))
