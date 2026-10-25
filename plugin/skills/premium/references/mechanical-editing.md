# Mechanical drawing and editing

## Draw parts from the model

A machine part is one call: `mech_part_draw(spec=...)` with a `revolved`
segment list (`length`, `d_outer`, `d_inner`, `taper_to`) or a `prismatic`
outline plus `thickness`, and typed features (`hole`, `hole_pattern` linear /
grid / polar, keyways, threads, grooves, ...). It returns `part_id` (the
anchor handle), the views, every handle and `omitted`. Further views come from
the stored model: `mech_view_add(part_id=..., kind="section", plane=...)`;
`section_line` returns the plane that call consumes, so label and view cannot
disagree. Standard parts (`std_part_insert`) and features
(`std_feature_draw`) come from transcribed tables; a size outside a table is
refused by name and never interpolated.

Do not hand-draw what a generator owns: gear teeth (`gear_draw_*`), keyways
(`keyway_draw_*`), title blocks (`titleblock_apply`), sheet frames
(`sheet_frame`), hatches by material (`hatch_material`).

## The stored model is intent, not evidence

`mech_part_inspect` returns the model as it was drawn (`ACADMCP_MECH` XDATA on
the anchor). An operator who moves a hole afterwards changes the geometry,
not the XDATA. So:

- a question about the drawing ("is h1 still at x = 20?") is answered by
  measuring the geometry - `drawing_verify_requirements` with a feature-tag
  selector, or `entity_get` on the tagged handle - never by reading the model;
- when the two disagree, report both and say the stored model is stale.

There is no parametric rebuild in this release. To change a drawn part:
either delete its entities and draw the corrected spec again, or edit the
geometry in place (`entity_edit_geometry` keeps the handle, `entity_move`,
`entity_set_properties`) and accept that the stored model now lags - then
verify the geometry.

## Change existing drawings under a guard

An edit to a drawing someone else may be touching goes preview -> apply:

```text
pre = cad_batch(steps=[...], dry_run=True)            # validates, executes nothing
cad_batch(steps=[...], expected_revision=pre["preview"]["revision"],
          preconditions=pre["preview"]["preconditions"])
```

The same `expected_revision=` / `preconditions=` pair guards `drawing_refine`,
`text_find_replace` and `pid_from_spec`. A `stale_revision` refusal changed
nothing: take a new preview, look at what moved, and decide again (see
[the stale-revision example](../examples/stale-revision.md)).

## Corners

`entity_trim` (keep side by `keep_x` / `keep_y`), `entity_extend`,
`entity_fillet` (radius 0 merges a corner) and `entity_chamfer` work on
LINE + LINE only and treat the other line as infinite. An overshoot left in
place is reported by `drawing_critique` as `untrimmed_corner`. Read points
with `point_from_snap(handle, "end"|"mid"|"center"|"quad"|"perp"|"near", ...)`
and `point_intersection` / `point_tangent`; never compute them in your head.

## Layers, lineweights, dimensions

- `drawing_apply_iso_layers("mech")` sets ISO 128 lineweights (0.13, 0.18,
  0.25, 0.35, 0.50, 0.70, 1.00, 1.40, 2.00 mm only). Scaffolding goes on
  `CONSTRUCTION` and is removed with `construction_clear()` before critique.
- `mech_dimension_part(part_id=...)` dimensions a part model (ISO 129, ISO 286
  fits). Elsewhere `dimension_linear` / `dimension_radius` /
  `dimension_diameter` take read-back feature points, `fit="H7"` (from the
  ISO 286 tables) or `tol_upper=` / `tol_lower=` with a sourced tolerance.
  Keep the measured value in the text (`<>` placeholder); never a fixed
  number over a mismatch.
- Live AutoCAD will not create a layer on assignment; the drawing tools
  ensure their layers first. If you place raw entities live, `layer_create`
  first.

## Annotation

`surface_texture` (ISO 21920-1), `weld_symbol` (ISO 2553), `centre_marks`,
`gd_frame` / `datum_feature` (ISO 1101), `balloon_add` linked to `bom_table`
(a rerun renumbers instead of duplicating). The six `mech_*` critique focuses
run inside `drawing_critique(focus=None)` and `drawing_finalize`.
