---
type: 'llm'
focus: 'trace'
---

Count the calls the agent made to the plugin server's tools (every
mcp__plugin_autocad-mcp-pro_autocad__* call: search_tools, describe_tool and call_tool count, and a
cad_batch is one call however many steps it carries) AFTER the setup's entity_batch_create call
that drew the plate. Pass if that count is 6 or fewer and the plate was dimensioned. Fail if it is
more than 6, or if the dimensions were placed one dimension_linear, dimension_diameter or
dimension_radius call per feature.
