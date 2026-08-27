"""Hosted PR adoption validation/conformance edge coverage tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.adapters.base import AgentRunResult
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.control.executor import execution_validation as executor_execution_validation
from awf.control.executor.types import _PlanningValidationHandoff, _RebaseRecoveryResult
from awf.profiles.models import WorkspaceProfile
from awf.runtime.planning import (
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    PlanConformanceReport,
    PlanConformanceStatus,
)
from awf.runtime.validation import ValidationResult
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup
from tests.unit.control.test_executor_coverage_edges_parts.test_executor_coverage_edges_part_003 import (
    _command_result,
)


@pytest.mark.unit
async def test_hosted_post_validation_conformance_receives_pr_identity_and_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Hosted adopted-PR conformance must run against the hosted PR checkout."""
    profile = WorkspaceProfile.model_validate(
        {"name": "hosted-conformance", "planning": {"required": True}}
    )
    initial_head = "c" * 40
    workspace = SimpleNamespace(
        resolved_profile={"name": "hosted-conformance"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="Hosted conformance",
        task_prompt="implement the plan",
        agent="codex",
        owned_paths=("src/awf",),
        id="ws_hosted_conformance",
        task_tag=None,
        task_policy={
            "pr_adoption": {
                "execution": {"mode": "hosted"},
                "pr_url": "https://github.com/example/repo/pull/764",
                "pr_number": 764,
                "base_ref": "main",
                "head_ref": "awf/pr-764",
                "head_repo_url": "https://github.com/fork/repo.git",
                "head_sha": initial_head,
            }
        },
        repo_url="https://github.com/example/repo.git",
        pr_url="https://github.com/example/repo/pull/764",
        pr_number=764,
        branch_base="main",
        remote_push_branch="awf/pr-764",
        monitor_last_commit_sha=initial_head,
    )

    class _UnexpectedLocalValidation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            raise AssertionError("hosted PR adoption must use the hosted validator")

    class _HostedValidation:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def run_profile_phases(self, **kwargs: object) -> ValidationResult:
            self.calls.append(kwargs)
            return ValidationResult(commands=[_command_result(tmp_path, returncode=0)])

    class _ConformanceAdapter:
        is_hosted = True

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def run(self, **kwargs: object) -> AgentRunResult:
            self.calls.append(kwargs)
            return AgentRunResult(
                returncode=0,
                stdout='{"status":"satisfied","summary":"hosted PR validated","gaps":[]}',
                stderr="",
            )

    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # after conformance changed paths
    runner.queue_result(returncode=0, stdout="")  # committed paths since validated HEAD
    runner.queue_result(returncode=1, stderr="error: path not tracked")  # report restore
    executor = WorkspaceExecutor(
        session_factory=object(),  # type: ignore[arg-type]
        runner=runner,
        compose=object(),  # type: ignore[arg-type]
        validation=object(),  # type: ignore[arg-type]
        pr_creator=object(),  # type: ignore[arg-type]
        config=ExecutorConfig(
            worktrees_root=tmp_path / "worktrees",
            compose_projects_root=tmp_path / "compose",
        ),
    )
    executor._transition_if_current = AsyncMock(return_value=True)  # type: ignore[method-assign]
    executor._recheck_status = AsyncMock(return_value=True)  # type: ignore[method-assign]
    executor._capture_workspace_head_sha = AsyncMock(return_value=initial_head)  # type: ignore[method-assign]
    executor._start_validation_run = AsyncMock(return_value="vr-hosted-conformance")  # type: ignore[method-assign]
    executor._finish_validation_run = AsyncMock()  # type: ignore[method-assign]
    executor._finish_pending_validate_operations = AsyncMock()  # type: ignore[method-assign]
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]
    executor._finish_validation_callback_if_terminal = AsyncMock(return_value=False)  # type: ignore[method-assign]
    executor._update_subphase = AsyncMock()  # type: ignore[method-assign]
    executor._validation = _UnexpectedLocalValidation()  # type: ignore[assignment]
    executor._hosted_validation = _HostedValidation()  # type: ignore[attr-defined]
    executor._validation_run_evidence_for_conformance = AsyncMock(return_value="VALIDATION_OK")  # type: ignore[method-assign]
    executor._record_post_validation_conformance_event = AsyncMock()  # type: ignore[method-assign]
    executor._capture_post_validation_conformance_scope_baseline = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            before_compare=set(),
            before_compare_head="validated-head",
            before_dirty_digests={},
        )
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
        AsyncMock(return_value=ValidationWorktreeCheck(clean=True)),
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "cleanup_validation_worktree_side_effects",
        AsyncMock(
            return_value=ValidationWorktreeCleanup(
                cleaned=False,
                check=ValidationWorktreeCheck(clean=True),
                restore_ref=initial_head,
            )
        ),
    )
    handoff = _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="AWF validation evidence is missing.",
            gaps=("Run hosted AWF validation.",),
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        ),
        plan_path=Path("docs/awf-plans/ws_hosted_conformance.md"),
        report_path=Path("docs/awf-plans/ws_hosted_conformance.conformance.json"),
        iteration=0,
        max_iterations=1,
    )
    adapter = _ConformanceAdapter()

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id=workspace.id,
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_hosted_conformance",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_hosted_conformance",
        adapter=adapter,  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=handoff,
        recovery={"source": "hosted_pr_adoption", "recovery_mode": "validate_only"},
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert not result.stop
    assert len(adapter.calls) == 1
    conformance_kwargs = adapter.calls[0]
    assert conformance_kwargs["profile"] is profile
    hosted_pr_identity = conformance_kwargs["hosted_pr_identity"]
    assert isinstance(hosted_pr_identity, dict)
    assert hosted_pr_identity["head_ref"] == "awf/pr-764"
    assert hosted_pr_identity["expected_head_sha"] == initial_head
    assert executor._hosted_validation.calls[0]["pr_identity"]["expected_head_sha"] == initial_head


def _hosted_workspace(*, initial_head: str, hosted: bool) -> SimpleNamespace:
    task_policy: dict[str, object] | None
    if hosted:
        task_policy = {
            "pr_adoption": {
                "execution": {"mode": "hosted"},
                "pr_url": "https://github.com/example/repo/pull/764",
                "pr_number": 764,
                "base_ref": "main",
                "head_ref": "awf/pr-764",
                "head_repo_url": "https://github.com/fork/repo.git",
                "head_sha": initial_head,
            }
        }
    else:
        task_policy = None
    return SimpleNamespace(
        resolved_profile={"name": "hosted-recovery"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="Recovery validation",
        task_prompt="validate only",
        agent="codex",
        owned_paths=("src/awf",),
        id="ws_recovery_validation",
        task_tag=None,
        task_policy=task_policy,
        repo_url="https://github.com/example/repo.git",
        pr_url="https://github.com/example/repo/pull/764" if hosted else None,
        pr_number=764 if hosted else None,
        branch_base="main",
        remote_push_branch="awf/pr-764" if hosted else "awf/ws_recovery_validation",
        monitor_last_commit_sha=initial_head,
    )


def _build_recovery_validation_executor(
    *,
    tmp_path: Path,
    hosted: bool,
) -> tuple[WorkspaceExecutor, SimpleNamespace, object]:
    initial_head = "c" * 40
    workspace = _hosted_workspace(initial_head=initial_head, hosted=hosted)

    class _RecordingValidation:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def run_profile_phases(self, **kwargs: object) -> ValidationResult:
            self.calls.append(kwargs)
            return ValidationResult(commands=[_command_result(tmp_path, returncode=0)])

    runner = FakeCommandRunner()
    executor = WorkspaceExecutor(
        session_factory=object(),  # type: ignore[arg-type]
        runner=runner,
        compose=object(),  # type: ignore[arg-type]
        validation=_RecordingValidation(),  # type: ignore[arg-type]
        pr_creator=object(),  # type: ignore[arg-type]
        config=ExecutorConfig(
            worktrees_root=tmp_path / "worktrees",
            compose_projects_root=tmp_path / "compose",
        ),
    )
    hosted_validation = _RecordingValidation()
    executor._transition_if_current = AsyncMock(return_value=True)  # type: ignore[method-assign]
    executor._recheck_status = AsyncMock(return_value=True)  # type: ignore[method-assign]
    executor._capture_workspace_head_sha = AsyncMock(return_value=initial_head)  # type: ignore[method-assign]
    executor._start_validation_run = AsyncMock(return_value="vr-recovery")  # type: ignore[method-assign]
    executor._finish_validation_run = AsyncMock()  # type: ignore[method-assign]
    executor._finish_pending_validate_operations = AsyncMock()  # type: ignore[method-assign]
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]
    executor._finish_validation_callback_if_terminal = AsyncMock(return_value=False)  # type: ignore[method-assign]
    executor._update_subphase = AsyncMock()  # type: ignore[method-assign]
    executor._validation = _RecordingValidation()  # type: ignore[assignment]
    executor._hosted_validation = hosted_validation  # type: ignore[attr-defined]
    return executor, workspace, hosted_validation


def _patch_recovery_validation(monkeypatch: pytest.MonkeyPatch) -> WorkspaceProfile:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-recovery",
            "phases": {
                "setup": ["npm ci"],
                "post_agent": ["npm run lint"],
                "validate": ["pytest -q"],
            },
        }
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
        AsyncMock(return_value=ValidationWorktreeCheck(clean=True)),
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "cleanup_validation_worktree_side_effects",
        AsyncMock(
            return_value=ValidationWorktreeCleanup(
                cleaned=False,
                check=ValidationWorktreeCheck(clean=True),
                restore_ref="c" * 40,
            )
        ),
    )
    return profile


@pytest.mark.unit
@pytest.mark.parametrize("recovery_mode", ["validate_only", "rebase_only"])
@pytest.mark.parametrize(
    "recovery_source",
    ["pr_monitor", "operator_api", "worker_restart", "hosted_pr_adoption"],
)
async def test_hosted_pr_adoption_validate_only_recovery_includes_setup_phase_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    recovery_source: str,
    recovery_mode: str,
) -> None:
    executor, workspace, hosted_validation = _build_recovery_validation_executor(
        tmp_path=tmp_path,
        hosted=True,
    )
    _patch_recovery_validation(monkeypatch)

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id=workspace.id,
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_recovery_validation",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_recovery_validation",
        adapter=None,
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery={"source": recovery_source, "recovery_mode": recovery_mode},
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert not result.stop
    assert len(hosted_validation.calls) == 1
    assert hosted_validation.calls[0]["phase_names"] == ("setup", "post_agent", "validate")
    assert executor._start_validation_run.await_args.kwargs["phase_names"] == (
        "setup",
        "post_agent",
        "validate",
    )
    assert executor._start_validation_run.await_args.kwargs["use_hosted_command_plan"] is True
    assert executor._validation.calls == []


@pytest.mark.unit
async def test_hosted_rebase_only_recovery_includes_setup_phase_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor, workspace, hosted_validation = _build_recovery_validation_executor(
        tmp_path=tmp_path,
        hosted=True,
    )
    _patch_recovery_validation(monkeypatch)

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id=workspace.id,
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_recovery_validation",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_recovery_validation",
        adapter=None,
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery={"source": "pr_monitor", "recovery_mode": "rebase_only"},
        rebase_recovery_result=_RebaseRecoveryResult(
            base_sha="a" * 40,
            head_sha="c" * 40,
        ),
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert not result.stop
    assert len(hosted_validation.calls) == 1
    assert hosted_validation.calls[0]["phase_names"] == ("setup", "post_agent", "validate")
    assert executor._start_validation_run.await_args.kwargs["phase_names"] == (
        "setup",
        "post_agent",
        "validate",
    )
    assert executor._start_validation_run.await_args.kwargs["use_hosted_command_plan"] is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "recovery",
    [
        {"source": "pr_monitor", "recovery_mode": "validate_only"},
        {"source": "hosted_pr_adoption", "recovery_mode": "validate_only"},
        {"source": "operator_api", "recovery_mode": "validate_only"},
    ],
)
async def test_local_validate_only_recovery_keeps_post_agent_validate_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    recovery: dict[str, str],
) -> None:
    executor, workspace, hosted_validation = _build_recovery_validation_executor(
        tmp_path=tmp_path,
        hosted=False,
    )
    _patch_recovery_validation(monkeypatch)

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id=workspace.id,
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_recovery_validation",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_recovery_validation",
        adapter=None,
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=recovery,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert not result.stop
    assert len(executor._validation.calls) == 1
    assert executor._validation.calls[0]["phase_names"] == ("post_agent", "validate")
    assert executor._start_validation_run.await_args.kwargs["phase_names"] == (
        "post_agent",
        "validate",
    )
    assert executor._start_validation_run.await_args.kwargs["use_hosted_command_plan"] is False
    assert hosted_validation.calls == []


def _conformance_handoff() -> _PlanningValidationHandoff:
    return _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="AWF validation evidence is missing.",
            gaps=("Run hosted AWF validation.",),
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        ),
        plan_path=Path("docs/awf-plans/ws_recovery_validation.md"),
        report_path=Path("docs/awf-plans/ws_recovery_validation.conformance.json"),
        iteration=0,
        max_iterations=1,
    )


class _ConformanceAdapter:
    is_hosted = True

    async def run(self, **_kwargs: object) -> AgentRunResult:
        return AgentRunResult(returncode=0, stdout="{}", stderr="")


async def _run_recovery_conformance_terminal_head_check(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    recovery_mode: str,
    recovery_source: str = "pr_monitor",
    expected_require_hosted_terminal_head: bool,
) -> None:
    executor, workspace, _hosted_validation = _build_recovery_validation_executor(
        tmp_path=tmp_path,
        hosted=True,
    )
    _patch_recovery_validation(monkeypatch)
    executor._run_post_validation_conformance_check = AsyncMock(return_value=None)  # type: ignore[method-assign]
    executor._validation_run_evidence_for_conformance = AsyncMock(return_value="VALIDATION_OK")  # type: ignore[method-assign]
    executor._capture_post_validation_conformance_scope_baseline = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            before_compare=set(),
            before_compare_head="validated-head",
            before_dirty_digests={},
        )
    )

    rebase_recovery_result = (
        _RebaseRecoveryResult(base_sha="a" * 40, head_sha="d" * 40)
        if recovery_mode == "rebase_only"
        else None
    )
    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id=workspace.id,
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_recovery_validation",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_recovery_validation",
        adapter=_ConformanceAdapter(),  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=_conformance_handoff(),
        recovery={"source": recovery_source, "recovery_mode": recovery_mode},
        rebase_recovery_result=rebase_recovery_result,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert not result.stop
    conformance_kwargs = executor._run_post_validation_conformance_check.await_args.kwargs
    assert (
        conformance_kwargs["require_hosted_terminal_head"] is expected_require_hosted_terminal_head
    )


@pytest.mark.unit
async def test_hosted_rebase_only_recovery_requires_terminal_head_for_conformance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _run_recovery_conformance_terminal_head_check(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        recovery_mode="rebase_only",
        expected_require_hosted_terminal_head=True,
    )


@pytest.mark.unit
async def test_hosted_pr_adoption_validate_only_recovery_skips_terminal_head_for_conformance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _run_recovery_conformance_terminal_head_check(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        recovery_mode="validate_only",
        recovery_source="hosted_pr_adoption",
        expected_require_hosted_terminal_head=False,
    )


@pytest.mark.unit
async def test_hosted_pr_monitor_validate_only_recovery_requires_terminal_head_for_conformance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _run_recovery_conformance_terminal_head_check(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        recovery_mode="validate_only",
        recovery_source="pr_monitor",
        expected_require_hosted_terminal_head=True,
    )
