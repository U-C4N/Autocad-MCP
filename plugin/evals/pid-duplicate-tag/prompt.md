---
description: 'An instrument index is asked for on a P&ID that carries one tag twice.'
tags: ['negative', 'pid-duplicate-tag', 'pid', 'tags']
expected_outcome: 'The agent finds that PI-101 is carried by two symbols (pid_duplicate_tag from drawing_critique, or the two index rows) and says the P&ID is not ready to issue until one is re-tagged; it does not hand over the index as clean.'
max_turns: 30
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. New drawing. With pid_symbol_insert place a field pressure indicator
(symbol instrument) tagged PI-101 at (0, 0), and another instrument symbol tagged PI-101 at (60, 0) -
that second one was copied from an older sheet. Give me the instrument index and tell me whether
the P&ID is ready to issue.
