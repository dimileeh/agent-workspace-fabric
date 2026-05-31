"""Focused branch-coverage tests for executor helper behavior."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor import execution_validation as executor_execution_validation
from awf.control.executor import helpers as executor_helpers
from awf.control.executor import planning_ops as executor_planning_ops
from awf.control.executor import quality_gates as executor_quality_gates
from awf.control.executor.helpers import (
    _failure_salvage_payload,
    _profile_with_planning_iteration_default,
    _raw_profile_has_explicit_planning_max_iterations,
    _validation_tier_for_workspace,
)
from awf.db.enums import (
    AgentRuntime,
    FailureReason,
    OperationStatus,
    OperationType,
    TaskClass,
)
from awf.profiles.models import ProfilePlanning, WorkspaceProfile
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    PLAN_CONFORMANCE_UNSATISFIED,
    PlanConformanceReport,
    PlanConformanceStatus,
)
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
)
from awf.runtime.validation_worktree import ValidationWorktreeCheck
from awf.runtime.validation_worktree_constants import (
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
)


def _command_result(tmp_path: Path, *, returncode: int = 1) -> ValidationCommandResult:
    stdout = tmp_path / "cmd.stdout"
    stderr = tmp_path / "cmd.stderr"
    stdout.write_text("stdout", encoding="utf-8")
    stderr.write_text("stderr", encoding="utf-8")
    return ValidationCommandResult(
        command="pytest --cov",
        returncode=returncode,
        duration_seconds=0.1,
        stdout_path=stdout,
        stderr_path=stderr,
        phase="coverage",
        reason_code="COVERAGE_BELOW_THRESHOLD",
        policy_failed=returncode != 0,
    )


def _coverage(
    tmp_path: Path,
    *,
    percent: float | None,
    minimum: float = 99,
    reason_code: str = "COVERAGE_BELOW_THRESHOLD",
    status: str = "failed",
    command_result: ValidationCommandResult | None = None,
) -> ValidationCoverageResult:
    return ValidationCoverageResult(
        provider="python",
        percent=percent,
        minimum_percent=minimum,
        enforce=True,
        status=status,
        reason_code=reason_code,
        command_result=command_result if command_result is not None else _command_result(tmp_path),
    )


class _PlanningAdapter:
    def __init__(self, *stdout_values: str) -> None:
        self.stdout_values = list(stdout_values)
        self.prompts: list[str] = []

    async def run(self, **kwargs: object) -> SimpleNamespace:
        prompt = kwargs.get("prompt")
        assert isinstance(prompt, str)
        self.prompts.append(prompt)
        stdout = self.stdout_values.pop(0) if self.stdout_values else ""
        return SimpleNamespace(stdout=stdout, stderr="")


class _CoverageValidation:
    def __init__(self, coverage: ValidationCoverageResult | None) -> None:
        self.coverage = coverage
        self.calls: list[str] = []
        self.kwargs: list[dict[str, object]] = []

    async def run_profile_coverage(
        self, *, phase: str, **_kwargs: object
    ) -> ValidationCoverageResult | None:
        self.calls.append(phase)
        self.kwargs.append(dict(_kwargs))
        return self.coverage


def _coordination_task_policy() -> dict[str, object]:
    return {
        "coordination": {
            "warnings": [
                {
                    "warning_code": "OWNED_PATH_OVERLAP_RISK",
                    "message": "Owned paths overlap active workspaces.",
                    "severity": "advisory",
                    "blocks_launch": False,
                    "workspace_ids": ["ws_existing"],
                    "overlaps": [
                        {
                            "workspace_id": "ws_existing",
                            "existing_path": "src/awf/service/**",
                            "requested_path": "src/awf/service/workspaces.py",
                        }
                    ],
                    "stale_policy_context": {
                        "trigger_type": "path_overlap",
                        "stale_reason_code": "STALE_OVERLAP",
                    },
                }
            ]
        }
    }


def _executor_with_runner(
    runner: FakeCommandRunner,
    tmp_path: Path,
    *,
    validation: object | None = None,
) -> WorkspaceExecutor:
    executor = WorkspaceExecutor(
        session_factory=object(),  # type: ignore[arg-type]
        runner=runner,
        compose=object(),  # type: ignore[arg-type]
        validation=validation or object(),  # type: ignore[arg-type]
        pr_creator=object(),  # type: ignore[arg-type]
        config=ExecutorConfig(
            worktrees_root=tmp_path / "worktrees",
            compose_projects_root=tmp_path / "compose",
        ),
    )
    executor._update_subphase = AsyncMock()  # type: ignore[method-assign]
    return executor


def _autofix_classification(
    *,
    repair_files: tuple[str, ...] = ("src/app.py",),
) -> executor_quality_gates._PostAgentCommitClassification:  # noqa: SLF001
    return executor_quality_gates._PostAgentCommitClassification(  # noqa: SLF001
        reason_code="POST_AGENT_COMMIT_AUTOFIX_NEEDED",
        failed_hooks=("ruff-check",),
        format_repair_files=(),
        normalizer_repair_files=(),
        autofix_repair_files=repair_files,
        summary="ruff reported fixable diagnostics",
        repair_strategy="deterministic_autofix",
    )


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class _FakeExecutorSession:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> _FakeExecutorSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.unit
async def test_planning_required_prompts_include_coordination_warning(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_coord_plan.md\n",
    )  # dirty after planning
    runner.queue_result(returncode=0, stdout="")  # committed paths since baseline
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
    runner.queue_result(
        returncode=0, stdout="?? docs/awf-plans/ws_coord_plan.md\n"
    )  # before compare
    runner.queue_result(
        returncode=0, stdout="?? docs/awf-plans/ws_coord_plan.md\n"
    )  # after compare
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan",
        "implementation",
        '{"status":"satisfied","summary":"done","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-coordination",
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
        workspace=SimpleNamespace(
            id="ws_coord_plan",
            task_prompt="do overlapping work",
            task_policy=_coordination_task_policy(),
        ),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None
    assert len(adapter.prompts) == 3
    assert "Coordination warnings" in adapter.prompts[0]
    assert "Coordination warnings" in adapter.prompts[1]
    assert "OWNED_PATH_OVERLAP_RISK" in adapter.prompts[0]
    assert "ws_existing" in adapter.prompts[1]
    assert "STALE_OVERLAP" in adapter.prompts[1]


@pytest.mark.unit
async def test_planning_disabled_direct_prompt_includes_coordination_warning(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    adapter = _PlanningAdapter("done")
    profile = WorkspaceProfile.model_validate({"name": "direct-coordination"})

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(
            id="ws_coord_direct",
            task_prompt="do overlapping work",
            task_policy=_coordination_task_policy(),
        ),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None
    assert len(adapter.prompts) == 1
    assert adapter.prompts[0] != "do overlapping work"
    assert "Coordination warnings" in adapter.prompts[0]
    assert "OWNED_PATH_OVERLAP_RISK" in adapter.prompts[0]
    assert "src/awf/service/** -> src/awf/service/workspaces.py" in adapter.prompts[0]
    assert "do overlapping work" in adapter.prompts[0]


@pytest.mark.unit
async def test_planning_required_fails_when_plan_file_is_not_changed(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")
    runner.queue_result(returncode=0, stdout="sha1\n")
    runner.queue_result(returncode=0, stdout="")
    runner.queue_result(returncode=0, stdout="")
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter("plan written elsewhere")
    profile = WorkspaceProfile.model_validate(
        {"name": "planning-missing", "planning": {"required": True}}
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_plan_missing", task_prompt="do it"),  # type: ignore[arg-type]
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
        "planning phase did not create or modify required plan file "
        "`docs/awf-plans/ws_plan_missing.md`"
    )
    assert message.details is not None
    scope = message.details["planning_scope"]
    assert scope["scope_phase"] == "planning"
    assert scope["required_paths"] == ["docs/awf-plans/ws_plan_missing.md"]
    assert scope["offending_paths"] == []
    assert scope["offending_commands"] == []
    assert scope["recovery_strategy"] == "discard_and_replan"
    assert scope["salvage_policy"] == "explicit_salvage_required"
    assert "Retry planning from a clean workspace" in scope["recommended_action"]
    assert len(adapter.prompts) == 1


@pytest.mark.unit
async def test_planning_required_accepts_ignored_plan_file_written_by_agent(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    plan_path = worktree / "docs" / "awf-plans" / "ws_plan_ignored.md"

    class _IgnoredPlanAdapter(_PlanningAdapter):
        async def run(self, **kwargs: object) -> SimpleNamespace:
            result = await super().run(**kwargs)
            if len(self.prompts) == 1:
                plan_path.parent.mkdir(parents=True, exist_ok=True)
                plan_path.write_text("# Plan\n\nUse the on-disk profile.\n", encoding="utf-8")
            return result

    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
    runner.queue_result(returncode=0, stdout="")  # dirty_paths: ignored plan is hidden
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
    runner.queue_result(returncode=0, stdout="")  # before_compare
    runner.queue_result(returncode=0, stdout="")  # after_compare
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD post-compare
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _IgnoredPlanAdapter(
        "plan written",
        "implementation",
        '{"status":"satisfied","summary":"done","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-ignored",
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
        workspace=SimpleNamespace(id="ws_plan_ignored", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert message is None
    assert len(adapter.prompts) == 3


@pytest.mark.unit
async def test_planning_required_skips_digest_fallback_when_git_reports_plan_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    plan_path = Path("docs/awf-plans/ws_plan_tracked.md")
    digest_paths: list[Path] = []

    def _digest(path: Path) -> str | None:
        digest_paths.append(path.relative_to(worktree))
        return None

    monkeypatch.setattr(executor_planning_ops, "_digest_file_if_present", _digest)
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
    runner.queue_result(returncode=0, stdout=f"?? {plan_path.as_posix()}\n")  # dirty_paths
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
    runner.queue_result(returncode=0, stdout=f"?? {plan_path.as_posix()}\n")  # before_compare
    runner.queue_result(returncode=0, stdout=f"?? {plan_path.as_posix()}\n")  # after_compare
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD post-compare
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan written",
        "implementation",
        '{"status":"satisfied","summary":"done","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-tracked",
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
        workspace=SimpleNamespace(id="ws_plan_tracked", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert message is None
    assert digest_paths == [plan_path]


@pytest.mark.unit
def test_digest_file_if_present_streams_file_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = (b"0123456789abcdef" * 8192) + b"tail"
    path = tmp_path / "large-plan.md"
    path.write_bytes(payload)

    def _read_bytes_should_not_be_used(self: Path) -> bytes:
        raise AssertionError(f"unexpected read_bytes for {self}")

    monkeypatch.setattr(Path, "read_bytes", _read_bytes_should_not_be_used)

    assert executor_helpers._digest_file_if_present(path) == hashlib.sha256(payload).hexdigest()


@pytest.mark.unit
def test_exclude_agent_salvage_artifacts_uses_linked_gitdir_exclude(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    linked_git_dir = tmp_path / "mirror.git" / "worktrees" / "ws"
    worktree.mkdir()
    linked_git_dir.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")

    executor_planning_ops._exclude_agent_salvage_artifacts(object(), worktree)  # noqa: SLF001

    assert (linked_git_dir / "info" / "exclude").read_text(encoding="utf-8") == ("/.awf/salvage/\n")


@pytest.mark.unit
async def test_planning_event_helpers_skip_missing_workspace_and_missing_validation_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _MissingWorkspaceRepo:
        def __init__(self, _session: object) -> None:
            return None

        async def get(self, _workspace_id: str) -> object | None:
            return None

    class _MissingRunRepo:
        def __init__(self, _session: object) -> None:
            return None

        async def get(self, _run_id: str) -> object | None:
            return None

    monkeypatch.setattr(executor_planning_ops, "WorkspaceRepository", _MissingWorkspaceRepo)
    monkeypatch.setattr(executor_planning_ops, "ValidationRunRepository", _MissingRunRepo)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._session_factory = lambda: _FakeExecutorSession()  # type: ignore[method-assign]
    handoff = executor_planning_ops._PlanningValidationHandoff(  # noqa: SLF001
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="needs validation",
            gaps=("run tests",),
        ),
        plan_path=Path("plans/task.md"),
        report_path=Path("plans/task.validation.json"),
        iteration=0,
        max_iterations=1,
    )

    await executor_planning_ops._record_planning_validation_handoff_event(  # noqa: SLF001
        executor,
        workspace_id="ws_missing",
        handoff=handoff,
    )
    await executor_planning_ops._record_post_validation_conformance_event(  # noqa: SLF001
        executor,
        workspace_id="ws_missing",
        handoff=handoff,
        report=PlanConformanceReport(
            status=PlanConformanceStatus.satisfied,
            summary="ok",
            gaps=(),
        ),
        validation_run_id="vr_missing",
    )
    evidence = await executor_planning_ops._validation_run_evidence_for_conformance(  # noqa: SLF001
        executor,
        "vr_missing",
    )

    assert '"status": "missing"' in evidence
    assert '"reason_code": "VALIDATION_RUN_NOT_FOUND"' in evidence


@pytest.mark.unit
async def test_auto_retry_planning_scope_failure_records_skip_and_retry_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[tuple[str, str, dict[str, object]]] = []

    class _WorkspaceRepo:
        workspace: object | None = SimpleNamespace(
            id="ws_retry",
            task_policy={"scheduler": {"source_workspace_id": "ws_original"}},
        )

        def __init__(self, _session: object) -> None:
            return None

        async def get(self, _workspace_id: str) -> object | None:
            return self.workspace

        async def add_event(
            self,
            _workspace: object,
            *,
            event_type: str,
            reason_code: str,
            payload: dict[str, object],
        ) -> None:
            events.append((event_type, reason_code, payload))

    monkeypatch.setattr(executor_planning_ops, "WorkspaceRepository", _WorkspaceRepo)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._session_factory = lambda: _FakeExecutorSession()  # type: ignore[method-assign]
    failure = executor_planning_ops._PlanningRunFailure(  # noqa: SLF001
        message="scope violation",
        reason_code=executor_planning_ops.AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    )

    await executor_planning_ops._auto_retry_planning_scope_failure(  # noqa: SLF001
        executor,
        workspace_id="ws_retry",
        failure=failure,
    )
    assert events[-1][1] == "PLANNING_SCOPE_AUTO_RETRY_ALREADY_RETRIED"

    _WorkspaceRepo.workspace = SimpleNamespace(id="ws_retry", task_policy={})

    async def _retry_failed(_session: object, _workspace_id: str) -> object:
        raise executor_planning_ops.WorkspaceRetryError(
            "cannot retry",
            detail={"reason": "busy"},
        )

    monkeypatch.setattr(executor_planning_ops, "retry_workspace_row", _retry_failed)
    await executor_planning_ops._auto_retry_planning_scope_failure(  # noqa: SLF001
        executor,
        workspace_id="ws_retry",
        failure=failure,
    )

    assert events[-1][0] == "workspace.planning_scope_auto_retry_failed"
    assert events[-1][2]["detail"] == {"reason": "busy"}


@pytest.mark.unit
async def test_execution_validation_returns_stop_when_start_transition_is_stale(
    tmp_path: Path,
) -> None:
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=False),
    )

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id="ws_stale_validation",
        ws=SimpleNamespace(),
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_stale_validation",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_stale_validation",
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
    assert not result.has_known_non_plan_output


@pytest.mark.unit
async def test_execution_validation_returns_stop_when_validate_recheck_is_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=False),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
        ),
    )
    workspace = SimpleNamespace(
        resolved_profile={"name": "validation-stale"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        test_commands=[],
        task_class=None,
        operations=[],
    )

    async def _sync_profile_passthrough(*_args: object, **kwargs: object) -> WorkspaceProfile:
        profile = kwargs["profile"]
        assert isinstance(profile, WorkspaceProfile)
        return profile

    monkeypatch.setattr(
        executor_execution_validation,
        "_sync_resolved_profile",
        _sync_profile_passthrough,
    )

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id="ws_recheck_stale",
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_recheck_stale",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_recheck_stale",
        adapter=object(),  # type: ignore[arg-type]
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        has_known_non_plan_output=True,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert result.has_known_non_plan_output


@pytest.mark.unit
async def test_execution_validation_fails_when_workspace_head_sha_cannot_be_captured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ensure validation short-circuits when workspace head SHA cannot be captured."""
    profile = WorkspaceProfile.model_validate({"name": "validation-missing-head"})
    workspace = SimpleNamespace(
        resolved_profile={"name": "validation-missing-head"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        test_commands=[],
        task_class=None,
        operations=[],
    )
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(max_validation_fix_passes=0, planning_max_iterations_default=3),
        _capture_workspace_head_sha=AsyncMock(return_value=None),
        _start_validation_run=AsyncMock(return_value="vr-missing-head"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
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
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_validation_tier_for_workspace",
        lambda *_args, **_kwargs: 1,
    )

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id="ws_missing_head",
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_missing_head",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_missing_head",
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
    executor._start_validation_run.assert_awaited_once_with(
        workspace_id="ws_missing_head",
        profile=profile,
        base_commit="b" * 40,
        workspace_head_sha=None,
        target_branch="awf/ws_missing_head",
        target_head_sha=None,
        tier=1,
    )
    executor._finish_validation_run.assert_awaited_once()
    finish_kwargs = executor._finish_validation_run.await_args.kwargs
    assert finish_kwargs["status"] == "failed"
    assert finish_kwargs["reason_code"] == "VALIDATION_INFRASTRUCTURE_ERROR"
    executor._finish_pending_validate_operations.assert_awaited_once()
    assert executor._finish_pending_validate_operations.await_args.kwargs["status"] == "failed"
    executor._mark_failed.assert_awaited_once()
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert getattr(mark_kwargs["from_status"], "value", mark_kwargs["from_status"]) == "validating"
    assert mark_kwargs["failure_reason"] == FailureReason.infrastructure_failure
    assert mark_kwargs["reason_code"] == "VALIDATION_INFRASTRUCTURE_ERROR"


@pytest.mark.unit
async def test_execution_validation_fails_when_worktree_is_dirty_before_starting_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ensure a dirty worktree fails validation without creating a new run."""
    profile = WorkspaceProfile.model_validate({"name": "validation-dirty-worktree"})
    workspace = SimpleNamespace(
        resolved_profile={"name": "validation-dirty-worktree"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        test_commands=[],
        task_class=None,
        operations=[],
    )
    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(max_validation_fix_passes=0, planning_max_iterations_default=3),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-dirty-worktree"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
    )

    async def _sync_resolved_profile(*_args: object, **_kwargs: object) -> WorkspaceProfile:
        return profile

    async def _check_worktree_clean(*_args: object, **_kwargs: object) -> ValidationWorktreeCheck:
        return ValidationWorktreeCheck(
            clean=False,
            reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
            message="dirty file prevents validation",
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
        "check_validation_worktree_clean",
        _check_worktree_clean,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "profile_phase_command_plan",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_validation_tier_for_workspace",
        lambda *_args, **_kwargs: 1,
    )

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        executor,
        workspace_id="ws_dirty_validation",
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_dirty_validation",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_dirty_validation",
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
    assert result.has_known_non_plan_output is False
    executor._start_validation_run.assert_not_awaited()
    executor._finish_validation_run.assert_not_awaited()
    executor._finish_pending_validate_operations.assert_awaited_once()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["validation_run_id"] is None
    assert finish_kwargs["status"] == OperationStatus.failed
    assert finish_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    executor._mark_failed.assert_awaited_once()
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert getattr(mark_kwargs["from_status"], "value", mark_kwargs["from_status"]) == "validating"
    assert mark_kwargs["failure_reason"] == FailureReason.infrastructure_failure
    assert mark_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY


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
