"""The v9 task matrix (1.8.0): v8 plus the durability, economy, QA and security tasks.

Plan section 8: ``TASKS_V9 = TASKS_V8 + [...]``. Registered as ``"v9"`` in
``benchmarks.tasks_v8.MATRICES`` (the dict the runner reads) when this module
is imported - ``tasks_v8`` imports it at its end - without changing
``DEFAULT_MATRIX`` (1.8-25 moves the default). Each 1.8 item appends its task at
the end of ``NEW_TASKS_V9``.

* ``undo_across_restart`` (1.8-08, the rival's §4.2 claim): a real
  ``python server.py`` draws a line, saves, moves it, saves and is killed with
  ``Popen.kill()``; a new process opens the file and ``drawing_undo`` puts the
  line back where it was before the move - no undo setting in the
  environment. Its broken twin: a move after the last save, then a kill; the
  file is untouched and the reopen reports ``lost_since_save.count == 1``.
* ``atomic_batch_default`` (1.8-09, the rival's §4.3 probe): ``cad_batch``
  with ``[entity_move(L), entity_delete("DEAD")]`` and no ``on_error`` is
  refused ``unknown_handle`` with ``executed: 0`` and L's summary unchanged.
  Its run-time twin (the second step fails while it runs) is rolled back by
  the default ``on_error="auto"``, headless ``atomicity.verified: true``.
"""

from __future__ import annotations

from benchmarks.tasks_v8 import MATRICES, TASKS_V8, TaskSpec

#: Added in v1.8.0, in plan order; each item appends its own.
NEW_TASKS_V9: tuple[TaskSpec, ...] = (
    TaskSpec(
        "undo_across_restart",
        "durability",
        "Draw, save, move, save, kill the server; a new process opens the file and one "
        "drawing_undo puts the line back before the move. Unsaved twin: the file is "
        "untouched and lost_since_save.count is 1",
        1.0,
    ),
    TaskSpec(
        "atomic_batch_default",
        "durability",
        "Move a line and delete a handle that does not exist in one batch with no on_error: "
        "refused unknown_handle, nothing executed, the line unchanged. Run-time twin: the "
        "default rolls the move back, verified",
        1.0,
    ),
)

TASKS_V9: tuple[TaskSpec, ...] = TASKS_V8 + NEW_TASKS_V9

#: v9 tasks that drive the headless engine (or a headless server process) by
#: construction; on the live lane they are ``not_run``, never a zero.
HEADLESS_ONLY_V9: frozenset[str] = frozenset({"undo_across_restart", "atomic_batch_default"})

MATRICES["v9"] = TASKS_V9
