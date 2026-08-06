"""T0.8-B — which engine a test talks to must not be a matter of luck.

``_make_backend`` read ``os.environ["AUTOCAD_MCP_BACKEND"]`` directly while
``config.settings.backend`` — the setting that *looks* like the control — had no
production reader at all. So ``monkeypatch.setattr(config.settings, "backend",
"ezdxf")`` was a no-op that read like a safety guard, and on a developer machine
with AutoCAD open an unguarded test would attach to the operator's live unsaved
drawing and start creating entities in it. That is not hypothetical: it happened
during this release's development.

The fix makes ``Settings.backend`` a property that reads the environment live,
and points ``_make_backend`` at it. One source of truth, and late binding is
preserved so ``monkeypatch.setenv`` — which four call sites and
``benchmarks/token_suite.py`` already use correctly — keeps working. Assigning to
``config.settings.backend`` now raises instead of quietly protecting nothing.
"""

from __future__ import annotations

import sys
import types

import pytest

import config
import server

pytestmark = pytest.mark.asyncio


class _ComBackendSentinel:
    """Stands in for the real COM backend and screams if anyone builds it."""

    constructed: list[str] = []

    def __init__(self, *args, **kwargs):
        _ComBackendSentinel.constructed.append("ComBackend() CONSTRUCTED")
        raise AssertionError("a test tried to construct the live COM backend")


@pytest.fixture
def com_sentinel(monkeypatch):
    """Replace backends.com_backend with a module that cannot connect anywhere."""
    _ComBackendSentinel.constructed.clear()
    fake = types.ModuleType("backends.com_backend")
    fake.ComBackend = _ComBackendSentinel
    monkeypatch.setitem(sys.modules, "backends.com_backend", fake)
    return _ComBackendSentinel


async def test_the_env_var_is_what_actually_selects_the_backend(monkeypatch, com_sentinel):
    from backends.ezdxf_backend import EzdxfBackend

    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    backend = await server._make_backend()

    assert isinstance(backend, EzdxfBackend)
    assert com_sentinel.constructed == [], "the COM backend must never have been touched"
    await backend.disconnect()


async def test_settings_backend_reports_what_make_backend_will_use(monkeypatch):
    """The setting must not be able to disagree with the selector."""
    for value in ("ezdxf", "com", "auto"):
        monkeypatch.setenv("AUTOCAD_MCP_BACKEND", value)
        assert config.settings.backend == value

    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "  EzDxF  ")
    assert config.settings.backend == "ezdxf", "must normalise like the selector does"

    monkeypatch.delenv("AUTOCAD_MCP_BACKEND", raising=False)
    assert config.settings.backend == "auto"


async def test_assigning_to_settings_backend_fails_loudly(monkeypatch):
    """The dead monkeypatch that made two tests *look* guarded must now raise.

    Silently accepting the assignment is the whole bug: the test passes, reads
    as safe, and talks to live AutoCAD anyway.
    """
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    with pytest.raises(AttributeError):
        config.settings.backend = "com"
    assert config.settings.backend == "ezdxf"


async def test_make_backend_reads_the_setting_not_the_environment_directly(monkeypatch):
    """One source of truth: patching the property must steer the selector.

    If ``_make_backend`` goes back to reading ``os.environ`` itself, this fails —
    which is the regression guard, because that direct read is what let the
    setting and the behaviour drift apart in the first place.
    """
    from backends.ezdxf_backend import EzdxfBackend

    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "com")
    monkeypatch.setattr(type(config.settings), "backend", property(lambda self: "ezdxf"))

    backend = await server._make_backend()
    assert isinstance(backend, EzdxfBackend)
    await backend.disconnect()
