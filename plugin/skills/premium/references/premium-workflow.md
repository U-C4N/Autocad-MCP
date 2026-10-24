# Premium Workflow — Full Examples

The 9-step workflow walked through end-to-end. Use these as templates.

## Template 1 — Mechanical part (L-bracket with rounded inner corner)

```python
# 1. PLAN
plan = await drawing_plan(
    intent="L-bracket 50x80, R5 inner corner, 5mm thick",
    sheet_size="A4",
    scale=1.0,
    layer_set_id="mech",
    dim_style="chain",
)

# 2. LAYERS
await drawing_apply_iso_layers("mech")

# 3. CONSTRUCTION (optional — set up reference grid)
await construction_xline(0, 0, 0)         # horizontal axis
await construction_xline(0, 0, 90)        # vertical axis

# 4. GEOMETRY (on GEOMETRY layer)
l1 = await entity_create_line(0, 0, 100, 0, layer="GEOMETRY")
l2 = await entity_create_line(100, 0, 100, 80, layer="GEOMETRY")

# 5. CORNERS
arc = await entity_fillet(l1["handle"], l2["handle"], radius=5)

# 6. SELECT for dimensioning (avoid handle memorization)
geometry_lines = await entity_select_smart({
    "type": "LINE",
    "layer": "GEOMETRY",
})

# 7. DIMENSIONS
dims = await dimension_auto(
    [e["handle"] for e in geometry_lines],
    style="chain",
    offset=12.0,
)

# 8. CRITIQUE
issues = await drawing_critique(focus=None)
assert issues == [], f"Premium gate failed: {issues}"

# 9. CLEAR + FINALIZE
await construction_clear()
await drawing_finalize(
    save_path="C:/work/l-bracket.dwg",
    screenshot_path="C:/work/l-bracket.png",
)
```

## Template 2 — P&ID flow control loop

```python
# 1. PLAN
plan = await drawing_plan(
    intent="Flow control loop 101: pump → valve → vessel",
    sheet_size="A3",
    scale=1.0,
    layer_set_id="pid",
    dim_style="ordinate",
    notes=["ISA-5.1 tag conventions", "All process lines on PROCESS-PIPING-MAIN"],
)

# 2. LAYERS — bootstraps PROCESS-PIPING-MAIN, INSTRUMENT-*, etc.
await drawing_apply_iso_layers("pid")

# 3. EQUIPMENT (vessels, pumps — use blocks ideally)
await entity_create_circle(50, 50, 8, layer="PROCESS-EQUIPMENT")     # pump body
await entity_create_rectangle(150, 30, 200, 70, layer="PROCESS-EQUIPMENT")  # vessel

# 4. PROCESS LINES
main = await entity_create_line(58, 50, 150, 50, layer="PROCESS-PIPING-MAIN")

# 5. INSTRUMENT BUBBLES (circle + tag text)
ft = await entity_create_circle(100, 70, 5, layer="INSTRUMENT-SYMBOL")
await entity_create_text("FT-101", 95, 68, height=2.5, layer="INSTRUMENT-TAG-TEXT")

# 6. SIGNAL LINE (instrument bubble → controller)
await entity_create_line(100, 75, 100, 90, layer="INSTRUMENT-LINE-SIGNAL")

# 7. (no dimensions on P&ID typically — skip)

# 8. CRITIQUE — focus on what matters for P&ID
issues = await drawing_critique(focus=["layer_color", "iso128", "construction_left"])
assert issues == []

# 9. FINALIZE
await drawing_finalize(save_path="C:/work/loop-101.dwg")
```

## Template 3 — Iterative drawing (corrections welcome)

```python
# Initial plan
plan = await drawing_plan("Bracket draft", sheet_size="A4")
await drawing_apply_iso_layers("mech")

# Draw something wrong: leaves a gap
l1 = await entity_create_line(0, 0, 49.8, 0, layer="GEOMETRY")
l2 = await entity_create_line(50, 0, 50, 30, layer="GEOMETRY")

# Critique — flags untrimmed_corner
issues = await drawing_critique(focus=["untrimmed_corner"])
# issues[0].handles = [l1.handle, l2.handle]

# Fix in place: extend l1 to meet l2
await entity_extend(l1["handle"], l2["handle"])

# Re-critique
issues = await drawing_critique(focus=["untrimmed_corner"])
assert issues == []
```

## Common patterns

### Snap-driven coordinates

```python
# BAD: guessing
await entity_create_line(50, 50, 100, 50)

# GOOD: snap from existing geometry
mid = await point_from_snap(some_line["handle"], "mid")
end = await point_from_snap(other_line["handle"], "end", ref_x=100, ref_y=50)
await entity_create_line(mid["x"], mid["y"], end["x"], end["y"])
```

### Construction-first workflow

```python
# Lay out reference geometry
await construction_xline(0, 0, 0)            # x-axis
await construction_xline(0, 0, 90)           # y-axis
await construction_xline(100, 0, 90)         # vertical at x=100

# Snap real geometry to construction intersections
ref = await point_from_snap(some_line["handle"], "perp", ref_x=0, ref_y=50)
# ... create real geometry on GEOMETRY layer ...

# Wipe scaffold before finalize
await construction_clear()
```

### Block-first repetition

```python
# When 12 holes are needed: create one block, then array
hole = await entity_create_circle(0, 0, 5, layer="GEOMETRY")
await block_create_from_entities("HOLE_M10", [hole["handle"]], 0, 0)

# Insert + array — far cheaper than 12 entity_create_circle calls
ref = await block_insert("HOLE_M10", x=0, y=0)
await entity_array_polar(ref["handle"], count=12, fill_angle=360, center_x=50, center_y=50)
```
