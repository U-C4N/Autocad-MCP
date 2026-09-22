"""Tests for COM backend logic that runs without a live AutoCAD instance.

Strategy:
  - normalize_lineweight: imported from backends.base — no COM needed.
  - _safe_send_command: ComBackend is importable on non-Windows because the
    win32com block is guarded by ``if _WIN32_AVAILABLE``. The static method
    only calls methods on a *mock* doc object, so it runs fine on Linux/CI.
  - Live ComBackend integration tests: skipped unless AutoCAD is reachable.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from backends.base import normalize_lineweight
from backends.com_backend import ComBackend


@pytest.mark.asyncio
async def test_active_document_switch_clears_com_document_state(monkeypatch):
    backend = ComBackend()
    documents = SimpleNamespace(Count=1)
    app = SimpleNamespace(
        Documents=documents,
        ActiveDocument=SimpleNamespace(Name="First.dwg", FullName="C:/First.dwg"),
    )

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", run_inline)
    monkeypatch.setattr("backends.com_backend._acad_app", lambda: app)

    await backend._ensure_document_state()
    backend._plan_spec = object()
    backend._preflight_result = object()
    backend._gdt_datums_defined = {"A"}
    app.ActiveDocument = SimpleNamespace(Name="Second.dwg", FullName="C:/Second.dwg")

    await backend._ensure_document_state()

    assert backend._plan_spec is None
    assert backend._preflight_result is None
    assert backend._gdt_datums_defined == set()


# ---------------------------------------------------------------------------
# normalize_lineweight — parametrized table
# ---------------------------------------------------------------------------

NL_TABLE: list[tuple[Any, Any]] = [
    # mm-float inputs (0 < v <= 2.05) → converted to hundredths
    (0.13, 13),
    (0.18, 18),
    (0.25, 25),
    (0.35, 35),
    (0.50, 50),
    (0.70, 70),
    (1.00, 100),
    (1.40, 140),
    (2.00, 200),
    # Already-hundredths ints (v > 2.05)
    (25, 25),
    (50, 50),
    (100, 100),
    (200, 200),
    # Ambiguous bare ints (<= 2.05) — the P0 truncation hazard: treated as mm,
    # NOT as hundredths (NEW-test-com-1). 2 -> 2.00mm -> 200, 1 -> 1.00mm -> 100.
    (1, 100),
    (2, 200),
    # Sentinels: -1 = ByLayer, -2 = ByBlock, -3 = Default
    (-1, -1),
    (-2, -2),
    (-3, -3),
    # Zero passthrough
    (0, 0),
    # None passthrough
    (None, None),
    # String-encoded floats are coerced
    ("0.25", 25),
    ("50", 50),
]


@pytest.mark.parametrize("input_val, expected", NL_TABLE)
def test_normalize_lineweight(input_val, expected):
    assert normalize_lineweight(input_val) == expected


def test_normalize_lineweight_boundary_2_05():
    """2.00 is treated as mm; 2.06 is treated as hundredths."""
    assert normalize_lineweight(2.00) == 200
    assert normalize_lineweight(2.06) == 2  # 2.06 rounds to 2 hundredths


def test_normalize_lineweight_non_numeric_passthrough():
    """Non-numeric exotic values pass through unchanged (don't raise)."""
    result = normalize_lineweight("ByLayer")  # type: ignore[arg-type]
    assert result == "ByLayer"


# ---------------------------------------------------------------------------
# _safe_send_command — CMDACTIVE polling / timeout / var-restore
# ---------------------------------------------------------------------------


def _make_mock_doc(
    cmdactive_sequence: list[int],
    modelspace_handles_pre: set[str] | None = None,
    modelspace_handles_post: set[str] | None = None,
) -> MagicMock:
    """Build a minimal mock AutoCAD document for _safe_send_command tests."""
    doc = MagicMock()

    # Simulate CMDACTIVE returning successive values on each GetVariable("CMDACTIVE") call.
    # Other variables (OSMODE, SNAPMODE, CMDECHO) return 0 by default.
    getvar_calls: list[int] = list(cmdactive_sequence)

    def _get_variable(name: str):
        if name == "CMDACTIVE":
            if getvar_calls:
                return getvar_calls.pop(0)
            return 0
        return 0  # default for OSMODE, SNAPMODE, CMDECHO

    doc.GetVariable.side_effect = _get_variable
    doc.SetVariable.return_value = None
    doc.SendCommand.return_value = None

    # Modelspace iteration for handle snapshotting
    pre = modelspace_handles_pre or set()
    post = modelspace_handles_post or set()
    call_count: list[int] = [0]

    def _iter_modelspace():
        call_count[0] += 1
        handles = pre if call_count[0] == 1 else post
        for h in handles:
            entity = MagicMock()
            entity.Handle = h
            yield entity

    doc.ModelSpace.__iter__ = lambda _: _iter_modelspace()
    return doc


def test_safe_send_command_immediate_return():
    """CMDACTIVE=0 immediately → no polling loop, returns empty new-handles list."""
    doc = _make_mock_doc(cmdactive_sequence=[0])
    result = ComBackend._safe_send_command(doc, "ZOOM E\n")
    assert result == []
    doc.SendCommand.assert_called_once()


def test_safe_send_command_waits_for_active_to_clear():
    """CMDACTIVE=1, 1, 0 → polls twice before returning."""
    doc = _make_mock_doc(cmdactive_sequence=[1, 1, 0])
    with patch("time.sleep"):  # don't actually sleep
        result = ComBackend._safe_send_command(doc, "REGEN\n")
    assert result == []


def test_safe_send_command_restores_variables_on_success():
    """OSMODE/SNAPMODE/CMDECHO are saved and restored even when command succeeds."""
    saved_osmode = 4159  # typical user OSMODE

    doc = MagicMock()
    doc.SendCommand.return_value = None

    def _get_variable(name: str):
        if name == "CMDACTIVE":
            return 0
        if name == "OSMODE":
            return saved_osmode
        return 0

    doc.GetVariable.side_effect = _get_variable
    doc.ModelSpace.__iter__ = lambda _: iter([])

    ComBackend._safe_send_command(doc, "ZOOM E\n")

    # SetVariable should have been called at least once with OSMODE restored to saved value
    set_calls = [c for c in doc.SetVariable.call_args_list if c[0][0] == "OSMODE"]
    assert any(c[0][1] == saved_osmode for c in set_calls), (
        f"OSMODE {saved_osmode} not restored; SetVariable calls: {set_calls}"
    )


def test_safe_send_command_restores_variables_on_timeout():
    """Variables are restored even when the command times out (RuntimeError)."""
    saved_osmode = 4159
    doc = MagicMock()
    doc.SendCommand.return_value = None

    def _get_variable(name: str):
        if name == "CMDACTIVE":
            return 1  # never clears → will time out
        if name == "OSMODE":
            return saved_osmode
        return 0

    doc.GetVariable.side_effect = _get_variable
    doc.ModelSpace.__iter__ = lambda _: iter([])

    with patch("time.sleep"), patch("time.monotonic", side_effect=[0.0, 0.0, 999.0]):
        with pytest.raises(RuntimeError, match="did not finish"):
            ComBackend._safe_send_command(doc, "BAD_CMD\n", deadline_s=1.0)

    # OSMODE must still be restored
    set_calls = [c for c in doc.SetVariable.call_args_list if c[0][0] == "OSMODE"]
    assert any(c[0][1] == saved_osmode for c in set_calls)


def test_safe_send_command_timeout_sends_esc():
    """On timeout, three ESC sequences are sent to unblock the command prompt."""
    doc = MagicMock()
    doc.SendCommand.return_value = None
    doc.GetVariable.side_effect = lambda name: 1 if name == "CMDACTIVE" else 0
    doc.ModelSpace.__iter__ = lambda _: iter([])

    with patch("time.sleep"), patch("time.monotonic", side_effect=[0.0, 0.0, 999.0]):
        with pytest.raises(RuntimeError):
            ComBackend._safe_send_command(doc, "BAD\n", deadline_s=1.0)

    # Second SendCommand call should be the ESC sequence
    calls = doc.SendCommand.call_args_list
    assert len(calls) >= 2
    assert "\x1b" in calls[-1][0][0]


def test_safe_send_command_new_handle_detection():
    """Handles created during the command appear in the return value."""
    pre_handles = {"1A", "1B"}
    post_handles = {"1A", "1B", "1C", "1D"}
    doc = _make_mock_doc(
        cmdactive_sequence=[0],
        modelspace_handles_pre=pre_handles,
        modelspace_handles_post=post_handles,
    )
    result = ComBackend._safe_send_command(doc, "LINE\n")
    assert set(result) == {"1C", "1D"}


# ---------------------------------------------------------------------------
# dimension_linear — must use AddDimRotated (GH issue #3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dimension_linear_uses_add_dim_rotated():
    """Regression for GH issue #3: the AutoCAD ActiveX API has no AddDimLinear
    method — calling it raised ``<unknown>.AddDimLinear``. Linear dimensions
    must go through AddDimRotated."""
    backend = ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    backend._run = _run_inline  # bypass the COM executor thread

    mspace = MagicMock()
    with (
        patch("backends.com_backend._msp", return_value=mspace),
        patch("backends.com_backend._apoint", side_effect=lambda *a: tuple(float(v) for v in a)),
        patch("backends.com_backend._regen"),
        patch("backends.com_backend._apply_dim_tolerance"),
        patch("backends.com_backend._entity_info", side_effect=lambda e: e),
    ):
        await backend.dimension_linear(0, 0, 100, 0, 50, 20, rotation=0.0)

    mspace.AddDimRotated.assert_called_once()
    mspace.AddDimLinear.assert_not_called()
    args = mspace.AddDimRotated.call_args[0]
    assert args[0] == (0.0, 0.0)  # ext line 1 origin
    assert args[1] == (100.0, 0.0)  # ext line 2 origin
    assert args[2] == (50.0, 20.0)  # dim line location
    assert args[3] == pytest.approx(0.0)  # rotation in radians


# ---------------------------------------------------------------------------
# Live integration — skipped unless AutoCAD is reachable
# ---------------------------------------------------------------------------

_SKIP_LIVE = pytest.mark.skipif(
    sys.platform != "win32",
    reason="COM integration tests require Windows + AutoCAD",
)


@_SKIP_LIVE
@pytest.mark.asyncio
async def test_com_backend_connects():
    """Smoke test: connect and disconnect without errors."""
    backend = ComBackend()
    await backend.connect()
    assert backend.is_connected
    await backend.disconnect()
    assert not backend.is_connected


# ── entity_measure: a good .Area must not be thrown away ────────────────────
#
# The fallback fired when `.Area` OR `.Length` was missing, and then demanded
# `.Coordinates`, which only the polyline types carry. So HATCH, REGION and
# 3DSOLID — the very types the README sells the COM backend on — discarded a
# successfully-read area and raised "AutoCAD reported neither an area nor
# usable vertices", which was false: it had the area.
#
# These profiles are ASSUMPTIONS about the ActiveX member table, not facts, and
# they are what a live seat has to confirm. What they do test is our dispatch,
# which is where the defect is: given a member profile, does the code keep the
# number it already has?


@pytest.fixture(autouse=True)
def _restore_acad_doc():
    """`_measure_backend` swaps the module-level `_acad_doc`; put it back after
    every test here. Left in place, the MagicMock document leaked into every
    later test of the session that resolves the document itself (the settings
    facade's COM tests read sysvars through `_acad_doc()`)."""
    import backends.com_backend as cb

    original = cb._acad_doc
    yield
    cb._acad_doc = original


def _measure_backend(entity):
    """A ComBackend whose HandleToObject returns exactly this entity."""
    import backends.com_backend as cb

    backend = ComBackend()

    async def _run(func, *args, **kwargs):  # bypass the STA executor
        return func(*args, **kwargs)

    backend._run = _run
    doc = MagicMock()
    doc.HandleToObject.return_value = entity
    cb._acad_doc = lambda: doc  # restored by `_restore_acad_doc`
    return backend


class _Entity:
    """An ActiveX entity that has only the members it is given."""

    def __init__(self, object_name, **members):
        self.ObjectName = object_name
        for name, value in members.items():
            setattr(self, name, value)

    def __getattr__(self, name):  # anything not set simply does not exist
        raise AttributeError(name)


@pytest.mark.asyncio
async def test_a_hatch_with_an_area_and_no_length_keeps_the_area():
    """The red case: today this raises, holding a perfectly good 300.0."""
    backend = _measure_backend(_Entity("AcDbHatch", Area=300.0))

    result = await backend.entity_measure("2AF")

    assert result["area"] == pytest.approx(300.0)
    assert result["method"] == "activex_area"


@pytest.mark.asyncio
async def test_a_solid_without_a_perimeter_says_so_instead_of_inventing_one():
    backend = _measure_backend(_Entity("AcDb3dSolid", Area=1250.0))

    result = await backend.entity_measure("3B0")

    assert result["area"] == pytest.approx(1250.0)
    assert result["perimeter"] is None
    assert result["perimeter_exact"] is False


@pytest.mark.asyncio
async def test_the_polyline_fallback_still_fires_when_the_area_is_the_missing_one():
    """The fallback exists for a reason and must keep working."""
    entity = _Entity("AcDbPolyline", Coordinates=(0.0, 0.0, 10.0, 0.0, 10.0, 10.0, 0.0, 10.0))
    entity.GetBulge = lambda index: 0.0
    entity.Closed = True
    backend = _measure_backend(entity)

    result = await backend.entity_measure("4C1")

    assert result["method"] == "activex_fallback_analytic"
    assert result["area"] == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_the_refusal_is_only_used_when_there_is_genuinely_nothing():
    """The message names both halves, so it must only appear when both are true."""
    backend = _measure_backend(_Entity("AcDbSpline"))

    with pytest.raises(RuntimeError, match="neither an area nor usable vertices"):
        await backend.entity_measure("5D2")


@pytest.mark.asyncio
async def test_a_line_is_still_refused_before_any_of_this():
    backend = _measure_backend(_Entity("AcDbLine", Area=0.0))

    with pytest.raises(RuntimeError, match="does not bound an area"):
        await backend.entity_measure("6E3")


@pytest.mark.asyncio
async def test_a_live_bowtie_is_flagged_even_though_activex_answered():
    """`area: 0.0` with no warning is the exact failure this field prevents.

    A bowtie LWPOLYLINE has both `.Area` (which AutoCAD returns as the
    shoelace, cancelling the crossed lobes to ~0) and `.Length`, so it never
    reached the fallback where `self_intersecting` used to be computed.
    """
    entity = _Entity(
        "AcDbPolyline",
        Area=0.0,
        Length=200.0,
        Coordinates=(0.0, 0.0, 50.0, 50.0, 50.0, 0.0, 0.0, 50.0),
    )
    backend = _measure_backend(entity)

    result = await backend.entity_measure("7F4")

    assert result["method"] == "activex_area"
    assert result["self_intersecting"] is True


@pytest.mark.asyncio
async def test_a_simple_polyline_is_not_flagged():
    entity = _Entity(
        "AcDbPolyline",
        Area=2500.0,
        Length=200.0,
        Coordinates=(0.0, 0.0, 50.0, 0.0, 50.0, 50.0, 0.0, 50.0),
    )
    backend = _measure_backend(entity)

    assert (await backend.entity_measure("8A5"))["self_intersecting"] is False


# ── the member profile, measured on a live AutoCAD 2026 (2026-08-06) ────────
#
# Probed on a fresh seat with a throwaway drawing, nothing saved:
#   AcDbCircle    Area 314.1593  Circumference 62.8319   (no Length, no Coordinates)
#   AcDbPolyline  Area 1600.0    Length 160.0            Coordinates present
#   AcDbHatch     Area 942.4778  -- r20 with an r10 island, so pi*(400-100):
#                                   AutoCAD's hatch area DOES subtract islands
#   AcDbRegion    Area 1600.0    Perimeter 160.0
#   AcDb3dSolid   Area ABSENT    Volume 1000.0
#
# The last row is the one that matters: ActiveX exposes no `.Area` on a 3DSOLID
# at all, so "REGION / 3DSOLID area" was half a claim.


@pytest.mark.asyncio
async def test_a_circle_uses_circumference_for_its_perimeter():
    """Measured: AcDbCircle has `.Circumference` and no `.Length`."""
    backend = _measure_backend(_Entity("AcDbCircle", Area=314.1593, Circumference=62.8319))

    result = await backend.entity_measure("C01")

    assert result["area"] == pytest.approx(314.1593)
    assert result["perimeter"] == pytest.approx(62.8319)
    assert result["perimeter_exact"] is True


@pytest.mark.asyncio
async def test_a_region_uses_perimeter():
    """Measured: AcDbRegion has `.Perimeter` and no `.Length`."""
    backend = _measure_backend(_Entity("AcDbRegion", Area=1600.0, Perimeter=160.0))

    result = await backend.entity_measure("R01")

    assert result["area"] == pytest.approx(1600.0)
    assert result["perimeter"] == pytest.approx(160.0)


@pytest.mark.asyncio
async def test_a_3dsolid_is_refused_by_name_because_activex_has_no_area_for_it():
    """Measured: AcDb3dSolid exposes Volume and no Area at all.

    The generic "neither an area nor usable vertices" would send the reader
    hunting for geometry that was never the problem.
    """
    backend = _measure_backend(_Entity("AcDb3dSolid", Volume=1000.0))

    with pytest.raises(RuntimeError, match="no surface-area member"):
        await backend.entity_measure("S01")


# ── geometry_bbox on a block reference: the drawn body, not its tag ─────────
#
# ``GetBoundingBox`` on an INSERT takes the ATTRIBs with it, so a TAG lettered
# above a valve pushes the box past the body and a line ending on the body's
# real edge reads as *inside* the box. The definition's members are read in
# block space, attribute definitions skipped, and carried through the INSERT.


class _Block:
    """An AcadBlock: ``Count`` members reachable by ``Item(i)``."""

    def __init__(self, members, origin=(0.0, 0.0, 0.0)):
        self._members = members
        self.Count = len(members)
        self.Origin = origin

    def Item(self, i):
        return self._members[i]


def _blockref(name, blocks, insertion=(0.0, 0.0, 0.0), sx=1.0, sy=1.0, rotation=0.0, **extra):
    doc = SimpleNamespace(Blocks=SimpleNamespace(Item=lambda n: blocks[n]))
    return _Entity(
        "AcDbBlockReference",
        Name=name,
        Document=doc,
        InsertionPoint=insertion,
        XScaleFactor=sx,
        YScaleFactor=sy,
        ZScaleFactor=1.0,
        Rotation=rotation,
        Handle="B1",
        Layer="0",
        Color=256,
        Linetype="ByLayer",
        Visible=True,
        **extra,
    )


def _gate_valve_block():
    body_left = _Entity("AcDbPolyline", GetBoundingBox=lambda: ((-4.0, -2.0, 0.0), (0.0, 2.0, 0.0)))
    body_right = _Entity("AcDbPolyline", GetBoundingBox=lambda: ((0.0, -2.0, 0.0), (4.0, 2.0, 0.0)))
    tag = _Entity(
        "AcDbAttributeDefinition",
        GetBoundingBox=lambda: ((0.0, 4.0, 0.0), (10.75, 6.5, 0.0)),
    )
    return _Block([body_left, body_right, tag])


def test_com_geometry_bbox_is_the_drawn_body_without_the_attribute():
    from backends.com_backend import _entity_info

    ent = _blockref(
        "GATE_VALVE",
        {"GATE_VALVE": _gate_valve_block()},
        insertion=(50.0, 50.0, 0.0),
        # what ActiveX answers for the INSERT itself: the TAG is in it
        GetBoundingBox=lambda: ((46.0, 48.0, 0.0), (60.75, 56.5, 0.0)),
    )
    props = _entity_info(ent).properties
    assert props["bounding_box"] == {"min": [46.0, 48.0], "max": [60.75, 56.5]}
    assert props["geometry_bbox"]["min"] == pytest.approx([46.0, 48.0])
    assert props["geometry_bbox"]["max"] == pytest.approx([54.0, 52.0])
    assert props["block_name"] == "GATE_VALVE" and props["insertion"] == [50.0, 50.0]


def test_com_geometry_bbox_follows_the_inserts_scale_rotation_and_block_origin():
    import math

    from backends.com_backend import _com_geometry_bbox

    # scale 2 and a quarter turn: body x∈[-4,4] y∈[-2,2] → x∈[-4,4] y∈[-8,8]
    ent = _blockref(
        "GATE_VALVE",
        {"GATE_VALVE": _gate_valve_block()},
        insertion=(10.0, 20.0, 0.0),
        sx=2.0,
        sy=2.0,
        rotation=math.pi / 2,
    )
    box = _com_geometry_bbox(ent)
    assert box["min"] == pytest.approx([6.0, 12.0]) and box["max"] == pytest.approx([14.0, 28.0])
    # a mirrored INSERT (YScaleFactor -1) keeps the same extents for a symmetric body
    ent = _blockref("GATE_VALVE", {"GATE_VALVE": _gate_valve_block()}, sy=-1.0)
    box = _com_geometry_bbox(ent)
    assert box["min"] == pytest.approx([-4.0, -2.0]) and box["max"] == pytest.approx([4.0, 2.0])
    # the block's base point is what lands on the insertion point
    blk = _Block(
        [_Entity("AcDbLine", GetBoundingBox=lambda: ((10.0, 10.0, 0.0), (20.0, 10.0, 0.0)))],
        origin=(10.0, 10.0, 0.0),
    )
    ent = _blockref("OFFSET", {"OFFSET": blk}, insertion=(100.0, 100.0, 0.0))
    box = _com_geometry_bbox(ent)
    assert box["min"] == pytest.approx([100.0, 100.0]) and box["max"] == pytest.approx(
        [110.0, 100.0]
    )


def test_com_geometry_bbox_is_omitted_when_the_block_draws_nothing_or_activex_refuses():
    from backends.com_backend import _com_geometry_bbox, _entity_info

    only_tag = _Block(
        [
            _Entity(
                "AcDbAttributeDefinition", GetBoundingBox=lambda: ((0.0, 4.0, 0.0), (5.0, 6.5, 0.0))
            )
        ]
    )
    assert _com_geometry_bbox(_blockref("TAG_ONLY", {"TAG_ONLY": only_tag})) is None
    props = _entity_info(_blockref("TAG_ONLY", {"TAG_ONLY": only_tag})).properties
    assert "geometry_bbox" not in props and props["block_name"] == "TAG_ONLY"

    def _refuse(_name):
        raise RuntimeError("Key not found")

    ent = _blockref("MISSING", {})
    ent.Document = SimpleNamespace(Blocks=SimpleNamespace(Item=_refuse))
    assert _com_geometry_bbox(ent) is None
    props = _entity_info(ent).properties
    assert "geometry_bbox" not in props and props["rotation_deg"] == 0.0


# ── geometry_bbox off a right angle: the geometry, not the box of a box ──────
#
# Rotating a member's axis-aligned box and taking the extents of the corners
# is exact only at multiples of 90 degrees. A circle of radius 5 turned 45
# degrees is still x in [-5, 5]; its box's corners reach 7.07. The ezdxf
# engine measures the real geometry, so the live engine must too — or say
# that it could not.


def _circle(cx, cy, r):
    return _Entity(
        "AcDbCircle",
        Center=(cx, cy, 0.0),
        Radius=r,
        GetBoundingBox=lambda: ((cx - r, cy - r, 0.0), (cx + r, cy + r, 0.0)),
    )


def _line(x0, y0, x1, y1):
    return _Entity(
        "AcDbLine",
        StartPoint=(x0, y0, 0.0),
        EndPoint=(x1, y1, 0.0),
        GetBoundingBox=lambda: ((min(x0, x1), min(y0, y1), 0.0), (max(x0, x1), max(y0, y1), 0.0)),
    )


def _arc(cx, cy, r, start_deg, end_deg):
    import math

    return _Entity(
        "AcDbArc",
        Center=(cx, cy, 0.0),
        Radius=r,
        StartAngle=math.radians(start_deg),
        EndAngle=math.radians(end_deg),
        GetBoundingBox=lambda: ((cx - r, cy - r, 0.0), (cx + r, cy + r, 0.0)),
    )


def _polyline(points, bulges, closed=False):
    import math

    coords = [c for p in points for c in p]
    chord = sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))
    if closed:
        chord += math.dist(points[-1], points[0])
    length = chord + (1.0 if any(bulges) else 0.0)  # "longer than its chord" is all that matters
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return _Entity(
        "AcDbPolyline",
        Coordinates=coords,
        Closed=closed,
        Length=length,
        GetBulge=lambda i: bulges[i],
        GetBoundingBox=lambda: ((min(xs), min(ys), 0.0), (max(xs), max(ys), 0.0)),
    )


def test_com_geometry_bbox_of_a_rotated_circle_and_diagonal_line_is_exact():
    import math

    from backends.com_backend import _com_geometry_bbox

    ent = _blockref(
        "PUMP_C",
        {"PUMP_C": _Block([_circle(0.0, 0.0, 5.0)])},
        insertion=(100.0, 100.0, 0.0),
        rotation=math.radians(45.0),
    )
    box = _com_geometry_bbox(ent)
    assert box["min"] == pytest.approx([95.0, 95.0]) and box["max"] == pytest.approx([105.0, 105.0])
    assert "approximate" not in box
    # a diagonal line turned 45 degrees stands upright: x is exactly 0
    ent = _blockref(
        "DIAG", {"DIAG": _Block([_line(0.0, 0.0, 10.0, 10.0)])}, rotation=math.radians(45.0)
    )
    box = _com_geometry_bbox(ent)
    assert box["min"] == pytest.approx([0.0, 0.0], abs=1e-9)
    assert box["max"] == pytest.approx([0.0, math.sqrt(200.0)], abs=1e-9)
    assert "approximate" not in box


@pytest.mark.parametrize(
    ("rotation_deg", "sx", "sy"),
    [(37.0, 1.0, 1.0), (45.0, 1.5, 1.5), (-120.0, 1.0, -1.0), (37.0, 2.0, -2.0)],
)
def test_com_geometry_bbox_off_a_right_angle_matches_the_ezdxf_engine(rotation_deg, sx, sy):
    """Same block, same INSERT, both engines: a circle, an arc, a bulged
    polyline and a diagonal line, turned and scaled and mirrored."""
    import math

    import ezdxf
    from ezdxf import bbox as _bbox

    from backends.com_backend import _com_geometry_bbox

    doc = ezdxf.new()
    blk = doc.blocks.new("MIXED")
    blk.add_circle((3.0, 0.0), 5.0)
    blk.add_arc((0.0, 8.0), 4.0, 200.0, 330.0)
    blk.add_lwpolyline([(-6, -6, 0.0), (0, -6, 0.8), (0, -12, 0.0)], format="xyb")
    blk.add_line((-2.0, 1.0), (7.0, 9.0))
    blk.add_attdef("TAG", insert=(0, 15), text="", dxfattribs={"height": 2.5})
    ref = doc.modelspace().add_blockref(
        "MIXED",
        (50.0, 20.0),
        dxfattribs={"rotation": rotation_deg, "xscale": sx, "yscale": sy},
    )
    expected = _bbox.extents(
        e for e in ref.virtual_entities() if e.dxftype() not in ("ATTRIB", "ATTDEF")
    )

    members = [
        _circle(3.0, 0.0, 5.0),
        _arc(0.0, 8.0, 4.0, 200.0, 330.0),
        _polyline([(-6.0, -6.0), (0.0, -6.0), (0.0, -12.0)], [0.0, 0.8, 0.0]),
        _line(-2.0, 1.0, 7.0, 9.0),
        _Entity(
            "AcDbAttributeDefinition", GetBoundingBox=lambda: ((0.0, 15.0, 0.0), (8.0, 17.5, 0.0))
        ),
    ]
    ent = _blockref(
        "MIXED",
        {"MIXED": _Block(members)},
        insertion=(50.0, 20.0, 0.0),
        sx=sx,
        sy=sy,
        rotation=math.radians(rotation_deg),
    )
    box = _com_geometry_bbox(ent)
    assert "approximate" not in box
    # ezdxf flattens the curves (default 0.01), so agreement is to that
    assert box["min"] == pytest.approx([expected.extmin.x, expected.extmin.y], abs=0.02)
    assert box["max"] == pytest.approx([expected.extmax.x, expected.extmax.y], abs=0.02)


def test_com_geometry_bbox_is_marked_approximate_when_a_rotated_member_cannot_be_measured():
    import math

    from backends.com_backend import _com_geometry_bbox, _entity_info

    hatch = _Entity("AcDbHatch", GetBoundingBox=lambda: ((-5.0, -5.0, 0.0), (5.0, 5.0, 0.0)))
    blocks = {"FILLED": _Block([_circle(0.0, 0.0, 5.0), hatch])}
    # at a right angle the corners are exact — no marker, no geometry reads
    ent = _blockref("FILLED", blocks, insertion=(100.0, 100.0, 0.0), rotation=math.pi)
    box = _com_geometry_bbox(ent)
    assert box == {"min": [95.0, 95.0], "max": [105.0, 105.0]}
    # off one, the hatch is carried by its corners and the box says so
    ent = _blockref("FILLED", blocks, insertion=(100.0, 100.0, 0.0), rotation=math.radians(45.0))
    props = _entity_info(ent).properties
    box = props["geometry_bbox"]
    assert box["approximate"] is True
    half = 5.0 * math.sqrt(2.0)
    assert box["min"] == pytest.approx([100.0 - half, 100.0 - half])
    assert box["max"] == pytest.approx([100.0 + half, 100.0 + half])
    # a non-uniformly scaled arc is an elliptical arc: not measured, said so
    ent = _blockref(
        "ARC",
        {"ARC": _Block([_arc(0.0, 0.0, 4.0, 0.0, 90.0)])},
        sx=2.0,
        sy=1.0,
        rotation=math.radians(30.0),
    )
    assert _com_geometry_bbox(ent)["approximate"] is True


# ---------------------------------------------------------------------------
# System variables live on AcadDocument, never on AcadApplication
# ---------------------------------------------------------------------------
#
# Measured on AutoCAD 2026 (25.1s, 2026-09-22, scratch Documents.Add()):
# ``hasattr(app, "GetVariable") is False``, ``hasattr(app, "SetVariable") is
# False``, ``doc.GetVariable("INSUNITS") -> 4``, ``doc.GetVariable("MEASUREMENT")
# -> 1``, ``doc.SetVariable("FILEDIA", 0)`` takes and ``doc.GetVariable`` reads
# it back. Fifteen call sites went through the Application inside a
# ``try/except ... log.debug``, so on a live seat they silently degraded:
# every seat was "metric", ``drawing_info`` reported ``Unknown`` units, AUDIT
# ran with the operator's AUDITCTL. The fakes below model the measured shape
# only — an application with *no* sysvar members — so the old call shape
# cannot pass.
#
# Linetype loading, measured the same day: ``SendCommand("_-LINETYPE _LOAD
# CENTER acadiso.lin\n\n")`` loads nothing with FILEDIA=0 and leaves the
# command at ``Enter an option [?/Create/Load/Set]:`` (LASTPROMPT), which
# makes the seat reject every COM call until ESC; with FILEDIA=1 it opens the
# modal "Select Linetype File" dialog. ``doc.Linetypes.Load("CENTER",
# "acadiso.lin")`` returns None and the table grows; a second call raises
# 'Duplicate record name', an unknown name 'Undefined linetype', a missing
# file 'File system error' (excepinfo descriptions). So the loaders call
# ``Linetypes.Load`` and touch no FILEDIA at all.


class _StrictApp:
    """The ActiveX Application: ``Version``, ``Documents``, ``ActiveDocument``
    and nothing that reads or writes a system variable."""

    Version = "25.1s (LMS Tech)"

    def __init__(self, doc):
        self.ActiveDocument = doc
        self.Documents = SimpleNamespace(Count=1)

    def __getattr__(self, name):
        if name in ("GetVariable", "SetVariable"):
            raise AttributeError(f"AutoCAD.Application.{name}")
        raise AttributeError(name)


class _SysvarDoc:
    """An AcadDocument that records every sysvar read and write."""

    def __init__(self, **variables):
        self.variables = {"CMDACTIVE": 0, "FILEDIA": 1, "MEASUREMENT": 1, "AUDITCTL": 0}
        self.variables.update(variables)
        self.calls: list[tuple] = []
        self.sent: list[str] = []
        self.Name = "Drawing1.dwg"
        self.FullName = ""
        self.Saved = True
        self.Database = SimpleNamespace(Extmin=(0.0, 0.0, 0.0), Extmax=(10.0, 20.0, 0.0))
        self.ModelSpace = SimpleNamespace(Count=3)
        self.ActiveLayout = SimpleNamespace(Block=self.ModelSpace)
        self.Layers = SimpleNamespace(Count=2)
        self.Blocks = SimpleNamespace(Count=2)
        self.Linetypes = _RecordingLinetypes(self.calls)

    def GetVariable(self, name):
        self.calls.append(("GetVariable", name))
        return self.variables[name]

    def SetVariable(self, name, value):
        self.calls.append(("SetVariable", name, value))
        self.variables[name] = value

    def SendCommand(self, cmd):
        self.calls.append(("SendCommand", cmd, self.variables["FILEDIA"]))
        self.sent.append(cmd)


def _com_error(description: str, scode: int):
    """A pywin32 ``com_error`` shaped like AutoCAD's: localised HRESULT text,
    English excepinfo description at index 2 of ``args[2]``."""
    pywintypes = pytest.importorskip("pywintypes")
    return pywintypes.com_error(
        -2147352567,
        "Özel durum oluştu.",
        (0, "AutoCAD.Application", description, "OLE_ERR.CHM", scode, scode),
        None,
    )


class _RecordingLinetypes:
    """``doc.Linetypes`` with the measured ``Load`` failures; the .lin files
    it knows carry CENTER / HIDDEN / PHANTOM only."""

    FILES = {"acadiso.lin", "acad.lin", "C:\\lin\\custom.lin"}
    KNOWN = {"center", "hidden", "phantom"}

    def __init__(self, calls):
        self.names = ["ByLayer", "ByBlock", "Continuous"]
        self._calls = calls

    @property
    def Count(self):
        return len(self.names)

    def Item(self, index):
        return SimpleNamespace(Name=self.names[index])

    def Load(self, name, file):
        self._calls.append(("Linetypes.Load", name, file))
        if file not in self.FILES:
            raise _com_error("File system error", -2145386425)
        if name.lower() in {n.lower() for n in self.names}:
            raise _com_error("Duplicate record name", -2145386405)
        if name.lower() not in self.KNOWN:
            raise _com_error("Undefined linetype", -2145386359)
        self.names.append(name.upper())


@pytest.fixture
def sysvar_seat(monkeypatch):
    """``(backend, doc)``: a ComBackend whose ``_acad_doc`` is a recording
    document and whose ``_acad_app`` refuses every sysvar member."""
    import backends.com_backend as cb

    doc = _SysvarDoc()
    app = _StrictApp(doc)
    monkeypatch.setattr(cb, "_acad_app", lambda: app)
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    backend = ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, doc


def test_ensure_linetype_loaded_reads_measurement_on_the_document_and_calls_load(sysvar_seat):
    import backends.com_backend as cb

    _, doc = sysvar_seat
    cb._ensure_linetype_loaded("CENTER")
    assert doc.calls == [
        ("GetVariable", "MEASUREMENT"),
        ("Linetypes.Load", "CENTER", "acadiso.lin"),
    ], "MEASUREMENT (on the document) picks the .lin; Linetypes.Load does the loading"
    assert "CENTER" in doc.Linetypes.names
    assert doc.sent == [], "no -LINETYPE macro: nothing goes through SendCommand"
    assert doc.variables["FILEDIA"] == 1 and not [c for c in doc.calls if c[0] == "SetVariable"], (
        "Linetypes.Load needs no FILEDIA, so the operator's setting is never touched"
    )
    doc.calls.clear()
    cb._ensure_linetype_loaded("center")
    assert doc.calls == [], "already loaded (any case): no read, no load"


def test_ensure_linetype_loaded_picks_acad_lin_from_the_documents_measurement(sysvar_seat):
    import backends.com_backend as cb

    _, doc = sysvar_seat
    doc.variables["MEASUREMENT"] = 0  # imperial seat
    cb._ensure_linetype_loaded("HIDDEN")
    assert doc.calls[-1] == ("Linetypes.Load", "HIDDEN", "acad.lin")


def test_ensure_linetype_loaded_refuses_when_the_document_cannot_say_its_measurement(sysvar_seat):
    """The old path defaulted a failed MEASUREMENT read to metric; that read
    never worked (wrong object), so every seat was metric. A read the
    document refuses is an error, and nothing is loaded from a guessed file."""
    import backends.com_backend as cb

    _, doc = sysvar_seat
    del doc.variables["MEASUREMENT"]
    with pytest.raises(KeyError):
        cb._ensure_linetype_loaded("PHANTOM")
    assert not [c for c in doc.calls if c[0] == "Linetypes.Load"]


def test_ensure_linetype_loaded_names_the_linetype_and_file_when_load_fails(sysvar_seat):
    import backends.com_backend as cb

    _, doc = sysvar_seat
    with pytest.raises(RuntimeError, match=r"'NOSUCH' from 'acadiso.lin': Undefined linetype"):
        cb._ensure_linetype_loaded("NOSUCH")
    assert "NOSUCH" not in doc.Linetypes.names


@pytest.mark.asyncio
async def test_linetype_load_reads_measurement_on_the_document_and_calls_load(sysvar_seat):
    backend, doc = sysvar_seat
    result = await backend.linetype_load("CENTER")
    assert result == {"ok": True, "name": "CENTER", "file": "acadiso.lin"}
    assert doc.calls == [
        ("GetVariable", "MEASUREMENT"),
        ("Linetypes.Load", "CENTER", "acadiso.lin"),
    ]
    assert doc.sent == [] and doc.variables["FILEDIA"] == 1
    again = await backend.linetype_load("center")
    assert again == {"ok": True, "name": "center", "already_loaded": True}


@pytest.mark.asyncio
async def test_linetype_load_with_an_explicit_file_reads_no_measurement(sysvar_seat, monkeypatch):
    import backends.com_backend as cb

    backend, doc = sysvar_seat
    monkeypatch.setattr(cb, "validate_path", lambda p: p)  # the allowlist is tested elsewhere
    result = await backend.linetype_load("HIDDEN", "C:\\lin\\custom.lin")
    assert result == {"ok": True, "name": "HIDDEN", "file": "C:\\lin\\custom.lin"}
    assert doc.calls == [("Linetypes.Load", "HIDDEN", "C:\\lin\\custom.lin")]


@pytest.mark.asyncio
async def test_linetype_load_reports_an_unknown_name_and_a_missing_file_by_name(
    sysvar_seat, monkeypatch
):
    import backends.com_backend as cb

    backend, doc = sysvar_seat
    with pytest.raises(RuntimeError, match=r"'NOSUCH' from 'acadiso.lin': Undefined linetype"):
        await backend.linetype_load("NOSUCH")
    monkeypatch.setattr(cb, "validate_path", lambda p: p)
    with pytest.raises(RuntimeError, match=r"'CENTER' from 'C:\\lin\\nope.lin': File system error"):
        await backend.linetype_load("CENTER", "C:\\lin\\nope.lin")
    assert doc.Linetypes.names == ["ByLayer", "ByBlock", "Continuous"]


@pytest.mark.asyncio
async def test_linetype_load_reports_a_load_that_returned_but_did_not_take(sysvar_seat):
    backend, doc = sysvar_seat
    doc.Linetypes.Load = lambda name, file: doc.calls.append(("Linetypes.Load", name, file))
    with pytest.raises(RuntimeError, match="Failed to load linetype 'CENTER' from 'acadiso.lin'"):
        await backend.linetype_load("CENTER")


@pytest.mark.asyncio
async def test_drawing_info_reports_the_documents_insunits(sysvar_seat):
    backend, doc = sysvar_seat
    doc.variables["INSUNITS"] = 4
    info = await backend.drawing_info()
    assert info.units == "mm"
    assert ("GetVariable", "INSUNITS") in doc.calls
    assert info.version == "25.1s (LMS Tech)" and info.backend == "com"
    doc.variables["INSUNITS"] = 1
    assert (await backend.drawing_info()).units == "Inches"


@pytest.mark.asyncio
async def test_drawing_info_units_are_unknown_only_when_the_document_cannot_say(sysvar_seat):
    """An INSUNITS code outside the map is ``Unknown``; a read the document
    refuses is ``Unknown`` too, but that is the document's failure, never the
    Application's missing member."""
    backend, doc = sysvar_seat
    doc.variables["INSUNITS"] = 14  # microinches: not in the map
    assert (await backend.drawing_info()).units == "Unknown"
    del doc.variables["INSUNITS"]
    assert (await backend.drawing_info()).units == "Unknown"


@pytest.mark.asyncio
async def test_drawing_audit_sets_auditctl_on_the_document_and_restores_it(sysvar_seat):
    backend, doc = sysvar_seat
    result = await backend.drawing_audit()
    assert result["ok"] is True and result["repaired"] is None
    assert doc.calls == [
        ("GetVariable", "AUDITCTL"),
        ("SetVariable", "AUDITCTL", 1),
        ("SendCommand", "_AUDIT Y\n", 1),
        ("SetVariable", "AUDITCTL", 0),
    ], "AUDIT runs with the log on and the operator's AUDITCTL comes back"
    assert doc.variables["AUDITCTL"] == 0


@pytest.mark.asyncio
async def test_drawing_audit_restores_auditctl_when_the_command_raises(sysvar_seat):
    backend, doc = sysvar_seat

    def _boom(cmd):
        raise RuntimeError("SendCommand rejected")

    doc.SendCommand = _boom
    with pytest.raises(RuntimeError, match="rejected"):
        await backend.drawing_audit()
    assert doc.variables["AUDITCTL"] == 0
    assert doc.calls[-1] == ("SetVariable", "AUDITCTL", 0)
