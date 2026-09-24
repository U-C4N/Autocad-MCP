"""drawing_scale_check: is this drawing to scale against that one?

A P&ID and a layout of one plant carry the same equipment tags. If the P&ID
were drawn to scale, the distance between two tanks on it would be the distance
on the layout times one factor, for every pair. On a schematic it is not - and
a pipe length measured on a schematic is a length of drawn line, not of pipe.

The check, in the order it runs:

1. **Scope.** Model-space tags are read (`describe.tag_occurrences`) and the
   drawings split into clusters (`clusters.find_clusters`, outliers left out).
   Every pair of scopes - the whole drawing or one of its eight largest
   clusters, on each side - is tried, and the pair that matches the most tags
   wins (ties: the whole drawing first, then the larger cluster). Two copies
   of one plan side by side therefore resolve to one copy instead of making
   every tag ambiguous.
2. **Match.** Only a tag found exactly once in each scope is matched. A tag
   found more than once in either (a plan copy, a legend, a second sheet) is
   ``ambiguous`` and takes no part: picking one of its positions would be a
   guess.
3. **Pairs.** For every two matched tags whose distance on the reference
   drawing ``b`` is at least ``min_pair_mm``, the ratio of their distance on
   ``a`` to that on ``b`` is taken, both converted to millimetres by each
   drawing's inferred unit (`units.infer_units`).
4. **Statistics.** The median ratio, the quartiles and their range, and the
   share of pairs within +/-10 % of the median - overall, and per room of the
   reference drawing (the labelled faces `describe.find_rooms` finds) or, for
   tags in no room, per cluster.
5. **Verdict.** ``to_scale`` when at least 80 % of the pairs are within the
   band (``factor`` is then the median: a drawing at a different but uniform
   scale is recognised); ``schematic`` at 50 % or less; ``partly`` in between;
   ``insufficient`` with fewer than three matched tags or no pair above the
   floor.

A tag label is not an equipment centre: the band and the floor are there to
absorb the offset between the two, and the result says so. Pure; neither
drawing is touched.
"""

from __future__ import annotations

import math
import statistics
from itertools import combinations

from engineering.arch.faces import face_containing
from engineering.understand.describe import FACE_TOL_FRACTION, find_rooms, prepare, tag_occurrences
from engineering.understand.snapshot import Snapshot
from engineering.understand.units import infer_units

__all__ = ["MIN_TAGS", "SCHEMATIC_SHARE", "TO_SCALE_SHARE", "WITHIN", "match_tags", "scale_check"]

#: The band around the median a pair must fall in to count as consistent.
WITHIN = 0.10
TO_SCALE_SHARE = 0.80
SCHEMATIC_SHARE = 0.50
MIN_TAGS = 3
#: Clusters per drawing tried as a scope, largest first.
MAX_SCOPE_CLUSTERS = 8
#: Pair rows listed in the result, farthest from the median first.
PAIR_ROW_CAP = 200
NOTE = (
    "Tag labels are not equipment centres; the +/-10 % band and the min_pair_mm floor absorb "
    "the offset between a label and what it labels."
)


def _side(snap: Snapshot) -> dict:
    prep = prepare(snap)
    prep["tags"] = [o for o in tag_occurrences(snap, prep=prep) if o["space"] == "Model"]
    return prep


def _scoped(side: dict, cluster: str | None, name: str) -> list[dict]:
    if cluster is None:
        return side["tags"]
    ids = [c["id"] for c in side["clusters"]]
    if cluster not in ids:
        raise ValueError(f"{name}: no cluster {cluster!r}; the drawing has {ids}")
    return [o for o in side["tags"] if o["cluster"] == cluster]


def _match(side_a: dict, side_b: dict, cluster_a: str | None, cluster_b: str | None) -> dict:
    occ_a = _scoped(side_a, cluster_a, "cluster_a")
    occ_b = _scoped(side_b, cluster_b, "cluster_b")
    count_a: dict[str, list[dict]] = {}
    count_b: dict[str, list[dict]] = {}
    for o in occ_a:
        count_a.setdefault(o["tag"], []).append(o)
    for o in occ_b:
        count_b.setdefault(o["tag"], []).append(o)
    matched = {
        tag: (tuple(count_a[tag][0]["at"]), tuple(count_b[tag][0]["at"]))
        for tag in sorted(set(count_a) & set(count_b))
        if len(count_a[tag]) == 1 and len(count_b[tag]) == 1
    }
    ambiguous = sorted(
        tag
        for tag in set(count_a) | set(count_b)
        if len(count_a.get(tag, ())) > 1 or len(count_b.get(tag, ())) > 1
    )
    return {
        "matched": matched,
        "ambiguous": ambiguous,
        "occurrences": {
            tag: {"a": len(count_a.get(tag, ())), "b": len(count_b.get(tag, ()))}
            for tag in ambiguous
        },
        "only_a": sorted(t for t in count_a if t not in count_b and len(count_a[t]) == 1),
        "only_b": sorted(t for t in count_b if t not in count_a and len(count_b[t]) == 1),
        "cluster_a": cluster_a,
        "cluster_b": cluster_b,
    }


def match_tags(a: Snapshot, b: Snapshot, *, cluster_a=None, cluster_b=None) -> dict:
    """``{"matched": {tag: (pa, pb)}, "ambiguous": [tag, ...], ...}`` within the
    given scopes (a cluster id such as ``"C1"``, or None for the whole model
    space). ``pa`` / ``pb`` are the tag's positions in drawing units. Also
    ``occurrences`` (per ambiguous tag, how often each side has it),
    ``only_a`` / ``only_b`` and the scopes used. An unknown cluster id is
    refused with the ids the drawing has."""
    return _match(_side(a), _side(b), cluster_a, cluster_b)


def _stats(ratios: list[float]) -> dict:
    if not ratios:
        return {"median": None, "q1": None, "q3": None, "iqr": None, "within_10pct": None}
    median = statistics.median(ratios)
    if len(ratios) >= 2:
        q1, _q2, q3 = statistics.quantiles(ratios, n=4, method="inclusive")
    else:
        q1 = q3 = ratios[0]
    within = sum(1 for r in ratios if abs(r - median) <= WITHIN * median) / len(ratios)
    return {"median": median, "q1": q1, "q3": q3, "iqr": q3 - q1, "within_10pct": within}


def _verdict(tag_count: int, pair_count: int, stats: dict) -> str:
    if tag_count < MIN_TAGS or pair_count == 0 or not stats["median"] or stats["median"] <= 0:
        return "insufficient"
    if stats["within_10pct"] >= TO_SCALE_SHARE:
        return "to_scale"
    if stats["within_10pct"] <= SCHEMATIC_SHARE:
        return "schematic"
    return "partly"


def _summary(tags: list[str], rows: list[dict]) -> dict:
    stats = _stats([r["ratio"] for r in rows])
    verdict = _verdict(len(tags), len(rows), stats)
    return {
        "verdict": verdict,
        "factor": stats["median"] if verdict == "to_scale" else None,
        **stats,
        "pair_count": len(rows),
    }


def scale_check(a: Snapshot, b: Snapshot, *, min_pair_mm=2000.0) -> dict:
    """Is drawing ``a`` to scale against the reference drawing ``b``?

    Returns ``{"verdict", "factor", "median", "q1", "q3", "iqr", "within_10pct",
    "pair_count", "pairs", "pairs_truncated", "pairs_below_floor", "matched",
    "ambiguous", "occurrences", "only_a", "only_b", "groups", "scope", "units",
    "min_pair_mm", "thresholds", "notes"}``. ``pairs`` lists at most 200 rows,
    farthest from the median first; ``matched`` is ``[{"tag", "a": [x, y],
    "b": [x, y]}]`` in drawing units. Refused before anything is computed: a
    ``min_pair_mm`` that is not a positive finite number.
    """
    try:
        floor = float(min_pair_mm)
    except (TypeError, ValueError):
        raise ValueError(f"min_pair_mm: expected a number, got {min_pair_mm!r}") from None
    if not math.isfinite(floor) or floor <= 0.0:
        raise ValueError(f"min_pair_mm: must be a positive number of millimetres, got {floor:g}")
    side_a, side_b = _side(a), _side(b)

    best = None
    for ca in [None] + [c["id"] for c in side_a["clusters"][:MAX_SCOPE_CLUSTERS]]:
        for cb in [None] + [c["id"] for c in side_b["clusters"][:MAX_SCOPE_CLUSTERS]]:
            m = _match(side_a, side_b, ca, cb)
            if best is None or len(m["matched"]) > len(best["matched"]):
                best = m

    tol_b = side_b["diag"] * FACE_TOL_FRACTION if side_b["diag"] > 0 else 1.0
    rooms_b = find_rooms(side_b["kept"], tol=tol_b, membership=side_b["membership"])
    units = {}
    notes = [NOTE]
    for key, snap, side, rooms in (
        ("a", a, side_a, None),
        ("b", b, side_b, rooms_b),
    ):
        areas = [row["face"]["area"] for row, _f in rooms["labelled"]] if rooms else []
        u = infer_units(snap, records=side["kept"], room_areas=areas)
        units[key] = {
            "unit": u["inferred"],
            "mm_per_unit": u["mm_per_unit"],
            "declared": u["declared"],
        }
        if u["mm_per_unit"] is None:
            notes.append(
                f"drawing {key}: no unit could be settled; its drawing units are taken as mm"
            )
    fa = units["a"]["mm_per_unit"] or 1.0
    fb = units["b"]["mm_per_unit"] or 1.0

    matched = best["matched"]
    tags = sorted(matched)
    rows: list[dict] = []
    below = 0
    for ti, tj in combinations(tags, 2):
        d_b = math.dist(matched[ti][1], matched[tj][1]) * fb
        if d_b < floor:
            below += 1
            continue
        d_a = math.dist(matched[ti][0], matched[tj][0]) * fa
        rows.append({"tags": [ti, tj], "a_mm": d_a, "b_mm": d_b, "ratio": d_a / d_b})
    overall = _summary(tags, rows)

    faces = [face for _row, face in rooms_b["labelled"]]
    room_of = {id(face): row for row, face in rooms_b["labelled"]}
    groups: dict[str, dict] = {}
    for tag in tags:
        pb = matched[tag][1]
        face = face_containing(faces, pb) if faces else None
        if face is not None:
            row = room_of[id(face)]
            key = " ".join(p for p in ("room", row["number"], row["name"]) if p)
            kind = "room"
        else:
            cluster = next(
                (o["cluster"] for o in side_b["tags"] if o["tag"] == tag and tuple(o["at"]) == pb),
                None,
            )
            key, kind = (cluster or "unclustered"), "cluster"
        groups.setdefault(key, {"group": key, "kind": kind, "tags": []})["tags"].append(tag)
    group_rows = []
    for key in sorted(groups):
        g = groups[key]
        if len(g["tags"]) < 2:
            continue
        members = set(g["tags"])
        inside = [r for r in rows if r["tags"][0] in members and r["tags"][1] in members]
        group_rows.append({**g, **_summary(g["tags"], inside)})

    median = overall["median"]
    listed = sorted(
        rows,
        key=lambda r: (-(abs(r["ratio"] - median) if median else 0.0), r["tags"]),
    )[:PAIR_ROW_CAP]
    return {
        **overall,
        "pairs": listed,
        "pairs_truncated": len(rows) > PAIR_ROW_CAP,
        "pairs_below_floor": below,
        "matched": [{"tag": t, "a": list(matched[t][0]), "b": list(matched[t][1])} for t in tags],
        "ambiguous": best["ambiguous"],
        "occurrences": best["occurrences"],
        "only_a": best["only_a"],
        "only_b": best["only_b"],
        "groups": group_rows,
        "scope": {"cluster_a": best["cluster_a"], "cluster_b": best["cluster_b"]},
        "units": units,
        "min_pair_mm": floor,
        "thresholds": {
            "within": WITHIN,
            "to_scale_share": TO_SCALE_SHARE,
            "schematic_share": SCHEMATIC_SHARE,
            "min_tags": MIN_TAGS,
        },
        "notes": notes,
    }
