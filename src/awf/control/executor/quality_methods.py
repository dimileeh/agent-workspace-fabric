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
    AgentRunError,
)
from awf.common.audit import redact_audit_text
from awf.common.command_evidence import (
    append_command_evidence,
)
from awf.common.commands import CommandResult
from awf.common.git_identity import (
    git_safe_directory_config_args,
)
from awf.control.executor.constants import (
    _AWF_RUFF_FORMAT_CHECK_HOOK_ID,
    GIT_OBJECT_MISSING_REASON_CODE,
    GIT_OBJECT_MISSING_RECOVERED_REASON_CODE,
    POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
    POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE,
    POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE,
    POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
    POST_AGENT_GIT_ADD_FAILED_REASON_CODE,
)
from awf.control.executor.git_ops import (
    _git_name_lines,
)
from awf.control.executor.quality_gates import (
    _build_post_agent_precommit_repair_prompt,
    _classify_post_agent_commit_failure,
    _log,
    _PostAgentCommitClassification,
    _PostAgentCommitStepError,
)
from awf.control.executor.supply_chain_messages import _supply_chain_block_message
from awf.control.executor.types import _CoverageEvidenceResult
from awf.control.protected_file_diffs import (
    committed_changed_paths_since,
    protected_file_diffs_for_committed_paths,
)
from awf.control.quality_gates import (
    PLAN_ONLY_OUTPUT_REASON_CODE,
    QUALITY_GATE_POLICY_CHANGED_REASON_CODE,
    changed_paths_are_only_internal_plan_artifacts,
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
    ResourceReservationRepository,
    WorkspaceRepository,
)
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import ValidationCoverageResult
from awf.service.provider_recovery import (
    create_provider_recovery_attempt_row,
)
from awf.service.supply_chain_policy import (
    SupplyChainPolicyRefreshResult,
    SupplyChainPolicyRefreshService,
)


async def _run_baseline_coverage_preflight(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    profile: WorkspaceProfile,
) -> ValidationCoverageResult | None:
    from awf.control.executor.quality_gates import _run_baseline_coverage_preflight

    return await _run_baseline_coverage_preflight(
        self,
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
        profile=profile,
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
) -> ValidationCoverageResult | None:
    """Measure the pre-agent baseline coverage and persist it for blocked-resume.

    On a fresh run the measurement reflects base coverage (the agent has not yet
    mutated the worktree); persisting it lets a later pause into ``blocked`` hand
    the original base back to the resume path. A directive resume passes
    ``skip_measure=True`` to keep the already-reused base (``reuse``) instead of
    recomputing against the mutated blocked worktree.
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
    )
    await self._persist_block_baseline_coverage(
        workspace_id,
        baseline_coverage=baseline_coverage,
    )
    return baseline_coverage


async def _run_final_coverage_gate(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    profile: WorkspaceProfile,
    validation_tier: int,
    workspace_head_sha: str | None,
) -> _CoverageEvidenceResult:
    from awf.control.executor.quality_gates import _run_final_coverage_gate

    return await _run_final_coverage_gate(
        self,
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
        profile=profile,
        validation_tier=validation_tier,
        workspace_head_sha=workspace_head_sha,
    )


async def _parallel_worker_cpu_limit_for_workspace(
    self: Any,
    workspace_id: str,
    *,
    profile: WorkspaceProfile,
) -> int | None:
    if profile.validation.coverage.parallel_workers is None:
        return None
    async with self._session_factory() as session:
        reservation = await ResourceReservationRepository(session).active_for_workspace(
            workspace_id
        )
    if reservation is None:
        return None
    return max(1, int(reservation.steady_cpu))


async def _refresh_supply_chain_policy_for_workspace(
    self: Any,
    *,
    workspace_id: str,
    command_evidence: Sequence[str],
    changed_paths: Sequence[str],
) -> SupplyChainPolicyRefreshResult:
    async with self._session_factory() as session:
        result = await SupplyChainPolicyRefreshService(session).refresh_workspace(
            workspace_id,
            command_evidence=command_evidence,
            changed_paths=changed_paths,
        )
        await session.commit()
        return result


async def _verify_recovered_post_agent_commit(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    base_commit: str,
    owned_paths: list[str],
    expected_status: WorkspaceStatus,
) -> bool:
    changed_paths = sorted(
        await committed_changed_paths_since(
            self._runner,
            worktree_path=worktree_path,
            base_ref=base_commit,
        )
    )
    if not changed_paths:
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
    if await self._fail_if_plan_only_paths(
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
        await self.enter_blocked_for_protected_violation(
            workspace_id=workspace_id,
            from_status=expected_status,
            violations=violations,
            resume_phase="post_agent_commit_recovery_verify",
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
            ),
        )
    except Exception as exc:
        _log.exception(
            "executor.commit_step_missing_head_recovery_verification_failed",
            workspace_id=workspace_id,
        )
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
    command_evidence: list[str],
) -> None:
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
        return

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
            return

    if classification.repair_strategy == "agent" and allow_agent_repair:
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
            command_evidence=command_evidence,
        )
        return

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
    command_evidence: list[str],
) -> None:
    del commit_result
    prompt = _build_post_agent_precommit_repair_prompt(
        classification=classification,
        staged_paths=staged_paths,
    )
    repair_error: AgentRunError | None = None
    try:
        repair_result = await adapter.run(
            compose_project=compose_project,
            compose_file=compose_file,
            prompt=prompt,
            model=model,
            workspace_id=workspace_id,
            log_source="post_agent_precommit_repair",
        )
        append_command_evidence(
            command_evidence,
            stdout=repair_result.stdout,
            stderr=repair_result.stderr,
        )
    except AgentRunError as exc:
        repair_error = exc
        append_command_evidence(
            command_evidence,
            stdout=exc.result.stdout,
            stderr=exc.result.stderr,
        )

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
        return

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
        return

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
