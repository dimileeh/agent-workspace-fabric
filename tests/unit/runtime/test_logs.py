"""Workspace log store tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.adapters.codex import CodexAdapter
from awf.common.commands import FakeCommandRunner
from awf.db.repositories import WorkspaceLogStreamRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.logs import LogBroadcaster, LogStore
from awf.runtime.validation import ValidationRunner


@pytest.mark.unit
async def test_log_store_persists_stream_metadata_and_bytes(
    engine: AsyncEngine,
    tmp_path,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@example.com:repo/app.git",
            branch_base="main",
            task_title="Observe",
            task_prompt="Add logs",
            agent="codex",
            test_commands=[],
        )
        await session.commit()

    store = LogStore(root=tmp_path, session_factory=factory)
    sinks = await store.open_command_streams(
        workspace_id=workspace.id,
        base_stream_id="agent",
        source="agent",
        name="Codex agent",
    )
    await sinks.write_stdout("hello\n")
    await sinks.write_stderr("warning\n")
    await sinks.close()

    async with factory() as session:
        streams = await WorkspaceLogStreamRepository(session).list_for_workspace(workspace.id)

    assert [stream.stream_id for stream in streams] == ["agent.stdout", "agent.stderr"]
    stdout_stream = next(stream for stream in streams if stream.stream_id == "agent.stdout")
    assert stdout_stream.byte_count == len("hello\n")
    assert stdout_stream.line_count == 1
    assert stdout_stream.closed_at is not None

    data, next_offset, eof = await store.read(
        path=tmp_path / workspace.id / "agent.stdout.log",
        offset=0,
        limit_bytes=3,
    )
    assert data == "hel"
    assert next_offset == 3
    assert eof is False


@pytest.mark.unit
async def test_log_store_read_uses_threaded_bounded_file_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    path = tmp_path / "workspace.log"
    path.write_bytes(b"0123456789")
    to_thread_called = False

    async def fake_to_thread(func: Callable[..., object], /, *args: Any) -> object:
        nonlocal to_thread_called
        to_thread_called = True
        return func(*args)

    def fail_read_bytes(self: Path) -> bytes:
        raise AssertionError("read_bytes would load the entire log")

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    data, next_offset, eof = await LogStore(root=tmp_path).read(
        path=path,
        offset=3,
        limit_bytes=4,
    )

    assert to_thread_called is True
    assert data == "3456"
    assert next_offset == 7
    assert eof is False


@pytest.mark.unit
async def test_log_broadcaster_delivers_workspace_frames() -> None:
    broadcaster = LogBroadcaster()

    async with broadcaster.subscribe("ws_1") as queue:
        await broadcaster.publish(
            workspace_id="ws_1",
            stream_id="agent.stdout",
            source="agent",
            fd="stdout",
            offset=0,
            data="live\n",
        )
        frame = await asyncio.wait_for(queue.get(), timeout=1)

    assert frame.workspace_id == "ws_1"
    assert frame.stream_id == "agent.stdout"
    assert frame.data == "live\n"
    assert frame.seq == 1


@pytest.mark.unit
async def test_agent_and_validation_runners_create_indexed_log_streams(
    engine: AsyncEngine,
    tmp_path,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@example.com:repo/app.git",
            branch_base="main",
            task_title="Run with logs",
            task_prompt="Make the tests pass",
            agent="codex",
            test_commands=[],
        )
        await session.commit()

    log_store = LogStore(root=tmp_path / "logs", session_factory=factory)
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="agent out\n", stderr="agent err\n")
    adapter = CodexAdapter(runner=runner, log_store=log_store)

    await adapter.run(
        compose_project="awf_ws",
        compose_file=tmp_path / "compose.yml",
        prompt="Do the work",
        workspace_id=workspace.id,
    )

    runner.queue_result(returncode=0, stdout="validation out\n", stderr="validation err\n")
    validation = ValidationRunner(
        runner=runner,
        artifacts_dir=tmp_path / "artifacts",
        log_store=log_store,
    )
    await validation.run(
        workspace_id=workspace.id,
        compose_project="awf_ws",
        compose_file=tmp_path / "compose.yml",
        test_commands=["pytest -q"],
    )

    async with factory() as session:
        streams = await WorkspaceLogStreamRepository(session).list_for_workspace(workspace.id)

    assert [stream.stream_id for stream in streams] == [
        "agent.stdout",
        "agent.stderr",
        "validation.cmd_01.stdout",
        "validation.cmd_01.stderr",
    ]
    assert (tmp_path / "logs" / workspace.id / "agent.stdout.log").read_text() == "agent out\n"
    assert (
        tmp_path / "logs" / workspace.id / "validation.cmd_01.stderr.log"
    ).read_text() == "validation err\n"
