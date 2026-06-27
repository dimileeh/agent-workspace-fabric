"""Additional PR monitor runner HEAD object missing coverage edges."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.ownership import AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
from awf.runtime.pr_monitor import CheckFailure
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
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
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_ci_fix_catches_head_object_missing_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="abc123\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="AWF-VERDICT: FIXED: done")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _head_object_missing(**_kwargs: object) -> None:
        raise _MonitorHeadObjectMissingError(
            "HEAD_OBJECT_MISSING_CI_REPAIR_CUSTOM",
            "HEAD object missing for workspace test and recovery failed",
        )

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _head_object_missing)

    result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="lint", conclusion="FAILURE", log_excerpt="test failure"),),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch="awf/ws_test",
    )

    assert result.failed
    assert result.pushed is False
    assert result.returncode == 1
    assert result.reason_code == "HEAD_OBJECT_MISSING_CI_REPAIR_CUSTOM"
    assert "HEAD object missing" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    ("repair_exc", "expected_reason"),
    [
        (
            _MonitorHeadObjectMissingError(
                "HEAD_OBJECT_MISSING_CI_REPAIR_CLEANUP",
                "HEAD object missing during cleanup repair",
            ),
            "HEAD_OBJECT_MISSING_CI_REPAIR_CLEANUP",
        ),
        (
            _MonitorMirrorHooksPathRepairFailedError(),
            "MIRROR_HOOKS_PATH_POISONED",
        ),
    ],
)
async def test_ci_fix_agent_cleanup_repair_failure_returns_terminal_push_result(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repair_exc: Exception,
    expected_reason: str,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="abc123\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _raise_cleanup_repair_failure(**_kwargs: object) -> None:
        raise repair_exc

    async def _unexpected_commit_dirty_worktree(**_kwargs: object) -> bool:
        raise AssertionError("cleanup repair failures should return before commit sink")

    monkeypatch.setattr(
        runner,
        "_run_monitor_agent_with_service_recovery",
        _raise_cleanup_repair_failure,
    )
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _unexpected_commit_dirty_worktree)

    result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="lint", conclusion="FAILURE", log_excerpt="test failure"),),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch="awf/ws_test",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.returncode == 1
    assert result.reason_code == expected_reason


@pytest.mark.unit
async def test_ci_fix_agent_cleanup_ownership_repair_failure_returns_terminal_push_result(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="abc123\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _raise_cleanup_repair_failure(**_kwargs: object) -> None:
        raise _MonitorAgentRuntimeOwnershipRepairFailedError(
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
        )

    async def _unexpected_commit_dirty_worktree(**_kwargs: object) -> bool:
        raise AssertionError("cleanup repair failures should return before commit sink")

    monkeypatch.setattr(
        runner,
        "_run_monitor_agent_with_service_recovery",
        _raise_cleanup_repair_failure,
    )
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _unexpected_commit_dirty_worktree)

    result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="lint", conclusion="FAILURE", log_excerpt="test failure"),),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch="awf/ws_test",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.returncode == 1
    assert result.reason_code == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
    assert result.stderr == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE


@pytest.mark.unit
async def test_sync_base_catches_head_object_missing_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="partial conflict resolution")
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    for result in [
        (0, "abc123\n", ""),
        (0, "", ""),
        (0, "", ""),
        (1, "", "merge conflict"),
        (0, "UU src/conflict.py\n", ""),
    ]:
        cmd.queue_result(returncode=result[0], stdout=result[1], stderr=result[2])
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _head_object_missing(**_kwargs: object) -> None:
        raise _MonitorHeadObjectMissingError(
            "HEAD_OBJECT_MISSING_SYNC_BASE_CUSTOM",
            "HEAD object missing for workspace test and recovery failed",
        )

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _head_object_missing)

    push_result = await runner._run_sync_base(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert push_result.failed is True
    assert push_result.pushed is False
    assert push_result.returncode == 1
    assert push_result.reason_code == "HEAD_OBJECT_MISSING_SYNC_BASE_CUSTOM"
    assert "HEAD object missing" in push_result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    ("repair_exc", "expected_reason"),
    [
        (
            _MonitorHeadObjectMissingError(
                "HEAD_OBJECT_MISSING_SYNC_BASE_CLEANUP",
                "HEAD object missing during cleanup repair",
            ),
            "HEAD_OBJECT_MISSING_SYNC_BASE_CLEANUP",
        ),
        (
            _MonitorMirrorHooksPathRepairFailedError(),
            "MIRROR_HOOKS_PATH_POISONED",
        ),
    ],
)
async def test_sync_base_agent_cleanup_repair_failure_returns_terminal_push_result(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repair_exc: Exception,
    expected_reason: str,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    for result in [
        (0, "abc123\n", ""),
        (0, "", ""),
        (0, "", ""),
        (1, "", "merge conflict"),
        (0, "UU src/conflict.py\n", ""),
    ]:
        cmd.queue_result(returncode=result[0], stdout=result[1], stderr=result[2])
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _raise_cleanup_repair_failure(**_kwargs: object) -> None:
        raise repair_exc

    async def _unexpected_commit_dirty_worktree(**_kwargs: object) -> bool:
        raise AssertionError("cleanup repair failures should return before commit sink")

    monkeypatch.setattr(
        runner,
        "_run_monitor_agent_with_service_recovery",
        _raise_cleanup_repair_failure,
    )
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _unexpected_commit_dirty_worktree)

    push_result = await runner._run_sync_base(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert push_result.failed is True
    assert push_result.pushed is False
    assert push_result.returncode == 1
    assert push_result.reason_code == expected_reason


@pytest.mark.unit
async def test_sync_base_agent_ownership_repair_failure_returns_terminal_push_result(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    for result in [
        (0, "abc123\n", ""),
        (0, "", ""),
        (0, "", ""),
        (1, "", "merge conflict"),
        (0, "UU src/conflict.py\n", ""),
    ]:
        cmd.queue_result(returncode=result[0], stdout=result[1], stderr=result[2])
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _raise_ownership_repair_failure(**_kwargs: object) -> None:
        raise _MonitorAgentRuntimeOwnershipRepairFailedError(
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
        )

    async def _unexpected_commit_dirty_worktree(**_kwargs: object) -> bool:
        raise AssertionError("ownership repair failures should return before commit sink")

    monkeypatch.setattr(
        runner,
        "_run_monitor_agent_with_service_recovery",
        _raise_ownership_repair_failure,
    )
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _unexpected_commit_dirty_worktree)

    push_result = await runner._run_sync_base(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert push_result.failed is True
    assert push_result.pushed is False
    assert push_result.returncode == 1
    assert push_result.reason_code == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
    assert push_result.stderr == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
