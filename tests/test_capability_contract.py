"""``@capability``: the escape hatch that is not allowed to become a hole.

M7 split a 1854-line ABC with 112 abstract methods into per-domain contracts.
The split makes contract changes navigable; it does not fix the reason contract
changes hurt, which is that every method is an `@abstractmethod`, so adding one
makes *both* backends fail at instantiation until both are written — and two
test modules construct `ComBackend` at import time, so a half-finished edit
takes the whole suite red.

`@capability` gives such a method a default. The default is the dangerous part,
so it is pinned here:

* It **raises** `UnsupportedCapabilityError`, never returns a quiet
  `{"ok": False}`. A method nobody implemented must be indistinguishable from a
  deliberate refusal, not from a soft failure — the v1.5.0 honesty pass exists
  because things that quietly did nothing looked like things that worked.
* Its capability key is **declared in both backends' capability maps**. Without
  this test the decorator would be a way to ship refusals that
  `system_capabilities` never mentions, which is worse than the build break it
  replaces: a client that pre-checks the feature map would be told the call is
  fine and then be refused.
* Overriding it is ordinary subclassing, so an implementing backend pays
  nothing.
"""

from __future__ import annotations

import inspect

import pytest

from backends.base import AutoCADBackend, UnsupportedCapabilityError
from backends.capability import capability_keys, is_capability_default
from backends.com_backend import ComBackend
from backends.ezdxf_backend import EzdxfBackend

# Marked per-test rather than module-wide: half of these are plain registry
# checks, and pytest-asyncio warns on a sync test carrying the mark.
asyncio_test = pytest.mark.asyncio


def _feature_keys(backend) -> set[str]:
    return set(backend.capabilities().to_dict()["features"])


# ── the registry is real ────────────────────────────────────────────────────


def test_at_least_one_method_uses_the_decorator():
    """An unused abstraction is dead weight; this keeps the gate meaningful."""
    assert capability_keys(), "no @capability methods — delete the decorator or use it"


def test_every_declared_key_exists_in_both_capability_maps():
    """The gate. A refusal the feature map never mentions is a lie by omission."""
    ezdxf_keys = _feature_keys(EzdxfBackend())
    com_keys = _feature_keys(ComBackend())
    for key in capability_keys():
        assert key in ezdxf_keys, f"@capability({key!r}) is undeclared on the ezdxf backend"
        assert key in com_keys, f"@capability({key!r}) is undeclared on the COM backend"


def test_the_contract_still_exposes_every_method_it_used_to():
    """The mixin split moved code; it must not have dropped any."""
    for names in capability_keys().values():
        for name in names:
            assert callable(getattr(AutoCADBackend, name, None)), f"{name} vanished in the split"


# ── the default behaves like a deliberate refusal ───────────────────────────


@asyncio_test
async def test_a_backend_that_does_not_implement_it_refuses_with_the_key():
    backend = ComBackend()
    assert is_capability_default(ComBackend, "entity_change_space"), (
        "fixture assumption: the COM backend inherits the default here"
    )

    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await backend.entity_change_space(["1A"], "2B")

    assert excinfo.value.capability == "chspace"
    payload = excinfo.value.to_dict()
    assert payload["ok"] is False
    assert payload["capability"] == "chspace"


@asyncio_test
async def test_the_refusal_names_the_backend_and_says_what_to_do_instead():
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await ComBackend().entity_change_space(["1A"], "2B")
    message = str(excinfo.value)
    assert "com" in message
    assert "ezdxf" in message, "a refusal that does not name the way out is half a refusal"


@asyncio_test
async def test_the_default_never_returns_a_soft_failure():
    """The design decision this file exists to defend.

    Returning ``{"ok": False, "capability": ...}`` would satisfy every caller's
    type expectations and silently mean "nothing happened".
    """
    try:
        result = await ComBackend().entity_change_space(["1A"], "2B")
    except UnsupportedCapabilityError:
        return
    pytest.fail(f"the default returned {result!r} instead of raising")


@asyncio_test
async def test_an_implementing_backend_is_unaffected(backend):
    assert not is_capability_default(EzdxfBackend, "entity_change_space")
    assert inspect.iscoroutinefunction(EzdxfBackend.entity_change_space)
    result = await backend.entity_change_space([], "nope")
    assert result["ok"] is False
    assert "error" in result, "the real implementation still does its own validation"
