"""No sync test in the P&ID and pack modules carries the asyncio mark.

Backlog W2-T5-02, W3-T14-05, W3-T16-04 and W4-T18-01: a module-level
``pytestmark = pytest.mark.asyncio`` over a module that also holds plain
``def test_...`` functions makes pytest-asyncio warn once per sync test ("is
marked with '@pytest.mark.asyncio' but it is not an async function"). Those
modules mark their coroutine tests one by one instead; this reads the source,
so a new sync test under a module mark fails here rather than adding a warning.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
MODULES = sorted([*TESTS.glob("test_pid_*.py"), TESTS / "test_tool_packs.py"])


def _module_asyncio_mark(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            if "pytest.mark.asyncio" in ast.unparse(node.value):
                return True
    return False


def _sync_tests(tree: ast.Module) -> list[str]:
    out = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
            out.append(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            out += [
                f"{node.name}.{item.name}"
                for item in node.body
                if isinstance(item, ast.FunctionDef) and item.name.startswith("test")
            ]
    return out


def _marked_sync_tests(tree: ast.Module) -> list[str]:
    """Sync tests that carry the mark, from the module or from a decorator."""
    if _module_asyncio_mark(tree):
        return _sync_tests(tree)
    return [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any("pytest.mark.asyncio" in ast.unparse(d) for d in node.decorator_list)
    ]


def test_the_modules_exist():
    assert len(MODULES) >= 10, [m.name for m in MODULES]


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.name)
def test_no_sync_test_carries_the_asyncio_mark(module):
    tree = ast.parse(module.read_text(encoding="utf-8"))
    assert _marked_sync_tests(tree) == []


def test_the_reader_sees_a_marked_sync_test():
    source = "import pytest\npytestmark = pytest.mark.asyncio\ndef test_a():\n    pass\n"
    assert _marked_sync_tests(ast.parse(source)) == ["test_a"]
    decorated = "import pytest\n@pytest.mark.asyncio\ndef test_b():\n    pass\n"
    assert _marked_sync_tests(ast.parse(decorated)) == ["test_b"]
