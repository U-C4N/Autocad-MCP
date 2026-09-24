"""drawing_diff's engine: signatures, the three matching stages, the report.

Pure tests build ``EntityRecord``s by hand, so every coordinate is the
answer. The known-edits tests build two revisions of the synthetic plant P&ID
(``tests/fixtures/revisions.py``) and assert exactly the edits made - a move,
a text change, an attribute change, an add, a delete, under renumbered
handles - and nothing else.
"""

from __future__ import annotations

import ezdxf
import pytest

import config
from engineering.understand.diff import (
    block_changes,
    block_signatures,
    cap_report,
    diff_snapshots,
    read_revision,
    robust_box,
    signature,
)
from engineering.understand.snapshot import EntityRecord, Snapshot
from tests.fixtures.revisions import (
    ADDED_LAYER,
    NEW_TEXT,
    TAG_AFTER,
    TAG_BEFORE,
    build_revision_pair,
)


def rec(handle, type_, layer="PIPE", points=(), **kw):
    space = kw.pop("space", "Model")
    return EntityRecord(
        handle=handle, type=type_, layer=layer, space=space, points=tuple(points), **kw
    )


def snap(*records):
    return Snapshot(
        source="test",
        insunits=4,
        extmin=None,
        extmax=None,
        layers={},
        layouts=(),
        records=tuple(records),
    )


def line(handle, x1, y1, x2, y2, layer="PIPE"):
    return rec(handle, "LINE", layer, ((x1, y1), (x2, y2)))


STILL = [line("A", 0, 0, 100, 0), line("B", 0, 50, 100, 50), line("C", 0, 100, 100, 100)]
RENUMBERED = [line("1A", 0, 0, 100, 0), line("1B", 0, 50, 100, 50), line("1C", 0, 100, 100, 100)]


# -- signatures ------------------------------------------------------------------------


def test_a_signature_is_rounded_to_tol_and_ignores_a_lines_direction():
    a = line("A", 0.0, 0.0, 10.0, 0.0)
    b = line("B", 10.001, 0.004, 0.0, 0.0)
    assert signature(a, 0.01) == signature(b, 0.01)
    assert signature(a, 0.01) != signature(line("C", 0.0, 0.0, 10.02, 0.0), 0.01)


def test_identical_revisions_have_no_change_and_no_cluster():
    result = diff_snapshots(snap(*STILL), snap(*STILL))
    assert result["summary"] == {
        "old_entities": 3,
        "new_entities": 3,
        "unchanged": 3,
        "changed": 0,
        "added": 0,
        "removed": 0,
    }
    assert result["clusters"] == [] and result["handles_stable"] is True


# -- the three stages ------------------------------------------------------------------


def test_a_move_under_renumbered_handles_is_matched_by_position():
    old = snap(*STILL, line("D", 0, 200, 100, 200))
    new = snap(*RENUMBERED, line("1D", 3, 200, 103, 200))
    result = diff_snapshots(old, new, move_tol=10.0)
    assert result["handles_stable"] is False
    (row,) = result["changed"]
    assert (row["old_handle"], row["new_handle"], row["matched_by"]) == ("D", "1D", "position")
    assert row["changes"] == {"moved_by": [pytest.approx(3.0), pytest.approx(0.0)]}
    assert result["summary"]["unchanged"] == 3


def test_a_text_change_is_reported_from_and_to():
    old = snap(*STILL, rec("T", "TEXT", "NOTES", ((10, 10),), text="OLD", height=2.5))
    new = snap(*STILL, rec("T", "TEXT", "NOTES", ((10, 10),), text="NEW", height=2.5))
    (row,) = diff_snapshots(old, new)["changed"]
    assert row["matched_by"] == "handle"
    assert row["changes"] == {"text": {"from": "OLD", "to": "NEW"}}


def test_an_attribute_change_is_reported_by_tag():
    before = (("TAG", "A-1"), ("SERVICE", "CIP"))
    after = (("TAG", "A-2"), ("SERVICE", "CIP"))
    old = snap(*STILL, rec("I", "INSERT", "TAGS", ((500, 500),), block="TB", attribs=before))
    new = snap(*STILL, rec("I", "INSERT", "TAGS", ((500, 500),), block="TB", attribs=after))
    (row,) = diff_snapshots(old, new)["changed"]
    assert row["changes"] == {"attribs": [{"tag": "TAG", "from": "A-1", "to": "A-2"}]}


def test_a_radius_change_is_reported_from_and_to():
    old = snap(*STILL, rec("R", "CIRCLE", "PIPE", ((0, 0),), radius=10.0))
    new = snap(*STILL, rec("R", "CIRCLE", "PIPE", ((0, 0),), radius=12.0))
    (row,) = diff_snapshots(old, new)["changed"]
    assert row["changes"] == {"radius": {"from": 10.0, "to": 12.0}}


def test_the_handle_stage_catches_a_layer_change_the_position_stage_cannot():
    old = snap(*STILL, line("L", 0, 300, 100, 300, layer="P1"))
    new = snap(*STILL, line("L", 0, 300, 100, 300, layer="P2"))
    (row,) = diff_snapshots(old, new)["changed"]
    assert row["matched_by"] == "handle"
    assert row["changes"] == {"layer": {"from": "P1", "to": "P2"}}


def test_stable_handles_pair_a_far_move_by_handle():
    old = snap(*STILL, rec("20", "CIRCLE", "PIPE", ((0, 0),), radius=5.0))
    new = snap(*STILL, rec("20", "CIRCLE", "PIPE", ((5000, 5000),), radius=5.0))
    (row,) = diff_snapshots(old, new, move_tol=10.0)["changed"]
    assert row["matched_by"] == "handle"
    assert row["changes"] == {"moved_by": [pytest.approx(5000.0), pytest.approx(5000.0)]}


def test_renumbered_handles_never_pair_strangers_that_share_a_handle():
    old = snap(*STILL, rec("20", "CIRCLE", "PIPE", ((0, 0),), radius=5.0))
    new = snap(*RENUMBERED, rec("20", "CIRCLE", "PIPE", ((5000, 5000),), radius=5.0))
    result = diff_snapshots(old, new, move_tol=10.0)
    assert result["handles_stable"] is False
    assert result["changed"] == []
    assert [r["handle"] for r in result["removed"]] == ["20"]
    assert [r["at"] for r in result["added"]] == [[5000.0, 5000.0]]


def test_no_exact_match_is_no_evidence_that_handles_are_stable():
    # two fresh files hand out handles from one seed; every circle moved +1000 in x
    # and the new file added them in reverse order, so equal handles are strangers
    old = snap(
        *(rec(f"{0x20 + i:X}", "CIRCLE", "EQ", ((0, 200 * i),), radius=10.0) for i in range(8))
    )
    new = snap(
        *(
            rec(f"{0x20 + k:X}", "CIRCLE", "EQ", ((1000, 200 * i),), radius=10.0)
            for k, i in enumerate(reversed(range(8)))
        )
    )
    result = diff_snapshots(old, new)
    assert result["handles_stable"] is False
    assert result["handle_evidence"] == {"exact_matches": 0, "kept_handle": 0}
    assert result["changed"] == []
    assert (result["summary"]["added"], result["summary"]["removed"]) == (8, 8)
    # given a move tolerance that reaches, the position stage finds the true move
    moved = diff_snapshots(old, new, move_tol=1001.0)
    assert {row["matched_by"] for row in moved["changed"]} == {"position"}
    assert {tuple(row["changes"]["moved_by"]) for row in moved["changed"]} == {(1000.0, 0.0)}


def test_beyond_move_tol_a_line_is_removed_and_another_added():
    old = snap(*STILL, line("D", 0, 200, 100, 200))
    new = snap(*STILL, line("E", 0, 700, 100, 700))
    result = diff_snapshots(old, new, move_tol=10.0)
    assert (result["summary"]["added"], result["summary"]["removed"]) == (1, 1)
    assert result["changed"] == []


# -- a value split by the signature grid ----------------------------------------------


def test_a_value_one_ulp_across_a_half_grid_point_is_unchanged():
    # AutoCAD writes 16 significant digits: 0.14 + 0.005 = 0.14500000000000002 comes
    # back as 0.145, and at tol=0.01 the two snap to buckets 15 and 14
    assert 0.14 + 0.005 != 0.145
    edge = 0.14 + 0.005
    assert signature(line("L", edge, 300, 50, 300)) != signature(line("L", 0.145, 300, 50, 300))
    old = snap(*STILL, line("L", edge, 300, 50, 300))
    for new in (
        snap(*STILL, line("L", 0.145, 300, 50, 300)),  # paired by handle
        snap(*RENUMBERED, line("1L", 0.145, 300, 50, 300)),  # paired by position
    ):
        result = diff_snapshots(old, new, move_tol=10.0)
        assert result["summary"]["unchanged"] == 4
        assert (result["summary"]["changed"], result["summary"]["added"]) == (0, 0)
        assert result["summary"]["removed"] == 0
        assert result["changed"] == [] and result["clusters"] == []
        assert result["by_layer"] == {} and result["by_type"] == {}


def test_numbers_one_ulp_across_a_grid_boundary_are_not_changes():
    # at tol=1, 12.500000000000002 and 12.5 snap to 13 and 12
    edge = 12.500000000000002
    old = snap(
        *STILL,
        rec("R", "CIRCLE", "PIPE", ((edge, 0),), radius=edge),
        rec("T", "TEXT", "NOTES", ((10, 10),), text="X", height=edge, rotation=edge),
        rec("P", "LWPOLYLINE", "PIPE", ((0, 0), (10, 0)), bulges=(edge, 0.0)),
    )
    new = snap(
        *STILL,
        rec("R", "CIRCLE", "PIPE", ((12.5, 0),), radius=12.5),
        rec("T", "TEXT", "NOTES", ((10, 10),), text="X", height=12.5, rotation=12.5),
        rec("P", "LWPOLYLINE", "PIPE", ((0, 0), (10, 0)), bulges=(12.5, 0.0)),
    )
    result = diff_snapshots(old, new, tol=1.0)
    assert result["summary"]["unchanged"] == 6
    assert result["changed"] == [] and result["clusters"] == []


def test_a_real_edit_next_to_a_grid_split_is_still_reported():
    old = snap(
        *STILL,
        line("L", 0.14 + 0.005, 300, 50, 300),
        rec("R", "CIRCLE", "PIPE", ((0, 0),), radius=10.0),
    )
    new = snap(
        *STILL, line("L", 0.145, 300, 50, 300), rec("R", "CIRCLE", "PIPE", ((0, 0),), radius=10.2)
    )
    result = diff_snapshots(old, new)
    (row,) = result["changed"]
    assert row["old_handle"] == "R"
    assert row["changes"] == {"radius": {"from": 10.0, "to": 10.2}}
    assert result["summary"]["unchanged"] == 4
    assert [c["count"] for c in result["clusters"]] == [1]


# -- the move tolerance ----------------------------------------------------------------


def test_the_default_move_tol_is_2_percent_of_the_robust_diagonal():
    # 100 points on the diagonal of a 2970 x 3960 box, and one stray 10 km away:
    # the 1st-99th percentile box is (30, 40)-(2970, 3960), diagonal 4900.
    points = [rec(f"P{i}", "POINT", "PTS", ((i * 30.0, i * 40.0),)) for i in range(100)]
    stray = rec("FAR", "POINT", "PTS", ((1.0e7, 1.0e7),))
    drawing = snap(*points, stray)
    assert robust_box(drawing) == (30.0, 40.0, 2970.0, 3960.0)
    assert diff_snapshots(drawing, drawing)["move_tol"] == pytest.approx(98.0)


def test_a_non_positive_tolerance_is_refused_by_name():
    with pytest.raises(ValueError, match=r"^tol must be a positive number; got 0"):
        diff_snapshots(snap(), snap(), tol=0)
    with pytest.raises(ValueError, match=r"^move_tol must be a positive number; got -1"):
        diff_snapshots(snap(), snap(), move_tol=-1)


# -- the report ------------------------------------------------------------------------


def test_changes_are_counted_by_layer_and_type_and_clustered():
    old = snap(
        line("U", 0, -500, 100, -500),
        line("D", 0, 200, 100, 200),
        rec("T", "TEXT", "NOTES", ((10, 10),), text="OLD", height=2.5),
    )
    new = snap(
        line("U", 0, -500, 100, -500),
        line("D", 3, 200, 103, 200),
        rec("T", "TEXT", "NOTES", ((10, 10),), text="NEW", height=2.5),
        rec("N1", "CIRCLE", "PIPE", ((5000, 5000),), radius=5.0, bbox=(4995, 4995, 5005, 5005)),
    )
    result = diff_snapshots(old, new, move_tol=10.0)
    assert result["by_layer"] == {
        "NOTES": {"changed": 1, "added": 0, "removed": 0},
        "PIPE": {"changed": 1, "added": 1, "removed": 0},
    }
    assert result["by_type"] == {
        "CIRCLE": {"changed": 0, "added": 1, "removed": 0},
        "LINE": {"changed": 1, "added": 0, "removed": 0},
        "TEXT": {"changed": 1, "added": 0, "removed": 0},
    }
    # pad = max(0.25 x 10, 10 x 0.01) = 2.5 around each cluster
    assert result["clusters"] == [
        {"space": "Model", "box": [-2.5, 197.5, 105.5, 202.5], "count": 1},
        {"space": "Model", "box": [7.5, 7.5, 12.5, 12.5], "count": 1},
        {"space": "Model", "box": [4992.5, 4992.5, 5007.5, 5007.5], "count": 1},
    ]


def test_cap_report_cuts_every_list_and_says_how_much():
    old = snap(*STILL)
    new = snap(*STILL, *(line(f"N{i}", 0, 1000 * (i + 1), 100, 1000 * (i + 1)) for i in range(5)))
    capped = cap_report(diff_snapshots(old, new, move_tol=10.0), limit=2)
    assert len(capped["added"]) == 2
    assert capped["truncated"] == {"changed": 0, "added": 3, "removed": 0}
    assert capped["summary"]["added"] == 5


# -- block definitions -----------------------------------------------------------------


def _blocks(radius: float, extra: bool):
    doc = ezdxf.new()
    doc.blocks.new("B1").add_circle((0, 0), radius)
    doc.blocks.new("B2").add_line((0, 0), (10, 0))
    if extra:
        doc.blocks.new("B3").add_line((0, 0), (0, 10))
    doc.blocks.new_anonymous_block().add_line((0, 0), (1, 1))
    return doc


def test_block_definitions_are_compared_by_content():
    old = block_signatures(_blocks(5.0, extra=False))
    new = block_signatures(_blocks(6.0, extra=True))
    assert sorted(old) == ["B1", "B2"]  # the anonymous *U block is left out
    assert block_changes(old, new) == {
        "compared": True,
        "added": ["B3"],
        "removed": [],
        "changed": ["B1"],
    }
    assert block_changes(None, new)["compared"] is False


# -- reading a revision ----------------------------------------------------------------


def test_read_revision_refuses_a_file_over_the_size_limit(tmp_path, monkeypatch):
    path = tmp_path / "big.dxf"
    ezdxf.new().saveas(path)
    monkeypatch.setattr(config.settings, "max_dxf_bytes", 10)
    with pytest.raises(ValueError, match=r"over the MAX_DXF_BYTES limit of 10"):
        read_revision(str(path))


def test_read_revision_refuses_a_file_that_is_not_a_dxf(tmp_path):
    path = tmp_path / "notes.dxf"
    path.write_text("not a drawing\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"not a readable DXF"):
        read_revision(str(path))


def test_read_revision_refuses_a_truncated_dxf(tmp_path):
    # an interrupted save: ezdxf's tag reader runs out and raises StopIteration,
    # which must surface as the refusal, not escape (it would hang asyncio.to_thread)
    path = tmp_path / "half.dxf"
    path.write_bytes(b"  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1015\n")
    with pytest.raises(ValueError, match=r"half\.dxf: not a readable DXF \(the file ends early"):
        read_revision(str(path))


def test_read_revision_refuses_a_non_positive_tol_by_name(tmp_path):
    path = tmp_path / "empty.dxf"
    ezdxf.new().saveas(path)
    with pytest.raises(ValueError, match=r"^tol must be a positive number"):
        read_revision(str(path), tol=0)


# -- the known edits on the synthetic plant --------------------------------------------


def test_the_known_edits_and_nothing_else(tmp_path):
    pair = build_revision_pair(tmp_path)
    old, old_blocks = read_revision(pair["old"])
    new, new_blocks = read_revision(pair["new"])
    result = diff_snapshots(old, new, move_tol=pair["move_tol"])
    total = pair["old_count"]
    assert result["summary"] == {
        "old_entities": total,
        "new_entities": total,
        "unchanged": total - 4,
        "changed": 3,
        "added": 1,
        "removed": 1,
    }
    changed = {row["old_handle"]: row for row in result["changed"]}
    assert set(changed) == {pair["moved"], pair["text"], pair["insert"]}
    assert {row["matched_by"] for row in result["changed"]} == {"position"}
    moved = changed[pair["moved"]]["changes"]
    assert list(moved) == ["moved_by"]
    assert moved["moved_by"][0] == pytest.approx(pair["move_tol"] / 2.0, abs=1e-9)
    assert moved["moved_by"][1] == pytest.approx(0.0, abs=1e-9)
    assert changed[pair["text"]]["changes"] == {
        "text": {"from": pair["text_before"], "to": NEW_TEXT}
    }
    assert changed[pair["insert"]]["changes"] == {
        "attribs": [{"tag": "TAG", "from": TAG_BEFORE, "to": TAG_AFTER}]
    }
    assert [row["handle"] for row in result["removed"]] == [pair["deleted"]]
    assert [(row["type"], row["layer"]) for row in result["added"]] == [("CIRCLE", ADDED_LAYER)]
    assert block_changes(old_blocks, new_blocks) == {
        "compared": True,
        "added": [],
        "removed": [],
        "changed": [],
    }


def test_renumbered_handles_alone_are_no_change(tmp_path):
    pair = build_revision_pair(tmp_path, edits=False)
    old, _ = read_revision(pair["old"])
    new, _ = read_revision(pair["new"])
    summary = diff_snapshots(old, new)["summary"]
    assert (summary["changed"], summary["added"], summary["removed"]) == (0, 0, 0)
    assert summary["unchanged"] == pair["old_count"]
