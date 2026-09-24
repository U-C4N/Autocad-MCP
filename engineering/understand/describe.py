"""drawing_understand: one read of a foreign drawing, every claim with its evidence.

`describe` assembles the report from the pure readers - units and extents
(`units`), bodies of content (`clusters`), and here: layers and what they
carry, equipment tags, rooms, text languages, blocks, layouts and the title
block. Each claim carries its evidence (what was measured or matched) and a
confidence; nothing is corrected silently, and the drawing is never modified
(the snapshot is a frozen copy).

Confidences, in one place:

* a layer name matching the vocabulary: `vocab.classify_layer`'s own (0.9 for
  a direct keyword); the texts on an unnamed layer voting instead:
  :data:`TEXT_VOTE_CONFIDENCE` when most of them agree, :data:`WEAK_VOTE_CONFIDENCE`
  otherwise;
* a tag read from a block attribute :data:`ATTRIBUTE_TAG_CONFIDENCE`, from a
  text :data:`TEXT_TAG_CONFIDENCE`;
* a room label that sits in a face of the wall layers
  :data:`ROOM_FACE_CONFIDENCE`, a label alone :data:`ROOM_LABEL_CONFIDENCE`;
* a title block whose name says so :data:`TITLE_NAME_CONFIDENCE`, one chosen by
  its attribute count alone :data:`TITLE_ATTRIBUTE_CONFIDENCE`;
* character classes are exact: a text either holds a Cyrillic letter or not.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

from engineering.arch.faces import Face, face_containing, planar_faces
from engineering.pid.graph import ATTRIBUTE_TAGS, KEYWORDS
from engineering.understand.clusters import find_clusters
from engineering.understand.labels import parse_tag
from engineering.understand.snapshot import EntityRecord, Snapshot, length_of
from engineering.understand.units import infer_units, robust_extents, wall_segments
from engineering.understand.vocab import classify_layer, room_label

__all__ = [
    "describe",
    "find_rooms",
    "prepare",
    "tag_occurrences",
    "text_language",
]

TEXT_VOTE_CONFIDENCE = 0.6
WEAK_VOTE_CONFIDENCE = 0.4
ATTRIBUTE_TAG_CONFIDENCE = 0.95
TEXT_TAG_CONFIDENCE = 0.8
ROOM_FACE_CONFIDENCE = 0.9
ROOM_LABEL_CONFIDENCE = 0.6
TITLE_NAME_CONFIDENCE = 0.9
TITLE_ATTRIBUTE_CONFIDENCE = 0.6
#: The face finder pairs every wall segment with every other; above this many
#: the rooms are listed without faces and a warning says so.
FACE_SEGMENT_CAP = 3000
#: Endpoints closer than this fraction of the robust diagonal are one corner
#: (1 mm on a 50 m plan drawn in millimetres).
FACE_TOL_FRACTION = 2e-5
#: The same tag lettered twice closer than this fraction of the robust diagonal
#: (an attribute and a text beside one symbol) is one occurrence.
TAG_MERGE_FRACTION = 0.01
#: Rows listed for named blocks, most inserted first.
BLOCK_ROW_CAP = 100
#: Block-name words that mark a title block (EN / NL / DE / TR / RU).
TITLE_WORDS = ("TITLE", "TITEL", "STEMPEL", "SCHRIFTFELD", "ANTET", "ШТАМП")

_TEXT_TYPES = frozenset({"TEXT", "MTEXT", "MULTILEADER"})
_TOKEN_RE = re.compile(r"[A-Z]+")
_CYRILLIC = re.compile(r"[\u0400-\u04FF]")  # the Cyrillic block
#: Letters only Turkish (of the five languages) writes; ç, ö and ü are shared
#: with German and French and are not evidence on their own.
_TURKISH = re.compile(r"[ĞğİıŞş]")
_LATIN = re.compile(r"[A-Za-z\u00C0-\u024F]")  # ASCII and Latin-1 to Latin Extended-B


def _anchor(rec: EntityRecord) -> tuple[float, float] | None:
    if rec.points:
        return (float(rec.points[0][0]), float(rec.points[0][1]))
    if rec.bbox is not None:
        b = rec.bbox
        return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)
    return None


def prepare(snap: Snapshot) -> dict:
    """The shared first pass: model-space records, extents and outliers, the
    records kept, clusters and membership. `describe` and `scale.scale_check`
    both start here, so they agree on which entity is an outlier and which
    cluster a tag sits in."""
    model = [r for r in snap.records if r.space == "Model"]
    extents = robust_extents(model)
    outliers = {o["handle"] for o in extents["outliers"]}
    kept = [r for r in model if r.handle not in outliers]
    clusters, membership = find_clusters(kept)
    robust = extents["robust"]
    diag = math.hypot(robust[2] - robust[0], robust[3] - robust[1]) if robust else 0.0
    return {
        "model": model,
        "extents": extents,
        "kept": kept,
        "clusters": clusters,
        "membership": membership,
        "diag": diag,
    }


def tag_occurrences(snap: Snapshot, *, prep: dict | None = None) -> list[dict]:
    """Every equipment tag on the drawing, one row per occurrence.

    A tag is read with `labels.parse_tag` from a TEXT / MTEXT / MULTILEADER,
    or from an INSERT attribute named like a tag (`pid.graph.ATTRIBUTE_TAGS`).
    Two readings of one tag in one space closer than
    :data:`TAG_MERGE_FRACTION` of the diagonal are one occurrence (the
    attribute's position and confidence win). Rows carry ``tag``, ``handle``,
    ``at``, ``space``, ``layer``, ``cluster`` (model space only), ``source``
    and ``confidence``.
    """
    prep = prep or prepare(snap)
    radius = prep["diag"] * TAG_MERGE_FRACTION
    found: list[dict] = []
    for rec in snap.records:
        at = _anchor(rec)
        if at is None:
            continue
        readings = []
        if rec.type in _TEXT_TYPES and rec.text:
            readings.append((parse_tag(rec.text), "text", TEXT_TAG_CONFIDENCE))
        elif rec.type == "INSERT":
            for name, value in rec.attribs:
                if name.upper() in ATTRIBUTE_TAGS and value:
                    readings.append((parse_tag(value), "attribute", ATTRIBUTE_TAG_CONFIDENCE))
        for tag, source, confidence in readings:
            if not tag:
                continue
            found.append(
                {
                    "tag": tag,
                    "handle": rec.handle,
                    "at": [at[0], at[1]],
                    "space": rec.space,
                    "layer": rec.layer,
                    "cluster": prep["membership"].get(rec.handle) if rec.space == "Model" else None,
                    "source": source,
                    "confidence": confidence,
                }
            )
    found.sort(key=lambda o: (-o["confidence"], o["tag"], o["handle"]))
    kept: list[dict] = []
    for occ in found:
        twin = next(
            (
                k
                for k in kept
                if k["tag"] == occ["tag"]
                and k["space"] == occ["space"]
                and math.dist(k["at"], occ["at"]) <= radius
            ),
            None,
        )
        if twin is None:
            kept.append(occ)
    kept.sort(key=lambda o: (o["tag"], o["space"], o["at"][0], o["at"][1]))
    return kept


def find_rooms(records: Sequence[EntityRecord], *, tol: float, membership: dict) -> dict:
    """Room labels and the faces of the wall layers they sit in.

    A label is any text `vocab.room_label` recognises. Wall layers are the
    ones `vocab.classify_layer` files under ``architecture``; their straight
    segments go through `engineering.arch.faces.planar_faces` (arcs are left
    out and counted). ``labelled`` pairs each room row with its `Face` for a
    caller that needs the geometry; ``rooms`` is JSON-ready.
    """
    labels = []
    for rec in records:
        if rec.type in _TEXT_TYPES and rec.text:
            parsed = room_label(rec.text)
            at = _anchor(rec)
            if parsed and at is not None:
                labels.append((rec, parsed, at))
    segments, bulged = wall_segments(records)
    faces: tuple[Face, ...] = ()
    skipped = None
    if len(segments) > FACE_SEGMENT_CAP:
        skipped = (
            f"{len(segments)} wall segments exceed the {FACE_SEGMENT_CAP}-segment cap of the "
            "face finder; rooms are listed by label only"
        )
    elif segments:
        faces = planar_faces(segments, tol=tol)
    rooms: list[dict] = []
    labelled: list[tuple[dict, Face]] = []
    used: set[int] = set()
    for rec, parsed, at in labels:
        face = face_containing(faces, at) if faces else None
        row = {
            "number": parsed.get("number"),
            "name": parsed.get("name"),
            "language": parsed.get("language"),
            "text": rec.text,
            "handle": rec.handle,
            "at": [at[0], at[1]],
            "cluster": membership.get(rec.handle),
            "face": None,
            "source": "label",
            "confidence": ROOM_LABEL_CONFIDENCE,
        }
        if face is not None:
            used.add(id(face))
            row["face"] = {
                "area": face.area,
                "polygon": [[p[0], p[1]] for p in face.loop],
                "centroid": [face.centroid[0], face.centroid[1]],
            }
            row["source"] = "label+face"
            row["confidence"] = ROOM_FACE_CONFIDENCE
            labelled.append((row, face))
        rooms.append(row)
    rooms.sort(key=lambda r: (r["cluster"] or "", r["number"] or "", r["name"] or "", r["at"]))
    return {
        "rooms": rooms,
        "labelled": labelled,
        "faces": len(faces),
        "faces_unlabelled": sum(1 for f in faces if id(f) not in used),
        "wall_segments": len(segments),
        "arcs_skipped": bulged,
        "skipped": skipped,
    }


def text_language(text: str) -> str:
    """``cyrillic`` | ``turkish`` | ``latin`` | ``none`` by character class, in
    that precedence (a Russian label with a Latin tag in it is Cyrillic)."""
    if _CYRILLIC.search(text):
        return "cyrillic"
    if _TURKISH.search(text):
        return "turkish"
    if _LATIN.search(text):
        return "latin"
    return "none"


def _layers(snap: Snapshot, unit_mm: float | None) -> list[dict]:
    counts: Counter = Counter()
    lengths: dict[str, float] = {}
    texts: dict[str, list[str]] = {}
    for rec in snap.records:
        counts[rec.layer] += 1
        lengths[rec.layer] = lengths.get(rec.layer, 0.0) + length_of(rec)
        if rec.type in _TEXT_TYPES and rec.text:
            texts.setdefault(rec.layer, []).append(rec.text)
    rows = []
    for name in sorted(set(snap.layers) | set(counts)):
        props = snap.layers.get(name, {})
        c = classify_layer(name)
        row = {
            "name": name,
            "entities": counts.get(name, 0),
            "length": lengths.get(name, 0.0),
            "length_m": lengths.get(name, 0.0) * unit_mm / 1000.0 if unit_mm else None,
            "color": props.get("color"),
            "linetype": props.get("linetype"),
            "discipline": c.get("discipline", "unknown"),
            "service": c.get("service", "unknown"),
            "keyword": c.get("keyword"),
            "language": c.get("language"),
            "confidence": c.get("confidence", 0.0),
            "evidence": f"layer name keyword {c.get('keyword')!r}" if c.get("keyword") else None,
        }
        if row["discipline"] == "unknown" and row["service"] == "unknown":
            votes: Counter = Counter()
            said = texts.get(name, [])
            for text in said:
                t = classify_layer(text)
                if (
                    t.get("discipline", "unknown") != "unknown"
                    or t.get("service", "unknown") != "unknown"
                ):
                    votes[(t.get("discipline", "unknown"), t.get("service", "unknown"))] += 1
            if votes:
                (discipline, service), n = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0]
                row.update(
                    discipline=discipline,
                    service=service,
                    confidence=(
                        TEXT_VOTE_CONFIDENCE if n * 2 > len(said) else WEAK_VOTE_CONFIDENCE
                    ),
                    evidence=f"{n} of {len(said)} texts on the layer read as {service} / {discipline}",
                )
        rows.append(row)
    rows.sort(key=lambda r: (-r["entities"], r["name"]))
    return rows


def _block_kind(name: str) -> str | None:
    for token in _TOKEN_RE.findall(name.upper()):
        kind = KEYWORDS.get(token)
        if kind:
            return kind
    return None


def _blocks(snap: Snapshot) -> dict:
    inserts = [r for r in snap.records if r.type == "INSERT" and r.block]
    named = Counter(r.block for r in inserts if not r.block.startswith("*"))
    anonymous = Counter(r.block for r in inserts if r.block.startswith("*"))
    rows = [
        {
            "name": name,
            "inserts": n,
            "kind": _block_kind(name),
            "discipline": classify_layer(name).get("discipline", "unknown"),
        }
        for name, n in sorted(named.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    kinds = Counter(row["kind"] for row in rows if row["kind"])
    return {
        "inserts": len(inserts),
        "named": rows[:BLOCK_ROW_CAP],
        "named_total": len(rows),
        "anonymous": {"names": len(anonymous), "inserts": sum(anonymous.values())},
        "kinds": dict(sorted(kinds.items())),
    }


def _title_block(snap: Snapshot) -> dict:
    best = None
    for rec in snap.records:
        if rec.type != "INSERT" or len(rec.attribs) < 3:
            continue
        upper = (rec.block or "").upper()
        named = any(word in upper for word in TITLE_WORDS)
        key = (named, rec.space != "Model", len(rec.attribs))
        if best is None or key > best[0]:
            best = (key, rec, named)
    if best is None:
        return {"found": False, "reason": "no block reference with three or more attributes"}
    _key, rec, named = best
    return {
        "found": True,
        "handle": rec.handle,
        "block": rec.block,
        "space": rec.space,
        "attributes": {tag: value for tag, value in rec.attribs},
        "confidence": TITLE_NAME_CONFIDENCE if named else TITLE_ATTRIBUTE_CONFIDENCE,
        "evidence": (
            f"block name {rec.block!r} reads as a title block"
            if named
            else f"the block reference with the most attributes ({len(rec.attribs)})"
        ),
    }


def _languages(snap: Snapshot, layer_rows: list[dict]) -> dict:
    by_class: Counter = Counter()
    samples: dict[str, list[str]] = {}
    for rec in snap.records:
        if rec.type in _TEXT_TYPES and rec.text:
            lang = text_language(rec.text)
            by_class[lang] += 1
            if len(samples.setdefault(lang, [])) < 3:
                samples[lang].append(rec.handle)
    total = sum(by_class.values())
    layer_langs = Counter(row["language"] for row in layer_rows if row.get("language"))
    return {
        "texts": total,
        "by_class": {k: by_class.get(k, 0) for k in ("latin", "turkish", "cyrillic", "none")},
        "share": {
            k: (by_class.get(k, 0) / total if total else 0.0)
            for k in ("latin", "turkish", "cyrillic", "none")
        },
        "samples": samples,
        "layer_name_languages": dict(sorted(layer_langs.items())),
        "confidence": 1.0,
    }


def describe(snap: Snapshot) -> dict:
    """The whole report; see the module docstring for the confidences."""
    prep = prepare(snap)
    warnings: list[str] = []
    tol = prep["diag"] * FACE_TOL_FRACTION if prep["diag"] > 0 else 1.0
    rooms = find_rooms(prep["kept"], tol=tol, membership=prep["membership"])
    units = infer_units(
        snap,
        records=prep["kept"],
        room_areas=[row["face"]["area"] for row, _face in rooms["labelled"]],
    )
    if units["warning"]:
        warnings.append(units["warning"])
    extents = dict(prep["extents"])
    extents["declared"] = (
        {"min": list(snap.extmin), "max": list(snap.extmax)}
        if snap.extmin is not None and snap.extmax is not None
        else None
    )
    if extents["outliers"]:
        handles = ", ".join(o["handle"] for o in extents["outliers"][:5])
        stretch = extents["stretch"]
        warnings.append(
            f"{len(extents['outliers'])} entit{'y' if len(extents['outliers']) == 1 else 'ies'} "
            f"far outside the content stretch the extents"
            + (f" {stretch:.3g}x" if stretch else "")
            + f": {handles}"
        )
    if rooms["skipped"]:
        warnings.append(rooms["skipped"])

    occurrences = tag_occurrences(snap, prep=prep)
    by_tag: dict[str, list[dict]] = {}
    for occ in occurrences:
        by_tag.setdefault(occ["tag"], []).append(occ)
    tags = [
        {
            "tag": tag,
            "count": len(rows),
            "ambiguous": len(rows) > 1,
            "confidence": max(r["confidence"] for r in rows),
            "occurrences": [
                {k: r[k] for k in ("handle", "at", "space", "cluster", "layer", "source")}
                for r in rows
            ],
        }
        for tag, rows in sorted(by_tag.items())
    ]
    repeated = [t["tag"] for t in tags if t["ambiguous"]]
    if repeated:
        warnings.append(
            f"{len(repeated)} tag(s) appear more than once (plan copies, legends, second "
            f"sheets): {', '.join(repeated[:5])}"
        )

    layer_rows = _layers(snap, units["mm_per_unit"])
    layout_names = list(snap.layouts)
    if "Model" not in layout_names:
        layout_names.insert(0, "Model")
    space_counts = Counter(r.space for r in snap.records)
    return {
        "source": snap.source,
        "entities": len(snap.records),
        "units": units,
        "extents": extents,
        "clusters": prep["clusters"],
        "layers": layer_rows,
        "equipment_tags": tags,
        "rooms": rooms["rooms"],
        "room_faces": {
            "faces": rooms["faces"],
            "unlabelled": rooms["faces_unlabelled"],
            "wall_segments": rooms["wall_segments"],
            "arcs_skipped": rooms["arcs_skipped"],
            "tolerance": tol,
        },
        "languages": _languages(snap, layer_rows),
        "blocks": _blocks(snap),
        "layouts": [{"name": n, "entities": space_counts.get(n, 0)} for n in layout_names],
        "title_block": _title_block(snap),
        "warnings": warnings,
    }
