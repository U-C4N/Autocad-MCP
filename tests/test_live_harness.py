"""The live COM release harness (spec §6 Task 21, B63), against a fake seat.

No AutoCAD is touched. ``ComBackend`` is the real one, its ``_run`` on a plain
daemon thread (no CoInitialize); the seat models only members measured on
AutoCAD 2026 (25.1s) on 2026-10-25 in a scratch ``Documents.Add()`` document:
``Application.Name`` ("AutoCAD"), ``Application.Version`` ("25.1s (...)"),
``Documents.Count / Item / Add / Open``, ``ActiveDocument``, ``Document.Name``,
``FullName`` ("" until saved), ``Saved``, ``Activate``, ``Close(SaveChanges)``,
``GetVariable`` / ``SetVariable`` on the DOCUMENT (``_VERNUM`` read
"W.60.0.0 (UNICODE)"), ``ModelSpace.Count``. The application is reached with
``GetActiveObject`` only: the fake's ``Dispatch`` records the call and raises,
because the real one launches AutoCAD.
"""

from __future__ import annotations

import importlib.util
import io
import json
import platform
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backends import com_backend as cb
from backends.quarantine import DaemonExecutor
from scripts.live import harness
from scripts.live.harness import LiveHarness, run_smoke

ROOT = Path(__file__).resolve().parents[1]

RPC_E_CALL_REJECTED = -2147418111
MK_E_UNAVAILABLE = -2147221021


class com_error(Exception):  # noqa: N801 - the name pywintypes gives it; hresult in args[0]
    @property
    def hresult(self):
        return self.args[0]


class _Space:
    def __init__(self):
        self.items: list = []

    @property
    def Count(self):  # noqa: N802 - the ActiveX member name
        return len(self.items)


class _Doc:
    def __init__(self, seat, name, full_name=""):
        self._seat = seat
        self.Name = name
        self.FullName = full_name
        self.Saved = True
        self.ModelSpace = _Space()
        self.vars = {
            "LTSCALE": 1.0,
            "_VERNUM": "W.60.0.0 (UNICODE)",
            "ACADVER": "25.1s (test seat)",
        }
        self.writes: list[tuple[str, object]] = []
        self.closes: list[bool] = []
        self.activations = 0

    def GetVariable(self, name):  # noqa: N802
        if self._seat.reject_next > 0:
            self._seat.reject_next -= 1
            raise com_error(RPC_E_CALL_REJECTED, "Call was rejected by callee.", None, None)
        if self._seat.fail_next:
            self._seat.fail_next = False
            raise com_error(-2147352567, "Exception occurred.", None, None)
        return self.vars.get(name, 0)

    def SetVariable(self, name, value):  # noqa: N802
        self.writes.append((name, value))
        self.vars[name] = value
        self.Saved = False

    def Activate(self):  # noqa: N802
        self.activations += 1
        self._seat.ActiveDocument = self

    def Close(self, save_changes):  # noqa: N802
        self.closes.append(save_changes)
        docs = self._seat.Documents._docs
        docs.remove(self)
        # AutoCAD activates whichever document it likes after a Close; the
        # operator's (the first) is the case that matters.
        self._seat.ActiveDocument = docs[0] if docs else None


class _Docs:
    def __init__(self, seat):
        self._seat = seat
        self._docs: list[_Doc] = []
        self._next = 30

    @property
    def Count(self):  # noqa: N802
        return len(self._docs)

    def Item(self, index):  # noqa: N802
        return self._docs[index]

    def Add(self, template=None):  # noqa: N802
        self._next += 1
        doc = _Doc(self._seat, f"Drawing{self._next}.dwg")
        self._docs.append(doc)
        self._seat.ActiveDocument = doc
        self._seat.added.append(doc)
        return doc


class _Seat:
    Name = "AutoCAD"
    Version = "25.1s (test seat)"
    FullName = r"C:\Program Files\Autodesk\AutoCAD 2026\acad.exe"

    def __init__(self):
        self.Documents = _Docs(self)
        self.added: list[_Doc] = []
        self.reject_next = 0
        self.fail_next = False
        # The operator's drawing: never activated, written, saved or closed.
        self.operator = _Doc(self, "Drawing2.dwg")
        self.Documents._docs.append(self.operator)
        self.ActiveDocument = self.operator


class _Client:
    """``win32com.client`` as far as the harness and the backend may use it."""

    def __init__(self, seat):
        self.seat = seat
        self.running = True
        self.attached = 0
        self.dispatched: list[str] = []

    def GetActiveObject(self, progid):  # noqa: N802
        if not self.running:
            raise com_error(MK_E_UNAVAILABLE, "Operation unavailable", None, None)
        self.attached += 1
        return self.seat

    def Dispatch(self, progid):  # noqa: N802
        self.dispatched.append(progid)
        raise AssertionError("Dispatch launches AutoCAD; the harness must never reach it")

    DispatchWithEvents = Dispatch


@pytest.fixture
def seat(monkeypatch):
    fake = _Seat()
    client = _Client(fake)
    monkeypatch.setattr(cb, "_COM_STATE", {})
    monkeypatch.setattr(cb, "win32com", SimpleNamespace(client=client), raising=False)
    return fake, client


def _backend():
    backend = cb.ComBackend()
    backend._executor = DaemonExecutor(max_workers=1, name="test-sta")  # no CoInitialize
    backend._connected = True
    return backend


async def _no_sleep(_seconds):
    return None


async def _run(body, client, tmp_path, **kwargs):
    out = tmp_path / "result.json"
    stream = io.StringIO()
    code = await run_smoke(
        "fake",
        body,
        backend=_backend(),
        client=client,
        json_path=str(out),
        stream=stream,
        sleep=_no_sleep,
        **kwargs,
    )
    result = json.loads(out.read_text(encoding="utf-8"))
    assert json.loads(stream.getvalue()) == result, "stdout and the file carry one result"
    assert result["exit_code"] == code
    return code, result


# ── attach only ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_running_seat_is_unavailable_and_never_dispatched(seat, tmp_path):
    fake, client = seat
    client.running = False
    ran: list[str] = []

    async def body(h):
        ran.append("body")

    code, result = await _run(body, client, tmp_path)
    assert code == harness.EXIT_UNAVAILABLE == 2
    assert result["status"] == "unavailable"
    assert "attach" in result["reason"]
    assert ran == [], "nothing runs without a seat"
    assert client.dispatched == [], "the harness never launches AutoCAD"
    assert fake.added == []


@pytest.mark.asyncio
async def test_a_dropped_application_is_attached_again_never_dispatched(seat, tmp_path):
    """After a deadline overrun the backend drops its cached application and its
    own ``_acad_app`` would fall back to ``Dispatch``; the harness re-attaches."""
    fake, client = seat

    async def body(h):
        await h.new_document()
        cb._COM_STATE.pop("app", None)  # what ComBackend._run does on a timeout
        value = await h.backend.system_get_variable("LTSCALE")
        h.check("LTSCALE read after the drop", value == 1.0, expected=1.0, observed=value)

    code, result = await _run(body, client, tmp_path)
    assert code == 0, result
    assert client.dispatched == []
    assert client.attached == 2


def test_the_harness_source_never_names_dispatch_as_a_call():
    source = (ROOT / "scripts" / "live" / "harness.py").read_text(encoding="utf-8")
    assert "GetActiveObject(" in source
    assert not re.search(r"\bDispatch(WithEvents)?\(", source)
    assert "WithEvents" not in source.replace("``WithEvents``", "")


# ── its own documents, closed on every path ──────────────────────────────────


@pytest.mark.asyncio
async def test_every_document_it_opened_is_closed_even_when_a_check_raises(seat, tmp_path):
    fake, client = seat

    async def body(h):
        h.stage("drawing")
        await h.new_document()
        await h.new_document()
        h.check("two documents", True)
        raise ValueError("boom in the middle of a check")

    code, result = await _run(body, client, tmp_path)
    assert code == harness.EXIT_FAIL == 1
    assert result["status"] == "fail"
    assert len(fake.added) == 2
    assert all(doc.closes == [False] for doc in fake.added), "Close(False), once each"
    assert fake.Documents._docs == [fake.operator]
    assert fake.operator.closes == [] and fake.operator.activations == 0
    assert fake.operator.writes == []
    errors = [row for row in result["checks"] if row["status"] == "error"]
    assert len(errors) == 1 and "boom in the middle of a check" in errors[0]["error"]
    assert errors[0]["stage"] == "drawing"
    assert result["documents"] == {
        "opened": 2,
        "closed": 2,
        "left_open": [],
        "kept": False,
    }


@pytest.mark.asyncio
async def test_keep_leaves_its_documents_open_and_says_so(seat, tmp_path):
    fake, client = seat

    async def body(h):
        name = await h.new_document()
        h.check("made", bool(name))

    code, result = await _run(body, client, tmp_path, keep=True)
    assert code == 0
    assert fake.added[0].closes == []
    assert result["documents"]["kept"] is True
    assert result["documents"]["left_open"] == [fake.added[0].Name]


@pytest.mark.asyncio
async def test_closing_one_of_its_documents_binds_another_of_its_own(seat, tmp_path):
    """AutoCAD activates the operator's drawing after a Close; the harness moves
    the session back onto one of its own before the next call."""
    fake, client = seat

    async def body(h):
        first = await h.new_document()
        second = await h.new_document()
        await h.close_document(second)
        await h.backend.system_set_variable("LTSCALE", 4.0)
        h.check("the write landed in the first", first == fake.added[0].Name)

    code, result = await _run(body, client, tmp_path)
    assert code == 0, result
    assert fake.added[0].writes == [("LTSCALE", 4.0)]
    assert fake.operator.writes == [] and fake.operator.activations == 0


# ── the active-document guard ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_it_refuses_when_the_active_document_changes_under_it(seat, tmp_path):
    fake, client = seat

    async def body(h):
        await h.new_document()
        await h.backend.system_set_variable("LTSCALE", 2.0)
        fake.ActiveDocument = fake.operator  # the operator clicks their own tab
        await h.backend.system_set_variable("LTSCALE", 3.0)
        h.check("never reached", True)

    code, result = await _run(body, client, tmp_path)
    assert code == harness.EXIT_UNAVAILABLE == 2
    assert result["status"] == "refused"
    assert "Drawing2" not in json.dumps(result), "the operator's drawing is never named"
    assert fake.operator.writes == [], "nothing lands in the operator's drawing"
    assert fake.added[0].writes == [("LTSCALE", 2.0)]
    assert fake.added[0].closes == [False], "its own document is still closed"
    assert fake.operator.closes == [] and fake.operator.activations == 0
    assert all(row["name"] != "never reached" for row in result["checks"])


@pytest.mark.asyncio
async def test_after_its_last_document_closes_nothing_reaches_the_operators(seat, tmp_path):
    fake, client = seat

    async def body(h):
        name = await h.new_document()
        await h.close_document(name)
        await h.backend.system_set_variable("LTSCALE", 9.0)

    code, result = await _run(body, client, tmp_path)
    assert code == 2 and result["status"] == "refused"
    assert fake.operator.writes == []


# ── one shared RPC_E_CALL_REJECTED retry ─────────────────────────────────────


@pytest.mark.asyncio
async def test_a_rejected_call_is_retried_and_counted(seat, tmp_path):
    fake, client = seat

    async def body(h):
        await h.new_document()
        fake.reject_next = 2
        value = await h.backend.system_get_variable("LTSCALE")
        h.check("read through two rejections", value == 1.0)

    code, result = await _run(body, client, tmp_path)
    assert code == 0, result
    assert result["summary"]["rpc_retries"] == 2


@pytest.mark.asyncio
async def test_another_com_error_is_not_retried(seat, tmp_path):
    fake, client = seat

    async def body(h):
        await h.new_document()
        fake.fail_next = True
        await h.backend.system_get_variable("LTSCALE")

    code, result = await _run(body, client, tmp_path)
    assert code == 1
    assert result["summary"]["rpc_retries"] == 0
    assert any(row["status"] == "error" for row in result["checks"])


@pytest.mark.asyncio
async def test_a_seat_that_keeps_rejecting_gives_up_after_its_patience(seat, tmp_path):
    fake, client = seat

    async def body(h):
        await h.new_document()
        fake.reject_next = 10**6
        await h.backend.system_get_variable("LTSCALE")

    code, result = await _run(body, client, tmp_path, patience_s=0.0)
    assert code == 1
    assert fake.added[0].closes == [False]


# ── the JSON ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_json_names_the_build_os_python_pywin32_and_server_commit(seat, tmp_path):
    fake, client = seat

    async def body(h):
        await h.new_document()
        h.check("one", True, expected=1, observed=1)
        return {"answer": 42}

    code, result = await _run(body, client, tmp_path)
    assert code == 0
    assert result["schema"] == harness.SCHEMA
    assert result["smoke"] == "fake" and result["status"] == "pass"
    assert result["report"] == {"answer": 42}
    env = result["environment"]
    assert env["autocad"] == {
        "name": "AutoCAD",
        "version": "25.1s",  # the licence text in parentheses is not recorded
        "build": "W.60.0.0 (UNICODE)",
        "progid": "AutoCAD.Application",
    }
    assert env["python"]["version"] == platform.python_version()
    assert env["os"]["platform"] == platform.platform()
    assert "pywin32" in env
    head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert env["server"]["sha"] == head
    assert isinstance(env["server"]["dirty"], bool)
    assert env["server"]["version"]
    assert result["checks"] == [
        {"name": "one", "status": "pass", "expected": 1, "observed": 1, "stage": None}
    ]
    assert result["summary"] == {
        "checks": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "rpc_retries": 0,
    }


def test_the_home_folder_never_reaches_a_committed_result():
    value = {
        r"C:\Users\Someone\AppData\x.dwg": [r"c:\users\someone\Temp\a.pdf", "C:/Users/Someone/b"],
        "kept": r"C:\Program Files\Autodesk\AutoCAD 2026\acad.exe",
        "n": 3,
    }
    assert harness.scrub(value, home=r"C:\Users\Someone") == {
        r"~\AppData\x.dwg": [r"~\Temp\a.pdf", "~/b"],
        "kept": r"C:\Program Files\Autodesk\AutoCAD 2026\acad.exe",
        "n": 3,
    }
    assert harness.scrub({"p": "/home/someone/tmp/a"}, home="/home/someone") == {"p": "~/tmp/a"}


# ── exit codes ───────────────────────────────────────────────────────────────


async def _all_pass(h):
    await h.new_document()
    h.check("a", True)
    h.check("b", True)


async def _one_fails(h):
    await h.new_document()
    h.check("a", True)
    h.check("b", False, expected=1, observed=2)


async def _asserts_nothing(h):
    await h.new_document()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code", "status"),
    [(_all_pass, 0, "pass"), (_one_fails, 1, "fail"), (_asserts_nothing, 1, "fail")],
)
async def test_exit_codes(seat, tmp_path, body, code, status):
    fake, client = seat
    got, result = await _run(body, client, tmp_path)
    assert (got, result["status"]) == (code, status)


# ── the smokes run on it ─────────────────────────────────────────────────────

SMOKES = (
    "smoke_pid_com.py",
    "smoke_arch_com.py",
    "smoke_mech_com.py",
    "smoke_settings_com.py",
    "smoke_understand_com.py",
    "smoke_verify_com.py",
    "smoke_document_com.py",
)


def _module(name):
    spec = importlib.util.spec_from_file_location(name[:-3], ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", SMOKES)
def test_every_smoke_runs_on_the_harness(name):
    source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
    assert "from scripts.live.harness import" in source
    assert "run_smoke" in source or "main_cli" in source
    for banned in ("Dispatch(", "WithEvents", "SendCommand", "def _patient", "_OPENED"):
        assert banned not in source, f"{name}: {banned}"
    module = _module(name)
    assert callable(module.body) and callable(module.assess)


def _pid_report(**overrides):
    report = {
        "graph_stats": {
            "nodes_by_kind": {"equipment": 2, "instrument": 1, "offpage": 1, "valve": 1},
            "edges_by_class": {"electric": 1, "process_major": 3},
            "dangling": 0,
            "unclassified": 0,
            "confidence_min": 1.0,
        },
        "reference_stats": None,
        "handles": {"P-101": "1A", "V-201": "1B", "FCV-101": "1C", "FIC-101": "1D", "OP-1": "1E"},
        "critique": [],
        "xdata_chunks": 3,
    }
    report["reference_stats"] = dict(report["graph_stats"])
    for key, value in overrides.items():
        if key in report["graph_stats"]:
            report["graph_stats"] = {**report["graph_stats"], key: value}
        else:
            report[key] = value
    return report


@pytest.mark.parametrize(
    "overrides",
    [
        {"critique": [{"code": "pid_duplicate_tag"}]},
        {"confidence_min": 0.6},
        {"xdata_chunks": 0},
        {"unclassified": 1},
        {"handles": {"P-101": "1A"}},
        {"edges_by_class": {"process_major": 3}},
    ],
)
def test_the_pid_smoke_fails_on_more_than_a_dangling_end(overrides):
    """Backlog W4-T21-07: the exit code checked ``dangling`` alone."""
    pid = _module("smoke_pid_com.py")
    clean = LiveHarness("pid")
    pid.assess(clean, _pid_report())
    assert clean.checks and all(row["status"] == "pass" for row in clean.checks)
    broken = LiveHarness("pid")
    pid.assess(broken, _pid_report(**overrides))
    report = _pid_report(**overrides)
    assert report["graph_stats"]["dangling"] == 0
    assert any(row["status"] == "fail" for row in broken.checks), overrides
    assert broken.exit_code() == 1
