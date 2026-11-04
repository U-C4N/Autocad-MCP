"""1.8-25 - ``scripts/live/run_all.py`` writes the release's live matrix.

Three fake smokes stand in for the real ones (no AutoCAD is touched): one that
passes on an AutoCAD 2026 seat, one that raises after a COM disconnection, and
one with no seat to attach to. The matrix gets one row each - ``pass`` /
``fail`` / ``environment_blocked`` - with timestamps, the seat and the server
build, and the disconnection is logged with the time it was read.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

import pytest

from scripts.live import run_all

ROOT = Path(__file__).resolve().parents[1]

_RESULT = textwrap.dedent(
    """
    import json, sys
    path = sys.argv[sys.argv.index("--json") + 1]
    def write(status, exit_code, autocad, reason=None, checks=()):
        result = {
            "schema": "acadmcp-live-harness/1", "smoke": NAME, "status": status,
            "exit_code": exit_code, "reason": reason,
            "environment": {"autocad": autocad, "server": {"version": "9.9.9"}},
            "summary": {"checks": len(checks), "passed": len(checks), "failed": 0,
                        "errors": 0, "rpc_retries": 0},
            "checks": list(checks),
        }
        open(path, "w", encoding="utf-8").write(json.dumps(result))
        print(json.dumps(result))
        sys.exit(exit_code)
    """
)

_SMOKES = {
    "smoke_good_com.py": 'NAME = "good"\n'
    + _RESULT
    + 'write("pass", 0, {"name": "AutoCAD", "version": "25.1s", "build": "W.60"},'
    ' checks=[{"name": "drew", "status": "pass"}])\n',
    "smoke_boom_com.py": 'NAME = "boom"\n'
    + _RESULT
    + textwrap.dedent(
        """
        sys.stderr.write("com_error: (-2147417848, 'The object invoked has disconnected "
                         "from its clients.', None, None)\\n")
        sys.stderr.flush()
        raise RuntimeError("the seat went away")
        """
    ),
    "smoke_absent_com.py": 'NAME = "absent"\n'
    + _RESULT
    + 'write("unavailable", 2, None, reason="could not attach to a running AutoCAD")\n',
}


@pytest.fixture
def smokes(tmp_path) -> list[Path]:
    folder = tmp_path / "scripts"
    folder.mkdir()
    paths = []
    for name, body in _SMOKES.items():
        path = folder / name
        path.write_text(body, encoding="utf-8")
        paths.append(path)
    return paths


def test_the_matrix_has_one_row_per_smoke_and_logs_the_disconnect(smokes, tmp_path):
    out = tmp_path / "live"
    matrix = run_all.run_all(smokes, out=out, release_name="9.9.9", timeout=120)

    folder = out / "9.9.9"
    on_disk = json.loads((folder / "matrix.json").read_text(encoding="utf-8"))
    assert on_disk == matrix
    assert matrix["schema"] == run_all.SCHEMA and matrix["release"] == "9.9.9"
    rows = {row["smoke"]: row for row in matrix["rows"]}
    assert [row["smoke"] for row in matrix["rows"]] == ["good", "boom", "absent"]
    assert {name: row["status"] for name, row in rows.items()} == {
        "good": "pass",
        "boom": "fail",
        "absent": "environment_blocked",
    }
    assert matrix["summary"] == {"pass": 1, "fail": 1, "environment_blocked": 1}

    good = rows["good"]
    assert good["seat"]["version"] == "25.1s" and good["seat"]["year"] == 2026
    assert good["result"] == "good.json" and (folder / "good.json").is_file()
    assert good["summary"]["checks"] == 1 and good["exit_code"] == 0
    assert matrix["seat"] == good["seat"]
    for row in matrix["rows"]:
        started = datetime.fromisoformat(row["started_at"])
        ended = datetime.fromisoformat(row["ended_at"])
        assert started <= ended and row["duration_s"] >= 0

    boom = rows["boom"]
    assert boom["exit_code"] == 1 and boom["result"] is None and boom["seat"] is None
    assert "the seat went away" in boom["reason"]
    absent = rows["absent"]
    assert absent["harness_status"] == "unavailable" and absent["exit_code"] == 2
    assert "could not attach" in absent["reason"]

    (event,) = matrix["disconnects"]
    assert event["smoke"] == "boom" and event["stream"] == "stderr"
    assert (event["hresult"], event["name"]) == (-2147417848, "RPC_E_DISCONNECTED")
    datetime.fromisoformat(event["at"])

    server = matrix["server"]
    assert set(server) == {"version", "sha", "dirty"}
    assert run_all.exit_code(matrix) == run_all.EXIT_FAIL


def test_blocked_is_not_a_failure_unless_a_seat_is_required(smokes, tmp_path):
    good, _boom, absent = smokes
    matrix = run_all.run_all([good, absent], out=tmp_path / "live", release_name="9.9.9")
    assert run_all.exit_code(matrix) == run_all.EXIT_OK
    assert run_all.exit_code(matrix, require_seat=True) == run_all.EXIT_BLOCKED


def test_a_previous_result_is_never_read_as_this_runs(smokes, tmp_path):
    _good, boom, _absent = smokes
    folder = tmp_path / "live" / "9.9.9"
    folder.mkdir(parents=True)
    (folder / "boom.json").write_text(json.dumps({"status": "pass"}), encoding="utf-8")
    matrix = run_all.run_all([boom], out=tmp_path / "live", release_name="9.9.9")
    assert matrix["rows"][0]["status"] == "fail"


def test_a_smoke_that_overruns_is_killed_and_fails(tmp_path):
    slow = tmp_path / "smoke_slow_com.py"
    slow.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    matrix = run_all.run_all([slow], out=tmp_path / "live", release_name="9.9.9", timeout=1)
    (row,) = matrix["rows"]
    assert row["status"] == "fail" and "timeout" in row["reason"]


def test_disconnect_lines_are_recognised_in_every_spelling():
    for line in (
        "(-2147023174, 'The RPC server is unavailable.', None, None)",
        "HRESULT 0x800706BA",
        "RPC_S_SERVER_UNAVAILABLE",
    ):
        assert run_all.disconnect_event(line)["name"] == "RPC_S_SERVER_UNAVAILABLE"
    assert run_all.disconnect_event("0x80010108")["name"] == "RPC_E_DISCONNECTED"
    assert run_all.disconnect_event("RPC_E_CALL_REJECTED -2147418111") is None


def test_every_smoke_is_discovered_and_accore_runs_last():
    found = run_all.discover()
    names = [path.name for path in found]
    assert names[-1] == "smoke_accore.py"
    assert names[:-1] == sorted(p.name for p in (ROOT / "scripts").glob("smoke_*_com.py"))
    assert "smoke_premium.py" not in names, "not a live smoke"
    assert run_all.release()  # version.py


def test_run_all_is_runnable_headlessly():
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "live" / "run_all.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
    )
    assert done.returncode == 0 and "--require-seat" in done.stdout
    refused = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "live" / "run_all.py"), "--only", "nosuch"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
    )
    assert refused.returncode == 2 and "no such smoke" in refused.stderr
