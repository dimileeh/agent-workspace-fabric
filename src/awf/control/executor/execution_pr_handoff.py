"""WorkspaceExecutor PR persistence and monitor handoff."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from awf.control.executor.constants import (
    _AUDIT_GIT_PUSH_EVENT,
    _AUDIT_PR_CREATED_EVENT,
)
from awf.control.executor.helpers import (
    _call_pr_monitor_factory,
    _extract_pr_number,
    _provider_recovery_default_model_for_monitor_handoff,
)
from awf.control.executor.quality_gates import _log
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository


async def persist_pr_and_handoff(
    self: Any,
    *,
    workspace_id: str,
    pr: Any,
    adapter: Any,
    profile: Any,
    defaults: Any,
    successful_validation_run_id: str | None,
    compose_project: str,
    compose_file: Path,
) -> None:
    """Persist PR metadata and hand off to the monitor when configured."""
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        persisted = await repo.get(workspace_id)
        if persisted is None:  # pragma: no cover - destroyed mid-flight
            return
        if persisted.status != WorkspaceStatus.pushing.value:
            await self._record_stale_action_skip(
                repo,
                persisted,
                action="persist_pr",
                expected=WorkspaceStatus.pushing,
                reason_code="EXECUTOR_STALE_STATUS",
            )
            await session.commit()
            return
        had_existing_pr_url = bool(persisted.pr_url)
        persisted.pr_url = pr.url
        persisted.pr_number = _extract_pr_number(pr.url)
        if pr.head_sha:
            persisted.monitor_last_commit_sha = pr.head_sha
        if persisted.task_kind == "feature_branch_pr" and not persisted.remote_push_branch:
            persisted.remote_push_branch = (
                pr.branch or persisted.branch_name or f"awf/{workspace_id}"
            )
        pr_reason_code = "PR_UPDATED" if had_existing_pr_url else "PR_OPENED"
        await self._add_executor_pr_audit_event(
            repo,
            persisted,
            event_type=_AUDIT_GIT_PUSH_EVENT,
            action="git_push",
            outcome="succeeded",
            reason_code=pr_reason_code,
            branch_name=persisted.branch_name or pr.branch,
            remote_branch=persisted.remote_push_branch or pr.branch,
            pr_number=persisted.pr_number,
            pr_url=persisted.pr_url,
            source_head_sha=pr.head_sha,
        )
        await self._add_executor_pr_audit_event(
            repo,
            persisted,
            event_type=_AUDIT_PR_CREATED_EVENT,
            action="pr_create",
            outcome="reused" if had_existing_pr_url else "succeeded",
            reason_code=pr_reason_code,
            branch_name=persisted.branch_name or pr.branch,
            remote_branch=persisted.remote_push_branch or pr.branch,
            pr_number=persisted.pr_number,
            pr_url=persisted.pr_url,
            source_head_sha=pr.head_sha,
            evidence=pr.open_metadata,
        )
        monitor = self._pr_monitor
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
            await repo.transition(
                persisted,
                to=WorkspaceStatus.monitoring_pr,
                reason_code=pr_reason_code,
            )
            await session.commit()
        else:
            await repo.transition(
                persisted,
                to=WorkspaceStatus.completed,
                reason_code=pr_reason_code,
            )
            await session.commit()

    if successful_validation_run_id is not None and pr.head_sha:
        try:
            await self._set_validation_run_target_head_sha(
                validation_run_id=successful_validation_run_id,
                target_head_sha=pr.head_sha,
            )
        except Exception:
            _log.exception(
                "executor.validation_run_target_head_sha_update_failed",
                workspace_id=workspace_id,
                validation_run_id=successful_validation_run_id,
                target_head_sha=pr.head_sha,
            )

    if monitor is not None:
        _log.info(
            "executor.handoff_to_pr_monitor",
            workspace_id=workspace_id,
            pr_url=pr.url,
        )
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.monitoring_pr,
            action="run_pr_monitor",
        ):
            return
        await monitor.run(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
        )
        return
    _log.info(
        "executor.completed",
        workspace_id=workspace_id,
        pr_url=pr.url,
    )
