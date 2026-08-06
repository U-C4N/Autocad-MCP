"""AutoCAD command / natural-language alias corpus for tool discovery.

FastMCP's ``BM25SearchTransform`` builds its index from tool name, tool
description, parameter names and parameter descriptions only (see
``fastmcp/server/transforms/search/base.py::_extract_searchable_text``). Not one
of this server's ~131 tool descriptions mentions an AutoCAD command name, so a
drafter who searches the way drafters actually think -- ``BPOLY``, ``PEDIT``,
``WBLOCK``, ``QSELECT``, ``MATCHPROP`` -- scores df=0 and gets nothing back.
There are no synonyms either: "hole" never reaches ``entity_create_circle`` and
"bill of materials" reaches nothing at all.

That is a *data* gap, not a tuning gap: no serializer and no BM25 parameter can
invent vocabulary that is absent from the index. This module supplies the
missing vocabulary as plain data so a search transform can fold it into the
indexed text.

Field contract
--------------
``acad``
    AutoCAD command name(s) a drafter would actually type for this operation --
    uppercase, no spaces, two characters or more (single-letter aliases like
    ``L`` or ``C`` are omitted as pure noise). **Empty when there is genuinely
    no AutoCAD equivalent.** A wrong alias actively misroutes a search, so
    accuracy beats volume here: engineering meta-tools, the premium planning
    and critique gates, and the transaction wrappers all ship an empty ``acad``
    on purpose.
``synonyms``
    Lowercase natural-language phrases a user would search for -- shop-floor
    vocabulary ("hole", "bore", "round the corner", "parts list", "title
    block"), not restatements of the tool name.

Deliberately kept free of I/O, ``fastmcp``, ``ezdxf`` and config parsing so it
stays trivially importable and testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "SHARED_ACAD_COMMANDS",
    "TOOL_ALIASES",
    "ToolAliases",
    "alias_text",
    "aliases_for",
]


@dataclass(frozen=True, slots=True)
class ToolAliases:
    """Search vocabulary for a single registered tool."""

    acad: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()


# AutoCAD commands that legitimately route to more than one tool.
#
#   ARRAY, LAYER, LAYOUT, MEASUREGEOM, ZOOM  -- option/dialog-driven umbrella
#       commands in AutoCAD itself; this server splits each into discrete tools.
#   SETVAR -- one command both reads and writes a system variable; the server
#       splits read and write.
#   ERASE, INSERT -- the server genuinely exposes one operation through two
#       tools (single vs. batch delete; block insert vs. block-reference
#       creation), so both are correct destinations.
#   QSELECT -- AutoCAD's Quick Select dialog is both "filter by properties"
#       (selection_filter) and "find things like this one"
#       (entity_select_smart); a drafter typing it could mean either.
#
# Every other AutoCAD command must map to exactly one tool.
SHARED_ACAD_COMMANDS: frozenset[str] = frozenset(
    {
        "ARRAY",
        "ERASE",
        "INSERT",
        "LAYER",
        "LAYOUT",
        "MEASUREGEOM",
        "QSELECT",
        "SETVAR",
        "ZOOM",
    }
)


TOOL_ALIASES: dict[str, ToolAliases] = {
    # ── Analysis & query ────────────────────────────────────────────────────
    "analysis_bounding_box": ToolAliases(
        synonyms=(
            "bounding box",
            "drawing extents",
            "overall size",
            "how big is the drawing",
            "outer envelope",
        ),
    ),
    "analysis_entity_stats": ToolAliases(
        synonyms=(
            "entity count",
            "how many objects",
            "drawing statistics",
            "breakdown by type",
            "object inventory",
        ),
    ),
    "analysis_find_in_region": ToolAliases(
        synonyms=(
            "crossing window",
            "what is in this area",
            "entities inside a rectangle",
            "window selection",
            "objects in a region",
        ),
    ),
    "analysis_layer_stats": ToolAliases(
        synonyms=(
            "layer statistics",
            "entities per layer",
            "which layers are used",
            "layer usage report",
        ),
    ),
    # AREA/MEASUREGEOM belong to the ENTITY tool: a drafter typing AREA picks
    # objects on screen, they do not retype coordinates. Routing those commands
    # at the points-based tool sent "what is the area of this polyline" to the
    # one tool that cannot look at the polyline.
    "analysis_measure_area": ToolAliases(
        synonyms=(
            "area of a polygon i will type out",
            "area from coordinates",
            "shoelace",
            "area of a list of points",
        ),
    ),
    "analysis_measure_entity": ToolAliases(
        acad=("AREA", "MEASUREGEOM"),
        synonyms=(
            "area",
            "surface area",
            "how much area",
            "square millimetres",
            "polygon area",
            "area of this entity",
            "area of that polyline",
            "perimeter",
            "circumference",
            "how long is this polyline",
            "measure the part i just drew",
        ),
    ),
    "analysis_measure_distance": ToolAliases(
        acad=("DIST", "DI", "MEASUREGEOM"),
        synonyms=(
            "distance",
            "how far apart",
            "length between two points",
            "measure a gap",
            "spacing",
        ),
    ),
    "analysis_select_by_layer": ToolAliases(
        synonyms=(
            "everything on a layer",
            "entities on a layer",
            "select by layer",
            "layer contents",
        ),
    ),
    "analysis_select_by_type": ToolAliases(
        synonyms=(
            "all the circles",
            "all the lines",
            "entities of one type",
            "select by object type",
            "filter by type",
        ),
    ),
    # ── Batch execution ─────────────────────────────────────────────────────
    #
    # No ``acad``: AutoCAD's nearest equivalent is SCRIPT, which runs a file of
    # command lines and maps to nothing this server exposes. Claiming it would
    # misroute every SCRIPT search into a tool that cannot open a .scr file.
    "cad_batch": ToolAliases(
        synonyms=(
            "run several tools at once",
            "do it all in one call",
            "chain tool calls",
            "sequence of operations",
            "one round trip",
            "macro",
        ),
    ),
    # ── Blocks ──────────────────────────────────────────────────────────────
    "block_create_from_entities": ToolAliases(
        acad=("BLOCK", "BMAKE", "WBLOCK"),
        synonyms=(
            "make a block",
            "define a block",
            "group entities into a symbol",
            "save a selection as a block",
            "reusable symbol",
        ),
    ),
    "block_explode": ToolAliases(
        acad=("EXPLODE",),
        synonyms=(
            "explode",
            "break a block apart",
            "ungroup",
            "split a block into entities",
            "flatten a symbol",
        ),
    ),
    "block_find_references": ToolAliases(
        acad=("BCOUNT",),
        synonyms=(
            "where is this block used",
            "block instances",
            "find the inserts",
            "count block references",
        ),
    ),
    "block_get_attributes": ToolAliases(
        synonyms=(
            "read block attributes",
            "attribute values",
            "title block fields",
            "tag values",
        ),
    ),
    "block_insert": ToolAliases(
        acad=("INSERT",),
        synonyms=(
            "insert a block",
            "place a symbol",
            "stamp a block",
            "add a block instance",
        ),
    ),
    "block_list": ToolAliases(
        synonyms=(
            "list blocks",
            "block definitions",
            "what blocks exist",
            "available symbols",
        ),
    ),
    "block_set_attributes": ToolAliases(
        acad=("EATTEDIT", "ATTEDIT"),
        synonyms=(
            "edit block attributes",
            "fill in the title block",
            "set attribute values",
            "update the tags",
        ),
    ),
    # ── Construction geometry ───────────────────────────────────────────────
    "construction_clear": ToolAliases(
        synonyms=(
            "delete the construction lines",
            "remove the scaffolding",
            "clean the construction layer",
            "wipe the guides",
        ),
    ),
    "construction_xline": ToolAliases(
        acad=("XLINE", "XL"),
        synonyms=(
            "construction line",
            "infinite line",
            "guide line",
            "reference axis",
            "scaffolding line",
        ),
    ),
    # ── GD&T ────────────────────────────────────────────────────────────────
    "datum_feature": ToolAliases(
        synonyms=(
            "datum",
            "datum symbol",
            "datum triangle",
            "datum reference",
            "reference feature",
        ),
    ),
    "gd_frame": ToolAliases(
        acad=("TOLERANCE",),
        synonyms=(
            "feature control frame",
            "geometric tolerance",
            "gd&t",
            "flatness",
            "perpendicularity",
            "true position",
        ),
    ),
    # ── Dimensions ──────────────────────────────────────────────────────────
    "dimension_aligned": ToolAliases(
        acad=("DIMALIGNED", "DAL"),
        synonyms=(
            "aligned dimension",
            "true length dimension",
            "dimension along a slope",
            "slanted dimension",
        ),
    ),
    "dimension_angular": ToolAliases(
        acad=("DIMANGULAR", "DAN"),
        synonyms=(
            "angle dimension",
            "angular dimension",
            "measure an angle",
            "included angle",
        ),
    ),
    "dimension_auto": ToolAliases(
        acad=("QDIM", "DIMCONTINUE", "DIMBASELINE", "DIMORDINATE"),
        synonyms=(
            "dimension everything",
            "chain dimensions",
            "baseline dimensions",
            "ordinate dimensions",
            "quick dimension",
        ),
    ),
    "dimension_diameter": ToolAliases(
        acad=("DIMDIAMETER", "DDI"),
        synonyms=(
            "diameter dimension",
            "hole size",
            "bore diameter",
            "dimension a hole",
        ),
    ),
    "dimension_linear": ToolAliases(
        acad=("DIMLINEAR", "DLI"),
        synonyms=(
            "dimension",
            "linear dimension",
            "horizontal dimension",
            "vertical dimension",
            "size tolerance",
            "h7 fit",
        ),
    ),
    "dimension_radius": ToolAliases(
        acad=("DIMRADIUS", "DRA"),
        synonyms=(
            "radius dimension",
            "corner radius callout",
            "dimension an arc",
            "fillet radius",
        ),
    ),
    # ── Drawing management ──────────────────────────────────────────────────
    "drawing_apply_iso_layers": ToolAliases(
        synonyms=(
            "iso layers",
            "standard layer set",
            "set up drafting layers",
            "layer standard",
            "bootstrap the layers",
        ),
    ),
    "drawing_audit": ToolAliases(
        acad=("AUDIT",),
        synonyms=(
            "audit",
            "fix drawing errors",
            "integrity check",
            "repair the file",
            "corrupt drawing",
        ),
    ),
    "drawing_close": ToolAliases(
        acad=("CLOSE",),
        synonyms=("close the drawing", "shut the file", "finish with this drawing"),
    ),
    "drawing_critique": ToolAliases(
        synonyms=(
            "review the drawing",
            "quality review",
            "what is wrong with this drawing",
            "drafting mistakes",
            "check before finalising",
        ),
    ),
    "drawing_deliver": ToolAliases(
        synonyms=(
            "delivery bundle",
            "package the drawing",
            "hand off the drawing",
            "final deliverable",
        ),
    ),
    "drawing_export_dxf": ToolAliases(
        acad=("DXFOUT",),
        synonyms=("export dxf", "save as dxf", "interchange file", "dxf output"),
    ),
    "drawing_export_pdf": ToolAliases(
        acad=("PLOT", "PRINT", "EXPORTPDF"),
        synonyms=("print", "plot", "export pdf", "pdf output", "paper copy"),
    ),
    "drawing_finalize": ToolAliases(
        synonyms=(
            "finish the drawing",
            "validate and save",
            "final check",
            "completion gate",
            "the drawing is done",
        ),
    ),
    "drawing_info": ToolAliases(
        acad=("DWGPROPS", "STATUS"),
        synonyms=(
            "drawing info",
            "file properties",
            "drawing metadata",
            "what is in this drawing",
        ),
    ),
    "drawing_new": ToolAliases(
        acad=("NEW", "QNEW"),
        synonyms=("new drawing", "start a drawing", "blank sheet", "create a file"),
    ),
    "drawing_open": ToolAliases(
        acad=("OPEN",),
        synonyms=("open a drawing", "load a dwg", "read a dxf file", "open a file"),
    ),
    "drawing_plan": ToolAliases(
        synonyms=(
            "plan the drawing",
            "drawing plan",
            "decide what to draw",
            "drawing intent",
        ),
    ),
    "drawing_preflight": ToolAliases(
        synonyms=(
            "preflight",
            "check the requirements",
            "validate the brief",
            "normalise the requirements",
        ),
    ),
    "drawing_purge": ToolAliases(
        acad=("PURGE", "PU"),
        synonyms=(
            "purge",
            "remove unused layers",
            "clean out unused blocks",
            "slim the file",
        ),
    ),
    "drawing_redo": ToolAliases(
        acad=("REDO", "MREDO"),
        synonyms=("redo", "reapply the change", "undo the undo"),
    ),
    "drawing_refine": ToolAliases(
        acad=("OVERKILL",),
        synonyms=(
            "fix the issues",
            "auto repair",
            "clean up the drawing",
            "remove duplicate entities",
            "repair loop",
        ),
    ),
    "drawing_save": ToolAliases(
        acad=("QSAVE", "SAVE"),
        synonyms=("save", "save the drawing", "write the file", "keep my changes"),
    ),
    "drawing_save_as": ToolAliases(
        acad=("SAVEAS",),
        synonyms=(
            "save as",
            "save a copy",
            "write to a new file",
            "save as a template",
        ),
    ),
    "drawing_settings": ToolAliases(
        acad=("UNITS", "DDUNITS", "OSNAP", "DSETTINGS", "LTSCALE", "DIMSCALE"),
        synonyms=(
            "drawing units",
            "millimetres or inches",
            "decimal precision",
            "dimension scale",
            "linetype scale",
            "object snap settings",
        ),
    ),
    "drawing_undo": ToolAliases(
        acad=("UNDO",),
        synonyms=("undo", "take that back", "revert the last change", "step back"),
    ),
    # ── Entity creation ─────────────────────────────────────────────────────
    "entity_create_arc": ToolAliases(
        acad=("ARC",),
        synonyms=("arc", "curved segment", "part of a circle", "bend", "sweep"),
    ),
    "entity_create_block_ref": ToolAliases(
        acad=("INSERT",),
        synonyms=(
            "block reference",
            "place a block",
            "insert a symbol instance",
            "drop in a symbol",
        ),
    ),
    "entity_create_circle": ToolAliases(
        acad=("CIRCLE",),
        synonyms=("circle", "hole", "bore", "round hole", "pitch circle", "disc"),
    ),
    "entity_create_ellipse": ToolAliases(
        acad=("ELLIPSE", "EL"),
        synonyms=("ellipse", "oval", "elliptical shape", "squashed circle"),
    ),
    "entity_create_hatch": ToolAliases(
        acad=("HATCH", "BHATCH"),
        synonyms=(
            "hatch",
            "fill a region",
            "section hatching",
            "crosshatch",
            "shade an area",
            "ansi31",
        ),
    ),
    "entity_create_line": ToolAliases(
        acad=("LINE",),
        synonyms=("line", "straight segment", "edge", "connect two points"),
    ),
    "entity_create_mtext": ToolAliases(
        acad=("MTEXT",),
        synonyms=(
            "paragraph text",
            "multiline text",
            "note block",
            "wrapped text",
            "general notes",
        ),
    ),
    "entity_create_point": ToolAliases(
        acad=("POINT", "PO"),
        synonyms=("point", "node", "marker", "dot", "reference point"),
    ),
    "entity_create_polyline": ToolAliases(
        # BOUNDARY/BPOLY moved to boundary_trace in v1.5.0 — they were parked
        # here only because nothing traced a boundary yet.
        acad=("PLINE", "PL"),
        synonyms=(
            "polyline",
            "connected segments",
            "outline",
            "closed profile",
            "boundary",
            "contour",
        ),
    ),
    "entity_create_rectangle": ToolAliases(
        acad=("RECTANG", "REC"),
        synonyms=("rectangle", "box", "square", "plate outline", "four sided shape"),
    ),
    "entity_create_spline": ToolAliases(
        acad=("SPLINE", "SPL"),
        synonyms=("spline", "smooth curve", "freeform curve", "nurbs", "fit points"),
    ),
    "entity_create_table": ToolAliases(
        acad=("TABLE", "TB"),
        synonyms=(
            "table",
            "parts list",
            "bill of materials",
            "bom",
            "schedule",
            "revision table",
        ),
    ),
    "entity_create_text": ToolAliases(
        acad=("TEXT", "DTEXT"),
        synonyms=("text", "label", "single line text", "caption", "annotate"),
    ),
    "entity_batch_create": ToolAliases(
        synonyms=(
            "create many entities",
            "bulk create",
            "draw several things at once",
            "batch draw",
        ),
    ),
    "entity_batch_modify": ToolAliases(
        synonyms=(
            "bulk edit",
            "modify many entities",
            "batch changes",
            "apply edits at once",
        ),
    ),
    # ── Entity modification ─────────────────────────────────────────────────
    "entity_array_polar": ToolAliases(
        acad=("ARRAYPOLAR", "ARRAY"),
        synonyms=(
            "polar array",
            "circular pattern",
            "bolt circle",
            "repeat around a centre",
            "radial pattern",
        ),
    ),
    "entity_array_rectangular": ToolAliases(
        acad=("ARRAYRECT", "ARRAY"),
        synonyms=(
            "rectangular array",
            "grid of copies",
            "rows and columns",
            "repeat in a grid",
        ),
    ),
    "entity_chamfer": ToolAliases(
        acad=("CHAMFER", "CHA"),
        synonyms=(
            "chamfer",
            "bevel a corner",
            "break the edge",
            "45 degree corner",
            "cut the corner off",
        ),
    ),
    "entity_copy": ToolAliases(
        acad=("COPY", "CO", "CP"),
        synonyms=("copy", "duplicate", "clone an entity", "make another one"),
    ),
    "entity_delete": ToolAliases(
        acad=("ERASE",),
        synonyms=("delete", "erase", "remove an entity", "get rid of it"),
    ),
    "entity_delete_many": ToolAliases(
        acad=("ERASE",),
        synonyms=(
            "delete several",
            "erase many",
            "bulk delete",
            "remove a list of entities",
        ),
    ),
    "entity_edit_geometry": ToolAliases(
        acad=("LENGTHEN", "PEDIT"),
        synonyms=(
            "change the radius",
            "move an endpoint",
            "resize a circle",
            "edit geometry in place",
            "adjust the arc angles",
        ),
    ),
    "entity_edit_text": ToolAliases(
        acad=("DDEDIT", "TEXTEDIT"),
        synonyms=(
            "change the text",
            "rename a label",
            "edit the wording",
            "fix a typo",
            "retitle",
        ),
    ),
    "entity_extend": ToolAliases(
        acad=("EXTEND", "EX"),
        synonyms=(
            "extend",
            "lengthen to meet",
            "stretch to the boundary",
            "reach the other line",
        ),
    ),
    "entity_fillet": ToolAliases(
        acad=("FILLET",),
        synonyms=(
            "fillet",
            "round the corner",
            "rounded corner",
            "radius the corner",
            "tangent arc corner",
        ),
    ),
    "entity_mirror": ToolAliases(
        acad=("MIRROR", "MI"),
        synonyms=("mirror", "flip", "reflect", "symmetry", "mirrored copy"),
    ),
    "entity_move": ToolAliases(
        acad=("MOVE",),
        synonyms=("move", "shift", "translate", "relocate", "nudge"),
    ),
    "entity_offset": ToolAliases(
        acad=("OFFSET",),
        synonyms=(
            "offset",
            "parallel copy",
            "wall thickness",
            "concentric copy",
            "inset",
        ),
    ),
    "entity_rotate": ToolAliases(
        acad=("ROTATE", "RO"),
        synonyms=("rotate", "turn", "spin", "reorient", "set the angle"),
    ),
    "entity_scale": ToolAliases(
        acad=("SCALE", "SC"),
        synonyms=("scale", "resize", "enlarge", "shrink", "make it bigger"),
    ),
    "entity_set_properties": ToolAliases(
        acad=("PROPERTIES", "CHPROP", "CHANGE", "MATCHPROP", "PR"),
        synonyms=(
            "change the colour",
            "move to another layer",
            "set the linetype",
            "set the lineweight",
            "match properties",
            "hide an entity",
        ),
    ),
    "entity_trim": ToolAliases(
        acad=("TRIM", "TR"),
        synonyms=(
            "trim",
            "cut back",
            "clean up the overshoot",
            "cut to the intersection",
            "remove the excess",
        ),
    ),
    # ── Entity query & selection ────────────────────────────────────────────
    "entity_get": ToolAliases(
        acad=("LI",),  # LIST moved to analysis_list_properties, which is the real dump
        synonyms=(
            "entity properties",
            "what is this object",
            "inspect an entity",
            "details for a handle",
        ),
    ),
    "entity_list": ToolAliases(
        synonyms=(
            "list the entities",
            "what is in the drawing",
            "browse the objects",
            "all the handles",
        ),
    ),
    "entity_select_smart": ToolAliases(
        acad=("QSELECT", "FILTER", "SELECTSIMILAR"),
        synonyms=(
            "select by property",
            "find every matching entity",
            "smart selection",
            "quick select",
            "pick everything that is",
        ),
    ),
    "selection_get": ToolAliases(
        synonyms=(
            "current selection",
            "what the user selected",
            "picked entities",
            "highlighted objects",
            "pickfirst",
        ),
    ),
    # ── Engineering primitives ──────────────────────────────────────────────
    "gear_draw_helical_front_view": ToolAliases(
        synonyms=(
            "helical gear",
            "gear front view",
            "involute teeth",
            "toothed wheel",
            "helix gear",
        ),
    ),
    "gear_draw_section_aa": ToolAliases(
        synonyms=(
            "gear section",
            "section a-a",
            "cross section of a gear",
            "cut view of a gear",
        ),
    ),
    "gear_draw_spur_front_view": ToolAliases(
        synonyms=(
            "spur gear",
            "straight tooth gear",
            "involute gear",
            "gear front view",
        ),
    ),
    "keyway_draw_keyed_bore": ToolAliases(
        synonyms=(
            "keyway",
            "keyed bore",
            "key slot",
            "shaft keyway",
            "din 6885 key",
            "hub bore",
        ),
    ),
    "keyway_draw_section": ToolAliases(
        synonyms=(
            "keyway section",
            "keyed bore side view",
            "key slot cross section",
        ),
    ),
    "titleblock_apply_iso_a3": ToolAliases(
        synonyms=(
            "title block",
            "drawing frame",
            "sheet border",
            "iso 7200",
            "a3 sheet",
            "drawing header",
        ),
    ),
    # ── Layers & linetypes ──────────────────────────────────────────────────
    "layer_create": ToolAliases(
        acad=("LAYER", "LA"),
        synonyms=("new layer", "add a layer", "create a layer", "layer with a colour"),
    ),
    "layer_delete": ToolAliases(
        acad=("LAYDEL",),
        synonyms=("delete a layer", "remove a layer", "get rid of a layer"),
    ),
    "layer_freeze": ToolAliases(
        acad=("LAYFRZ",),
        synonyms=("freeze a layer", "freeze", "stop regenerating a layer"),
    ),
    "layer_hide": ToolAliases(
        acad=("LAYOFF",),
        synonyms=("turn a layer off", "hide a layer", "make a layer invisible"),
    ),
    "layer_isolate": ToolAliases(
        acad=("LAYISO",),
        synonyms=(
            "isolate a layer",
            "show only one layer",
            "hide everything else",
            "layer isolation",
        ),
    ),
    "layer_list": ToolAliases(
        acad=("LAYER",),
        synonyms=("list the layers", "what layers exist", "layer table", "layer overview"),
    ),
    "layer_lock": ToolAliases(
        acad=("LAYLCK",),
        synonyms=("lock a layer", "protect a layer", "make a layer read only"),
    ),
    "layer_modify": ToolAliases(
        acad=("LAYER",),
        synonyms=(
            "change a layer colour",
            "edit a layer",
            "set the layer linetype",
            "set the layer lineweight",
        ),
    ),
    "layer_set_current": ToolAliases(
        acad=("CLAYER", "LAYMCUR"),
        synonyms=(
            "current layer",
            "draw on this layer",
            "set the active layer",
            "switch layer",
        ),
    ),
    "layer_show": ToolAliases(
        acad=("LAYON",),
        synonyms=("turn a layer on", "show a layer", "unhide a layer"),
    ),
    "layer_thaw": ToolAliases(
        acad=("LAYTHW",),
        synonyms=("thaw a layer", "unfreeze a layer", "bring a layer back"),
    ),
    "layer_unlock": ToolAliases(
        acad=("LAYULK",),
        synonyms=("unlock a layer", "allow editing on a layer"),
    ),
    "linetype_list": ToolAliases(
        synonyms=(
            "list the linetypes",
            "which linetypes are loaded",
            "available dash patterns",
        ),
    ),
    "linetype_load": ToolAliases(
        acad=("LINETYPE", "LTYPE", "LT"),
        synonyms=(
            "load a linetype",
            "dashed line",
            "centre line linetype",
            "hidden linetype",
            "phantom linetype",
        ),
    ),
    "template_apply_layers": ToolAliases(
        acad=("LAYTRANS",),
        synonyms=(
            "apply a layer template",
            "standard layers",
            "architectural layers",
            "mechanical layers",
            "translate the layers",
        ),
    ),
    "template_list": ToolAliases(
        synonyms=(
            "available templates",
            "what layer templates exist",
            "list the templates",
        ),
    ),
    # ── Layouts & paper space ───────────────────────────────────────────────
    "layout_create": ToolAliases(
        acad=("LAYOUT",),
        synonyms=("new layout", "paper space tab", "create a sheet", "add a layout"),
    ),
    "layout_list": ToolAliases(
        synonyms=(
            "list the layouts",
            "what sheets exist",
            "layout tabs",
            "model and paper space",
        ),
    ),
    "layout_set_current": ToolAliases(
        acad=("LAYOUT", "PSPACE"),
        synonyms=(
            "switch to a layout",
            "activate a sheet",
            "go to paper space",
            "open a layout tab",
        ),
    ),
    "selection_window": ToolAliases(
        acad=("SSGET",),
        synonyms=(
            "select in a box",
            "window selection",
            "crossing selection",
            "pick everything in this rectangle",
        ),
    ),
    "selection_polygon": ToolAliases(
        acad=("WPOLYGON",),
        synonyms=(
            "select inside a polygon",
            "lasso selection",
            "pick everything in this shape",
        ),
    ),
    "selection_filter": ToolAliases(
        acad=("QSELECT",),
        synonyms=(
            "select by properties",
            "find all the red circles",
            "everything on this layer",
            "quick select",
        ),
    ),
    "boundary_trace": ToolAliases(
        acad=("BOUNDARY", "BPOLY"),
        synonyms=(
            "trace the boundary",
            "outline this area",
            "closed polyline around this point",
            "pick an internal point",
        ),
    ),
    "boundary_from_entities": ToolAliases(
        synonyms=(
            "join these into a closed shape",
            "make a loop from these lines",
            "chain entities into a boundary",
        ),
    ),
    "analysis_list_properties": ToolAliases(
        # LIST is the property *dump*; entity_get keeps LI, and PROPERTIES
        # stays with the tool that writes them.
        acad=("LIST",),
        synonyms=(
            "list the properties",
            "show me everything about this entity",
            "dump the dxf attributes",
            "what are this object's properties",
        ),
    ),
    "hatch_set_gradient": ToolAliases(
        acad=("GRADIENT",),
        synonyms=(
            "gradient fill",
            "fade from one colour to another",
            "colour ramp fill",
        ),
    ),
    "hatch_edit": ToolAliases(
        acad=("HATCHEDIT",),
        synonyms=(
            "change the hatch pattern",
            "rescale a hatch",
            "island detection style",
            "edit a hatch",
        ),
    ),
    "hatch_add_boundary": ToolAliases(
        synonyms=(
            "add a boundary to a hatch",
            "hatch boundary with arcs",
            "another island in the hatch",
        ),
    ),
    "entity_create_wipeout": ToolAliases(
        acad=("WIPEOUT",),
        synonyms=(
            "wipeout",
            "mask what is behind",
            "hide the drawing underneath",
            "white out an area",
        ),
    ),
    "entity_create_revcloud": ToolAliases(
        acad=("REVCLOUD",),
        synonyms=(
            "revision cloud",
            "cloud this area",
            "mark a revision",
            "circle the change",
        ),
    ),
    "text_set_background": ToolAliases(
        acad=("BACKGROUNDMASK",),
        synonyms=(
            "text background mask",
            "make the text readable over hatch",
            "opaque box behind text",
            "background fill for mtext",
        ),
    ),
    "text_find_replace": ToolAliases(
        acad=("FIND",),
        synonyms=(
            "find and replace text",
            "replace text everywhere",
            "search and replace",
            "change all the labels",
        ),
    ),
    "layout_delete": ToolAliases(
        acad=("LAYOUT",),
        synonyms=(
            "delete a layout",
            "remove a sheet tab",
            "get rid of a layout",
            "drop a paper space tab",
        ),
    ),
    "layout_rename": ToolAliases(
        acad=("LAYOUT", "RENAME"),
        synonyms=(
            "rename a layout",
            "rename a sheet tab",
            "change the layout name",
            "retitle a sheet",
        ),
    ),
    "layout_copy": ToolAliases(
        acad=("LAYOUT",),
        synonyms=(
            "copy a layout",
            "duplicate a sheet",
            "clone a layout tab",
            "another sheet like this one",
        ),
    ),
    "viewport_create": ToolAliases(
        acad=("MVIEW", "MV", "VPORTS"),
        synonyms=(
            "viewport",
            "scaled window on a sheet",
            "model view on paper",
            "paper space window",
        ),
    ),
    # No AutoCAD command lists viewports: the drafter reads them off the sheet
    # or the Properties palette. Synonyms only, like layout_list.
    "viewport_list": ToolAliases(
        synonyms=(
            "list the viewports",
            "what viewports are on this sheet",
            "viewport scales",
            "show the windows on the layout",
        ),
    ),
    "viewport_set_scale": ToolAliases(
        # The classic way to scale a viewport is ZOOM nXP from inside it.
        acad=("ZOOM",),
        synonyms=(
            "set the viewport scale",
            "make this viewport 1:50",
            "rescale a viewport",
            "zoom xp",
        ),
    ),
    "viewport_lock": ToolAliases(
        synonyms=(
            "lock a viewport",
            "unlock a viewport",
            "stop the viewport scale changing",
            "display locked",
        ),
    ),
    "viewport_delete": ToolAliases(
        synonyms=(
            "delete a viewport",
            "remove a viewport",
            "get rid of the window on the sheet",
        ),
    ),
    "entity_change_space": ToolAliases(
        acad=("CHSPACE",),
        synonyms=(
            "change space",
            "move to paper space",
            "move to model space",
            "move this onto the sheet",
        ),
    ),
    # ── Leaders ─────────────────────────────────────────────────────────────
    "leader_create_mleader": ToolAliases(
        acad=("MLEADER", "MLD", "QLEADER", "LEADER"),
        synonyms=(
            "leader",
            "callout",
            "arrow with a note",
            "pointer note",
            "balloon",
        ),
    ),
    # ── Snap points ─────────────────────────────────────────────────────────
    "point_from_snap": ToolAliases(
        synonyms=(
            "endpoint",
            "midpoint",
            "centre point",
            "quadrant",
            "perpendicular foot",
            "object snap",
            "osnap",
            "exact coordinate",
        ),
    ),
    "point_intersection": ToolAliases(
        synonyms=(
            "intersection",
            "where two lines cross",
            "crossing point",
            "meeting point",
        ),
    ),
    "point_tangent": ToolAliases(
        synonyms=(
            "tangent point",
            "tangency",
            "touch point on a circle",
            "tangent from a point",
        ),
    ),
    # ── 3D solids ───────────────────────────────────────────────────────────
    "solid_boolean": ToolAliases(
        acad=("UNION", "SUBTRACT", "INTERSECT"),
        synonyms=(
            "boolean",
            "union of solids",
            "subtract a solid",
            "cut a pocket",
            "combine solids",
        ),
    ),
    "solid_box": ToolAliases(
        acad=("BOX",),
        synonyms=("3d box", "cuboid", "solid block", "rectangular solid"),
    ),
    "solid_cylinder": ToolAliases(
        acad=("CYLINDER",),
        synonyms=("3d cylinder", "solid shaft", "round solid", "pin"),
    ),
    "solid_extrude": ToolAliases(
        acad=("EXTRUDE",),
        synonyms=(
            "extrude",
            "pull a profile into 3d",
            "give it thickness",
            "solid from a profile",
        ),
    ),
    "solid_revolve": ToolAliases(
        acad=("REVOLVE",),
        synonyms=(
            "revolve",
            "turn a profile around an axis",
            "lathe",
            "solid of revolution",
        ),
    ),
    # ── System ──────────────────────────────────────────────────────────────
    "system_about": ToolAliases(
        acad=("ABOUT",),
        synonyms=(
            "about",
            "what can this server do",
            "capabilities overview",
            "version information",
        ),
    ),
    "system_capabilities": ToolAliases(
        synonyms=(
            "what is supported",
            "backend capabilities",
            "feature support",
            "can it do this",
        ),
    ),
    "system_get_variable": ToolAliases(
        acad=("SETVAR",),
        synonyms=(
            "read a system variable",
            "get a sysvar",
            "current variable value",
        ),
    ),
    "system_run_command": ToolAliases(
        synonyms=(
            "run an autocad command",
            "command line",
            "send a command string",
            "macro",
            "raw command",
        ),
    ),
    "system_run_lisp": ToolAliases(
        synonyms=("autolisp", "lisp", "run a lisp expression", "vlisp", "script it"),
    ),
    "system_set_variable": ToolAliases(
        acad=("SETVAR",),
        synonyms=(
            "set a system variable",
            "change a sysvar",
            "configure an autocad variable",
        ),
    ),
    "system_status": ToolAliases(
        synonyms=(
            "server status",
            "is autocad connected",
            "backend status",
            "health check",
        ),
    ),
    # ── Transactions ────────────────────────────────────────────────────────
    "transaction_begin": ToolAliases(
        synonyms=(
            "start a transaction",
            "undo mark",
            "checkpoint",
            "savepoint",
            "begin a safe edit",
        ),
    ),
    "transaction_commit": ToolAliases(
        synonyms=("commit", "keep the changes", "end the transaction", "accept the edits"),
    ),
    "transaction_rollback": ToolAliases(
        synonyms=(
            "rollback",
            "discard the changes",
            "revert the transaction",
            "undo back to the checkpoint",
        ),
    ),
    # ── Validation ──────────────────────────────────────────────────────────
    "validation_check": ToolAliases(
        synonyms=(
            "quality check",
            "find duplicates",
            "empty layers",
            "zero length lines",
            "sanity check the drawing",
        ),
    ),
    # ── View & screenshot ───────────────────────────────────────────────────
    "view_screenshot": ToolAliases(
        synonyms=(
            "screenshot",
            "picture of the drawing",
            "render an image",
            "preview",
            "what does it look like",
        ),
    ),
    "view_zoom_and_screenshot": ToolAliases(
        acad=("ZOOM",),
        synonyms=(
            "show me the drawing",
            "zoom then capture",
            "fit and screenshot",
            "visual check",
        ),
    ),
    "view_zoom_extents": ToolAliases(
        acad=("ZOOM",),
        synonyms=("zoom to fit", "fit the drawing on screen", "see everything"),
    ),
    "view_zoom_window": ToolAliases(
        acad=("ZOOM",),
        synonyms=("zoom into a region", "close up of an area", "magnify a rectangle"),
    ),
}


def aliases_for(tool_name: str) -> ToolAliases | None:
    """Return the alias record for ``tool_name``, or ``None`` if it has none."""
    return TOOL_ALIASES.get(tool_name)


def alias_text(tool_name: str) -> str:
    """Return the alias vocabulary for ``tool_name`` as one whitespace-joined string.

    Suitable for appending to the text a search index builds from a tool. Returns
    an empty string for unknown tools so callers can concatenate unconditionally.
    """
    record = TOOL_ALIASES.get(tool_name)
    if record is None:
        return ""
    return " ".join((*record.acad, *record.synonyms))
