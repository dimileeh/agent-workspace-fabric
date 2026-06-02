"""Focused branch-coverage tests for executor helper behavior."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor import execution_validation as executor_execution_validation
from awf.control.executor import helpers as executor_helpers
from awf.control.executor import planning_ops as executor_planning_ops
from awf.control.executor import quality_gates as executor_quality_gates
from awf.db.enums import (
    FailureReason,
    OperationStatus,
)
from awf.profiles.models import WorkspaceProfile
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    PlanConformanceReport,
    PlanConformanceStatus,
)
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
)
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup
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

        async def get_for_update(self, workspace_id: str) -> object | None:
            return await self.get(workspace_id)

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

    async def _retry_failed(_session: object, _workspace_id: str, **_kwargs: object) -> object:
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

    _WorkspaceRepo.workspace = SimpleNamespace(id="ws_retry", task_policy={})

    async def _retry_dup_port(_session: object, _workspace_id: str, **_kwargs: object) -> object:
        raise executor_planning_ops.WorkspaceCreateDuplicateHostPortError(host_port=8080)

    monkeypatch.setattr(executor_planning_ops, "retry_workspace_row", _retry_dup_port)
    await executor_planning_ops._auto_retry_planning_scope_failure(  # noqa: SLF001
        executor,
        workspace_id="ws_retry",
        failure=failure,
    )
    assert events[-1][0] == "workspace.planning_scope_auto_retry_failed"
    assert events[-1][2]["detail"] == {"host_port": 8080}

    async def _retry_port_conflict(
        _session: object, _workspace_id: str, **_kwargs: object
    ) -> object:
        raise executor_planning_ops.WorkspaceCreateHostPortConflictError(
            host_port=9090,
            conflicting_workspace_id="ws_other",
        )

    async def _latest_retry_requested(_session: object, _workspace_id: str) -> bool:
        return False

    monkeypatch.setattr(
        executor_planning_ops,
        "_latest_planning_scope_auto_retry_is_retry_requested",
        _latest_retry_requested,
    )
    monkeypatch.setattr(executor_planning_ops, "retry_workspace_row", _retry_port_conflict)
    await executor_planning_ops._auto_retry_planning_scope_failure(  # noqa: SLF001
        executor,
        workspace_id="ws_retry",
        failure=failure,
    )
    assert events[-1][0] == "workspace.planning_scope_auto_retry_blocked"
    assert events[-1][1] == "PLANNING_SCOPE_AUTO_RETRY_HOST_PORT_CONFLICT"
    assert events[-1][2]["detail"]["host_port"] == 9090
    assert events[-1][2]["retry_after"] == "terminal_runtime_released"


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
        """Stub resolved profile lookup for head-capture failure path."""
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
async def test_execution_validation_reports_dirty_worktree_when_head_capture_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ensure pre-validation dirty worktrees are not hidden by HEAD capture failures."""
    profile = WorkspaceProfile.model_validate({"name": "validation-dirty-missing-head"})
    workspace = SimpleNamespace(
        resolved_profile={"name": "validation-dirty-missing-head"},
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
        _start_validation_run=AsyncMock(return_value="vr-dirty-missing-head"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
    )
    check_worktree_clean = AsyncMock(
        return_value=ValidationWorktreeCheck(
            clean=False,
            reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
            message="dirty file prevents validation",
        )
    )

    async def _sync_resolved_profile(*_args: object, **_kwargs: object) -> WorkspaceProfile:
        """Stub resolved profile lookup for dirty-before-missing-head test."""
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
        "check_validation_worktree_clean",
        check_worktree_clean,
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
        workspace_id="ws_dirty_missing_head",
        ws=workspace,  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        compose_project="awf_ws_dirty_missing_head",
        compose_file=tmp_path / "compose.yml",
        base_commit="b" * 40,
        expected_branch="awf/ws_dirty_missing_head",
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
    check_worktree_clean.assert_awaited_once()
    executor._start_validation_run.assert_awaited_once_with(
        workspace_id="ws_dirty_missing_head",
        profile=profile,
        base_commit="b" * 40,
        workspace_head_sha=None,
        target_branch="awf/ws_dirty_missing_head",
        target_head_sha=None,
        tier=1,
    )
    executor._finish_validation_run.assert_awaited_once()
    assert (
        executor._finish_validation_run.await_args.kwargs["reason_code"]
        == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    )
    executor._finish_pending_validate_operations.assert_awaited_once()
    assert (
        executor._finish_pending_validate_operations.await_args.kwargs["reason_code"]
        == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    )
    executor._mark_failed.assert_awaited_once()
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_kwargs["failure_reason"] == FailureReason.infrastructure_failure
    assert mark_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY


@pytest.mark.unit
async def test_execution_validation_fails_when_worktree_is_dirty_before_starting_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ensure a dirty worktree finalizes its started validation run."""
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
        """Stub resolved profile lookup for dirty-worktree preflight test."""
        return profile

    async def _check_worktree_clean(*_args: object, **_kwargs: object) -> ValidationWorktreeCheck:
        """Return a pre-existing dirty-worktree result for this test case."""
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
        has_known_non_plan_output=True,
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result.stop
    assert result.successful_validation_run_id is None
    assert result.has_known_non_plan_output is True
    executor._start_validation_run.assert_awaited_once_with(
        workspace_id="ws_dirty_validation",
        profile=profile,
        base_commit="b" * 40,
        workspace_head_sha="c" * 40,
        target_branch="awf/ws_dirty_validation",
        target_head_sha=None,
        tier=1,
    )
    executor._finish_validation_run.assert_awaited_once()
    finish_run_kwargs = executor._finish_validation_run.await_args.kwargs
    assert finish_run_kwargs["status"] == "failed"
    assert finish_run_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    executor._finish_pending_validate_operations.assert_awaited_once()
    finish_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_kwargs["validation_run_id"] == "vr-dirty-worktree"
    assert finish_kwargs["status"] == OperationStatus.failed
    assert finish_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    executor._mark_failed.assert_awaited_once()
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert getattr(mark_kwargs["from_status"], "value", mark_kwargs["from_status"]) == "validating"
    assert mark_kwargs["failure_reason"] == FailureReason.infrastructure_failure
    assert mark_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY


@pytest.mark.unit
async def test_execution_validation_rejects_new_ignored_paths_after_initial_validation_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = WorkspaceProfile.model_validate({"name": "validation-ignored-baseline"})
    workspace = SimpleNamespace(
        resolved_profile={"name": "validation-ignored-baseline"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        test_commands=[],
        task_class=None,
        operations=[],
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
    later_check = ValidationWorktreeCheck(
        clean=True,
        ignored_paths=("ignored-output/",),
        ignored_paths_snapshot=("ignored-output/stable.txt", "ignored-output/new.txt"),
    )

    class _Validation:
        def __init__(self) -> None:
            self.run_profile_calls = 0

        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            self.run_profile_calls += 1
            assert self.run_profile_calls == 1
            return ValidationResult(commands=[_command_result(tmp_path, returncode=1)])

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
        AsyncMock(side_effect=[setup_check, setup_check, later_check]),
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
    assert executor._start_validation_run.await_count == 2
    assert executor._validation.run_profile_calls == 1
    assert executor._finish_validation_run.await_args.kwargs["status"] == "failed"
    assert (
        executor._finish_validation_run.await_args.kwargs["reason_code"]
        == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    )
    finish_pending_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_pending_kwargs["status"] == OperationStatus.failed
    assert finish_pending_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert getattr(mark_kwargs["from_status"], "value", mark_kwargs["from_status"]) == "validating"
    assert mark_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert adapter.run.await_count == 1


@pytest.mark.unit
async def test_execution_validation_rejects_ignored_signature_drift_after_fix_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = WorkspaceProfile.model_validate({"name": "validation-ignored-baseline"})
    workspace = SimpleNamespace(
        resolved_profile={"name": "validation-ignored-baseline"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        test_commands=[],
        task_class=None,
        operations=[],
        task_title="Task with ignored artifact",
        agent="codex",
        owned_paths=(),
        id="ws_ignored",
    )

    setup_check = ValidationWorktreeCheck(
        clean=True,
        ignored_paths=("ignored-output/",),
        ignored_paths_snapshot=("ignored-output/stable.txt",),
        ignored_paths_snapshot_signatures=(("ignored-output/stable.txt", "sig-setup"),),
    )
    later_check = ValidationWorktreeCheck(
        clean=True,
        ignored_paths=("ignored-output/",),
        ignored_paths_snapshot=("ignored-output/stable.txt",),
        ignored_paths_snapshot_signatures=(("ignored-output/stable.txt", "sig-mutated"),),
    )

    class _Validation:
        def __init__(self) -> None:
            self.run_profile_calls = 0

        async def run_profile_phases(self, **_kwargs: object) -> ValidationResult:
            self.run_profile_calls += 1
            assert self.run_profile_calls == 1
            return ValidationResult(commands=[_command_result(tmp_path, returncode=1)])

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
        AsyncMock(side_effect=[setup_check, later_check, later_check]),
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
    assert executor._start_validation_run.await_count == 2
    assert executor._validation.run_profile_calls == 1
    assert executor._finish_validation_run.await_args.kwargs["status"] == "failed"
    assert (
        executor._finish_validation_run.await_args.kwargs["reason_code"]
        == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    )
    finish_pending_kwargs = executor._finish_pending_validate_operations.await_args.kwargs
    assert finish_pending_kwargs["status"] == OperationStatus.failed
    assert finish_pending_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    mark_kwargs = executor._mark_failed.await_args.kwargs
    assert getattr(mark_kwargs["from_status"], "value", mark_kwargs["from_status"]) == "validating"
    assert mark_kwargs["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert adapter.run.await_count == 1
