"""Focused branch-coverage tests for executor helper behavior."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor import helpers as executor_helpers
from awf.control.executor import quality_gates as executor_quality_gates
from awf.control.executor.types import (
    _PlanningValidationHandoff,
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


class _GitRestoreFakeRunner(FakeCommandRunner):
    """FakeCommandRunner that simulates ``git restore --source=<base_commit>``.

    A successful ``git restore`` puts the committed content back into the index
    and worktree, so the path remains on disk with the tracked content and the
    worktree stays clean. Tracked reports are left alone after restore; untracked
    reports still fall back to a plain unlink when the restore command fails.
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
        if not result.ok:
            return result
        if not (args and args[0] == "git" and "restore" in args and "--" in args):
            return result
        sep_index = args.index("--")
        for path_arg in args[sep_index + 1 :]:
            target = self._worktree_path / path_arg
            if target.exists():
                # Tracked path: restore leaves the committed copy in place.
                continue
            # Simulate untracked path restore failure; a non-ok result lets the
            # caller fall back to plain unlink.
            return CommandResult(
                returncode=128,
                stdout="",
                stderr=f"fatal: pathspec '{path_arg}' did not match any files\n",
            )
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
    runner = _GitRestoreFakeRunner(worktree_path)
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    report_file = worktree_path / report_path
    report_file.parent.mkdir(parents=True)
    # Untracked/gitingored report: git restore fails, so the executor falls back
    # to a plain unlink and the file disappears.
    report_file.write_text(
        '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}',
        encoding="utf-8",
    )
    runner.queue_result(returncode=0, stdout="")  # changed paths before conformance
    runner.queue_result(returncode=0, stdout="validated-head\n")
    runner.queue_result(returncode=0, stdout=f"?? {report_path.as_posix()}\n")
    runner.queue_result(returncode=0, stdout="")  # committed paths since validated HEAD
    runner.queue_result(
        returncode=128, stderr="fatal: pathspec '...' did not match any files\n"
    )  # git restore report path (untracked -> fails)
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
        base_commit="base-commit-sha",
    )

    assert failure is None
    # #604: the satisfied report is removed from the worktree so it cannot
    # dirty a tracked path, but the conformance outcome is recorded as an event.
    assert not report_file.exists()
    assert event_markers == ["record"]
    joined_calls = [" ".join(call.args) for call in runner.calls]
    assert any(
        "restore --source base-commit-sha --worktree --staged -- docs/awf-plans/ws_post.conformance.json"
        in call
        for call in joined_calls
    )
    assert not any("rm -- docs/awf-plans/ws_post.conformance.json" in call for call in joined_calls)
    # No staging or committing of the AWF artifact.
    assert all("add" not in call.args for call in runner.calls)
    assert all("commit" not in call.args for call in runner.calls)


@pytest.mark.unit
async def test_satisfied_post_validation_conformance_report_restores_from_base_commit(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6KIyra regression: a tracked conformance report must be
    restored from ``base_commit``, not from ``HEAD``.

    When a post-validation conformance miss gets a fix pass, the unsatisfied
    report can be committed (``git add -A`` stages everything). If the
    subsequent satisfied check restores the report path from ``HEAD``, it will
    resurrect that stale unsatisfied report in the worktree and leave the tree
    clean, allowing the PR to push an AWF-authored unsatisfied report. Restoring
    from ``base_commit`` instead keeps the original baseline content and avoids
    committing AWF-authored reports.
    """
    worktree_path = tmp_path / "worktree"
    runner = _GitRestoreFakeRunner(worktree_path)
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    report_file = worktree_path / report_path
    report_file.parent.mkdir(parents=True)
    # Simulate the worktree state after a fix pass committed an unsatisfied
    # report at HEAD: the on-disk file now carries the stale unsatisfied content,
    # but the original baseline content is the one we want restored.
    base_content = '{"status":"satisfied","summary":"baseline pre-awf content","gaps":[]}'
    stale_unsatisfied = '{"status":"needs_iteration","summary":"stale miss","gaps":["fix me"]}'
    report_file.write_text(stale_unsatisfied, encoding="utf-8")
    runner.queue_result(returncode=0, stdout=f" M {report_path.as_posix()}\n")  # before_compare
    runner.queue_result(returncode=0, stdout="fix-pass-head\n")  # before_compare_head
    runner.queue_result(returncode=0, stdout=f" M {report_path.as_posix()}\n")  # after_compare
    runner.queue_result(returncode=0, stdout="")  # committed paths since validated HEAD
    runner.queue_result(
        returncode=0, stdout="D  docs/awf-plans/ws_post.conformance.json\n"
    )  # git restore report path succeeds
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDED_OK"
    )
    event_markers: list[str] = []

    async def record_event(**_kwargs: object) -> None:
        event_markers.append("record")

    executor._record_post_validation_conformance_event = record_event  # type: ignore[method-assign]

    # Make the fake runner actually restore the base content so the test can
    # assert the file ends up with baseline content, not the stale HEAD content.
    real_run = runner.run

    async def restoring_run(
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        result = await real_run(args, input_bytes=input_bytes, cwd=cwd)
        if result.ok and args and args[0] == "git" and "restore" in args:
            if not report_file.exists():
                return result
            # Simulate restore from base_commit: put the baseline content back.
            report_file.write_text(base_content, encoding="utf-8")
        return result

    runner.run = restoring_run  # type: ignore[method-assign]

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
        base_commit="base-commit-sha",
    )

    assert failure is None
    assert event_markers == ["record"]
    # Restored from base_commit, so the stale unsatisfied content is gone and
    # the baseline content is present.
    assert report_file.read_text(encoding="utf-8") == base_content
    joined_calls = [" ".join(call.args) for call in runner.calls]
    assert any(
        "restore --source base-commit-sha --worktree --staged -- docs/awf-plans/ws_post.conformance.json"
        in call
        for call in joined_calls
    )
    assert not any("rm -- docs/awf-plans/ws_post.conformance.json" in call for call in joined_calls)
    assert all("add" not in call.args for call in runner.calls)
    assert all("commit" not in call.args for call in runner.calls)


@pytest.mark.unit
async def test_post_validation_conformance_prefers_stdout_when_report_is_stale(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktree"
    runner = _GitRestoreFakeRunner(worktree_path)
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
    runner.queue_result(
        returncode=128, stderr="fatal: pathspec '...' did not match any files\n"
    )  # git restore report path (untracked -> fails)

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
        base_commit="base-commit-sha",
    )

    assert failure is None
    # Untracked/stdout-derived report: git restore fails, so the executor falls
    # back to plain unlink and the file is removed before the function returns.
    # The conformance event is still recorded.
    assert not report_file.exists()
    executor._record_post_validation_conformance_event.assert_awaited_once()  # type: ignore[attr-defined]
    joined_calls = [" ".join(call.args) for call in runner.calls]
    assert any(
        "restore --source base-commit-sha --worktree --staged -- docs/awf-plans/ws_post.conformance.json"
        in call
        for call in joined_calls
    )
    assert not any("rm -- docs/awf-plans/ws_post.conformance.json" in call for call in joined_calls)


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
        base_commit="base-commit-sha",
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
    runner.queue_result(returncode=0, stdout="")  # git restore report path
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
        base_commit="base-commit-sha",
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
        base_commit="base-commit-sha",
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
        base_commit="base-commit-sha",
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
        base_commit="base-commit-sha",
    )

    assert failure is not None
    assert failure.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert failure.details is not None
    assert failure.details["planning_scope"]["offending_paths"] == ["src/unvalidated.py"]
    executor._record_post_validation_conformance_event.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_satisfied_post_validation_conformance_report_restores_tracked_report_from_base_commit(
    tmp_path: Path,
) -> None:
    """`#604` regression: for tracked conformance paths, satisfied cleanup must
    restore from ``base_commit`` so the baseline tracked copy remains on disk
    without dirtying the tree. The conformance event is still recorded and no
    git add/commit runs."""
    worktree_path = tmp_path / "worktree"
    runner = _GitRestoreFakeRunner(worktree_path)
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    report_file = worktree_path / report_path
    report_file.parent.mkdir(parents=True)
    # Pre-existing tracked-style report (e.g. from an earlier attempt).
    # It is modified by the agent during the conformance check; the executor
    # re-writes it from the satisfied report, then git restore succeeds and
    # restores the committed content.
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
    )  # git restore report path
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
        base_commit="base-commit-sha",
    )

    assert failure is None
    assert event_markers == ["record"]
    # Tracked report restored from base_commit: the original baseline copy
    # remains on disk so the worktree stays clean, instead of re-deleting the
    # restored file. Restoring from HEAD would resurrect any stale AWF-authored
    # report committed by an earlier fix pass; restoring from base_commit keeps
    # the original project content.
    assert report_file.exists()
    joined_calls = [" ".join(call.args) for call in runner.calls]
    assert any(
        "restore --source base-commit-sha --worktree --staged -- docs/awf-plans/ws_post.conformance.json"
        in call
        for call in joined_calls
    )
    assert not any("rm -- docs/awf-plans/ws_post.conformance.json" in call for call in joined_calls)
    # No staging or committing of the AWF artifact.
    assert all("add" not in call.args for call in runner.calls)
    assert all("commit" not in call.args for call in runner.calls)


@pytest.mark.unit
async def test_satisfied_post_validation_conformance_report_restores_from_head_when_base_differs(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6KKSZU regression: a successful ``git restore`` from
    ``base_commit`` can leave the report path staged relative to HEAD when
    HEAD differs from ``base_commit`` (e.g. an earlier fix pass committed the
    AWF-authored report). The executor must restore the path from HEAD so both
    the index and worktree match the current commit, leaving the committed
    report in place and the tree clean instead of publishing a staged change.
    """
    worktree_path = tmp_path / "worktree"
    runner = _GitRestoreFakeRunner(worktree_path)
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    report_file = worktree_path / report_path
    report_file.parent.mkdir(parents=True)
    committed_report = (
        '{"status":"needs_iteration","summary":"committed stale miss","gaps":["fix me"]}'
    )
    # Pre-seed the worktree with the committed HEAD copy; the fake runner only
    # simulates restore behavior for untracked paths, so for tracked paths the
    # file content remains as-is. The adapter rewrites it, then the code writes
    # the satisfied report; we patch the writer to keep the committed report.
    report_file.write_text(committed_report, encoding="utf-8")
    runner.queue_result(returncode=0, stdout=f" M {report_path.as_posix()}\n")  # before_compare
    runner.queue_result(returncode=0, stdout="fix-pass-head\n")  # before_compare_head
    runner.queue_result(returncode=0, stdout=f" M {report_path.as_posix()}\n")  # after_compare
    runner.queue_result(returncode=0, stdout="")  # committed paths since validated HEAD
    # base_commit restore exits 0 but leaves a staged modification relative to HEAD.
    runner.queue_result(returncode=0, stdout=f"M  {report_path.as_posix()}\n")
    # Cleanliness check after base_commit restore still sees the staged path.
    runner.queue_result(returncode=0, stdout=f"M  {report_path.as_posix()}\n")
    # HEAD restore exits 0 and cleans the path.
    runner.queue_result(returncode=0, stdout="")
    # Final cleanliness check after HEAD restore is clean.
    runner.queue_result(returncode=0, stdout="")
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )

    def _preserve_committed_report(**kwargs: Any) -> None:
        # Intentionally leave the committed report file on disk; the real writer
        # would overwrite it with the AWF-synthesized satisfied report.
        del kwargs

    executor._write_satisfied_post_validation_conformance_report = _preserve_committed_report  # type: ignore[method-assign]
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
        base_commit="base-commit-sha",
    )

    assert failure is None
    assert event_markers == ["record"]
    # The HEAD restore reconciled the staged diff, so the committed report copy
    # remains on disk and the tree is clean.
    assert report_file.exists()
    assert report_file.read_text(encoding="utf-8") == committed_report
    joined_calls = [" ".join(call.args) for call in runner.calls]
    assert any(
        "restore --source base-commit-sha --worktree --staged -- docs/awf-plans/ws_post.conformance.json"
        in call
        for call in joined_calls
    )
    assert any(
        "restore --source HEAD --worktree --staged -- docs/awf-plans/ws_post.conformance.json"
        in call
        for call in joined_calls
    )
    # No staging or committing of the AWF artifact.
    assert all("add" not in call.args for call in runner.calls)
    assert all("commit" not in call.args for call in runner.calls)


@pytest.mark.unit
async def test_satisfied_post_validation_conformance_report_unlinks_when_head_restore_fails(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6KKSZU regression: if the staged residue cannot be
    reconciled from HEAD, the executor must fall back to unlink() so the stale
    report is not published.
    """
    worktree_path = tmp_path / "worktree"
    runner = _GitRestoreFakeRunner(worktree_path)
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    report_file = worktree_path / report_path
    report_file.parent.mkdir(parents=True)
    report_file.write_text(
        '{"status":"needs_iteration","summary":"stale miss","gaps":["fix me"]}',
        encoding="utf-8",
    )
    runner.queue_result(returncode=0, stdout=f" M {report_path.as_posix()}\n")  # before_compare
    runner.queue_result(returncode=0, stdout="fix-pass-head\n")  # before_compare_head
    runner.queue_result(returncode=0, stdout=f" M {report_path.as_posix()}\n")  # after_compare
    runner.queue_result(returncode=0, stdout="")  # committed paths since validated HEAD
    # base_commit restore leaves staged modification.
    runner.queue_result(returncode=0, stdout=f"M  {report_path.as_posix()}\n")
    # Cleanliness check after base_commit restore still sees the staged path.
    runner.queue_result(returncode=0, stdout=f"M  {report_path.as_posix()}\n")
    # HEAD restore fails (or leaves the path dirty).
    runner.queue_result(returncode=128, stdout="", stderr="fatal: could not resolve HEAD\n")
    # Cleanliness check after failed HEAD restore still sees the staged path.
    runner.queue_result(returncode=0, stdout=f"M  {report_path.as_posix()}\n")
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
        base_commit="base-commit-sha",
    )

    assert failure is None
    assert event_markers == ["record"]
    # HEAD restore could not reconcile the index, so the report is unlinked.
    assert not report_file.exists()
    joined_calls = [" ".join(call.args) for call in runner.calls]
    assert any(
        "restore --source HEAD --worktree --staged -- docs/awf-plans/ws_post.conformance.json"
        in call
        for call in joined_calls
    )
    # No staging or committing of the AWF artifact.
    assert all("add" not in call.args for call in runner.calls)
    assert all("commit" not in call.args for call in runner.calls)
