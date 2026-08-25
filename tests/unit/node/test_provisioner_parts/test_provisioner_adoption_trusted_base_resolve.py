"""Trusted-base resolve path for adopted sync_feature_pr provisioning."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.audit import REDACTION_MARKER
from awf.common.auto_merge import AUTO_MERGE_INTENT_POLICY_KEY
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.node.compose_manager import ComposeOperationError
from awf.node.git_manager import GitManager, GitOperationError, WorktreeLayout
from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.node.provisioner_helpers import (
    _PROFILE_TRUSTED_BASE_SHA_KEY,
    _provision_profile_auto_merge_is_trusted,
    _stamp_trusted_base_profile_provenance,
    _trusted_base_profile_worktree_id,
)
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import ProfileResolutionError, resolve_workspace_profile
from tests.unit.node.test_provisioner_parts._adoption_trusted_base_helpers import (
    _adopted_workspace_kwargs,
    _assert_trusted_base_stamp,
    _git_stdout,
    git_manager,
    origin_stale_head,
    session_factory,
)

pytestmark = pytest.mark.unit

__all__ = ["git_manager", "origin_stale_head", "session_factory"]


@pytest.mark.asyncio
async def test_adopted_auto_profile_resolves_from_trusted_base_not_stale_head(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_repo, base_sha, head_sha = origin_stale_head
    resolve_paths: list[Path] = []
    real_resolve = resolve_workspace_profile

    def _spy_resolve(**kwargs: Any) -> Any:
        path = kwargs.get("worktree_path")
        if isinstance(path, Path):
            resolve_paths.append(path)
        return real_resolve(**kwargs)

    monkeypatch.setattr("awf.node.provisioner.resolve_workspace_profile", _spy_resolve)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(origin_repo, base_sha=base_sha)
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)

    head_worktree = git_manager.get_worktree_path(workspace_id)
    snap_id = _trusted_base_profile_worktree_id(workspace_id)
    snap_path = git_manager.get_worktree_path(snap_id)

    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "base-safe"
        assert str(reloaded.resolved_profile.get("source", "")).startswith("repo:")
        assert reloaded.remote_push_branch == "feature/stale"
        assert reloaded.base_commit == base_sha
        phases = reloaded.resolved_profile.get("phases") or {}
        setup = phases.get("setup") or []
        assert setup and "trusted-base-setup" in str(setup)
        stamped = ((reloaded.task_policy or {}).get("pr_adoption") or {}).get(
            _PROFILE_TRUSTED_BASE_SHA_KEY
        )
        assert stamped == base_sha.lower()
        assert reloaded.auto_merge is True

    assert _git_stdout(["rev-parse", "HEAD"], head_worktree) == head_sha
    head_profile = (head_worktree / ".awf" / "workspace.yml").read_text(encoding="utf-8")
    assert "head-unsafe" in head_profile
    assert not snap_path.exists()
    assert resolve_paths
    assert all(path != head_worktree for path in resolve_paths)
    assert any(snap_id in str(path) for path in resolve_paths)


@pytest.mark.asyncio
async def test_adopted_inline_profile_skips_trusted_base_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_repo, base_sha, _ = origin_stale_head
    calls: list[str] = []

    async def _boom(*_a: Any, **_k: Any) -> WorktreeLayout:
        calls.append("detached")
        raise AssertionError("inline profile must not materialize trusted base snapshot")

    monkeypatch.setattr(git_manager, "add_detached_worktree_at_commit", _boom)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(
                origin_repo,
                base_sha=base_sha,
                requested_profile={"name": "operator-inline", "docker": {"mode": "none"}},
            )
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    assert calls == []
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "operator-inline"


@pytest.mark.asyncio
async def test_adopted_explicit_registry_profile_skips_trusted_base_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_repo, base_sha, _ = origin_stale_head
    calls: list[str] = []

    async def _boom(*_a: Any, **_k: Any) -> WorktreeLayout:
        calls.append("detached")
        raise AssertionError("registry profile_ref must not use trusted base snapshot")

    monkeypatch.setattr(git_manager, "add_detached_worktree_at_commit", _boom)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(origin_repo, base_sha=base_sha, profile_ref="generic")
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    assert calls == []
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.resolved_profile is not None
        # Repo marker still beats registry under ordinary resolve; this test
        # only proves the trusted-base snapshot path was not used.
        assert reloaded.resolved_profile["name"] == "head-unsafe"
        assert str(reloaded.resolved_profile.get("source", "")).startswith("repo:")


@pytest.mark.asyncio
async def test_ordinary_feature_branch_still_resolves_from_checkout(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
) -> None:
    origin_repo, _base_sha, _ = origin_stale_head
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            repo_url=str(origin_repo),
            branch_base="development",
            task_title="feature",
            task_prompt="p",
            agent="codex",
            test_commands=[],
            task_kind="feature_branch_pr",
            profile_ref="auto",
        )
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "base-safe"


@pytest.mark.asyncio
async def test_frozen_resolved_profile_skips_trusted_base_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_repo, base_sha, _ = origin_stale_head
    calls: list[str] = []

    async def _boom(*_a: Any, **_k: Any) -> WorktreeLayout:
        calls.append("detached")
        raise AssertionError("frozen resolved_profile must not re-resolve from base")

    monkeypatch.setattr(git_manager, "add_detached_worktree_at_commit", _boom)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(
                origin_repo,
                base_sha=base_sha,
                resolved_profile={
                    "name": "already-frozen",
                    "source": "repo:.awf/workspace.yml",
                },
            )
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    assert calls == []
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "already-frozen"


@pytest.mark.asyncio
async def test_fork_head_identity_preserved_after_trusted_base_resolve(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
) -> None:
    origin_repo, base_sha, head_sha = origin_stale_head
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(
                origin_repo,
                base_sha=base_sha,
                head_ref="fork-owner:feature/stale",
                extra_adoption={
                    "head_repo_full_name": "fork-owner/app",
                    "head_repo_clone_url": "https://github.com/fork-owner/app.git",
                },
            )
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.remote_push_branch == "fork-owner:feature/stale"
        stored = (reloaded.task_policy or {}).get("pr_adoption") or {}
        assert stored.get("head_ref") == "fork-owner:feature/stale"
        assert stored.get("head_repo_full_name") == "fork-owner/app"
        assert stored.get("head_repo_clone_url") == ("https://github.com/fork-owner/app.git")
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "base-safe"
    assert (
        _git_stdout(["rev-parse", "HEAD"], git_manager.get_worktree_path(workspace_id)) == head_sha
    )


@pytest.mark.asyncio
async def test_missing_trusted_base_sha_fails_closed_without_head_fallback(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_repo, _base_sha, _ = origin_stale_head
    resolve_paths: list[Path] = []
    real_resolve = resolve_workspace_profile

    def _spy_resolve(**kwargs: Any) -> Any:
        path = kwargs.get("worktree_path")
        if isinstance(path, Path):
            resolve_paths.append(path)
        return real_resolve(**kwargs)

    monkeypatch.setattr("awf.node.provisioner.resolve_workspace_profile", _spy_resolve)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(origin_repo, base_sha=None)
        )
        ws.pr_number = 42
        await s.commit()
        workspace_id = ws.id

    with pytest.raises(ProfileResolutionError):
        await provisioner.provision(workspace_id)

    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        assert reloaded.failure_reason == "profile_resolution_failure"
    head_worktree = git_manager.get_worktree_path(workspace_id)
    assert all(path != head_worktree for path in resolve_paths)


@pytest.mark.asyncio
async def test_trusted_base_resolve_failure_reclaims_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_repo, base_sha, _ = origin_stale_head
    snap_ids_seen: list[str] = []
    real_add = git_manager.add_detached_worktree_at_commit

    async def _tracking_add(*, workspace_id: str, repo_url: str, commit_sha: str) -> WorktreeLayout:
        snap_ids_seen.append(workspace_id)
        return await real_add(workspace_id=workspace_id, repo_url=repo_url, commit_sha=commit_sha)

    monkeypatch.setattr(git_manager, "add_detached_worktree_at_commit", _tracking_add)

    def _boom(**_kwargs: Any) -> Any:
        raise ProfileResolutionError(
            "synthetic base resolve failure",
            reason_code="PROFILE_TRUSTED_BASE_RESOLVE_FAILED",
        )

    monkeypatch.setattr("awf.node.provisioner.resolve_workspace_profile", _boom)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(origin_repo, base_sha=base_sha)
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    with pytest.raises(ProfileResolutionError):
        await provisioner.provision(workspace_id)

    assert snap_ids_seen == [_trusted_base_profile_worktree_id(workspace_id)]
    assert not git_manager.get_worktree_path(snap_ids_seen[0]).exists()
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        assert reloaded.failure_reason == "profile_resolution_failure"


@pytest.mark.asyncio
async def test_trusted_base_snapshot_cleanup_failure_fails_closed_and_redacts(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_repo, base_sha, _ = origin_stale_head
    secret = "ghp_cleanupSecretTokenValue888"
    real_add = git_manager.add_detached_worktree_at_commit

    async def _tracking_add(*, workspace_id: str, repo_url: str, commit_sha: str) -> WorktreeLayout:
        return await real_add(workspace_id=workspace_id, repo_url=repo_url, commit_sha=commit_sha)

    async def _cleanup_boom(*, workspace_id: str, repo_url: str) -> None:
        del workspace_id, repo_url
        raise GitOperationError(
            operation="worktree.remove",
            returncode=1,
            stdout="",
            stderr=f"fatal: could not remove worktree using token {secret}",
            reason_code="GIT_WORKTREE_REMOVE_FAILED",
        )

    monkeypatch.setattr(git_manager, "add_detached_worktree_at_commit", _tracking_add)
    monkeypatch.setattr(git_manager, "remove_worktree", _cleanup_boom)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(origin_repo, base_sha=base_sha)
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    with pytest.raises(GitOperationError) as raised:
        await provisioner.provision(workspace_id)

    assert raised.value.reason_code == "GIT_WORKTREE_REMOVE_FAILED"
    assert secret not in raised.value.stderr
    assert secret not in str(raised.value)
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        assert reloaded.failure_reason == "infrastructure_failure"
        message = reloaded.failure_message or ""
        assert secret not in message
        assert REDACTION_MARKER in message or "ghp_" not in message
        assert any(event.reason_code == "GIT_WORKTREE_REMOVE_FAILED" for event in reloaded.events)


@pytest.mark.asyncio
async def test_trusted_base_resolve_failure_not_masked_by_cleanup_failure(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup failure must not swallow an in-flight resolve reason_code."""
    origin_repo, base_sha, _ = origin_stale_head

    def _boom(**_kwargs: Any) -> Any:
        raise ProfileResolutionError(
            "synthetic base resolve failure",
            reason_code="PROFILE_TRUSTED_BASE_RESOLVE_FAILED",
        )

    async def _cleanup_boom(*, workspace_id: str, repo_url: str) -> None:
        del workspace_id, repo_url
        raise GitOperationError(
            operation="worktree.remove",
            returncode=1,
            stdout="",
            stderr="fatal: could not remove worktree",
            reason_code="GIT_WORKTREE_REMOVE_FAILED",
        )

    monkeypatch.setattr("awf.node.provisioner.resolve_workspace_profile", _boom)
    monkeypatch.setattr(git_manager, "remove_worktree", _cleanup_boom)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(origin_repo, base_sha=base_sha)
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    with pytest.raises(ProfileResolutionError) as raised:
        await provisioner.provision(workspace_id)

    assert raised.value.reason_code == "PROFILE_TRUSTED_BASE_RESOLVE_FAILED"
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        assert reloaded.failure_reason == "profile_resolution_failure"


@pytest.mark.asyncio
async def test_legacy_frozen_repo_profile_requires_explicit_auto_merge_intent(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
) -> None:
    """Frozen PR-head repo profiles must not self-authorize auto-merge."""
    origin_repo, base_sha, _ = origin_stale_head
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(
                origin_repo,
                base_sha=base_sha,
                resolved_profile={
                    "name": "legacy-head",
                    "source": "repo:.awf/workspace.yml",
                    "monitor": {"auto_merge": {"default": True}},
                    "docker": {"mode": "none"},
                },
            )
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "legacy-head"
        # No trusted-base provenance stamp on legacy freeze → DEFAULT_AUTO_MERGE.
        assert reloaded.auto_merge is False
        adoption = (reloaded.task_policy or {}).get("pr_adoption") or {}
        assert _PROFILE_TRUSTED_BASE_SHA_KEY not in adoption


@pytest.mark.asyncio
async def test_trusted_base_materialize_failure_message_is_redacted(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_repo, base_sha, _ = origin_stale_head
    secret = "ghp_supersecretTokenValue999"

    async def _boom(*, workspace_id: str, repo_url: str, commit_sha: str) -> WorktreeLayout:
        del workspace_id, repo_url, commit_sha
        raise GitOperationError(
            operation="worktree.add_detached",
            returncode=1,
            stdout="",
            stderr=f"fatal: could not read '{secret}' from remote",
            reason_code="GIT_BASE_BRANCH_MISSING",
        )

    monkeypatch.setattr(git_manager, "add_detached_worktree_at_commit", _boom)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(origin_repo, base_sha=base_sha)
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    with pytest.raises(GitOperationError):
        await provisioner.provision(workspace_id)

    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        message = reloaded.failure_message or ""
        assert secret not in message
        assert REDACTION_MARKER in message or "ghp_" not in message


@pytest.mark.asyncio
async def test_host_port_failure_after_trusted_base_resolve_stamps_provenance(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
) -> None:
    """Host-port fail after trusted resolve must not leave an unstamped freeze."""
    origin_repo, base_sha, _ = origin_stale_head
    companion_policy = {
        "companions": [
            {
                "name": "sidecar",
                "repo_url": str(origin_repo),
                "base_branch": "development",
                "ports": [[5432, 15434]],
            }
        ]
    }

    class _RecordingStackLauncher:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def launch(self, request: Any) -> object:
            self.requests.append(request)
            raise AssertionError("stack launch must not run after host-port conflict")

    launcher = _RecordingStackLauncher()
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        stack_launcher=launcher,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        source = await repo.create(
            repo_url=str(origin_repo),
            branch_base="development",
            task_title="source-ports",
            task_prompt="p",
            agent="codex",
            task_policy=companion_policy,
            test_commands=[],
        )
        source.node_id = "test-node-01"
        source.compose_project_name = f"awf_{source.id}"
        await repo.transition(source, to=WorkspaceStatus.provisioning, reason_code="SEED")
        await repo.transition(source, to=WorkspaceStatus.failed, reason_code="SEED")

        kwargs = _adopted_workspace_kwargs(origin_repo, base_sha=base_sha)
        policy = dict(kwargs["task_policy"])
        policy["companions"] = companion_policy["companions"]
        kwargs["task_policy"] = policy
        ws = await repo.create(**kwargs)
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    assert launcher.requests == []

    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        assert reloaded.failure_reason == "infrastructure_failure"
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "base-safe"
        assert str(reloaded.resolved_profile.get("source", "")).startswith("repo:")
        _assert_trusted_base_stamp(reloaded, base_sha=base_sha)
        # Unset intent + stamped trusted repo profile must remain trustable.
        assert str(reloaded.resolved_profile.get("source", "")).startswith("repo:")
        assert _provision_profile_auto_merge_is_trusted(
            reloaded,
            WorkspaceProfile(
                name="base-safe",
                source="repo:.awf/workspace.yml",
                monitor={"auto_merge": {"default": True}},
            ),
        )


@pytest.mark.asyncio
async def test_stack_startup_failure_after_trusted_base_resolve_stamps_provenance(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
) -> None:
    """Pre-launch freeze + stack fail must persist matching trusted-base stamp."""
    origin_repo, base_sha, _ = origin_stale_head

    class _FailingStackLauncher:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def launch(self, request: Any) -> object:
            self.requests.append(request)
            raise ComposeOperationError(
                operation="compose.up",
                returncode=1,
                stdout="",
                stderr="service db failed to start",
                reason_code="COMPOSE_UP_FAILED",
            )

    launcher = _FailingStackLauncher()
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        stack_launcher=launcher,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(origin_repo, base_sha=base_sha)
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    with pytest.raises(ComposeOperationError):
        await provisioner.provision(workspace_id)

    assert launcher.requests
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "base-safe"
        _assert_trusted_base_stamp(reloaded, base_sha=base_sha)
        assert str(reloaded.resolved_profile.get("source", "")).startswith("repo:")
        assert _provision_profile_auto_merge_is_trusted(
            reloaded,
            WorkspaceProfile(
                name="base-safe",
                source="repo:.awf/workspace.yml",
                monitor={"auto_merge": {"default": True}},
            ),
        )


@pytest.mark.asyncio
async def test_retry_of_unstamped_frozen_trusted_profile_forces_auto_merge_false(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
) -> None:
    """Negative: frozen trusted-looking snapshot without stamp must not auto-merge."""
    origin_repo, base_sha, _ = origin_stale_head
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(
                origin_repo,
                base_sha=base_sha,
                resolved_profile={
                    "name": "base-safe",
                    "source": "repo:.awf/workspace.yml",
                    "monitor": {"auto_merge": {"default": True}},
                    "docker": {"mode": "none"},
                },
            )
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.resolved_profile is not None
        assert reloaded.auto_merge is False
        adoption = (reloaded.task_policy or {}).get("pr_adoption") or {}
        assert _PROFILE_TRUSTED_BASE_SHA_KEY not in adoption


@pytest.mark.asyncio
async def test_retry_of_stamped_frozen_profile_from_failure_honors_auto_merge(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
) -> None:
    """Stamped freeze from a prior failure path must keep profile auto-merge on retry."""
    origin_repo, base_sha, _ = origin_stale_head
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    stamped_policy = _stamp_trusted_base_profile_provenance(
        {
            "pr_adoption": {
                "pr_number": 42,
                "head_ref": "feature/stale",
                "base_ref": "development",
                "base_sha": base_sha,
            },
            AUTO_MERGE_INTENT_POLICY_KEY: None,
        },
        trusted_base_sha=base_sha,
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(
                origin_repo,
                base_sha=base_sha,
                resolved_profile={
                    "name": "base-safe",
                    "source": "repo:.awf/workspace.yml",
                    "monitor": {"auto_merge": {"default": True}},
                    "docker": {"mode": "none"},
                },
            )
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        ws.task_policy = stamped_policy
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "base-safe"
        _assert_trusted_base_stamp(reloaded, base_sha=base_sha)
        assert reloaded.auto_merge is True


@pytest.mark.asyncio
async def test_rehydration_failure_replaces_legacy_freeze_before_stamp(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
) -> None:
    """Rehydrate+fail must not stamp an untouched PR-head freeze in place."""
    origin_repo, base_sha, _ = origin_stale_head
    companion_policy = {
        "companions": [
            {
                "name": "sidecar",
                "repo_url": str(origin_repo),
                "base_branch": "development",
                "ports": [[5432, 15435]],
            }
        ]
    }

    class _RecordingStackLauncher:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def launch(self, request: Any) -> object:
            self.requests.append(request)
            raise AssertionError("stack launch must not run after host-port conflict")

    launcher = _RecordingStackLauncher()
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        stack_launcher=launcher,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        source = await repo.create(
            repo_url=str(origin_repo),
            branch_base="development",
            task_title="source-ports-rehydrate",
            task_prompt="p",
            agent="codex",
            task_policy=companion_policy,
            test_commands=[],
        )
        source.node_id = "test-node-01"
        source.compose_project_name = f"awf_{source.id}"
        await repo.transition(source, to=WorkspaceStatus.provisioning, reason_code="SEED")
        await repo.transition(source, to=WorkspaceStatus.failed, reason_code="SEED")

        kwargs = _adopted_workspace_kwargs(
            origin_repo,
            base_sha=base_sha,
            resolved_profile={
                "name": "legacy-head",
                "source": "repo:.awf/workspace.yml",
                "monitor": {"auto_merge": {"default": True}},
                "docker": {"mode": "none"},
                # Force credential rehydration so trusted-base resolve runs
                # while a PR-head freeze is already on the row.
                "secrets": {"CURSOR_API_KEY": REDACTION_MARKER},
            },
        )
        policy = dict(kwargs["task_policy"])
        policy["companions"] = companion_policy["companions"]
        kwargs["task_policy"] = policy
        ws = await repo.create(**kwargs)
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    assert launcher.requests == []

    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        assert reloaded.resolved_profile is not None
        # Must replace the legacy PR-head freeze with the trusted-base snapshot
        # before stamping — never mint provenance onto legacy-head.
        assert reloaded.resolved_profile["name"] == "base-safe"
        assert reloaded.resolved_profile["name"] != "legacy-head"
        _assert_trusted_base_stamp(reloaded, base_sha=base_sha)
        assert _provision_profile_auto_merge_is_trusted(
            reloaded,
            WorkspaceProfile(
                name="base-safe",
                source="repo:.awf/workspace.yml",
                monitor={"auto_merge": {"default": True}},
            ),
        )
