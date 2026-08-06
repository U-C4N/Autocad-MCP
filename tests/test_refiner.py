from __future__ import annotations

import pytest

import config
from engineering.refiner import RepairRegistry, refine_drawing

pytestmark = pytest.mark.asyncio


async def test_transaction_stack_is_separate_from_drawing_undo(backend, monkeypatch):
    """An undo must not consume a transaction checkpoint, or vice versa."""
    monkeypatch.setattr(config.settings, "ezdxf_undo_depth", 4)
    await backend.drawing_new()
    await backend.entity_create_line(0, 0, 10, 0)
    await backend.transaction_begin()

    undo = await backend.drawing_undo()
    status = await backend.system_status()

    assert undo["ok"] is True, "undo walks its own history"
    assert status["transaction_depth"] == 1, "and leaves the checkpoint standing"
    await backend.transaction_rollback()


async def test_transaction_rollback_restores_document(backend):
    await backend.entity_create_line(0, 0, 10, 0)
    await backend.transaction_begin()
    await backend.entity_create_circle(5, 5, 2)

    await backend.transaction_rollback()

    entities = await backend.entity_list(limit=100)
    assert [entity.type for entity in entities] == ["LINE"]


async def test_refiner_does_not_mutate_when_transaction_cannot_start(backend, monkeypatch):
    await backend.entity_create_line(0, 0, 20, 0)
    await backend.entity_create_line(0, 0, 20, 0)

    async def transaction_rejected():
        return {"ok": False, "error": "A transaction is already active"}

    async def unexpected_transaction_end():
        raise AssertionError("refiner must not end a transaction it did not start")

    monkeypatch.setattr(backend, "transaction_begin", transaction_rejected)
    monkeypatch.setattr(backend, "transaction_commit", unexpected_transaction_end)
    monkeypatch.setattr(backend, "transaction_rollback", unexpected_transaction_end)

    result = await refine_drawing(backend, focus=["duplicate_entities"])

    assert result.status == "transaction_unavailable"
    assert result.rounds[0].transaction == "not_started"
    assert len(await backend.entity_list(type_filter="LINE")) == 2


async def test_refiner_repairs_construction_and_duplicates(backend):
    await backend.entity_create_line(0, 0, 20, 0)
    await backend.entity_create_line(0, 0, 20, 0)
    await backend.construction_xline(0, 0, 0)

    result = await refine_drawing(
        backend,
        max_rounds=3,
        min_score=95,
        focus=["duplicate_entities", "construction_left"],
    )

    assert result.status == "threshold_met"
    assert result.final_score == 100.0
    assert result.rounds[0].score_after > result.rounds[0].score_before
    assert await backend.drawing_critique(["duplicate_entities", "construction_left"]) == []


async def test_refiner_dry_run_does_not_mutate_drawing(backend):
    await backend.entity_create_line(0, 0, 20, 0)
    await backend.entity_create_line(0, 0, 20, 0)

    result = await refine_drawing(
        backend,
        focus=["duplicate_entities"],
        dry_run=True,
    )

    assert result.status == "dry_run"
    assert len(await backend.entity_list(type_filter="LINE")) == 2
    assert result.rounds[0].actions[0].status == "planned"


async def test_refiner_rolls_back_when_custom_repair_lowers_score(backend):
    await backend.entity_create_line(0, 0, 20, 0)
    await backend.entity_create_line(0, 0, 20, 0)

    registry = RepairRegistry()

    async def harmful_repair(active_backend, issue):
        await active_backend.construction_xline(0, 0, 0)
        return {"created": "construction"}

    registry.register("duplicate_entities", harmful_repair)
    before = len(await backend.entity_list(limit=100))

    result = await refine_drawing(
        backend,
        focus=["duplicate_entities", "construction_left"],
        registry=registry,
    )

    assert result.status == "rolled_back"
    assert result.rounds[0].transaction == "rolled_back"
    assert len(await backend.entity_list(limit=100)) == before


async def test_refiner_leaves_missing_gdt_datum_for_manual_resolution(backend):
    backend._gdt_datums_referenced = {"A"}

    result = await refine_drawing(backend, focus=["gdt"])

    assert result.status == "manual_required"
    assert result.remaining_issues[0]["focus"] == "gdt"


async def test_server_drawing_refine_returns_structured_payload(backend):
    import server

    class Ctx:
        lifespan_context = {"backend": backend}

    await backend.entity_create_line(0, 0, 20, 0)
    await backend.entity_create_line(0, 0, 20, 0)

    result = await server.drawing_refine(
        max_rounds=3,
        min_score=95,
        focus=["duplicate_entities"],
        ctx=Ctx(),
    )

    assert result["status"] == "threshold_met"
    assert result["final_score"] == 100.0
