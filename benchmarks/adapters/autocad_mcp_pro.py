"""Reference adapter for this repository, covering the v3 task matrix.

Every task verifies against a value worked out independently of the code under
test — a closed-form area, a count of entities placed on purpose, a token
ceiling fixed before the measurement. A task that only checks the server agrees
with itself is a task that cannot fail.
"""

from __future__ import annotations

import math
from pathlib import Path

from benchmarks.adapters.base import BenchmarkAdapter, TaskResult
from benchmarks.tasks_v3 import TaskSpec
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
