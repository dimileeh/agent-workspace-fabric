"""Preserved active execution recovery flow.

Mechanically extracted from recovery_preserved.py; behavior is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import (
    replace,
)
from datetime import (
    UTC,
    datetime,
)
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from awf.control.worker.constants import (
    _ACTIVE_EXECUTION_PRESERVED_SUBPHASE,
    _ACTIVE_EXECUTION_SALVAGE_BLOCKED_EVENT_TYPE,
    _ACTIVE_EXECUTION_SALVAGE_BLOCKED_REASON_CODE,
    _ACTIVE_EXECUTION_SALVAGE_BLOCKED_SUBPHASE,
    _ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_EVENT_TYPE,
    _ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE,
    _ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_EVENT_TYPE,
    _ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_REASON_CODE,
    _ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_EVENT_TYPE,
    _ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_REASON_CODE,
    _ACTIVE_EXECUTION_SALVAGE_OPERATOR_SUBPHASE,
    _ACTIVE_EXECUTION_SALVAGE_REPLACED_SUBPHASE,
    _ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_EVENT_TYPE,
    _ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_REASON_CODE,
    _ACTIVE_EXECUTION_SALVAGE_SOURCE,
    _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_EVENT_TYPE,
    _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
    _ACTIVE_EXECUTION_STATUSES,
)
from awf.control.worker.helpers import (
    _active_execution_preservation_claim_cleanup_payload,
    _active_execution_preservation_payload,
    _active_execution_salvage_idempotency_key,
    _active_execution_salvage_payload,
    _execution_claim_is_stale,
    _extract_pr_number,
    _nonempty_str,
    _payload_preservation_event_id,
    _pr_adoption_expected_head_repo_slug,
    _preserved_active_replacement_remote_push_branch,
    _workspace_claim_snapshot,
)
from awf.control.worker.logging import _log
from awf.control.worker.types import (
    _ActiveExecutionCandidate,
    _OpenPullRequestSummary,
    _PreservedWorktreeClassification,
)
from awf.db.enums import (
    OperationStatus,
    OperationType,
    WorkspaceStatus,
)
from awf.db.models import (
    Operation,
    WorkspaceEvent,
)
from awf.db.repositories import (
    OperationRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.runtime.inspection import RuntimeSnapshot
from awf.service.failure_causality import (
    attach_primary_failure,
    load_primary_failure_snapshot,
)
from awf.service.workspace_runtime_health import (
    ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
    ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
)


async def _recover_preserved_active_execution(
    self: Any,
    candidate: _ActiveExecutionCandidate,
    *,
    record_lineage_blocked_before_grace: bool = True,
) -> bool:
    if candidate.status not in _ACTIVE_EXECUTION_STATUSES:
        return False

    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return False
        # A validation rewind is part of the same active recovery cycle; do
        # not let its new running state-change hide earlier salvage evidence.
        event_floor_status = candidate.status
        has_active_validation_recovery = candidate.status in (
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
        ) and await self._has_active_preserved_validation_recovery(
            session,
            candidate.workspace_id,
        )
        if has_active_validation_recovery:
            committed_validation_rewind = False
            can_continue_validation = self._preserved_active_validation_can_continue(
                candidate.workspace_id
            )
            if can_continue_validation and candidate.status != WorkspaceStatus.running:
                previous_claim = _workspace_claim_snapshot(ws)
                ws.execution_claimed_by = None
                ws.execution_claim_expires_at = None
                ws.subphase = "runtime_preserved_validation_requested"
                await repo.transition(
                    ws,
                    to=WorkspaceStatus.running,
                    reason_code=_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
                    payload={
                        "source": _ACTIVE_EXECUTION_SALVAGE_SOURCE,
                        "reason_code": (_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE),
                        "decision": "redispatch_active_validation_recovery",
                        "workspace_status": candidate.status.value,
                        "previous_claim": previous_claim,
                    },
                )
                await session.commit()
                committed_validation_rewind = True
            dispatched = self._dispatch_preserved_active_validation(candidate.workspace_id)
            can_continue_validation = self._preserved_active_validation_can_continue(
                candidate.workspace_id
            )
            _log.info(
                "worker.preserved_active_validation_redispatch",
                workspace_id=candidate.workspace_id,
                status=candidate.status.value,
                reason_code=_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
                dispatched=dispatched,
            )
            if dispatched or can_continue_validation:
                return True
            if committed_validation_rewind:
                await session.refresh(ws)
                refreshed_status = WorkspaceStatus(ws.status)
                if refreshed_status not in _ACTIVE_EXECUTION_STATUSES:
                    return False
                if refreshed_status != candidate.status:
                    candidate = replace(candidate, status=refreshed_status)
        event_floor = await self._active_execution_preservation_event_floor(
            session,
            ws,
            event_floor_status,
        )
        preserved_event = await self._latest_preserved_active_execution_event(
            session,
            candidate.workspace_id,
            candidate.status,
            event_floor=event_floor,
        )
        if preserved_event is None and candidate.status in _ACTIVE_EXECUTION_STATUSES:
            salvage_event_floor = await self._active_execution_preservation_salvage_event_floor(
                session,
                ws,
                candidate.status,
            )
            active_preserved_event = await self._latest_preserved_active_execution_event(
                session,
                candidate.workspace_id,
                candidate.status,
                event_floor=salvage_event_floor,
                match_active_execution_statuses=True,
            )
            if active_preserved_event is not None and await self._has_current_salvage_event(
                session,
                candidate.workspace_id,
                event_type=_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_EVENT_TYPE,
                reason_code=_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
                event_floor=active_preserved_event.occurred_at,
                workspace_status=candidate.status,
            ):
                preserved_event = active_preserved_event
        if preserved_event is None:
            return (
                event_floor is not None
                and has_active_validation_recovery
                and await self._has_current_salvage_event(
                    session,
                    candidate.workspace_id,
                    event_type=_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_EVENT_TYPE,
                    reason_code=_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
                    event_floor=event_floor,
                    workspace_status=candidate.status,
                )
            )

        preservation_expired = self._active_execution_preservation_is_expired(
            preserved_event.occurred_at
        )
        attempt = ws.task_attempt
        attempt_id = attempt.id if attempt is not None else None
        task_id = attempt.task_id if attempt is not None else None

        if await self._has_current_salvage_event(
            session,
            candidate.workspace_id,
            event_type=_ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_EVENT_TYPE,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_REASON_CODE,
            event_floor=preserved_event.occurred_at,
            workspace_status=candidate.status,
        ):
            return True
        if await self._has_current_salvage_event(
            session,
            candidate.workspace_id,
            event_type=_ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_EVENT_TYPE,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_REASON_CODE,
            event_floor=preserved_event.occurred_at,
            workspace_status=candidate.status,
        ):
            return True
        if await self._has_current_salvage_event(
            session,
            candidate.workspace_id,
            event_type=_ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_EVENT_TYPE,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE,
            event_floor=preserved_event.occurred_at,
            workspace_status=candidate.status,
        ):
            return True
        if await self._has_current_salvage_event(
            session,
            candidate.workspace_id,
            event_type=_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_EVENT_TYPE,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
            event_floor=preserved_event.occurred_at,
            workspace_status=candidate.status,
        ):
            dispatched = self._dispatch_preserved_active_validation(candidate.workspace_id)
            if dispatched or candidate.workspace_id in self._execution_tasks:
                return True
            if self._executor is None:
                await session.commit()
                await self._record_preserved_active_salvage_blocked(
                    candidate,
                    preserved_event=preserved_event,
                    reason="validation_executor_unavailable",
                    attempt_id=attempt_id,
                    task_id=task_id,
                    preservation_expired=preservation_expired,
                )
                return True
            if self._available_execution_slots() <= 0:
                if not preservation_expired or self._can_dispatch_execution_when_slot_available():
                    return True
                await session.commit()
                await self._record_preserved_active_salvage_not_possible(
                    candidate,
                    preserved_event=preserved_event,
                    reason="validation_execution_slots_disabled",
                )
                return False
            return True

        extracted_pr_number = (
            _extract_pr_number(ws.pr_url) if ws.pr_url and ws.pr_number is None else ws.pr_number
        )
        attach_existing_pr_monitor = bool(ws.pr_url and extracted_pr_number is not None)
        branch_recovery_context: (
            tuple[
                str,
                str,
                str | None,
                str | None,
                str | None,
                str | None,
                str | None,
            ]
            | None
        ) = None
        if not attach_existing_pr_monitor:
            branch_recovery_context = (
                ws.repo_url,
                ws.id,
                ws.branch_name,
                ws.remote_push_branch,
                ws.base_commit,
                _pr_adoption_expected_head_repo_slug(ws),
                (ws.resolved_profile or {}).get("forge"),
            )
        await session.commit()

    if attach_existing_pr_monitor:
        await self._attach_preserved_active_pr_monitor(
            candidate,
            preserved_event=preserved_event,
            attempt_id=attempt_id,
            task_id=task_id,
        )
        return True

    assert branch_recovery_context is not None
    (
        repo_url,
        workspace_id,
        branch_name,
        remote_push_branch,
        base_commit,
        expected_head_repo_slug,
        resolved_forge,
    ) = branch_recovery_context
    lookup_branch_name = _nonempty_str(remote_push_branch) or _nonempty_str(branch_name)
    branch_lookup = await self._resolve_preserved_active_branch_open_pr(
        repo_url=repo_url,
        branch_name=lookup_branch_name,
        expected_head_repo_slug=expected_head_repo_slug,
        # Honor the persisted/resolved forge so a bare-slug Bitbucket workspace
        # fails fast instead of slipping into the GitHub-only resolver.
        resolved_forge=resolved_forge,
        # A preserved branch PR may have been retargeted after creation.
        # Recover by head branch and let match/ambiguity checks below guard safety.
        base_branch=None,
    )
    if branch_lookup is not None and branch_lookup.state == "matched":
        await self._attach_preserved_active_pr_monitor(
            candidate,
            preserved_event=preserved_event,
            attempt_id=attempt_id,
            task_id=task_id,
            open_pr=branch_lookup.match,
            branch_pr_lookup=branch_lookup.payload,
        )
        return True
    if branch_lookup is not None and branch_lookup.state == "ambiguous":
        await self._record_preserved_active_operator_required(
            candidate,
            preserved_event=preserved_event,
            classification=None,
            ambiguity_reason=branch_lookup.ambiguity_reason or "open_pr_lookup_ambiguous",
            attempt_id=attempt_id,
            task_id=task_id,
            branch_pr_lookup=branch_lookup.payload,
        )
        return True

    failed_branch_lookup = (
        branch_lookup if branch_lookup is not None and branch_lookup.state == "failed" else None
    )
    branch_lookup_failure_reason = (
        failed_branch_lookup.ambiguity_reason or "open_pr_lookup_failed"
        if failed_branch_lookup is not None
        else None
    )
    if failed_branch_lookup is not None and not preservation_expired:
        await self._record_preserved_active_salvage_blocked(
            candidate,
            preserved_event=preserved_event,
            reason=branch_lookup_failure_reason or "open_pr_lookup_failed",
            attempt_id=attempt_id,
            task_id=task_id,
            branch_pr_lookup=failed_branch_lookup.payload,
        )
        return True
    missing_lineage_reason: str | None = None
    if attempt_id is None:
        missing_lineage_reason = "missing_task_attempt_lineage"
    elif task_id is None:
        missing_lineage_reason = "missing_task_lineage"
    if missing_lineage_reason is not None:
        if not preservation_expired:
            if not record_lineage_blocked_before_grace:
                return True
            await self._record_preserved_active_salvage_blocked(
                candidate,
                preserved_event=preserved_event,
                reason=missing_lineage_reason,
                attempt_id=attempt_id,
                task_id=task_id,
            )
            return True
        if failed_branch_lookup is None:
            await self._record_preserved_active_salvage_blocked(
                candidate,
                preserved_event=preserved_event,
                reason=missing_lineage_reason,
                attempt_id=attempt_id,
                task_id=task_id,
                preservation_expired=True,
            )
            await self._record_preserved_active_salvage_not_possible(
                candidate,
                preserved_event=preserved_event,
                reason=missing_lineage_reason,
            )
            return False

    classification = await self._classify_preserved_active_worktree(
        workspace_id=workspace_id,
        expected_branch_name=lookup_branch_name,
        base_commit=base_commit,
    )
    if failed_branch_lookup is not None:
        await self._record_preserved_active_operator_required(
            candidate,
            preserved_event=preserved_event,
            classification=classification,
            ambiguity_reason=(
                missing_lineage_reason or branch_lookup_failure_reason or "open_pr_lookup_failed"
            ),
            attempt_id=attempt_id,
            task_id=task_id,
            branch_pr_lookup=failed_branch_lookup.payload,
        )
        return True
    if classification.state == "committed":
        assert attempt_id is not None
        assert task_id is not None
        if self._executor is None:
            await self._record_preserved_active_salvage_blocked(
                candidate,
                preserved_event=preserved_event,
                reason="validation_executor_unavailable",
                attempt_id=attempt_id,
                task_id=task_id,
                classification=classification,
                preservation_expired=preservation_expired,
            )
            return True
        validation_requested = await self._request_preserved_active_validation(
            candidate,
            preserved_event=preserved_event,
            classification=classification,
            attempt_id=attempt_id,
            task_id=task_id,
        )
        if validation_requested:
            dispatched = self._dispatch_preserved_active_validation(candidate.workspace_id)
            if dispatched or candidate.workspace_id in self._execution_tasks:
                return True
            if self._available_execution_slots() <= 0:
                if not preservation_expired or self._can_dispatch_execution_when_slot_available():
                    return True
                await self._record_preserved_active_salvage_not_possible(
                    candidate,
                    preserved_event=preserved_event,
                    reason="validation_execution_slots_disabled",
                )
                return False
        return True
    if classification.state == "no_work":
        if not preservation_expired:
            await self._record_preserved_active_salvage_blocked(
                candidate,
                preserved_event=preserved_event,
                reason=classification.reason,
                attempt_id=attempt_id,
                task_id=task_id,
                classification=classification,
            )
            return True
        assert attempt_id is not None
        assert task_id is not None
        return cast(
            bool,
            await self._create_preserved_active_replacement(
                candidate,
                preserved_event=preserved_event,
                classification=classification,
                attempt_id=attempt_id,
                task_id=task_id,
            ),
        )
    if classification.state == "failed" and not preservation_expired:
        await self._record_preserved_active_salvage_blocked(
            candidate,
            preserved_event=preserved_event,
            reason=classification.reason,
            attempt_id=attempt_id,
            task_id=task_id,
            classification=classification,
        )
        return True

    await self._record_preserved_active_operator_required(
        candidate,
        preserved_event=preserved_event,
        classification=classification,
        ambiguity_reason=classification.reason,
        attempt_id=attempt_id,
        task_id=task_id,
        branch_pr_lookup=(
            failed_branch_lookup.payload if failed_branch_lookup is not None else None
        ),
    )
    return True


async def _record_preserved_active_execution_after_restart(
    self: Any,
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
) -> None:
    now = datetime.now(UTC)
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return
        if not _execution_claim_is_stale(ws, now):
            return
        if await self._has_operator_refresh_after_latest_preservation_for_workspace(
            session,
            ws,
            candidate.status,
        ):
            return
        event_floor = await self._active_execution_preservation_event_floor(
            session,
            ws,
            candidate.status,
        )
        if await self._has_preserved_active_execution_event(
            session,
            candidate.workspace_id,
            candidate.status,
            event_floor=event_floor,
        ):
            return

        previous_claim = _workspace_claim_snapshot(ws)
        claim_cleanup = _active_execution_preservation_claim_cleanup_payload(
            ws,
            claim_cutoff=now,
        )
        if claim_cleanup["action"] == "cleared_stale":
            ws.execution_claimed_by = None
            ws.execution_claim_expires_at = None
        ws.subphase = _ACTIVE_EXECUTION_PRESERVED_SUBPHASE
        await repo.advance_workspace_version(ws)
        payload = _active_execution_preservation_payload(
            candidate,
            snapshot,
            worker_id=self._worker_id,
            previous_claim=previous_claim,
            claim_cleanup=claim_cleanup,
        )
        primary_failure = await load_primary_failure_snapshot(session, ws)
        # Active-execution preservation writes diagnostic refresh/non-state
        # payloads; causality lookup reads failed state-change events only.
        payload = attach_primary_failure(payload, primary_failure)
        operation = await OperationRepository(session).create(
            workspace_id=candidate.workspace_id,
            operation_type=OperationType.refresh,
            status=OperationStatus.running,
            payload=payload,
        )
        payload_with_operation = {**payload, "operation_id": operation.id}
        operation.payload = payload_with_operation
        await OperationRepository(session).finish(
            operation,
            status=OperationStatus.succeeded,
            result={
                "decision": "preserve_runtime",
                "reason_code": ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
            },
        )
        await repo.add_event(
            ws,
            event_type=ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
            reason_code=ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
            payload=payload_with_operation,
        )
        await session.commit()

    _log.warning(
        ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        reason_code=ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
    )


async def _attach_preserved_active_pr_monitor(
    self: Any,
    candidate: _ActiveExecutionCandidate,
    *,
    preserved_event: WorkspaceEvent,
    attempt_id: str | None,
    task_id: str | None,
    open_pr: _OpenPullRequestSummary | None = None,
    branch_pr_lookup: Mapping[str, Any] | None = None,
) -> None:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return
        claim_cutoff = datetime.now(UTC)
        if not _execution_claim_is_stale(ws, claim_cutoff):
            return
        if open_pr is not None and (not ws.pr_url or ws.pr_number is None):
            ws.pr_url = open_pr.pr_url
            ws.pr_number = open_pr.pr_number
            ws.remote_push_branch = open_pr.head_ref or ws.remote_push_branch or ws.branch_name
            if open_pr.head_sha:
                ws.monitor_last_commit_sha = open_pr.head_sha
        if ws.pr_url and ws.pr_number is None:
            ws.pr_number = _extract_pr_number(ws.pr_url)
        if not ws.pr_url or ws.pr_number is None:
            return
        if await self._has_current_salvage_event(
            session,
            candidate.workspace_id,
            event_type=_ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_EVENT_TYPE,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE,
            event_floor=preserved_event.occurred_at,
            workspace_status=candidate.status,
        ):
            return

        previous_claim = _workspace_claim_snapshot(ws)
        claim_cleanup = _active_execution_preservation_claim_cleanup_payload(
            ws,
            claim_cutoff=claim_cutoff,
        )
        ws.execution_claimed_by = None
        ws.execution_claim_expires_at = None
        ws.subphase = None
        extra: dict[str, Any] = {
            "pr_url": ws.pr_url,
            "pr_number": ws.pr_number,
            "remote_push_branch": ws.remote_push_branch,
            "monitor_last_commit_sha": ws.monitor_last_commit_sha,
        }
        if branch_pr_lookup is not None:
            extra["branch_pr_lookup"] = dict(branch_pr_lookup)
        payload = _active_execution_salvage_payload(
            candidate,
            preserved_event=preserved_event,
            worker_id=self._worker_id,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE,
            decision="attach_pr_monitor",
            attempt_id=attempt_id,
            task_id=task_id,
            previous_claim=previous_claim,
            claim_cleanup=claim_cleanup,
            extra=extra,
        )
        if candidate.status != WorkspaceStatus.running:
            cancelled_active_operations = await self._cancel_superseded_active_execution_operations(
                session,
                workspace_id=ws.id,
                replacement_operation_id=None,
                preservation_event_id=preserved_event.id,
                reason_code=_ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE,
                requested_action=OperationType.remonitor,
                preserve_current_salvage_operation=False,
                error_message=(
                    "Cancelled superseded active operation before worker-restart "
                    "PR monitor attachment."
                ),
            )
            if cancelled_active_operations:
                payload["cancelled_active_operations"] = cancelled_active_operations
        await repo.transition(
            ws,
            to=WorkspaceStatus.monitoring_pr,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE,
            payload=payload,
        )
        await repo.add_event(
            ws,
            event_type=_ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_EVENT_TYPE,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE,
            payload=payload,
        )
        await session.commit()


async def _request_preserved_active_validation(
    self: Any,
    candidate: _ActiveExecutionCandidate,
    *,
    preserved_event: WorkspaceEvent,
    classification: _PreservedWorktreeClassification,
    attempt_id: str,
    task_id: str,
    branch_pr_lookup: Mapping[str, Any] | None = None,
) -> bool:
    idempotency_key = _active_execution_salvage_idempotency_key(
        "validate",
        candidate.workspace_id,
        preserved_event.id,
    )
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return False
        claim_cutoff = datetime.now(UTC)
        if not _execution_claim_is_stale(ws, claim_cutoff):
            return False
        if await self._has_current_salvage_event(
            session,
            candidate.workspace_id,
            event_type=_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_EVENT_TYPE,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
            event_floor=preserved_event.occurred_at,
            workspace_status=candidate.status,
        ):
            return False

        previous_claim = _workspace_claim_snapshot(ws)
        claim_cleanup = _active_execution_preservation_claim_cleanup_payload(
            ws,
            claim_cutoff=claim_cutoff,
        )
        extra: dict[str, Any] = {
            "action": "validate_only",
            "requested_action": OperationType.validate.value,
            "recovery_mode": "validate_only",
            "source_workspace_id": ws.id,
            "source_head_sha": classification.head_sha,
            "source_base_sha": ws.base_commit,
            "base_commit": ws.base_commit,
            "head_sha": classification.head_sha,
            "branch_name": ws.branch_name,
            "remote_branch": ws.remote_push_branch or ws.branch_name,
            "target_branch": ws.branch_base,
        }
        if branch_pr_lookup is not None:
            extra["branch_pr_lookup"] = dict(branch_pr_lookup)
        payload = _active_execution_salvage_payload(
            candidate,
            preserved_event=preserved_event,
            worker_id=self._worker_id,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
            decision="validate_committed_work",
            attempt_id=attempt_id,
            task_id=task_id,
            previous_claim=previous_claim,
            claim_cleanup=claim_cleanup,
            classification=classification,
            extra=extra,
        )
        operation, _created = await OperationRepository(session).create_idempotent(
            workspace_id=ws.id,
            operation_type=OperationType.validate,
            status=OperationStatus.pending,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        payload_with_operation = {**payload, "operation_id": operation.id}
        if candidate.status != WorkspaceStatus.running:
            cancelled_active_operations = await self._cancel_superseded_active_execution_operations(
                session,
                workspace_id=ws.id,
                replacement_operation_id=operation.id,
                preservation_event_id=preserved_event.id,
            )
            if cancelled_active_operations:
                payload_with_operation["cancelled_active_operations"] = cancelled_active_operations
        if operation.payload != payload_with_operation:
            operation.payload = payload_with_operation
        ws.execution_claimed_by = None
        ws.execution_claim_expires_at = None
        ws.subphase = "runtime_preserved_validation_requested"
        if candidate.status == WorkspaceStatus.running:
            await repo.advance_workspace_version(ws)
        else:
            await repo.transition(
                ws,
                to=WorkspaceStatus.running,
                reason_code=_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
                payload=payload_with_operation,
            )
        await repo.add_event(
            ws,
            event_type=_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_EVENT_TYPE,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
            payload=payload_with_operation,
        )
        await session.commit()
        return True


async def _cancel_superseded_active_execution_operations(
    self: Any,
    session: AsyncSession,
    *,
    workspace_id: str,
    replacement_operation_id: str | None,
    preservation_event_id: str,
    reason_code: str = _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
    requested_action: OperationType = OperationType.validate,
    preserve_current_salvage_operation: bool = True,
    error_message: str = (
        "Cancelled superseded active operation before worker-restart validation recovery."
    ),
) -> list[dict[str, object]]:
    _ = self
    operation_repo = OperationRepository(session)
    active_operations: list[Operation] = []
    for status in (OperationStatus.pending, OperationStatus.running):
        active_operations.extend(
            await operation_repo.list_for_workspace(
                workspace_id,
                status=status,
                limit=100,
            )
        )

    cancelled: list[dict[str, object]] = []
    for operation in active_operations:
        if replacement_operation_id is not None and operation.id == replacement_operation_id:
            continue
        if operation.type not in {
            OperationType.validate.value,
            OperationType.push.value,
            OperationType.rebase.value,
        }:
            continue
        payload = operation.payload if isinstance(operation.payload, Mapping) else {}
        if (
            preserve_current_salvage_operation
            and payload.get("source") == _ACTIVE_EXECUTION_SALVAGE_SOURCE
            and _payload_preservation_event_id(payload) == preservation_event_id
        ):
            continue

        detail: dict[str, object] = {
            "operation_id": operation.id,
            "operation_type": operation.type,
            "previous_status": operation.status,
        }
        cancelled.append(detail)
        result: dict[str, object] = {
            "status": OperationStatus.cancelled.value,
            "reason_code": reason_code,
            "requested_action": requested_action.value,
        }
        if replacement_operation_id is not None:
            result["replacement_operation_id"] = replacement_operation_id
        await operation_repo.finish(
            operation,
            status=OperationStatus.cancelled,
            result=result,
            error_code=reason_code,
            error_message=error_message,
        )
    return cancelled


async def _create_preserved_active_replacement(
    self: Any,
    candidate: _ActiveExecutionCandidate,
    *,
    preserved_event: WorkspaceEvent,
    classification: _PreservedWorktreeClassification,
    attempt_id: str,
    task_id: str,
) -> bool:
    idempotency_key = _active_execution_salvage_idempotency_key(
        "replacement",
        candidate.workspace_id,
        preserved_event.id,
    )
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        await repo.acquire_idempotency_key_lock(idempotency_key)
        existing = await repo.get_by_idempotency_key(idempotency_key)
        ws = await repo.get_for_update(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return False
        claim_cutoff = datetime.now(UTC)
        if not _execution_claim_is_stale(ws, claim_cutoff):
            return True
        original_attempt = await TaskAttemptRepository(session).get_by_workspace_id(ws.id)
        if original_attempt is None or original_attempt.id != attempt_id:
            current_attempt_id = original_attempt.id if original_attempt is not None else None
            mismatch_reason = (
                "missing_task_attempt_lineage"
                if original_attempt is None
                else "attempt_lineage_mismatch"
            )
            _log.warning(
                "worker.preserved_active_replacement_attempt_mismatch",
                workspace_id=ws.id,
                expected_attempt_id=attempt_id,
                current_attempt_id=current_attempt_id,
                reason=mismatch_reason,
                reason_code=_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_REASON_CODE,
            )
            if not await self._has_current_salvage_event(
                session,
                ws.id,
                event_type=_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_EVENT_TYPE,
                reason_code=_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_REASON_CODE,
                event_floor=preserved_event.occurred_at,
                workspace_status=candidate.status,
            ):
                payload = _active_execution_salvage_payload(
                    candidate,
                    preserved_event=preserved_event,
                    worker_id=self._worker_id,
                    reason_code=_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_REASON_CODE,
                    decision="allow_stale_active_failure",
                    attempt_id=attempt_id,
                    task_id=task_id,
                    previous_claim=_workspace_claim_snapshot(ws),
                    claim_cleanup=_active_execution_preservation_claim_cleanup_payload(
                        ws,
                        claim_cutoff=claim_cutoff,
                    ),
                    classification=classification,
                    extra={
                        "unrecoverable_reason": mismatch_reason,
                        "source_workspace_id": ws.id,
                        "current_attempt_id": current_attempt_id,
                    },
                )
                await repo.add_event(
                    ws,
                    event_type=_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_EVENT_TYPE,
                    reason_code=_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_REASON_CODE,
                    payload=payload,
                )
                await session.commit()
            return False
        if await self._has_current_salvage_event(
            session,
            ws.id,
            event_type=_ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_EVENT_TYPE,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_REASON_CODE,
            event_floor=preserved_event.occurred_at,
            workspace_status=candidate.status,
        ):
            return True

        replacement = existing
        replacement_attempt = (
            await TaskAttemptRepository(session).get_by_workspace_id(replacement.id)
            if replacement is not None
            else None
        )
        if replacement is None:
            replacement = await repo.create_replacement_from(
                ws,
                idempotency_key=idempotency_key,
                remote_push_branch=_preserved_active_replacement_remote_push_branch(ws),
            )
            task = await TaskRepository(session).get(task_id)
            if task is None:
                task = await TaskRepository(session).create_or_get(
                    repo_url=ws.repo_url,
                    base_branch=ws.branch_base,
                    title=ws.task_title,
                    prompt=ws.task_prompt,
                    external_id=ws.task_external_id,
                    idempotency_key=None,
                    task_class=ws.task_class,
                    owned_paths=list(ws.owned_paths),
                )
            replacement_attempt = await TaskAttemptRepository(session).create_for_workspace(
                task=task,
                workspace=replacement,
                parent_attempt_id=original_attempt.id,
                redispatch_from_attempt_id=original_attempt.id,
            )
            reservations = [
                reservation
                for reservation in ws.resource_reservations
                if reservation.released_at is None
            ]
            reservation_repo = ResourceReservationRepository(session)
            for reservation in reservations:
                await reservation_repo.create(
                    workspace_id=replacement.id,
                    attempt_id=replacement_attempt.id,
                    node_id=reservation.node_id,
                    steady_cpu=reservation.steady_cpu,
                    steady_memory_gb=reservation.steady_memory_gb,
                    peak_cpu=reservation.peak_cpu,
                    peak_memory_gb=reservation.peak_memory_gb,
                    disk_mb=reservation.disk_mb,
                    dind_slots=reservation.dind_slots,
                    phase=reservation.phase,
                )

        if replacement_attempt is None:
            _log.warning(
                "worker.preserved_active_replacement_attempt_missing",
                workspace_id=ws.id,
                replacement_workspace_id=replacement.id,
                reason_code=_ACTIVE_EXECUTION_SALVAGE_BLOCKED_REASON_CODE,
            )
            await session.commit()
            await self._record_preserved_active_salvage_blocked(
                candidate,
                preserved_event=preserved_event,
                reason="replacement_attempt_missing",
                attempt_id=attempt_id,
                task_id=task_id,
                classification=classification,
                preservation_expired=self._active_execution_preservation_is_expired(
                    preserved_event.occurred_at
                ),
                additional_payload={"replacement_workspace_id": replacement.id},
            )
            return True
        original_attempt.superseded_by_attempt_id = replacement_attempt.id
        previous_claim = _workspace_claim_snapshot(ws)
        claim_cleanup = _active_execution_preservation_claim_cleanup_payload(
            ws,
            claim_cutoff=claim_cutoff,
        )
        ws.execution_claimed_by = None
        ws.execution_claim_expires_at = None
        ws.subphase = _ACTIVE_EXECUTION_SALVAGE_REPLACED_SUBPHASE
        payload = _active_execution_salvage_payload(
            candidate,
            preserved_event=preserved_event,
            worker_id=self._worker_id,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_REASON_CODE,
            decision="replacement_workspace_created",
            attempt_id=attempt_id,
            task_id=task_id,
            previous_claim=previous_claim,
            claim_cleanup=claim_cleanup,
            classification=classification,
            extra={
                "replacement_workspace_id": replacement.id,
                "replacement_attempt_id": replacement_attempt.id,
                "source_workspace_id": ws.id,
            },
        )
        operation, _created = await OperationRepository(session).create_idempotent(
            workspace_id=ws.id,
            operation_type=OperationType.retry,
            status=OperationStatus.succeeded,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        operation.result = {
            "new_workspace_id": replacement.id,
            "new_attempt_id": replacement_attempt.id,
            "status": replacement.status,
        }
        payload_with_operation = {**payload, "operation_id": operation.id}
        if candidate.status != WorkspaceStatus.running:
            cancelled_active_operations = await self._cancel_superseded_active_execution_operations(
                session,
                workspace_id=ws.id,
                replacement_operation_id=operation.id,
                preservation_event_id=preserved_event.id,
                reason_code=_ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_REASON_CODE,
                requested_action=OperationType.retry,
                error_message=(
                    "Cancelled superseded active operation before worker-restart "
                    "replacement recovery."
                ),
            )
            if cancelled_active_operations:
                payload_with_operation["cancelled_active_operations"] = cancelled_active_operations
        operation.payload = payload_with_operation
        await repo.transition(
            ws,
            to=WorkspaceStatus.cancelled,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_REASON_CODE,
            payload=payload_with_operation,
        )
        await repo.add_event(
            ws,
            event_type=_ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_EVENT_TYPE,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_REASON_CODE,
            payload=payload_with_operation,
        )
        await repo.add_event(
            replacement,
            event_type=_ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_EVENT_TYPE,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_REASON_CODE,
            payload={
                **payload_with_operation,
                "workspace_role": "replacement",
                "source_workspace_id": ws.id,
            },
        )
        await session.commit()
        return True


async def _record_preserved_active_operator_required(
    self: Any,
    candidate: _ActiveExecutionCandidate,
    *,
    preserved_event: WorkspaceEvent,
    classification: _PreservedWorktreeClassification | None,
    ambiguity_reason: str,
    attempt_id: str | None,
    task_id: str | None,
    branch_pr_lookup: Mapping[str, Any] | None = None,
) -> None:
    idempotency_key = _active_execution_salvage_idempotency_key(
        "operator",
        candidate.workspace_id,
        preserved_event.id,
    )
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return
        claim_cutoff = datetime.now(UTC)
        if not _execution_claim_is_stale(ws, claim_cutoff):
            return
        if await self._has_current_salvage_event(
            session,
            candidate.workspace_id,
            event_type=_ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_EVENT_TYPE,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_REASON_CODE,
            event_floor=preserved_event.occurred_at,
            workspace_status=candidate.status,
        ):
            return
        previous_claim = _workspace_claim_snapshot(ws)
        claim_cleanup = _active_execution_preservation_claim_cleanup_payload(
            ws,
            claim_cutoff=claim_cutoff,
        )
        extra: dict[str, Any] = {
            "ambiguity_reason": ambiguity_reason,
            "source_workspace_id": ws.id,
        }
        if branch_pr_lookup is not None:
            extra["branch_pr_lookup"] = dict(branch_pr_lookup)
        payload = _active_execution_salvage_payload(
            candidate,
            preserved_event=preserved_event,
            worker_id=self._worker_id,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_REASON_CODE,
            decision="operator_recovery_required",
            attempt_id=attempt_id,
            task_id=task_id,
            previous_claim=previous_claim,
            claim_cleanup=claim_cleanup,
            classification=classification,
            extra=extra,
        )
        operation, _created = await OperationRepository(session).create_idempotent(
            workspace_id=ws.id,
            operation_type=OperationType.refresh,
            status=OperationStatus.succeeded,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        payload_with_operation = {**payload, "operation_id": operation.id}
        cancelled_active_operations = await self._cancel_superseded_active_execution_operations(
            session,
            workspace_id=ws.id,
            replacement_operation_id=operation.id,
            preservation_event_id=preserved_event.id,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_REASON_CODE,
            requested_action=OperationType.refresh,
            preserve_current_salvage_operation=False,
            error_message=(
                "Cancelled superseded active operation before worker-restart "
                "operator-required recovery."
            ),
        )
        if cancelled_active_operations:
            payload_with_operation["cancelled_active_operations"] = cancelled_active_operations
        operation.payload = payload_with_operation
        operation.result = {
            "decision": "operator_recovery_required",
            "reason_code": _ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_REASON_CODE,
        }
        if claim_cleanup["action"] == "cleared_stale":
            ws.execution_claimed_by = None
            ws.execution_claim_expires_at = None
        ws.subphase = _ACTIVE_EXECUTION_SALVAGE_OPERATOR_SUBPHASE
        await repo.advance_workspace_version(ws)
        await repo.add_event(
            ws,
            event_type=_ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_EVENT_TYPE,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_REASON_CODE,
            payload=payload_with_operation,
        )
        await session.commit()


async def _record_preserved_active_salvage_not_possible(
    self: Any,
    candidate: _ActiveExecutionCandidate,
    *,
    preserved_event: WorkspaceEvent,
    reason: str,
) -> None:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return
        claim_cutoff = datetime.now(UTC)
        if not _execution_claim_is_stale(ws, claim_cutoff):
            return
        if await self._has_current_salvage_event(
            session,
            candidate.workspace_id,
            event_type=_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_EVENT_TYPE,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_REASON_CODE,
            event_floor=preserved_event.occurred_at,
            workspace_status=candidate.status,
        ):
            return
        payload = _active_execution_salvage_payload(
            candidate,
            preserved_event=preserved_event,
            worker_id=self._worker_id,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_REASON_CODE,
            decision="allow_stale_active_failure",
            attempt_id=None,
            task_id=None,
            previous_claim=_workspace_claim_snapshot(ws),
            claim_cleanup=_active_execution_preservation_claim_cleanup_payload(
                ws,
                claim_cutoff=claim_cutoff,
            ),
            extra={
                "unrecoverable_reason": reason,
                "source_workspace_id": ws.id,
            },
        )
        await repo.add_event(
            ws,
            event_type=_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_EVENT_TYPE,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_REASON_CODE,
            payload=payload,
        )
        await session.commit()


async def _record_preserved_active_salvage_blocked(
    self: Any,
    candidate: _ActiveExecutionCandidate,
    *,
    preserved_event: WorkspaceEvent,
    reason: str,
    attempt_id: str | None,
    task_id: str | None,
    classification: _PreservedWorktreeClassification | None = None,
    branch_pr_lookup: Mapping[str, Any] | None = None,
    preservation_expired: bool = False,
    additional_payload: Mapping[str, Any] | None = None,
) -> None:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get_for_update(candidate.workspace_id)
        if ws is None or ws.status != candidate.status.value:
            return
        claim_cutoff = datetime.now(UTC)
        if not _execution_claim_is_stale(ws, claim_cutoff):
            return
        latest_blocked_reason = await self._latest_current_salvage_blocked_reason(
            session,
            candidate.workspace_id,
            event_floor=preserved_event.occurred_at,
            workspace_status=candidate.status,
        )
        if latest_blocked_reason == reason:
            return
        previous_claim = _workspace_claim_snapshot(ws)
        claim_cleanup = _active_execution_preservation_claim_cleanup_payload(
            ws,
            claim_cutoff=claim_cutoff,
        )
        if claim_cleanup["action"] == "cleared_stale":
            ws.execution_claimed_by = None
            ws.execution_claim_expires_at = None
        ws.subphase = _ACTIVE_EXECUTION_SALVAGE_BLOCKED_SUBPHASE
        await repo.advance_workspace_version(ws)
        extra: dict[str, Any] = {
            "blocked_reason": reason,
            "source_workspace_id": ws.id,
            "preservation_expired": preservation_expired,
        }
        if branch_pr_lookup is not None:
            extra["branch_pr_lookup"] = dict(branch_pr_lookup)
        if additional_payload is not None:
            extra.update(dict(additional_payload))
        payload = _active_execution_salvage_payload(
            candidate,
            preserved_event=preserved_event,
            worker_id=self._worker_id,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_BLOCKED_REASON_CODE,
            decision="wait_for_preservation_grace",
            attempt_id=attempt_id,
            task_id=task_id,
            previous_claim=previous_claim,
            claim_cleanup=claim_cleanup,
            classification=classification,
            extra=extra,
        )
        await repo.add_event(
            ws,
            event_type=_ACTIVE_EXECUTION_SALVAGE_BLOCKED_EVENT_TYPE,
            reason_code=_ACTIVE_EXECUTION_SALVAGE_BLOCKED_REASON_CODE,
            payload=payload,
        )
        await session.commit()
