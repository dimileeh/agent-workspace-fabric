"""Cleanup and status regressions for PR monitor pre-push validation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_pre_push_validation import (
    _FakeValidation,
    _mark_git_worktree,
    _set_resolved_profile,
    _validation_result,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


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
async def test_pre_push_validation_ignores_git_clean_empty_directory(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A Git-clean worktree with an empty untracked directory must not block push."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    (worktree / "generated").mkdir()
    cmd = FakeCommandRunner()
    local_head = "e" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    # Empty-dir-aware pre-check calls status once; cleanup also calls status with
    # ignore_all_ignored=True and we leave its restore/verify commands queued.
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    pushed = False
    pushed_validation_kwargs: dict[str, object] | None = None

    class _RecordingFakeValidation(_FakeValidation):
        async def run_profile_phases(self, **kwargs: object) -> Any:
            nonlocal pushed_validation_kwargs
            pushed_validation_kwargs = dict(kwargs)
            return await super().run_profile_phases(**kwargs)

    async def fake_git_push_result(*_args: object, **_kwargs: object) -> Any:
        nonlocal pushed
        pushed = True
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    runner._deps.validation = _RecordingFakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    runner._git_push_result = fake_git_push_result  # type: ignore[method-assign]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert pushed is True
    assert not (worktree / "generated").exists()
    assert pushed_validation_kwargs is not None
    assert pushed_validation_kwargs["worktree_path"] == worktree


@pytest.mark.unit
async def test_pre_push_validation_still_fails_for_real_untracked_file(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A real untracked file must still block pre-push validation."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    (worktree / "untracked.py").write_text("x\n", encoding="utf-8")
    cmd = FakeCommandRunner()
    local_head = "f" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout="?? untracked.py\n")
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
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.details is not None
    assert result.details["paths"] == ["untracked.py"]
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]
    assert (worktree / "untracked.py").exists()
