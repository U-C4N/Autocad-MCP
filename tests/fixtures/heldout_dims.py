"""A held-out binding set from a generator family the development fixtures never use (B29).

The development fixtures of the verification engine are parts drawn by the
mechanical generator: one part model, its views, its feature tags. This family
is built differently on purpose - raw ``ezdxf`` plates with holes, no part
model, no view engine - so a binder tuned on the development fixtures meets
geometry it was not tuned on:

* each plate is drawn either straight into model space or as a block INSERTed
  with a rotation, a mirror (x scale -1) and a uniform scale of 1 or 2;
* holes have distinct diameters, except on "twin" plates, which carry two
  holes of one diameter - a requirement naming "the hole of that diameter on
  this plate" must then abstain;
* holes carry ``ACADMCP_MECH`` feature tags written with ``set_xdata``; on
  "copied" plates the block is inserted twice, so the copy carries the same
  tag - a requirement naming that tag must abstain;
* every model-space plate carries a diameter DIMENSION on its first hole.

Every truth is computed from what the generator placed - a hole's WCS centre
is its placement applied to its local centre by :func:`_place`, written here -
never read back from the binder. Seeded and synthetic: no client data.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass

import ezdxf

from engineering.verification.binding import wilson_interval
from engineering.verification.types import FeatureSelector, Requirement

__all__ = [
    "SEED",
    "TARGET_COVERAGE",
    "TARGET_PRECISION",
    "HeldoutCase",
    "build_heldout",
    "score",
]

#: The seed of the published held-out set. Changing it makes a new set.
SEED = 20261024
PLATES = 48
SPACING = 2000.0
COPY_OFFSET = 900.0
DIAMETERS = (6.0, 8.0, 10.0, 12.0, 16.0, 20.0)
#: The B29 targets on the supported native subset.
TARGET_PRECISION = 0.98
TARGET_COVERAGE = 0.80


@dataclass(frozen=True)
class HeldoutCase:
    """One requirement and what it must bind to.

    ``truth`` is ``((block_path, handle), ...)``, one per selector, or None
    when the right answer is to abstain (the requirement cannot name one
    feature).
    """

    case_id: str
    requirement: Requirement
    truth: tuple[tuple[tuple[str, ...], str], ...] | None


def _place(local, placement) -> tuple[float, float]:
    """Plate coordinates to WCS: mirror x, scale, rotate, translate - the order
    an INSERT applies them. ``placement`` None: the plate was drawn in place."""
    if placement is None:
        return (float(local[0]), float(local[1]))
    x, y = float(local[0]), float(local[1])
    if placement["mirror"]:
        x = -x
    x, y = x * placement["scale"], y * placement["scale"]
    c = math.cos(math.radians(placement["rotation"]))
    s = math.sin(math.radians(placement["rotation"]))
    ox, oy = placement["origin"]
    return (ox + c * x - s * y, oy + s * x + c * y)


def _requirement(case_id: str, kind: str, value: dict, diameter: float) -> Requirement:
    return Requirement(
        id=case_id,
        quantity="diameter",
        target=(("nominal", diameter), ("unit", "mm")),
        features=(FeatureSelector(kind=kind, value=tuple(sorted(value.items()))),),
        provenance="explicit",
        source=(("document", "heldout"), ("label", case_id)),
    )


def _tag(entity, part: str, feature: str) -> None:
    payload = json.dumps(
        {"feature": feature, "kind": "feature", "part": part, "v": 1, "view": "plan"},
        separators=(",", ":"),
        sort_keys=True,
    )
    entity.set_xdata("ACADMCP_MECH", [(1000, payload)])


def _box(points, pad: float) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def build_heldout(seed: int = SEED, plates: int = PLATES):
    """``(doc, cases)``: the drawing and every case with its truth."""
    rng = random.Random(seed)
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    doc.appids.add("ACADMCP_MECH")
    msp = doc.modelspace()
    cases: list[HeldoutCase] = []
    for index in range(plates):
        part = f"P{index}"
        origin = ((index % 8) * SPACING, (index // 8) * SPACING)
        as_block = rng.random() < 0.5
        rotation = rng.choice((0.0, 30.0, 45.0, 90.0, 137.0, 210.0))
        mirror = as_block and rng.random() < 0.5
        scale = rng.choice((1.0, 2.0)) if as_block else 1.0
        twin = rng.random() < 0.15
        copied = as_block and not twin and rng.random() < 0.15
        count = rng.randint(3, 5)
        sizes = rng.sample(DIAMETERS, count)
        if twin:
            sizes[1] = sizes[0]
        width, height = 60.0 * count, 120.0
        if as_block:
            name = f"PLATE_{index}"
            space = doc.blocks.new(name)
            base = (0.0, 0.0)
            placement = {"origin": origin, "rotation": rotation, "mirror": mirror, "scale": scale}
        else:
            space = msp
            base = origin
            placement = None
        corners = [
            (base[0] + x, base[1] + y)
            for x, y in ((0, 0), (width, 0), (width, height), (0, height))
        ]
        space.add_lwpolyline(corners, close=True)
        holes = []
        for k, diameter in enumerate(sizes):
            centre = (base[0] + 30.0 + 60.0 * k, base[1] + 40.0 + 40.0 * (k % 2))
            circle = space.add_circle(centre, diameter / 2.0)
            _tag(circle, part, f"H{k}")
            holes.append((circle.dxf.handle, centre, diameter))
        path: tuple[str, ...] = ()
        if as_block:
            attribs = {"rotation": rotation, "xscale": -scale if mirror else scale, "yscale": scale}
            path = (msp.add_blockref(name, origin, dxfattribs=attribs).dxf.handle,)
            if copied:
                msp.add_blockref(name, (origin[0], origin[1] + COPY_OFFSET), dxfattribs=attribs)
        plate_region = _box([_place(c, placement) for c in corners], 1.0)
        for k, (handle, centre, diameter) in enumerate(holes):
            cx, cy = _place(centre, placement)
            radius = diameter / 2.0 * scale
            margin = 15.0 * scale
            truth = ((path, handle),)
            twins = sum(1 for _h, _c, d in holes if d == diameter) > 1
            window = {"type": "circle", "radius_min": radius - 1e-6, "radius_max": radius + 1e-6}
            region = (cx - margin, cy - margin, cx + margin, cy + margin)
            cases.append(
                HeldoutCase(
                    f"{part}-H{k}-region",
                    _requirement(
                        f"{part}-H{k}-region",
                        "predicate",
                        {**window, "region": region},
                        2.0 * radius,
                    ),
                    truth,
                )
            )
            cases.append(
                HeldoutCase(
                    f"{part}-H{k}-size",
                    _requirement(
                        f"{part}-H{k}-size",
                        "predicate",
                        {**window, "region": plate_region},
                        2.0 * radius,
                    ),
                    None if twins else truth,
                )
            )
            cases.append(
                HeldoutCase(
                    f"{part}-H{k}-tag",
                    _requirement(
                        f"{part}-H{k}-tag",
                        "feature_tag",
                        {"feature": f"H{k}", "part": part, "type": "circle"},
                        2.0 * radius,
                    ),
                    None if copied else truth,
                )
            )
            cases.append(
                HeldoutCase(
                    f"{part}-H{k}-handle",
                    _requirement(
                        f"{part}-H{k}-handle",
                        "handle",
                        {"handle": handle, "block_path": path},
                        2.0 * radius,
                    ),
                    truth,
                )
            )
        if not as_block:
            handle, centre, diameter = holes[0]
            angle = rng.choice((0.0, 45.0, 120.0))
            dimension = msp.add_diameter_dim(center=centre, radius=diameter / 2.0, angle=angle)
            dimension.render()
            dim_handle = dimension.dimension.dxf.handle
            cases.append(
                HeldoutCase(
                    f"{part}-dim",
                    _requirement(f"{part}-dim", "dimension", {"handle": dim_handle}, diameter),
                    (((), handle),),
                )
            )
    return doc, tuple(cases)


def score(cases, bindings) -> dict:
    """Eligible / attempted / accepted / abstained, precision and coverage.

    ``attempted``: the binder bound the case. ``accepted``: an attempted case
    whose refs are exactly its truth (binding a must-abstain case is an attempt
    that is never accepted). ``precision = accepted / attempted`` and
    ``coverage = attempted / eligible``, each with its Wilson 95 % interval.
    Abstaining on everything gives coverage 0 and never meets the target.
    """
    by_id = {b.requirement_id: b for b in bindings}
    eligible = len(cases)
    attempted = accepted = abstained = abstained_correctly = 0
    for case in cases:
        binding = by_id[case.case_id]
        if binding.status != "bound":
            abstained += 1
            abstained_correctly += case.truth is None
            continue
        attempted += 1
        got = tuple((tuple(ref.block_path), ref.handles[0]) for ref in binding.refs)
        if case.truth is not None and got == case.truth:
            accepted += 1
    precision = accepted / attempted if attempted else 0.0
    coverage = attempted / eligible if eligible else 0.0
    p_low, p_high = wilson_interval(accepted, attempted)
    c_low, c_high = wilson_interval(attempted, eligible)
    return {
        "eligible": eligible,
        "attempted": attempted,
        "accepted": accepted,
        "abstained": abstained,
        "must_abstain": sum(case.truth is None for case in cases),
        "abstained_correctly": abstained_correctly,
        "precision": {"value": precision, "low": p_low, "high": p_high},
        "coverage": {"value": coverage, "low": c_low, "high": c_high},
        "meets_target": precision >= TARGET_PRECISION and coverage >= TARGET_COVERAGE,
    }
