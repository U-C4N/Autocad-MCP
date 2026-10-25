"""Write the plugin's copy of the premium skill, or prove it is current.

    uv run --frozen python scripts/build_plugin.py            # write plugin/skills/premium
    uv run --frozen python scripts/build_plugin.py --check    # exit 1 on any difference
    uv run --frozen --all-extras python scripts/build_plugin.py --variant skill-only --out out/plugin-skill-only

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

``--variant full|skill-only|server-only --out <folder>`` writes one arm of the
skill's A/B evaluation (spec §6 Task 22, ``benchmarks/skill_eval/README.md``):
the plugin's manifest and eval cases, the skill whole (``full``), its
``SKILL.md`` alone (``skill-only``) or no skill (``server-only``), and an
``.mcp.json`` whose server is this checkout's ``server.py`` run by the
interpreter that ran the script, on the headless engine. That server config is
written into the variant because ``claude plugin eval`` hands its runs only the
operator's ``EVAL_*`` variables (CLI 2.1.310): ``AUTOCAD_MCP_SOURCE`` and
``AUTOCAD_MCP_BACKEND`` never reach the shipped ``.mcp.json``, whose server
would then be the PyPI release on ``auto`` - the live AutoCAD on a seat that
runs one. A variant is generated, never committed: the folder must be outside
the repository or under its git-ignored ``out/``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = ROOT / ".claude" / "skills" / "autocad-mcp-premium"
PLUGIN = ROOT / "plugin"
PLUGIN_SKILL = PLUGIN / "skills" / "premium"
#: The evaluation arms ``--variant`` writes: which skill files each one carries
#: (None: every file of the canonical skill).
VARIANTS: dict[str, tuple[str, ...] | None] = {
    "full": None,
    "skill-only": ("SKILL.md",),
    "server-only": (),
}
#: What ``claude plugin eval`` writes into an eval folder; never part of a variant.
_EVAL_OUTPUT = ("results", "mocks")
#: Written into every variant: only a folder carrying it is ever replaced.
VARIANT_MARKER = ".acadmcp-variant"


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


def variant_target_ok(out: Path) -> bool:
    """True when ``out`` is outside the repository or under its git-ignored ``out/``."""
    target = Path(out).resolve()
    if not target.is_relative_to(ROOT):
        return True
    return target.is_relative_to(ROOT / "out") and target != (ROOT / "out")


def build_variant(variant: str, out: Path, *, python: str | None = None) -> list[str]:
    """Write the evaluation arm ``variant`` to ``out`` (replaced); return the files written.

    The arm carries the plugin's manifest (its name is part of every tool name
    a grader checks), the eval cases without any run output, the skill files
    ``VARIANTS`` names and an ``.mcp.json`` that runs this checkout headless
    under the shipped server's name, with ``python`` (default: this interpreter).
    """
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; one of {', '.join(VARIANTS)}")
    out = Path(out)
    if not variant_target_ok(out):
        raise ValueError(
            f"{out}: a variant is generated outside git - write it outside the repository "
            f"or under {(ROOT / 'out').as_posix()}"
        )
    if out.exists():
        if any(out.iterdir()) and not (out / VARIANT_MARKER).is_file():
            raise ValueError(
                f"{out}: not empty and not a variant this script wrote (no {VARIANT_MARKER}); "
                "refusing to replace it"
            )
        shutil.rmtree(out)
    written: list[str] = []

    def copy(source: Path, rel: str) -> None:
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        written.append(rel)

    copy(PLUGIN / ".claude-plugin" / "plugin.json", ".claude-plugin/plugin.json")
    for rel, path in _files(PLUGIN / "evals").items():
        if rel.split("/", 1)[0] not in _EVAL_OUTPUT:
            copy(path, f"evals/{rel}")
    keep = VARIANTS[variant]
    for rel, path in _files(SKILL_SOURCE).items():
        if keep is None or rel in keep:
            copy(path, f"skills/premium/{rel}")
    shipped = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    servers = {
        name: {
            "command": Path(python or sys.executable).as_posix(),
            "args": [(ROOT / "server.py").as_posix()],
            "env": {"AUTOCAD_MCP_BACKEND": "ezdxf"},
        }
        for name in shipped
    }
    (out / ".mcp.json").write_text(
        json.dumps({"mcpServers": servers}, indent=2) + "\n", encoding="utf-8"
    )
    written.append(".mcp.json")
    (out / VARIANT_MARKER).write_text(variant + "\n", encoding="utf-8")
    written.append(VARIANT_MARKER)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when plugin/skills/premium differs from the canonical skill",
    )
    parser.add_argument(
        "--variant",
        choices=sorted(VARIANTS),
        help="write an evaluation arm instead (needs --out; see benchmarks/skill_eval)",
    )
    parser.add_argument("--out", type=Path, help="the folder a --variant is written to")
    args = parser.parse_args(argv)
    if args.variant:
        if args.out is None:
            parser.error("--variant needs --out <folder> (outside git, e.g. out/plugin-full)")
        try:
            written = build_variant(args.variant, args.out)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"wrote the {args.variant} variant ({len(written)} files) to {args.out}")
        return 0
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
