# Example — Premium Shaft Drawing

A fully worked example: power-transmission shaft Ø30 × 200 mm with two keyway
seats (10 × 4 × 50 mm each per DIN 6885), drawn at 1:2 on A4.

## Goal

```text
       50    100   50
    ┌──┴──┬───┴───┬──┴──┐
    │     │       │     │   keyway both ends, M0.04 surface finish
    └─────┴───────┴─────┘   bearing seat in middle (Ø30 h7)
    ────── 200 ──────
```

## Full workflow

```python
# 1. PLAN — commit intent first
plan = await drawing_plan(
    intent="Power transmission shaft Ø30 x 200, 2× DIN 6885 keyway",
    sheet_size="A4",
    scale=0.5,                      # 1:2 (drawing is half real size)
    layer_set_id="mech",
    dim_style="chain",
    notes=[
        "Material: C45 normalized",
        "Surface finish Ra 1.6 µm overall",
        "Keyways: 10 x 4 x 50 mm per DIN 6885 A",
    ],
)

# 2. LAYERS — mech standard
await drawing_apply_iso_layers("mech")

# 3. CONSTRUCTION — centerline as reference
cl = await construction_xline(0, 0, 0)  # horizontal centerline at y=0

# 4. GEOMETRY — front view (top half mirrored later)
top_edge = await entity_create_line(0, 15, 200, 15, layer="GEOMETRY")
right_edge = await entity_create_line(200, 15, 200, -15, layer="GEOMETRY")
bot_edge = await entity_create_line(200, -15, 0, -15, layer="GEOMETRY")
left_edge = await entity_create_line(0, -15, 0, 15, layer="GEOMETRY")

# 4b. KEYWAY LEFT — use the engineering primitive (per CLAUDE.md rule)
keyway_l = await keyway_draw_section(
    bore_x=25, bore_y=0, bore_diameter=30,
    keyway_width=10, keyway_depth=4,
)

# 4c. KEYWAY RIGHT — symmetric on the other end
keyway_r = await keyway_draw_section(
    bore_x=175, bore_y=0, bore_diameter=30,
    keyway_width=10, keyway_depth=4,
)

# 4d. Centerline (engineering layer, dashed)
center = await entity_create_line(-10, 0, 210, 0, layer="CENTER")

# 5. CORNERS — small chamfer at each end (1×45°)
await entity_chamfer(top_edge["handle"], left_edge["handle"], dist1=1)
await entity_chamfer(top_edge["handle"], right_edge["handle"], dist1=1)
await entity_chamfer(bot_edge["handle"], left_edge["handle"], dist1=1)
await entity_chamfer(bot_edge["handle"], right_edge["handle"], dist1=1)

# 6. SELECT geometry for dim
geometry = await entity_select_smart({
    "type": "LINE",
    "layer": "GEOMETRY",
    "length_range": [40, 220],   # exclude small chamfer lines
})

# 7. DIMENSIONS — chain dimensioning, 12 mm offset above the part
dims = await dimension_auto(
    [e["handle"] for e in geometry],
    style="chain",
    offset=12.0,
)

# 8. CRITIQUE — must be empty before finalize
issues = await drawing_critique(focus=None)
if issues:
    for i in issues:
        print(f"  [{i['severity']}] {i['focus']}: {i['message']}")
    raise RuntimeError("Premium gate failed; fix before finalize")

# 9. CLEAR + TITLEBLOCK + FINALIZE
await construction_clear()

await titleblock_apply_iso_a3(
    title="Transmission Shaft",
    drawing_number="ANK-SH-2026-001",
    revision="A",
    scale="1:2",
    material="C45 normalized",
    drawn_by="UE",
    company="Anka-Makine",
)

result = await drawing_finalize(
    save_path=r"C:\work\anka\sh-2026-001.dwg",
    screenshot_path=r"C:\work\anka\sh-2026-001.png",
    expected={
        "part_type": "shaft",
        "must_have_keyway": True,
        "must_have_bore": False,   # solid shaft, not bored
    },
)
```

## What this example demonstrates

- **Plan-first workflow** (rule 1)
- **Engineering primitives** for the keyway, not hand-drawn (rule 8 + CLAUDE.md
  production drawing rule)
- **Construction line** for the centerline reference, cleared before finalize
- **Corner explicitness** via `entity_chamfer` (rule 5)
- **`entity_select_smart`** with `length_range` to filter out the tiny chamfer
  lines (rule 8 — meta-tools over loops)
- **`dimension_auto`** with chain style (rule 6)
- **Critique gate** before finalize (rule 7)
- **`titleblock_apply_iso_a3`** + `drawing_finalize` with `expected` contract

The whole drawing comes out reproducible: the same call sequence generates an
identical DXF every time, suitable for `git diff` and ISO 9001 traceability.
