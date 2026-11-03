"""1.8-07 - an honest region image (ergo-9, narrowed).

Before 1.8.0 the headless ``view_zoom_and_screenshot`` passed its window to a
no-op ``view_zoom_window`` and then rendered the whole model space: the caller
asked for one corner of the sheet and got the sheet, with nothing saying so
(the C2 class of false success). ``view_screenshot`` had no region at all, so a
detail cost a full-page PNG.

The gate: of two far-apart circles, a window on one draws only that one - its
ink bounding box sits where ``px_per_unit`` says it should - the report says
``applied: true`` with ``{px, region, px_per_unit}``, and ``max_px`` is obeyed.
On the live engine a region renders the ``Document.Export`` snapshot through
the same headless renderer (``source: "export_snapshot"``); without one it is
the window capture (``source: "window"``). No AutoCAD is touched here: the
seat is the fake of ``tests/test_bound_document_com.py``.
"""

from __future__ import annotations

import io
import json

import ezdxf
import pytest

pytest.importorskip("matplotlib", reason="rendering needs the [pdf] extra")
Image = pytest.importorskip("PIL.Image", reason="the ink check reads pixels with Pillow")

from backends import com_backend as cb  # noqa: E402
from backends.base import UnsupportedCapabilityError  # noqa: E402
from backends.document_guard import ActiveDocumentChangedError  # noqa: E402
from backends.quarantine import DaemonExecutor  # noqa: E402
from backends.region_render import (  # noqa: E402
    DEFAULT_MAX_PX,
    png_size,
    render_dxf_file,
)
from tests.test_bound_document_com import _Seat  # noqa: E402

LEFT = (0.0, 0.0)
RIGHT = (1000.0, 0.0)
RADIUS = 5.0


def _ink_box(png: bytes) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) of every pixel unlike the background corner."""
    image = Image.open(io.BytesIO(png)).convert("RGB")
    background = image.getpixel((0, 0))
    width, height = image.size
    pixels = image.load()
    xs, ys = [], []
    for y in range(height):
        for x in range(width):
            if pixels[x, y] != background:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _expected_box(report: dict, cx: float, cy: float, r: float) -> tuple[float, ...]:
    """The circle's pixel box from the report's own region and scale."""
    x0, _y0, _x1, y1 = report["region"]
    ppu = report["px_per_unit"]
    return ((cx - r - x0) * ppu, (y1 - (cy + r)) * ppu, (cx + r - x0) * ppu, (y1 - (cy - r)) * ppu)


async def _two_circles(backend):
    await backend.entity_create_circle(*LEFT, RADIUS)
    await backend.entity_create_circle(*RIGHT, RADIUS)


# ── headless: the window is applied ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_window_on_one_of_two_far_circles_draws_only_that_one(backend):
    await _two_circles(backend)
    report = await backend.view_render(region=[-10, -10, 10, 10], max_px=256)

    assert report["applied"] is True
    assert report["source"] == "headless"
    assert report["framing"] == "region"
    assert report["px"] == [256, 256]
    assert report["region"] == pytest.approx([-10, -10, 10, 10])
    assert report["px_per_unit"] == pytest.approx(256 / 20)

    ink = _ink_box(report["png"])
    assert ink is not None, "the circle inside the window was not drawn"
    expected = _expected_box(report, *LEFT, RADIUS)
    for got, want in zip(ink, expected, strict=True):
        assert got == pytest.approx(want, abs=3), (ink, expected)


@pytest.mark.asyncio
async def test_a_window_between_the_circles_draws_nothing(backend):
    await _two_circles(backend)
    report = await backend.view_render(region=[400, -10, 600, 10], max_px=200)
    assert report["applied"] is True
    assert _ink_box(report["png"]) is None, "geometry outside the window leaked into the image"


@pytest.mark.asyncio
async def test_the_window_on_the_far_circle_puts_it_where_the_scale_says(backend):
    await _two_circles(backend)
    report = await backend.view_render(region=[990, -20, 1020, 10], max_px=300)
    ink = _ink_box(report["png"])
    expected = _expected_box(report, *RIGHT, RADIUS)
    for got, want in zip(ink, expected, strict=True):
        assert got == pytest.approx(want, abs=3), (ink, expected)


@pytest.mark.asyncio
async def test_max_px_is_obeyed_and_the_scale_is_one_number(backend):
    await _two_circles(backend)
    report = await backend.view_render(region=[0, 0, 100, 37], max_px=400)
    width, height = report["px"]
    assert max(width, height) == 400
    assert png_size(report["png"]) == (width, height), "the reported px is the file's own size"
    x0, y0, x1, y1 = report["region"]
    ppu = report["px_per_unit"]
    assert width / (x1 - x0) == pytest.approx(ppu, rel=1e-9)
    assert height / (y1 - y0) == pytest.approx(ppu, rel=1e-9)
    # the drawn region holds the requested one, widened by under a pixel
    assert x0 <= 0 and y0 <= 0 and x1 >= 100 and y1 >= 37
    assert (x1 - x0) - 100 < 1 / ppu + 1e-9 and (y1 - y0) - 37 < 1 / ppu + 1e-9
    assert report["requested_region"] == [0, 0, 100, 37]


@pytest.mark.asyncio
async def test_the_default_size_is_the_model_image_limit(backend):
    await _two_circles(backend)
    report = await backend.view_render(region=[-10, -10, 10, 10])
    assert DEFAULT_MAX_PX == 1568
    assert report["px"] == [1568, 1568]


@pytest.mark.asyncio
async def test_corners_in_any_order_are_one_region(backend):
    await _two_circles(backend)
    report = await backend.view_render(region=[10, 10, -10, -10], max_px=128)
    assert report["requested_region"] == [-10, -10, 10, 10]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "region",
    [[0, 0, 0, 10], [0, 5, 10, 5], [0, 0, 10], [0, 0, float("nan"), 1], "abcd"],
)
async def test_a_degenerate_region_is_refused_by_name(backend, region):
    with pytest.raises(ValueError, match="region"):
        await backend.view_render(region=region)


@pytest.mark.asyncio
@pytest.mark.parametrize("max_px", [0, 63, 4097])
async def test_max_px_out_of_range_is_refused_by_name(backend, max_px):
    with pytest.raises(ValueError, match="max_px"):
        await backend.view_render(region=[0, 0, 1, 1], max_px=max_px)


@pytest.mark.asyncio
async def test_no_region_frames_the_extents_and_says_so(backend):
    await _two_circles(backend)
    report = await backend.view_render(max_px=800)
    assert report["applied"] is True
    assert report["framing"] == "extents"
    assert report["requested_region"] is None
    x0, y0, x1, y1 = report["region"]
    assert x0 <= -RADIUS and x1 >= 1000 + RADIUS and y0 <= -RADIUS and y1 >= RADIUS
    assert max(report["px"]) == 800


@pytest.mark.asyncio
async def test_handle_labels_only_for_entities_in_the_region(backend):
    left = await backend.entity_create_circle(*LEFT, RADIUS)
    await backend.entity_create_circle(*RIGHT, RADIUS)
    report = await backend.view_render(region=[-10, -10, 10, 10], max_px=256, overlay_handles=True)
    assert set(report["labels"]) == {left.handle}
    assert report["total"] == 1 and report["truncated"] is False


# ── the tools ────────────────────────────────────────────────────────────────


def _tool_report(result) -> tuple[bytes, dict]:
    import base64

    images = [c for c in result.content if getattr(c, "type", None) == "image"]
    assert len(images) == 1, result.content
    report = result.structured_content
    texts = [c for c in result.content if getattr(c, "type", None) == "text"]
    assert texts and json.loads(texts[0].text) == report, "the text block carries the report"
    return base64.b64decode(images[0].data), report


@pytest.mark.asyncio
async def test_view_screenshot_tool_reports_the_region():
    from fastmcp import Client

    import server

    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        await client.call_tool("entity_create_circle", {"cx": 0, "cy": 0, "radius": RADIUS})
        await client.call_tool("entity_create_circle", {"cx": 1000, "cy": 0, "radius": RADIUS})
        result = await client.call_tool(
            "view_screenshot", {"region": [-10, -10, 10, 10], "max_px": 256}
        )
    png, report = _tool_report(result)
    assert report["ok"] is True and report["applied"] is True
    assert report["source"] == "headless"
    assert report["px"] == [256, 256] and png_size(png) == (256, 256)
    assert report["px_per_unit"] == pytest.approx(12.8)
    assert "png" not in report


@pytest.mark.asyncio
async def test_view_zoom_and_screenshot_applies_the_window_headlessly():
    from fastmcp import Client

    import server

    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        await client.call_tool("entity_create_circle", {"cx": 0, "cy": 0, "radius": RADIUS})
        await client.call_tool("entity_create_circle", {"cx": 1000, "cy": 0, "radius": RADIUS})
        result = await client.call_tool(
            "view_zoom_and_screenshot",
            {"x1": 990, "y1": -10, "x2": 1010, "y2": 10, "max_px": 200},
        )
    png, report = _tool_report(result)
    assert report["applied"] is True and report["framing"] == "region"
    assert report["region"] == pytest.approx([990, -10, 1010, 10])
    ink = _ink_box(png)
    expected = _expected_box(report, *RIGHT, RADIUS)
    for got, want in zip(ink, expected, strict=True):
        assert got == pytest.approx(want, abs=3)


@pytest.mark.asyncio
async def test_a_partial_window_is_refused_not_ignored():
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    import server

    async with Client(server.mcp) as client:
        await client.call_tool("drawing_new", {})
        with pytest.raises(ToolError, match="y2"):
            await client.call_tool("view_zoom_and_screenshot", {"x1": 0, "y1": 0, "x2": 10})


# ── live engine (fake seat) ──────────────────────────────────────────────────


def _two_circle_dxf(path: str) -> None:
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_circle(LEFT, RADIUS)
    msp.add_circle(RIGHT, RADIUS)
    doc.saveas(path)


class _ExportingSeat(_Seat):
    """The fake seat whose Export writes the two-circle drawing, plus ZoomWindow."""

    def __init__(self):
        super().__init__()
        self.zoomed: list[tuple] = []
        for doc in (self.first, self.second):
            doc.Export = lambda base, ext, sel: _two_circle_dxf(f"{base}.dxf")

    def ZoomWindow(self, p1, p2):
        self.zoomed.append((tuple(p1), tuple(p2)))


@pytest.fixture
def seat(monkeypatch):
    fake = _ExportingSeat()
    monkeypatch.setattr(cb, "_acad_app", lambda: fake)
    return fake


@pytest.fixture
def com(seat):
    backend = cb.ComBackend()
    backend._executor = DaemonExecutor(max_workers=1, name="test-sta")  # no CoInitialize
    backend._connected = True
    yield backend
    if backend._executor is not None:
        backend._executor.shutdown(wait=False)
        backend._executor = None


def _pixels(png: bytes) -> bytes:
    return Image.open(io.BytesIO(png)).convert("RGB").tobytes()


@pytest.mark.asyncio
async def test_a_live_region_is_the_headless_render_of_the_exported_dxf(com, tmp_path):
    report = await com.view_render(region=[-10, -10, 10, 10], max_px=256)
    assert report["source"] == "export_snapshot"
    assert report["applied"] is True
    assert report["snapshot"] == "live:First.dwg"
    assert report["px"] == [256, 256]

    same = tmp_path / "same.dxf"
    _two_circle_dxf(str(same))
    headless = render_dxf_file(str(same), region=(-10.0, -10.0, 10.0, 10.0), max_px=256)
    assert _pixels(report["png"]) == _pixels(headless["png"])
    assert report["region"] == headless["region"]
    assert report["px_per_unit"] == headless["px_per_unit"]


@pytest.mark.asyncio
async def test_a_live_region_after_a_tab_switch_is_refused(com, seat):
    await com.system_get_variable("LTSCALE")  # bound to First.dwg
    seat.ActiveDocument = seat.second
    with pytest.raises(ActiveDocumentChangedError):
        await com.view_render(region=[-10, -10, 10, 10], max_px=128)


@pytest.mark.asyncio
async def test_without_a_region_the_live_engine_captures_the_window(com, monkeypatch):
    buf = io.BytesIO()
    Image.new("RGB", (3000, 1500), "white").save(buf, format="PNG")
    monkeypatch.setattr(cb, "_find_autocad_hwnd", lambda: 1234)
    monkeypatch.setattr(cb, "_capture_window", lambda hwnd: buf.getvalue())
    report = await com.view_render(max_px=600)
    assert report["source"] == "window"
    assert report["applied"] is True and report["framing"] == "current_view"
    assert report["px"] == [600, 300], "max_px is obeyed on a capture too"
    assert png_size(report["png"]) == (600, 300)
    assert report["region"] is None and report["px_per_unit"] is None


@pytest.mark.asyncio
async def test_live_handle_labels_are_still_refused(com):
    with pytest.raises(UnsupportedCapabilityError) as refused:
        await com.view_render(region=[0, 0, 1, 1], overlay_handles=True)
    assert refused.value.capability == "handle_overlay"
