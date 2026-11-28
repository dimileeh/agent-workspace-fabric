"""Recovery validation handoff helpers for WorkspaceExecutor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from awf.adapters.base import AgentAdapter, AgentDefaults
from awf.common.audit import redact_audit_text
from awf.common.commands import AsyncCommandRunner
from awf.common.forge import ForgeNotSupportedError, concrete_forge_for_repo, make_forge_client
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import parse_forge_pull_request_url
from awf.common.work_slot import work_slot_kwargs
from awf.control.executor.constants import (
    _AUDIT_PR_CONFORMANCE_COMMENT_EVENT,
    _PR_CONFORMANCE_COMMENT_FAILED_REASON_CODE,
)
from awf.control.executor.helpers import (
    _call_pr_monitor_factory,
    _existing_pr_remote_push_url,
    _monitor_execution_claim_kwargs,
    _provider_recovery_default_model_for_monitor_handoff,
    _render_pr_conformance_gaps,
)
from awf.control.executor.pr_open_step import (
    _pr_snapshot_contains_pushed_tip,
    retry_pr_conformance_comment,
)
from awf.control.executor.protocols import _MonitorRunnerProto
from awf.control.executor.quality_gates import _log
from awf.control.executor.recovery_payloads import (
    _planning_carried_gaps_from_metadata,
    _pr_conformance_publication_pending,
    _recovery_needs_existing_pr_push,
    _validate_only_recovery_target_head_sha,
)
from awf.control.executor.types import _RebaseRecoveryResult
from awf.control.executor.work_slot_binding import work_slot_for
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.hosted_node.identity import MaterialDeniedError
from awf.profiles.models import WorkspaceProfile


async def ensure_carried_gaps_published(
    runner: AsyncCommandRunner,
    *,
    pr_url: str,
    pr_number: int,
    resolved_forge: object,
    carried_gaps: Sequence[dict[str, str]],
    validated_head_sha: str | None = None,
    worktree_path: Path | None = None,
    fetch_remote: str = "origin",
) -> str | None:
    """Confirm the notice or publish it on the PR before attaching its monitor."""
    operator_wait = validated_head_sha is not None
    gap_section = _render_pr_conformance_gaps(carried_gaps, operator_wait=operator_wait)
    try:
        forge = concrete_forge_for_repo(resolved_forge, pr_url)
        repo, number_from_url = parse_forge_pull_request_url(pr_url, forge=forge)
        if number_from_url != pr_number:
            raise ValueError("Stored pull request number does not match its URL")
        async with make_forge_client(forge, runner) as client:
            if operator_wait:
                snapshot = await client.fetch_pull_request_snapshot(repo=repo, pr_number=pr_number)
                if not await _pr_snapshot_contains_pushed_tip(
                    snapshot=snapshot,
                    pushed_head_sha=validated_head_sha,
                    runner=runner,
                    worktree_path=worktree_path,
                    fetch_remote=fetch_remote,
                ):
                    return "The pull request does not contain the validated tip"
            else:
                body = await client.fetch_pull_request_body(repo=repo, pr_number=pr_number)
                if f"\n{gap_section}\n" in f"\n{body}\n".replace("\r\n", "\n"):
                    return None
            comments = await client.fetch_pull_request_comment_bodies(
                repo=repo, pr_number=pr_number
            )
            if not any(
                comment.replace("\r\n", "\n").strip() == gap_section for comment in comments
            ):
                await client.post_comment(repo=repo, pr_number=pr_number, body=gap_section)
    except MaterialDeniedError:
        raise
    except (ForgeClientError, ForgeNotSupportedError, OSError, TimeoutError, ValueError) as exc:
        return redact_audit_text(str(exc), limit=500)
    return None


async def confirm_pending_gap_notice(self: Any, ws: Workspace) -> bool:
    """Finish a recorded publication failure before starting the resumed monitor."""
    if not _pr_conformance_publication_pending(ws):
        return True

    # The resume metadata check has already verified the PR identity is present.
    assert ws.pr_url and ws.pr_number is not None
    metadata = ws.block_planning_conformance_handoff or {}
    carried_gaps = _planning_carried_gaps_from_metadata(metadata)
    park_head = metadata.get("conformance_park_head_sha")
    park_verification = (
        {
            "validated_head_sha": park_head,
            "worktree_path": self._config.worktrees_root / ws.id,
            "fetch_remote": _existing_pr_remote_push_url(ws) or "origin",
        }
        if isinstance(park_head, str) and park_head
        else {}
    )
    error = (
        await ensure_carried_gaps_published(
            self._runner,
            pr_url=ws.pr_url,
            pr_number=ws.pr_number,
            resolved_forge=(ws.resolved_profile or {}).get("forge"),
            carried_gaps=carried_gaps,
            **park_verification,
        )
        if carried_gaps
        else "The stored gap details are missing"
    )
    if not await self._recheck_status(
        ws.id,
        expected=WorkspaceStatus.monitoring_pr,
        action="resume_gap_notice",
        monitor_owner_id=ws.monitor_claimed_by,
    ):
        return False
    if error is not None:
        await self._mark_failed(
            workspace_id=ws.id,
            from_status=WorkspaceStatus.monitoring_pr,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"Could not publish unresolved plan-conformance gaps on the pull request: {error}",
            reason_code=_PR_CONFORMANCE_COMMENT_FAILED_REASON_CODE,
            monitor_owner_id=ws.monitor_claimed_by,
        )
        return False

    await self._record_executor_pr_audit_event(
        ws.id,
        event_type=_AUDIT_PR_CONFORMANCE_COMMENT_EVENT,
        action="pr_conformance_comment",
        outcome="succeeded",
        reason_code="PR_CONFORMANCE_COMMENT_PUBLISHED",
        pr_url=ws.pr_url,
        pr_number=ws.pr_number,
        source_head_sha=ws.monitor_last_commit_sha,
    )
    return True


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
    adapter: AgentAdapter | None,
    profile: WorkspaceProfile,
    defaults: AgentDefaults | None,
    compose_project: str,
    compose_file: Path,
    pending_gaps: Sequence[dict[str, str]] = (),
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
        except SQLAlchemyError:
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
        except SQLAlchemyError:
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
    if pending_gaps:
        publication_error = await retry_pr_conformance_comment(
            self,
            ws=ws,
            profile=profile,
            worktree_path=self._config.worktrees_root / workspace_id,
            head_sha=successful_validation_workspace_head_sha,
            carried_gaps=pending_gaps,
        )
        if publication_error is not None:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.validating,
                failure_reason=FailureReason.infrastructure_failure,
                message=(
                    "Could not publish unresolved plan-conformance gaps on the pull request: "
                    f"{publication_error}"
                ),
                reason_code=_PR_CONFORMANCE_COMMENT_FAILED_REASON_CODE,
            )
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
        if pending_gaps:
            await self._add_executor_pr_audit_event(
                repo,
                persisted,
                event_type=_AUDIT_PR_CONFORMANCE_COMMENT_EVENT,
                action="pr_conformance_comment",
                outcome="succeeded",
                reason_code="PR_CONFORMANCE_COMMENT_PUBLISHED",
                source_head_sha=successful_validation_workspace_head_sha,
            )
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
                **_monitor_execution_claim_kwargs(ws),
                **work_slot_kwargs(work_slot_for(self, workspace_id)),
            )
    return True
