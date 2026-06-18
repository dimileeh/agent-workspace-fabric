"""Mixed toolchain/real failure regressions for PR monitor pre-push validation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.session import make_session_factory
from awf.runtime.validation_types import ValidationResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_pre_push_validation import (
    _command_result,
    _FakeValidation,
    _set_resolved_profile,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _mixed_failure_setup(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> tuple[str, Path, FakeCommandRunner, FakeAdapter]:
    """Create the common monitor fixtures for mixed failure scenarios."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted mixed failure fix\n")
    return workspace_id, worktree, cmd, adapter


@pytest.mark.unit
async def test_mixed_127_fix_commit_failure_reports_real_pre_push_details(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed toolchain and real failures should report the real terminal failure."""
    workspace_id, worktree, cmd, adapter = await _mixed_failure_setup(factory, tmp_path)
    cmd.queue_result(returncode=0, stdout=f"{'4' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'5' * 40}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        ValidationResult(
            commands=[
                _command_result(
                    tmp_path,
                    ok=False,
                    command="ruff check .",
                    returncode=127,
                    reason_code="COMMAND_FAILED",
                    artifact_name="mixed_commit_fail_ruff_missing",
                ),
                _command_result(
                    tmp_path,
                    ok=False,
                    command="pytest -q",
                    returncode=1,
                    reason_code="PYTEST_TEST_FAILURE",
                    artifact_name="mixed_commit_fail_pytest_failure",
                ),
            ]
        ),
    )

    async def _no_commit(**_kwargs: object) -> bool:
        """Return a failed commit result for the mixed-failure fix pass."""
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
    assert result.details["reason_code"] == "PRE_PUSH_VALIDATION_FIX_FAILED"
    assert result.details["validation_reason_code"] == "PYTEST_TEST_FAILURE"
    assert result.details["failing_command"] == "pytest -q"
    assert result.details["failing_returncode"] == 1
    assert "Failing command: pytest -q" in adapter.calls[0]
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_mixed_127_fix_pass_exhaustion_reports_real_pre_push_details(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhausted mixed-failure retries should keep real failure diagnostics."""
    workspace_id, worktree, cmd, adapter = await _mixed_failure_setup(factory, tmp_path)
    cmd.queue_result(returncode=0, stdout=f"{'5' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'6' * 40}\n")
    # The commit sink then advances HEAD to ``{'7' * 40}``. The commit-sink
    # ``except`` clauses capture HEAD INSIDE each clause (after the sink
    # raised), not before the sink (review thread ``PRRT_kwDOSJAM6s6Klf78``
    # / ``PRRT_kwDOSJAM6s6KpAD6``).
    cmd.queue_result(returncode=0, stdout=f"{'7' * 40}\n")
    # merge-base --is-ancestor: the dirty commit still descends from fix_start_head.
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=f"{'8' * 40}\n")
    first_mixed = ValidationResult(
        commands=[
            _command_result(
                tmp_path,
                ok=False,
                command="ruff check .",
                returncode=127,
                reason_code="COMMAND_FAILED",
                artifact_name="mixed_exhaustion_first_ruff_missing",
            ),
            _command_result(
                tmp_path,
                ok=False,
                command="pytest -q",
                returncode=1,
                reason_code="PYTEST_TEST_FAILURE",
                artifact_name="mixed_exhaustion_first_pytest_failure",
            ),
        ]
    )
    second_mixed = ValidationResult(
        commands=[
            _command_result(
                tmp_path,
                ok=False,
                command="ruff check .",
                returncode=127,
                reason_code="COMMAND_FAILED",
                artifact_name="mixed_exhaustion_second_ruff_missing",
            ),
            _command_result(
                tmp_path,
                ok=False,
                command="pytest -q",
                returncode=1,
                reason_code="PYTEST_TEST_FAILURE",
                artifact_name="mixed_exhaustion_second_pytest_failure",
            ),
        ]
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )
    validation = _FakeValidation(first_mixed, second_mixed)
    runner._deps.validation = validation  # type: ignore[assignment]

    async def _commit_dirty(**_kwargs: object) -> bool:
        """Report a successful synthetic fix commit before the retry fails again."""
        return True

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty)

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
    assert result.details["reason_code"] == "PRE_PUSH_VALIDATION_FAILED"
    assert result.details["validation_reason_code"] == "PYTEST_TEST_FAILURE"
    assert result.details["failing_command"] == "pytest -q"
    assert result.details["failing_returncode"] == 1
    assert len(validation.calls) == 2
    assert len(adapter.calls) == 1
    assert "Failing command: pytest -q" in adapter.calls[0]
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]
