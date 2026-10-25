# Example: two equal holes, one requirement

**Source (synthetic):** a plate 80 x 50 x 8 with two Ø10 holes at (20, 25) and
(60, 25). The sheet's note says "hole Ø10 - left hole reamed".

```json tool=mech_part_draw
{
  "spec": {
    "kind": "prismatic", "name": "plate", "material": "aluminium", "thickness": 8,
    "outline": [[0, 0], [80, 0], [80, 50], [0, 50]],
    "features": [
      {"kind": "hole", "id": "h1", "x": 20, "y": 25, "diameter": 10},
      {"kind": "hole", "id": "h2", "x": 60, "y": 25, "diameter": 10}
    ]
  },
  "dimension": false
}
```

## The tempting requirement

"A Ø10 circle" - a predicate that both holes satisfy:

```json requirements name=ambiguous
{
  "units": "mm",
  "items": [
    {"id": "D1", "quantity": "diameter", "target": {"nominal": 10, "unit": "mm"},
     "features": [{"kind": "predicate", "type": "circle", "radius_min": 4.9, "radius_max": 5.1}],
     "provenance": "explicit", "source": {"label": "hole Ø10 - left hole reamed"}}
  ]
}
```

`drawing_verify_requirements` answers:

```text
status: incomplete   accepted: false
D1  ambiguous  kind: ambiguous_binding
    "geometric predicate: 2 equal candidates, none chosen"
```

Both holes measure 10.0. The server still does not pick one, because the note
is about **the left** hole - a pass on the right one would verify the wrong
feature. Do not "resolve" this by taking the first candidate or by reporting
"both are Ø10, so D1 passes".

## Name the feature

Either by its tag (the part model tags every entity; `part` is the
`part_id` the draw returned) ...

```json requirements name=resolved
{
  "units": "mm",
  "items": [
    {"id": "D1", "quantity": "diameter", "target": {"nominal": 10, "unit": "mm"},
     "features": [{"kind": "feature_tag", "part": "3E", "feature": "h1", "view": "front", "type": "circle"}],
     "provenance": "explicit", "source": {"label": "hole Ø10 - left hole reamed"}}
  ]
}
```

... or, on a drawing without tags, by a region that holds only the left hole:
`{"kind": "predicate", "type": "circle", "radius_min": 4.9, "radius_max": 5.1,
"region": [0, 0, 40, 50]}`. Either way D1 is `pass`, bound to one handle.

If the source does not say which hole it means, ask - quoting the note and
both positions - instead of choosing.

## Copies of a feature

The same answer comes back when a tagged hole was **copied**: the copy
carries the tag, so the tag now has two candidates and the row is
`ambiguous`. That is the server noticing a duplicate, not a bug to work
around; find out which entity is the real feature (`drawing_diff` against the
previous revision, or ask), then delete or re-tag the other.
