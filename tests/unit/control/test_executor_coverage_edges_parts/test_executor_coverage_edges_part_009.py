"""Focused branch-coverage tests for the validation fix-cycle.

These drive ``run_validation_and_fix_cycle`` directly with a lightweight
``SimpleNamespace`` fake executor (same pattern as
``test_executor_coverage_edges_part_007``) to exercise narrow branches in
``execution_validation.py`` that the heavier full-pipeline executor suites do
not reach: the post-validation conformance report error detail enrichment, the
fix-pass status-recheck races, the fix-pass git-failure ``command_reason_code``
detail, the plan-only fix-pass guard, and the budget-exhausted loop fall
through.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import CommandResult
from awf.control.executor import execution_validation as executor_execution_validation
from awf.control.executor.constants import (
    POST_VALIDATION_CONFORMANCE_REPORT_GIT_FAILED_REASON_CODE,
    POST_VALIDATION_CONFORMANCE_REPORT_WRITE_FAILED_REASON_CODE,
)
from awf.control.executor.types import (
    _PlanningRunFailure,
    _PlanningValidationHandoff,
    _PostValidationConformanceReportGitError,
    _PostValidationConformanceReportWriteError,
)
from awf.control.quality_gates import PLAN_ONLY_OUTPUT_REASON_CODE
from awf.db.enums import OperationStatus
from awf.profiles.models import WorkspaceProfile
from awf.runtime.planning import (
    PlanConformanceReport,
    PlanConformanceStatus,
)
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationResult,
)
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup


def _passing_command(tmp_path: Path) -> ValidationCommandResult:
    stdout = tmp_path / "ok.stdout"
    stderr = tmp_path / "ok.stderr"
    stdout.write_text("ok", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    return ValidationCommandResult(
        command="pytest -q",
        returncode=0,
        duration_seconds=0.1,
        stdout_path=stdout,
        stderr_path=stderr,
        phase="validate",
        reason_code=None,
        policy_failed=False,
    )


def _failing_command(tmp_path: Path) -> ValidationCommandResult:
    stdout = tmp_path / "fail.stdout"
    stderr = tmp_path / "fail.stderr"
    stdout.write_text("boom", encoding="utf-8")
    stderr.write_text("boom", encoding="utf-8")
    return ValidationCommandResult(
        command="pytest -q",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout,
        stderr_path=stderr,
        phase="validate",
        reason_code="PYTEST_TEST_FAILURE",
        policy_failed=True,
    )


def _workspace(workspace_id: str, *, pr_url: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        resolved_profile={"name": f"prof-{workspace_id}"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="A task",
        agent="codex",
        owned_paths=(),
        id=workspace_id,
        pr_url=pr_url,
    )


def _handoff(
    tmp_path: Path, *, iteration: int = 1, max_iterations: int = 3
) -> _PlanningValidationHandoff:
    report = PlanConformanceReport(
        status=PlanConformanceStatus.satisfied,
        summary="ok",
        gaps=(),
    )
    return _PlanningValidationHandoff(
        report=report,
        plan_path=tmp_path / "plan.md",
        report_path=tmp_path / "report.md",
        iteration=iteration,
        max_iterations=max_iterations,
    )


def _patch_profile(monkeypatch: pytest.MonkeyPatch, profile: WorkspaceProfile) -> None:
    async def _sync_profile(*_args: object, **_kwargs: object) -> WorkspaceProfile:
        return profile

    monkeypatch.setattr(
        executor_execution_validation,
        "_profile_for_workspace",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_sync_resolved_profile",
        _sync_profile,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "profile_phase_command_plan",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_validation_tier_for_workspace",
        lambda *_args, **_kwargs: 1,
    )


def _patch_clean_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        executor_execution_validation,
        "check_validation_worktree_clean",
        AsyncMock(return_value=ValidationWorktreeCheck(clean=True)),
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "cleanup_validation_worktree_side_effects",
        AsyncMock(
            return_value=ValidationWorktreeCleanup(
                cleaned=True,
                check=ValidationWorktreeCheck(clean=True),
                restore_ref="c" * 40,
            )
        ),
    )


async def _run_cycle(
    executor: SimpleNamespace,
    *,
    workspace: SimpleNamespace,
    tmp_path: Path,
    adapter: Any,
    planning_validation_handoff: _PlanningValidationHandoff | None = None,
    recovery: dict[str, Any] | None = None,
    git_in_worktree: Any | None = None,
) -> Any:
    return await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id=workspace.id,
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project=f"awf_{workspace.id}",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch=f"awf/{workspace.id}",
        adapter=adapter,  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=planning_validation_handoff,
        recovery=recovery,
        rebase_recovery_result=None,
        has_known_non_plan_output=False,
        git_in_worktree=git_in_worktree
        or AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )


@pytest.mark.unit
async def test_conformance_report_git_error_records_command_reason_code_detail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A conformance report git failure with a command reason_code enriches details."""
    profile = WorkspaceProfile.model_validate({"name": "prof-conf-git"})
    workspace = _workspace("ws_conf_git")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    git_error = _PostValidationConformanceReportGitError(
        operation="commit",
        result=CommandResult(
            returncode=1, stdout="", stderr="locked", reason_code="GIT_COMMIT_FAILED"
        ),
    )

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_command(tmp_path)])

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-conf-git"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _run_post_validation_conformance_check=AsyncMock(side_effect=git_error),
    )
    adapter = SimpleNamespace(run=AsyncMock())

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        planning_validation_handoff=_handoff(tmp_path),
    )

    assert result.stop
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_kwargs["reason_code"] == POST_VALIDATION_CONFORMANCE_REPORT_GIT_FAILED_REASON_CODE
    assert mark_kwargs["details"]["command_reason_code"] == "GIT_COMMIT_FAILED"
    assert mark_kwargs["details"]["operation"] == "commit"


@pytest.mark.unit
async def test_conformance_report_write_error_records_errno_detail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A conformance report write failure with an errno enriches the failure details."""
    profile = WorkspaceProfile.model_validate({"name": "prof-conf-write"})
    workspace = _workspace("ws_conf_write")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    os_error = OSError("disk full")
    os_error.errno = 28
    write_error = _PostValidationConformanceReportWriteError(
        report_path=tmp_path / "report.md",
        error=os_error,
    )

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_command(tmp_path)])

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-conf-write"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _run_post_validation_conformance_check=AsyncMock(side_effect=write_error),
    )
    adapter = SimpleNamespace(run=AsyncMock())

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        planning_validation_handoff=_handoff(tmp_path),
    )

    assert result.stop
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_kwargs["reason_code"] == POST_VALIDATION_CONFORMANCE_REPORT_WRITE_FAILED_REASON_CODE
    assert mark_kwargs["details"]["errno"] == 28
    assert mark_kwargs["details"]["operation"] == "write"


@pytest.mark.unit
async def test_recovery_conformance_success_recaptures_post_conformance_head_sha(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Recovery + open PR + handoff re-captures HEAD after a passing conformance check."""
    profile = WorkspaceProfile.model_validate({"name": "prof-recovery-conf"})
    workspace = _workspace("ws_recovery_conf", pr_url="https://github.com/x/y/pull/7")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_command(tmp_path)])

    pre_head = "a" * 40
    post_conformance_head = "f" * 40
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        # First capture is the pre-validation HEAD; second is the
        # post-conformance HEAD that recovery re-captures.
        _capture_workspace_head_sha=AsyncMock(side_effect=[pre_head, post_conformance_head]),
        _start_validation_run=AsyncMock(return_value="vr-recovery-conf"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        # Conformance passes (returns None) under recovery.
        _run_post_validation_conformance_check=AsyncMock(return_value=None),
    )
    adapter = SimpleNamespace(run=AsyncMock())

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        planning_validation_handoff=_handoff(tmp_path),
        recovery={"recovery_mode": "validate_only", "source": "pr_monitor"},
    )

    assert not result.stop
    assert result.successful_validation_run_id == "vr-recovery-conf"
    # The re-captured post-conformance HEAD wins over the pre-validation HEAD.
    assert result.successful_validation_workspace_head_sha == post_conformance_head
    assert executor._capture_workspace_head_sha.await_count == 2
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["status"] == OperationStatus.succeeded
    assert finish_kwargs["reason_code"] == "VALIDATION_OK"


@pytest.mark.unit
async def test_recovery_conformance_success_keeps_prevalidation_head_when_recapture_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Recovery conformance success keeps the pre-validation HEAD when re-capture is empty.

    Exercises the false branch of the post-conformance HEAD re-capture: when the
    second ``_capture_workspace_head_sha`` returns a falsy value, the successful
    head SHA must remain the pre-validation HEAD rather than being overwritten.
    """
    profile = WorkspaceProfile.model_validate({"name": "prof-recovery-conf-empty"})
    workspace = _workspace("ws_recovery_conf_empty", pr_url="https://github.com/x/y/pull/8")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_command(tmp_path)])

    pre_head = "a" * 40
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        # Second capture (post-conformance) returns None → keep pre-validation HEAD.
        _capture_workspace_head_sha=AsyncMock(side_effect=[pre_head, None]),
        _start_validation_run=AsyncMock(return_value="vr-recovery-conf-empty"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _run_post_validation_conformance_check=AsyncMock(return_value=None),
    )
    adapter = SimpleNamespace(run=AsyncMock())

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        planning_validation_handoff=_handoff(tmp_path),
        recovery={"recovery_mode": "validate_only", "source": "pr_monitor"},
    )

    assert not result.stop
    assert result.successful_validation_run_id == "vr-recovery-conf-empty"
    # The empty re-capture left the pre-validation HEAD intact.
    assert result.successful_validation_workspace_head_sha == pre_head
    assert executor._capture_workspace_head_sha.await_count == 2


@pytest.mark.unit
async def test_fix_pass_status_recheck_race_before_agent_run_stops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A status race right before the fix-pass agent run stops the cycle cleanly."""
    profile = WorkspaceProfile.model_validate({"name": "prof-fix-recheck-agent"})
    workspace = _workspace("ws_fix_recheck_agent")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_failing_command(tmp_path)])

    # First recheck (top of loop / "validate") passes; the recheck guarding
    # the fix-pass agent run returns False to simulate a mid-flight cancel.
    recheck = AsyncMock(side_effect=[True, False])
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=recheck,
        _config=SimpleNamespace(
            max_validation_fix_passes=1,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-fix-recheck-agent"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
    )
    adapter = SimpleNamespace(run=AsyncMock())

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
    )

    assert result.stop
    assert result.successful_validation_run_id is None
    # The agent fix run never happened because the recheck failed first.
    adapter.run.assert_not_awaited()
    assert recheck.await_count == 2
    last_recheck_kwargs = recheck.await_args.kwargs
    assert last_recheck_kwargs["action"] == "validation_fix_agent_run"


@pytest.mark.unit
async def test_fix_pass_status_recheck_race_before_commit_stops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A status race right before the fix-pass commit recheck stops the cycle."""
    profile = WorkspaceProfile.model_validate({"name": "prof-fix-recheck-commit"})
    workspace = _workspace("ws_fix_recheck_commit")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_failing_command(tmp_path)])

    # rechecks: validate(True) -> validation_fix_agent_run(True) ->
    # validation_fix_commit(False) triggers the early stop at line 1112.
    recheck = AsyncMock(side_effect=[True, True, False])
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=recheck,
        _config=SimpleNamespace(
            max_validation_fix_passes=1,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-fix-recheck-commit"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _ensure_worktree_available=AsyncMock(return_value=True),
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
    )

    assert result.stop
    assert result.successful_validation_run_id is None
    # The agent fix run executed; the race happened on the post-run commit recheck.
    adapter.run.assert_awaited_once()
    assert recheck.await_count == 3
    assert recheck.await_args.kwargs["action"] == "validation_fix_commit"


@pytest.mark.unit
async def test_fix_pass_git_add_failure_records_command_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failing fix-pass ``git add`` with a reason_code records command_reason_code."""
    profile = WorkspaceProfile.model_validate({"name": "prof-fix-add-fail"})
    workspace = _workspace("ws_fix_add_fail")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_failing_command(tmp_path)])

    # The fix-pass ``git add -A`` fails and carries a command reason_code that
    # must be threaded into the failure details (line 1153).
    git_in_worktree = AsyncMock(
        return_value=CommandResult(
            returncode=1,
            stdout="",
            stderr="index lock",
            reason_code="GIT_ADD_LOCKED",
        )
    )
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=1,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-fix-add-fail"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _ensure_worktree_available=AsyncMock(return_value=True),
        _repair_agent_git_ownership=AsyncMock(),
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        git_in_worktree=git_in_worktree,
    )

    assert result.stop
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_kwargs["reason_code"] == "VALIDATION_FIX_GIT_ADD_FAILED"
    assert mark_kwargs["details"]["operation"] == "git add -A"
    assert mark_kwargs["details"]["command_reason_code"] == "GIT_ADD_LOCKED"


@pytest.mark.unit
async def test_fix_pass_plan_only_output_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A fix pass that stages only plan artifacts fails with PLAN_ONLY_OUTPUT."""
    profile = WorkspaceProfile.model_validate({"name": "prof-fix-plan-only"})
    workspace = _workspace("ws_fix_plan_only")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_failing_command(tmp_path)])

    # ``git add -A`` succeeds (ok); ``git diff --cached --name-only`` reports a
    # single staged plan artifact, which the plan-only guard rejects.
    git_in_worktree = AsyncMock(
        side_effect=[
            CommandResult(returncode=0, stdout="", stderr=""),  # git add -A
            CommandResult(
                returncode=0,
                stdout="docs/awf-plans/plan.md\n",
                stderr="",
            ),  # git diff --cached --name-only
        ]
    )
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=1,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-fix-plan-only"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _ensure_worktree_available=AsyncMock(return_value=True),
        _repair_agent_git_ownership=AsyncMock(),
        _refresh_supply_chain_policy_for_workspace=AsyncMock(
            return_value=SimpleNamespace(policy_blocked=False),
        ),
        _fail_if_plan_only_paths=AsyncMock(return_value=True),
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        git_in_worktree=git_in_worktree,
    )

    assert result.stop
    executor._fail_if_plan_only_paths.assert_awaited_once()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["status"] == OperationStatus.failed
    assert finish_kwargs["reason_code"] == PLAN_ONLY_OUTPUT_REASON_CODE


@pytest.mark.unit
async def test_post_validation_conformance_fix_pass_loop_falls_through_to_continue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A conformance fix pass that exhausts attempts re-validates then exits the loop.

    Drives a planning-validation-handoff flow where validation passes but the
    post-validation conformance check reports a gap with a remaining iteration
    budget. The conformance fix-pass synthesises a failing result, re-runs the
    agent, re-commits nothing, and loops back. The handoff is exhausted on the
    next attempt so the loop completes without ``break`` (the 260->1418 fall
    through), returning ``stop=False`` with no successful run id.
    """
    profile = WorkspaceProfile.model_validate({"name": "prof-conf-fix-loop"})
    workspace = _workspace("ws_conf_fix_loop")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_command(tmp_path)])

    # First conformance check reports a gap (remaining budget > 0 → fix pass);
    # second conformance check passes (returns None) on the next iteration.
    conformance_failure = _PlanningRunFailure(
        message="conformance gap",
        reason_code="PLAN_CONFORMANCE_UNSATISFIED",
        details={"attempt": 1},
    )
    conformance_check = AsyncMock(side_effect=[conformance_failure, None])

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(side_effect=["vr-1", "vr-2"]),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _run_post_validation_conformance_check=conformance_check,
        _ensure_worktree_available=AsyncMock(return_value=True),
        _repair_agent_git_ownership=AsyncMock(),
        _refresh_supply_chain_policy_for_workspace=AsyncMock(
            return_value=SimpleNamespace(policy_blocked=False),
        ),
        _fail_if_plan_only_paths=AsyncMock(return_value=False),
        _protected_file_diffs_for_staged_paths=AsyncMock(return_value=()),
        _runner=SimpleNamespace(run=AsyncMock(return_value=CommandResult(0, "", ""))),
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    # handoff: iteration 1 of 2 → exactly one remaining conformance iteration.
    handoff = _handoff(tmp_path, iteration=1, max_iterations=2)

    # The fix-pass produces no staged changes (empty diff) so no commit happens;
    # the loop re-validates and conformance passes on attempt 2.
    git_in_worktree = AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr=""))

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        planning_validation_handoff=handoff,
        git_in_worktree=git_in_worktree,
    )

    # Conformance passed on the second pass → successful run recorded, break.
    assert not result.stop
    assert result.successful_validation_run_id == "vr-2"
    assert conformance_check.await_count == 2
    # The conformance fix pass re-invoked the agent exactly once between checks.
    assert adapter.run.await_count == 1
