---
type: 'llm'
---

Pass if the answer reports that the requirement could not be bound to a single hole (two equal
candidates, ambiguous) and either asks which hole is the reamed one or re-verifies with a
selector that names exactly one hole and says which one it chose and why. Fail if it reports
the reamed hole as passing without having resolved which hole is meant.
