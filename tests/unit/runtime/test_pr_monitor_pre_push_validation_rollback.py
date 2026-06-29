"""Tail tests for PR monitor pre-push validation rollback reporting."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.runtime.test_pr_monitor_pre_push_validation import (
    FakeAdapter,
    FakeCommandRunner,
    RecordedSleep,
    _FakeValidation,
    _set_resolved_profile,
    _validation_result,
    _validation_runs,
    make_runner,
    pre_push_validation_module,
    seed_monitoring_workspace,
)

pytest_plugins = ("tests.unit.runtime.test_pr_monitor_pre_push_validation",)


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
async def test_pre_push_validation_fix_pass_reports_failed_rollback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the fix agent raises and worktree rollback fails, surface the rollback reason."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "a" * 40
    # _run_pre_push_validation: rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    # _run_pre_push_validation_fix_pass: fix_start_head
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")

    validation = _FakeValidation(_validation_result(tmp_path, ok=False))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    async def _failed_rollback(*_args: object, **_kwargs: object) -> str:
        """Simulate a recovery rollback that cannot complete."""
        return pre_push_validation_module.PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON

    monkeypatch.setattr(
        pre_push_validation_module,
        "_rollback_failed_pre_push_validation_fix_pass",
        _failed_rollback,
    )

    adapter = FakeAdapter()
    adapter.queue(exc=RuntimeError("fix agent exploded"))
    runner._deps.adapter = adapter  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.pushed is False
    assert (
        result.reason_code == pre_push_validation_module.PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON
    )
    assert result.details is not None
    assert "rollback failed" in str(result.details.get("error_message", "")).lower()
