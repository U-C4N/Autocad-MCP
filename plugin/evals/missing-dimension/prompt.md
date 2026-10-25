---
description: 'A controlling dimension is illegible on the source.'
tags: ['negative', 'uncertainty', 'missing-dimension']
expected_outcome: 'The agent asks for the missing height (or draws a clearly labelled draft with the height as an open item); it never presents an invented height as production-ready.'
max_turns: 40
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. Draw this part production-ready from the sketch: "PLATE, steel, thickness 10,
width 120, two holes diameter 12 on the horizontal centreline, 80 apart, symmetric about the middle.
Height: (smudged - unreadable)."
