"""Focused branch-coverage tests for executor helper behavior."""

from __future__ import annotations

import json
from contextlib import suppress
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
from awf.control.executor import logging_ops as executor_logging_ops
from awf.control.executor import quality_gates as executor_quality_gates
from awf.control.executor import recovery_payloads as executor_recovery_payloads
from awf.control.executor.git_ops import (
    _read_ref_sha,
)
from awf.control.executor.recovery_payloads import (
    _planning_validation_handoff_from_metadata,
    _planning_validation_handoff_from_recovery_payload,
    _planning_validation_handoff_metadata,
    _recovery_needs_existing_pr_push,
)
from awf.control.executor.types import (
    _PlanningRunFailure,
    _PlanningValidationHandoff,
    _RebaseRecoveryResult,
)
from awf.profiles.models import WorkspaceProfile
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
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup
from awf.service import artifacts as executor_service_artifacts


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


class _GitRmFakeRunner(FakeCommandRunner):
    """FakeCommandRunner that simulates ``git rm`` by deleting the target path.

    Unit tests for the post-validation conformance report cleanup use a fake
    git runner. The real cleanup now issues ``git rm`` instead of
    ``git restore`` + ``unlink``; this subclass makes a successful ``git rm``
    actually remove the on-worktree file so the tests can assert the file is
    gone and the staged-deletion semantics are exercised.
    """

    def __init__(self, worktree_path: Path) -> None:
        super().__init__()
        self._worktree_path = worktree_path

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        result = await super().run(args, input_bytes=input_bytes, cwd=cwd)
        if result.ok and args and args[0] == "git" and "rm" in args and "--" in args:
            sep_index = args.index("--")
            for path_arg in args[sep_index + 1 :]:
                target = self._worktree_path / path_arg
                with suppress(OSError):
                    target.unlink()
        return result


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
    assert executor_recovery_payloads._recovery_conformance_gaps(  # noqa: SLF001
        {"gaps": [" first gap ", "", 42]}
    ) == ("first gap", "42")
    assert executor_recovery_payloads._recovery_conformance_gaps(
        {"gaps": " rerun AWF validation "}
    ) == (  # noqa: SLF001
        "rerun AWF validation",
    )
    assert executor_recovery_payloads._recovery_conformance_gaps({"gaps": ""}) == ()  # noqa: SLF001
    assert executor_recovery_payloads._recovery_conformance_gaps({"gaps": object()}) == ()  # noqa: SLF001


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
            {"recovery_mode": "rebase_only", "source_head_sha": "old"},
            "rebased",
            _RebaseRecoveryResult(
                base_sha="base",
                head_sha="rebased",
                requires_pr_update=True,
            ),
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
    assert executor_quality_gates._ruff_check_autofix_repair_files(  # noqa: SLF001
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
    metadata_key = executor_logging_ops.SETUP_DEPENDENCY_NETWORK_METADATA_KEY

    assert (
        executor_logging_ops._setup_dependency_network_details(  # noqa: SLF001
            SimpleNamespace(metadata=None)
        )
        is None
    )
    assert (
        executor_logging_ops._setup_dependency_network_details(  # noqa: SLF001
            SimpleNamespace(metadata={metadata_key: "not-a-dict"})
        )
        is None
    )
    assert (
        executor_logging_ops._setup_dependency_network_failure_details(  # noqa: SLF001
            SimpleNamespace(
                reason_code="OTHER_FAILURE",
                metadata={metadata_key: {"host": "files.pythonhosted.org"}},
            )
        )
        is None
    )

    details = executor_logging_ops._setup_dependency_network_failure_details(  # noqa: SLF001
        SimpleNamespace(
            reason_code=executor_logging_ops.SETUP_DEPENDENCY_NETWORK_FAILURE,
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
    assert json.loads(executor_helpers._validation_evidence_json({"status": "ok"})) == {
        "status": "ok"
    }
    assert executor_helpers._validation_evidence_coverage_summary(None) == {  # noqa: SLF001
        "truncated": True,
        "original_type": "NoneType",
    }
    assert executor_helpers._validation_evidence_floor_value("short") == "short"  # noqa: SLF001
    assert executor_helpers._validation_evidence_floor_value(3) == 3  # noqa: SLF001
    assert executor_helpers._validation_evidence_floor_value(True) is True  # noqa: SLF001

    long_summary = executor_helpers._validation_evidence_floor_value("x" * 513)  # noqa: SLF001
    assert long_summary == {
        "truncated": True,
        "original_type": "string",
        "original_length": 513,
    }
    assert executor_helpers._validation_evidence_size_summary({"a": 1, "b": 2}) == {  # noqa: SLF001
        "truncated": True,
        "original_type": "mapping",
        "original_entry_count": 2,
        "retained_keys": ["a", "b"],
    }
    assert executor_helpers._validation_evidence_size_summary([1, 2, 3]) == {  # noqa: SLF001
        "truncated": True,
        "original_type": "list",
        "original_length": 3,
    }


@pytest.mark.unit
def test_requested_tier_metadata_and_adopted_remote_helpers_handle_invalid_shapes() -> None:
    assert executor_helpers._requested_tier_from_metadata(None) is None  # noqa: SLF001
    assert executor_helpers._requested_tier_from_metadata({"requested_tier": 0}) is None  # noqa: SLF001
    assert (  # noqa: SLF001
        executor_helpers._requested_tier_from_metadata({"validation": {"requested_tier": 2}}) == 2
    )
    assert executor_helpers._requested_tier_from_metadata({"validation": []}) is None  # noqa: SLF001
    assert (
        executor_helpers._existing_pr_remote_push_url(  # noqa: SLF001
            SimpleNamespace(
                task_kind="sync_feature_pr",
                repo_url="https://git.example.invalid/org/repo",
            )
        )
        is None
    )


@pytest.mark.unit
def test_post_validation_conformance_failure_text_includes_structured_report_details() -> None:
    message = executor_helpers._post_validation_conformance_failure_text(  # noqa: SLF001
        executor_helpers._PlanningRunFailure(
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


@pytest.mark.unit
def test_planning_validation_handoff_metadata_round_trips() -> None:
    # PRRT_kwDOSJAM6s6KAbBL: the block-time handoff persistence must faithfully
    # reproduce the in-memory handoff so an approve-and-keep resume reconstructs
    # the SAME pending post-validation conformance requirement.
    handoff = _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="AWF validation evidence is required.",
            gaps=("rerun pytest under AWF", "confirm migration applied"),
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        ),
        plan_path=Path("docs/awf-plans/ws123.md"),
        report_path=Path("docs/awf-plans/ws123.conformance.json"),
        iteration=2,
        max_iterations=5,
    )

    metadata = _planning_validation_handoff_metadata(handoff)
    assert _planning_validation_handoff_from_metadata(metadata) == handoff


@pytest.mark.unit
@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"plan_path": "docs/plan.md"},  # missing report_path
        {"report_path": "docs/report.json"},  # missing plan_path
        "not-a-mapping",
    ],
)
def test_planning_validation_handoff_from_metadata_treats_absent_or_malformed_as_none(
    metadata: object,
) -> None:
    # A missing payload means conformance was satisfied inline on the original
    # run; a malformed payload is treated as absent. Either way the resume must
    # NOT invent a pending handoff (which would force an unwarranted conformance
    # check) and must never crash.
    assert _planning_validation_handoff_from_metadata(metadata) is None


@pytest.mark.unit
def test_planning_validation_handoff_from_metadata_defaults_iterations_and_status() -> None:
    # Defensive reconstruction from a sparse payload: missing iteration counters
    # default to 0 and an absent/invalid status falls back to needs_iteration so
    # the conformance check still runs (and, on a grant resume, treats a miss as
    # terminal regardless of the budget).
    handoff = _planning_validation_handoff_from_metadata(
        {
            "plan_path": "docs/awf-plans/ws.md",
            "report_path": "docs/awf-plans/ws.conformance.json",
            "report": {"status": "bogus-status", "summary": "", "gaps": []},
        }
    )

    assert handoff is not None
    assert handoff.iteration == 0
    assert handoff.max_iterations == 0
    assert handoff.report.status == PlanConformanceStatus.needs_iteration


@pytest.mark.unit
async def test_satisfied_post_validation_conformance_report_write_failure_proceeds(
    tmp_path: Path,
) -> None:
    """#544 resilience: a failure to persist the (gitignored) conformance
    report must NEVER discard completed agent work. When the file write raises
    OSError, the check logs a warning, still records the conformance event, and
    returns success rather than failing the workspace."""
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    worktree_path = tmp_path / "worktree"
    runner.queue_result(returncode=0, stdout="")  # changed paths before conformance
    runner.queue_result(returncode=0, stdout="validated-head\n")
    runner.queue_result(returncode=0, stdout=f"?? {report_path.as_posix()}\n")
    runner.queue_result(returncode=0, stdout="")  # committed paths since validated HEAD
    runner.queue_result(
        returncode=128, stderr="fatal: path not in index\n"
    )  # git rm fails for untracked/gitignored report; fallback to unlink
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )

    def fail_write(**_kwargs: object) -> None:
        raise OSError("disk full")

    executor._write_satisfied_post_validation_conformance_report = fail_write  # type: ignore[method-assign]
    recorded: list[str] = []

    async def record_event(**kwargs: object) -> None:
        recorded.append(str(kwargs.get("validation_run_id")))

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
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
    )

    # Write failure is non-fatal: success returned, event still recorded, and
    # nothing is staged or committed. The report is still removed after the
    # best-effort artifact deposit.
    assert failure is None
    assert recorded == ["validation-run-1"]
    report_abs = worktree_path / report_path
    assert not report_abs.exists()
    joined_calls = [" ".join(call.args) for call in runner.calls]
    assert any("rm -- docs/awf-plans/ws_post.conformance.json" in call for call in joined_calls)
    assert not any("restore" in call for call in joined_calls)
    # No staging or committing of the AWF artifact.
    assert all("add" not in call.args for call in runner.calls)
    assert all("commit" not in call.args for call in runner.calls)


@pytest.mark.unit
async def test_satisfied_post_validation_conformance_report_untracked_fallback_to_unlink(
    tmp_path: Path,
) -> None:
    """When the report path is untracked/gitignored, ``git rm`` fails; the
    executor must still remove the on-worktree copy via plain ``unlink`` so the
    path does not dirty the worktree as an unignored untracked file."""
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    worktree_path = tmp_path / "worktree"
    report_file = worktree_path / report_path
    report_file.parent.mkdir(parents=True)
    report_file.write_text(
        '{"status":"satisfied","summary":"stale success","gaps":[]}',
        encoding="utf-8",
    )
    runner.queue_result(returncode=0, stdout=f"?? {report_path.as_posix()}\n")  # before_compare
    runner.queue_result(returncode=0, stdout="validated-head\n")  # before_compare_head
    runner.queue_result(returncode=0, stdout=f"?? {report_path.as_posix()}\n")  # after_compare
    runner.queue_result(returncode=0, stdout="")  # committed paths since validated HEAD
    runner.queue_result(
        returncode=128, stderr="fatal: pathspec '...' did not match any files\n"
    )  # git rm fails
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )
    event_markers: list[str] = []

    async def record_event(**_kwargs: object) -> None:
        event_markers.append("record")

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
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
    )

    assert failure is None
    assert event_markers == ["record"]
    assert not report_file.exists()
    joined_calls = [" ".join(call.args) for call in runner.calls]
    assert any("rm -- docs/awf-plans/ws_post.conformance.json" in call for call in joined_calls)
    assert not any("restore" in call for call in joined_calls)
    assert all("add" not in call.args for call in runner.calls)
    assert all("commit" not in call.args for call in runner.calls)

    report_abs = worktree_path / report_path
    # The report is removed after deposit, even when the AWF re-synthesis write
    # failed.
    assert not report_abs.exists()


@pytest.mark.unit
async def test_satisfied_post_validation_conformance_stdout_deposits_artifact_before_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#608: a satisfied report produced from stdout must be deposited into the
    served artifact dir before the on-worktree copy is removed.

    ``_run_post_validation_conformance_check`` records the satisfied event,
    deposits the plan/report through ``deposit_workspace_planning_artifacts``,
    then restores and unlinks the on-worktree report. Without the deposit step,
    the served artifact directory would lose the conformance report because the
    worktree copy is deleted before ``execution_validation.py`` can deposit it.
    """
    worktree_path = tmp_path / "worktree"
    runner = _GitRmFakeRunner(worktree_path)
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    plan_path = Path("docs/awf-plans/ws_post.md")
    report_abs = worktree_path / report_path
    plan_abs = worktree_path / plan_path
    satisfied = '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}'

    runner.queue_result(returncode=0, stdout="")  # before_compare
    runner.queue_result(returncode=0, stdout="validated-head\n")  # before_compare_head
    runner.queue_result(returncode=0, stdout="")  # after_compare
    runner.queue_result(returncode=0, stdout="")  # committed paths since validated HEAD
    runner.queue_result(returncode=0, stdout="")  # git rm report path

    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )
    recorded: list[str] = []

    async def record_event(**kwargs: object) -> None:
        recorded.append(str(kwargs.get("validation_run_id")))

    executor._record_post_validation_conformance_event = record_event  # type: ignore[method-assign]

    # Track deposit invocations so we can assert ordering relative to unlink.
    deposited: list[tuple[bool, bool]] = []
    real_deposit = executor_service_artifacts.deposit_workspace_planning_artifacts

    def _spy_deposit(*args: object, **kwargs: object) -> None:
        # Record whether both source files were present when the deposit ran.
        source = kwargs.get("report_path") or args[3]
        report_present = (worktree_path / source).exists()
        plan_present = (worktree_path / (kwargs.get("plan_path") or args[2])).exists()
        deposited.append((plan_present, report_present))
        real_deposit(*args, **kwargs)

    import awf.control.executor.planning_ops as _planning_ops_module

    monkeypatch.setattr(
        _planning_ops_module,
        "deposit_workspace_planning_artifacts",
        _spy_deposit,
    )

    # Create the on-worktree plan and a stale report. The conformance call only
    # emits the report in stdout, so the file on disk stays stale and should be
    # overwritten by the AWF-synthesized satisfied report before removal.
    plan_abs.parent.mkdir(parents=True, exist_ok=True)
    plan_abs.write_text("# plan\n", encoding="utf-8")
    report_abs.parent.mkdir(parents=True, exist_ok=True)
    report_abs.write_text('{"status":"stale"}', encoding="utf-8")

    profile = WorkspaceProfile.model_validate({"name": "planned", "planning": {"required": True}})
    handoff = _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="AWF validation evidence is missing.",
            gaps=("Run AWF validation.",),
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        ),
        plan_path=plan_path,
        report_path=report_path,
        iteration=0,
        max_iterations=2,
    )

    failure = await executor._run_post_validation_conformance_check(
        adapter=_PlanningAdapter(satisfied),  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
    )

    assert failure is None
    assert recorded == ["validation-run-1"]
    # Deposit ran while the worktree report copy still existed.
    assert deposited == [(True, True)]
    # The on-worktree report copy is removed last.
    assert not report_abs.exists()
    joined_calls = [" ".join(call.args) for call in runner.calls]
    assert any("rm -- docs/awf-plans/ws_post.conformance.json" in call for call in joined_calls)
    assert not any("restore" in call for call in joined_calls)
    # The final served artifact dir contains the satisfied conformance report
    # (written with the AWF-synthesized JSON shape, which includes a reason_code).
    artifact_dir = executor_service_artifacts.workspace_artifact_dir(
        tmp_path / "compose" / "..", "ws_post"
    ).resolve()
    deposited_report = json.loads((artifact_dir / "conformance.json").read_text(encoding="utf-8"))
    assert deposited_report["status"] == "satisfied"
    assert deposited_report["summary"] == "validated evidence satisfies plan"
    assert (artifact_dir / "plan.md").read_text(encoding="utf-8") == "# plan\n"


@pytest.mark.unit
async def test_validation_success_path_deposits_inline_satisfied_planning_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6KDGKY: when planning is required but conformance is
    satisfied inline (no AWF-validation handoff), a passing validation must
    still deposit the plan and conformance report into the served artifact dir.

    ``_run_post_validation_conformance_check`` is skipped when
    ``planning_validation_handoff`` is ``None``, so the success path must do
    the best-effort deposit before returning to ``execution_flow``.
    """
    profile = WorkspaceProfile.model_validate(
        {"name": "prof-inline-satisfied", "planning": {"required": True}}
    )
    workspace = SimpleNamespace(
        resolved_profile={"name": "prof-inline-satisfied"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="A task",
        agent="codex",
        owned_paths=(),
        id="ws_inline_satisfied",
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

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-inline-satisfied"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _run_post_validation_conformance_check=AsyncMock(return_value=None),
    )

    worktree_path = tmp_path / "worktree"
    plan_path = Path("docs/awf-plans/ws_inline_satisfied.md")
    report_path = Path("docs/awf-plans/ws_inline_satisfied.conformance.json")
    plan_abs = worktree_path / plan_path
    report_abs = worktree_path / report_path
    plan_abs.parent.mkdir(parents=True, exist_ok=True)
    plan_abs.write_text("# plan\n", encoding="utf-8")
    satisfied_report = {
        "status": "satisfied",
        "summary": "plan satisfied inline",
        "reason_code": "PLAN_CONFORMANCE_SATISFIED",
        "gaps": [],
    }
    report_abs.write_text(json.dumps(satisfied_report), encoding="utf-8")

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
    executor._run_post_validation_conformance_check.assert_not_awaited()
    artifact_dir = executor_service_artifacts.workspace_artifact_dir(
        tmp_path / "artifacts" / "..", workspace.id
    ).resolve()
    assert (artifact_dir / "plan.md").read_text(encoding="utf-8") == "# plan\n"
    deposited_report = json.loads((artifact_dir / "conformance.json").read_text(encoding="utf-8"))
    assert deposited_report["status"] == "satisfied"
    assert deposited_report["summary"] == "plan satisfied inline"


@pytest.mark.unit
async def test_validation_success_path_does_not_redeposit_after_conformance_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6KCdzX: after a passing validation, the conformance
    report is already deposited (and the worktree copy unlinked) from inside
    ``_run_post_validation_conformance_check``. The success path in
    ``run_validation_and_fix_cycle`` must not attempt a second, no-op deposit
    that would silently skip the report because the worktree file is gone.
    """
    profile = WorkspaceProfile.model_validate(
        {"name": "prof-no-second-deposit", "planning": {"required": True}}
    )
    workspace = SimpleNamespace(
        resolved_profile={"name": "prof-no-second-deposit"},
        requested_profile=None,
        profile_ref=None,
        env_profile=None,
        task_class=None,
        operations=[],
        test_commands=[],
        task_title="A task",
        agent="codex",
        owned_paths=(),
        id="ws_no_second_deposit",
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

    executor = SimpleNamespace(
        _transition_if_current=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=3,
            compose_projects_root=tmp_path / "artifacts",
        ),
        _capture_workspace_head_sha=AsyncMock(return_value="c" * 40),
        _start_validation_run=AsyncMock(return_value="vr-no-second-deposit"),
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(),
        _finish_validation_callback_if_terminal=AsyncMock(return_value=False),
        _update_subphase=AsyncMock(),
        _validation=_Validation(),
        _run_post_validation_conformance_check=AsyncMock(return_value=None),
    )

    outer_deposits: list[object] = []
    real_outer_deposit = (
        executor_execution_validation._planning_artifacts._deposit_planning_artifacts_best_effort
    )

    def _spy_outer_deposit(*_args: object, **_kwargs: object) -> None:
        outer_deposits.append(True)
        real_outer_deposit(*_args, **_kwargs)

    monkeypatch.setattr(
        executor_execution_validation._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _spy_outer_deposit,
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
        max_iterations=2,
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

    assert not result.stop
    # No second best-effort deposit from the success-path block: the real
    # deposit already happened inside _run_post_validation_conformance_check.
    assert outer_deposits == []
    executor._run_post_validation_conformance_check.assert_awaited_once()


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


def _passing_validation_command(tmp_path: Path) -> ValidationCommandResult:
    stdout = tmp_path / "ok.stdout"
    stderr = tmp_path / "ok.stderr"
    stdout.write_text("ok", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    return ValidationCommandResult(
        command="pytest -q",
        returncode=0,
        duration_seconds=0.1,
        stdout_path=stdout,
        stderr_path=stderr,
        phase="validate",
        reason_code=None,
        policy_failed=False,
    )


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

    evidence = executor_helpers._validation_evidence_json(payload)

    assert len(evidence) <= executor_helpers._VALIDATION_EVIDENCE_JSON_LIMIT
    decoded = json.loads(evidence)
    assert decoded["evidence_truncated"] is True
    assert decoded["coverage"]["truncated"] is True
    assert "percent" not in decoded["coverage"]
    assert decoded["oversized_serialized_length"] == len(
        json.dumps(executor_helpers.redact_audit_value(payload), default=str)
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

    evidence = executor_helpers._validation_evidence_json(payload)

    decoded = json.loads(evidence)
    assert decoded["evidence_truncated"] is True
    assert decoded["coverage"]["status"] == "failed"
    assert decoded["coverage"]["retained_keys"] == ["status", "packages"]
    assert decoded["commands"]["original_type"] == "list"
    assert len(evidence) <= executor_helpers._VALIDATION_EVIDENCE_JSON_LIMIT


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

    evidence = executor_helpers._validation_evidence_json(payload)

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
    monkeypatch.setattr(executor_helpers, "_VALIDATION_EVIDENCE_JSON_LIMIT", 10)

    evidence = executor_helpers._validation_evidence_json(
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
    original_floor_value = executor_helpers._validation_evidence_floor_value

    def record_floor_value(value: object) -> object:
        floor_values.append(value)
        return original_floor_value(value)

    monkeypatch.setattr(
        executor_helpers,
        "_validation_evidence_floor_value",
        record_floor_value,
    )

    floor_payload = executor_helpers._validation_evidence_floor_payload(
        payload,
        oversized_serialized_length=123456,
    )

    assert coverage not in floor_values
    assert floor_payload["coverage"] == executor_helpers._validation_evidence_size_summary(coverage)


@pytest.mark.unit
def test_validation_evidence_floor_payload_handles_payload_without_coverage() -> None:
    floor_payload = executor_helpers._validation_evidence_floor_payload(  # noqa: SLF001
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
    assert executor_helpers._validation_evidence_coverage_summary("raw coverage") == {  # noqa: SLF001
        "truncated": True,
        "original_type": "string",
        "original_length": len("raw coverage"),
    }
    assert executor_helpers._validation_evidence_coverage_summary({"other": 1}) == {  # noqa: SLF001
        "truncated": True,
        "original_type": "mapping",
        "original_entry_count": 1,
        "retained_keys": ["other"],
    }
    assert executor_helpers._validation_evidence_floor_value("short") == "short"  # noqa: SLF001
    assert executor_helpers._validation_evidence_floor_value(3) == 3  # noqa: SLF001
    assert executor_helpers._validation_evidence_floor_value(None) is None  # noqa: SLF001
    assert executor_helpers._validation_evidence_floor_value("x" * 600) == {  # noqa: SLF001
        "truncated": True,
        "original_type": "string",
        "original_length": 600,
    }
    assert executor_helpers._validation_evidence_floor_value(("tuple",)) == {  # noqa: SLF001
        "truncated": True,
        "original_type": "tuple",
    }


@pytest.mark.unit
def test_validation_evidence_serializer_uses_evidence_limit_for_redaction_expansion() -> None:
    payload = {"output": " ".join(["SECRET=a"] * 2166)}
    raw_length = len(json.dumps(payload, default=str))
    assert raw_length < executor_helpers._VALIDATION_EVIDENCE_JSON_LIMIT

    evidence = executor_helpers._serialize_validation_evidence_payload(payload)

    assert len(evidence) == executor_helpers._VALIDATION_EVIDENCE_JSON_LIMIT + len("...[truncated]")
    assert len(evidence) < raw_length + 4096
    assert "[redacted]" in evidence
    assert "SECRET=a" not in evidence
    assert evidence.endswith("...[truncated]")


@pytest.mark.unit
def test_post_validation_conformance_fix_result_preserves_attempt_artifacts(
    tmp_path: Path,
) -> None:
    first = executor_helpers._post_validation_conformance_fix_result(
        failure=executor_helpers._PlanningRunFailure(
            message="first conformance gap",
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
        ),
        workspace_id="ws_post",
        artifacts_root=tmp_path,
        attempt=1,
    )
    second = executor_helpers._post_validation_conformance_fix_result(
        failure=executor_helpers._PlanningRunFailure(
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
    text = executor_helpers._post_validation_conformance_failure_text(  # noqa: SLF001
        executor_helpers._PlanningRunFailure(  # noqa: SLF001
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
        executor_helpers._existing_pr_remote_push_url(  # noqa: SLF001
            SimpleNamespace(task_kind="feature_branch_pr", repo_url="not a url")
        )
        is None
    )
    assert (
        executor_helpers._existing_pr_remote_push_url(  # noqa: SLF001
            SimpleNamespace(task_kind="sync_feature_pr", repo_url="not a url")
        )
        is None
    )


@pytest.mark.unit
async def test_satisfied_post_validation_conformance_report_is_written_not_committed(
    tmp_path: Path,
) -> None:
    """The satisfied report is written to the gitignored ``docs/awf-plans/``
    path for inspection and the conformance event is recorded, but the report
    is never ``git add``-ed or committed (#544: the path is gitignored, so
    committing it crashed the workspace and discarded completed agent work)."""
    worktree_path = tmp_path / "worktree"
    runner = _GitRmFakeRunner(worktree_path)
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
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
    runner.queue_result(
        returncode=0, stdout="D  docs/awf-plans/ws_post.conformance.json\n"
    )  # git rm report path
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )
    event_markers: list[str] = []

    async def record_event(**_kwargs: object) -> None:
        event_markers.append("record")

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
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
    )

    assert failure is None
    # #604: the satisfied report is removed from the worktree so it cannot
    # dirty a tracked path, but the conformance outcome is recorded as an event.
    assert not report_file.exists()
    assert event_markers == ["record"]
    joined_calls = [" ".join(call.args) for call in runner.calls]
    assert any("rm -- docs/awf-plans/ws_post.conformance.json" in call for call in joined_calls)
    assert not any("restore" in call for call in joined_calls)
    # It is never staged or committed.
    assert all("add" not in call.args for call in runner.calls)
    assert all("commit" not in call.args for call in runner.calls)


@pytest.mark.unit
async def test_post_validation_conformance_prefers_stdout_when_report_is_stale(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktree"
    runner = _GitRmFakeRunner(worktree_path)
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
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
    runner.queue_result(returncode=0, stdout="")  # git rm report path
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
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
        adapter=_PlanningAdapter(satisfied_stdout),  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
    )

    assert failure is None
    # #604: stdout-derived report is also removed from the worktree before the
    # function returns; the conformance event is still recorded.
    assert not report_file.exists()
    executor._record_post_validation_conformance_event.assert_awaited_once()  # type: ignore[attr-defined]
    joined_calls = [" ".join(call.args) for call in runner.calls]
    assert any("rm -- docs/awf-plans/ws_post.conformance.json" in call for call in joined_calls)
    assert not any("restore" in call for call in joined_calls)


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
    runner.queue_result(returncode=0, stdout="")  # git restore report path
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
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
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
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
    executor._record_post_validation_conformance_event.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_post_validation_conformance_failure_counts_handoff_iterations(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    runner.queue_result(returncode=0, stdout="")  # changed paths before conformance
    runner.queue_result(returncode=0, stdout="validated-head\n")
    runner.queue_result(returncode=0, stdout=f"?? {report_path.as_posix()}\n")
    runner.queue_result(returncode=0, stdout="")  # committed paths since validated HEAD
    runner.queue_result(returncode=0, stdout="")  # git rm report path
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
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
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
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
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
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
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
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
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
    executor._record_post_validation_conformance_event.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_satisfied_post_validation_conformance_report_unlinks_tracked_report(
    tmp_path: Path,
) -> None:
    """#604 regression: an AWF-synthesised satisfied report must not remain as a
    tracked dirty file. We simulate a tracked repo where the conformance report
    path is already in the index; after the check returns success, the on-worktree
    report file is removed so the PR monitor's dirty-worktree guard sees a clean
    tree. The conformance event is still recorded and no git add/commit runs."""
    worktree_path = tmp_path / "worktree"
    runner = _GitRmFakeRunner(worktree_path)
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    report_file = worktree_path / report_path
    report_file.parent.mkdir(parents=True)
    # Pre-existing tracked-style report (e.g. from an earlier attempt).
    report_file.write_text(
        '{"status":"satisfied","summary":"stale success","gaps":[]}',
        encoding="utf-8",
    )
    runner.queue_result(returncode=0, stdout=f" M {report_path.as_posix()}\n")  # before_compare
    runner.queue_result(returncode=0, stdout="validated-head\n")  # before_compare_head
    runner.queue_result(returncode=0, stdout=f" M {report_path.as_posix()}\n")  # after_compare
    runner.queue_result(returncode=0, stdout="")  # committed paths since validated HEAD
    runner.queue_result(
        returncode=0, stdout="D  docs/awf-plans/ws_post.conformance.json\n"
    )  # git rm report path
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )
    event_markers: list[str] = []

    async def record_event(**_kwargs: object) -> None:
        event_markers.append("record")

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
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
    )

    assert failure is None
    assert event_markers == ["record"]
    # The on-worktree report is removed so it cannot dirty the tree later.
    assert not report_file.exists()
    # ``git rm`` stages the deletion, avoiding the unstaged-deletion dirty-tree
    # class of issue #604.
    joined_calls = [" ".join(call.args) for call in runner.calls]
    assert any("rm -- docs/awf-plans/ws_post.conformance.json" in call for call in joined_calls)
    assert not any("restore" in call for call in joined_calls)
    # No staging or committing of the AWF artifact.
    assert all("add" not in call.args for call in runner.calls)
    assert all("commit" not in call.args for call in runner.calls)
