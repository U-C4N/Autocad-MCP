---
type: 'llm'
---

Pass if the final answer reports hole h1's centre at x = 25 (measured on the geometry, e.g. via
entity_get, point_from_snap or drawing_verify_requirements) and says the stored part model /
ACADMCP_MECH data (x = 20) no longer matches the drawing. Fail if it confirms x = 20, or bases
its answer only on mech_part_inspect or on the numbers it typed.
