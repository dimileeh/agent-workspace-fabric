"""Provisioner resolves the final workspace.auto_merge from intent + profile.

The resolver runs once at provision (after the resolved profile is materialized)
and is the single shared call site for both create and adopt. It writes the final
``workspace.auto_merge`` the monitor reads. task_kind never affects it.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.auto_merge import AUTO_MERGE_INTENT_POLICY_KEY
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeProjectPaths
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from tests.postgres import postgres_test_engine

pytestmark = pytest.mark.unit


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@t"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    # A second base branch so by_base_branch can be exercised for a non-default base.
    _git(["branch", "main"], repo)
    return repo


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def git_manager(tmp_path: Path) -> GitManager:
    return GitManager(tmp_path / "awf-work")


class _RecordingStackLauncher:
    async def launch(self, request: Any) -> ComposeProjectPaths:
        del request
        return ComposeProjectPaths(
            project_dir=Path("/tmp/awf-compose/ws_am"),
            compose_file=Path("/tmp/awf-compose/ws_am/compose.yml"),
        )


def _profile(*, default: bool, by_base_branch: dict[str, bool]) -> dict[str, Any]:
    return {
        "name": "generic",
        "monitor": {"auto_merge": {"default": default, "by_base_branch": by_base_branch}},
    }


@pytest.mark.parametrize(
    ("base_branch", "profile", "intent", "expected"),
    [
        # by_base_branch exact hit for the default base -> True gate.
        ("development", _profile(default=False, by_base_branch={"development": True}), None, True),
        # by_base_branch exact hit for a non-default base -> False gate.
        (
            "main",
            _profile(default=True, by_base_branch={"development": True, "main": False}),
            None,
            False,
        ),
        # No base match -> repo global default.
        ("development", _profile(default=True, by_base_branch={}), None, True),
        # Terminal default: nothing set -> False.
        ("development", _profile(default=False, by_base_branch={}), None, False),
        # Explicit per-task intent overrides the profile config.
        ("development", _profile(default=False, by_base_branch={"development": False}), True, True),
        ("development", _profile(default=True, by_base_branch={"development": True}), False, False),
    ],
)
async def test_provision_resolves_auto_merge(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_repo: Path,
    base_branch: str,
    profile: dict[str, Any],
    intent: bool | None,
    expected: bool,
) -> None:
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        stack_launcher=_RecordingStackLauncher(),
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            repo_url=str(origin_repo),
            branch_base=base_branch,
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
            resolved_profile=profile,
            task_policy={AUTO_MERGE_INTENT_POLICY_KEY: intent},
            # Provisional value from create; the resolver must overwrite it.
            auto_merge=bool(intent) if intent is not None else False,
        )
        await s.commit()
        ws_id = ws.id

    await provisioner.provision(ws_id)

    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(ws_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.auto_merge is expected
        events = [e.event_type for e in reloaded.events]
        assert "workspace.auto_merge_resolved" in events
