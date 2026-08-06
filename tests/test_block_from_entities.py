"""M5 — `block_create_from_entities` on both engines.

The live backend refused this outright ("not supported in COM backend. Use
system_run_command with _BLOCK instead"), which pushed users at the free-text
escape hatch for something ActiveX does directly: `Blocks.Add` makes the
definition and `Document.CopyObjects` moves entities into it. Meanwhile the
tool's own docstring said the opposite — "works by using AutoCAD's BLOCK command
(COM backend only)" — so the one backend that could not do it was named as the
one that could.

Both engines also used to swallow handles they could not resolve: pass five
handles with two typos and you got `entity_count: 3` and no mention of the two.
The count is now accompanied by what was skipped.

The COM path is unit-tested against a fake ActiveX object. This machine has a
live AutoCAD and connecting to it during development is forbidden, so that path
ships verified at this level only — the CHANGELOG says so rather than implying
hardware coverage.
"""

from __future__ import annotations

import types

import pytest

from backends.ezdxf_backend import EzdxfBackend

pytestmark = pytest.mark.asyncio


# ── headless ────────────────────────────────────────────────────────────────


async def test_ezdxf_builds_the_definition_from_real_entities(backend):
    line = await backend.entity_create_line(0, 0, 10, 0)
    circle = await backend.entity_create_circle(5, 5, 2)

    result = await backend.block_create_from_entities("BRACKET", [line.handle, circle.handle])

    assert result["ok"] is True
    assert result["entity_count"] == 2
    assert result["skipped"] == []
    block = backend._doc.blocks.get("BRACKET")
    assert sorted(e.dxftype() for e in block) == ["CIRCLE", "LINE"]


async def test_ezdxf_reports_handles_it_could_not_resolve(backend):
    line = await backend.entity_create_line(0, 0, 10, 0)

    result = await backend.block_create_from_entities("PARTIAL", [line.handle, "NOSUCH", "ALSONO"])

    assert result["entity_count"] == 1
    assert result["skipped"] == ["NOSUCH", "ALSONO"], (
        "a count with no mention of what was dropped is the same shape of lie "
        "as the audit reporting no fixes"
    )


async def test_ezdxf_refuses_a_block_with_nothing_in_it(backend):
    with pytest.raises(RuntimeError, match="NOSUCH"):
        await backend.block_create_from_entities("EMPTY", ["NOSUCH"])
    assert "EMPTY" not in backend._doc.blocks, "a failed call must leave no stub behind"


async def test_ezdxf_honours_the_base_point(backend):
    line = await backend.entity_create_line(0, 0, 10, 0)
    await backend.block_create_from_entities("BASED", [line.handle], base_x=3.0, base_y=4.0)

    block = backend._doc.blocks.get("BASED")
    assert tuple(block.block.dxf.base_point)[:2] == (3.0, 4.0)


# ── live backend, against a fake ActiveX surface ────────────────────────────


class _FakeBlock:
    def __init__(self, name):
        self.Name = name


class _FakeDocument:
    """The three ActiveX members the implementation is allowed to touch."""

    def __init__(self, handles):
        self._handles = handles
        self.Blocks = types.SimpleNamespace(Add=self._add_block)
        self.copied = None
        self.added = None

    def _add_block(self, base_point, name):
        self.added = (tuple(base_point.value), name)
        return _FakeBlock(name)

    def HandleToObject(self, handle):
        if handle not in self._handles:
            raise RuntimeError(f"no object with handle {handle}")
        return types.SimpleNamespace(Handle=handle, ObjectName="AcDbLine")

    def CopyObjects(self, objects, owner, *rest):
        self.copied = ([o.Handle for o in objects.value], owner.Name)


@pytest.fixture
def com_backend(monkeypatch):
    """A ComBackend whose document is a fake — no COM connection is made."""
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    document = _FakeDocument({"2F", "30"})
    monkeypatch.setattr(module, "_acad_doc", lambda: document)
    monkeypatch.setattr(module, "_regen", lambda: None)
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, document


async def test_com_creates_the_definition_and_copies_into_it(com_backend):
    backend, document = com_backend

    result = await backend.block_create_from_entities("BRACKET", ["2F", "30"], 3.0, 4.0)

    assert result["ok"] is True
    assert result["entity_count"] == 2
    assert result["skipped"] == []
    assert document.added == ((3.0, 4.0, 0.0), "BRACKET"), "base point must reach Blocks.Add"
    assert document.copied == (["2F", "30"], "BRACKET")


async def test_com_reports_handles_it_could_not_resolve(com_backend):
    backend, document = com_backend

    result = await backend.block_create_from_entities("PARTIAL", ["2F", "NOSUCH"])

    assert result["entity_count"] == 1
    assert result["skipped"] == ["NOSUCH"]
    assert document.copied == (["2F"], "PARTIAL")


async def test_com_refuses_a_block_with_nothing_in_it(com_backend):
    backend, document = com_backend

    with pytest.raises(RuntimeError, match="NOSUCH"):
        await backend.block_create_from_entities("EMPTY", ["NOSUCH"])

    assert document.copied is None, "nothing may be copied when nothing resolved"


async def test_both_backends_return_the_same_key_set(backend, com_backend):
    com, _ = com_backend
    line = await backend.entity_create_line(0, 0, 10, 0)

    headless = await backend.block_create_from_entities("A", [line.handle])
    live = await com.block_create_from_entities("A", ["2F"])

    assert headless.keys() == live.keys()


async def test_ezdxf_still_declares_no_capability_gap_here():
    """Both engines can do this now, so it must not look like a boundary."""
    features = EzdxfBackend().capabilities().features
    assert "block_definition" not in features, (
        "a capability key would imply one engine cannot; both can"
    )
