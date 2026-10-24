# Example — Simple P&ID Flow Control Loop

ISA-5.1 tagged flow loop: pump P-101 → flow transmitter FT-101 → control valve
FCV-101 → vessel V-201. Drawn on A3 with the `pid` layer set.

## Layout

```text
   ┌────┐                  ╭──╮               ┌────────┐
   │P101├──────────────────┤FT├────────╳──────│ V-201  │
   └────┘  PROCESS-MAIN    ╰┬─╯  PROCESS │    │        │
                            │ signal     │FCV │        │
                            ↓            │101 │        │
                          (FIC101)       │    └────────┘
                          controller     │
                                        ─┴─ control output
```

## Full workflow

```python
# 1. PLAN
plan = await drawing_plan(
    intent="Flow control loop 101: P-101 → FT-101 → FCV-101 → V-201",
    sheet_size="A3",
    scale=1.0,
    layer_set_id="pid",
    dim_style="ordinate",   # P&ID typically minimal dim
    notes=["ISA-5.1 tag conventions", "Pneumatic signal lines dashed"],
)

# 2. P&ID LAYERS
await drawing_apply_iso_layers("pid")

# 3. EQUIPMENT — pump (circle) and vessel (rectangle)
pump = await entity_create_circle(40, 60, 6, layer="PROCESS-EQUIPMENT")
await entity_create_text("P-101", 33, 50, height=3.0, layer="INSTRUMENT-TAG-TEXT")

vessel = await entity_create_rectangle(180, 40, 230, 80, layer="PROCESS-EQUIPMENT")
await entity_create_text("V-201", 195, 60, height=3.5, layer="INSTRUMENT-TAG-TEXT")

# 4. PROCESS LINE — main horizontal (heavy)
main_line_a = await entity_create_line(46, 60, 110, 60, layer="PROCESS-PIPING-MAIN")
main_line_b = await entity_create_line(110, 60, 150, 60, layer="PROCESS-PIPING-MAIN")
main_line_c = await entity_create_line(150, 60, 180, 60, layer="PROCESS-PIPING-MAIN")

# 5. INSTRUMENT BUBBLES — flow transmitter, control valve
ft = await entity_create_circle(110, 60, 4, layer="INSTRUMENT-SYMBOL")
await entity_create_text("FT", 107, 60.5, height=2.0, layer="INSTRUMENT-TAG-TEXT")
await entity_create_text("101", 106, 57.5, height=2.0, layer="INSTRUMENT-TAG-TEXT")

# control valve symbol = bowtie (2 triangles) — built from lines for now
fcv_top1 = await entity_create_line(150, 60, 154, 64, layer="PROCESS-VALVES")
fcv_top2 = await entity_create_line(150, 60, 154, 56, layer="PROCESS-VALVES")
fcv_bot1 = await entity_create_line(154, 64, 158, 60, layer="PROCESS-VALVES")
fcv_bot2 = await entity_create_line(154, 56, 158, 60, layer="PROCESS-VALVES")
await entity_create_text("FCV-101", 145, 70, height=2.5, layer="INSTRUMENT-TAG-TEXT")

# 6. SIGNAL LINES — FT bubble up to controller, then to FCV (dashed)
sig1 = await entity_create_line(110, 64, 110, 90, layer="INSTRUMENT-LINE-SIGNAL")
controller = await entity_create_circle(110, 95, 5, layer="INSTRUMENT-SYMBOL")
await entity_create_text("FIC", 107, 95.5, height=2.0, layer="INSTRUMENT-TAG-TEXT")
await entity_create_text("101", 106, 92.5, height=2.0, layer="INSTRUMENT-TAG-TEXT")
sig2 = await entity_create_line(110, 90, 154, 90, layer="INSTRUMENT-LINE-SIGNAL")
sig3 = await entity_create_line(154, 90, 154, 64, layer="INSTRUMENT-LINE-SIGNAL")

# 7. (no dimensions — P&ID is schematic, not dimensioned)

# 8. CRITIQUE — focus on what matters for P&ID
issues = await drawing_critique(focus=["layer_color", "iso128", "construction_left"])
assert issues == [], f"Premium gate failed: {issues}"

# 9. FINALIZE
await titleblock_apply_iso_a3(
    title="P&ID — Flow Control Loop 101",
    drawing_number="ANK-PID-2026-001",
    revision="0",
    scale="NTS",
    drawn_by="UE",
    company="Anka-Makine",
)

await drawing_finalize(
    save_path=r"C:\work\anka\pid-loop-101.dwg",
    screenshot_path=r"C:\work\anka\pid-loop-101.png",
)
```

## What this example demonstrates

- **`layer_set_id="pid"`** in `drawing_plan` triggers the P&ID layer bootstrap
  (PROCESS-PIPING-MAIN, INSTRUMENT-SYMBOL, INSTRUMENT-LINE-SIGNAL, etc).
- **Layer-by-purpose**: process lines on PROCESS-PIPING-MAIN (heavy 0.7 mm),
  signal lines on INSTRUMENT-LINE-SIGNAL (dashed 0.18), tags on
  INSTRUMENT-TAG-TEXT.
- **ISA-5.1 tags** in text: `FT-101`, `FCV-101`, `FIC-101`, `P-101`, `V-201`.
- **No dimensions** — P&IDs are schematic. We pass a focused critique that
  doesn't run dimension overlap checks.
- The same `drawing_finalize` infrastructure that handles mechanical drawings
  works for P&IDs without modification.

## Where to grow

V2/V3 features that would polish this example:
- Symbol library blocks for pumps, valves, instruments (`block_insert`).
- ISO 14617 graphic-symbol catalog as MCP tools.
- Equipment-tag and line-tag auto-numbering.
