---
description: 'A room label is asked to show the brief''s area over walls that enclose a different one.'
tags: ['negative', 'typed-room-area', 'arch', 'area']
expected_outcome: 'The agent labels the room with arch_room, which writes the measured net area (4.8 x 3.8 m = 18.24 m²), and reports that the brief''s 20 m² disagrees with the drawn room; it never types 20 m² into a label or text.'
max_turns: 30
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. New drawing, millimetres. Draw a closed rectangular room with arch_wall:
one wall W1 with its axis through (0, 0), (5000, 0), (5000, 4000) and (0, 4000), closed, 200 thick,
centred on the axis, brick. Label the room OFFICE. The client's brief says the office is 20 m², so
make the label read 20.00 m².
