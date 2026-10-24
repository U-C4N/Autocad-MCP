# ISO Standards Reference

Distilled tables for the standards this skill enforces.

## ISO 128 — Line widths

Allowed lineweight set (mm), with 4:2:1 ratio rule:

| Width | Common use |
|-------|------------|
| 0.13 | Hatch, dim text shadow |
| 0.18 | Hidden, center, phantom, dim text |
| 0.25 | Default text, leader |
| 0.35 | Auxiliary visible (where contrast needed) |
| 0.50 | Visible solid edges (most common production weight) |
| 0.70 | Heavy solid (large-scale parts), P&ID main process |
| 1.00 | Border, key heavy outlines |
| 1.40 | Title borders |
| 2.00 | Sheet outer border |

**Rule:** every layer's lineweight must be one of these (or 0.0 = "Default" =
inherits from active style). Anything else fails `drawing_critique` focus
`iso128`.

## ISO 128 — Line types

| Type | Linetype name | Used for |
|------|---------------|----------|
| Continuous thick | `Continuous` (0.50+ mm) | Visible edges |
| Dashed thin | `HIDDEN` (0.18–0.25) | Hidden edges |
| Chain thin | `CENTER` (0.18) | Centerlines, axes of symmetry |
| Chain double-dashed | `PHANTOM` (0.18) | Section / cutting plane / alternate position |
| Continuous thin | `Continuous` (0.18–0.25) | Dimensions, hatch, leaders |

## ISO 13567 — Layer naming (4-field)

Format: `<Agent2c>-<Element6c>-<Presentation2c>-<Status1c>`

| Field | Length | Examples |
|-------|--------|----------|
| Agent | 2 chars | A=Architectural, S=Structural, M=Mechanical, P=Plumbing, E=Electrical |
| Element | 6 chars | GEOMET=geometry, HIDDEN=hidden edges, DIMEN=dimensions, TITLE=title block |
| Presentation | 2 chars | E=edge, T=text, H=hatch |
| Status | 1 char | N=new, E=existing, D=demolish |

Example layer set ships in `engineering/layers.py::ISO13567_LAYERS`.

## ISO 129-1 — Dimensioning rules

1. **Each dimension is given once** — never repeat.
2. **Extension lines** project at least 8× line width past the dim line.
3. **Decimal separator** is the comma (per ISO; we accept point too).
4. **Text orientation**: read from below the drawing or from the right side.
5. **Font**: ISO 3098 (`isocp.shx` ships with AutoCAD).
6. **Dimension styles**: chain, baseline, ordinate are the three standard
   layouts — choose one per drawing and stick to it (`dim_style` in PlanSpec).

## ISO 7200 — Title-block fields

Mandatory:
- Legal owner (Anka-Makine)
- Drawing number
- Title
- Date
- Sheet / total sheets
- Approver name + signature
- Creator name
- Document type

Common optional:
- Revision
- Scale (matches PlanSpec.scale)
- Projection symbol (1st angle ISO E vs 3rd angle ISO A)

This skill assumes you call `titleblock_apply_iso_a3` (or its A3 cousin) to
populate these fields after `drawing_finalize`'s validator runs.

## ISO 14617 / 10628 — P&ID symbology

When `layer_set_id="pid"` is passed to `drawing_plan`, the bootstrap creates
the P&ID layer set:

| Layer | Linetype | Lineweight | Purpose |
|-------|----------|------------|---------|
| `PROCESS-PIPING-MAIN` | Continuous | 0.70 | Main process line (heavy) |
| `PROCESS-PIPING-SECONDARY` | Continuous | 0.50 | Secondary process line |
| `PROCESS-EQUIPMENT` | Continuous | 0.50 | Vessels / pumps / drums |
| `PROCESS-VALVES` | Continuous | 0.35 | Valves & fittings |
| `INSTRUMENT-SYMBOL` | Continuous | 0.35 | Instrument bubbles (FCFs) |
| `INSTRUMENT-LINE-SIGNAL` | DASHED | 0.18 | Pneumatic / electrical signal |
| `INSTRUMENT-TAG-TEXT` | Continuous | 0.25 | Tag labels |
| `ELECTRICAL-LINE` | DASHDOT | 0.18 | Electrical line |
| `UTILITY-LINE` | PHANTOM | 0.25 | Steam / water / air utility |
| `INSULATION-HATCH` | Continuous | 0.13 | Insulation hatching |

### ISA-5.1 tag conventions (P&ID instruments)

`<First letter><Second letter>-<Loop number><Suffix?>`

| First letter (measured variable) | Second letter (function) |
|----------------------------------|--------------------------|
| F = Flow | T = Transmitter |
| P = Pressure | I = Indicator |
| T = Temperature | C = Controller |
| L = Level | V = Valve |
| A = Analytical | S = Switch |

Examples: `FT-101` (flow transmitter, loop 101), `PCV-203A` (pressure control
valve, loop 203, A spare).
