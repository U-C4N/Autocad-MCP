"""Optional contract methods: declared once, refused loudly, gated by a test.

The backend contract is large (112 abstract methods before this file existed,
and the plan adds ~39 more). Every one of them is an `@abstractmethod`, which
means adding a single method to the contract makes **both** backends fail at
instantiation until both are written — and two test modules construct
`ComBackend` at import time, so the whole suite goes red on a half-finished
edit. That is a real cost paid on every contract change.

The obvious fix is to give new methods a default body. The obvious default is
wrong: returning ``{"ok": False, "capability": ...}`` from a method nobody
implemented produces exactly the quiet "this didn't work and nothing told you"
that the v1.5.0 honesty pass spent a milestone deleting. A default that lies
quietly is worse than a build break.

So `@capability` gives a default that **raises**
`UnsupportedCapabilityError` — the same typed refusal the deliberate
boundaries raise, carrying the same `capability` key, surfaced by the same
`CapabilityRefusalMiddleware`, and indistinguishable to a client from a
hand-written refusal. Three properties follow:

* An unimplemented method refuses instead of pretending. It cannot silently
  succeed and it cannot silently do nothing.
* The refusal is *declared*: `test_capability_contract.py` fails if a key used here is
  missing from either backend's `CapabilityMap`, so `system_capabilities` can
  never disagree with what a call actually does. This is the part that keeps
  the escape hatch honest — without it, `@capability` would just be a way to
  ship undeclared holes.
* Overriding is ordinary subclassing. A backend that implements the method
  simply defines it; nothing needs to be un-registered.

`@abstractmethod` remains the default for anything both backends must have.
Reach for `@capability` only when a method is genuinely optional — an engine
boundary (no ACIS kernel headlessly) rather than an unfinished chore.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

__all__ = [
    "UnsupportedCapabilityError",
    "capability",
    "capability_keys",
    "is_capability_default",
]


# ---------------------------------------------------------------------------
# The refusal vocabulary
#
# This lives here rather than in ``backends.base`` because the contracts import
# the decorator and ``base`` imports the contracts — putting the exception in
# ``base`` makes that a cycle. ``backends.base`` re-exports both names, so
# ``from backends.base import UnsupportedCapabilityError`` is unchanged for the
# rest of the repo.
#
# There is exactly one authoring rule: a backend that cannot do a thing RAISES
# ``UnsupportedCapabilityError(capability, message)``, where ``capability`` is a
# key of ``capabilities().features`` so a client can pre-check the same boundary
# through ``system_capabilities`` instead of parsing English. Tools never catch
# it and never hand-build refusal payloads — one middleware turns it into the
# wire payload, and ``cad_batch`` turns it into a step row. One class object,
# defined once: two look-alike types would silently break every ``isinstance``
# classifier.
#
# The ``{"ok": False, "error": ...}`` returned-dict shape stays legal only for
# soft failures that are not capability boundaries ("Layout not found", "No
# active transaction").
# ---------------------------------------------------------------------------


def _unsupported(error: str, capability_key: str) -> dict:
    """Build a backend's machine-readable "this backend can't" payload."""
    return {"ok": False, "error": error, "capability": capability_key}


class UnsupportedCapabilityError(RuntimeError):
    """Raised when a call needs a capability this backend does not have.

    Stays a plain ``RuntimeError``: rebasing it on ``FastMCPError`` was measured
    and changes nothing a client can observe, because the refusal is carried by
    ``ToolResult(structured_content=..., is_error=True)`` rather than by the
    exception class. ``to_dict()`` keeps the ``{ok, error, capability}`` payload
    attached to the raise so the boundary survives the ``__cause__`` chain.
    """

    def __init__(self, capability: str, error: str):
        super().__init__(error)
        self.capability = capability

    def to_dict(self) -> dict:
        return _unsupported(str(self), self.capability)


#: ``{capability_key: (method_name, ...)}`` for every method declared optional.
#: Populated at import by the decorator and read by the parity gate.
_REGISTRY: dict[str, set[str]] = {}


def capability(key: str, *, reason: str = "") -> Callable[[Callable], Callable]:
    """Mark a contract method optional, defaulting to a typed refusal.

    Args:
        key: capability name; must appear in **both** backends' capability maps.
        reason: appended to the refusal message. Say what the caller can do
            instead, not merely that they cannot do this.
    """

    def decorate(func: Callable) -> Callable:
        @functools.wraps(func)
        async def default(self, *args: Any, **kwargs: Any):
            message = f"{func.__name__}: not supported by the {self.name} backend"
            raise UnsupportedCapabilityError(key, f"{message}. {reason}".rstrip())

        default.__capability__ = key
        default.__is_capability_default__ = True
        _REGISTRY.setdefault(key, set()).add(func.__name__)
        return default

    return decorate


def capability_keys() -> dict[str, set[str]]:
    """Every optional-capability key and the methods declared under it."""
    return {key: set(names) for key, names in _REGISTRY.items()}


def is_capability_default(owner: type, method_name: str) -> bool:
    """True when ``owner`` inherited the refusing default rather than implementing it."""
    attribute = getattr(owner, method_name, None)
    return bool(getattr(attribute, "__is_capability_default__", False))
