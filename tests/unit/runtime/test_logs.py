"""Workspace log store tests."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.runtime.logs as logs_module
from awf.adapters.codex import CodexAdapter
from awf.common.commands import FakeCommandRunner
from awf.common.redaction import REDACTION_MARKER
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

    assert [call[0].__name__ for call in to_thread_calls] == ["redact_secrets", "_append_log_bytes"]
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
async def test_workspace_log_sink_redacts_persisted_data_live_frames_and_metadata(
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@example.com:repo/app.git",
            branch_base="main",
            task_title="Redact logs",
            task_prompt="Keep workspace logs safe.",
            agent="codex",
            test_commands=[],
        )
        await session.commit()

    broadcaster = LogBroadcaster()
    store = LogStore(root=tmp_path, session_factory=factory, broadcaster=broadcaster)
    sink = await store.open_stream(
        workspace_id=workspace.id,
        stream_id="agent.stdout",
        source="agent",
        name="Codex agent stdout",
        kind="stdout",
    )
    raw_secret_bodies = (
        "ghp_LOGredactGitHubToken123456",
        "sk-fakeOpenAIKey123456789",
        "sk-ant-fakeAnthropicKey123456789",
        "AIzaFakeGeminiApiKey1234567890ABCD",
        "frameBearerToken123456",
        "url-password-value",
        "awf-api-token-123456",
    )
    raw_line = (
        "context before "
        "github ghp_LOGredactGitHubToken123456 "
        "openai sk-fakeOpenAIKey123456789 "
        "anthropic sk-ant-fakeAnthropicKey123456789 "
        "gemini AIzaFakeGeminiApiKey1234567890ABCD "
        "Authorization: Bearer frameBearerToken123456 "
        "repo https://user:url-password-value@github.com/example/repo.git "
        "AWF_API_TOKEN=awf-api-token-123456 context after\n"
    )

    async with broadcaster.subscribe(workspace.id) as queue:
        await sink.write(raw_line)
        frame = await asyncio.wait_for(queue.get(), timeout=1)

    persisted = sink.path.read_text(encoding="utf-8")
    for raw_secret_body in raw_secret_bodies:
        assert raw_secret_body not in persisted
        assert raw_secret_body not in frame.data
    assert "context before" in persisted
    assert "context after" in persisted
    assert "Authorization: Bearer <redacted>" in persisted
    assert "https://<redacted>@github.com/example/repo.git" in persisted
    assert "AWF_API_TOKEN=<redacted>" in persisted
    assert persisted.count(REDACTION_MARKER) == 7
    assert frame.data == persisted
    assert frame.offset == 0

    async with factory() as session:
        stream = await WorkspaceLogStreamRepository(session).get(
            workspace_id=workspace.id,
            stream_id="agent.stdout",
        )

    assert stream is not None
    assert stream.byte_count == len(persisted.encode("utf-8"))
    assert stream.line_count == 1


@pytest.mark.unit
async def test_workspace_log_sink_does_not_redact_live_frame_after_persisting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    redaction_inputs: list[str] = []

    def fake_redact_secrets(data: str) -> str:
        redaction_inputs.append(data)
        return data.replace("token", "token<redaction-pass>")

    monkeypatch.setattr(logs_module, "redact_secrets", fake_redact_secrets)
    path = tmp_path / "workspace.log"
    broadcaster = LogBroadcaster()
    sink = WorkspaceLogSink(
        workspace_id="ws_single_redact",
        stream_id="agent.stdout",
        source="agent",
        fd="stdout",
        path=path,
        session_factory=None,
        broadcaster=broadcaster,
    )

    async with broadcaster.subscribe("ws_single_redact") as queue:
        await sink.write("live token\n")
        frame = await asyncio.wait_for(queue.get(), timeout=1)

    persisted = path.read_text(encoding="utf-8")
    assert redaction_inputs == ["live token\n"]
    assert persisted == "live token<redaction-pass>\n"
    assert frame.data == persisted


@pytest.mark.unit
async def test_workspace_log_sink_redacts_tokens_split_across_write_boundaries(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace.log"
    broadcaster = LogBroadcaster()
    sink = WorkspaceLogSink(
        workspace_id="ws_split",
        stream_id="agent.stdout",
        source="agent",
        fd="stdout",
        path=path,
        session_factory=None,
        broadcaster=broadcaster,
    )

    async with broadcaster.subscribe("ws_split") as queue:
        await sink.write("context ghp_FA")
        assert not path.exists() or path.read_text(encoding="utf-8") == ""
        assert queue.empty()

        await sink.write("KEgithubTokenValue123456 done\n")
        frame = await asyncio.wait_for(queue.get(), timeout=1)

    persisted = path.read_text(encoding="utf-8")
    assert persisted == f"context {REDACTION_MARKER} done\n"
    assert frame.data == persisted
    assert frame.offset == 0
    for raw_fragment in ("ghp_FA", "KEgithubTokenValue123456"):
        assert raw_fragment not in persisted
        assert raw_fragment not in frame.data


@pytest.mark.unit
async def test_workspace_log_sink_flushes_pending_redacted_data_on_close(tmp_path: Path) -> None:
    path = tmp_path / "workspace.log"
    broadcaster = LogBroadcaster()
    sink = WorkspaceLogSink(
        workspace_id="ws_pending",
        stream_id="agent.stdout",
        source="agent",
        fd="stdout",
        path=path,
        session_factory=None,
        broadcaster=broadcaster,
    )

    async with broadcaster.subscribe("ws_pending") as queue:
        await sink.write("partial ghp_PENDINGgithubTokenValue123456")
        assert not path.exists() or path.read_text(encoding="utf-8") == ""
        assert queue.empty()

        await sink.close()
        frame = await asyncio.wait_for(queue.get(), timeout=1)

    persisted = path.read_text(encoding="utf-8")
    assert persisted == f"partial {REDACTION_MARKER}"
    assert frame.data == persisted
    assert "ghp_PENDINGgithubTokenValue123456" not in persisted
    assert "ghp_PENDINGgithubTokenValue123456" not in frame.data


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


@pytest.mark.unit
async def test_log_broadcaster_redacts_direct_live_frames() -> None:
    broadcaster = LogBroadcaster()

    async with broadcaster.subscribe("ws_1") as queue:
        await broadcaster.publish(
            workspace_id="ws_1",
            stream_id="agent.stdout",
            source="agent",
            fd="stdout",
            offset=0,
            data="Authorization: Bearer directBearerToken123456\n",
        )
        frame = await asyncio.wait_for(queue.get(), timeout=1)

    assert frame.data == "Authorization: Bearer <redacted>\n"
    assert "directBearerToken123456" not in frame.data


@pytest.mark.unit
async def test_log_broadcaster_offloads_direct_live_frame_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    to_thread_calls: list[tuple[Callable[..., object], tuple[object, ...]]] = []

    async def fake_to_thread(func: Callable[..., object], /, *args: object) -> object:
        to_thread_calls.append((func, args))
        return func(*args)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    broadcaster = LogBroadcaster()

    async with broadcaster.subscribe("ws_1") as queue:
        await broadcaster.publish(
            workspace_id="ws_1",
            stream_id="agent.stdout",
            source="agent",
            fd="stdout",
            offset=0,
            data="Authorization: Bearer directBearerToken123456\n",
        )
        frame = await asyncio.wait_for(queue.get(), timeout=1)

    assert to_thread_calls == [
        (logs_module.redact_secrets, ("Authorization: Bearer directBearerToken123456\n",)),
    ]
    assert frame.data == "Authorization: Bearer <redacted>\n"


@pytest.mark.unit
async def test_log_broadcaster_keeps_workspace_entry_until_last_subscriber_exits() -> None:
    broadcaster = LogBroadcaster()

    async with broadcaster.subscribe("ws_1") as first:
        async with broadcaster.subscribe("ws_1") as second:
            await broadcaster.publish(
                workspace_id="ws_1",
                stream_id="agent.stdout",
                source="agent",
                fd="stdout",
                offset=0,
                data="live\n",
            )
            assert first.get_nowait().data == "live\n"
            assert second.get_nowait().data == "live\n"

        assert "ws_1" in broadcaster._subscribers

    assert "ws_1" not in broadcaster._subscribers


@pytest.mark.unit
async def test_log_store_without_database_opens_writes_and_closes_noop(tmp_path: Path) -> None:
    broadcaster = LogBroadcaster()
    store = LogStore(root=tmp_path, broadcaster=broadcaster)
    sink = await store.open_stream(
        workspace_id="ws_no_db",
        stream_id="custom/stdout",
        source="agent",
        name="Custom stdout",
        kind="stdout",
    )

    async with broadcaster.subscribe("ws_no_db") as queue:
        await sink.write("")
        assert queue.empty()
        await sink.write("hello\n")
        frame = queue.get_nowait()
        await sink.close()

    assert sink.path.name == "custom_stdout.log"
    assert sink.path.read_text(encoding="utf-8") == "hello\n"
    assert frame.offset == 0
    assert frame.data == "hello\n"
    assert frame.seq == 1


@pytest.mark.unit
async def test_log_broadcaster_removes_empty_workspace_bucket() -> None:
    broadcaster = LogBroadcaster()

    async with broadcaster.subscribe("ws_cleanup") as queue:
        assert queue in broadcaster._subscribers["ws_cleanup"]

    assert "ws_cleanup" not in broadcaster._subscribers


@pytest.mark.unit
async def test_log_broadcaster_keeps_workspace_bucket_while_other_subscribers_remain() -> None:
    broadcaster = LogBroadcaster()

    async with broadcaster.subscribe("ws_shared") as first:
        async with broadcaster.subscribe("ws_shared") as second:
            assert first in broadcaster._subscribers["ws_shared"]
            assert second in broadcaster._subscribers["ws_shared"]
        assert "ws_shared" in broadcaster._subscribers
        assert broadcaster._subscribers["ws_shared"] == {first}

    assert "ws_shared" not in broadcaster._subscribers


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

    data, next_offset, eof = await store.read(path=path, offset=999, limit_bytes=5)
    assert (data, next_offset, eof) == ("", 6, True)

    data, next_offset, eof = await store.read(path=path, offset=2, limit_bytes=0)
    assert (data, next_offset, eof) == ("", 2, False)

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
async def test_workspace_log_sink_reopen_behavior(
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@example.com:repo/app.git",
            branch_base="main",
            task_title="Reopen Logs",
            task_prompt="Reopen",
            agent="codex",
            test_commands=[],
        )
        await session.commit()

    store = LogStore(root=tmp_path, session_factory=factory)
    sinks = await store.open_command_streams(
        workspace_id=workspace.id,
        base_stream_id="monitor.log",
        source="monitor",
        name="PR monitor",
    )
    await sinks.write_stdout("first line\n")
    await sinks.close()

    async with factory() as session:
        repo = WorkspaceLogStreamRepository(session)
        stream = await repo.get(workspace_id=workspace.id, stream_id="monitor.log.stdout")
        assert stream is not None
        assert stream.closed_at is not None

    await sinks.write_stdout("second line\n")

    async with factory() as session:
        repo = WorkspaceLogStreamRepository(session)
        stream = await repo.get(workspace_id=workspace.id, stream_id="monitor.log.stdout")
        assert stream is not None
        assert stream.closed_at is None
        assert stream.byte_count == len("first line\nsecond line\n")


@pytest.mark.unit
async def test_log_store_appends_to_existing_closed_stream_without_reopening_sinks(
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@example.com:repo/app.git",
            branch_base="main",
            task_title="Append Logs",
            task_prompt="Append",
            agent="codex",
            test_commands=[],
        )
        await session.commit()

    broadcaster = LogBroadcaster()
    store = LogStore(root=tmp_path, session_factory=factory, broadcaster=broadcaster)
    sink = await store.open_stream(
        workspace_id=workspace.id,
        stream_id="validation.01_healthcheck.stderr",
        source="validation",
        name="healthcheck stderr",
        kind="stderr",
    )
    await sink.write("connection refused\n")
    await sink.close()

    async with broadcaster.subscribe(workspace.id) as queue:
        await store.append_to_stream(
            workspace_id=workspace.id,
            stream_id="validation.01_healthcheck.stderr",
            source="validation",
            fd="stderr",
            data="\nhealth check api failed after 1 attempt(s)\n",
            close_after_append=True,
        )
        frame = await asyncio.wait_for(queue.get(), timeout=1)

    log_path = tmp_path / workspace.id / "validation.01_healthcheck.stderr.log"
    expected = "connection refused\n\nhealth check api failed after 1 attempt(s)\n"
    assert log_path.read_text(encoding="utf-8") == expected
    assert frame.stream_id == "validation.01_healthcheck.stderr"
    assert frame.offset == len("connection refused\n")
    assert frame.data == "\nhealth check api failed after 1 attempt(s)\n"

    async with factory() as session:
        repo = WorkspaceLogStreamRepository(session)
        stream = await repo.get(
            workspace_id=workspace.id,
            stream_id="validation.01_healthcheck.stderr",
        )

    assert stream is not None
    assert stream.closed_at is not None
    assert stream.byte_count == len(expected.encode("utf-8"))
    assert stream.line_count == expected.count("\n")


@pytest.mark.unit
async def test_log_store_append_empty_data_is_noop(tmp_path: Path) -> None:
    store = LogStore(root=tmp_path)

    await store.append_to_stream(
        workspace_id="ws_noop",
        stream_id="validation.stdout",
        source="validation",
        fd="stdout",
        data="",
    )

    assert not (tmp_path / "ws_noop").exists()


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


class _AlreadyExitedComposeLogsProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = 0
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
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
    assert (tmp_path / "logs" / "ws_compose_logs" / "services.compose.stdout.log").read_text(
        encoding="utf-8"
    ) == "service out\n"
    assert (tmp_path / "logs" / "ws_compose_logs" / "services.compose.stderr.log").read_text(
        encoding="utf-8"
    ) == "service err\n"


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


@pytest.mark.unit
async def test_stream_compose_service_logs_cancel_does_not_signal_already_exited_process(
    tmp_path: Path,
) -> None:
    process = _AlreadyExitedComposeLogsProcess()

    async def process_factory(
        *_args: object, **_kwargs: object
    ) -> _AlreadyExitedComposeLogsProcess:
        return process

    task = asyncio.create_task(
        stream_compose_service_logs(
            workspace_id="ws_compose_logs_exited",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            log_store=LogStore(root=tmp_path / "logs"),
            process_factory=process_factory,
        )
    )
    await asyncio.sleep(0)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert process.terminated is False
    assert process.killed is False
