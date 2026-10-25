# Takeoff evidence

A quantity is only as good as the drawing it was measured on. Every number
you hand over says where it came from, and a number nobody stated stays empty.

## Which drawing gives lengths

1. `drawing_scale_check(reference=<layout.dxf>, path=<pid.dxf>)` compares tag
   positions on both. Verdicts: `to_scale` (80 % or more of tag pairs within
   +/-10 % of the median ratio; `factor` is that ratio), `partly`,
   `schematic` (50 % or less), `insufficient` (under three matched tags).
2. Only a `to_scale` P&ID is a length source. Otherwise lengths come from the
   layout: the rectilinear minimum spanning tree of the tags a run connects,
   plus `vertical_allowance` per connection - an **estimate**, labelled so,
   with the P&ID's drawn length beside it as `schematic_m` for comparison.
3. `pipe_takeoff(length_source="pid")` on a P&ID the check calls schematic is
   refused (the refusal quotes the statistics). `force=True` overrides it; do
   that only when the person asks, and put the reason in the report.
4. Without a layout every length is the drawn length, marked
   `scale_verified: false`. Say "drawn length", never "metres of pipe".

Lengths are metres in the unit the geometry implies; an `INSUNITS` that
disagrees is a warning, never a factor of 25.4.

## Pipe takeoff

`pipe_takeoff(pid_path=..., layout_path=..., output=...)`: runs merged at
their ends and split at tees, a line drawn twice counted once, inline fittings
bridged. Sizes are kept exactly as labelled (direct, carried by continuity but
never across a reducer, or `unassigned`). Status per run: A, B, or C (an end
on no equipment, a tag missing from or doubled on the layout) - C rows are on
the Kontrol sheet, counted and flagged. Report the C rows; do not drop them.

## Cable takeoff

`cable_takeoff(pid_path=..., layout_path=..., output=...)`: power, phases and
voltage only from the P&ID's own texts - **never invented**; a missing kW is an
empty cell and an open item. Panels from the layout's wiring callouts; a load
no callout places takes the nearest panel flagged `panel_inferred`
(`panel_rule="stated"` leaves it open). Length is the Manhattan distance plus
the allowance, rounded up to the metre. A cable section only from your
`section_rules=`; no table is assumed.

## Revisions

For "what changed between revision A and B", run `drawing_diff` on the two
drawings and the takeoff on each; report the delta per row. The
`"revision_takeoff_delta"` recipe (`autocad://recipes/index`) is that chain.

## Files

Both takeoffs are read-only on the drawings and write one workbook (XLSX with
the `office` extra, CSV always). Text cells starting with `=`, `+`, `-`, `@`
are escaped in CSV and stored as strings in XLSX, so a tag can never become a
formula.
