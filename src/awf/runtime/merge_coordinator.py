"""Merge-attempt coordination for PR monitors.

The current implementation is intentionally process-local: it serializes
auto-merge attempts by ``repo_url + base_branch`` for monitors running in
the same worker process. That is enough to prevent the local service
worker from firing simultaneous final rechecks and merge attempts at the
same repository branch.

Next hardening step: replace this abstraction with a Postgres advisory
lock so independently spawned monitor processes and multiple workers can
share the same coordination key.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol


class MergeCoordinator(Protocol):
    """Coordinates final auto-merge critical sections."""

    def serialized_merge(
        self,
        *,
        repo_url: str,
        base_branch: str,
    ) -> AbstractAsyncContextManager[None]:
        """Return an async context manager for one repo/base merge lane."""


@dataclass(frozen=True)
class MergeCoordinationKey:
    repo_url: str
    base_branch: str

    @classmethod
    def from_values(cls, *, repo_url: str, base_branch: str) -> MergeCoordinationKey:
        return cls(repo_url=repo_url.strip(), base_branch=base_branch.strip())


class InProcessMergeCoordinator:
    """An asyncio-lock based coordinator scoped to the current process."""

    def __init__(self) -> None:
        self._locks: dict[MergeCoordinationKey, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    @asynccontextmanager
    async def serialized_merge(
        self,
        *,
        repo_url: str,
        base_branch: str,
    ) -> AsyncIterator[None]:
        lock = await self._lock_for(
            MergeCoordinationKey.from_values(repo_url=repo_url, base_branch=base_branch)
        )
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()

    async def _lock_for(self, key: MergeCoordinationKey) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock


DEFAULT_MERGE_COORDINATOR = InProcessMergeCoordinator()
