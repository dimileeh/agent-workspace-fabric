"""Validation fix-cycle branch coverage (part 2 of former part_009)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import CommandResult
from awf.control.executor import execution_validation as executor_execution_validation
from awf.control.executor import (
    execution_validation_fix_pass as executor_execution_validation_fix_pass,
)
from awf.control.executor import validation_fix_helpers as executor_validation_fix_helpers
from awf.control.executor.constants import (
    POST_VALIDATION_CONFORMANCE_REPORT_CLEANUP_FAILED_REASON_CODE,
)
from awf.control.executor.types import _PlanningRunFailure
from awf.control.quality_gates import PLAN_ONLY_OUTPUT_REASON_CODE
from awf.db.enums import FailureReason, OperationStatus, WorkspaceStatus
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import ValidationResult
from tests.unit.control.test_executor_coverage_edges_parts.test_executor_coverage_edges_part_009 import (
    _failing_command,
    _handoff,
    _passing_command,
    _patch_clean_worktree,
    _patch_profile,
    _run_cycle,
    _workspace,
)


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
    deposit_calls: list[str] = []

    def _spy_deposit(*_args: object, **kwargs: object) -> None:
        deposit_calls.append(str(kwargs["workspace_id"]))

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

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
    assert deposit_calls == [workspace.id]


@pytest.mark.unit
async def test_fix_pass_recovery_abort_marks_validating_workspace_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression PRRT_kwDOSJAM6s6MwuSg: fix-pass recovery abort is terminal."""
    profile = WorkspaceProfile.model_validate({"name": "prof-fix-recovery-abort"})
    workspace = _workspace("ws_fix_recovery_abort")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_failing_command(tmp_path)])

    async def _abort_recovery(*_args: object, **kwargs: object) -> tuple[bool, object | None]:
        assert kwargs["before_mark_failed_marks_workspace"] is True
        before_mark_failed = kwargs["before_mark_failed"]
        await before_mark_failed(reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED")
        return False, None

    monkeypatch.setattr(
        executor_execution_validation_fix_pass,
        "_run_agent_callable_with_service_recovery",
        _abort_recovery,
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
        _start_validation_run=AsyncMock(return_value="vr-fix-recovery-abort"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _ensure_worktree_available=AsyncMock(return_value=True),
    )
    adapter = SimpleNamespace(run=AsyncMock())

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
    )

    assert result.stop
    adapter.run.assert_not_awaited()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["status"] == OperationStatus.failed
    assert finish_kwargs["reason_code"] == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_kwargs["from_status"] is WorkspaceStatus.validating
    assert mark_kwargs["failure_reason"] is FailureReason.infrastructure_failure
    assert mark_kwargs["reason_code"] == "MIRROR_HOOKS_PATH_REPAIR_FAILED"


@pytest.mark.unit
async def test_unexpected_validation_cleanup_guard_deposits_planning_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When an unexpected validation exception is terminally handled by the
    cleanup guard, successful cleanup still deposits planning artifacts before
    returning."""
    profile = WorkspaceProfile.model_validate(
        {"name": "prof-unexpected-cleanup", "planning": {"required": True}}
    )
    workspace = _workspace("ws_unexpected_cleanup")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    guard_result = executor_execution_validation.ExecutionValidationResult(
        stop=True,
        successful_validation_run_id=None,
        successful_validation_workspace_head_sha=None,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_handle_validation_cleanup_guard",
        AsyncMock(return_value=guard_result),
    )

    deposit_calls: list[str] = []

    def _spy_deposit(*_args: object, **kwargs: object) -> None:
        deposit_calls.append(str(kwargs["workspace_id"]))

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            raise RuntimeError("validation runner exploded")

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-unexpected-cleanup"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
    )

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=SimpleNamespace(run=AsyncMock()),
    )

    assert result is guard_result
    assert deposit_calls == [workspace.id]
    executor._finish_validation_run.assert_not_awaited()
    executor._mark_failed.assert_not_awaited()


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
    deposit_calls: list[str] = []

    def _spy_deposit(*_args: object, **kwargs: object) -> None:
        deposit_calls.append(str(kwargs["workspace_id"]))

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

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
    assert deposit_calls == [workspace.id]


@pytest.mark.unit
@pytest.mark.parametrize(
    "unavailable_action",
    [
        "validation_fix_agent_run",
        "validation_fix_git_add",
        "validation_fix_git_diff",
        "validation_fix_git_commit",
    ],
)
async def test_fix_pass_worktree_guard_stops_deposit_planning_artifacts(
    unavailable_action: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression PRRT_kwDOSJAM6s6KxQa0: fix-pass worktree guard stops must
    deposit planning artifacts before returning stop=True.
    """
    profile = WorkspaceProfile.model_validate({"name": f"prof-{unavailable_action}"})
    workspace = _workspace(f"ws_{unavailable_action}")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)
    monkeypatch.setattr(
        executor_execution_validation_fix_pass,
        "find_protected_quality_gate_changes",
        lambda **_kwargs: [],
    )

    deposit_calls: list[str] = []

    def _spy_deposit(*_args: object, **kwargs: object) -> None:
        deposit_calls.append(str(kwargs["workspace_id"]))

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_failing_command(tmp_path)])

    async def _ensure_worktree_available(**kwargs: object) -> bool:
        return kwargs.get("action") != unavailable_action

    git_in_worktree = AsyncMock(
        side_effect=[
            CommandResult(returncode=0, stdout="", stderr=""),  # git add -A
            CommandResult(
                returncode=0,
                stdout="src/foo.py\n",
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
        _start_validation_run=AsyncMock(return_value=f"vr-{unavailable_action}"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _ensure_worktree_available=AsyncMock(side_effect=_ensure_worktree_available),
        _repair_agent_git_ownership=AsyncMock(),
        _refresh_supply_chain_policy_for_workspace=AsyncMock(
            return_value=SimpleNamespace(policy_blocked=False),
        ),
        _committed_and_staged_output_is_plan_only=AsyncMock(return_value=False),
        _active_operator_grant_specs=AsyncMock(return_value=[]),
        _protected_file_diffs_for_staged_paths=AsyncMock(return_value=()),
        _runner=SimpleNamespace(run=AsyncMock(return_value=CommandResult(0, "", ""))),
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
    assert deposit_calls == [workspace.id]


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
async def test_validation_failure_deposits_planning_artifacts_before_mark_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A terminal in-cycle validation failure deposits the plan/conformance
    artifacts BEFORE publishing the FAILED status.

    The console keys its artifact refetch on the workspace ``updated_at``
    (TaskArtifactsSection ``refreshKey``); ``_mark_failed`` bumps ``updated_at``
    when it publishes FAILED, but the filesystem deposit does not touch the row.
    Depositing after the mark would let a poll observe the terminal status in
    the window before the copy, record an empty artifact list, then never
    refetch — hiding the Plan/Validation controls. The cycle must therefore
    deposit before every in-cycle ``_mark_failed``.
    """
    profile = WorkspaceProfile.model_validate(
        {"name": "prof-validation-deposit", "planning": {"required": True}}
    )
    workspace = _workspace("ws_validation_deposit")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    order: list[str] = []
    real_deposit = (
        executor_execution_validation._planning_artifacts._deposit_planning_artifacts_best_effort
    )

    def _spy_deposit(*args: object, **kwargs: object) -> None:
        order.append("deposit")
        real_deposit(*args, **kwargs)

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

    async def _mark_failed(**_kwargs: object) -> None:
        order.append("mark_failed")

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_failing_command(tmp_path)])

    # A fix-pass ``git add -A`` failure drives a terminal in-cycle ``_mark_failed``
    # deterministically without the agent producing a real fix.
    git_in_worktree = AsyncMock(
        return_value=CommandResult(
            returncode=1, stdout="", stderr="index lock", reason_code="GIT_ADD_LOCKED"
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
        _start_validation_run=AsyncMock(return_value="vr-validation-deposit"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=_mark_failed,
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
    assert order.index("deposit") < order.index("mark_failed")


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
        _committed_and_staged_output_is_plan_only=AsyncMock(return_value=True),
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
    post_fix_handoff = AsyncMock(
        wraps=executor_validation_fix_helpers.check_post_fix_worktree_clean
    )
    monkeypatch.setattr(
        executor_validation_fix_helpers,
        "check_post_fix_worktree_clean",
        post_fix_handoff,
    )

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
    post_fix_handoff.assert_awaited_once()


@pytest.mark.unit
async def test_post_validation_conformance_report_cleanup_failure_skips_fix_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Report cleanup residue is AWF/git failure, not an agent-correctable gap."""
    profile = WorkspaceProfile.model_validate({"name": "prof-conf-cleanup"})
    workspace = _workspace("ws_conf_cleanup")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)
    deposit_calls: list[str] = []

    def _spy_deposit(*_args: object, **_kwargs: object) -> None:
        deposit_calls.append("deposit")

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_command(tmp_path)])

    conformance_failure = _PlanningRunFailure(
        message="post-validation conformance report cleanup left report path dirty: report.json",
        reason_code=POST_VALIDATION_CONFORMANCE_REPORT_CLEANUP_FAILED_REASON_CODE,
        details={"conformance_report_cleanup": {"report_path": "report.json"}},
    )
    conformance_check = AsyncMock(return_value=conformance_failure)

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=2,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-1"),
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

    # Handoff has remaining iteration budget, but cleanup residue must not trigger
    # a post-validation conformance fix pass.
    handoff = _handoff(tmp_path, iteration=1, max_iterations=3)

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        planning_validation_handoff=handoff,
    )

    assert result.stop
    assert conformance_check.await_count == 1
    assert adapter.run.await_count == 0
    assert deposit_calls == []
    executor._mark_failed.assert_awaited_once()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["status"] == OperationStatus.failed
    assert (
        finish_kwargs["reason_code"]
        == POST_VALIDATION_CONFORMANCE_REPORT_CLEANUP_FAILED_REASON_CODE
    )
    mark_failed_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_failed_kwargs["failure_reason"] == FailureReason.infrastructure_failure


@pytest.mark.unit
async def test_grant_resume_conformance_failure_skips_fix_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A grant-bearing resume never fires a conformance fix pass (PRRT_kwDOSJAM6s6KADN4).

    For a combined ``--directive ... --grant ...`` resume, ``resume_disable_fix_passes``
    is true. Even though the planning handoff has a remaining conformance iteration
    budget, a post-validation conformance miss must mark the workspace FAILED for
    operator triage rather than re-invoking the coding agent — re-running while
    operator grants are active could rewrite a granted protected file and have the
    new violation suppressed by the same single-use grant.
    """
    profile = WorkspaceProfile.model_validate({"name": "prof-grant-conf"})
    workspace = _workspace("ws_grant_conf")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_command(tmp_path)])

    conformance_failure = _PlanningRunFailure(
        message="conformance gap",
        reason_code="PLAN_CONFORMANCE_UNSATISFIED",
        details={"attempt": 1},
    )
    conformance_check = AsyncMock(return_value=conformance_failure)

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=2,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-1"),
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

    # Handoff has a remaining iteration budget (would normally allow a fix pass).
    handoff = _handoff(tmp_path, iteration=1, max_iterations=3)

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        planning_validation_handoff=handoff,
        resume_disable_fix_passes=True,
    )

    # Conformance checked once, then marked FAILED — no fix-pass agent re-invocation.
    assert result.stop
    assert conformance_check.await_count == 1
    assert adapter.run.await_count == 0
    executor._mark_failed.assert_awaited_once()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["status"] == OperationStatus.failed
    assert finish_kwargs["reason_code"] == "PLAN_CONFORMANCE_UNSATISFIED"
