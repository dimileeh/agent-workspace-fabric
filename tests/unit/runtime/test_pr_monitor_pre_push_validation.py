"""Regression tests for PR monitor pre-push validation."""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED, ComposeExecCleanupError
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner import pre_push_validation as pre_push_validation_module
from awf.runtime.pr_monitor_runner import pre_push_validation_failures
from awf.runtime.pr_monitor_runner.remote_ops import (
    _git_push_failure_outcome,
)
from awf.runtime.validation_types import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
)
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._pre_push_validation_helpers import (
    _command_result,
    _CommandlessFailureValidationResult,
    _coverage_result,
    _failing_coverage_result,
    _FakeValidation,
    _mark_git_worktree,
    _OverriddenFirstFailureValidationResult,
    _provider_coverage_failure_without_command,  # noqa: F401  (re-exported for sibling test modules)
    _seed_monitoring_workspace_without_attempt,
    _set_resolved_profile,
    _validation_result,
    _validation_runs,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
def test_pre_push_validation_structural_helpers_are_single_source() -> None:
    """Keep pre-push validation helper definitions and retry flow single-sourced."""
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
    # The failure-classification helpers live in the dedicated ``pre_push_validation_failures``
    # module (split out to keep ``pre_push_validation`` under the first-party file-size
    # guardrail). They must be defined there exactly once and not duplicated back into the
    # orchestration module.
    failures_path = Path(pre_push_validation_failures.__file__)
    failures_tree = ast.parse(failures_path.read_text(encoding="utf-8"))
    failures_functions = [
        node.name for node in failures_tree.body if isinstance(node, ast.FunctionDef)
    ]
    for helper_name in helper_names:
        assert failures_functions.count(helper_name) == 1

    source_path = Path(pre_push_validation_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    orchestrator_functions = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
    for helper_name in helper_names:
        assert orchestrator_functions.count(helper_name) == 0

    retry_function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_run_pre_push_validation_with_fix_passes"
    )
    assert isinstance(retry_function.body[-1], ast.While)
    assert isinstance(retry_function.body[-1].test, ast.Constant)
    assert retry_function.body[-1].test.value is True


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

    failures = pre_push_validation_failures._failed_pre_push_commands(result)

    assert failures == (migration_failure, coverage_command_failure)
    assert pre_push_validation_failures._first_real_pre_push_failure(result) is (migration_failure)
    assert pre_push_validation_failures._pure_toolchain_missing_failure(result) is None
    assert (
        pre_push_validation_failures._pre_push_validation_reason_code(result) == "MIGRATION_FAILED"
    )


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

    assert pre_push_validation_failures._first_real_pre_push_failure(result) is None
    assert pre_push_validation_failures._pure_toolchain_missing_failure(result) is (
        coverage_command_failure
    )
    assert (
        pre_push_validation_failures._pre_push_validation_reason_code(result)
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

    assert pre_push_validation_failures._first_real_pre_push_failure(result) is provider_failure
    assert pre_push_validation_failures._pure_toolchain_missing_failure(result) is None
    assert pre_push_validation_failures._preferred_pre_push_failure(result) is provider_failure
    assert (
        pre_push_validation_failures._pre_push_validation_reason_code(result)
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
    original_failed_commands = pre_push_validation_failures._failed_pre_push_commands
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
    # Post-agent/pre-sink HEAD (``post_agent_head``): the agent did not
    # self-commit, so it equals the fix-start HEAD. Review thread
    # ``PRRT_kwDOSJAM6s6Klf78``.
    cmd.queue_result(returncode=0, stdout=f"{'3' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'4' * 40}\n")
    # merge-base --is-ancestor: the dirty commit still descends from fix_start_head.
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=f"{'5' * 40}\n")
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
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")  # restore ref
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")  # current HEAD
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
    local_head = "b" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")  # rev-parse HEAD
    cmd.queue_result(returncode=0, stdout="")  # pre-push clean check
    cmd.queue_result(returncode=0, stdout=" M apps/console/next-env.d.ts\n")  # cleanup check
    cmd.queue_result(returncode=1, stderr="restore failed")  # restore
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")  # restore ref
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")  # current HEAD
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
async def test_pre_push_validation_tracked_side_effect_after_validation_blocks_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Passing validation with cleaned tracked side effects must block push."""
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
    assert result.pushed is False
    assert result.reason_code == "PRE_PUSH_VALIDATION_FAILED"
    assert result.details is not None
    assert result.details["validation_reason_code"] == VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED
    assert result.details["cleaned_paths"] == ["apps/console/next-env.d.ts"]
    cleanup_details = result.details["validation_worktree_cleanup"]
    assert isinstance(cleanup_details, dict)
    assert cleanup_details["paths"] == ["apps/console/next-env.d.ts"]
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(
        f"restore --source {local_head} --staged --worktree -- apps/console/next-env.d.ts" in call
        for call in joined_calls
    )
    assert not any(" push " in f" {call} " for call in joined_calls)
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].status == "failed"
    assert runs[-1].reason_code == VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED


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
    assert result.details["workspace_head_sha"] == local_head
    assert result.details["paths"] == ["apps/console/next-env.d.ts"]
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]
