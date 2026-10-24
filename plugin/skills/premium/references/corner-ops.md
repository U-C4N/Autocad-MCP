# Corner Operations Reference

V1 supports **LINE+LINE** only. Other entity combinations (line+arc, line+circle,
polyline-segment) raise `RuntimeError` and will land in V2/V3.

## Decision tree

```text
                       ┌────── two lines should meet at a sharp corner
                       │            → entity_trim   (when one line overshoots)
                       │            → entity_extend (when one line falls short)
                       │
        Need to close ─┼────── two lines should meet at a rounded corner (R>0)
        a corner?      │            → entity_fillet
                       │
                       └────── two lines should meet at an angled bevel
                                    → entity_chamfer
```

## `entity_trim(target_handle, cutter_handle, keep_x, keep_y)`

- `target` — the line being shortened.
- `cutter` — treated as an **infinite ray** (AutoCAD's "implied extend" trim).
- `keep_x/keep_y` — a point on the side of `target` you want to KEEP.
- Returns the (in-place modified) target's `EntityInfo`.

**Tip:** use `point_from_snap(target_handle, "mid")` or another snap to compute
`keep_x/keep_y` deterministically.

## `entity_extend(target_handle, boundary_handle, end_x=None, end_y=None)`

- Extends `target` until it hits `boundary` (treated as infinite).
- `end_x/end_y=None` → auto-pick the target endpoint nearest the boundary midpoint.
- Pass `end_x/end_y` explicitly to disambiguate when you need to extend a
  specific endpoint.

## `entity_fillet(handle1, handle2, radius, trim=True)`

- Inserts a tangent **ARC** between the two lines.
- `radius=0` → corner-merge (no arc, only trim).
- `trim=False` → leave source lines untouched (rare; AutoCAD default is True).
- Returns the new ARC handle (or the first source line for `radius=0`).

## `entity_chamfer(handle1, handle2, dist1, dist2=None, trim=True)`

- Inserts a straight chamfer **LINE** between the two source lines.
- `dist2=None` → symmetric (`dist2 = dist1`); supply for asymmetric chamfers.
- Returns the new chamfer LINE handle.

## Corner-case table

| Vaka | Behaviour |
|------|-----------|
| Lines are parallel / anti-parallel | `RuntimeError("…parallel…")` |
| Same handle for both | `RuntimeError("…same…")` |
| One handle is not a LINE | `RuntimeError("…LINE+LINE only…")` |
| Intersection beyond segment ends | trim/fillet still work (implied extend) |
| Negative radius / zero distance | `RuntimeError` |
| Tiny gap (< 0.5 mm) between lines | flagged by `drawing_critique` as `untrimmed_corner` |

## Worked example — L-bracket inner corner R5

```python
l1 = await entity_create_line(0, 0, 100, 0)            # bottom edge
l2 = await entity_create_line(100, 0, 100, 80)         # right edge
arc = await entity_fillet(l1.handle, l2.handle, radius=5)
# arc.center == (95, 5); arc.radius == 5
# l1.end is now (95, 0); l2.start is now (100, 5)
```

## Why this matters

LLMs without these tools compute corner geometry by hand: they pick endpoints,
solve intersections, and write coordinates. That fails the moment a line is
slightly off, an angle is non-orthogonal, or the user changes a length. The
corner ops + `point_from_snap` move that math out of the LLM's head and into
deterministic library code.
