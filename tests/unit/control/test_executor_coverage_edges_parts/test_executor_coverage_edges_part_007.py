"""Additional executor validation and planning edge coverage tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.executor import execution_validation as executor_execution_validation
from awf.control.executor import helpers as executor_helpers
from awf.control.executor import planning_ops as executor_planning_ops
from awf.control.executor.helpers import (
    _failure_salvage_payload,
    _profile_with_planning_iteration_default,
    _raw_profile_has_explicit_planning_max_iterations,
    _validation_tier_for_workspace,
)
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, TaskClass
from awf.profiles.models import ProfilePlanning, WorkspaceProfile
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    PLAN_CONFORMANCE_UNSATISFIED,
    PlanConformanceReport,
    PlanConformanceStatus,
)
from awf.runtime.validation import ValidationResult
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup
from awf.runtime.validation_worktree_constants import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
)
from tests.unit.control.test_executor_coverage_edges_parts.test_executor_coverage_edges_part_003 import (
    _command_result,
    _executor_with_runner,
    _PlanningAdapter,
)


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
        has_known_non_plan_output=False,
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
async def test_execution_validation_rejects_fix_pass_ignored_artifacts_when_snapshot_drifts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fail when validation fix pass generates ignored files not present at setup."""
    profile = WorkspaceProfile.model_validate({"name": "validation-ignored-baseline"})
    workspace = SimpleNamespace(
        resolved_profile={"name": "validation-ignored-baseline"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="Task with ignored artifact",
        agent="codex",
        owned_paths=(),
        id="ws_ignored",
    )

    setup_check = ValidationWorktreeCheck(
        clean=True,
        ignored_paths=("ignored-output/",),
        ignored_paths_snapshot=("ignored-output/stable.txt",),
    )
    fix_pass_check = ValidationWorktreeCheck(
        clean=True,
        ignored_paths=("ignored-output/",),
        ignored_paths_snapshot=("ignored-output/stable.txt", "ignored-output/new.pyc"),
    )

    class _Validation:
        def __init__(self) -> None:
            self.run_profile_calls = 0

        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            self.run_profile_calls += 1
            returncode = 1 if self.run_profile_calls == 1 else 0
            return ValidationResult(commands=[_command_result(tmp_path, returncode=returncode)])

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(max_validation_fix_passes=1, planning_max_iterations_default=3),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(side_effect=["vr-1", "vr-2"]),
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
    git_in_worktree = AsyncMock(
        side_effect=[
            CommandResult(0, "", ""),
            CommandResult(0, "", ""),
        ]
    )

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
        AsyncMock(side_effect=[setup_check, fix_pass_check, fix_pass_check]),
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
        workspace_id="ws_ignored",
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_ignored",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_ignored",
        adapter=adapter,  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        has_known_non_plan_output=False,
        git_in_worktree=git_in_worktree,
    )

    assert result.stop
    assert result.successful_validation_run_id is None
    assert result.successful_validation_workspace_head_sha is None
    assert executor._validation.run_profile_calls == 1
    assert executor._start_validation_run.await_count == 2
    assert executor._finish_validation_run.await_count == 2
    finish_kwargs = executor._finish_validation_run.await_args.kwargs
    assert finish_kwargs["status"] == "failed"
    assert finish_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert executor._mark_failed.await_count == 1
    assert adapter.run.await_count == 1


@pytest.mark.unit
async def test_execution_validation_stops_if_callback_becomes_stale_after_cleanup_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = WorkspaceProfile.model_validate({"name": "validation-callback-race"})
    workspace = SimpleNamespace(
        resolved_profile={"name": "validation-callback-race"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        test_commands=[],
        task_class=None,
        operations=[],
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
        _config=SimpleNamespace(max_validation_fix_passes=0, planning_max_iterations_default=3),
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
        workspace_id="ws_callback_race",
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
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
        has_known_non_plan_output=False,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert result.successful_validation_run_id is None
    assert executor._finish_validation_callback_if_terminal.await_count == 2
    executor._finish_validation_run.assert_not_awaited()
    executor._finish_pending_validate_operations.assert_not_awaited()


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
    workspace = SimpleNamespace(
        resolved_profile={"name": "validation-stale-cleanup-failure"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        test_commands=[],
        task_class=None,
        operations=[],
    )

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
        has_known_non_plan_output=False,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert result.successful_validation_run_id is None
    assert executor._finish_validation_callback_if_terminal.await_count == expected_callback_checks
    executor._finish_validation_run.assert_awaited_once_with(
        validation_run_id,
        status="failed",
        reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
    )
    finish_pending_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_pending_kwargs["workspace_id"] == workspace_id
    assert finish_pending_kwargs["status"] == OperationStatus.failed
    assert finish_pending_kwargs["validation_run_id"] == validation_run_id
    assert finish_pending_kwargs["requested_tier"] == 1
    assert finish_pending_kwargs["reason_code"] == VALIDATION_WORKTREE_CLEANUP_FAILED
    mark_failed_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_failed_kwargs["workspace_id"] == workspace_id
    assert mark_failed_kwargs["reason_code"] == VALIDATION_WORKTREE_CLEANUP_FAILED


@pytest.mark.unit
def test_executor_metadata_helpers_cover_unreadable_and_invalid_edges(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class UnreadablePath:
        def is_file(self) -> bool:
            return True

        def open(self, *_args: object, **_kwargs: object) -> object:
            raise OSError("cannot read")

    assert executor_helpers._digest_file_if_present(UnreadablePath()) is None  # type: ignore[arg-type]

    unreadable = tmp_path / "unreadable.md"
    unreadable.write_text("content", encoding="utf-8")
    original_open = Path.open

    def _raise_for_unreadable(self: Path, *args: object, **kwargs: object) -> object:
        if self == unreadable:
            raise OSError("permission denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _raise_for_unreadable)

    assert executor_helpers._digest_file_if_present(unreadable) is None  # noqa: SLF001
    assert (
        executor_helpers._requested_tier_from_metadata(  # noqa: SLF001
            {"validation": {"requested_tier": 0}}
        )
        is None
    )
    assert (
        executor_helpers._requested_tier_from_metadata(  # noqa: SLF001
            {"validation": {"requested_tier": True}}
        )
        is None
    )

    profile = WorkspaceProfile.model_validate({"name": "tier", "validation": {"requested_tier": 1}})
    workspace = SimpleNamespace(
        task_class=TaskClass.build_config_task.value,
        operations=[
            SimpleNamespace(
                type=OperationType.validate,
                status=OperationStatus.succeeded,
                payload={"requested_tier": 2},
                result={"validation": {"requested_tier": 4}},
            ),
            SimpleNamespace(
                type=OperationType.validate,
                status=OperationStatus.running,
                payload={"validation": {"requested_tier": 3}},
            ),
        ],
    )

    assert _validation_tier_for_workspace(workspace, profile) == 4  # type: ignore[arg-type]
    assert executor_helpers._validate_operation_requested_tier(workspace) == 4  # noqa: SLF001

    active_tier_workspace = SimpleNamespace(
        operations=[
            SimpleNamespace(
                type=OperationType.validate,
                status=OperationStatus.succeeded,
                payload={"requested_tier": 1},
                result={"requested_tier": 2},
            ),
            SimpleNamespace(
                type=OperationType.validate,
                status=OperationStatus.running,
                payload={"validation": {"requested_tier": 3}},
            ),
        ],
    )

    assert executor_helpers._validate_operation_requested_tier(active_tier_workspace) == 3  # noqa: SLF001
    assert _validation_tier_for_workspace(workspace, profile) == 4  # type: ignore[arg-type]

    coverage = executor_helpers._coverage_result_from_metadata(  # noqa: SLF001
        {
            "provider": "",
            "percent": "99.0",
            "minimum_percent": "99",
            "enforce": "yes",
            "status": "",
            "reason_code": "",
            "gaps": [{"file": "src/awf/control/executor.py"}, "ignored"],
            "failing_test_node_ids": ["tests/test_a.py::test_one", 42],
            "failing_test_evidence": [object(), "AssertionError"],
            "provider_failure_evidence": ["provider down", None],
            "parallel_workers_requested": "8",
            "parallel_workers_effective": 8,
            "parallel_distribution": 5,
        }
    )

    assert coverage.provider == "python"
    assert coverage.percent is None
    assert coverage.minimum_percent == 0.0
    assert coverage.enforce is True
    assert coverage.status == "passed"
    assert coverage.reason_code == "COVERAGE_OK"
    assert coverage.gaps == [{"file": "src/awf/control/executor.py"}]
    assert coverage.failing_test_node_ids == ["tests/test_a.py::test_one"]
    assert coverage.failing_test_evidence == ["AssertionError"]
    assert coverage.provider_failure_evidence == ["provider down"]
    assert coverage.parallel_workers_requested is None
    assert coverage.parallel_workers_effective == 8
    assert coverage.parallel_distribution is None


@pytest.mark.unit
async def test_planning_conformance_reraises_non_timeout_agent_error(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # baseline HEAD
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_non_timeout.md\n")
    runner.queue_result(returncode=0, stdout="")  # committed paths
    runner.queue_result(returncode=0, stdout="sha1\n")  # implementation baseline
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_non_timeout.md\n")
    executor = _executor_with_runner(runner, tmp_path)

    class _NonTimeoutConformanceAdapter(_PlanningAdapter):
        async def run(self, **kwargs: object) -> SimpleNamespace:
            if len(self.prompts) == 2:
                prompt = kwargs.get("prompt")
                assert isinstance(prompt, str)
                self.prompts.append(prompt)
                raise AgentRunError(
                    agent=AgentRuntime.codex,
                    result=CommandResult(returncode=2, stdout="", stderr="tool failed"),
                    reason_code="AGENT_CLI_FAILED",
                )
            return await super().run(**kwargs)

    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-non-timeout",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
            },
        }
    )
    adapter = _NonTimeoutConformanceAdapter("plan", "implementation")

    with pytest.raises(AgentRunError, match="AGENT_CLI_FAILED"):
        await executor._run_agent_task_with_optional_planning(
            adapter=adapter,  # type: ignore[arg-type]
            workspace=SimpleNamespace(id="ws_non_timeout", task_prompt="do it"),  # type: ignore[arg-type]
            profile=profile,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            worktree_path=tmp_path / "worktree",
            model=None,
        )


@pytest.mark.unit
async def test_planning_conformance_timeout_uses_fresh_report_file(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    report_file = worktree / "docs" / "awf-plans" / "ws_timeout.json"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # baseline HEAD
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_timeout.md\n")
    runner.queue_result(returncode=0, stdout="")  # committed paths
    runner.queue_result(returncode=0, stdout="sha1\n")  # implementation baseline
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_timeout.md\n")
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_timeout.md\n?? docs/awf-plans/ws_timeout.json\n",
    )
    runner.queue_result(returncode=0, stdout="sha1\n")
    executor = _executor_with_runner(runner, tmp_path)

    class _TimeoutConformanceAdapter(_PlanningAdapter):
        async def run(self, **kwargs: object) -> SimpleNamespace:
            if len(self.prompts) == 2:
                prompt = kwargs.get("prompt")
                assert isinstance(prompt, str)
                self.prompts.append(prompt)
                report_file.parent.mkdir(parents=True, exist_ok=True)
                report_file.write_text(
                    '{"status":"needs_iteration","summary":"still missing","gaps":["gap"]}',
                    encoding="utf-8",
                )
                raise AgentRunError(
                    agent=AgentRuntime.codex,
                    result=CommandResult(returncode=124, stdout="", stderr="timeout"),
                    reason_code="AGENT_TIMEOUT",
                )
            return await super().run(**kwargs)

    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-timeout",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
            },
        }
    )
    adapter = _TimeoutConformanceAdapter("plan", "implementation")

    failure = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_timeout", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert failure is not None
    assert not isinstance(failure, str)
    assert failure.reason_code == executor_planning_ops.AGENT_STALLED_IN_CONFORMANCE  # noqa: SLF001
    assert failure.details is not None
    assert failure.details["conformance"]["gaps"] == ["gap"]


@pytest.mark.unit
async def test_conformance_stall_failure_records_diff_and_event_failures(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._git_rev_parse_head = AsyncMock(return_value="h" * 40)  # type: ignore[method-assign]
    executor._git_commit_count_since = AsyncMock(return_value=2)  # type: ignore[method-assign]
    executor._committed_paths_since = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("diff failed")
    )

    class _RaisingSessionContext:
        async def __aenter__(self) -> object:
            raise RuntimeError("database unavailable")

        async def __aexit__(
            self,
            exc_type: object,
            exc: object,
            traceback: object,
        ) -> None:
            del exc_type, exc, traceback

    executor._session_factory = lambda: _RaisingSessionContext()  # type: ignore[method-assign]
    stall = executor_planning_ops.ConformanceStallEvidence(  # noqa: SLF001
        kind=executor_planning_ops.ConformanceStallKind.no_output,  # noqa: SLF001
        iteration_index=1,
        elapsed_seconds=700.0,
        no_output_seconds=700.0,
        repeated_output_count=0,
        last_report_digest=None,
        plan_path="docs/plan.md",
        report_path="docs/report.json",
    )
    last_report = PlanConformanceReport(
        status=PlanConformanceStatus.needs_iteration,
        summary="still missing validation",
        gaps=("rerun tests",),
    )

    failure = await executor._build_conformance_stall_failure(  # noqa: SLF001
        workspace=SimpleNamespace(id="ws_stall"),  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        baseline_sha="b" * 40,
        last_report=last_report,
        stall=stall,
        iterations_used=2,
        max_iterations=3,
        plan_path=Path("docs/plan.md"),
        report_path=Path("docs/report.json"),
        recovery_action="notify",
    )

    assert failure.reason_code == executor_planning_ops.AGENT_STALLED_IN_CONFORMANCE  # noqa: SLF001
    assert failure.details is not None
    salvage_hint = failure.details["conformance_stall"]["salvage_hint"]
    assert salvage_hint["implementation_commit_count"] == 2
    assert salvage_hint["changed_paths"] == []
    assert failure.details["conformance"]["gaps"] == ["rerun tests"]


@pytest.mark.unit
async def test_planning_required_reports_invalid_rendered_paths(tmp_path: Path) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    adapter = _PlanningAdapter()
    profile = WorkspaceProfile.model_construct(
        name="planning-invalid-path",
        planning=ProfilePlanning.model_construct(
            required=True,
            plan_path="/tmp/{workspace_id}.md",
            conformance_report_path="docs/awf-plans/{workspace_id}.json",
            max_iterations=0,
            enforce_plan_only_changes=True,
            fail_on_unexplained_deviation=True,
        ),
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_bad_path", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is not None
    assert message.startswith("planning profile is invalid:")
    assert adapter.prompts == []


@pytest.mark.unit
async def test_planning_required_rejects_extra_plan_phase_changes(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_plan_extra.md\n?? src/changed.py\n",
    )  # dirty_paths
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter("plan plus code")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-extra",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_plan_extra", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is not None
    assert not isinstance(message, str)
    assert message.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert message.message.startswith(
        "planning phase changed files outside `docs/awf-plans/ws_plan_extra.md`"
    )
    assert message.details is not None
    scope = message.details["planning_scope"]
    assert scope["required_paths"] == ["docs/awf-plans/ws_plan_extra.md"]
    assert scope["offending_paths"] == ["src/changed.py"]
    assert scope["recovery_strategy"] == "discard_and_replan"
    assert "preserved branch" in scope["recommended_action"]
    assert len(adapter.prompts) == 1


@pytest.mark.unit
async def test_planning_required_allows_extra_plan_changes_when_policy_disabled(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_plan_unenforced.md\n?? src/changed.py\n",
    )
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_plan_unenforced.md\n?? src/changed.py\n",
    )
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_plan_unenforced.md\n?? src/changed.py\n",
    )
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan plus code",
        "implementation",
        '{"status":"satisfied","summary":"done","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-extra-unenforced",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "enforce_plan_only_changes": False,
                "max_iterations": 0,
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_plan_unenforced", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None


@pytest.mark.unit
async def test_conformance_phase_rejects_extra_report_phase_changes(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan (1)
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD (2)
    runner.queue_result(
        returncode=0, stdout="?? docs/awf-plans/ws_compare.md\n"
    )  # dirty after plan (3)
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since (empty) (4)
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop (5)
    runner.queue_result(
        returncode=0, stdout="?? docs/awf-plans/ws_compare.md\n"
    )  # before_compare (6)
    runner.queue_result(
        returncode=0,
        stdout=(
            "?? docs/awf-plans/ws_compare.md\n"
            "?? docs/awf-plans/ws_compare.json\n"
            "?? src/side_effect.py\n"
        ),
    )  # after_compare (7) — but should not get this far on scope violation
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan",
        "implementation",
        '{"status":"satisfied","summary":"done","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-conformance-extra",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_compare", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is not None
    assert not isinstance(message, str)
    assert message.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert message.message.startswith(
        "conformance phase changed files outside `docs/awf-plans/ws_compare.json`"
    )
    assert message.details is not None
    scope = message.details["planning_scope"]
    assert scope["scope_phase"] == "conformance"
    assert scope["required_paths"] == ["docs/awf-plans/ws_compare.json"]
    assert scope["offending_paths"] == ["src/side_effect.py"]


@pytest.mark.unit
async def test_conformance_phase_allows_side_effects_when_deviation_policy_disabled(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_compare_unenforced.md\n")
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_compare_unenforced.md\n")
    runner.queue_result(
        returncode=0,
        stdout=(
            "?? docs/awf-plans/ws_compare_unenforced.md\n"
            "?? docs/awf-plans/ws_compare_unenforced.json\n"
            "?? src/side_effect.py\n"
        ),
    )
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan",
        "implementation",
        '{"status":"satisfied","summary":"done","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-conformance-unenforced",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "fail_on_unexplained_deviation": False,
                "max_iterations": 0,
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_compare_unenforced", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None


@pytest.mark.unit
async def test_planning_required_allows_extra_changes_when_profile_disables_guards(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_permissive.md\n?? src/allowed.py\n",
    )  # dirty after plan
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_permissive.md\n?? src/allowed.py\n",
    )  # before_compare
    runner.queue_result(
        returncode=0,
        stdout=(
            "?? docs/awf-plans/ws_permissive.md\n"
            "?? docs/awf-plans/ws_permissive.json\n"
            "?? src/allowed.py\n"
            "?? src/compare_extra.py\n"
        ),
    )  # after_compare
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan",
        "implementation",
        '{"status":"satisfied","summary":"ok","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-permissive",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
                "enforce_plan_only_changes": False,
                "fail_on_unexplained_deviation": False,
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_permissive", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None
    assert len(adapter.prompts) == 3


@pytest.mark.unit
async def test_planning_required_reports_unsatisfied_conformance_after_iterations(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_unsat.md\n")  # dirty after plan
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_unsat.md\n")  # before_compare
    runner.queue_result(
        returncode=0, stdout="?? docs/awf-plans/ws_unsat.md\n"
    )  # after_compare (first)
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_unsat.md\n?? docs/awf-plans/ws_unsat.json\n",
    )  # after_compare (second) — unused on max_iterations=0
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan",
        "implementation",
        '{"status":"needs_iteration","summary":"more tests needed","gaps":["gap one"]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-unsatisfied",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
            },
        }
    )

    failure = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_unsat", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert failure is not None
    assert not isinstance(failure, str)
    assert failure.message == "plan conformance was not satisfied after 0 iteration(s): gap one"
    assert failure.reason_code == PLAN_CONFORMANCE_UNSATISFIED
    assert failure.details["conformance"] == {
        "summary": "more tests needed",
        "gaps": ["gap one"],
        "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
        "report_reason_code": "PLAN_CONFORMANCE_REPORTED",
        "iterations_used": 1,
        "max_iterations": 0,
        "plan_path": "docs/awf-plans/ws_unsat.md",
        "report_path": "docs/awf-plans/ws_unsat.json",
    }


@pytest.mark.unit
def test_planning_iteration_settings_default_applies_only_when_profile_omits_value() -> None:
    omitted = WorkspaceProfile.model_validate(
        {"name": "planning-default", "planning": {"required": True}}
    )
    explicit = WorkspaceProfile.model_validate(
        {
            "name": "planning-explicit",
            "planning": {"required": True, "max_iterations": 1},
        }
    )

    assert _profile_with_planning_iteration_default(omitted, 4).planning.max_iterations == 4
    assert _profile_with_planning_iteration_default(explicit, 4).planning.max_iterations == 1


@pytest.mark.unit
def test_raw_profile_planning_detection_handles_missing_profile() -> None:
    assert _raw_profile_has_explicit_planning_max_iterations(None) is False
    assert _raw_profile_has_explicit_planning_max_iterations({"planning": {}}) is False
    assert (
        _raw_profile_has_explicit_planning_max_iterations({"planning": {"required": True}}) is False
    )
    assert (
        _raw_profile_has_explicit_planning_max_iterations({"planning": {"max_iterations": 0}})
        is True
    )
    assert (
        _raw_profile_has_explicit_planning_max_iterations({"planning": {"max_iterations": 2}})
        is True
    )


@pytest.mark.unit
def test_failure_salvage_payload_omits_empty_branch_fields(tmp_path: Path) -> None:
    payload = _failure_salvage_payload(
        SimpleNamespace(branch_name=None, remote_push_branch=None),  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
    )

    assert payload == {
        "hint": "Workspace worktree and branch were preserved for salvage.",
        "worktree_path": str(tmp_path / "worktree"),
    }


@pytest.mark.unit
def test_failure_salvage_payload_defaults_remote_branch_to_branch(tmp_path: Path) -> None:
    payload = _failure_salvage_payload(
        SimpleNamespace(branch_name="awf/ws_123", remote_push_branch=None),  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
    )

    assert payload["branch_name"] == "awf/ws_123"
    assert payload["remote_push_branch"] == "awf/ws_123"


@pytest.mark.unit
async def test_changed_paths_raises_when_git_status_fails(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=128, stderr="not a git repository")
    executor = _executor_with_runner(runner, tmp_path)

    with pytest.raises(RuntimeError, match="git status failed"):
        await executor._changed_paths(tmp_path / "worktree")
