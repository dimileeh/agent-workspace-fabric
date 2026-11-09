"""Operator-facing doctor reason text for work-slot waits (slot-release).

Kept beside ``reasons`` so that module stays within the first-party file-line
guardrail; the entries are merged into ``_REASON_TEXT`` at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from awf.common.work_slot import MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE
from awf.service.doctor.reasons_helpers import reason_catalog_link

if TYPE_CHECKING:
    from awf.service.doctor.reasons import _ReasonText


def get_work_slot_reasons(reason_text_cls: type[_ReasonText]) -> dict[str, _ReasonText]:
    """Return the work-slot catalog reason text entries."""
    return {
        MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE: reason_text_cls(
            (
                "A workspace is queued for a work slot before running an agent or "
                "validation (PR monitor action, blocked/recovering/paused resume)."
            ),
            (
                "No action is needed for a short wait: queued work is granted the next "
                "free slot in strict FIFO order, ahead of new requests. For a sustained "
                "wait, inspect `checks.work_slots` in `awf service status` for the slot "
                "holders, then raise `AWF_WORKER_MAX_CONCURRENT_EXECUTIONS` if the host "
                "has room, or cancel stuck work. An `unsatisfiable` waiter needs a larger "
                "`AWF_LOCAL_CAPACITY_*` limit or a smaller workspace resource reservation."
            ),
            (
                "Every work slot (`AWF_WORKER_MAX_CONCURRENT_EXECUTIONS`) is held by "
                "provisioning, an agent run or a validation run; or a held lease whose "
                "`awf-exec` cleanup was never proven is waiting for the reconciler; or "
                "the configured local capacity cannot fit the workspace's peak "
                "reservation."
            ),
            "awf service status --format json",
            reason_catalog_link(MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE),
        ),
    }
