"""scripts/field_test_takeoff.py never writes into the repository and never prints a quantity.

The script exists for client drawings, which never enter this repository
(spec §14). It is exercised here on the synthetic plant pair only - the field
test itself is never run in CI.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from tests.fixtures.plant_pair import build_plant_pair

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location(
        "field_test_takeoff", ROOT / "scripts" / "field_test_takeoff.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_output_folder_inside_the_repository_is_refused_before_anything_runs(tmp_path, capsys):
    truth = build_plant_pair(tmp_path)
    target = ROOT / "field_test_output_must_not_exist"
    code = _script().main(
        ["--pid", truth["pid"], "--layout", truth["layout"], "--out", str(target)]
    )
    assert code == 2
    assert not target.exists()
    assert "refused" in capsys.readouterr().out


def test_a_missing_drawing_is_refused(tmp_path, capsys):
    code = _script().main(
        ["--pid", str(tmp_path / "none.dxf"), "--layout", str(tmp_path / "none.dxf")]
    )
    assert code == 2
    assert "does not exist" in capsys.readouterr().out


def test_the_report_lands_next_to_the_pid_and_the_console_carries_no_quantity(tmp_path, capsys):
    truth = build_plant_pair(tmp_path)
    code = _script().main(["--pid", truth["pid"], "--layout", truth["layout"]])
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["scale_verdict"] == "schematic"
    assert Path(summary["report"]).parent == tmp_path
    assert all(Path(path).parent == tmp_path for path in summary["workbooks"])
    printed = json.dumps({k: v for k, v in summary.items() if k not in ("report", "workbooks")})
    for run in truth["pipe_runs"]:
        if run["layout_length_mm"] is not None:
            assert f"{run['layout_length_mm']:g}" not in printed, run["id"]
    for row in truth["cable_rows"]:
        assert f"{row['length_mm']:g}" not in printed, row["tag"]
    report = json.loads(Path(summary["report"]).read_text(encoding="utf-8"))
    assert report["scale"]["verdict"] == "schematic"


def test_show_prints_the_rows_on_request(tmp_path, capsys):
    truth = build_plant_pair(tmp_path)
    code = _script().main(["--pid", truth["pid"], "--layout", truth["layout"], "--show"])
    assert code == 0
    assert '"cable"' in capsys.readouterr().out
