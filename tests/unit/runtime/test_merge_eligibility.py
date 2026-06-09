from datetime import UTC, datetime, timedelta

import pytest

from awf.db.enums import TaskClass, WorkspaceStatus
from awf.db.models import MergeCandidate, Operation, TaskAttempt, ValidationRun, Workspace
from awf.db.repositories import sync_candidate_readiness
from awf.runtime.merge_eligibility import (
    VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
    _successful_validation_run_tier,
    compute_stale_reason,
    compute_stale_reason_for_attempt,
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
    commands: list[dict[str, object]] | None = None,
) -> ValidationRun:
    return ValidationRun(
        id=f"vr_{tier}_{started_at.timestamp()}",
        workspace_id="ws_tier",
        attempt_id="att_1",
        tier=tier,
        command_set_hash="hash",
        commands=commands or [],
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
def test_compute_stale_reason_accepts_targeted_validation_when_no_local_coverage_gate() -> None:
    workspace = _workspace_with_operations(
        task_class=TaskClass.refactor_task.value,
        operations=[],
    )
    workspace.validation_runs = [
        _validation_run(
            tier=2,
            started_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
            commands=[
                {"phase": "validate", "command": "pytest tests/unit/cli -q"},
            ],
        )
    ]

    assert compute_stale_reason(workspace) == (None, None)


@pytest.mark.unit
def test_successful_validation_run_tier_counts_targeted_validation_without_local_gate() -> None:
    run = _validation_run(
        tier=2,
        started_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        commands=[{"phase": "validate", "command": "pytest tests/unit/cli -q"}],
    )

    assert _successful_validation_run_tier(run) == 2


@pytest.mark.unit
def test_sync_candidate_readiness_accepts_targeted_validation_without_local_gate() -> None:
    validation_run = _validation_run(
        tier=1,
        started_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        commands=[{"phase": "validate", "command": "pytest tests/unit/cli -q"}],
    )
    validate_operation = _operation(
        operation_type="validate",
        created_at=datetime(2026, 4, 27, 12, 1, tzinfo=UTC),
        payload={"requested_tier": 1},
    )
    validate_operation.result = {"validation_run_id": validation_run.id}
    workspace = _workspace_with_operations(
        task_class=TaskClass.test_task.value,
        operations=[validate_operation],
    )
    workspace.auto_merge = True
    workspace.validation_runs = [validation_run]
    attempt = TaskAttempt(
        id="att_1",
        agent="codex",
        is_canonical_for_merge=True,
    )
    candidate = MergeCandidate(
        id="mc_tier_one_scm_check_coverage",
        workspace=workspace,
        attempt=attempt,
        status="open",
        stale=False,
    )

    sync_candidate_readiness(candidate, workspace=workspace, attempt=attempt)

    assert candidate.ready is True
    assert candidate.stale is False
    assert candidate.stale_reason is None


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
def test_advisory_stale_reason_does_not_block_or_require_recovery() -> None:
    from awf.runtime.merge_eligibility import (
        stale_reason_blocks_merge,
        stale_reason_required_action,
        stale_reason_severity,
    )

    reason_code = "ADVISORY_PLAN_ARTIFACT_OVERLAP"

    assert stale_reason_blocks_merge(reason_code) is False
    assert stale_reason_severity(reason_code) == "advisory"
    assert stale_reason_required_action(reason_code) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("reason_code", "required_action"),
    [
        ("STALE_TARGET_ADVANCED", "rebase"),
        ("STALE_OVERLAP", "rebase"),
        ("STALE_DEPENDENCY", "rebase"),
        ("STALE_BUILD_CONFIG", "rebase"),
        ("STALE_SCHEMA", "rebase"),
        (VALIDATION_INSUFFICIENT_TIER_STALE_REASON, "validate"),
    ],
)
def test_blocking_stale_reasons_require_recovery(
    reason_code: str,
    required_action: str,
) -> None:
    from awf.runtime.merge_eligibility import (
        stale_reason_blocks_merge,
        stale_reason_required_action,
        stale_reason_severity,
    )

    assert stale_reason_blocks_merge(reason_code) is True
    assert stale_reason_severity(reason_code) == "blocking"
    assert stale_reason_required_action(reason_code) == required_action


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
def test_compute_stale_reason_uses_profile_requested_tier_for_malformed_payload() -> None:
    workspace = _workspace_with_operations(
        task_class=TaskClass.refactor_task.value,
        requested_tier=2,
        operations=[
            _operation(
                operation_type="validate",
                created_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
                payload={"requested_tier": "2"},
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
def test_compute_stale_reason_ignores_validation_run_started_before_rebase() -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    workspace = _workspace_with_operations(
        task_class=TaskClass.refactor_task.value,
        operations=[
            _operation(operation_type="rebase", created_at=now),
        ],
    )
    workspace.validation_runs = [
        _validation_run(
            tier=2,
            started_at=now - timedelta(minutes=1),
            finished_at=now + timedelta(minutes=1),
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


@pytest.mark.unit
def test_compute_stale_reason_for_attempt_requires_post_rebase_validation() -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    workspace = _workspace_with_operations(
        task_class=TaskClass.test_task.value,
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
        _validation_run(
            tier=2,
            started_at=now + timedelta(minutes=2),
            finished_at=now + timedelta(minutes=3),
        ),
    ]

    assert compute_stale_reason_for_attempt(workspace, attempt_id="att_1") == (None, None)


@pytest.mark.unit
def test_compute_stale_reason_for_attempt_ignores_run_started_before_rebase() -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    workspace = _workspace_with_operations(
        task_class=TaskClass.test_task.value,
        operations=[
            _operation(operation_type="rebase", created_at=now),
        ],
    )
    workspace.validation_runs = [
        _validation_run(
            tier=2,
            started_at=now - timedelta(minutes=1),
            finished_at=now + timedelta(minutes=1),
        ),
    ]

    assert compute_stale_reason_for_attempt(workspace, attempt_id="att_1") == (
        VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
        "validate",
    )


@pytest.mark.unit
def test_compute_stale_reason_for_attempt_ignores_other_attempt_validation() -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    workspace = _workspace_with_operations(
        task_class=TaskClass.refactor_task.value,
        operations=[],
    )
    other_attempt_run = _validation_run(
        tier=2,
        started_at=now,
    )
    other_attempt_run.attempt_id = "att_other"
    workspace.validation_runs = [other_attempt_run]

    assert compute_stale_reason_for_attempt(workspace, attempt_id="att_1") == (
        VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
        "validate",
    )
