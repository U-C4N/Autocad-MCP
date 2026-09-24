"""Two revisions of the synthetic plant P&ID with known edits, for ``drawing_diff``.

Built from ``build_plant_pair`` (``tests/fixtures/plant_pair.py``) with
ezdxf. The older revision is the P&ID plus one tagged block reference; the
newer one moves a line by half the move tolerance, rewrites a text, changes
the block's attribute, deletes one line, adds a circle - and then gives every
model-space entity a new handle, the way a save through another program
renumbers them. What was edited is
picked from the older revision's own records (entities whose signature is
unique, so a double-drawn line can never be the one edited) and returned with
the paths: the expected answer is read off the fixture, never typed in.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import ezdxf

from engineering.understand.diff import diff_snapshots, read_revision, robust_box, signature
from engineering.understand.snapshot import records_from_doc
from tests.fixtures.plant_pair import build_plant_pair

TAG_BLOCK = "DIFF_TAG"
TAG_LAYER = "DIFF-TAGS"
ADDED_LAYER = "DIFF-ADDED"
NEW_TEXT = "REV-B NOTE"
TAG_BEFORE = "X-1"
TAG_AFTER = "X-2"


def renumber_modelspace(doc) -> None:
    """Every model-space entity replaced by a copy under a fresh handle."""
    msp = doc.modelspace()
    for entity in list(msp):
        clone = entity.copy()
        msp.add_entity(clone)
        msp.delete_entity(entity)


def build_revision_pair(directory, *, edits: bool = True) -> dict:
    directory = Path(directory)
    truth = build_plant_pair(directory / "plant")
    base = ezdxf.readfile(truth["pid"])
    box = robust_box(records_from_doc(base, source="base"))
    centre = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
    block = base.blocks.new(TAG_BLOCK)
    block.add_circle((0.0, 0.0), 5.0)
    block.add_attdef("TAG", (0.0, -8.0), dxfattribs={"height": 2.5})
    base.layers.add(TAG_LAYER)
    tag = base.modelspace().add_blockref(TAG_BLOCK, centre, dxfattribs={"layer": TAG_LAYER})
    tag.add_auto_attribs({"TAG": TAG_BEFORE})
    old_path = directory / "rev_a.dxf"
    base.saveas(old_path)

    old_snap, _blocks = read_revision(str(old_path))
    move_tol = diff_snapshots(old_snap, old_snap)["move_tol"]
    counts = Counter(signature(r) for r in old_snap.records)
    unique = [r for r in old_snap.records if r.space == "Model" and counts[signature(r)] == 1]
    moved = next(r for r in unique if r.type == "LINE")
    text = next(r for r in unique if r.type in ("TEXT", "MTEXT"))
    insert = next(r for r in old_snap.records if r.block == TAG_BLOCK)
    deleted = next(
        r
        for r in unique
        if r.type in ("LINE", "LWPOLYLINE") and (r.type, r.layer) != (moved.type, moved.layer)
    )

    new = ezdxf.readfile(old_path)
    msp = new.modelspace()
    if edits:
        new.entitydb[moved.handle].translate(move_tol / 2.0, 0.0, 0.0)
        target = new.entitydb[text.handle]
        if target.dxftype() == "MTEXT":
            target.text = NEW_TEXT
        else:
            target.dxf.text = NEW_TEXT
        new.entitydb[insert.handle].get_attrib("TAG").dxf.text = TAG_AFTER
        msp.delete_entity(new.entitydb[deleted.handle])
        new.layers.add(ADDED_LAYER)
        msp.add_circle(centre, move_tol / 10.0, dxfattribs={"layer": ADDED_LAYER})
    renumber_modelspace(new)
    new_path = directory / "rev_b.dxf"
    new.saveas(new_path)
    return {
        "old": str(old_path),
        "new": str(new_path),
        "move_tol": move_tol,
        "old_count": len(old_snap.records),
        "moved": moved.handle,
        "text": text.handle,
        "text_before": text.text,
        "insert": insert.handle,
        "deleted": deleted.handle,
    }
