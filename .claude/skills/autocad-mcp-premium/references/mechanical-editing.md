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

`mech_part_inspect(part_id, drift=True)` says whether the drawing still
matches its model (`model_is_current`, `drift` per feature and view);
`history=True, param="features.bore.diameter"` reads the part's revisions.

## Revise a drawn part

A change to a drawn part is one call, never delete-and-redraw:
`mech_part_revise(part_id, changes={"features.bore.diameter": 24,
"segments[1].length": 50})`, with `dry_run=True` first and the same
`expected_revision=` / `preconditions=` guard. Tagged lines, circles and arcs
keep their handles; a redrawn entity is listed in `handle_map`; dimensions are
placed again, so their handles change. A hand-edited tagged entity refuses the
call (`hand_edited_tagged_entity`, the handles named) and so does a part drawn
before 1.8.0 (`model_drift`); `overwrite=True` replaces the hand edit with the
model - ask before passing it. A part with a detail view, or two views sharing
a view tag, is refused by name. The `model_drift` critique focus names a part
whose drawing no longer matches its model.

In-place edits keep the handle, layer and XDATA (feature tags included):
`entity_edit_geometry` takes `points` / `bulges` for a polyline (the vertex
count may change) and `x` / `y` for TEXT and MTEXT; `entity_edit_text` takes
`x` / `y` beside content, height and rotation. An argument the type does not
take is refused by name. On a live seat a polyline whose vertex count AutoCAD
will not change in place is recreated with everything carried over, and the
result lists `replaced` `[{old, new}]` - use the new handle from then on.

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
- Entities follow their layer: lineweight `"ByLayer"` (-1), never -3, which
  is *Default* (LWDEFAULT). The `property_override` focus reports an entity on
  an engineering layer whose lineweight, colour or linetype is not ByLayer
  (an explicit non-ISO-128 lineweight is an error and fails finalize);
  `drawing_refine` sets them back to ByLayer.
- `mech_dimension_part` tags every DIMENSION it draws in `ACADMCP_MECH` XDATA
  with its part, feature and view (`role: "dim"`; the hole table
  `role: "hole_table"`).
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
and `model_drift` run inside `drawing_critique(focus=None)` and
`drawing_finalize`.
