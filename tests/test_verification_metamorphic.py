"""Metamorphic evaluation (B18-B20): translate, rotate, mirror, scale and mm <-> inch.

One drawing, measured as drawn and after every transform of the drawing (ezdxf's
own `transform`, so mirrored arcs really get a -Z extrusion), with the
requirement transformed alongside where its numbers carry a length. The
verdicts must not move. The expected verdicts are worked by hand from the
geometry below.
"""

from __future__ import annotations

import math

import ezdxf
import pytest
from ezdxf.math import Matrix44

from engineering.understand.snapshot import records_from_doc
from engineering.verification.binding import bind
from engineering.verification.measure import evaluate
from engineering.verification.observe import observe
from engineering.verification.types import FeatureSelector, Requirement, RequirementSet

EXPECTED = {
    "d_pp": "pass",  # centre (40, 15) to point (70, 15): 30
    "d_pl": "pass",  # point (70, 15) to the x axis: 15
    "d_ll": "pass",  # y = 0 and y = 30: 30
    "d_ll_bad": "fail",  # ... asked for 31
    "r1": "pass",  # radius 10
    "dia": "pass",  # diameter 8
    "ang": "pass",  # x axis and a 30 degree line
    "conc": "pass",  # both circles centred on (40, 15)
    "par": "pass",
    "perp": "pass",  # x axis and the y axis
    "perp_bad": "fail",  # x axis and the 30 degree line: 60 off
    "tan": "pass",  # y = 25 touches the r 10 circle at (40, 25)
    "tan_near": "fail",  # y = 25.001 misses it by 1e-3
    "tan_arc": "pass",  # the arc continues y = 0 at (100, 0)
    "cnt": "pass",
}


def _build(matrix=None, units=4):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = units
    msp = doc.modelspace()
    h = {
        "L1": msp.add_line((0, 0), (100, 0)).dxf.handle,
        "L2": msp.add_line((0, 30), (100, 30)).dxf.handle,
        "L3": msp.add_line((0, 0), (0, 30)).dxf.handle,
        "L4": msp.add_line((0, 0), (100, 100 * math.tan(math.radians(30)))).dxf.handle,
        "C1": msp.add_circle((40, 15), 10).dxf.handle,
        "C2": msp.add_circle((40, 15), 4).dxf.handle,
        "T1": msp.add_line((20, 25), (60, 25)).dxf.handle,
        "T2": msp.add_line((20, 25.001), (60, 25.001)).dxf.handle,
        "A1": msp.add_arc((100, 15), 15, 270, 90).dxf.handle,
        "P1": msp.add_point((70, 15)).dxf.handle,
    }
    if matrix is not None:
        for entity in msp:
            entity.transform(matrix)
    return doc, h


def _requirements(h, k=1.0, unit="mm"):
    def r(rid, quantity, names, **target):
        tol = target.pop("tol", None)
        return Requirement(
            id=rid,
            quantity=quantity,
            target=tuple(sorted(target.items())),
            features=tuple(
                FeatureSelector(kind="handle", value=(("handle", h[n]),)) for n in names
            ),
            provenance="explicit",
            source=(("label", rid),),
            manufacturing_tolerance=tol,
        )

    band = (("lower", -0.01 * k), ("upper", 0.01 * k))
    degrees = (("lower", -0.01), ("upper", 0.01))
    return RequirementSet(
        requirements=(
            r("d_pp", "distance", ["C1", "P1"], nominal=30.0 * k, unit=unit, tol=band),
            r("d_pl", "distance", ["P1", "L1"], nominal=15.0 * k, unit=unit, tol=band),
            r("d_ll", "distance", ["L1", "L2"], nominal=30.0 * k, unit=unit, tol=band),
            r("d_ll_bad", "distance", ["L1", "L2"], nominal=31.0 * k, unit=unit, tol=band),
            r("r1", "radius", ["C1"], nominal=10.0 * k, unit=unit, tol=band),
            r("dia", "diameter", ["C2"], nominal=8.0 * k, unit=unit, tol=band),
            r("ang", "angle", ["L1", "L4"], nominal=30.0, unit="deg", tol=degrees),
            r("conc", "concentric", ["C1", "C2"], unit=unit),
            r("par", "parallel", ["L1", "L2"]),
            r("perp", "perpendicular", ["L1", "L3"]),
            r("perp_bad", "perpendicular", ["L1", "L4"]),
            r("tan", "tangent", ["T1", "C1"], unit=unit),
            r("tan_near", "tangent", ["T2", "C1"], unit=unit),
            r("tan_arc", "tangent", ["L1", "A1"], unit=unit),
            r("cnt", "count", ["C1", "C2"], exact=2),
        ),
        units=unit,
    )


def _verdicts(doc, rs):
    observed = observe(records_from_doc(doc, source="t", block_defs=True))
    bindings = bind(rs, observed)
    return {
        r.id: evaluate(r, b, observed).status
        for r, b in zip(rs.requirements, bindings, strict=True)
    }


def test_the_drawing_as_drawn_gives_the_hand_worked_verdicts():
    doc, h = _build()
    assert _verdicts(doc, _requirements(h)) == EXPECTED


@pytest.mark.parametrize(
    "matrix",
    [
        Matrix44.translate(1234.5, -987.25, 0.0),
        Matrix44.z_rotate(math.radians(37.0)),
        Matrix44.scale(-1.0, 1.0, 1.0),
        Matrix44.chain(
            Matrix44.scale(1.0, -1.0, 1.0),
            Matrix44.z_rotate(math.radians(-61.0)),
            Matrix44.translate(10.0, 20.0, 0.0),
        ),
    ],
    ids=["translate", "rotate", "mirror", "mirror_rotate_translate"],
)
def test_rigid_motions_and_mirrors_keep_every_verdict(matrix):
    doc, h = _build(matrix)
    assert _verdicts(doc, _requirements(h)) == EXPECTED


def test_a_uniform_scale_with_the_requirement_scaled_keeps_every_verdict():
    doc, h = _build(Matrix44.scale(3.0, 3.0, 1.0))
    assert _verdicts(doc, _requirements(h, k=3.0)) == EXPECTED


def test_mm_to_inch_on_drawing_and_requirement_together_keeps_every_verdict():
    doc, h = _build(Matrix44.scale(1 / 25.4, 1 / 25.4, 1.0), units=1)
    assert _verdicts(doc, _requirements(h, k=1 / 25.4, unit="in")) == EXPECTED


def test_an_inch_drawing_measured_against_a_mm_requirement_keeps_every_verdict():
    doc, h = _build(Matrix44.scale(1 / 25.4, 1 / 25.4, 1.0), units=1)
    assert _verdicts(doc, _requirements(h)) == EXPECTED
