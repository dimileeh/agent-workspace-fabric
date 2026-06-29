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
from awf.runtime.validation import ValidationCommandResult, ValidationResult
from awf.runtime.validation_setup import runtime_browser_probe_deferred_until_validate
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


@pytest.mark.unit
async def test_validation_records_deferred_browser_findings_after_validate_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    (worktree_path / "requirements.txt").write_text("playwright\n", encoding="utf-8")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "browser-validate-install-test",
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": ["node scripts/generate-config.js"],
                "validate": [
                    "pip install -r requirements.txt",
                    "pnpm test",
                ],
            },
        }
    )
    workspace = SimpleNamespace(
        resolved_profile=profile.model_dump(mode="json"),
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="Browser task",
        agent="codex",
        owned_paths=(),
        id="ws_browser_validate",
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
        "_validation_tier_for_workspace",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "check_validation_worktree_clean",
        AsyncMock(return_value=ValidationWorktreeCheck(clean=True)),
    )
    order: list[str] = []

    async def _cleanup_validation_worktree_side_effects(
        **_kwargs: object,
    ) -> ValidationWorktreeCleanup:
        order.append("cleanup")
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref="c" * 40,
        )

    monkeypatch.setattr(
        executor_execution_validation,
        "cleanup_validation_worktree_side_effects",
        _cleanup_validation_worktree_side_effects,
    )

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(commands=[_passing_validation_command(tmp_path)])

    browser_calls: list[dict[str, object]] = []

    async def _record_runtime_browser_findings_safe(**kwargs: object) -> None:
        order.append("browser_probe")
        browser_calls.append(kwargs)

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-browser-validate"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _record_runtime_browser_findings_safe=_record_runtime_browser_findings_safe,
    )

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id=workspace.id,
        ws=workspace,
        worktree_path=worktree_path,
        compose_project=f"awf_{workspace.id}",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch=f"awf/{workspace.id}",
        adapter=SimpleNamespace(run=AsyncMock()),
        run_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert not result.stop
    assert browser_calls == [
        {
            "workspace_id": workspace.id,
            "compose_project": f"awf_{workspace.id}",
            "compose_file": tmp_path / "compose.yml",
            "profile": profile,
            "worktree_path": worktree_path,
        }
    ]
    assert order == ["browser_probe", "cleanup"]


@pytest.mark.unit
async def test_validation_skips_deferred_browser_findings_when_validate_raises_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    (worktree_path / "requirements.txt").write_text("playwright\n", encoding="utf-8")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "browser-validate-install-exception-test",
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": ["node scripts/generate-config.js"],
                "validate": [
                    "pip install -r requirements.txt",
                    "pnpm test",
                ],
            },
        }
    )
    workspace = SimpleNamespace(
        resolved_profile=profile.model_dump(mode="json"),
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="Browser task",
        agent="codex",
        owned_paths=(),
        id="ws_browser_validate_exception",
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
        "_validation_tier_for_workspace",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "check_validation_worktree_clean",
        AsyncMock(return_value=ValidationWorktreeCheck(clean=True)),
    )
    order: list[str] = []

    async def _cleanup_validation_worktree_side_effects(
        **_kwargs: object,
    ) -> ValidationWorktreeCleanup:
        order.append("cleanup")
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref="c" * 40,
        )

    monkeypatch.setattr(
        executor_execution_validation,
        "cleanup_validation_worktree_side_effects",
        _cleanup_validation_worktree_side_effects,
    )

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            raise RuntimeError("validation runner failed before deferred install completed")

    browser_calls: list[dict[str, object]] = []

    async def _record_runtime_browser_findings_safe(**kwargs: object) -> None:
        order.append("browser_probe")
        browser_calls.append(kwargs)

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-browser-validate-exception"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _record_runtime_browser_findings_safe=_record_runtime_browser_findings_safe,
    )

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id=workspace.id,
        ws=workspace,
        worktree_path=worktree_path,
        compose_project=f"awf_{workspace.id}",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch=f"awf/{workspace.id}",
        adapter=SimpleNamespace(run=AsyncMock()),
        run_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert browser_calls == []
    assert order == ["cleanup"]
    executor._finish_validation_run.assert_awaited_once()


@pytest.mark.unit
async def test_validation_records_browser_findings_for_validation_only_injected_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    profile = WorkspaceProfile.model_validate(
        {
            "name": "browser-setup-satisfied-validation-only-test",
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": ["python -m pip install playwright"],
                "validate": ["pytest --browser chromium"],
            },
        }
    )
    assert not runtime_browser_probe_deferred_until_validate(
        profile,
        workspace_root=worktree_path,
    )
    workspace = SimpleNamespace(
        resolved_profile=profile.model_dump(mode="json"),
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="Browser task",
        agent="codex",
        owned_paths=(),
        id="ws_browser_validation_only",
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
        "_validation_tier_for_workspace",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "check_validation_worktree_clean",
        AsyncMock(return_value=ValidationWorktreeCheck(clean=True)),
    )
    order: list[str] = []

    async def _cleanup_validation_worktree_side_effects(
        **_kwargs: object,
    ) -> ValidationWorktreeCleanup:
        order.append("cleanup")
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref="c" * 40,
        )

    monkeypatch.setattr(
        executor_execution_validation,
        "cleanup_validation_worktree_side_effects",
        _cleanup_validation_worktree_side_effects,
    )

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            stdout = tmp_path / "browser.stdout"
            stderr = tmp_path / "browser.stderr"
            stdout.write_text("ok", encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            return ValidationResult(
                commands=[
                    ValidationCommandResult(
                        command="python -m playwright install chromium",
                        returncode=0,
                        duration_seconds=0.1,
                        stdout_path=stdout,
                        stderr_path=stderr,
                        phase="setup",
                        reason_code=None,
                        required=False,
                    ),
                    ValidationCommandResult(
                        command="pytest --browser chromium",
                        returncode=0,
                        duration_seconds=0.1,
                        stdout_path=stdout,
                        stderr_path=stderr,
                        phase="validate",
                        reason_code=None,
                    ),
                ]
            )

    browser_calls: list[dict[str, object]] = []

    async def _record_runtime_browser_findings_safe(**kwargs: object) -> None:
        order.append("browser_probe")
        browser_calls.append(kwargs)

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-browser-validation-only"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _record_runtime_browser_findings_safe=_record_runtime_browser_findings_safe,
    )

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id=workspace.id,
        ws=workspace,
        worktree_path=worktree_path,
        compose_project=f"awf_{workspace.id}",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch=f"awf/{workspace.id}",
        adapter=SimpleNamespace(run=AsyncMock()),
        run_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert not result.stop
    assert len(browser_calls) == 1
    assert order == ["browser_probe", "cleanup"]


@pytest.mark.unit
async def test_validation_records_deferred_browser_findings_before_callback_terminal_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "browser-validate-callback-terminal-test",
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": ["node scripts/generate-config.js"],
                "validate": [
                    "pnpm install --frozen-lockfile",
                    "pnpm test",
                ],
            },
        }
    )
    workspace = SimpleNamespace(
        resolved_profile=profile.model_dump(mode="json"),
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="Browser task",
        agent="codex",
        owned_paths=(),
        id="ws_browser_validate_callback_terminal",
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

    browser_calls: list[dict[str, object]] = []
    order: list[str] = []

    async def _record_runtime_browser_findings_safe(**kwargs: object) -> None:
        order.append("browser_probe")
        browser_calls.append(kwargs)

    async def _finish_validation_callback_if_terminal(**_kwargs: object) -> bool:
        order.append("callback")
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
        _start_validation_run=AsyncMock(return_value="vr-browser-validate-callback-terminal"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=_finish_validation_callback_if_terminal,
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _record_runtime_browser_findings_safe=_record_runtime_browser_findings_safe,
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
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert browser_calls == [
        {
            "workspace_id": workspace.id,
            "compose_project": f"awf_{workspace.id}",
            "compose_file": tmp_path / "compose.yml",
            "profile": profile,
            "worktree_path": tmp_path / "worktree",
        }
    ]
    assert order == ["browser_probe", "callback"]
    executor._finish_validation_run.assert_not_awaited()


@pytest.mark.unit
async def test_validation_records_deferred_browser_findings_before_fix_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "browser-validate-fix-pass-test",
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": ["node scripts/generate-config.js"],
                "validate": [
                    "pnpm install --frozen-lockfile",
                    "pnpm test",
                ],
            },
        }
    )
    workspace = SimpleNamespace(
        resolved_profile=profile.model_dump(mode="json"),
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="Browser task",
        agent="codex",
        owned_paths=(),
        id="ws_browser_validate_fix_pass",
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

    stdout_path = tmp_path / "validate.stdout"
    stderr_path = tmp_path / "validate.stderr"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("playwright failed after advisory install", encoding="utf-8")

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(
                commands=[
                    ValidationCommandResult(
                        command="pnpm test",
                        returncode=1,
                        duration_seconds=0.1,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                        phase="validate",
                        reason_code="VALIDATION_COMMAND_FAILED",
                    )
                ]
            )

    browser_calls: list[dict[str, object]] = []
    order: list[str] = []

    async def _record_runtime_browser_findings_safe(**kwargs: object) -> None:
        order.append("browser_probe")
        browser_calls.append(kwargs)

    async def _recheck_status(*_args: object, **kwargs: object) -> bool:
        action = kwargs.get("action")
        if action == "validate":
            return True
        order.append("fix_recheck")
        return False

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=_recheck_status,
        _config=SimpleNamespace(
            max_validation_fix_passes=1,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-browser-validate-fix-pass"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _record_runtime_browser_findings_safe=_record_runtime_browser_findings_safe,
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
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert browser_calls == [
        {
            "workspace_id": workspace.id,
            "compose_project": f"awf_{workspace.id}",
            "compose_file": tmp_path / "compose.yml",
            "profile": profile,
            "worktree_path": tmp_path / "worktree",
        }
    ]
    assert order == ["browser_probe", "fix_recheck"]


@pytest.mark.unit
async def test_validation_reprobes_deferred_browser_findings_after_fix_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "browser-validate-retry-test",
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": ["node scripts/generate-config.js"],
                "validate": [
                    "pnpm install --frozen-lockfile",
                    "pnpm test",
                ],
            },
        }
    )
    workspace = SimpleNamespace(
        resolved_profile=profile.model_dump(mode="json"),
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="Browser task",
        agent="codex",
        owned_paths=(),
        id="ws_browser_validate_retry",
        pr_url=None,
        task_tag=None,
    )

    from awf.control.executor import execution_validation as executor_execution_validation

    async def _sync_profile(*_args: object, **_kwargs: object) -> WorkspaceProfile:
        return profile

    async def _run_agent_without_recovery(*_args: object, **kwargs: object) -> tuple[bool, object]:
        return True, await kwargs["run_agent"](False)

    monkeypatch.setattr(
        executor_execution_validation,
        "_profile_for_workspace",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(executor_execution_validation, "_sync_resolved_profile", _sync_profile)
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
    monkeypatch.setattr(
        executor_execution_validation,
        "_run_agent_callable_with_service_recovery",
        _run_agent_without_recovery,
    )

    stdout_path = tmp_path / "validate.stdout"
    stderr_path = tmp_path / "validate.stderr"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("playwright failed after advisory install", encoding="utf-8")

    class _Validation:
        def __init__(self) -> None:
            self.calls = 0

        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            self.calls += 1
            if self.calls == 1:
                return ValidationResult(
                    commands=[
                        ValidationCommandResult(
                            command="pnpm test",
                            returncode=1,
                            duration_seconds=0.1,
                            stdout_path=stdout_path,
                            stderr_path=stderr_path,
                            phase="validate",
                            reason_code="VALIDATION_COMMAND_FAILED",
                        )
                    ]
                )
            return ValidationResult(commands=[_passing_validation_command(tmp_path)])

    validation = _Validation()
    browser_calls: list[dict[str, object]] = []

    async def _record_runtime_browser_findings_safe(**kwargs: object) -> None:
        browser_calls.append(kwargs)

    async def _git_in_worktree(argv: list[str]) -> CommandResult:
        if argv == ["diff", "--cached", "--name-only"]:
            return CommandResult(returncode=0, stdout="", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=1,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(side_effect=["vr-browser-retry-1", "vr-browser-retry-2"]),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=validation,
        _record_runtime_browser_findings_safe=_record_runtime_browser_findings_safe,
        _ensure_worktree_available=AsyncMock(return_value=True),
        _repair_agent_git_ownership=AsyncMock(),
        _refresh_supply_chain_policy_for_workspace=AsyncMock(
            return_value=SimpleNamespace(policy_blocked=False, findings=())
        ),
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id=workspace.id,
        ws=workspace,
        worktree_path=tmp_path / "worktree",
        compose_project=f"awf_{workspace.id}",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch=f"awf/{workspace.id}",
        adapter=adapter,
        run_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=_git_in_worktree,
    )

    assert not result.stop
    assert validation.calls == 2
    adapter.run.assert_awaited_once()
    assert browser_calls == [
        {
            "workspace_id": workspace.id,
            "compose_project": f"awf_{workspace.id}",
            "compose_file": tmp_path / "compose.yml",
            "profile": profile,
            "worktree_path": tmp_path / "worktree",
        },
        {
            "workspace_id": workspace.id,
            "compose_project": f"awf_{workspace.id}",
            "compose_file": tmp_path / "compose.yml",
            "profile": profile,
            "worktree_path": tmp_path / "worktree",
        },
    ]


@pytest.mark.unit
async def test_validation_retries_deferred_browser_findings_after_record_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "browser-validate-record-retry-test",
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": ["node scripts/generate-config.js"],
                "validate": [
                    "pnpm install --frozen-lockfile",
                    "pnpm test",
                ],
            },
        }
    )
    workspace = SimpleNamespace(
        resolved_profile=profile.model_dump(mode="json"),
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="Browser task",
        agent="codex",
        owned_paths=(),
        id="ws_browser_validate_record_retry",
        pr_url=None,
        task_tag=None,
    )

    from awf.control.executor import execution_validation as executor_execution_validation

    async def _sync_profile(*_args: object, **_kwargs: object) -> WorkspaceProfile:
        return profile

    async def _run_agent_without_recovery(*_args: object, **kwargs: object) -> tuple[bool, object]:
        return True, await kwargs["run_agent"](False)

    monkeypatch.setattr(
        executor_execution_validation,
        "_profile_for_workspace",
        lambda *_args, **_kwargs: profile,
    )
    monkeypatch.setattr(executor_execution_validation, "_sync_resolved_profile", _sync_profile)
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
    monkeypatch.setattr(
        executor_execution_validation,
        "_run_agent_callable_with_service_recovery",
        _run_agent_without_recovery,
    )

    stdout_path = tmp_path / "validate.stdout"
    stderr_path = tmp_path / "validate.stderr"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("playwright failed after advisory install", encoding="utf-8")

    class _Validation:
        def __init__(self) -> None:
            self.calls = 0

        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            self.calls += 1
            if self.calls == 1:
                return ValidationResult(
                    commands=[
                        ValidationCommandResult(
                            command="pnpm test",
                            returncode=1,
                            duration_seconds=0.1,
                            stdout_path=stdout_path,
                            stderr_path=stderr_path,
                            phase="validate",
                            reason_code="VALIDATION_COMMAND_FAILED",
                        )
                    ]
                )
            return ValidationResult(commands=[_passing_validation_command(tmp_path)])

    validation = _Validation()
    browser_calls: list[dict[str, object]] = []

    async def _record_runtime_browser_findings_safe(**kwargs: object) -> bool:
        browser_calls.append(kwargs)
        return len(browser_calls) > 1

    async def _git_in_worktree(argv: list[str]) -> CommandResult:
        if argv == ["diff", "--cached", "--name-only"]:
            return CommandResult(returncode=0, stdout="", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=1,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(side_effect=["vr-browser-retry-1", "vr-browser-retry-2"]),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=validation,
        _record_runtime_browser_findings_safe=_record_runtime_browser_findings_safe,
        _ensure_worktree_available=AsyncMock(return_value=True),
        _repair_agent_git_ownership=AsyncMock(),
        _refresh_supply_chain_policy_for_workspace=AsyncMock(
            return_value=SimpleNamespace(policy_blocked=False, findings=())
        ),
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout="", stderr="")))

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id=workspace.id,
        ws=workspace,
        worktree_path=tmp_path / "worktree",
        compose_project=f"awf_{workspace.id}",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch=f"awf/{workspace.id}",
        adapter=adapter,
        run_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=_git_in_worktree,
    )

    assert not result.stop
    assert validation.calls == 2
    adapter.run.assert_awaited_once()
    assert browser_calls == [
        {
            "workspace_id": workspace.id,
            "compose_project": f"awf_{workspace.id}",
            "compose_file": tmp_path / "compose.yml",
            "profile": profile,
            "worktree_path": tmp_path / "worktree",
        },
        {
            "workspace_id": workspace.id,
            "compose_project": f"awf_{workspace.id}",
            "compose_file": tmp_path / "compose.yml",
            "profile": profile,
            "worktree_path": tmp_path / "worktree",
        },
    ]


@pytest.mark.unit
async def test_validation_records_deferred_browser_findings_before_terminal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "browser-validate-failure-test",
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": ["node scripts/generate-config.js"],
                "validate": [
                    "pnpm install --frozen-lockfile",
                    "pnpm test",
                ],
            },
        }
    )
    workspace = SimpleNamespace(
        resolved_profile=profile.model_dump(mode="json"),
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="Browser task",
        agent="codex",
        owned_paths=(),
        id="ws_browser_validate_failure",
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

    stdout_path = tmp_path / "validate.stdout"
    stderr_path = tmp_path / "validate.stderr"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("tests failed", encoding="utf-8")

    class _Validation:
        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            return ValidationResult(
                commands=[
                    ValidationCommandResult(
                        command="pnpm test",
                        returncode=1,
                        duration_seconds=0.1,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                        phase="validate",
                        reason_code="VALIDATION_COMMAND_FAILED",
                    )
                ]
            )

    browser_calls: list[dict[str, object]] = []

    async def _record_runtime_browser_findings_safe(**kwargs: object) -> None:
        browser_calls.append(kwargs)

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-browser-validate-failure"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _record_runtime_browser_findings_safe=_record_runtime_browser_findings_safe,
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
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert browser_calls == [
        {
            "workspace_id": workspace.id,
            "compose_project": f"awf_{workspace.id}",
            "compose_file": tmp_path / "compose.yml",
            "profile": profile,
            "worktree_path": tmp_path / "worktree",
        }
    ]


@pytest.mark.unit
async def test_validation_skips_deferred_browser_findings_before_infrastructure_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "browser-validate-infra-failure-test",
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": ["node scripts/generate-config.js"],
                "validate": [
                    "pnpm install --frozen-lockfile",
                    "pnpm test",
                ],
            },
        }
    )
    workspace = SimpleNamespace(
        resolved_profile=profile.model_dump(mode="json"),
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="Browser task",
        agent="codex",
        owned_paths=(),
        id="ws_browser_validate_infra_failure",
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
            raise RuntimeError("validate infrastructure unavailable")

    browser_calls: list[dict[str, object]] = []

    async def _record_runtime_browser_findings_safe(**kwargs: object) -> None:
        browser_calls.append(kwargs)

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-browser-validate-infra-failure"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _record_runtime_browser_findings_safe=_record_runtime_browser_findings_safe,
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
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert browser_calls == []
