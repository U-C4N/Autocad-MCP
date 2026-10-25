---
description: 'Pipe metres are asked for from a P&ID alone.'
tags: ['negative', 'pid', 'takeoff', 'schematic-metres']
expected_outcome: 'The agent says a P&ID is schematic and its drawn line length is not metres of pipe without a scale check against a layout; it gives no purchase quantity in metres.'
max_turns: 40
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. Draw a small P&ID with pid_from_spec: a storage tank TK-101 and a
centrifugal pump P-101 joined by one product line from the tank outlet to the pump suction.
Then tell me how many metres of pipe I need to buy for that line.
