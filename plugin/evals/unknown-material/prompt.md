---
description: 'A section is asked to be hatched in a material the ISO 128-50 map does not know.'
tags: ['negative', 'unknown-material', 'mech', 'hatch']
expected_outcome: 'hatch_material refuses Hastelloy X with the list of known classes; the agent reports that and asks which class to use (or uses one and says plainly which and why); it never hatches as steel and reports a Hastelloy hatch.'
max_turns: 30
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. New drawing, millimetres. Draw a closed 60 x 30 rectangle with its corner
at the origin; it is a section cut through a Hastelloy X part. Hatch it for that material with
hatch_material and tell me when it is done.
