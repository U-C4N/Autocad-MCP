"""Named views and UCS on both engines.

Headless, a named view is a VIEW table entry and "restore" writes the
``*Active`` VPORT (the view the file opens on) — ezdxf's header has no
``$VIEWCTR``; measured. A UCS is a UCS table entry plus the ``$UCS*`` header
variables. Tool coordinates stay WCS on both engines: a UCS is stored and
made current for the operator, and no tool interprets its inputs in it.
"""

from __future__ import annotations

import types

import pytest

from engineering.environment.ucs import resolve_ucs_axes
from engineering.environment.views import resolve_view_args

pytestmark = pytest.mark.asyncio


# ── pure helpers ────────────────────────────────────────────────────────────


def test_view_args_validate_before_any_write():
    assert resolve_view_args([10, 20], 50, None) == {
        "center": (10.0, 20.0),
        "height": 50.0,
        "width": None,
    }
    assert resolve_view_args(None, None, None) == {"center": None, "height": None, "width": None}
    with pytest.raises(ValueError, match="height must be > 0"):
        resolve_view_args(None, 0, None)
    with pytest.raises(ValueError, match="width must be > 0"):
        resolve_view_args(None, 10, -1)
    with pytest.raises(ValueError, match="center"):
        resolve_view_args([1], None, None)
    with pytest.raises(ValueError, match="finite"):
        resolve_view_args([float("nan"), 0], None, None)


def test_ucs_axes_are_normalised_and_orthogonality_is_measured():
    axes = resolve_ucs_axes([10, 10, 0], [0, 2, 0], [-3, 0, 0])
    assert axes["origin"] == (10.0, 10.0, 0.0)
    assert axes["x_axis"] == (0.0, 1.0, 0.0) and axes["y_axis"] == (-1.0, 0.0, 0.0)
    assert axes["z_axis"] == (0.0, 0.0, 1.0)
    assert axes["angle_deg"] == pytest.approx(90.0)
    with pytest.raises(ValueError, match=r"measured 45\.00"):
        resolve_ucs_axes([0, 0, 0], [1, 0, 0], [1, 1, 0])
    with pytest.raises(ValueError, match="x_axis.*zero"):
        resolve_ucs_axes([0, 0, 0], [0, 0, 0], [0, 1, 0])
    with pytest.raises(ValueError, match="origin"):
        resolve_ucs_axes([0, 0], [1, 0, 0], [0, 1, 0])


# ── headless: named views ───────────────────────────────────────────────────


async def test_view_defaults_to_the_drawing_extents(backend):
    await backend.entity_create_line(0, 0, 100, 50)
    result = await backend.view_named_save("OVERALL")
    assert result["ok"] and result["replaced"] is False
    assert result["center"] == [50.0, 25.0]
    # extents 100 x 50 at the *Active VPORT's default aspect 1.34: the width
    # is the binding side, so height = 100 / 1.34
    assert result["height"] == pytest.approx(100.0 / 1.34)
    assert result["width"] == pytest.approx(100.0)
    view = backend._doc.views.get("OVERALL")
    assert tuple(view.dxf.center)[:2] == (50.0, 25.0)


async def test_view_on_an_empty_drawing_needs_an_explicit_window(backend):
    with pytest.raises(ValueError, match="no extents"):
        await backend.view_named_save("EMPTY")
    assert "EMPTY" not in backend._doc.views
    result = await backend.view_named_save("EMPTY", center=[0, 0], height=10)
    assert result["center"] == [0.0, 0.0] and result["height"] == 10.0
    assert result["width"] == pytest.approx(13.4)


async def test_view_replace_restore_and_list(backend):
    await backend.view_named_save("V", center=[1, 2], height=10, width=15)
    again = await backend.view_named_save("v", center=[5, 5], height=20)
    assert again["replaced"] is True and again["name"] == "V"
    assert len(list(backend._doc.views)) == 1
    restored = await backend.view_named_restore("v")
    assert restored == {
        "ok": True,
        "name": "V",
        "center": [5.0, 5.0],
        "height": 20.0,
        "width": pytest.approx(26.8),
        "applied": "header_only",
        "store": "vport_active",
        "backend": "ezdxf",
    }
    (vport,) = backend._doc.viewports.get("*Active")
    assert tuple(vport.dxf.center)[:2] == (5.0, 5.0) and vport.dxf.height == 20.0
    assert await backend.view_named_list() == [
        {"name": "V", "center": [5.0, 5.0], "height": 20.0, "width": pytest.approx(26.8)}
    ]
    with pytest.raises(ValueError, match="no named view 'NOPE'"):
        await backend.view_named_restore("NOPE")
    with pytest.raises(ValueError, match="height must be > 0"):
        await backend.view_named_save("BAD", center=[0, 0], height=0)
    assert "BAD" not in backend._doc.views


# ── headless: UCS ───────────────────────────────────────────────────────────


async def test_ucs_set_stores_the_entry_and_makes_it_current(backend):
    result = await backend.ucs_set("FRONT", [10, 10, 0], [0, 1, 0], [-1, 0, 0])
    assert result == {
        "ok": True,
        "name": "FRONT",
        "origin": [10.0, 10.0, 0.0],
        "x_axis": [0.0, 1.0, 0.0],
        "y_axis": [-1.0, 0.0, 0.0],
        "replaced": False,
        "current": True,
        "backend": "ezdxf",
    }
    header = backend._doc.header
    assert header["$UCSNAME"] == "FRONT" and tuple(header["$UCSORG"]) == (10.0, 10.0, 0.0)
    assert tuple(header["$UCSXDIR"]) == (0.0, 1.0, 0.0)
    rows = await backend.ucs_list()
    assert rows[0] == {
        "name": "world",
        "origin": [0.0, 0.0, 0.0],
        "x_axis": [1.0, 0.0, 0.0],
        "y_axis": [0.0, 1.0, 0.0],
        "current": False,
    }
    assert rows[1]["name"] == "FRONT" and rows[1]["current"] is True


async def test_ucs_non_orthogonal_is_refused_before_any_write(backend):
    with pytest.raises(ValueError, match="measured 60"):
        await backend.ucs_set("SKEW", [0, 0, 0], [1, 0, 0], [0.5, 0.8660254, 0])
    assert "SKEW" not in backend._doc.ucs and backend._doc.header["$UCSNAME"] == ""


async def test_ucs_restore_world_and_named(backend):
    await backend.ucs_set("FRONT", [10, 10, 0], [0, 1, 0], [-1, 0, 0])
    world = await backend.ucs_restore("world")
    assert world["name"] == "world" and backend._doc.header["$UCSNAME"] == ""
    assert tuple(backend._doc.header["$UCSXDIR"]) == (1.0, 0.0, 0.0)
    assert (await backend.ucs_list())[0]["current"] is True
    named = await backend.ucs_restore("front")
    assert named["name"] == "FRONT" and backend._doc.header["$UCSNAME"] == "FRONT"
    with pytest.raises(ValueError, match="no UCS named 'NOPE'"):
        await backend.ucs_restore("NOPE")
    with pytest.raises(ValueError, match="reserved"):
        await backend.ucs_set("world", [0, 0, 0], [1, 0, 0], [0, 1, 0])


async def test_ucs_replace_keeps_one_entry(backend):
    await backend.ucs_set("A", [0, 0, 0], [1, 0, 0], [0, 1, 0])
    again = await backend.ucs_set("a", [1, 1, 0], [1, 0, 0], [0, 1, 0])
    assert again["replaced"] is True and again["name"] == "A"
    assert len(list(backend._doc.ucs)) == 1


# ── live engine, against a fake ActiveX surface ──────────────────────────────


class _FakeView:
    def __init__(self, name):
        self.Name = name
        self.Center = (0.0, 0.0)
        self.Height = 100.0
        self.Width = 150.0


class _FakeViews:
    def __init__(self):
        self.views: list[_FakeView] = []
        self.calls: list[tuple[str, tuple]] = []

    @property
    def Count(self):
        return len(self.views)

    def Item(self, key):
        if isinstance(key, int):
            return self.views[key]
        for v in self.views:
            if v.Name.lower() == key.lower():
                return v
        raise RuntimeError(f"no view {key}")

    def Add(self, name):
        self.calls.append(("Add", (name,)))
        v = _FakeView(name)
        self.views.append(v)
        return v


class _FakeUcs:
    def __init__(self, name, origin, xv, yv):
        self.Name = name
        self.Origin = origin
        self.XVector = xv
        self.YVector = yv


class _FakeUcsCollection:
    def __init__(self):
        self.items: list[_FakeUcs] = []
        self.calls: list[tuple[str, tuple]] = []

    @property
    def Count(self):
        return len(self.items)

    def Item(self, key):
        if isinstance(key, int):
            return self.items[key]
        for u in self.items:
            if u.Name.lower() == key.lower():
                return u
        raise RuntimeError(f"no ucs {key}")

    def Add(self, origin, x_point, y_point, name):
        self.calls.append(("Add", (origin, x_point, y_point, name)))
        # ActiveX stores unit vectors derived from the two points.
        xv = tuple(p - o for p, o in zip(x_point, origin, strict=True))
        yv = tuple(p - o for p, o in zip(y_point, origin, strict=True))
        u = _FakeUcs(name, origin, xv, yv)
        self.items.append(u)
        return u


class _FakeDocument:
    def __init__(self):
        self.Views = _FakeViews()
        self.UserCoordinateSystems = _FakeUcsCollection()
        self._viewport = types.SimpleNamespace(Center=(30.0, 40.0), Height=100.0, Width=150.0)
        self.viewport_sets = 0
        self.ActiveUCS = None
        self.commands: list[str] = []

    @property
    def ActiveViewport(self):
        return self._viewport

    @ActiveViewport.setter
    def ActiveViewport(self, value):
        self._viewport = value
        self.viewport_sets += 1

    def SendCommand(self, macro):
        self.commands.append(macro)


@pytest.fixture
def com_backend(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    document = _FakeDocument()
    variables = {"CMDACTIVE": 0, "UCSNAME": ""}
    app = types.SimpleNamespace(GetVariable=lambda name: variables[name])
    monkeypatch.setattr(module, "_acad_doc", lambda: document)
    monkeypatch.setattr(module, "_acad_app", lambda: app)
    monkeypatch.setattr(module, "_regen", lambda: None)
    monkeypatch.setattr(module, "_apoint", lambda x, y, z=0.0: (float(x), float(y), float(z)))
    monkeypatch.setattr(module, "_av", lambda values: tuple(float(v) for v in values))
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, document, variables


async def test_com_view_save_defaults_to_the_active_viewport(com_backend):
    backend, document, _ = com_backend
    result = await backend.view_named_save("HOME")
    assert document.Views.calls == [("Add", ("HOME",))]
    (view,) = document.Views.views
    assert view.Center == (30.0, 40.0) and view.Height == 100.0 and view.Width == 150.0
    assert result["center"] == [30.0, 40.0] and result["replaced"] is False
    again = await backend.view_named_save("home", center=[1, 2], height=10)
    assert again["replaced"] is True and len(document.Views.views) == 1
    assert view.Center == (1.0, 2.0) and view.Height == 10.0 and view.Width == 15.0


async def test_com_view_restore_sets_the_active_viewport(com_backend):
    backend, document, _ = com_backend
    await backend.view_named_save("HOME", center=[5, 6], height=20, width=30)
    result = await backend.view_named_restore("HOME")
    assert result["applied"] == "active_viewport"
    assert document.ActiveViewport.Center == (5.0, 6.0)
    assert document.ActiveViewport.Height == 20.0 and document.ActiveViewport.Width == 30.0
    assert document.viewport_sets == 1, "ActiveX applies a viewport change only on re-assignment"
    assert await backend.view_named_list() == [
        {"name": "HOME", "center": [5.0, 6.0], "height": 20.0, "width": 30.0}
    ]


async def test_com_ucs_add_uses_points_on_the_axes_not_vectors(com_backend):
    backend, document, variables = com_backend
    result = await backend.ucs_set("FRONT", [10, 10, 0], [0, 5, 0], [-1, 0, 0])
    assert document.UserCoordinateSystems.calls == [
        ("Add", ((10.0, 10.0, 0.0), (10.0, 11.0, 0.0), (9.0, 10.0, 0.0), "FRONT"))
    ]
    assert document.ActiveUCS is document.UserCoordinateSystems.items[0]
    assert result["x_axis"] == [0.0, 1.0, 0.0] and result["current"] is True
    variables["UCSNAME"] = "FRONT"
    rows = await backend.ucs_list()
    assert rows[0]["name"] == "world" and rows[0]["current"] is False
    assert rows[1] == {
        "name": "FRONT",
        "origin": [10.0, 10.0, 0.0],
        "x_axis": [0.0, 1.0, 0.0],
        "y_axis": [-1.0, 0.0, 0.0],
        "current": True,
    }


async def test_com_ucs_restore_world_sends_the_ucs_command_when_idle(com_backend):
    backend, document, variables = com_backend
    result = await backend.ucs_restore("world")
    assert document.commands == ["_.UCS _W\n"] and result["name"] == "world"
    variables["CMDACTIVE"] = 1
    with pytest.raises(RuntimeError, match="CMDACTIVE"):
        await backend.ucs_restore("world")
    assert len(document.commands) == 1


async def test_com_ucs_refuses_before_activex(com_backend):
    backend, document, _ = com_backend
    with pytest.raises(ValueError, match="measured"):
        await backend.ucs_set("SKEW", [0, 0, 0], [1, 0, 0], [1, 1, 0])
    assert document.UserCoordinateSystems.calls == []
