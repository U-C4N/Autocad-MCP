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


@pytest.fixture(scope="session")
def state_root():
    """A session folder for journal state, outside every repository checkout.

    ``--basetemp`` often points inside this repository (``out/pytest/...``), and
    the journal refuses a state folder anywhere inside a checkout (B13), so the
    state folders live under the system temp folder instead.
    """
    import shutil
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="acadmcp-test-state-"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def outside_repo_dir(state_root):
    """A fresh, empty folder outside every repository checkout."""
    import uuid

    folder = state_root / uuid.uuid4().hex[:12]
    folder.mkdir()
    return folder


@pytest.fixture(autouse=True)
def private_state_dir(state_root, monkeypatch):
    """Every test gets its own durable journal folder (B13, 1.8.0).

    The server's lifespan opens ``<ACADMCP_STATE_DIR>/journal.sqlite3``; left
    at its default that is the developer's real ``%LOCALAPPDATA%`` store, and a
    client id one test used ("op-1") would replay into the next test. The
    folder is only named here - the journal creates it when a lifespan opens.
    """
    import uuid

    folder = state_root / "auto" / uuid.uuid4().hex[:12]
    monkeypatch.setenv("ACADMCP_STATE_DIR", str(folder))


@pytest.fixture
def full_catalog(request):
    """``DISCOVERY_MODE=off`` for a test that reads the whole catalog off ``list_tools``.

    Since 1.8.0 the server's default is ``search`` (plan 1.8-11): a client lists
    the synthetic tools and the pins, every tool stays callable. A test about a
    tool's advertised schema or annotations asks for the full catalog by
    name; the configured mode is put back afterwards.

    Applied to the ``server`` module the test module imported AND the one
    ``sys.modules`` holds now: ``tests/test_http_auth.py`` re-imports server.py
    and drops it from ``sys.modules``, so the two can be different objects (and
    a bare ``import server`` here would build a third).
    """
    import sys

    modules = []
    for candidate in (getattr(request.module, "server", None), sys.modules.get("server")):
        if hasattr(candidate, "_apply_discovery_mode") and candidate not in modules:
            modules.append(candidate)
    if not modules:
        import server

        modules.append(server)
    for module in modules:
        module._apply_discovery_mode("off")
    yield
    for module in modules:
        module._apply_discovery_mode()


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


class WithoutPrivateState:
    """The real backend with its ezdxf-only private attributes taken away.

    ``EzdxfBackend`` tracks the current tab in ``self._current_space``;
    ``ComBackend`` does not -- the live tab lives in AutoCAD
    (``doc.ActiveLayout``), so there is nothing of that name on it. Shared code
    that reaches for the private attribute therefore silently takes the
    ``getattr`` default on every live seat, and no ezdxf-backed test can see
    it. Wrapping the real backend (rather than writing a fake that owns members
    the real object never had) keeps every other call honest.
    """

    def __init__(self, inner, hide=("_current_space",)):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_hidden", frozenset(hide))

    def __getattr__(self, name):
        if name in self._hidden:
            raise AttributeError(f"{type(self._inner).__name__} has no attribute {name!r}")
        return getattr(self._inner, name)


@pytest_asyncio.fixture
async def backend_without_private_state(backend):
    """`backend`, minus the private attributes only the headless engine has."""
    return WithoutPrivateState(backend)
