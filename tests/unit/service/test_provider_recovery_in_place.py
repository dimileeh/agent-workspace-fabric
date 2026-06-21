"""In-place provider-recovery decision helper tests (#612).

``should_recover_in_place`` decides whether an agent-run failure should divert the
SAME workspace into the auto-healing ``recovering`` pause (in-place retry) instead
of fail-and-relaunching a fresh workspace. It single-sources the classify + budget
logic of the fail-and-relaunch path (``provider_recovery_metadata_from_failure`` +
``decide_provider_recovery``) and returns a decision ONLY for an in-place
same-agent ``retry``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from awf.adapters.provider_failures import (
    AGENT_AUTH_FAILED,
    AGENT_PROVIDER_CAPACITY_EXHAUSTED,
)
from awf.service.provider_recovery import (
    PROVIDER_RETRY_DELAYED_REASON,
    ProviderInPlaceRecoveryDecision,
    in_place_recovery_task_policy,
    rearm_recovering_cooldown_task_policy,
    should_recover_in_place,
)

_NOW = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


@pytest.mark.unit
def test_retryable_idle_timeout_with_budget_returns_in_place_decision() -> None:
    decision = should_recover_in_place(
        reason_code="AGENT_IDLE_TIMEOUT",
        message="idle timeout exceeded after 600s",
        details={"provider": "openai", "model": "gpt-5"},
        task_policy={},
        agent="codex",
        current_model="gpt-5",
        now=_NOW,
    )

    assert decision is not None
    assert isinstance(decision, ProviderInPlaceRecoveryDecision)
    # Same-agent delayed retry → in-place; cooldown deadline is in the future.
    assert decision.reason_code == PROVIDER_RETRY_DELAYED_REASON
    assert decision.not_before > _NOW
    # The first retry consumes one budget slot.
    assert decision.retry_attempt_number == 1
    assert decision.metadata["reason_code"] == "AGENT_IDLE_TIMEOUT"


@pytest.mark.unit
def test_budget_exhausted_returns_none() -> None:
    # ``retry_attempt_number`` already at the default ``max_same_provider_retries``
    # of 1: no same-provider retry remains, so an idle-timeout (no fallback
    # configured) goes terminal → not an in-place divert.
    decision = should_recover_in_place(
        reason_code="AGENT_IDLE_TIMEOUT",
        message="idle timeout exceeded after 600s",
        details={"provider": "openai", "model": "gpt-5"},
        task_policy={"provider_recovery_state": {"retry_attempt_number": 1}},
        agent="codex",
        current_model="gpt-5",
        now=_NOW,
    )

    assert decision is None


@pytest.mark.unit
def test_non_retryable_unclassifiable_failure_returns_none() -> None:
    # A generic agent failure with no provider/markers is not classifiable as a
    # provider failure → no metadata → no in-place divert.
    decision = should_recover_in_place(
        reason_code="AGENT_FAILED",
        message="the agent gave up",
        details={},
        task_policy={},
        agent="codex",
        current_model="gpt-5",
        now=_NOW,
    )

    assert decision is None


@pytest.mark.unit
def test_auth_failure_returns_none() -> None:
    # Auth failures are non-retryable (credentials must be refreshed) → terminal,
    # never an in-place retry.
    decision = should_recover_in_place(
        reason_code=AGENT_AUTH_FAILED,
        message="401 Unauthorized",
        details={"provider": "openai", "model": "gpt-5"},
        task_policy={},
        agent="codex",
        current_model="gpt-5",
        now=_NOW,
    )

    assert decision is None


@pytest.mark.unit
def test_capacity_fallback_to_different_model_returns_none() -> None:
    # A codex capacity failure on a non-default model selects a FALLBACK to the
    # default model (action="fallback"), not an in-place same-model retry — so it
    # keeps today's fresh-relaunch path rather than diverting to recovering.
    decision = should_recover_in_place(
        reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
        message="MODEL_CAPACITY_EXHAUSTED Please try again later.",
        details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        task_policy={"agent_model": "gpt-5.3-codex-spark"},
        agent="codex",
        current_model="gpt-5.3-codex-spark",
        now=_NOW,
    )

    assert decision is None


@pytest.mark.unit
def test_in_place_recovery_task_policy_persists_cooldown_and_budget() -> None:
    decision = should_recover_in_place(
        reason_code="AGENT_IDLE_TIMEOUT",
        message="idle timeout exceeded after 600s",
        details={"provider": "openai", "model": "gpt-5"},
        task_policy={"agent_model": "gpt-5"},
        agent="codex",
        current_model="gpt-5",
        now=_NOW,
    )
    assert decision is not None

    policy = in_place_recovery_task_policy({"agent_model": "gpt-5"}, decision=decision)

    # Unrelated policy keys are preserved.
    assert policy["agent_model"] == "gpt-5"
    state = policy["provider_recovery_state"]
    assert state["action"] == "retry"
    assert state["recovery_scope"] == "agent_run_in_place"
    assert state["retry_attempt_number"] == 1
    assert state["not_before"] == decision.not_before.isoformat()
    # The failure fingerprint is recorded so an identical re-failure is caught as
    # a loop on the resumed run.
    assert state["failure_fingerprints"]
    # No retry-lineage / source-workspace fields — the in-place retry keeps a
    # single clean attempt id.
    assert "source_workspace_id" not in state
    assert "source_attempt_id" not in state


@pytest.mark.unit
def test_in_place_recovery_task_policy_appends_existing_fingerprints() -> None:
    decision = should_recover_in_place(
        reason_code="AGENT_IDLE_TIMEOUT",
        message="idle timeout exceeded after 600s",
        details={"provider": "openai", "model": "gpt-5"},
        task_policy={},
        agent="codex",
        current_model="gpt-5",
        now=_NOW,
    )
    assert decision is not None

    policy = in_place_recovery_task_policy(
        {"provider_recovery_state": {"failure_fingerprints": ["prior-fp"]}},
        decision=decision,
    )

    fingerprints = policy["provider_recovery_state"]["failure_fingerprints"]
    assert "prior-fp" in fingerprints
    assert len(fingerprints) == 2


@pytest.mark.unit
def test_in_place_recovery_task_policy_dedupes_repeated_fingerprint() -> None:
    # A resumed in-place run that re-fails with the SAME provider fingerprint must
    # not duplicate that fingerprint in the persisted state: the loop-detection
    # count would otherwise inflate. The append guard skips a fingerprint already
    # present, so the recorded list stays a single entry.
    decision = should_recover_in_place(
        reason_code="AGENT_IDLE_TIMEOUT",
        message="idle timeout exceeded after 600s",
        details={"provider": "openai", "model": "gpt-5"},
        task_policy={},
        agent="codex",
        current_model="gpt-5",
        now=_NOW,
    )
    assert decision is not None
    fingerprint = decision.metadata["failure_fingerprint"]

    policy = in_place_recovery_task_policy(
        {"provider_recovery_state": {"failure_fingerprints": [fingerprint]}},
        decision=decision,
    )

    fingerprints = policy["provider_recovery_state"]["failure_fingerprints"]
    assert fingerprints == [fingerprint]
    assert fingerprints.count(fingerprint) == 1


@pytest.mark.unit
def test_in_place_recovery_task_policy_preserves_spent_fallback_budget() -> None:
    # The workspace is already a provider fallback attempt
    # (``fallback_attempt_number == 1``). An in-place same-agent retry must carry
    # that spent budget forward so a later provider failure does not parse the
    # count as 0 and re-select an already-exhausted fallback.
    task_policy = {
        "agent_model": "gpt-5",
        "provider_recovery_state": {"fallback_attempt_number": 1},
    }
    decision = should_recover_in_place(
        reason_code="AGENT_IDLE_TIMEOUT",
        message="idle timeout exceeded after 600s",
        details={"provider": "openai", "model": "gpt-5"},
        task_policy=task_policy,
        agent="codex",
        current_model="gpt-5",
        now=_NOW,
    )
    assert decision is not None

    policy = in_place_recovery_task_policy(task_policy, decision=decision)

    assert policy["provider_recovery_state"]["fallback_attempt_number"] == 1


@pytest.mark.unit
def test_rearm_recovering_cooldown_advances_not_before_and_preserves_state() -> None:
    # #647: a failed pre-resume worktree reset re-arms the cooldown so the row backs
    # off a full provider cooldown instead of being re-selected every poll. The rest
    # of ``provider_recovery_state`` is preserved untouched, and the source policy is
    # not mutated in place.
    task_policy = {
        "agent_model": "gpt-5",
        "provider_recovery_state": {
            "action": "retry",
            "retry_attempt_number": 1,
            "not_before": (_NOW - timedelta(seconds=5)).isoformat(),
        },
    }

    rearmed = rearm_recovering_cooldown_task_policy(task_policy, now=_NOW)

    assert rearmed is not None
    state = rearmed["provider_recovery_state"]
    # Default provider cooldown is 300s; ``not_before`` is advanced to ``now + 300s``.
    assert datetime.fromisoformat(state["not_before"]) == _NOW + timedelta(seconds=300)
    assert state["retry_attempt_number"] == 1
    assert state["action"] == "retry"
    # The source policy is left untouched (cooldown still in the past).
    assert datetime.fromisoformat(
        task_policy["provider_recovery_state"]["not_before"]
    ) == _NOW - timedelta(seconds=5)


@pytest.mark.unit
def test_rearm_recovering_cooldown_honors_policy_cooldown_seconds() -> None:
    # A task-supplied ``provider_recovery.cooldown_seconds`` overrides the default.
    task_policy = {
        "provider_recovery": {"cooldown_seconds": 30},
        "provider_recovery_state": {"not_before": _NOW.isoformat()},
    }

    rearmed = rearm_recovering_cooldown_task_policy(task_policy, now=_NOW)

    assert rearmed is not None
    assert datetime.fromisoformat(
        rearmed["provider_recovery_state"]["not_before"]
    ) == _NOW + timedelta(seconds=30)


@pytest.mark.unit
@pytest.mark.parametrize("task_policy", [None, {}, {"provider_recovery_state": "not-a-mapping"}])
def test_rearm_recovering_cooldown_returns_none_without_state(
    task_policy: dict[str, object] | None,
) -> None:
    # A legacy/partial row with no ``provider_recovery_state`` mapping has no cooldown
    # to re-arm, so the helper returns ``None`` and the caller writes nothing.
    assert rearm_recovering_cooldown_task_policy(task_policy, now=_NOW) is None
