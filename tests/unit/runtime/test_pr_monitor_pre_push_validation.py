"""Regression tests for PR monitor pre-push validation."""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED, ComposeExecCleanupError
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
from awf.runtime.pr_monitor_runner import pre_push_validation as pre_push_validation_module
from awf.runtime.pr_monitor_runner.remote_ops import (
    _git_push_failure_outcome,
    _GitPushResult,
)
from awf.runtime.validation_types import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
)
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
    ValidationWorktreeCheck,
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


@pytest.mark.unit
def test_pre_push_validation_structural_helpers_are_single_source() -> None:
    """Keep pre-push validation helper definitions and retry flow single-sourced."""
    source_path = Path(pre_push_validation_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    helper_names = {
        "_failed_pre_push_commands",
        "_first_real_pre_push_failure",
        "_first_failure_outside_collected_failures",
        "_first_real_pre_push_failure_for_result",
        "_first_real_pre_push_failure_from_failures",
        "_pure_toolchain_missing_failure",
        "_pure_toolchain_missing_failure_for_result",
        "_pure_toolchain_missing_failure_from_failures",
        "_preferred_pre_push_failure",
        "_preferred_pre_push_failure_from_failures",
        "_pre_push_validation_reason_code_for_preferred_failure",
        "_pre_push_validation_reason_code",
    }
    top_level_functions = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]

    for helper_name in helper_names:
        assert top_level_functions.count(helper_name) == 1

    retry_function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_run_pre_push_validation_with_fix_passes"
    )
    assert isinstance(retry_function.body[-1], ast.While)
    assert isinstance(retry_function.body[-1].test, ast.Constant)
    assert retry_function.body[-1].test.value is True


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


class _CommandlessFailureValidationResult(ValidationResult):
    """Validation result that exposes a first failure outside command records."""

    _first_failure: ValidationCommandResult

    def __init__(self, first_failure: ValidationCommandResult) -> None:
        super().__init__(commands=[])
        object.__setattr__(self, "_first_failure", first_failure)

    @property
    def all_passed(self) -> bool:
        return False

    @property
    def first_failure(self) -> ValidationCommandResult | None:
        return self._first_failure


class _OverriddenFirstFailureValidationResult(ValidationResult):
    """Validation result whose provider-level first failure differs from commands."""

    _first_failure: ValidationCommandResult

    def __init__(
        self,
        *,
        commands: list[ValidationCommandResult],
        first_failure: ValidationCommandResult,
    ) -> None:
        super().__init__(commands=commands)
        object.__setattr__(self, "_first_failure", first_failure)

    @property
    def all_passed(self) -> bool:
        return False

    @property
    def first_failure(self) -> ValidationCommandResult | None:
        return self._first_failure


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


def _provider_coverage_failure_without_command() -> ValidationCoverageResult:
    """Build a failed provider result without an associated command record."""
    return ValidationCoverageResult(
        provider="python",
        percent=None,
        minimum_percent=99.0,
        enforce=True,
        status="failed",
        reason_code="COVERAGE_PROVIDER_FAILED",
        provider_failure_evidence=["coverage provider did not produce totals"],
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


def _mark_git_worktree(worktree: Path) -> None:
    """Make a lightweight temp directory look like a git worktree to guards."""
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake.git\n", encoding="utf-8")


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
    monkeypatch: pytest.MonkeyPatch,
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

    async def _unexpected_validation_commands(_self: Any, **_kwargs: object) -> tuple[str, ...]:
        """Fail loudly if disabled fix passes still load fix-prompt command context."""
        pytest.fail("validation commands must not load when fix passes are disabled")

    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_commands",
        _unexpected_validation_commands,
    )

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
def test_pre_push_failure_helpers_prefer_real_migration_and_coverage_commands(
    tmp_path: Path,
) -> None:
    """Direct helper coverage for migration and coverage command failures."""
    migration_failure = _command_result(
        tmp_path,
        ok=False,
        command="alembic upgrade head",
        returncode=1,
        reason_code="MIGRATION_FAILED",
        artifact_name="migration_failed",
    )
    coverage_command_failure = _command_result(
        tmp_path,
        ok=False,
        command="coverage report",
        returncode=127,
        reason_code="COMMAND_FAILED",
        artifact_name="coverage_missing",
    )
    coverage_failure = ValidationCoverageResult(
        provider="python",
        percent=None,
        minimum_percent=99.0,
        enforce=True,
        status="failed",
        reason_code="COVERAGE_COMMAND_FAILED",
        command_result=coverage_command_failure,
    )
    result = ValidationResult(
        migration=migration_failure,
        commands=[],
        coverage=coverage_failure,
    )

    failures = pre_push_validation_module._failed_pre_push_commands(result)

    assert failures == (migration_failure, coverage_command_failure)
    assert pre_push_validation_module._first_real_pre_push_failure(result) is (migration_failure)
    assert pre_push_validation_module._pure_toolchain_missing_failure(result) is None
    assert pre_push_validation_module._pre_push_validation_reason_code(result) == "MIGRATION_FAILED"


@pytest.mark.unit
def test_pre_push_failure_helpers_identify_pure_coverage_toolchain_failure(
    tmp_path: Path,
) -> None:
    """A coverage command returning 127 is still a pure toolchain-missing failure."""
    coverage_command_failure = _command_result(
        tmp_path,
        ok=False,
        command="coverage report",
        returncode=127,
        reason_code="COMMAND_FAILED",
        artifact_name="coverage_toolchain_missing",
    )
    result = ValidationResult(
        coverage=ValidationCoverageResult(
            provider="python",
            percent=None,
            minimum_percent=99.0,
            enforce=True,
            status="failed",
            reason_code="COVERAGE_COMMAND_FAILED",
            command_result=coverage_command_failure,
        )
    )

    assert pre_push_validation_module._first_real_pre_push_failure(result) is None
    assert pre_push_validation_module._pure_toolchain_missing_failure(result) is (
        coverage_command_failure
    )
    assert (
        pre_push_validation_module._pre_push_validation_reason_code(result)
        == "COVERAGE_COMMAND_FAILED"
    )


@pytest.mark.unit
def test_pre_push_failure_helpers_prefer_non_127_first_failure_over_command_127(
    tmp_path: Path,
) -> None:
    """Provider failures must not be hidden by all-127 command records."""
    command_failure = _command_result(
        tmp_path,
        ok=False,
        command="ruff check .",
        returncode=127,
        reason_code="COMMAND_FAILED",
        artifact_name="ruff_missing_with_provider_failure",
    )
    provider_failure = _command_result(
        tmp_path,
        ok=False,
        command="coverage provider",
        returncode=2,
        reason_code="COVERAGE_PROVIDER_FAILED",
        artifact_name="coverage_provider_failed",
    )
    result = _OverriddenFirstFailureValidationResult(
        commands=[command_failure],
        first_failure=provider_failure,
    )

    assert pre_push_validation_module._first_real_pre_push_failure(result) is provider_failure
    assert pre_push_validation_module._pure_toolchain_missing_failure(result) is None
    assert pre_push_validation_module._preferred_pre_push_failure(result) is provider_failure
    assert (
        pre_push_validation_module._pre_push_validation_reason_code(result)
        == "COVERAGE_PROVIDER_FAILED"
    )


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

    async def _unexpected_validation_commands(_self: Any, **_kwargs: object) -> tuple[str, ...]:
        """Fail loudly if terminal validation still loads fix-prompt command context."""
        pytest.fail("toolchain-missing validation must not load fix-pass commands")

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _unexpected_commit)
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_commands",
        _unexpected_validation_commands,
    )

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
async def test_pre_push_validation_commandless_toolchain_missing_bypasses_fix_pass(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command-less 127 first failure should still be terminal toolchain-missing."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'4' * 40}\n")
    first_failure = _command_result(
        tmp_path,
        ok=False,
        command="coverage provider",
        returncode=127,
        reason_code="COMMAND_FAILED",
        artifact_name="commandless_provider_missing",
    )
    validation = _FakeValidation(_CommandlessFailureValidationResult(first_failure))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    async def _unexpected_validation_commands(_self: Any, **_kwargs: object) -> tuple[str, ...]:
        """Fail loudly if command-less toolchain-missing loads fix-prompt context."""
        pytest.fail("command-less toolchain-missing validation must not run a fix pass")

    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_commands",
        _unexpected_validation_commands,
    )

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
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]
    assert result.details is not None
    assert result.details["validation_reason_code"] == "COMMAND_FAILED"
    assert result.details["failing_command"] == "coverage provider"
    assert result.details["failing_returncode"] == 127
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].reason_code == "PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING"


@pytest.mark.unit
async def test_pre_push_validation_collects_failed_commands_once_for_reason_codes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reason-code decisions should share one failed-command traversal."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'7' * 40}\n")
    validation = _FakeValidation(
        _validation_result(
            tmp_path,
            ok=False,
            command="ruff check .",
            returncode=127,
            reason_code="COMMAND_FAILED",
            artifact_name="single_scan_ruff_missing",
        )
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
    original_failed_commands = pre_push_validation_module._failed_pre_push_commands
    failed_command_calls = 0

    def _count_failed_commands(
        result: ValidationResult,
    ) -> tuple[ValidationCommandResult, ...]:
        nonlocal failed_command_calls
        failed_command_calls += 1
        return original_failed_commands(result)

    monkeypatch.setattr(
        pre_push_validation_module,
        "_failed_pre_push_commands",
        _count_failed_commands,
    )

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.passed is False
    assert result.reason_code == "PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING"
    assert result.validation_reason_code == "COMMAND_FAILED"
    assert failed_command_calls == 1


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
async def test_pre_push_validation_cleanup_error_preserves_compose_exec_context_on_cleanup_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Cleanup failures should preserve compose-exec cleanup context for triage."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    local_head = "c" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")  # rev-parse HEAD
    cmd.queue_result(returncode=0, stdout="")  # pre-push clean check
    cmd.queue_result(returncode=0, stdout=" M apps/console/next-env.d.ts\n")  # cleanup check
    cmd.queue_result(returncode=1, stderr="restore failed")  # restore
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
    assert result.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert result.details is not None
    assert result.details["compose_exec_reason_code"] == EXEC_PROCESS_CLEANUP_FAILED
    assert result.details["compose_exec_source"] == "validation"
    assert result.details["compose_exec_label"] == "pytest"
    assert result.details["compose_exec_invocation_id"] == "awf_pre_push_cleanup"
    assert "tagged process still running" in str(result.details["compose_exec_message"])
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].status == "failed"
    assert runs[-1].reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_pre_push_validation_unexpected_exception_preserves_context_on_cleanup_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Unexpected validation exceptions should preserve exception context when cleanup later fails."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    local_head = "x" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")  # rev-parse HEAD
    cmd.queue_result(returncode=0, stdout="")  # pre-push clean check
    cmd.queue_result(returncode=0, stdout=" M apps/console/next-env.d.ts\n")  # cleanup check
    cmd.queue_result(returncode=1, stderr="restore failed")  # restore
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
    assert result.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert result.details is not None
    assert result.details["unexpected_exception_type"] == "RuntimeError"
    assert "validation runner exploded" in str(result.details["unexpected_exception_message"])
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].status == "failed"
    assert runs[-1].reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
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
async def test_pre_push_validation_tracked_side_effect_after_validation_cleans_before_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Validation-generated tracked dirt should be cleaned up before push."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    local_head = "a" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")  # rev-parse HEAD
    cmd.queue_result(returncode=0, stdout="")  # clean before validation
    cmd.queue_result(
        returncode=0,
        stdout=" M apps/console/next-env.d.ts\n",
    )  # validation side effect
    cmd.queue_result(returncode=0)  # restore tracked side effect
    cmd.queue_result(returncode=0, stdout="")  # clean after cleanup
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")  # rev-parse HEAD@{awf}
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")  # rev-parse HEAD after cleanup
    cmd.queue_result(returncode=0, stdout="", stderr="")  # git push
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
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(
        f"restore --source {local_head} --staged --worktree -- apps/console/next-env.d.ts" in call
        for call in joined_calls
    )
    assert any("push" in call for call in joined_calls)


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
    assert "failing_command" not in result.details
    assert "failing_returncode" not in result.details
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].status == "failed"
    assert runs[-1].reason_code == "COVERAGE_BELOW_THRESHOLD"
    assert runs[-1].coverage is not None
    assert runs[-1].coverage["reason_code"] == "COVERAGE_BELOW_THRESHOLD"


@pytest.mark.unit
async def test_pre_push_validation_pre_existing_dirty_blocks_before_validation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Pre-existing dirt must fail before running validation or pushing."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    local_head = "b" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout=" M apps/console/next-env.d.ts\n")
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
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert validation.calls == []
    assert result.details is not None
    assert result.details["paths"] == ["apps/console/next-env.d.ts"]
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_pre_push_validation_coverage_provider_failure_without_command_skips_fix_pass(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Coverage provider failures without command records cannot run a fix pass."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'8' * 40}\n")
    validation = _FakeValidation(
        ValidationResult(coverage=_provider_coverage_failure_without_command())
    )
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
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
    assert result.reason_code == "PRE_PUSH_VALIDATION_FAILED"
    assert result.details is not None
    assert result.details["validation_reason_code"] == "COVERAGE_PROVIDER_FAILED"
    assert "failing_command" not in result.details
    assert "failing_returncode" not in result.details
    assert len(validation.calls) == 1
    assert adapter.calls == []
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_pre_push_validation_coverage_provider_skip_still_pushes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A configured coverage provider may decline to emit a result."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id, include_coverage=True)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'9' * 40}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    validation = _FakeValidation(
        _validation_result(tmp_path, ok=True),
        coverage_result=None,
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
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].status == "succeeded"
    assert runs[-1].coverage is None


@pytest.mark.unit
async def test_pre_push_validation_pre_push_status_check_failure_includes_stderr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Status failures should retain command stderr so operators can diagnose why pre-check failed."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    local_head = "h" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=1, stderr="permission denied (publickey)")
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

    assert result.failed is True
    assert result.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert result.details is not None
    assert result.details["command_stderr"] == "permission denied (publickey)"
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_pre_push_validation_cleanup_failure_blocks_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Cleanup failures must be surfaced before any push attempt."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    local_head = "c" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=" M apps/console/next-env.d.ts\n")
    cmd.queue_result(returncode=1, stderr="restore failed")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
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

    assert result.failed is True
    assert result.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert result.details is not None
    assert result.details["paths"] == ["apps/console/next-env.d.ts"]
    assert result.details["cleanup_command"] == "git restore"
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_failed_pre_push_validation_cleans_before_fix_pass(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed validation pass must not hand dirty validation side effects to the fix agent."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    local_head = "d" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=" M apps/console/next-env.d.ts\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=False))  # type: ignore[assignment]
    fix_called = False

    async def _assert_clean_before_fix(
        _runner: object, **_kwargs: object
    ) -> tuple[bool, str | None]:
        """Assert validation did cleanup worktree state before starting a fix pass."""
        nonlocal fix_called
        fix_called = True
        assert any(
            call.args[-4:]
            == [
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
            ]
            for call in cmd.calls
        )
        return False, None

    monkeypatch.setattr(
        pre_push_validation,
        "_run_pre_push_validation_fix_pass",
        _assert_clean_before_fix,
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
    assert fix_called is True


@pytest.mark.unit
async def test_pre_push_validation_untracked_cleanup_allows_fix_pass(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A failed validation with removable untracked artifacts should still run fix passes."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    local_head = "d" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="?? validation-artifact.log\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
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
    assert result.reason_code == "PRE_PUSH_VALIDATION_FIX_FAILED"
    assert "fix pass failed" in str(result.stderr)
    assert adapter.calls


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_uses_initial_ignored_snapshot_across_retries(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry attempts should reuse the first ignored snapshot instead of recapturing all ignored files."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = "workspace_fix_retry"
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True, exist_ok=True)
    fix_pass_calls: list[dict[str, object]] = []
    validation_calls: list[dict[str, object]] = []
    validation_results = [
        pre_push_validation._PrePushValidationResult(
            passed=False,
            validation_run_id="vr1",
            workspace_head_sha="a" * 40,
            reason_code="PRE_PUSH_VALIDATION_FAILED",
            message="attempt 1 failed",
            validation_reason_code="PYTEST_TEST_FAILURE",
            result=_validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
            ignore_ignored_paths=(),
            ignore_ignored_paths_snapshot=(),
        ),
        pre_push_validation._PrePushValidationResult(
            passed=False,
            validation_run_id="vr2",
            workspace_head_sha="a" * 40,
            reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
            message="attempt 2 failed",
            result=None,
        ),
    ]

    async def _run_pre_push_validation(
        _self: Any,
        *,
        ignore_ignored_paths: tuple[str, ...] | None,
        ignore_all_ignored: bool,
        capture_ignored_paths_snapshot: bool,
        **_kwargs: object,
    ) -> pre_push_validation._PrePushValidationResult:
        validation_calls.append(
            {
                "ignore_ignored_paths": ignore_ignored_paths,
                "ignore_all_ignored": ignore_all_ignored,
                "capture_ignored_paths_snapshot": capture_ignored_paths_snapshot,
            }
        )
        return validation_results.pop(0)

    async def _run_fix_pass(_runner: object, **_kwargs: object) -> tuple[bool, str | None]:
        fix_pass_calls.append(cast(dict[str, object], _kwargs))
        return True, None

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_run_pre_push_validation",
        _run_pre_push_validation,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_run_pre_push_validation_fix_pass",
        _run_fix_pass,
    )

    result = await pre_push_validation._run_pre_push_validation_with_fix_passes(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        remote_url=None,
        state=None,
    )

    assert len(validation_calls) == 2
    assert validation_calls[0]["ignore_all_ignored"] is True
    assert validation_calls[0]["ignore_ignored_paths"] is None
    assert validation_calls[0]["capture_ignored_paths_snapshot"] is True
    assert validation_calls[1]["ignore_all_ignored"] is True
    assert validation_calls[1]["ignore_ignored_paths"] == ()
    assert validation_calls[1]["capture_ignored_paths_snapshot"] is False
    assert len(fix_pass_calls) == 1
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rejects_new_ignored_paths_on_retry(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Newly introduced ignored entries should block retry logic before a fix pass."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = "workspace_fix_retry_new_ignored"
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True, exist_ok=True)
    validation_calls: list[dict[str, object]] = []
    fix_pass_calls: list[dict[str, object]] = []
    validation_results = [
        pre_push_validation._PrePushValidationResult(
            passed=False,
            validation_run_id="vr1",
            workspace_head_sha="a" * 40,
            reason_code="PRE_PUSH_VALIDATION_FAILED",
            message="attempt 1 failed",
            validation_reason_code="PYTEST_TEST_FAILURE",
            result=_validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
            ignore_ignored_paths=(".venv/",),
            ignore_ignored_paths_snapshot=(".venv/existing-artifact.log",),
            ignore_ignored_paths_snapshot_signatures=(
                (".venv/existing-artifact.log", "sig-existing"),
            ),
        ),
        pre_push_validation._PrePushValidationResult(
            passed=False,
            validation_run_id="vr2",
            workspace_head_sha="a" * 40,
            reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
            message="attempt 2 failed",
            result=None,
            ignore_ignored_paths=(".venv/",),
            ignore_ignored_paths_snapshot=(
                ".venv/existing-artifact.log",
                ".venv/new-artifact.log",
            ),
            ignore_ignored_paths_snapshot_signatures=(
                (".venv/existing-artifact.log", "sig-existing"),
                (".venv/new-artifact.log", "sig-new"),
            ),
        ),
    ]

    async def _run_pre_push_validation(
        _self: Any,
        **_kwargs: object,
    ) -> pre_push_validation._PrePushValidationResult:
        validation_calls.append(cast(dict[str, object], _kwargs))
        return validation_results.pop(0)

    async def _run_fix_pass(_runner: object, **kwargs: object) -> tuple[bool, str | None]:
        fix_pass_calls.append(cast(dict[str, object], kwargs))
        return True, None

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_run_pre_push_validation",
        _run_pre_push_validation,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_run_pre_push_validation_fix_pass",
        _run_fix_pass,
    )

    result = await pre_push_validation._run_pre_push_validation_with_fix_passes(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        remote_url=None,
        state=None,
    )

    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert len(validation_calls) == 2
    assert len(fix_pass_calls) == 1


@pytest.mark.unit
async def test_run_pre_push_validation_rejects_new_ignored_entries_before_validation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gained ignored snapshot should fail before any validation command executes."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    local_head = "a" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=False))  # type: ignore[assignment]

    async def _run_pre_push_validation_worktree_check(
        _self: object,
        **_kwargs: object,
    ) -> ValidationWorktreeCheck:
        return ValidationWorktreeCheck(
            clean=True,
            ignored_paths=(".venv/",),
            ignored_paths_snapshot=(
                ".venv/existing-artifact.log",
                ".venv/new-artifact.log",
            ),
            ignored_paths_snapshot_signatures=(
                (".venv/existing-artifact.log", "sig-existing"),
                (".venv/new-artifact.log", "sig-new"),
            ),
        )

    started_runs: list[str] = []

    async def _start_pre_push_validation_run(
        _self: object,
        **_kwargs: object,
    ) -> str:
        started_runs.append("started")
        return "vr-gained-ignored"

    finish_calls: list[dict[str, object]] = []

    async def _finish_pre_push_validation_run(
        _self: object,
        validation_run_id: str,
        *,
        status: str,
        reason_code: str | None,
        retry_count: int = 0,
        coverage: dict[str, object] | None = None,
        command_retries: list[int] | None = None,
    ) -> None:
        finish_calls.append(
            {
                "validation_run_id": validation_run_id,
                "status": status,
                "reason_code": reason_code,
                "retry_count": retry_count,
                "coverage": coverage,
                "command_retries": command_retries,
            }
        )

    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_worktree_check",
        _run_pre_push_validation_worktree_check,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_start_pre_push_validation_run",
        _start_pre_push_validation_run,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_finish_pre_push_validation_run",
        _finish_pre_push_validation_run,
    )

    result = await pre_push_validation._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        ignore_ignored_paths=(".venv/",),
        ignore_all_ignored=True,
        capture_ignored_paths_snapshot=False,
        baseline_ignored_roots=(".venv/",),
        baseline_ignored_paths_snapshot=(".venv/existing-artifact.log",),
        baseline_ignored_paths_snapshot_signatures=(
            (".venv/existing-artifact.log", "sig-existing"),
        ),
    )

    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id == "vr-gained-ignored"
    assert "Validation worktree ignored entries changed after setup baseline" in result.message
    assert started_runs == ["started"]
    assert len(finish_calls) == 1
    assert finish_calls[0]["status"] == "failed"
    assert finish_calls[0]["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert runner._deps.validation.calls == []


@pytest.mark.unit
def test_pre_push_validation_new_ignored_entries_rejects_removed_snapshot_paths() -> None:
    """Deleted baseline ignored artifacts should be treated as ignored drift."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    assert pre_push_validation._pre_push_validation_new_ignored_entries(
        baseline_ignored_roots=(".venv/",),
        baseline_ignored_snapshot=(".venv/existing-artifact.log",),
        baseline_ignored_snapshot_signatures=((".venv/existing-artifact.log", "sig-existing"),),
        current_ignored_roots=(".venv/",),
        current_ignored_snapshot=(),
        current_ignored_snapshot_signatures=(),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("baseline_signatures", "current_signatures"),
    [
        (((".venv/existing-artifact.log", "sig-existing"),), ()),
        ((), ((".venv/existing-artifact.log", "sig-existing"),)),
    ],
)
def test_pre_push_validation_new_ignored_entries_rejects_one_sided_signature_drift(
    baseline_signatures: tuple[tuple[str, str], ...],
    current_signatures: tuple[tuple[str, str], ...],
) -> None:
    """One-sided ignored artifact signatures should be treated as ignored drift."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    assert pre_push_validation._pre_push_validation_new_ignored_entries(
        baseline_ignored_roots=(".venv/",),
        baseline_ignored_snapshot=(".venv/existing-artifact.log",),
        baseline_ignored_snapshot_signatures=baseline_signatures,
        current_ignored_roots=(".venv/",),
        current_ignored_snapshot=(".venv/existing-artifact.log",),
        current_ignored_snapshot_signatures=current_signatures,
    )


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rolls_back_when_commit_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed validation-fix commit must not leave staged changes for the next repair."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    fix_start_head = "e" * 40
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {fix_start_head[:8]}\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """Simulate a validation-fix commit failure."""
        return False

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    committed, rollback_failed = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert committed is False
    assert rollback_failed is None
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(f"reset --hard {fix_start_head}" in call for call in joined_calls)
    assert any("clean -fdx" in call for call in joined_calls)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_without_failure_returns_false() -> None:
    """A validation result with no command failure should not invoke a fix agent."""
    validation_result = pre_push_validation_module._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_provider",
        workspace_head_sha="a" * 40,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="coverage provider failed",
        validation_reason_code="COVERAGE_PROVIDER_FAILED",
        result=ValidationResult(coverage=_provider_coverage_failure_without_command()),
    )

    committed, rollback_failed = await pre_push_validation_module._run_pre_push_validation_fix_pass(
        object(),
        workspace_id="ws_provider",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        remote_branch="awf/ws_provider",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=(),
    )

    assert committed is False
    assert rollback_failed is None


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rolls_back_when_commit_raises(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit-path exception should not strand the fix-pass worktree delta."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    fix_start_head = "9" * 40
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {fix_start_head[:8]}\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """Simulate a validation-fix commit failure."""
        raise RuntimeError("commit path failed")

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    committed, rollback_failed = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert committed is False
    assert rollback_failed is None
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(f"reset --hard {fix_start_head}" in call for call in joined_calls)
    assert any("clean -fdx" in call for call in joined_calls)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rollback_preserves_ignored_paths(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Rollback should keep ignored artifacts like .venv while removing generated files."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    cmd = cast(FakeCommandRunner, runner._deps.runner)
    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    restore_ref = "d" * 40

    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {restore_ref[:8]}\n")
    cmd.queue_result(returncode=0, stdout="?? generated.tmp\n!! .venv/\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")

    rollback_failure_reason = (
        await pre_push_validation._rollback_failed_pre_push_validation_fix_pass(
            runner,
            workspace_id="workspace",
            worktree_path=worktree,
            restore_ref=restore_ref,
            ignore_ignored_paths=(".venv",),
            pass_number=1,
            reason="test",
        )
    )

    assert rollback_failure_reason is None
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(f"reset --hard {restore_ref}" in call for call in joined_calls)
    assert any("clean -fdx -- generated.tmp" in call for call in joined_calls)
    assert all(not ("clean -fdx" in call and ".venv" in call) for call in joined_calls)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rollback_failure_is_bubbled_as_pre_push_validation_rollback_failed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed rollback after a fix pass should surface a distinct rollback failure code."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'f' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'f' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {'f' * 8}\n")
    cmd.queue_result(returncode=0, stdout="")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")

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

    async def _rollback_failed(*_args: object, **_kwargs: object) -> str:
        """Simulate a rollback failure in fix-pass cleanup."""
        return "PRE_PUSH_VALIDATION_ROLLBACK_FAILED"

    async def _commit_failed(**_kwargs: object) -> bool:
        """Simulate a repair commit failure exception path."""
        return False

    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed,
    )
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_failed)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_ROLLBACK_FAILED"


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_post_reset_cleanup_failure_surfaces_cleanup_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful reset plus failed cleanup should not be labeled rollback failed."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    restore_ref = "6" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {restore_ref[:8]}\n")
    cmd.queue_result(returncode=0, stdout="?? validation-artifact.log\n")
    cmd.queue_result(returncode=1, stderr="clean failed")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")
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

    async def _commit_failed(**_kwargs: object) -> bool:
        """Simulate a repair commit failure after the agent attempted a fix."""
        return False

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_failed)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert "rollback failed" not in result.stderr
    assert "cleanup failed" in result.stderr
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rollback_does_not_clean_when_reset_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A failed rollback reset should preserve untracked files for manual recovery."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    cmd = cast(FakeCommandRunner, runner._deps.runner)
    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    restore_ref = "b" * 40
    cmd.queue_result(returncode=1, stdout="")

    rollback_failure_reason = (
        await pre_push_validation._rollback_failed_pre_push_validation_fix_pass(
            runner,
            workspace_id="workspace",
            worktree_path=worktree,
            restore_ref=restore_ref,
            pass_number=1,
            reason="reset_failed",
        )
    )

    assert rollback_failure_reason == "PRE_PUSH_VALIDATION_ROLLBACK_FAILED"
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(f"reset --hard {restore_ref}" in call for call in joined_calls)
    assert not any("clean -fdx" in call for call in joined_calls)


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
    cmd.queue_result(returncode=0, stdout=f"{'2' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {'2' * 8}\n")
    cmd.queue_result(returncode=0, stdout="")
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
    cmd.queue_result(returncode=0, stdout=f"{'f' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {'f' * 8}\n")
    cmd.queue_result(returncode=0, stdout="")
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
        """Raise a session factory error to exercise early repair failure."""
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
