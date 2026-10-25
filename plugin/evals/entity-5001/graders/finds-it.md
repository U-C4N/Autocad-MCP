---
type: 'llm'
---

Pass if the answer reports that construction geometry is left (the CONSTRUCTION line) and that
the drawing is therefore not ready to deliver until it is cleared, based on a full check such
as drawing_critique. Fail if it declares the drawing clean, or if its conclusion rests on a
single entity_list page that cannot have seen all 5,001 entities.
