"""Focused branch-coverage tests for executor helper behavior."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import awf.control.executor as executor_mod
from awf.adapters.base import AgentDefaults, AgentRunError
from awf.common.commands import AsyncioSubprocessRunner, CommandResult, FakeCommandRunner
from awf.control.executor import (
    GIT_OBJECT_MISSING_REASON_CODE,
    GIT_OBJECT_MISSING_RECOVERED_REASON_CODE,
    PLAN_ONLY_OUTPUT_REASON_CODE,
    ExecutorConfig,
    WorkspaceExecutor,
    _agent_defaults_for_workspace,
    _agent_git_writability_preflight_script,
    _agent_model_for_workspace,
    _agent_pr_identity,
    _apply_baseline_coverage_ratchet,
    _call_pr_monitor_factory,
    _coverage_has_failing_tests,
    _coverage_preserves_below_threshold_baseline,
    _coverage_wrapped_pytest_failure_message,
    _failure_reason_for_phase,
    _failure_salvage_payload,
    _format_failing_test_evidence,
    _get_active_recovery_payload,
    _git_error_indicates_missing_head_object,
    _GitObjectRecoveryResult,
    _MonitorRebaseRecoveryError,
    _planning_validation_handoff_from_recovery_payload,
    _PlanningValidationHandoff,
    _profile_with_planning_iteration_default,
    _raw_profile_has_explicit_planning_max_iterations,
    _read_ref_sha,
    _read_text_if_present,
    _RebaseRecoveryResult,
    _recover_missing_head_from_filesystem,
    _recovery_needs_existing_pr_push,
    _should_run_local_coverage,
    _validation_command_count,
    _validation_failure_message,
    _validation_run_command_records,
    _validation_run_coverage_metadata,
    _validation_run_log_stream_refs,
    _validation_run_reason_code,
    _validation_tier_for_workspace,
)
from awf.db.enums import (
    AgentRuntime,
    FailureReason,
    OperationStatus,
    OperationType,
    TaskClass,
    WorkspaceStatus,
)
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.profiles.models import ProfilePlanning, WorkspaceProfile
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    PLAN_CONFORMANCE_UNSATISFIED,
    PlanConformanceReport,
    PlanConformanceStatus,
)
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
)
from awf.runtime.validation_identity import (
    environment_identity_digest,
    resolved_profile_digest,
)
from tests.postgres import create_postgres_test_engine


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


@pytest.mark.unit
def test_read_ref_sha_falls_back_to_packed_refs_and_missing_ref(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror.git"
    mirror.mkdir()
    (mirror / "packed-refs").write_text(
        "\n".join(
            [
                "# pack-refs with: peeled fully-peeled sorted",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa refs/heads/main",
                "^bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "cccccccccccccccccccccccccccccccccccccccc refs/remotes/origin/feature",
            ]
        ),
        encoding="utf-8",
    )

    assert _read_ref_sha(mirror, "refs/remotes/origin/feature") == (
        "cccccccccccccccccccccccccccccccccccccccc"
    )
    assert _read_ref_sha(mirror, "refs/remotes/origin/missing") is None


@pytest.mark.unit
def test_recovery_conformance_gaps_accepts_string_and_empty_values() -> None:
    assert executor_mod._recovery_conformance_gaps(  # noqa: SLF001
        {"gaps": [" first gap ", "", 42]}
    ) == ("first gap", "42")
    assert executor_mod._recovery_conformance_gaps({"gaps": " rerun AWF validation "}) == (  # noqa: SLF001
        "rerun AWF validation",
    )
    assert executor_mod._recovery_conformance_gaps({"gaps": ""}) == ()  # noqa: SLF001
    assert executor_mod._recovery_conformance_gaps({"gaps": object()}) == ()  # noqa: SLF001


@pytest.mark.unit
@pytest.mark.parametrize(
    ("recovery_payload", "head_sha", "rebase_result", "expected"),
    [
        (
            {"recovery_mode": "rebase_only", "source_head_sha": "old"},
            "new",
            None,
            False,
        ),
        (
            {"recovery_mode": "rebase_only", "source_head_sha": "old"},
            "rebased",
            _RebaseRecoveryResult(base_sha="base", head_sha="rebased"),
            False,
        ),
        (
            {"recovery_mode": "rebase_only", "source_head_sha": "old"},
            "post-validation-report",
            _RebaseRecoveryResult(base_sha="base", head_sha="rebased"),
            True,
        ),
        (
            {"recovery_mode": "validate_only", "source_head_sha": "old"},
            "new",
            _RebaseRecoveryResult(base_sha="base", head_sha="rebased"),
            False,
        ),
        (
            {"recovery_mode": "validate_only", "source_head_sha": "old"},
            None,
            None,
            False,
        ),
        (
            {"recovery_mode": "validate_only", "source_head_sha": "old"},
            "",
            None,
            False,
        ),
        (
            {"recovery_mode": "validate_only", "source_head_sha": "old"},
            "new",
            None,
            True,
        ),
        (
            {"recovery_mode": "validate_only", "source_head_sha": "old"},
            "   ",
            None,
            False,
        ),
        (
            {"recovery_mode": "unexpected"},
            "new",
            None,
            False,
        ),
    ],
)
def test_recovery_needs_existing_pr_push_edges(
    recovery_payload: dict[str, object],
    head_sha: str | None,
    rebase_result: _RebaseRecoveryResult | None,
    expected: bool,
) -> None:
    assert (
        _recovery_needs_existing_pr_push(
            recovery_payload,
            validated_workspace_head_sha=head_sha,
            rebase_recovery_result=rebase_result,
        )
        is expected
    )


@pytest.mark.unit
def test_ruff_check_autofix_repair_files_ignores_fixable_diagnostic_without_path() -> None:
    assert executor_mod._ruff_check_autofix_repair_files(  # noqa: SLF001
        "F401 [*] imported but unused\n"
        "help: Remove unused import\n"
        "F841 [*] local variable is assigned to but never used\n"
        "--> src/awf/example.py:10:5\n"
    ) == ("src/awf/example.py",)


@pytest.mark.unit
def test_recovery_needs_existing_pr_push_rejects_blank_and_unknown_modes() -> None:
    assert not _recovery_needs_existing_pr_push(
        {"recovery_mode": "validate_only", "source_head_sha": "old"},
        validated_workspace_head_sha="   ",
        rebase_recovery_result=None,
    )
    assert not _recovery_needs_existing_pr_push(
        {"recovery_mode": "repair_only", "source_head_sha": "old"},
        validated_workspace_head_sha="new",
        rebase_recovery_result=None,
    )


@pytest.mark.unit
def test_setup_dependency_network_failure_details_require_retry_metadata() -> None:
    metadata_key = executor_mod.SETUP_DEPENDENCY_NETWORK_METADATA_KEY

    assert (
        executor_mod._setup_dependency_network_details(  # noqa: SLF001
            SimpleNamespace(metadata=None)
        )
        is None
    )
    assert (
        executor_mod._setup_dependency_network_details(  # noqa: SLF001
            SimpleNamespace(metadata={metadata_key: "not-a-dict"})
        )
        is None
    )
    assert (
        executor_mod._setup_dependency_network_failure_details(  # noqa: SLF001
            SimpleNamespace(
                reason_code="OTHER_FAILURE",
                metadata={metadata_key: {"host": "files.pythonhosted.org"}},
            )
        )
        is None
    )

    details = executor_mod._setup_dependency_network_failure_details(  # noqa: SLF001
        SimpleNamespace(
            reason_code=executor_mod.SETUP_DEPENDENCY_NETWORK_FAILURE,
            metadata={
                metadata_key: {
                    "host": "files.pythonhosted.org",
                    "package": "docker==7.1.0",
                }
            },
        )
    )

    assert details == {"host": "files.pythonhosted.org", "package": "docker==7.1.0"}


@pytest.mark.unit
def test_validation_evidence_helpers_cover_compaction_value_shapes() -> None:
    assert json.loads(executor_mod._validation_evidence_json({"status": "ok"})) == {"status": "ok"}
    assert executor_mod._validation_evidence_coverage_summary(None) == {  # noqa: SLF001
        "truncated": True,
        "original_type": "NoneType",
    }
    assert executor_mod._validation_evidence_floor_value("short") == "short"  # noqa: SLF001
    assert executor_mod._validation_evidence_floor_value(3) == 3  # noqa: SLF001
    assert executor_mod._validation_evidence_floor_value(True) is True  # noqa: SLF001

    long_summary = executor_mod._validation_evidence_floor_value("x" * 513)  # noqa: SLF001
    assert long_summary == {
        "truncated": True,
        "original_type": "string",
        "original_length": 513,
    }
    assert executor_mod._validation_evidence_size_summary({"a": 1, "b": 2}) == {  # noqa: SLF001
        "truncated": True,
        "original_type": "mapping",
        "original_entry_count": 2,
        "retained_keys": ["a", "b"],
    }
    assert executor_mod._validation_evidence_size_summary([1, 2, 3]) == {  # noqa: SLF001
        "truncated": True,
        "original_type": "list",
        "original_length": 3,
    }


@pytest.mark.unit
def test_requested_tier_metadata_and_adopted_remote_helpers_handle_invalid_shapes() -> None:
    assert executor_mod._requested_tier_from_metadata(None) is None  # noqa: SLF001
    assert executor_mod._requested_tier_from_metadata({"requested_tier": 0}) is None  # noqa: SLF001
    assert (  # noqa: SLF001
        executor_mod._requested_tier_from_metadata({"validation": {"requested_tier": 2}}) == 2
    )
    assert executor_mod._requested_tier_from_metadata({"validation": []}) is None  # noqa: SLF001
    assert (
        executor_mod._existing_pr_remote_push_url(  # noqa: SLF001
            SimpleNamespace(
                task_kind="sync_feature_pr",
                repo_url="https://git.example.invalid/org/repo",
            )
        )
        is None
    )


@pytest.mark.unit
def test_post_validation_conformance_failure_text_includes_structured_report_details() -> None:
    message = executor_mod._post_validation_conformance_failure_text(  # noqa: SLF001
        executor_mod._PlanningRunFailure(
            message="post-validation conformance failed",
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
            details={
                "conformance": {
                    "summary": "docs still missing",
                    "report_reason_code": "PLAN_CONFORMANCE_UNSATISFIED",
                    "gaps": [" add docs ", "", "add validation evidence"],
                }
            },
        )
    )

    assert message.splitlines() == [
        "post-validation conformance failed",
        "Summary: docs still missing",
        "Report reason code: PLAN_CONFORMANCE_UNSATISFIED",
        "Remaining conformance gaps:",
        "- add docs",
        "- add validation evidence",
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("conformance_fields", "payload_fields", "expected_iteration", "expected_max_iterations"),
    [
        ({"iteration": 2, "max_iterations": 5}, {}, 2, 5),
        ({}, {"iteration": 1, "max_iterations": 0}, 1, 0),
        ({}, {}, 0, 3),
    ],
)
def test_recovery_conformance_handoff_preserves_iteration_budget(
    conformance_fields: dict[str, object],
    payload_fields: dict[str, object],
    expected_iteration: int,
    expected_max_iterations: int,
) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planned",
            "planning": {
                "required": True,
                "max_iterations": 3,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
            },
        }
    )
    handoff = _planning_validation_handoff_from_recovery_payload(
        workspace_id="ws123",
        profile=profile,
        recovery_payload={
            **payload_fields,
            "conformance": {
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "summary": "AWF validation evidence is required.",
                "gaps": ["rerun pytest under AWF"],
                **conformance_fields,
            },
        },
    )

    assert handoff is not None
    assert handoff.iteration == expected_iteration
    assert handoff.max_iterations == expected_max_iterations


@pytest.mark.unit
def test_recovery_conformance_handoff_reads_persisted_report_reason_code() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planned",
            "planning": {
                "required": True,
                "max_iterations": 3,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
            },
        }
    )
    handoff = _planning_validation_handoff_from_recovery_payload(
        workspace_id="ws123",
        profile=profile,
        recovery_payload={
            "conformance": {
                "report_reason_code": " conformance-requires-awf-validation ",
                "summary": "AWF validation evidence is required.",
                "gaps": ["rerun pytest under AWF"],
            },
        },
    )

    assert handoff is not None
    assert handoff.report.reason_code == CONFORMANCE_REQUIRES_AWF_VALIDATION


@pytest.mark.unit
@pytest.mark.parametrize("invalid_path_field", ["plan_path", "report_path"])
def test_recovery_conformance_handoff_falls_back_when_payload_path_escapes_workspace(
    invalid_path_field: str,
) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planned",
            "planning": {
                "required": True,
                "max_iterations": 3,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
            },
        }
    )
    conformance = {
        "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
        "summary": "AWF validation evidence is required.",
        "gaps": ["rerun pytest under AWF"],
        "plan_path": "docs/custom-plan.md",
        "report_path": "docs/custom-report.json",
        invalid_path_field: "../outside.json",
    }

    handoff = _planning_validation_handoff_from_recovery_payload(
        workspace_id="ws123",
        profile=profile,
        recovery_payload={"conformance": conformance},
    )

    assert handoff is not None
    assert handoff.plan_path == Path("docs/awf-plans/ws123.md")
    assert handoff.report_path == Path("docs/awf-plans/ws123.conformance.json")


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


@pytest.mark.unit
async def test_post_validation_report_repairs_git_ownership_before_add(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    runner.queue_result(returncode=0)  # git add report
    runner.queue_result(returncode=0, stdout=f"{report_path.as_posix()}\n")
    runner.queue_result(returncode=0)  # git commit report
    executor = _executor_with_runner(runner, tmp_path)
    repair_events: list[tuple[str, int]] = []

    async def record_repair(**kwargs: object) -> bool:
        reason = kwargs["reason"]
        assert isinstance(reason, str)
        repair_events.append((reason, len(runner.calls)))
        return True

    executor._repair_agent_git_ownership = record_repair  # type: ignore[method-assign]

    committed = await executor._commit_post_validation_conformance_report(
        workspace_id="ws_post",
        worktree_path=tmp_path / "worktree",
        report_path=report_path,
        validation_run_id="validation-run-1",
    )

    add_call_index = next(
        index
        for index, call in enumerate(runner.calls)
        if call.args[-3:] == ["add", "--", report_path.as_posix()]
    )
    assert committed is True
    assert repair_events[0] == (
        "post_validation_conformance_report_git_add",
        add_call_index,
    )


@pytest.mark.unit
async def test_post_validation_report_unstages_report_when_cached_diff_fails(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    runner.queue_result(returncode=0)  # git add report
    runner.queue_result(returncode=128, stderr="fatal: index.lock exists")
    runner.queue_result(returncode=0)  # git reset report
    executor = _executor_with_runner(runner, tmp_path)
    executor._repair_agent_git_ownership = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with pytest.raises(executor_mod._PostValidationConformanceReportGitError) as exc_info:
        await executor._commit_post_validation_conformance_report(
            workspace_id="ws_post",
            worktree_path=tmp_path / "worktree",
            report_path=report_path,
            validation_run_id="validation-run-1",
        )

    assert exc_info.value.operation == "diff"
    assert runner.calls[-1].args[-4:] == [
        "reset",
        "-q",
        "--",
        report_path.as_posix(),
    ]


@pytest.mark.unit
async def test_post_validation_report_git_error_preserves_unstage_failure_metadata(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    runner.queue_result(returncode=0)  # git add report
    runner.queue_result(
        returncode=128,
        stderr="fatal: index.lock exists",
        reason_code="GIT_DIFF_FAILED",
    )
    runner.queue_result(
        returncode=129,
        stderr="fatal: could not reset",
        reason_code="GIT_RESET_FAILED",
    )
    executor = _executor_with_runner(runner, tmp_path)
    executor._repair_agent_git_ownership = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with pytest.raises(executor_mod._PostValidationConformanceReportGitError) as exc_info:
        await executor._commit_post_validation_conformance_report(
            workspace_id="ws_post",
            worktree_path=tmp_path / "worktree",
            report_path=report_path,
            validation_run_id="validation-run-1",
        )

    assert exc_info.value.operation == "diff"
    assert exc_info.value.command_reason_code == "GIT_DIFF_FAILED"
    assert exc_info.value.cleanup_operation == "reset"
    assert exc_info.value.cleanup_returncode == 129
    assert exc_info.value.cleanup_command_reason_code == "GIT_RESET_FAILED"
    assert "git reset failed" in str(exc_info.value)
    assert runner.calls[-1].args[-4:] == [
        "reset",
        "-q",
        "--",
        report_path.as_posix(),
    ]


@pytest.mark.unit
async def test_post_validation_report_skips_commit_when_report_is_not_staged(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    runner.queue_result(returncode=0)  # git add report
    runner.queue_result(returncode=0, stdout="docs/awf-plans/other.conformance.json\n")
    executor = _executor_with_runner(runner, tmp_path)
    executor._repair_agent_git_ownership = AsyncMock(return_value=True)  # type: ignore[method-assign]

    committed = await executor._commit_post_validation_conformance_report(
        workspace_id="ws_post",
        worktree_path=tmp_path / "worktree",
        report_path=report_path,
        validation_run_id="validation-run-1",
    )

    assert committed is False
    assert all("commit" not in call.args for call in runner.calls)


@pytest.mark.unit
def test_validation_evidence_json_enforces_limit_on_minimal_fallback() -> None:
    oversized_percent = {f"pkg_{index}": "x" * 1000 for index in range(100)}
    payload = {
        "validation_run_id": "validation-run-1",
        "status": "failed",
        "reason_code": "COVERAGE_BELOW_THRESHOLD",
        "coverage": {
            "status": "failed",
            "reason_code": "COVERAGE_BELOW_THRESHOLD",
            "percent": oversized_percent,
            "minimum_percent": 99,
            "enforce": True,
            "provider": "python",
        },
        "workspace_head_sha": "validated-head",
        "target_branch": "main",
        "commands": [{"command": "pytest", "stdout": "x" * 100000}],
        "log_stream_refs": {"stdout": "x" * 100000},
        "raw_output": "x" * 100000,
    }

    evidence = executor_mod._validation_evidence_json(payload)

    assert len(evidence) <= executor_mod._VALIDATION_EVIDENCE_JSON_LIMIT
    decoded = json.loads(evidence)
    assert decoded["evidence_truncated"] is True
    assert decoded["coverage"]["truncated"] is True
    assert "percent" not in decoded["coverage"]
    assert decoded["oversized_serialized_length"] == len(
        json.dumps(executor_mod.redact_audit_value(payload), default=str)
    )
    assert decoded["oversized_serialized_length"] > len(evidence)


@pytest.mark.unit
def test_validation_evidence_json_returns_after_coverage_compaction() -> None:
    payload = {
        "validation_run_id": "validation-run-1",
        "status": "failed",
        "reason_code": "COVERAGE_BELOW_THRESHOLD",
        "coverage": {
            "status": "failed",
            "packages": {f"pkg_{index}": "x" * 1000 for index in range(100)},
        },
        "commands": [{"command": "pytest", "stdout": "x" * 120000}],
        "log_stream_refs": {"stdout": "x" * 120000},
    }

    evidence = executor_mod._validation_evidence_json(payload)

    decoded = json.loads(evidence)
    assert decoded["evidence_truncated"] is True
    assert decoded["coverage"]["status"] == "failed"
    assert decoded["coverage"]["retained_keys"] == ["status", "packages"]
    assert decoded["commands"]["original_type"] == "list"
    assert len(evidence) <= executor_mod._VALIDATION_EVIDENCE_JSON_LIMIT


@pytest.mark.unit
def test_validation_evidence_json_returns_minimal_payload_when_compact_payload_is_large() -> None:
    payload = {
        "validation_run_id": "validation-run-1",
        "status": "failed",
        "reason_code": "VALIDATION_FAILED",
        "commands": [{"command": "pytest", "stdout": "x" * 120000}],
        "log_stream_refs": {"stdout": "x" * 120000},
        **{f"extra_{index}": "x" * 50 for index in range(1000)},
    }

    evidence = executor_mod._validation_evidence_json(payload)

    decoded = json.loads(evidence)
    assert decoded == {
        "validation_run_id": "validation-run-1",
        "status": "failed",
        "reason_code": "VALIDATION_FAILED",
        "coverage": {
            "truncated": True,
            "original_type": "NoneType",
        },
        "evidence_truncated": True,
        "commands": {
            "truncated": True,
            "original_type": "list",
            "original_length": 1,
        },
        "log_stream_refs": {
            "truncated": True,
            "original_type": "mapping",
            "original_entry_count": 1,
            "retained_keys": ["stdout"],
        },
    }


@pytest.mark.unit
def test_validation_evidence_json_has_final_floor_when_limit_is_tiny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(executor_mod, "_VALIDATION_EVIDENCE_JSON_LIMIT", 10)

    evidence = executor_mod._validation_evidence_json(
        {
            "validation_run_id": "validation-run-1",
            "status": "failed",
            "reason_code": "VALIDATION_FAILED",
            "commands": ["pytest"],
            "log_stream_refs": {"stdout": "ref"},
            "target_branch": "main",
        }
    )

    assert evidence.endswith("...[truncated]")


@pytest.mark.unit
def test_validation_evidence_floor_payload_special_cases_coverage_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coverage = {
        "status": "failed",
        "reason_code": "COVERAGE_BELOW_THRESHOLD",
        "percent": {f"pkg_{index}": "x" * 1000 for index in range(10)},
    }
    payload = {
        "validation_run_id": "validation-run-1",
        "status": "failed",
        "coverage": coverage,
    }
    floor_values: list[object] = []
    original_floor_value = executor_mod._validation_evidence_floor_value

    def record_floor_value(value: object) -> object:
        floor_values.append(value)
        return original_floor_value(value)

    monkeypatch.setattr(
        executor_mod,
        "_validation_evidence_floor_value",
        record_floor_value,
    )

    floor_payload = executor_mod._validation_evidence_floor_payload(
        payload,
        oversized_serialized_length=123456,
    )

    assert coverage not in floor_values
    assert floor_payload["coverage"] == executor_mod._validation_evidence_size_summary(coverage)


@pytest.mark.unit
def test_validation_evidence_floor_payload_handles_payload_without_coverage() -> None:
    floor_payload = executor_mod._validation_evidence_floor_payload(  # noqa: SLF001
        {
            "validation_run_id": "validation-run-1",
            "status": "failed",
            "commands": ["pytest"],
            "log_stream_refs": {"stdout": "ref"},
        },
        oversized_serialized_length=1234,
    )

    assert "coverage" not in floor_payload
    assert floor_payload["validation_run_id"] == "validation-run-1"
    assert floor_payload["commands"]["original_type"] == "list"


@pytest.mark.unit
def test_validation_evidence_summary_helpers_cover_scalar_and_oversized_values() -> None:
    assert executor_mod._validation_evidence_coverage_summary("raw coverage") == {  # noqa: SLF001
        "truncated": True,
        "original_type": "string",
        "original_length": len("raw coverage"),
    }
    assert executor_mod._validation_evidence_coverage_summary({"other": 1}) == {  # noqa: SLF001
        "truncated": True,
        "original_type": "mapping",
        "original_entry_count": 1,
        "retained_keys": ["other"],
    }
    assert executor_mod._validation_evidence_floor_value("short") == "short"  # noqa: SLF001
    assert executor_mod._validation_evidence_floor_value(3) == 3  # noqa: SLF001
    assert executor_mod._validation_evidence_floor_value(None) is None  # noqa: SLF001
    assert executor_mod._validation_evidence_floor_value("x" * 600) == {  # noqa: SLF001
        "truncated": True,
        "original_type": "string",
        "original_length": 600,
    }
    assert executor_mod._validation_evidence_floor_value(("tuple",)) == {  # noqa: SLF001
        "truncated": True,
        "original_type": "tuple",
    }


@pytest.mark.unit
def test_validation_evidence_serializer_uses_evidence_limit_for_redaction_expansion() -> None:
    payload = {"output": " ".join(["SECRET=a"] * 2166)}
    raw_length = len(json.dumps(payload, default=str))
    assert raw_length < executor_mod._VALIDATION_EVIDENCE_JSON_LIMIT

    evidence = executor_mod._serialize_validation_evidence_payload(payload)

    assert len(evidence) == executor_mod._VALIDATION_EVIDENCE_JSON_LIMIT + len("...[truncated]")
    assert len(evidence) < raw_length + 4096
    assert "[redacted]" in evidence
    assert "SECRET=a" not in evidence
    assert evidence.endswith("...[truncated]")


@pytest.mark.unit
def test_post_validation_conformance_fix_result_preserves_attempt_artifacts(
    tmp_path: Path,
) -> None:
    first = executor_mod._post_validation_conformance_fix_result(
        failure=executor_mod._PlanningRunFailure(
            message="first conformance gap",
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
        ),
        workspace_id="ws_post",
        artifacts_root=tmp_path,
        attempt=1,
    )
    second = executor_mod._post_validation_conformance_fix_result(
        failure=executor_mod._PlanningRunFailure(
            message="second conformance gap",
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
        ),
        workspace_id="ws_post",
        artifacts_root=tmp_path,
        attempt=2,
    )

    first_command = first.commands[0]
    second_command = second.commands[0]
    assert first_command.stdout_path.name == "post_validation_conformance.1.stdout"
    assert first_command.stderr_path.name == "post_validation_conformance.1.stderr"
    assert second_command.stdout_path.name == "post_validation_conformance.2.stdout"
    assert second_command.stderr_path.name == "post_validation_conformance.2.stderr"
    assert first_command.stdout_path.read_text(encoding="utf-8") == "first conformance gap"
    assert second_command.stdout_path.read_text(encoding="utf-8") == "second conformance gap"
    assert not (
        tmp_path / "ws_post" / "post_validation_conformance" / "post_validation_conformance.stdout"
    ).exists()


@pytest.mark.unit
def test_post_validation_conformance_failure_text_renders_conformance_details() -> None:
    text = executor_mod._post_validation_conformance_failure_text(  # noqa: SLF001
        executor_mod._PlanningRunFailure(  # noqa: SLF001
            message="Plan conformance still requires validation evidence.",
            details={
                "conformance": {
                    "summary": "AWF validation evidence is missing.",
                    "report_reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                    "gaps": ["rerun coverage", "", 42],
                }
            },
        )
    )

    assert text == "\n".join(
        [
            "Plan conformance still requires validation evidence.",
            "Summary: AWF validation evidence is missing.",
            f"Report reason code: {CONFORMANCE_REQUIRES_AWF_VALIDATION}",
            "Remaining conformance gaps:",
            "- rerun coverage",
            "- 42",
        ]
    )


@pytest.mark.unit
def test_existing_pr_remote_push_url_ignores_non_sync_or_invalid_repo_urls() -> None:
    assert (
        executor_mod._existing_pr_remote_push_url(  # noqa: SLF001
            SimpleNamespace(task_kind="feature_branch_pr", repo_url="not a url")
        )
        is None
    )
    assert (
        executor_mod._existing_pr_remote_push_url(  # noqa: SLF001
            SimpleNamespace(task_kind="sync_feature_pr", repo_url="not a url")
        )
        is None
    )


@pytest.mark.unit
async def test_satisfied_post_validation_conformance_report_is_committed(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    worktree_path = tmp_path / "worktree"
    report_file = worktree_path / report_path
    report_file.parent.mkdir(parents=True)
    report_file.write_text(
        '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}',
        encoding="utf-8",
    )
    runner.queue_result(returncode=0, stdout="")  # changed paths before conformance
    runner.queue_result(returncode=0, stdout="validated-head\n")
    runner.queue_result(returncode=0, stdout=f"?? {report_path.as_posix()}\n")
    runner.queue_result(returncode=0, stdout="")  # committed paths since validated HEAD
    runner.queue_result(returncode=0)  # git add report
    runner.queue_result(returncode=0, stdout=f"{report_path.as_posix()}\n")
    runner.queue_result(returncode=0)  # git commit report
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )
    executor._repair_agent_git_ownership = AsyncMock(return_value=True)  # type: ignore[method-assign]
    event_markers: list[tuple[str, int]] = []

    async def record_event(**_kwargs: object) -> None:
        event_markers.append(("record", len(runner.calls)))

    executor._record_post_validation_conformance_event = record_event  # type: ignore[method-assign]
    profile = WorkspaceProfile.model_validate({"name": "planned", "planning": {"required": True}})
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
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
    )

    assert failure is None
    add_index = next(
        index
        for index, call in enumerate(runner.calls)
        if call.args[-3:] == ["add", "--", report_path.as_posix()]
    )
    commit_index = next(index for index, call in enumerate(runner.calls) if "commit" in call.args)
    assert add_index < commit_index
    assert event_markers == [("record", len(runner.calls))]
    assert commit_index < event_markers[0][1]


@pytest.mark.unit
async def test_post_validation_conformance_prefers_stdout_when_report_is_stale(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    worktree_path = tmp_path / "worktree"
    report_file = worktree_path / report_path
    report_file.parent.mkdir(parents=True)
    report_file.write_text(
        (
            '{"status":"needs_iteration","summary":"AWF validation evidence is missing.",'
            f'"reason_code":"{CONFORMANCE_REQUIRES_AWF_VALIDATION}",'
            '"gaps":["Run AWF validation."]}'
        ),
        encoding="utf-8",
    )
    satisfied_stdout = (
        '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}'
    )
    runner.queue_result(returncode=0, stdout=f"?? {report_path.as_posix()}\n")
    runner.queue_result(returncode=0, stdout="validated-head\n")
    runner.queue_result(returncode=0, stdout=f"?? {report_path.as_posix()}\n")
    runner.queue_result(returncode=0, stdout="")
    runner.queue_result(returncode=0)
    runner.queue_result(returncode=0, stdout=f"{report_path.as_posix()}\n")
    runner.queue_result(returncode=0)
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )
    executor._repair_agent_git_ownership = AsyncMock(return_value=True)  # type: ignore[method-assign]
    executor._record_post_validation_conformance_event = AsyncMock()  # type: ignore[method-assign]
    profile = WorkspaceProfile.model_validate({"name": "planned", "planning": {"required": True}})
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
        adapter=_PlanningAdapter(satisfied_stdout),  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
    )

    assert failure is None
    assert "validated evidence satisfies plan" in report_file.read_text(encoding="utf-8")
    executor._record_post_validation_conformance_event.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_post_validation_conformance_ignores_stale_report_without_stdout(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    worktree_path = tmp_path / "worktree"
    report_file = worktree_path / report_path
    report_file.parent.mkdir(parents=True)
    report_file.write_text(
        '{"status":"satisfied","summary":"stale success","gaps":[]}',
        encoding="utf-8",
    )
    runner.queue_result(returncode=0, stdout=f"?? {report_path.as_posix()}\n")
    runner.queue_result(returncode=0, stdout="validated-head\n")
    runner.queue_result(returncode=0, stdout=f"?? {report_path.as_posix()}\n")
    runner.queue_result(returncode=0, stdout="")
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )
    executor._commit_post_validation_conformance_report = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    executor._record_post_validation_conformance_event = AsyncMock()  # type: ignore[method-assign]
    profile = WorkspaceProfile.model_validate({"name": "planned", "planning": {"required": True}})
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
        adapter=_PlanningAdapter(""),  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
    )

    assert failure is not None
    assert failure.reason_code == PLAN_CONFORMANCE_UNSATISFIED
    assert "Produce a valid plan-conformance JSON report." in failure.message
    executor._commit_post_validation_conformance_report.assert_not_awaited()  # type: ignore[attr-defined]
    executor._record_post_validation_conformance_event.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_post_validation_conformance_failure_counts_handoff_iterations(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    runner.queue_result(returncode=0, stdout="")  # changed paths before conformance
    runner.queue_result(returncode=0, stdout="validated-head\n")
    runner.queue_result(returncode=0, stdout="")  # changed paths after conformance
    runner.queue_result(returncode=0, stdout="")  # committed paths since validated HEAD
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )
    profile = WorkspaceProfile.model_validate({"name": "planned", "planning": {"required": True}})
    handoff = _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="AWF validation evidence is missing.",
            gaps=("Run AWF validation.",),
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        ),
        plan_path=Path("docs/awf-plans/ws_post.md"),
        report_path=report_path,
        iteration=1,
        max_iterations=2,
    )

    failure = await executor._run_post_validation_conformance_check(
        adapter=_PlanningAdapter(
            '{"status":"needs_iteration","summary":"docs still missing",'
            '"gaps":["Document the validated endpoint."]}'
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
    assert failure.details is not None
    assert failure.details["conformance"]["iterations_used"] == 3
    assert failure.details["conformance"]["max_iterations"] == 2


@pytest.mark.unit
async def test_post_validation_conformance_rejects_committed_implementation_paths(
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
    profile = WorkspaceProfile.model_validate({"name": "planned", "planning": {"required": True}})
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
    assert failure.message.startswith(
        "post-validation conformance phase changed files outside "
        "`docs/awf-plans/ws_post.conformance.json`"
    )
    assert failure.details is not None
    assert failure.details["planning_scope"]["offending_paths"] == ["src/unvalidated.py"]
    executor._committed_paths_since.assert_awaited_once_with(  # type: ignore[attr-defined]
        tmp_path / "worktree",
        "validated-head",
    )
    executor._record_post_validation_conformance_event.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_post_validation_conformance_rejects_pre_dirty_committed_paths(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(
        returncode=0,
        stdout=" M src/unvalidated.py\n",
    )  # changed paths before conformance
    runner.queue_result(returncode=0, stdout="")  # clean status after conformance
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )
    executor._git_rev_parse_head = AsyncMock(return_value="validated-head")  # type: ignore[method-assign]
    executor._committed_paths_since = AsyncMock(  # type: ignore[method-assign]
        return_value={Path("src/unvalidated.py")}
    )
    executor._commit_post_validation_conformance_report = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    executor._repair_agent_git_ownership = AsyncMock(return_value=True)  # type: ignore[method-assign]
    executor._record_post_validation_conformance_event = AsyncMock()  # type: ignore[method-assign]
    profile = WorkspaceProfile.model_validate({"name": "planned", "planning": {"required": True}})
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
    executor._commit_post_validation_conformance_report.assert_not_awaited()  # type: ignore[attr-defined]
    executor._record_post_validation_conformance_event.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_post_validation_conformance_rejects_edits_to_pre_dirty_paths(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktree"
    dirty_path = worktree_path / "src" / "unvalidated.py"
    dirty_path.parent.mkdir(parents=True)
    dirty_path.write_text("validation dirty content", encoding="utf-8")
    runner = FakeCommandRunner()
    runner.queue_result(
        returncode=0,
        stdout=" M src/unvalidated.py\n",
    )  # changed paths before conformance
    runner.queue_result(
        returncode=0,
        stdout=" M src/unvalidated.py\n",
    )  # same path remains dirty after conformance
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )
    executor._git_rev_parse_head = AsyncMock(return_value="validated-head")  # type: ignore[method-assign]
    executor._committed_paths_since = AsyncMock(return_value=set())  # type: ignore[method-assign]
    executor._commit_post_validation_conformance_report = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    executor._record_post_validation_conformance_event = AsyncMock()  # type: ignore[method-assign]
    profile = WorkspaceProfile.model_validate({"name": "planned", "planning": {"required": True}})
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

    class _SamePathEditingAdapter(_PlanningAdapter):
        async def run(self, **kwargs: object) -> SimpleNamespace:
            dirty_path.write_text("conformance-only edit", encoding="utf-8")
            return await super().run(**kwargs)

    failure = await executor._run_post_validation_conformance_check(
        adapter=_SamePathEditingAdapter(
            '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}'
        ),  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
    )

    assert failure is not None
    assert failure.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert failure.details is not None
    assert failure.details["planning_scope"]["offending_paths"] == ["src/unvalidated.py"]
    executor._commit_post_validation_conformance_report.assert_not_awaited()  # type: ignore[attr-defined]
    executor._record_post_validation_conformance_event.assert_not_awaited()  # type: ignore[attr-defined]


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
) -> executor_mod._PostAgentCommitClassification:  # noqa: SLF001
    return executor_mod._PostAgentCommitClassification(  # noqa: SLF001
        reason_code="POST_AGENT_COMMIT_AUTOFIX_NEEDED",
        failed_hooks=("ruff-check",),
        format_repair_files=(),
        normalizer_repair_files=(),
        autofix_repair_files=repair_files,
        summary="ruff reported fixable diagnostics",
        repair_strategy="deterministic_autofix",
    )


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

    with pytest.raises(executor_mod._PostAgentCommitStepError) as exc_info:
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
async def test_autofixable_precommit_repair_raises_when_restaging_fails(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0)
    executor = _executor_with_runner(runner, tmp_path)
    executor._record_post_agent_commit_format_repair = AsyncMock()  # type: ignore[method-assign]
    executor._repair_agent_git_ownership = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with pytest.raises(executor_mod._PostAgentCommitStepError) as exc_info:
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
async def test_autofixable_precommit_repair_raises_when_retry_commit_still_fails(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0)
    executor = _executor_with_runner(runner, tmp_path)
    executor._record_post_agent_commit_format_repair = AsyncMock()  # type: ignore[method-assign]
    executor._repair_agent_git_ownership = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with pytest.raises(executor_mod._PostAgentCommitStepError) as exc_info:
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
    monkeypatch.setattr(executor_mod, "_should_run_local_coverage", lambda _: True)

    with pytest.raises(RuntimeError, match="coverage.command is None"):
        _validation_run_command_records(
            profile=profile,
            phase_names=("validate",),
            run_healthchecks=False,
        )


@pytest.mark.unit
def test_validation_command_count_includes_database_refresh_hooks_and_coverage() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "count-db-refresh",
            "phases": {
                "post_agent": ["ruff format --check"],
                "validate": ["pytest -q"],
            },
            "database": {"pre_validation_refresh": ["python scripts/db_refresh.py"]},
            "validation": {
                "strategy": {"final_gate": "coverage"},
                "coverage": {"command": "pytest --cov=awf"},
            },
        }
    )

    workspace = SimpleNamespace(
        resolved_profile=profile.model_dump(mode="json", by_alias=True),
        test_commands=[],
    )

    assert _validation_command_count(workspace) == 4


@pytest.mark.unit
def test_validation_command_count_ignores_coverage_without_local_final_gate() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "count-targeted-with-coverage-command",
            "phases": {"validate": ["pytest -q"]},
            "validation": {
                "strategy": {"edit_gate": "targeted", "final_gate": "none"},
                "coverage": {"command": "pytest --cov=awf"},
            },
        }
    )

    workspace = SimpleNamespace(
        resolved_profile=profile.model_dump(mode="json", by_alias=True),
        test_commands=[],
    )

    assert _validation_command_count(workspace) == 1


@pytest.mark.unit
def test_validation_run_command_records_include_http_healthcheck_display() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records-http-healthcheck",
            "phases": {"validate": ["pytest -q"]},
            "validation": {
                "healthchecks": [
                    {
                        "name": "api",
                        "url": "http://api:8080/healthz",
                        "expected_status": 204,
                    }
                ]
            },
        }
    )

    records = _validation_run_command_records(
        profile=profile,
        phase_names=("validate",),
        run_healthchecks=True,
    )

    assert records[0] == {
        "phase": "healthcheck",
        "command_index": 1,
        "command": "GET http://api:8080/healthz expected 204",
        "healthcheck_name": "api",
        "healthcheck_kind": "http",
        "target": "http://api:8080/healthz",
        "stream_ids": {
            "stdout": "validation.01_healthcheck.stdout",
            "stderr": "validation.01_healthcheck.stderr",
        },
    }


@pytest.mark.unit
def test_validation_run_command_records_include_alembic_policy_before_healthchecks() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records-alembic-policy",
            "phases": {"validate": ["pytest -q"]},
            "validation": {
                "alembic": {"enabled": True},
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
        ("migration_policy", 1),
        ("healthcheck", 1),
        ("validate", 1),
    ]
    assert records[0]["command"] == "awf validate alembic migration chain"
    assert records[0]["stream_ids"] == {
        "stdout": "validation.01_migration_policy.stdout",
        "stderr": "validation.01_migration_policy.stderr",
    }


@pytest.mark.unit
def test_validation_run_command_records_can_skip_healthchecks_and_coverage() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records-no-healthchecks",
            "phases": {
                "setup": ["uv sync"],
                "validate": ["pytest -q"],
            },
            "validation": {
                "healthchecks": [{"name": "api", "command": "curl -fsS localhost/health"}],
                "coverage": {"command": "pytest --cov=awf --cov-report=term"},
            },
        }
    )

    records = _validation_run_command_records(
        profile=profile,
        phase_names=("setup",),
        run_healthchecks=False,
    )

    assert [(record["phase"], record["command"]) for record in records] == [("setup", "uv sync")]


@pytest.mark.unit
def test_validation_tier_for_workspace_uses_task_class_floor() -> None:
    profile = WorkspaceProfile.model_validate({"name": "tier", "validation": {"requested_tier": 1}})

    assert (
        _validation_tier_for_workspace(
            SimpleNamespace(task_class=TaskClass.migration_task.value),  # type: ignore[arg-type]
            profile,
        )
        == 3
    )
    assert (
        _validation_tier_for_workspace(
            SimpleNamespace(task_class=TaskClass.refactor_task.value),  # type: ignore[arg-type]
            profile,
        )
        == 2
    )
    assert (
        _validation_tier_for_workspace(
            SimpleNamespace(task_class=None),  # type: ignore[arg-type]
            profile,
        )
        == 1
    )


@pytest.mark.unit
def test_validation_tier_for_workspace_uses_successful_validate_operation_tier() -> None:
    profile = WorkspaceProfile.model_validate({"name": "tier", "validation": {"requested_tier": 1}})
    workspace = SimpleNamespace(
        task_class=None,
        operations=[
            SimpleNamespace(
                type=OperationType.validate.value,
                status=OperationStatus.failed.value,
                payload={"requested_tier": 3},
                result={"requested_tier": 3},
            ),
            SimpleNamespace(
                type=OperationType.refresh.value,
                status=OperationStatus.succeeded.value,
                payload={"requested_tier": 3},
                result={"requested_tier": 3},
            ),
            SimpleNamespace(
                type=OperationType.validate.value,
                status=OperationStatus.succeeded.value,
                payload={"requested_tier": "3"},
                result={"requested_tier": "3"},
            ),
            SimpleNamespace(
                type=OperationType.validate.value,
                status=OperationStatus.succeeded.value,
                payload={"requested_tier": 2},
                result={"validation": {"requested_tier": 3}},
            ),
        ],
    )

    assert _validation_tier_for_workspace(workspace, profile) == 3  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize(
    "active_status",
    [OperationStatus.pending.value, OperationStatus.running.value],
)
def test_validation_tier_for_workspace_uses_active_validate_operation_payload_tier(
    active_status: str,
) -> None:
    profile = WorkspaceProfile.model_validate({"name": "tier", "validation": {"requested_tier": 1}})
    workspace = SimpleNamespace(
        task_class=None,
        operations=[
            SimpleNamespace(
                type=OperationType.validate.value,
                status=OperationStatus.failed.value,
                payload={"requested_tier": 3},
                result={"requested_tier": 3},
            ),
            SimpleNamespace(
                type=OperationType.validate.value,
                status=OperationStatus.cancelled.value,
                payload={"requested_tier": 3},
                result={"requested_tier": 3},
            ),
            SimpleNamespace(
                type=OperationType.refresh.value,
                status=active_status,
                payload={"requested_tier": 3},
            ),
            SimpleNamespace(
                type=OperationType.validate.value,
                status=active_status,
                payload={"requested_tier": 3},
            ),
        ],
    )

    assert _validation_tier_for_workspace(workspace, profile) == 3  # type: ignore[arg-type]


@pytest.mark.unit
async def test_baseline_coverage_preflight_returns_logged_policy_result(
    tmp_path: Path,
) -> None:
    baseline = _coverage(tmp_path, percent=88, minimum=99)
    validation = _CoverageValidation(baseline)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path, validation=validation)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "coverage-preflight",
            "validation": {
                "coverage": {
                    "minimum_percent": 99,
                    "enforce": True,
                    "command": "pytest --cov=awf",
                }
            },
        }
    )

    result = await executor._run_baseline_coverage_preflight(
        workspace_id="ws_preflight",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
    )

    assert result is baseline
    assert validation.calls == ["baseline_coverage"]


@pytest.mark.unit
async def test_baseline_coverage_preflight_returns_passing_policy_result(
    tmp_path: Path,
) -> None:
    baseline = _coverage(tmp_path, percent=100, minimum=99, status="passed")

    validation = _CoverageValidation(baseline)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path, validation=validation)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "coverage-preflight-passing",
            "validation": {
                "coverage": {
                    "minimum_percent": 99,
                    "enforce": True,
                    "command": "pytest --cov=awf",
                }
            },
        }
    )

    result = await executor._run_baseline_coverage_preflight(
        workspace_id="ws_preflight_ok",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
    )

    assert result is baseline
    assert validation.calls == ["baseline_coverage"]


@pytest.mark.unit
async def test_baseline_coverage_preflight_returns_successful_result(tmp_path: Path) -> None:
    baseline = _coverage(
        tmp_path,
        percent=99,
        minimum=99,
        status="passed",
        reason_code="COVERAGE_OK",
    )
    validation = _CoverageValidation(baseline)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path, validation=validation)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "coverage-preflight-success",
            "validation": {
                "coverage": {
                    "minimum_percent": 99,
                    "enforce": True,
                    "command": "pytest --cov=awf",
                }
            },
        }
    )

    result = await executor._run_baseline_coverage_preflight(
        workspace_id="ws_preflight_success",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
    )

    assert result is baseline
    assert validation.calls == ["baseline_coverage"]


@pytest.mark.unit
async def test_baseline_coverage_preflight_skips_when_strategy_disables_it(
    tmp_path: Path,
) -> None:
    baseline = _coverage(tmp_path, percent=99, minimum=99, status="passed")
    validation = _CoverageValidation(baseline)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path, validation=validation)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "coverage-preflight-skip",
            "validation": {
                "strategy": {"baseline_coverage": "skip"},
                "coverage": {
                    "minimum_percent": 99,
                    "enforce": True,
                    "command": "pytest --cov=awf",
                },
            },
        }
    )

    result = await executor._run_baseline_coverage_preflight(
        workspace_id="ws_preflight_skip",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
    )

    assert result is None
    assert validation.calls == []


@pytest.mark.unit
async def test_final_coverage_gate_skips_when_coverage_command_is_absent(
    tmp_path: Path,
) -> None:
    validation = _CoverageValidation(_coverage(tmp_path, percent=100, status="passed"))
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path, validation=validation)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "final-gate-no-command",
            "validation": {"strategy": {"final_gate": "coverage"}},
        }
    )

    result = await executor._run_final_coverage_gate(
        workspace_id="ws_no_coverage_command",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
        validation_tier=1,
        workspace_head_sha="head",
    )

    assert result.coverage is None
    assert validation.calls == []


@pytest.mark.unit
async def test_final_coverage_gate_reuses_exact_fresh_evidence(
    tmp_path: Path,
) -> None:
    engine = await create_postgres_test_engine()
    factory = make_session_factory(engine)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "final-gate",
            "validation": {
                "strategy": {
                    "final_gate": "coverage",
                    "reuse_evidence": True,
                    "freshness_max_age_seconds": 3600,
                },
                "coverage": {
                    "minimum_percent": 99,
                    "command": "pytest --cov=awf",
                },
            },
        }
    )
    commands = _validation_run_command_records(
        profile=profile,
        phase_names=("post_agent", "validate"),
        run_healthchecks=True,
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/awf.git",
            branch_base="main",
            task_title="reuse final coverage",
            task_prompt="reuse final coverage",
            agent="codex",
            test_commands=[],
        )
        run = await ValidationRunRepository(session).start(
            workspace_id=workspace.id,
            attempt_id=None,
            tier=1,
            commands=commands,
            base_commit="base",
            target_branch="main",
            target_head_sha=None,
            workspace_head_sha="head",
            resolved_profile_digest=resolved_profile_digest(profile),
            environment_identity_digest=environment_identity_digest(profile),
            log_stream_refs={},
        )
        await ValidationRunRepository(session).finish(
            run.id,
            status="succeeded",
            reason_code="VALIDATION_OK",
            coverage={"status": "passed", "reason_code": "COVERAGE_OK", "percent": 99.5},
        )
        await session.commit()
        workspace_id = workspace.id
        source_run_id = run.id

    validation = _CoverageValidation(_coverage(tmp_path, percent=100, status="passed"))
    executor = WorkspaceExecutor(
        session_factory=factory,
        runner=FakeCommandRunner(),
        compose=object(),  # type: ignore[arg-type]
        validation=validation,  # type: ignore[arg-type]
        pr_creator=object(),  # type: ignore[arg-type]
        config=ExecutorConfig(
            worktrees_root=tmp_path / "worktrees",
            compose_projects_root=tmp_path / "compose",
        ),
    )

    result = await executor._run_final_coverage_gate(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
        validation_tier=1,
        workspace_head_sha="head",
    )

    assert result.coverage is not None
    assert result.coverage.percent == 99.5
    assert result.evidence_status == "reused"
    assert result.source_run_id == source_run_id
    assert validation.calls == []
    await engine.dispose()


@pytest.mark.unit
async def test_final_coverage_gate_caps_parallel_workers_to_active_reservation(
    tmp_path: Path,
) -> None:
    engine = await create_postgres_test_engine()
    try:
        factory = make_session_factory(engine)
        profile = WorkspaceProfile.model_validate(
            {
                "name": "final-gate-parallel",
                "validation": {
                    "strategy": {"final_gate": "coverage"},
                    "coverage": {
                        "minimum_percent": 99,
                        "command": "pytest --cov=awf",
                        "parallel_workers": 20,
                    },
                },
            }
        )
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/awf.git",
                branch_base="main",
                task_title="parallel final coverage",
                task_prompt="parallel final coverage",
                agent="codex",
                test_commands=[],
            )
            task = await TaskRepository(session).create_or_get(
                repo_url=workspace.repo_url,
                base_branch=workspace.branch_base,
                title=workspace.task_title,
                prompt=workspace.task_prompt,
                external_id=None,
                idempotency_key=None,
                task_class=None,
                owned_paths=[],
            )
            attempt = await TaskAttemptRepository(session).create_for_workspace(
                task=task,
                workspace=workspace,
            )
            await ResourceReservationRepository(session).create(
                workspace_id=workspace.id,
                attempt_id=attempt.id,
                node_id="local",
                steady_cpu=3.0,
                steady_memory_gb=10.0,
                peak_cpu=6.0,
                peak_memory_gb=16.0,
                disk_mb=None,
                phase="execution",
            )
            await session.commit()
            workspace_id = workspace.id

        coverage = _coverage(tmp_path, percent=100, status="passed", reason_code="COVERAGE_OK")
        validation = _CoverageValidation(coverage)
        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=FakeCommandRunner(),
            compose=object(),  # type: ignore[arg-type]
            validation=validation,  # type: ignore[arg-type]
            pr_creator=object(),  # type: ignore[arg-type]
            config=ExecutorConfig(
                worktrees_root=tmp_path / "worktrees",
                compose_projects_root=tmp_path / "compose",
            ),
        )

        result = await executor._run_final_coverage_gate(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            profile=profile,
            validation_tier=1,
            workspace_head_sha="head",
        )

        assert result.coverage is coverage
        assert validation.calls == ["coverage"]
        assert validation.kwargs[0]["parallel_worker_cpu_limit"] == 3
    finally:
        await engine.dispose()


@pytest.mark.unit
async def test_validation_run_evidence_for_conformance_reports_missing_run(
    tmp_path: Path,
) -> None:
    engine = await create_postgres_test_engine()
    try:
        executor = WorkspaceExecutor(
            session_factory=make_session_factory(engine),
            runner=FakeCommandRunner(),
            compose=object(),  # type: ignore[arg-type]
            validation=object(),  # type: ignore[arg-type]
            pr_creator=object(),  # type: ignore[arg-type]
            config=ExecutorConfig(
                worktrees_root=tmp_path / "worktrees",
                compose_projects_root=tmp_path / "compose",
            ),
        )

        evidence = await executor._validation_run_evidence_for_conformance("missing-run")

        assert "AWF persisted validation run evidence" in evidence
        assert '"status": "missing"' in evidence
        assert '"reason_code": "VALIDATION_RUN_NOT_FOUND"' in evidence
    finally:
        await engine.dispose()


@pytest.mark.unit
async def test_auto_retry_planning_scope_failure_ignores_other_reason_codes(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)

    await executor._auto_retry_planning_scope_failure(
        workspace_id="ws_plan",
        failure=executor_mod._PlanningRunFailure(
            message="ordinary conformance failure",
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
        ),
    )


@pytest.mark.unit
async def test_git_commit_count_since_handles_failed_and_invalid_output(
    tmp_path: Path,
) -> None:
    failed_runner = FakeCommandRunner()
    failed_runner.queue_result(returncode=1, stderr="bad revision")
    failed_executor = _executor_with_runner(failed_runner, tmp_path)
    assert await failed_executor._git_commit_count_since(tmp_path / "worktree", "base") == 0

    invalid_runner = FakeCommandRunner()
    invalid_runner.queue_result(returncode=0, stdout="not-an-int\n")
    invalid_executor = _executor_with_runner(invalid_runner, tmp_path)
    assert await invalid_executor._git_commit_count_since(tmp_path / "worktree", "base") == 0


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

    monkeypatch.setattr(executor_mod, "_digest_file_if_present", _digest)
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

    assert executor_mod._digest_file_if_present(path) == hashlib.sha256(payload).hexdigest()

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


@pytest.mark.unit
def test_git_error_indicates_missing_head_object() -> None:
    assert _git_error_indicates_missing_head_object("fatal: bad object HEAD\n")
    assert _git_error_indicates_missing_head_object("fatal: not a valid object name HEAD\n")
    assert not _git_error_indicates_missing_head_object("fatal: not a git repository\n")


@pytest.mark.unit
def test_agent_git_writability_preflight_script_exercises_object_and_ref_writes() -> None:
    script = _agent_git_writability_preflight_script("ws_preflight")

    assert "git status --porcelain" in script
    assert "git hash-object -w --stdin" in script
    assert 'git cat-file -e "$blob^{blob}"' in script
    assert 'git update-ref "$ref" HEAD' in script
    assert 'git update-ref -d "$ref"' in script


@pytest.mark.unit
async def test_agent_git_writability_preflight_runs_inside_agent_container(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    executor = _executor_with_runner(runner, tmp_path)
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    (worktree_path / ".git").write_text("gitdir: /tmp/mirror/worktrees/ws_preflight\n")
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    ok = await executor._run_agent_git_writability_preflight(
        workspace_id="ws_preflight",
        compose_project="awf_ws_preflight",
        compose_file=compose_file,
        worktree_path=worktree_path,
    )

    assert ok is True
    assert runner.calls
    call = runner.calls[0]
    assert call.input_bytes == b""
    assert call.args[:2] == ["docker", "compose"]
    assert call.args[call.args.index("-p") + 1] == "awf_ws_preflight"
    assert call.args[call.args.index("-f") + 1] == str(compose_file)
    assert "agent_git_writability_preflight" not in " ".join(call.args[:10])
    assert "git hash-object -w --stdin" in " ".join(call.args)


@pytest.mark.unit
async def test_agent_git_writability_preflight_skips_non_provisioned_fakes(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    executor = _executor_with_runner(runner, tmp_path)
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    assert await executor._run_agent_git_writability_preflight(
        workspace_id="ws_no_git",
        compose_project="awf_ws_no_git",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
    )

    (worktree_path / ".git").write_text("gitdir: /tmp/mirror/worktrees/ws_no_git\n")
    assert await executor._run_agent_git_writability_preflight(
        workspace_id="ws_no_compose",
        compose_project="awf_ws_no_compose",
        compose_file=tmp_path / "missing-compose.yml",
        worktree_path=worktree_path,
    )
    assert runner.calls == []


@pytest.mark.unit
async def test_agent_git_writability_preflight_fails_when_repair_fails(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    executor = _executor_with_runner(runner, tmp_path)
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    (worktree_path / ".git").write_text("gitdir: /tmp/mirror/worktrees/ws_repair_fail\n")
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    executor._repair_agent_git_ownership = AsyncMock(return_value=False)  # type: ignore[method-assign]
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]

    ok = await executor._run_agent_git_writability_preflight(
        workspace_id="ws_repair_fail",
        compose_project="awf_ws_repair_fail",
        compose_file=compose_file,
        worktree_path=worktree_path,
    )

    assert ok is False
    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    assert runner.calls == []


@pytest.mark.unit
async def test_agent_git_writability_preflight_records_container_failure(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=128, stderr="fatal: cannot write object")
    executor = _executor_with_runner(runner, tmp_path)
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    (worktree_path / ".git").write_text("gitdir: /tmp/mirror/worktrees/ws_git_fail\n")
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    ok = await executor._run_agent_git_writability_preflight(
        workspace_id="ws_git_fail",
        compose_project="awf_ws_git_fail",
        compose_file=compose_file,
        worktree_path=worktree_path,
    )

    assert ok is False
    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    kwargs = executor._mark_failed.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["reason_code"] == "GIT_AGENT_WRITABILITY_FAILED"
    assert kwargs["details"]["stderr"] == "fatal: cannot write object"


@pytest.mark.unit
async def test_repair_agent_git_ownership_reports_repair_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("cannot repair")

    monkeypatch.setattr(executor_mod, "repair_agent_writable_worktree", _raise)

    assert not await executor._repair_agent_git_ownership(
        workspace_id="ws_repair_exception",
        worktree_path=tmp_path / "worktree",
        reason="test",
    )


def _fake_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    mirror = tmp_path / "mirror.git"
    linked_git_dir = mirror / "worktrees" / "ws_missing_head"
    linked_git_dir.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    return mirror, worktree


@pytest.mark.unit
def test_read_ref_sha_returns_none_for_missing_ref(tmp_path: Path) -> None:
    assert _read_ref_sha(tmp_path, "refs/heads/missing") is None


@pytest.mark.unit
def test_read_ref_sha_reads_packed_ref_when_loose_ref_is_missing(tmp_path: Path) -> None:
    sha = "a" * 40
    (tmp_path / "packed-refs").write_text(
        f"# pack-refs with: peeled fully-peeled sorted\n{sha} refs/heads/awf/ws_packed\n",
        encoding="utf-8",
    )

    assert _read_ref_sha(tmp_path, "refs/heads/awf/ws_packed") == sha


@pytest.mark.unit
@pytest.mark.parametrize(
    ("queued", "expected_call_count"),
    [
        ([(1, "", "base missing")], 1),
        ([(0, "", ""), (1, "", "update failed")], 2),
        ([(0, "", ""), (0, "", ""), (1, "", "reset failed")], 3),
        ([(0, "", ""), (0, "", ""), (0, "", ""), (1, "", "add failed")], 4),
        ([(0, "", ""), (0, "", ""), (0, "", ""), (0, "", ""), (0, "", "")], 5),
        ([(0, "", ""), (0, "", ""), (0, "", ""), (0, "", ""), (2, "", "diff failed")], 5),
        (
            [
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (1, "", ""),
                (1, "", "commit failed"),
            ],
            6,
        ),
        (
            [
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (1, "", ""),
                (0, "", ""),
                (1, "", "head failed"),
            ],
            7,
        ),
        (
            [
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (1, "", ""),
                (0, "", ""),
                (0, "", ""),
            ],
            7,
        ),
    ],
)
async def test_missing_head_recovery_returns_none_for_each_unrecoverable_step(
    tmp_path: Path,
    queued: list[tuple[int, str, str]],
    expected_call_count: int,
) -> None:
    _mirror, worktree = _fake_linked_worktree(tmp_path)
    runner = FakeCommandRunner()
    for returncode, stdout, stderr in queued:
        runner.queue_result(returncode=returncode, stdout=stdout, stderr=stderr)

    result = await _recover_missing_head_from_filesystem(
        runner=runner,
        workspace_id="ws_missing_head",
        worktree_path=worktree,
        base_commit="a" * 40,
        branch_name="awf/ws_missing_head",
    )

    assert result is None
    assert len(runner.calls) == expected_call_count


@pytest.mark.unit
async def test_missing_head_recovery_returns_none_without_linked_git_dir(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    result = await _recover_missing_head_from_filesystem(
        runner=runner,
        workspace_id="ws_missing_head",
        worktree_path=worktree,
        base_commit="a" * 40,
        branch_name="awf/ws_missing_head",
    )

    assert result is None
    assert runner.calls == []


@pytest.mark.unit
async def test_recover_missing_git_head_or_mark_failed_handles_terminal_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]
    executor._record_git_object_recovery_event = AsyncMock()  # type: ignore[method-assign]

    assert not await executor._recover_missing_git_head_or_mark_failed(
        workspace_id="ws_no_base",
        worktree_path=tmp_path,
        base_commit=None,
        branch_name="awf/ws_no_base",
        from_status=WorkspaceStatus.running,
        stage="agent_run",
        error=RuntimeError("fatal: bad object HEAD"),
    )
    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]

    executor._mark_failed.reset_mock()  # type: ignore[attr-defined]
    monkeypatch.setattr(
        executor_mod,
        "_recover_missing_head_from_filesystem",
        AsyncMock(return_value=None),
    )
    assert not await executor._recover_missing_git_head_or_mark_failed(
        workspace_id="ws_unrecoverable",
        worktree_path=tmp_path,
        base_commit="a" * 40,
        branch_name="awf/ws_unrecoverable",
        from_status=WorkspaceStatus.running,
        stage="post_agent_commit",
        error=RuntimeError("fatal: bad object HEAD"),
    )
    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]

    executor._mark_failed.reset_mock()  # type: ignore[attr-defined]
    recovery = _GitObjectRecoveryResult(
        broken_head_sha="b" * 40,
        recovered_head_sha="c" * 40,
    )
    monkeypatch.setattr(
        executor_mod,
        "_recover_missing_head_from_filesystem",
        AsyncMock(return_value=recovery),
    )
    assert await executor._recover_missing_git_head_or_mark_failed(
        workspace_id="ws_recovered",
        worktree_path=tmp_path,
        base_commit="a" * 40,
        branch_name="awf/ws_recovered",
        from_status=WorkspaceStatus.running,
        stage="post_agent_commit",
        error=RuntimeError("fatal: bad object HEAD"),
    )
    executor._mark_failed.assert_not_awaited()  # type: ignore[attr-defined]
    executor._record_git_object_recovery_event.assert_awaited_once_with(  # type: ignore[attr-defined]
        workspace_id="ws_recovered",
        stage="post_agent_commit",
        recovery=recovery,
    )


@pytest.mark.unit
async def test_recover_missing_git_head_or_mark_failed_fails_when_event_recording_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]
    recovery = _GitObjectRecoveryResult(
        broken_head_sha="b" * 40,
        recovered_head_sha="c" * 40,
    )
    monkeypatch.setattr(
        executor_mod,
        "_recover_missing_head_from_filesystem",
        AsyncMock(return_value=recovery),
    )
    executor._record_git_object_recovery_event = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("database unavailable")
    )

    recovered = await executor._recover_missing_git_head_or_mark_failed(
        workspace_id="ws_recovery_event_failed",
        worktree_path=tmp_path,
        base_commit="a" * 40,
        branch_name="awf/ws_recovery_event_failed",
        from_status=WorkspaceStatus.running,
        stage="post_agent_commit",
        error=RuntimeError("fatal: bad object HEAD"),
    )

    assert not recovered
    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    kwargs = executor._mark_failed.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["workspace_id"] == "ws_recovery_event_failed"
    assert kwargs["failure_reason"] == FailureReason.infrastructure_failure
    assert kwargs["reason_code"] == GIT_OBJECT_MISSING_REASON_CODE
    assert "could not record the recovery event" in kwargs["message"]
    assert "database unavailable" in kwargs["message"]


@pytest.mark.unit
async def test_recover_missing_git_head_or_mark_failed_fails_when_filesystem_recovery_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]
    executor._record_git_object_recovery_event = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        executor_mod,
        "_recover_missing_head_from_filesystem",
        AsyncMock(side_effect=RuntimeError("repair exploded")),
    )

    recovered = await executor._recover_missing_git_head_or_mark_failed(
        workspace_id="ws_recovery_raised",
        worktree_path=tmp_path,
        base_commit="a" * 40,
        branch_name="awf/ws_recovery_raised",
        from_status=WorkspaceStatus.running,
        stage="agent_run",
        error=RuntimeError("fatal: bad object HEAD"),
    )

    assert not recovered
    executor._record_git_object_recovery_event.assert_not_awaited()  # type: ignore[attr-defined]
    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    kwargs = executor._mark_failed.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["workspace_id"] == "ws_recovery_raised"
    assert kwargs["failure_reason"] == FailureReason.infrastructure_failure
    assert kwargs["reason_code"] == GIT_OBJECT_MISSING_REASON_CODE
    assert "could not run filesystem recovery" in kwargs["message"]
    assert "repair exploded" in kwargs["message"]


@pytest.mark.unit
async def test_record_git_object_recovery_event_persists_workspace_event(
    tmp_path: Path,
) -> None:
    engine = await create_postgres_test_engine()
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:x/y.git",
            branch_base="development",
            task_title="recover",
            task_prompt="recover",
            agent="codex",
            test_commands=[],
            requires_database=False,
        )
        await session.commit()
        workspace_id = ws.id

    executor = WorkspaceExecutor(
        session_factory=factory,
        runner=FakeCommandRunner(),
        compose=object(),  # type: ignore[arg-type]
        validation=object(),  # type: ignore[arg-type]
        pr_creator=object(),  # type: ignore[arg-type]
        config=ExecutorConfig(
            worktrees_root=tmp_path / "worktrees",
            compose_projects_root=tmp_path / "compose",
        ),
    )
    await executor._record_git_object_recovery_event(
        workspace_id=workspace_id,
        stage="agent_run",
        recovery=_GitObjectRecoveryResult(
            broken_head_sha="b" * 40,
            recovered_head_sha="c" * 40,
        ),
    )

    async with factory() as session:
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)
    await engine.dispose()
    recovery_events = [
        event for event in events if event.event_type == "workspace.git_object_missing_recovered"
    ]

    assert len(recovery_events) == 1
    assert recovery_events[0].reason_code == "GIT_OBJECT_MISSING_RECOVERED"
    assert recovery_events[0].payload["stage"] == "agent_run"


@pytest.mark.unit
async def test_verify_recovered_post_agent_commit_rejects_protected_paths(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._committed_paths_since = AsyncMock(  # type: ignore[method-assign]
        return_value={Path(".awf/workspace.yml")}
    )
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]

    assert not await executor._verify_recovered_post_agent_commit(
        workspace_id="ws_recovered_policy",
        worktree_path=tmp_path / "worktree",
        base_commit="a" * 40,
        owned_paths=[],
        expected_status=WorkspaceStatus.running,
    )

    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    kwargs = executor._mark_failed.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["failure_reason"] == FailureReason.policy_failure
    assert kwargs["reason_code"] == "QUALITY_GATE_POLICY_CHANGED"


@pytest.mark.unit
async def test_verify_recovered_post_agent_commit_rejects_empty_recovery(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._committed_paths_since = AsyncMock(return_value=set())  # type: ignore[method-assign]
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]

    assert not await executor._verify_recovered_post_agent_commit(
        workspace_id="ws_recovered_empty",
        worktree_path=tmp_path / "worktree",
        base_commit="d" * 40,
        owned_paths=[],
        expected_status=WorkspaceStatus.running,
    )

    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    kwargs = executor._mark_failed.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["failure_reason"] == FailureReason.agent_failure
    assert kwargs["reason_code"] == GIT_OBJECT_MISSING_RECOVERED_REASON_CODE
    assert kwargs["details"] == {"recovered_stage": "post_agent_commit"}


@pytest.mark.unit
async def test_verify_recovered_post_agent_commit_rejects_plan_only_recovery(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._committed_paths_since = AsyncMock(  # type: ignore[method-assign]
        return_value={Path("docs/awf-plans/ws_plan_only.md")}
    )
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]

    assert not await executor._verify_recovered_post_agent_commit(
        workspace_id="ws_recovered_plan_only",
        worktree_path=tmp_path / "worktree",
        base_commit="e" * 40,
        owned_paths=[],
        expected_status=WorkspaceStatus.running,
    )

    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    kwargs = executor._mark_failed.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["failure_reason"] == FailureReason.agent_failure
    assert kwargs["reason_code"] == PLAN_ONLY_OUTPUT_REASON_CODE


@pytest.mark.unit
async def test_verify_recovered_post_agent_commit_rejects_orphaned_history(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=1, stderr="not an ancestor")
    executor = _executor_with_runner(runner, tmp_path)
    executor._committed_paths_since = AsyncMock(  # type: ignore[method-assign]
        return_value={Path("src/app.py")}
    )
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]

    assert not await executor._verify_recovered_post_agent_commit(
        workspace_id="ws_recovered_orphan",
        worktree_path=tmp_path / "worktree",
        base_commit="b" * 40,
        owned_paths=[],
        expected_status=WorkspaceStatus.running,
    )

    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    kwargs = executor._mark_failed.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["failure_reason"] == FailureReason.agent_failure
    assert "does not descend from base commit" in kwargs["message"]


@pytest.mark.unit
async def test_verify_recovered_post_agent_commit_accepts_policy_clean_commit(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0)
    executor = _executor_with_runner(runner, tmp_path)
    executor._committed_paths_since = AsyncMock(  # type: ignore[method-assign]
        return_value={Path("src/app.py")}
    )
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]

    assert await executor._verify_recovered_post_agent_commit(
        workspace_id="ws_recovered_clean",
        worktree_path=tmp_path / "worktree",
        base_commit="c" * 40,
        owned_paths=[],
        expected_status=WorkspaceStatus.running,
    )

    executor._mark_failed.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_recovered_post_agent_commit_verification_exceptions_mark_failed(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._verify_recovered_post_agent_commit = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("git diff failed")
    )
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]

    assert not await executor._verify_recovered_post_agent_commit_or_mark_failed(
        workspace_id="ws_recovered_verify_error",
        worktree_path=tmp_path / "worktree",
        base_commit="f" * 40,
        owned_paths=[],
        expected_status=WorkspaceStatus.running,
    )

    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    kwargs = executor._mark_failed.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["from_status"] == WorkspaceStatus.running
    assert kwargs["failure_reason"] == FailureReason.infrastructure_failure
    assert kwargs["reason_code"] == GIT_OBJECT_MISSING_REASON_CODE
    assert "git diff failed" in kwargs["message"]


@pytest.mark.unit
async def test_missing_head_recovery_rebuilds_canonical_commit_from_filesystem(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin"
    mirror = tmp_path / "mirror.git"
    worktree = tmp_path / "worktree"
    alternate_objects = tmp_path / "alternate-objects"
    alternate_objects.mkdir()

    subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "-C", str(origin), "config", "user.name", "AWF Test"], check=True)
    subprocess.run(["git", "-C", str(origin), "config", "user.email", "awf@test.local"], check=True)
    (origin / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(origin), "add", "."], check=True)
    subprocess.run(["git", "-C", str(origin), "commit", "-q", "-m", "base"], check=True)
    base_commit = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    subprocess.run(["git", "clone", "--mirror", str(origin), str(mirror)], check=True)
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(mirror),
            "worktree",
            "add",
            "-b",
            "awf/ws_missing_object",
            str(worktree),
            "main",
        ],
        check=True,
    )

    (worktree / "IMPLEMENTATION.md").write_text("agent work\n", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_OBJECT_DIRECTORY": str(alternate_objects),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(mirror / "objects"),
        "GIT_AUTHOR_NAME": "AWF Agent",
        "GIT_AUTHOR_EMAIL": "awf@example.com",
        "GIT_COMMITTER_NAME": "AWF Agent",
        "GIT_COMMITTER_EMAIL": "awf@example.com",
    }
    subprocess.run(["git", "-C", str(worktree), "add", "IMPLEMENTATION.md"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-q", "-m", "hidden alternate commit"],
        check=True,
        env=env,
    )

    broken = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert broken.returncode != 0
    assert "bad object HEAD" in broken.stderr

    result = await _recover_missing_head_from_filesystem(
        runner=AsyncioSubprocessRunner(),
        workspace_id="ws_missing_object",
        worktree_path=worktree,
        base_commit=base_commit,
        branch_name="awf/ws_missing_object",
    )

    assert result is not None
    assert result.recovered_head_sha
    assert result.broken_head_sha != result.recovered_head_sha
    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""
    changed = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--name-only", f"{base_commit}..HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert changed.stdout.splitlines() == ["IMPLEMENTATION.md"]


@pytest.mark.unit
async def test_committed_paths_since_raises_when_git_diff_fails(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=128, stderr="bad object")
    executor = _executor_with_runner(runner, tmp_path)

    with pytest.raises(RuntimeError, match="git diff --name-only failed"):
        await executor._committed_paths_since(tmp_path / "worktree", "baseline-sha")


@pytest.mark.unit
def test_digest_dirty_content_distinguishes_content_changes_within_same_paths(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    executor = _executor_with_runner(runner, tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    target = worktree / "src" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("first")

    paths = {Path("src/x.py")}
    first = executor._digest_dirty_content(worktree, paths)

    target.write_text("second")
    second = executor._digest_dirty_content(worktree, paths)

    assert first != second, (
        "digest must reflect content changes so iterative re-edits of the "
        "same file register as progress, not a repeated-output stall"
    )


@pytest.mark.unit
def test_digest_dirty_content_is_stable_when_paths_and_content_unchanged(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    executor = _executor_with_runner(runner, tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    target = worktree / "src" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("same")

    paths = {Path("src/x.py")}
    assert executor._digest_dirty_content(worktree, paths) == (
        executor._digest_dirty_content(worktree, paths)
    )


@pytest.mark.unit
def test_digest_dirty_content_handles_missing_files_deterministically(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    executor = _executor_with_runner(runner, tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    paths = {Path("src/x.py"), Path("src/y.py")}
    first = executor._digest_dirty_content(worktree, paths)
    second = executor._digest_dirty_content(worktree, paths)
    assert first == second
    assert first != executor._digest_dirty_content(worktree, {Path("src/x.py")})


@pytest.mark.unit
def test_digest_dirty_content_flips_on_head_sha_change_with_clean_tree(
    tmp_path: Path,
) -> None:
    """Commits made during an iteration must register as progress.

    Without folding HEAD into the digest, an agent that commits each
    iteration leaves a clean working tree (empty dirty path set) and the
    digest stays identical, falsely tripping the repeated_output stall.
    """
    runner = FakeCommandRunner()
    executor = _executor_with_runner(runner, tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    empty_paths: set[Path] = set()
    before = executor._digest_dirty_content(worktree, empty_paths, head_sha="sha_before")
    after = executor._digest_dirty_content(worktree, empty_paths, head_sha="sha_after")

    assert before != after, (
        "digest must reflect HEAD progression so commits register as "
        "progress even when the working tree is clean"
    )


@pytest.mark.unit
def test_baseline_coverage_ratchet_accepts_no_regression(tmp_path: Path) -> None:
    command = _command_result(tmp_path, returncode=1)
    result = ValidationResult(
        commands=[command],
        coverage=_coverage(tmp_path, percent=90, command_result=command),
    )
    baseline = _coverage(tmp_path, percent=90, status="failed")

    adjusted = _apply_baseline_coverage_ratchet(result, baseline_coverage=baseline)

    assert adjusted.all_passed
    assert adjusted.coverage is not None
    assert adjusted.coverage.status == "baseline_debt"
    assert adjusted.coverage.reason_code == "COVERAGE_BASELINE_DEBT_NO_REGRESSION"
    assert adjusted.commands[0].returncode == 0
    assert adjusted.commands[0].reason_code == "COVERAGE_BASELINE_DEBT_NO_REGRESSION"


@pytest.mark.unit
def test_baseline_coverage_ratchet_accepts_no_regression_without_command_result(
    tmp_path: Path,
) -> None:
    result = ValidationResult(
        coverage=ValidationCoverageResult(
            provider="python",
            percent=90,
            minimum_percent=99,
            enforce=True,
            status="failed",
            reason_code="COVERAGE_BELOW_THRESHOLD",
            command_result=None,
        ),
    )
    baseline = _coverage(tmp_path, percent=90, status="failed")

    adjusted = _apply_baseline_coverage_ratchet(result, baseline_coverage=baseline)

    assert adjusted.all_passed
    assert adjusted.coverage is not None
    assert adjusted.coverage.command_result is None
    assert adjusted.coverage.reason_code == "COVERAGE_BASELINE_DEBT_NO_REGRESSION"


@pytest.mark.unit
def test_baseline_coverage_ratchet_rejects_missing_or_regressed_measurements(
    tmp_path: Path,
) -> None:
    coverage = _coverage(tmp_path, percent=88)
    baseline = _coverage(tmp_path, percent=90)

    assert not _coverage_preserves_below_threshold_baseline(None, baseline_coverage=baseline)
    assert not _coverage_preserves_below_threshold_baseline(
        _coverage(tmp_path, percent=None),
        baseline_coverage=baseline,
    )
    assert not _coverage_preserves_below_threshold_baseline(
        _coverage(tmp_path, percent=99, status="passed", reason_code="COVERAGE_OK"),
        baseline_coverage=baseline,
    )
    assert not _coverage_preserves_below_threshold_baseline(
        coverage,
        baseline_coverage=_coverage(
            tmp_path, percent=99, status="passed", reason_code="COVERAGE_OK"
        ),
    )
    assert not _coverage_preserves_below_threshold_baseline(coverage, baseline_coverage=baseline)


@pytest.mark.unit
def test_validation_coverage_metadata_includes_baseline_fields(tmp_path: Path) -> None:
    result = ValidationResult(coverage=_coverage(tmp_path, percent=91, status="reported"))
    baseline = _coverage(tmp_path, percent=None, status="failed", reason_code="COVERAGE_NOT_FOUND")

    metadata = _validation_run_coverage_metadata(result, baseline_coverage=baseline)

    assert metadata is not None
    assert metadata["percent"] == 91.0
    assert metadata["baseline_percent"] is None
    assert metadata["baseline_status"] == "failed"
    assert metadata["baseline_reason_code"] == "COVERAGE_NOT_FOUND"
    assert _validation_run_coverage_metadata(ValidationResult()) is None

    no_baseline = _validation_run_coverage_metadata(
        ValidationResult(
            coverage=ValidationCoverageResult(
                provider="python",
                percent=None,
                minimum_percent=99,
                enforce=True,
                status="failed",
                reason_code="COVERAGE_NOT_FOUND",
            )
        )
    )
    assert no_baseline == {
        "provider": "python",
        "minimum_percent": 99.0,
        "enforce": True,
        "status": "failed",
        "reason_code": "COVERAGE_NOT_FOUND",
    }


@pytest.mark.unit
def test_coverage_helpers_handle_failing_pytest_evidence(tmp_path: Path) -> None:
    coverage = _coverage(
        tmp_path,
        percent=88,
        minimum=99,
        reason_code="COVERAGE_BELOW_THRESHOLD",
    )
    coverage = ValidationCoverageResult(
        provider=coverage.provider,
        percent=coverage.percent,
        minimum_percent=coverage.minimum_percent,
        enforce=coverage.enforce,
        status=coverage.status,
        reason_code=coverage.reason_code,
        command_result=coverage.command_result,
        gaps=[{"file": "src/awf/service/provider_recovery.py", "missing_lines": [10]}],
        failing_test_node_ids=["tests/test_provider.py::test_capacity"],
        failing_test_evidence=["AssertionError: capacity"],
    )
    baseline = _coverage(tmp_path, percent=88, minimum=99)

    assert _coverage_has_failing_tests(None) is False
    assert _coverage_has_failing_tests(coverage) is True
    assert (
        _format_failing_test_evidence(
            ValidationCoverageResult(
                provider="python",
                percent=None,
                minimum_percent=99,
                enforce=True,
                status="failed",
                reason_code="PYTEST_FAILED",
                failing_test_evidence=["traceback summary"],
            )
        )
        == "traceback summary"
    )
    assert not _coverage_preserves_below_threshold_baseline(
        coverage,
        baseline_coverage=baseline,
    )
    assert "fix the failing test first" in _coverage_wrapped_pytest_failure_message(coverage)
    assert "top uncovered areas" in _coverage_wrapped_pytest_failure_message(coverage)


@pytest.mark.unit
def test_validation_result_prioritizes_pytest_failure_when_coverage_met(
    tmp_path: Path,
) -> None:
    stdout = tmp_path / "coverage.stdout"
    stderr = tmp_path / "coverage.stderr"
    stdout.write_text("TOTAL 100 1 99.02%\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    command = ValidationCommandResult(
        command="pytest --cov=awf --cov-report=term-missing",
        returncode=1,
        duration_seconds=12.0,
        stdout_path=stdout,
        stderr_path=stderr,
        phase="coverage",
        reason_code="PYTEST_TEST_FAILURE",
        policy_failed=False,
        metadata={
            "coverage_reason_code": "COVERAGE_OK",
            "failing_test_node_ids": [
                "tests/unit/runtime/test_validation.py::test_parallel_fixture_timeout"
            ],
            "failing_test_evidence": [
                "ERROR tests/unit/runtime/test_validation.py::test_parallel_fixture_timeout"
            ],
        },
    )
    coverage = ValidationCoverageResult(
        provider="python",
        percent=99.02,
        minimum_percent=99,
        enforce=True,
        status="passed",
        reason_code="COVERAGE_OK",
        command_result=command,
        failing_test_node_ids=[
            "tests/unit/runtime/test_validation.py::test_parallel_fixture_timeout"
        ],
        failing_test_evidence=[
            "ERROR tests/unit/runtime/test_validation.py::test_parallel_fixture_timeout"
        ],
        parallel_workers_requested=3,
        parallel_workers_effective=3,
        parallel_distribution="loadscope",
    )
    result = ValidationResult(commands=[command], coverage=coverage)

    assert not result.all_passed
    assert _validation_run_reason_code(result) == "PYTEST_TEST_FAILURE"
    assert _failure_reason_for_phase(result.first_failure) == FailureReason.validation_failure
    message = _validation_failure_message(result)
    assert "pytest reported failing tests" in message
    assert "coverage met the 99.0% requirement at 99.0%" in message
    metadata = _validation_run_coverage_metadata(result)
    assert metadata is not None
    assert metadata["reason_code"] == "COVERAGE_OK"
    assert metadata["failing_test_node_ids"] == [
        "tests/unit/runtime/test_validation.py::test_parallel_fixture_timeout"
    ]
    assert metadata["failing_test_evidence"] == [
        "ERROR tests/unit/runtime/test_validation.py::test_parallel_fixture_timeout"
    ]
    assert metadata["parallel_workers_requested"] == 3
    assert metadata["parallel_workers_effective"] == 3
    assert metadata["parallel_distribution"] == "loadscope"


@pytest.mark.unit
def test_coverage_wrapped_pytest_failure_message_handles_missing_coverage() -> None:
    coverage = ValidationCoverageResult(
        provider="python",
        percent=None,
        minimum_percent=99,
        enforce=True,
        status="failed",
        reason_code="PYTEST_FAILED",
        failing_test_node_ids=["tests/test_provider.py::test_failure"],
    )

    assert "coverage output was not available" in _coverage_wrapped_pytest_failure_message(coverage)


@pytest.mark.unit
def test_validation_failure_message_carries_coverage_context(tmp_path: Path) -> None:
    below_threshold = ValidationResult(
        coverage=_coverage(tmp_path, percent=88, minimum=99, reason_code="COVERAGE_BELOW_THRESHOLD")
    )
    command_failed = ValidationResult(
        coverage=_coverage(
            tmp_path,
            percent=99,
            minimum=99,
            reason_code="COVERAGE_COMMAND_FAILED",
        )
    )
    baseline = _coverage(tmp_path, percent=90, minimum=99)

    assert "pre-agent base coverage was 90.0%" in _validation_failure_message(
        below_threshold,
        baseline_coverage=baseline,
    )
    assert "coverage command failed" in _validation_failure_message(
        command_failed,
        baseline_coverage=baseline,
    )
    assert "unsupported coverage provider" in _validation_failure_message(
        ValidationResult(
            coverage=ValidationCoverageResult(
                provider="lcov",
                percent=None,
                minimum_percent=90,
                enforce=True,
                status="failed",
                reason_code="COVERAGE_PROVIDER_UNSUPPORTED",
            )
        )
    )
    assert (
        _validation_failure_message(
            ValidationResult(
                coverage=_coverage(
                    tmp_path,
                    percent=None,
                    minimum=99,
                    reason_code="COVERAGE_NOT_FOUND",
                )
            )
        )
        == "validation failed: coverage output was not found"
    )
    assert (
        _validation_failure_message(ValidationResult(commands=[_command_result(tmp_path)]))
        == "validation failed: pytest --cov"
    )
    assert (
        _validation_failure_message(
            ValidationResult(
                coverage=ValidationCoverageResult(
                    provider="python",
                    percent=None,
                    minimum_percent=90,
                    enforce=True,
                    status="failed",
                    reason_code="COVERAGE_UNKNOWN",
                    command_result=None,
                )
            )
        )
        == "validation failed"
    )


@pytest.mark.unit
def test_post_validation_conformance_result_uses_attempt_from_failure_details(
    tmp_path: Path,
) -> None:
    failure = executor_mod._PlanningRunFailure(  # noqa: SLF001
        message="Plan conformance still requires validation evidence.",
        details={
            "attempt": 2,
            "conformance": {
                "summary": "AWF validation evidence is missing.",
                "report_reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "gaps": ["pytest coverage evidence is stale", "  ", 42],
            },
        },
    )

    result = executor_mod._post_validation_conformance_fix_result(  # noqa: SLF001
        failure=failure,
        workspace_id="ws_conformance",
        artifacts_root=tmp_path,
    )

    command = result.commands[0]
    assert command.stdout_path.name == "post_validation_conformance.2.stdout"
    assert command.reason_code == PLAN_CONFORMANCE_UNSATISFIED
    text = command.stdout_path.read_text(encoding="utf-8")
    assert "Summary: AWF validation evidence is missing." in text
    assert f"Report reason code: {CONFORMANCE_REQUIRES_AWF_VALIDATION}" in text
    assert "- pytest coverage evidence is stale" in text
    assert "- 42" in text


@pytest.mark.unit
def test_post_validation_conformance_agent_failure_details_include_output_and_agent_details() -> (
    None
):
    exc = AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=2, stdout="stdout detail", stderr="stderr detail"),
        reason_code="",
        details={"provider": "codex", "retry": True},
    )

    details = executor_mod._post_validation_conformance_agent_failure_details(  # noqa: SLF001
        exc,
        validation_run_id="vr_123",
    )

    assert details["validation_run_id"] == "vr_123"
    assert details["conformance"] == {
        "phase": "post_validation",
        "reason_code": "AGENT_CLI_FAILED",
        "returncode": 2,
        "stdout": "stdout detail",
        "stderr": "stderr detail",
    }
    assert details["agent"] == {"provider": "codex", "retry": True}

    stdout_only = AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=1, stdout="stdout only", stderr=""),
    )
    stdout_only_details = executor_mod._post_validation_conformance_agent_failure_details(  # noqa: SLF001
        stdout_only,
        validation_run_id="vr_456",
    )
    assert stdout_only_details["conformance"] == {
        "phase": "post_validation",
        "reason_code": "AGENT_CLI_FAILED",
        "returncode": 1,
        "stdout": "stdout only",
    }


@pytest.mark.unit
def test_validation_failure_message_carries_healthcheck_context(tmp_path: Path) -> None:
    stdout = tmp_path / "health.stdout"
    stderr = tmp_path / "health.stderr"
    stdout.write_text("starting", encoding="utf-8")
    stderr.write_text("connection refused", encoding="utf-8")
    failure = ValidationCommandResult(
        command="GET http://api:8080/healthz expected 200",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout,
        stderr_path=stderr,
        phase="healthcheck",
        reason_code="HEALTHCHECK_HTTP_STATUS_MISMATCH",
        stream_ids={
            "stdout": "validation.01_healthcheck.stdout",
            "stderr": "validation.01_healthcheck.stderr",
        },
        metadata={
            "healthcheck_name": "api",
            "healthcheck_kind": "http",
            "target": "http://api:8080/healthz",
            "attempts": 3,
            "timeout_seconds": 30,
        },
    )

    message = _validation_failure_message(ValidationResult(commands=[failure]))

    assert "health check api" in message
    assert "http://api:8080/healthz" in message
    assert "HEALTHCHECK_HTTP_STATUS_MISMATCH" in message
    assert "validation.01_healthcheck.stderr" in message


@pytest.mark.unit
def test_validation_failure_message_omits_missing_healthcheck_optional_context(
    tmp_path: Path,
) -> None:
    stdout = tmp_path / "health-minimal.stdout"
    stderr = tmp_path / "health-minimal.stderr"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("failed", encoding="utf-8")
    failure = ValidationCommandResult(
        command="GET http://api:8080/healthz expected 200",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout,
        stderr_path=stderr,
        phase="healthcheck",
        reason_code="HEALTHCHECK_HTTP_STATUS_MISMATCH",
        stream_ids={},
        metadata={
            "healthcheck_name": "api",
            "healthcheck_kind": "http",
            "target": "http://api:8080/healthz",
        },
    )

    message = _validation_failure_message(ValidationResult(commands=[failure]))

    assert "health check api" in message
    assert " after " not in message
    assert " across " not in message
    assert "; logs:" not in message


@pytest.mark.unit
def test_validation_failure_message_handles_minimal_healthcheck_metadata(tmp_path: Path) -> None:
    failure = ValidationCommandResult(
        command="curl -fsS http://api:8000/healthz",
        returncode=7,
        duration_seconds=0.1,
        stdout_path=tmp_path / "health.stdout",
        stderr_path=tmp_path / "health.stderr",
        phase="healthcheck",
        reason_code="HEALTHCHECK_COMMAND_FAILED",
        stream_ids={"stdout": None, "stderr": None},
        metadata={
            "healthcheck_kind": 123,
            "target": None,
            "attempts": "one",
            "timeout_seconds": "30",
        },
    )

    message = _validation_failure_message(ValidationResult(commands=[failure]))

    assert message == (
        "validation failed: health check curl -fsS http://api:8000/healthz "
        "(unknown target=curl -fsS http://api:8000/healthz) "
        "failed with HEALTHCHECK_COMMAND_FAILED"
    )


@pytest.mark.unit
def test_validation_failure_message_handles_minimal_healthcheck_context(tmp_path: Path) -> None:
    stdout = tmp_path / "health-min.stdout"
    stderr = tmp_path / "health-min.stderr"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    failure = ValidationCommandResult(
        command="custom healthcheck",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout,
        stderr_path=stderr,
        phase="healthcheck",
        reason_code="HEALTHCHECK_COMMAND_FAILED",
        stream_ids={},
        metadata={},
    )

    message = _validation_failure_message(ValidationResult(commands=[failure]))

    assert message == (
        "validation failed: health check custom healthcheck "
        "(unknown target=custom healthcheck) failed with HEALTHCHECK_COMMAND_FAILED"
    )


@pytest.mark.unit
def test_validation_run_reason_code_defaults_when_no_failure_detail(tmp_path: Path) -> None:
    assert _validation_run_reason_code(ValidationResult()) == "VALIDATION_OK"
    assert (
        _validation_run_reason_code(  # type: ignore[arg-type]
            SimpleNamespace(all_passed=False, coverage=None, first_failure=None)
        )
        == "VALIDATION_FAILED"
    )
    assert (
        _validation_run_reason_code(
            ValidationResult(
                coverage=ValidationCoverageResult(
                    provider="python",
                    percent=None,
                    minimum_percent=90,
                    enforce=True,
                    status="failed",
                    reason_code="COVERAGE_UNKNOWN",
                    command_result=None,
                )
            )
        )
        == "COVERAGE_UNKNOWN"
    )
    assert (
        _validation_run_reason_code(
            ValidationResult(
                commands=[
                    ValidationCommandResult(
                        command="pytest -q",
                        returncode=1,
                        duration_seconds=0,
                        stdout_path=tmp_path / "pytest.out",
                        stderr_path=tmp_path / "pytest.err",
                        reason_code="COMMAND_FAILED",
                    )
                ]
            )
        )
        == "COMMAND_FAILED"
    )
    assert (
        _validation_run_reason_code(
            ValidationResult(
                commands=[
                    ValidationCommandResult(
                        command="awf validate alembic migration chain",
                        returncode=1,
                        duration_seconds=0,
                        stdout_path=tmp_path / "policy.out",
                        stderr_path=tmp_path / "policy.err",
                        phase="migration_policy",
                        reason_code="ALEMBIC_MULTIPLE_HEADS",
                        policy_failed=True,
                    )
                ]
            )
        )
        == "ALEMBIC_MULTIPLE_HEADS"
    )


@pytest.mark.unit
def test_read_text_if_present_handles_empty_missing_and_present_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"
    empty = tmp_path / "empty.txt"
    present = tmp_path / "present.txt"
    empty.write_text(" \n", encoding="utf-8")
    present.write_text(" useful output \n", encoding="utf-8")

    assert _read_text_if_present(missing) is None
    assert _read_text_if_present(empty) is None
    assert _read_text_if_present(present) == "useful output"


@pytest.mark.unit
def test_read_text_if_present_returns_none_when_file_read_raises() -> None:
    class _UnreadablePath:
        def is_file(self) -> bool:
            return True

        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            raise OSError("permission denied")

    assert _read_text_if_present(_UnreadablePath()) is None  # type: ignore[arg-type]


@pytest.mark.unit
def test_call_pr_monitor_factory_uses_widest_supported_signature() -> None:
    calls: list[tuple[object, object, object]] = []
    adapter = object()
    profile = WorkspaceProfile.model_validate({"name": "factory-profile"})
    workspace = object()

    def factory(adapter_arg: object, profile_arg: object, workspace_arg: object) -> object:
        calls.append((adapter_arg, profile_arg, workspace_arg))
        return "monitor"

    assert (
        _call_pr_monitor_factory(
            factory,
            adapter=adapter,  # type: ignore[arg-type]
            profile=profile,
            workspace=workspace,  # type: ignore[arg-type]
        )
        == "monitor"
    )
    assert calls == [(adapter, profile, workspace)]


@pytest.mark.unit
def test_call_pr_monitor_factory_passes_provider_recovery_default_when_supported() -> None:
    calls: list[tuple[object, object, object, str | None]] = []
    adapter = object()
    profile = WorkspaceProfile.model_validate({"name": "factory-profile"})
    workspace = object()

    def factory(
        adapter_arg: object,
        profile_arg: object,
        workspace_arg: object,
        *,
        provider_recovery_default_model: str | None = None,
    ) -> object:
        calls.append(
            (
                adapter_arg,
                profile_arg,
                workspace_arg,
                provider_recovery_default_model,
            )
        )
        return "monitor"

    assert (
        _call_pr_monitor_factory(
            factory,
            adapter=adapter,  # type: ignore[arg-type]
            profile=profile,
            workspace=workspace,  # type: ignore[arg-type]
            provider_recovery_default_model="gpt-5",
        )
        == "monitor"
    )
    assert calls == [(adapter, profile, workspace, "gpt-5")]


@pytest.mark.unit
def test_call_pr_monitor_factory_uses_two_argument_fallback_when_signature_is_opaque() -> None:
    class _OpaqueFactory:
        @property
        def __signature__(self) -> object:
            raise ValueError("opaque callable")

        def __call__(self, adapter_arg: object, profile_arg: object) -> object:
            return (adapter_arg, profile_arg)

    adapter = object()
    profile = WorkspaceProfile.model_validate({"name": "factory-profile"})

    assert _call_pr_monitor_factory(
        _OpaqueFactory(),
        adapter=adapter,  # type: ignore[arg-type]
        profile=profile,
        workspace=object(),  # type: ignore[arg-type]
    ) == (adapter, profile)


@pytest.mark.unit
def test_call_pr_monitor_factory_surfaces_bind_error() -> None:
    adapter = object()
    profile = WorkspaceProfile.model_validate({"name": "factory-profile"})

    def factory(*, required_keyword: str) -> object:
        return required_keyword

    with pytest.raises(TypeError):
        _call_pr_monitor_factory(
            factory,
            adapter=adapter,  # type: ignore[arg-type]
            profile=profile,
            workspace=object(),  # type: ignore[arg-type]
        )


@pytest.mark.unit
async def test_planning_required_accepts_committed_plan_file(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    # before_plan (clean)
    runner.queue_result(returncode=0, stdout="")
    # rev-parse HEAD -> baseline sha
    runner.queue_result(returncode=0, stdout="abc1234\n")
    # dirty after planning (still clean because agent committed)
    runner.queue_result(returncode=0, stdout="")
    # git diff --name-only <base>..HEAD -> plan file
    runner.queue_result(returncode=0, stdout="docs/awf-plans/ws_plan_commit.md\n")
    # rev-parse HEAD pre-loop (post-plan progress digest)
    runner.queue_result(returncode=0, stdout="abc1234\n")
    # before_compare
    runner.queue_result(returncode=0, stdout="")
    # after_compare
    runner.queue_result(returncode=0, stdout="")
    # rev-parse HEAD iter 0 post (iteration progress digest)
    runner.queue_result(returncode=0, stdout="abc1234\n")
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan committed",
        "implemented",
        '{"status":"satisfied","summary":"ok","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-committed",
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
        workspace=SimpleNamespace(id="ws_plan_commit", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None
    assert len(adapter.prompts) == 3


@pytest.mark.unit
async def test_planning_required_rejects_committed_code_as_outside_plan(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="base5678\n")  # rev-parse HEAD
    runner.queue_result(returncode=0, stdout="")  # dirty after planning (clean)
    runner.queue_result(
        returncode=0,
        stdout=("docs/awf-plans/ws_plan_code.md\nsrc/awf/executor.py\n"),
    )  # committed paths since baseline
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter("plan plus code")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-code-committed",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "enforce_plan_only_changes": True,
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_plan_code", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is not None
    assert not isinstance(message, str)
    assert message.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert message.details is not None
    scope = message.details["planning_scope"]
    assert scope["required_paths"] == ["docs/awf-plans/ws_plan_code.md"]
    assert scope["offending_paths"] == ["src/awf/executor.py"]
    assert len(adapter.prompts) == 1


@pytest.mark.unit
async def test_planning_required_falls_back_to_porcelain_when_no_baseline_sha(
    tmp_path: Path,
) -> None:
    # Fresh repo or detached state where rev-parse HEAD fails.
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(
        returncode=128, stderr="fatal: not a git repository"
    )  # rev-parse HEAD fails
    runner.queue_result(
        returncode=0, stdout="?? docs/awf-plans/ws_plan_fallback.md\n"
    )  # dirty after planning
    runner.queue_result(
        returncode=128, stderr="fatal: not a git repository"
    )  # rev-parse HEAD pre-loop also fails
    runner.queue_result(returncode=0, stdout="")  # before_compare
    runner.queue_result(returncode=0, stdout="")  # after_compare
    runner.queue_result(
        returncode=128, stderr="fatal: not a git repository"
    )  # rev-parse HEAD iter 0 post also fails
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan fallback",
        "implemented",
        '{"status":"satisfied","summary":"ok","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-fallback",
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
        workspace=SimpleNamespace(id="ws_plan_fallback", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None
    # No git diff --name-only call should have been issued because rev-parse failed.
    diff_calls = [
        call for call in runner.calls if "diff" in call.args and "--name-only" in call.args
    ]
    assert not diff_calls


@pytest.mark.unit
async def test_planning_required_dirty_plan_still_accepted(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="old_sha\n")  # rev-parse HEAD
    runner.queue_result(
        returncode=0, stdout="?? docs/awf-plans/ws_plan_dirty.md\n"
    )  # dirty after planning
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
    runner.queue_result(returncode=0, stdout="old_sha\n")  # rev-parse HEAD pre-loop
    runner.queue_result(returncode=0, stdout="")  # before_compare
    runner.queue_result(returncode=0, stdout="")  # after_compare
    runner.queue_result(returncode=0, stdout="old_sha\n")  # rev-parse HEAD iter 0 post
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "dirty plan",
        "implemented",
        '{"status":"satisfied","summary":"ok","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-dirty",
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
        workspace=SimpleNamespace(id="ws_plan_dirty", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None
    # Because no new commits, the diff call should return empty; porcelain still carries the plan.
    diff_calls = [
        call for call in runner.calls if "diff" in call.args and "--name-only" in call.args
    ]
    assert diff_calls


@pytest.mark.unit
async def test_planning_required_dirty_extra_file_still_rejected(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="base_sha\n")  # rev-parse HEAD
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_plan_extra_dirty.md\n?? src/extra.py\n",
    )  # after_plan
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter("dirty extra")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-extra-dirty",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "enforce_plan_only_changes": True,
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_plan_extra_dirty", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is not None
    assert not isinstance(message, str)
    assert message.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert message.details is not None
    scope = message.details["planning_scope"]
    assert scope["required_paths"] == ["docs/awf-plans/ws_plan_extra_dirty.md"]
    assert scope["offending_paths"] == ["src/extra.py"]


@pytest.mark.unit
def test_agent_model_for_workspace_prefers_nonblank_policy_override() -> None:
    defaults = AgentDefaults(model="default-model")

    assert (
        _agent_model_for_workspace(  # type: ignore[arg-type]
            SimpleNamespace(task_policy={"agent_model": "  gpt-special  "}),
            defaults,
        )
        == "gpt-special"
    )
    assert (
        _agent_model_for_workspace(  # type: ignore[arg-type]
            SimpleNamespace(task_policy={"agent_model": "   "}),
            defaults,
        )
        == "default-model"
    )
    assert (
        _agent_model_for_workspace(  # type: ignore[arg-type]
            SimpleNamespace(task_policy=None),
            None,
        )
        is None
    )


@pytest.mark.unit
def test_agent_defaults_for_workspace_binds_policy_model_for_monitor_recovery() -> None:
    defaults = AgentDefaults(model="ollama/kimi-k2.6:cloud", effort="xhigh")

    bound = _agent_defaults_for_workspace(  # type: ignore[arg-type]
        SimpleNamespace(task_policy={"agent_model": "  ollama/glm-5.1:cloud  "}),
        defaults,
    )

    assert bound is not None
    assert bound.model == "ollama/glm-5.1:cloud"
    assert bound.effort == "xhigh"


@pytest.mark.unit
def test_agent_defaults_for_workspace_handles_policy_without_base_defaults() -> None:
    effort_only = _agent_defaults_for_workspace(  # type: ignore[arg-type]
        SimpleNamespace(task_policy={"agent_effort": "high"}),
        None,
    )
    model_only = _agent_defaults_for_workspace(  # type: ignore[arg-type]
        SimpleNamespace(task_policy={"agent_model": "gpt-5.4-mini"}),
        None,
    )
    bound = _agent_defaults_for_workspace(  # type: ignore[arg-type]
        SimpleNamespace(task_policy={"agent_model": "gpt-special", "agent_effort": "high"}),
        None,
    )
    created = _agent_defaults_for_workspace(  # type: ignore[arg-type]
        SimpleNamespace(task_policy={"agent_model": "gpt-5.5", "agent_effort": "xhigh"}),
        None,
    )

    assert effort_only is None
    assert model_only == AgentDefaults(model="gpt-5.4-mini", effort=None)
    assert bound == AgentDefaults(model="gpt-special", effort="high")
    assert created == AgentDefaults(model="gpt-5.5", effort="xhigh")


@pytest.mark.unit
def test_agent_pr_identity_omits_missing_model_and_effort() -> None:
    assert (
        _agent_pr_identity(  # type: ignore[arg-type]
            SimpleNamespace(agent="codex", task_policy={}),
            defaults=None,
        )
        == "agent: `codex`"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("queued", "message"),
    [
        ([(1, "", "fetch failed")], "git fetch origin main failed"),
        ([(0, "", ""), (1, "", "switch failed")], "git switch awf/ws failed"),
        (
            [(0, "", ""), (0, "", ""), (128, "", "merge-base failed")],
            "merge-base --is-ancestor origin/main HEAD failed",
        ),
        (
            [
                (0, "", ""),
                (0, "", ""),
                (1, "", ""),
                (1, "", "conflict"),
                (0, "", ""),
            ],
            "git rebase origin/main failed",
        ),
        (
            [
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (1, "", "no target"),
            ],
            "could not resolve origin/main",
        ),
        (
            [
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (0, "b" * 40 + "\n", ""),
                (1, "", "no head"),
            ],
            "could not resolve HEAD",
        ),
        (
            [
                (0, "", ""),
                (0, "", ""),
                (1, "", ""),
                (0, "", ""),
                (0, "b" * 40 + "\n", ""),
                (0, "c" * 40 + "\n", ""),
                (1, "", "lease failed"),
            ],
            "git push --force-with-lease failed",
        ),
    ],
)
async def test_monitor_rebase_recovery_reports_git_failures(
    queued: list[tuple[int, str, str]],
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeCommandRunner()
    for returncode, stdout, stderr in queued:
        runner.queue_result(returncode=returncode, stdout=stdout, stderr=stderr)
    executor = _executor_with_runner(runner, tmp_path)

    async def skip_begin_operation(**_kwargs: object) -> None:
        return None

    async def skip_finish_operation(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        executor,
        "_begin_rebase_recovery_operation",
        skip_begin_operation,
    )
    monkeypatch.setattr(
        executor,
        "_finish_rebase_recovery_operation",
        skip_finish_operation,
    )
    monkeypatch.setattr(
        executor,
        "_record_executor_pr_audit_event",
        AsyncMock(),
    )

    with pytest.raises(_MonitorRebaseRecoveryError, match=message):
        await executor._run_monitor_rebase_recovery(
            workspace_id="ws_rebase",
            worktree_path=tmp_path / "worktrees" / "ws_rebase",
            base_branch="main",
            branch_name="awf/ws",
            remote_branch="awf/ws",
            reason="stale",
            recovery_payload={},
        )


@pytest.mark.unit
def test_active_recovery_payload_ignores_rebase_validate_only_operations() -> None:
    workspace = SimpleNamespace(
        operations=[
            SimpleNamespace(
                status=OperationStatus.pending.value,
                type=OperationType.rebase.value,
                payload={
                    "source": "pr_monitor",
                    "recovery_mode": "validate_only",
                },
            ),
            SimpleNamespace(
                status=OperationStatus.running.value,
                type=OperationType.validate.value,
                payload={
                    "source": "operator_api",
                    "recovery_mode": "validate_only",
                    "reason": "operator requested validation",
                },
            ),
        ]
    )

    payload = _get_active_recovery_payload(workspace)

    assert payload == {
        "source": "operator_api",
        "recovery_mode": "validate_only",
        "reason": "operator requested validation",
    }


@pytest.mark.unit
async def test_rebase_operation_helpers_noop_for_lightweight_executor(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)

    assert (
        await executor._begin_rebase_recovery_operation(
            workspace_id="ws_rebase",
            base_branch="main",
            remote_branch="awf/ws",
            reason="stale",
            reason_code="STALE_TARGET_BRANCH",
            source_base_sha=None,
            source_head_sha=None,
            recovery_payload={},
        )
        is None
    )
    await executor._finish_rebase_recovery_operation(
        SimpleNamespace(operation_id="op_skip", should_finish=False),  # type: ignore[arg-type]
        status=OperationStatus.succeeded,
        result={"status": "succeeded"},
    )
    await executor._finish_rebase_recovery_operation(
        None,
        status=OperationStatus.succeeded,
        result={"status": "succeeded"},
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


@pytest.mark.unit
async def test_healthcheck_failure_event_noops_when_workspace_is_not_validating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    workspace = SimpleNamespace(id="ws_health_stale", status=WorkspaceStatus.completed.value)

    class FakeWorkspaceRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, workspace_id: str) -> object:
            assert workspace_id == workspace.id
            return workspace

        async def add_event(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("stale healthcheck failures should not add events")

    monkeypatch.setattr(executor_mod, "WorkspaceRepository", FakeWorkspaceRepository)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._session_factory = lambda: session  # type: ignore[method-assign]

    await executor._record_health_check_failed_event(
        workspace_id=workspace.id,
        failure=_command_result(tmp_path),
    )

    assert session.commits == 0


@pytest.mark.unit
async def test_stale_terminal_workspace_paths_record_ignored_callbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    workspace = SimpleNamespace(id="ws_terminal", status=WorkspaceStatus.completed.value)
    stale_events: list[str] = []
    ignored_callbacks: list[dict[str, object]] = []
    finished_callbacks: list[dict[str, object]] = []

    class FakeWorkspaceRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, workspace_id: str) -> object:
            assert workspace_id == workspace.id
            return workspace

        async def get_with_operations(self, workspace_id: str) -> object:
            assert workspace_id == workspace.id
            workspace.operations = []
            return workspace

        async def record_ignored_stale_callback(
            self,
            _workspace: object,
            *,
            callback_source: str,
            callback_action: str,
            expected_status: WorkspaceStatus,
            reason_code: str,
        ) -> None:
            ignored_callbacks.append(
                {
                    "source": callback_source,
                    "action": callback_action,
                    "expected": expected_status.value,
                    "reason_code": reason_code,
                }
            )

        async def add_event(
            self,
            _workspace: object,
            *,
            event_type: str,
            **_kwargs: object,
        ) -> None:
            stale_events.append(event_type)

        async def transition(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("stale terminal workspace should not transition")

    async def finish_ignored(
        _session: object,
        **kwargs: object,
    ) -> None:
        finished_callbacks.append(kwargs)

    monkeypatch.setattr(executor_mod, "WorkspaceRepository", FakeWorkspaceRepository)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._session_factory = lambda: session  # type: ignore[method-assign]
    monkeypatch.setattr(
        executor,
        "_finish_ignored_stale_callback_operations_in_session",
        finish_ignored,
    )

    transitioned = await executor._transition_if_current(
        workspace.id,
        from_status=WorkspaceStatus.running,
        to=WorkspaceStatus.validating,
        reason="RUN_OK",
        action="start_validation",
    )
    worktree_available = await executor._ensure_worktree_available(
        workspace_id=workspace.id,
        worktree_path=tmp_path / "missing-worktree",
        expected=WorkspaceStatus.running,
        action="post_agent_commit",
        validation_run_id="vr_stale",
        requested_tier=2,
    )
    await executor._mark_failed(
        workspace_id=workspace.id,
        from_status=WorkspaceStatus.running,
        failure_reason=FailureReason.infrastructure_failure,
        message="late failure",
    )
    blocked = await executor._block_open_pr_reexecution_without_recovery(
        workspace_id=workspace.id,
    )

    assert transitioned is False
    assert worktree_available is False
    assert blocked.blocked is True
    assert [item["action"] for item in ignored_callbacks] == [
        "start_validation",
        "post_agent_commit",
        "mark_failed",
        "pr_reexecution_guard",
    ]
    assert len(finished_callbacks) == 3
    assert finished_callbacks[1]["validation_run_id"] == "vr_stale"
    assert finished_callbacks[1]["requested_tier"] == 2
    assert stale_events == ["workspace.stale_action_skipped"] * 4
    assert session.commits == 4


@pytest.mark.unit
async def test_record_rebase_recovery_success_ignores_terminal_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    workspace = SimpleNamespace(id="ws_terminal", status=WorkspaceStatus.completed.value)
    ignored_callbacks: list[tuple[str, str]] = []
    finished_callbacks: list[dict[str, object]] = []

    class FakeWorkspaceRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, workspace_id: str) -> object:
            assert workspace_id == workspace.id
            return workspace

        async def record_ignored_stale_callback(
            self,
            _workspace: object,
            *,
            callback_source: str,
            callback_action: str,
            expected_status: WorkspaceStatus,
            reason_code: str,
        ) -> None:
            assert expected_status == WorkspaceStatus.running
            assert reason_code == "STALE_CALLBACK_IGNORED"
            ignored_callbacks.append((callback_source, callback_action))

    async def finish_ignored(
        _session: object,
        **kwargs: object,
    ) -> None:
        finished_callbacks.append(kwargs)

    monkeypatch.setattr(executor_mod, "WorkspaceRepository", FakeWorkspaceRepository)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._session_factory = lambda: session  # type: ignore[method-assign]
    monkeypatch.setattr(
        executor,
        "_finish_ignored_stale_callback_operations_in_session",
        finish_ignored,
    )

    await executor._record_rebase_recovery_success(
        workspace_id=workspace.id,
        base_sha="b" * 40,
        head_sha="h" * 40,
        source_base_sha="old-base",
        source_head_sha="old-head",
        operation=SimpleNamespace(operation_id="op", should_finish=True),  # type: ignore[arg-type]
        pushed=True,
        rebased=True,
    )

    assert ignored_callbacks == [("executor", "rebase_recovery")]
    assert finished_callbacks[0]["workspace_id"] == workspace.id
    assert finished_callbacks[0]["actual_status"] == WorkspaceStatus.completed.value
    assert session.commits == 1


@pytest.mark.unit
async def test_record_rebase_recovery_success_updates_candidate_and_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    workspace = SimpleNamespace(
        id="ws_rebased",
        status=WorkspaceStatus.running.value,
        base_commit="old-base",
        monitor_last_commit_sha="old-head",
    )
    candidate_workspace = SimpleNamespace(
        base_commit="old-base", monitor_last_commit_sha="old-head"
    )
    candidate = SimpleNamespace(
        id="candidate",
        workspace_id=workspace.id,
        attempt_id="attempt",
        task_id="task",
        base_sha="old-base",
        head_sha="old-head",
        workspace=candidate_workspace,
        attempt=SimpleNamespace(id="attempt"),
    )
    readiness_calls: list[tuple[str, str]] = []
    finished_operations: list[dict[str, object]] = []

    class FakeWorkspaceRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, workspace_id: str) -> object:
            assert workspace_id == workspace.id
            return workspace

    class FakeMergeCandidateRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get_open_for_workspace_with_merge_inputs(self, workspace_id: str) -> object:
            assert workspace_id == workspace.id
            return candidate

    def sync_readiness(_candidate: object, **kwargs: object) -> None:
        readiness_calls.append(
            (
                kwargs["workspace"].base_commit,  # type: ignore[index, union-attr]
                kwargs["workspace"].monitor_last_commit_sha,  # type: ignore[index, union-attr]
            )
        )

    async def finish_operation(_session: object, **kwargs: object) -> None:
        finished_operations.append(kwargs)

    monkeypatch.setattr(executor_mod, "WorkspaceRepository", FakeWorkspaceRepository)
    monkeypatch.setattr(executor_mod, "MergeCandidateRepository", FakeMergeCandidateRepository)
    monkeypatch.setattr(executor_mod, "sync_candidate_readiness", sync_readiness)
    monkeypatch.setattr(executor_mod, "finish_monitor_operation", finish_operation)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._session_factory = lambda: session  # type: ignore[method-assign]
    monkeypatch.setattr(executor, "_add_executor_pr_audit_event", AsyncMock())

    await executor._record_rebase_recovery_success(
        workspace_id=workspace.id,
        base_sha="new-base",
        head_sha="new-head",
        source_base_sha="old-base",
        source_head_sha="old-head",
        operation=SimpleNamespace(operation_id="op_rebase", should_finish=True),  # type: ignore[arg-type]
        pushed=True,
        rebased=True,
    )

    assert workspace.base_commit == "new-base"
    assert workspace.monitor_last_commit_sha == "new-head"
    assert candidate.base_sha == "new-base"
    assert candidate.head_sha == "new-head"
    assert readiness_calls == [("new-base", "new-head")]
    assert finished_operations[0]["operation_id"] == "op_rebase"
    assert finished_operations[0]["status"] == OperationStatus.succeeded
    assert finished_operations[0]["result"]["target_base_sha"] == "new-base"
    assert session.commits == 1


@pytest.mark.unit
async def test_clear_rebase_recovery_staleness_refreshes_candidate_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    candidate = SimpleNamespace(
        id="candidate",
        workspace_id="ws_rebase",
        attempt_id="attempt",
        task_id="task",
        stale=True,
        stale_reason="target moved",
        workspace=SimpleNamespace(id="ws_rebase"),
        attempt=SimpleNamespace(id="attempt"),
    )
    replaced_findings: list[dict[str, object]] = []
    readiness_calls: list[object] = []

    class FakeMergeCandidateRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get_open_for_workspace_with_merge_inputs(self, workspace_id: str) -> object:
            assert workspace_id == candidate.workspace_id
            return candidate

    class FakeStaleReasonRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def replace_active_findings(self, **kwargs: object) -> None:
            replaced_findings.append(kwargs)

    def sync_readiness(candidate_arg: object, **_kwargs: object) -> None:
        readiness_calls.append(candidate_arg)

    monkeypatch.setattr(executor_mod, "MergeCandidateRepository", FakeMergeCandidateRepository)
    monkeypatch.setattr(executor_mod, "StaleReasonRepository", FakeStaleReasonRepository)
    monkeypatch.setattr(executor_mod, "sync_candidate_readiness", sync_readiness)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._session_factory = lambda: session  # type: ignore[method-assign]

    await executor._clear_rebase_recovery_staleness(workspace_id="ws_rebase")

    assert replaced_findings == [
        {
            "workspace_id": "ws_rebase",
            "candidate_id": "candidate",
            "attempt_id": "attempt",
            "task_id": "task",
            "findings": [],
        }
    ]
    assert candidate.stale is False
    assert candidate.stale_reason is None
    assert readiness_calls == [candidate]
    assert session.commits == 1
