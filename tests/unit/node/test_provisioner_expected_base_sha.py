"""Provisioner forwards the expected base SHA only for a fresh named-branch checkout."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.git_manager import GitOperationError, WorktreeLayout
from awf.node.provisioner import Provisioner, ProvisionerConfig
from tests.postgres import postgres_test_engine

_SHA = "a" * 40


class _RecordingGit:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def add_worktree(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        raise GitOperationError(
            operation="worktree.add",
            returncode=1,
            stdout="",
            stderr="expected base pin failed closed",
            reason_code="GIT_EXPECTED_BASE_SHA_MISMATCH",
        )


class _RecordingLauncher:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def launch(self, request: object) -> None:
        self.calls.append(request)
        raise AssertionError("stack launch must not start after a git pin failure")


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _create(session_factory: async_sessionmaker[AsyncSession], **kwargs: Any) -> str:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="https://github.com/example/app.git",
            task_title="pin",
            task_prompt="pin the base",
            agent="codex",
            test_commands=[],
            **kwargs,
        )
        await session.commit()
        return workspace.id


@pytest.mark.unit
async def test_fresh_checkout_forwards_pin_and_git_failure_skips_stack_launch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    git = _RecordingGit()
    launcher = _RecordingLauncher()
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git,  # type: ignore[arg-type]
        stack_launcher=launcher,  # type: ignore[arg-type]
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    workspace_id = await _create(
        session_factory,
        branch_base="development",
        task_policy={"expected_base_sha": _SHA},
    )
    with pytest.raises(GitOperationError) as exc:
        await provisioner.provision(workspace_id)
    assert exc.value.reason_code == "GIT_EXPECTED_BASE_SHA_MISMATCH"
    assert launcher.calls == []
    assert git.calls == [
        {
            "workspace_id": workspace_id,
            "repo_url": "https://github.com/example/app.git",
            "base_branch": "development",
            "new_branch": f"awf/{workspace_id}",
            "expected_base_sha": _SHA,
        }
    ]
    async with session_factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row is not None
        assert row.status == WorkspaceStatus.failed.value
        assert any(event.reason_code == "GIT_EXPECTED_BASE_SHA_MISMATCH" for event in row.events)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("create_kwargs", "expected_base"),
    [
        (
            {
                "branch_base": "development",
                "task_kind": "feature_branch_pr",
                "task_policy": {"expected_base_sha": _SHA},
                "remote_push_branch": "feature/old",
            },
            "refs/pull/12/head",
        ),
        (
            {
                "branch_base": "development",
                "task_kind": "sync_feature_pr",
                "task_policy": {
                    "expected_base_sha": _SHA,
                    "pr_adoption": {
                        "pr_number": "12",
                        "head_ref": "feature/adopted",
                        "base_ref": "development",
                    },
                },
            },
            "refs/pull/12/head",
        ),
        (
            {
                "branch_base": "main",
                "task_kind": "sync_release_pr",
                "task_policy": {
                    "expected_base_sha": _SHA,
                    "release_sync": {
                        "source_branch": "development",
                        "target_branch": "main",
                    },
                },
            },
            "development",
        ),
    ],
)
async def test_preserved_adopted_and_release_source_checkouts_are_not_pinned(
    session_factory: async_sessionmaker[AsyncSession],
    create_kwargs: dict[str, Any],
    expected_base: str,
) -> None:
    if create_kwargs["task_kind"] == "feature_branch_pr":
        extra = {
            "pr_url": "https://github.com/example/app/pull/12",
            "pr_number": 12,
        }
    else:
        extra = {}
    git = _RecordingGit()
    launcher = _RecordingLauncher()
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git,  # type: ignore[arg-type]
        stack_launcher=launcher,  # type: ignore[arg-type]
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="https://github.com/example/app.git",
            task_title="pin",
            task_prompt="do not reset an existing PR",
            agent="codex",
            test_commands=[],
            **create_kwargs,
        )
        if extra:
            workspace.pr_url = extra["pr_url"]
            workspace.pr_number = extra["pr_number"]
        await session.commit()
        workspace_id = workspace.id

    with pytest.raises(GitOperationError):
        await provisioner.provision(workspace_id)

    assert launcher.calls == []
    assert "expected_base_sha" not in git.calls[0]
    assert git.calls[0]["base_branch"] == expected_base


class _ReadyGit:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def add_worktree(self, **kwargs: Any) -> WorktreeLayout:
        self.calls.append(kwargs)
        return WorktreeLayout(
            mirror_path=Path("/tmp/mirror.git"),
            worktree_path=Path("/tmp/worktree"),
            branch_name=str(kwargs["new_branch"]),
        )

    async def head_sha(self, **kwargs: Any) -> str:
        del kwargs
        return _SHA


@pytest.mark.unit
@pytest.mark.parametrize(
    ("preserve_pr", "inherited_evidence"),
    [
        (False, True),
        (False, False),
        (True, True),
    ],
)
async def test_fresh_retry_checkout_drops_inherited_launch_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    preserve_pr: bool,
    inherited_evidence: bool,
) -> None:
    evidence = {"sha": _SHA, "status": "verified"}
    policy: dict[str, Any] = {"expected_base_sha": _SHA, "unrelated": "kept"}
    if inherited_evidence:
        policy["expected_base_sha_first_launch"] = evidence
    git = _ReadyGit()
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git,  # type: ignore[arg-type]
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="https://github.com/example/app.git",
            branch_base="development",
            task_title="retry pin",
            task_prompt="recheck a fresh checkout",
            agent="codex",
            test_commands=[],
            task_kind="feature_branch_pr",
            task_policy=policy,
            resolved_profile={"name": "generic"},
        )
        if preserve_pr:
            workspace.pr_url = "https://github.com/example/app/pull/12"
            workspace.pr_number = 12
        await session.commit()
        workspace_id = workspace.id

    await provisioner.provision(workspace_id)

    async with session_factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row is not None
        assert row.status == WorkspaceStatus.ready.value
        assert row.task_policy["expected_base_sha"] == _SHA
        assert row.task_policy["unrelated"] == "kept"
        if preserve_pr and inherited_evidence:
            assert row.task_policy["expected_base_sha_first_launch"] == evidence
        else:
            assert "expected_base_sha_first_launch" not in row.task_policy
    if preserve_pr:
        assert "expected_base_sha" not in git.calls[0]
    else:
        assert git.calls[0]["expected_base_sha"] == _SHA
