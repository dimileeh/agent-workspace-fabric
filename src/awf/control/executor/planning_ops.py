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
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from awf.adapters.base import (
    AgentAdapter,
    AgentRunError,
)
from awf.common.command_evidence import (
    append_command_evidence,
)
from awf.common.git_identity import (
    git_identity_config_args,
    git_safe_directory_config_args,
)
from awf.control.executor.constants import _FILE_DIGEST_CHUNK_SIZE, PLAN_CONFORMANCE_UNSATISFIED
from awf.control.executor.git_ops import (
    _git_name_lines,
)
from awf.control.executor.helpers import (
    _digest_file_if_present,
    _digest_text,
    _read_text_if_present,
    _validation_evidence_json,
)
from awf.control.executor.planning_scope import _build_planning_scope_failure
from awf.control.executor.quality_gates import (
    _log,
)
from awf.control.executor.time_utils import _monotonic
from awf.control.executor.types import (
    _ConformanceSalvageExecutionResult,
    _PlanningRunFailure,
    _PlanningValidationHandoff,
    _PostValidationConformanceReportGitError,
    _PostValidationConformanceReportWriteError,
)
from awf.db.enums import (
    FailureReason,
    WorkspaceStatus,
)
from awf.db.models import Workspace
from awf.db.repositories import (
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.db.validation_runs import (
    validation_run_coverage_payload,
)
from awf.profiles.models import WorkspaceProfile
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    AGENT_STALLED_IN_CONFORMANCE,
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    ConformanceIterationRecord,
    ConformanceStallEvidence,
    ConformanceStallKind,
    ConformanceStallPolicy,
    PlanConformanceReport,
    build_agent_task_prompt,
    build_conformance_failure_evidence,
    build_conformance_prompt,
    build_conformance_stall_failure_evidence,
    build_execution_prompt,
    build_planning_prompt,
    classify_conformance_stall,
    conformance_requires_awf_validation,
    parse_conformance_report,
    render_workspace_path,
)
from awf.runtime.workspace_prompt_context import (
    render_workspace_runtime_context,
)
from awf.service.coordination import (
    coordination_warnings_from_task_policy,
)
from awf.service.workspaces import (
    WorkspaceCreateDuplicateHostPortError,
    WorkspaceCreateHostPortConflictError,
    WorkspaceRetryError,
    retry_workspace_row,
)


async def _prepare_conformance_salvage_for_execution(
    self: Any,
    *,
    workspace_id: str,
    workspace: Workspace,
    worktree_path: Path,
) -> _ConformanceSalvageExecutionResult | None:
    from awf.control.executor.git_ops import _prepare_conformance_salvage_for_execution

    return await _prepare_conformance_salvage_for_execution(
        self,
        workspace_id=workspace_id,
        workspace=workspace,
        worktree_path=worktree_path,
    )


async def _fail_conformance_salvage_execution(
    self: Any,
    *,
    workspace_id: str,
    reason_code: str,
    message: str,
    salvage: Mapping[str, Any],
) -> _ConformanceSalvageExecutionResult:
    await self._mark_failed(
        workspace_id=workspace_id,
        from_status=WorkspaceStatus.running,
        failure_reason=FailureReason.infrastructure_failure,
        message=f"{reason_code}: {message}"[:2000],
        reason_code=reason_code,
        details={
            "reason_code": reason_code,
            "conformance_salvage": dict(salvage),
        },
    )
    return _ConformanceSalvageExecutionResult(status="failed")


async def _record_conformance_salvage_event(
    self: Any,
    *,
    workspace_id: str,
    event_type: str,
    reason_code: str,
    payload: dict[str, Any],
) -> None:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        if workspace is None:  # pragma: no cover - destroyed mid-flight
            return
        await repo.add_event(
            workspace,
            event_type=event_type,
            reason_code=reason_code,
            payload=payload,
        )
        await session.commit()


def _materialize_salvage_patch_for_agent(
    self: Any,
    *,
    worktree_path: Path,
    patch_path: Path,
    patch_bytes: bytes,
) -> str:
    relative_path = Path(".awf") / "salvage" / patch_path.name
    agent_patch_path = worktree_path / relative_path
    agent_patch_path.parent.mkdir(parents=True, exist_ok=True)
    agent_patch_path.write_bytes(patch_bytes)
    self._exclude_agent_salvage_artifacts(worktree_path)
    return relative_path.as_posix()


def _exclude_agent_salvage_artifacts(self: Any, worktree_path: Path) -> None:
    _ = self
    git_dir_file = worktree_path / ".git"
    exclude_path = worktree_path / ".git" / "info" / "exclude"
    if git_dir_file.is_file():
        content = git_dir_file.read_text(encoding="utf-8", errors="replace").strip()
        prefix = "gitdir:"
        if content.startswith(prefix):
            git_dir = Path(content[len(prefix) :].strip())
            if not git_dir.is_absolute():
                git_dir = (worktree_path / git_dir).resolve()
            exclude_path = git_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    pattern = "/.awf/salvage/"
    if pattern not in existing.splitlines():
        suffix = "" if existing.endswith("\n") or not existing else "\n"
        exclude_path.write_text(f"{existing}{suffix}{pattern}\n", encoding="utf-8")


async def _record_planning_validation_handoff_event(
    self: Any,
    *,
    workspace_id: str,
    handoff: _PlanningValidationHandoff,
) -> None:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        if workspace is None:
            return
        await repo.add_event(
            workspace,
            event_type="workspace.planning_conformance_requires_awf_validation",
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
            payload={
                "summary": handoff.report.summary,
                "gaps": list(handoff.report.gaps),
                "report_reason_code": handoff.report.reason_code,
                "plan_path": handoff.plan_path.as_posix(),
                "report_path": handoff.report_path.as_posix(),
                "iteration": handoff.iteration,
                "max_iterations": handoff.max_iterations,
            },
        )
        await session.commit()


async def _run_post_validation_conformance_check(
    self: Any,
    *,
    adapter: AgentAdapter,
    workspace: Workspace,
    profile: WorkspaceProfile,
    compose_project: str,
    compose_file: Path,
    worktree_path: Path,
    model: str | None,
    handoff: _PlanningValidationHandoff,
    validation_run_id: str,
) -> _PlanningRunFailure | None:
    # Post-validation conformance is strictly report-only, regardless of
    # ordinary planning unexplained-deviation policy.
    del profile
    evidence = await self._validation_run_evidence_for_conformance(validation_run_id)
    # Snapshot before the adapter run and before any AWF-synthesized
    # satisfied report write below; this scope check only polices changes
    # made during the report-only conformance command.
    before_compare = await self._changed_paths(worktree_path)
    before_compare_head = await self._git_rev_parse_head(worktree_path)
    allowed_paths = {handoff.report_path}
    # Path-set subtraction misses edits to already-dirty paths, so keep a
    # content snapshot for every pre-dirty non-report path.
    before_dirty_digests = {
        path: self._digest_dirty_content(worktree_path, {path})
        for path in before_compare - allowed_paths
    }
    # A stale handoff report may still be present at this path; prefer
    # stdout unless the conformance rerun actually refreshed the file.
    report_path = worktree_path / handoff.report_path
    before_report_text = _read_text_if_present(report_path)
    before_report_digest = _digest_text(before_report_text) if before_report_text else None
    await self._update_subphase(workspace.id, "conformance")
    compare_result = await adapter.run(
        compose_project=compose_project,
        compose_file=compose_file,
        prompt=build_conformance_prompt(
            task_prompt=workspace.task_prompt,
            plan_path=handoff.plan_path,
            report_path=handoff.report_path,
            iteration=handoff.iteration + 1,
            validation_evidence=evidence,
        ),
        model=model,
        workspace_id=workspace.id,
    )
    after_compare = await self._changed_paths(worktree_path)
    committed_compare = (
        await self._committed_paths_since(worktree_path, before_compare_head)
        if before_compare_head is not None
        else set()
    )
    edited_pre_dirty_extra = {
        path
        for path, digest in before_dirty_digests.items()
        if self._digest_dirty_content(worktree_path, {path}) != digest
    }
    dirty_extra = after_compare - before_compare - allowed_paths
    committed_extra = committed_compare - allowed_paths
    extra = sorted(dirty_extra | committed_extra | edited_pre_dirty_extra)
    if extra:
        return _build_planning_scope_failure(
            scope_phase="conformance",
            required_paths=(handoff.report_path,),
            offending_paths=extra,
            summary=(
                f"post-validation conformance phase changed files outside `{handoff.report_path}`"
            ),
        )

    report_text = _read_text_if_present(report_path)
    report_from_fresh_file = (
        report_text is not None and _digest_text(report_text) != before_report_digest
    )
    if not report_from_fresh_file:
        report_text = None
    if report_text is None and compare_result.stdout:
        report_text = compare_result.stdout
    report = parse_conformance_report(report_text or "")
    if report.satisfied:
        if not report_from_fresh_file:
            try:
                self._write_satisfied_post_validation_conformance_report(
                    worktree_path=worktree_path,
                    report_path=handoff.report_path,
                    report=report,
                )
            except OSError as exc:
                raise _PostValidationConformanceReportWriteError(
                    report_path=handoff.report_path,
                    error=exc,
                ) from exc
        await self._commit_post_validation_conformance_report(
            workspace_id=workspace.id,
            worktree_path=worktree_path,
            report_path=handoff.report_path,
            validation_run_id=validation_run_id,
        )
        await self._record_post_validation_conformance_event(
            workspace_id=workspace.id,
            handoff=handoff,
            report=report,
            validation_run_id=validation_run_id,
        )
        return None

    gap_text = "; ".join(report.gaps) or report.summary
    return _PlanningRunFailure(
        message=f"post-validation plan conformance was not satisfied: {gap_text}",
        reason_code=PLAN_CONFORMANCE_UNSATISFIED,
        details={
            "conformance": build_conformance_failure_evidence(
                report=report,
                iterations_used=handoff.iteration + 2,
                max_iterations=handoff.max_iterations,
                plan_path=handoff.plan_path,
                report_path=handoff.report_path,
            ),
            "validation_run_id": validation_run_id,
        },
    )


def _write_satisfied_post_validation_conformance_report(
    *,
    worktree_path: Path,
    report_path: Path,
    report: PlanConformanceReport,
) -> None:
    path = worktree_path / report_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "status": report.status.value,
                "summary": report.summary,
                "reason_code": report.reason_code,
                "gaps": list(report.gaps),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


async def _commit_post_validation_conformance_report(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    report_path: Path,
    validation_run_id: str,
) -> bool:
    report_path_text = report_path.as_posix()
    git_base = [
        "git",
        *git_safe_directory_config_args(worktree_path),
        "-C",
        str(worktree_path),
    ]
    await self._repair_agent_git_ownership(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="post_validation_conformance_report_git_add",
    )
    add_result = await self._runner.run([*git_base, "add", "--", report_path_text])
    if not add_result.ok:
        raise _PostValidationConformanceReportGitError(
            operation="add",
            result=add_result,
        )

    cached = await self._runner.run(
        [*git_base, "diff", "--cached", "--name-only", "--", report_path_text]
    )
    if not cached.ok:
        reset_result = await self._runner.run([*git_base, "reset", "-q", "--", report_path_text])
        if not reset_result.ok:
            _log.warning(
                "executor.post_validation_conformance_report_unstage_failed",
                workspace_id=workspace_id,
                report_path=report_path_text,
                triggering_operation="diff",
                returncode=reset_result.returncode,
                command_reason_code=reset_result.reason_code,
            )
        raise _PostValidationConformanceReportGitError(
            operation="diff",
            result=cached,
            cleanup_operation="reset" if not reset_result.ok else None,
            cleanup_result=reset_result if not reset_result.ok else None,
        )
    staged_paths = _git_name_lines(cached.stdout) if cached.stdout.strip() else []
    if report_path_text not in staged_paths:
        _log.info(
            "executor.post_validation_conformance_report_no_commit_needed",
            workspace_id=workspace_id,
            report_path=report_path_text,
            validation_run_id=validation_run_id,
        )
        return False

    commit_result = await self._runner.run(
        [
            *git_base,
            *git_identity_config_args(),
            "commit",
            "-m",
            "awf: post-validation conformance report",
            "-m",
            (
                "Persist satisfied post-validation conformance report "
                f"for validation run {validation_run_id}."
            ),
            "--",
            report_path_text,
        ],
    )
    await self._repair_agent_git_ownership(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="post_validation_conformance_report_git_commit",
    )
    if not commit_result.ok:
        raise _PostValidationConformanceReportGitError(
            operation="commit",
            result=commit_result,
        )
    return True


async def _record_post_validation_conformance_event(
    self: Any,
    *,
    workspace_id: str,
    handoff: _PlanningValidationHandoff,
    report: PlanConformanceReport,
    validation_run_id: str,
) -> None:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        if workspace is None:
            return
        await repo.add_event(
            workspace,
            event_type="workspace.post_validation_conformance_satisfied",
            reason_code=report.reason_code,
            payload={
                "summary": report.summary,
                "plan_path": handoff.plan_path.as_posix(),
                "report_path": handoff.report_path.as_posix(),
                "validation_run_id": validation_run_id,
            },
        )
        await session.commit()


async def _validation_run_evidence_for_conformance(self: Any, validation_run_id: str) -> str:
    async with self._session_factory() as session:
        run = await ValidationRunRepository(session).get(validation_run_id)
        if run is None:
            payload: dict[str, Any] = {
                "validation_run_id": validation_run_id,
                "status": "missing",
                "reason_code": "VALIDATION_RUN_NOT_FOUND",
            }
        else:
            log_stream_refs = dict(run.log_stream_refs or {})
            log_stream_refs.pop("coverage", None)
            coverage = validation_run_coverage_payload(run)
            payload = {
                "validation_run_id": run.id,
                "status": run.status,
                "reason_code": run.reason_code,
                "coverage": coverage,
                "workspace_head_sha": run.workspace_head_sha,
                "target_branch": run.target_branch,
                "target_head_sha": run.target_head_sha,
                "base_commit": run.base_commit,
                "base_sha": run.base_sha,
                "tier": run.tier,
                "retry_count": run.retry_count,
                "command_set_hash": run.command_set_hash,
                # Command records are metadata-only; stdout/stderr stay in log refs.
                # If that changes, update evidence compaction before passing them through.
                "commands": list(run.commands or []),
                "log_stream_refs": log_stream_refs,
                "profile_name": run.profile_name,
                "profile_version": run.profile_version,
                "profile_source": run.profile_source,
                "resolved_profile_digest": run.resolved_profile_digest,
                "environment_identity_digest": run.environment_identity_digest,
            }
    # Preserve the complete evidence shape when it fits. If it does not,
    # compact bulky fields as JSON values so the fenced evidence stays
    # parseable while retaining the validation result fields first.
    safe_serialized_payload = _validation_evidence_json(payload)
    return f"AWF persisted validation run evidence:\n```json\n{safe_serialized_payload}\n```"


async def _auto_retry_planning_scope_failure(
    self: Any,
    *,
    workspace_id: str,
    failure: _PlanningRunFailure,
) -> None:
    """Create one clean retry for a planning-scope violation."""
    if failure.reason_code != AGENT_PLAN_PHASE_SCOPE_VIOLATION:
        return
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        if workspace is None:
            return
        task_policy = workspace.task_policy if isinstance(workspace.task_policy, Mapping) else {}
        scheduler_policy = task_policy.get("scheduler")
        if isinstance(scheduler_policy, Mapping) and scheduler_policy.get("source_workspace_id"):
            await repo.add_event(
                workspace,
                event_type="workspace.planning_scope_auto_retry_skipped",
                reason_code="PLANNING_SCOPE_AUTO_RETRY_ALREADY_RETRIED",
                payload={"source_reason_code": failure.reason_code},
            )
            await session.commit()
            return
        try:
            result = await retry_workspace_row(
                session,
                workspace_id,
                ignore_source_runtime_check=True,
            )
        except (
            WorkspaceCreateDuplicateHostPortError,
            WorkspaceCreateHostPortConflictError,
            WorkspaceRetryError,
        ) as exc:
            rollback = getattr(session, "rollback", None)
            if rollback is not None:
                await rollback()
            workspace = await repo.get(workspace_id)
            if workspace is None:
                return
            await repo.add_event(
                workspace,
                event_type="workspace.planning_scope_auto_retry_failed",
                reason_code="PLANNING_SCOPE_AUTO_RETRY_FAILED",
                payload={
                    "source_reason_code": failure.reason_code,
                    "error": str(exc)[:2000],
                    "detail": getattr(exc, "detail", None),
                },
            )
            await session.commit()
            return
        await repo.add_event(
            workspace,
            event_type="workspace.planning_scope_auto_retry_requested",
            reason_code="PLANNING_SCOPE_AUTO_RETRY_REQUESTED",
            payload={
                "source_reason_code": failure.reason_code,
                "new_workspace_id": result.new_workspace.id,
            },
        )
        await session.commit()


async def _run_agent_task_with_optional_planning(
    self: Any,
    *,
    adapter: AgentAdapter,
    workspace: Workspace,
    profile: WorkspaceProfile,
    compose_project: str,
    compose_file: Path,
    worktree_path: Path,
    model: str | None,
    command_evidence: list[str] | None = None,
) -> str | _PlanningRunFailure | _PlanningValidationHandoff | None:
    planning = profile.planning
    coordination_warnings = coordination_warnings_from_task_policy(
        getattr(workspace, "task_policy", None)
    )
    workspace_runtime_context = render_workspace_runtime_context(profile)
    if not planning.required:
        await self._update_subphase(workspace.id, "agent")
        result = await adapter.run(
            compose_project=compose_project,
            compose_file=compose_file,
            prompt=build_agent_task_prompt(
                task_prompt=workspace.task_prompt,
                coordination_warnings=coordination_warnings,
                workspace_runtime_context=workspace_runtime_context,
            ),
            model=model,
            workspace_id=workspace.id,
        )
        append_command_evidence(
            command_evidence,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        return None

    try:
        plan_path = render_workspace_path(planning.plan_path, workspace_id=workspace.id)
        report_path = render_workspace_path(
            planning.conformance_report_path,
            workspace_id=workspace.id,
        )
    except ValueError as exc:
        return f"planning profile is invalid: {exc}"

    before_plan = await self._changed_paths(worktree_path)
    plan_file_digest_before = _digest_file_if_present(worktree_path / plan_path)
    baseline_sha: str | None = None
    rev_r = await self._runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "rev-parse",
            "HEAD",
        ]
    )
    if rev_r.ok and rev_r.stdout.strip():
        baseline_sha = rev_r.stdout.strip()
    await self._update_subphase(workspace.id, "planning")
    plan_result = await adapter.run(
        compose_project=compose_project,
        compose_file=compose_file,
        prompt=build_planning_prompt(
            task_prompt=workspace.task_prompt,
            plan_path=plan_path,
            coordination_warnings=coordination_warnings,
            workspace_runtime_context=workspace_runtime_context,
        ),
        model=model,
        workspace_id=workspace.id,
    )
    append_command_evidence(
        command_evidence,
        stdout=plan_result.stdout,
        stderr=plan_result.stderr,
    )
    dirty_paths = await self._changed_paths(worktree_path)
    committed_paths = (
        await self._committed_paths_since(worktree_path, baseline_sha)
        if baseline_sha is not None
        else set()
    )
    after_plan = dirty_paths | committed_paths
    if plan_path not in after_plan:
        plan_file_digest_after = _digest_file_if_present(worktree_path / plan_path)
        if plan_file_digest_after is not None and plan_file_digest_after != plan_file_digest_before:
            after_plan = {*after_plan, plan_path}
    if plan_path not in after_plan:
        return _build_planning_scope_failure(
            scope_phase="planning",
            required_paths=(plan_path,),
            offending_paths=sorted(after_plan - before_plan),
            summary=(f"planning phase did not create or modify required plan file `{plan_path}`"),
        )
    if planning.enforce_plan_only_changes:
        extra = sorted(after_plan - before_plan - {plan_path})
        if extra:
            return _build_planning_scope_failure(
                scope_phase="planning",
                required_paths=(plan_path,),
                offending_paths=extra,
                summary=f"planning phase changed files outside `{plan_path}`",
            )

    gaps: tuple[str, ...] = ()
    last_report: PlanConformanceReport | None = None
    last_iteration = 0
    stall_policy = ConformanceStallPolicy(
        no_output_seconds=planning.conformance_stall.no_output_seconds,
        over_duration_seconds=planning.conformance_stall.over_duration_seconds,
        repeated_output_threshold=(planning.conformance_stall.repeated_output_threshold),
    )
    iteration_history: list[ConformanceIterationRecord] = []
    # Post-planning HEAD. Serves two purposes:
    #
    # 1. Implementation baseline for stall commit metrics. Pre-planning
    #    HEAD (``baseline_sha``) would inflate ``implementation_commit_count``
    #    if the agent committed the plan artifact during planning — the
    #    scope check accepts ``committed_paths`` and the agent is not
    #    blocked from committing the one allowed file.
    #
    # 2. Seeds the iteration progress digest. Combining the HEAD commit
    #    SHA with hashed file bytes lets re-edits to the same dirty file
    #    *and* commits made during an iteration both register as
    #    progress; without the HEAD signal an agent that commits each
    #    iteration leaves a clean working tree and produces identical
    #    empty digests, which would falsely trip
    #    ``classify_conformance_stall``'s repeated_output detector.
    implementation_baseline_sha = await self._git_rev_parse_head(worktree_path)
    iteration_start_digest = self._digest_dirty_content(
        worktree_path, dirty_paths, head_sha=implementation_baseline_sha
    )
    for iteration in range(planning.max_iterations + 1):
        last_iteration = iteration
        await self._update_subphase(workspace.id, "agent")
        execute_result = await adapter.run(
            compose_project=compose_project,
            compose_file=compose_file,
            prompt=build_execution_prompt(
                task_prompt=workspace.task_prompt,
                plan_path=plan_path,
                iteration=iteration,
                gaps=gaps,
                coordination_warnings=coordination_warnings,
                workspace_runtime_context=workspace_runtime_context,
            ),
            model=model,
            workspace_id=workspace.id,
        )
        append_command_evidence(
            command_evidence,
            stdout=execute_result.stdout,
            stderr=execute_result.stderr,
        )
        before_compare = await self._changed_paths(worktree_path)
        # Snapshot any pre-existing report digest so the timeout branch
        # can distinguish a report this compare call produced from a
        # stale leftover (e.g., a satisfied JSON written by a prior
        # interrupted run on this workspace, or by an out-of-scope
        # earlier-phase write). Without this guard, a satisfied JSON
        # already on disk would short-circuit the loop on
        # AGENT_IDLE_TIMEOUT/AGENT_TIMEOUT with no evidence the current
        # compare call produced it.
        before_report_text = _read_text_if_present(worktree_path / report_path)
        before_report_digest = _digest_text(before_report_text) if before_report_text else None
        iteration_started_at = _monotonic()
        compare_error: AgentRunError | None = None
        compare_result = None
        try:
            await self._update_subphase(workspace.id, "conformance")
            compare_result = await adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=build_conformance_prompt(
                    task_prompt=workspace.task_prompt,
                    plan_path=plan_path,
                    report_path=report_path,
                    iteration=iteration,
                ),
                model=model,
                workspace_id=workspace.id,
            )
            append_command_evidence(
                command_evidence,
                stdout=compare_result.stdout,
                stderr=compare_result.stderr,
            )
        except AgentRunError as exc:
            if exc.reason_code not in {"AGENT_IDLE_TIMEOUT", "AGENT_TIMEOUT"}:
                raise
            compare_error = exc
            append_command_evidence(
                command_evidence,
                stdout=exc.result.stdout,
                stderr=exc.result.stderr,
            )

        elapsed_seconds = _monotonic() - iteration_started_at
        # Compute after_compare on both success and timeout paths so the
        # scope check runs uniformly. Otherwise an idle/timeout that still
        # leaves a satisfied report could write files outside report_path
        # and slip past the success short-circuit below.
        after_compare = await self._changed_paths(worktree_path)
        if planning.fail_on_unexplained_deviation:
            extra = sorted(after_compare - before_compare - {report_path})
            if extra:
                return _build_planning_scope_failure(
                    scope_phase="conformance",
                    required_paths=(report_path,),
                    offending_paths=extra,
                    summary=(f"conformance phase changed files outside `{report_path}`"),
                )
        if compare_error is None:
            stdout = compare_result.stdout if compare_result is not None else ""
            stderr = compare_result.stderr if compare_result is not None else ""
            report_text = _read_text_if_present(worktree_path / report_path) or stdout
            report = parse_conformance_report(report_text)
            last_report = report
            report_digest = _digest_text(report_text) if report_text else None
            fresh_report_written = (
                report_digest is not None and report_digest != before_report_digest
            )
        else:
            stdout = compare_error.result.stdout
            stderr = compare_error.result.stderr
            # Even when the conformance call idles or times out, the agent
            # may have already written a valid (potentially satisfied)
            # report. Honor the on-disk report only when its digest
            # changed during this call so a stale satisfied JSON cannot
            # short-circuit the loop. Fall back to stdout — which is
            # always produced by this call — when the file is stale or
            # absent. A truly fresh write will produce a digest
            # different from the pre-call snapshot; otherwise the
            # iteration is treated as no_output by stall classification.
            current_report_text = _read_text_if_present(worktree_path / report_path)
            if (
                current_report_text is not None
                and _digest_text(current_report_text) != before_report_digest
            ):
                report_text = current_report_text
            elif stdout:
                report_text = stdout
            else:
                report_text = None
            if report_text:
                report = parse_conformance_report(report_text)
                last_report = report
                report_digest = _digest_text(report_text)
                fresh_report_written = (
                    report_digest is not None and report_digest != before_report_digest
                )
            else:
                report = None
                report_digest = None
                fresh_report_written = False
        after_head = await self._git_rev_parse_head(worktree_path)
        after_digest = self._digest_dirty_content(worktree_path, after_compare, head_sha=after_head)
        worktree_changed = iteration_start_digest != after_digest
        iteration_start_digest = after_digest

        iteration_history.append(
            ConformanceIterationRecord(
                iteration=iteration,
                elapsed_seconds=elapsed_seconds,
                report_digest=report_digest,
                worktree_changed=worktree_changed,
                stdout=stdout,
                stderr=stderr,
                error_reason_code=(
                    compare_error.reason_code if compare_error is not None else None
                ),
            )
        )

        # Honour conformance success before stall classification so a
        # slow-but-satisfied iteration is not misread as over_duration,
        # and so a run that wrote a satisfied report before idling /
        # timing out is not misread as no_output.
        if report is not None and report.satisfied:
            _log.info(
                "executor.planning_conformance_satisfied",
                workspace_id=workspace.id,
                iteration=iteration,
                summary=report.summary,
            )
            return None

        if report is not None and conformance_requires_awf_validation(report):
            _log.info(
                "executor.planning_conformance_requires_awf_validation",
                workspace_id=workspace.id,
                iteration=iteration,
                max_iterations=planning.max_iterations,
                gaps=list(report.gaps),
                reason_code=report.reason_code,
            )
            return _PlanningValidationHandoff(
                report=report,
                plan_path=plan_path,
                report_path=report_path,
                iteration=iteration,
                max_iterations=planning.max_iterations,
            )

        stall = classify_conformance_stall(
            history=iteration_history,
            policy=stall_policy,
            plan_path=plan_path,
            report_path=report_path,
            latest_error=compare_error,
        )
        if stall is not None and not (
            stall.kind == ConformanceStallKind.over_duration
            and compare_error is None
            and report is not None
            and fresh_report_written
        ):
            return cast(
                str | _PlanningRunFailure | _PlanningValidationHandoff | None,
                await self._build_conformance_stall_failure(
                    workspace=workspace,
                    worktree_path=worktree_path,
                    baseline_sha=implementation_baseline_sha,
                    last_report=last_report,
                    stall=stall,
                    iterations_used=last_iteration + 1,
                    max_iterations=planning.max_iterations,
                    plan_path=plan_path,
                    report_path=report_path,
                    recovery_action=planning.conformance_stall.recovery_action,
                ),
            )

        if compare_error is not None:
            # Not classified as a stall, but the conformance call failed —
            # bubble up so the outer agent_failure handler captures it.
            raise compare_error

        assert report is not None
        gaps = report.gaps or (report.summary,)
        _log.info(
            "executor.planning_conformance_needs_iteration",
            workspace_id=workspace.id,
            iteration=iteration,
            max_iterations=planning.max_iterations,
            gaps=list(gaps),
            reason_code=report.reason_code,
        )

    if last_report is None:  # pragma: no cover - defensive
        return "planning conformance did not run"
    gap_text = "; ".join(last_report.gaps) or last_report.summary
    message = (
        "plan conformance was not satisfied after "
        f"{planning.max_iterations} iteration(s): {gap_text}"
    )
    return _PlanningRunFailure(
        message=message,
        reason_code=PLAN_CONFORMANCE_UNSATISFIED,
        details={
            "conformance": build_conformance_failure_evidence(
                report=last_report,
                iterations_used=last_iteration + 1,
                max_iterations=planning.max_iterations,
                plan_path=plan_path,
                report_path=report_path,
            )
        },
    )


async def _build_conformance_stall_failure(
    self: Any,
    *,
    workspace: Workspace,
    worktree_path: Path,
    baseline_sha: str | None,
    last_report: PlanConformanceReport | None,
    stall: ConformanceStallEvidence,
    iterations_used: int,
    max_iterations: int,
    plan_path: Path,
    report_path: Path,
    recovery_action: str | None = None,
) -> _PlanningRunFailure:
    head_sha = await self._git_rev_parse_head(worktree_path)
    commit_count = 0
    changed_paths: list[str] = []
    if baseline_sha:
        commit_count = await self._git_commit_count_since(worktree_path, baseline_sha)
        try:
            changed = await self._committed_paths_since(worktree_path, baseline_sha)
        except RuntimeError:
            _log.exception(
                "executor.planning_conformance_stalled_diff_failed",
                workspace_id=workspace.id,
                baseline_sha=baseline_sha,
            )
        else:
            changed_paths = sorted(path.as_posix() for path in changed)
    stall_evidence_payload = build_conformance_stall_failure_evidence(
        stall=stall,
        head_sha=head_sha,
        base_sha=baseline_sha,
        commit_count=commit_count,
        changed_paths=changed_paths,
        recovery_action=recovery_action,
    )
    details: dict[str, Any] = {"conformance_stall": stall_evidence_payload}
    if last_report is not None:
        details["conformance"] = build_conformance_failure_evidence(
            report=last_report,
            iterations_used=iterations_used,
            max_iterations=max_iterations,
            plan_path=plan_path,
            report_path=report_path,
        )
    message = (
        f"plan conformance stalled in iteration {stall.iteration_index} "
        f"({stall.kind.value}); preserving worktree for recovery"
    )
    _log.info(
        "executor.planning_conformance_stalled",
        workspace_id=workspace.id,
        iteration=stall.iteration_index,
        kind=stall.kind.value,
        elapsed_seconds=stall.elapsed_seconds,
        no_output_seconds=stall.no_output_seconds,
        repeated_output_count=stall.repeated_output_count,
        implementation_commit_count=commit_count,
    )
    try:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            persisted = await repo.get(workspace.id)
            if persisted is not None:
                await repo.add_event(
                    persisted,
                    event_type="workspace.planning_conformance_stalled",
                    reason_code=AGENT_STALLED_IN_CONFORMANCE,
                    payload=stall_evidence_payload,
                )
                await session.commit()
    except Exception:
        _log.exception(
            "executor.planning_conformance_stalled_record_failed",
            workspace_id=workspace.id,
        )
    return _PlanningRunFailure(
        message=message,
        reason_code=AGENT_STALLED_IN_CONFORMANCE,
        details=details,
    )


def _digest_dirty_content(
    self: Any,
    worktree_path: Path,
    paths: set[Path],
    *,
    head_sha: str | None = None,
) -> str:
    """Progress fingerprint combining HEAD SHA and dirty content bytes.

    Path-set equality alone treats iterative re-edits of the same file as
    no progress; hashing per-file bytes lets repeat edits register as
    work. Folding ``head_sha`` in additionally lets commits register as
    progress — an agent that commits each iteration leaves a clean
    working tree, so the dirty portion would otherwise digest identically
    and falsely trip ``classify_conformance_stall``'s repeated_output
    detector. Missing files contribute a deterministic marker so the
    digest stays stable across iterations whose worktree exists only in
    mocked git output.
    """
    _ = self
    hasher = hashlib.sha256()
    if head_sha is not None:
        hasher.update(head_sha.encode("utf-8"))
        hasher.update(b"\0")
    # Stream file bytes in fixed-size chunks rather than read_bytes() so a
    # large generated artifact in the dirty set does not balloon peak
    # memory on every conformance iteration.
    for path in sorted(paths, key=lambda p: p.as_posix()):
        hasher.update(path.as_posix().encode("utf-8"))
        hasher.update(b"\0")
        try:
            with (worktree_path / path).open("rb") as fh:
                while chunk := fh.read(_FILE_DIGEST_CHUNK_SIZE):
                    hasher.update(chunk)
        except OSError:
            hasher.update(b"<missing>")
        hasher.update(b"\0")
    return hasher.hexdigest()
