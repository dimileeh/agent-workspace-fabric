"""Executor runtime profile snapshot persistence tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.executor import execution_flow as executor_execution_flow
from awf.control.executor import execution_validation as executor_execution_validation
from awf.control.executor import state_ops as executor_state_ops
from awf.control.executor.helpers import (
    _profile_for_workspace,
    _realign_profile_from_resolved_profile_snapshot,
)
from awf.control.executor.state_ops import (
    _persist_resolved_profile_snapshot_if_missing,
    _sync_resolved_profile,
)
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
async def test_runtime_profile_snapshot_returns_competing_snapshot_for_realigning_loser(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    competing_snapshot = _custom_planning_profile("first-worker").model_dump(
        mode="json",
        by_alias=True,
    )
    stale_worker_profile = _custom_planning_profile("stale-worker")
    async with factory() as session:
        workspace = await _create_workspace(session, resolved_profile=competing_snapshot)
        workspace_id = workspace.id

    executor = SimpleNamespace(_session_factory=factory)
    frozen_snapshot = await _persist_resolved_profile_snapshot_if_missing(
        executor,
        workspace_id=workspace_id,
        profile=stale_worker_profile,
    )

    async with factory() as session:
        reloaded = await WorkspaceRepository(session).get(workspace_id)

    assert reloaded is not None
    assert reloaded.resolved_profile == competing_snapshot
    assert frozen_snapshot == competing_snapshot


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
            self.scalar_calls = 0
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

        async def scalar(self, statement: object) -> dict | None:
            self.scalar_calls += 1
            compiled = str(statement)
            assert "SELECT workspaces.resolved_profile" in compiled
            assert "workspaces.id" in compiled
            return store["resolved_profile"]

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

    frozen_snapshot = await _persist_resolved_profile_snapshot_if_missing(
        executor,
        workspace_id="ws_profile_race",
        profile=stale_worker_profile,
    )

    session = session_factory.sessions[0]
    assert session.execute_calls == 1
    assert session.scalar_calls == 1
    assert session.commits == 0
    assert store["resolved_profile"] == competing_snapshot
    assert frozen_snapshot == competing_snapshot


@pytest.mark.unit
async def test_runtime_profile_snapshot_commits_json_string_returning_value() -> None:
    profile = _custom_planning_profile("repo-auto")
    snapshot = profile.model_dump(mode="json", by_alias=True)

    class FakeUpdateResult:
        def scalar_one_or_none(self) -> str:
            return json.dumps(snapshot)

    class ReturningStringSession:
        def __init__(self) -> None:
            self.commits = 0
            self.execute_calls = 0
            self.scalar_calls = 0

        async def __aenter__(self) -> ReturningStringSession:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def execute(self, statement: object) -> FakeUpdateResult:
            self.execute_calls += 1
            compiled = str(statement)
            assert "UPDATE workspaces" in compiled
            assert "RETURNING workspaces.resolved_profile" in compiled
            return FakeUpdateResult()

        async def scalar(self, statement: object) -> None:
            self.scalar_calls += 1

        async def commit(self) -> None:
            self.commits += 1

    class ReturningStringSessionFactory:
        def __init__(self) -> None:
            self.sessions: list[ReturningStringSession] = []

        def __call__(self) -> ReturningStringSession:
            session = ReturningStringSession()
            self.sessions.append(session)
            return session

    session_factory = ReturningStringSessionFactory()
    executor = SimpleNamespace(_session_factory=session_factory)

    frozen_snapshot = await _persist_resolved_profile_snapshot_if_missing(
        executor,
        workspace_id="ws_profile_returning_string",
        profile=profile,
    )

    session = session_factory.sessions[0]
    assert session.execute_calls == 1
    assert session.scalar_calls == 0
    assert session.commits == 1
    assert frozen_snapshot == snapshot


@pytest.mark.unit
async def test_runtime_profile_snapshot_logs_warning_for_unparseable_returning_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _custom_planning_profile("repo-auto")
    snapshot = profile.model_dump(mode="json", by_alias=True)
    warnings: list[tuple[str, dict[str, object]]] = []

    class OpaqueReturningValue:
        pass

    class RecordingLogger:
        def warning(self, event: str, **kwargs: object) -> None:
            warnings.append((event, kwargs))

    class FakeUpdateResult:
        def scalar_one_or_none(self) -> OpaqueReturningValue:
            return OpaqueReturningValue()

    class ReturningOpaqueSession:
        def __init__(self) -> None:
            self.commits = 0
            self.execute_calls = 0
            self.scalar_calls = 0

        async def __aenter__(self) -> ReturningOpaqueSession:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def execute(self, statement: object) -> FakeUpdateResult:
            self.execute_calls += 1
            compiled = str(statement)
            assert "UPDATE workspaces" in compiled
            assert "RETURNING workspaces.resolved_profile" in compiled
            return FakeUpdateResult()

        async def scalar(self, statement: object) -> None:
            self.scalar_calls += 1

        async def commit(self) -> None:
            self.commits += 1

    class ReturningOpaqueSessionFactory:
        def __init__(self) -> None:
            self.sessions: list[ReturningOpaqueSession] = []

        def __call__(self) -> ReturningOpaqueSession:
            session = ReturningOpaqueSession()
            self.sessions.append(session)
            return session

    monkeypatch.setattr(executor_state_ops, "_log", RecordingLogger())
    session_factory = ReturningOpaqueSessionFactory()
    executor = SimpleNamespace(_session_factory=session_factory)

    frozen_snapshot = await _persist_resolved_profile_snapshot_if_missing(
        executor,
        workspace_id="ws_profile_returning_opaque",
        profile=profile,
    )

    session = session_factory.sessions[0]
    assert session.execute_calls == 1
    assert session.scalar_calls == 0
    assert session.commits == 1
    assert frozen_snapshot == snapshot
    assert warnings == [
        (
            "executor.resolved_profile_returning_unparseable",
            {
                "workspace_id": "ws_profile_returning_opaque",
                "returned_type": "OpaqueReturningValue",
            },
        )
    ]


@pytest.mark.unit
async def test_sync_resolved_profile_returns_winning_snapshot_and_realigns_workspace(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    competing_snapshot = _custom_planning_profile("first-worker").model_dump(
        mode="json",
        by_alias=True,
    )
    competing_snapshot["planning"].pop("max_iterations")
    stale_worker_profile = _custom_planning_profile("stale-worker")
    stale_snapshot = stale_worker_profile.model_dump(mode="json", by_alias=True)
    async with factory() as session:
        persisted_workspace = await _create_workspace(
            session,
            resolved_profile=competing_snapshot,
        )
        workspace_id = persisted_workspace.id

    in_memory_workspace = Workspace(
        id=workspace_id,
        status=WorkspaceStatus.running.value,
        repo_url="git@github.com:example/app.git",
        branch_base="development",
        task_title="Runtime profile",
        task_prompt="Resolve the repo profile.",
        agent=AgentRuntime.codex.value,
        test_commands=[],
        owned_paths=[],
        profile_ref="auto",
        resolved_profile=stale_snapshot,
    )
    executor = SimpleNamespace(_session_factory=factory)

    profile = await _sync_resolved_profile(
        executor,
        ws=in_memory_workspace,
        workspace_id=workspace_id,
        profile=stale_worker_profile,
        planning_max_iterations_default=4,
    )

    assert profile.name == "first-worker"
    assert profile.planning.max_iterations == 4
    assert in_memory_workspace.resolved_profile == competing_snapshot


@pytest.mark.unit
async def test_execute_skips_profile_sync_when_snapshot_already_frozen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    frozen_snapshot = _custom_planning_profile("first-writer").model_dump(
        mode="json",
        by_alias=True,
    )
    workspace = Workspace(
        id="ws_frozen_profile",
        status=WorkspaceStatus.running.value,
        repo_url="git@github.com:example/app.git",
        branch_base="development",
        task_title="Runtime profile",
        task_prompt="Resolve the repo profile.",
        agent=AgentRuntime.codex.value,
        test_commands=[],
        owned_paths=[],
        profile_ref="auto",
        resolved_profile=frozen_snapshot,
    )
    events: list[str] = []

    class FrozenProfileExecutor:
        _config = SimpleNamespace(
            agent_idle_timeout_seconds=30,
            agent_wall_timeout_seconds=60,
            compose_projects_root=tmp_path / "compose",
            planning_max_iterations_default=6,
            worktrees_root=tmp_path / "worktrees",
        )
        _log_store = None
        _runner = object()
        _usage_sampler = None

        async def _begin_execution(
            self,
            *args: object,
            **kwargs: object,
        ) -> tuple[Workspace, bool, bool, None]:
            # Fresh-claim entry: workspace acquired + confirmed ``running``,
            # no blocked-resume skip, no disabled fix passes, no persisted baseline.
            return workspace, False, False, None

        async def _reject_unsupported_task_kind(
            self,
            *args: object,
            **kwargs: object,
        ) -> bool:
            return False

        async def _block_open_pr_reexecution_without_recovery(
            self,
            *args: object,
            **kwargs: object,
        ) -> object:
            return SimpleNamespace(blocked=False, recovery=None)

        async def _dispatch_non_feature_task_kind(
            self,
            *args: object,
            **kwargs: object,
        ) -> bool:
            return False

        async def _prepare_conformance_salvage_for_execution(
            self,
            *args: object,
            **kwargs: object,
        ) -> None:
            return None

        def _defaults_for(self, *args: object) -> None:
            return None

        async def _mark_failed(self, *args: object, **kwargs: object) -> None:
            events.append("failed")

    def fake_get_adapter(*args: object, **kwargs: object) -> object:
        events.append("adapter")
        return SimpleNamespace(runtime_scratch_paths=())

    async def fail_sync_resolved_profile(*args: object, **kwargs: object) -> WorkspaceProfile:
        raise AssertionError("frozen resolved_profile should skip persistence sync")

    async def fake_repair_agent_runtime_ownership(*args: object, **kwargs: object) -> bool:
        events.append("repair")
        return False

    monkeypatch.setattr(executor_execution_flow, "get_adapter", fake_get_adapter)
    monkeypatch.setattr(
        executor_execution_flow,
        "_sync_resolved_profile",
        fail_sync_resolved_profile,
    )
    monkeypatch.setattr(
        executor_execution_flow,
        "repair_agent_runtime_ownership",
        fake_repair_agent_runtime_ownership,
    )

    await executor_execution_flow.execute(
        FrozenProfileExecutor(),
        workspace.id,
    )

    assert events == ["adapter", "repair", "failed"]


@pytest.mark.unit
async def test_validation_cycle_syncs_profile_before_command_planning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[tuple[str, object]] = []
    runtime_profile = _custom_planning_profile("runtime-auto")
    frozen_profile = _custom_planning_profile("first-writer")
    workspace = Workspace(
        id="ws_validation_profile",
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

    class ValidationSyncExecutor:
        _config = SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=6,
        )

        async def _transition_if_current(self, *args: object, **kwargs: object) -> bool:
            events.append(("transition", kwargs["action"]))
            return True

        async def _recheck_status(self, *args: object, **kwargs: object) -> bool:
            events.append(("recheck", kwargs["action"]))
            return False

    def fake_profile_for_workspace(
        ws: Workspace,
        *,
        worktree_path: Path,
        planning_max_iterations_default: int,
    ) -> WorkspaceProfile:
        assert worktree_path == tmp_path
        events.append(("resolve", planning_max_iterations_default))
        return runtime_profile

    async def fake_sync_resolved_profile(
        self: object,
        *,
        ws: Workspace,
        workspace_id: str,
        profile: WorkspaceProfile,
        planning_max_iterations_default: int,
    ) -> WorkspaceProfile:
        assert workspace_id == workspace.id
        assert profile.name == "runtime-auto"
        ws.resolved_profile = frozen_profile.model_dump(mode="json", by_alias=True)
        events.append(("sync", planning_max_iterations_default))
        return frozen_profile

    def fake_profile_phase_command_plan(
        profile: WorkspaceProfile,
        phase_names: tuple[str, ...],
    ) -> list[object]:
        events.append(("plan", profile.name))
        assert phase_names == ("post_agent", "validate")
        return []

    def fake_validation_tier_for_workspace(
        ws: Workspace,
        profile: WorkspaceProfile,
    ) -> int:
        assert ws is workspace
        events.append(("tier", profile.name))
        return 1

    async def unused_git_in_worktree(argv: list[str]) -> object:
        raise AssertionError(f"git should not run after stale validation status: {argv}")

    monkeypatch.setattr(
        executor_execution_validation,
        "_profile_for_workspace",
        fake_profile_for_workspace,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_sync_resolved_profile",
        fake_sync_resolved_profile,
        raising=False,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "profile_phase_command_plan",
        fake_profile_phase_command_plan,
    )
    monkeypatch.setattr(
        executor_execution_validation,
        "_validation_tier_for_workspace",
        fake_validation_tier_for_workspace,
    )

    result = await executor_execution_validation.run_validation_and_fix_cycle(
        ValidationSyncExecutor(),
        workspace_id=workspace.id,
        ws=workspace,
        worktree_path=tmp_path,
        compose_project="awf_ws_validation_profile",
        compose_file=tmp_path / "compose.yml",
        base_commit="a" * 40,
        expected_branch="awf/ws_validation_profile",
        adapter=object(),
        default_model=None,
        baseline_coverage=None,
        planning_validation_handoff=None,
        recovery=None,
        rebase_recovery_result=None,
        git_in_worktree=unused_git_in_worktree,
    )

    assert result.stop is True
    assert events == [
        ("resolve", 6),
        ("sync", 6),
        ("transition", "start_validation"),
        ("plan", "first-writer"),
        ("tier", "first-writer"),
        ("recheck", "validate"),
    ]


@pytest.mark.unit
def test_profile_for_workspace_resolves_without_stamping_runtime_snapshot(
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
    assert workspace.resolved_profile is None


@pytest.mark.unit
async def test_sync_resolved_profile_stamps_runtime_snapshot_when_missing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime_profile = _custom_planning_profile("repo-auto")
    async with factory() as session:
        persisted_workspace = await _create_workspace(session)
        workspace_id = persisted_workspace.id

    in_memory_workspace = Workspace(
        id=workspace_id,
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
    executor = SimpleNamespace(_session_factory=factory)

    profile = await _sync_resolved_profile(
        executor,
        ws=in_memory_workspace,
        workspace_id=workspace_id,
        profile=runtime_profile,
    )

    async with factory() as session:
        reloaded = await WorkspaceRepository(session).get(workspace_id)

    expected_snapshot = runtime_profile.model_dump(mode="json", by_alias=True)
    assert profile.name == "repo-auto"
    assert in_memory_workspace.resolved_profile == expected_snapshot
    assert reloaded is not None
    assert reloaded.resolved_profile == expected_snapshot


@pytest.mark.unit
def test_realign_profile_from_resolved_snapshot_realigns_workspace_and_active_profile() -> None:
    stale_snapshot = _custom_planning_profile("stale-worker").model_dump(
        mode="json",
        by_alias=True,
    )
    competing_snapshot = _custom_planning_profile("first-worker").model_dump(
        mode="json",
        by_alias=True,
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
        resolved_profile=stale_snapshot,
    )

    profile = _realign_profile_from_resolved_profile_snapshot(
        workspace,
        competing_snapshot,
    )

    assert profile is not None
    assert profile.name == "first-worker"
    assert workspace.resolved_profile == competing_snapshot
