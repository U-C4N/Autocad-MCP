"""Golden query set for tool discovery: English in, one right tool out.

Sixty-six cases (51 tuning + 15 holdout) split into a **tuning** set and a
**holdout** set. The split is
the point of the file. Ranking work is measured against
:data:`TUNING_CASES` only; :data:`HOLDOUT_CASES` were written at the same time,
before any scoring parameter was chosen, and were not looked at until the
scorer was frozen. A retrieval number quoted against cases the ranker was fitted
to is not evidence of anything.

Rules the cases follow:

* Every ``expect`` is a tool that genuinely, unambiguously does the job. Where
  two tools would both be defensible answers (``view_screenshot`` vs
  ``view_zoom_and_screenshot``, ``entity_delete`` vs ``entity_delete_many``)
  the query is worded to pick one, or the case is left out. A golden set that
  scores ambiguity is measuring the author, not the ranker.
* ``kind`` records what the case exercises, so accuracy can be reported per
  category instead of as one flattering average.
* ``risk`` is the ceiling a caller would sensibly pass. It is ``"read"`` for
  the counting questions — the ones where stock BM25 answers "how many entities
  are on the GEOMETRY layer" with ``entity_delete_many``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ALL_CASES", "HOLDOUT_CASES", "TUNING_CASES", "GoldenCase"]


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """One query and the tool it must find."""

    query: str
    expect: str
    kind: str  # "command" | "synonym" | "paraphrase" | "counting"
    risk: str = "any"

    def __str__(self) -> str:  # readable pytest ids
        return f"{self.kind}:{self.query}"


# ── tuning set ──────────────────────────────────────────────────────────────
# Free to be measured against, argued with and re-measured.

TUNING_CASES: tuple[GoldenCase, ...] = (
    # AutoCAD command names a drafter types verbatim.
    # Re-pointed in v1.5.0 (M8): BPOLY now has a real destination. It was
    # parked on entity_create_polyline only because nothing traced a boundary,
    # and this is a correction of the expectation, not a tuning of the ranker.
    GoldenCase("BPOLY", "boundary_trace", "command"),
    GoldenCase("PEDIT", "entity_edit_geometry", "command"),
    GoldenCase("WBLOCK", "block_create_from_entities", "command"),
    GoldenCase("OVERKILL", "drawing_refine", "command"),
    GoldenCase("LAYTRANS", "template_apply_layers", "command"),
    GoldenCase("MATCHPROP", "entity_set_properties", "command"),
    GoldenCase("QSELECT", "entity_select_smart", "command"),
    GoldenCase("DIMLINEAR", "dimension_linear", "command"),
    GoldenCase("ARRAYPOLAR", "entity_array_polar", "command"),
    GoldenCase("MLEADER", "leader_create_mleader", "command"),
    GoldenCase("BHATCH", "entity_create_hatch", "command"),
    GoldenCase("LAYISO", "layer_isolate", "command"),
    GoldenCase("XLINE", "construction_xline", "command"),
    GoldenCase("EXPLODE", "block_explode", "command"),
    GoldenCase("DXFOUT", "drawing_export_dxf", "command"),
    GoldenCase("QDIM", "dimension_auto", "command"),
    GoldenCase("CHA", "entity_chamfer", "command"),
    GoldenCase("DLI", "dimension_linear", "command"),
    GoldenCase("LAYDEL", "layer_delete", "command"),
    GoldenCase("PURGE", "drawing_purge", "command"),
    # Shop-floor vocabulary.
    GoldenCase("bill of materials", "entity_create_table", "synonym"),
    GoldenCase("parts list", "entity_create_table", "synonym"),
    GoldenCase("round the corner", "entity_fillet", "synonym"),
    GoldenCase("bolt circle", "entity_array_polar", "synonym"),
    GoldenCase("title block", "titleblock_apply_iso_a3", "synonym"),
    GoldenCase("keyed bore", "keyway_draw_keyed_bore", "synonym"),
    GoldenCase("feature control frame", "gd_frame", "synonym"),
    GoldenCase("object snap", "point_from_snap", "synonym"),
    GoldenCase("zoom to fit", "view_zoom_extents", "synonym"),
    GoldenCase("smooth curve", "entity_create_spline", "synonym"),
    GoldenCase("true position", "gd_frame", "synonym"),
    GoldenCase("datum triangle", "datum_feature", "synonym"),
    # Paraphrases — no shared vocabulary with the tool name.
    GoldenCase("I need the corner rounded off with a 3 mm radius", "entity_fillet", "paraphrase"),
    GoldenCase(
        "put an arrow with a note pointing at the hole", "leader_create_mleader", "paraphrase"
    ),
    GoldenCase("turn a layer off so it does not print", "layer_hide", "paraphrase"),
    GoldenCase("make the whole shape smaller by half", "entity_scale", "paraphrase"),
    GoldenCase(
        "repeat this shape around a centre point six times", "entity_array_polar", "paraphrase"
    ),
    GoldenCase(
        "the two lines overshoot, cut them back to where they cross", "entity_trim", "paraphrase"
    ),
    GoldenCase("write a paragraph of general notes", "entity_create_mtext", "paraphrase"),
    GoldenCase("take a picture of the current drawing", "view_screenshot", "paraphrase"),
    GoldenCase("start a checkpoint I can roll back to", "transaction_begin", "paraphrase"),
    GoldenCase(
        "what is wrong with this drawing before I finish it", "drawing_critique", "paraphrase"
    ),
    GoldenCase("fill this closed area with section hatching", "entity_create_hatch", "paraphrase"),
    GoldenCase("bevel a corner at 45 degrees", "entity_chamfer", "paraphrase"),
    GoldenCase("load the dashed hidden linetype", "linetype_load", "paraphrase"),
    # Counting / inspection. Under stock BM25 the first of these answers with
    # entity_delete_many.
    GoldenCase(
        "how many entities are on the GEOMETRY layer",
        "analysis_entity_stats",
        "counting",
        risk="read",
    ),
    GoldenCase("what layers exist in this drawing", "layer_list", "counting", risk="read"),
    GoldenCase("how big is the drawing overall", "analysis_bounding_box", "counting", risk="read"),
    # Re-pointed when analysis_measure_entity was added, NOT to match the
    # ranker: "this closed polyline" names an entity, and until that tool existed
    # the only destination was the one tool that cannot look at the drawing. The
    # expectation was wrong the whole time; the case is now answerable.
    GoldenCase(
        "what is the area of this closed polyline",
        "analysis_measure_entity",
        "counting",
        risk="read",
    ),
    # Withdrawn: "list every circle in the drawing" -> analysis_select_by_type.
    # ``entity_list(type_filter="CIRCLE")`` answers it just as correctly (and
    # with a layer filter and paging on top), so the case scored the author's
    # preference, not the ranker. Replaced rather than re-labelled: relabelling
    # a case to whatever the ranker returned is how a golden set stops being
    # evidence.
    GoldenCase(
        "what is inside this rectangular window",
        "analysis_find_in_region",
        "counting",
        risk="read",
    ),
    GoldenCase(
        "where is this block used in the drawing",
        "block_find_references",
        "counting",
        risk="read",
    ),
)


# ── holdout set ─────────────────────────────────────────────────────────────
# Written alongside the tuning cases, before any scoring constant was chosen,
# and deliberately not measured until the scorer was frozen. Do not tune
# against these. If a holdout case fails, the honest fix is a new tuning case
# that captures the same class of failure — not an edit to this tuple.

HOLDOUT_CASES: tuple[GoldenCase, ...] = (
    GoldenCase("DDEDIT", "entity_edit_text", "command"),
    GoldenCase("MVIEW", "viewport_create", "command"),
    GoldenCase("SPL", "entity_create_spline", "command"),
    GoldenCase("TOLERANCE", "gd_frame", "command"),
    GoldenCase("crosshatch", "entity_create_hatch", "synonym"),
    GoldenCase("wall thickness", "entity_offset", "synonym"),
    GoldenCase("undo the undo", "drawing_redo", "synonym"),
    GoldenCase("pitch circle", "entity_create_circle", "synonym"),
    GoldenCase("I want a parallel copy 5 mm inside the outline", "entity_offset", "paraphrase"),
    GoldenCase("get rid of the unused blocks and layers", "drawing_purge", "paraphrase"),
    GoldenCase("change the wording of this label", "entity_edit_text", "paraphrase"),
    GoldenCase("make a scaled window of the model on the sheet", "viewport_create", "paraphrase"),
    GoldenCase(
        "how far apart are these two points",
        "analysis_measure_distance",
        "counting",
        risk="read",
    ),
    GoldenCase("which blocks are defined", "block_list", "counting", risk="read"),
    GoldenCase("what are the properties of this entity", "entity_get", "counting", risk="read"),
)


ALL_CASES: tuple[GoldenCase, ...] = TUNING_CASES + HOLDOUT_CASES
