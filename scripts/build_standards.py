"""Generate the B40 feature tables from their double entry, reproducibly.

    uv run --frozen python scripts/build_standards.py            # write _rows.py
    uv run --frozen python scripts/build_standards.py --check    # exit 1 on any drift

Reads ``engineering/mech/standards/data/<std>.a.json`` and ``<std>.b.json``
(DIN 471, DIN 472, DIN 509, DIN 332-1, ISO 3601-2), refuses unless both entries
agree value for value, are by two different named people once a table has rows,
carry a complete ``row_source`` and pass the physical invariants, and writes
``engineering/mech/standards/_rows.py`` - the only place the table modules read
rows from. ``--check`` changes nothing: it fails when the build is refused or
when ``_rows.py`` is not exactly the rendering of the data (the
``build_templates.py`` pattern). The procedure is in the docstring of
``engineering/mech/standards/transcription.py``.

Tables with no rows are listed as deferred to the next release by name: their
licensed copies have not been transcribed, and nothing is filled from memory or
from a secondary catalogue in their place.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engineering.mech.standards.transcription import (  # noqa: E402
    DATA_DIR,
    DEFERRED_RELEASE,
    GENERATED_PATH,
    TranscriptionError,
    build_tables,
    check,
    deferred_tables,
    render,
)


def _summary(tables: dict[str, dict]) -> str:
    filled = [f"{name} ({len(t['rows'])} rows)" for name, t in tables.items() if t["rows"]]
    deferred = deferred_tables(tables)
    parts = [f"filled: {', '.join(filled) if filled else 'none'}"]
    if deferred:
        parts.append(
            f"deferred to {DEFERRED_RELEASE} (no transcription from a licensed copy): "
            + ", ".join(deferred)
        )
    return "; ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--check", action="store_true", help="fail on drift, write nothing")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="the JSON entries")
    parser.add_argument("--out", type=Path, default=GENERATED_PATH, help="the generated module")
    args = parser.parse_args(argv)
    if args.check:
        problems = check(args.data_dir, args.out)
        if problems:
            print("standards tables drifted from scripts/build_standards.py:")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print(
            f"{args.out.name} is the rendering of the double entry; "
            + _summary(build_tables(args.data_dir))
        )
        return 0
    try:
        tables = build_tables(args.data_dir)
    except TranscriptionError as exc:
        print(exc)
        return 1
    args.out.write_text(render(tables), encoding="utf-8", newline="\n")
    print(f"wrote {args.out}; " + _summary(tables))
    return 0


if __name__ == "__main__":
    sys.exit(main())
