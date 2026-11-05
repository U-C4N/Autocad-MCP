"""#17: the live window capture is DPI-correct at any display scaling.

With Windows display scaling at 150 % a DPI-unaware process reads AutoCAD's
window rectangle in logical pixels (2/3 of the physical size) while
``PrintWindow`` draws physical ones, so the capture was the top-left 2/3 of the
window. These tests fake win32gui / win32ui and the ``user32`` / ``shcore``
DLLs (``cb._win_dll``) - no AutoCAD, no real GDI - and hold:

* the process is made DPI-aware before ``GetWindowRect`` is read;
* aware, the rectangle is used as it is;
* unaware (every awareness call refused), the bitmap is the rectangle scaled
  by ``GetDpiForWindow / 96``;
* a minimized window is reported, and the result carries ``dpi`` / ``capture``.

The 1.7.0 code fails each one: it never asks for awareness, never scales, and
reports nothing beside the PNG.
"""

from __future__ import annotations

import io

import pytest

Image = pytest.importorskip("PIL.Image", reason="the capture is encoded with Pillow")

from backends import com_backend as cb  # noqa: E402
from backends.quarantine import DaemonExecutor  # noqa: E402
from tests.test_bound_document_com import _Seat  # noqa: E402

LOGICAL = (0, 0, 200, 100)  # what an unaware process reads at 150 %
DPI = 144


class _User32:
    def __init__(self, calls: list, *, aware: bool, iconic: bool = False):
        self.calls = calls
        self._aware = aware
        self._iconic = iconic
        self.awareness = 0

        # Plain functions, not bound methods: the code under test sets ctypes
        # ``restype`` / ``argtypes`` on them, as on a real DLL export.
        def get_context():
            return 1000 + self.awareness

        def from_context(ctx):
            return ctx - 1000

        self.GetThreadDpiAwarenessContext = get_context
        self.GetAwarenessFromDpiAwarenessContext = from_context

    def SetProcessDpiAwarenessContext(self, ctx):
        self.calls.append("SetProcessDpiAwarenessContext")
        if self._aware:
            self.awareness = 2
        return 1 if self._aware else 0

    def SetProcessDPIAware(self):
        self.calls.append("SetProcessDPIAware")
        return 0

    def GetDpiForWindow(self, hwnd):
        return DPI

    def IsIconic(self, hwnd):
        return 1 if self._iconic else 0

    def PrintWindow(self, hwnd, hdc, flags):
        self.calls.append("PrintWindow")
        return 1


class _Shcore:
    def __init__(self, calls: list):
        self.calls = calls

    def SetProcessDpiAwareness(self, value):
        self.calls.append("SetProcessDpiAwareness")
        return 0x80070005  # E_ACCESSDENIED


class _Bitmap:
    def __init__(self, sizes: list):
        self.sizes = sizes
        self.size = (0, 0)

    def CreateCompatibleBitmap(self, dc, w, h):
        self.size = (w, h)
        self.sizes.append((w, h))

    def GetBitmapBits(self, flag):
        w, h = self.size
        return b"\xff" * (w * h * 4)

    def GetHandle(self):
        return 7


class _DC:
    def CreateCompatibleDC(self):
        return _DC()

    def SelectObject(self, obj):
        return None

    def GetSafeHdc(self):
        return 9

    def DeleteDC(self):
        return None


@pytest.fixture
def gdi(monkeypatch):
    """Fake GDI; returns (calls, bitmap sizes, install(user32))."""
    calls: list = []
    sizes: list = []

    class _Gui:
        @staticmethod
        def GetWindowRect(hwnd):
            calls.append("GetWindowRect")
            return LOGICAL if dlls["user32"].awareness == 0 else (0, 0, 300, 150)

        GetWindowDC = staticmethod(lambda hwnd: 5)
        DeleteObject = staticmethod(lambda h: None)
        ReleaseDC = staticmethod(lambda hwnd, dc: None)

    class _Ui:
        CreateDCFromHandle = staticmethod(lambda dc: _DC())
        CreateBitmap = staticmethod(lambda: _Bitmap(sizes))

    dlls: dict = {"shcore": _Shcore(calls)}
    monkeypatch.setattr(cb, "_WIN32_AVAILABLE", True)
    monkeypatch.setattr(cb, "_PIL_OK", True)
    monkeypatch.setattr(cb, "win32gui", _Gui, raising=False)
    monkeypatch.setattr(cb, "win32ui", _Ui, raising=False)
    monkeypatch.setattr(cb, "_win_dll", lambda name: dlls.get(name), raising=False)
    monkeypatch.setattr(cb, "_DPI_STATE", {}, raising=False)

    def install(user32):
        dlls["user32"] = user32

    return calls, sizes, install


def _png_size(png: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(png)).size


def test_awareness_is_set_before_the_rectangle_is_read(gdi):
    calls, sizes, install = gdi
    install(_User32(calls, aware=True))
    png = cb._capture_window(1234)
    assert png is not None
    assert "SetProcessDpiAwarenessContext" in calls
    assert calls.index("SetProcessDpiAwarenessContext") < calls.index("GetWindowRect")


def test_aware_the_physical_rectangle_is_used_as_it_is(gdi):
    calls, sizes, install = gdi
    install(_User32(calls, aware=True))
    png = cb._capture_window(1234)
    assert sizes == [(300, 150)]
    assert _png_size(png) == (300, 150)
    assert cb._LAST_CAPTURE["dpi"] == DPI
    assert cb._LAST_CAPTURE["dpi_awareness"] == "per_monitor"
    assert cb._LAST_CAPTURE["capture"] == [300, 150]
    assert cb._LAST_CAPTURE["window_minimized"] is False


def test_unaware_the_bitmap_is_scaled_by_the_window_dpi(gdi):
    calls, sizes, install = gdi
    install(_User32(calls, aware=False))
    png = cb._capture_window(1234)
    # every awareness route was tried and refused, then the rect was scaled
    assert "SetProcessDpiAwareness" in calls and "SetProcessDPIAware" in calls
    assert sizes == [(300, 150)], "200x100 logical x 144/96 = 300x150 physical"
    assert _png_size(png) == (300, 150)
    assert cb._LAST_CAPTURE["dpi_awareness"] == "unaware"
    assert cb._LAST_CAPTURE["capture"] == [300, 150]


def test_awareness_is_asked_for_once_per_process(gdi):
    calls, sizes, install = gdi
    install(_User32(calls, aware=True))
    cb._capture_window(1234)
    cb._capture_window(1234)
    assert calls.count("SetProcessDpiAwarenessContext") == 1


def test_the_sta_initializer_makes_the_process_aware(gdi, monkeypatch):
    calls, sizes, install = gdi
    install(_User32(calls, aware=True))
    monkeypatch.setattr(cb, "_COM_IMPORTS_OK", False)  # no CoInitialize in the test
    cb._com_init()
    assert calls == ["SetProcessDpiAwarenessContext"]


def test_a_failing_awareness_call_is_never_raised(gdi):
    calls, sizes, install = gdi

    class _Broken(_User32):
        def SetProcessDpiAwarenessContext(self, ctx):
            raise OSError("access denied")

    install(_Broken(calls, aware=False))
    assert cb._ensure_dpi_aware() == "unaware"


@pytest.mark.asyncio
async def test_a_minimized_window_is_reported_in_the_live_report(gdi, monkeypatch):
    calls, sizes, install = gdi
    install(_User32(calls, aware=True, iconic=True))
    seat = _Seat()
    monkeypatch.setattr(cb, "_acad_app", lambda: seat)
    monkeypatch.setattr(cb, "_find_autocad_hwnd", lambda: 1234)
    backend = cb.ComBackend()
    backend._executor = DaemonExecutor(max_workers=1, name="test-sta")  # no CoInitialize
    backend._connected = True
    try:
        report = await backend.view_render(max_px=1568)
    finally:
        backend._executor.shutdown(wait=False)
        backend._executor = None
    assert report["source"] == "window"
    assert report["window_minimized"] is True
    assert report["dpi"] == DPI
    assert report["capture"] == [300, 150]
    assert report["px"] == [300, 150]
