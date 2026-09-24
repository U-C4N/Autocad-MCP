"""The async layer of track H's takeoffs: read the drawings, run the pure engine, write the workbook.

The only module of the takeoffs that touches a backend, and it only reads:
`take_snapshot` gives one bulk read of each drawing (a file, or the current
document), the pure modules compute in a worker thread so a 35,000-entity
P&ID does not stall the server, and the workbook is a file beside the path the
caller names. The drawings themselves are never modified.
"""

from __future__ import annotations

import asyncio
import math

from engineering.understand.network import build_network
from engineering.understand.report import check_lang, write_workbook
from engineering.understand.snapshot import take_snapshot
from engineering.understand.takeoff import check_pipe_options, pipe_rows

__all__ = ["run_pipe_takeoff"]


def _check_tol(tol) -> float:
    try:
        value = float(tol)
    except (TypeError, ValueError):
        raise ValueError(f"tol: expected a number, got {tol!r}") from None
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"tol: must be a finite number greater than zero, got {tol!r}")
    return value


async def run_pipe_takeoff(
    backend,
    *,
    pid_path: str | None = None,
    layout_path: str | None = None,
    output: str | None = None,
    lang: str = "en",
    services=None,
    allowance: float = 0.20,
    vertical_allowance: float = 0.0,
    length_source: str = "auto",
    force: bool = False,
    layers=None,
    tol: float = 1.0,
) -> dict:
    """`pipe_rows` over a P&ID and an optional layout, plus the workbook when `output` is named.

    Every option is checked before a drawing is read. `pid_path=None` reads the
    current document as the P&ID. The result is `pipe_rows`'s dict with
    ``files`` added: `write_workbook`'s answer, or None when no `output` was named.
    """
    check_lang(lang)
    options = check_pipe_options(
        services=services,
        allowance=allowance,
        vertical_allowance=vertical_allowance,
        length_source=length_source,
    )
    tol = _check_tol(tol)
    if length_source == "layout" and not layout_path:
        raise ValueError("length_source='layout' needs a layout drawing; none was given")
    pid = await take_snapshot(backend, pid_path)
    layout = await take_snapshot(backend, layout_path) if layout_path else None

    def compute() -> dict:
        network = build_network(pid, layers=layers, tol=tol)
        return pipe_rows(
            network,
            pid,
            layout,
            services=options["services"],
            allowance=options["allowance"],
            vertical_allowance=options["vertical_allowance"],
            length_source=options["length_source"],
            force=bool(force),
        )

    result = await asyncio.to_thread(compute)
    result["files"] = (
        await asyncio.to_thread(write_workbook, result, kind="pipe", lang=lang, path=output)
        if output
        else None
    )
    return result
