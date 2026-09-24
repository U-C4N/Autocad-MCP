"""Reference adapter for this repository, covering the v7 task matrix.

Every task verifies against a value worked out independently of the code under
test — a closed-form area, a count of entities placed on purpose, a token
ceiling fixed before the measurement. A task that only checks the server agrees
with itself is a task that cannot fail.
"""

from __future__ import annotations

import math
from pathlib import Path

from benchmarks.adapters.base import BenchmarkAdapter, TaskResult
from benchmarks.tasks_v7 import TaskSpec
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
            from backends.com_backend import ComBackend

            self.backend = ComBackend()
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

        cable = {row["tag"]: row for row in cable_rows(pid, layout)["rows"]}
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
