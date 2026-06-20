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
    classify_provider_failure,
    infer_provider,
)
from awf.common.redaction import redact_secrets
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

PROVIDER_RECOVERY_REASON_CODES: frozenset[str] = frozenset(
    {
        PROVIDER_MODEL_CIRCUIT_OPEN_REASON,
        PROVIDER_RETRY_DELAYED_REASON,
        PROVIDER_FALLBACK_SELECTED_REASON,
        PROVIDER_AUTH_FAILED,
        PROVIDER_RECOVERY_NO_LOOP_REASON,
        NON_RETRYABLE_PROVIDER_FAILURE,
        PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED,
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
    in_place: bool = False


@dataclass(frozen=True)
class ProviderInPlaceRecoveryDecision:
    """A retryable provider failure that should pause the SAME workspace into
    ``recovering`` and re-run the agent in place after the cooldown (#612),
    rather than fail-and-relaunch a fresh workspace.

    ``not_before`` is the provider cooldown deadline; ``retry_attempt_number`` is
    the budget-consuming count to persist so a second failure on the resumed run
    correctly sees the budget spent; ``metadata`` is the classified provider
    failure metadata to persist for observability / loop detection."""

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

    policy = parse_provider_recovery_policy(task_policy)
    state = parse_provider_recovery_state(task_policy)
    auth_failure = _is_auth_failure_metadata(metadata)
    if auth_failure:
        metadata["retryable"] = False
    metadata["fallback_allowed"] = (
        False
        if auth_failure
        else (
            bool(policy.fallbacks)
            and state.fallback_attempt_number < policy.max_fallback_attempts
            and bool(metadata.get("retryable"))
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

    if _is_auth_failure_metadata(metadata):
        return _terminal_decision(PROVIDER_AUTH_FAILED, state=state)
    if not bool(metadata.get("retryable")):
        return _terminal_decision("NON_RETRYABLE_PROVIDER_FAILURE", state=state)
    if fingerprint is not None and fingerprint in state.failure_fingerprints:
        return _terminal_decision(PROVIDER_RECOVERY_NO_LOOP_REASON, state=state)

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
        )

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


def _is_auth_failure_metadata(metadata: Mapping[str, Any]) -> bool:
    return (
        _metadata_str(metadata, "failure_type") == "auth"
        or _metadata_str(metadata, "reason_code") == AGENT_AUTH_FAILED
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
    if policy.fallbacks:
        return None
    if state.fallback_attempt_number > 0:
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
    """Decide whether an agent-run failure should divert into in-place ``recovering``.

    Single-sources the classify + budget logic the fail-and-relaunch path uses:
    reconstructs the SAME provider-failure metadata
    (``provider_recovery_metadata_from_failure``) and runs the SAME decision
    (``decide_provider_recovery``). Returns a decision ONLY when the result is an
    in-place retry — ``action == "retry"`` AND the target stays the current agent
    (a fallback to a different agent/model is not an in-place resume and keeps
    today's fresh-relaunch path). Returns ``None`` for a non-provider failure
    (no metadata), a terminal/budget-exhausted decision, an auth failure, a loop
    fingerprint, or a fallback — the caller then falls through to ``_mark_failed``
    (and the existing downstream ``_prepare_provider_recovery`` policy is
    unchanged for those branches). T7: lifting this UP to the executor fork lets
    the divert land in ``recovering`` BEFORE the terminal teardown fires, with no
    duplicated classification."""
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
        "retry_attempt_number": decision.retry_attempt_number,
        "not_before": decision.not_before.isoformat(),
        "recommended_action": _metadata_str(decision.metadata, "recommended_action"),
    }
    policy[PROVIDER_RECOVERY_STATE_KEY] = {
        key: value for key, value in recovery_state.items() if value is not None
    }
    return policy


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
    if source_not_before is not None or decision.action == "terminal":
        source.task_policy = source_policy
        source_workspace_mutated = True
    elif monitor_in_place_recovery and decision.action in {"retry", "fallback"}:
        source.task_policy = source_policy
        source_workspace_mutated = True
        if decision.action == "fallback":
            source.agent = decision.target_agent or source.agent
            if decision.target_model is not None:
                source.task_policy = {
                    **source.task_policy,
                    "agent_model": decision.target_model,
                }
    if source_workspace_mutated:
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
    if decision.target_model is not None:
        new_policy["agent_model"] = decision.target_model

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
    if agent == "cursor":
        return "cursor"
    inferred = infer_provider(model=model)
    if inferred is not None:
        return inferred
    return {
        "codex": "openai",
        "gemini": "google",
        "claude_code": "anthropic",
        "opencode": "opencode",
        "grok": "xai",
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
        "action": decision.action,
        "target_agent": decision.target_agent,
        "target_provider": decision.target_provider,
        "target_model": decision.target_model,
        "recommended_action": _metadata_str(metadata, "recommended_action"),
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
        if (
            getattr(event, "event_type", None) == "workspace.state_changed"
            and getattr(
                event,
                "new_state",
                None,
            )
            == "failed"
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


@dataclass(frozen=True)
class ProviderRecoveryStateView:
    action: Literal["retry", "fallback", "terminal"] | None
    reason_code: str | None
    source_provider: str | None
    source_model: str | None
    retry_attempt_number: int | None
    fallback_attempt_number: int | None
    cooldown_until: datetime | None
    next_eligible_at: datetime | None
    fallback_target: FallbackTarget | None
    source_workspace_id: str | None
    source_attempt_id: str | None
    recommended_action: str | None
    terminal: bool | None


def provider_recovery_state_for_workspace(
    workspace: Workspace,
    *,
    now: datetime | None = None,  # noqa: ARG001
) -> ProviderRecoveryStateView | None:
    task_policy = (
        workspace.task_policy
        if isinstance(getattr(workspace, "task_policy", None), Mapping)
        else {}
    )
    recovery_state = task_policy.get(PROVIDER_RECOVERY_STATE_KEY)
    event_view = _provider_recovery_state_from_events(workspace)
    if isinstance(recovery_state, Mapping):
        return _merge_recovery_views(
            _provider_recovery_state_from_task_policy(recovery_state),
            event_view,
        )
    return event_view


def provider_recovery_decision_from_workspace(
    workspace: Workspace,
) -> ProviderRecoveryStateView | None:
    return provider_recovery_state_for_workspace(workspace)


def _validate_recovery_action(raw: str | None) -> Literal["retry", "fallback", "terminal"] | None:
    if raw not in {"retry", "fallback", "terminal"}:
        return None
    return raw  # type: ignore[return-value]


def _recommended_action_for_action(
    action: Literal["retry", "fallback", "terminal"] | None,
) -> str | None:
    if action == "retry":
        return "Retry after provider cooldown."
    if action == "fallback":
        return "Dispatch an approved fallback model."
    if action == "terminal":
        return "No further recovery possible; inspect failure details."
    return None


def _parse_not_before(not_before_str: str | None) -> tuple[datetime | None, datetime | None]:
    if not_before_str is None:
        return None, None
    try:
        parsed = datetime.fromisoformat(not_before_str)
        cooldown_until = (
            parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        )
        return cooldown_until, cooldown_until
    except ValueError:
        return None, None


def _provider_recovery_state_from_task_policy(
    recovery_state: Mapping[str, Any],
) -> ProviderRecoveryStateView | None:
    action = _validate_recovery_action(_mapping_str(recovery_state, "action"))
    reason_code = _mapping_str(recovery_state, "decision_reason_code") or _mapping_str(
        recovery_state, "source_reason_code"
    )
    source_provider = _mapping_str(recovery_state, "source_provider")
    source_model = _mapping_str(recovery_state, "source_model")
    retry_attempt_number = (
        _nonnegative_int(recovery_state.get("retry_attempt_number"), default=0)
        if "retry_attempt_number" in recovery_state
        else None
    )
    fallback_attempt_number = (
        _nonnegative_int(recovery_state.get("fallback_attempt_number"), default=0)
        if "fallback_attempt_number" in recovery_state
        else None
    )
    cooldown_until, next_eligible_at = _parse_not_before(_mapping_str(recovery_state, "not_before"))
    target_agent = _mapping_str(recovery_state, "target_agent")
    target_provider = _mapping_str(recovery_state, "target_provider")
    target_model = _mapping_str(recovery_state, "target_model")
    fallback_target: FallbackTarget | None = None
    if action == "fallback" and target_agent is not None and target_model is not None:
        fallback_target = FallbackTarget(
            agent=target_agent,
            provider=target_provider,
            model=target_model,
        )
    source_workspace_id = _mapping_str(recovery_state, "source_workspace_id")
    source_attempt_id = _mapping_str(recovery_state, "source_attempt_id")
    recommended_action = _mapping_str(
        recovery_state, "recommended_action"
    ) or _recommended_action_for_action(action)
    terminal = action == "terminal" if action is not None else None
    return ProviderRecoveryStateView(
        action=action,
        reason_code=reason_code,
        source_provider=source_provider,
        source_model=source_model,
        retry_attempt_number=retry_attempt_number,
        fallback_attempt_number=fallback_attempt_number,
        cooldown_until=cooldown_until,
        next_eligible_at=next_eligible_at,
        fallback_target=fallback_target,
        source_workspace_id=source_workspace_id,
        source_attempt_id=source_attempt_id,
        recommended_action=recommended_action,
        terminal=terminal,
    )


def _provider_recovery_state_from_events(
    workspace: Workspace,
) -> ProviderRecoveryStateView | None:
    recovery_event_types = frozenset(
        {
            PROVIDER_RECOVERY_REQUESTED_EVENT,
            PROVIDER_RECOVERY_COOLDOWN_EVENT,
            PROVIDER_RECOVERY_TERMINAL_EVENT,
            "workspace.provider_recovery_created",
        }
    )
    events = getattr(workspace, "events", []) or []
    latest_event: Any | None = None
    for event in reversed(events):
        event_type = getattr(event, "event_type", None)
        if event_type in recovery_event_types:
            latest_event = event
            break
    if latest_event is None:
        return None
    payload = getattr(latest_event, "payload", None)
    if not isinstance(payload, Mapping):
        return None
    recovery = payload.get("provider_recovery")
    if not isinstance(recovery, Mapping):
        recovery = payload
    action = _validate_recovery_action(_mapping_str(recovery, "action"))
    reason_code = (
        _mapping_str(recovery, "decision_reason_code")
        or _mapping_str(recovery, "reason_code")
        or (getattr(latest_event, "reason_code", None) or None)
    )
    source_provider = _mapping_str(recovery, "source_provider") or _mapping_str(
        recovery, "provider"
    )
    source_model = _mapping_str(recovery, "source_model") or _mapping_str(recovery, "model")
    cooldown_until, next_eligible_at = _parse_not_before(_mapping_str(recovery, "not_before"))
    target_agent = _mapping_str(recovery, "target_agent")
    target_provider = _mapping_str(recovery, "target_provider")
    target_model = _mapping_str(recovery, "target_model")
    fallback_target: FallbackTarget | None = None
    if action == "fallback" and target_agent is not None and target_model is not None:
        fallback_target = FallbackTarget(
            agent=target_agent,
            provider=target_provider,
            model=target_model,
        )
    source_workspace_id = _mapping_str(payload, "source_workspace_id")
    source_attempt_id = _mapping_str(payload, "source_attempt_id")
    retry_attempt_number = (
        _nonnegative_int(recovery.get("retry_attempt_number"), default=0)
        if "retry_attempt_number" in recovery
        else None
    )
    fallback_attempt_number = (
        _nonnegative_int(recovery.get("fallback_attempt_number"), default=0)
        if "fallback_attempt_number" in recovery
        else None
    )
    recommended_action = _mapping_str(
        recovery, "recommended_action"
    ) or _recommended_action_for_action(action)
    terminal = action == "terminal" if action is not None else None
    return ProviderRecoveryStateView(
        action=action,
        reason_code=reason_code,
        source_provider=source_provider,
        source_model=source_model,
        retry_attempt_number=retry_attempt_number,
        fallback_attempt_number=fallback_attempt_number,
        cooldown_until=cooldown_until,
        next_eligible_at=next_eligible_at,
        fallback_target=fallback_target,
        source_workspace_id=source_workspace_id,
        source_attempt_id=source_attempt_id,
        recommended_action=recommended_action,
        terminal=terminal,
    )


def _merge_recovery_views(
    policy_view: ProviderRecoveryStateView | None,
    event_view: ProviderRecoveryStateView | None,
) -> ProviderRecoveryStateView | None:
    if policy_view is None:
        return event_view
    if event_view is None:
        return policy_view
    return ProviderRecoveryStateView(
        action=policy_view.action if policy_view.action is not None else event_view.action,
        reason_code=policy_view.reason_code
        if policy_view.reason_code is not None
        else event_view.reason_code,
        source_provider=policy_view.source_provider
        if policy_view.source_provider is not None
        else event_view.source_provider,
        source_model=policy_view.source_model
        if policy_view.source_model is not None
        else event_view.source_model,
        retry_attempt_number=policy_view.retry_attempt_number
        if policy_view.retry_attempt_number is not None
        else event_view.retry_attempt_number,
        fallback_attempt_number=policy_view.fallback_attempt_number
        if policy_view.fallback_attempt_number is not None
        else event_view.fallback_attempt_number,
        cooldown_until=policy_view.cooldown_until
        if policy_view.cooldown_until is not None
        else event_view.cooldown_until,
        next_eligible_at=policy_view.next_eligible_at
        if policy_view.next_eligible_at is not None
        else event_view.next_eligible_at,
        fallback_target=policy_view.fallback_target
        if policy_view.fallback_target is not None
        else event_view.fallback_target,
        source_workspace_id=policy_view.source_workspace_id
        if policy_view.source_workspace_id is not None
        else event_view.source_workspace_id,
        source_attempt_id=policy_view.source_attempt_id
        if policy_view.source_attempt_id is not None
        else event_view.source_attempt_id,
        recommended_action=policy_view.recommended_action
        if policy_view.recommended_action is not None
        else event_view.recommended_action,
        terminal=policy_view.terminal if policy_view.terminal is not None else event_view.terminal,
    )
