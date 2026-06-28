"""Plan-only validation fix-pass regression tests."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import CommandResult
from awf.control.executor import execution_validation as executor_execution_validation
from awf.control.executor import quality_methods as executor_quality_methods
from awf.control.quality_gates import PLAN_ONLY_OUTPUT_REASON_CODE
from awf.control.quality_gates_common import QualityGateViolation
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import ValidationResult
from tests.unit.control.test_executor_coverage_edges_parts.test_executor_coverage_edges_part_009 import (
    _failing_command,
    _patch_clean_worktree,
    _patch_profile,
    _run_cycle,
    _workspace,
)

# ---------------------------------------------------------------------------
# Regression coverage for #427: the fix-pass PLAN_ONLY_OUTPUT guard must look at
# the NET committed output (base..HEAD), not just the latest staged delta. A
# fix-pass that stages only the conformance artifact must not false-fail a
# workspace whose real implementation/test/doc work is in earlier commits.
# ---------------------------------------------------------------------------

_CONFORMANCE_PATH = "docs/awf-plans/ws_x.conformance.json"


def _plan_only_guard_executor(
    tmp_path: Path,
    *,
    committed_paths: set[Path],
    fail_if_plan_only: bool = True,
) -> SimpleNamespace:
    """Build a fix-cycle executor wired with the REAL plan-only guard helper.

    ``_committed_paths_since`` is the only collaborator the guard consults, so a
    mocked return value here exercises the genuine ``base..HEAD`` net-diff logic
    through the call site at ``execution_validation.py`` line ~1183.
    """

    class _FailingValidation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_failing_command(tmp_path)])

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=1,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-plan-guard"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_FailingValidation(),
        _ensure_worktree_available=AsyncMock(return_value=True),
        _repair_agent_git_ownership=AsyncMock(),
        _refresh_supply_chain_policy_for_workspace=AsyncMock(
            return_value=SimpleNamespace(policy_blocked=False),
        ),
        _committed_paths_since=AsyncMock(return_value=committed_paths),
        _fail_if_plan_only_paths=AsyncMock(return_value=fail_if_plan_only),
        _active_operator_grant_specs=AsyncMock(return_value=[]),
        _protected_file_diffs_for_staged_paths=AsyncMock(return_value=()),
        _runner=SimpleNamespace(run=AsyncMock(return_value=CommandResult(0, "", ""))),
    )
    # Wire the real guard so the net committed state is read exactly as in prod.
    executor._committed_and_staged_output_is_plan_only = partial(
        executor_quality_methods._committed_and_staged_output_is_plan_only,
        executor,
    )
    return executor


def _conformance_fix_pass_git(tmp_path: Path) -> AsyncMock:
    """``git add -A`` succeeds; ``git diff --cached`` stages only the conformance."""
    return AsyncMock(
        side_effect=[
            CommandResult(returncode=0, stdout="", stderr=""),  # git add -A
            CommandResult(
                returncode=0,
                stdout=f"{_CONFORMANCE_PATH}\n",
                stderr="",
            ),  # git diff --cached --name-only
        ]
    )


@pytest.mark.unit
async def test_fix_pass_plan_only_staged_with_real_committed_output_proceeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """R1: net committed output has a real file → no false PLAN_ONLY_OUTPUT.

    The fix-pass stages only the conformance artifact, but ``base..HEAD`` already
    contains ``src/foo.py``. The guard must short-circuit the failure so
    ``_fail_if_plan_only_paths`` is never awaited and execution proceeds.
    """
    profile = WorkspaceProfile.model_validate({"name": "prof-fix-real-committed"})
    workspace = _workspace("ws_fix_real_committed")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    executor = _plan_only_guard_executor(
        tmp_path,
        committed_paths={Path("src/foo.py")},
    )

    # Worktree stays available everywhere except the post-guard pre-commit check,
    # which stops the cycle cleanly right after the guard has been bypassed.
    async def _ensure(**kwargs: object) -> bool:
        return kwargs.get("action") != "validation_fix_git_commit"

    executor._ensure_worktree_available = AsyncMock(side_effect=_ensure)
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        git_in_worktree=_conformance_fix_pass_git(tmp_path),
    )

    assert result.stop
    # The plan-only failure was bypassed (real committed work exists).
    executor._fail_if_plan_only_paths.assert_not_awaited()
    executor._committed_paths_since.assert_awaited_once()
    # No PLAN_ONLY_OUTPUT failure was finished; downstream knows real work exists.
    for call in executor._finish_pending_validate_operations.await_args_list:
        assert call.kwargs.get("reason_code") != PLAN_ONLY_OUTPUT_REASON_CODE


@pytest.mark.unit
async def test_fix_pass_plan_only_staged_with_empty_committed_output_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """R3: net committed output empty AND staged plan-only → still PLAN_ONLY_OUTPUT."""
    profile = WorkspaceProfile.model_validate({"name": "prof-fix-empty-committed"})
    workspace = _workspace("ws_fix_empty_committed")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    executor = _plan_only_guard_executor(tmp_path, committed_paths=set())
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        git_in_worktree=_conformance_fix_pass_git(tmp_path),
    )

    assert result.stop
    executor._fail_if_plan_only_paths.assert_awaited_once()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["status"] == OperationStatus.failed
    assert finish_kwargs["reason_code"] == PLAN_ONLY_OUTPUT_REASON_CODE


@pytest.mark.unit
async def test_fix_pass_plan_only_staged_with_plan_only_committed_output_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """R4: net committed output itself plan-only AND staged plan-only → fails."""
    profile = WorkspaceProfile.model_validate({"name": "prof-fix-plan-committed"})
    workspace = _workspace("ws_fix_plan_committed")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    executor = _plan_only_guard_executor(
        tmp_path,
        committed_paths={Path("docs/awf-plans/ws_x.md")},
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        git_in_worktree=_conformance_fix_pass_git(tmp_path),
    )

    assert result.stop
    executor._fail_if_plan_only_paths.assert_awaited_once()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["reason_code"] == PLAN_ONLY_OUTPUT_REASON_CODE


@pytest.mark.unit
async def test_fix_pass_plan_only_reverted_real_output_still_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """R-revert: a real file committed then reverted nets to empty base..HEAD.

    The guard reads net git state (``_committed_paths_since`` → empty set), not a
    stale "saw a real file once" flag, so PLAN_ONLY_OUTPUT still fires.
    """
    profile = WorkspaceProfile.model_validate({"name": "prof-fix-reverted"})
    workspace = _workspace("ws_fix_reverted")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    executor = _plan_only_guard_executor(tmp_path, committed_paths=set())
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        git_in_worktree=_conformance_fix_pass_git(tmp_path),
    )

    assert result.stop
    # Net state was consulted with the worktree + base commit in scope.
    committed_args = executor._committed_paths_since.await_args
    assert committed_args.args[0] == tmp_path / "worktree"
    assert committed_args.args[1] == "b" * 40
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["reason_code"] == PLAN_ONLY_OUTPUT_REASON_CODE


@pytest.mark.unit
async def test_fix_pass_plan_only_deposits_artifacts_before_marking_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression PRRT_kwDOSJAM6s6JPz04: the plan-only fix-pass path must deposit
    planning artifacts BEFORE the terminal FAILED status is published.

    ``_fail_if_plan_only_paths`` routes through bare ``_mark_failed`` (not the
    ``_mark_failed_preserving_planning_artifacts`` helper every other terminal
    path in this cycle uses), so depositing afterward would let the console
    observe the FAILED ``updated_at`` and cache an empty artifact list before the
    post-cycle deposit (in ``execution_flow``) lands — never refetching and
    hiding the Plan/Validation controls. The deposit must precede the
    ``_fail_if_plan_only_paths`` call.
    """
    profile = WorkspaceProfile.model_validate({"name": "prof-fix-plan-deposit"})
    workspace = _workspace("ws_fix_plan_deposit")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    order: list[str] = []

    def _spy_deposit(*_args: object, **_kwargs: object) -> None:
        order.append("deposit")

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_deposit,
    )

    executor = _plan_only_guard_executor(tmp_path, committed_paths=set())

    async def _fail(*_args: object, **_kwargs: object) -> bool:
        order.append("fail")
        return True

    executor._fail_if_plan_only_paths = AsyncMock(side_effect=_fail)
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    result = await _run_cycle(
        executor,
        workspace=workspace,
        tmp_path=tmp_path,
        adapter=adapter,
        git_in_worktree=_conformance_fix_pass_git(tmp_path),
    )

    assert result.stop
    # The artifact deposit happens strictly before the terminal FAILED publish.
    assert order == ["deposit", "fail"]
    executor._fail_if_plan_only_paths.assert_awaited_once()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["reason_code"] == PLAN_ONLY_OUTPUT_REASON_CODE


def _real_staged_fix_pass_git(tmp_path: Path) -> AsyncMock:
    """``git add -A`` succeeds; ``git diff --cached`` stages a real (non-plan) file."""
    return AsyncMock(
        side_effect=[
            CommandResult(returncode=0, stdout="", stderr=""),  # git add -A
            CommandResult(
                returncode=0,
                stdout="pyproject.toml\n",
                stderr="",
            ),  # git diff --cached --name-only
        ]
    )


@pytest.mark.unit
async def test_fix_pass_protected_block_fences_on_execution_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression PRRT_kwDOSJAM6s6J6rcG: the validation fix-cycle protected-gate
    block must forward ``execution_owner_id`` so its epoch-guarded CAS fences on
    the execution claim — mirroring every other pre-PR block site
    (post-agent commit, recovery verify, committed output, pre-commit repair).

    Without the owner id, ``enter_blocked_for_protected_violation`` skips the
    ``execution_claimed_by`` condition, so a stale executor that already lost the
    claim could still pause a workspace out of ``validating``.
    """
    profile = WorkspaceProfile.model_validate({"name": "prof-fix-protected-owner"})
    workspace = _workspace("ws_fix_protected_owner")
    _patch_profile(monkeypatch, profile)
    _patch_clean_worktree(monkeypatch)

    # The fix-pass stages a real protected file (``pyproject.toml``); the staged
    # delta is not plan-only, so the cycle reaches the protected quality-gate
    # classifier, which we force to report a violation.
    violation = QualityGateViolation(
        path="pyproject.toml",
        protected_pattern="pyproject.toml",
        section="tool.coverage",
        line=1,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "find_protected_quality_gate_changes",
        lambda **_kwargs: [violation],
    )

    executor = _plan_only_guard_executor(tmp_path, committed_paths=set())
    executor.enter_blocked_for_protected_violation = AsyncMock(return_value=True)
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    result = await executor_execution_validation.run_validation_and_fix_cycle(
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
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=_real_staged_fix_pass_git(tmp_path),
        execution_owner_id="worker-x",
    )

    assert result.stop
    executor.enter_blocked_for_protected_violation.assert_awaited_once()
    block_kwargs = executor.enter_blocked_for_protected_violation.await_args.kwargs
    assert block_kwargs["execution_owner_id"] == "worker-x"
    assert block_kwargs["from_status"] == WorkspaceStatus.validating
    assert block_kwargs["resume_phase"] == "validation_fix_cycle"
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["reason_code"] == "QUALITY_GATE_POLICY_CHANGED"
