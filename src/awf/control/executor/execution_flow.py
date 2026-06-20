"""WorkspaceExecutor execution flow."""

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
from awf.common.command_evidence import append_command_evidence
from awf.common.commands import CommandResult
from awf.common.compose_exec import (
    EXEC_PROCESS_CLEANUP_FAILED,
    ComposeExecCleanupError,
    cleanup_failure_message,
)
from awf.common.git_identity import (
    git_identity_config_args,
    git_safe_directory_config_args,
)
from awf.common.task_tag import (
    commit_message_with_task_tag,
    strip_leading_task_tag,
)
from awf.control.executor import execution_validation as _execution_validation
from awf.control.executor import planning_artifacts as _planning_artifacts
from awf.control.executor import pr_open_step as _pr_open_step
from awf.control.executor.constants import GIT_OBJECT_MISSING_RECOVERED_REASON_CODE
from awf.control.executor.execution_pr_handoff import persist_pr_and_handoff
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
    _call_pr_monitor_factory,
    _failure_reason_for_phase,
    _profile_for_workspace,
    _provider_recovery_default_model_for_monitor_handoff,
)
from awf.control.executor.logging_ops import (
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    _setup_dependency_network_failure_details,
)
from awf.control.executor.mirror_hooks_repair import (
    MirrorHooksPathRepairAbortedError,
    repair_mirror_hooks_path_after_agent_cleanup_failure,
    repair_mirror_hooks_path_or_mark_failed,
)
from awf.control.executor.protocols import _MonitorRunnerProto
from awf.control.executor.quality_gates import (
    _classify_post_agent_commit_failure,
    _is_nothing_to_commit,
    _log,
    _PostAgentCommitStepError,
)
from awf.control.executor.recovery_payloads import (
    _get_active_recovery_payload,
    _planning_validation_handoff_from_metadata,
    _planning_validation_handoff_from_recovery_payload,
    _recovery_needs_existing_pr_push,
    _validate_only_recovery_target_head_sha,
)
from awf.control.executor.state_ops import _sync_resolved_profile
from awf.control.executor.supply_chain_messages import _supply_chain_block_message
from awf.control.executor.types import (
    _MonitorRebaseRecoveryError,
    _PlanningValidationHandoff,
    _RebaseRecoveryResult,
)
from awf.control.quality_gates import (
    find_protected_quality_gate_changes,
)
from awf.db.enums import (
    AgentRuntime,
    FailureReason,
    OperationStatus,
    WorkspaceStatus,
)
from awf.db.repositories import WorkspaceRepository
from awf.node.git_manager import (
    mirror_path_for_worktree,
    repair_mirror_hooks_path,
    verify_head_object_exists,
)
from awf.profiles.models import WorkspaceProfile
from awf.runtime.agent_scratch import apply_agent_scratch_excludes
from awf.runtime.ownership import (
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.validation import (
    ValidationResult,
)


async def execute(
    self: Any,
    workspace_id: str,
    *,
    execution_owner_id: str | None = None,
    execution_lease_expires_at: datetime | None = None,
    resume_from_blocked: bool = False,
) -> None:
    """Drive a ``ready`` workspace to ``completed`` (or ``failed``).

    The function is idempotent in the sense that it refuses to run on a
    workspace that is not currently in ``ready`` — useful when a poll
    loop races with a manual invocation.

    ``resume_from_blocked`` re-enters a workspace the worker already moved
    ``blocked -> running`` after an operator resolved a protected quality-gate
    violation; ``_begin_execution`` decides whether to re-run the agent (a
    revert/redo directive) or skip it (an approve-and-keep grant). Active
    operator grants are honored by every gate and consumed once the gate passes;
    if a protected violation still stands the gate re-blocks (bumping
    ``block_epoch``, invalidating the now-stale grants).
    """
    begin = await self._begin_execution(
        workspace_id,
        resume_from_blocked=resume_from_blocked,
        execution_owner_id=execution_owner_id,
        execution_lease_expires_at=execution_lease_expires_at,
    )
    if begin is None:
        return
    # ``baseline_coverage`` is reused on blocked-resume; the resume flags
    # independently gate the main agent and secondary fix passes.
    ws, resume_skip_agent, resume_disable_fix_passes, baseline_coverage = begin

    compose_file = (
        Path(ws.compose_file_path)
        if ws.compose_file_path
        else self._config.compose_projects_root / workspace_id / "compose.yml"
    )
    compose_project = ws.compose_project_name or f"awf_{workspace_id}"
    worktree_path = self._config.worktrees_root / workspace_id

    def _deposit_planning_artifacts() -> None:
        # Best-effort deposit before handlers return with a preserved FAILED worktree.
        _planning_artifacts._deposit_planning_artifacts_best_effort(
            self,
            profile=profile,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
        )

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
    post_agent_mirror_repair_done = False
    mirror_path = mirror_path_for_worktree(worktree_path)
    recovery_active = recovery is not None

    async def _repair_mirror_hooks_path_or_mark_failed(
        *,
        failure_stage: str,
        failure_from_status: WorkspaceStatus = WorkspaceStatus.running,
        before_mark_failed: Any = None,
    ) -> bool:
        return await repair_mirror_hooks_path_or_mark_failed(
            executor=self,
            workspace_id=workspace_id,
            mirror_path=mirror_path,
            repair_mirror_hooks_path_fn=repair_mirror_hooks_path,
            recovery_active=recovery_active,
            failure_stage=failure_stage,
            failure_from_status=failure_from_status,
            before_mark_failed=before_mark_failed,
        )

    async def _repair_mirror_hooks_path_after_cleanup_failure(
        *, failure_stage: str = "after agent cleanup failure"
    ) -> bool:
        return await repair_mirror_hooks_path_after_agent_cleanup_failure(
            executor=self,
            workspace_id=workspace_id,
            mirror_path=mirror_path,
            repair_mirror_hooks_path_fn=repair_mirror_hooks_path,
            recovery_active=recovery_active,
            failure_stage=failure_stage,
            before_mark_failed=_deposit_planning_artifacts,
        )

    async def _repair_hooks_after_agent_cleanup_failure() -> bool:
        return await _repair_mirror_hooks_path_after_cleanup_failure()

    async def _recover_missing_head_after_setup_cleanup_failure(
        exc: ComposeExecCleanupError,
    ) -> bool:
        if await verify_head_object_exists(worktree_path):
            return True
        recover_missing_head = getattr(self, "_recover_missing_git_head_or_mark_failed", None)
        if recover_missing_head is None:
            return False
        recovered = await recover_missing_head(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            base_commit=ws.base_commit,
            branch_name=expected_branch,
            from_status=WorkspaceStatus.running,
            stage="profile_setup_cleanup_failure",
            error=exc,
            task_tag=ws.task_tag,
            mark_failed_on_failure=False,
        )
        return bool(recovered)

    async def _recover_missing_head_after_agent_cleanup_failure(
        exc: ComposeExecCleanupError,
    ) -> bool:
        if await verify_head_object_exists(worktree_path):
            return True
        recover_missing_head = getattr(self, "_recover_missing_git_head_or_mark_failed", None)
        if recover_missing_head is None:
            return False
        if not await recover_missing_head(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            base_commit=ws.base_commit,
            branch_name=expected_branch,
            from_status=WorkspaceStatus.running,
            stage="agent_run_cleanup_failure",
            error=exc,
            task_tag=ws.task_tag,
            mark_failed_on_failure=False,
        ):
            return False
        recovered_commit_verified = await self._verify_recovered_post_agent_commit_or_mark_failed(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            base_commit=ws.base_commit,
            owned_paths=list(ws.owned_paths),
            expected_status=WorkspaceStatus.running,
            execution_owner_id=execution_owner_id,
            mark_failed_on_failure=False,
        )
        return bool(recovered_commit_verified)

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
        if not await _repair_mirror_hooks_path_or_mark_failed(failure_stage="before profile setup"):
            return
        try:
            setup_result = await self._validation.run_profile_phases(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                profile=profile,
                phase_names=("setup", "pre_agent"),
                worktree_path=worktree_path,
            )
        except ComposeExecCleanupError as exc:
            if not await _repair_mirror_hooks_path_after_cleanup_failure(
                failure_stage="after profile setup cleanup failure"
            ):
                return
            if not await _recover_missing_head_after_setup_cleanup_failure(exc):
                raise
            raise
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
            if not await _repair_mirror_hooks_path_or_mark_failed(
                failure_stage="after profile setup failure"
            ):
                return
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
        if not await _repair_mirror_hooks_path_or_mark_failed(
            failure_stage="after successful profile setup"
        ):
            return
        await self._record_runtime_toolchain_findings_safe(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            profile=profile,
        )
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
        if recovery is None and not resume_skip_agent:
            if not await self._run_agent_git_writability_preflight(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                worktree_path=worktree_path,
            ):
                return
            # For OpenCode/Ollama, discover + auto-pull the requested model
            # before the agent runs so OpenCode never rejects a daemon-served
            # model (issue #552). No-op for other runtimes; recovery runs skip
            # this block since the model was already ensured on the first run.
            if not await self._ensure_ollama_model_or_mark_failed(
                workspace_id=workspace_id,
                ws=ws,
            ):
                return
            # The git-writability preflight and the Ollama pull above can take a
            # long time (an absent model pull is bounded only by the pull
            # deadline, up to ~30 minutes). Recheck the status before the
            # baseline-coverage preflight — which itself runs the profile
            # coverage command — so a workspace cancelled during the pull stops
            # promptly instead of running baseline coverage before the agent-run
            # recheck below notices.
            if not await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.running,
                action="baseline_coverage_preflight",
            ):
                return
            try:
                baseline_coverage = await self._measure_and_persist_baseline_coverage(
                    workspace_id=workspace_id,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    profile=profile,
                    reuse=baseline_coverage,
                    skip_measure=resume_from_blocked,
                )
            except ComposeExecCleanupError:
                if not await _repair_mirror_hooks_path_after_cleanup_failure():
                    return
                raise
            if not await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.running,
                action="agent_run",
            ):
                return
            if not await _repair_mirror_hooks_path_or_mark_failed(
                failure_stage="before agent launch"
            ):
                return
            try:
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
                if not await _repair_mirror_hooks_path_or_mark_failed(
                    failure_stage="after agent run",
                    before_mark_failed=_deposit_planning_artifacts,
                ):
                    return
                post_agent_mirror_repair_done = True
            except ComposeExecCleanupError as exc:
                if not await _repair_hooks_after_agent_cleanup_failure():
                    return
                if not await _recover_missing_head_after_agent_cleanup_failure(exc):
                    raise
                raise
            except AgentRunError:
                raise
            except Exception:
                if not await _repair_mirror_hooks_path_or_mark_failed(
                    failure_stage="after agent run",
                    before_mark_failed=_deposit_planning_artifacts,
                ):
                    return
                post_agent_mirror_repair_done = True
                raise
            (
                planning_validation_handoff,
                planning_should_return,
            ) = await _planning_artifacts.handle_agent_planning_result(
                self,
                workspace_id=workspace_id,
                ws=ws,
                worktree_path=worktree_path,
                profile=profile,
                planning_failure=planning_failure,
            )
            if planning_should_return:
                return
            # Persist even ``None`` so approve-and-keep resumes preserve the
            # original post-validation conformance behavior.
            await self._persist_block_planning_conformance_handoff(
                workspace_id,
                handoff=planning_validation_handoff,
            )
        elif recovery is not None:
            # Move the recovery validate operation from pending to running before validation.
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
        elif resume_skip_agent:
            # Reconstruct the blocked run's persisted conformance handoff.
            planning_validation_handoff = _planning_validation_handoff_from_metadata(
                ws.block_planning_conformance_handoff
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
        # Deposit possible plan artifacts before returning with a FAILED workspace.
        _deposit_planning_artifacts()
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
        # Salvage uncommitted work after non-zero CLI exits; the later no-work
        # path preserves structured provider-failure metadata if salvage is empty.
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
            recover_missing_head = getattr(self, "_recover_missing_git_head_or_mark_failed", None)
            if recover_missing_head is not None and await recover_missing_head(
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
                # Recovery already marked FAILED; deposit possible plan artifacts now.
                _deposit_planning_artifacts()
                return
        else:
            _log.exception("executor.unexpected_in_agent", workspace_id=workspace_id)
            # Avoid stranding plan artifacts before returning with a FAILED workspace.
            _deposit_planning_artifacts()
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message=f"unexpected error during agent run: {exc!r}"[:2000],
            )
            return
    if (
        recovery is None
        and not resume_skip_agent
        and not post_agent_mirror_repair_done
        and not await _repair_mirror_hooks_path_or_mark_failed(
            failure_stage="after agent run",
            before_mark_failed=_deposit_planning_artifacts,
        )
    ):
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
        # The workspace transitioned out of ``running`` concurrently (e.g. a
        # cancel that preserves the worktree). Planning may already have
        # finished and written the plan + conformance report into the worktree,
        # but this skip returns before the post-validation deposit block.
        # Deposit them first, mirroring the agent-phase failure handlers above,
        # so the console can still surface them. Best-effort and idempotent.
        _deposit_planning_artifacts()
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
        # This invariant violation marks the workspace FAILED and returns
        # before the post-validation deposit block, stranding any plan +
        # conformance report the agent already wrote into the preserved-
        # FAILED worktree. Deposit them first, mirroring the agent-phase
        # failure handlers above. Best-effort and idempotent.
        _deposit_planning_artifacts()
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
                # Planning ran before this post-agent policy gate, so the
                # preserved FAILED worktree can already hold the plan +
                # conformance report. Deposit them BEFORE ``_mark_failed``
                # publishes the terminal status: the console keys its artifact
                # refetch on the workspace ``updated_at`` (TaskArtifactsSection
                # ``refreshKey``), and marking FAILED first would bump
                # ``updated_at`` and let a poll observe it in the window before
                # the deposit, record an empty artifact list, then never refetch
                # — hiding the Plan/Validation controls. Best-effort and
                # idempotent.
                _deposit_planning_artifacts()
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
                ):
                    # Plan-only output will mark the workspace FAILED. Planning
                    # ran before this gate, so the preserved worktree holds the
                    # plan + conformance report. Deposit them BEFORE
                    # ``_fail_if_plan_only_paths`` publishes the terminal status:
                    # the console keys its artifact refetch on the workspace
                    # ``updated_at`` (TaskArtifactsSection ``refreshKey``), and
                    # marking FAILED first would bump ``updated_at`` and let a
                    # poll observe it in the window before the deposit, record an
                    # empty artifact list, then never refetch — hiding the
                    # Plan/Validation controls. This branch returns before the
                    # post-validation deposit block. Best-effort and idempotent.
                    _deposit_planning_artifacts()
                    # The plan-only gate above already confirmed the staged
                    # delta is entirely internal plan artifacts, so this marks
                    # the workspace FAILED (PLAN_ONLY_OUTPUT) and returns True.
                    await self._fail_if_plan_only_paths(
                        workspace_id=workspace_id,
                        changed_paths=staged_paths,
                        expected_status=WorkspaceStatus.running,
                    )
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
                    operator_granted_paths=await self._active_operator_grant_specs(workspace_id),
                )
                if violations:
                    # A protected quality-gate edit outside owned_paths pauses
                    # the workspace for an operator decision instead of throwing
                    # away the spent work. Deposit the plan + conformance report
                    # BEFORE the block transition for the same artifact-ordering
                    # reason as the FAILED paths (the transition bumps
                    # ``updated_at``, which the console keys its refetch on).
                    # Best-effort and idempotent.
                    _deposit_planning_artifacts()
                    await self.enter_blocked_for_protected_violation(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.running,
                        violations=violations,
                        resume_phase="post_agent_commit",
                        execution_owner_id=execution_owner_id,
                    )
                    return
                commit_msg = commit_message_with_task_tag(
                    f"awf: {strip_leading_task_tag(ws.task_title, ws.task_tag)}", ws.task_tag
                )[:72]
                commit_body = f"Authored by AWF workspace {workspace_id} (agent: {ws.agent}).\n"

                async def _run_commit() -> CommandResult:
                    """Execute the post-agent ``git commit`` with AWF's identity."""
                    if not await _repair_mirror_hooks_path_or_mark_failed(
                        failure_stage="before post-agent commit",
                        before_mark_failed=_deposit_planning_artifacts,
                    ):
                        raise MirrorHooksPathRepairAbortedError
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
                                # An active grant keeps the approved protected
                                # change verbatim. Semantic pre-commit repair would
                                # re-invoke the agent and could rewrite that approved
                                # change, so gate it off whenever grants are active
                                # (``resume_disable_fix_passes`` — covers both the
                                # grant-only resume and a combined directive+grant
                                # resume), not just on upstream agent failures.
                                allow_agent_repair=(
                                    agent_run_failure_reason is None
                                    and not resume_disable_fix_passes
                                ),
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

                # Planning ran before this no-work check, so the preserved
                # FAILED worktree can already hold the plan + conformance
                # report even though the implementation produced no commits.
                # Deposit them BEFORE ``_mark_failed`` publishes the terminal
                # status: the console keys its artifact refetch on the
                # workspace ``updated_at`` (TaskArtifactsSection ``refreshKey``),
                # and marking FAILED first would bump ``updated_at`` and let a
                # poll observe it in the window before the deposit, record an
                # empty artifact list, then never refetch — hiding the
                # Plan/Validation controls. This branch returns before the
                # post-validation deposit block. Best-effort and idempotent.
                _deposit_planning_artifacts()
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

            # Reattach a severed feature branch to base before push/PR. A plain
            # ``return`` here (orphan recovery failed) bypasses the ``except``
            # deposit handlers below, so the helper deposits planning artifacts
            # before marking FAILED. See ``_recover_orphan_history``.
            if not await self._recover_orphan_history(
                workspace_id=workspace_id,
                ws=ws,
                base_commit=base_commit,
                worktree_path=worktree_path,
                git_in_worktree=_git_in_worktree,
                deposit_planning_artifacts=_deposit_planning_artifacts,
            ):
                return
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
                # Planning may have run before this monitor-rebase recovery, so
                # the preserved FAILED worktree can already hold the plan +
                # conformance report. Deposit them BEFORE ``_mark_failed``
                # publishes the terminal status: the console keys its artifact
                # refetch on the workspace ``updated_at`` (TaskArtifactsSection
                # ``refreshKey``), and marking FAILED first would bump
                # ``updated_at`` and let a poll observe it in the window before
                # the deposit, record an empty artifact list, then never refetch
                # — hiding the Plan/Validation controls. This branch returns from
                # inside the ``try`` before the post-validation deposit block,
                # and a plain ``return`` bypasses the ``except`` deposit handlers
                # below. Best-effort and idempotent.
                _deposit_planning_artifacts()
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.running,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=message,
                    reason_code="MONITOR_RECOVERY_REBASE_FAILED",
                )
                return
    except MirrorHooksPathRepairAbortedError:
        return
    except _PostAgentCommitStepError as exc:
        # Planning ran before the commit step, so the worktree (preserved on
        # the FAILED workspace) can already hold the plan + conformance report.
        # Deposit them BEFORE ``_mark_post_agent_commit_failed`` publishes the
        # terminal status: the console keys its artifact refetch on the
        # workspace ``updated_at`` (TaskArtifactsSection ``refreshKey``), and
        # marking FAILED first would bump ``updated_at`` and let a poll observe
        # it in the window before the deposit, record an empty artifact list,
        # then never refetch — hiding the Plan/Validation controls. Otherwise a
        # commit-step failure (e.g. a pre-commit hook rejecting the staged
        # changes) strands the artifacts in the worktree. Best-effort and
        # idempotent.
        _deposit_planning_artifacts()
        await self._mark_post_agent_commit_failed(
            workspace_id=workspace_id,
            error=exc,
            agent_run_reason_code=agent_run_reason_code,
            agent_run_details=agent_run_details,
            agent_exit_note=agent_exit_note,
            upstream_failure_reason=agent_run_failure_reason,
            execution_owner_id=execution_owner_id,
        )
        return
    except Exception as exc:  # unexpected — mark infrastructure
        # Planning ran before the commit step, so the worktree (preserved on the
        # FAILED workspace) can already hold the plan + conformance report. Every
        # failure-return path below marks the workspace FAILED and returns before
        # the post-validation deposit block, so deposit them now — otherwise an
        # unexpected commit-step error (e.g. a failed ``git rev-list`` or an
        # unrecoverable missing-HEAD) strands the artifacts in the worktree and
        # the console can never surface them. Best-effort and idempotent: the
        # recovery fall-through redeposits at the post-validation block.
        _deposit_planning_artifacts()
        if _git_error_indicates_missing_head_object(str(exc)):
            recover_missing_head = getattr(self, "_recover_missing_git_head_or_mark_failed", None)
            if recover_missing_head is not None:
                if not await recover_missing_head(
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    base_commit=base_commit,
                    branch_name=expected_branch,
                    from_status=WorkspaceStatus.running,
                    stage="post_agent_commit",
                    error=exc,
                    task_tag=ws.task_tag,
                ):
                    return
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
                    execution_owner_id=execution_owner_id,
                ):
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
        execution_owner_id=execution_owner_id,
        resume_disable_fix_passes=resume_disable_fix_passes,
    )
    if validation_result.stop:
        await _repair_mirror_hooks_path_or_mark_failed(
            failure_stage="after validation stop",
            failure_from_status=WorkspaceStatus.validating,
        )
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
            if not await _repair_mirror_hooks_path_or_mark_failed(
                failure_stage="before recovery skip-push handoff",
                failure_from_status=WorkspaceStatus.validating,
            ):
                return
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
    if not await _repair_mirror_hooks_path_or_mark_failed(
        failure_stage="before post-validation policy checks",
        failure_from_status=WorkspaceStatus.validating,
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
            execution_owner_id=execution_owner_id,
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

    if not await _repair_mirror_hooks_path_or_mark_failed(
        failure_stage="before PR push",
        failure_from_status=WorkspaceStatus.validating,
    ):
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

    if resume_from_blocked:
        # The protected gate passed with the operator's grants applied, so the
        # grants are now single-use spent: consume them so a later DIFFERENT
        # change to the same file must be granted again. This MUST run only
        # after the validating→pushing CAS above commits this validated change
        # to push. Consuming before the transition would mark the grants spent
        # even when the CAS loses (cancel, version race, stale status) and the
        # workspace never enters ``pushing`` — a later protected check on the
        # same resume would then re-block with no usable grant.
        await self._consume_active_operator_grants(workspace_id)
    if not await self._ensure_worktree_available(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        expected=WorkspaceStatus.pushing,
        action="pr_push_open",
    ):
        return

    pr = await _pr_open_step.push_and_open_pr(
        self,
        ws=ws,
        profile=profile,
        defaults=defaults,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
    )
    if pr is None:
        return

    # ── Step 4: persist PR URL + (optionally) hand off to monitor ──────
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
    )
