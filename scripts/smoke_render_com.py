"""Exercise the 1.8-07 region render once against the live AutoCAD on this machine.

Runs on the live harness (scripts/live/harness.py): attach only, a NEW
document of its own (never the operator's drawings), closed on every path.

    entity_create_circle x2 (far apart)
    view_render(region around the first)      -> source "export_snapshot"
    drawing_export_dxf(<tmp>.dxf)             -> the same drawing as a file
    render_dxf_file(<tmp>.dxf, same region)   -> the headless render of it
    view_render()                             -> source "window"

Checks: the live region render is ``applied`` from the export snapshot, its
pixels equal the headless render of the same drawing exported to DXF, its
region and scale are that render's, and without a region the window capture
answers with ``max_px`` obeyed.

Exit codes are the harness's: 0 every check passed, 1 one failed or raised,
2 no running AutoCAD to attach to or the active document changed under it.

    uv run --frozen python scripts/smoke_render_com.py [--keep] [--json PATH]
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.live.harness import LiveHarness, main_cli  # noqa: E402

REGION = [-10.0, -10.0, 10.0, 10.0]
MAX_PX = 256


def _pixels(png: bytes) -> bytes:
    from PIL import Image

    return Image.open(io.BytesIO(png)).convert("RGB").tobytes()


async def body(h: LiveHarness) -> dict:
    from backends.region_render import render_dxf_file

    h.stage("draw")
    await h.new_document()
    backend = h.backend
    await backend.entity_create_circle(0.0, 0.0, 5.0)
    await backend.entity_create_circle(1000.0, 0.0, 5.0)

    h.stage("region")
    live = await backend.view_render(region=REGION, max_px=MAX_PX)
    folder = tempfile.mkdtemp(prefix="acadmcp_smoke_render_")
    try:
        target = str(Path(folder) / "same.dxf")
        await backend.drawing_export_dxf(target)
        headless = render_dxf_file(target, region=tuple(REGION), max_px=MAX_PX)
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    h.stage("window")
    window = await backend.view_render(max_px=MAX_PX)

    report = {
        "region": {k: v for k, v in live.items() if k != "png"},
        "headless": {k: v for k, v in headless.items() if k != "png"},
        "window": {k: v for k, v in window.items() if k != "png"},
        "pixels_equal": live.get("png") is not None
        and _pixels(live["png"]) == _pixels(headless["png"]),
    }
    h.stage("checks")
    assess(h, report)
    return report


def assess(h: LiveHarness, report: dict) -> None:
    live, headless, window = report["region"], report["headless"], report["window"]
    h.check(
        "a region renders the export snapshot",
        live.get("source") == "export_snapshot" and live.get("applied") is True,
        expected={"source": "export_snapshot", "applied": True},
        observed={"source": live.get("source"), "applied": live.get("applied")},
    )
    h.check(
        "the live region equals the headless render of the same DXF",
        report["pixels_equal"] is True,
        expected="identical pixels",
        observed={"live_px": live.get("px"), "headless_px": headless.get("px")},
    )
    h.check(
        "region and scale agree",
        live.get("region") == headless["region"]
        and live.get("px_per_unit") == headless["px_per_unit"],
        expected={"region": headless["region"], "px_per_unit": headless["px_per_unit"]},
        observed={"region": live.get("region"), "px_per_unit": live.get("px_per_unit")},
    )
    h.check(
        "without a region the window is captured within max_px",
        window.get("source") == "window" and max(window.get("px") or [MAX_PX + 1]) <= MAX_PX,
        expected={"source": "window", "max_px": MAX_PX},
        observed={"source": window.get("source"), "px": window.get("px")},
    )


if __name__ == "__main__":
    raise SystemExit(main_cli("render", body, description=__doc__.split("\n\n")[0]))
