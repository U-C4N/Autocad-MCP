---
description: 'Thirty entities on three misnamed layers with colour overrides; the clean-up must not cost one call per entity.'
tags: ['negative', 'call-budget', 'layer-cleanup-budget']
expected_outcome: 'After the setup the agent merges the three layers into GEOMETRY and sets every colour and lineweight to ByLayer with set-based tools (layer_merge and entity_modify_where, ideally in one cad_batch), checks the result with drawing_critique, and spends at most 6 server calls on the clean-up, discovery calls included; it never edits the entities one call each.'
max_turns: 30
timeout_seconds: 600
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. First set up a test drawing: a new drawing, then one
entity_batch_create call drawing 30 short horizontal lines (each 5 units long, 3 units apart in
y), ten on each of the layers "geom", "Outline_old" and "contour", every one with colour 1 (red).

Then clean the drawing up the way a drafter would: every entity on layer GEOMETRY, the three
misnamed layers gone, and every entity's colour and lineweight ByLayer. Prove it is clean and tell
me what you changed.
