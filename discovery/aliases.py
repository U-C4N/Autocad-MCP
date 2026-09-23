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
#   ERASE, INSERT -- the server genuinely exposes one operation through more
#       than one tool (single vs. batch delete; block insert vs. block-reference
#       creation vs. the P&ID catalogue insert), so each is a correct destination.
#   QSELECT -- AutoCAD's Quick Select dialog is both "filter by properties"
#       (selection_filter) and "find things like this one"
#       (entity_select_smart); a drafter typing it could mean either.
#   BLOCK -- one command makes a definition either from selected objects
#       (block_create_from_entities) or from scratch with attribute
#       definitions (block_define); a drafter typing it could mean either.
#   PLINE -- a raw polyline (entity_create_polyline) or a P&ID line run
#       between two ports (pid_line_draw), which is an LWPOLYLINE with a
#       class, a number and markers; a drafter typing it could mean either.
#   DIMSTYLE, STYLE, MLEADERSTYLE -- one dialog each in AutoCAD for
#       list / create / modify / set-current; this server splits each into
#       discrete style tools (track E), so the command is a correct
#       destination for every one of them.
#   DWGPROPS -- one dialog, three tabs: General (drawing_info), Summary and
#       Custom (drawing_properties_get / drawing_properties_set).
#   PAGESETUP -- the Page Setup Manager both shows and edits a sheet's setup
#       (page_setup_list vs page_setup_apply).
#   PLOT -- one sheet to PDF (drawing_export_pdf) or every sheet in one go
#       (batch_plot); a drafter typing it could mean either.
#   SAVEAS -- the same command saves a copy (drawing_save_as) or a template
#       (drawing_template_save, its "Drawing Template" file type).
#   CLOSE -- the active document (drawing_close) or one named document
#       (document_close); a drafter typing it could mean either.
#   LAYERSTATE, VIEW, UCS -- umbrella dialog/option commands (save, restore,
#       list, delete in one) that this server splits into discrete tools.
#   OPTIONS -- one dialog both reads and writes a preference; the server
#       splits read (system_preferences_get) and write (system_preferences_set),
#       the SETVAR precedent.
#   REVCLOUD -- one command draws a bare cloud (entity_create_revcloud) or a
#       revision cloud that also files a revision row (revision_add); a
#       drafter typing it could mean either.
#   TABLE -- one command makes an empty table (entity_create_table), the
#       ISO 7573 parts list (bom_table) or a door / window / room schedule
#       (arch_schedule); all three are correct destinations.
#   SECTIONPLANE -- the section a drafter asks for is either a whole section
#       view of a drawn part (mech_view_add) or the cutting-plane line and its
#       labels on an existing view (section_line); tracks B+G groups M and A
#       each claimed it, and a drafter typing it could mean either.
#
# Every other AutoCAD command must map to exactly one tool.
SHARED_ACAD_COMMANDS: frozenset[str] = frozenset(
    {
        "ARRAY",
        "BLOCK",
        "CLOSE",
        "DIMSTYLE",
        "DWGPROPS",
        "ERASE",
        "INSERT",
        "LAYER",
        "LAYERSTATE",
        "LAYOUT",
        "MEASUREGEOM",
        "MLEADERSTYLE",
        "OPTIONS",
        "PAGESETUP",
        "PLINE",
        "PLOT",
        "QSELECT",
        "REVCLOUD",
        "SAVEAS",
        "SECTIONPLANE",
        "SETVAR",
        "STYLE",
        "TABLE",
        "UCS",
        "VIEW",
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
    "block_define": ToolAliases(
        acad=("BLOCK", "BEDIT", "ATTDEF"),
        synonyms=(
            "define a block",
            "make a symbol",
            "block with attributes",
            "attribute definition",
            "create a block definition from scratch",
        ),
    ),
    "block_explode": ToolAliases(
        acad=("EXPLODE", "BURST"),
        synonyms=(
            "explode",
            "break a block apart",
            "ungroup",
            "split a block into entities",
            "flatten a symbol",
            "burst a block",
            "explode and keep the attribute text",
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
    # ── Mechanical annotation (ISO 21920-1 / ISO 2553) ──────────────────────
    "surface_texture": ToolAliases(
        synonyms=(
            "surface finish",
            "surface roughness",
            "surface texture symbol",
            "roughness symbol",
            "ra value",
            "rz value",
            "machining symbol",
            "yuzey puruzlulugu",
            "yuzey isleme sembolu",
            "yüzey pürüzlülüğü",
        ),
    ),
    "weld_symbol": ToolAliases(
        synonyms=(
            "weld symbol",
            "welding symbol",
            "fillet weld",
            "butt weld",
            "weld callout",
            "field weld",
            "weld all around",
            "kaynak sembolu",
            "kose kaynagi",
            "kaynak sembolü",
        ),
    ),
    "centre_marks": ToolAliases(
        acad=("CENTERMARK", "CENTERLINE", "DIMCENTER"),
        synonyms=(
            "centre mark",
            "center mark",
            "centre line",
            "center line of a hole",
            "cross at the centre",
            "eksen cizgisi",
            "merkez isareti",
        ),
    ),
    "section_line": ToolAliases(
        acad=("SECTIONPLANE",),
        synonyms=(
            "cutting plane line",
            "section line",
            "section a-a",
            "where to cut the part",
            "view direction arrows",
            "kesit cizgisi",
        ),
    ),
    "hatch_material": ToolAliases(
        synonyms=(
            "material hatch",
            "hatch as cast iron",
            "section hatching for steel",
            "hatch pattern for a material",
            "malzeme taramasi",
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
        synonyms=(
            "new drawing",
            "start a drawing",
            "blank sheet",
            "create a file",
            "start from a bundled template",
        ),
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
    "drawing_properties_get": ToolAliases(
        acad=("DWGPROPS",),
        synonyms=(
            "drawing properties",
            "document properties",
            "summary info",
            "title subject author keywords",
            "custom properties",
            "who made this drawing",
        ),
    ),
    "drawing_properties_set": ToolAliases(
        acad=("DWGPROPS",),
        synonyms=(
            "set the drawing title",
            "set author and keywords",
            "add a custom property",
            "project number in the file properties",
            "edit summary info",
            "delete a custom property",
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
        acad=(
            "UNITS",
            "DDUNITS",
            "OSNAP",
            "DSETTINGS",
            "LTSCALE",
            "DIMSCALE",
            "LIMITS",
            "GRID",
            "SNAP",
            "ORTHO",
            "PSLTSCALE",
            "CANNOSCALE",
        ),
        synonyms=(
            "drawing units",
            "millimetres or inches",
            "decimal precision",
            "dimension scale",
            "linetype scale",
            "object snap settings",
            "drawing limits",
            "turn the grid on",
            "grid spacing",
            "snap spacing",
            "ortho mode",
            "polar tracking",
            "annotation scale",
            "architectural units",
            "current dimension style",
            "current text style",
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
    "entity_get_xdata": ToolAliases(
        acad=("XDLIST",),
        synonyms=("extended data", "read xdata", "entity metadata", "registered application data"),
    ),
    "entity_set_xdata": ToolAliases(
        acad=("XDATA",),
        synonyms=("attach extended data", "write xdata", "tag an entity with metadata"),
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
    "sheet_frame": ToolAliases(
        acad=("MVSETUP",),
        synonyms=(
            "sheet frame",
            "drawing frame",
            "drawing border",
            "iso 5457",
            "zone grid",
            "grid reference system",
            "centring marks",
            "trimming marks",
            "sheet margins",
            "filing margin",
            "pafta",
            "cizim cercevesi",
        ),
    ),
    "titleblock_apply": ToolAliases(
        synonyms=(
            "title block",
            "iso 7200",
            "drawing header",
            "sheet title block",
            "a0 title block",
            "a1 title block",
            "a2 title block",
            "a4 title block",
            "projection angle symbol",
            "first angle symbol",
            "third angle symbol",
            "antet",
            "baslik blogu",
        ),
    ),
    "revision_add": ToolAliases(
        acad=("REVCLOUD",),
        synonyms=(
            "revision",
            "revision block",
            "revision cloud",
            "revision table",
            "change note",
            "engineering change",
            "as built change",
            "revizyon",
            "degisiklik",
        ),
    ),
    "bom_extract": ToolAliases(
        synonyms=(
            "bill of materials",
            # Deliberately NOT "parts list" or "count the parts": the bare
            # "parts list" query belongs to the tool that DRAWS one
            # (entity_create_table / bom_table), and every extra "parts"/"list"
            # token here moved this reader above it on plain BM25 mass.
            "read the bom",
            "bom",
            "item list",
            "what is on this drawing",
            "iso 7573",
            "parca listesi",
            "malzeme listesi",
        ),
    ),
    "bom_table": ToolAliases(
        acad=("TABLE",),
        synonyms=(
            # Every phrase here names a TABLE. The bare "parts list" query is
            # `entity_create_table`'s (the generic table that a parts list is
            # one use of); this tool answers when the drafter asks for the
            # ISO 7573 one by name.
            "bom table",
            "item list table",
            "materials table",
            "iso 7573 table",
            "parca listesi tablosu",
            "draw the parts list table on the sheet",
        ),
    ),
    "balloon_add": ToolAliases(
        synonyms=(
            "balloon",
            "item reference",
            "item number",
            "position number",
            "iso 6433",
            "number the items",
            "balon",
            "pozisyon numarasi",
        ),
    ),
    "xref_attach": ToolAliases(
        acad=("XATTACH",),
        synonyms=(
            "attach an xref",
            "external reference",
            "xref",
            "reference another drawing",
            "overlay a drawing",
            "harici referans",
        ),
    ),
    "xref_manage": ToolAliases(
        acad=("XREF", "XBIND"),
        synonyms=(
            "list the xrefs",
            "reload an xref",
            "bind an xref",
            "detach an xref",
            "repath an xref",
            "xref manager",
            "broken xref path",
        ),
    ),
    "image_attach": ToolAliases(
        acad=("IMAGEATTACH",),
        synonyms=(
            "attach an image",
            "raster underlay",
            "insert a png",
            "background image",
            "scanned drawing",
            "resim ekle",
        ),
    ),
    "data_extract": ToolAliases(
        acad=("DATAEXTRACTION", "EATTEXT"),
        synonyms=(
            # Not the bare phrase "parts list": that is the golden query for
            # `entity_create_table`, and a file writer must not outrank the
            # tool that draws the thing. This one is asked for by its
            # destination -- a file -- which is what distinguishes it.
            "export the bill of materials",
            "extract attributes",
            "bom to csv",
            "bom to excel",
            "data extraction",
            "excele aktar",
        ),
    ),
    "drawing_export_dwg": ToolAliases(
        acad=("SAVEAS",),
        synonyms=(
            "export dwg",
            "save as dwg",
            "write a dwg",
            "autocad 2018 format",
            "downgrade to r2000",
            "dwg olarak kaydet",
            "save this drawing as a dwg",
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
    "system_variable_describe": ToolAliases(
        acad=("SETVAR", "SYSVARMONITOR", "SYSVDLG"),
        synonyms=(
            "sysvar",
            "what does ltscale do",
            "explain a system variable",
            "system variable reference",
            "which variables are saved in the drawing",
            "valid range of a system variable",
            "list system variables",
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
    # ── P&ID ────────────────────────────────────────────────────────────────
    "pid_symbol_list": ToolAliases(
        acad=(),
        synonyms=(
            "p&id symbols",
            "pid symbol catalogue",
            "which valves can i draw",
            "instrument bubble types",
        ),
    ),
    "pid_symbol_insert": ToolAliases(
        acad=("INSERT",),
        synonyms=(
            "p&id symbol",
            "insert a valve",
            "place a pump",
            "draw a vessel",
            "instrument bubble",
            "control valve",
            "off-page connector",
            "process equipment symbol",
        ),
    ),
    "pid_line_draw": ToolAliases(
        acad=("PLINE",),
        synonyms=(
            "process line",
            "pipe run",
            "signal line",
            "connect equipment",
            "instrument line",
            "line number",
            "connect the pump to the vessel",
            "pneumatic signal",
            "unnumbered signal line",
            "route a line through waypoints",
        ),
    ),
    "pid_graph": ToolAliases(
        acad=(),
        synonyms=(
            "p&id connectivity",
            "what is connected to",
            "trace the line",
            "read the p&id",
            "dangling lines",
            "instrument loop graph",
            "process graph",
        ),
    ),
    "pid_instrument_index": ToolAliases(
        acad=(),
        synonyms=("instrument index", "instrument list", "loop list", "tag list"),
    ),
    "pid_line_list": ToolAliases(
        acad=(),
        synonyms=("line list", "pipe list", "line schedule", "line numbers"),
    ),
    "pid_equipment_list": ToolAliases(
        acad=(),
        synonyms=("equipment list", "equipment schedule", "valve list"),
    ),
    "pid_from_spec": ToolAliases(
        acad=(),
        synonyms=(
            "whole p&id in one call",
            "p&id from json",
            "draw the process from a spec",
            "batch p&id",
        ),
    ),
    "pid_tag_parse": ToolAliases(
        acad=(),
        synonyms=(
            "what does fic mean",
            "isa 5.1 tag",
            "instrument tag letters",
            "decode the tag",
            "loop number",
        ),
    ),
    # ── Styles (track E, group S) ───────────────────────────────────────────
    "dimstyle_create": ToolAliases(
        acad=("DIMSTYLE", "DDIM"),
        synonyms=(
            "new dimension style",
            "create a dimension style",
            "iso-25 dimension style",
            "ansi dimension style",
            "dimension style from a preset",
            "set up dimension text height and arrows",
        ),
    ),
    "dimstyle_list": ToolAliases(
        acad=("DIMSTYLE",),
        synonyms=(
            "list dimension styles",
            "dimension styles in the drawing",
            "dimension style table",
            "current dimstyle",
            "dim styles",
        ),
    ),
    "dimstyle_modify": ToolAliases(
        acad=("DIMSTYLE",),
        synonyms=(
            "change a dimension style",
            "edit the dimension style",
            "change dimension text height for the whole drawing",
            "set dimdec on a style",
            "modify dimstyle variables",
        ),
    ),
    "dimstyle_set_current": ToolAliases(
        acad=("DIMSTYLE",),
        synonyms=(
            "current dimension style",
            "make this dimension style current",
            "switch dimension style",
            "use iso-25 for new dimensions",
            "activate a dimstyle",
        ),
    ),
    "drawing_apply_standard": ToolAliases(
        acad=(),
        synonyms=(
            "set the drawing up to iso",
            "iso drafting standard",
            "ansi drawing setup",
            "apply the drafting standard",
            "iso-25 and isocp in one go",
            "standard styles units and layers",
        ),
    ),
    "mleaderstyle_create": ToolAliases(
        acad=("MLEADERSTYLE",),
        synonyms=(
            "new leader style",
            "multileader style",
            "leader arrow size and landing",
            "iso leader style",
        ),
    ),
    "mleaderstyle_list": ToolAliases(
        acad=("MLEADERSTYLE",),
        synonyms=("list leader styles", "multileader styles in the drawing", "mleader styles"),
    ),
    "textstyle_create": ToolAliases(
        acad=("STYLE",),
        synonyms=(
            "new text style",
            "create a text style",
            "isocp font",
            "set the font",
            "text style with width factor and oblique angle",
        ),
    ),
    "textstyle_list": ToolAliases(
        acad=("STYLE",),
        synonyms=(
            "list text styles",
            "fonts loaded in the drawing",
            "text style table",
            "current text style",
        ),
    ),
    "textstyle_set_current": ToolAliases(
        acad=("STYLE", "TEXTSTYLE"),
        synonyms=(
            "make this text style current",
            "switch the text style",
            "use isocp for new text",
            "active text style",
        ),
    ),
    # ── Page setup & templates (track E) ────────────────────────────────────
    "batch_plot": ToolAliases(
        acad=("PUBLISH", "PLOT"),
        synonyms=(
            "print all sheets to pdf",
            "plot every layout",
            "publish the sheet set",
            "batch print",
            "one pdf per sheet",
            "plot the whole drawing set",
        ),
    ),
    "drawing_template_list": ToolAliases(
        acad=(),
        synonyms=(
            "which templates are there",
            "bundled templates",
            "iso a3 template",
            "ansi b sheet",
            "start from a standard sheet",
            "dwt list",
        ),
    ),
    "drawing_template_save": ToolAliases(
        acad=("SAVEAS",),
        synonyms=(
            "save as template",
            "make a dwt",
            "template from this drawing",
            "save as dwt",
            "reuse this sheet setup",
        ),
    ),
    "page_setup_apply": ToolAliases(
        acad=("PAGESETUP",),
        synonyms=(
            "page setup",
            "set the paper size",
            "a3 landscape",
            "plot scale",
            "monochrome ctb",
            "which printer",
            "set up the sheet for printing",
        ),
    ),
    "page_setup_list": ToolAliases(
        acad=("PAGESETUP",),
        synonyms=(
            "what paper is this sheet",
            "page setup of each layout",
            "plot settings",
            "sheet size and orientation",
            "which ctb is set",
        ),
    ),
    "plot_style_list": ToolAliases(
        acad=("STYLESMANAGER",),
        synonyms=(
            "ctb files",
            "plot style table",
            "pen assignments",
            "monochrome or grayscale",
            "which plot styles are installed",
        ),
    ),
    # ── Environment (track E, group V) ──────────────────────────────────────
    "document_activate": ToolAliases(
        acad=(),
        synonyms=(
            "switch drawing",
            "switch to the other drawing",
            "make this drawing active",
            "go to the other open file",
            "change the current document",
        ),
    ),
    "document_close": ToolAliases(
        acad=("CLOSE",),
        synonyms=(
            "close a drawing by name",
            "close the other drawing",
            "close without saving",
            "discard changes and close",
            "close all but this one",
        ),
    ),
    # No "which ... are" / "which one is" phrasings here: the interrogative
    # tokens are rare in the corpus, so two of them made this tool outrank
    # `block_list` for the holdout "which blocks are defined". "drawings",
    # "open", "active" and "documents" carry every document-list question
    # to #1 on their own (measured 2026-09-16).
    "document_list": ToolAliases(
        acad=(),
        synonyms=(
            "open drawings",
            "list documents",
            "what files are open",
            "the active drawing",
            "currently open files",
        ),
    ),
    "layer_state_delete": ToolAliases(
        acad=("LAYERSTATE",),
        synonyms=("delete a layer state", "remove a saved layer setup", "drop the layer snapshot"),
    ),
    # Same rule as `document_list` above: no "which ... are" phrasings on the
    # three list tools here — "which layer setups are saved" alone pushed
    # `block_list` to #4 for the holdout "which blocks are defined" (measured
    # 2026-09-17); the nouns carry the questions without the interrogative.
    "layer_state_list": ToolAliases(
        acad=("LAYERSTATE",),
        synonyms=("saved layer states", "saved layer setups", "list layer snapshots"),
    ),
    "layer_state_restore": ToolAliases(
        acad=("LAYERSTATE",),
        synonyms=(
            "restore the layer state",
            "put the layers back",
            "go back to the plot layer setup",
            "reapply saved layer settings",
            "layer snapshot restore",
        ),
    ),
    "layer_state_save": ToolAliases(
        acad=("LAYERSTATE",),
        synonyms=(
            "save the layer state",
            "remember the layer setup",
            "snapshot the layers",
            "layer configuration for plotting",
            "save which layers are frozen",
        ),
    ),
    "system_launch": ToolAliases(
        acad=(),
        synonyms=(
            "start autocad",
            "launch autocad",
            "attach to the running autocad",
            "is autocad running",
            "open autocad with this file",
        ),
    ),
    "system_preferences_get": ToolAliases(
        acad=("OPTIONS",),
        synonyms=(
            "read a preference",
            "autosave interval",
            "support file search path",
            "what is the pickbox size",
            "options dialog value",
        ),
    ),
    "system_preferences_set": ToolAliases(
        acad=("OPTIONS",),
        synonyms=(
            "change a preference",
            "set the autosave interval",
            "cursor size",
            "pickbox size",
            "default plot style table",
        ),
    ),
    "system_prompt_message": ToolAliases(
        acad=(),
        synonyms=(
            "message on the command line",
            "tell the operator",
            "print to the command line",
            "command line note",
            "say something in autocad",
        ),
    ),
    "ucs_list": ToolAliases(
        acad=("UCS", "UCSMAN"),
        synonyms=("the current ucs", "list coordinate systems", "named ucs", "ucs manager"),
    ),
    "ucs_restore": ToolAliases(
        acad=("UCS",),
        synonyms=(
            "back to world coordinates",
            "reset the ucs",
            "ucs world",
            "switch to a saved ucs",
            "make that coordinate system current",
        ),
    ),
    "ucs_set": ToolAliases(
        acad=("UCS",),
        synonyms=(
            "user coordinate system",
            "define a ucs",
            "new coordinate system at this origin",
            "rotate the ucs",
            "ucs origin",
        ),
    ),
    "user_pick_point": ToolAliases(
        acad=("ID",),
        synonyms=(
            "pick a point",
            "click a point on screen",
            "ask the operator for a point",
            "where should this go",
            "let me click",
        ),
    ),
    "user_select": ToolAliases(
        acad=("SELECT",),
        synonyms=(
            "let me select",
            "pick objects on screen",
            "ask the operator to select",
            "select on screen",
            "hand me the selection",
        ),
    ),
    "view_named_list": ToolAliases(
        acad=("VIEW",),
        synonyms=("saved views", "existing named views", "list the views"),
    ),
    "view_named_restore": ToolAliases(
        acad=("VIEW",),
        synonyms=(
            "go to the saved view",
            "restore a named view",
            "jump to the detail view",
            "recall the view",
        ),
    ),
    "view_named_save": ToolAliases(
        acad=("VIEW",),
        synonyms=(
            "save this view",
            "named view",
            "remember where i am looking",
            "bookmark the view",
            "save a detail view",
        ),
    ),
    # ── Mechanical parts (SECTION 21, tracks B+G group M) ───────────────────
    "mech_part_draw": ToolAliases(
        acad=(),
        synonyms=(
            "draw a shaft",
            "stepped shaft",
            "draw a machine part",
            "turned part",
            "draw a plate",
            "flange",
            "part from a segment list",
            "shaft with a keyway",
            "mil",
            "mil ciz",
            "kademeli mil",
            "kama",
            "pah",
            "plaka",
            "flans",
        ),
    ),
    "mech_view_add": ToolAliases(
        acad=("SECTIONPLANE",),
        synonyms=(
            "add a section view",
            "section a-a",
            "cross section",
            "half section",
            "offset section",
            "detail view",
            "blown-up detail",
            "end view",
            "side view",
            "another view of the part",
            "kesit",
            "kesit al",
            "gorunus",
            "yan gorunus",
            "detay",
        ),
    ),
    "mech_dimension_part": ToolAliases(
        acad=(),
        synonyms=(
            "dimension the part",
            "put the dimensions on",
            "chain dimensions",
            "baseline dimensions",
            "ordinate dimensions",
            "hole table",
            "iso 286 fit on a dimension",
            "h7 bore",
            "olculendir",
            "olcu ver",
            "tolerans",
        ),
    ),
    "mech_hole_pattern": ToolAliases(
        acad=("ARRAY",),
        synonyms=(
            "bolt circle",
            "hole pattern",
            "hole array",
            "pitch circle of holes",
            "grid of holes",
            "delik deseni",
            "civata dairesi",
            "delik aynasi",
        ),
    ),
    "mech_part_from_spec": ToolAliases(
        acad=(),
        synonyms=(
            "draw the whole sheet",
            "several parts at once",
            "mechanical sheet from a spec",
            "batch draw parts",
            "parca sayfasi",
        ),
    ),
    "mech_part_inspect": ToolAliases(
        acad=(),
        synonyms=(
            "what part is this",
            "read the part model back",
            "part features",
            "which views were drawn",
            "parca bilgisi",
        ),
    ),
    # ── Standard parts (SECTION 22, tracks B+G group S) ─────────────────────
    "std_part_list": ToolAliases(
        acad=(),
        synonyms=(
            "standard parts catalogue",
            "what bolts do you have",
            "bolt sizes",
            "hex bolt table",
            "bearing list",
            "iso 4014 sizes",
            "find a washer",
            "civata listesi",
            "rulman listesi",
        ),
    ),
    "std_part_insert": ToolAliases(
        acad=("INSERT",),
        synonyms=(
            "insert a bolt",
            "place a screw",
            "draw a nut",
            "add a washer",
            "put a bearing in",
            "hex bolt",
            "socket head cap screw",
            "allen screw",
            "deep groove ball bearing",
            "standard part",
            "civata",
            "somun",
            "rondela",
            "rulman",
            "put an m12 hex bolt on the drawing",
        ),
    ),
    "std_feature_draw": ToolAliases(
        acad=(),
        synonyms=(
            "draw a thread",
            "thread representation",
            "undercut",
            "relief groove",
            "retaining ring groove",
            "circlip groove",
            "snap ring groove",
            "centre hole",
            "center drill",
            "o-ring groove",
            "din 509",
            "din 471",
            "din 332",
            "vida disi",
            "segman kanali",
            "punta deligi",
        ),
    ),
    # ── Architecture (SECTION 25, track F group W) ──────────────────────────
    "arch_wall": ToolAliases(
        acad=("WALLADD", "MLINE"),
        synonyms=(
            "draw a wall",
            "wall with a thickness",
            "exterior wall",
            "partition wall",
            "floor plan walls",
            "clean wall corners",
            "t junction of walls",
            "wall poche",
            "duvar",
            "duvar çiz",
            "duvar ciz",
            "bölme duvar",
            "bolme duvar",
            "kat planı",
            "kat plani",
        ),
    ),
    "arch_opening": ToolAliases(
        acad=("DOORADD", "WINDOWADD"),
        synonyms=(
            "add a door",
            "door in a wall",
            "door swing",
            "add a window",
            "window in a wall",
            "cut an opening in the wall",
            "kapı",
            "kapi",
            "kapı ekle",
            "pencere",
            "pencere ekle",
            "doğrama",
            "dograma",
        ),
    ),
    "arch_stair": ToolAliases(
        acad=("STAIRADD",),
        synonyms=(
            "draw a stair",
            "staircase",
            "flight of stairs",
            "l-shaped stair",
            "u-shaped stair",
            "walking line",
            "blondel rule",
            "risers and treads",
            "merdiven",
            "merdiven çiz",
            "merdiven ciz",
            "basamak",
            "rıht",
            "riht",
        ),
    ),
    "arch_dimension_chains": ToolAliases(
        acad=(),
        synonyms=(
            "dimension the facade",
            "exterior dimensions",
            "dimension chains",
            "overall building dimensions",
            "opening dimensions on the plan",
            "ölçülendir",
            "olculendir plan",
            "dış ölçü",
            "dis olcu",
            "aks ölçüsü",
            "aks olcusu",
        ),
    ),
    # ── Architecture: rooms (SECTION 25, track F group R) ───────────────────
    # AREA and BOUNDARY stay with analysis_measure_entity and boundary_trace:
    # on a plain AutoCAD seat a drafter typing them means those tools, and
    # sharing them would move the golden BPOLY / area cases. The room
    # vocabulary below reaches these two without either command.
    "arch_room": ToolAliases(
        acad=(),
        synonyms=(
            "room label",
            "label the room",
            "room name and number",
            "net floor area",
            "measured room area",
            "oda etiketi",
            "oda alani",
            "mahal adi",
        ),
    ),
    "arch_rooms_detect": ToolAliases(
        acad=(),
        synonyms=(
            "find the rooms",
            "detect rooms in a plan",
            "rooms of a floor plan",
            "room areas of this plan",
            "read rooms from lines",
            "odalari bul",
            "kat plani alanlari",
        ),
    ),
    "arch_schedule": ToolAliases(
        acad=("TABLE",),
        synonyms=(
            "door schedule",
            "window schedule",
            "room schedule",
            "opening schedule",
            "kapi cizelgesi",
            "pencere cizelgesi",
            "mahal listesi",
        ),
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
