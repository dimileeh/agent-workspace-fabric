"""Regression tests for PR monitor pre-push validation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.github_client import RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import ValidationRunRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.profiles.models import WorkspaceProfile
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner.remote_ops import _git_push_failure_outcome, _GitPushResult
from awf.runtime.validation_types import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _FakeValidation:
    """Minimal validation runner used to script pass/fail outcomes."""

    def __init__(
        self,
        *results: ValidationResult | Exception,
        coverage_result: ValidationCoverageResult | None = None,
    ) -> None:
        """Store queued validation results for later retrieval."""
        self.results = list(results)
        self.coverage_result = coverage_result
        self.calls: list[dict[str, object]] = []
        self.coverage_calls: list[dict[str, object]] = []

    async def run_profile_phases(self, **kwargs: object) -> ValidationResult:
        """Return the next queued validation outcome."""
        self.calls.append(dict(kwargs))
        if not self.results:
            raise AssertionError("validation called more times than expected")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def run_profile_coverage(self, **kwargs: object) -> ValidationCoverageResult | None:
        """Stub profile coverage step; included for interface compatibility."""
        self.coverage_calls.append(dict(kwargs))
        return self.coverage_result


def _command_result(
    tmp_path: Path,
    *,
    ok: bool,
    reason_code: str | None = None,
    command: str = "pytest -q",
    returncode: int | None = None,
    artifact_name: str | None = None,
) -> ValidationCommandResult:
    """Build a deterministic validation command result with local artifact paths."""
    if reason_code is None:
        reason_code = "VALIDATION_OK" if ok else "PYTEST_TEST_FAILURE"
    label = artifact_name or ("ok" if ok else command.replace("/", "_").replace(" ", "_"))
    stdout_path = tmp_path / f"{label}.stdout"
    stderr_path = tmp_path / f"{label}.stderr"
    stdout_path.write_text("passed\n" if ok else "failed\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    return ValidationCommandResult(
        command=command,
        returncode=0 if ok else (returncode if returncode is not None else 1),
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        reason_code=reason_code,
    )


def _validation_result(
    tmp_path: Path,
    *,
    ok: bool,
    reason_code: str | None = None,
    command: str = "pytest -q",
    returncode: int | None = None,
    artifact_name: str | None = None,
) -> ValidationResult:
    """Wrap one command result into a single-command validation result."""
    return ValidationResult(
        commands=[
            _command_result(
                tmp_path,
                ok=ok,
                reason_code=reason_code,
                command=command,
                returncode=returncode,
                artifact_name=artifact_name,
            )
        ]
    )


def _coverage_result(tmp_path: Path) -> ValidationCoverageResult:
    """Build a successful explicit coverage result for pre-push coverage tests."""
    return ValidationCoverageResult(
        provider="python",
        percent=99.5,
        minimum_percent=99.0,
        enforce=True,
        status="passed",
        reason_code="COVERAGE_OK",
        command_result=_command_result(tmp_path, ok=True, reason_code="COVERAGE_OK"),
        gaps=[{"path": "src/awf/runtime/pr_monitor_runner/pre_push_validation.py"}],
    )


def _failing_coverage_result(tmp_path: Path) -> ValidationCoverageResult:
    """Build a failed coverage result whose command exited successfully."""
    return ValidationCoverageResult(
        provider="python",
        percent=98.5,
        minimum_percent=99.0,
        enforce=True,
        status="failed",
        reason_code="COVERAGE_BELOW_THRESHOLD",
        command_result=_command_result(
            tmp_path,
            ok=True,
            reason_code="VALIDATION_OK",
            command="coverage run -m pytest && coverage report",
            artifact_name="coverage_below_threshold",
        ),
        gaps=[{"path": "src/awf/runtime/pr_monitor_runner/pre_push_validation.py"}],
    )


async def _set_resolved_profile(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    include_coverage: bool = False,
) -> None:
    """Attach a simple resolved validation profile to the workspace."""
    profile_payload: dict[str, object] = {
        "name": "test-profile",
        "phases": {"validate": ["pytest -q"]},
    }
    if include_coverage:
        profile_payload["validation"] = {
            "coverage": {
                "minimum_percent": 99.0,
                "command": "coverage run -m pytest && coverage report",
            },
            "strategy": {"final_gate": "coverage"},
        }
    profile = WorkspaceProfile.model_validate(profile_payload)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.resolved_profile = profile.model_dump(mode="json", by_alias=True)
        await session.commit()


async def _seed_monitoring_workspace_without_attempt(
    factory: async_sessionmaker[AsyncSession],
) -> str:
    """Create a monitoring workspace row without a task-attempt record."""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-web.git",
            branch_base="development",
            task_title="monitor test without attempt",
            task_prompt="x",
            agent="claude_code",
            test_commands=["pytest -q"],
            requires_database=False,
            auto_merge=True,
        )
        for target in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.monitoring_pr,
        ):
            await repo.transition(ws, to=target, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.remote_push_branch = ws.branch_name
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/awf/{ws.id}/compose.yml"
        ws.pr_url = "https://github.com/dimileeh/aira-web/pull/42"
        ws.pr_number = 42
        await session.commit()
        return ws.id


async def _validation_runs(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> list[Any]:
    """Return all persisted validation runs for a workspace."""
    async with factory() as session:
        return await ValidationRunRepository(session).list_for_workspace(workspace_id)


@pytest.mark.unit
async def test_pre_push_validation_records_target_head_before_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Persisted validation run should track workspace and target head before push."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "f" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert result.pushed is True
    runs = await _validation_runs(factory, workspace_id)
    pre_push_run = runs[-1]
    assert pre_push_run.workspace_head_sha == local_head
    assert pre_push_run.target_head_sha == local_head
    assert pre_push_run.target_branch == f"awf/{workspace_id}"
    assert pre_push_run.status == "succeeded"


@pytest.mark.unit
async def test_pre_push_validation_failure_does_not_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A validation failure should block raw push and expose a validation reason."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=0,
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=False))  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_FAILED"
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]
    assert result.details is not None
    assert result.details["validation_reason_code"] == "PYTEST_TEST_FAILURE"
    assert result.details["failing_command"] == "pytest -q"
    assert result.details["failing_returncode"] == 1


@pytest.mark.unit
async def test_pre_push_validation_toolchain_missing_bypasses_fix_pass(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pure command-not-found validation failure should not spend fix-pass budget."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "1" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    adapter = FakeAdapter()
    validation = _FakeValidation(
        _validation_result(
            tmp_path,
            ok=False,
            command="ruff check .",
            returncode=127,
            reason_code="COMMAND_FAILED",
            artifact_name="ruff_missing",
        )
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    async def _unexpected_commit(**_kwargs: object) -> bool:
        """Fail loudly if a toolchain-missing failure reaches the fix-pass commit step."""
        pytest.fail("toolchain-missing validation must not run a fix pass")

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _unexpected_commit)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.reason_code == "PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING"
    assert len(validation.calls) == 1
    assert adapter.calls == []
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]
    assert result.details is not None
    assert result.details["reason_code"] == "PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING"
    assert result.details["validation_reason_code"] == "COMMAND_FAILED"
    assert result.details["failing_command"] == "ruff check ."
    assert result.details["failing_returncode"] == 127
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].reason_code == "PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING"


@pytest.mark.unit
async def test_pre_push_validation_mixed_127_prefers_real_failure_for_fix_pass(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed 127 and non-127 failures should surface the genuine validation failure."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'2' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'3' * 40}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed mixed validation\n")
    mixed = ValidationResult(
        commands=[
            _command_result(
                tmp_path,
                ok=False,
                command="ruff check .",
                returncode=127,
                reason_code="COMMAND_FAILED",
                artifact_name="mixed_ruff_missing",
            ),
            _command_result(
                tmp_path,
                ok=False,
                command="pytest -q",
                returncode=1,
                reason_code="PYTEST_TEST_FAILURE",
                artifact_name="mixed_pytest_failure",
            ),
        ]
    )
    validation = _FakeValidation(mixed, _validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    async def _commit_dirty(**_kwargs: object) -> bool:
        """Report a successful synthetic fix commit."""
        return True

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert result.pushed is True
    assert len(validation.calls) == 2
    assert len(adapter.calls) == 1
    assert "Failing command: pytest -q" in adapter.calls[0]
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-2].reason_code == "PYTEST_TEST_FAILURE"


@pytest.mark.unit
async def test_pre_push_validation_without_task_attempt_fails_without_persisting_run(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A missing task attempt should fail explicitly without creating an invisible run."""
    workspace_id = await _seed_monitoring_workspace_without_attempt(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "b" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.reason_code == "PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED"
    assert result.details is not None
    assert result.details["workspace_head_sha"] == local_head
    assert "task attempt" in result.stderr
    assert validation.calls == []
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]
    assert await _validation_runs(factory, workspace_id) == []


@pytest.mark.unit
async def test_pre_push_validation_missing_workspace_blocks_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A deleted workspace should fail as infrastructure before push or validation."""
    workspace_id = "ws_missing_pre_push_validation"
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    validation = _FakeValidation()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED"
    assert "workspace disappeared" in result.stderr
    assert validation.calls == []
    assert cmd.calls == []


@pytest.mark.unit
async def test_pre_push_validation_missing_head_blocks_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A failed local HEAD lookup should block push before creating a validation run."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    run_ids_before = [run.id for run in await _validation_runs(factory, workspace_id)]
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stdout="", stderr="not a git checkout")
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED"
    assert "could not capture local HEAD" in result.stderr
    assert validation.calls == []
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]
    run_ids_after = [run.id for run in await _validation_runs(factory, workspace_id)]
    assert run_ids_after == run_ids_before


@pytest.mark.unit
async def test_pre_push_validation_cleanup_error_records_failed_run(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Cleanup failures should persist cleanup detail but classify push as infrastructure."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "c" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    validation = _FakeValidation(
        ComposeExecCleanupError(
            invocation_id="awf_pre_push_cleanup",
            source="validation",
            label="pytest",
            message="tagged process still running",
        )
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED"
    assert _git_push_failure_outcome(result) == "pre_push_validation_failed"
    assert result.details is not None
    assert result.details["reason_code"] == "PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED"
    assert result.details["workspace_head_sha"] == local_head
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].status == "failed"
    assert runs[-1].reason_code == "EXEC_PROCESS_CLEANUP_FAILED"
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_pre_push_validation_unexpected_exception_records_infrastructure_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Unexpected validation errors should finish the run with infrastructure failure."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "d" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    validation = _FakeValidation(RuntimeError("validation runner exploded"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED"
    assert "unexpected error during PR monitor pre-push validation" in result.stderr
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].status == "failed"
    assert runs[-1].reason_code == "PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED"
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_pre_push_validation_runs_profile_coverage_before_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Profile coverage should run after passing phases and persist before push."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id, include_coverage=True)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "e" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    coverage = _coverage_result(tmp_path)
    validation = _FakeValidation(
        _validation_result(tmp_path, ok=True),
        coverage_result=coverage,
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert result.pushed is True
    assert len(validation.coverage_calls) == 1
    assert validation.coverage_calls[0]["phase"] == "coverage"
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].status == "succeeded"
    assert runs[-1].coverage is not None
    assert runs[-1].coverage["percent"] == 99.5
    assert runs[-1].coverage["reason_code"] == "COVERAGE_OK"


@pytest.mark.unit
async def test_pre_push_validation_coverage_failure_persists_coverage_reason_code(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Coverage policy failures should not be reported as successful commands."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id, include_coverage=True)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "6" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    coverage = _failing_coverage_result(tmp_path)
    validation = _FakeValidation(
        _validation_result(tmp_path, ok=True),
        coverage_result=coverage,
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=0,
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.reason_code == "PRE_PUSH_VALIDATION_FAILED"
    assert result.stderr.endswith("COVERAGE_BELOW_THRESHOLD")
    assert result.details is not None
    assert result.details["validation_reason_code"] == "COVERAGE_BELOW_THRESHOLD"
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].status == "failed"
    assert runs[-1].reason_code == "COVERAGE_BELOW_THRESHOLD"
    assert runs[-1].coverage is not None
    assert runs[-1].coverage["reason_code"] == "COVERAGE_BELOW_THRESHOLD"


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_revalidates_before_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair passes should re-run validation before allowing push."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    first_head = "b" * 40
    fixed_head = "c" * 40
    cmd.queue_result(returncode=0, stdout=f"{first_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fixed_head}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed validation\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False),
        _validation_result(tmp_path, ok=True),
    )
    committed: list[str] = []

    async def _commit_dirty(**kwargs: object) -> bool:
        """Record a synthetic commit and return a successful outcome."""
        committed.append(str(kwargs["message"]))
        return True

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert committed == [f"awf: pre-push validation fix for {workspace_id}"]
    assert len(adapter.calls) == 1
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].target_head_sha == fixed_head


@pytest.mark.unit
async def test_pre_push_validation_fix_prompt_includes_underlying_reason_code(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix prompts should include the first failing validation reason code."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    first_head = "d" * 40
    fixed_head = "e" * 40
    cmd.queue_result(returncode=0, stdout=f"{first_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fixed_head}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed validation\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(
            tmp_path,
            ok=False,
            reason_code="PYTEST_TEST_FAILURE",
        ),
        _validation_result(tmp_path, ok=True),
    )
    committed: list[str] = []

    async def _commit_dirty(**kwargs: object) -> bool:
        """Record a synthetic commit and return a successful outcome."""
        committed.append(str(kwargs["message"]))
        return True

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert committed == [f"awf: pre-push validation fix for {workspace_id}"]
    assert len(adapter.calls) == 1
    assert "Reason code: PYTEST_TEST_FAILURE" in adapter.calls[0]


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_commits_agent_failure_evidence(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-zero fix agents should preserve evidence and still commit attempted fixes."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    first_head = "f" * 40
    fixed_head = "1" * 40
    cmd.queue_result(returncode=0, stdout=f"{first_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fixed_head}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    adapter = FakeAdapter()
    adapter.queue(stdout="agent stdout", stderr="agent stderr", returncode=2)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
        _validation_result(tmp_path, ok=True),
    )
    committed: list[dict[str, object]] = []

    async def _commit_dirty(**kwargs: object) -> bool:
        """Record the attempted fix commit and report success."""
        committed.append(kwargs)
        return True

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert result.pushed is True
    assert len(adapter.calls) == 1
    assert committed[0]["message"] == f"awf: pre-push validation fix for {workspace_id}"
    assert "agent stdout" in "\n".join(committed[0]["command_evidence"])  # type: ignore[index]
    assert "agent stderr" in "\n".join(committed[0]["command_evidence"])  # type: ignore[index]


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_cleanup_failure_blocks_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Fix-pass cleanup failures should surface as fix failures and avoid push."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'2' * 40}\n")
    adapter = FakeAdapter()
    adapter.queue(
        exc=ComposeExecCleanupError(
            invocation_id="awf_pre_push_fix_cleanup",
            source="agent",
            label="monitor-pre-push-validation-fix",
            message="tagged process still running",
        )
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
    )

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_FIX_FAILED"
    assert "fix pass failed" in result.stderr
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_commit_fail_returns_fix_failed_reason_code(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed fix commit attempts should surface ``PRE_PUSH_VALIDATION_FIX_FAILED``."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'f' * 40}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
    )

    async def _no_commit(**_kwargs: object) -> bool:
        """Return a failed commit result for the fix-pass test."""
        return False

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _no_commit)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_FIX_FAILED"
    assert result.details is not None
    assert result.details["validation_reason_code"] == "PYTEST_TEST_FAILURE"
    assert result.details["failing_command"] == "pytest -q"
    assert result.details["failing_returncode"] == 1
    assert "fix pass failed" in result.stderr


@pytest.mark.unit
async def test_comment_repair_uses_validated_push_and_does_not_resolve_on_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review-thread repair must route through validated push when a fix fails."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_validation",
        path="src/foo.py",
        line=1,
        body_excerpt="please fix",
        author="reviewer",
    )
    calls: list[str] = []
    state = MonitorState()

    async def _no_dirty(**_kwargs: object) -> None:
        """Indicate there is no pre-existing dirty worktree state."""

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        """Return a fixed starting head for the repair operation."""
        return ("start", None)

    async def _address(**_kwargs: object) -> str:
        """Return a synthetic fixed commit id after thread addressing."""
        return "fix_committed"

    async def _clean_status(**_kwargs: object) -> object:
        """Return a clean PR status used to continue the repair loop."""
        return PRStatus(
            number=42,
            head_sha="start",
            mergeable=MergeableState.MERGEABLE,
            check_state=CheckState.SUCCESS,
            unresolved_inline_threads=(),
            unresolved_review_comments=(),
            base_behind_count=0,
            merge_state_status=MergeStateStatus.CLEAN,
        )

    async def _no_block(**_kwargs: object) -> None:
        """Allow repair flow to bypass protected-scope checks."""

    async def _validated(**_kwargs: object) -> _GitPushResult:
        """Simulate a validated-push failure and record the invocation."""
        calls.append("validated")
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="validation failed",
            reason_code="PRE_PUSH_VALIDATION_FAILED",
        )

    async def _unexpected_push(**_kwargs: object) -> _GitPushResult:
        """Fail loudly if raw push is called in this repair path."""
        pytest.fail("comment repair must not call raw push")

    async def _unexpected_resolve(**_kwargs: object) -> None:
        """Fail loudly if threads are resolved before validation succeeds."""
        pytest.fail("threads must not be resolved when validation blocks push")

    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_address_thread", _address)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _clean_status)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated)
    monkeypatch.setattr(runner, "_git_push_result", _unexpected_push)
    monkeypatch.setattr(runner._deps.gh, "resolve_thread", _unexpected_resolve)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="start",
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.reason_code == "PRE_PUSH_VALIDATION_FAILED"
    assert calls == ["validated"]
    assert "T_validation" not in state.threads_addressed_ids


@pytest.mark.unit
async def test_ci_repair_uses_validated_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI-repair flow should use validated push and avoid raw push."""
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed\n")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    calls: list[str] = []

    async def _no_dirty(**_kwargs: object) -> None:
        """Indicate there is no pre-existing dirty worktree state."""

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)

    async def _provider_allows_cli(*_args: object) -> bool:
        """Return a fixed provider policy for CLI suppression in repairs."""
        return False

    monkeypatch.setattr(runner, "_provider_recovery_suppresses_cli", _provider_allows_cli)

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        """Return a fixed starting head for CI repair simulation."""
        return ("start", None)

    async def _commit(**_kwargs: object) -> bool:
        """Return a successful synthetic commit result."""
        return True

    async def _no_block(**_kwargs: object) -> None:
        """Allow the CI repair flow to skip protected-scope checks."""

    async def _validated(**_kwargs: object) -> _GitPushResult:
        """Simulate a validated push success and track invocation."""
        calls.append("validated")
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _unexpected_push(**_kwargs: object) -> _GitPushResult:
        """Fail loudly if raw push is called in this repair path."""
        pytest.fail("CI repair must not call raw push")

    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated)
    monkeypatch.setattr(runner, "_git_push_result", _unexpected_push)

    result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(
            CheckFailure(
                name="ci",
                conclusion="FAILURE",
                log_excerpt="failed",
            ),
        ),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
        state=MonitorState(),
    )

    assert result.failed is False
    assert calls == ["validated"]


@pytest.mark.unit
async def test_ci_repair_owned_path_lookup_failure_stops_before_agent(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI repair should not build prompts with fallback-empty owned paths."""
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _no_dirty(**_kwargs: object) -> None:
        """Indicate there is no pre-existing dirty worktree state."""

    async def _provider_allows_cli(*_args: object) -> bool:
        """Return a fixed provider policy for CLI suppression in repairs."""
        return False

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        """Return a fixed starting head for CI repair simulation."""
        return ("start", None)

    def _broken_session_factory() -> object:
        raise TypeError("session factory unavailable")

    async def _unexpected_commit(**_kwargs: object) -> bool:
        """Fail loudly if repair reaches commit after owned-path lookup fails."""
        pytest.fail("CI repair must not commit after owned-path lookup failure")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)
    monkeypatch.setattr(runner, "_provider_recovery_suppresses_cli", _provider_allows_cli)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner._deps, "session_factory", _broken_session_factory)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _unexpected_commit)

    with pytest.raises(TypeError, match="session factory unavailable"):
        await runner._run_ci_fix(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            failures=(
                CheckFailure(
                    name="ci",
                    conclusion="FAILURE",
                    log_excerpt="failed",
                ),
            ),
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            workspace_id=workspace_id,
            remote_branch=f"awf/{workspace_id}",
            state=MonitorState(),
        )

    assert adapter.calls == []


@pytest.mark.unit
async def test_sync_base_uses_validated_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync-base recovery should also rely on validated push."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # merge --abort
    cmd.queue_result(returncode=0)  # merge --no-edit origin/development
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()
    calls: list[str] = []

    async def _fetch_base(**_kwargs: object) -> None:
        """Mock base sync fetching for sync-base repair."""

    async def _no_block(**_kwargs: object) -> None:
        """Allow the sync-base flow to skip protected-scope checks."""

    async def _validated(**_kwargs: object) -> _GitPushResult:
        """Simulate validated push success and record that it was used."""
        assert "state" in _kwargs
        assert _kwargs["state"] is state
        calls.append("validated")
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _unexpected_push(**_kwargs: object) -> _GitPushResult:
        """Fail loudly if raw push is called in sync-base repair."""
        pytest.fail("sync-base repair must not call raw push")

    monkeypatch.setattr(runner, "_fetch_base", _fetch_base)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated)
    monkeypatch.setattr(runner, "_git_push_result", _unexpected_push)

    result = await runner._run_sync_base(
        workspace_id=workspace_id,
        state=state,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert calls == ["validated"]
