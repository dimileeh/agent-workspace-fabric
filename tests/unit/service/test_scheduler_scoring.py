"""Pure scheduler scoring contract tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from awf.db.enums import FailureReason, TaskClass
from awf.service.scheduler import (
    HUMAN_BOOST_MAX,
    SchedulerScoreInput,
    compute_scheduler_score,
    scheduler_order_key,
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
