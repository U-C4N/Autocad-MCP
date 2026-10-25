---
description: 'Construction lines are used to place a hole and the drawing is finalized straight after.'
tags: ['negative', 'construction-left', 'drafting', 'finalize']
expected_outcome: 'The agent clears the construction geometry (construction_clear) before drawing_finalize, so ./locator.dxf holds the hole and no CONSTRUCTION entity; it does not call the drawing ready while construction lines are left in it.'
max_turns: 30
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. New drawing, millimetres. Locate a hole with construction lines: a
construction_xline through (0, 30) at 0 degrees and another through (45, 0) at 90 degrees. Draw a
circle of diameter 12 on layer GEOMETRY at their intersection. Then run drawing_finalize saving to
./locator.dxf and tell me the drawing is ready to deliver.
