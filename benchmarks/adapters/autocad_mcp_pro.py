"""Reference adapter for this repository, covering the v8 task matrix.

Every task verifies against a value worked out independently of the code under
test — a closed-form area, a count of entities placed on purpose, a token
ceiling fixed before the measurement. A task that only checks the server agrees
with itself is a task that cannot fail.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import csv
import math
import threading
from collections import Counter
from pathlib import Path

from benchmarks.adapters.base import BenchmarkAdapter, EnvironmentBlocked, TaskResult
from benchmarks.tasks_v8 import NEW_TASKS_V8, TaskSpec
from benchmarks.tasks_v9 import HEADLESS_ONLY_V9
from engineering.delivery import deliver_drawing
from engineering.layers import ensure_engineering_layers, ensure_standard_linetypes
from engineering.refiner import refine_drawing

#: AutoCAD command names a drafter types, none of which appeared in any tool
#: name or description before v1.5.0 — measured `df = 0` against the stock BM25
#: index, so no amount of tuning could have surfaced them.
#: Each command is answered by a *set*, because more than one tool can be a
#: correct answer -- QSELECT is both "filter by properties" and "select
#: similar", and picking a winner between them would test the tie-break rather
#: than the discovery.
DISCOVERY_QUERIES: tuple[tuple[str, frozenset[str]], ...] = (
    ("FILLET", frozenset({"entity_fillet"})),
    ("BPOLY", frozenset({"boundary_trace"})),
    ("QSELECT", frozenset({"selection_filter", "entity_select_smart"})),
    ("WBLOCK", frozenset({"block_create_from_entities"})),
    ("OVERKILL", frozenset({"drawing_refine"})),
    ("CHSPACE", frozenset({"entity_change_space"})),
)

#: How far down the ranking still counts as found. A drafter reads the top few.
DISCOVERY_TOP_N = 3

#: The advertised catalog must stay under this in discovery mode. Fixed here
#: rather than derived from the measurement, so the measurement can miss it.
TOKEN_CEILING = 2_000

#: The net floor of the two rooms of ``engineering/arch/spec.py::EXAMPLE_SPEC``,
#: computed by hand: the 250 mm ring on axis (0,0)-(9000,6000) has its inner
#: faces at x = 125 / 8875 and y = 125 / 5875, the 100 mm wall on x = 5000 its
#: faces at 4950 / 5050. Room 01: 4825 x 5750; room 02: 3825 x 5750.
ROUNDTRIP_AREAS_MM2: dict[str, float] = {
    "01": 4825.0 * 5750.0,
    "02": 3825.0 * 5750.0,
}

# ── v8 (v1.7.0) fixtures ──────────────────────────────────────────────────────

#: The v8 tasks reach into the headless engine (its document, its lock, its
#: files) to stage what an operator or a damaged file would do. On the live
#: engine they report ``not_run``; the live evidence is the COM harness
#: (``scripts/live``, spec §6 Task 21), never a zero here.
HEADLESS_ONLY: frozenset[str] = frozenset(task.task_id for task in NEW_TASKS_V8) | HEADLESS_ONLY_V9

#: ``requirements_roundtrip``: a 120 x 80 plate, a Ø20 bore at (60, 40) and four
#: Ø8 holes 15 mm in from the corners. Every target below is worked out by hand.
ROUNDTRIP_PLATE: dict = {
    "kind": "prismatic",
    "name": "plate",
    "material": "steel",
    "thickness": 10.0,
    "outline": [[0.0, 0.0], [120.0, 0.0], [120.0, 80.0], [0.0, 80.0]],
    "features": [
        {"kind": "hole", "id": "bore", "x": 60.0, "y": 40.0, "diameter": 20.0},
        {"kind": "hole", "id": "c1", "x": 15.0, "y": 15.0, "diameter": 8.0},
        {"kind": "hole", "id": "c2", "x": 105.0, "y": 15.0, "diameter": 8.0},
        {"kind": "hole", "id": "c3", "x": 105.0, "y": 65.0, "diameter": 8.0},
        {"kind": "hole", "id": "c4", "x": 15.0, "y": 65.0, "diameter": 8.0},
    ],
}
#: How far the saved file's bore is moved: inside the outline, so counts and
#: extents - all 1.6's delivery parity compared - stay the same.
ROUNDTRIP_MOVE_MM = 3.0

#: Text that starts like a spreadsheet formula, as a drawing can carry it.
HOSTILE_TEXT: tuple[str, ...] = ("=1+1", "@SUM(A1:A9)", "+cmd|' /C calc'!A0", "-2+3")

#: ``foreign_edit_verified``: a plate somebody else drew - four holes, H3 drawn
#: Ø9 where its requirement says Ø10. ``(id, x, y, radius)``.
FOREIGN_HOLES: tuple[tuple[str, float, float, float], ...] = (
    ("H1", 20.0, 20.0, 5.0),
    ("H2", 80.0, 20.0, 5.0),
    ("H3", 80.0, 40.0, 4.5),
    ("H4", 20.0, 40.0, 5.0),
)


def _failed(report: dict) -> list[str]:
    return sorted(row["id"] for row in report["rows"] if row["status"] == "fail")


def _measured(report: dict) -> dict[str, float | None]:
    return {row["id"]: (row.get("measured") or {}).get("value") for row in report["rows"]}


def _parity_view(path: Path) -> tuple[Counter, tuple]:
    """What 1.6's delivery parity compared: entity counts by type and the
    model-space extents."""
    import ezdxf
    from ezdxf import bbox

    msp = ezdxf.readfile(str(path)).modelspace()
    box = bbox.extents(msp)
    corners = tuple(round(v, 9) for v in (*box.extmin, *box.extmax)) if box.has_data else ()
    return Counter(entity.dxftype() for entity in msp), corners


def _formula_cells(rows: list[list[str]], filename: str) -> list[dict]:
    """CSV cells a spreadsheet would evaluate: text starting with a formula lead
    that is not a plain number."""
    from engineering.export.cells import FORMULA_PREFIXES, parse_decimal

    found = []
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            # parse_decimal hands anything but a plain decimal back unchanged
            if cell.startswith(FORMULA_PREFIXES) and isinstance(parse_decimal(cell), str):
                found.append({"file": filename, "row": r, "column": c, "value": cell})
    return found


def _read_csv(path: Path) -> list[list[str]]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


def _search_mode() -> bool:
    """True while the server advertises the search surface (DISCOVERY_MODE=search)."""
    import server

    return server._discovery_transform is not None


class _ProxiedClient:
    """A client that reaches every tool through the ``call_tool`` proxy.

    What a search-mode client that only calls listed tools does: ``call_tool``
    with the tool's name and arguments. Everything else is the wrapped client.
    """

    #: The synthetic tools a search-mode client calls directly, never through
    #: the proxy (the proxy refuses its own name): a task that names one gets
    #: the real call, and it is not counted as proxied.
    SYNTHETIC = frozenset({"call_tool", "search_tools", "describe_tool"})

    def __init__(self, client):
        self._client = client
        self.proxied_calls = 0

    async def call_tool(self, name: str, arguments: dict | None = None, **kwargs):
        if name in self.SYNTHETIC:
            return await self._client.call_tool(name, arguments or {}, **kwargs)
        self.proxied_calls += 1
        return await self._client.call_tool(
            "call_tool", {"name": name, "arguments": arguments or {}}, **kwargs
        )

    def __getattr__(self, attr):
        return getattr(self._client, attr)


class AutoCADMCPProAdapter(BenchmarkAdapter):
    name = "autocad-mcp-pro"

    def __init__(self, *, backend: str = "ezdxf"):
        self.backend_name = backend
        self.backend = None
        self.artifact_dir = Path()

    async def setup(self, artifact_dir: Path) -> None:
        if self.backend_name == "ezdxf":
            from backends.ezdxf_backend import EzdxfBackend

            self.backend = EzdxfBackend()
        elif self.backend_name == "com":
            try:
                from backends.com_backend import ComBackend

                self.backend = ComBackend()
            except ImportError as exc:  # no pywin32: not a Windows seat
                raise EnvironmentBlocked(f"the live engine is unavailable here: {exc}") from exc
        else:
            raise ValueError(f"Unknown backend: {self.backend_name}")
        self.artifact_dir = artifact_dir
        await self.backend.connect()

    async def cleanup(self) -> None:
        if self.backend is not None:
            await self.backend.disconnect()

    def metadata(self) -> dict:
        return {
            "backend": self.backend_name,
            "capabilities": self.backend.capabilities().to_dict() if self.backend else None,
            # 1.8-11: in search mode the in-process client tasks call through call_tool
            "discovery_mode": "search" if _search_mode() else "off",
            # how many tool calls of this run went through the call_tool proxy
            # (0 in "off"): the evidence a search-mode pass is not vacuous
            "proxied_calls": getattr(self, "_proxied_calls", 0),
        }

    async def _reset(self) -> None:
        await self.backend.drawing_new()

    async def run_task(self, task: TaskSpec) -> TaskResult:
        if self.backend_name != "ezdxf" and task.task_id in HEADLESS_ONLY:
            return TaskResult(
                task.task_id,
                "not_run",
                0.0,
                "headless lane only in 1.7.0: the task stages its input inside the ezdxf "
                "engine; the live evidence is the COM harness (scripts/live)",
            )
        await self._reset()
        handler = getattr(self, f"_task_{task.task_id}", None)
        if handler is None:
            return TaskResult(task.task_id, "unsupported", 0.0, "No adapter implementation")
        outcome = await handler()
        if isinstance(outcome, TaskResult):  # a lane's own status (environment_blocked)
            return outcome
        passed, metrics, artifacts = outcome
        return TaskResult(
            task.task_id,
            "pass" if passed else "fail",
            100.0 if passed else 0.0,
            metrics=metrics,
            artifacts=artifacts,
        )

    async def _task_core_geometry(self):
        line = await self.backend.entity_create_line(0, 0, 3, 4)
        circle = await self.backend.entity_create_circle(10, 10, 2)
        info = await self.backend.entity_get(line.handle)
        passed = abs(float(info.properties["length"]) - 5.0) < 1e-6
        return passed and bool(circle.handle), {"line_length": info.properties["length"]}, []

    async def _task_modify_query(self):
        line = await self.backend.entity_create_line(0, 0, 10, 0)
        await self.backend.entity_move(line.handle, 5, 2)
        moved = await self.backend.entity_get(line.handle)
        return moved.properties["start"][:2] == [5.0, 2.0], {}, []

    async def _task_layers_linetypes(self):
        await ensure_standard_linetypes(self.backend)
        await ensure_engineering_layers(self.backend)
        layers = {item.name for item in await self.backend.layer_list()}
        linetypes = {item.upper() for item in await self.backend.linetype_list()}
        return "GEOMETRY" in layers and "CENTER" in linetypes, {}, []

    async def _task_dimensions(self):
        await ensure_engineering_layers(self.backend)
        dimension = await self.backend.dimension_linear(0, 0, 50, 0, 25, 10, layer="DIM")
        return "DIM" in dimension.type.upper(), {"entity_type": dimension.type}, []

    async def _task_table_mleader(self):
        await ensure_engineering_layers(self.backend)
        table = await self.backend.entity_create_table(
            0, 20, [["A", "1"]], headers=["ITEM", "QTY"], layer="TEXT"
        )
        leader = await self.backend.leader_create_mleader([[0, 0], [10, 10]], "NOTE", layer="DIM")
        representations = [
            table.properties.get("representation"),
            leader.properties.get("representation"),
        ]
        return all(representations), {"representations": representations}, []

    async def _task_transactions(self):
        await self.backend.transaction_begin()
        await self.backend.entity_create_circle(0, 0, 1)
        await self.backend.transaction_rollback()
        entities = await self.backend.entity_list(limit=100)
        return len(entities) == 0, {"entity_count": len(entities)}, []

    async def _task_preflight(self):
        result = await self.backend.drawing_preflight(
            "Benchmark plate",
            {
                "units": "mm",
                "part_type": "plate",
                "dimensions": {"width": 50, "height": 25},
                "tolerance_policy": "ISO 2768-m",
            },
        )
        return result.ready and result.spec_hash.startswith("sha256:"), {}, []

    async def _task_quality_refiner(self):
        await ensure_engineering_layers(self.backend)
        await self.backend.entity_create_line(0, 0, 10, 0, layer="GEOMETRY")
        await self.backend.entity_create_line(0, 0, 10, 0, layer="GEOMETRY")
        result = await refine_drawing(self.backend, focus=["duplicate_entities"])
        return result.final_score > result.initial_score, result.to_dict(), []

    async def _task_dxf_roundtrip(self):
        await self.backend.entity_create_line(0, 0, 10, 10)
        path = self.artifact_dir / "roundtrip.dxf"
        await self.backend.drawing_export_dxf(str(path))
        from backends.ezdxf_backend import EzdxfBackend

        reopened = EzdxfBackend()
        await reopened.connect()
        try:
            await reopened.drawing_open(str(path))
            count = (await reopened.drawing_info()).entity_count
        finally:
            await reopened.disconnect()
        return count == 1, {"entity_count": count}, [str(path)]

    # ── v3 (v1.5.0) ─────────────────────────────────────────────────────────

    async def _task_tool_discovery(self):
        """Can a drafter find the tool by typing the command they know?

        Ranked through the real transform, not a stub: the corpus is what makes
        these commands findable at all, and testing anything else would test
        the fixture.
        """
        import server
        from discovery.transform import CadSearchTransform

        tools = list(await server.mcp._list_tools())
        transform = CadSearchTransform()

        ranks: dict[str, int | None] = {}
        for command, accepted in DISCOVERY_QUERIES:
            hits = [
                tool.name for tool in transform.rank(tools, command, limit=DISCOVERY_TOP_N).hits
            ]
            found = next((index for index, name in enumerate(hits) if name in accepted), None)
            ranks[command] = None if found is None else found + 1

        passed = all(rank is not None for rank in ranks.values())
        return passed, {"ranks": ranks, "top_n": DISCOVERY_TOP_N}, []

    async def _task_token_budget(self):
        """What the client pays before it has asked for anything.

        The ceiling is a constant, so a regression that doubles the catalog
        fails this rather than quietly reporting a bigger number.
        """
        import server
        from benchmarks.token_suite import (
            FORMAT_JSON_SCHEMA,
            _client,
            _wire_payload,
            build_counter,
        )

        async def _advertised(mode: str) -> tuple[int, int]:
            """Tokens on the wire for `tools/list`, and how many tools that is.

            Measured through a real client rather than the registry: the
            registry is what the transform hides, so counting it would make
            discovery mode look free.
            """
            server._apply_discovery_mode(mode)
            async with _client() as client:
                tools = await client.list_tools()
            return counter.count(_wire_payload(tools), FORMAT_JSON_SCHEMA), len(tools)

        counter = build_counter("ratio")
        applied = "search" if _search_mode() else "off"
        try:
            full, catalog_count = await _advertised("off")
            discovery, advertised_count = await _advertised("search")
        finally:
            # the mode the run started in, not "off": a search-mode run of the
            # matrix must stay in search mode for the tasks after this one
            server._apply_discovery_mode(applied)

        passed = discovery < TOKEN_CEILING and discovery < full
        return (
            passed,
            {
                "catalog_tokens": full,
                "discovery_tokens": discovery,
                "ceiling": TOKEN_CEILING,
                "reduction_x": round(full / discovery, 1) if discovery else None,
                "catalog_tools": catalog_count,
                "advertised_tools": advertised_count,
                # Named, because "40,305 tokens" reads like a count and is not
                # one. `benchmarks/token_suite.py --tokenizer anthropic` counts
                # for real; this lane has to stay offline and deterministic.
                "tokenizer": f"{counter.name} (estimate)" if counter.estimated else counter.name,
            },
            [],
        )

    async def _task_hatch_islands(self):
        """A section view is mostly holes, so the island is the measurement.

        20x20 outer square with a 10x10 island: the filled area is 300, and a
        server that records the island without subtracting it reports 400 while
        looking entirely correct on screen.
        """
        await ensure_engineering_layers(self.backend)
        hatch = await self.backend.entity_create_hatch(
            "ANSI31", [[0, 0], [20, 0], [20, 20], [0, 20]], layer="HATCH"
        )
        island = [
            {"type": "line", "start": [5, 5], "end": [15, 5]},
            {"type": "line", "start": [15, 5], "end": [15, 15]},
            {"type": "line", "start": [15, 15], "end": [5, 15]},
            {"type": "line", "start": [5, 15], "end": [5, 5]},
        ]
        added = await self.backend.hatch_add_boundary(hatch.handle, island)
        measured = await self.backend.entity_measure(hatch.handle)
        ignored = await self.backend.hatch_edit(hatch.handle, style="ignore")
        over_island = await self.backend.entity_measure(hatch.handle)

        passed = (
            added["path_count"] == 2
            and abs(measured["area"] - 300.0) < 1e-6
            and measured["hatch_style"] == "outer"  # what entity_create_hatch writes
            and ignored["changed"] == ["style"]
            and abs(over_island["area"] - 400.0) < 1e-6
        )
        return (
            passed,
            {
                "path_count": added["path_count"],
                "filled_area": measured["area"],
                "area_ignoring_islands": over_island["area"],
                "expected_filled": 300.0,
                "hatch_style": measured["hatch_style"],
            },
            [],
        )

    async def _task_selection_filter(self):
        """Window is not crossing, and a polygon is not its bounding box.

        Three circles placed so each distinction has exactly one right answer:
        one wholly inside, one straddling the edge, one out in the corner that
        the bounding box catches and the triangle does not.
        """
        await ensure_engineering_layers(self.backend)
        await self.backend.entity_create_circle(5, 5, 1, layer="GEOMETRY")
        await self.backend.entity_create_circle(10, 5, 3, layer="GEOMETRY")
        await self.backend.entity_create_circle(18, 18, 0.5, layer="GEOMETRY")
        await self.backend.entity_create_circle(40, 40, 1, layer="TEXT")

        window = await self.backend.selection_window(0, 0, 10, 10, mode="window")
        crossing = await self.backend.selection_window(0, 0, 10, 10, mode="crossing")
        bbox = await self.backend.selection_window(0, 0, 20, 20, mode="window")
        triangle = await self.backend.selection_polygon([[0, 0], [20, 0], [0, 20]], mode="window")
        by_layer = await self.backend.selection_filter(entity_type="CIRCLE", layer="TEXT")

        passed = (
            window["count"] == 1
            and crossing["count"] == 2
            and bbox["count"] == 3
            and triangle["count"] == 1
            and by_layer["count"] == 1
            and by_layer["filtered_by"] == ["entity_type", "layer"]
        )
        return (
            passed,
            {
                "window": window["count"],
                "crossing": crossing["count"],
                "bounding_box": bbox["count"],
                "polygon": triangle["count"],
                "by_layer": by_layer["count"],
            },
            [],
        )

    async def _task_measure_from_handle(self):
        """The 28.2% that reading coordinates back loses.

        A 10x10 square whose top edge is a semicircle encloses 139.2699. Chain
        the loose edges into a boundary, then measure that boundary by handle;
        shoelacing the four corners the model would have remembered gives 100.
        """
        await ensure_engineering_layers(self.backend)
        await self.backend.entity_create_line(0, 0, 10, 0, layer="GEOMETRY")
        await self.backend.entity_create_line(10, 0, 10, 10, layer="GEOMETRY")
        await self.backend.entity_create_line(0, 10, 0, 0, layer="GEOMETRY")
        await self.backend.entity_create_arc(5, 10, 5, 0, 180, layer="GEOMETRY")

        loop = await self.backend.boundary_trace(5.0, 5.0)
        measured = await self.backend.entity_measure(loop["handle"])
        naive = await self.backend.analysis_measure_area([[0, 0], [10, 0], [10, 10], [0, 10]])

        true_area = 100.0 + math.pi * 25.0 / 2.0
        lost = (true_area - naive) / true_area
        passed = (
            loop["ok"] is True
            and abs(measured["area"] - true_area) < 1e-5
            and abs(loop["area"] - measured["area"]) < 1e-9
            and measured["method"] == "analytic_bulge"
            and measured["exact"] is True
            and 0.28 < lost < 0.29
        )
        return (
            passed,
            {
                "measured_area": measured["area"],
                "true_area": round(true_area, 6),
                "vertex_shoelace_area": naive,
                "fraction_lost_by_reading_points_back": round(lost, 4),
                "boundary_agrees": abs(loop["area"] - measured["area"]) < 1e-9,
            },
            [],
        )

    async def _task_pid_roundtrip(self):
        """Five symbols, four lines, read back with nothing dangling.

        The spec is drawn through the same code ``pid_from_spec`` calls; the
        graph, the instrument index and the line list are then read back from
        the drawing by the reader, which never sees the spec. Every expected
        number below is counted from the spec by hand.
        """
        import copy

        from engineering.layers import apply_layer_set
        from engineering.pid.deliverables import instrument_index, line_list
        from engineering.pid.spec import EXAMPLE_SPEC, run_spec

        await apply_layer_set(self.backend, "pid")
        # Pinned to the fixed routes the v4 numbers were counted on (1.8-20:
        # ``sheet.routing`` is ``auto`` by default; said here so it stays so).
        spec = copy.deepcopy(EXAMPLE_SPEC)
        spec["sheet"]["routing"] = "auto"
        result = await run_spec(self.backend, spec)
        graph = result["graph"]
        index = instrument_index(graph)
        lines = line_list(graph)
        # The spec numbers its two process lines; the connector line is left
        # to the default ``number_format``, which with no size/service/spec/
        # insulation collapses to the bare sequence number, and it must come
        # back through XDATA, not a label search. The electric signal line is
        # unnumbered by rule (ISA-5.1 signal lines carry no pipe line number)
        # and must not have consumed a sequence number: the connector is "3".
        authored = "100-P-1001-CS1"
        numbers = {(r["from_tag"], r["to_tag"]): r["line_number"] for r in lines}
        authored_ok = (
            numbers.get(("P-101", "FCV-101")) == authored
            and numbers.get(("FCV-101", "V-201")) == authored
        )
        others = [r for r in lines if r["line_number"] != authored]
        signal = [r for r in others if r["line_class"] == "electric"]
        sequenced = [r for r in others if r["line_class"] != "electric"]
        sequenced_ok = (
            len(signal) == 1
            and signal[0]["line_number"] is None
            and len(sequenced) == 1
            and sequenced[0]["line_number"] == "3"
            and sequenced[0]["number_source"] == "xdata"
        )
        passed = (
            len(graph["nodes"]) == 5
            and len(graph["edges"]) == 4
            and graph["stats"]["dangling"] == 0
            and graph["stats"]["confidence_min"] == 1.0
            and result["critique"] == []
            and [r["tag"] for r in index] == ["FIC-101"]
            and index[0]["connected_to"] == "FCV-101"
            and len(lines) == 4
            and authored_ok
            and sequenced_ok
        )
        return (
            passed,
            {
                "nodes": len(graph["nodes"]),
                "edges": len(graph["edges"]),
                "dangling": graph["stats"]["dangling"],
                "confidence_min": graph["stats"]["confidence_min"],
                "critique_issues": len(result["critique"]),
                "instrument_index_rows": len(index),
                "line_list_rows": len(lines),
                "authored_line_numbers_kept": authored_ok,
                "unnumbered_lines_sequenced": sequenced_ok,
            },
            [],
        )

    async def _task_page_setup_truth(self):
        """ISO A3 landscape on Layout1, plotted, and the sheet read back from the PDF.

        The number that matters is parsed from the file's ``/MediaBox`` — the
        setter's ``applied`` block is what we asked for, the page-setup list is
        what the LAYOUT now says, and the PDF is what a printer would get. All
        three have to agree with ISO 216's 420 × 297.

        A fresh document's Layout1 is already A3 landscape, so that read-back
        alone would pass with the setter stubbed out. The task first moves the
        sheet to ANSI B — 432 × 279, a size no fresh document has — and reads
        that PDF, then applies A3 and requires the setter to report the layout
        moved from B to A3. The stub fails at the first PDF.
        """
        from engineering.standards.papers import resolve_page_setup
        from engineering.standards.plot import batch_plot

        out_dir = self.artifact_dir / "page_setup_truth"
        out_dir.mkdir(parents=True, exist_ok=True)
        # Stage 1: a non-default sheet, proven by its PDF.
        await self.backend.page_setup_apply(
            "Layout1",
            resolve_page_setup("ANSI_B", orientation="landscape", scale="1:1"),
        )
        before = await batch_plot(
            self.backend, ["Layout1"], str(out_dir), "{drawing}-{layout}-ansi_b.pdf"
        )
        row_before = before["sheets"][0]
        width_before, height_before = (float(v) for v in row_before["mediabox_mm"])
        # Stage 2: the sheet under test.
        setup = resolve_page_setup(
            "ISO_A3",
            orientation="landscape",
            plot_style="monochrome.ctb",
            scale="1:1",
        )
        applied = await self.backend.page_setup_apply("Layout1", setup)
        listed = (await self.backend.page_setup_list("Layout1"))[0]
        report = await batch_plot(
            self.backend, ["Layout1"], str(out_dir), "{drawing}-{layout}-iso_a3.pdf"
        )
        row = report["sheets"][0]
        width_mm, height_mm = (float(v) for v in row["mediabox_mm"])
        size_listed = tuple(float(v) for v in listed["size_mm"])
        size_changed = applied["changed"].get("size_mm")
        # ±0.5 mm, the spec's own tolerance: the live engine measures ANSI B
        # as 431.8 × 279.4 (11 × 17 in), the headless one as 432 × 279.
        moved = size_changed or [[0.0, 0.0], [0.0, 0.0]]
        moved_from_b = (
            abs(float(moved[0][0]) - 432.0) < 0.5 and abs(float(moved[0][1]) - 279.0) < 0.5
        )
        moved_to_a3 = (
            abs(float(moved[1][0]) - 420.0) < 0.5 and abs(float(moved[1][1]) - 297.0) < 0.5
        )
        passed = (
            abs(width_before - 432.0) < 0.5
            and abs(height_before - 279.0) < 0.5
            and applied["ok"] is True
            and applied["plot_style_known"] is True
            and moved_from_b
            and moved_to_a3
            and listed["paper"] == "ISO_A3"
            and listed["orientation"] == "landscape"
            and size_listed == (420.0, 297.0)
            and abs(width_mm - 420.0) < 0.5
            and abs(height_mm - 297.0) < 0.5
            and row["bytes"] > 0
        )
        return (
            passed,
            {
                "paper": listed["paper"],
                "orientation": listed["orientation"],
                "size_mm_listed": list(size_listed),
                "mediabox_mm": [width_mm, height_mm],
                "mediabox_mm_before": [width_before, height_before],
                "size_mm_changed": size_changed,
                "pdf_bytes": row["bytes"],
                "plot_style_known": applied["plot_style_known"],
            },
            [str(row_before["path"]), str(row["path"])],
        )

    async def _task_mech_assembly(self):
        """A flange-coupling sheet built from the part model and the sheet standard.

        Roadmap criterion 3. Two parts (a hub with a bore and a flange), a full
        section through the hub, four standard fasteners with their parts-list
        rows and balloons, an ISO 5457 A3 frame and an ISO 7200 title block.
        Every step goes through the code the tools call: the frame and title
        block through ``apply_titleblock``, the parts list is *read* off the
        drawing by ``extract_records`` and drawn by ``draw_bom_table``, and each
        balloon is linked to its row's inserts by ``add_balloon``.

        The gate is two numbers, and both are asserted rather than reported:
        ``run_critique(focus=None)`` must return zero issues, and the score the
        finalize gate computes - ``combine(validator.summary, critique)`` over
        the saved sheet - must be at least 90. A regression in either fails the
        task; the focuses that fired come back in the metrics so the failure
        names itself.

        No step here repairs what the critique would flag: the view engine
        draws the ISO 128-23 centre lines of every circle it emits (the end
        view's bore and bolt holes included), so the sheet is clean because
        the tools drew it clean, not because the benchmark tidied up after them.
        """
        from engineering import DrawingValidator
        from engineering.critique import run_critique
        from engineering.layers import apply_layer_set
        from engineering.mech.draw import add_view, dimension_part, draw_part
        from engineering.mech.part import build_part
        from engineering.mech.stdparts import insert_std_part
        from engineering.scoring import combine
        from engineering.sheet.bom import (
            add_balloon,
            draw_bom_table,
            extract_records,
            rows_from_records,
        )
        from engineering.sheet.titleblock import (
            TB_HEIGHT,
            TitleBlockMetadata,
            apply_titleblock,
            titleblock_origin,
        )

        await apply_layer_set(self.backend, "mech")
        await apply_titleblock(
            self.backend,
            size="A3",
            metadata=TitleBlockMetadata(
                title="FLANGE COUPLING",
                drawing_no="MA-001",
                drawn_by="autocad-mcp-pro",
                scale="1:1",
                material="S235JR",
            ),
            projection="first",
            frame=True,
            zones=True,
            marks=True,
        )

        hub = build_part(
            {
                "kind": "revolved",
                "name": "HUB",
                "material": "steel",
                "segments": [
                    {"length": 40.0, "d_outer": 40.0, "d_inner": 20.0},
                    {"length": 15.0, "d_outer": 90.0, "d_inner": 20.0},
                ],
            }
        )
        flange = build_part(
            {
                "kind": "revolved",
                "name": "FLANGE",
                "material": "cast_iron",
                "segments": [{"length": 15.0, "d_outer": 90.0, "d_inner": 40.0}],
                # Four M12 clearance holes (ISO 273 medium, 13.5) on a 65 PCD.
                "features": [
                    {
                        "kind": "hole_circle",
                        "id": "bolts",
                        "x": 7.5,
                        "pcd": 65.0,
                        "count": 4,
                        "diameter": 13.5,
                        "start_angle": 45.0,
                    }
                ],
            }
        )
        # A3 landscape: the frame runs (20, 10)-(410, 287) and the title block
        # fills its lower-right corner, 180 x 60. First angle throughout: the
        # hub's front view sits at x = 125 so its section lands left of it
        # inside the frame; the flange's front view stands at x = 345 so its
        # end view (the bolt circle) lands between the two parts; the
        # fasteners take the free band below, left of the title block.
        drawn_hub = await draw_part(
            self.backend, hub, at=(125.0, 160.0), views=("front",), dimension=False
        )
        # A full longitudinal section along the hub's axis (part-local frame).
        await add_view(
            self.backend,
            drawn_hub["part_id"],
            "section",
            plane={"p1": [0.0, 0.0], "p2": [55.0, 0.0], "label": "A"},
            style="full",
        )
        await dimension_part(self.backend, drawn_hub["part_id"], style="chain")
        await draw_part(
            self.backend, flange, at=(345.0, 160.0), views=("front", "side"), dimension=True
        )

        # Two bolt-and-nut pairs: each nut sits on the thread of its bolt.
        stations = (60.0, 110.0, 160.0, 210.0)
        fasteners = []
        for index, x in enumerate(stations):
            designation = "ISO 4014 - M12x60" if index % 2 == 0 else "ISO 4032 - M12"
            fasteners.append(
                await insert_std_part(self.backend, designation, at=(x, 90.0), view="side")
            )

        # The parts list is a read of the drawing, not a list typed beside it.
        records = await extract_records(self.backend)
        rows = rows_from_records(records)
        tb_x, tb_y = titleblock_origin("A3")
        await draw_bom_table(self.backend, rows, at=(tb_x, tb_y + TB_HEIGHT))
        station_of = {
            str(result.get("handle")): x for result, x in zip(fasteners, stations, strict=True)
        }
        for row in rows:
            targets = [str(handle) for handle in row.get("handles") or ()]
            x = station_of.get(targets[0], stations[0]) if targets else stations[0]
            await add_balloon(
                self.backend,
                item=int(row["item"]),
                at=(x, 135.0),
                leader_to=(x, 95.0),
                targets=targets,
            )

        # drawing_finalize saves before it validates; so does the benchmark,
        # or the validator's not_saved/no_path findings would grade the
        # harness instead of the sheet.
        sheet = self.artifact_dir / "mech_assembly.dxf"
        await self.backend.drawing_save_as(str(sheet))
        issues = await run_critique(self.backend, None)
        validation = await DrawingValidator().run(self.backend)
        summary = {"error": 0, "warning": 0, "info": 0}
        for issue in issues:
            summary[issue.severity] = summary.get(issue.severity, 0) + 1
        score = combine(validation.summary, summary)
        passed = len(issues) == 0 and float(score["score"]) >= 90.0
        return (
            passed,
            {
                "critique_issues": len(issues),
                "critique_focuses": sorted({issue.focus for issue in issues}),
                "score": float(score["score"]),
                "grade": score["grade"],
                "invalidity_ratio": score["invalidity_ratio"],
                "parts_list_rows": len(rows),
                "fasteners": len(fasteners),
                "validator_findings": sorted(f.code for f in validation.findings),
            },
            [str(sheet)],
        )

    async def _task_arch_roundtrip(self):
        """A two-room plan drawn from one spec and read back off the drawing.

        Track F's evidence (spec §10). ``arch_plan_from_spec`` draws the plan of
        ``engineering/arch/spec.py::EXAMPLE_SPEC`` - a 9 x 6 m brick ring at 250
        mm split by a 100 mm AAC wall, an entrance door, an interior door, two
        windows, a straight stair, two labelled rooms, the exterior chains and
        the three schedules. Then the drawing is read, never the spec:

        * ``rooms_detect`` on the wall layer must find each labelled room, and
          its area must agree with the area the label's record carries within
          0.1 %, and with the net floor computed by hand in
          ``ROUNDTRIP_AREAS_MM2`` - so a label that typed the axis area fails;
        * the door and window schedules read off the records must list exactly
          the drawn tags;
        * ``run_critique(focus=None)`` returns zero issues and the finalize
          score - ``combine(validator.summary, critique)`` over the saved plan -
          is at least 90.

        Any one slipping fails the task, and the metrics name what slipped.
        """
        from engineering import DrawingValidator
        from engineering.arch.critique import point_in_loop
        from engineering.arch.draw import read_plan
        from engineering.arch.layers import ARCH_ROLE_LAYER
        from engineering.arch.rooms import rooms_detect
        from engineering.arch.schedule import schedule_rows
        from engineering.arch.spec import EXAMPLE_SPEC, draw_plan_from_spec
        from engineering.critique import run_critique
        from engineering.scoring import combine

        drawn = await draw_plan_from_spec(self.backend, EXAMPLE_SPEC)
        plan = await read_plan(self.backend)
        detected = await rooms_detect(self.backend, layers=[ARCH_ROLE_LAYER["wall"]])
        faces = list(detected.get("rooms") or [])

        areas: dict[str, dict] = {}
        for room in plan["rooms"]:
            face = next((f for f in faces if point_in_loop(room.at, f["loop"])), None)
            detected_area = float(face["area"]) if face is not None else 0.0
            hand = ROUNDTRIP_AREAS_MM2.get(str(room.number))
            areas[str(room.number)] = {
                "label_mm2": float(room.area),
                "detected_mm2": detected_area,
                "hand_mm2": hand,
                "agrees": face is not None
                and hand is not None
                and abs(detected_area - float(room.area)) <= 1e-3 * float(room.area)
                and abs(detected_area - hand) <= 1e-3 * hand,
            }
        rooms_ok = (
            set(areas) == set(ROUNDTRIP_AREAS_MM2)
            and len(faces) == len(ROUNDTRIP_AREAS_MM2)
            and all(row["agrees"] for row in areas.values())
        )

        door_tags = sorted(str(row["tag"]) for row in schedule_rows("doors", plan, lang="en"))
        window_tags = sorted(str(row["tag"]) for row in schedule_rows("windows", plan, lang="en"))
        tags_ok = door_tags == ["D1", "D2"] and window_tags == ["W1", "W2"]

        sheet = self.artifact_dir / "arch_roundtrip.dxf"
        await self.backend.drawing_save_as(str(sheet))
        issues = await run_critique(self.backend, None)
        validation = await DrawingValidator().run(self.backend)
        summary = {"error": 0, "warning": 0, "info": 0}
        for issue in issues:
            summary[issue.severity] = summary.get(issue.severity, 0) + 1
        score = combine(validation.summary, summary)
        passed = rooms_ok and tags_ok and len(issues) == 0 and float(score["score"]) >= 90.0
        return (
            passed,
            {
                "rooms": areas,
                "faces_detected": len(faces),
                "confidence_min": detected.get("confidence_min"),
                "door_tags": door_tags,
                "window_tags": window_tags,
                "omitted": drawn.get("omitted"),
                "critique_issues": len(issues),
                "critique_focuses": sorted({issue.focus for issue in issues}),
                "score": float(score["score"]),
                "grade": score["grade"],
                "invalidity_ratio": score["invalidity_ratio"],
                "validator_findings": sorted(f.code for f in validation.findings),
            },
            [str(sheet)],
        )

    async def _task_takeoff_roundtrip(self):
        """Pipe and cable quantities off the synthetic plant pair, against its truth.

        Track H's evidence (spec §15). The pair is written fresh into the
        artifact folder by ``tests/fixtures/plant_pair.py``, whose truth is
        computed from the positions it placed. Then only the two DXFs are read:

        * ``scale_check(pid, layout)`` must say ``schematic`` - room 1 is the
          layout stretched 1.3x, room 2 is rearranged, and 21.8 % of the 55 tag
          pairs lie within ±10 % of the median ratio;
        * ``pipe_rows(..., length_source="auto")`` must report every run of the
          P&ID (the ice-water utility included, because ``services`` names it)
          with the truth's status; a routable run's layout length must equal the
          rectilinear MST of its tags exactly, its sizes (both across the
          reducer) and its service must be the drawn ones (the CIP return drawn
          on the supply layer included); the run ending on T105, which the
          layout lacks, is status C with no layout length;
        * ``cable_rows(pid, layout)`` must give every load its panel from the
          layout's wiring callouts, its stated power (the heater's stays empty),
          the Manhattan length to the panel and the metres rounded up after the
          20 % allowance.

        Any one slipping fails the task, and the metrics name what slipped.
        """
        from engineering.understand.network import build_network
        from engineering.understand.scale import scale_check
        from engineering.understand.snapshot import read_snapshot
        from engineering.understand.takeoff import cable_rows, pipe_rows
        from tests.fixtures.plant_pair import build_plant_pair

        truth = build_plant_pair(self.artifact_dir / "plant_pair")
        pid = read_snapshot(truth["pid"])
        layout = read_snapshot(truth["layout"])
        scale = scale_check(pid, layout)
        services = tuple(sorted({run["service"] for run in truth["pipe_runs"]}))
        pipe = pipe_rows(
            build_network(pid), pid, layout, scale=scale, services=services, length_source="auto"
        )
        found = {(run["service"], tuple(sorted(run["tags"]))): run for run in pipe["runs"]}
        pipe_mismatch = []
        for run in truth["pipe_runs"]:
            got = found.get((run["service"], tuple(run["tags"])))
            if got is None:
                pipe_mismatch.append({"run": run["id"], "reason": "no run with these tags"})
                continue
            reasons = []
            if got["status"] != run["status"]:
                reasons.append(f"status {got['status']} != {run['status']}")
            want, have = run["layout_length_mm"], got.get("layout_m")
            if want is None and have is not None:
                reasons.append(f"a layout length ({have} m) for an unroutable run")
            elif want is not None and (have is None or abs(float(have) * 1000.0 - want) > 1e-6):
                reasons.append(f"layout length {have} m != {want} mm")
            if set(got["diameters"]) != set(run["diameters"]):
                reasons.append(f"sizes {sorted(got['diameters'])} != {sorted(run['diameters'])}")
            if reasons:
                pipe_mismatch.append({"run": run["id"], "reason": "; ".join(reasons)})

        # panel_rule="stated": the gate reads what the drawing says; a nearest-panel
        # guess is no evidence
        cable = {row["tag"]: row for row in cable_rows(pid, layout, panel_rule="stated")["rows"]}
        cable_mismatch = []
        for want in truth["cable_rows"]:
            got = cable.get(want["tag"])
            if got is None:
                cable_mismatch.append({"tag": want["tag"], "wrong": ["missing"]})
                continue
            wrong = [key for key in ("panel", "kw") if got.get(key) != want[key]]
            if got.get("cable_m") != want["roundup_m"]:
                wrong.append("cable_m")
            length = got.get("length_m")
            if length is None or abs(float(length) * 1000.0 - want["length_mm"]) > 1e-6:
                wrong.append("length_m")
            if wrong:
                cable_mismatch.append({"tag": want["tag"], "wrong": wrong})

        passed = scale["verdict"] == truth["scale_verdict"] and not pipe_mismatch
        passed = passed and not cable_mismatch
        return (
            passed,
            {
                "scale_verdict": scale["verdict"],
                "within_10pct": scale.get("within_10pct"),
                "length_source": pipe.get("length_source"),
                "runs_checked": len(truth["pipe_runs"]),
                "pipe_mismatch": pipe_mismatch,
                "loads_checked": len(truth["cable_rows"]),
                "cable_mismatch": cable_mismatch,
            },
            [truth["pid"], truth["layout"]],
        )

    async def _task_understand_foreign(self):
        """One call on a foreign layout reports what is wrong with it, by evidence.

        Track H's evidence (spec §5, §15), on the synthetic plant pair: the
        layout declares inches (``$INSUNITS = 1``) over millimetre geometry, a
        stray LINE 5,000 km away stretches its declared extents, and the plan is
        drawn twice, 100 m apart. ``describe`` of the layout must infer
        millimetres, report that they disagree with the declared unit and warn
        about ``INSUNITS``, name the stray entity's handle in its extents, and
        report exactly two clusters; ``describe`` of the P&ID must classify its
        four service layers and the electrical layer as the generator names them
        (English and Dutch names). A warning or an extents report is searched as
        JSON, so the gate holds whatever shape the evidence takes.
        """
        import json

        from engineering.understand.describe import describe
        from engineering.understand.snapshot import read_snapshot
        from tests.fixtures.plant_pair import build_plant_pair

        truth = build_plant_pair(self.artifact_dir / "plant_pair")
        layout = describe(read_snapshot(truth["layout"]))
        pid = describe(read_snapshot(truth["pid"]))
        units = layout["units"]
        units_flagged = (
            str(units.get("inferred")) == truth["layout_true_unit"]
            and units.get("agree") is False
            and "INSUNITS" in json.dumps(layout["warnings"], ensure_ascii=False)
        )
        outlier_found = f'"{truth["outlier_handle"]}"' in json.dumps(layout["extents"])
        copies_separated = len(layout["clusters"]) == truth["cluster_count"]
        read = {row["name"]: row["service"] for row in pid["layers"]}
        misread = {
            name: read.get(name)
            for name, service in truth["layer_services"].items()
            if read.get(name) != service
        }
        passed = units_flagged and outlier_found and copies_separated and not misread
        return (
            passed,
            {
                "units": units,
                "units_flagged": units_flagged,
                "outlier_found": outlier_found,
                "clusters": len(layout["clusters"]),
                "copies_separated": copies_separated,
                "misread_layers": misread,
            },
            [truth["layout"], truth["pid"]],
        )

    async def _task_auditable_delivery(self):
        await ensure_engineering_layers(self.backend)
        await self.backend.entity_create_line(0, 0, 10, 0, layer="GEOMETRY")
        destination = self.artifact_dir / "delivery"
        result = await deliver_drawing(
            self.backend,
            destination,
            formats=["dxf"],
            min_score=0,
            strict_critique=False,
        )
        return result.status == "success", {"score": result.score}, [result.manifest_path]

    # ── v8 (v1.7.0) ─────────────────────────────────────────────────────────

    async def _fresh_backend(self):
        from backends.ezdxf_backend import EzdxfBackend

        backend = EzdxfBackend()
        await backend.connect()
        return backend

    @contextlib.asynccontextmanager
    async def _served(self):
        """An in-memory MCP client whose server works on THIS adapter's backend,
        so a task can call the tools the way a client does and still reach the
        document behind the server's back, the way an operator would. The
        server's lifespan normally disconnects its backend on exit; here the
        adapter owns it, so the disconnect is held off until ``cleanup``."""
        from fastmcp import Client

        import server

        backend = self.backend
        make, disconnect = server._make_backend, server._disconnect_backend

        async def _make():
            return backend

        async def _keep(_backend):
            return None

        server._make_backend, server._disconnect_backend = _make, _keep
        try:
            async with Client(server.mcp) as client:
                # 1.8-11: under DISCOVERY_MODE=search every call goes through the
                # call_tool proxy, as a client that sees only the listed tools
                # makes it - the evidence the server default flips on.
                if _search_mode():
                    proxied = _ProxiedClient(client)
                    try:
                        yield proxied
                    finally:
                        self._proxied_calls = (
                            getattr(self, "_proxied_calls", 0) + proxied.proxied_calls
                        )
                else:
                    yield client
        finally:
            server._make_backend, server._disconnect_backend = make, disconnect

    async def _task_requirements_roundtrip(self):
        """Measured on the document, then on the saved file after it changed.

        Spec §6 Task 20 / roadmap T11. A plate is drawn from its part model in
        millimetres (``$INSUNITS`` 4 - the verification holds a drawing to its
        declared unit) and four requirements are bound by feature tag:
        R1 the bore is Ø20; R2 the c1-c2 pitch is 90; R3 the bore centre is
        sqrt(45² + 25²) = 51.478 from c1; R4 exactly four Ø8 holes. All four
        must pass on the document. The bore of the SAVED file is then moved
        3 mm right - inside the outline, so the entity counts and extents
        1.6's delivery parity compared do not move - and the file must fail
        exactly R3 (now sqrt(48² + 25²) = 54.120), both re-bound from the
        document (``verify_artifact``) and opened on its own by a fresh engine.
        """
        import ezdxf

        from engineering.layers import apply_layer_set
        from engineering.mech.draw import draw_part
        from engineering.mech.part import build_part
        from engineering.verification import artifact as artifact_module
        from engineering.verification.report import verify_requirements
        from engineering.verification.requirements import parse_requirements

        await self.backend.system_set_variable("INSUNITS", 4)
        await apply_layer_set(self.backend, "mech")
        drawn = await draw_part(
            self.backend,
            build_part(ROUNDTRIP_PLATE),
            at=(0.0, 0.0),
            views=("front",),
            dimension=False,
        )
        part = drawn["part_id"]

        def tag(feature: str) -> dict:
            return {"kind": "feature_tag", "part": part, "feature": feature, "type": "circle"}

        rs = parse_requirements(
            {
                "units": "mm",
                "items": [
                    {
                        "id": "R1",
                        "quantity": "diameter",
                        "target": {"nominal": 20.0},
                        "features": [tag("bore")],
                        "provenance": "explicit",
                    },
                    {
                        "id": "R2",
                        "quantity": "distance",
                        "target": {"nominal": 90.0},
                        "features": [tag("c1"), tag("c2")],
                        "provenance": "explicit",
                    },
                    {
                        "id": "R3",
                        "quantity": "distance",
                        "target": {"nominal": math.hypot(45.0, 25.0)},
                        "features": [tag("c1"), tag("bore")],
                        "provenance": "explicit",
                    },
                    {
                        "id": "R4",
                        "quantity": "count",
                        "target": {"exact": 4},
                        "features": [
                            {
                                "kind": "predicate",
                                "type": "circle",
                                "radius_min": 3.999,
                                "radius_max": 4.001,
                            }
                        ],
                        "provenance": "explicit",
                    },
                ],
            }
        )
        on_document = await verify_requirements(self.backend, rs)

        saved = self.artifact_dir / "requirements_roundtrip.dxf"
        await self.backend.drawing_save_as(str(saved))
        parity_before = _parity_view(saved)
        doc = ezdxf.readfile(str(saved))
        bores = [c for c in doc.modelspace().query("CIRCLE") if abs(c.dxf.radius - 10.0) < 1e-9]
        for circle in bores:
            centre = circle.dxf.center
            circle.dxf.center = (centre.x + ROUNDTRIP_MOVE_MM, centre.y, centre.z)
        doc.saveas(str(saved))
        parity_blind = _parity_view(saved) == parity_before

        on_artifact = await artifact_module.verify_artifact(str(saved), rs, backend=self.backend)
        fresh = await self._fresh_backend()
        try:
            await fresh.drawing_open(str(saved))
            reopened = await verify_requirements(fresh, rs)
        finally:
            await fresh.disconnect()

        passed = (
            on_document["status"] == "pass"
            and on_document["counts"]["pass"] == 4
            and len(bores) == 1
            and parity_blind
            and on_artifact["status"] == "fail"
            and _failed(on_artifact) == ["R3"]
            and _failed(reopened) == ["R3"]
        )
        return (
            passed,
            {
                "document": {"status": on_document["status"], "counts": on_document["counts"]},
                "artifact": {
                    "status": on_artifact["status"],
                    "counts": on_artifact["counts"],
                    "failed": _failed(on_artifact),
                    "measured": _measured(on_artifact),
                    "handles_stable": (on_artifact.get("rebinding") or {}).get("handles_stable"),
                },
                "reopened": {"status": reopened["status"], "failed": _failed(reopened)},
                "bores_moved": len(bores),
                "moved_mm": ROUNDTRIP_MOVE_MM,
                "parity_blind": parity_blind,
            },
            [str(saved)],
        )

    async def _task_cancel_safety(self):
        """A cancelled call's worker keeps the drawing until it ends (B01).

        The first call's worker blocks on an event; the awaiting task is
        cancelled - what an MCP client's Esc does - and a second call starts at
        once. The second must not run while the first worker is still inside
        the drawing: the trace is A then B. Afterwards the document answers.
        """
        entered, release = threading.Event(), threading.Event()
        trace: list[str] = []

        def held():
            entered.set()
            release.wait(10)
            trace.append("A:end")

        overlapped = None
        try:
            first = asyncio.ensure_future(self.backend._async(held))
            if not await asyncio.to_thread(entered.wait, 10):
                return False, {"error": "the first call never started"}, []
            first.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await first
            second = asyncio.ensure_future(self.backend._async(lambda: trace.append("B:run")))
            await asyncio.sleep(0.3)
            overlapped = "B:run" in trace  # B ran while A's worker was still blocked
        finally:
            release.set()
        await asyncio.wait_for(second, 10)
        line = await self.backend.entity_create_line(0, 0, 10, 0)
        count = (await self.backend.drawing_info()).entity_count
        passed = overlapped is False and trace == ["A:end", "B:run"] and count == 1
        return (
            passed,
            {
                "trace": trace,
                "overlapped": overlapped,
                "entity_count_after": count,
                "line": line.handle,
            },
            [],
        )

    async def _task_qa_large_drawing(self):
        """Every offender of a 10,000-line drawing, by handle (B02).

        1.6's checks read the first 5,000 entities of the current space and
        reported a crash as a clean result. Offenders are placed on purpose: a
        CONSTRUCTION line as the 5,001st entity, another after the 10,000th, one
        on a paper layout, and a duplicate LINE pair (one drawn backwards) last.
        Every one must come back by handle with no ``scan_incomplete`` and no
        ``check_failed``. The longest event-loop stall is reported, not gated.
        """
        import time

        from engineering.critique import CHECK_FAILED, SCAN_INCOMPLETE, run_critique

        await ensure_engineering_layers(self.backend)
        doc = self.backend._doc
        msp = doc.modelspace()
        construction: dict[str, str] = {}
        for index in range(1, 10_001):
            layer = "CONSTRUCTION" if index == 5_001 else "GEOMETRY"
            y = 10.0 * index
            line = msp.add_line((0.0, y), (100.0, y), dxfattribs={"layer": layer})
            if index == 5_001:
                construction["at_5001"] = line.dxf.handle
        construction["after_10000"] = msp.add_line(
            (-300.0, 0.0), (-200.0, 0.0), dxfattribs={"layer": "CONSTRUCTION"}
        ).dxf.handle
        construction["paper"] = (
            doc.layouts.get("Layout1")
            .add_line((0.0, 0.0), (50.0, 0.0), dxfattribs={"layer": "CONSTRUCTION"})
            .dxf.handle
        )
        twin_a = msp.add_line((500.0, -50.0), (600.0, -50.0), dxfattribs={"layer": "GEOMETRY"})
        twin_b = msp.add_line((600.0, -50.0), (500.0, -50.0), dxfattribs={"layer": "GEOMETRY"})
        duplicate = sorted([twin_a.dxf.handle, twin_b.dxf.handle])

        stalls: list[float] = []
        running = True

        async def ticker():
            last = time.perf_counter()
            while running:
                await asyncio.sleep(0.02)
                now = time.perf_counter()
                stalls.append(now - last)
                last = now

        tick = asyncio.ensure_future(ticker())
        started = time.perf_counter()
        try:
            issues = await run_critique(self.backend, ["construction_left", "duplicate_entities"])
        finally:
            running = False
            await tick
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        found_construction = {
            handle
            for issue in issues
            if issue.focus == "construction_left" and not (issue.detail or {}).get("code")
            for handle in issue.handles
        }
        found_duplicates = [
            sorted(issue.handles)
            for issue in issues
            if issue.focus == "duplicate_entities" and not (issue.detail or {}).get("code")
        ]
        incomplete = sorted(
            {
                str((issue.detail or {}).get("code"))
                for issue in issues
                if (issue.detail or {}).get("code") in (SCAN_INCOMPLETE, CHECK_FAILED)
            }
        )
        missing = [
            name for name, handle in construction.items() if handle not in found_construction
        ]
        if duplicate not in found_duplicates:
            missing.append("duplicate_pair")
        passed = not missing and not incomplete
        return (
            passed,
            {
                "entities": len(msp) + len(doc.layouts.get("Layout1")),
                "construction": construction,
                "duplicate": duplicate,
                "missing": missing,
                "incomplete": incomplete,
                "elapsed_ms": round(elapsed_ms, 1),
                "max_loop_stall_ms": round(max(stalls, default=0.0) * 1000.0, 1),
            },
            [],
        )

    async def _task_export_safety(self):
        """Drawing text that starts like a formula leaves as text (B03).

        Two exporters, fed from the drawing rather than from rows typed beside
        it: the P&ID line list of a sheet whose authored line number is
        ``=1+1`` (read back off the drawing by the graph reader), and the parts
        list read off block references whose attributes and ACADMCP_MECH payload
        carry the hostile texts and a genuine ``-12.5`` mass. In CSV no cell a
        spreadsheet would evaluate may remain; in XLSX every text cell is a
        string cell and the mass a numeric one.
        """
        from openpyxl import load_workbook

        from engineering.layers import apply_layer_set
        from engineering.mech.xdata import APP_ID, PAYLOAD_VERSION, to_values
        from engineering.pid.deliverables import LINE_COLUMNS, line_list
        from engineering.pid.deliverables import write_csv as write_pid_csv
        from engineering.pid.spec import EXAMPLE_SPEC, run_spec
        from engineering.sheet.bom import extract_records, rows_from_records
        from engineering.sheet.extract import write_rows

        out = self.artifact_dir / "export_safety"
        out.mkdir(parents=True, exist_ok=True)

        # 1. the P&ID line list: the authored number is read back off the drawing
        await apply_layer_set(self.backend, "pid")
        spec = copy.deepcopy(EXAMPLE_SPEC)
        for line in spec["lines"]:
            if line.get("number"):
                line["number"] = HOSTILE_TEXT[0]
        drawn = await run_spec(self.backend, spec)
        lines = line_list(drawn["graph"])
        lines_csv = out / "lines.csv"
        write_pid_csv(lines, str(lines_csv), LINE_COLUMNS)
        line_rows = _read_csv(lines_csv)
        numbers = [row[LINE_COLUMNS.index("line_number")] for row in line_rows[1:]]
        pid_ok = numbers.count("'" + HOSTILE_TEXT[0]) == 2

        # 2. the parts list: block attributes and a std_part payload
        await self.backend.drawing_new()
        await ensure_engineering_layers(self.backend)
        await self.backend.block_define(
            "BENCH_PART",
            [{"type": "line", "x1": 0, "y1": 0, "x2": 10, "y2": 0}],
            [
                {"tag": "DESIGNATION", "x": 0, "y": 3, "height": 2.5},
                {"tag": "MATERIAL", "x": 0, "y": 6, "height": 2.5},
            ],
        )
        for index, text in enumerate(HOSTILE_TEXT[:3]):
            await self.backend.block_insert(
                "BENCH_PART",
                20.0 * index,
                0.0,
                attributes={"DESIGNATION": text, "MATERIAL": HOSTILE_TEXT[3]},
            )
        payload_ref = await self.backend.block_insert("BENCH_PART", 100.0, 0.0)
        payload = {
            "v": PAYLOAD_VERSION,
            "kind": "std_part",
            "designation": "@NOW()",
            "standard": "-ISO 4014",
            "qty": 1,
            "material": "=HYPERLINK(1)",
            "mass": "-12.5",
        }
        await self.backend.entity_set_xdata(payload_ref.handle, APP_ID, to_values(payload))
        columns = ("item", "qty", "designation", "standard", "material", "mass")
        rows = rows_from_records(await extract_records(self.backend), columns=columns)
        bom_csv, bom_xlsx = out / "bom.csv", out / "bom.xlsx"
        write_rows(rows, str(bom_csv), columns=columns, fmt="csv")
        write_rows(rows, str(bom_xlsx), columns=columns, fmt="xlsx")
        bom_rows = _read_csv(bom_csv)
        masses = [row[columns.index("mass")] for row in bom_rows[1:]]
        bom_csv_ok = "-12.5" in masses and len(bom_rows) == 1 + len(rows) == 5

        sheet = load_workbook(bom_xlsx).active
        formula_xlsx = []
        texts = set()
        numeric_mass = None
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                if cell.data_type == "f":
                    formula_xlsx.append({"cell": cell.coordinate, "value": cell.value})
                elif cell.data_type == "s":
                    texts.add(cell.value)
            mass = row[columns.index("mass")]
            if mass.data_type == "n" and mass.value == -12.5:
                numeric_mass = mass.value
        wanted = {*HOSTILE_TEXT, "@NOW()", "-ISO 4014", "=HYPERLINK(1)"}
        xlsx_ok = not formula_xlsx and wanted <= texts and numeric_mass == -12.5

        formula_cells = _formula_cells(line_rows, "lines.csv") + _formula_cells(bom_rows, "bom.csv")
        passed = pid_ok and bom_csv_ok and xlsx_ok and not formula_cells
        return (
            passed,
            {
                "pid_csv": {"ok": pid_ok, "line_numbers": numbers},
                "bom_csv": {"ok": bom_csv_ok, "rows": len(bom_rows) - 1, "masses": masses},
                "bom_xlsx": {
                    "ok": xlsx_ok,
                    "formula_cells": formula_xlsx,
                    "mass": numeric_mass,
                    "texts_missing": sorted(wanted - texts),
                },
                "formula_cells": formula_cells,
            },
            [str(lines_csv), str(bom_csv), str(bom_xlsx)],
        )

    async def _task_stale_preview(self):
        """A preview, an edit behind the server's back, a refused apply (Task 12).

        Through the real ``cad_batch`` tool: a dry run fingerprints the line the
        batch would move; the line's colour is then changed directly in the
        document, as an operator or a second program would - the content
        revision cannot see that, the exact fingerprint does. The apply carrying
        the preview must be refused ``stale_revision`` naming that handle, and
        the line must not move. A fresh preview then applies.
        """
        line = await self.backend.entity_create_line(0, 0, 10, 0)
        handle = line.handle
        steps = [{"tool": "entity_move", "args": {"handle": handle, "dx": 5.0, "dy": 0.0}}]

        def start() -> tuple[float, float]:
            point = self.backend._doc.entitydb[handle].dxf.start
            return (point.x, point.y)

        async with self._served() as client:
            dry = await client.call_tool("cad_batch", {"steps": steps, "dry_run": True})
            preview = dry.structured_content["preview"]
            self.backend._doc.entitydb[handle].dxf.color = 5  # behind the server's back
            stale = await client.call_tool(
                "cad_batch",
                {
                    "steps": steps,
                    "expected_revision": preview["revision"],
                    "preconditions": preview["preconditions"],
                },
                raise_on_error=False,
            )
            payload = stale.structured_content or {}
            refused_kind = payload.get("kind") if stale.is_error else None
            changed = [row.get("handle") for row in payload.get("changed") or []]
            after_stale = start()
            moved_over = after_stale != (0.0, 0.0)

            again = await client.call_tool("cad_batch", {"steps": steps, "dry_run": True})
            fresh = again.structured_content["preview"]
            applied = await client.call_tool(
                "cad_batch",
                {
                    "steps": steps,
                    "expected_revision": fresh["revision"],
                    "preconditions": fresh["preconditions"],
                },
                raise_on_error=False,
            )
            applied_ok = not applied.is_error and (applied.structured_content or {}).get("ok")
        final = start()
        passed = (
            refused_kind == "stale_revision"
            and changed == [handle]
            and not moved_over
            and bool(applied_ok)
            and final == (5.0, 0.0)
        )
        return (
            passed,
            {
                "refused_kind": refused_kind,
                "changed_handles": changed,
                "moved_over_a_stale_preview": moved_over,
                "fresh_apply_ok": bool(applied_ok),
                "start_after_the_refused_apply": list(after_stale),
                "final_start": list(final),
            },
            [],
        )

    async def _task_revision_guarded_edit(self):
        """beiming183's ground: every edit names the revision it was planned on.

        Two ``cad_batch`` edits, each carrying the revision read before it,
        must each move the revision forward; a third carrying the FIRST
        revision - planned before the other two landed - must be refused
        ``stale_revision`` with the expected and actual revisions, and change
        nothing: neither the line nor the revision moves.
        """
        line = await self.backend.entity_create_line(0, 0, 10, 0)
        handle = line.handle

        def revision() -> int:
            return int(self.backend.document_identity()["revision"])

        def start() -> tuple[float, float]:
            point = self.backend._doc.entitydb[handle].dxf.start
            return (point.x, point.y)

        steps = [{"tool": "entity_move", "args": {"handle": handle, "dx": 1.0, "dy": 0.0}}]
        revisions = [revision()]
        async with self._served() as client:
            oks = []
            for _ in range(2):
                result = await client.call_tool(
                    "cad_batch",
                    {"steps": steps, "expected_revision": revisions[-1]},
                    raise_on_error=False,
                )
                oks.append(not result.is_error and (result.structured_content or {}).get("ok"))
                revisions.append(revision())
            before = start()
            stale = await client.call_tool(
                "cad_batch",
                {"steps": steps, "expected_revision": revisions[0]},
                raise_on_error=False,
            )
            payload = stale.structured_content or {}
        stale_refused = bool(stale.is_error) and payload.get("kind") == "stale_revision"
        unchanged = start() == before and revision() == revisions[-1]
        forward = all(b > a for a, b in zip(revisions, revisions[1:], strict=False))
        passed = (
            all(oks)
            and forward
            and stale_refused
            and payload.get("expected_revision") == revisions[0]
            and payload.get("actual_revision") == revisions[-1]
            and unchanged
            and before == (2.0, 0.0)
        )
        return (
            passed,
            {
                "revisions": revisions,
                "edits_applied": [bool(ok) for ok in oks],
                "stale_refused": stale_refused,
                "refusal": {
                    key: payload.get(key)
                    for key in ("kind", "expected_revision", "actual_revision")
                },
                "unchanged_after_refusal": unchanged,
                "start_after_edits": list(before),
            },
            [],
        )

    async def _task_pid_file_roundtrip(self):
        """puran-water's ground: a P&ID sheet survives its own file.

        ``engineering/pid/spec.py::EXAMPLE_SPEC`` is drawn, saved as DXF and
        opened by a FRESH engine, whose reader never saw the spec or the first
        engine. The graph read off the file must equal the graph read off the
        drawing it came from: the same tags, the same edges (from, to, class),
        the same line numbers, the same instrument index and equipment list,
        nothing dangling.
        """
        from engineering.layers import apply_layer_set
        from engineering.pid.deliverables import equipment_list, instrument_index, line_list
        from engineering.pid.graph import build_graph
        from engineering.pid.spec import EXAMPLE_SPEC, run_spec

        await apply_layer_set(self.backend, "pid")
        drawn = await run_spec(self.backend, EXAMPLE_SPEC)
        saved = self.artifact_dir / "pid_file_roundtrip.dxf"
        await self.backend.drawing_save_as(str(saved))
        fresh = await self._fresh_backend()
        try:
            await fresh.drawing_open(str(saved))
            read = await build_graph(fresh)
        finally:
            await fresh.disconnect()

        def tags(graph):
            return sorted(str(node.get("tag") or "") for node in graph["nodes"])

        def edges(graph):
            return sorted(
                (str(row["from_tag"]), str(row["to_tag"]), str(row["line_class"]))
                for row in line_list(graph)
            )

        def numbers(graph):
            return sorted(str(row["line_number"]) for row in line_list(graph))

        def index(graph):
            return [(row["tag"], row.get("connected_to")) for row in instrument_index(graph)]

        def equipment(graph):
            return [row.get("tag") for row in equipment_list(graph)]

        original = drawn["graph"]
        result = {
            "nodes": len(read["nodes"]),
            "edges": len(read["edges"]),
            "dangling": read["stats"]["dangling"],
            "tags_equal": tags(read) == tags(original),
            "edges_equal": edges(read) == edges(original),
            "line_numbers_equal": numbers(read) == numbers(original),
            "index_equal": index(read) == index(original),
            "equipment_equal": equipment(read) == equipment(original),
        }
        passed = (
            result["nodes"] == len(original["nodes"]) == 5
            and result["edges"] == len(original["edges"]) == 4
            and result["dangling"] == 0
            and all(result[key] for key in result if key.endswith("_equal"))
        )
        return passed, result, [str(saved)]

    async def _task_foreign_edit_verified(self):
        """A drawing somebody else made: measured, repaired, accepted on file.

        The plate is written with raw ezdxf - no record of ours, its own
        handles - and opened. Requirements select by geometry (a region and a
        size window, since there are no tags): four Ø10 holes, exactly four
        holes, a 60 pitch. H3 is drawn Ø9 and must be the one failure. The
        repair is a ``cad_batch`` ``entity_edit_geometry`` applied under the
        dry run's fingerprint precondition; the saved file must then pass every
        requirement, and a diff against the original must show exactly one
        changed entity, nothing added or removed.
        """
        import ezdxf

        from engineering.understand.diff import diff_snapshots
        from engineering.understand.snapshot import read_snapshot
        from engineering.verification.artifact import verify_artifact
        from engineering.verification.report import verify_requirements
        from engineering.verification.requirements import parse_requirements

        original = self.artifact_dir / "foreign_plate.dxf"
        doc = ezdxf.new("R2010", units=4)
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (100, 0), (100, 60), (0, 60)], close=True)
        for _name, x, y, radius in FOREIGN_HOLES:
            msp.add_circle((x, y), radius)
        msp.add_text("PLATE 100 x 60", height=2.5).set_placement((5, 52))
        doc.saveas(str(original))
        await self.backend.drawing_open(str(original))

        def hole(x: float, y: float) -> dict:
            return {
                "kind": "predicate",
                "type": "circle",
                "region": [x - 6.0, y - 6.0, x + 6.0, y + 6.0],
                "radius_min": 3.0,
                "radius_max": 7.0,
            }

        items = [
            {
                "id": name,
                "quantity": "diameter",
                "target": {"nominal": 10.0},
                "features": [hole(x, y)],
                "provenance": "explicit",
            }
            for name, x, y, _radius in FOREIGN_HOLES
        ]
        items.append(
            {
                "id": "N",
                "quantity": "count",
                "target": {"exact": 4},
                "features": [
                    {"kind": "predicate", "type": "circle", "radius_min": 3.0, "radius_max": 7.0}
                ],
                "provenance": "explicit",
            }
        )
        items.append(
            {
                "id": "P",
                "quantity": "distance",
                "target": {"nominal": 60.0},
                "features": [hole(20.0, 20.0), hole(80.0, 20.0)],
                "provenance": "explicit",
            }
        )
        rs = parse_requirements({"units": "mm", "items": items})
        before = await verify_requirements(self.backend, rs)
        failing = [row for row in before["rows"] if row["status"] == "fail"]
        handle = failing[0]["handles"][0] if len(failing) == 1 and failing[0]["handles"] else None

        applied_ok = False
        if handle is not None:
            steps = [{"tool": "entity_edit_geometry", "args": {"handle": handle, "radius": 5.0}}]
            async with self._served() as client:
                dry = await client.call_tool("cad_batch", {"steps": steps, "dry_run": True})
                preview = dry.structured_content["preview"]
                result = await client.call_tool(
                    "cad_batch",
                    {
                        "steps": steps,
                        "expected_revision": preview["revision"],
                        "preconditions": preview["preconditions"],
                    },
                    raise_on_error=False,
                )
                applied_ok = bool(
                    not result.is_error and (result.structured_content or {}).get("ok")
                )
        repaired = self.artifact_dir / "foreign_plate_repaired.dxf"
        await self.backend.drawing_save_as(str(repaired))
        after = await verify_artifact(str(repaired), rs, backend=self.backend)
        diff = diff_snapshots(read_snapshot(str(original)), read_snapshot(str(repaired)))
        counts = {key: len(diff[key]) for key in ("changed", "added", "removed")}
        passed = (
            _failed(before) == ["H3"]
            and applied_ok
            and after["status"] == "pass"
            and after["counts"]["pass"] == len(items)
            and counts == {"changed": 1, "added": 0, "removed": 0}
        )
        return (
            passed,
            {
                "before": {"status": before["status"], "failed": _failed(before)},
                "repaired_handle": handle,
                "precondition_held": applied_ok,
                "after": {
                    "status": after["status"],
                    "failed": _failed(after),
                    "measured": _measured(after),
                },
                "diff": counts,
            },
            [str(original), str(repaired)],
        )

    async def _task_damaged_dxf_refusal(self):
        """A damaged file is refused by name and the open work is kept.

        The default ``drawing_open`` does not recover a damaged DXF; what it
        must do is refuse honestly (since 1.8.0 the refusal names
        ``recover=True``, B09, and that opt-in recovers the truncated file -
        ``tests/test_identity_lineage.py``). A valid 50-line DXF is cut in half
        (no ENDSEC, no EOF) and a text file is given a .dxf name. Opening each
        must raise an error that names the fault (DXF in its text), and the
        document that was open - one line - must still be the current document,
        unchanged: never an empty or half-read drawing reported as opened.
        """
        import ezdxf

        await self.backend.entity_create_line(0, 0, 10, 0)
        identity = dict(self.backend.document_identity())
        count = (await self.backend.drawing_info()).entity_count

        intact = self.artifact_dir / "intact.dxf"
        doc = ezdxf.new("R2010", units=4)
        for index in range(50):
            doc.modelspace().add_line((0, index), (10, index))
        doc.saveas(str(intact))
        data = intact.read_bytes()
        truncated = self.artifact_dir / "truncated.dxf"
        truncated.write_bytes(data[: len(data) // 2])
        not_dxf = self.artifact_dir / "not_a_drawing.dxf"
        not_dxf.write_bytes(b"this is not a drawing\n" * 8)

        cases: dict[str, dict] = {}
        for name, path in (("truncated", truncated), ("not_dxf", not_dxf)):
            try:
                await self.backend.drawing_open(str(path))
                cases[name] = {"refused": False, "error": None, "named": False}
            except Exception as exc:  # noqa: BLE001 - the refusal is the measurement
                # the file by name: a published report carries no machine layout
                error = f"{type(exc).__name__}: {exc}".replace(str(path), path.name)
                cases[name] = {"refused": True, "error": error, "named": "DXF" in error.upper()}
        identity_after = dict(self.backend.document_identity())
        count_after = (await self.backend.drawing_info()).entity_count
        document_kept = (
            identity_after.get("document_id") == identity.get("document_id")
            and identity_after.get("revision") == identity.get("revision")
            and count_after == count == 1
        )
        passed = all(case["refused"] and case["named"] for case in cases.values()) and document_kept
        return (
            passed,
            {
                "cases": cases,
                "document_kept": document_kept,
                "entity_count_after": count_after,
                "recover": "opt-in: drawing_open(recover=True), 1.8.0 (B09)",
            },
            [str(truncated), str(not_dxf)],
        )

    async def _task_dimension_binding(self):
        """best-cad-mcp's claim: a dimension bound to what it measures.

        Four DIMENSIONs over isolated features, written with raw ezdxf: D1
        honest over 42; D2 over 40 with its text overridden to "42"; D3 over 21
        with DIMLFAC 2 (the sheet shows 42); D4 an honest Ø40. Each requirement
        names its DIMENSION; the row is measured on the geometry the defpoints
        land on (42, 40, 21, 40 - all pass), and D2 and D3 are
        ``dimension_mismatch`` findings that block acceptance: the annotation
        is judged separately from the geometry and the lie is visible.
        """
        import ezdxf

        from engineering.verification.report import verify_requirements
        from engineering.verification.requirements import parse_requirements

        doc = ezdxf.new("R2010", setup=True, units=4)
        msp = doc.modelspace()
        handles: dict[str, str] = {}

        def linear(name, x0, width, **kw):
            msp.add_line((x0, 0), (x0, 20))
            msp.add_line((x0 + width, 0), (x0 + width, 20))
            dim = msp.add_linear_dim(
                base=(x0, 30), p1=(x0, 0), p2=(x0 + width, 0), dimstyle="Standard", **kw
            )
            dim.render()
            handles[name] = dim.dimension.dxf.handle

        linear("D1", 0.0, 42.0)
        linear("D2", 100.0, 40.0, text="42")
        linear("D3", 200.0, 21.0, override={"dimlfac": 2.0})
        msp.add_circle((350, 10), 20)
        # "Standard", not ezdxf's EZ_RADIUS: that style carries DIMLFAC 100 (a
        # metre drawing dimensioned in cm), which is exactly the lie D3 stages
        diameter = msp.add_diameter_dim(center=(350, 10), radius=20, angle=0, dimstyle="Standard")
        diameter.render()
        handles["D4"] = diameter.dimension.dxf.handle
        path = self.artifact_dir / "dimension_binding.dxf"
        doc.saveas(str(path))
        await self.backend.drawing_open(str(path))

        targets = {"D1": ("distance", 42.0), "D2": ("distance", 40.0)}
        targets.update({"D3": ("distance", 21.0), "D4": ("diameter", 40.0)})
        rs = parse_requirements(
            {
                "units": "mm",
                "items": [
                    {
                        "id": name,
                        "quantity": quantity,
                        "target": {"nominal": nominal},
                        "features": [{"kind": "dimension", "handle": handles[name]}],
                        "provenance": "explicit",
                    }
                    for name, (quantity, nominal) in targets.items()
                ],
            }
        )
        report = await verify_requirements(self.backend, rs)
        rows = {
            row["id"]: {
                "status": row["status"],
                "measured": (row.get("measured") or {}).get("value"),
                "displayed": (row.get("dimension") or {}).get("displayed_value"),
                "dimension_findings": (row.get("dimension") or {}).get("findings"),
            }
            for row in report["rows"]
        }
        findings = {
            str(finding.get("requirement")): str(finding.get("kind"))
            for finding in report.get("findings") or []
        }
        geometry_ok = all(rows[name]["status"] == "pass" for name in targets) and all(
            rows[name]["measured"] is not None and abs(rows[name]["measured"] - nominal) < 1e-9
            for name, (_quantity, nominal) in targets.items()
        )
        passed = (
            geometry_ok
            and findings == {"D2": "dimension_mismatch", "D3": "dimension_mismatch"}
            and rows["D2"]["displayed"] == 42.0
            and rows["D3"]["displayed"] == 42.0
            and report["accepted"] is False
        )
        return (
            passed,
            {"rows": rows, "findings": findings, "accepted": report["accepted"]},
            [str(path)],
        )

    # ── v9 (v1.8.0) ───────────────────────────────────────────────────────────

    async def _task_dimension_lie_detect(self):
        """1.8-15: one ``dimension_truth`` finding per lie, on the dimension telling it.

        The fixture (``tests/fixtures/lying_dims.py``) draws seven dimensions,
        each lying one way, and says which code each must earn; the expected
        set is that map, fixed before the critique runs. A 19.6 mm dimension
        under DIMDEC 0 must read "20" with a display rounding of 0.4, and the
        twin - DIMDEC 2 -> 0 with ``rerender=False`` - must leave exactly that
        dimension ``dim_render_stale`` until ``drawing_refine`` redraws it.
        """
        from engineering.critique import run_critique
        from engineering.qa_scan import qa_snapshot
        from engineering.verification.binding import bind_dimension
        from engineering.verification.measure import default_epsilon
        from engineering.verification.observe import observe
        from tests.fixtures.lying_dims import build_lying_dims

        expected = await build_lying_dims(self.backend)
        issues = await run_critique(self.backend, ["dimension_truth"])
        got = sorted((str(i.detail.get("code")), i.handles[0]) for i in issues)
        lies_ok = got == sorted(expected.items())

        await self._reset()
        msp = self.backend._doc.modelspace()
        msp.add_line((0, 0), (0, 20))
        msp.add_line((19.6, 0), (19.6, 20))
        dim = await self.backend.dimension_linear(0, 0, 19.6, 0, 10, -10)
        modified = await self.backend.dimstyle_modify("Standard", {"DIMDEC": 0}, rerender=False)
        snap, _ = await qa_snapshot(self.backend, {})
        observed = observe(snap)
        (feature,) = [f for f in observed.features if f.type == "dimension"]
        bound = bind_dimension(feature, observed, epsilon=default_epsilon(observed))
        shows_20 = (
            bound["displayed_text"] == "20"
            and abs((bound.get("display_rounding") or 0.0) - 0.4) < 1e-9
            and bound["mismatch"] is False
        )
        stale = await run_critique(self.backend, ["dimension_truth"])
        stale_named = [(str(i.detail.get("code")), i.handles[0]) for i in stale] == [
            ("dim_render_stale", dim.handle)
        ]
        refined = await refine_drawing(self.backend, focus=["dimension_truth"], min_score=100.0)
        cleared = refined.remaining_issues == []
        return (
            lies_ok
            and shows_20
            and modified["rerender_required"] is True
            and stale_named
            and cleared,
            {
                "expected": expected,
                "found": got,
                "displayed_text": bound["displayed_text"],
                "display_rounding": bound.get("display_rounding"),
                "stale_named": stale_named,
                "refine_status": refined.status,
            },
            [],
        )

    async def _task_policy_read_only(self):
        """READ_ONLY refuses a write before it runs, on both routes (1.8-17).

        Through the real server: with ``READ_ONLY`` on, a ``cad_batch`` whose
        one step is ``entity_create_line`` must be refused ``policy_refused``
        (policy ``read_only``) before step 1 - the content revision and the
        entity count read off the document itself stay what they were. The
        broken-input twin sends the same write through the search proxy,
        ``call_tool("entity_create_line")``, and must be refused the same way.
        The expected values are fixed here, not read from the server: one
        line drawn before the policy, revision unchanged by the refusals.
        """
        import config
        import server

        await self.backend.entity_create_line(0, 0, 10, 0)
        before_revision = self.backend.document_identity()["revision"]
        before_count = len(await self.backend.entity_list())
        line = {"x1": 0.0, "y1": 0.0, "x2": 5.0, "y2": 5.0}
        saved = config.settings.read_only
        config.settings.read_only = True
        try:
            async with self._served() as client:
                batch = await client.call_tool(
                    "cad_batch",
                    {"steps": [{"tool": "entity_create_line", "args": line}]},
                    raise_on_error=False,
                )
            server._apply_discovery_mode("search")
            try:
                async with self._served() as client:
                    proxied = await client.call_tool(
                        "call_tool",
                        {"name": "entity_create_line", "arguments": line},
                        raise_on_error=False,
                    )
            finally:
                server._apply_discovery_mode("off")
        finally:
            config.settings.read_only = saved
            await server._apply_tool_profile("full")
        after_revision = self.backend.document_identity()["revision"]
        after_count = len(await self.backend.entity_list())
        batch_payload = batch.structured_content or {}
        proxy_payload = proxied.structured_content or {}
        passed = (
            batch.is_error
            and batch_payload.get("kind") == "policy_refused"
            and batch_payload.get("policy") == "read_only"
            and proxied.is_error
            and proxy_payload.get("kind") == "policy_refused"
            and proxy_payload.get("tool") == "entity_create_line"
            and after_revision == before_revision
            and after_count == before_count == 1
        )
        return (
            passed,
            {
                "batch_kind": batch_payload.get("kind"),
                "batch_policy": batch_payload.get("policy"),
                "proxy_kind": proxy_payload.get("kind"),
                "revision": [before_revision, after_revision],
                "entities": [before_count, after_count],
            },
            [],
        )

    async def _call_budget_arm(self, scenario_id: str, work: Path):
        """One least-call oracle of ``benchmarks/call_budget.py`` through the real
        server on this adapter's backend: ``(counted tools, answer, verified,
        detail)``. The verifier reads the drawing back outside the count."""
        from benchmarks import call_budget as cb

        scenario = cb.scenario_by_id(scenario_id)
        work.mkdir(parents=True, exist_ok=True)
        async with self._served() as client:
            ctx = await scenario.setup(client, work)
            rec = cb.Recorder(client)
            try:
                answer = await scenario.batched(rec, ctx)
                verified, detail = await scenario.verify(client, ctx, answer)
            except Exception as exc:  # an arm that errors did not do the job
                answer, verified, detail = None, False, f"{type(exc).__name__}: {exc}"[:500]
        return [call.tool for call in rec.calls], answer, verified, detail

    async def _task_layer_cleanup_budget(self):
        """The rival's H4 in at most 3 calls (1.8-12 verbs, 1.8-13 oracle).

        The ceiling is plan section 4's, fixed before the measurement. The
        verifier holds the drawing to ISO names, ByLayer, no empty layer and an
        empty iso128 / layer_color / property_override critique. The
        broken-input twin: a reopened copy, an ``entity_modify_where`` whose
        ``expect_count`` is one more than the 300 placed on purpose - refused
        ``stale_revision``, the revision and every colour unchanged.
        """
        from benchmarks import call_budget as cb

        ceiling = 3
        work = self.artifact_dir / "layer_cleanup_budget"
        calls, _answer, verified, detail = await self._call_budget_arm("layer_cleanup_300", work)
        (work / "twin").mkdir(parents=True, exist_ok=True)
        async with self._served() as client:
            await cb._setup_messy(client, work / "twin")
            before = (await client.call_tool("drawing_info", {})).structured_content["revision"]
            refused = await client.call_tool(
                "entity_modify_where",
                {
                    "where": {"type": "*"},
                    "set": {"color": 256, "lineweight": "ByLayer"},
                    "space": "all",
                    "expect_count": cb.MESSY_ENTITIES + 1,
                },
                raise_on_error=False,
            )
            after = (await client.call_tool("drawing_info", {})).structured_content["revision"]
            rows = await cb._all_entities(client)
        overridden = sum(1 for row in rows if row.get("color") != 256)
        twin_ok = (
            refused.is_error
            and (refused.structured_content or {}).get("kind") == "stale_revision"
            and before == after
            and overridden == cb.MESSY_ENTITIES
        )
        passed = verified and len(calls) <= ceiling and twin_ok
        return (
            passed,
            {
                "calls": len(calls),
                "ceiling": ceiling,
                "tools": calls,
                "detail": detail,
                "twin": {
                    "kind": (refused.structured_content or {}).get("kind"),
                    "revision": [before, after],
                    "still_overridden": overridden,
                },
            },
            [],
        )

    async def _task_lisp_read_tier(self):
        """Three AutoLISP reads run on the first try; a LISP erase is refused (1.8-18).

        Through the real server, on this adapter's engine: a circle of radius 5
        is drawn; ``(vla-get-Area (vlax-ename->vla-object (handent H)))`` must
        answer the area ``analysis_measure_entity`` reads (25 pi, fixed here),
        ``(getvar "CLAYER")`` the value ``system_get_variable`` reads, and
        ``(mapcar (function (lambda (x) (* x 2))) (list 1 2 3))`` ``(2 4 6)`` -
        each on its first call. The broken-input twin ``(command "_ERASE" "_L"
        "")`` must be refused ``policy_refused`` with symbol ``command`` at line
        1, column 2, and ``entity_delete`` among the alternatives. The reads
        need AutoCAD: on the headless lane the task is ``environment_blocked``
        and its metrics carry only the policy's verdicts.
        """
        import math

        circle = await self.backend.entity_create_circle(0.0, 0.0, 5.0)
        reads = {
            "area": f'(vla-get-Area (vlax-ename->vla-object (handent "{circle.handle}")))',
            "clayer": '(getvar "CLAYER")',
            "mapcar": "(mapcar (function (lambda (x) (* x 2))) (list 1 2 3))",
        }
        twin = '(command "_ERASE" "_L" "")'
        async with self._served() as client:
            answers = {
                key: await client.call_tool(
                    "system_run_lisp", {"expression": form}, raise_on_error=False
                )
                for key, form in reads.items()
            }
            refused = await client.call_tool(
                "system_run_lisp", {"expression": twin}, raise_on_error=False
            )
            measured = (
                await client.call_tool(
                    "analysis_measure_entity", {"handle": circle.handle}, raise_on_error=False
                )
            ).structured_content or {}
            clayer = (
                await client.call_tool(
                    "system_get_variable", {"name": "CLAYER"}, raise_on_error=False
                )
            ).structured_content or {}
        twin_payload = refused.structured_content or {}
        twin_ok = (
            refused.is_error
            and twin_payload.get("kind") == "policy_refused"
            and twin_payload.get("policy") == "lisp_allowlist"
            and twin_payload.get("symbol") == "command"
            and (twin_payload.get("line"), twin_payload.get("column")) == (1, 2)
            and "entity_delete"
            in {row.get("tool") for row in twin_payload.get("alternatives") or []}
        )
        policy_passed = {key: not result.is_error for key, result in answers.items()}
        metrics = {
            "policy_passed": policy_passed,
            "twin": {k: twin_payload.get(k) for k in ("kind", "symbol", "line", "column")},
        }
        if self.backend_name == "ezdxf":
            return TaskResult(
                "lisp_read_tier",
                "environment_blocked",
                0.0,
                "AutoLISP runs only in AutoCAD (COM); the headless engine has no LISP",
                metrics={**metrics, "twin_ok": twin_ok},
            )
        values = {
            key: (result.structured_content or {}).get("result") for key, result in answers.items()
        }
        area_expected = math.pi * 25.0

        def _near(text, want) -> bool:
            try:
                return abs(float(text) - want) <= 1e-6 * max(1.0, abs(want))
            except (TypeError, ValueError):
                return False

        clayer_value = clayer.get("value", clayer.get("result"))
        passed = (
            all(policy_passed.values())
            and _near(values["area"], area_expected)
            and _near(values["area"], float(measured.get("area") or 0.0))
            and str(values["clayer"]) == str(clayer_value)
            and values["mapcar"] == "(2 4 6)"
            and twin_ok
        )
        return (passed, {**metrics, "values": values, "measured_area": measured.get("area")}, [])

    async def _task_call_count_dimension_part(self):
        """The rival's H5 in at most 2 calls (``dimension_auto`` v2, 1.8-13).

        The plate's sizes come from the fixture (four edges, six Ø11 holes, two
        R8 arcs), not from the server. The verifier binds every dimension
        through ``drawing_verify_requirements`` (no ``dimension_mismatch``),
        runs ``mech_duplicate_dimension``'s own test and a second
        ``dimension_auto`` that must draw nothing. The broken-input twin: an
        ELLIPSE and a SPLINE on the part's layer must come back in ``omitted``
        with their reasons.
        """
        import ezdxf

        ceiling = 2
        work = self.artifact_dir / "call_count_dimension_part"
        calls, answer, verified, detail = await self._call_budget_arm("dimension_plate", work)
        twin_path = work / "twin.dxf"
        doc = ezdxf.new("R2018")
        doc.layers.add("GEOMETRY")
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (40, 0), (40, 20), (0, 20)], close=True,
                           dxfattribs={"layer": "GEOMETRY"})  # fmt: skip
        ellipse = msp.add_ellipse((20, 10), major_axis=(6, 0), ratio=0.5,
                                  dxfattribs={"layer": "GEOMETRY"})  # fmt: skip
        spline = msp.add_spline([(50, 0), (55, 8), (60, 0)], dxfattribs={"layer": "GEOMETRY"})
        doc.saveas(twin_path)
        async with self._served() as client:
            await client.call_tool("drawing_open", {"path": str(twin_path)})
            twin = (
                await client.call_tool("dimension_auto", {"selector": {"layer": "GEOMETRY"}})
            ).structured_content or {}
        omitted = {row["handle"]: row for row in twin.get("omitted") or []}
        twin_ok = (
            set(omitted) == {ellipse.dxf.handle, spline.dxf.handle}
            and all(row.get("reason") for row in omitted.values())
            and len(twin.get("created") or []) == 4
        )
        created = len((answer or {}).get("auto", {}).get("created") or [])
        passed = verified and len(calls) <= ceiling and twin_ok
        return (
            passed,
            {
                "calls": len(calls),
                "ceiling": ceiling,
                "tools": calls,
                "created": created,
                "detail": detail,
                "twin_omitted": sorted(row["type"] for row in omitted.values()),
            },
            [],
        )

    async def _task_pid_project_offpage(self):
        """Three sheet files, four cross-sheet defects, each once (1.8-21).

        The sheets are drawn by ``pid_from_spec`` on a fresh drawing each and
        saved; the truth (which connector and which pump carry each defect) is
        the handle map ``pid_from_spec`` returned, never the checker's output.
        ``pid_project`` reads the files through the real server with
        ``include_current=False``. The broken-input twins: a manifest naming a
        file that does not exist (``file_not_found``) and 51 sheets
        (``invalid_args``), both before anything is read.
        """
        work = self.artifact_dir / "pid_project_offpage"
        work.mkdir(parents=True, exist_ok=True)
        sheet = {"layer_set": "pid", "sheet_size": "A3", "scale": 1.0}

        def pump(item_id, y):
            return {"id": item_id, "symbol": "centrifugal_pump", "x": 100, "y": y, "tag": item_id}

        def conn(item_id, direction, link, port, number, y):
            end = {"from": port} if direction == "out" else {"to": port}
            return {
                "id": item_id,
                "direction": direction,
                "x": 200 if direction == "out" else 20,
                "y": y + (40 if direction == "out" else 0),
                "tag": "CONTINUATION",
                "link": link,
                "number": number,
                **end,
            }

        specs = {
            "P-001": {
                "sheet": sheet,
                "equipment": [pump("P-101", 100), pump("P-102", 200), pump("P-103", 300)],
                "connectors": [
                    conn("A1", "out", "L-1", "P-101.discharge", "100-P-1", 100),
                    conn("A2", "out", "L-2", "P-102.discharge", "100-P-2", 200),
                    conn("A3", "out", "L-3", "P-103.discharge", "100-P-3", 300),
                ],
            },
            "P-002": {
                "sheet": sheet,
                "equipment": [pump("P-201", 100), pump("P-202", 200)],
                "connectors": [
                    conn("B1", "in", "L-1", "P-201.suction", "100-P-1", 100),
                    conn("B2", "in", "L-2", "P-202.suction", "100-P-9", 200),
                    conn("B4", "out", "L-4", "P-202.discharge", "100-P-4", 200),
                ],
            },
            "P-003": {
                "sheet": sheet,
                "equipment": [pump("P-101", 100)],
                "connectors": [conn("C1", "out", "L-3", "P-101.discharge", "100-P-3", 100)],
            },
        }
        handles: dict[str, dict] = {}
        manifest = {"project": "offpage", "sheets": []}
        async with self._served() as client:
            for number, spec in specs.items():
                await client.call_tool("drawing_new", {})
                drawn = await client.call_tool("pid_from_spec", {"spec": spec})
                handles[number] = (drawn.structured_content or {}).get("handles") or {}
                path = work / f"{number}.dxf"
                await client.call_tool("drawing_save_as", {"path": str(path)})
                manifest["sheets"].append({"path": str(path), "number": number})
            checked = await client.call_tool(
                "pid_project",
                {"action": "check", "manifest": manifest, "include_current": False},
                raise_on_error=False,
            )
            missing = await client.call_tool(
                "pid_project",
                {
                    "manifest": {"sheets": [*manifest["sheets"], {"path": str(work / "gone.dxf")}]},
                    "include_current": False,
                },
                raise_on_error=False,
            )
            many = await client.call_tool(
                "pid_project",
                {"sheets": [str(work / f"s{i}.dxf") for i in range(51)]},
                raise_on_error=False,
            )
        result = checked.structured_content or {}
        links = {
            row["link"]: (row["status"], [(c["sheet"], c["handle"]) for c in row["connectors"]])
            for row in result.get("links") or []
        }
        want_links = {
            "L-1": ("matched", [("P-001", handles["P-001"].get("A1")), ("P-002", handles["P-002"].get("B1"))]),
            "L-2": ("matched", [("P-001", handles["P-001"].get("A2")), ("P-002", handles["P-002"].get("B2"))]),
            "L-3": ("direction_mismatch", [("P-001", handles["P-001"].get("A3")), ("P-003", handles["P-003"].get("C1"))]),
            "L-4": ("orphan", [("P-002", handles["P-002"].get("B4"))]),
        }  # fmt: skip
        dups = [
            (row["tag"], [(o["sheet"], o["handle"]) for o in row["occurrences"]])
            for row in result.get("duplicate_tags") or []
        ]
        want_dups = [
            ("P-101", [("P-001", handles["P-001"].get("P-101")), ("P-003", handles["P-003"].get("P-101"))])
        ]  # fmt: skip
        mismatches = [
            (row["link"], row["out"]["handle"], row["in"]["handle"])
            for row in result.get("line_continuity") or []
            if row["status"] == "mismatch"
        ]
        summary = result.get("summary") or {}
        twins = {
            "missing": (missing.structured_content or {}).get("kind") if missing.is_error else None,
            "many": (many.structured_content or {}).get("kind") if many.is_error else None,
        }
        passed = (
            not checked.is_error
            and links == want_links
            and dups == want_dups
            and mismatches == [("L-2", handles["P-001"].get("A2"), handles["P-002"].get("B2"))]
            and summary.get("orphan") == 1
            and summary.get("direction_mismatch") == 1
            and twins == {"missing": "file_not_found", "many": "invalid_args"}
        )
        return (
            passed,
            {"summary": summary, "twins": twins, "sheets": len(result.get("sheets") or [])},
            [],
        )

    async def _task_std_tables_b40(self):
        """B40 (1.8-22): one size drawn from every filled feature table, a size
        between rows refused by name.

        The expected dimensions are the double entry's own rows (``_rows.TABLES``,
        generated from ``data/<std>.{a,b}.json`` by ``scripts/build_standards.py``),
        not the drawing code's output. Every filled table draws its smallest size
        through ``draw_std_feature`` (the axial O-ring housing, which no drawing
        path takes, is looked up instead); the twin draws a size between its two
        smallest rows - or past the only one - and must be refused naming the
        table, with the entity count unchanged. With no table filled the task is
        ``not_run`` naming every table deferred to 1.8.1 - never a vacuous pass -
        and its metrics still record that each deferred table refuses by name.
        """
        from engineering.mech.standards import lookup
        from engineering.mech.standards._rows import TABLES
        from engineering.mech.standards.feature_tables import (
            deferred_message,
            deferred_tables,
            filled_tables,
            probe,
        )
        from engineering.mech.stdparts import draw_std_feature

        refused_by_name = {}
        for name in deferred_tables():
            try:
                lookup(name, 10.0)
                refused_by_name[name] = False
            except ValueError as exc:
                refused_by_name[name] = name in str(exc) and "no rows transcribed" in str(exc)
        filled = filled_tables()
        metrics = {
            "filled": list(filled),
            "deferred": list(deferred_tables()),
            "deferred_refused_by_name": refused_by_name,
        }
        if not filled:
            return TaskResult(
                "std_tables_b40",
                "not_run",
                0.0,
                f"B40: {deferred_message()}",
                metrics=metrics,
            )
        rows_ok, twins_ok, drawn = {}, {}, {}
        for index, name in enumerate(filled):
            p = probe(name)
            expected = TABLES[name]["rows"][p["size"]]
            if p["draw"] is None:
                row = lookup(name, p["size"])
                rows_ok[name] = all(row.get(k) == v for k, v in expected.items())
                try:
                    lookup(name, p["refused_size"])
                    twins_ok[name] = False
                except ValueError as exc:
                    twins_ok[name] = name in str(exc)
                continue
            kind, params = p["draw"]
            result = await draw_std_feature(self.backend, kind, (60.0 * index, 0.0), params)
            dims = result["dims"]
            if kind == "undercut":
                same = dims["profile"] == [
                    list(pair) for pair in lookup(name, p["size"])["profile"]
                ]
            else:
                shared = [k for k in expected if k in dims]
                same = bool(shared) and all(dims[k] == expected[k] for k in shared)
            drawn[name] = len(result["handles"])
            rows_ok[name] = same and bool(result["handles"])
            before = len(await self.backend.entity_list())
            try:
                await draw_std_feature(
                    self.backend, kind, (60.0 * index, 80.0), p["refused_draw"][1]
                )
                twins_ok[name] = False
            except ValueError as exc:
                twins_ok[name] = name in str(exc)
            twins_ok[name] = twins_ok[name] and len(await self.backend.entity_list()) == before
        passed = all(rows_ok.values()) and all(twins_ok.values()) and all(refused_by_name.values())
        return (
            passed,
            {**metrics, "drawn": drawn, "rows_ok": rows_ok, "twins_ok": twins_ok},
            [],
        )

    async def _task_dwg_headless_roundtrip(self):
        """A DWG opened headlessly through a converter, saved as DXF (K1).

        The DWG is the bundled ``templates/iso_a3_mech.dwt`` under a ``.dwg``
        name (a DWT is a DWG container); the truth is the template's DXF twin,
        read with ezdxf directly. Through the real server: ``drawing_open`` on
        the DWG, ``drawing_save_as`` to a DXF, and that file must hold the
        twin's layers, block names and per-layout entity counts. The
        broken-input twin runs first: with the converters switched off
        (``ACCORECONSOLE_PATH=off``, the ODA probe answering no) the same open
        is refused ``unsupported`` / capability ``dwg_read`` and the open
        document is unchanged. A machine with no converter at all is
        ``environment_blocked``, never a pass.
        """
        import os
        import shutil

        import ezdxf

        from backends import ezdxf_backend

        def counts(doc) -> dict:
            return {
                "layers": sorted(layer.dxf.name for layer in doc.layers),
                "blocks": sorted(block.name for block in doc.blocks),
                "entities": {layout.name: len(layout) for layout in doc.layouts},
            }

        templates = Path(__file__).resolve().parents[2] / "templates"
        work = self.artifact_dir / "dwg_headless_roundtrip"
        work.mkdir(parents=True, exist_ok=True)
        dwg = work / "iso_a3_mech.dwg"
        shutil.copyfile(templates / "iso_a3_mech.dwt", dwg)
        truth = counts(ezdxf.readfile(templates / "iso_a3_mech.dxf"))

        saved_path = os.environ.get("ACCORECONSOLE_PATH")
        saved_probe = ezdxf_backend._odafc_available
        os.environ["ACCORECONSOLE_PATH"] = "off"
        ezdxf_backend._odafc_available = lambda: False
        try:
            async with self._served() as client:
                before = (await client.call_tool("drawing_info", {})).structured_content
                refused = await client.call_tool(
                    "drawing_open", {"path": str(dwg)}, raise_on_error=False
                )
                after = (await client.call_tool("drawing_info", {})).structured_content
        finally:
            ezdxf_backend._odafc_available = saved_probe
            if saved_path is None:
                os.environ.pop("ACCORECONSOLE_PATH", None)
            else:
                os.environ["ACCORECONSOLE_PATH"] = saved_path
        twin = refused.structured_content or {}
        twin_ok = (
            refused.is_error
            and twin.get("kind") == "unsupported"
            and twin.get("capability") == "dwg_read"
            and (before.get("name"), before.get("revision"))
            == (after.get("name"), after.get("revision"))
        )
        metrics: dict = {
            "twin": {k: twin.get(k) for k in ("kind", "capability")},
            "twin_ok": twin_ok,
        }
        route = self.backend.capabilities().features.get("dwg_read")
        metrics["route"] = route.mode if route is not None else None
        if route is None or not route.supported:
            return TaskResult(
                "dwg_headless_roundtrip",
                "environment_blocked",
                0.0,
                "no DWG converter on this machine (ODA File Converter or accoreconsole)",
                metrics=metrics,
            )
        target = work / "roundtrip.dxf"
        async with self._served() as client:
            opened = await client.call_tool(
                "drawing_open", {"path": str(dwg)}, raise_on_error=False
            )
            saved = await client.call_tool(
                "drawing_save_as", {"path": str(target)}, raise_on_error=False
            )
        report = (opened.structured_content or {}).get("dwg_read") or {}
        metrics["dwg_read"] = {
            k: report.get(k) for k in ("route", "seconds", "exit_code", "dxf_version")
        }
        if opened.is_error or saved.is_error or not target.is_file():
            metrics["error"] = (opened.structured_content or saved.structured_content or {}).get(
                "message"
            )
            return False, metrics, []
        observed = counts(ezdxf.readfile(target))
        metrics["equal"] = {key: observed[key] == truth[key] for key in truth}
        metrics["entities"] = observed["entities"]
        passed = twin_ok and all(metrics["equal"].values())
        return passed, metrics, [str(target)]

    async def _task_parametric_revise_keeps_handles(self):
        """B24-B28 in one call (``mech_part_revise``, 1.8-19).

        A flange (Ø120 x 20 disc, Ø60 x 40 hub, Ø20 bore, 4 x Ø11 on a Ø90
        bolt circle, R3 at the step) is drawn in front, end and section A-A
        views and dimensioned, with a stored requirement "the bore is Ø24".
        One revision - bore 24, hub 50 (length 70), a 1.5 chamfer on the hub
        end, the R3 removed - must keep every LINE / CIRCLE / ARC it edits in
        place under its handle, leave the entity signatures (``drawing_diff``'s,
        handles excluded) equal to a fresh draw of ``REVISED_FLANGE`` - written
        out by hand below, not derived from the call - show ⌀24 and (70) on its
        DIMENSIONs and pass the requirement. The broken-input twin
        ``revise_hand_edit_refused``: on a fresh copy, a tagged line moved 1 mm
        by hand must refuse the same revision ``hand_edited_tagged_entity``,
        naming that handle, with every entity's fingerprint unchanged.
        """
        from engineering.changes.preview import entity_fingerprints
        from engineering.errors import HandEditedTaggedEntityError, classify
        from engineering.mech.draw import add_view, draw_part
        from engineering.mech.drift import tagged_entities
        from engineering.mech.part import build_part
        from engineering.mech.revise import revise_part
        from engineering.understand.diff import signature
        from engineering.understand.snapshot import take_snapshot
        from engineering.verification.requirements import parse_requirements, store_requirements

        flange = {
            "kind": "revolved",
            "name": "flange",
            "material": "steel",
            "segments": [{"length": 20.0, "d_outer": 120.0}, {"length": 40.0, "d_outer": 60.0}],
            "features": [
                {"kind": "axial_bore", "id": "bore", "diameter": 20.0},
                {
                    "kind": "hole_circle",
                    "id": "hc",
                    "x": 10.0,
                    "pcd": 90.0,
                    "count": 4,
                    "diameter": 11.0,
                },
                {"kind": "fillet", "id": "r1", "at": "step:0", "radius": 3.0},
            ],
        }
        revised = {
            "kind": "revolved",
            "name": "flange",
            "material": "steel",
            "segments": [{"length": 20.0, "d_outer": 120.0}, {"length": 50.0, "d_outer": 60.0}],
            "features": [
                {"kind": "axial_bore", "id": "bore", "diameter": 24.0},
                {
                    "kind": "hole_circle",
                    "id": "hc",
                    "x": 10.0,
                    "pcd": 90.0,
                    "count": 4,
                    "diameter": 11.0,
                },
                {"kind": "chamfer", "id": "c2", "at": "end", "size": 1.5},
            ],
        }
        changes = {
            "features.bore.diameter": 24.0,
            "segments[1].length": 50.0,
            "features+": {"kind": "chamfer", "id": "c2", "at": "end", "size": 1.5},
            "features.r1": None,
        }
        plane = {"p1": [-5.0, 0.0], "p2": [80.0, 0.0], "label": "A"}
        bore = {
            "units": "mm",
            "items": [
                {
                    "id": "BORE",
                    "quantity": "diameter",
                    "target": {"nominal": 24.0},
                    "provenance": "explicit",
                    "features": [
                        {
                            "kind": "predicate",
                            "type": "circle",
                            "layer": "GEOMETRY",
                            "radius_min": 8.0,
                            "radius_max": 15.0,
                        }
                    ],
                }
            ],
        }

        async def sheet(backend, spec) -> str:
            await backend.system_set_variable("INSUNITS", 4)
            drawn = await draw_part(backend, build_part(spec), views=("front", "side"))
            await add_view(backend, drawn["part_id"], "section", plane=plane)
            return drawn["part_id"]

        def signatures(snap) -> Counter:
            return Counter(signature(rec) for rec in snap.records)

        pid = await sheet(self.backend, flange)
        await store_requirements(self.backend, parse_requirements(bore))
        result = await revise_part(self.backend, pid, changes)
        snap = await take_snapshot(self.backend)
        alive = {rec.handle for rec in snap.records}
        kept = set(result["in_place"])
        texts = [rec.rendered_text or "" for rec in snap.records if rec.type == "DIMENSION"]

        from backends.ezdxf_backend import EzdxfBackend

        fresh = EzdxfBackend()
        await fresh.connect()
        try:
            await fresh.drawing_new()
            await sheet(fresh, revised)
            same = signatures(snap) == signatures(await take_snapshot(fresh))
        finally:
            await fresh.disconnect()

        # the broken-input twin, on a fresh copy of the original sheet
        await self._reset()
        twin_pid = await sheet(self.backend, flange)
        before_snap = await take_snapshot(self.backend)
        line = next(t for t in tagged_entities(before_snap.records, twin_pid) if t.type == "LINE")
        await self.backend.entity_move(line.handle, 1.0, 0.0)
        handles = [rec.handle for rec in (await take_snapshot(self.backend)).records]
        before = await entity_fingerprints(self.backend, handles)
        refusal: dict = {}
        try:
            await revise_part(self.backend, twin_pid, changes)
        except HandEditedTaggedEntityError as exc:
            refusal = classify(exc)
        after = await entity_fingerprints(self.backend, handles)
        twin_ok = (
            refusal.get("kind") == "hand_edited_tagged_entity"
            and refusal.get("handles") == [line.handle]
            and before == after
        )
        passed = (
            len(kept) >= 40
            and kept <= alive
            and same
            and any("⌀24" in t for t in texts)
            and any("(70)" in t for t in texts)
            and (result.get("verify") or {}).get("status") == "pass"
            and twin_ok
        )
        return (
            passed,
            {
                "in_place": len(kept),
                "in_place_alive": len(kept & alive),
                "handle_map": len(result["handle_map"]),
                "signatures_equal_fresh_draw": same,
                "dimension_texts": sorted(texts),
                "requirement": (result.get("verify") or {}).get("status"),
                "twin": {
                    "kind": refusal.get("kind"),
                    "handles": refusal.get("handles"),
                    "fingerprints_unchanged": before == after,
                },
            },
            [],
        )

    async def _task_pid_routing_obstacles(self):
        """Lines round the vessels in their way (``sheet.routing: "avoid"``, 1.8-20).

        Two process lines whose fixed ``auto`` routes each run through a vessel
        between their ports (the auto dry run must say so, or the fixture
        proves nothing); drawn with ``avoid`` they must leave the critique
        without a ``pid_line_through_symbol`` finding, the graph without a
        dangling end, every ``obstacle_hits`` empty, the drawn polylines on the
        dry run's vertices and the crossings within what the dry run declared.
        The broken-input twin, on a document of its own: a pump walled in by
        four vessels is refused ``no_route`` naming exactly those four tags,
        and the rolled-back sheet holds nothing.
        """
        import copy

        from backends.ezdxf_backend import EzdxfBackend
        from engineering import errors
        from engineering.critique import run_critique
        from engineering.layers import apply_layer_set
        from engineering.pid.graph import build_graph
        from engineering.pid.router import NoRouteError
        from engineering.pid.spec import run_spec

        def vessel(item_id, x, tag):
            return {"id": item_id, "symbol": "vertical_vessel", "x": x, "y": 0, "tag": tag}

        sheet = {
            "equipment": [
                {"id": "P-1", "symbol": "centrifugal_pump", "x": 0, "y": -20, "tag": "P-101"},
                vessel("V-1", 80, "V-101"),
                vessel("V-2", 160, "V-102"),
                vessel("V-3", 240, "V-103"),
                vessel("V-4", 320, "V-104"),
            ],
            "lines": [
                {"from": "P-1.discharge", "to": "V-2.N3"},
                {"from": "V-2.N4", "to": "V-4.N3"},
                {"from": "V-1.N1", "to": "V-3.N1", "class": "process_minor"},
            ],
        }
        auto = copy.deepcopy(sheet)
        auto["sheet"] = {"routing": "auto"}
        avoid = copy.deepcopy(sheet)
        avoid["sheet"] = {"routing": "avoid"}
        await apply_layer_set(self.backend, "pid")
        auto_dry = await run_spec(self.backend, auto, dry_run=True)
        dry = await run_spec(self.backend, avoid, dry_run=True)
        result = await run_spec(self.backend, avoid)
        through = [
            i for i in await run_critique(self.backend) if i.focus == "pid_line_through_symbol"
        ]
        graph = await build_graph(self.backend)
        drawn = sorted(
            [[float(x), float(y)] for x, y in info.properties["points"]]
            for info in await self.backend.entity_list()
            if info.type == "LWPOLYLINE" and info.layer.startswith("PROCESS-PIPING")
        )
        planned = sorted(line["vertices"] for line in dry["lines"])

        twin_backend = EzdxfBackend()
        await twin_backend.connect()
        await twin_backend.drawing_new()
        walled = {
            "sheet": {"routing": "avoid"},
            "equipment": [
                {"id": "P-1", "symbol": "centrifugal_pump", "x": 0, "y": 0, "tag": "P-101"},
                {"id": "TK-1", "symbol": "horizontal_vessel", "x": 0, "y": 30, "tag": "TK-1"},
                {"id": "TK-2", "symbol": "horizontal_vessel", "x": -25, "y": 10, "tag": "TK-2"},
                {"id": "TK-3", "symbol": "horizontal_vessel", "x": 25, "y": 10, "tag": "TK-3"},
                {"id": "TK-4", "symbol": "horizontal_vessel", "x": 0, "y": -20, "tag": "TK-4"},
                vessel("V-9", 200, "V-900"),
            ],
            "lines": [{"from": "P-1.discharge", "to": "V-9.N3"}],
        }
        refusal: dict = {}
        try:
            await run_spec(twin_backend, walled)
        except NoRouteError as exc:
            refusal = errors.classify(exc)
        left = len(await twin_backend.entity_list())
        await twin_backend.disconnect()
        named = sorted(o.get("tag") for o in refusal.get("obstacles") or [])
        twin_ok = (
            refusal.get("kind") == "no_route" and named == ["TK-1", "TK-2", "TK-3", "TK-4"]
        ) and left == 0
        passed = (
            len(auto_dry["obstacle_hits"]) >= 2
            and through == []
            and graph["stats"]["dangling"] == 0
            and result["obstacle_hits"] == []
            and drawn == planned
            and result["crossings_total"] <= dry["crossings_total"]
            and twin_ok
        )
        return (
            passed,
            {
                "auto_obstacle_hits": [(h["where"], h["tag"]) for h in auto_dry["obstacle_hits"]],
                "line_through_symbol": len(through),
                "dangling": graph["stats"]["dangling"],
                "obstacle_hits": len(result["obstacle_hits"]),
                "vertices_match_dry_run": drawn == planned,
                "crossings": result["crossings_total"],
                "crossings_declared": dry["crossings_total"],
                "twin_kind": refusal.get("kind"),
                "twin_tags": named,
                "twin_entities_left": left,
            },
            [],
        )

    async def _task_undo_across_restart(self):
        """v9 (1.8-08, §4.2): undo works after the server is killed and restarted.

        A real ``python server.py`` (headless, a private state folder, no
        ``EZDXF_UNDO_DEPTH``) draws a line (0, 0)-(10, 0), saves, moves it by
        (5, 5), saves and is killed with ``Popen.kill()``. A new process opens
        the file and one ``drawing_undo`` must put the line back at
        (0, 0)-(10, 0) - endpoints read with ``entity_get``, an independent
        oracle - reported at ``granularity: "save"``. The twin: that process
        saves, moves the line once more and is killed unsaved; a third
        process's ``drawing_open`` must find the file byte for byte as saved
        and report ``lost_since_save.count == 1``.
        """
        import tempfile

        from benchmarks.correctness_suite import _server_env, _StdioServer

        root = Path(__file__).resolve().parents[2]
        script = str(root / "server.py")
        state = tempfile.mkdtemp(prefix="acadmcp-v9-state-")
        env = {**_server_env(state), "PYTHONPATH": str(root)}
        env.pop("EZDXF_UNDO_DEPTH", None)
        folder = self.artifact_dir / "undo_across_restart"
        folder.mkdir(parents=True, exist_ok=True)
        path = str(folder / "undo.dxf")
        log_path = str(folder / "server.log")

        def run() -> dict:
            first = _StdioServer(script, env, str(folder), log_path, timeout=60.0)
            try:
                first.call("drawing_new")
                handle = first.call(
                    "entity_create_line", {"x1": 0, "y1": 0, "x2": 10, "y2": 0}
                ).get("handle")
                first.call("drawing_save_as", {"path": path})
                first.call("entity_move", {"handle": handle, "dx": 5, "dy": 5})
                first.call("drawing_save", {})
            finally:
                first.kill()
            second = _StdioServer(script, env, str(folder), log_path, timeout=60.0)
            try:
                second.call("drawing_open", {"path": path})
                undone = second.call("drawing_undo")
                line = second.call("entity_get", {"handle": handle})
                second.call("drawing_save", {})
                with open(path, "rb") as stream:
                    on_disk = stream.read()
                second.call("entity_move", {"handle": handle, "dx": 1, "dy": 0})
            finally:
                second.kill()
            third = _StdioServer(script, env, str(folder), log_path, timeout=60.0)
            try:
                reopened = third.call("drawing_open", {"path": path})
            finally:
                third.kill()
            with open(path, "rb") as stream:
                unchanged = stream.read() == on_disk
            return {"undone": undone, "line": line, "reopened": reopened, "unchanged": unchanged}

        try:
            seen = await asyncio.to_thread(run)
        finally:
            import shutil

            shutil.rmtree(state, ignore_errors=True)
        props = seen["line"].get("properties") or {}
        start, end = list(props.get("start") or [])[:2], list(props.get("end") or [])[:2]
        lost = seen["reopened"].get("lost_since_save") or {}
        passed = (
            seen["undone"].get("ok") is True
            and seen["undone"].get("granularity") == "save"
            and start == [0.0, 0.0]
            and end == [10.0, 0.0]
            and seen["unchanged"]
            and lost.get("count") == 1
        )
        return (
            passed,
            {
                "undo": {
                    key: seen["undone"].get(key)
                    for key in ("ok", "granularity", "checkpoint", "restored_revision")
                },
                "endpoints_after_undo": [start, end],
                "file_unchanged_after_unsaved_kill": seen["unchanged"],
                "lost_since_save": lost,
                "server_env_undo_depth": env.get("EZDXF_UNDO_DEPTH"),
            },
            [path],
        )

    async def _task_atomic_batch_default(self):
        """v9 (1.8-09, §4.3): a batch with no ``on_error`` is all or nothing.

        The rival's probe through the real ``cad_batch`` tool: line L, then
        ``[entity_move(L), entity_delete("DEAD")]`` with no ``on_error``. The
        oracle is L's ``entity_get`` summary read straight from the engine
        before and after - the batch must be refused ``unknown_handle`` with
        ``executed: 0`` and the summary unchanged (1.7.0 kept the move). The
        run-time twin, ``[entity_move(L), entity_edit_text(L)]`` (a LINE has
        no text, so the second step fails while it runs), must be rolled back
        by the default: ``ok: false``, ``atomicity.default_applied`` and
        ``verified`` true, the summary unchanged again. Headless lane only:
        the live engine's undo is ``scripts/smoke_durability_com.py``.
        """
        from dataclasses import asdict

        line = await self.backend.entity_create_line(0, 0, 10, 0)
        handle = line.handle

        async def summary() -> dict:
            return asdict(await self.backend.entity_get(handle))

        original = await summary()
        move = {"tool": "entity_move", "args": {"handle": handle, "dx": 5.0, "dy": 5.0}}
        async with self._served() as client:
            probe = await client.call_tool(
                "cad_batch",
                {"steps": [move, {"tool": "entity_delete", "args": {"handle": "DEAD"}}]},
                raise_on_error=False,
            )
            refused = probe.structured_content or {}
            after_probe = await summary()
            runtime = await client.call_tool(
                "cad_batch",
                {
                    "steps": [
                        move,
                        {"tool": "entity_edit_text", "args": {"handle": handle, "text": "X"}},
                    ]
                },
                raise_on_error=False,
            )
            undone = runtime.structured_content or {}
        after_runtime = await summary()
        atomicity = undone.get("atomicity") or {}
        passed = (
            probe.is_error is True
            and refused.get("kind") == "unknown_handle"
            and refused.get("executed") == 0
            and after_probe == original
            and runtime.is_error is False
            and undone.get("ok") is False
            and undone.get("on_error") == "auto"
            and atomicity.get("default_applied") is True
            and atomicity.get("rolled_back") is True
            and atomicity.get("verified") is True
            and after_runtime == original
        )
        return (
            passed,
            {
                "probe": {
                    "kind": refused.get("kind"),
                    "executed": refused.get("executed"),
                    "missing": refused.get("missing"),
                    "summary_unchanged": after_probe == original,
                },
                "runtime_twin": {
                    "ok": undone.get("ok"),
                    "on_error": undone.get("on_error"),
                    "atomicity": {
                        key: atomicity.get(key)
                        for key in (
                            "mode",
                            "guarantee",
                            "default_applied",
                            "rolled_back",
                            "verified",
                        )
                    },
                    "summary_unchanged": after_runtime == original,
                },
            },
            [],
        )

    async def _task_history_value_lookup(self):
        """v9 (1.8-10, H1): the round-2 value, after a restart, in at most two calls.

        A real ``python server.py`` (headless, a private state folder) draws
        three rounds of a plate with ``mech_part_draw`` - hole 10, 12 (the call
        labelled ``round 2`` through ``_meta.label``), 16 - saving after each
        round, plus the unlabelled twin (one circle edited 5 -> 6 -> 8), and is
        killed with ``Popen.kill()``. A new process: ``drawing_open`` +
        ``drawing_history(label="round 2", detail="args")`` must answer 12 in
        at most two calls; ``label="round 9"`` must be refused ``invalid_args``
        naming the labels there are; the unlabelled round's checkpoint diff
        (``detail="changes"``) must say radius 5 -> 6, a 12 diameter. The
        oracle is the values the script itself drew, never the server's word.
        """
        import shutil
        import tempfile

        from benchmarks.correctness_suite import (
            _history_rounds,
            _history_rounds_pass,
            _server_env,
        )

        root = Path(__file__).resolve().parents[2]
        script = str(root / "server.py")
        state = tempfile.mkdtemp(prefix="acadmcp-v9-state-")
        env = {**_server_env(state), "PYTHONPATH": str(root)}
        folder = self.artifact_dir / "history_value_lookup"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            seen = await asyncio.to_thread(
                _history_rounds, script, env, str(folder), str(folder / "server.log")
            )
        finally:
            shutil.rmtree(state, ignore_errors=True)
        return (
            _history_rounds_pass(seen),
            seen,
            [str(folder / "rounds.dxf"), str(folder / "plain.dxf")],
        )

    # ── v9, 1.8-25: the section-8 rows no earlier item wrote ────────────────────

    async def _task_replay_across_restart(self):
        """v9 (1.8-03, B13): a client operation id survives a kill, and so does a kill inside a call.

        Through ``benchmarks/fault_inject.py`` (a real ``python server.py``, a
        private temporary state folder): process 1 runs
        ``cad_batch(operation_id="v9-replay-1")`` with one line, saves and is
        killed. Process 2 opens the file; the same id and steps must answer
        ``replayed: true`` with the entity count still 1, the same id with
        other steps ``operation_conflict`` (``different_request``), count 1.
        The in-call twin: process 3 is parked at the ``journal.after_begin``
        barrier inside ``cad_batch(operation_id="v9-replay-kill")`` and killed
        there; process 4 must answer that id ``requires_reconcile`` (record
        ``unknown_commit``, ``error_kind: "process_exit"``) and run nothing.
        Every expected count is the script's own: one line drawn on purpose.
        """
        from benchmarks.fault_inject import FaultHarness, describe

        folder = self.artifact_dir / "replay_across_restart"
        folder.mkdir(parents=True, exist_ok=True)
        path = str(folder / "replay.dxf")
        line = {"x1": 0, "y1": 0, "x2": 10, "y2": 0}
        batch = {
            "steps": [{"tool": "entity_create_line", "args": line}],
            "operation_id": "v9-replay-1",
        }
        other = {
            "steps": [{"tool": "entity_create_line", "args": {**line, "x2": 20}}],
            "operation_id": "v9-replay-1",
        }
        killed = {
            "steps": [{"tool": "entity_create_circle", "args": {"cx": 0, "cy": 0, "radius": 5}}],
            "operation_id": "v9-replay-kill",
        }

        def run() -> dict:
            seen: dict = {}
            with FaultHarness(prefix="acadmcp-v9-replay-") as fh:
                first = fh.spawn()
                first.call("drawing_new")
                seen["ran"] = first.call("cad_batch", batch)
                first.call("drawing_save_as", {"path": path})
                seen["kill_1"] = first.kill()
                second = fh.spawn()
                second.call("drawing_open", {"path": path})
                seen["replay"] = second.call("cad_batch", batch)
                seen["count_after_replay"] = second.call("drawing_info").get("entity_count")
                seen["conflict"] = second.call("cad_batch", other)
                seen["count_after_conflict"] = second.call("drawing_info").get("entity_count")
                seen["kill_2"] = second.kill()
                third = fh.spawn(barrier="journal.after_begin:v9-replay-kill")
                third.call("drawing_open", {"path": path})
                third.send("tools/call", {"name": "cad_batch", "arguments": killed})
                seen["parked"] = fh.wait_barrier("journal.after_begin")
                seen["kill_3"] = third.kill()
                seen["dead_3"] = describe(third)
                fourth = fh.spawn()
                fourth.call("drawing_open", {"path": path})
                seen["reconcile"] = fourth.call("cad_batch", killed)
                seen["count_after_reconcile"] = fourth.call("drawing_info").get("entity_count")
                seen["servers"] = len(fh.servers)
            seen["state_removed"] = fh.removed
            return seen

        seen = await asyncio.to_thread(run)
        replay, conflict, reconcile = seen["replay"], seen["conflict"], seen["reconcile"]
        record = reconcile.get("record") or {}
        killed_in_call = (
            seen["kill_3"] is True
            and seen["parked"].get("key") == "v9-replay-kill"
            and seen["dead_3"]["alive"] is False
        )
        passed = (
            seen["ran"].get("ok") is True
            and all(seen[k] is True for k in ("kill_1", "kill_2"))
            and replay.get("replayed") is True
            and replay.get("operation_id") == "v9-replay-1"
            and seen["count_after_replay"] == 1
            and conflict.get("is_error") is True
            and (conflict.get("kind"), conflict.get("reason"))
            == ("operation_conflict", "different_request")
            and seen["count_after_conflict"] == 1
            and killed_in_call
            and reconcile.get("is_error") is True
            and reconcile.get("kind") == "requires_reconcile"
            and record.get("state") == "unknown_commit"
            and record.get("error_kind") == "process_exit"
            and seen["count_after_reconcile"] == 1
            and seen["servers"] == 4
        )
        return (
            passed,
            {
                "replayed": replay.get("replayed"),
                "entity_count_after_replay": seen["count_after_replay"],
                "conflict": [conflict.get("kind"), conflict.get("reason")],
                "killed_inside_call": killed_in_call,
                "barrier": seen["parked"].get("name"),
                "reconcile": {
                    "kind": reconcile.get("kind"),
                    "state": record.get("state"),
                    "error_kind": record.get("error_kind"),
                },
                "entity_count_after_reconcile": seen["count_after_reconcile"],
                "processes": seen["servers"],
                "state_dir_removed": seen["state_removed"],
            },
            [path],
        )

    async def _task_interrupted_save_recover(self):
        """v9 (1.8-04): an interrupted save never costs the client's file.

        Half-way death: a 1-line DXF is the client's file; a child interpreter
        on the harness's state folder reads it, adds 40 circles and calls
        ``atomic_write_dxf`` with a writer that writes half the DXF and dies
        (``os._exit(9)``: no ``finally``, as under a kill). The file's sha
        must be the one taken before; the next real server must report
        ``rolled_back`` for it and open 1 entity, no marker and no tmp left.
        The in-call twin: a server armed at ``save.before_replace`` opens the
        file, adds one circle and is killed parked inside ``drawing_save``
        (tmp written, fsynced, verified; target untouched); the next server's
        startup must report ``finished`` and the file's sha must be the one
        the marker named, opening 2 entities. The counts are the script's.
        """
        import hashlib
        import subprocess
        import sys
        import textwrap

        import ezdxf

        from backends.atomic_write import TMP_SUFFIX
        from benchmarks.fault_inject import ROOT, FaultHarness, describe

        folder = self.artifact_dir / "interrupted_save_recover"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "client.dxf"
        for leftover in folder.iterdir():
            if leftover.is_file():
                leftover.unlink()
        seed = ezdxf.new("R2010", units=4)
        seed.modelspace().add_line((0, 0), (10, 0))
        seed.saveas(str(target))

        def sha() -> str:
            return hashlib.sha256(target.read_bytes()).hexdigest()

        def leftovers() -> list[str]:
            return sorted(p.name for p in folder.iterdir() if p.name.endswith(TMP_SUFFIX))

        writer = textwrap.dedent(
            """
            import io, os, sys
            sys.path.insert(0, os.getcwd())
            import ezdxf
            from backends import atomic_write as aw
            target = sys.argv[1]
            doc = ezdxf.readfile(target)
            for i in range(40):
                doc.modelspace().add_circle((i, 0), 1)
            real = doc.write
            def dies_half_way(stream, fmt="asc"):
                buffer = io.StringIO()
                real(buffer, fmt)
                text = buffer.getvalue()
                stream.write(text[: len(text) // 2])
                stream.flush()
                os._exit(9)
            doc.write = dies_half_way
            aw.atomic_write_dxf(doc, target)
            """
        )

        def outcomes(rows) -> list[str]:
            return [r.get("outcome") for r in rows or [] if r.get("target") == str(target)]

        def run() -> dict:
            seen: dict = {"before": sha()}
            with FaultHarness(prefix="acadmcp-v9-save-") as fh:
                child = subprocess.run(
                    [sys.executable, "-c", writer, str(target)],
                    cwd=str(ROOT),
                    env=fh.env(),
                    capture_output=True,
                    timeout=120,
                )
                seen["writer_exit"] = child.returncode
                seen["after_half_write"] = sha()
                seen["marker_phase"] = [m.get("phase") for m in fh.pending_markers()]
                first = fh.spawn()
                status = first.call("system_status")
                opened = first.call("drawing_open", {"path": str(target)})
                seen["rolled"] = outcomes(status.get("save_recovery")) + outcomes(
                    opened.get("save_recovery")
                )
                seen["count_rolled"] = first.call("drawing_info").get("entity_count")
                seen["after_roll_back"] = sha()
                seen["markers_after_roll_back"] = len(fh.pending_markers())
                seen["tmp_after_roll_back"] = leftovers()
                seen["kill_1"] = first.kill()

                second = fh.spawn(barrier=f"save.before_replace:{target.name}")
                second.call("drawing_open", {"path": str(target)})
                second.call("entity_create_circle", {"cx": 50, "cy": 50, "radius": 5})
                second.send("tools/call", {"name": "drawing_save", "arguments": {}})
                seen["parked"] = fh.wait_barrier("save.before_replace")
                seen["pending"] = fh.pending_markers()
                seen["parked_sha"] = sha()
                seen["kill_2"] = second.kill()
                seen["dead_2"] = describe(second)

                third = fh.spawn()
                status = third.call("system_status")
                seen["finished"] = outcomes(status.get("save_recovery"))
                seen["after_finish"] = sha()
                third.call("drawing_open", {"path": str(target)})
                seen["count_finished"] = third.call("drawing_info").get("entity_count")
                seen["markers_after_finish"] = len(fh.pending_markers())
                seen["tmp_after_finish"] = leftovers()
            seen["state_removed"] = fh.removed
            return seen

        seen = await asyncio.to_thread(run)
        pending = seen["pending"]
        expected = pending[0].get("expected_sha256") if len(pending) == 1 else None
        half_way_ok = (
            seen["writer_exit"] == 9
            and seen["after_half_write"] == seen["before"]
            and seen["marker_phase"] == ["writing"]
            and seen["rolled"] == ["rolled_back"]
            and seen["after_roll_back"] == seen["before"]
            and seen["count_rolled"] == 1
            and seen["markers_after_roll_back"] == 0
            and seen["tmp_after_roll_back"] == []
        )
        in_call_ok = (
            seen["kill_2"] is True
            and seen["dead_2"]["alive"] is False
            and seen["parked"].get("key") == target.name
            and len(pending) == 1
            and pending[0].get("phase") == "verified"
            and seen["parked_sha"] == seen["before"]
            and seen["finished"] == ["finished"]
            and expected is not None
            and seen["after_finish"] == expected
            and seen["count_finished"] == 2
            and seen["markers_after_finish"] == 0
            and seen["tmp_after_finish"] == []
        )
        return (
            half_way_ok and in_call_ok,
            {
                "half_way": {
                    "writer_exit": seen["writer_exit"],
                    "sha_unchanged": seen["after_half_write"] == seen["before"],
                    "marker_phase": seen["marker_phase"],
                    "outcome": seen["rolled"],
                    "entities": seen["count_rolled"],
                },
                "killed_before_replace": {
                    "killed_inside_call": seen["kill_2"] is True and not seen["dead_2"]["alive"],
                    "sha_unchanged_while_parked": seen["parked_sha"] == seen["before"],
                    "outcome": seen["finished"],
                    "sha_is_the_markers": expected is not None and seen["after_finish"] == expected,
                    "entities": seen["count_finished"],
                },
                "state_dir_removed": seen["state_removed"],
            },
            [str(target)],
        )

    async def _task_damaged_dxf_recover(self):
        """v9 (1.8-04, B09): a damaged DXF is recovered on request, and only then.

        A valid 50-line DXF is damaged three ways: cut before its last ENDSEC
        (no OBJECTS end, no EOF), three entity handles set to an earlier
        entity's, four set to text that is not hex. The expected outcome is
        fixed here, by construction: the recover reader drops the three
        conflicting entities (one fix each) and re-numbers the four non-hex
        ones, so ``fixes == 3``, ``entities_read == 47`` and every model-space
        handle unique. The source's sha must not move - not by the open, not
        by a ``drawing_save`` onto it (refused ``recovered_source_protected``).
        The twin: the plain open is refused ``damaged_drawing`` naming
        ``recover=True``, the open document (one line) kept.
        """
        import hashlib

        import ezdxf

        folder = self.artifact_dir / "damaged_dxf_recover"
        folder.mkdir(parents=True, exist_ok=True)
        doc = ezdxf.new("R2010", units=4)
        for index in range(50):
            doc.modelspace().add_line((0, index), (10, index))
        intact = folder / "intact.dxf"
        doc.saveas(str(intact))
        duplicate_at, non_hex_at = (10, 20, 30), (5, 15, 25, 35)
        lines = intact.read_text(encoding="utf-8").split("\n")
        out, in_entities, entity, first_handle = [], False, -1, None
        for i in range(0, len(lines) - 1, 2):
            code, value = lines[i], lines[i + 1]
            if code.strip() == "2" and value.strip() == "ENTITIES":
                in_entities = True
            elif in_entities and code.strip() == "0" and value.strip() == "ENDSEC":
                in_entities = False
            elif in_entities and code.strip() == "0":
                entity += 1
            elif in_entities and code.strip() == "5":
                if first_handle is None:
                    first_handle = value
                elif entity in duplicate_at:
                    value = first_handle
                elif entity in non_hex_at:
                    value = "ZZZZ"
            out.extend([code, value])
        text = "\n".join(out)
        damaged = folder / "damaged.dxf"
        damaged.write_text(text[: text.rindex("ENDSEC")], encoding="utf-8")

        def sha() -> str:
            return hashlib.sha256(damaged.read_bytes()).hexdigest()

        before = sha()
        await self.backend.entity_create_line(0, 0, 10, 0)
        identity = dict(self.backend.document_identity())
        async with self._served() as client:
            refused = await client.call_tool(
                "drawing_open", {"path": str(damaged)}, raise_on_error=False
            )
            identity_after_refusal = dict(self.backend.document_identity())
            count_after_refusal = (await client.call_tool("drawing_info", {})).structured_content[
                "entity_count"
            ]
            opened = (
                await client.call_tool("drawing_open", {"path": str(damaged), "recover": True})
            ).structured_content or {}
            sha_after_open = sha()
            blocked = await client.call_tool("drawing_save", {}, raise_on_error=False)
            sha_after_blocked_save = sha()
            listed = await client.call_tool("entity_list", {"limit": 1000})
            handles = [row["handle"] for row in listed.structured_content["result"]]
        recovered = opened.get("recovered") or {}
        refusal = refused.structured_content or {}
        refusal_text = refused.content[0].text if refused.content else ""
        twin_ok = (
            refused.is_error
            and refusal.get("kind") == "damaged_drawing"
            and "recover=True" in refusal_text
            and identity_after_refusal.get("document_id") == identity.get("document_id")
            and identity_after_refusal.get("revision") == identity.get("revision")
            and count_after_refusal == 1
        )
        blocked_payload = blocked.structured_content or {}
        passed = (
            twin_ok
            and recovered.get("fixes") == len(duplicate_at)
            and recovered.get("entities_read") == 50 - len(duplicate_at)
            and len(handles) == 50 - len(duplicate_at) == len(set(handles))
            and opened.get("recovered_from") == str(damaged)
            and sha_after_open == before
            and blocked.is_error
            and blocked_payload.get("kind") == "recovered_source_protected"
            and sha_after_blocked_save == before
        )
        return (
            passed,
            {
                "plain_open": {
                    "kind": refusal.get("kind"),
                    "names_recover": "recover=True" in refusal_text,
                    "document_kept": twin_ok,
                },
                "recovered": {k: recovered.get(k) for k in ("fixes", "errors", "entities_read")},
                "handles_unique": len(handles) == len(set(handles)),
                "planted": {"conflicting": len(duplicate_at), "non_hex": len(non_hex_at)},
                "source_sha_unchanged": sha_after_open == before == sha_after_blocked_save,
                "save_onto_source": blocked_payload.get("kind"),
            },
            [str(damaged)],
        )

    async def _task_bylayer_truth(self):
        """v9 (1.8-01, the rival's §4.5): the lineweight in the file, not in the answer.

        Through the real server: the ISO ``mech`` layers, 22 lines on GEOMETRY
        created with ``lineweight: "Default"`` and three with ``"ByLayer"``
        (``entity_batch_create``), saved. The oracle is the saved DXF read with
        ezdxf - 22 lines at -3 and three at -1 - never the server's report.
        ``drawing_critique(focus=["property_override"])`` must return exactly
        one issue, ``lineweight_not_bylayer`` on GEOMETRY, count 22 and those
        22 handles. After ``drawing_refine(focus=["property_override"])`` and a
        save, the DXF must hold -1 on every line and the focus must be empty.
        """
        import ezdxf

        folder = self.artifact_dir / "bylayer_truth"
        folder.mkdir(parents=True, exist_ok=True)
        first, repaired = folder / "before.dxf", folder / "after.dxf"

        def spec(y: float, weight: str) -> dict:
            row = {"type": "line", "x1": 0, "y1": y, "x2": 10, "y2": y, "layer": "GEOMETRY"}
            return {**row, "lineweight": weight}

        specs = [spec(y, "Default") for y in range(22)] + [
            spec(30 + y, "ByLayer") for y in range(3)
        ]

        def weights(path) -> dict[str, int]:
            msp = ezdxf.readfile(str(path)).modelspace()
            return {e.dxf.handle: e.dxf.get("lineweight", -1) for e in msp if e.dxftype() == "LINE"}

        async def critique(client) -> list[dict]:
            # a union return type (list | dict) comes back wrapped in "result"
            answer = await client.call_tool(
                "drawing_critique", {"focus": ["property_override"], "report": True}
            )
            return answer.structured_content["result"]["issues"]

        async with self._served() as client:
            await client.call_tool("drawing_apply_iso_layers", {"standard": "mech"})
            made = (
                await client.call_tool("entity_batch_create", {"entities": specs})
            ).structured_content or {}
            rows = made.get("entities") or []
            default = sorted(row["handle"] for row in rows[:22])
            await client.call_tool("drawing_save_as", {"path": str(first)})
            issues = await critique(client)
            await client.call_tool(
                "drawing_refine", {"focus": ["property_override"], "min_score": 100.0}
            )
            await client.call_tool("drawing_save_as", {"path": str(repaired)})
            after = await critique(client)
        on_disk = weights(first)
        fixed = weights(repaired)
        detail = (issues[0].get("detail") or {}) if len(issues) == 1 else {}
        file_ok = (
            sorted(h for h, w in on_disk.items() if w == -3) == default
            and sum(1 for w in on_disk.values() if w == -1) == 3
            and len(on_disk) == 25
        )
        grouped = (
            len(issues) == 1
            and detail.get("code") == "lineweight_not_bylayer"
            and detail.get("layer") == "GEOMETRY"
            and detail.get("count") == 22
            and sorted(issues[0].get("handles") or []) == default
        )
        repaired_ok = len(fixed) == 25 and set(fixed.values()) == {-1} and after == []
        return (
            file_ok and grouped and repaired_ok,
            {
                "dxf_lineweights_before": dict(
                    Counter(str(w) for w in on_disk.values()).most_common()
                ),
                "reported_names": sorted({str(row.get("lineweight_name")) for row in rows}),
                "issue": {k: detail.get(k) for k in ("code", "layer", "count", "values")},
                "dxf_lineweights_after": sorted(set(fixed.values())),
                "focus_after_refine": len(after),
            },
            [str(first), str(repaired)],
        )

    async def _task_space_context(self):
        """v9 (1.8-05, the rival's §4.6): new geometry goes to model space, whatever tab was open.

        A DXF saved with Layout1 the active tab and two lines on that sheet is
        opened through the real server and a circle created: it must report
        no paper ``space``, and the saved file must hold the circle in model
        space and Layout1 its two original lines (handles unchanged). The
        twin: under a UCS rotated 30° about the moved origin (100, 50), a trim
        of (0,0)-(20,0) by (10,-5)-(10,5) keeping (2, 0) must leave the WCS
        segment (0,0)-(10,0) - worked out by hand - exactly what the same trim
        at y = 30 under the world UCS leaves, shifted. Headless; the live half
        is ``scripts/smoke_space_com.py`` (a DWG saved with Layout1 active).
        """
        import math

        import ezdxf

        folder = self.artifact_dir / "space_context"
        folder.mkdir(parents=True, exist_ok=True)
        source, saved = folder / "layout1_active.dxf", folder / "saved.dxf"
        doc = ezdxf.new("R2010", setup=True, units=4)
        sheet = doc.layout("Layout1")
        paper = sorted(
            [
                sheet.add_line((10, 10), (100, 10)).dxf.handle,
                sheet.add_line((10, 20), (100, 20)).dxf.handle,
            ]
        )
        doc.layouts.set_active_layout("Layout1")
        doc.saveas(str(source))
        active_on_disk = ezdxf.readfile(str(source)).layouts.active_layout().name

        angle = math.radians(30.0)
        x_axis = [math.cos(angle), math.sin(angle), 0.0]
        y_axis = [-math.sin(angle), math.cos(angle), 0.0]

        async def ends(client, handle) -> list[list[float]]:
            props = (
                await client.call_tool("entity_get", {"handle": handle})
            ).structured_content.get("properties") or {}
            return sorted([[round(float(v), 9) for v in props[k][:2]] for k in ("start", "end")])

        async def trimmed(client, y: float) -> list[list[float]]:
            def line(x1, y1, x2, y2):
                return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}

            target = (
                await client.call_tool("entity_create_line", line(0, y, 20, y))
            ).structured_content["handle"]
            cutter = (
                await client.call_tool("entity_create_line", line(10, y - 5, 10, y + 5))
            ).structured_content["handle"]
            await client.call_tool(
                "entity_trim",
                {"target_handle": target, "cutter_handle": cutter, "keep_x": 2, "keep_y": y},
            )
            return await ends(client, target)

        async with self._served() as client:
            await client.call_tool("drawing_open", {"path": str(source)})
            circle = (
                await client.call_tool("entity_create_circle", {"cx": 5, "cy": 5, "radius": 2})
            ).structured_content or {}
            await client.call_tool("drawing_save_as", {"path": str(saved)})
            await client.call_tool(
                "ucs_set",
                {
                    "name": "TILT30",
                    "origin": [100.0, 50.0, 0.0],
                    "x_axis": x_axis,
                    "y_axis": y_axis,
                },
            )
            under_ucs = await trimmed(client, 0.0)
            await client.call_tool("ucs_restore", {"name": "world"})
            under_world = await trimmed(client, 30.0)
        reread = ezdxf.readfile(str(saved))
        model = {e.dxf.handle: e.dxftype() for e in reread.modelspace()}
        on_sheet = sorted(e.dxf.handle for e in reread.layout("Layout1"))
        space = (circle.get("properties") or {}).get("space")
        in_model = (
            active_on_disk == "Layout1"
            and space in (None, "Model")
            and model.get(circle.get("handle")) == "CIRCLE"
            and on_sheet == paper
        )
        shifted = [[x, y - 30.0] for x, y in under_world]
        trim_ok = under_ucs == [[0.0, 0.0], [10.0, 0.0]] and shifted == under_ucs
        return (
            in_model and trim_ok,
            {
                "active_layout_on_disk": active_on_disk,
                "circle_space": space or "Model",
                "circle_in_model_space": model.get(circle.get("handle")) == "CIRCLE",
                "sheet_entities": [len(paper), len(on_sheet)],
                "trim_under_ucs": under_ucs,
                "trim_under_world": under_world,
            },
            [str(source), str(saved)],
        )
