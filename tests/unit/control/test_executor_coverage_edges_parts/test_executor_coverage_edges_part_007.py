"""Additional executor validation and planning edge coverage tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import CommandResult
from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.executor import execution_validation as executor_execution_validation
from awf.control.executor import validation_cleanup_guards as executor_validation_cleanup_guards
from awf.db.enums import (
    OperationStatus,
)
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import ValidationResult
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup
from awf.runtime.validation_worktree_constants import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED,
)
from awf.service import artifacts as executor_service_artifacts
from tests.unit.control.test_executor_coverage_edges_parts.test_executor_coverage_edges_part_003 import (
    _command_result,
)


def _workspace() -> SimpleNamespace:
    return SimpleNamespace(
        resolved_profile={"name": "validation-callback-race"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        test_commands=[],
        task_class=None,
        operations=[],
    )


@pytest.mark.unit
async def test_execution_validation_rejects_success_after_cleaned_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Passing validation must fail if it only passed before AWF cleaned side effects."""
    profile = WorkspaceProfile.model_validate({"name": "validation-cleaned-side-effects"})
    workspace = SimpleNamespace(
        resolved_profile={"name": "validation-cleaned-side-effects"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="Task with validation side effects",
        agent="codex",
        owned_paths=(),
        id="ws_side_effects",
    )

    pre_validation_check = ValidationWorktreeCheck(clean=True)
    side_effect_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/generated.py",),
        untracked_paths=("src/generated.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
        message="validation wrote an untracked module",
    )
    cleanup_result = ValidationWorktreeCleanup(
        cleaned=True,
        check=side_effect_check,
        restore_ref="c" * 40,
        verify_check=ValidationWorktreeCheck(clean=True),
    )

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_command_result(tmp_path, returncode=0)])

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-side-effects"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
    )
    adapter = SimpleNamespace(run=AsyncMock())

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
    monkeypatch.setattr(
        executor_execution_validation,
        "check_validation_worktree_clean",
        AsyncMock(return_value=pre_validation_check),
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "cleanup_validation_worktree_side_effects",
        AsyncMock(return_value=cleanup_result),
    )

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id="ws_side_effects",
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_side_effects",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_side_effects",
        adapter=adapter,  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(0, "", "")),
    )

    assert result.stop
    assert result.successful_validation_run_id is None
    assert result.successful_validation_workspace_head_sha is None
    finish_run_kwargs = executor._finish_validation_run.await_args.kwargs
    assert finish_run_kwargs["status"] == "failed"
    assert finish_run_kwargs["reason_code"] == VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED
    finish_pending_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_pending_kwargs["status"] == OperationStatus.failed
    assert finish_pending_kwargs["reason_code"] == VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED
    mark_failed_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_failed_kwargs["reason_code"] == VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED
    adapter.run.assert_not_awaited()

    stdout_path = (
        tmp_path
        / "artifacts"
        / "ws_side_effects"
        / "validation_worktree"
        / "vr-side-effects.side_effects.stdout"
    )
    assert "src/generated.py" in stdout_path.read_text(encoding="utf-8")


@pytest.mark.unit
async def test_execution_validation_rejects_fix_pass_dirty_worktree_without_reclosing_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fail post-fix dirty worktrees without finishing the closed validation run again."""
    profile = WorkspaceProfile.model_validate({"name": "validation-fix-pass-dirty"})
    workspace = SimpleNamespace(
        resolved_profile={"name": "validation-fix-pass-dirty"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="Task with dirty fix pass",
        agent="codex",
        owned_paths=(),
        id="ws_dirty_fix",
        task_tag=None,
    )

    setup_check = ValidationWorktreeCheck(clean=True)
    dirty_fix_pass_check = ValidationWorktreeCheck(
        clean=False,
        paths=("generated.log",),
        untracked_paths=("generated.log",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
        message="fix pass left an untracked file",
    )

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_command_result(tmp_path, returncode=1)])

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(max_validation_fix_passes=1, planning_max_iterations_default=3),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-fix-dirty"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _repair_agent_git_ownership=AsyncMock(),
        _ensure_worktree_available=AsyncMock(return_value=True),
        _refresh_supply_chain_policy_for_workspace=AsyncMock(
            return_value=SimpleNamespace(policy_blocked=False),
        ),
        _fail_if_plan_only_paths=AsyncMock(return_value=False),
        _protected_file_diffs_for_staged_paths=AsyncMock(return_value=()),
        _runner=SimpleNamespace(run=AsyncMock(return_value=CommandResult(0, "", ""))),
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

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
    monkeypatch.setattr(
        executor_execution_validation,
        "check_validation_worktree_clean",
        AsyncMock(side_effect=[setup_check, dirty_fix_pass_check]),
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "cleanup_validation_worktree_side_effects",
        AsyncMock(
            return_value=ValidationWorktreeCleanup(
                cleaned=True,
                check=setup_check,
                restore_ref="c" * 40,
            )
        ),
    )

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id="ws_dirty_fix",
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_dirty_fix",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_dirty_fix",
        adapter=adapter,  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert result.successful_validation_run_id is None
    executor._start_validation_run.assert_awaited_once()
    executor._finish_validation_run.assert_awaited_once()
    finish_run_kwargs = executor._finish_validation_run.await_args.kwargs
    assert finish_run_kwargs["status"] == "failed"
    assert finish_run_kwargs["reason_code"] == "COVERAGE_BELOW_THRESHOLD"
    finish_pending_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_pending_kwargs["validation_run_id"] is None
    assert finish_pending_kwargs["status"] == OperationStatus.failed
    assert finish_pending_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert adapter.run.await_count == 1


@pytest.mark.unit
async def test_execution_validation_stops_if_callback_becomes_stale_after_cleanup_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_id = "ws_callback_race"
    profile = WorkspaceProfile.model_validate(
        {"name": "validation-callback-race", "planning": {"required": True}}
    )
    workspace = _workspace()
    calls: list[str] = []
    real_deposit = (
        executor_execution_validation._planning_artifacts._deposit_planning_artifacts_best_effort
    )

    def _spy_deposit(*_args: object, **_kwargs: object) -> None:
        calls.append("deposit")
        real_deposit(*_args, **_kwargs)

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

    class _ThrowingValidation:
        async def run_profile_phases(self, **_kwargs: object) -> object:
            raise ComposeExecCleanupError(
                invocation_id="validate-race",
                source="agent",
                label="validate",
                message="cleanup timed out",
            )

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-callback-race"),
        _finish_validation_callback_if_terminal=AsyncMock(side_effect=[False, True]),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _update_subphase=AsyncMock(),
        _validation=_ThrowingValidation(),
    )

    async def _sync_resolved_profile(*_args: object, **_kwargs: object) -> WorkspaceProfile:
        return profile

    worktree_path = tmp_path / "worktree"
    plan_path = worktree_path / "docs" / "awf-plans" / f"{workspace_id}.md"
    report_path = worktree_path / "docs" / "awf-plans" / f"{workspace_id}.conformance.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# plan\n", encoding="utf-8")
    report_path.write_text('{"status":"satisfied"}', encoding="utf-8")

    monkeypatch.setattr(
        executor_execution_validation,
        "_profile_for_workspace",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_sync_resolved_profile",
        _sync_resolved_profile,
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

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id=workspace_id,
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=worktree_path,
        compose_project="awf_ws_callback_race",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_callback_race",
        adapter=object(),  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert result.successful_validation_run_id is None
    assert executor._finish_validation_callback_if_terminal.await_count == 2
    executor._finish_validation_run.assert_not_awaited()
    executor._finish_pending_validate_operations.assert_not_awaited()
    assert calls == ["deposit"]
    artifact_dir = executor_service_artifacts.workspace_artifact_dir(
        (tmp_path / "artifacts").parent, workspace_id
    )
    assert (artifact_dir / "plan.md").read_text(encoding="utf-8") == "# plan\n"
    deposited_report = json.loads((artifact_dir / "conformance.json").read_text(encoding="utf-8"))
    assert deposited_report["status"] == "satisfied"


@pytest.mark.unit
async def test_execution_validation_deposits_artifacts_before_stale_callback_after_normal_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A callback-terminal status after normal cleanup must still deposit planning artifacts."""
    profile = WorkspaceProfile.model_validate(
        {"name": "validation-callback-terminal-deposit", "planning": {"required": True}}
    )
    workspace = _workspace()
    calls: list[str] = []
    real_deposit = (
        executor_execution_validation._planning_artifacts._deposit_planning_artifacts_best_effort
    )

    def _spy_deposit(*_args: object, **_kwargs: object) -> None:
        calls.append("deposit")
        real_deposit(*_args, **_kwargs)

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_command_result(tmp_path, returncode=0)])

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-callback-terminal-deposit"),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=True),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
    )

    async def _sync_resolved_profile(*_args: object, **_kwargs: object) -> WorkspaceProfile:
        return profile

    worktree_path = tmp_path / "worktree"
    plan_path = worktree_path / "docs" / "awf-plans" / "ws_callback_terminal_deposit.md"
    report_path = (
        worktree_path / "docs" / "awf-plans" / "ws_callback_terminal_deposit.conformance.json"
    )
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# plan\n", encoding="utf-8")
    report_path.write_text('{"status":"satisfied"}', encoding="utf-8")

    monkeypatch.setattr(
        executor_execution_validation,
        "_profile_for_workspace",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_sync_resolved_profile",
        _sync_resolved_profile,
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

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id="ws_callback_terminal_deposit",
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=worktree_path,
        compose_project="awf_ws_callback_terminal_deposit",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_callback_terminal_deposit",
        adapter=object(),  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert calls == ["deposit"]
    executor._finish_validation_callback_if_terminal.assert_awaited_once()
    artifact_dir = executor_service_artifacts.workspace_artifact_dir(
        (tmp_path / "artifacts").parent, "ws_callback_terminal_deposit"
    )
    assert (artifact_dir / "plan.md").read_text(encoding="utf-8") == "# plan\n"
    deposited_report = json.loads((artifact_dir / "conformance.json").read_text(encoding="utf-8"))
    assert deposited_report["status"] == "satisfied"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("callback_results", "expected_callback_checks", "workspace_suffix"),
    [
        pytest.param((True,), 1, "already_stale", id="callback-already-stale"),
        pytest.param((False, True), 2, "became_stale", id="callback-becomes-stale"),
    ],
)
async def test_execution_validation_fails_cleanup_when_callback_becomes_stale_after_cleanup_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    callback_results: tuple[bool, ...],
    expected_callback_checks: int,
    workspace_suffix: str,
) -> None:
    profile = WorkspaceProfile.model_validate({"name": "validation-stale-cleanup-failure"})
    workspace_id = f"ws_stale_cleanup_failure_{workspace_suffix}"
    validation_run_id = f"vr-stale-cleanup-failure-{workspace_suffix}"
    workspace = _workspace()

    class _ThrowingValidation:
        async def run_profile_phases(self, **_kwargs: object) -> object:
            raise ComposeExecCleanupError(
                invocation_id="validate-stale-cleanup-failure",
                source="agent",
                label="validate",
                message="cleanup timed out",
            )

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(max_validation_fix_passes=0, planning_max_iterations_default=3),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value=validation_run_id),
        _finish_validation_callback_if_terminal=AsyncMock(side_effect=callback_results),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _update_subphase=AsyncMock(),
        _validation=_ThrowingValidation(),
    )

    async def _sync_resolved_profile(*_args: object, **_kwargs: object) -> WorkspaceProfile:
        return profile

    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("generated.log",),
        untracked_paths=("generated.log",),
    )
    cleanup_result = ValidationWorktreeCleanup(
        cleaned=False,
        check=dirty_check,
        restore_ref="c" * 40,
        reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        message="restore failed",
        cleanup_command="git restore --source cccccccc -- generated.log",
        cleanup_stderr="restore failed",
        verify_check=dirty_check,
    )

    monkeypatch.setattr(
        executor_execution_validation,
        "_profile_for_workspace",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_sync_resolved_profile",
        _sync_resolved_profile,
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
    monkeypatch.setattr(
        executor_execution_validation,
        "check_validation_worktree_clean",
        AsyncMock(return_value=ValidationWorktreeCheck(clean=True)),
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "cleanup_validation_worktree_side_effects",
        AsyncMock(return_value=cleanup_result),
    )
    record_stale_cleanup_failure = AsyncMock()
    monkeypatch.setattr(
        executor_validation_cleanup_guards,
        "_record_stale_validation_cleanup_failure",
        record_stale_cleanup_failure,
    )

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id=workspace_id,
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch=f"awf/{workspace_id}",
        adapter=object(),  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert result.successful_validation_run_id is None
    assert executor._finish_validation_callback_if_terminal.await_count == expected_callback_checks
    executor._finish_validation_run.assert_not_awaited()
    finish_pending_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_pending_kwargs["workspace_id"] == workspace_id
    assert finish_pending_kwargs["status"] == OperationStatus.failed
    assert finish_pending_kwargs["validation_run_id"] is None
    assert finish_pending_kwargs["requested_tier"] == 1
    assert finish_pending_kwargs["reason_code"] == VALIDATION_WORKTREE_CLEANUP_FAILED
    mark_failed_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_failed_kwargs["workspace_id"] == workspace_id
    assert mark_failed_kwargs["reason_code"] == VALIDATION_WORKTREE_CLEANUP_FAILED
    record_stale_cleanup_failure.assert_awaited_once()
    assert record_stale_cleanup_failure.await_args.kwargs["workspace_id"] == workspace_id
    assert (
        record_stale_cleanup_failure.await_args.kwargs["reason_code"]
        == VALIDATION_WORKTREE_CLEANUP_FAILED
    )
