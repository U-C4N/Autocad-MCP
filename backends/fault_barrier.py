"""Named fault-injection barriers (1.8-25) - test-only, inert in production.

``barrier(name, key=None)`` is a point in the server where a test may park the
process so that ``benchmarks/fault_inject.py`` can kill it *inside* a call
(``Popen.kill()``: TerminateProcess on Windows, SIGKILL elsewhere - no atexit
handler, no ``finally`` and no shutdown hook runs). The barriers in the tree:

* ``journal.after_begin`` (key: the operation id) - ``backends/journal_store.py``,
  right after a call's ``running`` row is committed (a kill there must leave an
  ``unknown_commit`` that the next server answers ``requires_reconcile``);
* ``save.before_replace`` (key: the target's file name) -
  ``backends/atomic_write.py``, after the tmp is written, fsynced and verified
  and the marker says ``verified``, before the target is replaced (a kill there
  must be ``finished`` by the next server's startup).

**Two locks, both needed.** A barrier parks only when BOTH hold:

1. ``ACADMCP_TEST_BARRIER`` names it - ``"<name>"`` (any key) or
   ``"<name>:<key>"``, several separated by commas; and
2. the marker module ``acadmcp_fault_marker`` is importable *and* carries
   ``MARKER == "acadmcp-fault-injection-test-only"``. It lives in
   ``tests/fault_marker/``, which is not a package of the project: the wheel's
   ``only-include`` (pyproject.toml) never ships it, ``scripts/check_dist_contents.py``
   refuses any ``tests/`` member in a wheel, and pytest never puts that folder
   on ``sys.path``. Only ``benchmarks/fault_inject.py`` adds it, to the
   ``PYTHONPATH`` of a child server it spawns with a barrier armed.

The variable alone - set by mistake on a production host - finds no marker:
the barrier logs one warning and does nothing. The marker alone names no
barrier. Unset, a barrier costs one environment lookup.

When it parks it writes ``<ACADMCP_TEST_BARRIER_DIR>/<name>.json``
(``{name, key, pid, at}``; ``.`` in the name kept) so the harness knows the
child is there, then sleeps forever.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("autocad_mcp.fault_barrier")

__all__ = [
    "BARRIERS",
    "BARRIER_DIR_ENV",
    "BARRIER_ENV",
    "MARKER_MODULE",
    "MARKER_VALUE",
    "armed",
    "barrier",
    "marker_present",
]

BARRIER_ENV = "ACADMCP_TEST_BARRIER"
BARRIER_DIR_ENV = "ACADMCP_TEST_BARRIER_DIR"
MARKER_MODULE = "acadmcp_fault_marker"
MARKER_VALUE = "acadmcp-fault-injection-test-only"

#: Every barrier the server code calls; ``armed`` refuses any other name, so a
#: typo in a test is an error rather than a barrier that never fires.
BARRIERS: frozenset[str] = frozenset({"journal.after_begin", "save.before_replace"})

_marker: bool | None = None
_warned = False


def marker_present() -> bool:
    """Whether the test-only marker module is importable here (cached per process)."""
    global _marker
    if _marker is None:
        try:
            import importlib

            module = importlib.import_module(MARKER_MODULE)
        except ImportError:
            _marker = False
        else:
            _marker = getattr(module, "MARKER", None) == MARKER_VALUE
    return _marker


def _entries(spec: str) -> list[tuple[str, str | None]]:
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, key = part.partition(":")
        out.append((name.strip(), key.strip() if sep else None))
    return out


def armed(name: str, environ=None) -> bool:
    """Whether ``ACADMCP_TEST_BARRIER`` names ``name`` (any key) - the
    variable only; ``barrier`` also requires the marker module."""
    if name not in BARRIERS:
        raise ValueError(f"unknown fault barrier {name!r}; known: {sorted(BARRIERS)}")
    spec = (os.environ if environ is None else environ).get(BARRIER_ENV)
    if not spec:
        return False
    return any(entry == name for entry, _key in _entries(spec))


def barrier(name: str, key: str | None = None) -> None:
    """Park this process forever if a test armed ``name`` (and ``key``); else return."""
    spec = os.environ.get(BARRIER_ENV)
    if not spec:
        return
    if name not in BARRIERS:
        raise ValueError(f"unknown fault barrier {name!r}; known: {sorted(BARRIERS)}")
    hit = any(
        entry == name and (wanted is None or wanted == key) for entry, wanted in _entries(spec)
    )
    if not hit:
        return
    if not marker_present():
        global _warned
        if not _warned:
            _warned = True
            log.warning(
                "%s=%s is set but the test-only marker module %s is not importable: "
                "fault barriers stay off",
                BARRIER_ENV,
                spec,
                MARKER_MODULE,
            )
        return
    folder = os.environ.get(BARRIER_DIR_ENV)
    if folder:
        try:
            target = Path(folder) / f"{name}.json"
            scratch = target.with_suffix(".part")
            scratch.write_text(
                json.dumps({"name": name, "key": key, "pid": os.getpid(), "at": time.time()}),
                encoding="utf-8",
            )
            os.replace(scratch, target)
        except OSError as exc:  # the harness then waits on its own deadline
            log.warning("fault barrier %s: signal file not written (%s)", name, exc)
    log.warning("fault barrier %s (key %r): parked until killed (test hook)", name, key)
    while True:
        time.sleep(1.0)
