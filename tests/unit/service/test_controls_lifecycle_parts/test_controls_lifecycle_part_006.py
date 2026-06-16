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
async def test_cancel_stop_legacy_empty_success_records_runtime_released(
    session: AsyncSession,
) -> None:
    """A legacy cleaner that signals success with no compose-down step still releases.

    The compat path (``_normalize_cleanup_result`` over a ``[]`` / minimal
    ``{"status": "succeeded"}`` legacy result) produces a succeeded cleanup with
    no ``compose_down`` step. The teardown ran successfully, so a terminal
    workspace with a ``compose_project_name`` must emit
    ``terminal_runtime_released`` and leave the host-port conflict set — matching
    the destroy success path rather than being suppressed.
    """
    workspace = await _workspace(session, status=WorkspaceStatus.running)
    # Default RecordingCleaner with no result/failures returns ``[]`` -> a
    # succeeded cleanup with no compose_down step (the legacy compat shape).
    cleaner = RecordingCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    await service.cancel_workspace(
        workspace.id,
        reason="operator requested",
        stop_stack=True,
    )
    release_events = _release_events(await _events(session, workspace.id))

    assert len(release_events) == 1
    assert release_events[0].payload["source"] == "cancel_workspace"
    assert await has_terminal_runtime_released_event(session, workspace.id) is True


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
async def test_stop_absent_stack_records_runtime_released(
    session: AsyncSession,
) -> None:
    """A both-null no-op cleanup success still records ``terminal_runtime_released``.

    Even when neither ``compose_project_name`` nor ``compose_file_path`` was ever
    stamped (e.g. a ``requested``/``provisioning`` workspace cancelled before
    launch), the cleaner runs the default ``awf_<workspace_id>`` teardown and the
    host-port conflict query treats a terminal null-runtime row carrying a
    ``ResourceReservation`` (or a NULL ``node_id``) and no pre-launch-failure
    marker as a possible port holder. Recording the release here mirrors the
    worker terminal sweep (which includes null-runtime rows) and clears the port
    promptly instead of leaving it blocked until a later worker sweep.
    """
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
    # The default-project teardown succeeded, so the release is recorded to clear
    # any phantom host-port conflict — even with both locators null.
    assert len(release_events) == 1
    assert release_events[0].payload["compose_project_name"] is None
    assert release_events[0].payload["compose_file_path"] is None
    assert release_events[0].payload["source"] == "stop_workspace"
    assert await has_terminal_runtime_released_event(session, workspace.id) is True


@pytest.mark.unit
async def test_stop_compose_file_only_records_runtime_released(
    session: AsyncSession,
) -> None:
    """A legacy/partial workspace with a compose_file_path but null project releases.

    The cleaner derives the default ``awf_<workspace_id>`` project and tears the
    stack down, and ``find_host_port_conflicts`` treats a non-null
    ``compose_file_path`` as runtime evidence. Without recording the release the
    host port would stay blocked until a later worker sweep, so the file-path-only
    successful cleanup must emit ``terminal_runtime_released`` — mirroring the
    destroy/worker paths (the both-null no-op case is covered by
    ``test_stop_absent_stack_records_runtime_released``, which records the release
    for the same prompt-port-reclaim reason).
    """
    workspace = await _workspace(session, status=WorkspaceStatus.running)
    compose_file_path = workspace.compose_file_path
    assert compose_file_path is not None
    workspace.compose_project_name = None
    await session.flush()
    cleaner = RecordingCleaner(result=compose_down_succeeded_result())
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    await service.stop_workspace(workspace.id, reason="halt")
    release_events = _release_events(await _events(session, workspace.id))

    assert cleaner.calls[0].compose_project_name is None
    assert str(cleaner.calls[0].compose_file_path) == compose_file_path
    assert await has_terminal_runtime_released_event(session, workspace.id) is True
    assert len(release_events) == 1
    assert release_events[0].payload["compose_project_name"] is None
    assert release_events[0].payload["compose_file_path"] == compose_file_path
    assert release_events[0].payload["source"] == "stop_workspace"


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
    # The response message must agree with the failed operation status, not
    # claim the cancellation succeeded when stack teardown actually failed.
    assert response.operation_status == OperationStatus.failed.value
    assert response.message == "workspace cancelled but stack teardown failed"
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
    # The response message must reflect the failure, not the success string.
    assert response.operation_status == OperationStatus.failed.value
    assert response.message == "workspace stack stop failed"
    assert operations[0].error_code == "STACK_STOP_FAILED"
    assert await has_terminal_runtime_released_event(session, workspace.id) is False


@pytest.mark.unit
async def test_stop_terminal_workspace_compose_down_failure_skips_stack_stopped_event(
    session: AsyncSession,
) -> None:
    """A failed teardown of a terminal workspace must not emit ``stack_stopped``.

    For a non-active (already terminal) workspace the stop path records a
    ``workspace.stack_stopped`` event instead of a state transition. That event
    asserts the stack was stopped, so it must only be emitted once the compose
    down actually succeeds — otherwise observers would see a successful
    stack-stopped event for a teardown that the finalizer marks failed.
    """
    workspace = await _workspace(session, status=WorkspaceStatus.completed)
    cleaner = RecordingCleaner(result=compose_down_failed_result())
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.stop_workspace(workspace.id, reason="already done")
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    stack_stopped_events = [e for e in events if e.event_type == "workspace.stack_stopped"]

    assert response.status == WorkspaceStatus.completed
    assert workspace.status == WorkspaceStatus.completed.value
    assert operations[0].status == OperationStatus.failed.value
    assert response.operation_status == OperationStatus.failed.value
    assert response.message == "workspace stack stop failed"
    assert operations[0].error_code == "STACK_STOP_FAILED"
    # No success-asserting stack_stopped event for a failed teardown, and no
    # terminal runtime-release event since the runtime was not released.
    assert stack_stopped_events == []
    assert await has_terminal_runtime_released_event(session, workspace.id) is False


@pytest.mark.unit
async def test_cancel_failed_stack_release_replay_keeps_failure_message(
    session: AsyncSession,
) -> None:
    """Idempotent retry of a failed cancel teardown must not claim success.

    The first call records a ``failed`` operation (compose down failed) and
    returns the failure message. A retry with the same idempotency key is served
    from the stored failed operation, so the replay message must stay the
    failure string rather than the hard-coded success string — otherwise a
    client sees ``operation_status: failed`` with a message saying the
    cancellation was requested.
    """
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    cleaner = RecordingCleaner(result=compose_down_failed_result(error="compose down denied"))
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    first = await service.cancel_workspace(
        workspace.id,
        reason="operator cancel",
        stop_stack=True,
        idempotency_key="cancel-key",
    )
    replay = await service.cancel_workspace(
        workspace.id,
        reason="operator cancel",
        stop_stack=True,
        idempotency_key="cancel-key",
    )
    operations = await _operations(session, workspace.id)

    assert len(operations) == 1
    assert first.operation_status == OperationStatus.failed.value
    assert first.message == "workspace cancelled but stack teardown failed"
    assert replay.operation_id == first.operation_id
    assert replay.operation_status == OperationStatus.failed.value
    assert replay.message == "workspace cancelled but stack teardown failed"


@pytest.mark.unit
async def test_stop_failed_stack_release_replay_keeps_failure_message(
    session: AsyncSession,
) -> None:
    """Idempotent retry of a failed stop teardown must echo the failure message."""
    workspace = await _workspace(session, status=WorkspaceStatus.running)
    cleaner = RecordingCleaner(result=compose_down_failed_result())
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    first = await service.stop_workspace(
        workspace.id,
        reason="halt",
        idempotency_key="stop-key",
    )
    replay = await service.stop_workspace(
        workspace.id,
        reason="halt",
        idempotency_key="stop-key",
    )
    operations = await _operations(session, workspace.id)

    assert len(operations) == 1
    assert first.operation_status == OperationStatus.failed.value
    assert first.message == "workspace stack stop failed"
    assert replay.operation_id == first.operation_id
    assert replay.operation_status == OperationStatus.failed.value
    assert replay.message == "workspace stack stop failed"


@pytest.mark.unit
async def test_stop_succeeded_stack_release_replay_keeps_success_message(
    session: AsyncSession,
) -> None:
    """A successful teardown still replays the success message on retry."""
    workspace = await _workspace(session, status=WorkspaceStatus.running)
    cleaner = RecordingCleaner(result=compose_down_succeeded_result())
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    first = await service.stop_workspace(
        workspace.id,
        reason="halt",
        idempotency_key="stop-ok-key",
    )
    replay = await service.stop_workspace(
        workspace.id,
        reason="halt",
        idempotency_key="stop-ok-key",
    )

    assert first.operation_status == OperationStatus.succeeded.value
    assert first.message == "workspace stack stopped"
    assert replay.operation_id == first.operation_id
    assert replay.operation_status == OperationStatus.succeeded.value
    assert replay.message == "workspace stack stopped"


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


@pytest.mark.unit
async def test_stop_blocked_workspace_cancels_before_stack_teardown(
    session: AsyncSession,
) -> None:
    """Regression: ``blocked`` is active, so stop routes it through ``cancelled``.

    ``blocked`` is a non-terminal pause that still holds a warm stack and
    execution claim. If it were not classified as active, stop would tear the
    stack down but leave the row ``blocked`` pointing at removed resources.
    """
    workspace = await _workspace(session, status=WorkspaceStatus.blocked)
    cleaner = RecordingCleaner(result=compose_down_succeeded_result())
    service, stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.stop_workspace(workspace.id, reason="halt")

    assert response.status == WorkspaceStatus.cancelled
    assert workspace.status == WorkspaceStatus.cancelled.value
    assert stopper.calls == []
    assert len(cleaner.calls) == 1
    release_events = _release_events(await _events(session, workspace.id))
    assert len(release_events) == 1
    assert release_events[0].payload["workspace_status"] == WorkspaceStatus.cancelled.value


@pytest.mark.unit
async def test_destroy_blocked_workspace_requires_force(
    session: AsyncSession,
) -> None:
    """Regression: ``blocked`` is active, so destroy honours the ``force`` guard."""
    from awf.service.controls import ActiveWorkspaceDestroyError

    workspace = await _workspace(session, status=WorkspaceStatus.blocked)
    cleaner = RecordingCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    with pytest.raises(ActiveWorkspaceDestroyError):
        await service.destroy_workspace(
            workspace.id,
            force=False,
            remove_volumes=True,
            remove_worktree=True,
        )

    assert cleaner.calls == []
    assert await _operations(session, workspace.id) == []


@pytest.mark.unit
async def test_destroy_blocked_workspace_with_force_routes_through_cancelled(
    session: AsyncSession,
) -> None:
    """Regression: forced destroy of ``blocked`` routes cancelled → destroying → destroyed.

    ``blocked`` cannot transition directly to ``destroying``; classifying it as
    active routes it through ``cancelled`` first so cleanup never runs while the
    row stays ``blocked``.
    """
    workspace = await _workspace(session, status=WorkspaceStatus.blocked)
    cleaner = RecordingCleaner(result=compose_down_succeeded_result())
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=True,
        remove_volumes=True,
        remove_worktree=True,
    )

    assert response.status == WorkspaceStatus.destroyed
    assert workspace.status == WorkspaceStatus.destroyed.value
    assert len(cleaner.calls) == 1
