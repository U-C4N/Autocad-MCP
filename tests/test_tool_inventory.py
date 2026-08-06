"""``docs/tool-inventory.json`` must be the live surface, not a stale copy of it.

The generated inventory replaces the oldest and most fragile release gate in
this repo: counting the literal text ``@mcp.tool(`` in server.py. Text counting
cannot see a group, a cost or a summary, and it miscounts the moment someone
writes the string in prose — which is why ``cad_tool``'s own docstring has to
spell its example without parentheses. This module holds the committed snapshot
equal to what ``scripts/generate_tool_inventory.py`` derives from the live
FastMCP component registry, so the JSON can be trusted as an input by anything
else (``tests/test_release_consistency.py`` reads its totals for the README
release-snapshot line).

Three properties are pinned:

  * **No drift** — the committed document equals the freshly built one, field by
    field, and byte for byte. Every failure names the regeneration command.
  * **Honest totals** — the counts are re-derived here from the registry by a
    path the generator does not share, so a generator that miscounts is caught
    by something other than itself.
  * **Usable rows** — every tool row carries the group/cost/summary a consumer
    reads, with the same validity rules the live ``@cad_tool`` cards obey.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import server
from discovery.serialize import MAX_SUMMARY_CHARS
from scripts import generate_tool_inventory as generator

INVENTORY_PATH = generator.INVENTORY_PATH

_HOW_TO_FIX = (
    f"\n\nThis file is generated. Regenerate it with:\n    {generator.REGENERATE_COMMAND}\n"
    "(and commit the result). Nothing here is meant to be hand-edited."
)


def _committed() -> dict[str, Any]:
    assert INVENTORY_PATH.exists(), f"{generator.INVENTORY_REL_PATH} is missing.{_HOW_TO_FIX}"
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


def _differences(committed: dict[str, Any], live: dict[str, Any]) -> list[str]:
    """Human-readable drift, rather than a 131-row dict diff nobody can read.

    Deliberately reports field *names* and identifiers only, never a summary
    string: tool names are ASCII, whereas summaries carry glyphs like the
    diameter sign, and a cp1254 console would turn such a failure into a
    UnicodeEncodeError instead of a readable diff.
    """
    problems: list[str] = []
    for key in ("schema_version", "package_version", "totals", "groups"):
        if committed.get(key) != live.get(key):
            problems.append(f"{key}: committed {committed.get(key)!r} != live {live.get(key)!r}")
    for section, id_key in (("tools", "name"), ("resources", "uri"), ("prompts", "name")):
        old = {row.get(id_key): row for row in committed.get(section, [])}
        new = {row.get(id_key): row for row in live.get(section, [])}
        for name in sorted(set(new) - set(old), key=str):
            problems.append(f"{section}: {name} is registered but absent from the committed file")
        for name in sorted(set(old) - set(new), key=str):
            problems.append(f"{section}: {name} is in the committed file but is not registered")
        for name in sorted(set(old) & set(new), key=str):
            if old[name] != new[name]:
                changed = sorted(
                    field
                    for field in set(old[name]) | set(new[name])
                    if old[name].get(field) != new[name].get(field)
                )
                problems.append(f"{section}: {name} changed fields {changed}")
    return problems


# ── the drift gate ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_committed_inventory_matches_the_live_surface():
    """The gate. Adding, renaming or re-costing a tool fails here until regenerated."""
    live = await generator.build_inventory()
    problems = _differences(_committed(), live)
    assert not problems, (
        f"{generator.INVENTORY_REL_PATH} no longer describes the live server:\n  "
        + "\n  ".join(problems)
        + _HOW_TO_FIX
    )


@pytest.mark.asyncio
async def test_committed_inventory_is_byte_identical_to_the_generator_output():
    """Formatting counts too: a hand-tweaked file must not survive review.

    Only fires when the *content* already matches (that check reports a readable
    diff first), so this failing means indentation/ordering/newlines drifted.
    """
    expected = generator.render(await generator.build_inventory())
    assert INVENTORY_PATH.read_text(encoding="utf-8") == expected, (
        f"{generator.INVENTORY_REL_PATH} has the right contents but not the "
        f"generator's exact formatting.{_HOW_TO_FIX}"
    )


# ── the totals, re-derived independently of the generator ───────────────────


@pytest.mark.asyncio
async def test_totals_agree_with_the_live_registry():
    """Counted straight off the registry keys, a path build_inventory does not use.

    Guards the generator itself: were its row builders to drop a component
    category, the drift gate alone would happily lock the loss in.
    """
    keys = list(server.mcp._local_provider._components)
    expected = {
        "tools": sum(1 for key in keys if key.startswith("tool:")),
        # The README's single "resources" number covers both registry prefixes.
        "resources": sum(1 for key in keys if key.startswith(("resource:", "template:"))),
        "prompts": sum(1 for key in keys if key.startswith("prompt:")),
        "groups": len(await server._tool_groups()),
    }
    assert _committed()["totals"] == expected


def test_totals_match_the_row_counts():
    """Internal consistency: the header numbers describe the body."""
    inventory = _committed()
    totals = inventory["totals"]
    assert totals["tools"] == len(inventory["tools"])
    assert totals["resources"] == len(inventory["resources"])
    assert totals["prompts"] == len(inventory["prompts"])
    assert totals["groups"] == len(inventory["groups"])


def test_group_counts_match_the_tool_rows():
    inventory = _committed()
    tallied: dict[str, int] = {}
    for row in inventory["tools"]:
        tallied[row["group"]] = tallied.get(row["group"], 0) + 1
    assert inventory["groups"] == dict(sorted(tallied.items()))
    assert sum(inventory["groups"].values()) == inventory["totals"]["tools"]


# ── the rows a consumer actually reads ──────────────────────────────────────


@pytest.mark.asyncio
async def test_every_tool_row_names_a_real_group():
    groups = await server._tool_groups()
    bad = sorted(row["name"] for row in _committed()["tools"] if row["group"] not in groups)
    assert not bad, f"tool rows filed under a group that does not exist: {bad}"


def test_every_tool_row_carries_a_usable_cost_and_summary():
    """The minimum contract of a row: name, group, cost, one-line summary."""
    bad: list[str] = []
    for row in _committed()["tools"]:
        summary = row.get("summary")
        if not row.get("name") or not row.get("group"):
            bad.append(f"{row.get('name')!r}: missing name/group")
        elif row.get("cost") not in server.CAD_TOOL_COSTS:
            bad.append(f"{row['name']}: cost {row.get('cost')!r} is not a CAD_TOOL_COSTS value")
        elif not isinstance(summary, str) or not summary.strip() or "\n" in summary:
            bad.append(f"{row['name']}: summary is empty or spans lines")
        elif len(summary) > MAX_SUMMARY_CHARS:
            bad.append(f"{row['name']}: summary is {len(summary)} chars, over the search budget")
    assert not bad, f"unusable tool rows: {bad}"


def test_tool_rows_are_sorted_by_name():
    """A generated file whose order wobbles produces unreviewable diffs."""
    names = [row["name"] for row in _committed()["tools"]]
    assert names == sorted(names)
    assert len(names) == len(set(names)), "duplicate tool rows"


def test_the_resource_template_is_recorded_as_templated():
    """FastMCP files templates under a separate prefix; the merge must not lose that."""
    templated = [row for row in _committed()["resources"] if row["templated"]]
    assert [row["uri"] for row in templated] == ["autocad://entities/{layer_name}"]
    assert templated[0]["parameters"] == ["layer_name"]


def test_prompt_rows_record_their_arguments():
    prompts = {row["name"]: row for row in _committed()["prompts"]}
    required = [a["name"] for a in prompts["prompt_quick_drawing"]["arguments"] if a["required"]]
    assert required == ["description"]


def test_package_version_is_the_pyproject_version():
    """The snapshot says which release's surface it describes."""
    assert _committed()["package_version"] == generator._pyproject_version()


# ── the regeneration path itself ────────────────────────────────────────────


def test_check_mode_accepts_the_committed_file():
    assert generator.main(["--check"]) == 0


def test_check_mode_reports_drift_and_names_the_regeneration_command(tmp_path, monkeypatch, capsys):
    """A developer who never opens this test file still gets told what to run."""
    stale = tmp_path / "tool-inventory.json"
    stale.write_text('{"totals": {"tools": 1}}\n', encoding="utf-8")
    monkeypatch.setattr(generator, "INVENTORY_PATH", stale)

    assert generator.main(["--check"]) == 1
    assert generator.REGENERATE_COMMAND in capsys.readouterr().out


def test_check_mode_reports_a_missing_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(generator, "INVENTORY_PATH", tmp_path / "absent.json")

    assert generator.main(["--check"]) == 1
    output = capsys.readouterr().out
    assert "is missing" in output
    assert generator.REGENERATE_COMMAND in output


def test_writing_then_checking_round_trips(tmp_path, monkeypatch):
    """Write mode produces exactly what check mode accepts, in a scratch location."""
    target = tmp_path / "nested" / "tool-inventory.json"
    monkeypatch.setattr(generator, "INVENTORY_PATH", target)

    assert generator.main([]) == 0
    assert generator.main(["--check"]) == 0
    # Committed as LF regardless of platform, so the file does not churn on Windows.
    assert b"\r\n" not in target.read_bytes()
