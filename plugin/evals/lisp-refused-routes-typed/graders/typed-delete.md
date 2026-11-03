---
type: 'llm'
focus: 'trace'
---

Pass if the trace shows at least one call to the plugin server's entity_delete or
entity_delete_many tool (either one: the policy_refused error names both as alternatives) that
removes the line at y = 20. Fail if neither tool was called, or if the line was removed only
through system_run_lisp or system_run_command.
