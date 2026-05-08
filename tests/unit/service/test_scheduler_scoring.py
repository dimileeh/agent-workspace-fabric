"""Pure scheduler scoring contract tests."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import awf.service.scheduler as scheduler
from awf.db.enums import FailureReason, TaskClass
from awf.service.scheduler import (
    HUMAN_BOOST_MAX,
    SchedulerScoreInput,
    compute_scheduler_score,
    scheduler_order_key,
    scheduler_policy_snapshot,
    scheduler_retry_policy_context,
    scheduler_score_from_workspace,
    scheduler_score_input_from_workspace,
    score_summary_with_suppression,
    task_class_bias,
    task_class_priority,
)


@pytest.mark.unit
def test_age_boost_increases_with_queue_wait_and_is_capped() -> None:
    queued_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)

    fresh = compute_scheduler_score(
        SchedulerScoreInput(
            workspace_id="ws_fresh",
            task_class=TaskClass.refactor_task.value,
            base_priority=10,
            queued_at=queued_at,
        ),
        now=queued_at + timedelta(minutes=14),
    )
    aged = compute_scheduler_score(
        SchedulerScoreInput(
            workspace_id="ws_aged",
            task_class=TaskClass.refactor_task.value,
            base_priority=10,
            queued_at=queued_at,
        ),
        now=queued_at + timedelta(minutes=45),
    )
    capped = compute_scheduler_score(
        SchedulerScoreInput(
            workspace_id="ws_capped",
            task_class=TaskClass.refactor_task.value,
            base_priority=10,
            queued_at=queued_at,
        ),
        now=queued_at + timedelta(hours=8),
    )

    assert fresh.age_boost == 0
    assert aged.age_boost == 3
    assert capped.age_boost == 12
    assert capped.score_summary["age_boost"] == 12


@pytest.mark.unit
def test_task_class_priority_and_bias_match_prd_ordering() -> None:
    assert [
        task_class_priority(task_class.value)
        for task_class in (
            TaskClass.migration_task,
            TaskClass.dependency_task,
            TaskClass.build_config_task,
            TaskClass.refactor_task,
            TaskClass.test_task,
            TaskClass.docs_task,
        )
    ] == [5, 4, 3, 2, 1, 0]
    assert task_class_bias(TaskClass.migration_task.value) == 15
    assert task_class_bias(TaskClass.dependency_task.value) == 12
    assert task_class_bias(TaskClass.build_config_task.value) == 10
    assert task_class_bias(TaskClass.refactor_task.value) == 4
    assert task_class_bias(TaskClass.test_task.value) == 2
    assert task_class_bias(TaskClass.docs_task.value) == 0


@pytest.mark.unit
def test_explicit_priority_changes_effective_score_predictably() -> None:
    queued_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    low = compute_scheduler_score(
        SchedulerScoreInput(
            workspace_id="ws_low",
            task_class=TaskClass.test_task.value,
            base_priority=20,
            queued_at=queued_at,
        ),
        now=queued_at,
    )
    high = compute_scheduler_score(
        SchedulerScoreInput(
            workspace_id="ws_high",
            task_class=TaskClass.test_task.value,
            base_priority=35,
            queued_at=queued_at,
        ),
        now=queued_at,
    )

    assert high.effective_score - low.effective_score == 15
    assert high.score_summary["base_priority"] == 35


@pytest.mark.unit
def test_retry_bonus_only_applies_to_infrastructure_failure_parent() -> None:
    queued_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)

    infra = compute_scheduler_score(
        SchedulerScoreInput(
            workspace_id="ws_retry_infra",
            task_class=TaskClass.refactor_task.value,
            base_priority=10,
            queued_at=queued_at,
            parent_failure_reason=FailureReason.infrastructure_failure.value,
        ),
        now=queued_at,
    )
    validation = compute_scheduler_score(
        SchedulerScoreInput(
            workspace_id="ws_retry_validation",
            task_class=TaskClass.refactor_task.value,
            base_priority=10,
            queued_at=queued_at,
            parent_failure_reason=FailureReason.validation_failure.value,
        ),
        now=queued_at,
    )

    assert infra.retry_bonus == 3
    assert validation.retry_bonus == 0


@pytest.mark.unit
def test_retry_backoff_context_is_explained_without_score_advantage() -> None:
    queued_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    not_before = queued_at + timedelta(minutes=20)

    score = compute_scheduler_score(
        SchedulerScoreInput(
            workspace_id="ws_retry_backoff",
            task_class=TaskClass.refactor_task.value,
            base_priority=10,
            queued_at=queued_at,
            provider_not_before=not_before,
        ),
        now=queued_at,
    )

    assert score.retry_bonus == 0
    assert score.score_summary["effective_score"] == 14
    assert score.score_summary["suppression"] == {
        "suppressed": True,
        "reason_code": "PROVIDER_RECOVERY_NOT_BEFORE",
        "not_before": "2026-05-02T12:20:00+00:00",
    }


@pytest.mark.unit
def test_scheduler_workspace_policy_parsing_ignores_unsafe_priority_shapes() -> None:
    queued_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    workspace = SimpleNamespace(
        id="ws_policy_shapes",
        task_class=TaskClass.test_task.value,
        created_at=queued_at,
        task_policy={
            "priority": 7,
            "human_boost": 2,
            "scheduler": {
                "base_priority": True,
                "human_escalation_boost": 3.0,
                "retry_attempt_number": "not-an-int",
            },
            "provider_recovery_state": {
                "parent_failure_reason": " infrastructure_failure ",
                "retry_attempt_number": 4.0,
                "not_before": "not-a-date",
            },
        },
    )

    score_input = scheduler_score_input_from_workspace(workspace)

    assert score_input.base_priority == 7
    assert score_input.human_boost == 3
    assert score_input.parent_failure_reason == "infrastructure_failure"
    assert score_input.retry_attempt_number == 4
    assert score_input.provider_not_before is None


@pytest.mark.unit
def test_scheduler_past_provider_backoff_does_not_suppress_dispatch() -> None:
    queued_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    score = compute_scheduler_score(
        SchedulerScoreInput(
            workspace_id="ws_backoff_elapsed",
            task_class=TaskClass.refactor_task.value,
            base_priority=10,
            queued_at=queued_at,
            provider_not_before=queued_at - timedelta(minutes=5),
        ),
        now=queued_at,
    )

    assert score.score_summary["suppression"] == {"suppressed": False}
    assert score_summary_with_suppression(
        score,
        reason_code="MANUAL_SUPPRESSION",
        detail={"operator": "test"},
    )["suppression"] == {
        "suppressed": True,
        "reason_code": "MANUAL_SUPPRESSION",
        "operator": "test",
    }
    assert score_summary_with_suppression(
        score,
        reason_code="EMPTY_DETAIL_SUPPRESSION",
    )["suppression"] == {
        "suppressed": True,
        "reason_code": "EMPTY_DETAIL_SUPPRESSION",
    }
    assert scheduler._parse_datetime("not-a-date") is None


@pytest.mark.unit
def test_human_escalation_boost_is_bounded_and_explained() -> None:
    queued_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)

    score = compute_scheduler_score(
        SchedulerScoreInput(
            workspace_id="ws_human",
            task_class=TaskClass.refactor_task.value,
            base_priority=10,
            queued_at=queued_at,
            human_boost=99,
        ),
        now=queued_at,
    )

    assert score.human_boost == HUMAN_BOOST_MAX
    assert score.score_summary["human_boost"] == HUMAN_BOOST_MAX
    assert score.effective_score == 19


@pytest.mark.unit
def test_scheduler_order_key_is_deterministic() -> None:
    now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    old_refactor = compute_scheduler_score(
        SchedulerScoreInput(
            workspace_id="ws_old_refactor",
            task_class=TaskClass.refactor_task.value,
            base_priority=10,
            queued_at=now - timedelta(minutes=30),
        ),
        now=now,
    )
    young_refactor = compute_scheduler_score(
        SchedulerScoreInput(
            workspace_id="ws_young_refactor",
            task_class=TaskClass.refactor_task.value,
            base_priority=20,
            queued_at=now,
        ),
        now=now,
    )
    migration = compute_scheduler_score(
        SchedulerScoreInput(
            workspace_id="ws_migration",
            task_class=TaskClass.migration_task.value,
            base_priority=0,
            queued_at=now,
        ),
        now=now,
    )

    ordered = sorted(
        [old_refactor, young_refactor, migration],
        key=scheduler_order_key,
    )

    assert [score.workspace_id for score in ordered] == [
        "ws_migration",
        "ws_young_refactor",
        "ws_old_refactor",
    ]
    assert ordered[0].score_summary["ordering_tuple"] == {
        "class_priority": 5,
        "effective_score": 15,
        "queued_at": "2026-05-02T12:00:00+00:00",
        "workspace_id": "ws_migration",
    }


@pytest.mark.unit
def test_scheduler_order_key_tuple_contract_is_explicit() -> None:
    queued_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    score = compute_scheduler_score(
        SchedulerScoreInput(
            workspace_id="ws_tuple",
            task_class=TaskClass.dependency_task.value,
            base_priority=50,
            queued_at=queued_at,
        ),
        now=queued_at,
    )

    assert scheduler_order_key(score) == (-4, -62, queued_at, "ws_tuple")


@pytest.mark.unit
def test_scheduler_score_from_workspace_parses_policy_fallbacks_and_recovery_state() -> None:
    queued_at = datetime(2026, 5, 2, 12, 0)
    workspace = SimpleNamespace(
        id="ws_policy",
        task_class=TaskClass.refactor_task.value,
        created_at=queued_at,
        task_policy={
            "priority": 7.0,
            "human_boost": 2,
            "scheduler": {
                "human_boost": True,
                "human_escalation_boost": "4",
                "parent_failure_reason": " infrastructure_failure ",
                "retry_attempt_number": "2",
            },
            "provider_recovery_state": {
                "not_before": "2026-05-02T11:59:00+00:00",
            },
        },
    )

    score_input = scheduler_score_input_from_workspace(workspace)
    score = scheduler_score_from_workspace(workspace, now=queued_at.replace(tzinfo=UTC))

    assert score_input.base_priority == 7
    assert score_input.human_boost == 4
    assert score_input.parent_failure_reason == FailureReason.infrastructure_failure.value
    assert score_input.retry_attempt_number == 2
    assert score_input.provider_not_before == datetime(2026, 5, 2, 11, 59, tzinfo=UTC)
    assert score.retry_bonus == 3
    assert score.human_boost == 4
    assert score.score_summary["suppression"] == {"suppressed": False}


@pytest.mark.unit
def test_scheduler_score_from_workspace_parses_integer_valued_decimal_strings() -> None:
    queued_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    workspace = SimpleNamespace(
        id="ws_decimal_policy",
        task_class=TaskClass.docs_task.value,
        created_at=queued_at,
        task_policy={
            "scheduler": {
                "base_priority": "100.0",
                "human_boost": "5.00",
                "retry_attempt_number": "2.0",
            }
        },
    )

    score_input = scheduler_score_input_from_workspace(workspace)
    score = scheduler_score_from_workspace(workspace, now=queued_at)

    assert score_input.base_priority == 100
    assert score_input.human_boost == 5
    assert score_input.retry_attempt_number == 2
    assert score.effective_score == 105


@pytest.mark.unit
def test_scheduler_policy_parsing_falls_back_for_oversized_numeric_text() -> None:
    previous_limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        oversized_digits = "9" * 641

        assert (
            scheduler._policy_int(
                {"priority": oversized_digits},
                "priority",
                fallback=17,
            )
            == 17
        )
    finally:
        sys.set_int_max_str_digits(previous_limit)


@pytest.mark.unit
def test_scheduler_policy_parsing_rejects_invalid_scalar_values() -> None:
    queued_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    workspace = SimpleNamespace(
        id="ws_invalid_policy",
        task_class=TaskClass.test_task.value,
        created_at=queued_at,
        task_policy={
            "priority": "not-an-int",
            "scheduler": {
                "base_priority": "also-not-an-int",
                "retry_attempt_number": "not-an-int",
            },
            "provider_recovery_state": {"not_before": "not-a-datetime"},
        },
    )

    score_input = scheduler_score_input_from_workspace(workspace)

    assert score_input.base_priority == 0
    assert score_input.retry_attempt_number == 0
    assert score_input.provider_not_before is None


@pytest.mark.unit
def test_scheduler_policy_helpers_preserve_retry_context_and_bound_boosts() -> None:
    policy = scheduler_retry_policy_context(
        {"scheduler": {"base_priority": 10}},
        source_workspace_id="ws_parent",
        parent_failure_reason=FailureReason.infrastructure_failure.value,
    )
    snapshot = scheduler_policy_snapshot(base_priority=500, human_boost=500)

    assert policy == {
        "scheduler": {
            "base_priority": 10,
            "source_workspace_id": "ws_parent",
            "parent_failure_reason": FailureReason.infrastructure_failure.value,
        }
    }
    assert snapshot == {"base_priority": 100, "human_boost": HUMAN_BOOST_MAX}


@pytest.mark.unit
def test_score_summary_with_suppression_merges_operator_detail() -> None:
    queued_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    score = compute_scheduler_score(
        SchedulerScoreInput(
            workspace_id="ws_suppressed",
            task_class=TaskClass.refactor_task.value,
            base_priority=10,
            queued_at=queued_at,
        ),
        now=queued_at,
    )

    summary = score_summary_with_suppression(
        score,
        reason_code="PROVIDER_MODEL_CIRCUIT_OPEN",
        detail={"cooldown_until": "2026-05-02T12:30:00+00:00"},
    )

    assert summary["suppression"] == {
        "suppressed": True,
        "reason_code": "PROVIDER_MODEL_CIRCUIT_OPEN",
        "cooldown_until": "2026-05-02T12:30:00+00:00",
    }


@pytest.mark.unit
def test_score_summary_with_suppression_works_without_detail() -> None:
    queued_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    score = compute_scheduler_score(
        SchedulerScoreInput(
            workspace_id="ws_suppressed_without_detail",
            task_class=TaskClass.refactor_task.value,
            base_priority=10,
            queued_at=queued_at,
        ),
        now=queued_at,
    )

    summary = score_summary_with_suppression(score, reason_code="MANUAL_DEFER")

    assert summary["suppression"] == {
        "suppressed": True,
        "reason_code": "MANUAL_DEFER",
    }
