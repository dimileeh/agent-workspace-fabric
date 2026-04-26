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
import weakref
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


DEFAULT_MERGE_COORDINATOR = InProcessMergeCoordinator()
