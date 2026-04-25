"""Durable workspace log streams and in-process realtime broadcast."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

    async def read(
        self,
        *,
        path: Path,
        offset: int,
        limit_bytes: int,
    ) -> tuple[str, int, bool]:
        data = path.read_bytes()
        safe_offset = min(max(offset, 0), len(data))
        chunk = data[safe_offset : safe_offset + limit_bytes]
        next_offset = safe_offset + len(chunk)
        return chunk.decode("utf-8", errors="replace"), next_offset, next_offset >= len(data)


async def stream_compose_service_logs(
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    log_store: LogStore,
) -> None:
    """Tail compose service logs into durable workspace streams until cancelled."""
    sinks = await log_store.open_command_streams(
        workspace_id=workspace_id,
        base_stream_id="services.compose",
        source="service",
        name="Compose services",
    )
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        "--project-name",
        compose_project,
        "--file",
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
                await asyncio.wait_for(proc.wait(), timeout=5)
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

    async def write(self, data: str) -> None:
        if not data:
            return
        encoded = data.encode("utf-8")
        offset = self.path.stat().st_size if self.path.exists() else 0
        with self.path.open("ab") as handle:
            handle.write(encoded)
        if self.session_factory is not None:
            async with self.session_factory() as session:
                repo = WorkspaceLogStreamRepository(session)
                await repo.append_metadata(
                    workspace_id=self.workspace_id,
                    stream_id=self.stream_id,
                    byte_delta=len(encoded),
                    line_delta=data.count("\n"),
                )
                await session.commit()
        await self.broadcaster.publish(
            workspace_id=self.workspace_id,
            stream_id=self.stream_id,
            source=self.source,
            fd=self.fd,
            offset=offset,
            data=data,
        )

    async def close(self) -> None:
        if self.session_factory is None:
            return
        async with self.session_factory() as session:
            repo = WorkspaceLogStreamRepository(session)
            await repo.close(workspace_id=self.workspace_id, stream_id=self.stream_id)
            await session.commit()


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
