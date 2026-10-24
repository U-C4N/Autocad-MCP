"""The bound document on the live engine (v1.7.0, B11).

``_acad_doc()`` used to re-read ``ActiveDocument`` on every call, so a click on
another drawing tab between two calls sent the second call's writes into the
drawing the operator had just switched to. ``ComBackend`` now binds one
document - ``(Name, FullName)`` and a uuid4 ``document_id`` - when the session
creates, opens, activates or closes one (and at the first call that touches a
document), and every executor job checks AutoCAD's active document against it
before any document work. A mismatch is this refusal; nothing was written.

Operator edits are not counted in the revision. The document events that
could count them (``ObjectAdded`` / ``ObjectModified`` / ``ObjectErased``
through ``win32com.client.WithEvents``) were probed on AutoCAD 2026 on
2026-10-24 on a scratch ``Documents.Add()`` document: once subscribed, the very
next ``ModelSpace.AddLine`` from the subscribing STA thread raised
``RPC_E_SERVERFAULT`` (-2147417851) although the line was added, and every
later ``Documents.Add`` the probe tried on that seat was refused with "Filer
error" - still so on 2026-10-25 (with an explicit ``acadiso.dwt`` too), when
``Documents.Open`` of a freshly written DXF was refused with "Invalid
execution context". They are not used: the live revision moves on this server's own writes
only, and staleness on the live engine rests on the entity fingerprints of the
change-preview contract (Task 12).
"""

from __future__ import annotations


class ActiveDocumentChangedError(RuntimeError):
    """AutoCAD's active document is not the one this session is bound to.

    Raised inside the executor job, before any document work, so nothing was
    written - not into the bound document and not into the active one. Carries
    both documents as ``{"document_id", "name", "path"}`` (``active`` is None
    when AutoCAD has no document open).
    """

    kind = "active_document_changed"

    def __init__(self, bound: dict, active: dict | None):
        self.bound = dict(bound)
        self.active = None if active is None else dict(active)
        bound_name = self.bound.get("name") or "the bound document"
        if self.active is None:
            message = (
                f"AutoCAD has no document open, but this session is bound to {bound_name!r}; "
                f"nothing was written. Open it again and call document_activate({bound_name!r}), "
                "or drawing_new / drawing_open to bind another document."
            )
        else:
            active_name = self.active.get("name") or "another document"
            message = (
                f"AutoCAD's active document changed: this session is bound to {bound_name!r} "
                f"but {active_name!r} is active, so nothing was written. Switch AutoCAD back to "
                f"{bound_name!r}, or call document_activate({active_name!r}) to bind this "
                "session to it."
            )
        super().__init__(message)

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "kind": self.kind,
            "error": str(self),
            "bound": dict(self.bound),
            "active": None if self.active is None else dict(self.active),
        }
