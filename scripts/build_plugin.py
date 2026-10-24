"""Write the plugin's copy of the premium skill, or prove it is current.

    uv run --frozen python scripts/build_plugin.py            # write plugin/skills/premium
    uv run --frozen python scripts/build_plugin.py --check    # exit 1 on any difference

The canonical skill is ``.claude/skills/autocad-mcp-premium/`` — the folder a
checkout of this repository loads as a project skill, and the one every edit
goes to. The Claude Code plugin under ``plugin/`` ships the same files as
``plugin/skills/premium/`` (invoked as ``/autocad-mcp-pro:premium``). That copy
is generated, never edited by hand: this script replaces it wholesale, and
``--check`` fails when a file is missing, extra or different — the
``build_templates.py --check`` pattern.

Files are compared as text with line endings normalised, because git's
``core.autocrlf`` rewrites both sides on checkout and a CRLF/LF difference is
not drift.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = ROOT / ".claude" / "skills" / "autocad-mcp-premium"
PLUGIN_SKILL = ROOT / "plugin" / "skills" / "premium"


def _files(root: Path) -> dict[str, Path]:
    """Every file under ``root`` keyed by its POSIX path relative to ``root``."""
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def _normalised(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def build(dest: Path = PLUGIN_SKILL, source: Path = SKILL_SOURCE) -> list[str]:
    """Replace ``dest`` with a copy of ``source``; return the files written."""
    if not (source / "SKILL.md").is_file():
        raise FileNotFoundError(f"no SKILL.md in {source}")
    if dest.exists():
        shutil.rmtree(dest)
    written = []
    for rel, path in _files(source).items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        written.append(rel)
    return written


def differences(dest: Path = PLUGIN_SKILL, source: Path = SKILL_SOURCE) -> list[str]:
    """What separates ``dest`` from a fresh build of ``source`` (empty when current)."""
    want = _files(source)
    have = _files(dest)
    drift = [f"missing: {rel}" for rel in sorted(want.keys() - have.keys())]
    drift += [f"extra: {rel}" for rel in sorted(have.keys() - want.keys())]
    drift += [
        f"differs: {rel}"
        for rel in sorted(want.keys() & have.keys())
        if _normalised(want[rel]) != _normalised(have[rel])
    ]
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when plugin/skills/premium differs from the canonical skill",
    )
    args = parser.parse_args(argv)
    rel_dest = PLUGIN_SKILL.relative_to(ROOT).as_posix()
    rel_source = SKILL_SOURCE.relative_to(ROOT).as_posix()
    if args.check:
        drift = differences()
        if drift:
            print(f"{rel_dest} is stale against {rel_source}:")
            for line in drift:
                print(f"  {line}")
            print("run: uv run --frozen python scripts/build_plugin.py")
            return 1
        print(f"{rel_dest} matches {rel_source} ({len(_files(SKILL_SOURCE))} files)")
        return 0
    written = build()
    print(f"wrote {len(written)} files to {rel_dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
