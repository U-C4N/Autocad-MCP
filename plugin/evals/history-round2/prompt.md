---
description: 'Three rounds on one part, then the value of round 2 - read from the drawing''s own history, not recalled.'
tags: ['negative', 'history-round2', 'history', 'durability']
expected_outcome: 'The agent labels the rounds as it draws them (_meta.label or cad_batch(label=)), saves ./part.dxf, and answers the round-2 question with drawing_history (label="round 2", detail="args" - or detail="changes" for an unlabelled round): diameter 12, citing the history row; it never answers from its own memory of the conversation alone, and never guesses a label the history does not carry.'
max_turns: 40
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. New drawing, millimetres. We work in rounds on one plate, drawn with
mech_part_draw: prismatic, outline 80 x 50 with its corner at the origin, 8 thick, aluminium, one
hole h1 at x 40, y 25, no dimensions. Round 1: the hole is diameter 10. Round 2: draw the plate
again 200 mm to the right with the hole at diameter 12. Round 3: once more, another 200 mm to the
right, diameter 16. Label each round's call ("round 1", "round 2", "round 3") so it can be found
later, and save the drawing as ./part.dxf after each round. Then imagine a colleague asks
tomorrow, in a new session: "what was the hole diameter in round 2?" Answer it the way that
colleague would have to - from the drawing's recorded history, not from this conversation - and
name the history row that shows it.
