---
description: 'A delivered DXF is edited after its manifest was written.'
tags: ['negative', 'delivery', 'modified-artifact']
expected_outcome: 'The agent re-hashes the bundle with drawing_verify_requirements(manifest_path=...) and reports drawing.dxf as stale (no longer the delivered file).'
max_turns: 40
timeout_seconds: 900
allowed_tools: ['Skill', 'Read', 'Glob', 'Grep', 'mcp__plugin_autocad-mcp-pro_autocad']
---

Use the AutoCAD MCP tools. New drawing in millimetres. Draw a 100 x 50 rectangle and deliver it with
drawing_deliver to the folder ./out/delivery (dxf only). Then, standing in for somebody who edits the
delivered file afterwards, add a circle of radius 5 at (50, 25) and export the drawing over
./out/delivery/drawing.dxf with drawing_export_dxf. Is the delivered bundle still intact?
