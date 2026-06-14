"""Service-driven cancel/stop run a FULL compose down (issue #588 / #583).

A sibling PR (#585) fixed the worker-driven terminal release; these tests pin
the SERVICE-driven cancel/stop path (``WorkspaceControlService``), which used to
record ``terminal_runtime_released`` after only a bare ``docker stop`` and so
leaked the agent + postgres containers and the ``awf-ws_<id>-net`` network while
holding the host port. The fix routes those paths through the shared
``WorkspaceCleaner`` (``docker compose down --remove-orphans -v``), preserving the
git worktree for inspection (``remove_worktree=False``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.repositories.base import (
    TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
    has_terminal_runtime_released_event,
)
from tests.postgres import postgres_test_session
from tests.unit.service.test_controls_lifecycle_parts.controls_lifecycle_helpers import (
    RecordingCleaner,
    _events,
    _operations,
    _service,
    _workspace,
    compose_down_failed_result,
    compose_down_succeeded_result,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


def _release_events(events: list) -> list:
    return [e for e in events if e.event_type == TERMINAL_RUNTIME_RELEASE_EVENT_TYPE]


@pytest.mark.unit
async def test_cancel_stop_stack_runs_full_compose_down_not_docker_stop(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    cleaner = RecordingCleaner(result=compose_down_succeeded_result())
    service, stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.cancel_workspace(
        workspace.id,
        reason="operator requested",
        stop_stack=True,
    )

    assert response.status == WorkspaceStatus.cancelled
    assert workspace.status == WorkspaceStatus.cancelled.value
    # The full compose down ran via the cleaner, NOT a bare docker stop.
    assert stopper.calls == []
    assert len(cleaner.calls) == 1
    call = cleaner.calls[0]
    assert call.compose_project_name == workspace.compose_project_name
    assert str(call.compose_file_path) == workspace.compose_file_path
    # The git worktree is preserved (cancel keeps the workspace for inspection)
    # while volumes/network are removed so the host port is freed.
    assert call.remove_worktree is False
    assert call.remove_volumes is True


@pytest.mark.unit
async def test_cancel_stop_stack_terminal_release_means_runtime_released(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.running)
    cleaner = RecordingCleaner(result=compose_down_succeeded_result())
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    await service.cancel_workspace(
        workspace.id,
        reason="operator requested",
        stop_stack=True,
    )
    events = await _events(session, workspace.id)
    operations = await _operations(session, workspace.id)
    release_events = _release_events(events)

    assert await has_terminal_runtime_released_event(session, workspace.id) is True
    assert len(release_events) == 1
    payload = release_events[0].payload
    assert payload is not None
    assert payload["compose_project_name"] == workspace.compose_project_name
    assert payload["compose_file_path"] == workspace.compose_file_path
    assert payload["workspace_status"] == WorkspaceStatus.cancelled.value
    assert payload["source"] == "cancel_workspace"
    # The event now carries the compose-down evidence: a succeeded compose_down step.
    assert payload["cleanup"]["status"] == "succeeded"
    assert any(
        step["name"] == "compose_down" and step["status"] == "succeeded"
        for step in payload["cleanup"]["completed_steps"]
    )
    # The operation result records the same teardown evidence.
    assert operations[0].status == OperationStatus.succeeded.value
    assert operations[0].result["status"] == WorkspaceStatus.cancelled.value
    assert operations[0].result["cleanup"]["status"] == "succeeded"


@pytest.mark.unit
async def test_stop_workspace_runs_full_compose_down(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.running)
    cleaner = RecordingCleaner(result=compose_down_succeeded_result())
    service, stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.stop_workspace(workspace.id, reason="halt")
    release_events = _release_events(await _events(session, workspace.id))

    assert response.status == WorkspaceStatus.cancelled
    assert stopper.calls == []
    assert len(cleaner.calls) == 1
    assert cleaner.calls[0].compose_project_name == workspace.compose_project_name
    assert cleaner.calls[0].remove_worktree is False
    assert len(release_events) == 1
    assert release_events[0].payload["source"] == "stop_workspace"


@pytest.mark.unit
async def test_stop_already_terminal_workspace_is_idempotent(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.completed)
    cleaner = RecordingCleaner(result=compose_down_succeeded_result())
    service, stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.stop_workspace(workspace.id, reason="already done")
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)

    # Idempotent: stopping an already-terminal workspace still tears the stack
    # down (no-op compose down) without error and keeps it terminal.
    assert response.status == WorkspaceStatus.completed
    assert workspace.status == WorkspaceStatus.completed.value
    assert stopper.calls == []
    assert len(cleaner.calls) == 1
    assert operations[0].status == OperationStatus.succeeded.value
    assert any(e.event_type == "workspace.stack_stopped" for e in events)
    assert await has_terminal_runtime_released_event(session, workspace.id) is True


@pytest.mark.unit
async def test_stop_absent_stack_is_no_op_success(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    # No compose project/file was ever stamped (e.g. provisioning never started).
    workspace.compose_project_name = None
    workspace.compose_file_path = None
    await session.flush()
    cleaner = RecordingCleaner(result=compose_down_succeeded_result())
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.stop_workspace(workspace.id, reason="halt")
    operations = await _operations(session, workspace.id)
    release_events = _release_events(await _events(session, workspace.id))

    assert response.status == WorkspaceStatus.cancelled
    assert len(cleaner.calls) == 1
    assert cleaner.calls[0].compose_project_name is None
    assert cleaner.calls[0].compose_file_path is None
    assert operations[0].status == OperationStatus.succeeded.value
    # No compose project means no host port to release, so no release event.
    assert release_events == []
    assert await has_terminal_runtime_released_event(session, workspace.id) is False


@pytest.mark.unit
async def test_cancel_stop_stack_compose_down_failure_is_surfaced(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    cleaner = RecordingCleaner(result=compose_down_failed_result(error="compose down denied"))
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    # Surfaced (operation failed), not swallowed and not raised: the workspace
    # still reaches terminal so it is not stranded mid-cancel.
    response = await service.cancel_workspace(
        workspace.id,
        reason="operator cancel",
        stop_stack=True,
    )
    operations = await _operations(session, workspace.id)
    audit_events = [
        e
        for e in await _events(session, workspace.id)
        if e.event_type == "workspace.audit.control_operation"
    ]
    release_events = _release_events(await _events(session, workspace.id))

    assert response.status == WorkspaceStatus.cancelled
    assert workspace.status == WorkspaceStatus.cancelled.value
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "STACK_STOP_FAILED"
    assert "compose down denied" in (operations[0].error_message or "")
    assert audit_events[0].payload["outcome"] == "failed"
    assert audit_events[0].payload["reason_code"] == "STACK_STOP_FAILED"
    # The runtime was NOT actually released, so no terminal release event.
    assert release_events == []
    assert await has_terminal_runtime_released_event(session, workspace.id) is False


@pytest.mark.unit
async def test_stop_compose_down_failure_is_surfaced(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.running)
    cleaner = RecordingCleaner(result=compose_down_failed_result())
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.stop_workspace(workspace.id, reason="halt")
    operations = await _operations(session, workspace.id)

    assert response.status == WorkspaceStatus.cancelled
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "STACK_STOP_FAILED"
    assert await has_terminal_runtime_released_event(session, workspace.id) is False


@pytest.mark.unit
async def test_destroy_still_removes_worktree_unlike_cancel_stop(
    session: AsyncSession,
) -> None:
    """Regression: destroy keeps removing the worktree; cancel/stop must not."""
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    cleaner = RecordingCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    await service.destroy_workspace(
        workspace.id,
        force=True,
        remove_volumes=True,
        remove_worktree=True,
    )

    assert len(cleaner.calls) == 1
    assert cleaner.calls[0].remove_worktree is True
