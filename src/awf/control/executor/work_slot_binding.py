"""Per-workspace work-slot binding for the executor (slot-release).

The worker dispatches one task per workspace and enters
``executor.work_slot_binding(workspace_id, scope)`` around the executor call.
Executor call sites that run an agent, a validation, or the PR monitor read the
bound scope back with :func:`work_slot_for` and inject it through
``work_slot_kwargs`` so nested acquisition reuses the task's lease. Outside a
binding nothing is passed, so executor fakes and direct callers are unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from awf.common.work_slot import WorkSlot


@contextmanager
def work_slot_binding(
    executor: Any,
    workspace_id: str,
    work_slot: WorkSlot | None,
) -> Iterator[None]:
    """Bind ``work_slot`` to ``workspace_id`` for the duration of the block."""
    slots: dict[str, WorkSlot] = executor._work_slots
    previous = slots.get(workspace_id)
    if work_slot is not None:
        slots[workspace_id] = work_slot
    try:
        yield
    finally:
        if previous is None:
            slots.pop(workspace_id, None)
        else:
            slots[workspace_id] = previous


def work_slot_for(executor: Any, workspace_id: str | None) -> WorkSlot | None:
    """Return the work slot bound to ``workspace_id`` (``None`` when unbound)."""
    slots = getattr(executor, "_work_slots", None)
    if workspace_id is None or not isinstance(slots, dict):
        return None
    return slots.get(workspace_id)
