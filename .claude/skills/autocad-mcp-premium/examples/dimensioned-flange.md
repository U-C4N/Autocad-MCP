# Example: a dimensioned flange, sketch to accepted report

**Source (synthetic):** a square flange plate, 160 x 160 x 15, steel. Central
bore Ø40. Six holes Ø11 on a Ø120 pitch circle, the first on the horizontal
centreline. All values printed; no tolerances given.

## 1. Requirements before geometry

Every printed value becomes a requirement. The pitch circle is checked as the
centre distance between two opposite holes (`derived`: 120 = the PCD itself
for holes 180 degrees apart). The six holes are also counted, so a seventh
circle of the same size cannot hide.

```json requirements name=flange
{
  "units": "mm",
  "items": [
    {"id": "D1", "quantity": "diameter", "target": {"nominal": 40, "unit": "mm"},
     "features": [{"kind": "feature_tag", "part": "3E", "feature": "bore", "view": "front", "type": "circle"}],
     "provenance": "explicit", "source": {"document": "flange-sketch", "label": "Ø40"}},
    {"id": "D2", "quantity": "diameter", "target": {"nominal": 11, "unit": "mm"},
     "features": [{"kind": "feature_tag", "part": "3E", "feature": "bolt_0", "view": "front", "type": "circle"}],
     "provenance": "explicit", "source": {"document": "flange-sketch", "label": "6x Ø11"}},
    {"id": "N1", "quantity": "count", "target": {"exact": 6},
     "features": [{"kind": "predicate", "type": "circle", "radius_min": 5.4, "radius_max": 5.6}],
     "provenance": "explicit", "source": {"document": "flange-sketch", "label": "6x Ø11"}},
    {"id": "P1", "quantity": "distance", "target": {"nominal": 120, "unit": "mm"},
     "features": [{"kind": "feature_tag", "part": "3E", "feature": "bolt_0", "view": "front", "type": "circle"},
                  {"kind": "feature_tag", "part": "3E", "feature": "bolt_3", "view": "front", "type": "circle"}],
     "provenance": "derived", "source": {"document": "flange-sketch", "label": "PCD Ø120"}},
    {"id": "C1", "quantity": "concentric",
     "features": [{"kind": "feature_tag", "part": "3E", "feature": "bore", "view": "front", "type": "circle"},
                  {"kind": "predicate", "type": "circle", "radius_min": 19.9, "radius_max": 20.1}],
     "provenance": "explicit", "source": {"document": "flange-sketch", "label": "bore on the plate centre"}}
  ]
}
```

`"part": "3E"` is the `part_id` that `mech_part_draw` returns below (the anchor
handle; on a fresh headless drawing it is `3E`). Fill it in from the result,
never by guessing. The hole ids `bolt_0` ... `bolt_5` are how a `hole_pattern`
named `bolt` expands.

## 2. Draw from the model

`drawing_new()`, `drawing_settings(settings={"units": "mm"})`,
`drawing_apply_iso_layers("mech")`, then:

```json tool=mech_part_draw
{
  "spec": {
    "kind": "prismatic", "name": "flange", "material": "steel", "thickness": 15,
    "outline": [[0, 0], [160, 0], [160, 160], [0, 160]],
    "features": [
      {"kind": "hole", "id": "bore", "x": 80, "y": 80, "diameter": 40},
      {"kind": "hole_pattern", "id": "bolt", "pattern": "polar", "count": 6, "pcd": 120,
       "start_angle": 0, "base": {"x": 80, "y": 80, "diameter": 11}}
    ]
  }
}
```

Result: `part_id: "3E"`, `omitted: []`, nine ISO 129 dimensions.

## 3. Verify on the geometry

`drawing_verify_requirements(requirements=<the set from step 1>)` returns:

| Req | Target | Measured | Delta | Status | Handles |
|---|---|---|---|---|---|
| C1 | concentric | 0.0 mm centre distance | - | pass | 44 |
| D1 | Ø40 | 40.0 | 0.0 | pass | 44 |
| D2 | Ø11 | 11.0 | 0.0 | pass | 47 |
| N1 | 6 | 6 | 0 | pass | 47, 4A, 4D, 50, 53, 56 |
| P1 | 120 | 120.0 | 0.0 | pass | 47, 50 |

`status: pass`, `accepted: true`, `coverage.ratio: 1.0`. Measured on CAD-read
circles, bound by feature tag - not on the dimension texts, not on the spec.

## 4. Drafting gate, then deliver

`construction_clear()`, `drawing_critique(focus=None)` -> `[]`, then
`drawing_deliver(output_dir=..., requirements=<the set>)` - continued in
[accepted-delivery-report](accepted-delivery-report.md).

## What to tell the person

The table above, plus: "no tolerances were given on the sketch, so none were
applied; the rows pass at the verification epsilon. Material and thickness
are drawn as stated and not otherwise checked."
