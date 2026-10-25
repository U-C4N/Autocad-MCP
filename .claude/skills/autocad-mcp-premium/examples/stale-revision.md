# Example: an edit refused because the drawing moved

**Situation:** you prepared a change - move the part outline's first edge 1 mm
right - and showed the person the preview. Before they said "go", a colleague
added a line to the same drawing.

## 1. Preview

```json tool=cad_batch
{"steps": [{"tool": "entity_move", "args": {"handle": "3F", "dx": 1, "dy": 0}}],
 "dry_run": true}
```

returns, besides the validation report:

```text
preview:
  document_id: fdd2...f1c2
  revision: 28
  preconditions: [{handle: 3F, fingerprint: 3f06...7913}]
  planned: [{i: 0, tool: entity_move, args: {handle: 3F, dx: 1, dy: 0}}]
```

## 2. Apply under the guard

```json tool=cad_batch
{"steps": [{"tool": "entity_move", "args": {"handle": "3F", "dx": 1, "dy": 0}}],
 "expected_revision": 28}
```

The colleague's line moved the document to revision 29:

```text
ok: false   kind: stale_revision
"stale_revision: the document changed since the preview (revision 28 expected,
 29 now); nothing was changed. Run the dry run again and apply its new preview."
```

Nothing ran. The two guards differ: `expected_revision` blocks on any change to
the document; `preconditions` alone (the fingerprint of `3F`) blocks only when
an entity you touch changed. Passed together, both must hold.

## 3. What you do

1. Do **not** resend without the guard, and do not copy the new revision into
   the old request.
2. Dry-run again; compare the new preview with the old one (did `3F`'s
   fingerprint change? what else moved? `drawing_diff` against the last saved
   file answers that).
3. Tell the person what changed and ask whether the edit still applies.
4. Apply with the new preview's `revision` and `preconditions`.

On live AutoCAD, edits the operator makes by hand are not counted in the
revision - there the fingerprints in `preconditions` are what catches them, so
never guard a live edit with the revision alone.

## Retries

If the apply itself timed out or was cancelled, its outcome may be
`unknown_commit`: the write may or may not have landed. Had you given it an
`operation_id`, the same request with the same id answers `requires_reconcile`
instead of moving the entity a second time. Either way, inspect the entity
(`entity_get`, `system_status` operations) before doing anything else.
