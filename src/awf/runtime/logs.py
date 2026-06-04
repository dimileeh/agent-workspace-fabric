"""Durable workspace log streams and in-process realtime broadcast."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.redaction import redact_secrets
from awf.db.repositories import WorkspaceLogStreamRepository


@dataclass(frozen=True)
class LogFrame:
    seq: int
    workspace_id: str
    stream_id: str
    source: str
    fd: str
    offset: int
    data: str
    occurred_at: datetime

    def model_dump(self) -> dict[str, object]:
        return {
            "type": "log",
            "seq": self.seq,
            "workspace_id": self.workspace_id,
            "stream_id": self.stream_id,
            "source": self.source,
            "fd": self.fd,
            "offset": self.offset,
            "data": self.data,
            "occurred_at": self.occurred_at.isoformat(),
        }


class LogBroadcaster:
    """Tiny per-process pub/sub for active workspace WebSocket clients."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[LogFrame]]] = defaultdict(set)
        self._seq = count(1)

    @asynccontextmanager
    async def subscribe(self, workspace_id: str) -> AsyncIterator[asyncio.Queue[LogFrame]]:
        queue: asyncio.Queue[LogFrame] = asyncio.Queue(maxsize=1000)
        self._subscribers[workspace_id].add(queue)
        try:
            yield queue
        finally:
            self._subscribers[workspace_id].discard(queue)
            if not self._subscribers[workspace_id]:
                del self._subscribers[workspace_id]

    async def publish(
        self,
        *,
        workspace_id: str,
        stream_id: str,
        source: str,
        fd: str,
        offset: int,
        data: str,
    ) -> None:
        redacted_data = await asyncio.to_thread(redact_secrets, data)
        self.publish_redacted(
            workspace_id=workspace_id,
            stream_id=stream_id,
            source=source,
            fd=fd,
            offset=offset,
            data=redacted_data,
        )

    def publish_redacted(
        self,
        *,
        workspace_id: str,
        stream_id: str,
        source: str,
        fd: str,
        offset: int,
        data: str,
    ) -> None:
        """Publish data that has already passed through redact_secrets."""
        frame = LogFrame(
            seq=next(self._seq),
            workspace_id=workspace_id,
            stream_id=stream_id,
            source=source,
            fd=fd,
            offset=offset,
            data=data,
            occurred_at=datetime.now(UTC),
        )
        for queue in tuple(self._subscribers.get(workspace_id, ())):
            with suppress(asyncio.QueueFull):
                queue.put_nowait(frame)


LOG_BROADCASTER = LogBroadcaster()


class _ComposeLogsProcess(Protocol):
    stdout: asyncio.StreamReader | None
    stderr: asyncio.StreamReader | None
    returncode: int | None

    def terminate(self) -> None: ...  # pragma: no cover - Protocol method declaration only.

    def kill(self) -> None: ...  # pragma: no cover - Protocol method declaration only.

    async def wait(self) -> int: ...  # pragma: no cover - Protocol method declaration only.


_ComposeLogsProcessFactory = Callable[..., Awaitable[_ComposeLogsProcess]]


class LogStore:
    """File-backed log store with DB metadata indexing."""

    def __init__(
        self,
        *,
        root: Path,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        broadcaster: LogBroadcaster = LOG_BROADCASTER,
    ) -> None:
        self._root = root
        self._session_factory = session_factory
        self._broadcaster = broadcaster

    async def open_command_streams(
        self,
        *,
        workspace_id: str,
        base_stream_id: str,
        source: str,
        name: str,
    ) -> CommandLogSinks:
        stdout = await self.open_stream(
            workspace_id=workspace_id,
            stream_id=f"{base_stream_id}.stdout",
            source=source,
            name=f"{name} stdout",
            kind="stdout",
        )
        stderr = await self.open_stream(
            workspace_id=workspace_id,
            stream_id=f"{base_stream_id}.stderr",
            source=source,
            name=f"{name} stderr",
            kind="stderr",
        )
        return CommandLogSinks(stdout=stdout, stderr=stderr)

    async def open_stream(
        self,
        *,
        workspace_id: str,
        stream_id: str,
        source: str,
        name: str,
        kind: str,
    ) -> WorkspaceLogSink:
        stream_path = self._root / workspace_id / f"{_safe_stream_id(stream_id)}.log"
        stream_path.parent.mkdir(parents=True, exist_ok=True)
        stream_path.touch(exist_ok=True)
        if self._session_factory is not None:
            async with self._session_factory() as session:
                repo = WorkspaceLogStreamRepository(session)
                await repo.create_or_get(
                    workspace_id=workspace_id,
                    stream_id=stream_id,
                    source=source,
                    name=name,
                    kind=kind,
                    path=str(stream_path),
                )
                await session.commit()
        return WorkspaceLogSink(
            workspace_id=workspace_id,
            stream_id=stream_id,
            source=source,
            fd=kind,
            path=stream_path,
            session_factory=self._session_factory,
            broadcaster=self._broadcaster,
        )

    async def append_to_stream(
        self,
        *,
        workspace_id: str,
        stream_id: str,
        source: str,
        fd: str,
        data: str,
        close_after_append: bool = False,
    ) -> None:
        """Append data to an already indexed stream without opening new sinks."""
        if not data:
            return
        stream_path = self._root / workspace_id / f"{_safe_stream_id(stream_id)}.log"
        stream_path.parent.mkdir(parents=True, exist_ok=True)
        stream_path.touch(exist_ok=True)
        redacted_data = await asyncio.to_thread(redact_secrets, data)
        encoded = redacted_data.encode("utf-8")
        offset = await asyncio.to_thread(_append_log_bytes, stream_path, encoded)
        if self._session_factory is not None:
            async with self._session_factory() as session:
                repo = WorkspaceLogStreamRepository(session)
                await repo.append_metadata(
                    workspace_id=workspace_id,
                    stream_id=stream_id,
                    byte_delta=len(encoded),
                    line_delta=redacted_data.count("\n"),
                )
                if close_after_append:
                    await repo.close(workspace_id=workspace_id, stream_id=stream_id)
                await session.commit()
        self._broadcaster.publish_redacted(
            workspace_id=workspace_id,
            stream_id=stream_id,
            source=source,
            fd=fd,
            offset=offset,
            data=redacted_data,
        )

    async def read(
        self,
        *,
        path: Path,
        offset: int,
        limit_bytes: int,
    ) -> tuple[str, int, bool]:
        return await read_log_chunk(
            path=self._resolve_read_path(path),
            offset=offset,
            limit_bytes=limit_bytes,
        )

    def _resolve_read_path(self, path: Path) -> Path:
        read_path = path if path.is_absolute() else self._root / path
        root = self._root.resolve()
        resolved = read_path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ValueError("LogStore.read path must be within root") from None
        return resolved


async def read_log_chunk(
    *,
    path: Path,
    offset: int,
    limit_bytes: int,
) -> tuple[str, int, bool]:
    chunk, next_offset, eof = await read_log_chunk_bytes(
        path=path,
        offset=offset,
        limit_bytes=limit_bytes,
    )
    return chunk.decode("utf-8", errors="replace"), next_offset, eof


async def read_log_chunk_bytes(
    *,
    path: Path,
    offset: int,
    limit_bytes: int,
) -> tuple[bytes, int, bool]:
    """Read a byte-preserving log chunk without decoding caller offsets."""
    return await asyncio.to_thread(_read_log_chunk_bytes, path, offset, limit_bytes)


def _read_log_chunk_bytes(path: Path, offset: int, limit_bytes: int) -> tuple[bytes, int, bool]:
    """Read a bounded byte chunk and return the next byte offset."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        file_size = handle.tell()
        safe_offset = min(max(offset, 0), file_size)
        handle.seek(safe_offset)
        chunk = handle.read(max(limit_bytes, 0))

    next_offset = safe_offset + len(chunk)
    return chunk, next_offset, next_offset >= file_size


async def stream_compose_service_logs(
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    log_store: LogStore,
    process_factory: _ComposeLogsProcessFactory | None = None,
    terminate_timeout_seconds: float = 5,
) -> None:
    """Tail compose service logs into durable workspace streams until cancelled."""
    sinks = await log_store.open_command_streams(
        workspace_id=workspace_id,
        base_stream_id="services.compose",
        source="service",
        name="Compose services",
    )
    factory = process_factory or cast(_ComposeLogsProcessFactory, asyncio.create_subprocess_exec)
    proc = await factory(
        "docker",
        "compose",
        "-p",
        compose_project,
        "-f",
        str(compose_file),
        "logs",
        "--follow",
        "--no-color",
        "--timestamps",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None

    async def read_to_sink(reader: asyncio.StreamReader, sink: WorkspaceLogSink) -> None:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                return
            await sink.write(chunk.decode("utf-8", errors="replace"))

    try:
        await asyncio.gather(
            read_to_sink(proc.stdout, sinks.stdout),
            read_to_sink(proc.stderr, sinks.stderr),
        )
        await proc.wait()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=terminate_timeout_seconds)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        raise
    finally:
        await sinks.close()


@dataclass
class WorkspaceLogSink:
    workspace_id: str
    stream_id: str
    source: str
    fd: str
    path: Path
    session_factory: async_sessionmaker[AsyncSession] | None
    broadcaster: LogBroadcaster
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _pending_data: str = field(default="", init=False, repr=False)

    async def write(self, data: str) -> None:
        if not data:
            return
        frame: tuple[int, str] | None = None
        async with self._write_lock:
            self._pending_data += data
            raw_data = self._pop_complete_lines_locked()
            if raw_data:
                frame = await self._append_redacted_locked(raw_data)
        if frame is not None:
            await self._publish_frame(frame)

    async def close(self) -> None:
        frame: tuple[int, str] | None = None
        async with self._write_lock:
            if self._pending_data:
                raw_data = self._pending_data
                self._pending_data = ""
                frame = await self._append_redacted_locked(raw_data)
            if self.session_factory is not None:
                async with self.session_factory() as session:
                    repo = WorkspaceLogStreamRepository(session)
                    await repo.close(workspace_id=self.workspace_id, stream_id=self.stream_id)
                    await session.commit()
        if frame is not None:
            await self._publish_frame(frame)

    def _pop_complete_lines_locked(self) -> str:
        # Hold partial lines so token prefixes split across writes are redacted
        # before any bytes are persisted or streamed.
        last_newline = self._pending_data.rfind("\n")
        if last_newline == -1:
            return ""
        split_at = last_newline + 1
        raw_data = self._pending_data[:split_at]
        self._pending_data = self._pending_data[split_at:]
        return raw_data

    async def _append_redacted_locked(self, raw_data: str) -> tuple[int, str]:
        redacted_data = await asyncio.to_thread(redact_secrets, raw_data)
        encoded = redacted_data.encode("utf-8")
        offset = await asyncio.to_thread(_append_log_bytes, self.path, encoded)
        if self.session_factory is not None:
            async with self.session_factory() as session:
                repo = WorkspaceLogStreamRepository(session)
                await repo.append_metadata(
                    workspace_id=self.workspace_id,
                    stream_id=self.stream_id,
                    byte_delta=len(encoded),
                    line_delta=redacted_data.count("\n"),
                )
                await session.commit()
        return offset, redacted_data

    async def _publish_frame(self, frame: tuple[int, str]) -> None:
        offset, redacted_data = frame
        self.broadcaster.publish_redacted(
            workspace_id=self.workspace_id,
            stream_id=self.stream_id,
            source=self.source,
            fd=self.fd,
            offset=offset,
            data=redacted_data,
        )


def _append_log_bytes(path: Path, data: bytes) -> int:
    offset = path.stat().st_size if path.exists() else 0
    with path.open("ab") as handle:
        handle.write(data)
    return offset


@dataclass
class CommandLogSinks:
    stdout: WorkspaceLogSink
    stderr: WorkspaceLogSink

    async def write_stdout(self, data: str) -> None:
        await self.stdout.write(data)

    async def write_stderr(self, data: str) -> None:
        await self.stderr.write(data)

    async def close(self) -> None:
        await self.stdout.close()
        await self.stderr.close()


def _safe_stream_id(stream_id: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in stream_id)
