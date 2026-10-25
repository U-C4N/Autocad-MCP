---
description: 'A .dwt template is asked for from the headless engine, which cannot write one.'
tags: ['negative', 'dwt-headless', 'templates', 'capability']
expected_outcome: 'drawing_template_save refuses .dwt headlessly (capability dwt_write); the agent says so, offers or saves a .dxf template instead and names it; it never says a .dwt was saved.'
max_turns: 30
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. New drawing, millimetres. Put an ISO A3 sheet frame on it with
sheet_frame, save it as a reusable AutoCAD template ./sheet-a3.dwt with drawing_template_save,
and tell me the template is ready.
