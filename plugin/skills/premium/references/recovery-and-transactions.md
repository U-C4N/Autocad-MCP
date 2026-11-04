# Recovery and transactions

Every call ends in a typed outcome. Read the `kind`, act on it, and never
report a write you cannot prove happened.

## Group writes

- **`cad_batch(steps=[...])`** runs many calls in one round trip; `bind`
  passes a result to a later step (`"$edge"`, `"$mid.x"`). Validation runs
  first: an unknown tool, a schema-invalid argument, an unresolved reference or
  a literal handle that names no entity (`unknown_handle`, each step index and
  handle in `missing`) refuses the whole batch before anything runs. The
  default `on_error="auto"` makes a batch that writes all or nothing: a step
  that fails rolls back the steps before it (headless: a snapshot; live: an
  UNDO judged by before/after fingerprints). Read the returned `atomicity`
  block (`default_applied`, `rolled_back`, `verified`); pass
  `on_error="stop"` only when you want the steps before a failure kept. A
  batch with its own `transaction_*` step, or one that switches documents,
  runs as `stop` under auto (`default_applied: false`).
- **`entity_delete_many`** deletes all of its handles or none: a missing handle
  refuses the call, and a delete that fails rolls the others back
  (`ok: false`, `rolled_back`).
- **`transaction_begin` / `transaction_commit` / `transaction_rollback`** wrap
  hand-made sequences. Always close what you open. The whole-sheet tools
  (`pid_from_spec`, `arch_plan_from_spec`, `mech_part_draw`) already run in
  one transaction and roll back on any bad item or a cancellation.

## Checkpoints, undo after a restart, history

- Headless, every save, open, new drawing and rollback batch keeps a durable
  checkpoint. Live AutoCAD keeps one only at `checkpoint_create`, at each
  `transaction_begin` (every rollback batch and the whole-sheet `*_from_spec`
  tools) and before `entity_delete_many` / `drawing_purge` /
  `text_find_replace` - not at a save, an open or a new drawing, so on a live
  seat call `checkpoint_create(label=...)` after a save you may want back.
  Either engine: `checkpoint_create(label=...)` adds one before a risky step.
  They survive a restart: `checkpoint_list` names them,
  `checkpoint_restore(checkpoint, expected_revision=...,
  mode="in_place"|"new_document")` brings one back (live AutoCAD restores only
  as a new document). A reference to a checkpoint that does not exist, or that
  the disk budget evicted, is refused: pick a kept one from `checkpoint_list`.
- The headless `drawing_undo` steps back to the newest checkpoint, also after a
  restart - to the last save, batch or label, not one call (`granularity`). Redo
  across a restart is not kept. Live, undo is AutoCAD's own.
- The headless `drawing_open` reports `lost_since_save`: calls the server
  committed but never saved before it ended. Report them; they are not in the
  file. (A killed server loses nothing of a live document: it stays open in
  AutoCAD.)
- `drawing_history(label=..., handle=..., query=..., detail="args")` reads what
  was done to the drawing, with which arguments, across restarts. Label a round
  with `cad_batch(label=...)` or `_meta.label`; `drawing_diff(old_checkpoint=...,
  new_checkpoint=...)` compares two checkpoints, model payloads included.

## Preview, then apply

`cad_batch(dry_run=True)`, `drawing_refine(dry_run=True)`,
`text_find_replace(dry_run=True)` and `pid_from_spec(dry_run=True)` return a
`preview`: the document id, its content `revision`, and the exact fingerprint
of every entity the change touches. Apply with
`expected_revision=preview.revision` and/or `preconditions=preview.preconditions`.
Anything moved -> refused `stale_revision`, nothing changed. Then: take a new
preview, compare it with the old one, and tell the person what moved before
you apply again. A guarded run reports `revision: {checked, after,
interleaved}` - a non-empty `interleaved` means another call wrote between
two of your steps.

## Retry safely

Give a write an `operation_id` (`cad_batch(operation_id=...)`, or the request's
`_meta.operation_id` on any tool). Sent again with the same request it returns
the recorded result (`replayed: true`) instead of drawing twice; with a
different request it is refused `operation_conflict`. The journal is durable
(`system_status.state_store`): the same id replays after a server restart, and
a call that was running when its server died answers `requires_reconcile`
(`error_kind: "process_exit"`). If `state_store.degraded` is set, the journal is
in memory only and a restart forgets it.

## Outcomes and what to do

Every error is one envelope: `ok: false`, `kind`, `category`, `retryable`,
`hint`, `tool`, `backend`, `operation_id`, `message`. Branch on `kind`, never
on the sentence. `retryable: true` only when nothing was written; a retryable
refusal is not replayed by its `operation_id`, so the same request runs again.

| `kind` | Meaning | Do |
|---|---|---|
| `invalid_args` | refused before work, path named | fix the argument |
| `not_found` | the handle names no entity | `entity_list` / `entity_select_smart` for live handles |
| `unknown_handle` | a batch or list call named a handle that does not exist; nothing ran (`missing`) | fix the handles in `missing`, then the same call |
| `path_refused`, `file_not_found` | outside `ALLOWED_PATHS`, or no file there | use an allowed path; check the name |
| `policy_refused` | a raw command or LISP the denylist refuses | use a tool from `alternatives` |
| `application_busy` | live: AutoCAD at a prompt or dialog, nothing written | wait (ESC in AutoCAD), then the same call |
| `command_incomplete` | live: a command stopped at `last_prompt` and was cancelled | inspect what landed; never resend blindly |
| `damaged_drawing`, `recover_unsupported`, `recovered_source_protected` | the file does not parse, a DWG cannot be recovered here, or a save onto a recovered source; nothing opened or written | see *Damaged files* below |
| `save_locked`, `save_verify_failed`, `save_failed` | the save did not land; the target is the file it was before | `save_locked`: another program holds the file - close it, save again; the other two: report it, do not loop |
| `"validation_failed"`, `"critique_gate_failed"`, `"failed_acceptance"` | the finalize / deliver gate | fix the `blocking` rows (`detail="full"` lists all) |
| `unsupported` | engine boundary, `capability` named | report it; use the named route |
| `stale_revision` | the drawing moved since the preview | new preview, explain, re-decide |
| `active_document_changed` | live: the operator switched drawings | stop; ask |
| `operation_conflict` | the id was used for another request | new id, or find the first result |
| `unknown_commit` | a write outran its deadline while its worker ran | do not repeat blindly: inspect the drawing (`drawing_history`, `entity_get`), then reconcile |
| `requires_reconcile` | a retry of an `unknown_commit` operation | same as above |
| `timed_out`, `cancelled` | the call ended without a result | check the operation's state before retrying |
| `quarantined` | the document is untrusted after an abandoned write | reopen the last saved file |
| `busy` | a long read still holds the document | wait, then retry the same call |
| `scan_incomplete`, `check_failed` | a QA read or check did not complete | the drawing is not "clean"; report it |
| `hand_edited_tagged_entity`, `model_drift` | `mech_part_revise`: a tagged entity was edited by hand, or the views are not the model's | report the handles; `overwrite=True` only if the person accepts losing the hand edit |
| `no_route`, `routing_budget_exceeded` | `route="avoid"` found no way round the named symbols | move a named symbol, lower `clearance`, pass waypoints, or `on_blocked="cross"` |
| `dwg_conversion_failed` | every headless DWG converter failed (`attempts`) | report it; open the DWG on a live seat |

`system_status.operations` is a summary (count, the last operation, any
`unknown_commit`); `drawing_history` lists the calls with their `state`
(committed, partial, failed, refused, quarantined, cancelled, unknown_commit,
running) and revision.

## Damaged files

A DXF that does not parse is refused by name (`damaged_drawing`). Recovery
happens only when asked: `drawing_open(path, recover=True)` reports
`recovered` (`fixes`, `errors`, `messages`, `entities_read`) - the reader drops
what it cannot parse, so compare `entities_read` with what the drawing should
hold and report the losses. The recovered drawing will not save over the
damaged file unless `drawing_save(overwrite_source=True)`; save it under a new
name with `drawing_save_as` instead. Never hand-repair the file text and
present the result as the original drawing.

Headless saves are atomic (a checked temporary file renamed over the target);
a save a dead process left half done is resolved at the next start or open of
that path (`save_recovery`). Reopening a file this server saved continues its
document id and `revision`; a file changed outside it says
`external_change: true`.
