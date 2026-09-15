# backends/block_specs.py
"""Typed primitive specs for ``block_define``, validated before any write.

Both engines consume the same normalised specs, so a malformed request is
refused identically on COM and ezdxf and nothing is written on either. Every
error names the offending entry (``entities[i]`` / ``attdefs[i]``) and key.
"""

from __future__ import annotations

import math
import re

ALLOWED_TYPES = ("line", "circle", "arc", "polyline", "text", "solid")
TEXT_ALIGNMENTS = ("left", "center", "right", "middle_center")
ATTDEF_TAG_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,30}$")

_REQUIRED = {
    "line": ("x1", "y1", "x2", "y2"),
    "circle": ("cx", "cy", "r"),
    "arc": ("cx", "cy", "r", "start_deg", "end_deg"),
    "polyline": ("points",),
    "text": ("text", "x", "y", "height"),
    "solid": ("points",),
}
_POSITIVE = {"r", "height"}


def _scalar(value, where: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{where} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise TypeError(f"{where} must be finite")
    if positive and number <= 0:
        raise TypeError(f"{where} must be > 0, got {number}")
    return number


def _number(spec: dict, key: str, where: str) -> float:
    return _scalar(spec.get(key), f"{where}: {key!r}", positive=key in _POSITIVE)


def _points(
    spec: dict, where: str, *, minimum: int, maximum: int | None = None
) -> list[tuple[float, float]]:
    raw = spec.get("points")
    if not isinstance(raw, (list, tuple)) or len(raw) < minimum:
        raise TypeError(f"{where}: 'points' must be a list of at least {minimum} [x, y] pairs")
    if maximum is not None and len(raw) > maximum:
        raise TypeError(f"{where}: 'points' must have at most {maximum} pairs")
    out: list[tuple[float, float]] = []
    for i, pt in enumerate(raw):
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise TypeError(f"{where}: points[{i}] must be an [x, y] pair")
        out.append(
            (
                _scalar(pt[0], f"{where}: points[{i}].x"),
                _scalar(pt[1], f"{where}: points[{i}].y"),
            )
        )
    return out


def _align(spec: dict, where: str) -> str:
    align = spec.get("align", "left")
    if align not in TEXT_ALIGNMENTS:
        raise TypeError(f"{where}: 'align' must be one of {TEXT_ALIGNMENTS}, got {align!r}")
    return align


def _layer(spec: dict, where: str) -> str:
    layer = spec.get("layer", "0")
    if not isinstance(layer, str) or not layer.strip():
        raise TypeError(f"{where}: 'layer' must be a non-empty string")
    return layer.strip()


def validate_entity_specs(entities) -> list[dict]:
    """Normalise ``block_define`` primitive specs; refuse the whole list on the first defect."""
    if not isinstance(entities, (list, tuple)) or not entities:
        raise ValueError(
            "block_define: 'entities' is empty — a block that draws nothing is refused"
        )
    out: list[dict] = []
    for i, spec in enumerate(entities):
        where = f"entities[{i}]"
        if not isinstance(spec, dict):
            raise TypeError(f"{where}: must be an object")
        kind = spec.get("type")
        if kind not in ALLOWED_TYPES:
            raise TypeError(f"{where}: 'type' must be one of {ALLOWED_TYPES}, got {kind!r}")
        for key in _REQUIRED[kind]:
            if key not in spec:
                raise TypeError(f"{where}: {kind} requires {key!r}")
        norm: dict = {"type": kind, "layer": _layer(spec, where)}
        if kind == "line":
            for key in ("x1", "y1", "x2", "y2"):
                norm[key] = _number(spec, key, where)
        elif kind == "circle":
            for key in ("cx", "cy", "r"):
                norm[key] = _number(spec, key, where)
        elif kind == "arc":
            for key in ("cx", "cy", "r", "start_deg", "end_deg"):
                norm[key] = _number(spec, key, where)
        elif kind == "polyline":
            pts = _points(spec, where, minimum=2)
            closed = spec.get("closed", False)
            if not isinstance(closed, bool):
                raise TypeError(f"{where}: 'closed' must be a boolean")
            bulges = spec.get("bulges")
            if bulges is not None:
                if not isinstance(bulges, (list, tuple)) or len(bulges) != len(pts):
                    raise TypeError(f"{where}: 'bulges' must list one value per point")
                bulges = [_scalar(b, f"{where}: bulges[{j}]") for j, b in enumerate(bulges)]
            norm.update(points=pts, closed=closed, bulges=list(bulges) if bulges else None)
        elif kind == "text":
            text = spec.get("text")
            if not isinstance(text, str):
                raise TypeError(f"{where}: 'text' must be a string")
            norm.update(
                text=text,
                x=_number(spec, "x", where),
                y=_number(spec, "y", where),
                height=_number(spec, "height", where),
                rotation_deg=_scalar(spec.get("rotation_deg", 0.0), f"{where}: 'rotation_deg'"),
                align=_align(spec, where),
            )
        elif kind == "solid":
            norm["points"] = _points(spec, where, minimum=3, maximum=4)
        out.append(norm)
    return out


def validate_attdef_specs(attdefs) -> list[dict]:
    """Normalise ATTDEF specs: tag pattern, uniqueness, numeric fields, flags."""
    if attdefs is None:
        return []
    if not isinstance(attdefs, (list, tuple)):
        raise TypeError("block_define: 'attdefs' must be a list")
    out: list[dict] = []
    seen: set[str] = set()
    for i, spec in enumerate(attdefs):
        where = f"attdefs[{i}]"
        if not isinstance(spec, dict):
            raise TypeError(f"{where}: must be an object")
        tag = spec.get("tag")
        if not isinstance(tag, str) or not ATTDEF_TAG_RE.match(tag):
            raise TypeError(f"{where}: 'tag' must match {ATTDEF_TAG_RE.pattern}, got {tag!r}")
        if tag in seen:
            raise TypeError(f"{where}: duplicate tag {tag!r}")
        seen.add(tag)
        prompt = spec.get("prompt", tag)
        default = spec.get("default", "")
        invisible = spec.get("invisible", False)
        if not isinstance(prompt, str):
            raise TypeError(f"{where}: 'prompt' must be a string")
        if not isinstance(default, str):
            raise TypeError(f"{where}: 'default' must be a string")
        if not isinstance(invisible, bool):
            raise TypeError(f"{where}: 'invisible' must be a boolean")
        out.append(
            {
                "tag": tag,
                "prompt": prompt,
                "default": default,
                "x": _number(spec, "x", where),
                "y": _number(spec, "y", where),
                "height": _number(spec, "height", where),
                "rotation_deg": _scalar(spec.get("rotation_deg", 0.0), f"{where}: 'rotation_deg'"),
                "align": _align(spec, where),
                "invisible": invisible,
            }
        )
    return out


def solid_vertices(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """DXF SOLID draws vtx0→vtx1→vtx3→vtx2, so a quad given in polygon order
    (a, b, c, d) is stored as (a, b, d, c). Triangles are stored as given."""
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) == 4:
        return [pts[0], pts[1], pts[3], pts[2]]
    return pts
