"""Centre marks (ISO 128-23), the cutting-plane line (ISO 128-40) and material hatch.

Pure geometry asserted as emitted primitive tuples; the async layer against a
real ezdxf document through the `backend` fixture.
"""

from __future__ import annotations

import pytest

from engineering.mech.marks import (
    CENTRE_STYLES,
    centre_mark_prims,
)
from engineering.mech.primitives import Line

pytestmark = pytest.mark.asyncio

EPS = 1e-9


def _flat(points):
    """Flatten a point sequence: pytest.approx refuses nested structures."""
    return tuple(float(value) for point in points for value in point)


async def test_a_centre_mark_is_a_small_cross_on_the_centre_role():
    prims = centre_mark_prims((10.0, 20.0), 5.0, style="mark", extension=3.0)
    assert len(prims) == 2
    horizontal, vertical = prims
    assert isinstance(horizontal, Line) and isinstance(vertical, Line)
    assert horizontal.p1 == pytest.approx((7.0, 20.0), abs=EPS)
    assert horizontal.p2 == pytest.approx((13.0, 20.0), abs=EPS)
    assert vertical.p1 == pytest.approx((10.0, 17.0), abs=EPS)
    assert vertical.p2 == pytest.approx((10.0, 23.0), abs=EPS)
    assert horizontal.role == "center" and vertical.role == "center"


async def test_centre_lines_cross_the_circle_and_overrun_it_by_the_extension():
    prims = centre_mark_prims((10.0, 20.0), 5.0, style="lines", extension=3.0)
    horizontal, vertical = prims
    assert horizontal.p1 == pytest.approx((2.0, 20.0), abs=EPS)
    assert horizontal.p2 == pytest.approx((18.0, 20.0), abs=EPS)
    assert vertical.p1 == pytest.approx((10.0, 12.0), abs=EPS)
    assert vertical.p2 == pytest.approx((10.0, 28.0), abs=EPS)
    assert horizontal.role == "center" and vertical.role == "center"


async def test_the_style_enum_is_closed_and_a_bad_radius_is_refused():
    assert CENTRE_STYLES == ("mark", "lines")
    with pytest.raises(ValueError) as excinfo:
        centre_mark_prims((0.0, 0.0), 5.0, style="cross")
    assert "mark, lines" in str(excinfo.value)
    with pytest.raises(ValueError) as excinfo:
        centre_mark_prims((0.0, 0.0), 0.0, style="lines")
    assert "radius" in str(excinfo.value)
    with pytest.raises(ValueError) as excinfo:
        centre_mark_prims((0.0, 0.0), 5.0, extension=0.0)
    assert "extension" in str(excinfo.value)


# ── ISO 128-40 cutting-plane line ───────────────────────────────────────────

from engineering.mech.marks import (  # noqa: E402 - grouped with its own tests
    SECTION_STYLES,
    section_line_prims,
    section_plane,
)
from engineering.mech.primitives import Poly, Text  # noqa: E402


async def test_the_cutting_plane_line_is_thick_at_the_ends_and_thin_between():
    prims = section_line_prims(
        (0.0, 0.0), (100.0, 0.0), label="A", direction=(0.0, -1.0), height=5.0
    )
    head_end, tail_end, middle = prims[0], prims[1], prims[2]
    assert head_end.p1 == pytest.approx((0.0, 0.0), abs=EPS)
    assert head_end.p2 == pytest.approx((10.0, 0.0), abs=EPS)
    assert head_end.role == "visible"
    assert tail_end.p1 == pytest.approx((90.0, 0.0), abs=EPS)
    assert tail_end.p2 == pytest.approx((100.0, 0.0), abs=EPS)
    assert tail_end.role == "visible"
    assert middle.p1 == pytest.approx((10.0, 0.0), abs=EPS)
    assert middle.p2 == pytest.approx((90.0, 0.0), abs=EPS)
    assert middle.role == "center"


async def test_a_short_cutting_plane_is_one_wide_segment_with_no_thin_middle():
    prims = section_line_prims((0.0, 0.0), (15.0, 0.0), height=5.0)
    assert prims[0].p1 == pytest.approx((0.0, 0.0), abs=EPS)
    assert prims[0].p2 == pytest.approx((15.0, 0.0), abs=EPS)
    assert prims[0].role == "visible"
    assert not [p for p in prims if isinstance(p, Line) and p.role == "center"]


async def test_each_end_gets_an_arrow_pointing_the_viewing_direction_and_the_same_letter():
    prims = section_line_prims(
        (0.0, 0.0), (100.0, 0.0), label="B", direction=(0.0, -1.0), height=5.0
    )
    shafts = [p for p in prims if isinstance(p, Line) and p.p1[0] == p.p2[0]]
    assert len(shafts) == 2
    # The shaft comes from outside and the head's tip sits on the line end.
    assert shafts[0].p1 == pytest.approx((0.0, 10.0), abs=EPS)
    assert shafts[0].p2 == pytest.approx((0.0, 0.0), abs=EPS)
    assert shafts[1].p1 == pytest.approx((100.0, 10.0), abs=EPS)
    assert shafts[1].p2 == pytest.approx((100.0, 0.0), abs=EPS)
    heads = [p for p in prims if isinstance(p, Poly)]
    assert len(heads) == 2 and all(h.closed for h in heads)
    assert _flat(heads[0].points) == pytest.approx((0.0, 0.0, 1.4, 4.0, -1.4, 4.0), abs=EPS)
    labels = [p for p in prims if isinstance(p, Text)]
    assert [t.text for t in labels] == ["B", "B"]
    assert labels[0].at == pytest.approx((0.0, 13.5), abs=EPS)
    assert labels[1].at == pytest.approx((100.0, 13.5), abs=EPS)
    assert all(t.height == 5.0 and t.role == "text" for t in labels)


async def test_a_viewing_direction_parallel_to_the_plane_is_refused():
    with pytest.raises(ValueError) as excinfo:
        section_line_prims((0.0, 0.0), (100.0, 0.0), direction=(1.0, 0.0))
    assert "perpendicular" in str(excinfo.value)
    with pytest.raises(ValueError) as excinfo:
        section_line_prims((0.0, 0.0), (0.0, 0.0), direction=(0.0, -1.0))
    assert "distinct" in str(excinfo.value)


async def test_section_plane_is_exactly_the_dict_the_view_engine_consumes():
    plane = section_plane((0.0, 0.0), (100.0, 0.0), label="A", direction=(0.0, -2.0))
    assert plane == {
        "p1": (0.0, 0.0),
        "p2": (100.0, 0.0),
        "label": "A",
        "direction": (0.0, -1.0),
        "style": "full",
    }
    assert list(plane) == ["p1", "p2", "label", "direction", "style"]
    assert SECTION_STYLES == ("full", "half", "offset", "revolved")
    with pytest.raises(ValueError) as excinfo:
        section_plane((0.0, 0.0), (10.0, 0.0), style="broken")
    assert "full, half, offset, revolved" in str(excinfo.value)
    with pytest.raises(ValueError) as excinfo:
        section_plane((0.0, 0.0), (10.0, 0.0), label="")
    assert "label" in str(excinfo.value)


# ── the async layer ─────────────────────────────────────────────────────────

from engineering.mech.marks import (  # noqa: E402 - grouped with its own tests
    draw_centre_marks,
    draw_material_hatch,
    draw_section_line,
)
from engineering.mech.primitives import ROLE_LAYER  # noqa: E402
from engineering.mech.standards.materials import MATERIALS, hatch_for  # noqa: E402


async def test_centre_marks_read_the_circles_real_radius_from_the_drawing(backend):
    small = await backend.entity_create_circle(0.0, 0.0, 4.0, "GEOMETRY")
    large = await backend.entity_create_circle(50.0, 0.0, 12.0, "GEOMETRY")
    result = await draw_centre_marks(
        backend, handles=[small.handle, large.handle], style="lines", extension=2.0
    )
    assert result["ok"] is True and result["count"] == 4
    assert result["sources"] == [
        {"handle": small.handle, "center": [0.0, 0.0], "radius": 4.0},
        {"handle": large.handle, "center": [50.0, 0.0], "radius": 12.0},
    ]
    first = await backend.entity_get(result["handles"][0])
    assert first.layer == ROLE_LAYER["center"]
    assert first.properties["start"] == pytest.approx([-6.0, 0.0], abs=EPS)
    assert first.properties["end"] == pytest.approx([6.0, 0.0], abs=EPS)
    third = await backend.entity_get(result["handles"][2])
    assert third.properties["start"] == pytest.approx([36.0, 0.0], abs=EPS)
    assert third.properties["end"] == pytest.approx([64.0, 0.0], abs=EPS)


async def test_centre_marks_refuse_a_handle_that_is_not_a_circular_feature(backend):
    line = await backend.entity_create_line(0.0, 0.0, 10.0, 0.0, 0.0, 0.0, "GEOMETRY")
    before = (await backend.analysis_stats())["total_entities"]
    with pytest.raises(ValueError) as excinfo:
        await draw_centre_marks(backend, handles=[line.handle])
    assert line.handle in str(excinfo.value) and "LINE" in str(excinfo.value)
    assert (await backend.analysis_stats())["total_entities"] == before


async def test_explicit_centres_are_accepted_and_lines_need_a_radius(backend):
    result = await draw_centre_marks(backend, centers=[[5.0, 5.0]], style="mark", extension=3.0)
    assert result["count"] == 2 and result["sources"] == [
        {"handle": None, "center": [5.0, 5.0], "radius": None}
    ]
    with pytest.raises(ValueError) as excinfo:
        await draw_centre_marks(backend, centers=[[5.0, 5.0]], style="lines")
    assert "centers[0]" in str(excinfo.value)
    with pytest.raises(ValueError) as excinfo:
        await draw_centre_marks(backend)
    assert "handles" in str(excinfo.value) and "centers" in str(excinfo.value)


async def test_draw_section_line_returns_the_plane_dict_the_view_engine_consumes(backend):
    result = await draw_section_line(
        backend, (0.0, 0.0), (100.0, 0.0), label="A", direction=(0.0, -1.0), height=5.0
    )
    assert result["ok"] is True
    assert result["plane"] == {
        "p1": (0.0, 0.0),
        "p2": (100.0, 0.0),
        "label": "A",
        "direction": (0.0, -1.0),
        "style": "full",
    }
    assert list(result["plane"]) == ["p1", "p2", "label", "direction", "style"]
    layers = set()
    for handle in result["handles"]:
        layers.add((await backend.entity_get(handle)).layer)
    assert layers == {ROLE_LAYER["visible"], ROLE_LAYER["center"], ROLE_LAYER["text"]}


async def test_material_hatch_uses_the_iso_128_50_map_and_scales_on_top_of_it(backend):
    steel = hatch_for("steel")
    result = await draw_material_hatch(
        backend,
        boundary=[[0.0, 0.0], [40.0, 0.0], [40.0, 20.0], [0.0, 20.0]],
        material="steel",
        scale=2.0,
    )
    assert result["ok"] is True and result["material"] == "steel"
    assert result["pattern"] == steel["pattern"]
    assert result["scale"] == pytest.approx(steel["scale"] * 2.0, abs=EPS)
    assert result["angle"] == pytest.approx(steel["angle"], abs=EPS)
    hatch = backend._doc.entitydb.get(result["handle"])
    assert hatch.dxftype() == "HATCH"
    assert hatch.dxf.pattern_name == steel["pattern"]
    assert hatch.dxf.pattern_scale == pytest.approx(steel["scale"] * 2.0, abs=EPS)
    assert hatch.dxf.pattern_angle == pytest.approx(steel["angle"], abs=EPS)
    assert (await backend.entity_get(result["handle"])).layer == ROLE_LAYER["hatch"]
    # An explicit angle overrides the material's own.
    turned = await draw_material_hatch(
        backend,
        boundary=[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]],
        material="steel",
        angle=90.0,
    )
    assert turned["angle"] == pytest.approx(90.0, abs=EPS)


async def test_an_unknown_material_is_refused_with_the_list_before_any_write(backend):
    assert "steel" in MATERIALS
    before = (await backend.analysis_stats())["total_entities"]
    with pytest.raises(ValueError):
        await draw_material_hatch(
            backend,
            boundary=[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]],
            material="unobtainium",
        )
    assert (await backend.analysis_stats())["total_entities"] == before


async def test_a_boundary_that_is_not_a_closed_polygon_is_refused(backend):
    with pytest.raises(ValueError) as excinfo:
        await draw_material_hatch(backend, boundary=[[0.0, 0.0], [10.0, 0.0]], material="steel")
    assert "at least three" in str(excinfo.value)
    with pytest.raises(ValueError) as excinfo:
        await draw_material_hatch(backend, material="steel")
    assert "boundary" in str(excinfo.value) and "handles" in str(excinfo.value)


# ── the HATCH layer must exist before the hatch is created (live) ────────────

import types  # noqa: E402


class _StrictLayerHatchBackend:
    """A backend that refuses a hatch on a layer the drawing does not have.

    MEASURED on AutoCAD 2026 (probe: GetActiveObject, `Documents.Add()`, a
    scratch document whose layer table is `['0']`, `doc.Close(False)` in a
    `finally:`): `msp.AddHatch(0, "ANSI31", False)` + `AppendOuterLoop` +
    `Evaluate()` leaves the AcDbHatch in model space, and the very next
    statement `hatch.Layer = "HATCH"` raises
    `com_error (-2147352567, ..., 'Key not found', ..., -2145386476)`. Model
    space then still reports `[('AcDbHatch', '0')]` and the layer table is
    still `['0']` - the refusal leaves an orphan hatch behind. After
    `doc.Layers.Add("HATCH")` the same assignment succeeds. `ComBackend`
    reaches that assignment through `_apply_entity_attrs`, which is the one
    line `entity.Layer = layer`, so this is the hatch spelling of the failure
    `ensure_annotation_layers` already fixed for the annotation symbols.

    The fake models that measured refusal and nothing else: every member below
    exists on the real `AutoCADBackend` with this signature.
    """

    def __init__(self, layers=("0",)):
        self.layers = {name.lower(): name for name in layers}
        self.calls: list[tuple] = []

    async def layer_list(self):
        self.calls.append(("layer_list",))
        return [types.SimpleNamespace(name=name) for name in self.layers.values()]

    async def layer_create(self, name, color=7, linetype="Continuous", lineweight=-3):
        self.calls.append(("layer_create", name, color, linetype, lineweight))
        self.layers[name.lower()] = name
        return types.SimpleNamespace(name=name)

    async def entity_create_hatch(
        self, pattern, boundary_points, scale=1.0, angle=0.0, layer=None, color=None
    ):
        if layer is not None and layer.lower() not in self.layers:
            raise RuntimeError(f"AutoCAD COM error (-0x7ffdfff7) ('Key not found'): {layer}")
        self.calls.append(("HATCH", layer, pattern))
        return types.SimpleNamespace(handle="2F")


async def test_the_hatch_layer_is_created_before_the_hatch_is():
    backend = _StrictLayerHatchBackend()
    result = await draw_material_hatch(
        backend,
        boundary=[[0.0, 0.0], [40.0, 0.0], [40.0, 20.0], [0.0, 20.0]],
        material="steel",
    )
    assert result["ok"] is True and result["handle"] == "2F"
    assert result["layers_created"] == [ROLE_LAYER["hatch"]]
    # The layer is created strictly before the hatch entity.
    kinds = [call[0] for call in backend.calls]
    assert kinds.index("layer_create") < kinds.index("HATCH")
    # ENGINEERING_LAYERS' own definition, not a bare default.
    create = next(c for c in backend.calls if c[0] == "layer_create")
    assert create[1] == ROLE_LAYER["hatch"]


async def test_a_drawing_that_already_has_the_hatch_layer_is_not_rewritten():
    backend = _StrictLayerHatchBackend(layers=("0", "hatch"))
    result = await draw_material_hatch(
        backend, boundary=[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]], material="steel"
    )
    assert result["layers_created"] == []
    assert not [c for c in backend.calls if c[0] == "layer_create"]
