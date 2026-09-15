"""XDATA value typing and limits, shared by both engines.

DXF XDATA is a list of (group code, value) pairs under a registered APPID.
This module is the one place that decides how a JSON value becomes a group
code and enforces the two limits AutoCAD enforces silently: 255 characters
per string and 16 KB per *entity* -- across every application on it, the ACAD
app's own overrides (DSTYLE on a toleranced dimension) included, not just the
app being written. ``encode_values`` refuses a single call that cannot fit;
``check_entity_budget`` is the real gate, run by both engines against the
entity's existing XDATA before anything is written, so the headless engine
refuses exactly the write the live engine's ``SetXData`` would reject.

It also refuses the values neither engine can store: a line separator inside a
string, and a non-finite or non-numeric "number". DXF is a text format of
alternating "group code" / "value" lines, and ezdxf writes a 1000 tag verbatim,
so a LF or CR in the value splits the tag pair and the saved file -- and every
undo/transaction snapshot, which is also a DXF save -- becomes unreadable. A
``nan``/``inf`` real is written as the literal text ``nan``/``inf``, which is
not a DXF double, and a string coerced into a point coordinate is a silent
wrong number; both are refused before any write, the same way
``backends/block_specs.py`` refuses them for block primitives.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping

APP_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,30}$")
RESERVED_APPS = {"ACAD"}
MAX_STRING = 255
MAX_BYTES = 16 * 1024
INT_MIN, INT_MAX = -(2**31), 2**31 - 1
LINE_SEPARATORS = ("\n", "\r")

Tag = tuple[int, object]


def validate_app_name(app) -> str:
    if not isinstance(app, str) or not APP_NAME_RE.match(app):
        raise ValueError(f"xdata: app name must match {APP_NAME_RE.pattern}, got {app!r}")
    if app.upper() in RESERVED_APPS:
        raise ValueError(f"xdata: app name {app!r} is reserved")
    return app


def _real(value, where: str) -> float:
    """A finite float from an int or float; strings, bools and nan/inf are refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"xdata: {where} must be a number, got {value!r}")
    try:
        number = float(value)
    except OverflowError:
        number = math.inf
    if not math.isfinite(number):
        raise ValueError(f"xdata: {where} must be finite, got {value!r}")
    return number


def tag_size(code: int, value) -> int:
    """Bytes one XDATA tag occupies in AutoCAD's accounting (what ``xdsize`` reports).

    Every tag carries a one-byte group code; strings (1000-1004) add a two-byte
    length, reals are 8 bytes, points 3 reals, a 32-bit int 4, a 16-bit int 2,
    a handle 8. The 1001 application marker is a string and counts like one.
    """
    if code in (1000, 1001, 1002, 1003, 1004, 1005):
        if isinstance(value, str):
            return 3 + len(value.encode("utf-8"))
        if isinstance(value, (bytes, bytearray, list, tuple)):  # 1004 binary chunk
            return 3 + len(value)
        return 3 + len(str(value).encode("utf-8"))
    if code in (1010, 1011, 1012, 1013):
        return 1 + 3 * 8
    if code in (1040, 1041, 1042):
        return 1 + 8
    if code == 1070:
        return 1 + 2
    if code == 1071:
        return 1 + 4
    raise ValueError(f"xdata: {code} is not an XDATA group code")


def app_size(app: str, tags: Iterable[Tag]) -> int:
    """Bytes one application's XDATA occupies: its 1001 marker plus every tag.

    ``tags`` may or may not carry the 1001 marker (ezdxf stores it, its
    ``get_xdata`` strips it); it is counted exactly once either way.
    """
    return tag_size(1001, app) + sum(tag_size(code, value) for code, value in tags if code != 1001)


def encode_values(values) -> list[Tag]:
    """JSON values → (group code, value). Raises TypeError/ValueError; writes nothing."""
    if not isinstance(values, (list, tuple)):
        raise TypeError("xdata: values must be a list")
    tags: list[Tag] = []
    for i, value in enumerate(values):
        where = f"values[{i}]"
        if isinstance(value, bool):
            raise TypeError(f"xdata: {where} booleans have no XDATA group code")
        if isinstance(value, str):
            if len(value) > MAX_STRING:
                raise ValueError(
                    f"xdata: {where} is {len(value)} characters; the limit is {MAX_STRING}"
                )
            if any(sep in value for sep in LINE_SEPARATORS):
                raise ValueError(
                    f"xdata: {where} contains a newline; "
                    "line separators are not allowed in XDATA strings"
                )
            tags.append((1000, value))
        elif isinstance(value, int):
            if not INT_MIN <= value <= INT_MAX:
                raise ValueError(f"xdata: {where} is outside the 32-bit integer range")
            tags.append((1071, int(value)))
        elif isinstance(value, float):
            tags.append((1040, _real(value, where)))
        elif isinstance(value, (list, tuple)) and len(value) in (2, 3):
            coords = tuple(_real(c, f"{where}[{j}]") for j, c in enumerate(value))
            if len(coords) == 2:
                coords = (coords[0], coords[1], 0.0)
            tags.append((1010, coords))
        else:
            raise TypeError(f"xdata: {where} must be a string, int, float or [x, y(, z)] point")
    # A single call that cannot fit on an empty entity is refused here; the
    # per-entity budget (existing apps included) is check_entity_budget().
    size = sum(tag_size(code, value) for code, value in tags)
    if size > MAX_BYTES:
        raise ValueError(f"xdata: encoded size {size} bytes exceeds the 16 KB per-entity limit")
    return tags


def check_entity_budget(
    app: str, tags: Iterable[Tag], existing: Mapping[str, Iterable[Tag]]
) -> int:
    """Bytes the entity's XDATA occupies once ``app`` is replaced by ``tags``.

    ``existing`` is every application currently on the entity (ACAD included)
    with its tags; ``app``'s own entry is what the write replaces, so it is
    dropped from the sum. Raises ValueError over ``MAX_BYTES``; writes nothing.
    An empty ``tags`` removes the app and is never refused, whatever the other
    applications already hold (a file opened with too much XDATA must still be
    shrinkable).
    """
    tags = list(tags)
    others = 0
    for other, other_tags in existing.items():
        if other == app:
            continue
        others += app_size(other, other_tags)
    if not tags:
        return others
    total = app_size(app, tags) + others
    if total > MAX_BYTES:
        raise ValueError(
            f"xdata: entity would carry {total} bytes of XDATA ({others} under other "
            f"applications); the limit is 16 KB per entity across every application"
        )
    return total


def decode_values(tags) -> list:
    """(group code, value) → JSON values. Skips the 1001 app marker and 1002 braces."""
    out: list = []
    for code, value in tags:
        if code in (1001, 1002):
            continue
        if code == 1010 or code in (1011, 1012, 1013):
            out.append([float(c) for c in value])
        elif code in (1040, 1041, 1042):
            out.append(float(value))
        elif code in (1070, 1071):
            out.append(int(value))
        else:
            out.append(str(value))
    return out


def split_tags_by_app(codes, values) -> dict[str, list[Tag]]:
    """A flat ActiveX GetXData stream (1001 markers inline) → {app: raw tags}."""
    out: dict[str, list[Tag]] = {}
    current: str | None = None
    for code, value in zip(codes, values, strict=True):
        if code == 1001:
            current = str(value)
            out.setdefault(current, [])
        elif current is not None:
            out[current].append((code, value))
    return out


def split_by_app(codes, values) -> dict[str, list]:
    """A flat ActiveX GetXData stream (1001 markers inline) → {app: values}."""
    return {app: decode_values(tags) for app, tags in split_tags_by_app(codes, values).items()}
