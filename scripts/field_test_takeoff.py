"""Run both takeoffs on a real P&ID and layout, and write the report next to them.

LOCAL ONLY. This is the field test of track H (spec §15): the user points it at
a client's P&ID and layout, compares what comes out with the hand-made
workbooks, and keeps the result on their own disk. Nothing it reads or writes
may enter this repository (spec §14), so:

* the report is written next to the P&ID (or into ``--out``) and **the script
  refuses any output folder inside this repository**, before reading a byte;
* the console gets file names, row counts and the scale verdict - **never a
  quantity**, unless ``--show`` is given on the user's own terminal;
* it is never run in CI and never by a test on a real drawing; the test next
  to it (tests/test_field_test_takeoff.py) runs it on the synthetic plant pair.

What it writes: ``pipe_takeoff.xlsx`` / ``cable_takeoff.xlsx`` (CSV beside
them always, XLSX when the ``office`` extra is installed) through the same
``write_workbook`` the tools use, and ``field_test_report.json`` with the scale
check and both results in full.

    uv run --frozen python scripts/field_test_takeoff.py --pid D:/job/pid.dxf --layout D:/job/layout.dxf
    uv run --frozen python scripts/field_test_takeoff.py --pid ... --layout ... --lang en --show
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: Exit code for a refused run (output inside the repository, a missing file).
REFUSED = 2


def _inside_repository(folder: Path) -> bool:
    resolved = folder.resolve()
    return resolved == ROOT or ROOT in resolved.parents


def _parse(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pipe and cable takeoffs on a real P&ID + layout (local only)."
    )
    parser.add_argument("--pid", required=True, help="the P&ID, a .dxf")
    parser.add_argument("--layout", required=True, help="the layout, a .dxf")
    parser.add_argument("--out", default=None, help="report folder (default: the P&ID's folder)")
    parser.add_argument("--lang", default="tr", choices=("tr", "en", "ru"))
    parser.add_argument("--allowance", type=float, default=0.20)
    parser.add_argument(
        "--show", action="store_true", help="also print the totals (quantities) to the console"
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse(argv)
    pid_path, layout_path = Path(args.pid), Path(args.layout)
    out = Path(args.out) if args.out else pid_path.resolve().parent
    if _inside_repository(out):
        print(
            f"refused: {out} is inside the repository ({ROOT}); client drawings and "
            "anything derived from them never enter it. Pass --out outside it."
        )
        return REFUSED
    for path in (pid_path, layout_path):
        if not path.is_file():
            print(f"refused: {path} does not exist")
            return REFUSED

    from engineering.understand.network import build_network
    from engineering.understand.report import write_workbook
    from engineering.understand.scale import scale_check
    from engineering.understand.snapshot import read_snapshot
    from engineering.understand.takeoff import cable_rows, pipe_rows

    out.mkdir(parents=True, exist_ok=True)
    pid = read_snapshot(str(pid_path))
    layout = read_snapshot(str(layout_path))
    scale = scale_check(pid, layout)
    pipe = pipe_rows(build_network(pid), pid, layout, scale=scale, allowance=args.allowance)
    cable = cable_rows(pid, layout, allowance=args.allowance)
    written = [
        write_workbook(pipe, kind="pipe", lang=args.lang, path=str(out / "pipe_takeoff.xlsx")),
        write_workbook(cable, kind="cable", lang=args.lang, path=str(out / "cable_takeoff.xlsx")),
    ]
    report = out / "field_test_report.json"
    report.write_text(
        json.dumps(
            {"scale": scale, "pipe": pipe, "cable": cable, "written": written},
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "scale_verdict": scale["verdict"],
                "pipe_rows": len(pipe["rows"]),
                "cable_rows": len(cable["rows"]),
                "report": str(report),
                "workbooks": [
                    path
                    for item in written
                    for path in ([item["xlsx"]] if item["xlsx"] else []) + list(item["csv"])
                ],
            },
            indent=2,
        )
    )
    if args.show:
        print(
            json.dumps(
                {"pipe": pipe["rows"], "cable": cable["rows"]},
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
