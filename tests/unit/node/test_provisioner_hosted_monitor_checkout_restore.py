"""Hosted monitor checkout restore remapping on Provisioner."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.repositories import WorkspaceRepository
from awf.node.git_manager import GitManager, GitOperationError, WorktreeLayout
from awf.node.provisioner import Provisioner, ProvisionerConfig
from tests.unit.node.test_provisioner_parts.test_provisioner_part_001 import (
    git_manager,
    origin_repo,
    session_factory,
)

__all__ = ("git_manager", "origin_repo", "session_factory")


async def _seed_hosted_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repo_url: str,
    branch_name: str,
    pr_number: int,
) -> str:
    async with session_factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url=repo_url,
            branch_base="development",
            task_title="hosted restore",
            task_prompt="p",
            agent="codex",
            test_commands=[],
            requires_database=False,
            task_policy={"pr_adoption": {"execution": {"mode": "hosted"}}},
            task_kind="sync_feature_pr",
        )
        ws.branch_name = branch_name
        ws.remote_push_branch = branch_name
        ws.pr_number = pr_number
        await session.commit()
        return ws.id


@pytest.mark.unit
async def test_ensure_hosted_monitor_worktree_remaps_underlying_git_failure(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-restore reason codes from ensure_worktree become MONITOR_RECOVERY_…."""
    workspace_id = await _seed_hosted_workspace(
        session_factory,
        repo_url=str(origin_repo),
        branch_name="awf/ws_hosted_restore",
        pr_number=7,
    )

    async def _failing_ensure(**kwargs: object) -> WorktreeLayout:
        raise GitOperationError(
            operation="worktree.add",
            returncode=128,
            stdout="",
            stderr="fatal: couldn't find remote ref refs/pull/7/head",
            reason_code="GIT_FETCH_BASE_FAILED",
        )

    monkeypatch.setattr(git_manager, "ensure_worktree", _failing_ensure)
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="node-a", branch_prefix="awf"),
    )
    with pytest.raises(GitOperationError) as raised:
        await provisioner.ensure_hosted_monitor_worktree(workspace_id)
    assert raised.value.reason_code == "MONITOR_RECOVERY_CHECKOUT_RESTORE_FAILED"
    assert raised.value.operation == "hosted_monitor.ensure_worktree"
    assert "underlying=GIT_FETCH_BASE_FAILED" in raised.value.stderr


@pytest.mark.unit
async def test_ensure_hosted_monitor_worktree_preserves_restore_reason_code(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Already-mapped restore failures are re-raised without double wrapping."""
    workspace_id = await _seed_hosted_workspace(
        session_factory,
        repo_url=str(origin_repo),
        branch_name="awf/ws_hosted_preserve",
        pr_number=8,
    )

    original = GitOperationError(
        operation="hosted_monitor.ensure_worktree",
        returncode=1,
        stdout="",
        stderr="missing adoption tip",
        reason_code="MONITOR_RECOVERY_CHECKOUT_RESTORE_FAILED",
    )
    monkeypatch.setattr(
        git_manager,
        "ensure_worktree",
        AsyncMock(side_effect=original),
    )
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="node-a", branch_prefix="awf"),
    )
    with pytest.raises(GitOperationError) as raised:
        await provisioner.ensure_hosted_monitor_worktree(workspace_id)
    assert raised.value is original


@pytest.mark.unit
async def test_ensure_hosted_monitor_worktree_rejects_unknown_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown workspace id fails closed before ensure_worktree."""
    ensure = AsyncMock()
    monkeypatch.setattr(git_manager, "ensure_worktree", ensure)
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="node-a", branch_prefix="awf"),
    )
    with pytest.raises(GitOperationError) as raised:
        await provisioner.ensure_hosted_monitor_worktree("ws_missing_hosted_restore")
    assert raised.value.reason_code == "MONITOR_RECOVERY_CHECKOUT_RESTORE_FAILED"
    assert "not found for checkout restore" in raised.value.stderr
    ensure.assert_not_awaited()


@pytest.mark.unit
async def test_ensure_hosted_monitor_worktree_rejects_non_hosted_policy(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-hosted task_policy fails closed before ensure_worktree."""
    async with session_factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url=str(origin_repo),
            branch_base="development",
            task_title="local restore",
            task_prompt="p",
            agent="codex",
            test_commands=[],
            requires_database=False,
            task_policy={"pr_adoption": {"execution": {"mode": "local"}}},
            task_kind="sync_feature_pr",
        )
        ws.branch_name = "awf/ws_local_restore"
        ws.remote_push_branch = "awf/ws_local_restore"
        ws.pr_number = 9
        await session.commit()
        workspace_id = ws.id

    ensure = AsyncMock()
    monkeypatch.setattr(git_manager, "ensure_worktree", ensure)
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="node-a", branch_prefix="awf"),
    )
    with pytest.raises(GitOperationError) as raised:
        await provisioner.ensure_hosted_monitor_worktree(workspace_id)
    assert raised.value.reason_code == "MONITOR_RECOVERY_CHECKOUT_RESTORE_FAILED"
    assert "requires hosted PR adoption" in raised.value.stderr
    ensure.assert_not_awaited()


@pytest.mark.unit
async def test_ensure_hosted_monitor_worktree_rejects_empty_repo_url(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty repo_url fails closed before ensure_worktree."""
    async with session_factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url="https://example.invalid/org/repo.git",
            branch_base="development",
            task_title="hosted empty url",
            task_prompt="p",
            agent="codex",
            test_commands=[],
            requires_database=False,
            task_policy={"pr_adoption": {"execution": {"mode": "hosted"}}},
            task_kind="sync_feature_pr",
        )
        ws.branch_name = "awf/ws_hosted_empty_url"
        ws.remote_push_branch = "awf/ws_hosted_empty_url"
        ws.pr_number = 10
        ws.repo_url = ""
        await session.commit()
        workspace_id = ws.id

    ensure = AsyncMock()
    monkeypatch.setattr(git_manager, "ensure_worktree", ensure)
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="node-a", branch_prefix="awf"),
    )
    with pytest.raises(GitOperationError) as raised:
        await provisioner.ensure_hosted_monitor_worktree(workspace_id)
    assert raised.value.reason_code == "MONITOR_RECOVERY_CHECKOUT_RESTORE_FAILED"
    assert "missing repo_url" in raised.value.stderr
    ensure.assert_not_awaited()
