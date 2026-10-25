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
HEADLESS_ONLY: frozenset[str] = frozenset(task.task_id for task in NEW_TASKS_V8)

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
        passed, metrics, artifacts = await handler()
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
        try:
            full, catalog_count = await _advertised("off")
            discovery, advertised_count = await _advertised("search")
        finally:
            server._apply_discovery_mode("off")

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
        from engineering.layers import apply_layer_set
        from engineering.pid.deliverables import instrument_index, line_list
        from engineering.pid.spec import EXAMPLE_SPEC, run_spec

        await apply_layer_set(self.backend, "pid")
        result = await run_spec(self.backend, EXAMPLE_SPEC)
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

        1.7.0 does not recover a damaged DXF (``ezdxf.recover`` is B09, 1.7.1);
        what it must do is refuse honestly. A valid 50-line DXF is cut in half
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
                error = f"{type(exc).__name__}: {exc}"
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
                "recover": "not in 1.7.0 (B09, 1.7.1)",
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
