"""Focused branch-coverage tests for executor helper behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor import helpers as executor_helpers
from awf.control.executor import quality_gates as executor_quality_gates
from awf.control.executor.helpers import (
    _failure_reason_for_phase,
    _failure_salvage_payload,
    _raw_profile_has_explicit_planning_max_iterations,
    _should_run_local_coverage,
    _validation_run_command_records,
)
from awf.control.executor.logging_ops import _validation_run_log_stream_refs
from awf.control.executor.types import (
    _PlanningValidationHandoff,
)
from awf.db.enums import (
    FailureReason,
)
from awf.profiles.models import WorkspaceProfile
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    PlanConformanceReport,
    PlanConformanceStatus,
)
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
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


def _fake_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    mirror = tmp_path / "mirror.git"
    linked_git_dir = mirror / "worktrees" / "ws_missing_head"
    linked_git_dir.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    return mirror, worktree


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.unit
async def test_post_validation_conformance_rejects_committed_paths_when_deviation_guard_disabled(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # changed paths before conformance
    runner.queue_result(returncode=0, stdout="")  # clean status after conformance
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )
    executor._git_rev_parse_head = AsyncMock(return_value="validated-head")  # type: ignore[method-assign]
    executor._committed_paths_since = AsyncMock(  # type: ignore[method-assign]
        return_value={Path("src/unvalidated.py")}
    )
    executor._repair_agent_git_ownership = AsyncMock(return_value=True)  # type: ignore[method-assign]
    executor._record_post_validation_conformance_event = AsyncMock()  # type: ignore[method-assign]
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planned",
            "planning": {
                "required": True,
                "fail_on_unexplained_deviation": False,
            },
        }
    )
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    handoff = _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="AWF validation evidence is missing.",
            gaps=("Run AWF validation.",),
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        ),
        plan_path=Path("docs/awf-plans/ws_post.md"),
        report_path=report_path,
        iteration=0,
        max_iterations=2,
    )

    failure = await executor._run_post_validation_conformance_check(
        adapter=_PlanningAdapter(
            '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}'
        ),  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
    )

    assert failure is not None
    assert failure.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert failure.details is not None
    assert failure.details["planning_scope"]["offending_paths"] == ["src/unvalidated.py"]
    executor._git_rev_parse_head.assert_awaited_once_with(  # type: ignore[attr-defined]
        tmp_path / "worktree"
    )
    executor._committed_paths_since.assert_awaited_once_with(  # type: ignore[attr-defined]
        tmp_path / "worktree",
        "validated-head",
    )
    executor._record_post_validation_conformance_event.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_autofixable_precommit_repair_skips_when_no_staged_python_matches(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._record_post_agent_commit_format_repair = AsyncMock()  # type: ignore[method-assign]

    repaired = await executor._run_post_agent_autofixable_precommit_repair(
        workspace_id="ws_autofix",
        worktree_path=tmp_path / "worktree",
        commit_result=CommandResult(returncode=1, stdout="", stderr="pre-commit failed"),
        classification=_autofix_classification(repair_files=("src/app.py",)),
        staged_paths=["README.md"],
        run_commit=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert repaired is False
    executor._record_post_agent_commit_format_repair.assert_awaited_once()  # type: ignore[attr-defined]
    assert (
        executor._record_post_agent_commit_format_repair.await_args.kwargs["retry_outcome"]  # type: ignore[attr-defined]
        == "skipped"
    )


@pytest.mark.unit
async def test_autofixable_precommit_repair_raises_when_ruff_fix_fails(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=1, stderr="ruff failed")
    executor = _executor_with_runner(runner, tmp_path)
    executor._record_post_agent_commit_format_repair = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(executor_quality_gates._PostAgentCommitStepError) as exc_info:
        await executor._run_post_agent_autofixable_precommit_repair(
            workspace_id="ws_autofix",
            worktree_path=tmp_path / "worktree",
            commit_result=CommandResult(returncode=1, stdout="", stderr="pre-commit failed"),
            classification=_autofix_classification(),
            staged_paths=["src/app.py"],
            run_commit=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
            git_in_worktree=AsyncMock(
                return_value=CommandResult(returncode=0, stdout="", stderr="")
            ),
        )

    assert exc_info.value.stage == "ruff check --fix"
    assert exc_info.value.reason_code_override == "POST_AGENT_FORMAT_REPAIR_FAILED"
    assert runner.calls[0].args[-2:] == ["--", "src/app.py"]


@pytest.mark.unit
async def test_autofixable_precommit_repair_raises_when_ruff_format_fails(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0)  # ruff check --fix succeeds
    runner.queue_result(returncode=1, stderr="ruff format failed")
    executor = _executor_with_runner(runner, tmp_path)
    executor._record_post_agent_commit_format_repair = AsyncMock()  # type: ignore[method-assign]

    classification = executor_quality_gates._PostAgentCommitClassification(  # noqa: SLF001
        reason_code="POST_AGENT_COMMIT_AUTOFIX_NEEDED",
        failed_hooks=("ruff-check", "awf-ruff-format-check"),
        format_repair_files=("src/app.py",),
        normalizer_repair_files=(),
        autofix_repair_files=("src/app.py",),
        summary="ruff reported fixable diagnostics and format issues",
        repair_strategy="deterministic_autofix",
    )

    with pytest.raises(executor_quality_gates._PostAgentCommitStepError) as exc_info:
        await executor._run_post_agent_autofixable_precommit_repair(
            workspace_id="ws_autofix",
            worktree_path=tmp_path / "worktree",
            commit_result=CommandResult(returncode=1, stdout="", stderr="pre-commit failed"),
            classification=classification,
            staged_paths=["src/app.py"],
            run_commit=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
            git_in_worktree=AsyncMock(
                return_value=CommandResult(returncode=0, stdout="", stderr="")
            ),
        )

    assert exc_info.value.stage == "ruff format"
    assert exc_info.value.reason_code_override == "POST_AGENT_FORMAT_REPAIR_FAILED"
    assert exc_info.value.format_repair_attempted is True
    assert exc_info.value.precommit_repair_attempted is True
    assert exc_info.value.repair_strategy == "deterministic_autofix"

    executor._record_post_agent_commit_format_repair.assert_awaited_once()  # type: ignore[attr-defined]
    record_kwargs = executor._record_post_agent_commit_format_repair.await_args.kwargs  # type: ignore[attr-defined]
    assert record_kwargs["retry_outcome"] == "error"
    assert record_kwargs["formatter_paths"] == ["src/app.py"]
    assert record_kwargs["repaired_paths"] == ["src/app.py"]

    ruff_calls = [call for call in runner.calls if "ruff" in call.args]
    assert len(ruff_calls) == 2
    assert "check" in ruff_calls[0].args and "--fix" in ruff_calls[0].args
    assert "format" in ruff_calls[1].args and "--check" not in ruff_calls[1].args


@pytest.mark.unit
async def test_autofixable_precommit_repair_raises_when_restaging_fails(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0)
    executor = _executor_with_runner(runner, tmp_path)
    executor._record_post_agent_commit_format_repair = AsyncMock()  # type: ignore[method-assign]
    executor._repair_agent_git_ownership = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with pytest.raises(executor_quality_gates._PostAgentCommitStepError) as exc_info:
        await executor._run_post_agent_autofixable_precommit_repair(
            workspace_id="ws_autofix",
            worktree_path=tmp_path / "worktree",
            commit_result=CommandResult(returncode=1, stdout="", stderr="pre-commit failed"),
            classification=_autofix_classification(),
            staged_paths=["src/app.py"],
            run_commit=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
            git_in_worktree=AsyncMock(
                return_value=CommandResult(returncode=1, stdout="", stderr="add failed")
            ),
        )

    assert exc_info.value.stage == "git add"
    assert exc_info.value.reason_code_override == "POST_AGENT_FORMAT_REPAIR_FAILED"


@pytest.mark.unit
async def test_autofixable_precommit_repair_commits_repaired_paths(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0)
    executor = _executor_with_runner(runner, tmp_path)
    executor._record_post_agent_commit_format_repair = AsyncMock()  # type: ignore[method-assign]
    executor._repair_agent_git_ownership = AsyncMock(return_value=True)  # type: ignore[method-assign]
    run_commit = AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr=""))
    git_in_worktree = AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr=""))

    repaired = await executor._run_post_agent_autofixable_precommit_repair(
        workspace_id="ws_autofix",
        worktree_path=tmp_path / "worktree",
        commit_result=CommandResult(returncode=1, stdout="", stderr="pre-commit failed"),
        classification=_autofix_classification(),
        staged_paths=["src/app.py"],
        run_commit=run_commit,
        git_in_worktree=git_in_worktree,
    )

    assert repaired is True
    run_commit.assert_awaited_once()
    git_in_worktree.assert_awaited_once_with(["add", "--", "src/app.py"])
    assert (
        executor._record_post_agent_commit_format_repair.await_args.kwargs["retry_outcome"]  # type: ignore[attr-defined]
        == "succeeded"
    )


@pytest.mark.unit
async def test_autofixable_precommit_repair_runs_format_on_autofixed_paths(
    tmp_path: Path,
) -> None:
    """Regression: ruff format must run on autofixed paths even when
    classification.format_repair_files is empty.  Ruff check --fix can
    produce import-sort changes that need formatting, so the formatter
    must run over the union of format_repair_paths and repair_paths
    (see PR review thread PRRT_kwDOSJAM6s6F5kGM).
    """
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0)  # ruff check --fix succeeds
    runner.queue_result(returncode=0)  # ruff format succeeds
    executor = _executor_with_runner(runner, tmp_path)
    executor._record_post_agent_commit_format_repair = AsyncMock()  # type: ignore[method-assign]
    executor._repair_agent_git_ownership = AsyncMock(return_value=True)  # type: ignore[method-assign]
    run_commit = AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr=""))
    git_in_worktree = AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr=""))

    classification = executor_quality_gates._PostAgentCommitClassification(  # noqa: SLF001
        reason_code="POST_AGENT_COMMIT_AUTOFIX_NEEDED",
        failed_hooks=("ruff-check",),
        format_repair_files=(),
        normalizer_repair_files=(),
        autofix_repair_files=("src/app.py",),
        summary="ruff reported fixable diagnostics",
        repair_strategy="deterministic_autofix",
    )

    repaired = await executor._run_post_agent_autofixable_precommit_repair(
        workspace_id="ws_autofix",
        worktree_path=tmp_path / "worktree",
        commit_result=CommandResult(returncode=1, stdout="", stderr="pre-commit failed"),
        classification=classification,
        staged_paths=["src/app.py"],
        run_commit=run_commit,
        git_in_worktree=git_in_worktree,
    )

    assert repaired is True
    ruff_calls = [call for call in runner.calls if "ruff" in call.args]
    assert len(ruff_calls) == 2
    assert "check" in ruff_calls[0].args and "--fix" in ruff_calls[0].args
    assert "format" in ruff_calls[1].args and "--check" not in ruff_calls[1].args
    assert "src/app.py" in ruff_calls[1].args
    record_kwargs = executor._record_post_agent_commit_format_repair.await_args.kwargs  # type: ignore[attr-defined]
    assert record_kwargs["formatter_paths"] == ["src/app.py"]


@pytest.mark.unit
async def test_autofixable_precommit_repair_raises_when_retry_commit_still_fails(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0)
    executor = _executor_with_runner(runner, tmp_path)
    executor._record_post_agent_commit_format_repair = AsyncMock()  # type: ignore[method-assign]
    executor._repair_agent_git_ownership = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with pytest.raises(executor_quality_gates._PostAgentCommitStepError) as exc_info:
        await executor._run_post_agent_autofixable_precommit_repair(
            workspace_id="ws_autofix",
            worktree_path=tmp_path / "worktree",
            commit_result=CommandResult(returncode=1, stdout="", stderr="pre-commit failed"),
            classification=_autofix_classification(),
            staged_paths=["src/app.py"],
            run_commit=AsyncMock(
                return_value=CommandResult(returncode=1, stdout="", stderr="commit still failed")
            ),
            git_in_worktree=AsyncMock(
                return_value=CommandResult(returncode=0, stdout="", stderr="")
            ),
        )

    assert exc_info.value.stage == "git commit"
    assert exc_info.value.precommit_repair_attempted is True
    assert exc_info.value.repair_strategy == "deterministic_autofix"


@pytest.mark.unit
async def test_autofixable_precommit_repair_retry_commit_format_rewrite_override(
    tmp_path: Path,
) -> None:
    """Regression: autofix retry commit that re-fails with awf-ruff-format-check
    must override reason_code to POST_AGENT_FORMAT_REPAIR_FAILED, matching the
    deterministic repair path (see PR review thread PRRT_kwDOSJAM6s6F5i4A).
    """
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0)
    runner.queue_result(returncode=0)
    executor = _executor_with_runner(runner, tmp_path)
    executor._record_post_agent_commit_format_repair = AsyncMock()  # type: ignore[method-assign]
    executor._repair_agent_git_ownership = AsyncMock(return_value=True)  # type: ignore[method-assign]

    format_only_retry_stderr = "\n".join(
        [
            "ruff format --check.....................................................Failed",
            "- hook id: awf-ruff-format-check",
            "- exit code: 1",
            "",
            "Would reformat: src/app.py",
            "1 file would be reformatted",
        ]
    )
    with pytest.raises(executor_quality_gates._PostAgentCommitStepError) as exc_info:
        await executor._run_post_agent_autofixable_precommit_repair(
            workspace_id="ws_autofix",
            worktree_path=tmp_path / "worktree",
            commit_result=CommandResult(returncode=1, stdout="", stderr="pre-commit failed"),
            classification=_autofix_classification(),
            staged_paths=["src/app.py"],
            run_commit=AsyncMock(
                return_value=CommandResult(returncode=1, stdout="", stderr=format_only_retry_stderr)
            ),
            git_in_worktree=AsyncMock(
                return_value=CommandResult(returncode=0, stdout="", stderr="")
            ),
        )

    assert exc_info.value.stage == "git commit"
    assert exc_info.value.precommit_repair_attempted is True
    assert exc_info.value.repair_strategy == "deterministic_autofix"
    assert exc_info.value.reason_code_override == "POST_AGENT_FORMAT_REPAIR_FAILED"


@pytest.mark.unit
def test_failure_reason_for_phase_maps_setup_timeout_and_healthcheck() -> None:
    assert (
        _failure_reason_for_phase(
            SimpleNamespace(phase="healthcheck", reason_code="COMMAND_FAILED")
        )
        == FailureReason.health_check_failure
    )
    assert (
        _failure_reason_for_phase(SimpleNamespace(phase="validate", reason_code="PHASE_TIMEOUT"))
        == FailureReason.phase_timeout
    )
    assert (
        _failure_reason_for_phase(SimpleNamespace(phase="pre_agent", reason_code="COMMAND_FAILED"))
        == FailureReason.service_startup_failure
    )
    assert (
        _failure_reason_for_phase(
            SimpleNamespace(
                phase="profile_preflight",
                reason_code="PROFILE_VALIDATION_TOOL_UNAVAILABLE",
            )
        )
        == FailureReason.profile_resolution_failure
    )
    assert _failure_reason_for_phase(None) == FailureReason.validation_failure


@pytest.mark.unit
def test_executor_small_helpers_handle_absent_optional_metadata(tmp_path: Path) -> None:
    assert _raw_profile_has_explicit_planning_max_iterations(None) is False
    assert _raw_profile_has_explicit_planning_max_iterations({"planning": []}) is False

    salvage = _failure_salvage_payload(  # type: ignore[arg-type]
        SimpleNamespace(branch_name=None, remote_push_branch=None),
        worktree_path=tmp_path / "worktree",
    )

    assert salvage == {
        "hint": "Workspace worktree and branch were preserved for salvage.",
        "worktree_path": str(tmp_path / "worktree"),
    }


@pytest.mark.unit
def test_failure_reason_for_database_hook_phase() -> None:
    assert (
        _failure_reason_for_phase(
            SimpleNamespace(
                phase="db_generated_setup",
                reason_code="DATABASE_GENERATED_SETUP_TIMEOUT",
            )
        )
        == FailureReason.phase_timeout
    )
    assert (
        _failure_reason_for_phase(
            SimpleNamespace(phase="db_refresh", reason_code="DATABASE_REFRESH_TIMEOUT")
        )
        == FailureReason.phase_timeout
    )
    assert (
        _failure_reason_for_phase(
            SimpleNamespace(
                phase="db_generated_setup",
                reason_code="DATABASE_GENERATED_SETUP_FAILED",
            )
        )
        == FailureReason.service_startup_failure
    )
    assert (
        _failure_reason_for_phase(
            SimpleNamespace(phase="db_refresh", reason_code="DATABASE_REFRESH_FAILED")
        )
        == FailureReason.validation_failure
    )


@pytest.mark.unit
def test_validation_run_log_stream_refs_preserve_only_string_stream_ids() -> None:
    refs = _validation_run_log_stream_refs(
        [
            {"stream_ids": {"stdout": "validation.01.stdout", "stderr": 123}},
            {"stream_ids": "not-a-dict"},
            {},
        ]
    )

    assert refs == {
        "commands": [
            {"stdout": "validation.01.stdout", "stderr": None},
            {"stdout": None, "stderr": None},
            {"stdout": None, "stderr": None},
        ]
    }


@pytest.mark.unit
def test_validation_run_command_records_include_healthchecks_and_coverage() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records",
            "phases": {
                "post_agent": ["ruff format --check"],
                "validate": ["pytest -q"],
            },
            "validation": {
                "healthchecks": [{"name": "api", "command": "curl -fsS localhost/health"}],
                "strategy": {"final_gate": "coverage"},
                "coverage": {"command": "pytest --cov=awf --cov-report=term"},
            },
        }
    )

    records = _validation_run_command_records(
        profile=profile,
        phase_names=("post_agent", "validate"),
        run_healthchecks=True,
    )

    assert [(record["phase"], record["command_index"]) for record in records] == [
        ("healthcheck", 1),
        ("post_agent", 1),
        ("validate", 1),
        ("coverage", 1),
    ]
    assert records[-1]["stream_ids"] == {
        "stdout": "validation.01_coverage.stdout",
        "stderr": "validation.01_coverage.stderr",
    }


@pytest.mark.unit
def test_validation_run_command_records_include_database_refresh_hooks() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records-db-refresh",
            "phases": {
                "post_agent": ["ruff format --check"],
                "validate": ["pytest -q"],
            },
            "database": {
                "pre_validation_refresh": [
                    {"command": "python scripts/db_refresh.py", "timeout_seconds": 120}
                ]
            },
            "validation": {
                "healthchecks": [{"name": "api", "command": "curl -fsS localhost/health"}],
            },
        }
    )

    records = _validation_run_command_records(
        profile=profile,
        phase_names=("post_agent", "validate"),
        run_healthchecks=True,
    )

    assert [(record["phase"], record["command_index"]) for record in records] == [
        ("post_agent", 1),
        ("db_refresh", 1),
        ("healthcheck", 1),
        ("validate", 1),
    ]
    assert records[1] == {
        "phase": "db_refresh",
        "command": "python scripts/db_refresh.py",
        "command_index": 1,
        "database_hook": True,
        "hook_kind": "pre_validation_refresh",
        "timeout_seconds": 120,
        "stream_ids": {
            "stdout": "validation.01_db_refresh.stdout",
            "stderr": "validation.01_db_refresh.stderr",
        },
    }


@pytest.mark.unit
def test_validation_run_command_records_run_pending_healthchecks_after_refresh_without_validate() -> (
    None
):
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records-db-refresh-no-validate",
            "database": {"pre_validation_refresh": ["python scripts/db_refresh.py"]},
            "validation": {
                "healthchecks": [{"name": "api", "command": "curl -fsS localhost/health"}],
            },
        }
    )

    records = _validation_run_command_records(
        profile=profile,
        phase_names=("validate",),
        run_healthchecks=True,
    )

    assert [(record["phase"], record["command_index"]) for record in records] == [
        ("db_refresh", 1),
        ("healthcheck", 1),
    ]


@pytest.mark.unit
def test_validation_command_records_omit_coverage_when_no_local_command_is_declared() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records",
            "phases": {"validate": ["pytest tests/unit -q"]},
        }
    )

    records = _validation_run_command_records(
        profile=profile,
        phase_names=("validate",),
        run_healthchecks=False,
    )

    assert [(record["phase"], record["command"]) for record in records] == [
        ("validate", "pytest tests/unit -q")
    ]


@pytest.mark.unit
def test_local_coverage_runs_only_for_explicit_final_gate_with_coverage_command() -> None:
    no_local_coverage = WorkspaceProfile.model_validate(
        {
            "name": "awf-self",
            "validation": {
                "strategy": {"edit_gate": "targeted"},
                "coverage": {"command": "uv run pytest --cov=awf"},
            },
            "phases": {"validate": ["uv run pytest tests/unit/cli -q"]},
        }
    )
    final_gate_without_command = WorkspaceProfile.model_validate(
        {
            "name": "final-gate-without-command",
            "validation": {"strategy": {"final_gate": "coverage"}},
        }
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "explicit-local-coverage",
            "validation": {
                "strategy": {"edit_gate": "targeted", "final_gate": "coverage"},
                "coverage": {
                    "minimum_percent": 99,
                    "enforce": True,
                    "command": "uv run --python 3.12 --extra dev pytest --cov=awf",
                },
            },
            "phases": {"validate": ["uv run pytest tests/unit/cli -q"]},
        }
    )

    assert _should_run_local_coverage(no_local_coverage) is False
    assert _should_run_local_coverage(final_gate_without_command) is False
    assert _should_run_local_coverage(profile) is True


@pytest.mark.unit
def test_validation_command_records_omit_coverage_without_local_final_gate() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records-coverage-disabled-final-gate",
            "validation": {
                "strategy": {"edit_gate": "targeted", "final_gate": "none"},
                "coverage": {"command": "pytest --cov=awf"},
            },
            "phases": {"validate": ["pytest tests/unit/cli -q"]},
        }
    )

    records = _validation_run_command_records(
        profile=profile,
        phase_names=("validate",),
        run_healthchecks=False,
    )

    assert [(record["phase"], record["command"]) for record in records] == [
        ("validate", "pytest tests/unit/cli -q")
    ]


@pytest.mark.unit
def test_validation_command_records_can_mark_coverage_reused() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records",
            "validation": {
                "strategy": {"final_gate": "coverage"},
                "coverage": {"command": "pytest --cov=awf"},
            },
        }
    )

    records = _validation_run_command_records(
        profile=profile,
        phase_names=("validate",),
        run_healthchecks=False,
        coverage_evidence_status="reused",
        coverage_evidence_reason_code="VALIDATION_EVIDENCE_REUSED",
    )

    assert records[-1]["phase"] == "coverage"
    assert records[-1]["evidence_status"] == "reused"
    assert records[-1]["evidence_reason_code"] == "VALIDATION_EVIDENCE_REUSED"


@pytest.mark.unit
def test_validation_command_records_raise_when_coverage_predicate_loses_invariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records-missing-coverage-command",
            "validation": {"strategy": {"final_gate": "coverage"}},
            "phases": {"validate": ["pytest tests/unit -q"]},
        }
    )
    monkeypatch.setattr(executor_helpers, "_should_run_local_coverage", lambda _: True)

    with pytest.raises(RuntimeError, match="coverage.command is None"):
        _validation_run_command_records(
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=False,
        )
