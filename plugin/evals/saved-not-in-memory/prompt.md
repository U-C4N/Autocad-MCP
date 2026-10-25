---
description: 'A corrected drawing is saved; agreement with the sheet has to be measured on the saved file, not on the drawing in memory or the part model.'
tags: ['negative', 'saved-not-in-memory', 'artifact', 'verification']
expected_outcome: 'The agent moves hole h2 back so the centres are 40 mm apart (measured on the CAD), saves ./bracket.dxf and verifies the saved file with drawing_verify_requirements(artifact_path=...) (or by reopening and measuring it), quoting the measured 40 mm; it never confirms the sheet from the open drawing, the coordinates it typed or mech_part_inspect.'
max_turns: 40
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. New drawing, millimetres. Draw this plate with mech_part_draw: prismatic,
outline 80 x 50 with its corner at the origin, 8 thick, aluminium, holes h1 (diameter 10 at x 20,
y 25) and h2 (diameter 10 at x 60, y 25), no dimensions. Then - standing in for an operator's later
edit - move hole h2's circle 5 mm in +x with entity_move. The customer's sheet says: two holes of
diameter 10, centres 40 mm apart on y = 25, the left one at x = 20. Make the drawing meet the sheet,
save it as ./bracket.dxf, and confirm from the saved file that it does.
