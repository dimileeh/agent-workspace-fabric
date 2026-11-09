"""Injected per-invocation work-slot context (slot-release).

A work slot means provisioning, an agent run or a validation run is in flight
right now. ``AgentAdapter.run()`` and ``ValidationRunner`` hold one through an
async context manager that their *callers inject* (the worker builds one per
dispatched task); there is no module state. This module only declares the
dependency-free contract so ``adapters/`` and ``runtime/`` never import
``control/``.

Nested acquisition within the same invocation is a no-op, so the lease taken at
the requested claim covers the first agent and validation runs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Final, Protocol, TypedDict

WORK_SLOT_INVOCATION_ENV: Final = "AWF_WORK_SLOT_INVOCATION_ID"
"""Env marker set on tagged ``awf-exec`` processes; the restart probe scans for it."""

MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE: Final = "MONITOR_WAITING_FOR_WORK_SLOT"
"""Cataloged reason for an invocation queued for a work slot (monitor action,
blocked/recovering/paused resume). Shared by the worker, status and metrics."""


class WorkSlotHandle(Protocol):
    """The held slot as seen by the run it gates."""

    @property
    def invocation_id(self) -> str: ...  # pragma: no cover - Protocol declaration only.


class WorkSlot(Protocol):
    """Per-invocation slot scope injected by the caller of an agent/validation run."""

    def hold(
        self, phase: str
    ) -> AbstractAsyncContextManager[WorkSlotHandle]: ...  # pragma: no cover

    async def release_idle(self) -> None: ...  # pragma: no cover - Protocol declaration only.


class WorkSlotKwargs(TypedDict, total=False):
    """Keyword arguments carrying an injected work slot (absent when unbound)."""

    work_slot: WorkSlot


def work_slot_kwargs(work_slot: WorkSlot | None) -> WorkSlotKwargs:
    """Return ``{"work_slot": ...}`` only when a slot is bound.

    Call sites splat this into ``adapter.run(...)``/validation calls so existing
    fakes that predate the keyword keep working when no slot is bound.
    """
    return WorkSlotKwargs() if work_slot is None else WorkSlotKwargs(work_slot=work_slot)


@asynccontextmanager
async def hold_work_slot(work_slot: WorkSlot | None, phase: str) -> AsyncIterator[str | None]:
    """Hold ``work_slot`` (if any) and yield the invocation id to tag ``awf-exec`` with."""
    if work_slot is None:
        yield None
        return
    async with work_slot.hold(phase) as handle:
        yield handle.invocation_id
