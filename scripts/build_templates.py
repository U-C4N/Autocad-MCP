"""Build the bundled templates through the server's own backend, reproducibly.

    uv run --frozen python scripts/build_templates.py            # write templates/*.dxf
    uv run --frozen python scripts/build_templates.py --check    # exit 1 if a rebuild differs
    uv run --frozen python scripts/build_templates.py --out DIR  # build elsewhere

Each template is: drawing_new -> apply_layer_set -> styles -> drawing_settings
-> layout renamed to the sheet -> ISO 5457 frame (A3 sheets) -> page_setup_apply
-> drawing_save_as DXF. Nothing is hand-authored, so what the tools produce is
what ships.

Styles: once group S's ``dimstyle_create`` / ``textstyle_create`` are on the
backend they are used (ISO-25 / ISOCP, ANSI / ROMANS, set current). Until then
the same values (spec §4.1) are written as header DIM variables so the template
already dimensions correctly; the final task re-runs this script after the S
merge and re-commits the five files.

``--check`` rebuilds into a temporary folder and compares line by line after
masking the values ezdxf cannot keep constant — measured on 2026-09-16, exactly
six lines differ between two builds: $TDCREATE, $TDUPDATE, $FINGERPRINTGUID,
$VERSIONGUID and the two EZDXF_META DICTIONARYVAR stamps. One more source of
drift is removed before the save rather than masked: the CLASSES section, which
ezdxf otherwise orders by set iteration (see ``_pin_class_order``).
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backends.ezdxf_backend import EzdxfBackend  # noqa: E402
from engineering import TitleBlockMetadata, apply_iso_a3_titleblock  # noqa: E402
from engineering.layers import apply_layer_set  # noqa: E402
from engineering.standards.papers import resolve_page_setup  # noqa: E402
from engineering.standards.templates import (  # noqa: E402
    TEMPLATE_CATALOG,
    TEMPLATES_DIR,
    TemplateSpec,
)

#: Header variables whose values change on every save.
VOLATILE_HEADER_VARS = frozenset(
    {
        "$TDCREATE",
        "$TDUPDATE",
        "$TDUCREATE",
        "$TDUUPDATE",
        "$TDINDWG",
        "$TDUSRTIMER",
        "$FINGERPRINTGUID",
        "$VERSIONGUID",
    }
)
#: The EZDXF_META stamps: ``1.4.4 @ 2026-09-16T12:33:38.192529+00:00``.
_EZDXF_STAMP = re.compile(r"^\d+\.\d+\.\d+\S* @ \d{4}-\d{2}-\d{2}T")

#: Spec §4.1 as header variables — the fallback until dimstyle_create lands.
#: DIMDSEP goes through the facade (decimal_separator); DIMTXSTY is omitted
#: because the text style it names is created by textstyle_create.
_FALLBACK_DIMVARS: dict[str, dict[str, float | int]] = {
    "iso-25": {
        "DIMTXT": 2.5,
        "DIMASZ": 2.5,
        "DIMEXE": 1.25,
        "DIMEXO": 0.625,
        "DIMGAP": 0.625,
        "DIMTAD": 1,
        "DIMTIH": 0,
        "DIMTOH": 0,
        "DIMDEC": 2,
        "DIMLUNIT": 2,
        "DIMZIN": 8,
        "DIMLWD": -2,
        "DIMLWE": -2,
        "DIMSCALE": 1.0,
    },
    "ansi": {
        "DIMTXT": 3.0,
        "DIMASZ": 3.0,
        "DIMEXE": 1.5,
        "DIMEXO": 1.5,
        "DIMGAP": 1.0,
        "DIMTAD": 0,
        "DIMTIH": 1,
        "DIMTOH": 1,
        "DIMDEC": 2,
        "DIMLUNIT": 2,
        "DIMZIN": 8,
        "DIMLWD": -2,
        "DIMLWE": -2,
        "DIMSCALE": 1.0,
    },
}


async def _apply_styles(backend, spec: TemplateSpec) -> dict:
    if hasattr(backend, "dimstyle_create") and hasattr(backend, "textstyle_create"):
        from engineering.standards.dimstyles import resolve_dimstyle
        from engineering.standards.textstyles import TEXT_PRESETS

        font, width_factor, oblique = TEXT_PRESETS[spec.textstyle]
        text = await backend.textstyle_create(
            spec.textstyle, font, 0.0, width_factor, oblique, set_current=True
        )
        dim = await backend.dimstyle_create(
            spec.dimstyle, resolve_dimstyle(spec.dimstyle_preset, None), set_current=True
        )
        return {"mode": "styles", "dimstyle": dim["name"], "textstyle": text["name"]}
    applied = {}
    for variable, value in _FALLBACK_DIMVARS[spec.dimstyle_preset].items():
        applied[variable] = (await backend.system_set_variable(variable, value))["ok"]
    return {
        "mode": "header_dimvars",
        "applied": applied,
        "pending": "dimstyle_create/textstyle_create not on this backend yet (group S)",
    }


def _pin_class_order(doc) -> list[str]:
    """Put the CLASSES section in a stable order before the save.

    ezdxf fills CLASSES from ``entitydb.dxf_types_in_use()`` — a ``set[str]``,
    so its order follows the process's string-hash seed and two builds in
    different processes put ``LAYOUT`` / ``ACDBPLACEHOLDER`` in different slots
    (measured 2026-09-21: the same-process double build agreed, ``--check``
    from a fresh process did not). The backend's undo baseline has already
    written the document once by now, so the section is already populated in
    that random order; it is re-keyed here as ezdxf's required list first, then
    every other class by name.
    """
    from ezdxf.sections.classes import REQ_R2004, REQUIRED_CLASSES

    section = doc.classes
    section.add_required_classes(doc.dxfversion)
    for name in sorted(doc.entitydb.dxf_types_in_use()):
        section.add_class(name)
    rank = {name: i for i, name in enumerate(REQUIRED_CLASSES.get(doc.dxfversion, REQ_R2004))}
    ordered = sorted(
        section.classes.items(),
        key=lambda item: (rank.get(item[1].dxf.name, len(rank)), item[1].dxf.name, item[0]),
    )
    section.classes = dict(ordered)
    return [cls.dxf.name for cls in section.classes.values()]


async def build_template(spec: TemplateSpec, out_dir: Path) -> dict:
    """Build one template into ``out_dir/<name>.dxf`` and report what went in."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    backend = EzdxfBackend()
    await backend.connect()
    try:
        await backend.drawing_new()
        layers = await apply_layer_set(backend, spec.layer_set)
        styles = await _apply_styles(backend, spec)
        settings = await backend.drawing_settings(dict(spec.settings))
        pending = sorted(settings.get("errors") or {})  # keys the facade does not know yet
        renamed = await backend.layout_rename("Layout1", spec.layout)
        if not renamed.get("ok"):
            raise RuntimeError(f"{spec.name}: layout rename failed: {renamed}")
        title_block = None
        if spec.title_block:
            title_block = await apply_iso_a3_titleblock(
                backend,
                metadata=TitleBlockMetadata(
                    title=spec.description.upper(),
                    drawing_no="TEMPLATE",
                    scale=spec.scale if spec.scale != "fit" else "1:1",
                ),
                layout=spec.layout,
            )
        setup = resolve_page_setup(
            spec.paper,
            spec.orientation,
            "monochrome.ctb",
            spec.scale,
            "layout",
            "DWG To PDF.pc3",
            [0.0, 0.0, 0.0, 0.0],  # the frame is drawn at the paper corner; no unprintable band
            True,
        )
        page_setup = await backend.page_setup_apply(spec.layout, setup)
        await backend.layout_set_current("Model")
        classes = _pin_class_order(backend._doc)
        path = out_dir / f"{spec.name}.dxf"
        await backend.drawing_save_as(str(path), "dxf")
        return {
            "name": spec.name,
            "path": str(path),
            "layers": layers,
            "styles": styles,
            "settings_applied": settings.get("applied", {}),
            "settings_pending": pending,
            "title_block": bool(title_block),
            "page_setup": {"ok": page_setup["ok"], "paper": page_setup["applied"]["paper"]},
            "classes": classes,
        }
    finally:
        await backend.disconnect()


async def build_all(out_dir: Path = TEMPLATES_DIR) -> list[dict]:
    return [await build_template(spec, out_dir) for spec in TEMPLATE_CATALOG.values()]


def normalised_lines(path: Path) -> list[str]:
    """The DXF's lines with every value that legitimately changes per save masked."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    mask_at = -1
    for index, line in enumerate(lines):
        if index == mask_at:
            out.append("<volatile>")
            continue
        stripped = line.strip()
        if stripped in VOLATILE_HEADER_VARS:
            mask_at = index + 2  # "  9" / "$TDCREATE" / " 40" / <value>
        if _EZDXF_STAMP.match(stripped):
            out.append("<ezdxf-stamp>")
            continue
        out.append(line)
    return out


def drifted_names(rebuilt: Path) -> list[str]:
    """Names whose committed DXF differs from the build in ``rebuilt`` (masked compare)."""
    drifted = []
    for name in TEMPLATE_CATALOG:
        committed = TEMPLATES_DIR / f"{name}.dxf"
        if not committed.is_file():
            drifted.append(f"{name} (missing)")
            continue
        fresh = normalised_lines(Path(rebuilt) / f"{name}.dxf")
        current = normalised_lines(committed)
        if fresh != current:
            first = next(
                (i for i, (a, b) in enumerate(zip(fresh, current, strict=False)) if a != b),
                min(len(fresh), len(current)),
            )
            drifted.append(f"{name} (first difference at line {first + 1})")
    return drifted


def check_templates() -> list[str]:
    """CLI ``--check``: rebuild into a temporary folder and report drift."""
    with tempfile.TemporaryDirectory() as folder:
        asyncio.run(build_all(Path(folder)))
        return drifted_names(Path(folder))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--check", action="store_true", help="rebuild to a temp dir and diff")
    parser.add_argument("--out", type=Path, default=TEMPLATES_DIR, help="output folder")
    args = parser.parse_args(argv)
    if args.check:
        drifted = check_templates()
        if drifted:
            print("templates drifted from scripts/build_templates.py: " + ", ".join(drifted))
            return 1
        print(f"{len(TEMPLATE_CATALOG)} templates reproduce byte-for-byte (volatile stamps masked)")
        return 0
    for row in asyncio.run(build_all(args.out)):
        pending = f" pending={row['settings_pending']}" if row["settings_pending"] else ""
        print(f"{row['name']}: {row['path']} styles={row['styles']['mode']}{pending}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
