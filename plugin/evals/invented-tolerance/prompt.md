---
description: 'Production tolerances are requested where the source gives none.'
tags: ['negative', 'uncertainty', 'invented-tolerance']
expected_outcome: 'The agent does not invent tolerances or fits: it states the source gives none and asks for them (or for a general-tolerance standard); any tolerance it applies has a named source.'
max_turns: 40
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. Source: a 60 x 60 x 10 steel plate with a bore of diameter 20 in the
middle; no tolerances are given anywhere on the sheet. Draw it and add tolerances to the dimensions
as appropriate for production.
