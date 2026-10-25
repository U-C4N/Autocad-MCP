# Architecture

## The plan model

A wall is an axis, a thickness, a justification and a material
(`arch_wall`); a door or window is hosted in a wall (`arch_opening`). Never
hand-draw either with lines: the engine resolves L / T / X junctions, cuts the
openings and hatches cut faces by material, and lists what it cannot place in
`omitted`.

A whole plan is one call, inside one transaction:

```text
arch_plan_from_spec(spec={"sheet": {...}, "lang": "en", "walls": [...],
                         "openings": [...], "rooms": [...], ...})
```

It returns each step's result, `omitted` and the three critique focuses
`arch_room_unlabelled`, `arch_wall_gap`, `arch_opening_clash`. A refusal names
its path (`walls[1].thickness: ...`) and leaves the drawing untouched.
Furniture, grids and symbols are separate (`arch_catalogue_insert`,
`arch_grid`, `arch_symbol`); stairs are `arch_stair`, whose Blondel check
(2R + G within 600-650 mm) is a design rule, not a building code.

## Areas are measured, never typed

A room's area is the planar face its label point lies in, measured off the
walls just drawn (`arch_room`). No tool accepts an area from you;
`arch_plan_from_spec` refuses a typed `area` by path. To read rooms from any
plan - including one you did not draw - use `arch_rooms_detect` (read-only;
confidence 1.0 for our walls, 0.6 for plain lines). Never shoelace vertices
you read back; `analysis_measure_entity(handle)` measures real geometry and
states its own accuracy.

## Text and scale

`scale` is a denominator (50 = 1:50) and a paper text height `h` is drawn at
`h x scale` units. `lang="tr"` switches words and the decimal comma; any
other language is refused with the list.

## Schedules and dimensions

`arch_schedule` draws door / window / room schedules as real TABLEs read from
the `ACADMCP_ARCH` records; `arch_dimension_chains` adds exterior chains.
Furniture and sanitary blocks carry nominal catalogue sizes and say so.
