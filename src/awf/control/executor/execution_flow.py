"""WorkspaceExecutor execution flow.

Mechanically extracted from the original orchestrator; behavior is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from awf.adapters.base import (
    AgentAdapter,
    AgentDefaults,
    AgentRunError,
    get_adapter,
)
from awf.common.command_evidence import (
    append_command_evidence,
)
from awf.common.commands import CommandResult
from awf.common.compose_exec import (
    EXEC_PROCESS_CLEANUP_FAILED,
    ComposeExecCleanupError,
    cleanup_failure_message,
)
from awf.common.forge import concrete_forge_for_repo, make_forge_client
from awf.common.git_identity import (
    git_identity_config_args,
    git_safe_directory_config_args,
)
from awf.common.task_tag import commit_message_with_task_tag, title_with_task_tag
from awf.control.executor import execution_validation as _execution_validation
from awf.control.executor.constants import (
    _AUDIT_GIT_PUSH_EVENT,
    _AUDIT_PR_CREATED_EVENT,
    _GIT_PUSH_FAILED_REASON_CODE,
    _PR_CREATE_FAILED_REASON_CODE,
    GIT_OBJECT_MISSING_RECOVERED_REASON_CODE,
)
from awf.control.executor.forge_gate import (
    unsupported_forge_error,
)
from awf.control.executor.git_ops import (
    _git_error_indicates_missing_head_object,
    _git_name_lines,
    _recover_branch_drift,
)
from awf.control.executor.helpers import (
    _agent_defaults_for_workspace,
    _agent_run_model_for_workspace,
    _build_pr_body,
    _call_pr_monitor_factory,
    _existing_pr_remote_push_url,
    _extract_pr_number,
    _failure_reason_for_phase,
    _failure_salvage_payload,
    _profile_for_workspace,
    _provider_recovery_default_model_for_monitor_handoff,
)
from awf.control.executor.logging_ops import (
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    _setup_dependency_network_failure_details,
)
from awf.control.executor.metadata import _str_or_none
from awf.control.executor.protocols import _MonitorRunnerProto
from awf.control.executor.quality_gates import (
    _classify_post_agent_commit_failure,
    _is_nothing_to_commit,
    _log,
    _PostAgentCommitStepError,
)
from awf.control.executor.recovery_payloads import (
    _get_active_recovery_payload,
    _planning_validation_handoff_from_recovery_payload,
    _recovery_needs_existing_pr_push,
)
from awf.control.executor.state_ops import _sync_resolved_profile
from awf.control.executor.supply_chain_messages import _supply_chain_block_message
from awf.control.executor.types import (
    _MonitorRebaseRecoveryError,
    _PlanningRunFailure,
    _PlanningValidationHandoff,
    _RebaseRecoveryResult,
)
from awf.control.quality_gates import (
    find_protected_quality_gate_changes,
    quality_gate_violation_message,
)
from awf.db.enums import (
    AgentRuntime,
    FailureReason,
    OperationStatus,
    WorkspaceStatus,
)
from awf.db.repositories import WorkspaceRepository
from awf.profiles.models import WorkspaceProfile
from awf.runtime.agent_scratch import apply_agent_scratch_excludes
from awf.runtime.ownership import (
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.planning import AGENT_PLAN_PHASE_SCOPE_VIOLATION
from awf.runtime.pr_creator import PullRequestError
from awf.runtime.validation import (
    ValidationCoverageResult,
    ValidationResult,
)


def _validate_only_recovery_target_head_sha(
    recovery: Mapping[str, Any] | None,
    *,
    validated_workspace_head_sha: str | None,
) -> str | None:
    """Return the recovery source head SHA when this is validate-only recovery."""
    if not recovery or recovery.get("recovery_mode") != "validate_only":
        return None
    source_head_sha = _str_or_none(recovery.get("source_head_sha"))
    if source_head_sha is None:
        return None
    normalized_source_head_sha = source_head_sha.strip()
    if not normalized_source_head_sha:
        return None
    if validated_workspace_head_sha != normalized_source_head_sha:
        return None
    return normalized_source_head_sha


async def execute(
    self: Any,
    workspace_id: str,
    *,
    execution_owner_id: str | None = None,
    execution_lease_expires_at: datetime | None = None,
) -> None:
    """Drive a ``ready`` workspace to ``completed`` (or ``failed``).

    The function is idempotent in the sense that it refuses to run on a
    workspace that is not currently in ``ready`` — useful when a poll
    loop races with a manual invocation.
    """
    ws = await self._claim_ready(
        workspace_id,
        execution_owner_id=execution_owner_id,
        execution_lease_expires_at=execution_lease_expires_at,
    )
    if ws is None:
        return
    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.running,
        action="execute",
    ):
        return

    compose_file = (
        Path(ws.compose_file_path)
        if ws.compose_file_path
        else self._config.compose_projects_root / workspace_id / "compose.yml"
    )
    compose_project = ws.compose_project_name or f"awf_{workspace_id}"
    worktree_path = self._config.worktrees_root / workspace_id

    # Deprecated/unsupported task kinds must fail fast unconditionally,
    # BEFORE branching on recovery. The recovery branch below skips
    # ``_dispatch_non_feature_task_kind``, so a ``monitor_release_pr`` or
    # unknown kind that re-entered the executor with an active validate /
    # rebase recovery (e.g. a worker-restart salvage of a stale ``running``
    # claim) would otherwise bypass the guard and resume the validation
    # path — the "silently run as feature work" scenario this is meant to
    # forbid. ``sync_feature_pr`` / ``sync_release_pr`` are NOT rejected
    # here so their recovery resumption stays intact.
    if await self._reject_unsupported_task_kind(
        workspace_id=workspace_id,
        workspace=ws,
    ):
        return

    # Forge-support gate — fail fast on a detected-but-unimplemented forge
    # (e.g. bitbucket) BEFORE every downstream gh path (non-feature dispatch,
    # agent run, push, ``gh pr create``). See ``forge_gate`` for the
    # resolved-vs-detected forge reasoning.
    forge_error = unsupported_forge_error(ws)
    if forge_error is not None:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=forge_error.message,
            reason_code=forge_error.reason_code,
        )
        return

    # When the PR monitor's RECOVERY_DISPATCH path delivered this
    # workspace, the executor must NOT re-run planning, the agent
    # CLI, or any post-agent commit hooks — those would rewrite the
    # plan artifact and re-implement the feature mid-merge. Recovery
    # only re-runs validation against the already-pushed work.
    recovery = _get_active_recovery_payload(ws)
    if recovery is None:
        guard_result = await self._block_open_pr_reexecution_without_recovery(
            workspace_id=workspace_id,
        )
        if guard_result.blocked:
            return
        recovery = guard_result.recovery

    if recovery is None and await self._dispatch_non_feature_task_kind(
        workspace_id=workspace_id,
        workspace=ws,
        compose_project=compose_project,
        compose_file=compose_file,
        worktree_path=worktree_path,
    ):
        return

    # ── Step 1: agent CLI runs the task inside the container ────────────
    if recovery is None:
        salvage_result = await self._prepare_conformance_salvage_for_execution(
            workspace_id=workspace_id,
            workspace=ws,
            worktree_path=worktree_path,
        )
        if salvage_result is not None:
            if salvage_result.status == "failed":
                return
            if salvage_result.prompt_override is not None:
                ws.task_prompt = salvage_result.prompt_override
    rebase_recovery_result: _RebaseRecoveryResult | None = None
    baseline_coverage: ValidationCoverageResult | None = None
    profile: WorkspaceProfile | None = None
    agent_exit_note: str | None = None
    agent_run_reason_code: str | None = None
    agent_run_details: Mapping[str, Any] | None = None
    # ``agent_run_failure_reason`` is only set when the upstream cause was
    # an actual agent/provider failure (``AgentRunError``). Recovered
    # infrastructure paths (e.g. missing-HEAD recovery) leave this None so
    # downstream commit failures route through the standard infra path
    # instead of being mis-classified as agent failures and queueing
    # provider recovery.
    agent_run_failure_reason: FailureReason | None = None
    planning_validation_handoff: _PlanningValidationHandoff | None = None
    expected_branch = ws.branch_name or f"awf/{workspace_id}"
    adapter: AgentAdapter | None = None
    defaults: AgentDefaults | None = None
    run_model: str | None = None
    agent_command_evidence: list[str] = []
    try:
        agent = AgentRuntime(ws.agent)
        defaults = self._defaults_for(agent)
        adapter_defaults = _agent_defaults_for_workspace(ws, defaults)
        run_model = _agent_run_model_for_workspace(ws)
        adapter = get_adapter(
            agent,
            runner=self._runner,
            defaults=adapter_defaults,
            log_store=self._log_store,
            agent_wall_timeout_seconds=self._config.agent_wall_timeout_seconds,
            agent_idle_timeout_seconds=self._config.agent_idle_timeout_seconds,
            usage_sampler=self._usage_sampler,
        )
        # Make the agent runtime's checkout-local scratch dirs (e.g.
        # claude_code's ``.claude/worktrees/``) git-ignored in this worktree
        # before the agent can create them, so AWF's validation-cleanliness
        # guard treats them as ignored agent-runtime state rather than a dirty
        # tree. No-op for agents that declare no scratch paths; runs on both the
        # initial and recovery paths since each later runs validation.
        await apply_agent_scratch_excludes(
            run_git=lambda args: self._runner.run(
                [
                    "git",
                    *git_safe_directory_config_args(worktree_path),
                    "-C",
                    str(worktree_path),
                    *args,
                ]
            ),
            worktree_path=worktree_path,
            scratch_paths=adapter.runtime_scratch_paths,
        )
        profile = _profile_for_workspace(
            ws,
            worktree_path=worktree_path,
            planning_max_iterations_default=(self._config.planning_max_iterations_default),
        )
        if not ws.resolved_profile:
            profile = await _sync_resolved_profile(
                self,
                ws=ws,
                workspace_id=workspace_id,
                profile=profile,
                planning_max_iterations_default=(self._config.planning_max_iterations_default),
            )
            # Re-run the forge gate on the just-resolved profile. The
            # pre-resolution gate above only saw the *absent* snapshot plus
            # repo_url, so an explicit ``forge: bitbucket`` carried by the
            # requested/repo-local profile (with a GitHub or undetectable
            # repo_url that detects as github) slipped past it. Resolution
            # has now stamped + persisted the concrete forge onto
            # ``ws.resolved_profile``, so fail fast here — before profile
            # setup, the agent run, and push — instead of letting an
            # unsupported forge reach the push/PR-open step.
            resolved_forge_error = unsupported_forge_error(ws)
            if resolved_forge_error is not None:
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.running,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=resolved_forge_error.message,
                    reason_code=resolved_forge_error.reason_code,
                )
                return
        if not await repair_agent_runtime_ownership(
            logger=_log,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="profile_setup",
            event_name=EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
            reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
        ):
            if recovery is not None:
                await self._finish_active_recovery_operations(
                    workspace_id=workspace_id,
                    status=OperationStatus.failed,
                    reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
                    error_message=("agent runtime ownership repair failed before profile setup"),
                )
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message="agent runtime ownership repair failed before profile setup",
                reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
            )
            return
        setup_result = await self._validation.run_profile_phases(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            profile=profile,
            phase_names=("setup", "pre_agent"),
            worktree_path=worktree_path,
        )
        try:
            await self._record_setup_dependency_network_events(
                workspace_id=workspace_id,
                result=setup_result,
            )
        except Exception:
            _log.exception(
                "executor.setup_dependency_network_event_record_failed",
                workspace_id=workspace_id,
                setup_all_passed=setup_result.all_passed,
            )
        if not setup_result.all_passed:
            first_fail = setup_result.first_failure
            setup_dependency_details = _setup_dependency_network_failure_details(first_fail)
            setup_failure_reason_code = (
                SETUP_DEPENDENCY_NETWORK_FAILURE if setup_dependency_details is not None else None
            )
            if recovery is not None:
                recovery_setup_failure_reason_code = (
                    setup_failure_reason_code or "MONITOR_RECOVERY_SETUP_FAILED"
                )
                await self._finish_active_recovery_operations(
                    workspace_id=workspace_id,
                    status=OperationStatus.failed,
                    reason_code=recovery_setup_failure_reason_code,
                    error_message=(
                        f"profile setup failed: {first_fail.command}"
                        if first_fail is not None
                        else "profile setup failed"
                    )[:2000],
                )
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=_failure_reason_for_phase(first_fail),
                message=(
                    f"profile setup failed: {first_fail.command}"
                    if first_fail is not None
                    else "profile setup failed"
                )[:2000],
                reason_code=setup_failure_reason_code,
                details=setup_dependency_details,
            )
            return
        profile_preflight = getattr(self._validation, "run_profile_tool_preflight", None)
        profile_preflight_result = (
            await profile_preflight(workspace_id=workspace_id, profile=profile)
            if callable(profile_preflight)
            else ValidationResult()
        )
        if not profile_preflight_result.all_passed:
            first_fail = profile_preflight_result.first_failure
            if recovery is not None:
                await self._finish_active_recovery_operations(
                    workspace_id=workspace_id,
                    status=OperationStatus.failed,
                    reason_code="MONITOR_RECOVERY_PROFILE_PREFLIGHT_FAILED",
                    error_message=(
                        f"profile preflight failed: {first_fail.command}"
                        if first_fail is not None
                        else "profile preflight failed"
                    )[:2000],
                )
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=_failure_reason_for_phase(first_fail),
                message=(
                    f"profile preflight failed: {first_fail.command}"
                    if first_fail is not None
                    else "profile preflight failed"
                )[:2000],
            )
            return
        if recovery is None:
            if not await self._run_agent_git_writability_preflight(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                worktree_path=worktree_path,
            ):
                return
            baseline_coverage = await self._run_baseline_coverage_preflight(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                profile=profile,
            )
            if not await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.running,
                action="agent_run",
            ):
                return
            planning_failure = await self._run_agent_task_with_optional_planning(
                adapter=adapter,
                workspace=ws,
                profile=profile,
                compose_project=compose_project,
                compose_file=compose_file,
                worktree_path=worktree_path,
                model=run_model,
                command_evidence=agent_command_evidence,
            )
            if isinstance(planning_failure, _PlanningValidationHandoff):
                planning_validation_handoff = planning_failure
                await self._record_planning_validation_handoff_event(
                    workspace_id=workspace_id,
                    handoff=planning_failure,
                )
            elif planning_failure is not None:
                failure_message = (
                    planning_failure
                    if isinstance(planning_failure, str)
                    else planning_failure.message
                )
                reason_code = (
                    None if isinstance(planning_failure, str) else planning_failure.reason_code
                )
                details = None if isinstance(planning_failure, str) else planning_failure.details
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.running,
                    failure_reason=FailureReason.agent_failure,
                    message=failure_message[:2000],
                    reason_code=reason_code,
                    details=details,
                    salvage=_failure_salvage_payload(ws, worktree_path=worktree_path),
                )
                if (
                    isinstance(planning_failure, _PlanningRunFailure)
                    and planning_failure.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
                ):
                    await self._auto_retry_planning_scope_failure(
                        workspace_id=workspace_id,
                        failure=planning_failure,
                    )
                return
        else:
            # Recovery dispatch created the validate Operation in ``pending``;
            # flush it to ``running`` before validation so observability
            # tooling sees a real ``started_at`` (otherwise the row jumps
            # straight from pending → succeeded/failed when the validate
            # finalizer fires, with started_at == finished_at).
            await self._start_pending_recovery_operations(
                workspace_id=workspace_id,
            )
            _log.info(
                "executor.validate_only_recovery_started",
                workspace_id=workspace_id,
                source=recovery.get("source"),
                recovery_mode=recovery.get("recovery_mode"),
                reason=recovery.get("reason"),
            )
            planning_validation_handoff = _planning_validation_handoff_from_recovery_payload(
                workspace_id=workspace_id,
                profile=profile,
                recovery_payload=recovery,
            )
    except ComposeExecCleanupError as exc:
        _log.error(
            "executor.exec_process_cleanup_failed",
            workspace_id=workspace_id,
            source=exc.source,
            label=exc.label,
            invocation_id=exc.invocation_id,
            reason_code=exc.reason_code,
        )
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=cleanup_failure_message(exc),
            reason_code=EXEC_PROCESS_CLEANUP_FAILED,
        )
        return
    except AgentRunError as exc:
        append_command_evidence(
            agent_command_evidence,
            stdout=exc.result.stdout,
            stderr=exc.result.stderr,
        )
        # Do NOT bail out yet. A CLI that exits non-zero — typically
        # ``claude_code`` hitting a 1-hour internal session cap and
        # returning 137 (SIGKILL), or a timeout against a flaky
        # dependency — may have left valuable uncommitted work in the
        # worktree. Coding CLIs in general don't commit on their own;
        # AWF's post-agent auto-commit is the only thing that captures
        # their edits. Log the exit code, remember it for the final
        # failure message, but let the commit + validate pipeline run.
        # If there's nothing to commit, the existing no-work check
        # fails the workspace with ``agent_failure`` below. If there
        # IS work, validation decides whether it's pushable.
        # Structured provider-failure metadata is preserved in
        # ``agent_run_details``. If salvage finds no commits, the
        # no-work failure path below persists that metadata before
        # preparing the authorized provider retry/fallback workspace.
        agent_exit_note = (
            f"agent CLI exited {exc.result.returncode} ({exc.reason_code}); "
            f"continuing to salvage any uncommitted work"
        )
        agent_run_reason_code = exc.reason_code
        agent_run_details = getattr(exc, "details", None)
        agent_run_failure_reason = FailureReason.agent_failure
        _log.warning(
            "executor.agent_nonzero_exit_salvaging",
            workspace_id=workspace_id,
            agent=ws.agent,
            returncode=exc.result.returncode,
            reason_code=exc.reason_code,
        )
        await self._repair_agent_git_ownership(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="agent_nonzero_exit_salvage",
        )
    except Exception as exc:  # unexpected — surface with generic reason
        if _git_error_indicates_missing_head_object(str(exc)):
            if await self._recover_missing_git_head_or_mark_failed(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                base_commit=ws.base_commit,
                branch_name=expected_branch,
                from_status=WorkspaceStatus.running,
                stage="agent_run",
                error=exc,
                task_tag=ws.task_tag,
            ):
                agent_exit_note = (
                    "AWF recovered a missing Git HEAD object during the agent run; "
                    "continuing to salvage filesystem work"
                )
                agent_run_reason_code = GIT_OBJECT_MISSING_RECOVERED_REASON_CODE
                agent_run_details = {"recovered_stage": "agent_run"}
            else:
                return
        else:
            _log.exception("executor.unexpected_in_agent", workspace_id=workspace_id)
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message=f"unexpected error during agent run: {exc!r}"[:2000],
            )
            return
    if adapter is None:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message="executor could not initialize agent adapter before post-agent capture",
        )
        return

    # ── Step 1b: capture the agent's work as a commit on the feature branch ──
    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.running,
        action="post_agent_commit",
    ):
        return

    # Coding CLIs make file edits reliably but are inconsistent about git:
    # some commit, some leave changes unstaged, some commit partial subsets
    # and leave the rest dirty. AWF normalizes: after the agent exits, we
    # stage everything and commit if anything's cached. If HEAD still
    # matches the base branch afterwards, the agent produced zero change
    # and we fail with a specific reason rather than pushing nothing.
    if not await self._ensure_worktree_available(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        expected=WorkspaceStatus.running,
        action="post_agent_commit",
    ):
        return

    # ``base_commit`` is set by the provisioner before a workspace ever
    # reaches ``ready`` — if it's missing here something went wrong
    # upstream and every ``rev-list``/``merge-base`` below would
    # inject the literal string "None" into a git command. Fail
    # cleanly instead of passing "None..HEAD" to git.
    if ws.base_commit is None:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=(
                "workspace has no base_commit — provisioning must set "
                "this before the agent run; cannot verify feature-branch "
                "commits without it"
            ),
        )
        return
    base_commit: str = ws.base_commit

    async def _git_in_worktree(args: list[str]):  # type: ignore[no-untyped-def]
        """Run a git command inside the workspace worktree."""
        return await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                *args,
            ]
        )

    try:
        if recovery is None:
            # Branch-drift recovery: fast-forward the expected AWF branch onto
            # the agent's tip if a coding CLI drifted to a self-named branch
            # mid-session. See ``git_ops._recover_branch_drift`` for rationale.
            await _recover_branch_drift(
                git_in_worktree=_git_in_worktree,
                workspace_id=workspace_id,
                expected_branch=expected_branch,
            )

            add_result = await _git_in_worktree(["add", "-A"])
            await self._repair_agent_git_ownership(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                reason="post_agent_git_add",
            )
            if not add_result.ok:
                raise _PostAgentCommitStepError(
                    stage="git add",
                    result=add_result,
                    classification=None,
                )
            cached = await _git_in_worktree(["diff", "--cached", "--name-only"])
            if not cached.ok:
                _log.warning(
                    "executor.post_agent_cached_diff_failed",
                    workspace_id=workspace_id,
                    reason="git diff --cached --name-only failed; assuming no staged paths for policy checks",
                    stderr=cached.stderr,
                )
                staged_paths = []
            else:
                staged_paths = _git_name_lines(cached.stdout) if cached.stdout.strip() else []
            supply_chain_result = await self._refresh_supply_chain_policy_for_workspace(
                workspace_id=workspace_id,
                command_evidence=agent_command_evidence,
                changed_paths=staged_paths,
            )
            if supply_chain_result.policy_blocked:
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.running,
                    failure_reason=FailureReason.policy_failure,
                    reason_code="SUPPLY_CHAIN_POLICY_BLOCKED",
                    message=_supply_chain_block_message(supply_chain_result.findings)[:2000],
                )
                return
            if staged_paths:
                if await self._committed_and_staged_output_is_plan_only(
                    worktree_path=worktree_path,
                    base_commit=base_commit,
                    staged_paths=staged_paths,
                ) and await self._fail_if_plan_only_paths(
                    workspace_id=workspace_id,
                    changed_paths=staged_paths,
                    expected_status=WorkspaceStatus.running,
                ):
                    return
                protected_file_diffs = await self._protected_file_diffs_for_staged_paths(
                    worktree_path=worktree_path,
                    base_ref=base_commit,
                    changed_paths=staged_paths,
                    owned_paths=list(ws.owned_paths),
                )
                violations = find_protected_quality_gate_changes(
                    changed_paths=staged_paths,
                    owned_paths=list(ws.owned_paths),
                    protected_file_diffs=protected_file_diffs,
                )
                if violations:
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.running,
                        failure_reason=FailureReason.policy_failure,
                        reason_code="QUALITY_GATE_POLICY_CHANGED",
                        message=quality_gate_violation_message(violations)[:2000],
                    )
                    return
                commit_msg = commit_message_with_task_tag(f"awf: {ws.task_title}", ws.task_tag)[:72]
                commit_body = f"Authored by AWF workspace {workspace_id} (agent: {ws.agent}).\n"

                async def _run_commit() -> CommandResult:
                    """Execute the post-agent ``git commit`` with AWF's identity."""
                    return cast(
                        CommandResult,
                        await self._runner.run(
                            [
                                "git",
                                *git_safe_directory_config_args(worktree_path),
                                "-C",
                                str(worktree_path),
                                *git_identity_config_args(),
                                "commit",
                                "-m",
                                commit_msg,
                                "-m",
                                commit_body,
                            ],
                        ),
                    )

                commit_result = await _run_commit()
                await self._repair_agent_git_ownership(
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    reason="post_agent_git_commit",
                )
                if not commit_result.ok:
                    if _is_nothing_to_commit(commit_result):
                        _log.info(
                            "executor.post_agent_commit_nothing_to_commit",
                            workspace_id=workspace_id,
                            output=(commit_result.stderr or commit_result.stdout or "").strip()[
                                :200
                            ],
                        )
                    else:
                        classification = _classify_post_agent_commit_failure(commit_result)
                        if classification.repair_strategy in {"deterministic", "agent"}:
                            await self._run_post_agent_commit_repair(
                                workspace_id=workspace_id,
                                worktree_path=worktree_path,
                                base_commit=base_commit,
                                commit_result=commit_result,
                                classification=classification,
                                staged_paths=staged_paths,
                                run_commit=_run_commit,
                                git_in_worktree=_git_in_worktree,
                                adapter=adapter,
                                compose_project=compose_project,
                                compose_file=compose_file,
                                model=run_model,
                                allow_agent_repair=agent_run_failure_reason is None,
                                ws=ws,
                                command_evidence=agent_command_evidence,
                            )
                        else:
                            raise _PostAgentCommitStepError(
                                stage="git commit",
                                result=commit_result,
                                classification=classification,
                                format_repair_attempted=False,
                            )
            # Regardless of whether we just committed, verify HEAD has advanced
            # past the base commit. If not, the agent produced no change.
            rev_count = await _git_in_worktree(["rev-list", "--count", f"{base_commit}..HEAD"])
            if not rev_count.ok:
                raise RuntimeError(
                    f"post-agent commit: `git rev-list --count {base_commit}..HEAD` failed with "
                    f"exit {rev_count.returncode}: {rev_count.stderr!r}"
                )
            if int(rev_count.stdout.strip() or "0") == 0:
                base_short = base_commit[:10] if base_commit else "unknown"
                message = (
                    f"agent exited without producing any commits on the feature branch "
                    f"(base={base_short})"
                )
                if agent_exit_note is not None:
                    message = f"{message}; {agent_exit_note}"

                # Provider recovery reads the failed state event, so
                # persist the structured reason/details first. The
                # recovery service creates an authorized delayed retry
                # or fallback workspace and no-ops for ordinary agent
                # failures.
                #
                # Gate provider recovery on
                # ``agent_run_failure_reason == agent_failure`` rather
                # than on ``agent_run_reason_code is not None``. The
                # recovered missing-HEAD path also populates
                # ``agent_run_reason_code`` (with
                # ``GIT_OBJECT_MISSING_RECOVERED``) but its upstream
                # cause is infrastructure recovery, not a provider
                # failure that warrants a delayed retry.
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.running,
                    failure_reason=FailureReason.agent_failure,
                    message=message,
                    reason_code=agent_run_reason_code,
                    details=agent_run_details,
                )
                if agent_run_failure_reason == FailureReason.agent_failure:
                    await self._prepare_provider_recovery(workspace_id)
                return

            # Some agents sever git history (e.g. by accidentally running
            # ``git checkout --orphan`` or by re-initialising the repo).
            # rev-list counts HIGH in that case (every HEAD commit is "new"
            # w.r.t. base because there's no shared ancestor), so the
            # previous check wouldn't notice. Without this guard, the push
            # succeeds but ``gh pr create`` dies with a cryptic
            # ``branch has no history in common with <base>`` error.
            #
            # Recovery: ``git reset --soft <base>`` moves HEAD to the base
            # commit while leaving the index untouched — the index still
            # reflects the orphan's tree. A fresh ``git commit`` then
            # produces a single commit on top of base that contains the
            # cumulative diff, and the branch is reattached to a valid
            # ancestry so the PR can be opened normally.
            #
            # Invariant: ``base_commit`` is always populated by
            # ``_claim_ready`` before this block runs. The ``assert`` both
            # documents and satisfies mypy.
            ancestor = await _git_in_worktree(["merge-base", "--is-ancestor", base_commit, "HEAD"])
            if not ancestor.ok:
                _log.warning(
                    "executor.orphan_history_detected",
                    workspace_id=workspace_id,
                    base_commit=base_commit,
                )
                reset = await _git_in_worktree(["reset", "--soft", base_commit])
                await self._repair_agent_git_ownership(
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    reason="orphan_history_reset",
                )
                if reset.ok:
                    recovery_msg = commit_message_with_task_tag(
                        f"awf: {ws.task_title} (recovered from orphan)", ws.task_tag
                    )[:72]
                    recovery_body = (
                        f"AWF detected orphan history on workspace {workspace_id} "
                        f"(agent: {ws.agent}) and squashed the cumulative diff "
                        f"onto base commit {base_commit[:10]}.\n"
                    )
                    recover_commit = await self._runner.run(
                        [
                            "git",
                            *git_safe_directory_config_args(worktree_path),
                            "-C",
                            str(worktree_path),
                            *git_identity_config_args(),
                            "commit",
                            "-m",
                            recovery_msg,
                            "-m",
                            recovery_body,
                        ],
                    )
                    await self._repair_agent_git_ownership(
                        workspace_id=workspace_id,
                        worktree_path=worktree_path,
                        reason="orphan_history_recovery_commit",
                    )
                    if recover_commit.ok:
                        ancestor = await _git_in_worktree(
                            ["merge-base", "--is-ancestor", base_commit, "HEAD"]
                        )
                if not ancestor.ok:
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.running,
                        failure_reason=FailureReason.agent_failure,
                        message=(
                            "agent severed git history — HEAD does not descend from "
                            f"base commit {base_commit[:10] if base_commit else 'unknown'}, "
                            "and automatic recovery (reset --soft + fresh commit) also failed. "
                            "The coding CLI likely ran `git checkout --orphan` or reinitialised "
                            "the repo; inspect the worktree manually."
                        ),
                    )
                    return
                _log.info(
                    "executor.orphan_history_recovered",
                    workspace_id=workspace_id,
                    base_commit=base_commit,
                )
        elif recovery.get("recovery_mode") == "rebase_only":
            try:
                rebase_recovery_result = await self._run_monitor_rebase_recovery(
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    base_branch=ws.branch_base,
                    branch_name=expected_branch,
                    remote_branch=ws.remote_push_branch or expected_branch,
                    reason=str(recovery.get("reason") or "stale"),
                    recovery_payload=recovery,
                )
                base_commit = rebase_recovery_result.base_sha
            except _MonitorRebaseRecoveryError as exc:
                message = str(exc)[:2000]
                await self._finish_active_recovery_operations(
                    workspace_id=workspace_id,
                    status=OperationStatus.failed,
                    reason_code="MONITOR_RECOVERY_REBASE_FAILED",
                    error_message=message,
                )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.running,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=message,
                    reason_code="MONITOR_RECOVERY_REBASE_FAILED",
                )
                return
    except _PostAgentCommitStepError as exc:
        await self._mark_post_agent_commit_failed(
            workspace_id=workspace_id,
            error=exc,
            agent_run_reason_code=agent_run_reason_code,
            agent_run_details=agent_run_details,
            agent_exit_note=agent_exit_note,
            upstream_failure_reason=agent_run_failure_reason,
        )
        return
    except Exception as exc:  # unexpected — mark infrastructure
        if _git_error_indicates_missing_head_object(str(exc)):
            if await self._recover_missing_git_head_or_mark_failed(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                base_commit=base_commit,
                branch_name=expected_branch,
                from_status=WorkspaceStatus.running,
                stage="post_agent_commit",
                error=exc,
                task_tag=ws.task_tag,
            ):
                _log.warning(
                    "executor.commit_step_missing_head_recovered",
                    workspace_id=workspace_id,
                )
                if not await self._verify_recovered_post_agent_commit_or_mark_failed(
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    base_commit=base_commit,
                    owned_paths=list(ws.owned_paths),
                    expected_status=WorkspaceStatus.running,
                ):
                    return
            else:
                return
        else:
            _log.exception("executor.commit_step_failed", workspace_id=workspace_id)
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message=f"post-agent commit step failed: {exc!r}"[:2000],
            )
            return

    validation_result = await _execution_validation.run_validation_and_fix_cycle(
        self,
        workspace_id=workspace_id,
        ws=ws,
        worktree_path=worktree_path,
        compose_project=compose_project,
        compose_file=compose_file,
        base_commit=base_commit,
        expected_branch=expected_branch,
        adapter=adapter,
        run_model=run_model,
        baseline_coverage=baseline_coverage,
        planning_validation_handoff=planning_validation_handoff,
        recovery=recovery,
        rebase_recovery_result=rebase_recovery_result,
        git_in_worktree=_git_in_worktree,
    )
    if validation_result.stop:
        return
    assert profile is not None
    successful_validation_run_id = validation_result.successful_validation_run_id
    successful_validation_workspace_head_sha = (
        validation_result.successful_validation_workspace_head_sha
    )

    # ── Recovery skip-push guard ───────────────────────────────────────
    # Recovery for a workspace that already has an open PR must NOT
    # re-create the PR. Clean validate-only recovery does not push; if a
    # fix pass or handoff report created a new validated local commit,
    # update the existing PR branch before handing back to the monitor.
    # Rebase-only recovery already pushed the rebased branch above, but
    # later validation work can still advance local HEAD.
    if recovery is not None and ws.pr_url:
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
        if not recovery_requires_pr_update:
            if not await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.validating,
                action="recovery_skip_push",
            ):
                return
            async with self._session_factory() as session:
                repo = WorkspaceRepository(session)
                persisted = await repo.get(workspace_id)
                if persisted is None:  # pragma: no cover - destroyed mid-flight
                    return
                if persisted.status != WorkspaceStatus.validating.value:
                    await self._record_stale_action_skip(
                        repo,
                        persisted,
                        action="recovery_skip_push",
                        expected=WorkspaceStatus.validating,
                        reason_code="EXECUTOR_STALE_STATUS",
                    )
                    await session.commit()
                    return
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
                _monitor: _MonitorRunnerProto | None = self._pr_monitor
                if _monitor is None and self._pr_monitor_factory is not None:
                    _monitor = _call_pr_monitor_factory(
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
                if _monitor is not None:
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
                        return
                    await _monitor.run(
                        workspace_id=workspace_id,
                        compose_project=compose_project,
                        compose_file=compose_file,
                    )
            return
        _log.info(
            "executor.recovery_existing_pr_update_required",
            workspace_id=workspace_id,
            pr_url=ws.pr_url,
            source_head_sha=recovery.get("source_head_sha"),
            validated_workspace_head_sha=successful_validation_workspace_head_sha,
        )

    # The committed-output gates below diff ``base..HEAD`` in the worktree. If
    # the worktree vanished during validation/repair the diff would fail and the
    # empty-net-diff branch of the plan-only gate would mislabel the disappearance
    # as a terminal PLAN_ONLY_OUTPUT agent failure. Surface the missing worktree
    # as WORKTREE_MISSING (infrastructure) first so the reason code reflects the
    # real cause, mirroring the worktree guard at the push step below.
    if not await self._ensure_worktree_available(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        expected=WorkspaceStatus.validating,
        action="pre_push_policy_check",
    ):
        return
    try:
        if await self._fail_if_plan_only_committed_output(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            base_commit=base_commit,
            expected_status=WorkspaceStatus.validating,
        ):
            return
        if await self._fail_if_protected_quality_gate_committed_output(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            base_commit=base_commit,
            owned_paths=list(ws.owned_paths),
            expected_status=WorkspaceStatus.validating,
        ):
            return
    except Exception as exc:
        _log.exception("executor.pre_push_policy_check_failed", workspace_id=workspace_id)
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.validating,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"pre-push policy check failed: {exc!r}"[:2000],
        )
        return

    # PR creation is forge-neutral: ``push_and_open`` does a plain ``git push``
    # and routes the PR-open step through the resolved ``ForgeClient`` (GitHub or
    # Bitbucket Cloud). The forge client is resolved from the persisted profile +
    # repo URL and passed in per-call, so a Bitbucket feature workspace opens its
    # PR via ``BitbucketClient`` instead of the GitHub-only ``gh pr create``.

    # ── Step 3: push + open PR ──────────────────────────────────────────
    if not await self._transition_if_current(
        workspace_id,
        from_status=WorkspaceStatus.validating,
        to=WorkspaceStatus.pushing,
        reason="VALIDATION_OK",
        action="start_push",
    ):
        return
    if not await self._ensure_worktree_available(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        expected=WorkspaceStatus.pushing,
        action="pr_push_open",
    ):
        return

    pr_title = title_with_task_tag(ws.task_title, ws.task_tag)
    pr_body = _build_pr_body(ws, defaults=defaults)
    push_branch_name = ws.branch_name or f"awf/{workspace_id}"
    existing_pr_remote_branch = ws.remote_push_branch if ws.pr_url else None
    existing_pr_remote_url = _existing_pr_remote_push_url(ws) if ws.pr_url else None
    audit_remote_branch = existing_pr_remote_branch or push_branch_name

    try:
        if ws.pr_url:
            # Reuse path: ``push_and_open`` only does a plain ``git push`` and
            # reuses the existing PR — it never touches the forge client. Skip
            # resolving one so a Bitbucket reuse push is not gated on forge API
            # env: ``make_forge_client`` builds ``BitbucketClient`` eagerly via
            # ``from_env()``, which would fail the run on missing/invalid
            # Bitbucket API env before the push, even though reuse makes no forge
            # API call. (This mirrors the pre-forge-client flow, where reuse
            # never resolved a forge client.)
            pr = await self._pr_creator.push_and_open(
                worktree_path=worktree_path,
                branch_name=push_branch_name,
                base_branch=ws.branch_base,
                title=pr_title,
                body=pr_body,
                forge_client=None,
                repo_url=ws.repo_url,
                existing_pr_url=ws.pr_url,
                remote_branch_name=existing_pr_remote_branch,
                remote_url=existing_pr_remote_url,
            )
        else:
            # New PR: ``make_forge_client`` builds the Bitbucket client eagerly
            # via ``from_env()``, so a missing/invalid Bitbucket API env raises
            # ``BitbucketClientError`` here — before ``push_and_open`` runs the
            # git push or the create-PR call, so it cannot be wrapped as a
            # ``PullRequestError`` downstream. Map it onto the same
            # PR_CREATE_FAILED audit event + evidence that a ``create_pull_request``
            # failure records (instead of falling through to the opaque
            # "unexpected error" handler that emits no PR audit event), then fail
            # the run. No git_push-succeeded event is recorded because the push
            # never ran. ``BitbucketClientError`` is imported lazily so the
            # GitHub-only hot path never pays for the httpx import it drags in
            # (mirrors forge.make_forge_client / pr_creator). ``async with``
            # releases the Bitbucket httpx pool deterministically (GitHub aclose
            # is a no-op).
            from awf.common.bitbucket_client import BitbucketClientError

            try:
                forge_client = make_forge_client(
                    concrete_forge_for_repo(profile.forge, ws.repo_url),
                    self._runner,
                )
            except BitbucketClientError as exc:
                _log.error(
                    "executor.pr_failed",
                    workspace_id=workspace_id,
                    operation=exc.operation,
                    returncode=exc.status if exc.status is not None else 0,
                )
                await self._record_executor_pr_audit_event(
                    workspace_id,
                    event_type=_AUDIT_PR_CREATED_EVENT,
                    action="pr_create",
                    outcome="failed",
                    reason_code=_PR_CREATE_FAILED_REASON_CODE,
                    branch_name=push_branch_name,
                    remote_branch=audit_remote_branch,
                    pr_number=None,
                    pr_url=None,
                    source_head_sha=None,
                    evidence={
                        "operation": exc.operation,
                        "returncode": exc.status if exc.status is not None else 0,
                        "error_message": exc.body.strip() or "<no output>",
                    },
                )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.pushing,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=str(exc)[:2000],
                    # Preserve the forge-specific reason code (e.g.
                    # BITBUCKET_AUTH_NOT_CONFIGURED) so the failed workspace
                    # carries actionable doctor guidance, matching the
                    # PullRequestError path below instead of falling back to
                    # the generic INFRASTRUCTURE_FAILURE.
                    reason_code=exc.reason_code,
                )
                return
            async with forge_client:
                pr = await self._pr_creator.push_and_open(
                    worktree_path=worktree_path,
                    branch_name=push_branch_name,
                    base_branch=ws.branch_base,
                    title=pr_title,
                    body=pr_body,
                    forge_client=forge_client,
                    repo_url=ws.repo_url,
                    existing_pr_url=None,
                    remote_branch_name=existing_pr_remote_branch,
                    remote_url=existing_pr_remote_url,
                )
    except PullRequestError as exc:
        _log.error(
            "executor.pr_failed",
            workspace_id=workspace_id,
            operation=exc.operation,
            returncode=exc.returncode,
        )
        if exc.operation != "git push":
            await self._record_executor_pr_audit_event(
                workspace_id,
                event_type=_AUDIT_GIT_PUSH_EVENT,
                action="git_push",
                outcome="succeeded",
                reason_code="PR_UPDATED" if ws.pr_url else "PR_OPENED",
                branch_name=push_branch_name,
                remote_branch=audit_remote_branch,
                pr_number=_extract_pr_number(ws.pr_url) if ws.pr_url else None,
                pr_url=ws.pr_url,
                source_head_sha=exc.head_sha,
            )
        await self._record_executor_pr_audit_event(
            workspace_id,
            event_type=(
                _AUDIT_GIT_PUSH_EVENT if exc.operation == "git push" else _AUDIT_PR_CREATED_EVENT
            ),
            action="git_push" if exc.operation == "git push" else "pr_create",
            outcome="failed",
            reason_code=(
                _GIT_PUSH_FAILED_REASON_CODE
                if exc.operation == "git push"
                else _PR_CREATE_FAILED_REASON_CODE
            ),
            branch_name=push_branch_name,
            remote_branch=audit_remote_branch,
            pr_number=_extract_pr_number(ws.pr_url) if ws.pr_url else None,
            pr_url=ws.pr_url,
            source_head_sha=exc.head_sha,
            evidence={
                "operation": exc.operation,
                "returncode": exc.returncode,
                "error_message": exc.stderr.strip() or "<no output>",
            },
        )
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.pushing,
            failure_reason=FailureReason.infrastructure_failure,
            message=str(exc)[:2000],
            # Preserve a forge-specific reason code (e.g. Bitbucket auth /
            # rate-limit / transport) so the failed workspace carries the
            # actionable doctor guidance; ``None`` (git push, GitHub, no-URL)
            # falls back to ``INFRASTRUCTURE_FAILURE``.
            reason_code=exc.reason_code,
        )
        return
    except Exception as exc:
        _log.exception("executor.pr_unexpected_failed", workspace_id=workspace_id)
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.pushing,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"unexpected error during PR creation: {exc!r}"[:2000],
        )
        return

    # ── Step 4: persist PR URL + (optionally) hand off to monitor ──────
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
        )
        # Resolve which monitor (if any) to hand off to. Pre-constructed
        # ``pr_monitor`` wins (tests); otherwise the factory builds one
        # from the per-task adapter now that we have it.
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
            # Hand off to the monitor — it will transition to completed
            # (on merge) or failed (on abort / cap / close).
            await repo.transition(
                persisted,
                to=WorkspaceStatus.monitoring_pr,
                reason_code=pr_reason_code,
            )
            await session.commit()
        else:
            # No monitor wired (legacy executor path / unit-test shim) —
            # preserve the original ``pushing → completed`` contract.
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
