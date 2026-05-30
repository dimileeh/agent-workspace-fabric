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
async def test_runtime_profile_snapshot_atomic_update_preserves_competing_snapshot() -> None:
    competing_snapshot = _custom_planning_profile("first-worker").model_dump(
        mode="json",
        by_alias=True,
    )
    stale_worker_profile = _custom_planning_profile("stale-worker")
    store = {"resolved_profile": None}

    class FakeUpdateResult:
        def scalar_one_or_none(self) -> str | None:
            return None

    class RaceAwareSession:
        def __init__(self) -> None:
            self.info: dict[str, object] = {}
            self.bind = None
            self.commits = 0
            self.execute_calls = 0
            self.loaded_workspace: SimpleNamespace | None = None

        async def __aenter__(self) -> RaceAwareSession:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def get(self, model: object, workspace_id: str) -> SimpleNamespace:
            assert model is Workspace
            self.loaded_workspace = SimpleNamespace(
                id=workspace_id,
                resolved_profile=None,
            )
            store["resolved_profile"] = competing_snapshot
            return self.loaded_workspace

        async def execute(self, statement: object) -> FakeUpdateResult:
            self.execute_calls += 1
            compiled = str(statement)
            assert "workspaces.resolved_profile IS NULL" in compiled
            assert "workspaces.id" in compiled
            store["resolved_profile"] = competing_snapshot
            return FakeUpdateResult()

        async def commit(self) -> None:
            self.commits += 1
            if self.loaded_workspace is not None:
                store["resolved_profile"] = self.loaded_workspace.resolved_profile

    class RaceAwareSessionFactory:
        def __init__(self) -> None:
            self.sessions: list[RaceAwareSession] = []

        def __call__(self) -> RaceAwareSession:
            session = RaceAwareSession()
            self.sessions.append(session)
            return session

    session_factory = RaceAwareSessionFactory()
    executor = SimpleNamespace(_session_factory=session_factory)

    await _persist_resolved_profile_snapshot_if_missing(
        executor,
        workspace_id="ws_profile_race",
        profile=stale_worker_profile,
    )

    session = session_factory.sessions[0]
    assert session.execute_calls == 1
    assert session.commits == 0
    assert store["resolved_profile"] == competing_snapshot


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
