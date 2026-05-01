"""Provider/model recovery decision logic and fallback attempt creation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from awf.adapters.provider_failures import (
    AGENT_PROVIDER_CAPACITY_EXHAUSTED,
    classify_provider_failure,
    infer_provider,
)
from awf.common.redaction import redact_secrets
from awf.db.enums import OperationStatus, OperationType
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
PROVIDER_RECOVERY_NO_LOOP_REASON = "REPEATED_PROVIDER_FAILURE_FINGERPRINT"

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
    fallbacks: tuple[FallbackTarget, ...] = ()
    max_fallback_attempts: int = 0
    max_same_provider_retries: int = 1
    cooldown_seconds: int = 300
    backoff_seconds: int | None = None
    retry_after_cap_seconds: int = 3600
    circuit_breaker_failure_threshold: int = 2
    circuit_breaker_cooldown_seconds: int = 900


@dataclass(frozen=True)
class ProviderRecoveryState:
    failure_fingerprints: tuple[str, ...] = ()
    fallback_attempt_number: int = 0
    retry_attempt_number: int = 0


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


@dataclass(frozen=True)
class ProviderRecoveryAttemptResult:
    source_workspace_id: str
    new_workspace_id: str
    action: RecoveryAction
    reason_code: str
    provider_recovery: dict[str, Any]


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

    policy = parse_provider_recovery_policy(task_policy)
    state = parse_provider_recovery_state(task_policy)
    metadata["fallback_allowed"] = (
        bool(policy.fallbacks)
        and state.fallback_attempt_number < policy.max_fallback_attempts
        and bool(metadata.get("retryable"))
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
) -> ProviderRecoveryDecision:
    policy = parse_provider_recovery_policy(task_policy)
    state = parse_provider_recovery_state(task_policy)
    fingerprint = _metadata_str(metadata, "failure_fingerprint")
    provider = _metadata_str(metadata, "provider") or provider_for_agent_model(
        current_agent,
        current_model,
    )
    model = _metadata_str(metadata, "model") or current_model

    if not bool(metadata.get("retryable")):
        return _terminal_decision("NON_RETRYABLE_PROVIDER_FAILURE", state=state)
    if fingerprint is not None and fingerprint in state.failure_fingerprints:
        return _terminal_decision(PROVIDER_RECOVERY_NO_LOOP_REASON, state=state)

    if state.retry_attempt_number < policy.max_same_provider_retries:
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
        )

    fallback_target = _select_fallback_target(policy, state)
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
            fallback_attempt_number=state.fallback_attempt_number + 1,
            retry_attempt_number=0,
        )

    return _terminal_decision("PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED", state=state)


async def create_provider_recovery_attempt_row(
    session: AsyncSession,
    source_workspace_id: str,
    *,
    now: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ProviderRecoveryAttemptResult | None:
    """Create a requested retry/fallback workspace for a retryable provider failure."""

    recovery_now = now or datetime.now(UTC)
    repo = WorkspaceRepository(session)
    source = await repo.get_for_update(source_workspace_id)
    if source is None:
        return None
    recovery_metadata = (
        dict(metadata)
        if metadata is not None
        else provider_recovery_metadata_from_workspace(source)
    )
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
    source_not_before = _source_suppression_not_before(
        recovery_metadata,
        policy=policy,
        state=state,
        decision=decision,
        now=recovery_now,
    )
    if source_not_before is not None or decision.action == "terminal":
        source.task_policy = _recovery_task_policy(
            source.task_policy,
            source_workspace_id=source.id,
            source_attempt=source_attempt,
            source_canonical_attempt=source_canonical_attempt,
            metadata=recovery_metadata,
            decision=decision,
            not_before=source_not_before,
        )
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
    await _record_provider_circuit_breaker(
        session,
        source,
        recovery_metadata,
        now=recovery_now,
    )
    if _has_existing_provider_recovery_event(source, recovery_metadata):
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
        return None

    new_policy = _recovery_task_policy(
        source.task_policy,
        source_workspace_id=source.id,
        source_attempt=source_attempt,
        source_canonical_attempt=source_canonical_attempt,
        metadata=recovery_metadata,
        decision=decision,
    )
    target_agent = decision.target_agent or source.agent
    if decision.target_model is not None:
        new_policy["agent_model"] = decision.target_model

    retried = await repo.create(
        repo_url=source.repo_url,
        branch_base=source.branch_base,
        task_title=source.task_title,
        task_prompt=source.task_prompt,
        task_external_id=source.task_external_id,
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
        if source.task_kind in {"monitor_release_pr", "sync_release_pr", "sync_feature_pr"}
        else None,
    )

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


def parse_provider_recovery_policy(
    task_policy: Mapping[str, Any] | None,
) -> ProviderRecoveryPolicy:
    raw = task_policy.get(PROVIDER_RECOVERY_POLICY_KEY) if task_policy else None
    policy = raw if isinstance(raw, Mapping) else {}
    fallbacks = tuple(_fallback_targets(policy.get("fallbacks")))
    max_fallback_attempts = _nonnegative_int(
        policy.get("max_fallback_attempts"),
        default=len(fallbacks),
    )
    return ProviderRecoveryPolicy(
        fallbacks=fallbacks,
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
    return ProviderRecoveryState(
        failure_fingerprints=fingerprint_values,
        fallback_attempt_number=_nonnegative_int(
            state.get("fallback_attempt_number"),
            default=0,
        ),
        retry_attempt_number=_nonnegative_int(
            state.get("retry_attempt_number"),
            default=0,
        ),
    )


def provider_for_agent_model(agent: str, model: str | None) -> str | None:
    inferred = infer_provider(model=model)
    if inferred is not None:
        return inferred
    return {
        "codex": "openai",
        "gemini": "google",
        "claude_code": "anthropic",
        "opencode": "opencode",
    }.get(agent)


def provider_cooldown_not_before(
    task_policy: Mapping[str, Any] | None,
) -> datetime | None:
    raw = task_policy.get(PROVIDER_RECOVERY_STATE_KEY) if task_policy else None
    if not isinstance(raw, Mapping):
        return None
    value = raw.get("not_before")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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
    existing = metadata.get("recommended_action")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    return "Retry after provider cooldown or dispatch an approved fallback model."


def _select_fallback_target(
    policy: ProviderRecoveryPolicy,
    state: ProviderRecoveryState,
) -> FallbackTarget | None:
    if state.fallback_attempt_number >= policy.max_fallback_attempts:
        return None
    index = state.fallback_attempt_number
    if index >= len(policy.fallbacks):
        return None
    return policy.fallbacks[index]


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
    backoff = base * (2 ** state.retry_attempt_number)
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
        "failure_fingerprints": fingerprints,
        "fallback_attempt_number": decision.fallback_attempt_number,
        "retry_attempt_number": decision.retry_attempt_number,
        "action": decision.action,
        "target_agent": decision.target_agent,
        "target_provider": decision.target_provider,
        "target_model": decision.target_model,
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
) -> dict[str, str]:
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
        "terminal_reason": decision.terminal_reason,
    }
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
        if getattr(event, "event_type", None) == "workspace.state_changed" and getattr(
            event,
            "new_state",
            None,
        ) == "failed":
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


def _fallback_targets(raw: object) -> list[FallbackTarget]:
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return []
    targets: list[FallbackTarget] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        agent = _mapping_str(item, "agent")
        model = _mapping_str(item, "model")
        if agent is None or model is None:
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
