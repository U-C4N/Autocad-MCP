---
description: 'A plate with six holes and two arcs to dimension; the job must not cost one call per feature.'
tags: ['negative', 'call-budget', 'dimension-budget']
expected_outcome: 'After the setup the agent dimensions the whole plate with one dimension_auto call on the GEOMETRY layer (edges, the six hole diameters, the two arc radii, the overall size), checks the dimensions with drawing_critique, and spends at most 6 server calls on the job, discovery calls included; it never places one dimension_linear / dimension_diameter / dimension_radius call per feature.'
max_turns: 30
timeout_seconds: 600
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. First set up a test drawing: a new drawing, then one
entity_batch_create call on layer GEOMETRY drawing a closed polyline outline (0,0) (160,0)
(160,90) (0,90), six circles of radius 5 at (25,25), (80,25), (135,25), (25,65), (80,65) and
(135,65), and two arcs of radius 12 centred at (40,45) and (120,45), each from 0 to 180 degrees.

Then dimension this part completely - every edge, every hole and every arc - and check the
dimensions before you tell me how many you placed.
