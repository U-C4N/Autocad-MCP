---
type: 'llm'
---

Pass if the answer says the bundle is no longer intact because drawing.dxf no longer matches
the hash recorded in manifest.json (stale), based on re-hashing it against the manifest. Fail
if it says the bundle is intact, or answers from the delivery result produced before the edit.
