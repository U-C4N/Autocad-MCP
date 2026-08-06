"""Shared fixtures for AutoCAD MCP tests."""

import pytest
import pytest_asyncio

from backends.ezdxf_backend import EzdxfBackend


@pytest.fixture(autouse=True)
def pin_headless_backend(request, monkeypatch):
    """Every test talks to the headless engine unless it says otherwise.

    This suite runs on developer machines that have AutoCAD installed, and a
    test that opens ``Client(server.mcp)`` without pinning would run the real
    lifespan: ``AUTOCAD_MCP_BACKEND`` unset means ``auto``, which on Windows
    means attaching to the operator's open, unsaved drawing and creating
    entities in it. That has actually happened here. Defaulting the whole suite
    to ``ezdxf`` is the only guard that covers tests nobody has written yet.

    Opt out with ``@pytest.mark.live_backend`` for a test that deliberately
    exercises backend selection.
    """
    if request.node.get_closest_marker("live_backend"):
        return
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_backend: test drives backend selection itself; do not pin AUTOCAD_MCP_BACKEND",
    )


@pytest_asyncio.fixture
async def backend():
    """Create an ezdxf backend with a fresh empty document."""
    b = EzdxfBackend()
    await b.connect()
    await b.drawing_new()
    yield b
    await b.disconnect()
