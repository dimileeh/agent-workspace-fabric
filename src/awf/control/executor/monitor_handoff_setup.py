"""Profile setup helpers for PR monitor handoff workspaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from awf.common.audit import redact_audit_text
from awf.common.compose_exec import ComposeExecCleanupError, cleanup_failure_message
from awf.common.logging import get_logger
from awf.control.executor.constants import PR_MONITOR_SETUP_FAILED_REASON_CODE
from awf.control.executor.helpers import _failure_reason_for_phase
from awf.control.executor.logging_ops import (
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    _setup_dependency_network_failure_details,
)
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.runtime.ownership import (
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)

_log = get_logger(__name__)


async def _run_monitor_handoff_profile_setup(
    self: Any,
    *,
    workspace_id: str,
    profile: Any,
    compose_project: str,
    compose_file: Path,
    worktree_path: Path,
) -> bool:
    """Run profile setup before handing an adopted/release PR to the monitor."""
    try:
        if not await repair_agent_runtime_ownership(
            logger=_log,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="profile_setup",
            event_name=EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
            reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
        ):
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message="agent runtime ownership repair failed before profile setup",
                reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
            )
            return False
        setup_result = await self._validation.run_profile_phases(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            profile=profile,
            phase_names=("setup", "pre_agent"),
            worktree_path=worktree_path,
        )
    except ComposeExecCleanupError as exc:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=cleanup_failure_message(exc),
            reason_code=exc.reason_code,
        )
        return False
    except Exception as exc:
        _log.exception("executor.monitor_handoff_setup_failed", workspace_id=workspace_id)
        safe_error = redact_audit_text(repr(exc))
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"monitor handoff profile setup failed: {safe_error}"[:2000],
            reason_code=PR_MONITOR_SETUP_FAILED_REASON_CODE,
        )
        return False

    try:
        await self._record_setup_dependency_network_events(
            workspace_id=workspace_id,
            result=setup_result,
        )
    except Exception:
        _log.exception(
            "executor.monitor_handoff_setup_dependency_network_event_record_failed",
            workspace_id=workspace_id,
            setup_all_passed=setup_result.all_passed,
        )

    if setup_result.all_passed:
        return True

    first_fail = setup_result.first_failure
    setup_dependency_details = _setup_dependency_network_failure_details(first_fail)
    setup_failure_reason_code = (
        SETUP_DEPENDENCY_NETWORK_FAILURE
        if setup_dependency_details is not None
        else PR_MONITOR_SETUP_FAILED_REASON_CODE
    )
    await self._mark_failed(
        workspace_id=workspace_id,
        from_status=WorkspaceStatus.running,
        failure_reason=_failure_reason_for_phase(first_fail),
        message=(
            f"profile setup failed: {redact_audit_text(first_fail.command)}"
            if first_fail is not None
            else "profile setup failed"
        )[:2000],
        reason_code=setup_failure_reason_code,
        details=setup_dependency_details,
    )
    return False
