"""Replay profile setup when a compose ``up`` genuinely recreated the agent runtime.

Profile ``setup``/``pre_agent`` phases can install state inside the agent
container (for example a tool under ``/tmp``). Monitor resume and agent-service
recovery bring the runtime back with ``compose up``; when that recreates the
agent container, everything setup wrote there is gone and later ``validate``
commands fail with 127 (#787, #1006).

Recreation is detected from the agent container's identity (container id and
``State.StartedAt``) captured before and after the ``up``, never from the
``force_recreate`` flag. When identity cannot be read, the env-health probe of
the blocked-resume path (#743) decides, with its "on any doubt, re-run setup"
rule (#746). One ``workspace.runtime_setup_replayed`` event is persisted per
recreation, and an existing event for the same identity suppresses a second run.

This module never transitions the workspace. Callers apply their own failure
convention to an unsuccessful ``SetupReplayOutcome``.
"""

from __future__ import annotations

import enum
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.audit import redact_audit_text
from awf.common.compose_exec import ComposeExecCleanupError, cleanup_failure_message
from awf.common.logging import get_logger
from awf.control.executor.constants import (
    RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE,
    RUNTIME_SETUP_REPLAYED_EVENT_TYPE,
    RUNTIME_SETUP_REPLAYED_REASON_CODE,
)
from awf.control.executor.helpers import _failure_reason_for_phase
from awf.control.executor.logging_ops import _setup_dependency_network_failure_details
from awf.control.executor.state_ops import _blocked_resume_setup_phase_names
from awf.db.enums import FailureReason
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.node.compose_manager import ComposeOperationError
from awf.runtime.inspection import RuntimeInspector
from awf.runtime.ownership import (
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    repair_agent_runtime_ownership,
)

_log = get_logger(__name__)

SETUP_REPLAY_PHASE_NAMES: Final[tuple[str, ...]] = ("setup", "pre_agent")
_AGENT_SERVICE_NAME: Final[str] = "agent"
_OUTPUT_TAIL_CHARS: Final[int] = 2000


@dataclass(frozen=True)
class AgentContainerIdentity:
    """Identity of one agent container incarnation."""

    container_id: str
    started_at: str | None = None


class IdentityUnavailable(enum.Enum):
    """Sentinel: the agent container identity could not be read."""

    UNAVAILABLE = "unavailable"


IDENTITY_UNAVAILABLE: Final = IdentityUnavailable.UNAVAILABLE

AgentIdentityObservation = AgentContainerIdentity | IdentityUnavailable | None
"""An identity, ``None`` (stack readable, no agent container), or unavailable."""


class RecreationDecision(enum.StrEnum):
    recreated = "recreated"
    unchanged = "unchanged"
    unknown = "unknown"


@dataclass(frozen=True)
class RecreationVerdict:
    decision: RecreationDecision
    basis: str


@dataclass(frozen=True)
class SetupReplayOutcome:
    """Result of ``replay_setup_if_recreated``; ``ok=False`` carries the failure."""

    ok: bool
    replayed: bool
    reason_code: str | None = None
    failure_reason: FailureReason | None = None
    message: str | None = None
    details: dict[str, Any] | None = None


async def capture_agent_container_identity(
    compose_project: str | None,
    *,
    inspector: RuntimeInspector | None = None,
) -> AgentIdentityObservation:
    """Read the agent container's id and ``StartedAt`` through compose labels."""
    try:
        snapshot = await (inspector or RuntimeInspector()).inspect(compose_project)
    except (OSError, RuntimeError, ValueError) as exc:
        _log.warning(
            "executor.runtime_identity_unavailable",
            compose_project=compose_project,
            error=redact_audit_text(repr(exc), limit=500),
        )
        return IDENTITY_UNAVAILABLE
    if snapshot.stack_state in {"unknown", "unavailable"}:
        return IDENTITY_UNAVAILABLE
    service = next(
        (item for item in snapshot.services if item.name == _AGENT_SERVICE_NAME),
        None,
    )
    if service is None:
        # Mirror ``agent_service_health_from_snapshot``: unlabeled containers
        # mean the agent may exist but cannot be recognized.
        if any(not item.service_label_available for item in snapshot.services):
            return IDENTITY_UNAVAILABLE
        return None
    if not service.container_id:
        return IDENTITY_UNAVAILABLE
    return AgentContainerIdentity(container_id=service.container_id, started_at=service.started_at)


def runtime_recreation_decision(
    before: AgentIdentityObservation,
    after: AgentIdentityObservation,
) -> RecreationVerdict:
    """Decide whether the ``up`` between two identity observations recreated the agent."""
    if after is IDENTITY_UNAVAILABLE:
        return RecreationVerdict(RecreationDecision.unknown, "identity_unavailable")
    if after is None:
        # No agent container after ``up``: nothing to provision. The existing
        # compose/health failure paths own that case.
        return RecreationVerdict(RecreationDecision.unchanged, "no_agent_container")
    if before is None:
        return RecreationVerdict(RecreationDecision.recreated, "agent_container_created")
    if before is IDENTITY_UNAVAILABLE:
        return RecreationVerdict(RecreationDecision.recreated, "previous_identity_unavailable")
    if before == after:
        return RecreationVerdict(RecreationDecision.unchanged, "identity_unchanged")
    return RecreationVerdict(RecreationDecision.recreated, "identity_changed")


async def replay_setup_if_recreated(
    self: Any,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    validation: Any,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    profile: Any,
    worktree_path: Path,
    before: AgentIdentityObservation,
    after: AgentIdentityObservation,
    trigger: str,
    ownership_event_name: str,
) -> SetupReplayOutcome:
    """Replay ``setup``/``pre_agent`` once when the agent container was recreated."""
    verdict = runtime_recreation_decision(before, after)
    if verdict.decision is RecreationDecision.unchanged:
        return SetupReplayOutcome(ok=True, replayed=False)
    if verdict.decision is RecreationDecision.unknown:
        phase_names = await _blocked_resume_setup_phase_names(
            self,
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            profile=profile,
            validation=validation,
            worktree_path=worktree_path,
        )
        if "setup" not in phase_names:
            return SetupReplayOutcome(ok=True, replayed=False)
    elif isinstance(after, AgentContainerIdentity) and await _replay_already_recorded(
        session_factory, workspace_id=workspace_id, identity=after
    ):
        _log.info(
            "executor.runtime_setup_replay_already_done",
            workspace_id=workspace_id,
            trigger=trigger,
            container_id=after.container_id,
        )
        return SetupReplayOutcome(ok=True, replayed=False)

    context: dict[str, Any] = {"trigger": trigger, "basis": verdict.basis}
    _log.info(
        "executor.runtime_setup_replay_started",
        workspace_id=workspace_id,
        **context,
    )
    if not await repair_agent_runtime_ownership(
        logger=_log,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="runtime_setup_replay",
        event_name=ownership_event_name,
        reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    ):
        return _failed(
            {**context, "cause_reason_code": AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE},
            workspace_id=workspace_id,
            failure_reason=FailureReason.infrastructure_failure,
            message="runtime setup replay failed: agent runtime ownership repair failed",
        )
    try:
        result = await validation.run_profile_phases(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            profile=profile,
            phase_names=SETUP_REPLAY_PHASE_NAMES,
            worktree_path=worktree_path,
        )
    except ComposeExecCleanupError as exc:
        return _failed(
            {**context, "cause_reason_code": exc.reason_code},
            workspace_id=workspace_id,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"runtime setup replay failed: {cleanup_failure_message(exc)}",
        )
    except (ComposeOperationError, OSError, RuntimeError, ValueError) as exc:
        # Runner errors (compose exec, OS, timeout) surface as the replay's own
        # reason code instead of the caller's generic handler; the redacted repr
        # preserves the diagnosis.
        safe_error = redact_audit_text(repr(exc), limit=1900)
        return _failed(
            {**context, "error": safe_error},
            workspace_id=workspace_id,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"runtime setup replay failed: {safe_error}",
        )
    if not result.all_passed:
        first_fail = result.first_failure
        details = {**context, **_first_failure_details(first_fail)}
        dependency_details = _setup_dependency_network_failure_details(first_fail)
        if dependency_details is not None:
            details["setup_dependency_network"] = dependency_details
        command = details.get("command")
        return _failed(
            details,
            workspace_id=workspace_id,
            failure_reason=_failure_reason_for_phase(first_fail),
            message=f"runtime setup replay failed: {command or 'profile setup failed'}",
        )
    await _record_replayed_event(
        session_factory,
        workspace_id=workspace_id,
        payload={
            **context,
            "container_id": _container_id(after),
            "started_at": after.started_at if isinstance(after, AgentContainerIdentity) else None,
            "previous_container_id": _container_id(before),
            "phase_names": list(SETUP_REPLAY_PHASE_NAMES),
        },
    )
    _log.info("executor.runtime_setup_replayed", workspace_id=workspace_id, **context)
    return SetupReplayOutcome(ok=True, replayed=True)


def _container_id(identity: AgentIdentityObservation) -> str | None:
    return identity.container_id if isinstance(identity, AgentContainerIdentity) else None


def _failed(
    details: dict[str, Any],
    *,
    workspace_id: str,
    failure_reason: FailureReason,
    message: str,
) -> SetupReplayOutcome:
    """Every replay failure carries ``RUNTIME_SETUP_REPLAY_FAILED``.

    A more specific underlying code (ownership repair, exec cleanup) is kept in
    ``details["cause_reason_code"]`` so the replay stays recognisable end to end.
    """
    reason_code = RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
    _log.error(
        "executor.runtime_setup_replay_failed",
        workspace_id=workspace_id,
        reason_code=reason_code,
        cause_reason_code=details.get("cause_reason_code"),
        trigger=details.get("trigger"),
        basis=details.get("basis"),
        phase=details.get("phase"),
        command=details.get("command"),
        returncode=details.get("returncode"),
    )
    return SetupReplayOutcome(
        ok=False,
        replayed=True,
        reason_code=reason_code,
        failure_reason=failure_reason,
        message=message[:2000],
        details=details,
    )


def _first_failure_details(first_fail: Any) -> dict[str, Any]:
    if first_fail is None:
        return {}
    return {
        "phase": first_fail.phase,
        "command": redact_audit_text(first_fail.command, limit=1000),
        "returncode": first_fail.returncode,
        "phase_reason_code": first_fail.reason_code,
        "stdout_tail": _redacted_tail(first_fail.captured_stdout, first_fail.stdout_path),
        "stderr_tail": _redacted_tail(first_fail.captured_stderr, first_fail.stderr_path),
    }


def _redacted_tail(captured: str | None, path: Path) -> str:
    text = captured
    if text is None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    # Redact the whole output before tailing so a token straddling the cut
    # cannot leak half-redacted.
    # Redaction markers can lengthen the text, so never let it truncate here.
    redacted = redact_audit_text(text, limit=sys.maxsize)
    return redacted[-_OUTPUT_TAIL_CHARS:]


async def _replay_already_recorded(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: str,
    identity: AgentContainerIdentity,
) -> bool:
    try:
        async with session_factory() as session:
            payload = (
                await session.execute(
                    select(WorkspaceEvent.payload)
                    .where(
                        WorkspaceEvent.workspace_id == workspace_id,
                        WorkspaceEvent.event_type == RUNTIME_SETUP_REPLAYED_EVENT_TYPE,
                    )
                    .order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.event_order.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    except SQLAlchemyError:
        # Without the dedup record, replaying again is the safe choice.
        _log.exception(
            "executor.runtime_setup_replay_lookup_failed",
            workspace_id=workspace_id,
            reason_code=RUNTIME_SETUP_REPLAYED_REASON_CODE,
        )
        return False
    return (
        isinstance(payload, Mapping)
        and payload.get("container_id") == identity.container_id
        and payload.get("started_at") == identity.started_at
    )


async def _record_replayed_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: str,
    payload: dict[str, Any],
) -> None:
    try:
        async with session_factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(workspace_id)
            if workspace is None:
                return
            await repo.add_event(
                workspace,
                event_type=RUNTIME_SETUP_REPLAYED_EVENT_TYPE,
                reason_code=RUNTIME_SETUP_REPLAYED_REASON_CODE,
                payload=payload,
            )
            await session.commit()
    except SQLAlchemyError:
        # Setup already succeeded; a lost audit record only risks one extra
        # replay on a later observation of the same container.
        _log.exception(
            "executor.runtime_setup_replayed_event_record_failed",
            workspace_id=workspace_id,
            reason_code=RUNTIME_SETUP_REPLAYED_REASON_CODE,
        )
