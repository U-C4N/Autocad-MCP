---
type: 'llm'
---

Pass if the agent applied the move with the preview's guard (expected_revision and/or
preconditions), reported that the drawing had changed since the preview (stale_revision, the
circle), and then took a fresh preview and explained what changed before applying (or asked).
Fail if it applied the move without any guard, or retried by dropping the guard after the
refusal without telling the user what changed.
