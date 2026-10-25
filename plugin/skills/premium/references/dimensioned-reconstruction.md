# Dimensioned reconstruction

From a dimensioned source (a sketch, a PDF, a photo of a print) to geometry
whose every controlling dimension has been measured on the CAD.

## 1. Read the source into requirements, not into coordinates

Write one typed requirement per controlling value **before** any geometry
exists (schema: [requirement-binding](requirement-binding.md)):

- the value, its unit and the feature(s) it controls (`Ø40` -> the bore; `6x Ø11
  on Ø120` -> six holes, their diameter and the pitch circle);
- where it came from (`source`: document, page, view, the label as printed);
- its provenance: `explicit` when printed, `derived` when computed from printed
  values (a pitch-circle chord, an overall length from a chain), `assumed`
  when nobody said it, `raster_estimate` when read off pixels, `conflicting`
  when two places disagree;
- a manufacturing tolerance only with its own source (a note, a fit, a
  general-tolerance standard named on the sheet). Never derive one.

Pixels are never dimensions. A value you can only scale off the image is
`raster_estimate` and stays an open item (see
[uncertainty-and-questions](uncertainty-and-questions.md)).

## 2. Preflight and plan

```text
drawing_new()
drawing_settings(settings={"units": "mm"})        # declare the unit before drawing
pre = drawing_preflight(intent, requirements=req)  # questions + conflicts, never refusals
drawing_plan(**pre["normalized_spec"], spec_hash=pre["spec_hash"],
             persist_requirements=True)            # the set travels inside the DWG/DXF
```

`drawing_plan` takes the preflight's `normalized_spec` field for field (intent,
sheet, scale, requirements ...); anything else is refused as not matching the
hashed spec. `ready: false` means a question or a conflict is open. Answer it or carry it
as an open item; do not drop it. A new headless drawing declares metres until
`drawing_settings` says otherwise, and verification holds a drawing to its
declared unit.

## 3. Geometry from the model

Use the part model (`mech_part_draw` with a `revolved` segment list or a
`prismatic` outline plus typed features: `hole`, `hole_pattern`, keyways,
threads, ...). Every entity it draws carries a feature tag
(`{part, feature, view}`), which is what lets a requirement name "the bore"
instead of a handle. Read `omitted`: a feature that could not be projected is
listed there, never silently dropped.

Raw `entity_create_*` calls are for what no model covers. When you use them,
derive coordinates from the dimensions (and read existing points with
`point_from_snap`), and give the requirement a `predicate` or `handle`
selector.

Keep model geometry at 1:1. Presentation scale belongs to the sheet and its
viewports.

## 4. Verify, then annotate

```text
drawing_verify_requirements(requirements=req)   # status pass, accepted true
mech_dimension_part(part_id=...)                  # or dimension_* on read-back points
drawing_verify_requirements(requirements=req)   # again: no dimension_mismatch
```

A `fail` row is fixed in the geometry (edit the model and redraw, or move the
entity), then measured again. A dimension text that disagrees with the
geometry is reported as `dimension_mismatch`; the fix is never
`text_override=` or a `DIMLFAC`.

## 5. Finish

`construction_clear()`, `drawing_critique(focus=None)` until `[]`, then the
save / reopen / deliver chain in [delivery-and-roundtrip](delivery-and-roundtrip.md).

Worked through end to end in [the flange example](../examples/dimensioned-flange.md).
