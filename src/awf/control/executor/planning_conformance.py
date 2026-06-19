"""WorkspaceExecutor post-validation conformance operations.

Mechanically extracted from ``awf.control.executor.planning_ops``; behavior is unchanged.
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
from typing import Any

from awf.adapters.base import (
    AgentAdapter,
)
from awf.common.git_identity import (
    git_safe_directory_config_args,
)
from awf.control.executor.constants import (
    PLAN_CONFORMANCE_UNSATISFIED,
    POST_VALIDATION_CONFORMANCE_REPORT_CLEANUP_FAILED_REASON_CODE,
)
from awf.control.executor.helpers import (
    _digest_text,
    _read_text_if_present,
    _validation_evidence_json,
)
from awf.control.executor.planning_scope import _build_planning_scope_failure
from awf.control.executor.quality_gates import (
    _log,
)
from awf.control.executor.types import (
    _ConformanceSalvageExecutionResult,
    _PlanningRunFailure,
    _PlanningValidationHandoff,
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
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    PlanConformanceReport,
    build_conformance_failure_evidence,
    build_conformance_prompt,
    parse_conformance_report,
)
from awf.service.artifacts import (
    DEPOSITED_CONFORMANCE_NAME,
    DEPOSITED_PLAN_NAME,
    MAX_ARTIFACT_CONTENT_BYTES,
    _deposit_one_planning_artifact,
    deposit_workspace_planning_artifacts,
    workspace_artifact_dir,
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
        # A worktree ``.git`` file always carries a ``gitdir:`` pointer; the
        # non-pointer else-arc is unreachable defensive code (and would leave
        # ``exclude_path`` under a regular file, so the mkdir below could not
        # succeed anyway).
        if content.startswith(prefix):  # pragma: no branch
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
    base_commit: str,
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
    if not report.satisfied:
        return _build_unsatisfied_failure(report, handoff, validation_run_id)
    # Track whether the on-worktree report actually carries the satisfied
    # report we want to serve. A fresh file is already correct; an AWF-
    # synthesized rewrite from stdout/stale disk is correct only when the
    # write call itself succeeds. We must not infer success from file presence,
    # because a stale handoff report may still exist on disk after a failed
    # rewrite, which would make the served artifact disagree with the recorded
    # event (PRRT_kwDOSJAM6s6KL7-o).
    rewrite_succeeded = report_from_fresh_file
    if not report_from_fresh_file:
        try:
            self._write_satisfied_post_validation_conformance_report(
                worktree_path=worktree_path,
                report_path=handoff.report_path,
                report=report,
            )
            rewrite_succeeded = True
        except OSError as exc:
            # The report is written to ``docs/awf-plans/`` purely as an
            # inspectable on-worktree copy (the path is gitignored and the
            # file is deliberately not committed). The conformance outcome
            # is already durably captured by the event recorded below plus
            # the validation-run artifacts, so a write failure (e.g. a
            # read-only worktree or a full disk) must never discard the
            # agent's completed work. Log and proceed instead of failing.
            _log.warning(
                "executor.post_validation_conformance_report_write_failed",
                workspace_id=workspace.id,
                validation_run_id=validation_run_id,
                report_path=handoff.report_path.as_posix(),
                error_type=type(exc).__name__,
                errno=exc.errno,
            )
    # For successful planned workspaces where the report was produced from
    # stdout or a fresh on-disk file, deposit a snapshot into the served
    # artifact dir BEFORE removing the on-worktree copy. The deposit is
    # best-effort and gated on ``planning.required`` so the console still
    # surfaces the conformance report after teardown.
    #
    # When the report came from stdout and the on-disk copy was stale, the
    # AWF-synthesized satisfied report was written above. If that write failed,
    # the worktree file would still carry the stale report, so deposit the
    # in-memory satisfied report directly to keep the served artifact consistent
    # with the satisfied event recorded after cleanup succeeds.
    artifact_work_dir = _post_validation_conformance_artifact_work_dir(
        self,
        workspace_id=workspace.id,
        validation_run_id=validation_run_id,
    )
    if artifact_work_dir is not None:
        if not report_from_fresh_file and report_text is not None and not rewrite_succeeded:
            _deposit_satisfied_conformance_report(
                work_dir=artifact_work_dir,
                workspace_id=workspace.id,
                worktree_path=worktree_path,
                plan_path=handoff.plan_path,
                report=report,
            )
        else:
            deposit_workspace_planning_artifacts(
                work_dir=artifact_work_dir,
                workspace_id=workspace.id,
                worktree_path=worktree_path,
                plan_path=handoff.plan_path,
                report_path=handoff.report_path,
            )
    # The satisfied conformance report is an AWF artifact/event, not source
    # work. Remove the on-worktree copy so project-specific profiles that
    # track the conformance report path do not see a dirty worktree at
    # pre-push validation time. The outcome is already captured by the event
    # above and by the validation-run artifact deposit.
    #
    # The report may be tracked in the project profile. When the path is
    # tracked, ``git restore --source=base_commit --worktree --staged``
    # restores the original base content to both the index and worktree, so
    # the worktree is clean and the file must be left alone. ``unlink()`` would
    # re-delete the restored file and leave an unstaged deletion
    # (`` D ...``). For paths that do not exist at base_commit or are
    # untracked/gitignored, the restore will fail; fall back to a plain unlink
    # in that case. Use ``--`` to avoid mis-interpreting report paths that
    # start with a dash.
    restore_result = await self._runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "restore",
            "--source",
            base_commit,
            "--worktree",
            "--staged",
            "--",
            handoff.report_path.as_posix(),
        ]
    )
    restore_succeeded = restore_result.ok and not await _report_path_is_dirty(
        self, worktree_path, handoff.report_path
    )
    if restore_succeeded:
        # Tracked report: restore put the committed copy back; leave it there.
        await self._record_post_validation_conformance_event(
            workspace_id=workspace.id,
            handoff=handoff,
            report=report,
            validation_run_id=validation_run_id,
        )
        return None
    if restore_result.ok:
        # ``git restore --source=base_commit`` can leave the path staged
        # relative to HEAD when base_commit differs from HEAD (e.g. an earlier
        # fix pass committed the AWF-authored report). HEAD is the only tree
        # that matches the current commit, so restoring from HEAD cleans both
        # the index and worktree and preserves the committed report state.
        head_restore_result = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "restore",
                "--source",
                "HEAD",
                "--worktree",
                "--staged",
                "--",
                handoff.report_path.as_posix(),
            ]
        )
        if head_restore_result.ok and not await _report_path_is_dirty(
            self, worktree_path, handoff.report_path
        ):
            await self._record_post_validation_conformance_event(
                workspace_id=workspace.id,
                handoff=handoff,
                report=report,
                validation_run_id=validation_run_id,
            )
            return None
        # The path is still dirty after restoring from both base_commit and
        # HEAD. Log the partial restore so we do not silently leave the stale
        # AWF-authored report in the push candidate.
        _log.warning(
            "executor.post_validation_conformance_report_restore_left_dirty",
            workspace_id=workspace.id,
            validation_run_id=validation_run_id,
            report_path=handoff.report_path.as_posix(),
            base_commit=base_commit,
        )
    else:
        _log.warning(
            "executor.post_validation_conformance_report_git_restore_failed",
            workspace_id=workspace.id,
            validation_run_id=validation_run_id,
            report_path=handoff.report_path.as_posix(),
            stderr=restore_result.stderr,
        )
    report_worktree_path = worktree_path / handoff.report_path
    try:
        _remove_report_worktree_path(report_worktree_path, worktree_path=worktree_path)
    except OSError as exc:
        _log.warning(
            "executor.post_validation_conformance_report_unlink_failed",
            workspace_id=workspace.id,
            validation_run_id=validation_run_id,
            report_path=handoff.report_path.as_posix(),
            error_type=type(exc).__name__,
            errno=exc.errno,
        )
    if await _report_path_is_dirty(self, worktree_path, handoff.report_path):
        _log.warning(
            "executor.post_validation_conformance_report_unlink_left_dirty",
            workspace_id=workspace.id,
            validation_run_id=validation_run_id,
            report_path=handoff.report_path.as_posix(),
            base_commit=base_commit,
        )
        return _build_report_cleanup_failure(handoff, validation_run_id)
    await self._record_post_validation_conformance_event(
        workspace_id=workspace.id,
        handoff=handoff,
        report=report,
        validation_run_id=validation_run_id,
    )
    return None


def _post_validation_conformance_artifact_work_dir(
    self: Any,
    *,
    workspace_id: str,
    validation_run_id: str,
) -> Path | None:
    config = getattr(self, "_config", None)
    compose_projects_root = getattr(config, "compose_projects_root", None)
    if compose_projects_root is None:
        _log.warning(
            "executor.post_validation_conformance_deposit_skipped_missing_artifact_root",
            workspace_id=workspace_id,
            validation_run_id=validation_run_id,
        )
        return None
    return Path(compose_projects_root).parent


def _remove_report_worktree_path(path: Path, *, worktree_path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except IsADirectoryError:
        path.rmdir()
    _remove_empty_report_parent_dirs(path.parent, worktree_path=worktree_path)


def _remove_empty_report_parent_dirs(path: Path, *, worktree_path: Path) -> None:
    current = path
    while current != worktree_path and current != current.parent:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


async def _report_path_is_dirty(
    self: Any,
    worktree_path: Path,
    report_path: Path,
) -> bool:
    """Return True if report_path is dirty relative to HEAD.

    A successful git restore --source=base_commit can still leave the
    path staged or modified relative to HEAD when the base content differs
    from the current commit (e.g. an AWF-authored report was committed by a
    previous fix pass). Treating the command exit code alone as success would
    publish that stale report, so we inspect the actual worktree state.
    """
    report_worktree_path = worktree_path / report_path
    if report_worktree_path.is_dir():
        return True
    if _empty_report_parent_residue_is_dirty(
        report_worktree_path.parent,
        worktree_path=worktree_path,
    ):
        return True
    changed = await self._changed_paths(worktree_path)
    return report_path in changed


def _empty_report_parent_residue_is_dirty(path: Path, *, worktree_path: Path) -> bool:
    current = path
    while current != worktree_path and current != current.parent:
        if current.is_dir():
            try:
                next(current.iterdir())
            except StopIteration:
                return True
            except OSError:
                return True
        current = current.parent
    return False


def _build_unsatisfied_failure(
    report: PlanConformanceReport,
    handoff: _PlanningValidationHandoff,
    validation_run_id: str,
) -> _PlanningRunFailure:
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


def _build_report_cleanup_failure(
    handoff: _PlanningValidationHandoff,
    validation_run_id: str,
) -> _PlanningRunFailure:
    return _PlanningRunFailure(
        message=(
            "post-validation conformance report cleanup left report path dirty: "
            f"{handoff.report_path.as_posix()}"
        ),
        reason_code=POST_VALIDATION_CONFORMANCE_REPORT_CLEANUP_FAILED_REASON_CODE,
        details={
            "conformance_report_cleanup": {
                "validation_run_id": validation_run_id,
                "report_path": handoff.report_path.as_posix(),
                "plan_path": handoff.plan_path.as_posix(),
            },
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


def _remove_stale_satisfied_conformance_artifacts(
    *,
    workspace_id: str,
    dest: Path,
    tmp_dest: Path,
) -> None:
    for stale_path in (dest, tmp_dest):
        try:
            stale_path.unlink(missing_ok=True)
        except OSError as exc:
            _log.warning(
                "executor.satisfied_conformance_report_deposit_cleanup_failed",
                workspace_id=workspace_id,
                artifact_name=stale_path.name,
                error_type=type(exc).__name__,
                errno=exc.errno,
            )


def _deposit_satisfied_conformance_report(
    *,
    work_dir: str | Path,
    workspace_id: str,
    worktree_path: Path,
    plan_path: Path,
    report: PlanConformanceReport,
) -> None:
    """Deposit the satisfied conformance report into the served artifact dir
    directly from the parsed report, bypassing the worktree file.

    Used when the on-worktree report is stale and the AWF-synthesized write
    failed: the event and validation run already record success, so the served
    artifact must match that outcome rather than the stale disk copy. The plan
    is also copied (when present) so the served artifact directory contains the
    same planning documents as the normal deposit path.
    """
    artifact_dir = workspace_artifact_dir(work_dir, workspace_id)
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # The served conformance report is a convenience artifact; the outcome
        # is already recorded by the event and validation run. A permission or
        # full-disk failure creating the artifact directory must not fail the
        # post-validation conformance check.
        _log.warning(
            "executor.satisfied_conformance_report_deposit_failed",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            errno=exc.errno,
        )
        return

    # Deposit the in-memory satisfied conformance report first so the served
    # artifact matches the recorded event even if the worktree file is stale.
    dest = artifact_dir / DEPOSITED_CONFORMANCE_NAME
    tmp_dest = artifact_dir / f".{DEPOSITED_CONFORMANCE_NAME}.tmp"
    content = (
        json.dumps(
            {
                "status": report.status.value,
                "summary": report.summary,
                "reason_code": report.reason_code,
                "gaps": list(report.gaps),
            },
            indent=2,
        )
        + "\n"
    )
    content_size = len(content.encode("utf-8"))
    if content_size > MAX_ARTIFACT_CONTENT_BYTES:
        _log.warning(
            "executor.satisfied_conformance_report_deposit_rejected",
            workspace_id=workspace_id,
            reason="oversized",
            size_bytes=content_size,
            max_size_bytes=MAX_ARTIFACT_CONTENT_BYTES,
        )
        _remove_stale_satisfied_conformance_artifacts(
            workspace_id=workspace_id,
            dest=dest,
            tmp_dest=tmp_dest,
        )
        return
    try:
        tmp_dest.write_text(content, encoding="utf-8")
        tmp_dest.replace(dest)
    except OSError as exc:
        # The served conformance report is a convenience artifact; the outcome
        # is already recorded by the event and validation run. A disk or
        # permission failure here must not fail the post-validation check.
        _log.warning(
            "executor.satisfied_conformance_report_deposit_failed",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            errno=exc.errno,
        )
        _remove_stale_satisfied_conformance_artifacts(
            workspace_id=workspace_id,
            dest=dest,
            tmp_dest=tmp_dest,
        )
        return

    # Best-effort copy of the worktree plan so the served artifact contains
    # both planning documents, mirroring ``deposit_workspace_planning_artifacts``.
    # Use the hardened single-artifact deposit so symlink/worktree-escape/hard-
    # link/oversized guards apply even in this fallback path.
    _deposit_one_planning_artifact(
        artifact_dir=artifact_dir,
        workspace_id=workspace_id,
        source=worktree_path / plan_path,
        dest_name=DEPOSITED_PLAN_NAME,
        worktree_root=worktree_path.resolve(),
    )


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
