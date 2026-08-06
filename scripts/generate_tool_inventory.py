"""Generate ``docs/tool-inventory.json`` — a machine-readable snapshot of the surface.

Everything here is derived from the **live component registry**, never by
parsing server.py's text. That is the whole point: the release gates used to
count the literal string ``@mcp.tool(`` in server.py, which made that string
load-bearing anywhere in the file (writing it in a docstring inflated the
count, so the one docstring that needed the example had to spell it without its
parentheses). A snapshot taken from the registry cannot be fooled by prose, and
it carries far more than a count — every tool's group, cost and summary, plus
the resource and prompt inventories.

The snapshot is read back by ``tests/test_tool_inventory.py`` (drift gate) and
by ``tests/test_release_consistency.py`` (README release-snapshot counts).

Usage::

    python scripts/generate_tool_inventory.py            # write the file
    python scripts/generate_tool_inventory.py --check     # exit 1 if it drifted

``--check`` is the same comparison the test makes, for use outside pytest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

# Make the project root importable when this script is run from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

#: Where the generated snapshot lives (docs, not a shipped package file — it is
#: deliberately absent from the wheel's ``only-include`` and from the Docker
#: image, so the Dockerfile/wheel parity gate is unaffected).
INVENTORY_PATH = _PROJECT_ROOT / "docs" / "tool-inventory.json"

#: Path as written in messages, so a failure reads the same on every platform.
INVENTORY_REL_PATH = "docs/tool-inventory.json"

#: The one string every failure message must contain.
REGENERATE_COMMAND = "python scripts/generate_tool_inventory.py"

#: Bump when the document's shape changes (not when its contents change).
SCHEMA_VERSION = 1

_FILE_COMMENT = (
    "GENERATED FILE - do not edit by hand. Derived from the live FastMCP "
    f"component registry; regenerate with `{REGENERATE_COMMAND}`."
)

# Ordered so a diff of the annotations block is stable.
_ANNOTATION_FIELDS = (
    "title",
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
)


def _pyproject_version() -> str:
    """The version every other release gate keys off (pyproject is the source)."""
    with (_PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def _components() -> dict[str, Any]:
    """The untransformed local component registry — tools, resources, prompts.

    ``server._registered_tools()`` reads the same mapping for the tool half; this
    is the resource/prompt half of it. Reading the registry (rather than
    ``list_resources`` / ``list_prompts``) keeps the snapshot below every
    transform layer, so what gets committed is what is *registered*, not what
    some discovery mode happens to advertise.
    """
    import server

    return server.mcp._local_provider._components


def _annotations(tool: Any) -> dict[str, Any]:
    """The tool's own MCP annotations, minus the fields it left unset."""
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return {}
    return {
        field: getattr(annotations, field)
        for field in _ANNOTATION_FIELDS
        if getattr(annotations, field, None) is not None
    }


def _tool_rows(tools: list[Any], groups: dict[str, list[str]]) -> list[dict[str, Any]]:
    """One row per registered tool, sorted by name.

    ``cost`` and ``summary`` come from the ``meta["cad"]`` card that ``@cad_tool``
    writes; ``group`` from ``server._tool_groups()``, i.e. from the tool's own
    tags. Aliases are deliberately *not* copied here — ``discovery/aliases.py``
    is their single source of truth and already has its own coverage gate.
    """
    group_of = {name: label for label, names in groups.items() for name in names}
    rows = []
    for tool in tools:
        cad = (getattr(tool, "meta", None) or {}).get("cad") or {}
        rows.append(
            {
                "name": tool.name,
                "group": group_of.get(tool.name),
                "cost": cad.get("cost"),
                "summary": cad.get("summary"),
                "tags": sorted(getattr(tool, "tags", None) or ()),
                "annotations": _annotations(tool),
            }
        )
    return sorted(rows, key=lambda row: row["name"])


def _resource_rows(components: dict[str, Any]) -> list[dict[str, Any]]:
    """Plain resources and resource templates in one uniformly-shaped list.

    FastMCP files them under two registry prefixes (``resource:`` and
    ``template:``); the README counts them as one number, so they are merged and
    told apart by ``templated``.
    """
    rows = []
    for key, component in components.items():
        if key.startswith("resource:"):
            uri, templated, parameters = str(component.uri), False, []
        elif key.startswith("template:"):
            uri, templated = str(component.uri_template), True
            parameters = sorted(
                (getattr(component, "parameters", None) or {}).get("properties", {})
            )
        else:
            continue
        rows.append(
            {
                "uri": uri,
                "name": component.name,
                "description": component.description or "",
                "mime_type": component.mime_type,
                "tags": sorted(getattr(component, "tags", None) or ()),
                "templated": templated,
                "parameters": parameters,
            }
        )
    return sorted(rows, key=lambda row: row["uri"])


def _prompt_rows(components: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, component in components.items():
        if not key.startswith("prompt:"):
            continue
        rows.append(
            {
                "name": component.name,
                "description": component.description or "",
                "tags": sorted(getattr(component, "tags", None) or ()),
                "arguments": [
                    {"name": argument.name, "required": bool(argument.required)}
                    for argument in (component.arguments or [])
                ],
            }
        )
    return sorted(rows, key=lambda row: row["name"])


async def build_inventory() -> dict[str, Any]:
    """Build the snapshot document from the live registry.

    Raises rather than emitting an empty section: committing an inventory that
    silently lost every tool would be worse than a broken generator, because the
    drift gate would then happily lock the emptiness in.
    """
    import server

    components = _components()
    tools = await server._registered_tools()
    groups = await server._tool_groups()
    tool_rows = _tool_rows(tools, groups)
    resource_rows = _resource_rows(components)
    prompt_rows = _prompt_rows(components)
    for label, rows in (
        ("tools", tool_rows),
        ("resources", resource_rows),
        ("prompts", prompt_rows),
    ):
        if not rows:
            raise RuntimeError(
                f"refusing to write an inventory with zero {label}: the component "
                "registry looks unreadable (FastMCP layout change?)"
            )
    return {
        "_comment": _FILE_COMMENT,
        "schema_version": SCHEMA_VERSION,
        "package_version": _pyproject_version(),
        "totals": {
            "tools": len(tool_rows),
            "resources": len(resource_rows),
            "prompts": len(prompt_rows),
            "groups": len(groups),
        },
        "groups": {label: len(names) for label, names in sorted(groups.items())},
        "tools": tool_rows,
        "resources": resource_rows,
        "prompts": prompt_rows,
    }


def render(inventory: dict[str, Any]) -> str:
    """Serialize deterministically: same registry in, byte-identical text out."""
    return json.dumps(inventory, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate docs/tool-inventory.json from the live MCP registry."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the committed file has drifted from the live surface",
    )
    args = parser.parse_args(argv)

    text = render(asyncio.run(build_inventory()))

    if args.check:
        current = INVENTORY_PATH.read_text(encoding="utf-8") if INVENTORY_PATH.exists() else None
        if current == text:
            print(f"ok: {INVENTORY_REL_PATH} matches the live surface")
            return 0
        reason = "is missing" if current is None else "has drifted from the live surface"
        print(f"DRIFT: {INVENTORY_REL_PATH} {reason}")
        print(f"       regenerate it with: {REGENERATE_COMMAND}")
        return 1

    INVENTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    INVENTORY_PATH.write_text(text, encoding="utf-8", newline="\n")
    inventory = json.loads(text)
    totals = inventory["totals"]
    print(
        f"wrote {INVENTORY_REL_PATH}: {totals['tools']} tools, "
        f"{totals['resources']} resources, {totals['prompts']} prompts, "
        f"{totals['groups']} groups"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
