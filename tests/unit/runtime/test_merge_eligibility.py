from datetime import UTC, datetime, timedelta

import pytest

from awf.db.enums import TaskClass, WorkspaceStatus
from awf.db.models import MergeCandidate, Operation, TaskAttempt, ValidationRun, Workspace
from awf.db.repositories import sync_candidate_readiness
from awf.runtime.merge_eligibility import (
    VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
    compute_stale_reason,
)


@pytest.mark.asyncio
async def test_merge_candidate_requires_sufficient_validation_tier() -> None:
    # Set up mock or real db objects
    workspace = Workspace(
        id="ws_1",
        status=WorkspaceStatus.monitoring_pr.value,
        auto_merge=True,
        task_class=TaskClass.refactor_task.value,
    )
    attempt = TaskAttempt(
        id="att_1",
        agent="claude_code",
        is_canonical_for_merge=True,
    )
    candidate = MergeCandidate(
        id="mc_1",
        workspace=workspace,
        attempt=attempt,
        status="open",
        stale=False,
    )

    # In refactor_task, require tier 2. If no validation provenance exists, should be stale.
    # We will pass them to sync_candidate_readiness, maybe it queries validation history?
    # Or maybe a new component computes it. Let's just assert candidate is NOT ready.
    sync_candidate_readiness(candidate, workspace=workspace, attempt=attempt)

    assert candidate.ready is False
    assert candidate.stale is True
    # And there should be a stale reason exposed
    assert hasattr(candidate, "stale_reason")
    assert candidate.stale_reason == "validation_insufficient_tier"


@pytest.mark.asyncio
async def test_merge_candidate_clears_validation_stale_state_when_reason_clears() -> None:
    workspace = Workspace(
        id="ws_1",
        status=WorkspaceStatus.monitoring_pr.value,
        auto_merge=True,
        task_class=TaskClass.test_task.value,
    )
    attempt = TaskAttempt(
        id="att_1",
        agent="claude_code",
        is_canonical_for_merge=True,
    )
    candidate = MergeCandidate(
        id="mc_1",
        workspace=workspace,
        attempt=attempt,
        status="open",
        stale=True,
        stale_reason="validation_insufficient_tier",
    )

    sync_candidate_readiness(candidate, workspace=workspace, attempt=attempt)

    assert candidate.ready is True
    assert candidate.stale is False
    assert candidate.stale_reason is None


def _workspace_with_operations(
    *,
    task_class: str | None,
    requested_tier: int = 1,
    operations: list[Operation] | None = None,
) -> Workspace:
    workspace = Workspace(
        id="ws_tier",
        status=WorkspaceStatus.monitoring_pr.value,
        repo_url="git@github.com:example/repo.git",
        branch_base="main",
        task_title="Tier policy",
        task_prompt="Check merge eligibility",
        agent="codex",
        task_class=task_class,
        resolved_profile={"validation": {"requested_tier": requested_tier}},
    )
    workspace.operations = operations or []
    for operation in workspace.operations:
        operation.workspace_id = workspace.id
    return workspace


def _operation(
    *,
    operation_type: str,
    status: str = "succeeded",
    created_at: datetime,
    payload: dict[str, object] | None = None,
) -> Operation:
    return Operation(
        id=f"op_{operation_type}_{created_at.timestamp()}",
        workspace_id="ws_tier",
        type=operation_type,
        status=status,
        payload=payload,
        created_at=created_at,
    )


def _validation_run(
    *,
    tier: int,
    status: str = "succeeded",
    started_at: datetime,
    finished_at: datetime | None = None,
) -> ValidationRun:
    return ValidationRun(
        id=f"vr_{tier}_{started_at.timestamp()}",
        workspace_id="ws_tier",
        attempt_id="att_1",
        tier=tier,
        command_set_hash="hash",
        commands=[],
        base_commit="base",
        target_branch="awf/ws_tier",
        target_head_sha="head",
        status=status,
        reason_code="VALIDATION_OK" if status == "succeeded" else "COMMAND_FAILED",
        started_at=started_at,
        finished_at=finished_at or started_at + timedelta(minutes=1),
        log_stream_refs={},
    )


@pytest.mark.unit
def test_compute_stale_reason_uses_persisted_validation_run_tier() -> None:
    workspace = _workspace_with_operations(
        task_class=TaskClass.refactor_task.value,
        operations=[],
    )
    workspace.validation_runs = [
        _validation_run(
            tier=2,
            started_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        )
    ]

    assert compute_stale_reason(workspace) == (None, None)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("task_class", "required_tier"),
    [
        (TaskClass.test_task.value, 1),
        (TaskClass.refactor_task.value, 2),
        (TaskClass.dependency_task.value, 2),
        (TaskClass.build_config_task.value, 2),
        (TaskClass.migration_task.value, 3),
    ],
)
def test_compute_stale_reason_enforces_task_class_validation_tiers(
    task_class: str,
    required_tier: int,
) -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    workspace = _workspace_with_operations(
        task_class=task_class,
        operations=[
            _operation(
                operation_type="validate",
                created_at=now,
                payload={"requested_tier": required_tier},
            )
        ],
    )

    assert compute_stale_reason(workspace) == (None, None)


@pytest.mark.unit
def test_compute_stale_reason_blocks_when_tier_is_too_low() -> None:
    workspace = _workspace_with_operations(
        task_class=TaskClass.migration_task.value,
        operations=[
            _operation(
                operation_type="validate",
                created_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
                payload={"requested_tier": 2},
            )
        ],
    )

    assert compute_stale_reason(workspace) == (
        VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
        "validate",
    )


@pytest.mark.unit
def test_compute_stale_reason_accepts_nested_validation_requested_tier() -> None:
    workspace = _workspace_with_operations(
        task_class=TaskClass.refactor_task.value,
        operations=[
            _operation(
                operation_type="validate",
                created_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
                payload={"validation": {"requested_tier": 2}},
            )
        ],
    )

    assert compute_stale_reason(workspace) == (None, None)


@pytest.mark.unit
def test_compute_stale_reason_uses_profile_requested_tier_when_operation_has_no_payload() -> None:
    workspace = _workspace_with_operations(
        task_class=TaskClass.refactor_task.value,
        requested_tier=2,
        operations=[
            _operation(
                operation_type="validate",
                created_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
                payload=None,
            )
        ],
    )

    assert compute_stale_reason(workspace) == (None, None)


@pytest.mark.unit
def test_compute_stale_reason_requires_tier_two_validation_after_rebase() -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    workspace = _workspace_with_operations(
        task_class=TaskClass.test_task.value,
        operations=[
            _operation(operation_type="validate", created_at=now - timedelta(minutes=10)),
            _operation(operation_type="rebase", created_at=now),
        ],
    )

    assert compute_stale_reason(workspace) == (
        VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
        "validate",
    )


@pytest.mark.unit
def test_compute_stale_reason_clears_after_post_rebase_validation() -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    workspace = _workspace_with_operations(
        task_class=TaskClass.test_task.value,
        operations=[
            _operation(operation_type="rebase", created_at=now),
            _operation(
                operation_type="validate",
                created_at=now + timedelta(minutes=1),
                payload={"requested_tier": 1},
            ),
        ],
    )

    assert compute_stale_reason(workspace) == (None, None)


@pytest.mark.unit
def test_compute_stale_reason_ignores_failed_and_pre_rebase_validation_runs() -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    workspace = _workspace_with_operations(
        task_class=TaskClass.refactor_task.value,
        operations=[
            _operation(operation_type="rebase", created_at=now),
        ],
    )
    workspace.validation_runs = [
        _validation_run(
            tier=3,
            started_at=now - timedelta(minutes=10),
            finished_at=now - timedelta(minutes=9),
        ),
        _validation_run(
            tier=3,
            status="failed",
            started_at=now + timedelta(minutes=1),
        ),
    ]

    assert compute_stale_reason(workspace) == (
        VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
        "validate",
    )


@pytest.mark.unit
def test_compute_stale_reason_ignores_failed_validation_and_rebase_operations() -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    workspace = _workspace_with_operations(
        task_class=TaskClass.refactor_task.value,
        operations=[
            _operation(
                operation_type="rebase",
                status="failed",
                created_at=now,
            ),
            _operation(
                operation_type="validate",
                status="failed",
                created_at=now + timedelta(minutes=1),
                payload={"requested_tier": 3},
            ),
        ],
    )

    assert compute_stale_reason(workspace) == (
        VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
        "validate",
    )
