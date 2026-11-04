"""The literal-handle pre-check (plan 1.8-09).

§4.3 of the rival's report: a batch that moves a line and then deletes a
handle that does not exist left the move applied - the second step failed at
run time, after the first had written. Every handle a call names literally is
known before anything runs, so it is checked then: one
:func:`engineering.changes.preview.entity_fingerprints` read (the exact reader
the previews use - in-memory headless, ``Document.Export`` live) over every
literal handle, and a single missing one refuses the whole call with
:class:`engineering.errors.UnknownHandleError` (kind ``unknown_handle``) naming
each missing reference. Nothing is written, so neither engine has to undo
anything - the scenario is exact on both without relying on AutoCAD's UNDO.

The read is also the "before" of a rollback's evidence: :func:`changed_since`
reads the same handles again after a rollback and names those that differ.
Exact on the live engine (both reads are exports of the live document); on the
headless engine the rollback replaces the document with one read back from its
snapshot, whose tags are not bit-identical to the in-memory ones for every
entity type (a TEXT's are not), so there the comparison is presence only.
"""

from __future__ import annotations

from collections.abc import Iterable

from engineering.changes.preview import MISSING, entity_fingerprints
from engineering.errors import UnknownHandleError

__all__ = ["MISSING", "changed_since", "normalise", "read_handles", "require_handles"]

#: How many missing references the refusal's sentence spells out.
_LISTED = 5


def normalise(value) -> str | None:
    """A handle as the fingerprint reader keys it (stripped, upper-case), or None."""
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    return text or None


async def read_handles(backend, handles: Iterable) -> dict[str, str]:
    """``{handle: fingerprint}`` for the distinct handles, in ONE read; ``{}`` for none."""
    wanted = [key for key in dict.fromkeys(normalise(h) for h in handles) if key]
    if not wanted:
        return {}
    return await entity_fingerprints(backend, wanted)


def _where(row: dict) -> str:
    handle = row.get("handle")
    if "i" in row:
        return f"step {row['i']} ({row.get('tool')}) {handle!r}"
    if "index" in row:
        return f"[{row['index']}] {handle!r}"
    return repr(handle)


async def require_handles(backend, refs: Iterable[dict], *, subject: str, **fields) -> dict:
    """Refuse unless every ``refs[k]["handle"]`` names an entity; return the fingerprints.

    ``refs`` are rows carrying ``handle`` plus where it was named (``i`` and
    ``tool`` for a batch step, ``index`` for a list item); each missing one is
    reported with those keys. A blank or non-text handle is missing. ``subject``
    opens the refusal's sentence; ``fields`` (``executed: 0``, ``deleted: 0``)
    travel in the envelope. One read whatever the number of handles.
    """
    rows = [(dict(ref), normalise(ref.get("handle"))) for ref in refs]
    fingerprints = await read_handles(backend, [key for _ref, key in rows if key])
    missing: list[dict] = []
    seen: set[tuple] = set()
    for ref, key in rows:
        if key is not None and fingerprints.get(key) != MISSING:
            continue
        row = {**ref, "handle": key if key is not None else ref.get("handle")}
        identity = tuple(sorted((k, repr(v)) for k, v in row.items()))
        if identity in seen:
            continue
        seen.add(identity)
        missing.append(row)
    if missing:
        listed = "; ".join(_where(row) for row in missing[:_LISTED])
        more = "" if len(missing) <= _LISTED else f" (+{len(missing) - _LISTED} more)"
        raise UnknownHandleError(
            f"{subject}: {len(missing)} handle(s) name no entity in the document - "
            f"{listed}{more}. Nothing was run; entity_list returns live handles.",
            missing=missing,
            **fields,
        )
    return fingerprints


async def changed_since(backend, before: dict[str, str], *, exact: bool) -> list[str]:
    """The handles of ``before`` whose state differs now (sorted).

    ``exact`` compares the fingerprints; otherwise only whether each handle
    exists (see the module docstring for why the headless engine needs that).
    """
    if not before:
        return []
    now = await read_handles(backend, list(before))
    if exact:
        return sorted(h for h, value in before.items() if now.get(h) != value)
    return sorted(h for h, value in before.items() if (now.get(h) == MISSING) != (value == MISSING))
