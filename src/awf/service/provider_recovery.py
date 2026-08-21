"""Provider/model recovery decision logic and fallback attempt creation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS
from awf.adapters.provider_failures import (
    AGENT_AUTH_FAILED,
    AGENT_PROVIDER_CAPACITY_EXHAUSTED,
    AGENT_SERVICE_UNHEALTHY,
    classify_provider_failure,
    infer_provider,
)
from awf.common.redaction import redact_secrets
from awf.common.workspace_policy import CURSOR_AUTO_MODE_POLICY_KEY
from awf.control.state_machine import WorkspaceStateMachine
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Task, TaskAttempt, Workspace
from awf.db.repositories import (
    OperationRepository,
    ProviderModelCircuitBreakerRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)

PROVIDER_RECOVERY_POLICY_KEY = "provider_recovery"
PROVIDER_RECOVERY_STATE_KEY = "provider_recovery_state"
PROVIDER_RECOVERY_REQUESTED_EVENT = "workspace.provider_recovery_requested"
PROVIDER_RECOVERY_TERMINAL_EVENT = "workspace.provider_recovery_terminal"
PROVIDER_RECOVERY_COOLDOWN_EVENT = "workspace.provider_recovery_cooldown"
PROVIDER_MODEL_CIRCUIT_OPEN_REASON = "PROVIDER_MODEL_CIRCUIT_OPEN"
PROVIDER_RETRY_DELAYED_REASON = "PROVIDER_RETRY_DELAYED"
PROVIDER_FALLBACK_SELECTED_REASON = "PROVIDER_FALLBACK_SELECTED"
PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"
PROVIDER_RECOVERY_NO_LOOP_REASON = "REPEATED_PROVIDER_FAILURE_FINGERPRINT"
NON_RETRYABLE_PROVIDER_FAILURE = "NON_RETRYABLE_PROVIDER_FAILURE"
PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED = "PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED"
PROVIDER_RECOVERY_STALE_SOURCE = "PROVIDER_RECOVERY_STALE_SOURCE"
PROVIDER_RECOVERY_EXPECTED_SOURCE = "recoverable_provider_failure"
UNSUPPORTED_AGENT_RUNTIME = "UNSUPPORTED_AGENT_RUNTIME"

PROVIDER_RECOVERY_REASON_CODES: frozenset[str] = frozenset(
    {
        PROVIDER_MODEL_CIRCUIT_OPEN_REASON,
        PROVIDER_RETRY_DELAYED_REASON,
        PROVIDER_FALLBACK_SELECTED_REASON,
        PROVIDER_AUTH_FAILED,
        PROVIDER_RECOVERY_NO_LOOP_REASON,
        NON_RETRYABLE_PROVIDER_FAILURE,
        PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED,
        UNSUPPORTED_AGENT_RUNTIME,
    }
)

RecoveryAction = Literal["retry", "fallback", "terminal"]


@dataclass(frozen=True)
class FallbackTarget:
    agent: str
    provider: str | None
    model: str

    def to_payload(self) -> dict[str, str]:
        payload = {"agent": self.agent, "model": self.model}
        if self.provider is not None:
            payload["provider"] = self.provider
        return payload


@dataclass(frozen=True)
class ProviderRecoveryPolicy:
    fallbacks: tuple[FallbackTarget | None, ...] = ()
    has_explicit_fallbacks: bool = False
    max_fallback_attempts: int = 0
    max_same_provider_retries: int = 1
    cooldown_seconds: int = 300
    backoff_seconds: int | None = None
    retry_after_cap_seconds: int = 3600
    circuit_breaker_failure_threshold: int = 2
    circuit_breaker_cooldown_seconds: int = 900

    def __post_init__(self) -> None:
        if self.fallbacks and not self.has_explicit_fallbacks:
            object.__setattr__(self, "has_explicit_fallbacks", True)


@dataclass(frozen=True)
class ProviderRecoveryState:
    failure_fingerprints: tuple[str, ...] = ()
    fallback_attempt_number: int = 0
    retry_attempt_number: int = 0
    launched_fallback_attempts: int = 0


@dataclass(frozen=True)
class ProviderRecoveryDecision:
    action: RecoveryAction
    retryable: bool
    not_before: datetime | None
    target_agent: str | None
    target_provider: str | None
    target_model: str | None
    reason_code: str
    terminal_reason: str | None
    fallback_attempt_number: int
    retry_attempt_number: int
    launched_fallback_attempts: int = 0


@dataclass(frozen=True)
class ProviderRecoveryAttemptResult:
    source_workspace_id: str
    new_workspace_id: str
    action: RecoveryAction
    reason_code: str
    provider_recovery: dict[str, Any]
    in_place: bool = False


@dataclass(frozen=True)
class ProviderInPlaceRecoveryDecision:
    """A retryable provider failure that pauses the workspace into recovering (#612)."""

    not_before: datetime
    reason_code: str
    retry_attempt_number: int
    metadata: dict[str, Any]


def provider_recovery_metadata_from_failure(
    *,
    reason_code: str | None,
    message: str | None,
    details: Mapping[str, Any] | None,
    task_policy: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    details_mapping = details if isinstance(details, Mapping) else {}
    existing = details_mapping.get("provider_recovery")
    metadata: dict[str, Any] | None
    if isinstance(existing, Mapping):
        metadata = {str(key): value for key, value in existing.items()}
    else:
        metadata = _classification_metadata(
            reason_code=reason_code,
            message=message,
            details=details_mapping,
        )
        if metadata is None:
            return None

    if _is_infra_failure_metadata(metadata):
        return None

    policy = parse_provider_recovery_policy(task_policy)
    state = parse_provider_recovery_state(task_policy)
    auth_failure = _is_auth_failure_metadata(metadata)
    if auth_failure:
        metadata["retryable"] = False
    metadata["fallback_allowed"] = (
        False
        if auth_failure
        else (
            _select_fallback_target(policy, state) is not None and bool(metadata.get("retryable"))
        )
    )
    metadata["recommended_action"] = _metadata_recommended_action(metadata)
    if isinstance(message, str) and message:
        metadata.setdefault("evidence", redact_secrets(message[:1000]))
    return metadata


def decide_provider_recovery(
    metadata: Mapping[str, Any],
    *,
    task_policy: Mapping[str, Any] | None,
    current_agent: str,
    current_model: str | None,
    now: datetime,
    effective_default_model: str | None = None,
) -> ProviderRecoveryDecision:
    policy = parse_provider_recovery_policy(task_policy)
    state = parse_provider_recovery_state(task_policy)
    policy_model = _policy_model(task_policy)
    fingerprint = _metadata_str(metadata, "failure_fingerprint")
    provider = _metadata_str(metadata, "provider") or provider_for_agent_model(
        current_agent,
        current_model,
    )
    model = _metadata_str(metadata, "model") or current_model

    from awf.service.provider_readiness import is_launchable_agent

    is_launchable = is_launchable_agent(current_agent)

    if _is_auth_failure_metadata(metadata):
        return _terminal_decision(
            PROVIDER_AUTH_FAILED if is_launchable else UNSUPPORTED_AGENT_RUNTIME,
            state=state,
        )
    if not bool(metadata.get("retryable")):
        return _terminal_decision(
            NON_RETRYABLE_PROVIDER_FAILURE if is_launchable else UNSUPPORTED_AGENT_RUNTIME,
            state=state,
        )
    if fingerprint is not None and fingerprint in state.failure_fingerprints:
        return _terminal_decision(
            PROVIDER_RECOVERY_NO_LOOP_REASON if is_launchable else UNSUPPORTED_AGENT_RUNTIME,
            state=state,
        )

    default_fallback_target = _default_capacity_fallback_target(
        metadata,
        policy=policy,
        state=state,
        current_agent=current_agent,
        current_model=model,
        default_model=_capacity_default_model(
            policy_model=policy_model,
            effective_default_model=effective_default_model,
        ),
    )
    if default_fallback_target is not None:
        return ProviderRecoveryDecision(
            action="fallback",
            retryable=True,
            not_before=None,
            target_agent=default_fallback_target.agent,
            target_provider=default_fallback_target.provider,
            target_model=default_fallback_target.model,
            reason_code=PROVIDER_FALLBACK_SELECTED_REASON,
            terminal_reason=None,
            fallback_attempt_number=state.fallback_attempt_number + 1,
            retry_attempt_number=0,
            launched_fallback_attempts=state.launched_fallback_attempts + 1,
        )

    if is_launchable and state.retry_attempt_number < policy.max_same_provider_retries:
        delay = _retry_delay_seconds(metadata, policy, state)
        return ProviderRecoveryDecision(
            action="retry",
            retryable=True,
            not_before=now + timedelta(seconds=delay),
            target_agent=current_agent,
            target_provider=provider,
            target_model=model,
            reason_code=PROVIDER_RETRY_DELAYED_REASON,
            terminal_reason=None,
            fallback_attempt_number=state.fallback_attempt_number,
            retry_attempt_number=state.retry_attempt_number + 1,
            launched_fallback_attempts=state.launched_fallback_attempts,
        )

    fallback_target, target_index = _select_fallback_target_with_index(policy, state)
    if fallback_target is not None:
        return ProviderRecoveryDecision(
            action="fallback",
            retryable=True,
            not_before=None,
            target_agent=fallback_target.agent,
            target_provider=fallback_target.provider,
            target_model=fallback_target.model,
            reason_code=PROVIDER_FALLBACK_SELECTED_REASON,
            terminal_reason=None,
            fallback_attempt_number=target_index + 1,
            retry_attempt_number=0,
            launched_fallback_attempts=state.launched_fallback_attempts + 1,
        )

    if not is_launchable:
        return _terminal_decision(UNSUPPORTED_AGENT_RUNTIME, state=state)

    return _terminal_decision("PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED", state=state)


def _is_auth_failure_metadata(metadata: Mapping[str, Any]) -> bool:
    return (
        _metadata_str(metadata, "failure_type") == "auth"
        or _metadata_str(metadata, "reason_code") == AGENT_AUTH_FAILED
    )


def _is_infra_failure_metadata(metadata: Mapping[str, Any]) -> bool:
    return (
        _metadata_str(metadata, "failure_scope") == "infra"
        or _metadata_str(metadata, "reason_code") == AGENT_SERVICE_UNHEALTHY
    )


def _default_capacity_fallback_target(
    metadata: Mapping[str, Any],
    *,
    policy: ProviderRecoveryPolicy,
    state: ProviderRecoveryState,
    current_agent: str,
    current_model: str | None,
    default_model: str | None,
) -> FallbackTarget | None:
    if policy.has_explicit_fallbacks:
        return None
    if state.launched_fallback_attempts > 0:
        return None
    if current_agent != AgentRuntime.codex.value:
        return None
    if not _is_capacity_failure_metadata(metadata):
        return None
    if default_model is None or current_model is None or current_model == default_model:
        return None
    return FallbackTarget(
        agent=AgentRuntime.codex.value,
        provider=provider_for_agent_model(AgentRuntime.codex.value, default_model),
        model=default_model,
    )


def _capacity_default_model(
    *,
    policy_model: str | None,
    effective_default_model: str | None,
) -> str | None:
    if effective_default_model is not None:
        stripped = effective_default_model.strip()
        if stripped:
            return stripped
    # Without an effective default from the adapter, policy_model is only a
    # sentinel that the task explicitly selected a model; the fallback target is
    # still the runtime's system default below.
    if policy_model is None:
        return None
    defaults = DEFAULT_AGENT_DEFAULTS.get(AgentRuntime.codex)
    return defaults.model if defaults is not None else None


def _is_capacity_failure_metadata(metadata: Mapping[str, Any]) -> bool:
    return _metadata_str(
        metadata, "reason_code"
    ) == AGENT_PROVIDER_CAPACITY_EXHAUSTED or _metadata_str(metadata, "failure_type") in {
        "capacity",
        "quota",
        "usage_limit",
    }


def should_recover_in_place(
    *,
    reason_code: str | None,
    message: str | None,
    details: Mapping[str, Any] | None,
    task_policy: Mapping[str, Any] | None,
    agent: str,
    current_model: str | None,
    now: datetime,
    effective_default_model: str | None = None,
) -> ProviderInPlaceRecoveryDecision | None:
    """Decide whether an agent-run failure should divert into in-place recovering."""
    metadata = provider_recovery_metadata_from_failure(
        reason_code=reason_code,
        message=message,
        details=details,
        task_policy=task_policy,
    )
    if metadata is None:
        return None
    resolved_model = current_model or _metadata_str(metadata, "model")
    decision = decide_provider_recovery(
        metadata,
        task_policy=task_policy,
        current_agent=agent,
        current_model=resolved_model,
        effective_default_model=effective_default_model,
        now=now,
    )
    # ``decide_provider_recovery`` returns ``action == "retry"`` ONLY with
    # ``target_agent == current_agent`` and a non-None cooldown ``not_before``
    # (a same-agent delayed retry); a ``fallback`` (different agent/model) or a
    # ``terminal`` (budget exhausted / non-retryable / auth / loop) decision is
    # NOT an in-place resume, so the caller keeps today's fresh-relaunch path.
    if decision.action != "retry":
        return None
    if decision.not_before is None:  # pragma: no cover - a retry decision always sets a cooldown
        return None
    return ProviderInPlaceRecoveryDecision(
        not_before=decision.not_before,
        reason_code=decision.reason_code,
        retry_attempt_number=decision.retry_attempt_number,
        metadata=dict(metadata),
    )


def in_place_recovery_task_policy(
    task_policy: Mapping[str, Any] | None,
    *,
    decision: ProviderInPlaceRecoveryDecision,
) -> dict[str, Any]:
    """Build the task_policy for an in-place ``recovering`` pause (#612).

    Persists ``provider_recovery_state`` with the cooldown ``not_before`` (read
    back by ``provider_cooldown_not_before`` to gate the resume) and the bumped
    ``retry_attempt_number`` so a second failure on the resumed run sees the budget
    consumed (``decide_provider_recovery`` will then go terminal). The failure
    fingerprint is appended so an identical re-failure is caught as a loop. Unlike
    ``_recovery_task_policy`` this writes NO retry-lineage / source-workspace
    fields: an in-place retry keeps a single CLEAN attempt id (same workspace, same
    attempt), which is the whole point of #612."""
    policy = deepcopy(dict(task_policy or {}))
    state = parse_provider_recovery_state(policy)
    fingerprint = _metadata_str(decision.metadata, "failure_fingerprint")
    fingerprints = list(state.failure_fingerprints)
    if fingerprint is not None and fingerprint not in fingerprints:
        fingerprints.append(fingerprint)
    recovery_state: dict[str, Any] = {
        "action": "retry",
        "recovery_scope": "agent_run_in_place",
        "decision_reason_code": decision.reason_code,
        "source_reason_code": _metadata_str(decision.metadata, "reason_code"),
        "source_provider": _metadata_str(decision.metadata, "provider"),
        "source_model": _metadata_str(decision.metadata, "model"),
        "failure_fingerprints": fingerprints,
        # An in-place retry keeps the SAME workspace, so any already-spent fallback
        # budget must carry forward: a same-agent retry never changes the fallback
        # counter (``decide_provider_recovery`` preserves ``state.fallback_attempt_number``
        # for ``action == "retry"``). Dropping it here would let a later provider
        # failure parse the count as 0 and re-select ``fallbacks[0]`` — repeating an
        # already-exhausted fallback attempt.
        "fallback_attempt_number": state.fallback_attempt_number,
        "retry_attempt_number": decision.retry_attempt_number,
        "launched_fallback_attempts": state.launched_fallback_attempts,
        "not_before": decision.not_before.isoformat(),
        "recommended_action": _metadata_str(decision.metadata, "recommended_action"),
    }
    policy[PROVIDER_RECOVERY_STATE_KEY] = {
        key: value for key, value in recovery_state.items() if value is not None
    }
    return policy


def rearm_recovering_cooldown_task_policy(
    task_policy: Mapping[str, Any] | None,
    *,
    now: datetime,
) -> dict[str, Any] | None:
    """Re-arm the in-place ``recovering`` cooldown after a failed pre-resume worktree reset (#612, #647).

    When the worker reclaims a cooled-down ``recovering`` row but the pre-run
    worktree reset (``git status``/``stash``/``reset --hard``) fails, the row is
    restored to ``recovering`` for a later safe resume. Without moving
    ``not_before`` forward the cooldown stays in the past, so
    ``list_resumable_recovering_ids`` re-selects the same row every poll and a
    persistent git failure busy-loops the executor slot (consuming slots + log
    volume) instead of waiting for a later safe retry. Advance ``not_before`` to
    ``now + cooldown_seconds`` so the next attempt waits a full provider cooldown.

    Returns ``None`` when there is no ``provider_recovery_state`` mapping to re-arm
    (a legacy/partial row with no cooldown to gate on), leaving the policy untouched
    so the caller writes nothing."""
    raw = task_policy.get(PROVIDER_RECOVERY_STATE_KEY) if task_policy else None
    if not isinstance(raw, Mapping):
        return None
    policy = parse_provider_recovery_policy(task_policy)
    next_not_before = now + timedelta(seconds=policy.cooldown_seconds)
    updated = deepcopy(dict(task_policy or {}))
    state = dict(raw)
    state["not_before"] = next_not_before.isoformat()
    updated[PROVIDER_RECOVERY_STATE_KEY] = state
    return updated


async def create_provider_recovery_attempt_row(
    session: AsyncSession,
    source_workspace_id: str,
    *,
    now: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
    effective_default_model: str | None = None,
) -> ProviderRecoveryAttemptResult | Literal["terminal", "stale"] | None:
    """Create or attach a requested retry/fallback recovery for a retryable provider failure."""

    recovery_now = now or datetime.now(UTC)
    repo = WorkspaceRepository(session)
    source = await repo.get_for_update(source_workspace_id)
    if source is None:
        return None
    source_metadata = provider_recovery_metadata_from_workspace(source)
    if _is_stale_provider_recovery_source(source, source_metadata=source_metadata):
        await repo.record_ignored_stale_callback(
            source,
            callback_source="provider_recovery",
            callback_action="create_attempt",
            expected_status=PROVIDER_RECOVERY_EXPECTED_SOURCE,
            reason_code=PROVIDER_RECOVERY_STALE_SOURCE,
        )
        await session.flush()
        return "stale"
    recovery_metadata = dict(metadata) if metadata is not None else source_metadata
    if recovery_metadata is None:
        return None

    current_model = _policy_model(source.task_policy) or _metadata_str(
        recovery_metadata,
        "model",
    )
    policy = parse_provider_recovery_policy(source.task_policy)
    state = parse_provider_recovery_state(source.task_policy)
    decision = decide_provider_recovery(
        recovery_metadata,
        task_policy=source.task_policy,
        current_agent=source.agent,
        current_model=current_model,
        effective_default_model=effective_default_model,
        now=recovery_now,
    )
    attempt_repo = TaskAttemptRepository(session)
    source_attempt = await attempt_repo.get_by_workspace_id(source.id)
    source_canonical_attempt = (
        await attempt_repo.get_canonical_for_task(source_attempt.task_id)
        if source_attempt is not None
        else None
    )
    lineage_payload = _source_lineage_payload(
        source_workspace_id=source.id,
        source_attempt=source_attempt,
        source_canonical_attempt=source_canonical_attempt,
    )
    monitor_in_place_recovery = _is_recoverable_monitoring_pr_source(source)
    source_not_before = _source_suppression_not_before(
        recovery_metadata,
        policy=policy,
        state=state,
        decision=decision,
        now=recovery_now,
    )
    if monitor_in_place_recovery and decision.action == "fallback":
        source_not_before = None
    source_policy = _recovery_task_policy(
        source.task_policy,
        source_workspace_id=source.id,
        source_attempt=source_attempt,
        source_canonical_attempt=source_canonical_attempt,
        metadata=recovery_metadata,
        decision=decision,
        not_before=source_not_before,
    )
    has_existing_provider_recovery_event = _has_existing_provider_recovery_event(
        source,
        recovery_metadata,
    )
    if not has_existing_provider_recovery_event:
        await _record_provider_circuit_breaker(
            session,
            source,
            recovery_metadata,
            now=recovery_now,
        )
    if (
        has_existing_provider_recovery_event
        and monitor_in_place_recovery
        and decision.action != "terminal"
    ):
        await session.flush()
        return None
    source_workspace_mutated = False
    # This ``elif`` is reached only when ``source_not_before is None`` and the
    # decision is non-terminal, which happens solely for a monitor-in-place
    # fallback (the override above forces ``source_not_before = None`` for that
    # case alone). In that single reachable state the guard, ``decision.action ==
    # "fallback"``, and ``decision.target_model is not None`` are all always True
    # (a fallback decision always carries a concrete target model), so their False
    # arcs are unreachable defensive code — hence ``# pragma: no branch``.
    if source_not_before is not None or decision.action == "terminal":
        source.task_policy = source_policy
        source_workspace_mutated = True
    elif monitor_in_place_recovery and decision.action in {  # pragma: no branch
        "retry",
        "fallback",
    }:
        source.task_policy = source_policy
        source_workspace_mutated = True
        if decision.action == "fallback":  # pragma: no branch
            source.agent = decision.target_agent or source.agent
            if decision.target_model is not None:  # pragma: no branch
                source.task_policy = _install_fixed_recovery_model(
                    source.task_policy,
                    decision.target_model,
                )
    if source_workspace_mutated:  # pragma: no branch
        await repo.advance_workspace_version(source)
    if source_not_before is not None:
        await repo.add_event(
            source,
            event_type=PROVIDER_RECOVERY_COOLDOWN_EVENT,
            reason_code=decision.reason_code,
            payload={
                **lineage_payload,
                "provider_recovery": _decision_payload(
                    decision,
                    recovery_metadata,
                    not_before=source_not_before,
                ),
            },
        )
    if has_existing_provider_recovery_event and decision.action != "terminal":
        await session.flush()
        return None
    if decision.action == "terminal":
        await repo.add_event(
            source,
            event_type=PROVIDER_RECOVERY_TERMINAL_EVENT,
            reason_code=decision.terminal_reason or decision.reason_code,
            payload={
                **lineage_payload,
                "provider_recovery": _decision_payload(decision, recovery_metadata),
            },
        )
        await session.flush()
        return "terminal"
    if monitor_in_place_recovery and decision.action in {"retry", "fallback"}:
        return await _record_monitor_in_place_recovery(
            session,
            repo,
            source,
            lineage_payload=lineage_payload,
            decision=decision,
            recovery_metadata=recovery_metadata,
            not_before=source_not_before,
        )

    new_policy = _recovery_task_policy(
        source.task_policy,
        source_workspace_id=source.id,
        source_attempt=source_attempt,
        source_canonical_attempt=source_canonical_attempt,
        metadata=recovery_metadata,
        decision=decision,
    )
    target_agent = decision.target_agent or source.agent
    # Same-provider retries may carry target_model equal to the current Auto
    # selector; only real fallbacks should pin a fixed model and clear mode.
    if decision.action == "fallback" and decision.target_model is not None:
        new_policy = _install_fixed_recovery_model(new_policy, decision.target_model)

    retried = await repo.create(
        repo_url=source.repo_url,
        branch_base=source.branch_base,
        task_title=source.task_title,
        task_prompt=source.task_prompt,
        task_external_id=source.task_external_id,
        task_tag=source.task_tag,
        task_class=source.task_class,
        owned_paths=list(source.owned_paths),
        task_policy=new_policy,
        auto_merge=source.auto_merge,
        initial_review_grace_period_seconds=source.initial_review_grace_period_seconds,
        agent=target_agent,
        env_profile=source.env_profile,
        profile_ref=source.profile_ref,
        requested_profile=deepcopy(source.requested_profile),
        resolved_profile=deepcopy(source.resolved_profile),
        test_commands=list(source.test_commands),
        requires_database=source.requires_database,
        idempotency_key=None,
        task_kind=source.task_kind,
        remote_push_branch=source.remote_push_branch
        if source.task_kind in {"sync_release_pr", "sync_feature_pr"}
        else None,
    )

    if decision.action == "fallback":
        for field in (
            "pr_url",
            "pr_number",
            "branch_name",
            "remote_push_branch",
            "monitor_iter_count",
            "monitor_threads_addressed",
            "monitor_last_commit_sha",
        ):
            setattr(retried, field, getattr(source, field))

    task = await _retry_task_for_source(session, source, source_attempt=source_attempt)
    attempt = await attempt_repo.create_for_workspace(
        task=task,
        workspace=retried,
        parent_attempt_id=source_attempt.id if source_attempt is not None else None,
        redispatch_from_attempt_id=source_attempt.id if source_attempt is not None else None,
    )

    provider_payload = _decision_payload(decision, recovery_metadata)
    event_payload = {
        **lineage_payload,
        "new_workspace_id": retried.id,
        "attempt_number": attempt.attempt_number,
        "provider_recovery": provider_payload,
    }
    await repo.add_event(
        source,
        event_type=PROVIDER_RECOVERY_REQUESTED_EVENT,
        reason_code=decision.reason_code,
        payload=event_payload,
    )

    await repo.add_event(
        retried,
        event_type="workspace.provider_recovery_created",
        reason_code=decision.reason_code,
        payload=event_payload,
    )
    operation = await OperationRepository(session).create(
        workspace_id=retried.id,
        operation_type=OperationType.retry,
        status=OperationStatus.running,
        payload={
            **lineage_payload,
            "provider_recovery": provider_payload,
        },
    )
    await OperationRepository(session).finish(
        operation,
        status=OperationStatus.succeeded,
        result={
            **lineage_payload,
            "new_workspace_id": retried.id,
            "attempt_number": attempt.attempt_number,
            "provider_recovery": provider_payload,
        },
    )
    await session.flush()
    return ProviderRecoveryAttemptResult(
        source_workspace_id=source.id,
        new_workspace_id=retried.id,
        action=decision.action,
        reason_code=decision.reason_code,
        provider_recovery=provider_payload,
    )


def provider_recovery_metadata_from_workspace(workspace: Workspace) -> dict[str, Any] | None:
    event = _latest_failed_state_event(workspace)
    payload = event.payload if event is not None and isinstance(event.payload, dict) else {}
    details = payload.get("details")
    if not isinstance(details, Mapping):
        details = {}
    reason_code = (
        _payload_str(payload, "reason_code")
        or (event.reason_code if event is not None else None)
        or workspace.failure_reason
    )
    message = _payload_str(payload, "message") or workspace.failure_message
    return provider_recovery_metadata_from_failure(
        reason_code=reason_code,
        message=message,
        details=details,
        task_policy=workspace.task_policy,
    )


def _is_stale_provider_recovery_source(
    source: Workspace,
    *,
    source_metadata: Mapping[str, Any] | None,
) -> bool:
    try:
        status = WorkspaceStatus(source.status)
    except ValueError:  # pragma: no cover - defensive for legacy bad rows
        return False
    if not WorkspaceStateMachine.is_callback_terminal(status):
        return False
    if status == WorkspaceStatus.failed:
        return source_metadata is None
    return True


def parse_provider_recovery_policy(
    task_policy: Mapping[str, Any] | None,
) -> ProviderRecoveryPolicy:
    raw = task_policy.get(PROVIDER_RECOVERY_POLICY_KEY) if task_policy else None
    policy = raw if isinstance(raw, Mapping) else {}
    raw_fallbacks = policy.get("fallbacks")
    has_explicit_fallbacks = (
        "fallbacks" in policy
        and isinstance(raw_fallbacks, Sequence)
        and not isinstance(raw_fallbacks, str)
    )
    fallbacks = tuple(_fallback_targets(raw_fallbacks))
    max_fallback_attempts = _nonnegative_int(
        policy.get("max_fallback_attempts"),
        default=len(fallbacks),
    )
    return ProviderRecoveryPolicy(
        fallbacks=fallbacks,
        has_explicit_fallbacks=has_explicit_fallbacks,
        max_fallback_attempts=max_fallback_attempts,
        max_same_provider_retries=_nonnegative_int(
            policy.get("max_same_provider_retries"),
            default=1,
        ),
        cooldown_seconds=_positive_int(policy.get("cooldown_seconds"), default=300),
        backoff_seconds=_optional_positive_int(policy.get("backoff_seconds")),
        retry_after_cap_seconds=_positive_int(
            policy.get("retry_after_cap_seconds"),
            default=3600,
        ),
        circuit_breaker_failure_threshold=_positive_int(
            _nested_value(policy, "circuit_breaker", "failure_threshold"),
            default=2,
        ),
        circuit_breaker_cooldown_seconds=_positive_int(
            _nested_value(policy, "circuit_breaker", "cooldown_seconds"),
            default=900,
        ),
    )


def parse_provider_recovery_state(
    task_policy: Mapping[str, Any] | None,
) -> ProviderRecoveryState:
    raw = task_policy.get(PROVIDER_RECOVERY_STATE_KEY) if task_policy else None
    state = raw if isinstance(raw, Mapping) else {}
    fingerprints = state.get("failure_fingerprints")
    fingerprint_values = (
        tuple(item for item in fingerprints if isinstance(item, str))
        if isinstance(fingerprints, Sequence) and not isinstance(fingerprints, str)
        else ()
    )
    fallback_attempt_number = _nonnegative_int(state.get("fallback_attempt_number"), default=0)
    raw_launched = state.get("launched_fallback_attempts")
    if raw_launched is not None:
        launched_fallback_attempts = _nonnegative_int(raw_launched, default=0)
    else:
        # Reconstruct launched_fallback_attempts for legacy state written before the field existed.
        launched_fallback_attempts = fallback_attempt_number

    return ProviderRecoveryState(
        failure_fingerprints=fingerprint_values,
        fallback_attempt_number=fallback_attempt_number,
        retry_attempt_number=_nonnegative_int(state.get("retry_attempt_number"), default=0),
        launched_fallback_attempts=launched_fallback_attempts,
    )


def provider_for_agent_model(agent: str, model: str | None) -> str | None:
    # Env-key-only runtimes keep a stable provider identity independent of model IDs.
    if agent in {"cursor", "antigravity"}:
        return agent
    inferred = infer_provider(model=model)
    return (
        inferred
        if inferred is not None
        else {
            "codex": "openai",
            "claude_code": "anthropic",
            "opencode": "opencode",
            "grok": "xai",
        }.get(agent)
    )


def provider_cooldown_not_before(
    task_policy: Mapping[str, Any] | None,
) -> datetime | None:
    raw = task_policy.get(PROVIDER_RECOVERY_STATE_KEY) if task_policy else None
    if not isinstance(raw, Mapping) or not isinstance(raw.get("not_before"), str):
        return None
    try:
        parsed = datetime.fromisoformat(raw["not_before"])
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        return None


def _classification_metadata(
    *,
    reason_code: str | None,
    message: str | None,
    details: Mapping[str, Any],
) -> dict[str, Any] | None:
    classification = classify_provider_failure(
        reason_code=reason_code,
        stdout=None,
        stderr=message,
        provider=_mapping_str(details, "provider"),
        model=_mapping_str(details, "model"),
    )
    if classification is None:
        return None
    metadata = classification.to_metadata()
    recommended_action = _mapping_str(details, "recommended_action")
    if recommended_action is not None:
        metadata["recommended_action"] = recommended_action
    return metadata


def _metadata_recommended_action(metadata: Mapping[str, Any]) -> str:
    if _is_auth_failure_metadata(metadata):
        return "Refresh provider credentials before retrying this workspace."
    existing = metadata.get("recommended_action")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    return "Retry after provider cooldown or dispatch an approved fallback model."


def _select_fallback_target(
    policy: ProviderRecoveryPolicy,
    state: ProviderRecoveryState,
) -> FallbackTarget | None:
    target, _ = _select_fallback_target_with_index(policy, state)
    return target


def has_approved_launchable_fallback(
    task_policy: Mapping[str, Any] | None,
) -> bool:
    """Return True if task_policy has an approved launchable provider recovery fallback."""
    if not task_policy:
        return False
    policy = parse_provider_recovery_policy(task_policy)
    state = parse_provider_recovery_state(task_policy)
    target = _select_fallback_target(policy, state)
    if target is None or not target.agent:
        return False
    from awf.service.provider_readiness import is_launchable_agent

    return is_launchable_agent(target.agent)


def _select_fallback_target_with_index(
    policy: ProviderRecoveryPolicy,
    state: ProviderRecoveryState,
) -> tuple[FallbackTarget | None, int]:
    if state.launched_fallback_attempts >= policy.max_fallback_attempts:
        return None, len(policy.fallbacks)

    index = state.fallback_attempt_number
    while index < len(policy.fallbacks):
        target = policy.fallbacks[index]
        if target is not None:
            return target, index
        index += 1
    return None, len(policy.fallbacks)


def _retry_delay_seconds(
    metadata: Mapping[str, Any],
    policy: ProviderRecoveryPolicy,
    state: ProviderRecoveryState,
) -> int:
    retry_after = _nonnegative_int(
        metadata.get("retry_after_seconds"),
        default=0,
    )
    retry_after = min(retry_after, policy.retry_after_cap_seconds)
    base = policy.backoff_seconds or policy.cooldown_seconds
    backoff = base * (2**state.retry_attempt_number)
    delay = int(max(policy.cooldown_seconds, retry_after, backoff))
    return min(delay, policy.retry_after_cap_seconds)


def _source_suppression_not_before(
    metadata: Mapping[str, Any],
    *,
    policy: ProviderRecoveryPolicy,
    state: ProviderRecoveryState,
    decision: ProviderRecoveryDecision,
    now: datetime,
) -> datetime | None:
    if decision.not_before is not None:
        return decision.not_before
    if decision.action not in {"retry", "fallback"}:
        return None
    return now + timedelta(seconds=_retry_delay_seconds(metadata, policy, state))


def _terminal_decision(
    terminal_reason: str,
    *,
    state: ProviderRecoveryState,
) -> ProviderRecoveryDecision:
    return ProviderRecoveryDecision(
        action="terminal",
        retryable=False,
        not_before=None,
        target_agent=None,
        target_provider=None,
        target_model=None,
        reason_code=terminal_reason,
        terminal_reason=terminal_reason,
        fallback_attempt_number=state.fallback_attempt_number,
        retry_attempt_number=state.retry_attempt_number,
        launched_fallback_attempts=state.launched_fallback_attempts,
    )


async def _record_provider_circuit_breaker(
    session: AsyncSession,
    source: Workspace,
    metadata: Mapping[str, Any],
    *,
    now: datetime,
) -> None:
    provider = _metadata_str(metadata, "provider")
    model = _metadata_str(metadata, "model") or _policy_model(source.task_policy)
    fingerprint = _metadata_str(metadata, "failure_fingerprint")
    reason_code = _metadata_str(metadata, "reason_code")
    if provider is None or model is None or fingerprint is None or reason_code is None:
        return
    if reason_code != AGENT_PROVIDER_CAPACITY_EXHAUSTED:
        return
    policy = parse_provider_recovery_policy(source.task_policy)
    attempt = await TaskAttemptRepository(session).get_by_workspace_id(source.id)
    await ProviderModelCircuitBreakerRepository(session).record_failure(
        provider=provider,
        model=model,
        reason_code=reason_code,
        failure_fingerprint=fingerprint,
        workspace_id=source.id,
        attempt_id=attempt.id if attempt is not None else None,
        now=now,
        failure_threshold=policy.circuit_breaker_failure_threshold,
        cooldown_seconds=policy.circuit_breaker_cooldown_seconds,
    )


def _is_recoverable_monitoring_pr_source(source: Workspace) -> bool:
    if source.status != WorkspaceStatus.monitoring_pr.value:
        return False
    has_remote_push_branch = bool(source.remote_push_branch) or (
        source.task_kind == "feature_branch_pr" and bool(source.branch_name)
    )
    return bool(
        source.pr_url
        and source.pr_number is not None
        and has_remote_push_branch
        and source.compose_project_name
        and source.compose_file_path
    )


async def _record_monitor_in_place_recovery(
    session: AsyncSession,
    repo: WorkspaceRepository,
    source: Workspace,
    *,
    lineage_payload: Mapping[str, Any],
    decision: ProviderRecoveryDecision,
    recovery_metadata: Mapping[str, Any],
    not_before: datetime | None,
) -> ProviderRecoveryAttemptResult:
    provider_payload = _decision_payload(
        decision,
        recovery_metadata,
        not_before=not_before,
    )
    await repo.add_event(
        source,
        event_type=PROVIDER_RECOVERY_REQUESTED_EVENT,
        reason_code=decision.reason_code,
        payload={
            **dict(lineage_payload),
            "recovery_scope": "monitor_in_place",
            "provider_recovery": provider_payload,
        },
    )

    await session.flush()
    return ProviderRecoveryAttemptResult(
        source_workspace_id=source.id,
        new_workspace_id=source.id,
        action=decision.action,
        reason_code=decision.reason_code,
        provider_recovery=provider_payload,
        in_place=True,
    )


def _install_fixed_recovery_model(
    task_policy: Mapping[str, Any],
    target_model: str,
) -> dict[str, Any]:
    """Install a fixed recovery model and clear Cursor Auto mode if present.

    Admission treats ``cursor_auto_mode`` and a fixed ``agent_model`` as mutually
    exclusive. Provider recovery historically copied Auto mode while writing the
    fallback model, so executor helpers kept preferring ``auto-smart[...]`` and
    silently ignored the selected recovery target.
    """

    updated = {**dict(task_policy), "agent_model": target_model}
    updated.pop(CURSOR_AUTO_MODE_POLICY_KEY, None)
    return updated


def _recovery_task_policy(
    source_policy: Mapping[str, Any],
    *,
    source_workspace_id: str,
    source_attempt: TaskAttempt | None,
    source_canonical_attempt: TaskAttempt | None,
    metadata: Mapping[str, Any],
    decision: ProviderRecoveryDecision,
    not_before: datetime | None = None,
) -> dict[str, Any]:
    policy = deepcopy(dict(source_policy))
    state = parse_provider_recovery_state(policy)
    fingerprint = _metadata_str(metadata, "failure_fingerprint")
    fingerprints = list(state.failure_fingerprints)
    if fingerprint is not None and fingerprint not in fingerprints:
        fingerprints.append(fingerprint)
    recovery_state: dict[str, Any] = {
        "source_workspace_id": source_workspace_id,
        "source_attempt_id": source_attempt.id if source_attempt is not None else None,
        "source_task_id": source_attempt.task_id if source_attempt is not None else None,
        "source_canonical_attempt_id": (
            source_canonical_attempt.id if source_canonical_attempt is not None else None
        ),
        "source_reason_code": _metadata_str(metadata, "reason_code"),
        "decision_reason_code": decision.reason_code,
        "source_provider": _metadata_str(metadata, "provider"),
        "source_model": _metadata_str(metadata, "model"),
        "failure_fingerprints": fingerprints,
        "fallback_attempt_number": decision.fallback_attempt_number,
        "retry_attempt_number": decision.retry_attempt_number,
        "launched_fallback_attempts": decision.launched_fallback_attempts,
        "action": decision.action,
        "target_agent": decision.target_agent,
        "target_provider": decision.target_provider,
        "target_model": decision.target_model,
        "recommended_action": (
            None
            if decision.reason_code == UNSUPPORTED_AGENT_RUNTIME
            else _metadata_str(metadata, "recommended_action")
        ),
    }
    state_not_before = decision.not_before if not_before is None else not_before
    if state_not_before is not None:
        recovery_state["not_before"] = state_not_before.isoformat()
    policy[PROVIDER_RECOVERY_STATE_KEY] = {
        key: value for key, value in recovery_state.items() if value is not None
    }
    return policy


def _source_lineage_payload(
    *,
    source_workspace_id: str,
    source_attempt: TaskAttempt | None,
    source_canonical_attempt: TaskAttempt | None,
) -> dict[str, Any]:
    payload = {"source_workspace_id": source_workspace_id}
    if source_attempt is not None:
        payload["source_attempt_id"] = source_attempt.id
        payload["source_task_id"] = source_attempt.task_id
    if source_canonical_attempt is not None:
        payload["source_canonical_attempt_id"] = source_canonical_attempt.id
    return payload


def _decision_payload(
    decision: ProviderRecoveryDecision,
    metadata: Mapping[str, Any],
    *,
    not_before: datetime | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        **dict(metadata),
        "action": decision.action,
        "decision_reason_code": decision.reason_code,
        "target_agent": decision.target_agent,
        "target_provider": decision.target_provider,
        "target_model": decision.target_model,
        "fallback_attempt_number": decision.fallback_attempt_number,
        "retry_attempt_number": decision.retry_attempt_number,
        "launched_fallback_attempts": decision.launched_fallback_attempts,
        "terminal_reason": decision.terminal_reason,
    }
    if decision.reason_code == UNSUPPORTED_AGENT_RUNTIME:
        payload.pop("recommended_action", None)
    state_not_before = decision.not_before if not_before is None else not_before
    if state_not_before is not None:
        payload["not_before"] = state_not_before.isoformat()
    return {key: value for key, value in payload.items() if value is not None}


async def _retry_task_for_source(
    session: AsyncSession,
    source: Workspace,
    *,
    source_attempt: TaskAttempt | None,
) -> Task:
    if source_attempt is not None:
        task = await TaskRepository(session).get(source_attempt.task_id)
        if task is not None:
            return task

    fallback_idempotency_key = f"retry-source-workspace:{source.id}"
    return await TaskRepository(session).create_or_get(
        repo_url=source.repo_url,
        base_branch=source.branch_base,
        title=source.task_title,
        prompt=source.task_prompt,
        external_id=source.task_external_id,
        idempotency_key=fallback_idempotency_key,
        task_class=source.task_class,
        owned_paths=list(source.owned_paths),
    )


def _latest_failed_state_event(workspace: Workspace) -> Any | None:
    for event in reversed(getattr(workspace, "events", []) or []):
        if (
            getattr(event, "event_type", None) == "workspace.state_changed"
            and getattr(event, "new_state", None) == "failed"
        ):
            return event
    return None


def _has_existing_provider_recovery_event(
    source: Workspace,
    metadata: Mapping[str, Any],
) -> bool:
    fingerprint = _metadata_str(metadata, "failure_fingerprint")
    if fingerprint is None:
        return False
    for event in getattr(source, "events", []) or []:
        if getattr(event, "event_type", None) != PROVIDER_RECOVERY_REQUESTED_EVENT:
            continue
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        recovery = payload.get("provider_recovery")
        if isinstance(recovery, Mapping) and recovery.get("failure_fingerprint") == fingerprint:
            return True
    return False


def _fallback_targets(raw: object) -> list[FallbackTarget | None]:
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return []
    targets: list[FallbackTarget | None] = []
    from awf.service.provider_readiness import is_launchable_agent

    for item in raw:
        if item is None:
            targets.append(None)
            continue
        if not isinstance(item, Mapping):
            targets.append(None)
            continue
        agent = _mapping_str(item, "agent")
        model = _mapping_str(item, "model")
        if agent is None or model is None:
            targets.append(None)
            continue
        if not is_launchable_agent(agent):
            targets.append(None)
            continue
        provider = _mapping_str(item, "provider") or provider_for_agent_model(agent, model)
        targets.append(FallbackTarget(agent=agent, provider=provider, model=model))
    return targets


def _policy_model(task_policy: Mapping[str, Any] | None) -> str | None:
    if task_policy is None:
        return None
    return _mapping_str(task_policy, "agent_model")


def _metadata_str(metadata: Mapping[str, Any], key: str) -> str | None:
    return _mapping_str(metadata, key)


def _mapping_str(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _payload_str(payload: Mapping[str, Any], key: str) -> str | None:
    return _mapping_str(payload, key)


def _nonnegative_int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value.is_integer():
        return max(0, int(value))
    return default


def _positive_int(value: object, *, default: int) -> int:
    parsed = _nonnegative_int(value, default=default)
    return parsed if parsed > 0 else default


def _optional_positive_int(value: object) -> int | None:
    parsed = _nonnegative_int(value, default=0)
    return parsed if parsed > 0 else None


def _nested_value(mapping: Mapping[str, Any], key: str, nested_key: str) -> object:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        return None
    return value.get(nested_key)


from awf.service import provider_recovery_state as _provider_recovery_state  # noqa: E402

ProviderRecoveryStateView = _provider_recovery_state.ProviderRecoveryStateView
_build_provider_recovery_state_view = _provider_recovery_state._build_provider_recovery_state_view
_merge_recovery_views = _provider_recovery_state._merge_recovery_views
_parse_not_before = _provider_recovery_state._parse_not_before
_provider_recovery_state_from_events = _provider_recovery_state._provider_recovery_state_from_events
_provider_recovery_state_from_task_policy = (
    _provider_recovery_state._provider_recovery_state_from_task_policy
)
_recommended_action_for_action = _provider_recovery_state._recommended_action_for_action
_validate_recovery_action = _provider_recovery_state._validate_recovery_action
provider_recovery_decision_from_workspace = (
    _provider_recovery_state.provider_recovery_decision_from_workspace
)
provider_recovery_state_for_workspace = (
    _provider_recovery_state.provider_recovery_state_for_workspace
)
