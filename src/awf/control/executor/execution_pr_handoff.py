"""WorkspaceExecutor PR persistence and monitor handoff."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from awf.adapters.base import AgentAdapter, AgentDefaults
from awf.common.work_slot import work_slot_kwargs
from awf.control.executor import pr_open_step
from awf.control.executor.constants import (
    _AUDIT_GIT_PUSH_EVENT,
    _AUDIT_PR_CREATED_EVENT,
    _PR_CONFORMANCE_COMMENT_FAILED_REASON_CODE,
)
from awf.control.executor.helpers import (
    _call_pr_monitor_factory,
    _extract_pr_number,
    _monitor_execution_claim_kwargs,
    _provider_recovery_default_model_for_monitor_handoff,
)
from awf.control.executor.quality_gates import _log
from awf.control.executor.types import _PlanningConformancePark
from awf.control.executor.work_slot_binding import work_slot_for
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import ValidationRunRepository, WorkspaceRepository
from awf.profiles.models import WorkspaceProfile
from awf.runtime.monitor_state_keys import _with_settled_disposition_baseline
from awf.runtime.operator_hints import persist_operator_hint
from awf.runtime.planning import PLAN_CONFORMANCE_UNSATISFIED
from awf.runtime.pr_monitor import MonitorState, OperatorHint
from awf.runtime.pr_monitor_runner.applied_operator_directives import (
    append_plan_conformance_advisory,
)


async def push_and_handoff_pr(
    self: Any,
    *,
    workspace_id: str,
    ws: Workspace,
    worktree_path: Path,
    adapter: AgentAdapter | None,
    profile: WorkspaceProfile,
    defaults: AgentDefaults | None,
    successful_validation_run_id: str | None,
    compose_project: str,
    compose_file: Path,
    resume_from_blocked: bool,
    in_recovery: bool,
    carried_gaps: Sequence[dict[str, str]],
    retry_gap_publication: bool,
    conformance_park: _PlanningConformancePark | None = None,
    validated_head_sha: str | None = None,
) -> None:
    """Push a validated workspace, record the PR, and start its monitor."""
    if conformance_park is not None and not validated_head_sha:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.validating,
            failure_reason=FailureReason.infrastructure_failure,
            message="Could not identify the workspace HEAD that passed validation.",
            reason_code="VALIDATION_MISSING_FOR_CURRENT_HEAD",
        )
        return
    transition_options = (
        {
            "extra_conditions": (Workspace.execution_claimed_by == ws.execution_claimed_by,),
            "on_transition": partial(
                _stage_conformance_park, park=conformance_park, head_sha=validated_head_sha
            ),
        }
        if conformance_park is not None
        else {}
    )
    if not await self._transition_if_current(
        workspace_id,
        from_status=WorkspaceStatus.validating,
        to=WorkspaceStatus.pushing,
        reason="VALIDATION_OK",
        action="start_push",
        **transition_options,
    ):
        return

    if resume_from_blocked:
        # Consume single-use grants after the transition succeeds. A workspace
        # that loses the status race still needs its grants on the next resume.
        await self._consume_active_operator_grants(workspace_id)
    if not await self._ensure_worktree_available(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        expected=WorkspaceStatus.pushing,
        action="pr_push_open",
    ):
        return
    if conformance_park is not None and not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.pushing,
        action="pr_push_open",
        owner_id=ws.execution_claimed_by,
    ):
        return

    pr = await pr_open_step.push_and_open_pr(
        self,
        ws=ws,
        profile=profile,
        defaults=defaults,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        carried_gaps=carried_gaps,
        publish_gaps_on_reused_pr=(
            not in_recovery or retry_gap_publication or conformance_park is not None
        ),
        expected_head_sha=validated_head_sha if conformance_park is not None else None,
        retry_gap_publication=retry_gap_publication,
    )
    if pr is None:
        return

    await persist_pr_and_handoff(
        self,
        workspace_id=workspace_id,
        pr=pr,
        adapter=adapter,
        profile=profile,
        defaults=defaults,
        successful_validation_run_id=successful_validation_run_id,
        compose_project=compose_project,
        compose_file=compose_file,
        carried_gaps=(
            () if conformance_park is None and in_recovery and pr.url == ws.pr_url else carried_gaps
        ),
        conformance_park=conformance_park,
        validated_head_sha=validated_head_sha,
        **_monitor_execution_claim_kwargs(ws),
    )


async def _stage_conformance_park(
    repo: WorkspaceRepository,
    workspace: Workspace,
    *,
    park: _PlanningConformancePark,
    head_sha: str | None,
) -> None:
    # A worker can recover as soon as the pushing transition commits.
    workspace.monitor_threads_addressed = persist_operator_hint(
        dict(workspace.monitor_threads_addressed or {}),
        OperatorHint(
            reason=park.reason,
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
            status="needs_human",
        ),
    )
    workspace.block_planning_conformance_handoff = {
        "carried_gaps": park.gaps,
        "conformance_park_head_sha": head_sha,
        **park.details,
    }
    await repo.add_event(
        workspace,
        event_type="workspace.plan_conformance_operator_wait",
        reason_code=PLAN_CONFORMANCE_UNSATISFIED,
        payload={**park.details, "head_sha": head_sha},
    )


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
    carried_gaps: Sequence[dict[str, str]] = (),
    dormancy_execution_claim: tuple[str, int] | None = None,
    conformance_park: _PlanningConformancePark | None = None,
    validated_head_sha: str | None = None,
) -> None:
    """Persist PR metadata and hand off to the monitor when configured."""
    target_head_sha = validated_head_sha if conformance_park is not None else pr.head_sha
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        persisted = await repo.get_for_update(workspace_id)
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
        publication = pr.open_metadata or {}
        publication_pending = publication.get("conformance_comment_pending") is True
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
        if carried_gaps:
            monitor_state = MonitorState(
                threads_addressed_ids=dict(persisted.monitor_threads_addressed or {}),
            )
            append_plan_conformance_advisory(
                monitor_state,
                workspace_id=workspace_id,
                gaps=carried_gaps,
                head_sha=pr.head_sha,
            )
            persisted.monitor_threads_addressed = monitor_state.threads_addressed_ids
        monitor = None if publication_pending else self._pr_monitor
        if not publication_pending and monitor is None and self._pr_monitor_factory is not None:
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

        if publication_pending:
            persisted.failure_reason = FailureReason.infrastructure_failure.value
            persisted.failure_message = (
                "Could not publish unresolved plan-conformance gaps on the pull request: "
                f"{publication['conformance_comment_error']}"
            )
            await repo.transition(
                persisted,
                to=WorkspaceStatus.failed,
                reason_code=_PR_CONFORMANCE_COMMENT_FAILED_REASON_CODE,
            )
        elif monitor is not None or conformance_park is not None:
            if not had_existing_pr_url:
                persisted.monitor_threads_addressed = _with_settled_disposition_baseline(
                    persisted.monitor_threads_addressed
                )
            await repo.transition(
                persisted,
                to=WorkspaceStatus.monitoring_pr,
                reason_code=pr_reason_code,
            )
            if conformance_park is not None:
                await repo.set_workspace_attention(
                    workspace_id,
                    reason=conformance_park.reason,
                    now=datetime.now(UTC),
                )
        else:
            await repo.transition(
                persisted,
                to=WorkspaceStatus.completed,
                reason_code=pr_reason_code,
            )
        if (
            conformance_park is not None
            and successful_validation_run_id is not None
            and target_head_sha
        ):
            # Recovery can start monitoring as soon as this transaction commits.
            await ValidationRunRepository(session).update_target_head_sha(
                successful_validation_run_id,
                target_head_sha=target_head_sha,
            )
        await session.commit()

    if conformance_park is None and successful_validation_run_id is not None and target_head_sha:
        try:
            await self._set_validation_run_target_head_sha(
                validation_run_id=successful_validation_run_id,
                target_head_sha=target_head_sha,
            )
        except Exception:
            _log.exception(
                "executor.validation_run_target_head_sha_update_failed",
                workspace_id=workspace_id,
                validation_run_id=successful_validation_run_id,
                target_head_sha=target_head_sha,
            )

    if publication_pending or (conformance_park is not None and monitor is None):
        return
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
        claim_kwargs = (
            {"dormancy_execution_claim": dormancy_execution_claim}
            if dormancy_execution_claim is not None
            else {}
        )
        await monitor.run(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            **claim_kwargs,
            **work_slot_kwargs(work_slot_for(self, workspace_id)),
        )
        return
    _log.info(
        "executor.completed",
        workspace_id=workspace_id,
        pr_url=pr.url,
    )
