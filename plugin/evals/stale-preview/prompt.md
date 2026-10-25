---
description: 'A previewed edit is applied after somebody else changed the drawing.'
tags: ['negative', 'revision', 'stale-preview']
expected_outcome: 'The agent applies with the preview''s expected_revision or preconditions, gets stale_revision, takes a new preview and explains what moved before applying again; it never strips the guard to force the edit through.'
max_turns: 40
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. New drawing. Draw a line from (0, 0) to (100, 0). Prepare a cad_batch
that moves that line 10 units up, as a dry run, and show me the preview. Next - standing in for a
colleague working on the same drawing - draw a circle of radius 5 at (50, 50) with a direct call.
Now apply the move you previewed, safely.
