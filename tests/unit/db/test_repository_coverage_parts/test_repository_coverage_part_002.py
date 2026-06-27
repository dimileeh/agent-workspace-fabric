"""Coverage-focused repository behavior tests.

These tests intentionally use the real ORM metadata against PostgreSQL. They
target repository branches that are hard to observe through higher-level service
tests while still asserting durable behavior, not just execution.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Task, TaskAttempt, Workspace
from awf.db.repositories import (
    CallbackSubscriptionRepository,
    MergeCandidateRepository,
    OperationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkerHeartbeatRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
    _wildcard_prefixes_overlap,
    owned_path_overlap_match,
    owned_paths_overlap,
)
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        async with factory() as s:
            yield s


async def _workspace(
    session: AsyncSession,
    *,
    title: str,
    repo_url: str = "git@github.com:example/repository-coverage.git",
    branch_base: str = "development",
    status: WorkspaceStatus = WorkspaceStatus.requested,
    agent: AgentRuntime = AgentRuntime.codex,
    owned_paths: list[str] | None = None,
) -> Workspace:
    workspace = await WorkspaceRepository(session).create(
        repo_url=repo_url,
        branch_base=branch_base,
        task_title=title,
        task_prompt="Exercise repository behavior.",
        agent=agent.value,
        test_commands=["pytest -q"],
        owned_paths=list(owned_paths or []),
    )
    workspace.status = status.value
    await session.flush()
    return workspace


async def _task(
    session: AsyncSession,
    *,
    external_id: str,
    title: str = "Repository coverage task",
    repo_url: str = "git@github.com:example/repository-coverage.git",
    base_branch: str = "development",
) -> Task:
    return await TaskRepository(session).create_or_get(
        repo_url=repo_url,
        base_branch=base_branch,
        title=title,
        prompt="Exercise task behavior.",
        external_id=external_id,
        idempotency_key=None,
        task_class="test_task",
        owned_paths=["src/awf/**"],
    )


async def _attempt(
    session: AsyncSession,
    *,
    task: Task | None = None,
    workspace: Workspace | None = None,
    external_id: str = "REPO-COVERAGE-1",
    title: str = "Repository coverage attempt",
    status: WorkspaceStatus = WorkspaceStatus.requested,
    agent: AgentRuntime = AgentRuntime.codex,
) -> tuple[Task, TaskAttempt, Workspace]:
    workspace = workspace or await _workspace(
        session,
        title=title,
        status=status,
        agent=agent,
    )
    task = task or await _task(session, external_id=external_id, title=title)
    attempt = await TaskAttemptRepository(session).create_for_workspace(
        task=task,
        workspace=workspace,
    )
    attempt.status = workspace.status
    await session.flush()
    return task, attempt, workspace


async def _candidate(
    session: AsyncSession,
    *,
    title: str,
    external_id: str,
    pr_number: int,
    status: WorkspaceStatus = WorkspaceStatus.monitoring_pr,
    head_sha: str | None = "h" * 40,
    base_sha: str | None = "b" * 40,
) -> tuple[Task, TaskAttempt, Workspace]:
    task, attempt, workspace = await _attempt(
        session,
        external_id=external_id,
        title=title,
        status=status,
    )
    workspace.branch_name = f"awf/{workspace.id}"
    workspace.remote_push_branch = workspace.branch_name
    workspace.base_commit = base_sha
    workspace.pr_url = f"https://github.com/example/repository-coverage/pull/{pr_number}"
    workspace.pr_number = pr_number
    await MergeCandidateRepository(session).create_or_update_open_for_attempt(
        task=task,
        attempt=attempt,
        workspace=workspace,
        head_sha=head_sha,
        base_sha=base_sha,
    )
    await session.flush()
    return task, attempt, workspace


@pytest.mark.unit
@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("", "src/awf/db/repositories.py", False),
        (".", "src/awf/db/repositories.py", False),
        ("src/awf/../awf/db", "src/awf/db/repositories.py", True),
        ("**", "docs/index.md", True),
        ("src/awf/*", "src/awf/service/controls.py", True),
        ("src/awf/db/**", "src/awf/service/**", False),
        ("src/awf/db/**", "src/awf/db/repositories.py", True),
        ("docs/*.md", "docs/reference/*.md", True),
        ("packages/api", "packages/api-client", False),
        ("../src/**", "src/awf/db/repositories.py", True),
    ],
)
def test_owned_path_overlap_normalizes_literals_and_wildcards(
    left: str,
    right: str,
    expected: bool,
) -> None:
    assert owned_paths_overlap(left, right) is expected


@pytest.mark.unit
def test_wildcard_prefix_helpers_cover_root_and_nested_prefixes() -> None:
    assert _wildcard_prefixes_overlap("", "src/") is True
    assert _wildcard_prefixes_overlap("src/", "src/awf/") is True
    assert _wildcard_prefixes_overlap("src/awf/", "tests/") is False


@pytest.mark.unit
def test_owned_path_overlap_match_reports_overlapping_wildcard_prefixes() -> None:
    match = owned_path_overlap_match("src/awf/**", "src/awf/service/**")

    assert match is not None
    assert match.match_reason_code == "OWNED_PATH_WILDCARD_MATCH"
    assert "Wildcard owned-path prefixes overlap" in match.explanation


@pytest.mark.unit
def test_owned_path_overlap_match_reports_wildcard_prefix_only_overlap() -> None:
    match = owned_path_overlap_match("src/awf/service*/**", "src/awf/service-tests*/**")

    assert match is not None
    assert match.match_reason_code == "OWNED_PATH_WILDCARD_MATCH"


@pytest.mark.unit
async def test_repository_replay_key_helpers_short_circuit_non_positive_limits() -> None:
    assert (
        await WorkspaceRepository(  # type: ignore[arg-type]
            object(),
            dialect_name="sqlite",
        ).list_idempotency_replay_keys(limit=0)
        == []
    )
    assert (
        await CallbackSubscriptionRepository(  # type: ignore[arg-type]
            object(),
            dialect_name="sqlite",
        ).list_idempotency_replay_keys(limit=0)
        == []
    )


@pytest.mark.unit
async def test_workspace_events_can_be_filtered_without_workspace_id(
    session: AsyncSession,
) -> None:
    first = await _workspace(session, title="events first")
    second = await _workspace(session, title="events second")
    repo = WorkspaceRepository(session)
    await repo.add_event(
        first,
        event_type="workspace.custom",
        reason_code="CUSTOM",
    )
    await repo.add_event(
        second,
        event_type="workspace.other",
        reason_code="OTHER",
    )

    rows = await WorkspaceEventRepository(session).list(
        event_type="workspace.custom",
        limit=10,
    )

    assert [row.workspace_id for row in rows] == [first.id]


@pytest.mark.unit
async def test_operation_active_matching_payload_skips_non_dict_payloads(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, title="operation payload identity")
    repo = OperationRepository(session)
    await repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.refresh,
        status=OperationStatus.pending,
        payload=None,
    )
    matching = await repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.refresh,
        status=OperationStatus.running,
        payload={"source": "operator_api", "reason": "refresh"},
    )

    found = await repo.find_active_matching_payload(
        workspace_id=workspace.id,
        operation_type=OperationType.refresh,
        payload_identity={"source": "operator_api", "reason": "refresh"},
    )
    missing = await repo.find_active_matching_payload(
        workspace_id=workspace.id,
        operation_type=OperationType.refresh,
        payload_identity={"source": "operator_api", "reason": "different"},
    )

    assert found is not None
    assert found.id == matching.id
    assert missing is None


@pytest.mark.unit
async def test_operation_active_matching_payload_requires_present_null_keys(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, title="operation payload explicit null identity")
    repo = OperationRepository(session)
    await repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.refresh,
        status=OperationStatus.pending,
        payload={"source": "operator_api"},
    )
    matching = await repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.refresh,
        status=OperationStatus.pending,
        payload={"source": "operator_api", "reason": None},
    )

    found = await repo.find_active_matching_payload(
        workspace_id=workspace.id,
        operation_type=OperationType.refresh,
        payload_identity={"source": "operator_api", "reason": None},
    )

    assert found is not None
    assert found.id == matching.id


@pytest.mark.unit
async def test_operation_start_sets_running_started_at_once(session: AsyncSession) -> None:
    workspace = await _workspace(session, title="operation start audit")
    repo = OperationRepository(session)
    operation = await repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.validate,
        status=OperationStatus.pending,
        payload={"source": "pr_monitor", "reason_code": "STALE_TARGET_ADVANCED"},
    )

    started = await repo.start(operation)
    first_started_at = started.started_at
    restarted = await repo.start(operation)

    assert started.status == OperationStatus.running.value
    assert first_started_at is not None
    assert restarted.started_at == first_started_at


@pytest.mark.unit
async def test_operation_finish_success_preserves_audit_payload_and_log_refs(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, title="operation finish success audit")
    repo = OperationRepository(session)
    payload = {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "rerun validation",
        "reason_code": "OPERATOR_VALIDATE",
    }
    operation = await repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.validate,
        status=OperationStatus.running,
        payload=payload,
    )
    started_at = operation.started_at

    await repo.finish(
        operation,
        status=OperationStatus.succeeded,
        result={"status": "validated", "log_stream_refs": {"monitor": "monitor.log"}},
        log_stream_refs={"commands": [{"stdout": "validation.01_validate.stdout"}]},
    )

    assert operation.payload == payload
    assert operation.started_at == started_at
    assert operation.finished_at is not None
    assert operation.result == {
        "status": "validated",
        "log_stream_refs": {
            "monitor": "monitor.log",
            "commands": [{"stdout": "validation.01_validate.stdout"}],
        },
    }


@pytest.mark.unit
async def test_operation_finish_failure_sets_failure_audit_without_losing_payload(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, title="operation finish failure audit")
    repo = OperationRepository(session)
    payload = {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "stop workspace",
        "reason_code": "OPERATOR_STOP",
    }
    operation = await repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.stop,
        status=OperationStatus.pending,
        payload=payload,
    )

    await repo.finish(
        operation,
        status=OperationStatus.failed,
        error_code="STACK_STOP_FAILED",
        error_message="docker stop failed",
        log_stream_refs={"stderr": "stop.stderr"},
    )

    assert operation.status == OperationStatus.failed.value
    assert operation.payload == payload
    assert operation.started_at is not None
    assert operation.finished_at is not None
    assert operation.error_code == "STACK_STOP_FAILED"
    assert operation.error_message == "docker stop failed"
    assert operation.result == {"log_stream_refs": {"stderr": "stop.stderr"}}


@pytest.mark.unit
async def test_execution_claim_epoch_default_and_cas_fencing(
    session: AsyncSession,
) -> None:
    workspace_repo = WorkspaceRepository(session)
    workspace = await _workspace(
        session,
        title="execution claim epoch fencing",
        status=WorkspaceStatus.provisioning,
    )
    # D1: fresh rows default the fencing token to 0 (ORM default).
    assert workspace.execution_claim_epoch == 0

    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    expiry = now + timedelta(minutes=5)
    later = now + timedelta(minutes=10)
    workspace.execution_claimed_by = "runner-1"
    workspace.execution_claim_expires_at = expiry
    workspace.execution_claim_epoch = 5
    await session.flush()

    # read_execution_claim_epoch (D2): returns the epoch for the owner, None otherwise.
    assert await workspace_repo.read_execution_claim_epoch(workspace.id, owner_id="runner-1") == 5
    assert (
        await workspace_repo.read_execution_claim_epoch(workspace.id, owner_id="runner-2") is None
    )

    # refresh CAS (D6): matching owner+epoch succeeds; wrong epoch / wrong owner fenced.
    assert not await workspace_repo.refresh_execution_claim(
        workspace.id,
        owner_id="runner-1",
        lease_expires_at=later,
        execution_claim_epoch=4,
    )
    assert not await workspace_repo.refresh_execution_claim(
        workspace.id,
        owner_id="runner-2",
        lease_expires_at=later,
        execution_claim_epoch=5,
    )
    assert await workspace_repo.refresh_execution_claim(
        workspace.id,
        owner_id="runner-1",
        lease_expires_at=later,
        execution_claim_epoch=5,
    )
    # epoch=None keeps the legacy owner-only behavior.
    assert await workspace_repo.refresh_execution_claim(
        workspace.id,
        owner_id="runner-1",
        lease_expires_at=later,
    )

    # release must NOT clobber a row whose epoch advanced past the caller's.
    assert not await workspace_repo.release_execution_claim(
        workspace.id,
        owner_id="runner-1",
        execution_claim_epoch=4,
    )
    await session.refresh(workspace)
    assert workspace.execution_claimed_by == "runner-1"
    assert workspace.execution_claim_epoch == 5

    # matching epoch releases the claim.
    assert await workspace_repo.release_execution_claim(
        workspace.id,
        owner_id="runner-1",
        execution_claim_epoch=5,
    )
    await session.refresh(workspace)
    assert workspace.execution_claimed_by is None
    assert workspace.execution_claim_expires_at is None


@pytest.mark.unit
async def test_claim_monitoring_pr_clears_stale_execution_claim_bumps_epoch(
    session: AsyncSession,
) -> None:
    workspace_repo = WorkspaceRepository(session)
    now = datetime(2026, 5, 2, 9, 0, tzinfo=UTC)
    workspace = await _workspace(
        session,
        title="monitor claim clears stale execution epoch",
        status=WorkspaceStatus.monitoring_pr,
    )
    workspace.execution_claimed_by = "stale-runner"
    workspace.execution_claim_expires_at = now - timedelta(minutes=5)
    workspace.execution_claim_epoch = 7
    await session.flush()

    claimed = await workspace_repo.claim_monitoring_pr(
        workspace.id,
        owner_id="owner-1",
        lease_expires_at=now + timedelta(minutes=5),
        now=now,
        clear_stale_execution_claim_cutoff=now,
    )
    assert claimed
    await session.refresh(workspace)
    assert workspace.execution_claimed_by is None
    assert workspace.execution_claim_expires_at is None
    # D3: clearing a stale execution claim bumps the fencing token so a zombie
    # whose owner string still matches is fenced on its next CAS write.
    assert workspace.execution_claim_epoch == 8


@pytest.mark.unit
async def test_claim_monitoring_pr_defers_for_different_unexpired_execution_claim(
    session: AsyncSession,
) -> None:
    workspace_repo = WorkspaceRepository(session)
    now = datetime(2026, 5, 2, 9, 0, tzinfo=UTC)
    execution_expires_at = now + timedelta(minutes=5)
    workspace = await _workspace(
        session,
        title="monitor claim defers to unexpired execution claim",
        status=WorkspaceStatus.monitoring_pr,
    )
    workspace.execution_claimed_by = "live-runner"
    workspace.execution_claim_expires_at = execution_expires_at
    workspace.execution_claim_epoch = 7
    await WorkerHeartbeatRepository(session).record_heartbeat(
        worker_id="live-runner",
        node_id="local",
        started_at=now - timedelta(minutes=1),
        last_heartbeat_at=now,
        poll_interval_seconds=1.0,
    )
    await session.flush()

    claimed = await workspace_repo.claim_monitoring_pr(
        workspace.id,
        owner_id="owner-1",
        lease_expires_at=now + timedelta(minutes=5),
        now=now,
        clear_stale_execution_claim_cutoff=now,
    )
    assert not claimed
    await session.refresh(workspace)
    assert workspace.monitor_claimed_by is None
    assert workspace.monitor_claim_expires_at is None
    assert workspace.execution_claimed_by == "live-runner"
    assert workspace.execution_claim_expires_at == execution_expires_at
    assert workspace.execution_claim_epoch == 7


@pytest.mark.unit
async def test_claim_monitoring_pr_clears_unexpired_execution_claim_from_missing_heartbeat_owner(
    session: AsyncSession,
) -> None:
    workspace_repo = WorkspaceRepository(session)
    now = datetime(2026, 5, 2, 9, 0, tzinfo=UTC)
    execution_expires_at = now + timedelta(minutes=5)
    workspace = await _workspace(
        session,
        title="monitor claim clears dead owner execution claim",
        status=WorkspaceStatus.monitoring_pr,
    )
    workspace.execution_claimed_by = "dead-runner"
    workspace.execution_claim_expires_at = execution_expires_at
    workspace.execution_claim_epoch = 7
    await session.flush()

    claimed = await workspace_repo.claim_monitoring_pr(
        workspace.id,
        owner_id="owner-1",
        lease_expires_at=now + timedelta(minutes=5),
        now=now,
        clear_stale_execution_claim_cutoff=now,
    )
    assert claimed
    await session.refresh(workspace)
    assert workspace.monitor_claimed_by == "owner-1"
    assert workspace.execution_claimed_by is None
    assert workspace.execution_claim_expires_at is None
    assert workspace.execution_claim_epoch == 8


@pytest.mark.unit
async def test_claim_monitoring_pr_preserves_epoch_for_same_owner_execution_claim(
    session: AsyncSession,
) -> None:
    workspace_repo = WorkspaceRepository(session)
    now = datetime(2026, 5, 2, 9, 0, tzinfo=UTC)
    execution_expires_at = now + timedelta(minutes=5)
    monitor_expires_at = now + timedelta(minutes=10)
    workspace = await _workspace(
        session,
        title="monitor claim preserves same-owner execution epoch",
        status=WorkspaceStatus.monitoring_pr,
    )
    workspace.execution_claimed_by = "owner-1"
    workspace.execution_claim_expires_at = execution_expires_at
    workspace.execution_claim_epoch = 7
    await session.flush()

    claimed = await workspace_repo.claim_monitoring_pr(
        workspace.id,
        owner_id="owner-1",
        lease_expires_at=monitor_expires_at,
        now=now,
        clear_stale_execution_claim_cutoff=now,
    )
    assert claimed
    await session.refresh(workspace)
    assert workspace.monitor_claimed_by == "owner-1"
    assert workspace.monitor_claim_expires_at == monitor_expires_at
    assert workspace.execution_claimed_by == "owner-1"
    assert workspace.execution_claim_expires_at == execution_expires_at
    assert workspace.execution_claim_epoch == 7


@pytest.mark.unit
async def test_claim_monitoring_pr_with_active_postgres_expiry_uses_database_compare(
    session: AsyncSession,
) -> None:
    workspace_repo = WorkspaceRepository(session)
    claim_workspace = await _workspace(
        session,
        title="monitor claim naive expiry",
        status=WorkspaceStatus.monitoring_pr,
    )
    claim_workspace.monitor_claimed_by = "owner-1"
    claim_workspace.monitor_claim_expires_at = datetime(2026, 4, 27, 15, 5, tzinfo=UTC)
    await session.flush()

    assert not await workspace_repo.claim_monitoring_pr(
        claim_workspace.id,
        owner_id="owner-2",
        lease_expires_at=datetime(2026, 4, 27, 15, 10, tzinfo=UTC),
        now=datetime(2026, 4, 27, 15, 1, tzinfo=UTC),
    )
