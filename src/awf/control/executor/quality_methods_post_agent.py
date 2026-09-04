"""Post-agent semantic repair and provider-recovery helpers for WorkspaceExecutor.

Mechanically extracted from ``quality_methods``; behavior unchanged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from awf.adapters.base import (
    AgentAdapter,
    AgentRunError,
)
from awf.common.audit import redact_audit_text
from awf.common.command_evidence import (
    append_command_evidence,
)
from awf.common.commands import CommandResult
from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.executor.agent_service_recovery import (
    _run_agent_callable_with_service_recovery,
)
from awf.control.executor.constants import (
    POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE,
    POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE,
    POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
    POST_AGENT_GIT_ADD_FAILED_REASON_CODE,
)
from awf.control.executor.git_ops import (
    _git_name_lines,
)
from awf.control.executor.hosted_validation_sync import (
    _sync_hosted_validation_fix_head,
)
from awf.control.executor.quality_gates import (
    _build_post_agent_precommit_repair_prompt,
    _classify_post_agent_commit_failure,
    _log,
    _PostAgentCommitClassification,
    _PostAgentCommitStepError,
)
from awf.control.executor.state_ops import ProviderFailureDivert
from awf.control.executor.supply_chain_messages import _supply_chain_block_message
from awf.control.quality_gates import (
    PLAN_ONLY_OUTPUT_REASON_CODE,
    QUALITY_GATE_POLICY_CHANGED_REASON_CODE,
    find_protected_quality_gate_changes,
    plan_only_output_message,
    quality_gate_violation_message,
)
from awf.db.enums import (
    AgentRuntime,
    FailureReason,
    WorkspaceStatus,
)
from awf.db.models import Workspace
from awf.db.repositories import (
    WorkspaceRepository,
)
from awf.profiles.models import WorkspaceProfile
from awf.service.provider_recovery import (
    create_provider_recovery_attempt_row,
)


async def _run_post_agent_semantic_precommit_repair(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    base_commit: str,
    commit_result: CommandResult,
    classification: _PostAgentCommitClassification,
    staged_paths: Sequence[str],
    run_commit: Callable[[], Awaitable[CommandResult]],
    git_in_worktree: Callable[[list[str]], Awaitable[CommandResult]],
    adapter: AgentAdapter,
    compose_project: str,
    compose_file: Path,
    model: str | None,
    ws: Workspace,
    profile: WorkspaceProfile,
    command_evidence: list[str],
    hosted_pr_identity: dict[str, Any] | None,
    execution_owner_id: str | None = None,
    before_mark_failed: Callable[[], None | Awaitable[None]] | None = None,
    before_agent_retry: Callable[[], Awaitable[bool | str]] | None = None,
    after_agent_cleanup_failure_repair: (
        Callable[[ComposeExecCleanupError], Awaitable[bool | str]] | None
    ) = None,
) -> bool:
    del commit_result
    prompt = _build_post_agent_precommit_repair_prompt(
        classification=classification,
        staged_paths=staged_paths,
    )
    repair_error: AgentRunError | None = None
    try:

        async def _run_repair_agent(_accept_existing_plan: bool) -> Any:
            return await adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
                model=model,
                workspace_id=workspace_id,
                log_source="post_agent_precommit_repair",
                hosted_pr_identity=hosted_pr_identity,
                profile=profile,
                worktree_path=worktree_path,
            )

        recovered, repair_result = await _run_agent_callable_with_service_recovery(
            self,
            run_agent=_run_repair_agent,
            adapter=adapter,
            workspace=ws,
            profile=profile,
            compose_project=compose_project,
            compose_file=compose_file,
            model=model,
            command_evidence=command_evidence,
            workspace_id=workspace_id,
            execution_owner_id=execution_owner_id,
            before_mark_failed=before_mark_failed,
            before_agent_retry=before_agent_retry,
            after_agent_cleanup_failure_repair=after_agent_cleanup_failure_repair,
            # Post-agent commit already holds the worktree writer lock across
            # repair; a nested acquire on the same flock would deadlock.
            hold_writer_lock=False,
        )
        if not recovered:
            return False
        append_command_evidence(
            command_evidence,
            stdout=repair_result.stdout,
            stderr=repair_result.stderr,
        )
        if getattr(adapter, "is_hosted", False):
            terminal_head_sha = getattr(repair_result, "terminal_head_sha", None)
            if not isinstance(terminal_head_sha, str) or not terminal_head_sha.strip():
                sync_result = CommandResult(
                    returncode=1,
                    stdout=repair_result.stdout,
                    stderr="hosted pre-commit repair completed without terminal_head_sha",
                    reason_code="HOSTED_REMOTE_HEAD_MISSING",
                )
            else:
                terminal_head_sha = terminal_head_sha.strip()
                sync_result = await _sync_hosted_validation_fix_head(
                    self,
                    worktree_path=worktree_path,
                    hosted_pr_identity=hosted_pr_identity,
                    terminal_head_sha=terminal_head_sha,
                )
            if not sync_result.ok:
                reason_code = sync_result.reason_code or "HOSTED_REMOTE_HEAD_SYNC_FAILED"
                await self._record_post_agent_commit_format_repair(
                    workspace_id=workspace_id,
                    repaired_paths=[],
                    restaged_paths=[],
                    formatter_paths=classification.format_repair_files,
                    normalizer_paths=classification.normalizer_repair_files,
                    failed_hooks=classification.failed_hooks,
                    repair_strategy="agent",
                    retry_outcome="error",
                    reason_code=reason_code,
                )
                raise _PostAgentCommitStepError(
                    stage="hosted terminal head sync",
                    result=sync_result,
                    classification=classification,
                    precommit_repair_attempted=True,
                    repair_strategy="agent",
                    reason_code_override=reason_code,
                )
            cast(dict[str, Any], hosted_pr_identity)["expected_head_sha"] = (
                sync_result.stdout.strip()
            )
    except AgentRunError as exc:
        repair_error = exc
        append_command_evidence(
            command_evidence,
            stdout=exc.result.stdout,
            stderr=exc.result.stderr,
        )
    if repair_error is not None and getattr(adapter, "is_hosted", False):
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=[],
            restaged_paths=[],
            formatter_paths=classification.format_repair_files,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="agent",
            retry_outcome="error",
            reason_code=classification.reason_code,
        )
        raise _PostAgentCommitStepError(
            stage="post-agent pre-commit repair",
            result=repair_error.result,
            classification=classification,
            precommit_repair_attempted=True,
            repair_strategy="agent",
            reason_code_override=POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE,
        ) from repair_error

    add_again = await git_in_worktree(["add", "-A"])
    await self._repair_agent_git_ownership(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="post_agent_precommit_repair_add",
    )
    if not add_again.ok:
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=[],
            restaged_paths=[],
            formatter_paths=classification.format_repair_files,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="agent",
            retry_outcome="error",
            reason_code=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
        )
        raise _PostAgentCommitStepError(
            stage="git add",
            result=add_again,
            classification=classification,
            precommit_repair_attempted=True,
            repair_strategy="agent",
            reason_code_override=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
        )

    cached = await git_in_worktree(["diff", "--cached", "--name-only"])
    if not cached.ok:
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=[],
            restaged_paths=[],
            formatter_paths=classification.format_repair_files,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="agent",
            retry_outcome="error",
            reason_code=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
        )
        raise _PostAgentCommitStepError(
            stage="git diff --cached",
            result=cached,
            classification=classification,
            precommit_repair_attempted=True,
            repair_strategy="agent",
            reason_code_override=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
        )
    repair_staged_paths = _git_name_lines(cached.stdout) if cached.stdout.strip() else []
    supply_chain_result = await self._refresh_supply_chain_policy_for_workspace(
        workspace_id=workspace_id,
        command_evidence=command_evidence,
        changed_paths=repair_staged_paths,
    )
    if supply_chain_result.policy_blocked:
        result = CommandResult(
            returncode=1,
            stdout="",
            stderr=_supply_chain_block_message(supply_chain_result.findings),
        )
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=[],
            restaged_paths=repair_staged_paths,
            formatter_paths=classification.format_repair_files,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="agent",
            retry_outcome="error",
            reason_code="SUPPLY_CHAIN_POLICY_BLOCKED",
        )
        raise _PostAgentCommitStepError(
            stage="post-agent pre-commit repair policy",
            result=result,
            classification=classification,
            precommit_repair_attempted=True,
            repair_strategy="agent",
            reason_code_override="SUPPLY_CHAIN_POLICY_BLOCKED",
            failure_reason_override=FailureReason.policy_failure,
        )
    # Always evaluate the final repair diff. A semantic repair can remove
    # the real implementation change while leaving only hook-normalized
    # plan artifacts staged, and that must not become a PR. Guard with the
    # committed-output helper (#427) so a repair that re-stages only a plan
    # artifact while real implementation is already committed in earlier
    # commits (net base..HEAD has real code) does not false-fire (#430). The
    # helper short-circuits (no git call) when the staged delta has real
    # content, and still returns True when nothing real is committed -- so a
    # no-op repair with empty base..HEAD continues to fail.
    if await self._committed_and_staged_output_is_plan_only(
        worktree_path=worktree_path,
        base_commit=base_commit,
        staged_paths=repair_staged_paths,
    ) and await self._fail_if_plan_only_paths(
        workspace_id=workspace_id,
        changed_paths=repair_staged_paths,
        expected_status=WorkspaceStatus.running,
        mark_workspace_failed=False,
    ):
        result = CommandResult(
            returncode=1,
            stdout="",
            stderr=plan_only_output_message(repair_staged_paths),
        )
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=[],
            restaged_paths=repair_staged_paths,
            formatter_paths=classification.format_repair_files,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="agent",
            retry_outcome="error",
            reason_code=PLAN_ONLY_OUTPUT_REASON_CODE,
        )
        raise _PostAgentCommitStepError(
            stage="post-agent pre-commit repair policy",
            result=result,
            classification=classification,
            precommit_repair_attempted=True,
            repair_strategy="agent",
            reason_code_override=PLAN_ONLY_OUTPUT_REASON_CODE,
            failure_reason_override=FailureReason.agent_failure,
        )
    violations = find_protected_quality_gate_changes(
        changed_paths=repair_staged_paths,
        owned_paths=list(ws.owned_paths),
        protected_file_diffs=await self._protected_file_diffs_for_staged_paths(
            worktree_path=worktree_path,
            base_ref=base_commit,
            changed_paths=repair_staged_paths,
            owned_paths=list(ws.owned_paths),
        ),
        operator_granted_paths=await self._active_operator_grant_specs(workspace_id),
    )
    if violations:
        result = CommandResult(
            returncode=1,
            stdout="",
            stderr=quality_gate_violation_message(violations),
        )
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=[],
            restaged_paths=repair_staged_paths,
            formatter_paths=classification.format_repair_files,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="agent",
            retry_outcome="error",
            reason_code="QUALITY_GATE_POLICY_CHANGED",
        )
        raise _PostAgentCommitStepError(
            stage="post-agent pre-commit repair policy",
            result=result,
            classification=classification,
            precommit_repair_attempted=True,
            repair_strategy="agent",
            reason_code_override="QUALITY_GATE_POLICY_CHANGED",
            failure_reason_override=FailureReason.policy_failure,
            protected_violations=violations,
        )

    retry_result = await run_commit()
    await self._repair_agent_git_ownership(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="post_agent_precommit_repair_commit",
    )
    if retry_result.ok:
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=[],
            restaged_paths=repair_staged_paths,
            formatter_paths=classification.format_repair_files,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="agent",
            retry_outcome=(
                "agent_error_partial_commit" if repair_error is not None else "succeeded"
            ),
            reason_code=classification.reason_code,
        )
        return True

    retry_classification = _classify_post_agent_commit_failure(retry_result)
    if retry_classification.repair_strategy == "deterministic" and repair_error is None:
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=[],
            restaged_paths=repair_staged_paths,
            formatter_paths=classification.format_repair_files,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="agent",
            retry_outcome="failed",
            reason_code=POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE,
        )
        await self._run_post_agent_deterministic_precommit_repair(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            commit_result=retry_result,
            classification=retry_classification,
            staged_paths=repair_staged_paths,
            run_commit=run_commit,
            git_in_worktree=git_in_worktree,
        )
        return True

    await self._record_post_agent_commit_format_repair(
        workspace_id=workspace_id,
        repaired_paths=[],
        restaged_paths=repair_staged_paths,
        formatter_paths=classification.format_repair_files,
        normalizer_paths=classification.normalizer_repair_files,
        failed_hooks=classification.failed_hooks,
        repair_strategy="agent",
        retry_outcome="error" if repair_error is not None else "failed",
        reason_code=classification.reason_code,
    )
    if repair_error is not None:
        raise _PostAgentCommitStepError(
            stage="post-agent pre-commit repair",
            result=repair_error.result,
            classification=classification,
            precommit_repair_attempted=True,
            repair_strategy="agent",
            reason_code_override=POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE,
        ) from repair_error
    raise _PostAgentCommitStepError(
        stage="git commit",
        result=retry_result,
        classification=retry_classification,
        precommit_repair_attempted=True,
        repair_strategy="agent",
    )


async def _mark_post_agent_commit_failed(
    self: Any,
    *,
    workspace_id: str,
    error: _PostAgentCommitStepError,
    agent_run_reason_code: str | None,
    agent_run_details: Mapping[str, Any] | None,
    agent_exit_note: str | None,
    upstream_failure_reason: FailureReason | None,
    execution_owner_id: str | None = None,
) -> None:
    """Route a ``_PostAgentCommitStepError`` to ``_mark_failed`` with
    structured reason codes.

    When the agent already failed upstream (e.g.
    ``AgentRunError(reason_code=AGENT_IDLE_TIMEOUT)``), the agent's
    reason code AND ``FailureReason.agent_failure`` classification
    win on the terminal event so the workspace mirrors the no-commit
    agent failure path. The commit-step diagnostics live under
    ``details["post_agent_commit"]`` for observability without
    overwriting the original classification.

    ``upstream_failure_reason`` is the explicit signal for that
    branch. ``agent_run_reason_code`` alone is not sufficient: the
    recovered missing-HEAD path also sets a reason code
    (``GIT_OBJECT_MISSING_RECOVERED``), but its semantics are
    git/infrastructure recovery, not an agent/provider failure — so
    a downstream commit failure must NOT be re-classified as
    ``agent_failure`` and MUST NOT queue provider recovery.
    """
    # A protected quality-gate violation surfaced by the semantic pre-commit
    # repair gate pauses the workspace for an operator decision instead of
    # terminally failing — but only when there is no genuine upstream agent
    # failure (a real agent failure is not operator-resolvable here).
    if (
        error.reason_code_override == QUALITY_GATE_POLICY_CHANGED_REASON_CODE
        and error.protected_violations
        and upstream_failure_reason != FailureReason.agent_failure
    ):
        await self.enter_blocked_for_protected_violation(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            violations=error.protected_violations,
            resume_phase="post_agent_precommit_repair",
            execution_owner_id=execution_owner_id,
        )
        return

    classification = error.classification
    if error.reason_code_override is not None:
        commit_reason_code = error.reason_code_override
    elif classification is not None:
        commit_reason_code = classification.reason_code
    else:
        commit_reason_code = POST_AGENT_GIT_ADD_FAILED_REASON_CODE
    commit_details: dict[str, Any] = {
        "stage": error.stage,
        "reason_code": commit_reason_code,
        "returncode": error.result.returncode,
        "format_repair_attempted": error.format_repair_attempted,
        "precommit_repair_attempted": error.precommit_repair_attempted,
    }
    if error.repair_strategy:
        commit_details["repair_strategy"] = error.repair_strategy
    if classification is not None:
        if classification.failed_hooks:
            commit_details["failed_hooks"] = list(classification.failed_hooks)
        if classification.format_repair_files:
            commit_details["format_repair_files"] = list(classification.format_repair_files)
        if classification.normalizer_repair_files:
            commit_details["normalizer_repair_files"] = list(classification.normalizer_repair_files)
        if classification.deterministic_hooks:
            commit_details["deterministic_hooks"] = list(classification.deterministic_hooks)
        if classification.semantic_hooks:
            commit_details["semantic_hooks"] = list(classification.semantic_hooks)
    # ``classification`` holds the parsed output for the FAILING commit step:
    # - for ``ruff format`` crashes and post-format ``git add`` failures it
    #   is the FIRST ``git commit`` output (and stays stale — "Would
    #   reformat..." — even though the real failure is elsewhere).
    # - for retry-commit failures it is ``retry_classification`` (parsed from
    #   the retry output), so ``failed_hooks`` / ``format_repair_files``
    #   reflect the retry, not the initial commit.
    # Trust the classification summary only when this is a commit-stage
    # failure with no override; otherwise prefer the actual sub-step output.
    if (
        classification is not None
        and error.stage == "git commit"
        and error.reason_code_override is None
    ):
        summary = classification.summary
    else:
        summary = (error.result.stderr or error.result.stdout or "").strip()
    if summary:
        commit_details["summary"] = redact_audit_text(summary, limit=1000)

    if upstream_failure_reason == FailureReason.agent_failure:
        details: dict[str, Any] = dict(agent_run_details or {})
        details["post_agent_commit"] = commit_details
        base_message = f"post-agent {error.stage} failed (exit={error.result.returncode})"
        summary_text = commit_details.get("summary")
        if summary_text:
            base_message = f"{base_message}: {summary_text}"
        if agent_exit_note is not None:
            base_message = f"{base_message}; {agent_exit_note}"
        # Same divert as the no-commits agent-failure fork: a retryable provider
        # failure with budget remaining pauses into ``recovering`` for in-place
        # retry BEFORE the terminal teardown, instead of fail-and-relaunch (#612).
        divert = await self.enter_recovering_for_provider_failure(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            message=base_message[:2000],
            reason_code=agent_run_reason_code,
            details=details,
            execution_owner_id=execution_owner_id,
        )
        # ``paused`` (entered ``recovering`` for in-place retry) or ``fenced`` (a
        # newer claimant holds the running row) both skip the terminal teardown:
        # the fenced skip stops a stale executor from driving a newer claimant's
        # running row to ``failed`` through the non-owner-gated ``_mark_failed``
        # below (the D7 terminal-CAS fence the provisioner already applies, #421).
        if divert is not ProviderFailureDivert.terminal:
            return
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.agent_failure,
            message=base_message[:2000],
            reason_code=agent_run_reason_code,
            details=details,
        )
        await self._prepare_provider_recovery(workspace_id)
        return

    _log.warning(
        "executor.post_agent_commit_failed",
        workspace_id=workspace_id,
        stage=error.stage,
        reason_code=commit_reason_code,
        returncode=error.result.returncode,
        format_repair_attempted=error.format_repair_attempted,
    )
    failure_reason = error.failure_reason_override or FailureReason.infrastructure_failure
    base_message = f"post-agent {error.stage} failed (exit={error.result.returncode})"
    summary_text = commit_details.get("summary")
    if summary_text:
        base_message = f"{base_message}: {summary_text}"
    await self._mark_failed(
        workspace_id=workspace_id,
        from_status=WorkspaceStatus.running,
        failure_reason=failure_reason,
        message=base_message[:2000],
        reason_code=commit_reason_code,
        details={"post_agent_commit": commit_details},
    )


async def _prepare_provider_recovery(self: Any, workspace_id: str) -> None:
    async with self._session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        configured_default_model: str | None = None
        if workspace is not None:
            try:
                defaults = self._defaults_for(AgentRuntime(workspace.agent))
            except ValueError:
                defaults = None
            configured_default_model = defaults.model if defaults is not None else None
        result = await create_provider_recovery_attempt_row(
            session,
            workspace_id,
            effective_default_model=configured_default_model,
        )
        if result is None or result == "terminal" or result == "stale":
            await session.commit()
            return
        await session.commit()
        _log.info(
            "executor.provider_recovery_prepared",
            workspace_id=workspace_id,
            new_workspace_id=result.new_workspace_id,
            action=result.action,
            reason_code=result.reason_code,
        )
