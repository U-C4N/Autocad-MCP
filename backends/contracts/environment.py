"""Environment: documents, layer states, named views, UCS, application,
preferences, drawing properties and operator prompts (v1.6 track E).

``AutoCADBackend`` composes every contract in this package, so importing
``backends.base.AutoCADBackend`` is unchanged for callers.

Ownership: group V owns this module. The ``# ── properties ──`` block is
declared here and *implemented* by group C on both backends; the six
``@capability`` members are implemented on the COM backend only and refuse on
ezdxf with a key both capability maps declare.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from backends.capability import capability


class EnvironmentContract(ABC):
    # ── documents ────────────────────────────────────────────────────────────

    @abstractmethod
    async def document_list(self) -> list[dict]:
        """``[{"name", "path", "active": bool, "saved": bool, "entity_count"}]``."""
        ...

    @abstractmethod
    async def document_activate(self, name_or_path: str) -> dict:
        """Make one open document the target of every later call.
        → ``{"ok", "active", "previous"}``."""
        ...

    @abstractmethod
    async def document_close(
        self, name_or_path: str | None = None, save: bool = False, discard: bool = False
    ) -> dict:
        """Close one document (default: the active one).

        Unsaved and neither ``save`` nor ``discard`` → ``ValueError`` naming the
        document; ``save`` on a document that has never been saved →
        ``ValueError`` (no path to write, and the live engine would open a
        modal Save dialog). → ``{"ok", "closed", "saved": bool, "active": name|None}``.
        """
        ...

    # ── layer states (portable ACADMCP_LAYERSTATES XRECORDs, both engines) ──

    @abstractmethod
    async def layer_state_save(self, name: str, description: str | None = None) -> dict:
        """→ ``{"ok", "name", "layer_count", "replaced"}``."""
        ...

    @abstractmethod
    async def layer_state_restore(self, name: str, properties: list[str] | None = None) -> dict:
        """``properties`` ⊆ {"on","frozen","locked","color","linetype","lineweight","plot","current"};
        → ``{"ok", "name", "applied", "missing_layers", "new_layers"}``."""
        ...

    @abstractmethod
    async def layer_state_list(self) -> list[dict]:
        """→ ``[{"name", "description", "layer_count"}]``."""
        ...

    @abstractmethod
    async def layer_state_delete(self, name: str) -> dict: ...

    # ── named views ─────────────────────────────────────────────────────────

    @abstractmethod
    async def view_named_save(
        self, name: str, center=None, height: float | None = None, width: float | None = None
    ) -> dict:
        """→ ``{"ok", "name", "center": [x, y], "height", "width", "replaced"}``."""
        ...

    @abstractmethod
    async def view_named_restore(self, name: str) -> dict:
        """ezdxf: sets the ``*Active`` VPORT, ``"applied": "header_only"``."""
        ...

    @abstractmethod
    async def view_named_list(self) -> list[dict]: ...

    # ── UCS (stored and made current; tool coordinates stay WCS) ────────────

    @abstractmethod
    async def ucs_list(self) -> list[dict]:
        """→ ``[{"name", "origin", "x_axis", "y_axis", "current": bool}]`` + the implicit "world".

        Exactly one row is current. An *unnamed* current UCS (``UCS Origin`` /
        ``3P`` without saving) is a trailing row with ``name: None`` carrying
        its frame — "world" is current only when the frame is the WCS
        (WORLDUCS live, the ``$UCS*`` header headless), never because the
        name is empty.
        """
        ...

    @abstractmethod
    async def ucs_set(self, name: str, origin, x_axis, y_axis) -> dict:
        """non-orthogonal → ``ValueError`` with the measured angle."""
        ...

    @abstractmethod
    async def ucs_restore(self, name: str) -> dict:
        """``"world"`` resets."""
        ...

    # ── application, preferences, operator prompts (COM only) ───────────────

    @capability(  # noqa: B027 — @capability supplies the body
        "live_application",
        reason="only a live CAD application can be launched or attached",
    )
    async def system_launch(self, visible: bool = True, open_path: str | None = None) -> dict:
        """→ ``{"launched": bool, "attached": bool, "version", "document"}``."""
        ...

    @capability(  # noqa: B027 — @capability supplies the body
        "preferences",
        reason="AutoCAD preferences live in the running application, not in a file",
    )
    async def preferences_get(self, keys: list[str] | None = None) -> dict:
        """→ ``{"values": {key: value}, "read_only": [keys]}``."""
        ...

    @capability(  # noqa: B027 — @capability supplies the body
        "preferences",
        reason="AutoCAD preferences live in the running application, not in a file",
    )
    async def preferences_set(self, key: str, value) -> dict:
        """→ ``{"ok", "key", "old", "new"}``."""
        ...

    @capability(  # noqa: B027 — @capability supplies the body
        "interactive_prompt",
        reason="prompting the operator needs a live AutoCAD session",
    )
    async def user_pick_point(self, prompt: str) -> dict:
        """→ ``{"cancelled": bool, "x", "y"}`` | ``{"timed_out": True}``."""
        ...

    @capability(  # noqa: B027 — @capability supplies the body
        "interactive_prompt",
        reason="prompting the operator needs a live AutoCAD session",
    )
    async def user_select(self, prompt: str, mode: str = "single") -> dict:
        """→ ``{"cancelled", "handles": [...]}``."""
        ...

    @capability(  # noqa: B027 — @capability supplies the body
        "interactive_prompt",
        reason="prompting the operator needs a live AutoCAD session",
    )
    async def system_prompt_message(self, text: str) -> dict:
        """→ ``{"ok"}``."""
        ...

    # ── properties (declared here; implemented by group C on both engines) ───

    # TEMPORARY (track-e/environment worktree only — the merge agent deletes this
    # block and the two decorators below become @abstractmethod): until group C's
    # implementations land, declaring these abstract would stop both backends from
    # instantiating here. They refuse with a declared key rather than pretend.

    @capability("dwgprops", reason="drawing properties arrive with group C's merge")  # noqa: B027
    async def drawing_properties_get(self) -> dict:
        """→ ``{"summary": {"title","subject","author","keywords","comments"}
        (None each headless), "summary_available": bool, "custom": {k: v}}``."""
        ...

    @capability("dwgprops", reason="drawing properties arrive with group C's merge")  # noqa: B027
    async def drawing_properties_set(
        self, summary: dict | None = None, custom: dict | None = None
    ) -> dict:
        """ezdxf: ``summary`` with any non-None field → raise
        ``UnsupportedCapabilityError("dwgprops")``; ``custom`` → header
        custom_vars (replace keys given; value None deletes).
        → ``{"ok", "summary_written": [...], "custom_written": [...], "custom_deleted": [...]}``."""
        ...
