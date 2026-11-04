"""The critique focuses, as the ``autocad://critique/focuses`` resource serves them (1.8-16).

``drawing_critique``'s ``focus`` parameter used to carry the whole closed enum
in its description - about 900 characters on every tools/list. The list lives
here now, one row per focus with its domain, what it reports and when it is
silent, and the parameter description points at the resource. The enum is
still closed: ``CritiqueFocus`` / ``ALL_CRITIQUE_FOCUSES`` in
``engineering/plan_spec.py`` are the truth, and ``tests/test_qa_coverage.py``
holds this catalog to exactly that set - a new focus without a row here fails.
"""

from __future__ import annotations

from engineering.plan_spec import ALL_CRITIQUE_FOCUSES
from engineering.qa_coverage import GATE_EXEMPT_REASONS, NOT_CHECKED_REASONS

__all__ = ["FOCUS_CATALOG", "critique_focuses"]

_PID = "silent (domain_absent) on a drawing with no recognised P&ID symbol"
_MECH = "silent (domain_absent) on a drawing with no ACADMCP_MECH payload"
_ARCH = "silent (domain_absent) on a drawing with no ACADMCP_ARCH record"
_TOPO = (
    "silent (domain_absent) when no layer is classified piping or electrical outside "
    "the P&ID and architectural layer sets"
)

#: focus -> (domain, what it reports, when it is silent or None).
FOCUS_CATALOG: dict[str, tuple[str, str, str | None]] = {
    "iso128": (
        "core",
        "layer lineweights outside the ISO 128 set; dimension text under 2.5 mm (ISO 3098) "
        "or a decimal marker other than a point (ISO 129)",
        "the dimension-legibility lane is not_checked on a live document "
        "(engine_live_document_template) and on paper layouts (no_viewport_scale / "
        "viewport_scale_deferred)",
    ),
    "layer_color": ("core", "an engineering layer that lost its canonical ACI colour", None),
    "dim_overlap": ("core", "two dimensions of one space whose text points are within 5 mm", None),
    "untrimmed_corner": ("core", "two LINE ends within 0.5 mm that do not meet", None),
    "duplicate_entities": ("core", "two LINEs of one space with the same endpoints", None),
    "construction_left": ("core", "entities still on a construction layer", None),
    "property_override": (
        "core",
        "an entity's own lineweight / colour / linetype instead of ByLayer "
        "(lineweight_not_bylayer, color_not_bylayer, linetype_not_bylayer)",
        None,
    ),
    "dimension_truth": (
        "core",
        "a DIMENSION whose sheet text is not its geometry (dim_override_disagrees, "
        "dim_dimlfac_scaled), whose *D block or group 42 is stale (dim_render_stale, "
        "dim_group42_stale), that shows ØØ (dim_double_prefix) or hides its value "
        "(dim_value_hidden); dim_unbound (info) is a dimension not checked",
        None,
    ),
    "gdt": (
        "core",
        "a datum a feature control frame cites with no datum feature",
        "silent (domain_absent) when no feature control frame or datum feature was drawn",
    ),
    "pid_dangling_line": ("pid", "a P&ID line end that reaches no port or line", _PID),
    "pid_duplicate_tag": ("pid", "one instrument or equipment tag used twice", _PID),
    "pid_incompatible_connection": (
        "pid",
        "a line class joined to a port of another kind (process to signal)",
        _PID,
    ),
    "pid_untagged_instrument": ("pid", "an instrument symbol without a tag", _PID),
    "pid_illegal_tag": ("pid", "a tag the ISA-5.1 grammar refuses", _PID),
    "pid_unconnected_equipment": ("pid", "equipment no line reaches", _PID),
    "pid_line_through_symbol": (
        "pid",
        "a P&ID line segment running through the box of a symbol that is not its end",
        _PID,
    ),
    "mech_missing_centreline": ("mech", "a revolved feature without its centre line", _MECH),
    "mech_unhatched_section": ("mech", "a section view with no hatch", _MECH),
    "mech_view_misaligned": ("mech", "a projected view off its projection axis", _MECH),
    "mech_duplicate_dimension": ("mech", "two dimensions of one type in the same place", _MECH),
    "mech_thread_unrepresented": ("mech", "a thread feature with no drawn representation", _MECH),
    "mech_bom_balloon_mismatch": ("mech", "a balloon and the parts list disagree", _MECH),
    "arch_room_unlabelled": ("arch", "a room face with no room label inside it", _ARCH),
    "arch_wall_gap": ("arch", "two wall ends within 50 mm that do not join", _ARCH),
    "arch_opening_clash": (
        "arch",
        "overlapping openings, or an opening past its wall's axis",
        _ARCH,
    ),
    "topo_dangling_endpoint": ("topology", "a network line end touching nothing", _TOPO),
    "topo_near_miss": ("topology", "a network end that stops short of a line", _TOPO),
    "topo_interior_crossing": ("topology", "two network lines crossing with no junction", _TOPO),
}


def critique_focuses() -> dict:
    """What ``autocad://critique/focuses`` serves: every focus in the closed
    enum's order, the not-checked reasons a coverage report can carry, and
    how to ask for them."""
    rows = []
    for name in ALL_CRITIQUE_FOCUSES:
        domain, reports, silent = FOCUS_CATALOG[name]
        row = {"focus": name, "domain": domain, "reports": reports}
        if silent is not None:
            row["silent"] = silent
        rows.append(row)
    return {
        "focuses": rows,
        "not_checked_reasons": dict(NOT_CHECKED_REASONS),
        "strict_exempt_reasons": sorted(GATE_EXEMPT_REASONS),
        "usage": (
            "drawing_critique(focus=[...]) takes any subset (None runs every focus); an unknown "
            "name is reported as an issue, never run. report=True adds the coverage ledger; "
            "summary=True or a limit returns {total, by_focus, by_severity, issues[:limit]}."
        ),
    }
