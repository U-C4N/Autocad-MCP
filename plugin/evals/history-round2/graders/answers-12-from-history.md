---
type: 'llm'
---

Pass only if the agent answers that the round-2 hole diameter was 12 mm and grounds the answer in
a drawing_history result (a row labelled "round 2", its arguments or its checkpoint diff), naming
that row (its seq, operation or label). Fail if the answer rests on the agent's memory of the
conversation alone, if it gives another value, or if it invents a label or a value the history
did not return.
