---
name: autocad-mcp-premium
description: Use when working with AutoCAD MCP Pro tools and the user asks for technical drawings, P&ID, mechanical engineering output, ISO-conformant drawings, premium-quality CAD, or mentions trim/extend/fillet/chamfer, layer discipline, or "premium drawing". Apply the plan→draw→critique workflow strictly.
---

# AutoCAD MCP Premium Drafting Skill

Premium drafting discipline for the AutoCAD MCP Pro server. The goal: ISO-conformant
output that a real draftsman would sign. The path: a strict workflow + the corner
ops + meta-tool layer that ship with the server.

## When this skill applies

- The user asks for a **technical drawing**, **P&ID**, **mechanical part**, or
  any drawing intended for production / fabrication / approval.
- The user mentions **TRIM, EXTEND, FILLET, CHAMFER**, **OSNAP**, **ISO 128/129/
  13567**, **layer discipline**, or **premium drawing**.
- You are about to call `entity_create_*` for a deliverable drawing — **stop and
  apply the workflow below first**.

## The 9-step Premium Workflow (non-negotiable)

```text
1. drawing_plan(intent, sheet_size, scale)        # commit intent BEFORE drawing
2. drawing_apply_iso_layers("mech" | "pid" | "iso13567")
3. construction_xline(...)                        # scaffolding (optional)
4. entity_create_line / circle / arc / ...        # geometry on engineering layers
5. entity_trim / entity_fillet / entity_chamfer   # close corners explicitly
6. handles = entity_select_smart({type, layer, ...})  # don't memorise handles
7. dimension_auto(handles, style="chain"|"baseline"|"ordinate")
8. issues = drawing_critique(focus=None)          # must be []
9. construction_clear()
10. drawing_finalize(save_path=..., screenshot_path=...)
```

If any of these is skipped, the drawing is **not premium** — it's a sketch.

## The 8 Non-Negotiable Rules (mirror of CLAUDE.md)

1. **Plan before draw** — `drawing_plan` first; PlanSpec is the contract.
2. **No coordinate guessing** — `point_from_snap(handle, snap, ref_x, ref_y)`.
3. **Layer discipline** — engineering layers only; CONSTRUCTION for scaffold.
4. **Lineweights are ISO 128** — bootstrap via `drawing_apply_iso_layers`.
5. **Corners are explicit** — `entity_trim` / `entity_fillet` / `entity_chamfer`.
6. **Dimensions via auto** — `dimension_auto`, not loops of `dimension_linear`.
7. **Critique then finalize** — `drawing_critique` must return `[]`.
8. **Meta-tools over raw** — never reimplement what a meta-tool already does.

## Tool Inventory (this skill assumes server has these registered)

| Group | Tools | Count |
|-------|-------|-------|
| Drawing | `drawing_*` (info/new/open/save/export/finalize/...) | 11 |
| Entity Create | `entity_create_line/circle/arc/polyline/text/...` | 12 |
| Dimensions | `dimension_linear/aligned/angular/radius/diameter` | 5 |
| Entity Modify | `entity_move/copy/rotate/scale/mirror/offset/delete/...` | 10 |
| **Corner Ops** ⭐ | `entity_trim` `entity_extend` `entity_fillet` `entity_chamfer` | **4** |
| Entity Query | `entity_get/list/delete_many` | 3 |
| Layer Mgmt | `layer_*`, `linetype_list/load` | 14 |
| Block Ops | `block_*` | 7 |
| Analysis | `analysis_*` | 8 |
| View | `view_*` | 4 |
| Transactions | `transaction_*` | 3 |
| System | `system_*` | 6 |
| Engineering | `gear_*`, `keyway_*`, `titleblock_*`, `drawing_finalize` | 7 |
| **Premium Meta** ⭐ | `drawing_plan` `drawing_critique` `point_from_snap` `construction_xline/clear` `drawing_apply_iso_layers` `dimension_auto` `entity_select_smart` | **8** |

## Reference Documents (read on demand)

- [`references/corner-ops.md`](references/corner-ops.md) — trim/extend/fillet/chamfer
  semantics, decision tree (when to use which), corner-case table.
- [`references/meta-planning.md`](references/meta-planning.md) — `drawing_plan`,
  `drawing_critique`, `point_from_snap` deep dive + PlanSpec / Issue schema.
- [`references/iso-standards.md`](references/iso-standards.md) — ISO 128 line
  widths, ISO 13567 layer naming, ISO 129 dimension rules — printable tables.
- [`references/premium-workflow.md`](references/premium-workflow.md) — full
  worked examples of the 9-step workflow with code blocks.

## Examples

- [`examples/premium-shaft.md`](examples/premium-shaft.md) — Shaft with keyway
  (Anka-Makine reference part).
- [`examples/pid-simple-loop.md`](examples/pid-simple-loop.md) — P&ID flow
  control loop with ISA-5.1 tags.

## Anti-patterns to refuse

- Calling `entity_create_*` before `drawing_plan` for a deliverable drawing.
- Computing endpoint/midpoint/intersection coordinates by hand instead of
  `point_from_snap`.
- Drawing on layer 0 (or any non-engineering layer).
- Leaving the CONSTRUCTION layer populated at finalize time.
- Hand-rolling trim/extend math when `entity_trim` / `entity_extend` exists.
- Skipping `drawing_critique` and going straight to `drawing_finalize`.

When the user requests a premium drawing, **walk the 9 steps in order** and
narrate each call. If a critique returns non-empty issues, fix them in place
before proceeding to finalize.
