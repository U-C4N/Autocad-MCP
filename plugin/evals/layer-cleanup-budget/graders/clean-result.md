---
type: 'llm'
---

Pass if the answer reports that all 30 entities are on GEOMETRY, that the layers geom,
Outline_old and contour no longer exist, and that colour and lineweight are ByLayer, and the claim
rests on a check the agent ran (drawing_critique with the property_override or layer_color focus,
or a listing read back after the change). Fail if it claims the drawing is clean without checking,
or reports lineweight -3 (Default) as ByLayer.
