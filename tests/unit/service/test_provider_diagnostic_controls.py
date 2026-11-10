"""No ordinary control can cause a paid diagnostic replay."""

import pytest

from awf.service.controls_errors import WorkspaceControlError
from awf.service.provider_diagnostics import reject_diagnostic_control


@pytest.mark.parametrize(
    "action", ["retry", "refresh", "validate", "rebase", "remonitor", "guide", "resume"]
)
def test_replay_controls_rejected(action):
    with pytest.raises(WorkspaceControlError, match="cannot be replayed"):
        reject_diagnostic_control({"provider_diagnostic": {"request": {}}}, action)


@pytest.mark.parametrize("action", ["cancel", "stop", "destroy"])
def test_safe_controls_allowed(action):
    reject_diagnostic_control({"provider_diagnostic": {"request": {}}}, action)
    reject_diagnostic_control({}, "retry")


async def test_actual_cancel_without_stop_flag_stops_diagnostic_and_retry_rejected(engine):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from awf.api.schemas_providers import ProviderConnectionTestRequest
    from awf.common.config import Settings
    from awf.db.session import make_session_factory
    from awf.node.cleanup import WorkspaceCleanupResult
    from awf.service.controls import WorkspaceControlService
    from awf.service.provider_diagnostics import create_connection_test
    from awf.service.workspaces import WorkspaceService
    from awf.service.workspaces_retry import retry_workspace_row
    from tests.unit.api.test_provider_connection_tests import capable

    await capable(engine)
    factory = make_session_factory(engine)
    response = await create_connection_test(
        WorkspaceService(factory, settings=Settings(_env_file=None)),
        ProviderConnectionTestRequest(agent="codex", provider="ollama", model="candidate:cloud"),
        idempotency_key="controls",
    )
    from awf.control.worker.cancelled_activity import reconcile_cancelled_activity
    from awf.db.repositories import OperationRepository, WorkspaceRepository

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        ws.node_id = "local"
        await session.commit()
    cleaner = SimpleNamespace(cleanup=AsyncMock(return_value=WorkspaceCleanupResult.from_steps([])))
    async with factory() as session:
        controls = WorkspaceControlService(
            session, project_stopper=AsyncMock(), cleaner_factory=lambda: cleaner
        )
        result = await controls.cancel_workspace(
            response.workspace_id, reason="test cancel", stop_stack=False
        )
        await session.commit()
        assert result.status == "cancelled"
    cleaner.cleanup.assert_awaited_once()
    worker = SimpleNamespace(
        _session_factory=factory, _config=SimpleNamespace(node_id="local"), _execution_tasks={}
    )
    await reconcile_cancelled_activity(worker, limit=10)
    async with factory() as session:
        operations = await OperationRepository(session).list_for_workspace(response.workspace_id)
        diagnostic_op = next(
            o for o in operations if (o.payload or {}).get("source") == "provider_connection_test"
        )
        assert diagnostic_op.status == "cancelled"
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        assert ws.task_policy["provider_diagnostic"]["result"]["outcome"] == "cancelled"

    async with factory() as session:
        with pytest.raises(WorkspaceControlError, match="cannot be replayed"):
            await retry_workspace_row(
                session, response.workspace_id, settings=Settings(_env_file=None)
            )


async def test_force_destroy_disposes_resources_and_settles_diagnostic_operation(engine):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from awf.api.schemas_providers import ProviderConnectionTestRequest
    from awf.common.config import Settings
    from awf.db.repositories import OperationRepository, WorkspaceRepository
    from awf.db.session import make_session_factory
    from awf.node.cleanup import WorkspaceCleanupResult
    from awf.service.controls import WorkspaceControlService
    from awf.service.provider_diagnostics import create_connection_test
    from awf.service.workspaces import WorkspaceService
    from tests.unit.api.test_provider_connection_tests import capable

    await capable(engine)
    factory = make_session_factory(engine)
    response = await create_connection_test(
        WorkspaceService(factory, settings=Settings(_env_file=None)),
        ProviderConnectionTestRequest(agent="codex", provider="ollama", model="candidate:cloud"),
        idempotency_key="destroy",
    )
    cleaner = SimpleNamespace(cleanup=AsyncMock(return_value=WorkspaceCleanupResult.from_steps([])))
    async with factory() as session:
        controls = WorkspaceControlService(
            session, project_stopper=AsyncMock(), cleaner_factory=lambda: cleaner
        )
        result = await controls.destroy_workspace(
            response.workspace_id, force=True, remove_volumes=False, remove_worktree=False
        )
        await session.commit()
        assert result.status == "destroyed"
    assert cleaner.cleanup.call_args.kwargs["remove_volumes"]
    assert cleaner.cleanup.call_args.kwargs["remove_worktree"]
    async with factory() as session:
        operations = await OperationRepository(session).list_for_workspace(response.workspace_id)
        diagnostic = next(
            op
            for op in operations
            if (op.payload or {}).get("source") == "provider_connection_test"
        )
        assert diagnostic.status == "cancelled"
        ws = await WorkspaceRepository(session).get(response.workspace_id)
        assert ws.task_policy["provider_diagnostic"]["result"]["outcome"] == "cancelled"
