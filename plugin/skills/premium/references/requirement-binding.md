# Requirement binding

How a source value becomes a requirement the server can measure, and what the
answer means. The parser is `engineering/verification/requirements.py`; the
same shape is read by `drawing_preflight`, `drawing_plan`,
`drawing_verify_requirements`, `drawing_finalize` and `drawing_deliver`.

## The set

```json requirements
{
  "units": "mm",
  "items": [
    {
      "id": "D1",
      "quantity": "diameter",
      "target": {"nominal": 40, "unit": "mm"},
      "features": [{"kind": "feature_tag", "part": "3E", "feature": "bore", "view": "front", "type": "circle"}],
      "provenance": "explicit",
      "source": {"document": "sketch-rev-B.pdf", "page": 1, "view": "front", "label": "Ø40"},
      "manufacturing_tolerance": {"fit": "H7", "source": "sketch note 2"}
    }
  ]
}
```

- `units` is required: a length means nothing without it (`mm`, `cm`, `m`,
  `in`, `ft`).
- `id` names one requirement; a duplicate id is refused.
- `quantity`: `distance`, `radius`, `diameter`, `angle` (these take
  `target.nominal`), `count` (`target.exact`, or `min` / `max`), `absence`, and
  the relations `concentric`, `coincident`, `parallel`, `perpendicular`,
  `tangent`, `horizontal`, `vertical` (no target).
- `features`: one or two selectors (a `count` or `absence` takes any number).
- `manufacturing_tolerance` (`fit`, or `upper` + `lower`) must name its own
  `source`; `verification_epsilon` is numeric noise. They are never merged.
- Every refusal names its path: `items[2].target.nominal: must be greater than
  zero, got -1.0.`

## Selectors - how a requirement finds its feature

| `kind` | Use it when | Keys |
|---|---|---|
| `"feature_tag"` | the part was drawn by `mech_part_draw` (every entity is tagged) | `part` (the `part_id`), `feature`, optional `view`, `type`, `fallback` predicate |
| `"handle"` | you hold the entity's handle and it will not be renumbered | `handle` (hex) |
| `"arch_element"` | a wall, opening or room drawn by the `arch_*` tools | `element`, optional `type` |
| `"predicate"` | a drawing without tags | `type` (`line`, `circle`, `arc`, ...), `layer`, `space`, `region` `[x1, y1, x2, y2]`, `radius_min` / `radius_max`, `length_min` / `length_max` |
| `"dimension"` | the requirement is about a DIMENSION entity | `handle` |

Prefer a feature tag: it survives a save that renumbers handles, and a copied
tag shows up as two candidates instead of a silent wrong pick.

## Counts and absence

```json requirements
{
  "units": "mm",
  "items": [
    {
      "id": "N1",
      "quantity": "count",
      "target": {"exact": 6},
      "features": [{"kind": "predicate", "type": "circle", "radius_min": 5.4, "radius_max": 5.6}],
      "provenance": "explicit",
      "source": {"label": "6x Ø11"}
    },
    {
      "id": "X1",
      "quantity": "absence",
      "features": [{"kind": "predicate", "type": "line", "layer": "CONSTRUCTION"}],
      "provenance": "assumed"
    }
  ]
}
```

## Statuses

| Row status | Means | What you do |
|---|---|---|
| `pass` | measured on CAD-read geometry, within tolerance | report it with the handles |
| `fail` | measured, outside tolerance | fix the geometry, measure again |
| `unknown` (`missing_requirement`) | nothing bound or nothing measurable | tighten the selector or ask |
| `ambiguous` (`ambiguous_binding`) | two equal candidates; none chosen | name the feature (tag, region); never pick |
| `unsupported` (`unsupported_measurement`) | a spline, a tilted plane | say it is unverified |

The report is `pass` only when every row passes, `fail` when any fails,
`incomplete` otherwise. `accepted` additionally needs fresh evidence (the
document revision has not moved since) and no `error` finding - a DIMENSION
whose text or `DIMLFAC` disagrees with its geometry is the finding
`dimension_mismatch`; one that cannot be found again on a saved file is
`dimension_unverified`. `blocking` lists every reason.

## Refused, on purpose

A tolerance without a source is refused - write the source or leave the
tolerance out:

```json requirements-refused
{
  "units": "mm",
  "items": [
    {
      "id": "D1",
      "quantity": "diameter",
      "target": {"nominal": 40},
      "features": [{"kind": "handle", "handle": "44"}],
      "provenance": "explicit",
      "manufacturing_tolerance": {"upper": 0.1, "lower": -0.1}
    }
  ]
}
```

Conflicting values are kept, not collapsed: two items asking for the same
quantity of the same feature with different targets are reported as
`CONFLICTING_REQUIREMENT` by `drawing_preflight`, and an item marked
`"provenance": "conflicting"` as `CONFLICTING_PROVENANCE`. The verifier measures
such rows like any other - provenance never changes a status - so a `pass`
against one of two disagreeing values is not agreement with the source: report
the conflict and ask which value controls.
