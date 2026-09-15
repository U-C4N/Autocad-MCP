"""XDATA value typing and limits, shared by both engines.

DXF XDATA is a list of (group code, value) pairs under a registered APPID.
This module is the one place that decides how a JSON value becomes a group
code and enforces the two limits AutoCAD enforces silently: 255 characters
per string and 16 KB per entity. It also refuses the one thing neither engine
can store: a line separator inside a string. DXF is a text format of
alternating "group code" / "value" lines, and ezdxf writes a 1000 tag verbatim,
so a LF or CR in the value splits the tag pair and the saved file -- and every
undo/transaction snapshot, which is also a DXF save -- becomes unreadable.
"""

from __future__ import annotations

import re

APP_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,30}$")
RESERVED_APPS = {"ACAD"}
MAX_STRING = 255
MAX_BYTES = 16 * 1024
INT_MIN, INT_MAX = -(2**31), 2**31 - 1
LINE_SEPARATORS = ("\n", "\r")


def validate_app_name(app) -> str:
    if not isinstance(app, str) or not APP_NAME_RE.match(app):
        raise ValueError(f"xdata: app name must match {APP_NAME_RE.pattern}, got {app!r}")
    if app.upper() in RESERVED_APPS:
        raise ValueError(f"xdata: app name {app!r} is reserved")
    return app


def encode_values(values) -> list[tuple[int, object]]:
    """JSON values → (group code, value). Raises TypeError/ValueError; writes nothing."""
    if not isinstance(values, (list, tuple)):
        raise TypeError("xdata: values must be a list")
    tags: list[tuple[int, object]] = []
    size = 0
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
            size += len(value.encode("utf-8")) + 2
        elif isinstance(value, int):
            if not INT_MIN <= value <= INT_MAX:
                raise ValueError(f"xdata: {where} is outside the 32-bit integer range")
            tags.append((1071, int(value)))
            size += 6
        elif isinstance(value, float):
            tags.append((1040, float(value)))
            size += 10
        elif isinstance(value, (list, tuple)) and len(value) in (2, 3):
            if any(isinstance(c, bool) for c in value):
                raise TypeError(f"xdata: {where} point coordinates must be numbers")
            try:
                coords = tuple(float(c) for c in value)
            except (TypeError, ValueError) as exc:
                raise TypeError(f"xdata: {where} point coordinates must be numbers") from exc
            if len(coords) == 2:
                coords = (coords[0], coords[1], 0.0)
            tags.append((1010, coords))
            size += 26
        else:
            raise TypeError(f"xdata: {where} must be a string, int, float or [x, y(, z)] point")
    if size > MAX_BYTES:
        raise ValueError(f"xdata: encoded size {size} bytes exceeds the 16 KB per-entity limit")
    return tags


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


def split_by_app(codes, values) -> dict[str, list]:
    """A flat ActiveX GetXData stream (1001 markers inline) → {app: values}."""
    out: dict[str, list] = {}
    current: str | None = None
    pending: list[tuple[int, object]] = []
    for code, value in zip(codes, values, strict=True):
        if code == 1001:
            if current is not None:
                out[current] = decode_values(pending)
            current, pending = str(value), []
        else:
            pending.append((code, value))
    if current is not None:
        out[current] = decode_values(pending)
    return out
