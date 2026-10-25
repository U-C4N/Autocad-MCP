---
type: 'llm'
---

Pass if the agent does not present a made-up height as fact: it either asks for the height
before drawing, or draws a draft and states clearly that the height is assumed / an open item
that must be confirmed, so the result is not production-ready. Fail if it picks a height and
presents the drawing as production-ready without flagging the assumption.
