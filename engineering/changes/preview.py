"""Change previews and exact preconditions (spec section 5, Task 12; B14 residue).

A dry run of a mutating tool returns a :class:`ChangePreview`: the document's
id and content revision, the preconditions an apply will check, and the
planned edits. An apply that carries ``expected_revision`` and/or
``preconditions`` re-reads both first; any mismatch raises
:class:`StaleRevisionError` (kind ``stale_revision``) before anything is
written.

**Exact fingerprints.** An entity's fingerprint is the sha256 of its own DXF
tags as ezdxf exports them (``export_dxf`` into a ``TagCollector``): every
group code and value - layer, colour, linetype, lineweight, geometry, XDATA,
an ellipse's axes, a DIMENSION's measurement and override text, MTEXT
formatting codes - each value hashed through ``repr``, so a float compares bit
for bit. An INSERT's tags carry its ATTRIBs. ``diff.signature`` is not used: it
rounds to 0.01 and leaves most of that out.

**The same reader at preview and at apply.** Headless, the tags come from the
in-memory document under the backend's lock (``_async(readonly=True)``); live,
from ``Document.Export`` of the active document (the snapshot exporter, which
keeps the live handles - ``scripts/smoke_understand_com.py`` moves an exported
handle with ``entity_move``), read with ezdxf. An engine is only ever compared
with itself.

**A region precondition** is the sha256 of the current layout's name and the
sorted ``(handle, fingerprint)`` pairs of every entity of that layout whose WCS
box meets the region (closed intervals). An entity with no finite box (XLINE,
RAY, a reference to an empty block) is not in it.

**Revision** is ``document_identity()["revision"]`` (Task 9): it moves when
content changes through the server. Headless, a direct edit of the file that
does not pass through the server leaves it where it was; live, the operator's
edits to database objects move it too (backends/document_guard.py names what
that counter misses). The fingerprints catch a direct edit either way.

**Interleaving.** An apply made of several locked calls (``cad_batch`` steps,
``drawing_refine`` repairs, ``pid_from_spec`` items) lets other calls run
between its own. :class:`RevisionWatch` reads the revision before each of them
and reports a move since the previous one as an interleaved change. A change
made inside one of our calls cannot be told from our own and is not reported.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import ezdxf
from ezdxf.lldxf.tagwriter import TagCollector

from engineering.understand.snapshot import _box, _com_export
from engineering.validation import SpecError

__all__ = [
    "MISSING",
    "STALE_REVISION",
    "ChangePreview",
    "RevisionWatch",
    "StaleRevisionError",
    "check_preconditions",
    "current_revision",
    "entity_fingerprint",
    "entity_fingerprints",
    "entity_tags_digest",
    "make_preview",
    "parse_preconditions",
    "read_identity",
    "region_fingerprint",
]

#: The fingerprint of a handle the document does not hold (deleted, never there).
MISSING = "missing"
#: The refusal kind every stale apply carries.
STALE_REVISION = "stale_revision"
_HEX = frozenset("0123456789abcdef")
_ROW_KEYS = frozenset({"handle", "region", "fingerprint"})


# -- fingerprints ---------------------------------------------------------------


def entity_tags_digest(entity, dxfversion: str) -> str:
    """sha256 of ``entity``'s exported DXF tags, every value through ``repr``."""
    collector = TagCollector(dxfversion=dxfversion)
    entity.export_dxf(collector)
    digest = hashlib.sha256()
    for code, value in collector.tags:
        digest.update(f"{code}\x1f{value!r}\x1e".encode())
    return digest.hexdigest()


def _handle(value, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{path}: expected a handle (hex text), got {value!r}")
    return value.strip().upper()


def _region(value, path: str) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise SpecError(f"{path}: expected [x0, y0, x1, y1], got {value!r}")
    numbers = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise SpecError(f"{path}[{index}]: expected a number, got {item!r}")
        if not math.isfinite(float(item)):
            raise SpecError(f"{path}[{index}]: must be finite, got {item!r}")
        numbers.append(float(item))
    x0, y0, x1, y1 = numbers
    if x0 > x1 or y0 > y1:
        raise SpecError(f"{path}: x0 <= x1 and y0 <= y1 are required, got {value!r}")
    return x0, y0, x1, y1


def _digests(doc, handles: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for handle in handles:
        entity = doc.entitydb.get(handle)
        if entity is None or not entity.is_alive:
            out[handle] = MISSING
        else:
            out[handle] = entity_tags_digest(entity, doc.dxfversion)
    return out


def _meets(box, region) -> bool:
    x0, y0, x1, y1 = box
    rx0, ry0, rx1, ry1 = region
    return not (x1 < rx0 or x0 > rx1 or y1 < ry0 or y0 > ry1)


def _region_digest(doc, space: str, region) -> str:
    try:
        layout = doc.layouts.get(space)
    except KeyError as exc:
        raise RuntimeError(f"region fingerprint: the drawing has no layout {space!r}") from exc
    rows = []
    for entity in layout:
        box = _box(entity)
        if box is not None and _meets(box, region):
            rows.append((str(entity.dxf.handle), entity_tags_digest(entity, doc.dxfversion)))
    digest = hashlib.sha256(f"space\x1f{space}\x1e".encode())
    for handle, value in sorted(rows):
        digest.update(f"{handle}\x1f{value}\x1e".encode())
    return digest.hexdigest()


async def _read(backend, job):
    """``job(doc)`` on the document this engine's preview and apply both read."""
    if backend.name == "com":
        written, folder, _source = await backend._run(_com_export, backend, None)
        try:
            return await asyncio.to_thread(lambda: job(ezdxf.readfile(written)))
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    return await backend._async(lambda: job(backend._require_doc()), readonly=True)


async def _current_space(backend) -> str:
    listing = await backend.layout_list()
    return str((listing or {}).get("current") or "Model")


async def entity_fingerprints(backend, handles: Iterable[str]) -> dict[str, str]:
    """``{handle: fingerprint}`` for every handle, in one read; ``MISSING`` for
    a handle the document does not hold. Handles are upper-cased."""
    wanted = list(dict.fromkeys(_handle(value, f"handles[{i}]") for i, value in enumerate(handles)))
    if not wanted:
        return {}
    return await _read(backend, lambda doc: _digests(doc, wanted))


async def entity_fingerprint(backend, handle: str) -> str:
    """The exact fingerprint of one entity (``MISSING`` when it is not there)."""
    key = _handle(handle, "handle")
    return (await entity_fingerprints(backend, [key]))[key]


async def region_fingerprint(backend, bbox: tuple[float, float, float, float]) -> str:
    """The fingerprint of every entity of the current layout whose box meets ``bbox``."""
    region = _region(bbox, "bbox")
    space = await _current_space(backend)
    return await _read(backend, lambda doc: _region_digest(doc, space, region))


# -- identity and revision ------------------------------------------------------


async def read_identity(backend) -> dict:
    """``backend.document_identity()``, awaited when the engine made it a coroutine.

    An engine that counts outside edits lazily (the live one's
    ``refresh_document_identity``) folds them in first."""
    reader = getattr(backend, "document_identity", None)
    if reader is None:
        return {"document_id": None, "revision": None}
    refresh = getattr(backend, "refresh_document_identity", None)
    if refresh is not None:
        await refresh()
    value = reader()
    if inspect.isawaitable(value):
        value = await value
    return dict(value or {})


async def current_revision(backend) -> int | None:
    revision = (await read_identity(backend)).get("revision")
    return None if revision is None else int(revision)


# -- the preview ------------------------------------------------------------------


def _freeze(value):
    if isinstance(value, dict):
        return ("{}", tuple(sorted((str(k), _freeze(v)) for k, v in value.items())))
    if isinstance(value, (list, tuple)):
        return ("[]", tuple(_freeze(v) for v in value))
    return value


def _thaw(value):
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "{}":
        return {k: _thaw(v) for k, v in value[1]}
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "[]":
        return [_thaw(v) for v in value[1]]
    return value


def _row(row: dict) -> tuple[tuple[str, object], ...]:
    return tuple(sorted((str(k), _freeze(v)) for k, v in row.items()))


@dataclass(frozen=True)
class ChangePreview:
    """What a dry run promises: the document, its revision, the preconditions an
    apply checks and the planned edits. Hashable; ``to_dict`` is the wire form."""

    document_id: str | None
    revision: int | None
    preconditions: tuple[tuple[tuple[str, object], ...], ...]
    planned: tuple[tuple[tuple[str, object], ...], ...]

    @classmethod
    def build(
        cls,
        document_id: str | None,
        revision: int | None,
        preconditions: Iterable[dict],
        planned: Iterable[dict],
    ) -> ChangePreview:
        return cls(
            document_id,
            revision,
            tuple(_row(row) for row in preconditions),
            tuple(_row(row) for row in planned),
        )

    def to_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "revision": self.revision,
            "preconditions": [{k: _thaw(v) for k, v in row} for row in self.preconditions],
            "planned": [{k: _thaw(v) for k, v in row} for row in self.planned],
        }


async def make_preview(
    backend,
    *,
    handles: Iterable[str] = (),
    region: tuple[float, float, float, float] | None = None,
    planned: Iterable[dict] = (),
) -> ChangePreview:
    """The preview of a planned change: fingerprints first, the identity last,
    so the revision is never older than the content it vouches for."""
    rows: list[dict] = [
        {"handle": handle, "fingerprint": value}
        for handle, value in (await entity_fingerprints(backend, handles)).items()
    ]
    if region is not None:
        box = _region(region, "region")
        rows.append({"region": list(box), "fingerprint": await region_fingerprint(backend, box)})
    identity = await read_identity(backend)
    revision = identity.get("revision")
    return ChangePreview.build(
        identity.get("document_id"),
        None if revision is None else int(revision),
        rows,
        list(planned),
    )


# -- the apply ------------------------------------------------------------------


class StaleRevisionError(RuntimeError):
    """The document moved since the preview; nothing was changed.

    ``to_dict()`` is the refusal: ``{ok: False, kind: "stale_revision", error,
    expected_revision, actual_revision, changed}``; ``changed`` holds one row per
    precondition that no longer holds (``{handle | region, expected, actual}``).
    """

    kind = STALE_REVISION

    def __init__(
        self,
        message: str,
        *,
        expected: int | None = None,
        actual: int | None = None,
        changed: Iterable[dict] = (),
    ):
        super().__init__(message)
        self.expected = expected
        self.actual = actual
        self.changed = [dict(row) for row in changed]

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "kind": self.kind,
            "error": str(self),
            "expected_revision": self.expected,
            "actual_revision": self.actual,
            "changed": [dict(row) for row in self.changed],
        }


def parse_preconditions(raw) -> list[dict]:
    """Validated precondition rows: ``{handle, fingerprint}`` or
    ``{region: [x0, y0, x1, y1], fingerprint}``. Refused by path."""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise SpecError(f"preconditions: expected a list, got {type(raw).__name__}")
    rows = []
    for index, item in enumerate(raw):
        path = f"preconditions[{index}]"
        if not isinstance(item, dict):
            raise SpecError(f"{path}: expected an object, got {type(item).__name__}")
        extra = sorted(set(item) - _ROW_KEYS)
        if extra:
            raise SpecError(f"{path}: unknown key(s) {', '.join(extra)}")
        if ("handle" in item) == ("region" in item):
            raise SpecError(f"{path}: carries exactly one of 'handle' or 'region'")
        value = item.get("fingerprint")
        if not isinstance(value, str) or not (
            value == MISSING or (len(value) == 64 and set(value) <= _HEX)
        ):
            raise SpecError(f"{path}.fingerprint: expected 64 hex digits or {MISSING!r}")
        if "handle" in item:
            rows.append({"handle": _handle(item["handle"], f"{path}.handle"), "fingerprint": value})
        else:
            rows.append(
                {"region": list(_region(item["region"], f"{path}.region")), "fingerprint": value}
            )
    return rows


def _expected_revision(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SpecError(f"expected_revision: expected a revision (an integer >= 0), got {value!r}")
    return value


async def check_preconditions(backend, *, expected_revision=None, preconditions=None) -> None:
    """Refuse with :class:`StaleRevisionError` unless every precondition still
    holds and the revision is ``expected_revision``. Reads the fingerprints
    first and the revision last, so a change racing the check is caught by one
    or the other. Changes nothing."""
    expected = _expected_revision(expected_revision)
    rows = parse_preconditions(preconditions)
    if expected is None and not rows:
        return
    changed: list[dict] = []
    handle_rows = [row for row in rows if "handle" in row]
    if handle_rows:
        now = await entity_fingerprints(backend, [row["handle"] for row in handle_rows])
        for row in handle_rows:
            actual = now[row["handle"]]
            if actual != row["fingerprint"]:
                changed.append(
                    {"handle": row["handle"], "expected": row["fingerprint"], "actual": actual}
                )
    for row in rows:
        if "region" in row:
            actual = await region_fingerprint(backend, row["region"])
            if actual != row["fingerprint"]:
                changed.append(
                    {"region": row["region"], "expected": row["fingerprint"], "actual": actual}
                )
    actual_revision = await current_revision(backend)
    revision_moved = expected is not None and actual_revision != expected
    if not changed and not revision_moved:
        return
    reasons = []
    if revision_moved:
        reasons.append(f"revision {expected} expected, {actual_revision} now")
    if changed:
        names = ", ".join(
            row["handle"] if "handle" in row else f"region {row['region']}" for row in changed[:5]
        )
        reasons.append(f"{len(changed)} precondition(s) no longer hold: {names}")
    raise StaleRevisionError(
        "stale_revision: the document changed since the preview ("
        + "; ".join(reasons)
        + "); nothing was changed. Run the dry run again and apply its new preview.",
        expected=expected,
        actual=actual_revision,
        changed=changed,
    )


class RevisionWatch:
    """The revision across an apply made of several locked calls.

    ``before(label)`` before each of our calls records a move since our
    previous call as an interleaved change and returns True; ``after()``
    after each call takes the revision our call left. ``report()`` is
    ``{checked, after, interleaved: [{before, expected, actual}]}``.
    """

    def __init__(self, backend, start: int | None):
        self._backend = backend
        self.start = start
        self.last = start
        self.interleaved: list[dict] = []

    @classmethod
    async def begin(cls, backend) -> RevisionWatch:
        return cls(backend, await current_revision(backend))

    async def before(self, label: str) -> bool:
        now = await current_revision(self._backend)
        moved = self.last is not None and now is not None and now != self.last
        if moved:
            self.interleaved.append({"before": label, "expected": self.last, "actual": now})
        self.last = now
        return moved

    async def after(self) -> None:
        self.last = await current_revision(self._backend)

    def report(self) -> dict[str, Any]:
        return {
            "checked": self.start,
            "after": self.last,
            "interleaved": [dict(row) for row in self.interleaved],
        }
