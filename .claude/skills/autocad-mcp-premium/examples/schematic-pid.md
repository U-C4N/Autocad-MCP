# Example: a schematic P&ID is not a length source

**Ask (synthetic plant):** "How many metres of product pipe are on this P&ID?"
You have the P&ID (`pid.dxf`) and the plant layout (`layout.dxf`) of the same
plant.

## 1. Is the P&ID to scale?

```json tool=drawing_scale_check
{"reference": "layout.dxf", "path": "pid.dxf"}
```

On the synthetic pair the answer is `verdict: "schematic"`, `factor: null`:
only 22 % of 55 tag pairs lie within +/-10 % of the median distance ratio
(1.04, IQR 0.56). The P&ID's line lengths are lengths of drawn line.

## 2. The shortcut is refused

```json tool=pipe_takeoff
{"pid_path": "pid.dxf", "layout_path": "layout.dxf", "length_source": "pid"}
```

```text
Invalid params: length_source='pid': the scale check calls this P&ID schematic -
22% of 55 tag pairs lie within ±10 % of the median ratio 1.03975 (IQR 0.564509);
a length measured on it is drawn line, not pipe. Pass force=True to measure on it
anyway, or use length_source='layout'.
```

Do not add `force=True` to get a number. It exists for a person who knows the
sheet was drawn to scale despite the statistics, and the report then says so.

## 3. Lengths from the layout

```json tool=pipe_takeoff
{"pid_path": "pid.dxf", "layout_path": "layout.dxf", "length_source": "auto",
 "output": "takeoff.xlsx", "lang": "en"}
```

Topology (which equipment a run connects, its service and its sizes as
labelled) comes from the P&ID; each run's length is the rectilinear minimum
spanning tree of its tags on the layout plus the vertical allowance - an
estimate, labelled so, with the drawn P&ID length beside it as `schematic_m`.

## What to tell the person

- the total by service and size, called an **estimate from the layout**;
- the scale-check numbers that rule the P&ID out as a length source;
- every C-status run (an end on no equipment, a tag missing or doubled on the
  layout) and every `unassigned` size, as open items;
- that a size such as `SMS51` was kept as written, not converted to a DN.

Never: "the P&ID shows 184 m of pipe".
