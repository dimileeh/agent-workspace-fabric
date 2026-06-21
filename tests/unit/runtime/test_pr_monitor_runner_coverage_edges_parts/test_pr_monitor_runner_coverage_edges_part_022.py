"""Mirror hooks repair tests for PR monitor dirty commits."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.node.git_manager import GitOperationError
from awf.runtime.pr_monitor_runner import remote_repair as pr_remote_repair
from awf.runtime.pr_monitor_runner.types import _MonitorMirrorHooksPathRepairFailedError
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
        from awf.db.session import make_session_factory

        yield make_session_factory(engine)


def _write_worktree_with_mirror(tmp_path: Path, workspace_id: str) -> None:
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    mirror.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")


@pytest.mark.unit
async def test_commit_dirty_worktree_repairs_mirror_hooks_path(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    mirror = tmp_path / "mirrors" / "test.git"
    mirror.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {mirror}/worktrees/{workspace_id}\n")
    (mirror / "worktrees" / workspace_id).mkdir(parents=True)
    (mirror / "worktrees" / workspace_id / "commondir").write_text("../..\n")

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M src/foo.py\n")
    cmd.queue_result(returncode=0, stdout=" M src/foo.py\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0, stdout="abc123\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    hooks_path_repaired: list[Path] = []

    async def _repair_mirror_hooks_path(mirror_path: Path) -> bool:
        hooks_path_repaired.append(mirror_path)
        return True

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return True

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message="fix: test",
    )

    assert len(hooks_path_repaired) >= 1


@pytest.mark.unit
async def test_commit_dirty_worktree_preserves_mirror_hooks_repair_failure_details(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    _write_worktree_with_mirror(tmp_path, workspace_id)

    repair_error = GitOperationError(
        operation="mirror.hooks_path_repair",
        returncode=1,
        stdout="",
        stderr="fatal: config unset failed",
        reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
    )
    warning_calls: list[tuple[str, dict[str, object]]] = []

    def _warning(event: str, **kwargs: object) -> None:
        warning_calls.append((event, kwargs))

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        raise repair_error

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(pr_remote_repair._log, "warning", _warning)

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(_MonitorMirrorHooksPathRepairFailedError) as raised:
        await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="fix: test",
        )

    assert raised.value.__cause__ is repair_error
    assert str(raised.value) == "could not repair poisoned mirror hooks path"
    assert warning_calls == [
        (
            "monitor.mirror_hooks_path_repair_failed",
            {
                "workspace_id": workspace_id,
                "reason_code": "MIRROR_HOOKS_PATH_POISONED",
                "mirror_hooks_repair_failed": True,
                "repair_stage": "commit_dirty_worktree",
                "error_type": "GitOperationError",
                "mirror_path": str(tmp_path / "mirrors" / "test.git"),
                "repair_reason_code": "MIRROR_HOOKS_PATH_REPAIR_FAILED",
                "git_operation": "mirror.hooks_path_repair",
                "git_returncode": 1,
                "stderr": "fatal: config unset failed",
                "stdout": "",
            },
        )
    ]
