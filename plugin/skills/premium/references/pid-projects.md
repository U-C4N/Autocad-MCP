# P&ID projects

A P&ID is a **schematic**: it states what is connected to what, with which
tags, line classes and sizes. It does not state where anything is or how long
a pipe is. Verify connections and tags, not physical size.

## Drawing a sheet

- The catalogue (ISO 10628-2 + ISA-5.1) is the `autocad://pid/symbols`
  resource; `pid_symbol_list` filters it. ISA-5.1 letters are in
  `autocad://standards/isa51`; check a tag with `pid_tag_parse` before you
  place it.
- A whole sheet is one call: `pid_from_spec(spec={sheet, equipment, valves,
  instruments, lines, connectors})`, inside one transaction - a bad item rolls
  the sheet back and names its path (`lines[2].to`). Run it with
  `dry_run=True` first: it returns the routes, the crossings and a `preview`
  whose precondition is every entity in the routing region; apply with that
  `expected_revision=` / `preconditions=`.
- Single symbols and lines: `pid_symbol_insert` (real blocks with a TAG
  attribute, named ports, `ACADMCP_PID` XDATA) and `pid_line_draw` (port to
  port, orthogonal, ISA-5.1 line classes).
- Lines go round the vessels with `pid_line_draw(route="avoid", clearance=...,
  keep_out=[[x0, y0, x1, y1]])`, or for a whole sheet `sheet.routing: "avoid"`
  in `pid_from_spec` (the default `auto` does not look at symbols). No way
  round is `no_route` naming the enclosing tags; `on_blocked="cross"` draws
  through and lists what it entered in `obstacle_hits`.
- The response carries the graph read back and the P&ID critique issues:
  `pid_dangling_line`, `pid_duplicate_tag`, `pid_incompatible_connection`,
  `pid_untagged_instrument`, `pid_illegal_tag`, `pid_unconnected_equipment`,
  `pid_line_through_symbol`. A sheet is done when they are empty and
  `obstacle_hits` is empty.

## A project of several sheets

`pid_project(action="check", sheets=[...])` (or `manifest=`) reads up to 50
sheet files without modifying any: off-page links matched across files
(`matched` / `orphan` / `direction_mismatch` / `duplicate`), cross-sheet
duplicate tags and line-number continuity. `action="index"` merges the
instrument index, line list and equipment list with a `sheet` column. The last
result is the `autocad://pid/project/last` resource. A `.dwg` sheet is read
only on a live seat.

## Reading a sheet

`pid_graph` never modifies the drawing. Every node carries `source` and
`confidence` (catalog 1.0, xdata 0.95, heuristic 0.6, inferred 0.3). Read
`stats.confidence_min` before trusting a foreign P&ID: at 0.6 or below, the
lists that follow are a heuristic reading, and you say so.

The deliverables `pid_instrument_index`, `pid_line_list` and
`pid_equipment_list` are reads of that graph; with `csv_path=` they write a
CSV (text cells that start with `=`, `+`, `-`, `@` are escaped, so a tag can
never become a spreadsheet formula). They never modify the drawing.

## What a P&ID cannot answer

- **Lengths.** A drawn line length is not metres of pipe. Before any length
  leaves a P&ID, `drawing_scale_check(reference=<layout>, path=<pid>)` must
  call it `to_scale`; otherwise lengths come from the layout
  ([takeoff-evidence](takeoff-evidence.md), [the schematic example](../examples/schematic-pid.md)).
- **Sizes it does not print.** An unlabelled line stays `unassigned`; `Ø51`,
  `SMS51` and `DN20` are kept as written, never equated.

## Not yet (1.8.1)

A project-wide tag sequence, and refusing a tag another sheet already holds:
`pid_project` reads a project, it does not write one.
