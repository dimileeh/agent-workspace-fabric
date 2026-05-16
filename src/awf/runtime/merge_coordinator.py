"""Merge-attempt coordination for PR monitors.

The in-process coordinator is kept for tests and compatibility paths. Service
deployments backed by Postgres can use advisory locks so independently spawned
monitor processes and multiple workers share the same coordination key.
"""

from __future__ import annotations

import asyncio
import hashlib
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol, cast

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncEngine


class MergeCoordinator(Protocol):
    """Coordinates final auto-merge critical sections."""

    def serialized_merge(
        self,
        *,
        repo_url: str,
        base_branch: str,
    ) -> AbstractAsyncContextManager[None]:
        """Return an async context manager for one repo/base merge lane."""


class _AdvisoryConnection(Protocol):
    async def fetchval(  # pragma: no cover - Protocol method declaration only.
        self,
        query: str,
        *args: object,
    ) -> object: ...

    async def execute(  # pragma: no cover - Protocol method declaration only.
        self,
        query: str,
        *args: object,
    ) -> object: ...

    async def close(self) -> None: ...  # pragma: no cover - Protocol method declaration only.


_AdvisoryConnect = Callable[[str], Awaitable[_AdvisoryConnection]]


@dataclass(frozen=True)
class MergeCoordinationKey:
    repo_url: str
    base_branch: str

    @classmethod
    def from_values(cls, *, repo_url: str, base_branch: str) -> MergeCoordinationKey:
        return cls(repo_url=repo_url.strip(), base_branch=base_branch.strip())


@dataclass
class _MergeLockEntry:
    lock: asyncio.Lock
    ref_count: int = 0


class InProcessMergeCoordinator:
    """An asyncio-lock based coordinator scoped to the current process."""

    def __init__(self) -> None:
        self._locks_by_loop: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop,
            dict[MergeCoordinationKey, _MergeLockEntry],
        ] = weakref.WeakKeyDictionary()

    @asynccontextmanager
    async def serialized_merge(
        self,
        *,
        repo_url: str,
        base_branch: str,
    ) -> AsyncIterator[None]:
        key = MergeCoordinationKey.from_values(repo_url=repo_url, base_branch=base_branch)
        entry = self._claim_entry(key)
        try:
            async with entry.lock:
                yield
        finally:
            self._release_entry(key, entry)

    def _claim_entry(self, key: MergeCoordinationKey) -> _MergeLockEntry:
        loop = asyncio.get_running_loop()
        loop_locks = self._locks_by_loop.get(loop)
        if loop_locks is None:
            loop_locks = {}
            self._locks_by_loop[loop] = loop_locks

        entry = loop_locks.get(key)
        if entry is None:
            entry = _MergeLockEntry(lock=asyncio.Lock())
            loop_locks[key] = entry
        entry.ref_count += 1
        return entry

    def _release_entry(self, key: MergeCoordinationKey, entry: _MergeLockEntry) -> None:
        loop = asyncio.get_running_loop()
        loop_locks = self._locks_by_loop.get(loop)
        if loop_locks is None:
            return

        entry.ref_count -= 1
        if entry.ref_count == 0 and not entry.lock.locked() and loop_locks.get(key) is entry:
            del loop_locks[key]


def postgres_advisory_lock_key(*, repo_url: str, base_branch: str) -> int:
    """Return a stable signed 64-bit Postgres advisory lock key."""

    key = MergeCoordinationKey.from_values(repo_url=repo_url, base_branch=base_branch)
    payload = f"{key.repo_url}\0{key.base_branch}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big", signed=True)


class PostgresAdvisoryMergeCoordinator:
    """A coordinator backed by Postgres session-level advisory locks.

    Session-level advisory locks must stay tied to an open Postgres session for
    the whole merge body. Use a dedicated asyncpg connection, not the
    SQLAlchemy application pool, and make contenders poll with try-lock calls so
    failed attempts return their connection immediately.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        poll_interval_seconds: float = 0.5,
        connect: _AdvisoryConnect | None = None,
    ) -> None:
        self._dsn = _asyncpg_dsn_from_engine(engine)
        self._poll_interval_seconds = poll_interval_seconds
        self._connect = connect or _connect_asyncpg
        self._local = InProcessMergeCoordinator()

    @asynccontextmanager
    async def serialized_merge(
        self,
        *,
        repo_url: str,
        base_branch: str,
    ) -> AsyncIterator[None]:
        lock_key = postgres_advisory_lock_key(repo_url=repo_url, base_branch=base_branch)
        async with self._local.serialized_merge(repo_url=repo_url, base_branch=base_branch):
            connection = await self._acquire_lock_connection(lock_key)
            try:
                yield
            finally:
                try:
                    await connection.execute("SELECT pg_advisory_unlock($1)", lock_key)
                finally:
                    await connection.close()

    async def _acquire_lock_connection(self, lock_key: int) -> _AdvisoryConnection:
        while True:
            connection = await self._connect(self._dsn)
            try:
                acquired = bool(
                    await connection.fetchval("SELECT pg_try_advisory_lock($1)", lock_key)
                )
            except BaseException:
                await connection.close()
                raise

            if acquired:
                return connection

            await connection.close()
            await asyncio.sleep(self._poll_interval_seconds)


async def _connect_asyncpg(dsn: str) -> _AdvisoryConnection:
    return cast(_AdvisoryConnection, await asyncpg.connect(dsn=dsn))


def _asyncpg_dsn_from_engine(engine: AsyncEngine) -> str:
    url = engine.url
    if "+" in url.drivername:
        url = url.set(drivername=url.drivername.split("+", 1)[0])
    return url.render_as_string(hide_password=False)


DEFAULT_MERGE_COORDINATOR = InProcessMergeCoordinator()
