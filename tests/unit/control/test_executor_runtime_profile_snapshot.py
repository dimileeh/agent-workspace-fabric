"""Executor runtime profile snapshot persistence tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.executor.helpers import _profile_for_workspace
from awf.control.executor.state_ops import _persist_resolved_profile_snapshot_if_missing
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.profiles.models import WorkspaceProfile
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _custom_planning_profile(name: str) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": name,
            "planning": {
                "required": True,
                "plan_path": "docs/alternate/{workspace_id}.md",
                "conformance_report_path": "docs/alternate/{workspace_id}.json",
            },
        }
    )


async def _create_workspace(
    session: AsyncSession,
    *,
    resolved_profile: dict | None = None,
) -> Workspace:
    workspace = await WorkspaceRepository(session).create(
        repo_url="git@github.com:example/app.git",
        branch_base="development",
        task_title="Runtime profile",
        task_prompt="Exercise runtime profile persistence.",
        agent=AgentRuntime.codex.value,
        test_commands=[],
        owned_paths=["docs/alternate/ws_aaaaaaaaaaaaaaaaaaaaaaaa.md"],
        profile_ref="auto",
        resolved_profile=resolved_profile,
    )
    workspace.status = WorkspaceStatus.running.value
    await session.commit()
    return workspace


@pytest.mark.unit
async def test_runtime_resolved_profile_snapshot_is_persisted_when_missing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    profile = _custom_planning_profile("repo-auto")
    async with factory() as session:
        workspace = await _create_workspace(session)
        workspace_id = workspace.id

    executor = SimpleNamespace(_session_factory=factory)
    await _persist_resolved_profile_snapshot_if_missing(
        executor,
        workspace_id=workspace_id,
        profile=profile,
    )

    async with factory() as session:
        reloaded = await WorkspaceRepository(session).get(workspace_id)

    assert reloaded is not None
    assert reloaded.resolved_profile is not None
    assert reloaded.resolved_profile["name"] == "repo-auto"
    assert reloaded.resolved_profile["planning"]["required"] is True
    assert reloaded.resolved_profile["planning"]["plan_path"] == "docs/alternate/{workspace_id}.md"


@pytest.mark.unit
async def test_runtime_profile_snapshot_does_not_replace_existing_snapshot(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    frozen_profile = _custom_planning_profile("frozen").model_dump(mode="json", by_alias=True)
    runtime_profile = _custom_planning_profile("repo-auto")
    async with factory() as session:
        workspace = await _create_workspace(session, resolved_profile=frozen_profile)
        workspace_id = workspace.id

    executor = SimpleNamespace(_session_factory=factory)
    await _persist_resolved_profile_snapshot_if_missing(
        executor,
        workspace_id=workspace_id,
        profile=runtime_profile,
    )

    async with factory() as session:
        reloaded = await WorkspaceRepository(session).get(workspace_id)

    assert reloaded is not None
    assert reloaded.resolved_profile == frozen_profile


@pytest.mark.unit
def test_profile_for_workspace_attaches_runtime_snapshot_to_workspace(
    tmp_path,
) -> None:
    profile_dir = tmp_path / ".awf"
    profile_dir.mkdir()
    (profile_dir / "workspace.yml").write_text(
        "\n".join(
            [
                "awf:",
                "  name: repo-auto",
                "  planning:",
                "    required: true",
                "    plan_path: docs/alternate/{workspace_id}.md",
                "    conformance_report_path: docs/alternate/{workspace_id}.json",
                "",
            ]
        ),
        encoding="utf-8",
    )
    workspace = Workspace(
        id="ws_runtime",
        status=WorkspaceStatus.running.value,
        repo_url="git@github.com:example/app.git",
        branch_base="development",
        task_title="Runtime profile",
        task_prompt="Resolve the repo profile.",
        agent=AgentRuntime.codex.value,
        test_commands=[],
        owned_paths=[],
        profile_ref="auto",
        resolved_profile=None,
    )

    profile = _profile_for_workspace(workspace, worktree_path=tmp_path)

    assert profile.name == "repo-auto"
    assert workspace.resolved_profile is not None
    assert workspace.resolved_profile["planning"]["plan_path"] == (
        "docs/alternate/{workspace_id}.md"
    )
