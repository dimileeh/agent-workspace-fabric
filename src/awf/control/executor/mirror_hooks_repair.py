"""Mirror hooks-path repair helpers for executor startup and cleanup."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from awf.control.executor.quality_gates import _log
from awf.db.enums import FailureReason, OperationStatus, WorkspaceStatus
from awf.node.git_manager import GitOperationError


class MirrorHooksPathRepairAbortedError(RuntimeError):
    """Raised after mirror hooks repair already marked the workspace failed."""


async def repair_mirror_hooks_path_or_mark_failed(
    *,
    executor: Any,
    workspace_id: str,
    mirror_path: Path | None,
    repair_mirror_hooks_path_fn: Callable[[Path], Awaitable[bool]],
    recovery_active: bool,
    failure_stage: str,
    failure_from_status: WorkspaceStatus = WorkspaceStatus.running,
    before_mark_failed: Callable[[], None] | None = None,
) -> bool:
    if mirror_path is None:
        return True
    mirror_repair_failure_reason_code: str | None = None
    try:
        await repair_mirror_hooks_path_fn(mirror_path)
    except GitOperationError as exc:
        mirror_repair_failure_reason_code = exc.reason_code
        _log.warning(
            "executor.mirror_hooks_path_repair_failed",
            workspace_id=workspace_id,
            reason_code=exc.reason_code,
            stderr=exc.stderr[:400],
        )
    except OSError as exc:
        mirror_repair_failure_reason_code = "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        _log.warning(
            "executor.mirror_hooks_path_repair_failed",
            workspace_id=workspace_id,
            reason_code=mirror_repair_failure_reason_code,
            error=repr(exc)[:400],
        )
    if mirror_repair_failure_reason_code is not None:
        failure_message = f"could not repair poisoned mirror hooks path {failure_stage}"
        if before_mark_failed is not None:
            before_mark_failed()
        if recovery_active:
            await executor._finish_active_recovery_operations(
                workspace_id=workspace_id,
                status=OperationStatus.failed,
                reason_code=mirror_repair_failure_reason_code,
                error_message=failure_message,
            )
        await executor._mark_failed(
            workspace_id=workspace_id,
            from_status=failure_from_status,
            failure_reason=FailureReason.infrastructure_failure,
            message=failure_message,
            reason_code=mirror_repair_failure_reason_code,
        )
        return False
    return True


async def repair_mirror_hooks_path_after_agent_cleanup_failure(
    *,
    executor: Any,
    workspace_id: str,
    mirror_path: Path | None,
    repair_mirror_hooks_path_fn: Callable[[Path], Awaitable[bool]],
    recovery_active: bool,
    failure_stage: str = "after agent cleanup failure",
    failure_from_status: WorkspaceStatus = WorkspaceStatus.running,
    before_mark_failed: Callable[[], None] | None = None,
) -> bool:
    return await repair_mirror_hooks_path_or_mark_failed(
        executor=executor,
        workspace_id=workspace_id,
        mirror_path=mirror_path,
        repair_mirror_hooks_path_fn=repair_mirror_hooks_path_fn,
        recovery_active=recovery_active,
        failure_stage=failure_stage,
        failure_from_status=failure_from_status,
        before_mark_failed=before_mark_failed,
    )
