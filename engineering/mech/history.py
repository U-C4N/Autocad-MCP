"""The model history of a part: every revision's parameter changes, in the drawing (1.8-19).

Each ``mech_part_revise`` appends one entry - ``{rev, at, operation_id,
changes: {path: [old, new]}}`` - to the XRECORD named after the part's anchor
handle inside the ``ACADMCP_MODEL_HISTORY`` dictionary: the storage pattern of
``engineering/verification/requirements.py`` (``ACADMCP_REQUIREMENTS``),
JSON in 255-character 1000-code chunks, one level below the named object
dictionary on both engines because ActiveX can add only a dictionary there.
The record travels with the file, so "what was the bore at revision 2?" is
answered by the drawing itself, independently of the server's operation
journal. The newest ``MAX_ENTRIES`` entries are kept; the oldest are dropped
and the record says how many (``dropped``).

The history lives in an XRECORD, not on the anchor: the anchor's XDATA is
capped at 16 KB per entity and already carries the part model.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

DICT_NAME = "ACADMCP_MODEL_HISTORY"
FORMAT_VERSION = 1
CHUNK_SIZE = 255
#: Entries kept per part; the oldest are dropped first.
MAX_ENTRIES = 200


def _record_name(part_id: str) -> str:
    name = str(part_id).strip().upper()
    if not name or not all(c in "0123456789ABCDEF" for c in name):
        raise ValueError(f"part_id: expected an anchor handle (hex), got {part_id!r}.")
    return name


def encode_history(part_id: str, entries: list[dict], dropped: int = 0) -> list[str]:
    payload = {
        "format": FORMAT_VERSION,
        "part": str(part_id),
        "dropped": int(dropped),
        "entries": entries,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return [text[i : i + CHUNK_SIZE] for i in range(0, len(text), CHUNK_SIZE)] or [""]


def decode_history(chunks) -> dict:
    text = "".join(str(chunk) for chunk in chunks)
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{DICT_NAME}: the stored record is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("format") != FORMAT_VERSION:
        found = payload.get("format") if isinstance(payload, dict) else type(payload).__name__
        raise ValueError(f"{DICT_NAME}: not a format {FORMAT_VERSION} record (got {found!r})")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"{DICT_NAME}: the stored record carries no entry list")
    return {
        "part": str(payload.get("part", "")),
        "dropped": int(payload.get("dropped", 0) or 0),
        "entries": entries,
    }


async def load_history(backend, part_id: str) -> dict:
    """``{part, dropped, entries}`` of ``part_id``; an empty history when none is stored."""
    name = _record_name(part_id)
    if getattr(backend, "name", None) == "com":
        chunks = await backend._run(_com_chunks, name)
    else:
        chunks = await backend._async(_ezdxf_chunks, backend, name, readonly=True)
    if chunks is None:
        return {"part": name, "dropped": 0, "entries": []}
    return decode_history(chunks)


def new_entry(rev: int, changes: dict, operation_id: str | None) -> dict:
    return {
        "rev": int(rev),
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "operation_id": operation_id,
        "changes": changes,
    }


async def append_history(backend, part_id: str, changes: dict, operation_id: str | None) -> dict:
    """Append one revision entry and return it. A content change: the headless
    revision moves through ``_mark_dirty``, the live one through ``_bump_revision``."""
    name = _record_name(part_id)
    history = await load_history(backend, name)
    entries = list(history["entries"])
    last = max((int(e.get("rev", 0)) for e in entries if isinstance(e, dict)), default=0)
    entry = new_entry(last + 1, changes, operation_id)
    entries.append(entry)
    dropped = history["dropped"]
    if len(entries) > MAX_ENTRIES:
        dropped += len(entries) - MAX_ENTRIES
        entries = entries[-MAX_ENTRIES:]
    chunks = encode_history(name, entries, dropped)
    if getattr(backend, "name", None) == "com":
        from backends.document_guard import ActiveDocumentChangedError

        refused = False
        try:
            await backend._run(_com_store, name, chunks)
        except ActiveDocumentChangedError:
            refused = True
            raise
        finally:
            if not refused:
                backend._bump_revision()
    else:
        await backend._async(_ezdxf_store, backend, name, chunks)
    return entry


def param_values(history: dict, param: str) -> list[dict]:
    """Every entry that changed ``param`` (a change path), as ``{rev, at,
    operation_id, old, new}``; a path under ``param`` (``features.h1`` for
    ``features.h1.diameter``) counts too."""
    wanted = str(param).strip()
    out = []
    for entry in history.get("entries") or []:
        for path, pair in (entry.get("changes") or {}).items():
            if path == wanted or path.startswith(wanted + ".") or path.startswith(wanted + "["):
                old, new = (list(pair) + [None, None])[:2]
                out.append(
                    {
                        "rev": entry.get("rev"),
                        "at": entry.get("at"),
                        "operation_id": entry.get("operation_id"),
                        "path": path,
                        "old": old,
                        "new": new,
                    }
                )
    return out


# -- the engines ------------------------------------------------------------------


def _ezdxf_store(backend, name: str, chunks: list[str]) -> bool:
    doc = backend._require_doc()
    holder = doc.rootdict.get_required_dict(DICT_NAME)
    replaced = name in holder
    if replaced:
        holder.remove(name)
    record = holder.add_xrecord(name)
    record.extend([(1000, chunk) for chunk in chunks])
    backend._mark_dirty()
    return replaced


def _ezdxf_chunks(backend, name: str) -> list[str] | None:
    doc = backend._require_doc()
    if DICT_NAME not in doc.rootdict:
        return None
    holder = doc.rootdict.get(DICT_NAME)
    if name not in holder:
        return None
    return [str(tag.value) for tag in holder.get(name).tags if tag.code == 1000]


def _com_holder(doc, *, create: bool):
    from backends import com_backend as com

    try:
        return com._com_unnarrow(doc.Dictionaries.Item(DICT_NAME))
    except Exception:  # absent: Dictionaries.Item raises for an unknown key
        if not create:
            return None
    return com._com_unnarrow(doc.Dictionaries.Add(DICT_NAME))


def _com_record(holder, name: str):
    from backends.com_backend import ComBackend

    if holder is None:
        return None
    for key, obj in ComBackend._com_dict_entries(holder):
        if key == name:
            return obj
    return None


def _com_store(name: str, chunks: list[str]) -> bool:
    """COM thread: write the chunks into the bound document's record."""
    from backends import com_backend as com

    doc = com._acad_doc()
    holder = _com_holder(doc, create=True)
    record = _com_record(holder, name)
    replaced = record is not None
    if record is None:
        record = holder.AddXRecord(name)
    record.SetXRecordData(com._ai([1000] * len(chunks)), com._avar(chunks))
    return replaced


def _com_chunks(name: str) -> list[str] | None:
    """COM thread: the record's chunks, or None."""
    from backends import com_backend as com
    from backends.com_backend import ComBackend

    record = _com_record(_com_holder(com._acad_doc(), create=False), name)
    return None if record is None else ComBackend._com_xrecord_chunks(record)
