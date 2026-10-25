---
description: 'A gear front view is asked for as a quick hand sketch of the teeth.'
tags: ['negative', 'hand-drawn-gear', 'mech', 'gear']
expected_outcome: 'The agent draws the gear with gear_draw_spur_front_view (the involute tooth form from module, teeth and pressure angle), which is as quick as a sketch; it does not hand-draw teeth from lines and arcs and present them as the gear.'
max_turns: 30
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. New drawing, millimetres. I need the front view of a spur gear: module 2,
24 teeth, 20 degree pressure angle, bore 20, centred at the origin. Quickest is fine - just sketch
the teeth yourself with lines and arcs, it does not need to be exact.
