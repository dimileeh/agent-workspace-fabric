"""Recovery validation handoff helpers for WorkspaceExecutor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from awf.adapters.base import AgentAdapter, AgentDefaults
from awf.control.executor.helpers import (
    _call_pr_monitor_factory,
    _provider_recovery_default_model_for_monitor_handoff,
)
from awf.control.executor.protocols import _MonitorRunnerProto
from awf.control.executor.quality_gates import _log
from awf.control.executor.recovery_payloads import (
    _recovery_needs_existing_pr_push,
    _validate_only_recovery_target_head_sha,
)
from awf.control.executor.types import _RebaseRecoveryResult
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.profiles.models import WorkspaceProfile


async def handle_recovery_pr_handoff_after_validation(
    self: Any,
    *,
    workspace_id: str,
    ws: Workspace,
    recovery: Mapping[str, Any] | None,
    rebase_recovery_result: _RebaseRecoveryResult | None,
    successful_validation_run_id: str | None,
    successful_validation_workspace_head_sha: str | None,
    repair_mirror_hooks_path_or_mark_failed: Callable[..., Awaitable[Any]],
    adapter: AgentAdapter,
    profile: WorkspaceProfile,
    defaults: AgentDefaults | None,
    compose_project: str,
    compose_file: Path,
) -> bool:
    """Return True when recovery handoff handled execution flow."""
    if recovery is None or not ws.pr_url:
        return False

    recovery_requires_pr_update = _recovery_needs_existing_pr_push(
        recovery,
        validated_workspace_head_sha=successful_validation_workspace_head_sha,
        rebase_recovery_result=rebase_recovery_result,
    )
    if rebase_recovery_result is not None and successful_validation_run_id is not None:
        try:
            await self._set_validation_run_target_head_sha(
                validation_run_id=successful_validation_run_id,
                target_head_sha=rebase_recovery_result.head_sha,
            )
            await self._clear_rebase_recovery_staleness(
                workspace_id=workspace_id,
            )
        except Exception:
            _log.exception(
                "executor.rebase_recovery_staleness_clear_failed",
                workspace_id=workspace_id,
                validation_run_id=successful_validation_run_id,
            )
    validate_only_target_head_sha = _validate_only_recovery_target_head_sha(
        recovery,
        validated_workspace_head_sha=successful_validation_workspace_head_sha,
    )
    if (
        rebase_recovery_result is None
        and successful_validation_run_id is not None
        and validate_only_target_head_sha is not None
    ):
        try:
            await self._set_validation_run_target_head_sha(
                validation_run_id=successful_validation_run_id,
                target_head_sha=validate_only_target_head_sha,
                workspace_head_sha=successful_validation_workspace_head_sha,
            )
        except Exception:
            _log.exception(
                "executor.validate_only_recovery_target_head_sha_update_failed",
                workspace_id=workspace_id,
                validation_run_id=successful_validation_run_id,
                target_head_sha=validate_only_target_head_sha,
            )
    if recovery_requires_pr_update:
        _log.info(
            "executor.recovery_existing_pr_update_required",
            workspace_id=workspace_id,
            pr_url=ws.pr_url,
            source_head_sha=recovery.get("source_head_sha"),
            validated_workspace_head_sha=successful_validation_workspace_head_sha,
        )
        return False

    if not await repair_mirror_hooks_path_or_mark_failed(
        failure_stage="before recovery skip-push handoff",
        failure_from_status=WorkspaceStatus.validating,
    ):
        return True
    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.validating,
        action="recovery_skip_push",
    ):
        return True
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        persisted = await repo.get(workspace_id)
        if persisted is None:  # pragma: no cover - destroyed mid-flight
            return True
        if persisted.status != WorkspaceStatus.validating.value:
            await self._record_stale_action_skip(
                repo,
                persisted,
                action="recovery_skip_push",
                expected=WorkspaceStatus.validating,
                reason_code="EXECUTOR_STALE_STATUS",
            )
            await session.commit()
            return True
        has_monitor = self._pr_monitor is not None or self._pr_monitor_factory is not None
        await repo.transition(
            persisted,
            to=WorkspaceStatus.monitoring_pr if has_monitor else WorkspaceStatus.completed,
            reason_code="RECOVERY_VALIDATION_OK",
        )
        await session.commit()
    _log.info(
        "executor.recovery_skip_push",
        workspace_id=workspace_id,
        pr_url=ws.pr_url,
        has_monitor=has_monitor,
    )
    if has_monitor:
        monitor: _MonitorRunnerProto | None = self._pr_monitor
        if monitor is None and self._pr_monitor_factory is not None:
            monitor = _call_pr_monitor_factory(
                self._pr_monitor_factory,
                adapter=adapter,
                profile=profile,
                workspace=persisted,
                provider_recovery_default_model=(
                    _provider_recovery_default_model_for_monitor_handoff(
                        adapter=adapter,
                        defaults=defaults,
                    )
                ),
            )
        if monitor is not None:
            _log.info(
                "executor.recovery_handoff_to_pr_monitor",
                workspace_id=workspace_id,
                pr_url=ws.pr_url,
            )
            if not await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.monitoring_pr,
                action="run_pr_monitor",
            ):
                return True
            await monitor.run(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
            )
    return True
