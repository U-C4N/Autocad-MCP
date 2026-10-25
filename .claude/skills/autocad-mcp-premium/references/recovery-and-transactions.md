# Recovery and transactions

Every call ends in a typed outcome. Read the `kind`, act on it, and never
report a write you cannot prove happened.

## Group writes

- **`cad_batch(steps=[...])`** runs many calls in one round trip; `bind`
  passes a result to a later step (`"$edge"`, `"$mid.x"`). Validation runs
  first: an unknown tool, a schema-invalid argument or an unresolved reference
  refuses the whole batch before anything runs. `on_error="rollback"` opens a
  checkpoint - read the returned `atomicity` block: headless rollback restores
  a snapshot; live rollback sends an UNDO whose landing AutoCAD never confirms.
- **`transaction_begin` / `transaction_commit` / `transaction_rollback`** wrap
  hand-made sequences. Always close what you open. The whole-sheet tools
  (`pid_from_spec`, `arch_plan_from_spec`, `mech_part_draw`) already run in
  one transaction and roll back on any bad item or a cancellation.

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
different request it is refused `operation_conflict`. The journal lives in
memory: a server restart forgets it (durable journals are a 1.8.0 item).

## Outcomes and what to do

| `kind` | Meaning | Do |
|---|---|---|
| `invalid_args` | refused before work, path named | fix the argument |
| `unsupported` | engine boundary, `capability` named | report it; use the named route |
| `stale_revision` | the drawing moved since the preview | new preview, explain, re-decide |
| `active_document_changed` | live: the operator switched drawings | stop; ask |
| `operation_conflict` | the id was used for another request | new id, or find the first result |
| `unknown_commit` | a write outran its deadline while its worker ran | do not repeat blindly: inspect the drawing (`system_status` operations, `entity_get`), then reconcile |
| `requires_reconcile` | a retry of an `unknown_commit` operation | same as above |
| `timed_out`, `cancelled` | the call ended without a result | check the operation's state before retrying |
| `quarantined` | the document is untrusted after an abandoned write | reopen the last saved file |
| `busy` | a long read still holds the document | wait, then retry the same call |
| `scan_incomplete`, `check_failed` | a QA read or check did not complete | the drawing is not "clean"; report it |

`system_status` lists the last operations with `state` (committed, partial,
failed, refused, quarantined, cancelled, unknown_commit, running) and the
revision before and after each.

## Damaged files

A DXF that does not parse is refused by name. Automatic recovery of damaged
DXF files is not in this release (1.7.1); do not hand-repair the file text and
present the result as the original drawing.
