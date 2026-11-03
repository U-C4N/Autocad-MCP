"""K1 (1.8.0): ``drawing_open`` reads a DWG headlessly through a converter.

The headless engine converts a ``.dwg`` to DXF - the ODA File Converter first,
then AutoCAD's ``accoreconsole.exe`` - and refuses with capability ``dwg_read``
naming both routes when neither is on the machine. These tests drive the
console route with a fake executable (a Python script the module's ``_argv``
is pointed at) for the success, log, refusal and timeout paths, and with the
real ``accoreconsole.exe`` when AutoCAD 2026 is installed: the DWG it converts
is the bundled ``templates/iso_a3_mech.dwt`` copied under a ``.dwg`` name (a
DWT is a DWG container; no ODA here to write a DWG headlessly), compared with
the template's DXF twin.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import ezdxf
import pytest
from fastmcp import Client

import config
import server
from backends import dwg_read
from backends import ezdxf_backend as eb
from backends.base import UnsupportedCapabilityError
from backends.com_backend import ComBackend
from backends.ezdxf_backend import EzdxfBackend
from backends.journal_store import _probe
from engineering import errors

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DWT = ROOT / "templates" / "iso_a3_mech.dwt"
TEMPLATE_DXF = ROOT / "templates" / "iso_a3_mech.dxf"

# Marked per test: half of these are plain checks (pytest-asyncio warns on those).
asyncio_test = pytest.mark.asyncio

FAKE_CONSOLE = r"""
import json, os, re, shutil, subprocess, sys, time

args = sys.argv[1:]
dwg = args[args.index("/i") + 1]
scr = args[args.index("/s") + 1]
with open(scr, "rb") as handle:
    script = handle.read().decode("ascii")
record = os.environ.get("FAKE_ACCORE_RECORD")
if record:
    with open(record, "w", encoding="utf-8") as handle:
        json.dump({"argv": args, "cwd": os.getcwd(), "script": script,
                   "staged": sorted(os.listdir(os.getcwd()))}, handle)
out = re.findall(r'"([^"]+)"', script)[-1]
mode = os.environ.get("FAKE_ACCORE_MODE", "ok")
sys.stdout.buffer.write(("Command: _SAVEAS\r\nfake console " + mode + "\r\n").encode("utf-16-le"))
sys.stdout.flush()
if mode == "ok":
    shutil.copyfile(os.environ["FAKE_ACCORE_DXF"], out)
elif mode == "garbage":
    with open(out, "wb") as handle:
        handle.write(b"this is not a DXF file")
elif mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(os.environ["FAKE_ACCORE_PIDS"], "w", encoding="utf-8") as handle:
        json.dump([os.getpid(), child.pid], handle)
    time.sleep(120)
sys.exit(int(os.environ.get("FAKE_ACCORE_EXIT", "0")))
"""


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def no_odafc(monkeypatch):
    monkeypatch.setattr(eb, "_odafc_available", lambda: False)


@pytest.fixture
def fake_console(tmp_path, monkeypatch, no_odafc):
    """A fake accoreconsole: ACCORECONSOLE_PATH points at it, ``_argv`` runs it."""
    exe = tmp_path / "fake_accoreconsole.py"
    exe.write_text(FAKE_CONSOLE, encoding="utf-8")
    source = tmp_path / "source.dxf"
    doc = ezdxf.new("R2018")
    doc.layers.add("GEOMETRY")
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "GEOMETRY"})
    msp.add_circle((5, 5), 2, dxfattribs={"layer": "GEOMETRY"})
    doc.saveas(source)
    record = tmp_path / "record.json"
    monkeypatch.setenv("ACCORECONSOLE_PATH", str(exe))
    monkeypatch.setenv("FAKE_ACCORE_DXF", str(source))
    monkeypatch.setenv("FAKE_ACCORE_RECORD", str(record))
    monkeypatch.setenv("FAKE_ACCORE_MODE", "ok")
    monkeypatch.setattr(
        dwg_read, "_argv", lambda exe_, dwg, scr: [sys.executable, exe_, "/i", dwg, "/s", scr]
    )
    return {"exe": exe, "record": record, "tmp": tmp_path}


def _dwg(tmp_path: Path, name: str = "part.dwg") -> Path:
    path = tmp_path / name
    path.write_bytes(b"AC1032" + b"\x00" * 128)
    return path


def _staged_left() -> list[str]:
    root, _why = dwg_read.staging_root()
    assert root is not None
    return sorted(child.name for child in root.iterdir())


# ── capability, both maps ────────────────────────────────────────────────────


def test_dwg_read_is_declared_in_both_capability_maps(no_odafc):
    ezdxf_row = EzdxfBackend().capabilities().to_dict()["features"]["dwg_read"]
    com_row = ComBackend().capabilities().to_dict()["features"]["dwg_read"]
    assert com_row["supported"] is True and com_row["mode"] == "native"
    # conftest switches accoreconsole off; no ODA here: no route.
    assert ezdxf_row["supported"] is False
    assert "oda_file_converter" in ezdxf_row["reason"]
    assert "accoreconsole" in ezdxf_row["reason"]


def test_the_ezdxf_row_is_evaluated_per_call(tmp_path, monkeypatch, no_odafc):
    backend = EzdxfBackend()
    exe = tmp_path / "accoreconsole.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setenv("ACCORECONSOLE_PATH", str(exe))
    row = backend.capabilities().features["dwg_read"]
    assert (row.supported, row.mode) == (True, "accoreconsole")
    monkeypatch.setattr(eb, "_odafc_available", lambda: True)
    row = backend.capabilities().features["dwg_read"]
    assert (row.supported, row.mode) == (True, "odafc"), "ODA comes first"
    monkeypatch.setattr(eb, "_odafc_available", lambda: False)
    monkeypatch.setenv("ACCORECONSOLE_PATH", str(tmp_path / "missing.exe"))
    assert backend.capabilities().features["dwg_read"].supported is False


def test_an_unset_path_means_the_installed_console_only_when_it_exists(monkeypatch):
    monkeypatch.delenv("ACCORECONSOLE_PATH", raising=False)
    exe, why = dwg_read.accoreconsole_exe()
    if Path(dwg_read.ACCORE_DEFAULT_PATH).is_file():
        assert exe == dwg_read.ACCORE_DEFAULT_PATH and why == ""
    else:
        assert exe is None and "ACCORECONSOLE_PATH" in why
    monkeypatch.setenv("ACCORECONSOLE_PATH", "off")
    assert dwg_read.accoreconsole_exe() == (None, "disabled by ACCORECONSOLE_PATH=off")


# ── the refusal ──────────────────────────────────────────────────────────────


@asyncio_test
async def test_no_converter_is_a_dwg_read_refusal_naming_both_routes(backend, tmp_path, no_odafc):
    before = (await backend.drawing_info()).name
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await backend.drawing_open(str(_dwg(tmp_path)))
    assert excinfo.value.capability == "dwg_read"
    message = str(excinfo.value)
    assert "ODA File Converter" in message and "ACCORECONSOLE_PATH" in message
    assert "Nothing was opened" in message
    assert (await backend.drawing_info()).name == before, "the open document is kept"


@asyncio_test
async def test_the_refusal_reaches_the_wire_typed(tmp_path, monkeypatch, no_odafc):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "drawing_open", {"path": str(_dwg(tmp_path))}, raise_on_error=False
        )
    assert result.is_error is True
    payload = result.structured_content
    assert payload["kind"] == "unsupported" and payload["capability"] == "dwg_read"
    assert payload["retryable"] is False


@asyncio_test
async def test_recover_on_a_dwg_is_still_recover_unsupported(backend, tmp_path, fake_console):
    from backends.dxf_recover import RecoverUnsupportedError

    with pytest.raises(RecoverUnsupportedError):
        await backend.drawing_open(str(_dwg(tmp_path)), recover=True)
    assert not fake_console["record"].exists(), "recover never reaches the converter"


@asyncio_test
async def test_a_missing_dwg_is_file_not_found(backend, tmp_path, fake_console):
    with pytest.raises(FileNotFoundError):
        await backend.drawing_open(str(tmp_path / "absent.dwg"))
    assert errors.classify(FileNotFoundError("x"))["kind"] == "file_not_found"


# ── the script, the staging folder, the log ──────────────────────────────────


def test_the_script_is_only_our_prefixed_commands():
    script = dwg_read.build_script(r"C:\state\accore\0123456789ab\out.dxf")
    lines = script.split("\r\n")
    assert lines[-1] == "", "the script ends with a newline"
    assert lines[:-1] == [
        "_SAVEAS",
        "_DXF",
        "_V",
        "_2018",
        "16",
        r'"C:\state\accore\0123456789ab\out.dxf"',
    ]
    assert script.isascii()
    for bad in ("C:\\state\\çizim\\out.dxf", 'C:\\a"b\\out.dxf', "C:\\a\r\n_ERASE\\out.dxf"):
        with pytest.raises(ValueError):
            dwg_read.build_script(bad)


def test_the_log_is_decoded_as_utf16le_with_or_without_a_bom():
    text = "Command: _SAVEAS\r\nÇizim kaydedildi\r\n\r\n"
    assert dwg_read.decode_log(text.encode("utf-16-le")) == ["Command: _SAVEAS", "Çizim kaydedildi"]
    assert dwg_read.decode_log(b"\xff\xfe" + text.encode("utf-16-le"))[1] == "Çizim kaydedildi"
    assert dwg_read.decode_log("plain\nutf8 ç\n".encode()) == ["plain", "utf8 ç"]
    assert dwg_read.decode_log(b"") == []


def test_a_non_ascii_state_dir_without_a_short_name_disables_the_route(state_root, monkeypatch):
    folder = state_root / "çizimler-durumu"
    monkeypatch.setenv("ACADMCP_STATE_DIR", str(folder))
    monkeypatch.setattr(dwg_read, "_short_path", lambda _path: None)
    root, why = dwg_read.staging_root()
    assert root is None and "ASCII" in why


def test_the_timeout_stays_inside_the_call_deadline(monkeypatch):
    monkeypatch.setenv("ACCORECONSOLE_TIMEOUT_S", "60")
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 20.0)
    assert dwg_read.effective_timeout() == 15.0
    monkeypatch.setattr(config.settings, "ezdxf_call_timeout", 0.0)
    assert dwg_read.effective_timeout() == 60.0
    monkeypatch.setenv("ACCORECONSOLE_TIMEOUT_S", "-1")
    with pytest.raises(ValueError, match="ACCORECONSOLE_TIMEOUT_S"):
        dwg_read.effective_timeout()


# ── the console route on a fake executable ───────────────────────────────────


@asyncio_test
async def test_the_console_converts_and_the_log_is_reported(backend, tmp_path, fake_console):
    dwg = _dwg(tmp_path)
    result = await backend.drawing_open(str(dwg))

    report = result["dwg_read"]
    assert report["route"] == "accoreconsole"
    assert report["exit_code"] == 0 and report["timed_out"] is False
    assert "fake console ok" in report["log"]
    assert report["log_lines"] == 2
    assert report["dxf_version"] == "AC1032"
    assert result["name"] == "part.dwg" and result["path"] == str(dwg)
    types = sorted(e.type for e in await backend.entity_list(limit=10))
    assert types == ["CIRCLE", "LINE"]

    seen = json.loads(fake_console["record"].read_text(encoding="utf-8"))
    staged = Path(seen["cwd"])
    assert seen["cwd"].isascii()
    assert staged.parent == dwg_read.staging_root()[0], "under <state dir>/accore"
    assert seen["staged"] == ["accore.log", "convert.scr", "in.dwg"], "the drawing is copied in"
    assert str(dwg) not in json.dumps(seen["argv"]) and str(dwg) not in seen["script"]
    assert seen["script"] == dwg_read.build_script(str(staged / "out.dxf"))
    assert not staged.exists() and _staged_left() == [], "the staging folder is removed"


@asyncio_test
async def test_the_result_saves_as_dxf_but_not_onto_the_dwg(backend, tmp_path, fake_console):
    await backend.drawing_open(str(_dwg(tmp_path)))
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await backend.drawing_save()
    assert excinfo.value.capability == "dwg"
    saved = await backend.drawing_save_as(str(tmp_path / "part.dxf"), "dxf")
    assert saved["ok"] is True


@pytest.mark.parametrize(
    ("mode", "reason"),
    [("nothing", "wrote no DXF"), ("garbage", "did not read back")],
)
@asyncio_test
async def test_a_failed_conversion_is_typed_with_its_log(
    backend, tmp_path, fake_console, monkeypatch, mode, reason
):
    monkeypatch.setenv("FAKE_ACCORE_MODE", mode)
    monkeypatch.setenv("FAKE_ACCORE_EXIT", "3")
    before = (await backend.drawing_info()).name
    with pytest.raises(dwg_read.DwgConversionError) as excinfo:
        await backend.drawing_open(str(_dwg(tmp_path)))
    (attempt,) = excinfo.value.attempts
    assert attempt["route"] == "accoreconsole" and reason in attempt["reason"]
    assert attempt["exit_code"] == 3
    assert f"fake console {mode}" in attempt["log"]
    row = errors.classify(excinfo.value)
    assert row["kind"] == "dwg_conversion_failed" and row["attempts"][0]["exit_code"] == 3
    assert (await backend.drawing_info()).name == before
    assert _staged_left() == []


@asyncio_test
async def test_a_failed_conversion_reaches_the_wire_with_its_attempts(
    tmp_path, fake_console, monkeypatch
):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    monkeypatch.setenv("FAKE_ACCORE_MODE", "nothing")
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "drawing_open", {"path": str(_dwg(tmp_path))}, raise_on_error=False
        )
    payload = result.structured_content
    assert result.is_error is True
    assert payload["kind"] == "dwg_conversion_failed"
    assert payload["category"] == "dependency" and payload["retryable"] is False
    assert payload["attempts"][0]["route"] == "accoreconsole"
    assert server._STATE_OF_KIND["dwg_conversion_failed"] == "refused"


@asyncio_test
async def test_an_overrun_kills_the_whole_process_tree(
    backend, tmp_path, fake_console, monkeypatch
):
    pids = tmp_path / "pids.json"
    monkeypatch.setenv("FAKE_ACCORE_MODE", "hang")
    monkeypatch.setenv("FAKE_ACCORE_PIDS", str(pids))
    monkeypatch.setenv("ACCORECONSOLE_TIMEOUT_S", "4")
    started = time.monotonic()
    with pytest.raises(dwg_read.DwgConversionError) as excinfo:
        await backend.drawing_open(str(_dwg(tmp_path)))
    assert time.monotonic() - started < 60
    (attempt,) = excinfo.value.attempts
    assert attempt["timed_out"] is True and "timed_out after 4 s" in attempt["reason"]
    assert "fake console hang" in attempt["log"]
    console, child = json.loads(pids.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and "alive" in (_probe(console)[0], _probe(child)[0]):
        time.sleep(0.2)
    assert _probe(console)[0] == "gone", "the console was killed"
    assert _probe(child)[0] == "gone", "and the process it started (taskkill /T)"
    assert _staged_left() == []


@asyncio_test
async def test_a_console_that_cannot_start_is_a_typed_failure(
    backend, tmp_path, no_odafc, monkeypatch
):
    exe = tmp_path / "accoreconsole.exe"
    exe.write_bytes(b"MZ not really a program")
    monkeypatch.setenv("ACCORECONSOLE_PATH", str(exe))
    with pytest.raises(dwg_read.DwgConversionError) as excinfo:
        await backend.drawing_open(str(_dwg(tmp_path)))
    (attempt,) = excinfo.value.attempts
    assert "could not be started" in attempt["reason"]
    assert _staged_left() == []


# ── the ODA route comes first ────────────────────────────────────────────────


@asyncio_test
async def test_odafc_is_tried_first(backend, tmp_path, fake_console, monkeypatch):
    oda_doc = ezdxf.new("R2013")
    oda_doc.modelspace().add_point((1, 2))
    monkeypatch.setattr(eb, "_odafc_available", lambda: True)
    monkeypatch.setattr(dwg_read, "_read_with_odafc", lambda _path: oda_doc)
    result = await backend.drawing_open(str(_dwg(tmp_path)))
    assert result["dwg_read"]["route"] == "odafc"
    assert result["dwg_read"]["dxf_version"] == "AC1027"
    assert not fake_console["record"].exists(), "the console never ran"


@asyncio_test
async def test_a_failed_odafc_falls_through_to_the_console(
    backend, tmp_path, fake_console, monkeypatch
):
    def broken(_path):
        raise RuntimeError("ODA File Converter crashed")

    monkeypatch.setattr(eb, "_odafc_available", lambda: True)
    monkeypatch.setattr(dwg_read, "_read_with_odafc", broken)
    result = await backend.drawing_open(str(_dwg(tmp_path)))
    report = result["dwg_read"]
    assert report["route"] == "accoreconsole"
    assert report["failed_routes"][0]["route"] == "odafc"
    assert "crashed" in report["failed_routes"][0]["reason"]


# ── the real accoreconsole, when AutoCAD 2026 is installed ───────────────────


def _counts(doc) -> dict:
    return {
        "layers": sorted(layer.dxf.name for layer in doc.layers),
        "blocks": sorted(block.name for block in doc.blocks),
        "entities": {layout.name: len(layout) for layout in doc.layouts},
    }


@pytest.mark.skipif(
    not Path(dwg_read.ACCORE_DEFAULT_PATH).is_file(),
    reason="AutoCAD 2026's accoreconsole.exe is not installed",
)
@asyncio_test
async def test_the_real_console_converts_a_dwg(backend, tmp_path, monkeypatch, no_odafc):
    monkeypatch.setenv("ACCORECONSOLE_PATH", dwg_read.ACCORE_DEFAULT_PATH)
    dwg = tmp_path / "iso_a3_mech.dwg"
    shutil.copyfile(TEMPLATE_DWT, dwg)
    result = await backend.drawing_open(str(dwg))
    report = result["dwg_read"]
    assert report["route"] == "accoreconsole" and report["timed_out"] is False
    assert any("_SAVEAS" in line for line in report["log"])
    assert report["dxf_version"] == "AC1032"
    converted = _counts(backend._doc)
    assert converted == _counts(ezdxf.readfile(TEMPLATE_DXF))
    assert _staged_left() == []


# ── the v9 matrix task ───────────────────────────────────────────────────────


def test_the_v9_task_is_registered_headless_only():
    from benchmarks.adapters.autocad_mcp_pro import HEADLESS_ONLY, AutoCADMCPProAdapter
    from benchmarks.tasks_v9 import DEFAULT_MATRIX, NEW_TASKS_V9, task_by_id

    assert task_by_id("dwg_headless_roundtrip", "v9").category == "reliability"
    assert NEW_TASKS_V9[-1].task_id == "dwg_headless_roundtrip"
    assert "dwg_headless_roundtrip" in HEADLESS_ONLY
    assert hasattr(AutoCADMCPProAdapter, "_task_dwg_headless_roundtrip")
    assert DEFAULT_MATRIX == "v8"


async def _run_v9_task(tmp_path):
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter
    from benchmarks.tasks_v9 import task_by_id

    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        return await adapter.run_task(task_by_id("dwg_headless_roundtrip", "v9"))
    finally:
        await adapter.cleanup()


@asyncio_test
async def test_the_v9_task_without_a_converter_is_environment_blocked(tmp_path, no_odafc):
    result = await _run_v9_task(tmp_path)
    assert result.status == "environment_blocked", result
    assert result.metrics["twin_ok"] is True
    assert result.metrics["twin"] == {"kind": "unsupported", "capability": "dwg_read"}


@pytest.mark.skipif(
    not Path(dwg_read.ACCORE_DEFAULT_PATH).is_file(),
    reason="AutoCAD 2026's accoreconsole.exe is not installed",
)
@asyncio_test
async def test_the_v9_task_passes_through_the_real_console(tmp_path, monkeypatch, no_odafc):
    monkeypatch.setenv("ACCORECONSOLE_PATH", dwg_read.ACCORE_DEFAULT_PATH)
    result = await _run_v9_task(tmp_path)
    assert result.status == "pass", result
    assert result.metrics["dwg_read"]["route"] == "accoreconsole"
    assert all(result.metrics["equal"].values())


# ── the live smoke script ────────────────────────────────────────────────────


@asyncio_test
async def test_the_smoke_is_unavailable_without_a_console():
    import io

    from scripts import smoke_accore

    out = io.StringIO()
    assert await smoke_accore.run(stream=out) == 2  # conftest: ACCORECONSOLE_PATH=off
    result = json.loads(out.getvalue())
    assert result["smoke"] == "accore" and result["status"] == "unavailable"
    assert "ACCORECONSOLE_PATH=off" in result["reason"]
