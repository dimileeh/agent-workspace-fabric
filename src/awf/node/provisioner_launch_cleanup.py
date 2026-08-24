"""Extracted Provisioner launch-race cleanup helpers.

Mechanically moved from ``awf.node.provisioner`` to keep that module under the
first-party line-count guardrail. Behavior is unchanged; the functions take
``self`` and are wired back onto :class:`~awf.node.provisioner.Provisioner` via
:class:`ProvisionerLaunchCleanupMixin`.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final, cast

from sqlalchemy import func, select

from awf.common.logging import get_logger
from awf.db.enums import WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.repositories.base import (
    PROVISIONING_LAUNCHING_EVENT_TYPE,
    PROVISIONING_LAUNCHING_REASON_CODE,
    TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
    TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
    has_terminal_runtime_released_event,
)
from awf.service.controls_helpers import stop_project_containers

_log = get_logger(__name__)

_MAX_REVOKE_EVENTS: Final = 3
"""Maximum lifetime-total revoke events before recording an operator escalation event."""

_ORPHAN_STOP_TIMEOUT_SECONDS: Final = 30.0
"""Maximum time to spend stopping orphan containers after launch races cleanup."""


async def _launch_lost_to_terminal_cleanup_best_effort(
    self: Any,
    workspace_id: str,
    *,
    failure_context: str,
) -> bool:
    """Run the launch-cleanup race check without masking failure handling.

    This wrapper is only for exception handlers. The normal post-launch
    success path should keep calling `_launch_lost_to_terminal_cleanup`
    directly so an indeterminate cleanup check cannot incorrectly proceed
    to `ready`.
    """
    try:
        return cast(bool, await self._launch_lost_to_terminal_cleanup(workspace_id))
    except Exception:
        _log.exception(
            "provisioner.launch_lost_to_terminal_cleanup_check_failed",
            workspace_id=workspace_id,
            failure_context=failure_context,
            reason_code="TERMINAL_CLEANUP_CHECK_FAILED",
        )
        return False


async def _recheck_before_launch(self: Any, workspace_id: str) -> bool:
    """Recheck workspace status with a row lock and record a launch guard.

    Unlike :meth:`_recheck_status`, this method holds a ``SELECT FOR UPDATE``
    row lock while checking status and recording a
    ``workspace.provisioning_launching`` event in the same transaction.  The
    row lock serializes with concurrent cancel/stop/destroy operations that
    also read the workspace row before transitioning it to a terminal state.

    The current cancel/stop control operations use a *synchronous* project
    stopper that blocks until containers are fully down, which guarantees the
    stack is no longer consuming host ports before the ``terminal_runtime_released``
    event is committed.  The ``provisioning_launching`` event serves as an
    audit trail marker but is not currently read by cancel/stop logic; a
    future async stopper would need to check this event before emitting
    ``terminal_runtime_released`` to avoid a race with a launch that has
    already committed.

    Returns ``True`` when the workspace is still ``provisioning`` and the
    launch-guard event was recorded; ``False`` otherwise.
    """
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(workspace_id)
        if ws is None:
            _log.warning(
                "provisioner.skip_unknown_before_launch",
                workspace_id=workspace_id,
            )
            await session.commit()
            return False
        if ws.status != WorkspaceStatus.provisioning.value:
            await self._record_stale_action_skip(
                repo,
                ws,
                action="provision",
                expected=WorkspaceStatus.provisioning,
                reason_code="PROVISIONER_STALE_STATUS",
            )
            await session.commit()
            return False
        await repo.add_event(
            ws,
            event_type=PROVISIONING_LAUNCHING_EVENT_TYPE,
            reason_code=PROVISIONING_LAUNCHING_REASON_CODE,
            payload={"workspace_id": workspace_id},
        )
        await session.commit()
        return True


async def _launch_lost_to_terminal_cleanup(self: Any, workspace_id: str) -> bool:
    """Check whether terminal cleanup won while the stack was launching.

    When an operator force-destroys the workspace after
    ``_recheck_before_launch`` commits its ``provisioning_launching``
    guard but before ``_stack_launcher.launch`` actually starts,
    ``destroy_workspace(force=True)`` can see the pre-published
    ``compose_project_name``, run cleanup before any containers exist,
    transition to ``destroyed``, and record
    ``workspace.terminal_runtime_released``.  The provisioner then
    still launches the stack, leaving running containers that future
    host-port admission ignores because the release event exists.

    This method detects that outcome: if ``terminal_runtime_released``
    was recorded while we were launching, stop the just-launched
    containers and return ``True`` so the caller aborts without
    transitioning to ``ready``.

    The DB session is released before Docker I/O and reacquired
    afterwards so that a slow or unresponsive Docker daemon does not
    hold a pool connection.  The row lock (``get_for_update``) is
    acquired only for the brief ``add_event`` / ``commit`` step
    after Docker I/O completes.

    Mitigations for pool exhaustion: (1) the DB session is released
    before Docker I/O; (2) ``stop_project_containers`` is bounded by
    ``_ORPHAN_STOP_TIMEOUT_SECONDS``; (3) the pool ``max_size`` should
    account for at most one concurrent orphan-stop per node.

    Returns ``True`` when terminal cleanup won and containers were
    stopped; ``False`` when the workspace is still clear to proceed.
    """
    compose_project = f"awf_{workspace_id}"
    prior_status: str | None = None
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None:
            return False
        released = await has_terminal_runtime_released_event(session, workspace_id)
        if not released:
            return False
        prior_status = ws.status

    _log.warning(
        "provisioner.launch_lost_to_terminal_cleanup",
        workspace_id=workspace_id,
        reason_code="TERMINAL_CLEANUP_WON_DURING_LAUNCH",
    )
    orphan_stopped = True
    orphan_stop_error: str | None = None
    try:
        await asyncio.wait_for(
            stop_project_containers(compose_project),
            timeout=_ORPHAN_STOP_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        orphan_stopped = False
        orphan_stop_error = (
            f"stop_project_containers timed out after {_ORPHAN_STOP_TIMEOUT_SECONDS:g}s"
        )
        _log.warning(
            "provisioner.orphan_container_stop_timeout",
            workspace_id=workspace_id,
            reason_code="ORPHAN_STOP_TIMEOUT",
            timeout_seconds=_ORPHAN_STOP_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        orphan_stopped = False
        orphan_stop_error = str(exc)
        _log.exception(
            "provisioner.orphan_container_stop_failed",
            workspace_id=workspace_id,
            reason_code="ORPHAN_STOP_FAILED",
        )
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(workspace_id)
        if ws is None:
            return True
        payload: dict[str, object] = {
            "action": "provision",
            "expected_status": WorkspaceStatus.provisioning.value,
            "actual_status": prior_status,
            "orphan_containers_stopped": orphan_stopped,
        }
        if orphan_stop_error is not None:
            payload["orphan_stop_error"] = orphan_stop_error
            revoke_count_result = await session.execute(
                select(func.count()).where(
                    WorkspaceEvent.workspace_id == workspace_id,
                    WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
                    WorkspaceEvent.reason_code == TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
                )
            )
            revoke_count = revoke_count_result.scalar() or 0
            await repo.add_event(
                ws,
                event_type=TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
                reason_code=TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
                payload={
                    "workspace_id": workspace_id,
                    "orphan_stop_error": orphan_stop_error,
                },
            )
            if revoke_count + 1 >= _MAX_REVOKE_EVENTS:
                await repo.add_event(
                    ws,
                    event_type="workspace.stale_action_skipped",
                    reason_code="REVOKE_CAP_REACHED",
                    payload={
                        "workspace_id": workspace_id,
                        "revoke_count": revoke_count + 1,
                        "orphan_stop_error": orphan_stop_error,
                        "message": (
                            f"{revoke_count + 1} lifetime-total revoke events; "
                            "operator intervention may be required to stop "
                            "orphan containers and free host ports. "
                            "Revoke events will continue to be recorded "
                            "until the runtime is released."
                        ),
                    },
                )
        await repo.add_event(
            ws,
            event_type="workspace.stale_action_skipped",
            reason_code="TERMINAL_CLEANUP_WON_DURING_LAUNCH",
            payload=payload,
        )
        await session.commit()
    return True


class ProvisionerLaunchCleanupMixin:
    """Launch-race cleanup helpers mechanically delegated from ``Provisioner``."""

    _launch_lost_to_terminal_cleanup_best_effort = _launch_lost_to_terminal_cleanup_best_effort
    _recheck_before_launch = _recheck_before_launch
    _launch_lost_to_terminal_cleanup = _launch_lost_to_terminal_cleanup
