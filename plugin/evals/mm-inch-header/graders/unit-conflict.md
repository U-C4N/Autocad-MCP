---
type: 'llm'
---

Pass if the answer points out that the drawing declares inches while the 100 x 50 values came
from a millimetre source, so the geometry is 100 x 50 drawing units, and it either fixes the
declared unit to millimetres or asks which is intended. Fail if it reports 2540 x 1270 mm (a
silent 25.4 conversion) or reports 100 x 50 mm without mentioning the declared-unit conflict.
