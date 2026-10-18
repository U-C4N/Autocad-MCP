"""analysis_select_by_type on the live engine answers the DXF names the tool documents.

Found by the practice-exam run (2D-26 ... 2D-18): ``analysis_select_by_type("LWPOLYLINE")`` returned
nothing on AutoCAD because ActiveX calls the same entity ``AcDbPolyline`` and the tool compared the
spelling exactly. ``entity_list`` already used ``_type_filter_matches``; this tool did not.
"""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest

needs_pywin32 = pytest.mark.skipif(
    importlib.util.find_spec("win32com") is None, reason="pywin32 not installed"
)


class _Ent:
    def __init__(self, object_name):
        self.ObjectName = object_name


@needs_pywin32
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wanted", "expect"),
    [
        ("LWPOLYLINE", ["AcDbPolyline"]),
        ("POLYLINE", ["AcDbPolyline"]),  # the ActiveX spelling keeps working
        ("DIMENSION", ["AcDbRotatedDimension", "AcDbRadialDimension"]),
        ("INSERT", ["AcDbBlockReference"]),
        ("ARC", ["AcDbArc"]),
        ("CIRCLE", []),
    ],
)
async def test_select_by_type_accepts_dxf_names(monkeypatch, wanted, expect):
    from backends import com_backend
    from backends.com_backend import ComBackend

    ents = [
        _Ent("AcDbPolyline"),
        _Ent("AcDbRotatedDimension"),
        _Ent("AcDbRadialDimension"),
        _Ent("AcDbBlockReference"),
        _Ent("AcDbArc"),
    ]
    space = SimpleNamespace(Count=len(ents), Item=lambda i: ents[i])

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    backend = ComBackend()
    monkeypatch.setattr(backend, "_run", run_inline)
    monkeypatch.setattr(com_backend, "_acad_doc", lambda: object())
    monkeypatch.setattr(com_backend, "_msp", lambda: space)
    monkeypatch.setattr(com_backend, "_entity_info", lambda ent: ent.ObjectName)

    assert await backend.analysis_select_by_type(wanted) == expect
