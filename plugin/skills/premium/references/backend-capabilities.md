# Backend capabilities

The server runs one of two engines; `system_status` says which, and
`system_capabilities` lists every feature with `supported` and a reason.
Ask it instead of assuming.

| | live (`com`) | headless (`ezdxf`) |
|---|---|---|
| What it drives | the running AutoCAD (Windows) | a DXF document in memory |
| DWG | read and write | `drawing_open` converts through ODA or `accoreconsole` when installed (`"dwg_read"`); saving onto a `.dwg` is refused (`"dwg"`); DWG export only with the ODA File Converter (`"dwg_write"`) |
| Commands and LISP | `system_run_command`, `system_run_lisp` | refused (`"lisp"`) |
| Operator interaction | `user_pick_point`, `user_select`, `system_prompt_message` | refused (`"interactive_prompt"`) |
| 3D solids | `solid_*` when `ENABLE_3D=true` | refused (`"solid_3d"`) |
| Undo / rollback | AutoCAD UNDO marks - the landing is not confirmed | full document snapshots |
| Checkpoints | taken by export; restore only as a new document (`"checkpoint_restore_in_place"`) | restore in place or as a new document; `drawing_undo` steps back to them after a restart |
| Views | real zoom and screenshots | zoom is a no-op; screenshots are rendered |
| A region picture: `view_screenshot(region=[x0, y0, x1, y1], max_px=...)` | the document's export, rendered (`source: "export_snapshot"`) | rendered |

A screenshot comes with a report: `applied`, `px`, `region` (the window drawn)
and `px_per_unit` - drawing point (x, y) is pixel ((x - x0) * ppu, (y1 - y) *
ppu). A detail costs fewer tokens as a region than as a whole sheet.

A refusal by capability is an engine boundary, not a bug: report it with its
`capability` key and the route it names (an ODA install, a live seat), and
continue with what the engine can do.

## Coordinates and units

Every coordinate in or out of a tool is WCS on both engines, in drawing units;
angles in degrees, counter-clockwise. An entity in a tilted plane reports
`plane_normal` and refuses an xy edit (capability `"ocs_tilted_plane"`); move it instead. A
UCS set with `ucs_set` is for the operator only - no tool reads input in it.

## The live seat

- **The bound document.** The live engine works on the document it opened or
  activated. If the operator switches AutoCAD to another drawing, every call
  - reads included - is refused with `active_document_changed` (naming bound
  and active). Never "retry on the active one": ask, or `document_activate`
  the bound document when that is what the person wants.
- **The revision** counts this server's own writes. Edits the operator makes
  by hand in AutoCAD are not counted; before trusting a preview, compare the
  entity fingerprints (`preconditions`), not only the revision.
- **Where new geometry goes.** Model space, until `layout_set_current` names a
  sheet; a layout tab the operator clicks does not move it. To draw on a sheet,
  call `layout_set_current` first.
- **Raw commands** (`system_run_command`) wait for the command to finish (up
  to `COMMAND_DEADLINE_S`, default 8 s) and report `completed`, `created`
  (handle, type, space) and `last_prompt`; a command left at a prompt is
  cancelled and fails `command_incomplete`. A refused command lists the typed
  tools to use instead (`alternatives`), and an allowed one that a typed tool
  also does names it (`typed_alternative`) - prefer the typed tool.
- **Layers** are not created on assignment; create them before placing raw
  entities.
- **One seat, many callers.** A busy or rejected call (`busy`,
  `application_busy`) is contention; wait and retry the same call, do not
  change it.

## Quarantine

A headless call that outruns its deadline while its worker still writes
quarantines the document: later calls on it are refused `quarantined`; the
exits that replace the document (`drawing_open` of the saved file,
`drawing_new`, `drawing_close`) still work. What the abandoned call wrote is
unknown - never save over the last good file from a quarantined document. A
read-only call that runs long never quarantines; others answer `busy` until it
ends.
`drawing_history` lists recent operations and how each ended.
