"""One DIMENSION per ``dimension_truth`` code, each lying in exactly one way (1.8-15).

``build_lying_dims(backend)`` draws, on a headless backend's current drawing,
seven dimensions far apart from each other, every one drawn to real geometry
(two vertical lines, or a circle) except the unbound one, and returns
``{code: handle}``. Each is made to tell exactly one lie, so the focus must
report exactly that code for it and nothing else:

* ``dim_override_disagrees`` - a "42" override over 40 mm;
* ``dim_dimlfac_scaled`` - DIMLFAC 2 over 21 mm (the sheet says 42);
* ``dim_render_stale`` - DIMDEC 0 written into its DSTYLE override without a
  redraw: the fields say "20", the ``*D`` block still says "19.6";
* ``dim_group42_stale`` - group 42 set to 25 over a 30 mm measurement;
* ``dim_double_prefix`` - a diameter whose override is ``%%c<>`` (ØØ20);
* ``dim_value_hidden`` - an honest dimension on a layer that is off;
* ``dim_unbound`` - an honest-looking dimension drawn over nothing.
"""

from __future__ import annotations

CODES = (
    "dim_override_disagrees",
    "dim_dimlfac_scaled",
    "dim_render_stale",
    "dim_group42_stale",
    "dim_double_prefix",
    "dim_value_hidden",
    "dim_unbound",
)


def _posts(msp, x0: float, x1: float, layer: str = "GEOMETRY") -> None:
    """Two vertical lines whose lower ends are a linear dimension's measured points."""
    msp.add_line((x0, 0.0), (x0, 20.0), dxfattribs={"layer": layer})
    msp.add_line((x1, 0.0), (x1, 20.0), dxfattribs={"layer": layer})


async def build_lying_dims(backend) -> dict[str, str]:
    from ezdxf.entities import DimStyleOverride

    doc = backend._require_doc()
    msp = doc.modelspace()
    for name in ("GEOMETRY", "DIM"):
        if not doc.layers.has_entry(name):
            doc.layers.add(name)
    out: dict[str, str] = {}

    _posts(msp, 0.0, 40.0)
    dim = await backend.dimension_linear(0, 0, 40, 0, 20, -10, text_override="42")
    out["dim_override_disagrees"] = dim.handle

    _posts(msp, 100.0, 121.0)
    scaled = msp.add_linear_dim(
        base=(110, -10), p1=(100, 0), p2=(121, 0), dimstyle="Standard", override={"dimlfac": 2.0}
    )
    scaled.render()
    out["dim_dimlfac_scaled"] = scaled.dimension.dxf.handle

    _posts(msp, 200.0, 219.6)
    stale = await backend.dimension_linear(200, 0, 219.6, 0, 210, -10)
    entity = doc.entitydb[stale.handle]
    override = DimStyleOverride(entity)
    override.dimstyle_attribs["dimdec"] = 0
    override.commit()  # the DSTYLE XDATA changes; the *D block is not redrawn
    out["dim_render_stale"] = stale.handle

    _posts(msp, 300.0, 330.0)
    old42 = await backend.dimension_linear(300, 0, 330, 0, 315, -10)
    doc.entitydb[old42.handle].dxf.actual_measurement = 25.0
    out["dim_group42_stale"] = old42.handle

    msp.add_circle((450.0, 0.0), 10.0, dxfattribs={"layer": "GEOMETRY"})
    double = await backend.dimension_diameter(440, 0, 460, 0, text_override="%%c<>")
    out["dim_double_prefix"] = double.handle

    doc.layers.add("DIM_OFF")
    _posts(msp, 500.0, 540.0)
    hidden = await backend.dimension_linear(500, 0, 540, 0, 520, -10, layer="DIM_OFF")
    doc.layers.get("DIM_OFF").off()
    out["dim_value_hidden"] = hidden.handle

    unbound = await backend.dimension_linear(600, 0, 640, 0, 620, -10)
    out["dim_unbound"] = unbound.handle
    return out
