"""The five bundled drawing templates: what each contains, where its files live.

Built by ``scripts/build_templates.py`` through the server's own backend
(layers, styles, settings, the ISO 5457 A3 frame where the sheet is A3, page
setup) and saved as DXF; the ``.dwt`` twins are produced on a live AutoCAD by
``scripts/smoke_settings_com.py --build-dwt`` and committed once.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

__all__ = [
    "TEMPLATES_DIR",
    "TEMPLATE_CATALOG",
    "TemplateSpec",
    "resolve_template",
    "template_rows",
]

#: ``<repo>/templates`` — shipped in the wheel (pyproject ``only-include``) and the Docker image.
TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"

_ISO_SETTINGS = {
    "units": "mm",
    "ltscale": 1.0,
    "dimscale": 1.0,
    "linear_precision": 2,
    "angular_precision": 0,
    "decimal_separator": ",",
}
_ANSI_SETTINGS = {**_ISO_SETTINGS, "decimal_separator": "."}


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    standard: str  # "ISO" | "ANSI"
    sheet: str  # human label, e.g. "A3 landscape"
    paper: str  # engineering.standards.papers.PAPER_SIZES key
    orientation: str
    layout: str  # the paper-space tab's name
    layer_set: str  # engineering.layers.LAYER_SET_REGISTRY key
    dimstyle_preset: str  # group S preset id: "iso-25" | "ansi"
    dimstyle: str  # style name in the drawing
    textstyle: str  # "ISOCP" | "ROMANS"
    scale: str  # page setup scale label
    title_block: bool
    settings: dict = field(default_factory=dict)  # drawing_settings facade keys
    description: str = ""


TEMPLATE_CATALOG: dict[str, TemplateSpec] = {
    spec.name: spec
    for spec in (
        TemplateSpec(
            name="iso_a3_mech",
            standard="ISO",
            sheet="A3 landscape",
            paper="ISO_A3",
            orientation="landscape",
            layout="A3",
            layer_set="mech",
            dimstyle_preset="iso-25",
            dimstyle="ISO-25",
            textstyle="ISOCP",
            scale="1:1",
            title_block=True,
            settings=dict(_ISO_SETTINGS),
            description="ISO A3 mechanical part drawing",
        ),
        TemplateSpec(
            name="iso_a1_arch",
            standard="ISO",
            sheet="A1 landscape",
            paper="ISO_A1",
            orientation="landscape",
            layout="A1",
            layer_set="iso13567",
            dimstyle_preset="iso-25",
            dimstyle="ISO-25",
            textstyle="ISOCP",
            scale="fit",
            title_block=False,
            settings={**_ISO_SETTINGS, "annotation_scale": "1:50"},
            description="ISO A1 architectural sheet, ISO 13567 layers, 1:50 annotation",
        ),
        TemplateSpec(
            name="iso_a3_pid",
            standard="ISO",
            sheet="A3 landscape",
            paper="ISO_A3",
            orientation="landscape",
            layout="A3",
            layer_set="pid",
            dimstyle_preset="iso-25",
            dimstyle="ISO-25",
            textstyle="ISOCP",
            scale="1:1",
            title_block=True,
            settings=dict(_ISO_SETTINGS),
            description="ISO A3 P&ID sheet",
        ),
        TemplateSpec(
            name="ansi_b_mech",
            standard="ANSI",
            sheet="ANSI B landscape",
            paper="ANSI_B",
            orientation="landscape",
            layout="B",
            layer_set="mech",
            dimstyle_preset="ansi",
            dimstyle="ANSI",
            textstyle="ROMANS",
            scale="1:1",
            title_block=False,
            settings=dict(_ANSI_SETTINGS),
            description="ANSI B mechanical part drawing (metric)",
        ),
        TemplateSpec(
            name="ansi_d_arch",
            standard="ANSI",
            sheet="ANSI D landscape",
            paper="ANSI_D",
            orientation="landscape",
            layout="D",
            layer_set="iso13567",
            dimstyle_preset="ansi",
            dimstyle="ANSI",
            textstyle="ROMANS",
            scale="fit",
            title_block=False,
            settings=dict(_ANSI_SETTINGS),
            description="ANSI D architectural sheet (metric)",
        ),
    )
}


def _file(name: str, suffix: str) -> dict:
    path = TEMPLATES_DIR / f"{name}{suffix}"
    return {"path": str(path), "present": path.is_file()}


def template_rows() -> list[dict]:
    """Catalogue rows for ``drawing_template_list``: the spec plus its files."""
    rows = []
    for spec in TEMPLATE_CATALOG.values():
        row = asdict(spec)
        row["files"] = {"dxf": _file(spec.name, ".dxf"), "dwt": _file(spec.name, ".dwt")}
        rows.append(row)
    return rows


def resolve_template(name_or_path: str, engine: str) -> tuple[str, str]:
    """``(path, source)`` for a catalogue name or a template file path.

    A catalogue name resolves to ``templates/<name>.dxf`` (headless) or
    ``templates/<name>.dwt`` (live), falling back to the DXF with
    ``source="bundled_dxf"`` when the ``.dwt`` twin is missing. Anything with a
    path separator or a ``.dwt``/``.dxf`` suffix is a path (``source="path"``,
    not checked here). A bare word that is not in the catalogue is refused
    with the catalogue names.
    """
    raw = str(name_or_path or "").strip()
    if not raw:
        raise ValueError("template: pass a bundled template name or a path to a .dwt/.dxf file")
    key = raw.lower()
    if key in TEMPLATE_CATALOG:
        dxf = TEMPLATES_DIR / f"{key}.dxf"
        dwt = TEMPLATES_DIR / f"{key}.dwt"
        if engine == "com" and dwt.is_file():
            return str(dwt), "bundled"
        if dxf.is_file():
            return str(dxf), "bundled" if engine != "com" else "bundled_dxf"
        raise ValueError(
            f"template {key!r} is in the catalogue but {dxf} is missing — run "
            "`uv run --frozen python scripts/build_templates.py`"
        )
    looks_like_path = ("/" in raw or "\\" in raw) or Path(raw).suffix.lower() in (".dwt", ".dxf")
    if looks_like_path:
        return raw, "path"
    raise ValueError(
        f"unknown template {raw!r}. Bundled templates: {', '.join(TEMPLATE_CATALOG)}; "
        "or pass a path to a .dwt/.dxf file"
    )
