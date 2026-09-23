"""The furniture and sanitary catalogue: authored outlines in, real blocks out.

The standard-parts pattern of track B (``engineering/mech/stdparts.py``)
applied to an architectural plan: :func:`catalogue` searches the items by
name, family and their English or Turkish label; :func:`block_spec` turns one
item into the typed primitives and the ATTDEF ``block_define`` takes;
:func:`insert_item` defines the block ``ARCH_<NAME>`` once, inserts it on the
furniture or sanitary layer and reports what it did.

**Sizes are nominal catalogue dimensions, not standards values.** A 900 x 2000
single bed or a 600 x 600 washing machine is what a furniture or appliance
catalogue lists as a common nominal size; no standard fixes it, and every row
says so in ``SIZE_BASIS``. The outlines are plan symbols drawn inside that
footprint, not manufacturer drawings.

Block-local frame, the same for every item: the origin is the **back-left
corner**, the back (the side that stands against a wall) runs along +X from the
origin, and the item extends towards +Y. So ``rotation=0`` puts an item's back
on a wall that runs along +X below it, and every primitive lies inside
``[0, w] x [0, d]`` - a test holds each item to that.

Block content is on layer ``0`` (colour and linetype ByBlock, which is how
``block_define`` writes them), so an INSERT takes the look of the layer it is
inserted on: ``ARCH_ROLE_LAYER["furniture"]`` or ``ARCH_ROLE_LAYER["sanitary"]``.
"""

from __future__ import annotations

import difflib
import math
from typing import TYPE_CHECKING, NamedTuple

from engineering.arch.lang import LANGS
from engineering.arch.layers import ARCH_LAYERS, ARCH_ROLE_LAYER

if TYPE_CHECKING:
    from backends.base import AutoCADBackend

FAMILIES: tuple[str, ...] = ("furniture", "sanitary")

#: What every size in this module is - stated in each row and in the tool docstring.
SIZE_BASIS = "nominal catalogue dimensions (w x d, mm) - not a standards value"

BLOCK_PREFIX = "ARCH_"

#: The one ATTDEF every catalogue block carries: the item name, invisible, so a
#: count or a data extract can read what the INSERT is without parsing its name.
ITEM_TAG = "ITEM"
ITEM_TAG_HEIGHT = 100.0


# -- block_define spec helpers (block-local mm, layer 0) ---------------------


def _line(x1: float, y1: float, x2: float, y2: float) -> dict:
    return {
        "type": "line",
        "x1": float(x1),
        "y1": float(y1),
        "x2": float(x2),
        "y2": float(y2),
        "layer": "0",
    }


def _circle(cx: float, cy: float, r: float) -> dict:
    return {"type": "circle", "cx": float(cx), "cy": float(cy), "r": float(r), "layer": "0"}


def _arc(cx: float, cy: float, r: float, start_deg: float, end_deg: float) -> dict:
    return {
        "type": "arc",
        "cx": float(cx),
        "cy": float(cy),
        "r": float(r),
        "start_deg": float(start_deg),
        "end_deg": float(end_deg),
        "layer": "0",
    }


def _poly(points, closed: bool) -> dict:
    return {
        "type": "polyline",
        "points": [[float(x), float(y)] for x, y in points],
        "closed": bool(closed),
        "layer": "0",
    }


def _rect(x0: float, y0: float, x1: float, y1: float) -> dict:
    return _poly(((x0, y0), (x1, y0), (x1, y1), (x0, y1)), True)


# -- the items ---------------------------------------------------------------


class CatalogueItem(NamedTuple):
    name: str
    family: str
    label_en: str
    label_tr: str
    w: float
    d: float
    entities: tuple[dict, ...]


def _chair(x0: float, y0: float, back_at_top: bool) -> tuple[dict, ...]:
    """A 450 x 450 chair seat with its back rail 60 mm in from the back edge."""
    back_y = y0 + 390.0 if back_at_top else y0 + 60.0
    return (_rect(x0, y0, x0 + 450.0, y0 + 450.0), _line(x0, back_y, x0 + 450.0, back_y))


ITEMS: tuple[CatalogueItem, ...] = (
    # -- furniture -----------------------------------------------------------
    CatalogueItem(
        "single_bed",
        "furniture",
        "single bed",
        "tek kişilik yatak",
        900.0,
        2000.0,
        (_rect(0, 0, 900, 2000), _rect(100, 50, 800, 450), _line(0, 700, 900, 700)),
    ),
    CatalogueItem(
        "double_bed",
        "furniture",
        "double bed",
        "çift kişilik yatak",
        1600.0,
        2000.0,
        (
            _rect(0, 0, 1600, 2000),
            _rect(100, 50, 750, 450),
            _rect(850, 50, 1500, 450),
            _line(0, 700, 1600, 700),
        ),
    ),
    CatalogueItem(
        "wardrobe",
        "furniture",
        "wardrobe",
        "gardırop",
        1200.0,
        600.0,
        (_rect(0, 0, 1200, 600), _line(50, 300, 1150, 300)),
    ),
    CatalogueItem(
        "sofa_3_seat",
        "furniture",
        "sofa, three seats",
        "üçlü kanepe",
        2200.0,
        900.0,
        (
            _rect(0, 0, 2200, 900),
            _line(200, 0, 200, 900),
            _line(2000, 0, 2000, 900),
            _line(200, 200, 2000, 200),
            _line(800, 200, 800, 900),
            _line(1400, 200, 1400, 900),
        ),
    ),
    CatalogueItem(
        "armchair",
        "furniture",
        "armchair",
        "berjer koltuk",
        900.0,
        900.0,
        (
            _rect(0, 0, 900, 900),
            _line(150, 0, 150, 900),
            _line(750, 0, 750, 900),
            _line(150, 200, 750, 200),
        ),
    ),
    CatalogueItem(
        "chair",
        "furniture",
        "chair",
        "sandalye",
        450.0,
        450.0,
        _chair(0.0, 0.0, back_at_top=False),
    ),
    CatalogueItem(
        "dining_table_4",
        "furniture",
        "dining table with four chairs",
        "dört kişilik yemek masası",
        1400.0,
        1700.0,
        (
            _rect(0, 450, 1400, 1250),
            *_chair(125.0, 0.0, back_at_top=False),
            *_chair(825.0, 0.0, back_at_top=False),
            *_chair(125.0, 1250.0, back_at_top=True),
            *_chair(825.0, 1250.0, back_at_top=True),
        ),
    ),
    CatalogueItem(
        "desk",
        "furniture",
        "desk",
        "çalışma masası",
        1400.0,
        700.0,
        (_rect(0, 0, 1400, 700), _line(1000, 0, 1000, 700)),
    ),
    CatalogueItem(
        "kitchen_counter",
        "furniture",
        "kitchen counter run",
        "mutfak tezgâhı",
        2400.0,
        600.0,
        (
            _rect(0, 0, 2400, 600),
            _line(600, 0, 600, 600),
            _line(1200, 0, 1200, 600),
            _line(1800, 0, 1800, 600),
        ),
    ),
    CatalogueItem(
        "fridge",
        "furniture",
        "fridge",
        "buzdolabı",
        600.0,
        650.0,
        (_rect(0, 0, 600, 650), _line(0, 600, 600, 600)),
    ),
    CatalogueItem(
        "bookshelf",
        "furniture",
        "bookshelf",
        "kitaplık",
        800.0,
        300.0,
        (_rect(0, 0, 800, 300), _line(0, 0, 800, 300)),
    ),
    CatalogueItem(
        "nightstand",
        "furniture",
        "nightstand",
        "komodin",
        500.0,
        400.0,
        (_rect(0, 0, 500, 400),),
    ),
    CatalogueItem(
        "coffee_table",
        "furniture",
        "coffee table",
        "orta sehpa",
        1100.0,
        600.0,
        (_rect(0, 0, 1100, 600),),
    ),
    # -- sanitary ------------------------------------------------------------
    CatalogueItem(
        "wc",
        "sanitary",
        "wc with cistern",
        "klozet",
        380.0,
        700.0,
        (
            _rect(0, 0, 380, 180),
            _line(40, 180, 40, 530),
            _line(340, 180, 340, 530),
            _arc(190, 530, 150, 0, 180),
        ),
    ),
    CatalogueItem(
        "bidet",
        "sanitary",
        "bidet",
        "bide",
        360.0,
        560.0,
        (
            _line(0, 0, 360, 0),
            _line(0, 0, 0, 380),
            _line(360, 0, 360, 380),
            _arc(180, 380, 180, 0, 180),
            _circle(180, 80, 20),
            _circle(180, 380, 20),
        ),
    ),
    CatalogueItem(
        "wall_basin",
        "sanitary",
        "wall basin",
        "lavabo",
        550.0,
        450.0,
        (
            _poly(((0, 175), (0, 0), (550, 0), (550, 175)), False),
            _arc(275, 175, 275, 0, 180),
            _circle(275, 225, 150),
            _circle(275, 225, 20),
            _circle(275, 50, 15),
        ),
    ),
    CatalogueItem(
        "shower_tray",
        "sanitary",
        "shower tray",
        "duş teknesi",
        900.0,
        900.0,
        (_rect(0, 0, 900, 900), _rect(50, 50, 850, 850), _circle(450, 450, 50)),
    ),
    CatalogueItem(
        "bathtub",
        "sanitary",
        "bathtub",
        "küvet",
        1700.0,
        700.0,
        (_rect(0, 0, 1700, 700), _rect(60, 60, 1640, 640), _circle(200, 350, 25)),
    ),
    CatalogueItem(
        "kitchen_sink",
        "sanitary",
        "kitchen sink with drainer",
        "eviye",
        1000.0,
        500.0,
        (
            _rect(0, 0, 1000, 500),
            _rect(50, 50, 450, 450),
            _circle(250, 250, 30),
            _line(550, 150, 950, 150),
            _line(550, 250, 950, 250),
            _line(550, 350, 950, 350),
        ),
    ),
    CatalogueItem(
        "washing_machine",
        "sanitary",
        "washing machine",
        "çamaşır makinesi",
        600.0,
        600.0,
        (_rect(0, 0, 600, 600), _circle(300, 300, 200)),
    ),
)

CATALOGUE_NAMES: tuple[str, ...] = tuple(item.name for item in ITEMS)
_BY_NAME: dict[str, CatalogueItem] = {item.name: item for item in ITEMS}

#: Turkish letters folded to ASCII so "buzdolabi" finds "buzdolabı" and a
#: query typed on an English keyboard still reaches the Turkish label.
_FOLD = str.maketrans(
    {"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u"}
)


def _fold(text: str) -> str:
    """Lower-case and fold Turkish letters. ``İ`` is mapped before ``lower()``,
    which would otherwise turn it into ``i`` plus a combining dot."""
    return " ".join(str(text).replace("İ", "i").lower().translate(_FOLD).split())


def _check_lang(lang: str) -> str:
    if lang not in LANGS:
        raise ValueError(f"lang must be one of {', '.join(LANGS)}, got {lang!r}.")
    return lang


def _row(item: CatalogueItem, lang: str) -> dict:
    return {
        "name": item.name,
        "family": item.family,
        "label": item.label_tr if lang == "tr" else item.label_en,
        "label_en": item.label_en,
        "label_tr": item.label_tr,
        "size": [item.w, item.d],
        "size_basis": SIZE_BASIS,
        "block": f"{BLOCK_PREFIX}{item.name.upper()}",
        "layer": ARCH_ROLE_LAYER[item.family],
    }


def catalogue(
    query: str | None = None, family: str | None = None, *, lang: str = "en"
) -> tuple[dict, ...]:
    """Every catalogue item, optionally filtered.

    ``family`` is ``furniture`` or ``sanitary`` (anything else is refused with
    the list). ``query`` is split into words and every word must occur in the
    item's name, family, English label or Turkish label; matching ignores case
    and Turkish diacritics, so ``'bed'``, ``'yatak'``, ``'KLOZET'`` and
    ``'buzdolabi'`` all work. ``lang`` picks the ``label`` column.
    """
    _check_lang(lang)
    if family is not None and family not in FAMILIES:
        raise ValueError(f"family must be one of {', '.join(FAMILIES)}, got {family!r}.")
    words = _fold(query).split() if query else []
    rows: list[dict] = []
    for item in ITEMS:
        if family is not None and item.family != family:
            continue
        haystack = _fold(
            f"{item.name.replace('_', ' ')} {item.family} {item.label_en} {item.label_tr}"
        )
        if all(word in haystack for word in words):
            rows.append(_row(item, lang))
    return tuple(rows)


def _resolve(name: str) -> CatalogueItem:
    key = "_".join(str(name).replace("-", " ").lower().split())
    item = _BY_NAME.get(key)
    if item is not None:
        return item
    close = difflib.get_close_matches(key, CATALOGUE_NAMES, n=3, cutoff=0.4)
    hint = (
        f"nearest: {', '.join(close)}"
        if close
        else f"the catalogue holds {', '.join(CATALOGUE_NAMES)}"
    )
    raise ValueError(f"arch catalogue: no item {name!r}; {hint}.")


def block_spec(name: str) -> dict:
    """The typed primitives and the ITEM ATTDEF ``block_define`` takes for one item.

    Returns ``{"name", "entities", "attdefs", "base_x", "base_y", "item",
    "family", "size", "size_basis"}``. An unknown name is refused with the
    nearest catalogue names before anything is built.
    """
    item = _resolve(name)
    return {
        "name": f"{BLOCK_PREFIX}{item.name.upper()}",
        "entities": [dict(spec) for spec in item.entities],
        "attdefs": [
            {
                "tag": ITEM_TAG,
                "prompt": "Catalogue item",
                "default": item.name,
                "x": item.w / 2.0,
                "y": item.d / 2.0,
                "height": ITEM_TAG_HEIGHT,
                "align": "middle_center",
                "invisible": True,
            }
        ],
        "base_x": 0.0,
        "base_y": 0.0,
        "item": item.name,
        "family": item.family,
        "size": [item.w, item.d],
        "size_basis": SIZE_BASIS,
    }


async def _ensure_layer(backend: AutoCADBackend, name: str) -> bool:
    """Create the INSERT's layer from its ``ARCH_LAYERS`` row when the drawing lacks it.

    Live ActiveX refuses ``entity.Layer = "X"`` on a missing layer (the
    repository rule in CLAUDE.md); the headless engine would create it
    silently with no colour or lineweight. Returns whether it was created.
    """
    if name.lower() in {layer.name.lower() for layer in await backend.layer_list()}:
        return False
    row = next((row for row in ARCH_LAYERS if row[0] == name), None)
    color, linetype, lineweight = (row[1], row[2], row[3]) if row else (7, "Continuous", 0.25)
    await backend.layer_create(name=name, color=color, linetype=linetype, lineweight=lineweight)
    return True


async def insert_item(backend: AutoCADBackend, name: str, *, at, rotation: float = 0.0) -> dict:
    """Define ``ARCH_<NAME>`` once, insert it at ``at`` turned by ``rotation``.

    ``at`` is the WCS point the item's back-left corner lands on; ``rotation``
    is degrees CCW about it. Refusals - an unknown item (with the nearest
    names), a non-finite coordinate or rotation - fire before any write.
    """
    spec = block_spec(name)
    try:
        x, y = float(at[0]), float(at[1])
    except (TypeError, IndexError, ValueError) as exc:
        raise ValueError(f"arch catalogue: 'at' must be two numbers, got {at!r}.") from exc
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError(f"arch catalogue: 'at' must be two finite numbers, got {at!r}.")
    angle = float(rotation)
    if not math.isfinite(angle):
        raise ValueError(f"arch catalogue: rotation must be finite, got {rotation!r}.")

    layer = ARCH_ROLE_LAYER[spec["family"]]
    layer_created = await _ensure_layer(backend, layer)
    existing = {block.name.upper() for block in await backend.block_list()}
    defined = spec["name"].upper() not in existing
    if defined:
        await backend.block_define(
            spec["name"],
            spec["entities"],
            spec["attdefs"],
            spec["base_x"],
            spec["base_y"],
            False,
            False,
        )
    ref = await backend.block_insert(
        spec["name"], x, y, 1.0, 1.0, angle, {ITEM_TAG: spec["item"]}, layer
    )
    return {
        "ok": True,
        "handle": ref.handle,
        "block_name": spec["name"],
        "defined": defined,
        "item": spec["item"],
        "family": spec["family"],
        "layer": layer,
        "layer_created": layer_created,
        "at": [x, y],
        "rotation": angle,
        "size": spec["size"],
        "size_basis": SIZE_BASIS,
        "backend": backend.name,
    }
