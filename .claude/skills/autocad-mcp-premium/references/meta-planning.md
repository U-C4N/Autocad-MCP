# Meta-Planning Reference

The premium meta-tool layer wraps the entity primitives in a quality-first
workflow. Schemas live in `engineering/plan_spec.py`; checks live in
`engineering/critique.py`.

## `drawing_plan(intent, sheet_size, scale, layer_set_id, view_count, dim_style, notes)`

Commits a `PlanSpec` to the backend instance. Always call FIRST.

```yaml
PlanSpec:
  intent: str                                           # "L-bracket 50x80"
  sheet_size: A4 | A3 | A2 | A1 | A0                    # default A3
  scale: float                                          # 1.0 = 1:1
  layer_set_id: mech | pid | iso13567                   # default mech
  view_count: int                                       # default 1
  dim_style: chain | baseline | ordinate | mixed        # default chain
  notes: list[str]                                      # free-form constraints
```

The PlanSpec is held by the backend (not persisted to DXF) and replayed by
`drawing_critique` at finalize time.

## `drawing_critique(focus=None)`

Returns a list of `Issue` instances. **Empty list = pass.**

```yaml
Issue:
  severity: error | warning | info
  focus: <CritiqueFocus enum>
  message: str
  handles: list[str]                # entities involved
  detail: dict                      # numeric context
```

`CritiqueFocus` is a **closed enum** — adding a new focus requires a code change
to `plan_spec.py` AND `critique.py`. This is intentional: it keeps the critique
scope from drifting.

| Focus | Checks |
|-------|--------|
| `iso128` | Layer lineweights are in {0.13, 0.18, 0.25, 0.35, 0.5, 0.7, 1.0, 1.4, 2.0} mm |
| `layer_color` | Engineering layers keep their canonical ACI color |
| `dim_overlap` | No two dimensions within 5 mm of each other |
| `untrimmed_corner` | No two LINE endpoints within 0.5 mm but not exactly equal |
| `duplicate_entities` | No two LINEs with identical (or reversed) endpoints |
| `construction_left` | CONSTRUCTION layer is empty |

`focus=None` runs all six. Pass a list (e.g. `["construction_left", "untrimmed_corner"]`)
to scope a check during iterative editing.

## `point_from_snap(handle, snap, ref_x, ref_y)`

Eliminates LLM coordinate guessing. Returns `(x, y)` tuple.

| `snap` | LINE | CIRCLE/ARC | Required |
|--------|------|-----------|----------|
| `end` | start or end (closer to ref) | arc start or end | — (uses ref if given) |
| `mid` | midpoint | arc midpoint | — |
| `center` | — | center | — |
| `quad` | — | nearest of E/N/W/S quadrants | — (uses ref if given) |
| `perp` | foot of perpendicular from ref | — | `ref_x`, `ref_y` |
| `near` | nearest point on segment (clamped) | nearest on circle/arc | `ref_x`, `ref_y` |
| `int` | (V2 — needs 2nd handle) | | |

## `construction_xline(x, y, angle_deg, layer="CONSTRUCTION")`

Creates an infinite construction line on a non-printing layer (color 250,
lightest weight). Use as scaffolding when laying out geometry.

`construction_clear(layer="CONSTRUCTION")` — bulk-deletes everything on the
layer. Returns `{"ok": True, "deleted": N}`. **Idempotent.**

## `drawing_apply_iso_layers(standard)`

| `standard` | Layer count | Use case |
|------------|-------------|----------|
| `mech` | 10 | Mechanical part / assembly drawings |
| `pid` | 15 | Process & Instrumentation Diagrams (ISO 10628) |
| `iso13567` | 10 | ISO 13567 layer-naming convention starter |

Idempotent — existing layers are not modified.

## `dimension_auto(handles, style, offset)`

V1 supports LINE entities only. Generates one or more dimensions using the
chosen style.

| `style` | Behaviour |
|---------|-----------|
| `chain` | One linear dim per handle, perpendicular-offset from each segment |
| `baseline` | All dims share the first segment's start point as baseline |
| `ordinate` | X and Y coordinate dims for each segment endpoint |

`offset` (mm) controls the dim-line distance from the geometry.

## `entity_select_smart(predicate)`

Predicate is a dict; **all keys optional, AND-ed**:

```yaml
predicate:
  type: LINE | CIRCLE | ARC | ...     # entity type filter
  layer: <name>                        # exact match
  near: [x, y, radius]                 # entity within radius of point
  length_range: [min, max]             # LINE/ARC only
  color: <ACI int>                     # exact match
```

Returns a list of `EntityInfo`. Use this in lieu of memorising handles when
chaining `dimension_auto` or other operations.
