"""Additional protected-scope PR monitor runner edge coverage."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.quality_gates_common import QualityGateViolation
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import remote_ops as pr_remote_ops
from awf.runtime.pr_monitor_runner.types import _ProtectedScopeRollbackDeltaEvidence
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _protected_workflow_violation() -> QualityGateViolation:
    return QualityGateViolation(
        path=".github/workflows/ci.yml",
        protected_pattern=".github/workflows/*.yml",
        section="jobs.tests.steps[0]",
        line=12,
        reason="required test step policy changed",
    )


@pytest.mark.unit
async def test_repair_delta_paths_records_malformed_committed_diff_fallback_errors(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="not-nul-delimited")
    cmd.queue_result(returncode=0, stdout="valid.py\0\0")
    cmd.queue_result(returncode=0, stdout="")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    delta = await runner._protected_scope_repair_delta_paths(
        workspace_id="ws_delta",
        worktree_path=tmp_path,
        operation_start_head="a" * 40,
    )

    assert delta.reverted_paths == ()
    assert [error["phase"] for error in delta.collection_errors] == [
        "committed_diff_parse",
        "committed_diff_name_only_fallback_parse",
    ]


@pytest.mark.unit
async def test_repair_delta_paths_records_committed_diff_command_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=2, stderr="diff failed")
    cmd.queue_result(returncode=0, stdout="")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    delta = await runner._protected_scope_repair_delta_paths(
        workspace_id="ws_delta",
        worktree_path=tmp_path,
        operation_start_head="a" * 40,
    )

    assert delta.collection_errors == (
        {"phase": "committed_diff_command", "returncode": 2, "stderr": "diff failed"},
    )


@pytest.mark.unit
async def test_rollback_protected_scope_repair_delta_records_cleanup_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=1, stderr="leftover untracked file")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    recorded: dict[str, object] = {}

    async def _delta(**_kwargs: object) -> _ProtectedScopeRollbackDeltaEvidence:
        return _ProtectedScopeRollbackDeltaEvidence(
            reverted_paths=(".github/workflows/ci.yml",),
            cleanup_paths=("generated.tmp",),
        )

    async def _record(**kwargs: object) -> None:
        recorded.update(kwargs)

    monkeypatch.setattr(runner, "_protected_scope_repair_delta_paths", _delta)
    monkeypatch.setattr(runner, "_record_protected_scope_rollback_result", _record)

    result = await runner._rollback_protected_scope_repair_delta_before_push(
        workspace_id="ws_delta",
        pr_number=42,
        worktree_path=tmp_path,
        protected_scope_block=pr_remote_ops._ProtectedScopePushBlock(  # noqa: SLF001
            message="blocked",
            reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
            violations=(_protected_workflow_violation(),),
        ),
        operation_start_head="a" * 40,
        attempted_head="b" * 40,
        remote_branch="awf/ws_delta",
    )

    assert result.failed is True
    assert result.returncode == 1
    assert result.details is not None
    assert result.details["rollback_status"] == "reset_succeeded_cleanup_failed"
    assert result.details["clean_stderr"] == "leftover untracked file"
    assert recorded["outcome"] == "failed"


@pytest.mark.unit
async def test_protected_scope_repair_filters_remote_restored_status_violation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    violation = _protected_workflow_violation()

    async def _violations_for_status(**_kwargs: object) -> tuple[QualityGateViolation, ...]:
        return (violation,)

    async def _not_restored(**_kwargs: object) -> tuple[QualityGateViolation, ...]:
        return ()

    monkeypatch.setattr(runner, "_protected_scope_violations_for_status", _violations_for_status)
    monkeypatch.setattr(
        runner,
        "_protected_scope_violations_not_restored_to_remote_branch",
        _not_restored,
    )

    result = await runner._repair_protected_scope_changes_before_commit(
        workspace_id="ws_delta",
        status_stdout=" M .github/workflows/ci.yml\n",
        compose_project="awf_ws_delta",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        protected_scope_revert_remote_branch="awf/ws_delta",
        remote_push_url="git@example.com/repo.git",
    )

    assert result is not None
    assert result.ok


@pytest.mark.unit
async def test_protected_scope_repair_filters_remote_restored_remaining_violation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M .github/workflows/ci.yml\n")
    adapter = FakeAdapter()
    adapter.queue()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    violation = _protected_workflow_violation()
    status_calls = 0
    filter_calls = 0

    async def _violations_for_status(**_kwargs: object) -> tuple[QualityGateViolation, ...]:
        nonlocal status_calls
        status_calls += 1
        return (violation,)

    async def _not_restored(**_kwargs: object) -> tuple[QualityGateViolation, ...]:
        nonlocal filter_calls
        filter_calls += 1
        return (violation,) if filter_calls == 1 else ()

    async def _prompt(**_kwargs: object) -> str:
        return "repair prompt"

    async def _suppresses_cli(_workspace_id: str) -> bool:
        return False

    monkeypatch.setattr(runner, "_protected_scope_violations_for_status", _violations_for_status)
    monkeypatch.setattr(
        runner,
        "_protected_scope_violations_not_restored_to_remote_branch",
        _not_restored,
    )
    monkeypatch.setattr(runner, "_protected_scope_repair_prompt", _prompt)
    monkeypatch.setattr(runner, "_provider_recovery_suppresses_cli", _suppresses_cli)

    result = await runner._repair_protected_scope_changes_before_commit(
        workspace_id="ws_delta",
        status_stdout=" M .github/workflows/ci.yml\n",
        compose_project="awf_ws_delta",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        protected_scope_revert_remote_branch="awf/ws_delta",
        remote_push_url="git@example.com/repo.git",
    )

    assert result is not None
    assert result.ok
    assert status_calls == 2
    assert filter_calls == 2
