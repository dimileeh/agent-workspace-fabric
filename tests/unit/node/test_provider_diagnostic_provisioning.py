"""Diagnostic provisioning branches before project hooks and forge access."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.api.schemas_providers import ProviderConnectionTestRequest
from awf.common.config import Settings
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.service.provider_diagnostics import create_connection_test
from awf.service.workspaces import WorkspaceService
from tests.unit.api.test_provider_connection_tests import capable


async def test_early_provisioning_and_managed_fixture(engine, tmp_path):
    await capable(engine)
    factory = make_session_factory(engine)
    response = await create_connection_test(
        WorkspaceService(factory, settings=Settings(_env_file=None)),
        ProviderConnectionTestRequest(agent="codex", provider="ollama", model="candidate:cloud"),
        idempotency_key="provision",
    )
    launcher = SimpleNamespace(launch=AsyncMock(return_value=None))
    before = AsyncMock(side_effect=AssertionError("project preflight"))
    provisioner = Provisioner(
        session_factory=factory,
        git=GitManager(tmp_path),
        config=ProvisionerConfig(node_id="local"),
        stack_launcher=launcher,
        before_provision=before,
    )
    await provisioner.provision(response.workspace_id)
    before.assert_not_awaited()
    request = launcher.launch.call_args.args[0]
    assert request.diagnostic
    assert request.execution.provider == "ollama"
    assert (request.layout.worktree_path / ".git").is_file()
    assert (request.layout.worktree_path / "solution.py").is_file()
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        assert ws.status == WorkspaceStatus.ready.value
        assert ws.pr_url is None
    from awf.service.provider_diagnostics import INTERNAL_REPO

    await provisioner._git.remove_worktree(
        workspace_id=response.workspace_id, repo_url=INTERNAL_REPO
    )
    assert not request.layout.worktree_path.exists()


async def test_missing_runtime_settles_bootstrap_operation(engine, tmp_path):
    from awf.db.repositories import OperationRepository

    await capable(engine)
    factory = make_session_factory(engine)
    response = await create_connection_test(
        WorkspaceService(factory, settings=Settings(_env_file=None)),
        ProviderConnectionTestRequest(agent="codex", provider="ollama", model="candidate:cloud"),
        idempotency_key="no-runtime",
    )
    provisioner = Provisioner(
        session_factory=factory, git=GitManager(tmp_path), config=ProvisionerConfig(node_id="local")
    )
    await provisioner.provision(response.workspace_id)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        assert ws.status == "failed"
        assert ws.task_policy["provider_diagnostic"]["result"]["outcome"] == "inconclusive"
        operation = (await OperationRepository(session).list_for_workspace(ws.id))[0]
        assert operation.status == "failed"


@pytest.mark.parametrize("remove_allowed", [True, False])
async def test_cancel_while_fixture_is_created_removes_late_fixture_without_launch(
    engine, tmp_path, monkeypatch, remove_allowed
):
    import awf.node.provider_diagnostics as diagnostics

    await capable(engine)
    factory = make_session_factory(engine)
    response = await create_connection_test(
        WorkspaceService(factory, settings=Settings(_env_file=None)),
        ProviderConnectionTestRequest(agent="codex", provider="ollama", model="candidate:cloud"),
        idempotency_key="cancel-provision",
    )
    real_fixture = diagnostics.fixture_worktree

    async def cancelled_fixture(*args):
        layout = await real_fixture(*args)
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(response.workspace_id)
            await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="cancel-in-fixture")
            await session.commit()
        return layout

    monkeypatch.setattr(diagnostics, "fixture_worktree", cancelled_fixture)
    launcher = SimpleNamespace(launch=AsyncMock())
    provisioner = Provisioner(
        session_factory=factory,
        git=GitManager(tmp_path),
        config=ProvisionerConfig(node_id="local"),
        stack_launcher=launcher,
    )
    if not remove_allowed:
        from awf.node.git_manager import GitOperationError

        monkeypatch.setattr(
            provisioner._git,
            "remove_worktree",
            AsyncMock(
                side_effect=GitOperationError(
                    operation="remove",
                    returncode=1,
                    stdout="",
                    stderr="fixture protected",
                    reason_code="GIT_WORKTREE_UNOWNED",
                )
            ),
        )
    await provisioner.provision(response.workspace_id)
    launcher.launch.assert_not_awaited()
    assert (tmp_path / "worktrees" / response.workspace_id).exists() is not remove_allowed
    if not remove_allowed:
        from sqlalchemy import select

        from awf.db.models import WorkspaceEvent

        async with factory() as session:
            event = (
                await session.scalars(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == response.workspace_id,
                        WorkspaceEvent.event_type == "workspace.terminal_runtime_release_revoked",
                    )
                )
            ).one()
            assert event.payload["cleanup_reason"] == "GIT_WORKTREE_UNOWNED"


async def test_cancel_during_stack_launch_stops_runtime_before_fixture_disposal(engine, tmp_path):
    from awf.db.repositories import OperationRepository

    await capable(engine)
    factory = make_session_factory(engine)
    response = await create_connection_test(
        WorkspaceService(factory, settings=Settings(_env_file=None)),
        ProviderConnectionTestRequest(agent="codex", provider="ollama", model="candidate:cloud"),
        idempotency_key="cancel-launch",
    )
    fixture = tmp_path / "worktrees" / response.workspace_id

    async def down(*_args, **kwargs):
        assert fixture.exists()
        assert kwargs["remove_volumes"]
        return True

    compose = SimpleNamespace(down=AsyncMock(side_effect=down), remove_project_by_label=AsyncMock())

    async def launch(_request):
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(response.workspace_id)
            await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="cancel-in-launch")
            await session.commit()
        return

    launcher = SimpleNamespace(launch=launch, _compose=compose)
    provisioner = Provisioner(
        session_factory=factory,
        git=GitManager(tmp_path),
        config=ProvisionerConfig(node_id="local"),
        stack_launcher=launcher,
    )
    await provisioner.provision(response.workspace_id)
    compose.down.assert_awaited_once()
    assert not fixture.exists()
    async with factory() as session:
        ops = await OperationRepository(session).list_for_workspace(response.workspace_id)
        assert (
            next(
                op for op in ops if (op.payload or {}).get("source") == "provider_connection_test"
            ).status
            == "cancelled"
        )
