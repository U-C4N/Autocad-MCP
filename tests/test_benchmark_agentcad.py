"""Registration, provenance refusal, and unchanged delegated verdicts."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path

import pytest

from benchmarks.adapters import agentcad
from benchmarks.adapters.base import EnvironmentBlocked, TaskResult
from benchmarks.run_competitors import _load_registry, _make_adapter, run_tasks
from benchmarks.tasks_v2 import TaskSpec


def test_registered_lane_uses_the_measured_product_pin():
    entry = _load_registry()["agentcad"]
    assert entry["pinned_sha"] == agentcad.PINNED_SHA
    assert entry["backends"] == ["ezdxf"]
    assert isinstance(_make_adapter(entry, "ezdxf"), agentcad.AgentCADAdapter)
    with pytest.raises(ValueError, match="headless ezdxf"):
        _make_adapter(entry, "com")


@pytest.mark.asyncio
async def test_absent_no_fetch_environment_is_unscored(tmp_path, monkeypatch):
    monkeypatch.setattr(agentcad.importlib.util, "find_spec", lambda name: object())
    adapter = agentcad.AgentCADAdapter()
    adapter.cache_root = tmp_path / "empty-cache"
    adapter.fetch = False
    report = await run_tasks(
        adapter, [TaskSpec("core_geometry", "geometry", "probe", 1)], tmp_path / "out"
    )
    assert report["summary"]["score"] is None
    assert report["summary"]["environment_blocked"] == 1
    assert not adapter.cache_root.exists()  # no clone, install, or synthetic zero


@pytest.mark.asyncio
async def test_existing_evidence_is_not_overwritten(tmp_path):
    evidence = tmp_path / "events.jsonl"
    evidence.write_text("original evidence", encoding="utf-8")
    with pytest.raises(EnvironmentBlocked, match="new artifact directory"):
        await agentcad.AgentCADAdapter().setup(tmp_path)
    assert evidence.read_text(encoding="utf-8") == "original evidence"


@pytest.mark.asyncio
async def test_missing_evaluator_dependency_is_reported_before_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(agentcad.importlib.util, "find_spec", lambda name: None)

    def forbidden(*args, **kwargs):
        pytest.fail("must not fetch when evaluator dependencies are missing")

    monkeypatch.setattr(agentcad, "ensure_competitor", forbidden)
    with pytest.raises(EnvironmentBlocked, match="pypdf"):
        await agentcad.AgentCADAdapter().setup(tmp_path)


@pytest.fixture
def playbook_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = b"SENTINEL = 'pinned source'\n"
    for relative in agentcad.PLAYBOOK_FILES:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "--quiet")
    git("-c", "core.autocrlf=false", "add", "--", *agentcad.PLAYBOOK_FILES)
    git(
        "-c",
        "user.name=Benchmark Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    monkeypatch.setattr(agentcad, "PINNED_SHA", git("rev-parse", "HEAD"))
    return repo, source


def test_only_pinned_source_is_executed_and_hashed(playbook_repo):
    repo, source = playbook_repo
    module, hashes = agentcad._load_playbook(repo)
    assert module.SENTINEL == "pinned source"
    assert hashes == {name: hashlib.sha256(source).hexdigest() for name in agentcad.PLAYBOOK_FILES}
    assert Path(module.__file__).parent.parent.parent == repo


@pytest.mark.parametrize("change", ["tracked", "untracked", "wrong_pin"])
def test_changed_cached_sources_are_refused_before_loading(playbook_repo, monkeypatch, change):
    repo, _ = playbook_repo
    if change == "wrong_pin":
        monkeypatch.setattr(agentcad, "PINNED_SHA", "0" * 40)
    else:
        path = repo / (agentcad.PLAYBOOK_FILES[0] if change == "tracked" else "unexpected.py")
        path.write_text("raise AssertionError('unverified code executed')", encoding="utf-8")
    with pytest.raises(EnvironmentBlocked, match="pinned commit|dirty"):
        agentcad._load_playbook(repo)


@pytest.mark.parametrize("status,score", [("fail", 0), ("partial", 0), ("pass", 100)])
@pytest.mark.asyncio
async def test_delegated_verdict_and_artifacts_are_not_upgraded(status, score, capsys):
    expected = TaskResult("probe", status, score, artifacts=["drawing.dxf"])

    class Delegate:
        async def run_task(self, task):
            print("progress")
            return expected

    adapter = agentcad.AgentCADAdapter()
    adapter._delegate = Delegate()
    actual = await adapter.run_task(TaskSpec("probe", "geometry", "probe", 1))
    assert actual is expected
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "progress\n"


@pytest.mark.asyncio
async def test_cancellation_propagates_to_the_playbook():
    started = asyncio.Event()
    stopped = asyncio.Event()

    class Delegate:
        async def run_task(self, task):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    adapter = agentcad.AgentCADAdapter()
    adapter._delegate = Delegate()
    task = asyncio.create_task(adapter.run_task(TaskSpec("probe", "geometry", "probe", 1)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
