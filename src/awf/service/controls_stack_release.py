"""Service-driven runtime stack release extracted from :mod:`awf.service.controls`.

Keeps :class:`~awf.service.controls.WorkspaceControlService` under the
first-party file-size guardrail by housing the cancel/stop compose-down
teardown (issues #588 / #583) in a focused module. Behavior is unchanged; the
methods are wired back onto the service through
:class:`_WorkspaceStackReleaseMixin`, mirroring the delegate-mixin
decomposition used by the executor/worker orchestrators and the
operator-guidance control.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from awf.api.schemas import WorkspaceControlResponse
from awf.db.enums import OperationStatus
from awf.db.models import Operation, Workspace
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.repositories.base import (
    HOST_PORT_TERMINAL_RELEASE_STATUSES,
    TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
    TERMINAL_RUNTIME_RELEASE_REASON_CODE,
    has_terminal_runtime_released_event,
)
from awf.node.cleanup import WorkspaceCleanupResult
from awf.service.controls_errors import WorkspaceStackStopError
from awf.service.controls_helpers import (
    _add_control_audit_event,
    _control_response,
    _finish_stack_stop_failed_operation,
    _normalize_cleanup_result,
)


def _stack_stop_error_from_cleanup(
    cleanup_result: WorkspaceCleanupResult,
) -> WorkspaceStackStopError:
    """Synthesize a stack-stop error from a failed service-driven compose-down.

    Service-driven cancel/stop run the compose teardown through the shared
    cleaner (which records a failed ``compose_down`` step rather than raising).
    To surface that failure on the existing failed-operation path, rebuild a
    ``WorkspaceStackStopError`` carrying the step's error detail so the reason
    code and audit evidence stay identical to the legacy ``docker stop`` path.

    Only called when ``cleanup_result.ok`` is false. In the service-driven
    cancel/stop path the teardown preserves the worktree
    (``remove_worktree=False``) and passes no companions, so the only step that
    can fail is ``compose_down`` — the first (and sole) failed step, whose
    detail is surfaced. A non-ok result with no failed steps is not produced by
    the live path (``WorkspaceCleanupResult.from_steps`` makes a ``partial``
    status imply at least one failed step), but the value is representable when
    constructed directly or normalized from a mapping, so we fall back to the
    result reason code rather than indexing into an empty sequence.
    """
    failed_steps = cleanup_result.failed_steps
    if not failed_steps:
        stderr = cleanup_result.reason_code
    else:
        failed_step = failed_steps[0]
        stderr = failed_step.error or failed_step.reason_code
    return WorkspaceStackStopError(
        operation="compose down",
        returncode=1,
        stdout="",
        stderr=stderr,
    )


async def _release_runtime_stack(
    self: Any,
    workspace: Workspace,
) -> WorkspaceCleanupResult:
    """Tear down the workspace's compose stack via the shared cleaner.

    Service-driven cancel/stop reuse the same compose-down routine that
    destroy/completion use (``WorkspaceCleaner.cleanup`` ->
    ``docker compose down --remove-orphans -v``) instead of a bare
    ``docker stop``. That removes the containers *and* the per-workspace
    network and frees the host port, rather than leaving them ``Exited``
    behind a stopped-only stack (issue #588 / #583). The git worktree is
    preserved (``remove_worktree=False``) because cancel/stop keep the
    workspace for inspection; an absent or already-down project is a no-op
    success.
    """
    cleaner = self._cleaner_factory()
    return _normalize_cleanup_result(
        await cleaner.cleanup(
            workspace_id=workspace.id,
            repo_url=workspace.repo_url,
            companion_worktrees=(),
            compose_project_name=workspace.compose_project_name,
            compose_file_path=(
                Path(workspace.compose_file_path) if workspace.compose_file_path else None
            ),
            worktree_host_path=None,
            remove_volumes=True,
            remove_worktree=False,
        )
    )


async def _finalize_service_stack_release(
    self: Any,
    repo: WorkspaceRepository,
    operations: OperationRepository,
    operation: Operation,
    *,
    workspace: Workspace,
    cleanup_result: WorkspaceCleanupResult,
    source: str,
    action: str,
    reason_code: str,
    audit_extra: dict[str, object | None],
    success_message: str,
    failure_message: str,
) -> WorkspaceControlResponse:
    """Emit the terminal runtime release, then finish the operation.

    The workspace has already reached its terminal status by the time this
    runs. On a successful teardown the ``terminal_runtime_released`` event is
    emitted (gated on the overall cleanup status being ``succeeded``, matching
    the destroy success path) and the operation is finished succeeded with the
    teardown evidence attached. Gating on the result status — not on the
    presence of a ``compose_down`` step — keeps the legacy compat shapes
    (``_normalize_cleanup_result`` over a ``[]`` / minimal
    ``{"status": "succeeded"}`` result, which carry no compose-down step) from
    being stranded in the host-port conflict set; a failed compose-down makes
    the result non-``succeeded`` and is handled by the failure branch above,
    so the event still genuinely means "runtime released". The release is
    recorded on **any** successful terminal cleanup — including the
    both-null no-op case where neither ``compose_project_name`` nor
    ``compose_file_path`` was ever stamped — mirroring the worker terminal
    release sweep, which includes null-runtime terminal rows and records the
    release once the cleaner's default ``awf_<workspace_id>`` teardown
    succeeds. Recording it here too matters because the host-port conflict
    query still treats a terminal null-runtime row that carries a
    ``ResourceReservation`` (or a NULL ``node_id``) and no pre-launch-failure
    marker as a possible port holder: a ``requested``/``provisioning``
    workspace that reserved a host port but was cancelled before launch
    would otherwise stay in the conflict set until a later worker sweep
    finally records the release, defeating the prompt-release intent. On a
    compose-down
    failure no release event is emitted (the runtime was *not* released) and
    the operation is finished **failed** via the shared failed-operation
    helper so the teardown error is surfaced, not swallowed. The failure
    response carries ``failure_message`` (not ``success_message``) so the
    response message agrees with the failed ``operation_status`` instead of
    telling callers the stack was stopped when teardown actually failed.
    """
    if not cleanup_result.ok:
        await _finish_stack_stop_failed_operation(
            self._session,
            operations,
            operation,
            workspace=workspace,
            exc=_stack_stop_error_from_cleanup(cleanup_result),
        )
        return _control_response(
            workspace=workspace,
            operation=operation,
            message=failure_message,
        )
    runtime_cleanup_succeeded = cleanup_result.status == "succeeded"
    if (
        workspace.status in HOST_PORT_TERMINAL_RELEASE_STATUSES
        and runtime_cleanup_succeeded
        and not await has_terminal_runtime_released_event(self._session, workspace.id)
    ):
        await repo.add_event(
            workspace,
            event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            payload={
                "compose_project_name": workspace.compose_project_name,
                "compose_file_path": workspace.compose_file_path,
                "workspace_status": workspace.status,
                "cleanup": cleanup_result.to_dict(),
                "source": source,
            },
        )
    await operations.finish(
        operation,
        status=OperationStatus.succeeded,
        result={"status": workspace.status, "cleanup": cleanup_result.to_dict()},
    )
    await _add_control_audit_event(
        repo,
        workspace,
        operation=operation,
        action=action,
        outcome="succeeded",
        reason_code=reason_code,
        extra=audit_extra,
    )
    return _control_response(
        workspace=workspace,
        operation=operation,
        message=success_message,
    )


class _WorkspaceStackReleaseMixin:
    """Wires the extracted service-driven stack-release helpers onto the control service."""

    _release_runtime_stack = _release_runtime_stack
    _finalize_service_stack_release = _finalize_service_stack_release
