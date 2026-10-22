"""Monitor-resume compose runtime restart flow for ``WorkspaceExecutor``.

Mechanically extracted from ``awf.control.executor.monitor_handoff`` to keep
each module under the first-party line limit; behavior is unchanged. The
restart runs under the worktree writer lock held by
``resume_pr_monitor_handoff`` and is wired onto ``WorkspaceExecutor`` through
``mixins.py``. Helpers still defined in ``monitor_handoff`` are resolved there
at call time so existing monkeypatch seams keep working.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from awf.common.audit import redact_audit_text
from awf.control.executor import monitor_handoff as _monitor_handoff
from awf.control.executor.monitor_resume_restart_escalation import (
    dependencies_to_recreate_after_exhaustion,
    escalate_monitor_resume_restart_failure,
    reset_monitor_resume_restart_streak,
)
from awf.control.executor.monitor_resume_runtime import (
    AgentRecreateError,
    restart_runtime_for_monitor_resume,
)
from awf.control.executor.quality_gates import _log
from awf.node.companion_services import WorkspaceCompanionSpec
from awf.node.compose_manager import ComposeOperationError


async def _restart_compose_runtime_for_monitor_resume(
    self: Any,
    *,
    workspace_id: str,
    monitor_owner_id: str | None,
    compose_project: str,
    compose_file: Path,
    companion_specs: tuple[WorkspaceCompanionSpec, ...],
    compose_up_timeout_seconds: int,
) -> bool:
    """Restart the compose runtime for monitor resume; ``False`` means stop the resume.

    ``True`` only when the final agent recreate ran and succeeded in this attempt,
    which is also the only path that resets the restart streak.
    """
    try:
        _monitor_handoff._precheck_required_companion_env_secrets_for_resume(
            companion_specs=companion_specs,
            environ=os.environ,
        )
        rewritten_services = _monitor_handoff._refresh_optional_companion_env_secrets_for_resume(
            workspace_id=workspace_id,
            compose_file=compose_file,
            companion_specs=companion_specs,
            environ=os.environ,
        )
        requirements = _monitor_handoff._compose_runtime_requirements_from_file(compose_file)
        # A rotated secret leaves the ``${SOURCE}`` placeholder unchanged, so
        # every companion with a set env secret is recreated to pick it up.
        secret_companions = {
            name
            for name in _monitor_handoff._companions_with_set_env_secrets_for_resume(
                companion_specs=companion_specs,
                environ=os.environ,
            )
            if requirements is None or name in requirements.service_names
        }
        unsettled_services = await dependencies_to_recreate_after_exhaustion(
            self, workspace_id=workspace_id
        )
        # Recreate the agent and what must change; recovering dependencies are left alone (#936).
        tolerated_error = await restart_runtime_for_monitor_resume(
            self._compose,
            compose_project=compose_project,
            compose_file=compose_file,
            workspace_id=workspace_id,
            budget_seconds=compose_up_timeout_seconds,
            dependency_services=(
                None if requirements is None else tuple(sorted(requirements.service_names))
            ),
            runtime_usable=lambda: _monitor_handoff._compose_runtime_usable_after_restart_failure(
                compose_project, compose_file
            ),
            recreate_services=tuple(
                sorted(set(rewritten_services) | secret_companions | set(unsettled_services))
            ),
        )
        if tolerated_error is not None:
            await self._record_monitor_runtime_restart_failed(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file_path=str(compose_file),
                error=tolerated_error,
            )
    except _monitor_handoff.CompanionEnvSecretPrecheckError as exc:
        _log.error(
            "executor.resume_companion_env_secret_precheck_failed",
            workspace_id=workspace_id,
            reason_code=exc.reason_code,
            stderr=redact_audit_text(exc.stderr, limit=1000),
        )
        await self._record_monitor_runtime_restart_failed(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file_path=str(compose_file),
            error=exc,
            event_reason_code="MONITOR_RECOVERY_PRECHECK_FAILED",
        )
        await escalate_monitor_resume_restart_failure(
            self,
            workspace_id=workspace_id,
            monitor_owner_id=monitor_owner_id,
            compose_project=compose_project,
            error=exc,
        )
        return False
    except ComposeOperationError as exc:
        # Whatever the old stack looks like, compose never confirmed a fresh agent
        # with refreshed auth/env and a new identity to replay setup for.
        _log.error(
            "executor.resume_agent_recreate_failed"
            if isinstance(exc, AgentRecreateError)
            else "executor.resume_compose_up_failed",
            workspace_id=workspace_id,
            reason_code=exc.reason_code,
            stderr=redact_audit_text(exc.stderr, limit=1000),
        )
        await self._record_monitor_runtime_restart_failed(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file_path=str(compose_file),
            error=exc,
        )
        await escalate_monitor_resume_restart_failure(
            self,
            workspace_id=workspace_id,
            monitor_owner_id=monitor_owner_id,
            compose_project=compose_project,
            error=exc,
        )
        return False
    await reset_monitor_resume_restart_streak(self, workspace_id=workspace_id)
    return True
