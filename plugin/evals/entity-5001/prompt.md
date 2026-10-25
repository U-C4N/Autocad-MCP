---
description: 'Leftover construction geometry sits past the 5,000th entity.'
tags: ['negative', 'qa', 'entity-5001']
expected_outcome: 'The agent checks with drawing_critique(focus=None) (a full scan) and reports the leftover CONSTRUCTION entity; it never declares the drawing clean from a capped entity_list page.'
max_turns: 50
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. New drawing. With entity_batch_create draw 5,001 short horizontal lines
(each 1 unit long, spaced 2 units apart in y) on layer GEOMETRY, except that the very last one goes
on layer CONSTRUCTION. Then tell me whether the drawing is clean enough to deliver - in particular,
whether any construction geometry is left.
