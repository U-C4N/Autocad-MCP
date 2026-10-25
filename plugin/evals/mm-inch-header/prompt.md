---
description: 'The drawing header declares inches over values taken from a millimetre source.'
tags: ['negative', 'units', 'mm-inch-header']
expected_outcome: 'The agent notices the declared unit (inches) disagrees with the millimetre source and reports it (or sets the drawing to millimetres); it never silently multiplies by 25.4.'
max_turns: 40
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. Start a new drawing and set its units to inches with drawing_settings.
Draw a rectangle 100 by 50 - those values come from a millimetre sketch. Then tell me the
rectangle's size in millimetres.
