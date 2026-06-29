"""Focused validation-conformance failure coverage for executor execution."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import CommandResult
from awf.control.executor.types import (
    _PlanningRunFailure,
    _PlanningValidationHandoff,
)
from awf.profiles.models import WorkspaceProfile
from awf.runtime.planning import (
    PLAN_CONFORMANCE_UNSATISFIED,
    PlanConformanceReport,
    PlanConformanceStatus,
)
from awf.runtime.validation import ValidationResult
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup
from tests.unit.control.test_executor_coverage_edges_parts.test_executor_coverage_edges_part_001 import (
    _passing_validation_command,
)


@pytest.mark.unit
async def test_validation_conformance_failure_still_deposits_before_mark_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6KCdzX: a terminal conformance failure must still
    deposit planning artifacts before marking the workspace FAILED. The
    success-path deposit block was removed, but every terminal failure path
    must keep its pre-mark deposit.
    """
    profile = WorkspaceProfile.model_validate(
        {"name": "prof-failure-deposit", "planning": {"required": True}}
    )
    workspace = SimpleNamespace(
        resolved_profile={"name": "prof-failure-deposit"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="A task",
        agent="codex",
        owned_paths=(),
        id="ws_failure_deposit",
        pr_url=None,
        task_tag=None,
    )

    from awf.control.executor import execution_validation as executor_execution_validation

    async def _sync_profile(*_args: object, **_kwargs: object) -> WorkspaceProfile:
        return profile

    monkeypatch.setattr(
        executor_execution_validation,
        "_profile_for_workspace",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(executor_execution_validation, "_sync_resolved_profile", _sync_profile)
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

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_validation_command(tmp_path)])

    order: list[str] = []
    real_outer_deposit = (
        executor_execution_validation._planning_artifacts._deposit_planning_artifacts_best_effort
    )

    def _spy_outer_deposit(*_args: object, **_kwargs: object) -> None:
        order.append("deposit")
        real_outer_deposit(*_args, **_kwargs)

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_outer_deposit,
    )

    async def _mark_failed(**_kwargs: object) -> None:
        order.append("mark_failed")

    async def _ensure_worktree_available(**_kwargs: object) -> bool:
        return True

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-failure-deposit"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=_mark_failed,
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _run_post_validation_conformance_check=AsyncMock(
            return_value=_PlanningRunFailure(
                message="not satisfied",
                reason_code=PLAN_CONFORMANCE_UNSATISFIED,
                details={"conformance": {}},
            )
        ),
        _ensure_worktree_available=_ensure_worktree_available,
        _git_add_all_in_worktree=AsyncMock(
            return_value=CommandResult(returncode=0, stdout="", stderr="")
        ),
        _commit_in_worktree=AsyncMock(
            return_value=CommandResult(returncode=0, stdout="", stderr="")
        ),
        _repair_agent_git_ownership=AsyncMock(),
        _refresh_supply_chain_policy_for_workspace=AsyncMock(),
    )

    handoff = _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.satisfied,
            summary="ok",
            gaps=(),
        ),
        plan_path=tmp_path / "worktree" / "plan.md",
        report_path=tmp_path / "worktree" / "report.md",
        iteration=0,
        max_iterations=1,
    )

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id=workspace.id,
        ws=workspace,
        worktree_path=tmp_path / "worktree",
        compose_project=f"awf_{workspace.id}",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch=f"awf/{workspace.id}",
        adapter=SimpleNamespace(run=AsyncMock()),
        run_model=None,
        baseline_coverage=None,
        planning_validation_handoff=handoff,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    # Terminal conformance failure path still deposits before marking FAILED.
    assert order == ["deposit", "mark_failed"]
