"""Extracted WorkspaceExecutor domain operations.

This module contains mechanically moved methods from ``awf.control.executor.base`` and keeps behavior unchanged.
"""

from __future__ import annotations

import asyncio as asyncio
import hashlib as hashlib
import json as json
import re as re
import shlex as shlex
import time as time
import traceback as traceback
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from awf.adapters.base import (
    AgentAdapter,
)
from awf.common.commands import CommandResult
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.git_identity import (
    git_safe_directory_config_args,
)
from awf.control.executor import quality_methods_post_agent as _quality_methods_post_agent
from awf.control.executor.constants import (
    _AWF_RUFF_FORMAT_CHECK_HOOK_ID,
    GIT_OBJECT_MISSING_REASON_CODE,
    GIT_OBJECT_MISSING_RECOVERED_REASON_CODE,
    POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
    POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE,
    POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
)
from awf.control.executor.quality_gates import (
    _classify_post_agent_commit_failure,
    _log,
    _PostAgentCommitClassification,
    _PostAgentCommitStepError,
)
from awf.control.executor.types import _CoverageEvidenceResult
from awf.control.protected_file_diffs import (
    committed_changed_paths_since,
    protected_file_diffs_for_committed_paths,
)
from awf.control.quality_gates import (
    PLAN_ONLY_OUTPUT_REASON_CODE,
    changed_paths_are_only_internal_plan_artifacts,
    find_protected_quality_gate_changes,
    plan_only_output_message,
)
from awf.db.enums import (
    FailureReason,
    WorkspaceStatus,
)
from awf.db.models import Workspace
from awf.db.repositories import (
    WorkspaceRepository,
)
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import ValidationCoverageResult

# Re-export for mixin binding via ``quality_methods`` module attributes.
_mark_post_agent_commit_failed = _quality_methods_post_agent._mark_post_agent_commit_failed
_prepare_provider_recovery = _quality_methods_post_agent._prepare_provider_recovery
_run_post_agent_semantic_precommit_repair = (
    _quality_methods_post_agent._run_post_agent_semantic_precommit_repair
)


async def _run_baseline_coverage_preflight(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    profile: WorkspaceProfile,
    worktree_path: Path | None = None,
    coverage_runner: Any | None = None,
    coverage_run_kwargs: Mapping[str, Any] | None = None,
) -> ValidationCoverageResult | None:
    from awf.control.executor.quality_gates import _run_baseline_coverage_preflight

    return await _run_baseline_coverage_preflight(
        self,
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
        profile=profile,
        worktree_path=worktree_path,
        coverage_runner=coverage_runner,
        coverage_run_kwargs=coverage_run_kwargs,
    )


async def _measure_and_persist_baseline_coverage(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    profile: WorkspaceProfile,
    reuse: ValidationCoverageResult | None = None,
    skip_measure: bool = False,
    worktree_path: Path | None = None,
    coverage_runner: Any | None = None,
    coverage_run_kwargs: Mapping[str, Any] | None = None,
) -> ValidationCoverageResult | None:
    """Measure the pre-agent baseline coverage and persist it for blocked-resume.

    On a fresh run the measurement reflects base coverage (the agent has not yet
    mutated the worktree); persisting it lets a later pause into ``blocked`` hand
    the original base back to the resume path. A directive resume passes
    ``skip_measure=True`` to keep the already-reused base (``reuse``) instead of
    recomputing against the mutated blocked worktree. Hosted PR adoption supplies
    its validation delegate and PR identity through the optional coverage runner
    arguments.
    """
    if skip_measure:
        return reuse
    baseline_coverage: (
        ValidationCoverageResult | None
    ) = await self._run_baseline_coverage_preflight(
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
        profile=profile,
        worktree_path=worktree_path,
        coverage_runner=coverage_runner,
        coverage_run_kwargs=coverage_run_kwargs,
    )
    await self._persist_block_baseline_coverage(
        workspace_id,
        baseline_coverage=baseline_coverage,
    )
    return baseline_coverage


async def _measure_and_persist_symlink_form_baseline(
    self: Any,
    *,
    workspace_id: str,
    run_git: Any,
    worktree_path: Path,
    reuse: bool | None = None,
    skip_measure: bool = False,
) -> bool | None:
    """Capture pre-agent symlink checkout mode and persist for blocked-resume.

    On a fresh run the measurement reflects checkout capability before the agent
    can mutate ``core.symlinks`` or on-disk link form. A blocked resume passes
    ``skip_measure=True`` to keep the already-persisted baseline instead of
    re-reading agent-mutable paths.
    """
    if skip_measure:
        return reuse
    from awf.runtime.validation_worktree import read_validation_worktree_symlink_form_baseline

    index_symlinks_are_symlinks = await read_validation_worktree_symlink_form_baseline(
        run_git,
        worktree_path,
    )
    await self._persist_block_index_symlinks_are_symlinks(
        workspace_id,
        index_symlinks_are_symlinks=index_symlinks_are_symlinks,
    )
    return index_symlinks_are_symlinks


async def _run_final_coverage_gate(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    profile: WorkspaceProfile,
    validation_tier: int,
    workspace_head_sha: str | None,
    phase_names: tuple[str, ...] = ("post_agent", "validate"),
    use_hosted_command_plan: bool = False,
    worktree_path: Path | None = None,
    coverage_runner: Any | None = None,
    coverage_run_kwargs: Mapping[str, Any] | None = None,
) -> _CoverageEvidenceResult:
    """Delegate final coverage-gate execution to the quality-gates module."""
    from awf.control.executor.quality_gates import _run_final_coverage_gate

    return await _run_final_coverage_gate(
        self,
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
        profile=profile,
        validation_tier=validation_tier,
        workspace_head_sha=workspace_head_sha,
        phase_names=phase_names,
        use_hosted_command_plan=use_hosted_command_plan,
        worktree_path=worktree_path,
        coverage_runner=coverage_runner,
        coverage_run_kwargs=coverage_run_kwargs,
    )


async def _verify_recovered_post_agent_commit(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    base_commit: str,
    owned_paths: list[str],
    expected_status: WorkspaceStatus,
    execution_owner_id: str | None = None,
    mark_failed_on_failure: bool = True,
) -> bool:
    changed_paths = sorted(
        await committed_changed_paths_since(
            self._runner,
            worktree_path=worktree_path,
            base_ref=base_commit,
        )
    )
    if not changed_paths:
        if mark_failed_on_failure:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=expected_status,
                failure_reason=FailureReason.agent_failure,
                message=(
                    "AWF recovered a missing Git HEAD object but recovered no "
                    f"committed paths relative to base {base_commit[:10]}"
                )[:2000],
                reason_code=GIT_OBJECT_MISSING_RECOVERED_REASON_CODE,
                details={"recovered_stage": "post_agent_commit"},
            )
        return False
    if mark_failed_on_failure and await self._fail_if_plan_only_paths(
        workspace_id=workspace_id,
        changed_paths=changed_paths,
        expected_status=expected_status,
    ):
        return False
    protected_file_diffs = await protected_file_diffs_for_committed_paths(
        self._runner,
        worktree_path=worktree_path,
        base_ref=base_commit,
        changed_paths=changed_paths,
        owned_paths=owned_paths,
    )
    violations = find_protected_quality_gate_changes(
        changed_paths=changed_paths,
        owned_paths=owned_paths,
        protected_file_diffs=protected_file_diffs,
        operator_granted_paths=await self._active_operator_grant_specs(workspace_id),
    )
    if violations:
        # Pause for an operator decision instead of terminally failing — the
        # recovered commit + worktree are preserved through the block.
        if mark_failed_on_failure:
            await self.enter_blocked_for_protected_violation(
                workspace_id=workspace_id,
                from_status=expected_status,
                violations=violations,
                resume_phase="post_agent_commit_recovery_verify",
                execution_owner_id=execution_owner_id,
            )
        return False
    ancestor = await self._runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "merge-base",
            "--is-ancestor",
            base_commit,
            "HEAD",
        ]
    )
    if not ancestor.ok:
        if mark_failed_on_failure:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=expected_status,
                failure_reason=FailureReason.agent_failure,
                message=(
                    "AWF recovered a missing Git HEAD object but recovered HEAD "
                    f"does not descend from base commit {base_commit[:10]}"
                )[:2000],
            )
        return False
    return True


async def _verify_recovered_post_agent_commit_or_mark_failed(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    base_commit: str,
    owned_paths: list[str],
    expected_status: WorkspaceStatus,
    execution_owner_id: str | None = None,
    mark_failed_on_failure: bool = True,
) -> bool:
    try:
        return cast(
            bool,
            await self._verify_recovered_post_agent_commit(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                base_commit=base_commit,
                owned_paths=owned_paths,
                expected_status=expected_status,
                execution_owner_id=execution_owner_id,
                mark_failed_on_failure=mark_failed_on_failure,
            ),
        )
    except Exception as exc:
        _log.exception(
            "executor.commit_step_missing_head_recovery_verification_failed",
            workspace_id=workspace_id,
        )
        if mark_failed_on_failure:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=expected_status,
                failure_reason=FailureReason.infrastructure_failure,
                message=(f"post-agent missing HEAD recovery verification failed: {exc!r}")[:2000],
                reason_code=GIT_OBJECT_MISSING_REASON_CODE,
            )
        return False


async def _fail_if_plan_only_paths(
    self: Any,
    *,
    workspace_id: str,
    changed_paths: list[str] | tuple[str, ...],
    expected_status: WorkspaceStatus,
    mark_workspace_failed: bool = True,
) -> bool:
    if not changed_paths_are_only_internal_plan_artifacts(changed_paths):
        return False
    if not mark_workspace_failed:
        return True
    await self._mark_failed(
        workspace_id=workspace_id,
        from_status=expected_status,
        failure_reason=FailureReason.agent_failure,
        message=plan_only_output_message(changed_paths)[:2000],
        reason_code=PLAN_ONLY_OUTPUT_REASON_CODE,
        details={
            "changed_paths": list(changed_paths),
            "reason_code": PLAN_ONLY_OUTPUT_REASON_CODE,
        },
    )
    return True


async def _committed_and_staged_output_is_plan_only(
    self: Any,
    *,
    worktree_path: Path,
    base_commit: str,
    staged_paths: list[str] | tuple[str, ...],
) -> bool:
    """True only when the staged delta is entirely internal plan artifacts AND
    the already-committed net output (base..HEAD) is also plan-only (or empty)
    -- i.e. the workspace has produced no real implementation/test/doc output
    anywhere. Gates PLAN_ONLY_OUTPUT so a fix-pass that stages only the
    conformance artifact cannot false-fail a workspace whose real work is in
    earlier commits. Mirrors the post-agent guard in execution_flow."""
    if not changed_paths_are_only_internal_plan_artifacts(staged_paths):
        return False
    committed_paths = sorted(
        p.as_posix() for p in await self._committed_paths_since(worktree_path, base_commit)
    )
    return not committed_paths or changed_paths_are_only_internal_plan_artifacts(committed_paths)


async def _fail_if_plan_only_committed_output(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    base_commit: str,
    expected_status: WorkspaceStatus,
) -> bool:
    changed_paths = sorted(
        path.as_posix() for path in await self._committed_paths_since(worktree_path, base_commit)
    )
    if not changed_paths:
        # An empty net diff (``base..HEAD`` touches no paths) means the branch
        # has no implementation output to push -- e.g. a validation/fix pass
        # reverted the agent's real changes back to the base tree. The
        # post-agent no-work check counts *commits* (``rev-list --count``), so a
        # revert commit still passes it; this final gate is the last guard
        # before push, so treat an empty net diff as terminal output failure
        # rather than opening an empty PR. ``changed_paths_are_only_internal_plan_artifacts``
        # returns ``False`` for an empty list, so the delegate below cannot
        # catch this case on its own.
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=expected_status,
            failure_reason=FailureReason.agent_failure,
            message=(
                "agent produced no net implementation output -- the feature "
                f"branch's diff against base ({base_commit[:10]}) is empty "
                "(changes were reverted during validation/repair). AWF will not "
                "open an empty PR until the branch contains implementation, "
                "test, or user-facing documentation output for the task."
            )[:2000],
            reason_code=PLAN_ONLY_OUTPUT_REASON_CODE,
            details={
                "changed_paths": [],
                "reason_code": PLAN_ONLY_OUTPUT_REASON_CODE,
            },
        )
        return True
    return cast(
        bool,
        await self._fail_if_plan_only_paths(
            workspace_id=workspace_id,
            changed_paths=changed_paths,
            expected_status=expected_status,
        ),
    )


async def _fail_if_protected_quality_gate_committed_output(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    base_commit: str,
    owned_paths: list[str],
    expected_status: WorkspaceStatus,
    execution_owner_id: str | None = None,
) -> bool:
    changed_paths = sorted(
        await committed_changed_paths_since(
            self._runner,
            worktree_path=worktree_path,
            base_ref=base_commit,
        )
    )
    if not changed_paths:
        return False
    protected_file_diffs = await protected_file_diffs_for_committed_paths(
        self._runner,
        worktree_path=worktree_path,
        base_ref=base_commit,
        changed_paths=changed_paths,
        owned_paths=owned_paths,
    )
    violations = find_protected_quality_gate_changes(
        changed_paths=changed_paths,
        owned_paths=owned_paths,
        protected_file_diffs=protected_file_diffs,
        operator_granted_paths=await self._active_operator_grant_specs(workspace_id),
    )
    if not violations:
        return False
    # Pause for an operator decision instead of terminally failing.
    await self.enter_blocked_for_protected_violation(
        workspace_id=workspace_id,
        from_status=expected_status,
        violations=violations,
        resume_phase="post_agent_commit_committed_output",
        execution_owner_id=execution_owner_id,
    )
    return True


async def _record_post_agent_commit_format_repair(
    self: Any,
    *,
    workspace_id: str,
    repaired_paths: Sequence[str],
    retry_outcome: str,
    repair_strategy: str = "deterministic",
    failed_hooks: Sequence[str] = (),
    formatter_paths: Sequence[str] = (),
    normalizer_paths: Sequence[str] = (),
    restaged_paths: Sequence[str] = (),
    reason_code: str,
) -> None:
    """Emit the structured event describing a post-agent commit repair."""
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None:  # pragma: no cover - destroyed mid-flight
            return
        await repo.add_event(
            ws,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
            reason_code=reason_code,
            payload={
                "repaired_paths": list(repaired_paths),
                "restaged_paths": list(restaged_paths),
                "formatter_paths": list(formatter_paths),
                "normalizer_paths": list(normalizer_paths),
                "failed_hooks": list(failed_hooks),
                "repair_strategy": repair_strategy,
                "retry_outcome": retry_outcome,
            },
        )
        await session.commit()


async def _run_post_agent_commit_repair(
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
    allow_agent_repair: bool,
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
    """Repair a failed post-agent pre-commit run and retry the commit once."""
    if classification.repair_strategy == "deterministic":
        await self._run_post_agent_deterministic_precommit_repair(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            commit_result=commit_result,
            classification=classification,
            staged_paths=staged_paths,
            run_commit=run_commit,
            git_in_worktree=git_in_worktree,
        )
        return True

    if classification.autofix_repair_files:
        repaired = await self._run_post_agent_autofixable_precommit_repair(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            commit_result=commit_result,
            classification=classification,
            staged_paths=staged_paths,
            run_commit=run_commit,
            git_in_worktree=git_in_worktree,
        )
        if repaired:
            return True

    if classification.repair_strategy == "agent" and allow_agent_repair:
        return cast(
            bool,
            await self._run_post_agent_semantic_precommit_repair(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                base_commit=base_commit,
                commit_result=commit_result,
                classification=classification,
                staged_paths=staged_paths,
                run_commit=run_commit,
                git_in_worktree=git_in_worktree,
                adapter=adapter,
                compose_project=compose_project,
                compose_file=compose_file,
                model=model,
                ws=ws,
                profile=profile,
                command_evidence=command_evidence,
                hosted_pr_identity=hosted_pr_identity,
                execution_owner_id=execution_owner_id,
                before_mark_failed=before_mark_failed,
                before_agent_retry=before_agent_retry,
                after_agent_cleanup_failure_repair=after_agent_cleanup_failure_repair,
            ),
        )

    reported_repair_strategy = (
        "agent_skipped"
        if classification.repair_strategy == "agent" and not allow_agent_repair
        else classification.repair_strategy
    )
    raise _PostAgentCommitStepError(
        stage="git commit",
        result=commit_result,
        classification=classification,
        repair_strategy=reported_repair_strategy,
    )


async def _run_post_agent_deterministic_precommit_repair(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    commit_result: CommandResult,
    classification: _PostAgentCommitClassification,
    staged_paths: Sequence[str],
    run_commit: Callable[[], Awaitable[CommandResult]],
    git_in_worktree: Callable[[list[str]], Awaitable[CommandResult]],
) -> None:
    staged_python_set = {
        path for path in staged_paths if path.endswith(".py") or path.endswith(".pyi")
    }
    repair_paths = [
        path for path in classification.format_repair_files if path in staged_python_set
    ]
    if _AWF_RUFF_FORMAT_CHECK_HOOK_ID in classification.failed_hooks and not repair_paths:
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=[],
            restaged_paths=[],
            formatter_paths=list(classification.format_repair_files),
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="deterministic",
            retry_outcome="skipped",
            reason_code=classification.reason_code,
        )
        raise _PostAgentCommitStepError(
            stage="git commit",
            result=commit_result,
            classification=classification,
            format_repair_attempted=True,
            precommit_repair_attempted=True,
            repair_strategy="deterministic",
        )

    if repair_paths:
        format_result = await self._runner.run(
            [
                "uv",
                "run",
                "--python",
                "3.12",
                "--extra",
                "dev",
                "ruff",
                "format",
                "--",
                *repair_paths,
            ],
            cwd=str(worktree_path),
        )
        if not format_result.ok:
            await self._record_post_agent_commit_format_repair(
                workspace_id=workspace_id,
                repaired_paths=repair_paths,
                restaged_paths=[],
                formatter_paths=repair_paths,
                normalizer_paths=classification.normalizer_repair_files,
                failed_hooks=classification.failed_hooks,
                repair_strategy="deterministic",
                retry_outcome="error",
                reason_code=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
            )
            raise _PostAgentCommitStepError(
                stage="ruff format",
                result=format_result,
                classification=classification,
                format_repair_attempted=True,
                precommit_repair_attempted=True,
                repair_strategy="deterministic",
                reason_code_override=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
            )

    restage_paths = list(staged_paths)
    add_again = await git_in_worktree(["add", "--", *restage_paths])
    await self._repair_agent_git_ownership(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="post_agent_format_repair_add",
    )
    if not add_again.ok:
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=repair_paths,
            restaged_paths=restage_paths,
            formatter_paths=repair_paths,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="deterministic",
            retry_outcome="error",
            reason_code=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
        )
        raise _PostAgentCommitStepError(
            stage="git add",
            result=add_again,
            classification=classification,
            format_repair_attempted=True,
            precommit_repair_attempted=True,
            repair_strategy="deterministic",
            reason_code_override=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
        )
    retry_result = await run_commit()
    await self._repair_agent_git_ownership(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="post_agent_format_repair_commit",
    )
    if retry_result.ok:
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=repair_paths,
            restaged_paths=restage_paths,
            formatter_paths=repair_paths,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="deterministic",
            retry_outcome="succeeded",
            reason_code=classification.reason_code,
        )
        return

    retry_classification = _classify_post_agent_commit_failure(retry_result)
    await self._record_post_agent_commit_format_repair(
        workspace_id=workspace_id,
        repaired_paths=repair_paths,
        restaged_paths=restage_paths,
        formatter_paths=repair_paths,
        normalizer_paths=classification.normalizer_repair_files,
        failed_hooks=classification.failed_hooks,
        repair_strategy="deterministic",
        retry_outcome="failed",
        reason_code=classification.reason_code,
    )
    raise _PostAgentCommitStepError(
        stage="git commit",
        result=retry_result,
        classification=retry_classification,
        format_repair_attempted=True,
        precommit_repair_attempted=True,
        repair_strategy="deterministic",
        reason_code_override=(
            POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
            if retry_classification.reason_code
            == POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE
            else None
        ),
    )


async def _run_post_agent_autofixable_precommit_repair(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    commit_result: CommandResult,
    classification: _PostAgentCommitClassification,
    staged_paths: Sequence[str],
    run_commit: Callable[[], Awaitable[CommandResult]],
    git_in_worktree: Callable[[list[str]], Awaitable[CommandResult]],
) -> bool:
    del commit_result
    staged_python_set = {
        path for path in staged_paths if path.endswith(".py") or path.endswith(".pyi")
    }
    repair_paths = [
        path for path in classification.autofix_repair_files if path in staged_python_set
    ]
    format_repair_paths = [
        path for path in classification.format_repair_files if path in staged_python_set
    ]
    if not repair_paths:
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=[],
            restaged_paths=[],
            formatter_paths=format_repair_paths,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="deterministic_autofix",
            retry_outcome="skipped",
            reason_code=classification.reason_code,
        )
        return False

    fix_result = await self._runner.run(
        [
            "uv",
            "run",
            "--python",
            "3.12",
            "--extra",
            "dev",
            "ruff",
            "check",
            "--fix",
            "--",
            *repair_paths,
        ],
        cwd=str(worktree_path),
    )
    if not fix_result.ok:
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=repair_paths,
            restaged_paths=[],
            formatter_paths=format_repair_paths,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="deterministic_autofix",
            retry_outcome="error",
            reason_code=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
        )
        raise _PostAgentCommitStepError(
            stage="ruff check --fix",
            result=fix_result,
            classification=classification,
            format_repair_attempted=True,
            precommit_repair_attempted=True,
            repair_strategy="deterministic_autofix",
            reason_code_override=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
        )

    format_paths = sorted(set(format_repair_paths) | set(repair_paths))
    format_result = await self._runner.run(
        [
            "uv",
            "run",
            "--python",
            "3.12",
            "--extra",
            "dev",
            "ruff",
            "format",
            "--",
            *format_paths,
        ],
        cwd=str(worktree_path),
    )
    if not format_result.ok:
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=repair_paths,
            restaged_paths=[],
            formatter_paths=format_paths,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="deterministic_autofix",
            retry_outcome="error",
            reason_code=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
        )
        raise _PostAgentCommitStepError(
            stage="ruff format",
            result=format_result,
            classification=classification,
            format_repair_attempted=True,
            precommit_repair_attempted=True,
            repair_strategy="deterministic_autofix",
            reason_code_override=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
        )

    restage_paths = list(staged_paths)
    add_again = await git_in_worktree(["add", "--", *restage_paths])
    await self._repair_agent_git_ownership(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="post_agent_autofix_repair_add",
    )
    if not add_again.ok:
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=repair_paths,
            restaged_paths=restage_paths,
            formatter_paths=format_paths,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="deterministic_autofix",
            retry_outcome="error",
            reason_code=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
        )
        raise _PostAgentCommitStepError(
            stage="git add",
            result=add_again,
            classification=classification,
            format_repair_attempted=True,
            precommit_repair_attempted=True,
            repair_strategy="deterministic_autofix",
            reason_code_override=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
        )

    retry_result = await run_commit()
    await self._repair_agent_git_ownership(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="post_agent_autofix_repair_commit",
    )
    if retry_result.ok:
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=repair_paths,
            restaged_paths=restage_paths,
            formatter_paths=format_paths,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="deterministic_autofix",
            retry_outcome="succeeded",
            reason_code=classification.reason_code,
        )
        return True

    retry_classification = _classify_post_agent_commit_failure(retry_result)
    await self._record_post_agent_commit_format_repair(
        workspace_id=workspace_id,
        repaired_paths=repair_paths,
        restaged_paths=restage_paths,
        formatter_paths=format_paths,
        normalizer_paths=classification.normalizer_repair_files,
        failed_hooks=classification.failed_hooks,
        repair_strategy="deterministic_autofix",
        retry_outcome="failed",
        reason_code=classification.reason_code,
    )
    raise _PostAgentCommitStepError(
        stage="git commit",
        result=retry_result,
        classification=retry_classification,
        format_repair_attempted=True,
        precommit_repair_attempted=True,
        repair_strategy="deterministic_autofix",
        reason_code_override=(
            POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
            if retry_classification.reason_code
            == POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE
            else None
        ),
    )
