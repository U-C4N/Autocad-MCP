"""The durability docs name each engine's checkpoint points (1.8-08).

Headless, a save, an open, a new drawing and a batch boundary keep a durable
checkpoint (``backends/ezdxf_checkpoints.py``). Live, only
``checkpoint_create``, ``transaction_begin`` and the destructive tools do
(``backends/com_checkpoints.py``): no live save, open or new drawing takes one.
A sentence in the skill or the README that lists save / open / new drawing as
checkpoint points must therefore say *headless*, and the same document must say
the live engine does not, or a model on a live seat expects a restore point at
its last save that does not exist.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REFERENCE = Path("references") / "recovery-and-transactions.md"
DOCS = (
    ROOT / ".claude" / "skills" / "autocad-mcp-premium" / REFERENCE,
    ROOT / "plugin" / "skills" / "premium" / REFERENCE,
    ROOT / "README.md",
)

#: What the live engine checkpoints before: ``ComBackend.transaction_begin``
#: and ``server._checkpoint_before``'s destructive tools.
LIVE_POINTS = {"transaction_begin", "drawing_purge", "text_find_replace", "entity_delete_many"}

SAVE_OPEN_NEW = re.compile(r"save, (?:an )?open, (?:a )?new drawing", re.IGNORECASE)
LIVE_NEGATION = "not at a save, an open or a new drawing"


def _checkpoint_before_points(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in ("checkpoint_before", "_checkpoint_before"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
    return found


def test_the_live_engine_checkpoints_only_at_the_documented_points():
    points = _checkpoint_before_points(ROOT / "backends" / "com_backend.py")
    points |= _checkpoint_before_points(ROOT / "server.py")
    assert points == LIVE_POINTS
    assert not points & {"drawing_save", "drawing_save_as", "drawing_open", "drawing_new"}


def test_the_live_checkpoint_take_has_no_other_caller():
    tree = ast.parse((ROOT / "backends" / "com_checkpoints.py").read_text(encoding="utf-8"))
    callers = set()
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_cp_take"
                ):
                    callers.add(func.name)
    assert callers == {"checkpoint_before", "checkpoint_create"}


def test_the_docs_scope_save_open_new_checkpoints_to_the_headless_engine():
    for doc in DOCS:
        text = " ".join(doc.read_text(encoding="utf-8").split())
        matches = list(SAVE_OPEN_NEW.finditer(text))
        assert matches, f"{doc.name}: the checkpoint points are no longer listed"
        for match in matches:
            before = text[max(0, match.start() - 60) : match.start()].lower()
            assert "headless" in before, (
                f"{doc.relative_to(ROOT)}: '{text[match.start() - 60 : match.end()]}' "
                "lists save / open / new drawing as checkpoint points without saying headless"
            )
        assert LIVE_NEGATION in text, f"{doc.relative_to(ROOT)}: the live points are not stated"
        for point in ("checkpoint_create", "transaction_begin"):
            assert f"`{point}" in text
