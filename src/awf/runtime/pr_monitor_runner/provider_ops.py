"""Pull request monitor provider and logging operations.

Mechanically extracted from the original orchestrator; behavior is unchanged.
"""

from __future__ import annotations

import json
from collections.abc import (
    Iterable,
    Mapping,
)
from datetime import (
    UTC,
    datetime,
)
from typing import Any, Literal, cast

from awf.adapters.base import AgentRunError
from awf.common.workspace_policy import (
    agent_model_from_task_policy,
)
from awf.db.repositories import (
    ProviderModelCircuitBreakerRepository,
    WorkspaceEventCreate,
    WorkspaceRepository,
)
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.pr_monitor import (
    MonitorState,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.constants import _PR_MONITOR_AUDIT_ACTOR
from awf.runtime.pr_monitor_runner.helpers import (
    _stale_pending_check_warning_key,
    _stale_pending_check_warnings,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.recovery_payloads import (
    _task_policy_with_monitor_circuit_retry_state,
)
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
)
from awf.service.provider_recovery import (
    PROVIDER_MODEL_CIRCUIT_OPEN_REASON,
    PROVIDER_RECOVERY_COOLDOWN_EVENT,
    PROVIDER_RECOVERY_STATE_KEY,
    _is_auth_failure_metadata,
    create_provider_recovery_attempt_row,
    provider_cooldown_not_before,
    provider_for_agent_model,
    provider_recovery_metadata_from_failure,
)


async def _open_monitor_log(self: Any, workspace_id: str) -> WorkspaceLogSink | None:
    if self._deps.log_store is None:
        return None
    return cast(
        WorkspaceLogSink,
        await self._deps.log_store.open_stream(
            workspace_id=workspace_id,
            stream_id="monitor.log",
            source="monitor",
            name="PR monitor",
            kind="stdout",
        ),
    )


async def _write_monitor_log(
    _self: Any, sink: WorkspaceLogSink | None, payload: dict[str, object]
) -> None:
    if sink is None:
        return
    try:
        await sink.write(json.dumps(payload, default=str, sort_keys=True) + "\n")
    except Exception as exc:
        _log.warning("monitor.log_write_failed", error=str(exc)[:400])


async def _record_stale_pending_check_warnings(
    self: Any,
    *,
    workspace_id: str,
    status: PRStatus,
    state: MonitorState,
    monitor_log: WorkspaceLogSink | None,
) -> bool:
    emitted = False
    warnings = _stale_pending_check_warnings(
        status,
        now=datetime.now(UTC),
        threshold_seconds=self._config.stale_pending_check_warning_seconds,
    )
    events: list[WorkspaceEventCreate] = []
    for warning in warnings:
        key = _stale_pending_check_warning_key(
            workspace_id=workspace_id,
            head_sha=status.head_sha,
            check_name=warning.check_name,
            threshold_seconds=warning.threshold_seconds,
            threshold_window=warning.threshold_window,
        )
        if state.threads_addressed_ids.get(key) == "emitted":
            continue
        state.mark_addressed(key, "emitted")
        payload = warning.payload()
        _log.warning("workspace.pending_check_stale", workspace_id=workspace_id, **payload)
        await self._write_monitor_log(
            monitor_log,
            {
                "event": "workspace.pending_check_stale",
                "workspace_id": workspace_id,
                **payload,
            },
        )
        events.append(
            WorkspaceEventCreate(
                event_type="workspace.pending_check_stale",
                reason_code="PENDING_CHECK_STALE",
                payload=payload,
            )
        )
        emitted = True
    if events:
        await self._append_workspace_events(workspace_id=workspace_id, events=events)
    return emitted


async def _append_workspace_events(
    self: Any,
    *,
    workspace_id: str,
    events: list[WorkspaceEventCreate],
) -> None:
    async with self._deps.session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        if ws is None:
            return
        await repo.add_events(ws, events=events)
        await s.commit()


async def _workspace_test_commands(self: Any, workspace_id: str) -> tuple[str, ...]:
    async with self._deps.session_factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        if ws is None:
            return ()
        raw_test_commands: object = ws.test_commands
        if (
            not raw_test_commands
            or isinstance(raw_test_commands, (str, bytes, bytearray, Mapping))
            or not isinstance(raw_test_commands, Iterable)
        ):
            return ()
        return tuple(command for command in raw_test_commands if isinstance(command, str))


async def _provider_recovery_suppresses_cli(self: Any, workspace_id: str) -> bool:
    """Return whether provider recovery cooldown suppresses immediate CLI monitor actions.

    If an open breaker is detected for the workspace's current provider/model,
    this updates monitor task policy so the suppression can survive restarts and
    returns ``True`` to short-circuit CLI execution. ``False`` is returned only
    when the workspace cannot be resolved or no cooldown applies.
    """
    now = datetime.now(UTC)
    async with self._deps.session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        if ws is None:
            return False
        not_before = provider_cooldown_not_before(ws.task_policy)
        if not_before is not None and not_before > now:
            return True
        model = agent_model_from_task_policy(ws.task_policy)
        provider = provider_for_agent_model(ws.agent, model)
        if provider is None or model is None:
            return False
        breaker = await ProviderModelCircuitBreakerRepository(s).open_breaker(
            provider=provider,
            model=model,
            now=now,
        )
        if breaker is None:
            await s.commit()
            return False
        task_policy = ws.task_policy if isinstance(ws.task_policy, Mapping) else {}
        recovery_state = task_policy.get(PROVIDER_RECOVERY_STATE_KEY)
        if (
            isinstance(recovery_state, Mapping)
            and recovery_state.get("action") == "retry"
            and recovery_state.get("decision_reason_code") == PROVIDER_MODEL_CIRCUIT_OPEN_REASON
            and recovery_state.get("source_provider") == provider
            and recovery_state.get("source_model") == model
            and recovery_state.get("target_provider") == provider
            and recovery_state.get("target_model") == model
        ):
            if (
                breaker.cooldown_until is not None
                and provider_cooldown_not_before(task_policy) != breaker.cooldown_until
            ):
                ws.task_policy = _task_policy_with_monitor_circuit_retry_state(
                    task_policy,
                    provider=provider,
                    model=model,
                    cooldown_until=breaker.cooldown_until,
                    last_reason_code=breaker.last_reason_code,
                )
                await repo.advance_workspace_version(ws)
                await s.commit()
            return True
        await repo.add_event(
            ws,
            event_type=PROVIDER_RECOVERY_COOLDOWN_EVENT,
            reason_code=PROVIDER_MODEL_CIRCUIT_OPEN_REASON,
            payload={
                "provider": provider,
                "model": model,
                "source": "pr_monitor",
                "cooldown_until": breaker.cooldown_until.isoformat()
                if breaker.cooldown_until is not None
                else None,
                "failure_count": breaker.failure_count,
                "last_reason_code": breaker.last_reason_code,
            },
        )
        ws.task_policy = _task_policy_with_monitor_circuit_retry_state(
            task_policy,
            provider=provider,
            model=model,
            cooldown_until=breaker.cooldown_until,
            last_reason_code=breaker.last_reason_code,
        )
        await repo.advance_workspace_version(ws)
        await s.commit()
        return True


async def _record_provider_agent_run_error(
    self: Any,
    workspace_id: str,
    exc: AgentRunError,
) -> Literal["fallback", "retry", "auth_failed", "terminal", "deterministic"]:
    message = exc.result.stderr.strip() or exc.result.stdout.strip()
    async with self._deps.session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        if ws is None:
            return "deterministic"
        effective_default_model = (
            self._deps.provider_recovery_default_model
            if self._deps.provider_recovery_default_model is not None
            else self._deps.adapter.provider_recovery_default_model
        )
        metadata = provider_recovery_metadata_from_failure(
            reason_code=exc.reason_code,
            message=message,
            details=exc.details,
            task_policy=ws.task_policy,
        )
        if metadata is None:
            return "deterministic"
        provider_auth_failed = _is_auth_failure_metadata(metadata)
        result = await create_provider_recovery_attempt_row(
            s,
            workspace_id,
            metadata=metadata,
            effective_default_model=effective_default_model,
        )
        await s.commit()
        if provider_auth_failed:
            return "auth_failed"
        if result == "terminal":
            return "terminal"
        if result == "stale":
            return "deterministic"
        if result is not None and result.action == "fallback" and not result.in_place:
            return "fallback"
        return "retry"


async def _handle_provider_agent_run_error(
    self: Any,
    workspace_id: str,
    exc: AgentRunError,
    *,
    state: MonitorState | None = None,
) -> Literal["fallback", "retry", "auth_failed", "terminal", "deterministic"]:
    if state is not None:
        await self._persist_state(workspace_id, state)
    action = await self._record_provider_agent_run_error(workspace_id, exc)
    if action == "fallback":
        raise ProviderRecoveryFallbackError() from exc
    if action == "retry":
        raise ProviderRecoveryRetryError()
    if action == "auth_failed":
        raise ProviderRecoveryAuthError() from exc
    return cast(Literal["fallback", "retry", "auth_failed", "terminal", "deterministic"], action)


async def _record_pr_monitor_audit_event(
    self: Any,
    *,
    workspace_id: str,
    event_type: str,
    action: str,
    outcome: str,
    reason_code: str,
    pr_number: int,
    status: PRStatus | None,
    base_branch: str,
    remote_branch: str | None,
    operation_id: str | None = None,
    operation_type: str | None = None,
    monitor_log: WorkspaceLogSink | None = None,
    source_head_sha: str | None = None,
    source_base_sha: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    audit_evidence = dict(evidence or {})
    if monitor_log is not None:
        audit_evidence.setdefault("log_stream_refs", {"monitor": monitor_log.stream_id})
    async with self._deps.session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        if ws is None:  # pragma: no cover - destroyed mid-monitor
            return
        await repo.add_audit_event(
            ws,
            event_type=event_type,
            actor=_PR_MONITOR_AUDIT_ACTOR,
            source=_PR_MONITOR_AUDIT_ACTOR,
            action=action,
            outcome=outcome,
            reason_code=reason_code,
            operation_id=operation_id,
            operation_type=operation_type,
            pr_number=pr_number,
            pr_url=ws.pr_url,
            source_head_sha=source_head_sha or (status.head_sha if status else None),
            source_base_sha=source_base_sha or ws.base_commit,
            target_branch=base_branch,
            remote_branch=remote_branch,
            branch_name=ws.branch_name,
            evidence=audit_evidence or None,
        )
        await s.commit()
