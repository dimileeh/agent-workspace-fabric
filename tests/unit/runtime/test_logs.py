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
from awf.runtime.logs import LogBroadcaster, LogStore, WorkspaceLogSink, stream_compose_service_logs
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
async def test_log_store_read_rejects_paths_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("outside\n")

    with pytest.raises(ValueError, match="within root"):
        await LogStore(root=root).read(
            path=outside,
            offset=0,
            limit_bytes=16,
        )


@pytest.mark.unit
async def test_log_sink_write_uses_threaded_file_append(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    path = tmp_path / "workspace.log"
    path.write_bytes(b"abc")
    to_thread_calls: list[tuple[Callable[..., object], tuple[object, ...]]] = []

    async def fake_to_thread(func: Callable[..., object], /, *args: object) -> object:
        to_thread_calls.append((func, args))
        return func(*args)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    broadcaster = LogBroadcaster()
    sink = WorkspaceLogSink(
        workspace_id="ws_1",
        stream_id="agent.stdout",
        source="agent",
        fd="stdout",
        path=path,
        session_factory=None,
        broadcaster=broadcaster,
    )

    async with broadcaster.subscribe("ws_1") as queue:
        await sink.write("def\n")
        frame = queue.get_nowait()

    assert len(to_thread_calls) == 1
    assert to_thread_calls[0][0].__name__ == "_append_log_bytes"
    assert path.read_bytes() == b"abcdef\n"
    assert frame.offset == 3
    assert frame.data == "def\n"


@pytest.mark.unit
async def test_log_sink_updates_metadata_under_write_lock(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
    tmp_path: Path,
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
    sink = await store.open_stream(
        workspace_id=workspace.id,
        stream_id="agent.stdout",
        source="agent",
        name="Codex agent stdout",
        kind="stdout",
    )

    original_append_metadata = WorkspaceLogStreamRepository.append_metadata
    observed_lock_states: list[bool] = []

    async def append_metadata_spy(
        self: WorkspaceLogStreamRepository,
        **kwargs: Any,
    ) -> object:
        observed_lock_states.append(sink._write_lock.locked())
        return await original_append_metadata(self, **kwargs)

    monkeypatch.setattr(WorkspaceLogStreamRepository, "append_metadata", append_metadata_spy)

    await sink.write("hello\n")

    async with factory() as session:
        stream = await WorkspaceLogStreamRepository(session).get(
            workspace_id=workspace.id,
            stream_id="agent.stdout",
        )

    assert observed_lock_states == [True]
    assert stream is not None
    assert stream.byte_count == len("hello\n")
    assert stream.line_count == 1


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
async def test_log_broadcaster_removes_empty_workspace_bucket() -> None:
    broadcaster = LogBroadcaster()

    async with broadcaster.subscribe("ws_cleanup") as queue:
        assert queue in broadcaster._subscribers["ws_cleanup"]

    assert "ws_cleanup" not in broadcaster._subscribers


@pytest.mark.unit
async def test_open_stream_without_session_factory_sanitizes_path(tmp_path: Path) -> None:
    store = LogStore(root=tmp_path)

    sink = await store.open_stream(
        workspace_id="ws_logs",
        stream_id="agent/stdout:live",
        source="agent",
        name="Agent stdout",
        kind="stdout",
    )

    assert sink.path == tmp_path / "ws_logs" / "agent_stdout_live.log"
    assert sink.path.exists()


@pytest.mark.unit
async def test_log_store_read_clamps_offsets_and_zero_limits(tmp_path: Path) -> None:
    path = tmp_path / "ws" / "agent.log"
    path.parent.mkdir()
    path.write_text("abcdef", encoding="utf-8")
    store = LogStore(root=tmp_path)

    data, next_offset, eof = await store.read(path=path, offset=-10, limit_bytes=2)
    assert (data, next_offset, eof) == ("ab", 2, False)

    data, next_offset, eof = await store.read(path=path, offset=99, limit_bytes=4)
    assert (data, next_offset, eof) == ("", 6, True)

    data, next_offset, eof = await store.read(path=path, offset=2, limit_bytes=0)
    assert (data, next_offset, eof) == ("", 2, False)


@pytest.mark.unit
async def test_log_sink_empty_write_and_close_without_session_are_noops(tmp_path: Path) -> None:
    path = tmp_path / "workspace.log"
    path.write_text("seed", encoding="utf-8")
    broadcaster = LogBroadcaster()
    sink = WorkspaceLogSink(
        workspace_id="ws_empty",
        stream_id="agent.stdout",
        source="agent",
        fd="stdout",
        path=path,
        session_factory=None,
        broadcaster=broadcaster,
    )

    async with broadcaster.subscribe("ws_empty") as queue:
        await sink.write("")
        await sink.close()

    assert path.read_text(encoding="utf-8") == "seed"
    assert queue.empty()


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


class _FakeComposeLogsProcess:
    def __init__(self, *, exit_after_terminate: bool) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(b"service out\n")
        self.stderr.feed_data(b"service err\n")
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._exit_after_terminate = exit_after_terminate
        self._exit_event = asyncio.Event()
        self.wait_started = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        if self._exit_after_terminate:
            self.returncode = 0
            self._exit_event.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._exit_event.set()

    async def wait(self) -> int:
        self.wait_started.set()
        if self.returncode is None:
            await self._exit_event.wait()
        assert self.returncode is not None
        return self.returncode


@pytest.mark.unit
async def test_stream_compose_service_logs_terminates_process_on_cancel(tmp_path: Path) -> None:
    process = _FakeComposeLogsProcess(exit_after_terminate=True)

    async def process_factory(*_args: object, **_kwargs: object) -> _FakeComposeLogsProcess:
        return process

    task = asyncio.create_task(
        stream_compose_service_logs(
            workspace_id="ws_compose_logs",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            log_store=LogStore(root=tmp_path / "logs"),
            process_factory=process_factory,
        )
    )
    await asyncio.wait_for(process.wait_started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.terminated is True
    assert process.killed is False
    assert (
        tmp_path / "logs" / "ws_compose_logs" / "services.compose.stdout.log"
    ).read_text(encoding="utf-8") == "service out\n"
    assert (
        tmp_path / "logs" / "ws_compose_logs" / "services.compose.stderr.log"
    ).read_text(encoding="utf-8") == "service err\n"


@pytest.mark.unit
async def test_stream_compose_service_logs_kills_process_after_terminate_timeout(
    tmp_path: Path,
) -> None:
    process = _FakeComposeLogsProcess(exit_after_terminate=False)

    async def process_factory(*_args: object, **_kwargs: object) -> _FakeComposeLogsProcess:
        return process

    task = asyncio.create_task(
        stream_compose_service_logs(
            workspace_id="ws_compose_logs_kill",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            log_store=LogStore(root=tmp_path / "logs"),
            process_factory=process_factory,
            terminate_timeout_seconds=0.01,
        )
    )
    await asyncio.wait_for(process.wait_started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.terminated is True
    assert process.killed is True
