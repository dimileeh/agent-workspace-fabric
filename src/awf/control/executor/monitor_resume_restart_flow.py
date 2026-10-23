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
    AGENT_SERVICE_NAME,
    AgentRecreateError,
    restart_runtime_for_monitor_resume,
)
from awf.control.executor.quality_gates import _log
from awf.node.companion_services import COMPANION_SPECS_UNRESOLVED, WorkspaceCompanionSpec
from awf.node.compose_manager import ComposeOperationError


async def _restart_compose_runtime_for_monitor_resume(
    self: Any,
    *,
    workspace_id: str,
    monitor_owner_id: str | None,
    compose_project: str,
    compose_file: Path,
    companion_specs: tuple[WorkspaceCompanionSpec, ...] | None,
    compose_up_timeout_seconds: int,
) -> bool:
    """Restart the compose runtime for monitor resume; ``False`` means stop the resume.

    ``True`` only when the final agent recreate ran and succeeded in this attempt,
    which is also the only path that resets the restart streak.
    ``companion_specs`` is ``None`` when the stored companion policy could not be
    resolved; the restart then fails closed, since only the specs say which
    companions carry env secrets that a selective restart must recreate.
    """
    # Dependencies a park left unsettled stay pending until their recreate succeeds.
    pending_dependency_recreates: tuple[str, ...] = ()
    try:
        if companion_specs is None:
            raise _monitor_handoff.CompanionEnvSecretPrecheckError(
                stderr="companion specs could not be resolved from the stored task policy",
                reason_code=COMPANION_SPECS_UNRESOLVED,
            )
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
        # A removed optional source is recreated only while compose.yml still
        # carries it (the refresh could not persist the removal), so
        # ``${SOURCE:-}`` clears the revoked value, or while a persisted rewrite
        # is still pending because an earlier recreate failed.
        pending_recreates = _monitor_handoff._pending_companion_recreates_for_resume(
            workspace_id=workspace_id, compose_file=compose_file
        )
        # Explicitly targeting a profiled service starts it even when its profile
        # is disabled, so only active services are force-recreated.
        secret_companions = {
            name
            for name in (
                *rewritten_services,
                *pending_recreates,
                *_monitor_handoff._companions_with_set_env_secrets_for_resume(
                    companion_specs=companion_specs,
                    environ=os.environ,
                ),
                *_monitor_handoff._companions_with_stale_optional_env_secret_targets_for_resume(
                    compose_file=compose_file,
                    companion_specs=companion_specs,
                    environ=os.environ,
                ),
            )
            if requirements is None or name in requirements.service_names
        }
        unsettled_services = await dependencies_to_recreate_after_exhaustion(
            self, workspace_id=workspace_id
        )
        recreate_services = tuple(sorted(secret_companions | set(unsettled_services)))
        pending_dependency_recreates = unsettled_services

        recreated_services: set[str] = set()

        def _on_recreated(service: str) -> None:
            nonlocal pending_dependency_recreates
            recreated_services.add(service)
            pending_dependency_recreates = tuple(
                name for name in pending_dependency_recreates if name != service
            )
            _monitor_handoff._clear_pending_companion_recreates_for_resume(
                workspace_id=workspace_id,
                compose_file=compose_file,
                keep=tuple(name for name in pending_recreates if name not in recreated_services),
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
            # The agent stays stopped until the final recreate, so only dependencies count.
            runtime_usable=lambda: _monitor_handoff._compose_runtime_usable_after_restart_failure(
                compose_project, compose_file, ignored_services=frozenset({AGENT_SERVICE_NAME})
            ),
            recreate_services=recreate_services,
            # ``--no-deps`` starts skip depends_on, so dependents start after their prerequisites.
            dependency_conditions=None
            if requirements is None
            else requirements.dependency_conditions,
            # Each companion is retired as soon as its own recreate succeeds, so a
            # later failure never makes the next resume recreate it again
            # mid-recovery (#936); a companion not yet recreated stays pending.
            on_recreated=_on_recreated,
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
            before_restart=True,
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
            pending_dependency_recreates=pending_dependency_recreates,
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
