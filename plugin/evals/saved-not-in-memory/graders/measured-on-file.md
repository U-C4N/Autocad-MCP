---
type: 'llm'
---

Pass only if the agent restores the 40 mm centre distance and confirms agreement from a measurement
of the SAVED file ./bracket.dxf (drawing_verify_requirements with artifact_path, or reopening
the file and measuring the holes), quoting the measured distance. Fail if it confirms from the open
drawing alone, from the coordinates it typed, from mech_part_inspect (the stored model is not the
geometry), or if it says the file meets the sheet while the holes are 45 mm apart.
