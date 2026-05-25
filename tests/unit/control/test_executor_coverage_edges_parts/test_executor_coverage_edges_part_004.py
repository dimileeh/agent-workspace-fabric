"""Focused branch-coverage tests for executor helper behavior."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.adapters.base import AgentRunError
from awf.common.commands import AsyncioSubprocessRunner, CommandResult, FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor import git_methods as executor_git_methods
from awf.control.executor import helpers as executor_helpers
from awf.control.executor import quality_gates as executor_quality_gates
from awf.control.executor.constants import (
    GIT_OBJECT_MISSING_REASON_CODE,
    GIT_OBJECT_MISSING_RECOVERED_REASON_CODE,
)
from awf.control.executor.git_ops import (
    _GitObjectRecoveryResult,
    _recover_missing_head_from_filesystem,
)
from awf.control.executor.helpers import (
    _apply_baseline_coverage_ratchet,
    _coverage_has_failing_tests,
    _coverage_preserves_below_threshold_baseline,
    _coverage_wrapped_pytest_failure_message,
    _failure_reason_for_phase,
    _format_failing_test_evidence,
    _validation_failure_message,
    _validation_run_coverage_metadata,
    _validation_run_reason_code,
)
from awf.control.quality_gates import PLAN_ONLY_OUTPUT_REASON_CODE
from awf.db.enums import (
    AgentRuntime,
    FailureReason,
    WorkspaceStatus,
)
from awf.db.repositories import (
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.planning import (
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    PLAN_CONFORMANCE_UNSATISFIED,
)
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
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
        executor_git_methods,
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
        executor_git_methods,
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
        executor_git_methods,
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
        executor_git_methods,
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
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="M\0.awf/workspace.yml\0")
    executor = _executor_with_runner(runner, tmp_path)
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
async def test_verify_recovered_post_agent_commit_blocks_protected_rename_source(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    workflow_text = "name: CI\non: [pull_request]\njobs: {}\n"
    runner.queue_result(
        returncode=0,
        stdout="R100\0.github/workflows/ci.yml\0docs/ci.yml\0",
    )
    runner.queue_result(returncode=0)  # cat-file base:.github/workflows/ci.yml
    runner.queue_result(returncode=0, stdout=workflow_text)
    runner.queue_result(returncode=128, stderr="path does not exist in HEAD")
    runner.queue_result(returncode=0)  # ls-tree confirms renamed source is absent from HEAD
    executor = _executor_with_runner(runner, tmp_path)
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]

    assert not await executor._verify_recovered_post_agent_commit(
        workspace_id="ws_recovered_rename_policy",
        worktree_path=tmp_path / "worktree",
        base_commit="a" * 40,
        owned_paths=["src/**"],
        expected_status=WorkspaceStatus.running,
    )

    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    kwargs = executor._mark_failed.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["failure_reason"] == FailureReason.policy_failure
    assert kwargs["reason_code"] == "QUALITY_GATE_POLICY_CHANGED"
    assert ".github/workflows/ci.yml" in kwargs["message"]
    assert "--name-status" in runner.calls[0].args
    assert "-z" in runner.calls[0].args


@pytest.mark.unit
async def test_verify_recovered_post_agent_commit_rejects_empty_recovery(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")
    executor = _executor_with_runner(runner, tmp_path)
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
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="M\0docs/awf-plans/ws_plan_only.md\0")
    executor = _executor_with_runner(runner, tmp_path)
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
    runner.queue_result(returncode=0, stdout="M\0src/app.py\0")
    runner.queue_result(returncode=1, stderr="not an ancestor")
    executor = _executor_with_runner(runner, tmp_path)
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
    runner.queue_result(returncode=0, stdout="M\0src/app.py\0")
    runner.queue_result(returncode=0)
    executor = _executor_with_runner(runner, tmp_path)
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
async def test_committed_quality_gate_guard_blocks_protected_rename_source(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    workflow_text = "name: CI\non: [pull_request]\njobs: {}\n"
    runner.queue_result(
        returncode=0,
        stdout="R100\0.github/workflows/ci.yml\0docs/ci.yml\0",
    )
    runner.queue_result(returncode=0)  # cat-file base:.github/workflows/ci.yml
    runner.queue_result(returncode=0, stdout=workflow_text)
    runner.queue_result(returncode=128, stderr="path does not exist in HEAD")
    runner.queue_result(returncode=0)  # ls-tree confirms renamed source is absent from HEAD
    executor = _executor_with_runner(runner, tmp_path)
    executor._mark_failed = AsyncMock()  # type: ignore[method-assign]

    blocked = await executor._fail_if_protected_quality_gate_committed_output(
        workspace_id="ws_rename_policy",
        worktree_path=tmp_path / "worktree",
        base_commit="a" * 40,
        owned_paths=["src/**"],
        expected_status=WorkspaceStatus.running,
    )

    assert blocked is True
    executor._mark_failed.assert_awaited_once()  # type: ignore[attr-defined]
    kwargs = executor._mark_failed.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["failure_reason"] == FailureReason.policy_failure
    assert kwargs["reason_code"] == "QUALITY_GATE_POLICY_CHANGED"
    assert ".github/workflows/ci.yml" in kwargs["message"]
    assert "--name-status" in runner.calls[0].args
    assert "-z" in runner.calls[0].args


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
async def test_staged_protected_file_diffs_use_base_ref_for_old_side(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0)  # cat-file base-sha:.github/workflows/ci.yml
    runner.queue_result(returncode=0, stdout="base workflow\n")
    runner.queue_result(returncode=0)  # cat-file :.github/workflows/ci.yml
    runner.queue_result(returncode=0, stdout="staged workflow\n")
    executor = _executor_with_runner(runner, tmp_path)

    diffs = await executor._protected_file_diffs_for_staged_paths(
        worktree_path=tmp_path / "worktree",
        base_ref="base-sha",
        changed_paths=[".github/workflows/ci.yml"],
    )

    assert diffs[".github/workflows/ci.yml"].old_text == "base workflow\n"
    assert diffs[".github/workflows/ci.yml"].new_text == "staged workflow\n"
    assert [call.args[call.args.index("-C") + 2 :] for call in runner.calls] == [
        ["cat-file", "-e", "base-sha:.github/workflows/ci.yml"],
        ["show", "base-sha:.github/workflows/ci.yml"],
        ["cat-file", "-e", ":.github/workflows/ci.yml"],
        ["show", ":.github/workflows/ci.yml"],
    ]


@pytest.mark.unit
async def test_staged_protected_file_diffs_treat_deleted_index_path_as_absent(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0)  # cat-file base-sha:pyproject.toml
    runner.queue_result(returncode=0, stdout='[project]\nname = "demo"\n')
    runner.queue_result(returncode=128, stderr="fatal: path 'pyproject.toml' is not in the index")
    runner.queue_result(returncode=0)  # ls-files confirms deleted index path is absent
    executor = _executor_with_runner(runner, tmp_path)

    diffs = await executor._protected_file_diffs_for_staged_paths(
        worktree_path=tmp_path / "worktree",
        base_ref="base-sha",
        changed_paths=["pyproject.toml"],
    )

    assert diffs["pyproject.toml"].old_text == '[project]\nname = "demo"\n'
    assert diffs["pyproject.toml"].new_text is None
    assert [call.args[call.args.index("-C") + 2 :] for call in runner.calls] == [
        ["cat-file", "-e", "base-sha:pyproject.toml"],
        ["show", "base-sha:pyproject.toml"],
        ["cat-file", "-e", ":pyproject.toml"],
        ["ls-files", "--stage", "-z", "--", ":(literal)pyproject.toml"],
    ]


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
    failure = executor_helpers._PlanningRunFailure(  # noqa: SLF001
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

    result = executor_helpers._post_validation_conformance_fix_result(  # noqa: SLF001
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

    details = executor_quality_gates._post_validation_conformance_agent_failure_details(  # noqa: SLF001
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
    stdout_only_details = executor_quality_gates._post_validation_conformance_agent_failure_details(  # noqa: SLF001
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
