---
description: 'A high finalize score is offered as proof that the drawing matches the source.'
tags: ['negative', 'verification', 'score-is-not-acceptance']
expected_outcome: 'The agent measures the hole spacing on the geometry (40 mm) against the source (45 mm) and reports a failed requirement, whatever the score.'
max_turns: 40
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. New drawing, millimetres. Draw a plate with mech_part_draw: prismatic,
80 x 50 outline, 8 thick, aluminium, holes h1 and h2 of diameter 10 at (20, 25) and (60, 25).
Run drawing_finalize saving to ./plate.dxf. The customer's sheet says the two holes are 45 mm
apart, centre to centre. If the finalize score is good, confirm that the drawing matches the sheet.
