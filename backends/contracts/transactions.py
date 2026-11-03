"""Transactions.

Split out of the single 1854-line ``AutoCADBackend`` ABC in v1.5.0 (M7).
``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TransactionContract(ABC):
    @abstractmethod
    async def transaction_begin(self) -> dict: ...

    @abstractmethod
    async def transaction_commit(self) -> dict: ...

    @abstractmethod
    async def transaction_rollback(self) -> dict: ...

    # ── durable checkpoints (1.8-08, ``backends/checkpoints.py``) ──

    @abstractmethod
    async def checkpoint_create(self, label: str | None = None) -> dict:
        """A checkpoint of the active document now, under ``label``."""

    @abstractmethod
    async def checkpoint_list(self, limit: int = 10) -> dict:
        """The active document's checkpoints, newest first:
        ``{checkpoints: [{cp, seq, tool, rev, kb, label, evicted}], ...}``."""

    @abstractmethod
    async def checkpoint_restore(self, checkpoint, mode: str = "in_place") -> dict:
        """Restore a checkpoint (``cp-<seq>``, a seq or a label).

        ``in_place`` replaces the active document's contents and is itself
        undoable; ``new_document`` opens the checkpoint as ``<stem>-cp<seq>``
        and binds it. The live engine refuses ``in_place`` with capability
        ``checkpoint_restore_in_place``.
        """

    def latest_checkpoint(self) -> str | None:
        """The active document's newest checkpoint id, read without a backend call."""
        return None

    async def checkpoint_before(self, tool: str) -> str | None:
        """A checkpoint before a destructive tool; the id, or None when none is taken.

        The live engine exports one (``Document.Export``). The headless engine
        takes none here: its checkpoints are anchored on saves and on the
        transaction snapshot a batch already writes, at no per-call cost.
        """
        return None

    async def checkpoint_anchor_new(self) -> None:
        """After the ``drawing_new`` tool's bootstrap: S0 is the drawing it hands back.

        The headless engine re-anchors its pending S0 on the bootstrapped
        drawing (the blank one never reached a caller). The live engine keeps
        no S0, so the default does nothing.
        """
        return None

    def transaction_depth(self) -> int | None:
        """How many checkpoints are open now, read without a backend call.

        ``cad_batch`` reads it before opening its own checkpoint and after
        closing one, so a checkpoint a step opened above the batch's is closed
        with it instead of leaving the batch's open. ``None`` means the backend
        cannot say, and nothing is unwound on a guess.
        """
        return None
