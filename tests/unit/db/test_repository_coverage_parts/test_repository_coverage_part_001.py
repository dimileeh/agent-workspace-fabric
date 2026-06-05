"""Coverage-focused repository behavior tests.

These tests intentionally use the real ORM metadata against PostgreSQL. They
target repository branches that are hard to observe through higher-level service
tests while still asserting durable behavior, not just execution.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import AgentRuntime, OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.db.models import ProviderModelCircuitBreaker, Task, TaskAttempt, Workspace
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    PolicyFindingCreate,
    PolicyFindingRepository,
    QueueDecisionRepository,
    ResourceReservationRepository,
    StaleReasonCreate,
    StaleReasonRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
    _as_utc_naive,
    _callback_delivery_insert_if_absent_stmt,
    _callback_subscription_insert_if_absent_stmt,
    _candidate_terminal_close_reason,
    _circuit_breaker_expired,
    _claims_non_docs_path,
    _operation_idempotency_advisory_lock_key,
    _owned_path_conflict_advisory_lock_key,
    _provider_model_circuit_breaker_insert_if_absent_stmt,
    _secret_lease_insert_if_absent_stmt,
    _workspace_idempotency_advisory_lock_key,
    resolve_session_dialect_name,
    sync_candidate_readiness,
)
from awf.db.session import make_session_factory
from awf.runtime.merge_eligibility import (
    DOCS_TASK_SCOPE_VIOLATION_STALE_REASON,
    VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
)
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
async def test_task_attempt_lock_is_noop_for_non_postgres_dialects() -> None:
    class RecordingSession:
        info: dict[str, str] = {}

        def __init__(self) -> None:
            self.executed: list[tuple[object, dict[str, object] | None]] = []

        async def execute(
            self,
            statement: object,
            params: dict[str, object] | None = None,
        ) -> object:
            self.executed.append((statement, params))
            return object()

    session = RecordingSession()
    repo = TaskAttemptRepository(session, dialect_name="sqlite")

    await repo._lock_attempt_number_sequence("task-no-lock")  # noqa: SLF001
    assert session.executed == []


@pytest.mark.unit
def test_repository_time_helpers_normalize_circuit_breaker_expiry() -> None:
    aware_now = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    naive_now = aware_now.replace(tzinfo=None)

    assert _as_utc_naive(naive_now) is naive_now
    assert _as_utc_naive(aware_now) == naive_now
    assert not _circuit_breaker_expired(
        ProviderModelCircuitBreaker(
            id="pcb_closed",
            provider="codex",
            model="gpt-5.5",
            state="closed",
            cooldown_until=aware_now - timedelta(seconds=1),
        ),
        aware_now,
    )
    assert not _circuit_breaker_expired(
        ProviderModelCircuitBreaker(
            id="pcb_open_without_cooldown",
            provider="codex",
            model="gpt-5.5",
            state="open",
            cooldown_until=None,
        ),
        aware_now,
    )
    assert _circuit_breaker_expired(
        ProviderModelCircuitBreaker(
            id="pcb_expired",
            provider="codex",
            model="gpt-5.5",
            state="open",
            cooldown_until=aware_now - timedelta(seconds=1),
        ),
        aware_now,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("helper", "conflict_columns"),
    [
        (
            _secret_lease_insert_if_absent_stmt,
            "workspace_id, secret_name, kind, target",
        ),
        (
            _callback_subscription_insert_if_absent_stmt,
            "idempotency_key",
        ),
        (
            _callback_delivery_insert_if_absent_stmt,
            "subscription_id, dedupe_key",
        ),
        (
            _provider_model_circuit_breaker_insert_if_absent_stmt,
            "provider, model",
        ),
    ],
)
def test_idempotent_insert_helpers_support_postgres_and_fallback(
    helper: Callable[[str | None], object | None],
    conflict_columns: str,
) -> None:
    stmt = helper("postgresql")

    assert stmt is not None
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert f"ON CONFLICT ({conflict_columns}) DO NOTHING" in sql
    assert "RETURNING" in sql

    assert helper(None) is None


@pytest.mark.unit
async def test_task_create_or_get_backfills_identifiers_and_get_by_ref(
    session: AsyncSession,
) -> None:
    repo = TaskRepository(session)

    by_key = await repo.create_or_get(
        repo_url="git@github.com:example/repository-coverage.git",
        base_branch="development",
        title="Backfill external id",
        prompt="first request",
        external_id=None,
        idempotency_key="task-key-1",
        task_class=None,
        owned_paths=[],
    )
    same_by_key = await repo.create_or_get(
        repo_url="git@github.com:example/repository-coverage.git",
        base_branch="development",
        title="Backfill external id",
        prompt="second request",
        external_id="TICKET-BACKFILL",
        idempotency_key="task-key-1",
        task_class=None,
        owned_paths=[],
    )
    by_external = await repo.create_or_get(
        repo_url="git@github.com:example/repository-coverage.git",
        base_branch="development",
        title="Backfill idempotency",
        prompt="first external request",
        external_id="TICKET-KEY",
        idempotency_key=None,
        task_class=None,
        owned_paths=[],
    )
    same_by_external = await repo.create_or_get(
        repo_url="git@github.com:example/repository-coverage.git",
        base_branch="development",
        title="Backfill idempotency",
        prompt="first external request",
        external_id="TICKET-KEY",
        idempotency_key="task-key-2",
        task_class=None,
        owned_paths=[],
    )

    assert same_by_key.id == by_key.id
    assert by_key.external_id == "TICKET-BACKFILL"
    assert same_by_external.id == by_external.id
    assert by_external.idempotency_key == "task-key-2"
    assert (await repo.get_by_ref(by_key.id)).id == by_key.id
    assert (await repo.get_by_ref("TICKET-BACKFILL")).id == by_key.id


@pytest.mark.unit
async def test_task_attempt_latest_and_canonical_lookup_filters(
    session: AsyncSession,
) -> None:
    repo = TaskAttemptRepository(session)
    first_task = await _task(session, external_id="ATTEMPT-LATEST-1", title="first task")
    first_old_workspace = await _workspace(session, title="first old")
    first_new_workspace = await _workspace(
        session,
        title="first latest",
        status=WorkspaceStatus.ready,
        agent=AgentRuntime.gemini,
    )
    first_old = await repo.create_for_workspace(task=first_task, workspace=first_old_workspace)
    first_new = await repo.create_for_workspace(task=first_task, workspace=first_new_workspace)
    second_task, second_attempt, _second_workspace = await _attempt(
        session,
        external_id="ATTEMPT-LATEST-2",
        title="second latest",
        status=WorkspaceStatus.ready,
        agent=AgentRuntime.codex,
    )

    assert await repo.list_canonical_ids_for_tasks([]) == {}
    await repo.mark_canonical_for_merge(first_new)
    await repo.mark_canonical_for_merge(second_attempt)

    canonical = await repo.list_canonical_ids_for_tasks(
        [first_task.id, second_task.id, first_task.id]
    )
    listed_for_task = await repo.list_for_task(first_task.id, limit=10)
    latest_ready_gemini = await repo.list_latest(
        status=WorkspaceStatus.ready,
        agent=AgentRuntime.gemini,
        repo_url="git@github.com:example/repository-coverage.git",
        limit=10,
    )
    latest_ready_codex = await repo.list_latest(
        status=WorkspaceStatus.ready.value,
        agent=AgentRuntime.codex.value,
        limit=10,
    )

    assert canonical == {first_task.id: first_new.id, second_task.id: second_attempt.id}
    assert [attempt.id for attempt in listed_for_task] == [first_new.id, first_old.id]
    assert [attempt.id for attempt in latest_ready_gemini] == [first_new.id]
    assert [attempt.id for attempt in latest_ready_codex] == [second_attempt.id]


@pytest.mark.unit
async def test_postgres_only_repository_locks_execute_advisory_lock_helpers() -> None:
    class RecordingSession:
        info: dict[str, str] = {}

        def __init__(self) -> None:
            self.executed: list[tuple[object, dict[str, object] | None]] = []

        async def execute(
            self,
            statement: object,
            params: dict[str, object] | None = None,
        ) -> object:
            self.executed.append((statement, params))
            return object()

    class NoBindSession:
        info: dict[str, str] = {}
        bind = None

    class NonStringDialectSession:
        info: dict[str, str] = {}
        bind = type(
            "Bind",
            (),
            {"dialect": type("Dialect", (), {"name": object()})()},
        )()

    assert resolve_session_dialect_name(NoBindSession(), None) is None
    assert resolve_session_dialect_name(NonStringDialectSession(), None) is None

    task_session = RecordingSession()
    await TaskAttemptRepository(
        task_session,
        dialect_name="postgresql",
    )._lock_attempt_number_sequence("task-1")

    workspace_session = RecordingSession()
    workspace_repo = WorkspaceRepository(workspace_session, dialect_name="postgresql")
    await workspace_repo.acquire_owned_path_conflict_lock(
        repo_url="repo3",
        branch_base="main",
        owned_paths=[".", "src/awf/**"],
    )
    await workspace_repo.acquire_owned_path_conflict_lock(
        repo_url="repo3",
        branch_base="main",
        owned_paths=[".", ""],
    )
    workspace_idempotency_session = RecordingSession()
    await WorkspaceRepository(
        workspace_idempotency_session,
        dialect_name="postgresql",
    ).acquire_idempotency_key_lock("key0")
    operation_session = RecordingSession()
    await OperationRepository(
        operation_session,
        dialect_name="postgresql",
    ).acquire_idempotency_key_lock("key0")

    assert len(task_session.executed) == 1
    assert len(workspace_session.executed) == 1
    assert workspace_session.executed[0][1] == {
        "lock_key": _owned_path_conflict_advisory_lock_key(
            repo_url="repo3",
            branch_base="main",
        )
    }
    assert len(workspace_idempotency_session.executed) == 1
    assert workspace_idempotency_session.executed[0][1] == {
        "lock_key": _workspace_idempotency_advisory_lock_key("key0")
    }
    assert len(operation_session.executed) == 1
    assert operation_session.executed[0][1] == {
        "lock_key": _operation_idempotency_advisory_lock_key("key0")
    }
    assert _owned_path_conflict_advisory_lock_key(repo_url="repo0", branch_base="main") > 0
    assert _owned_path_conflict_advisory_lock_key(repo_url="repo3", branch_base="main") < 0
    assert _workspace_idempotency_advisory_lock_key("key0") > 0
    assert _workspace_idempotency_advisory_lock_key("key1") < 0
    assert _operation_idempotency_advisory_lock_key("key2") > 0
    assert _operation_idempotency_advisory_lock_key("key0") < 0


@pytest.mark.unit
async def test_queue_decisions_can_be_listed_by_attempt(
    session: AsyncSession,
) -> None:
    _task_row, attempt, workspace = await _attempt(
        session,
        external_id="QUEUE-BY-ATTEMPT",
        title="queue decisions by attempt",
    )
    repo = QueueDecisionRepository(session)
    first = await repo.create(
        workspace_id=workspace.id,
        task_id=attempt.task_id,
        attempt_id=attempt.id,
        decision="admitted",
        reason_code="FIRST",
        class_priority=1,
        computed_priority=2,
        age_boost=0,
        retry_bonus=0,
        resource_summary={"cpu": 1},
        overlap_risk_summary={"overlap_count": 0},
        decided_at=datetime(2026, 4, 27, 10, 0, tzinfo=UTC),
    )
    second = await repo.create(
        workspace_id=workspace.id,
        task_id=attempt.task_id,
        attempt_id=attempt.id,
        decision="deferred",
        reason_code="SECOND",
        class_priority=1,
        computed_priority=1,
        age_boost=0,
        retry_bonus=0,
        resource_summary={"cpu": 1},
        overlap_risk_summary={"overlap_count": 0},
        decided_at=datetime(2026, 4, 27, 10, 1, tzinfo=UTC),
    )

    rows = await repo.list_for_attempt(attempt.id, limit=10)

    assert [row.id for row in rows] == [second.id, first.id]
    assert [row.reason_code for row in rows] == ["SECOND", "FIRST"]


@pytest.mark.unit
async def test_merge_candidate_lifecycle_requires_pr_and_preserves_known_shas(
    session: AsyncSession,
) -> None:
    repo = MergeCandidateRepository(session)
    task, attempt, workspace = await _attempt(
        session,
        external_id="CANDIDATE-REQUIRES-PR",
        title="candidate without PR",
        status=WorkspaceStatus.monitoring_pr,
    )

    with pytest.raises(ValueError, match="workspace PR URL"):
        await repo.create_or_update_open_for_attempt(
            task=task,
            attempt=attempt,
            workspace=workspace,
        )

    workspace.pr_url = "https://github.com/example/repository-coverage/pull/80"
    workspace.pr_number = 80
    created = await repo.create_or_update_open_for_attempt(
        task=task,
        attempt=attempt,
        workspace=workspace,
        head_sha="1" * 40,
        base_sha="2" * 40,
    )
    updated = await repo.create_or_update_open_for_attempt(
        task=task,
        attempt=attempt,
        workspace=workspace,
        head_sha=None,
        base_sha=None,
    )
    closed = await repo.close_open_for_attempt(attempt.id, close_reason="TEST_CLOSE")
    replayed_close = await repo.close_open_for_attempt(attempt.id, close_reason="IGNORED")
    missing_close = await repo.close_open_for_attempt("attempt_missing", close_reason="MISSING")

    assert updated.id == created.id
    assert updated.head_sha == "1" * 40
    assert updated.base_sha == "2" * 40
    assert closed is not None
    assert closed.status == "closed"
    assert closed.close_reason == "TEST_CLOSE"
    assert replayed_close is not None
    assert replayed_close.close_reason == "TEST_CLOSE"
    assert missing_close is None


@pytest.mark.unit
async def test_merge_candidate_queue_filters_teardown_and_cursor(
    session: AsyncSession,
) -> None:
    repo = MergeCandidateRepository(session)
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    _old_task, _old_attempt, old_workspace = await _candidate(
        session,
        title="old queue row",
        external_id="CANDIDATE-QUEUE-OLD",
        pr_number=81,
    )
    _middle_task, _middle_attempt, middle_workspace = await _candidate(
        session,
        title="middle queue row",
        external_id="CANDIDATE-QUEUE-MIDDLE",
        pr_number=82,
    )
    _new_task, _new_attempt, new_workspace = await _candidate(
        session,
        title="new queue row",
        external_id="CANDIDATE-QUEUE-NEW",
        pr_number=83,
    )
    _destroyed_task, _destroyed_attempt, destroyed_workspace = await _candidate(
        session,
        title="destroyed queue row",
        external_id="CANDIDATE-QUEUE-DESTROYED",
        pr_number=84,
    )
    old_workspace.updated_at = base
    middle_workspace.updated_at = base + timedelta(seconds=10)
    new_workspace.updated_at = base + timedelta(seconds=20)
    destroyed_workspace.updated_at = base + timedelta(seconds=30)
    destroyed_workspace.status = WorkspaceStatus.destroyed.value
    await session.flush()

    first_page = await repo.list_queue(limit=10)
    after_middle = await repo.list_queue(
        before_updated_at=middle_workspace.updated_at,
        before_workspace_id=middle_workspace.id,
        limit=10,
    )
    development_only = await repo.list_queue(
        repo_url="git@github.com:example/repository-coverage.git",
        base_branch="development",
        status=WorkspaceStatus.monitoring_pr,
        limit=10,
    )

    assert [candidate.workspace_id for candidate in first_page] == [
        new_workspace.id,
        middle_workspace.id,
        old_workspace.id,
    ]
    assert [candidate.workspace_id for candidate in after_middle] == [old_workspace.id]
    assert [candidate.workspace_id for candidate in development_only] == [
        new_workspace.id,
        middle_workspace.id,
        old_workspace.id,
    ]


@pytest.mark.unit
async def test_mark_workspace_merged_selects_newest_open_candidate(
    session: AsyncSession,
) -> None:
    repo = MergeCandidateRepository(session)
    base = datetime(2026, 4, 27, 13, 0, tzinfo=UTC)
    task, first_attempt, workspace = await _candidate(
        session,
        title="merged workspace",
        external_id="CANDIDATE-MERGED",
        pr_number=85,
    )
    first = await repo.get_by_attempt_id(first_attempt.id)
    assert first is not None
    second_workspace = await _workspace(
        session,
        title="same workspace second attempt holder",
        status=WorkspaceStatus.monitoring_pr,
    )
    second_workspace.branch_name = f"awf/{second_workspace.id}"
    second_workspace.remote_push_branch = second_workspace.branch_name
    second_workspace.pr_url = "https://github.com/example/repository-coverage/pull/851"
    second_workspace.pr_number = 851
    second_attempt = await TaskAttemptRepository(session).create_for_workspace(
        task=task,
        workspace=second_workspace,
        parent_attempt_id=first_attempt.id,
    )
    second_attempt.status = WorkspaceStatus.monitoring_pr.value
    second = await repo.create_or_update_open_for_attempt(
        task=task,
        attempt=second_attempt,
        workspace=second_workspace,
        head_sha="3" * 40,
        base_sha="4" * 40,
    )
    second.workspace_id = workspace.id
    first.updated_at = base
    second.updated_at = base + timedelta(seconds=1)
    await session.flush()

    merged = await repo.mark_workspace_merged(workspace.id)
    missing = await repo.mark_workspace_merged("ws_missing")

    assert merged is not None
    assert merged.id == second.id
    assert merged.status == "merged"
    assert merged.merged_at is not None
    assert first.status == "open"
    assert missing is None


@pytest.mark.unit
async def test_validation_run_finish_updates_metadata_and_handles_missing(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, title="validation metadata")
    repo = ValidationRunRepository(session)
    run = await repo.start(
        workspace_id=workspace.id,
        attempt_id=None,
        tier=2,
        commands=[{"command": "pytest"}, {"command": "ruff"}],
        base_commit="a" * 40,
        target_branch="development",
        target_head_sha="b" * 40,
        log_stream_refs={"stdout": "validation.stdout"},
    )

    missing_finish = await repo.finish(
        "vr_missing",
        status="failed",
        reason_code="MISSING",
    )
    missing_update = await repo.update_target_head_sha(
        "vr_missing",
        target_head_sha="c" * 40,
    )
    finished = await repo.finish(
        run.id,
        status="succeeded",
        reason_code=None,
        retry_count=2,
        coverage={"total": 99.1},
        command_retries=[1, 0, 9],
    )
    updated = await repo.update_target_head_sha(
        run.id,
        target_head_sha="d" * 40,
        workspace_head_sha="e" * 40,
    )
    plain_run = await repo.start(
        workspace_id=workspace.id,
        attempt_id=None,
        tier=1,
        commands=[{"command": "mypy"}],
        base_commit=None,
        target_branch=None,
        target_head_sha=None,
        log_stream_refs={},
        started_at=datetime(2026, 4, 27, 9, 0, tzinfo=UTC),
    )
    plain_finished = await repo.finish(
        plain_run.id,
        status="failed",
        reason_code="COMMAND_FAILED",
        finished_at=datetime(2026, 4, 27, 9, 5, tzinfo=UTC),
    )
    listed = await repo.list_for_workspace(workspace.id)
    latest_empty = await repo.latest_by_workspace_ids([])

    assert missing_finish is None
    assert missing_update is None
    assert finished is not None
    assert finished.status == "succeeded"
    assert finished.retry_count == 2
    assert finished.coverage == {"total": 99.1}
    assert finished.log_stream_refs == {
        "stdout": "validation.stdout",
        "coverage": {"total": 99.1},
    }
    assert finished.commands == [
        {"command": "pytest", "retry_count": 1},
        {"command": "ruff", "retry_count": 0},
    ]
    assert updated is not None
    assert updated.target_head_sha == "d" * 40
    assert updated.workspace_head_sha == "e" * 40
    assert plain_finished is not None
    assert plain_finished.coverage is None
    assert plain_finished.log_stream_refs == {}
    assert plain_finished.commands == [{"command": "mypy"}]
    assert [row.id for row in listed] == [plain_run.id, run.id]
    assert latest_empty == {}


@pytest.mark.unit
async def test_validation_run_start_persists_identity_provenance_and_allows_legacy_rows(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, title="validation identity")
    repo = ValidationRunRepository(session)

    run = await repo.start(
        workspace_id=workspace.id,
        attempt_id=None,
        tier=2,
        commands=[{"phase": "validate", "command": "pytest -q"}],
        base_commit="legacy-base",
        base_sha="base-sha",
        workspace_head_sha="workspace-head",
        target_branch="development",
        target_head_sha="target-head",
        profile_name="python",
        profile_version=7,
        profile_source="repo:.awf/workspace.yml",
        resolved_profile_digest="1" * 64,
        environment_identity_digest="2" * 64,
        environment_identity_inputs={
            "schema_version": 1,
            "runtime": {"toolchain_image": "python:3.12"},
        },
        log_stream_refs={"commands": []},
    )
    legacy = await repo.start(
        workspace_id=workspace.id,
        attempt_id=None,
        tier=1,
        commands=[{"phase": "validate", "command": "ruff check"}],
        base_commit="legacy-only-base",
        target_branch=None,
        target_head_sha=None,
        log_stream_refs={},
    )

    await session.refresh(run)
    await session.refresh(legacy)

    assert run.base_sha == "base-sha"
    assert run.workspace_head_sha == "workspace-head"
    assert run.profile_name == "python"
    assert run.profile_version == 7
    assert run.profile_source == "repo:.awf/workspace.yml"
    assert run.resolved_profile_digest == "1" * 64
    assert run.environment_identity_digest == "2" * 64
    assert run.environment_identity_inputs == {
        "schema_version": 1,
        "runtime": {"toolchain_image": "python:3.12"},
    }
    assert legacy.base_sha is None
    assert legacy.workspace_head_sha is None
    assert legacy.profile_name is None
    assert legacy.profile_version is None
    assert legacy.profile_source is None
    assert legacy.resolved_profile_digest is None
    assert legacy.environment_identity_digest is None
    assert legacy.environment_identity_inputs is None


@pytest.mark.unit
async def test_stale_reason_replace_active_findings_is_idempotent_and_grouped(
    session: AsyncSession,
) -> None:
    _task_row, attempt, workspace = await _candidate(
        session,
        title="stale reason candidate",
        external_id="STALE-REASONS",
        pr_number=86,
    )
    candidate = await MergeCandidateRepository(session).get_by_attempt_id(attempt.id)
    assert candidate is not None
    repo = StaleReasonRepository(session)
    first_finding = StaleReasonCreate(
        reason_code="STALE_TARGET_ADVANCED",
        trigger_type="target_advanced",
        trigger_ref="abc123",
        explanation="Target branch advanced.",
    )
    kept_finding = StaleReasonCreate(
        reason_code="STALE_OVERLAP",
        trigger_type="path_overlap",
        trigger_ref="src/awf/db/repositories.py",
        explanation="Overlapping path changed.",
    )
    new_finding = StaleReasonCreate(
        reason_code="STALE_SCHEMA",
        trigger_type="schema_changed",
        trigger_ref="migrations/versions/1.py",
        explanation="Schema lineage changed.",
    )

    added, resolved = await repo.replace_active_findings(
        workspace_id=workspace.id,
        candidate_id=candidate.id,
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        findings=[first_finding, kept_finding],
    )
    repeated_added, repeated_resolved = await repo.replace_active_findings(
        workspace_id=workspace.id,
        candidate_id=candidate.id,
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        findings=[first_finding, kept_finding],
    )
    kept_id = next(row.id for row in added if row.reason_code == "STALE_OVERLAP")
    second_added, second_resolved = await repo.replace_active_findings(
        workspace_id=workspace.id,
        candidate_id=candidate.id,
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        findings=[kept_finding, new_finding],
    )
    active_by_candidate = await repo.list_active_for_candidates(
        [candidate.id, "mc_missing", candidate.id]
    )
    all_for_workspace = await repo.list_for_workspace(workspace.id)
    active_for_workspace = await repo.list_active_for_workspace(workspace.id)

    assert [row.reason_code for row in added] == [
        "STALE_TARGET_ADVANCED",
        "STALE_OVERLAP",
    ]
    assert resolved == []
    assert repeated_added == []
    assert repeated_resolved == []
    assert [row.reason_code for row in second_added] == ["STALE_SCHEMA"]
    assert [row.reason_code for row in second_resolved] == ["STALE_TARGET_ADVANCED"]
    assert second_resolved[0].status == "resolved"
    assert second_resolved[0].resolved_at is not None
    assert {row.id for row in active_by_candidate[candidate.id]} == {
        kept_id,
        second_added[0].id,
    }
    assert active_by_candidate["mc_missing"] == []
    assert await repo.list_active_for_candidates([]) == {}
    assert [row.status for row in all_for_workspace].count("resolved") == 1
    assert {row.reason_code for row in active_for_workspace} == {
        "STALE_OVERLAP",
        "STALE_SCHEMA",
    }


@pytest.mark.unit
async def test_policy_finding_replace_active_findings_handles_workspace_and_candidates(
    session: AsyncSession,
) -> None:
    _task_row, attempt, workspace = await _candidate(
        session,
        title="policy finding candidate",
        external_id="POLICY-FINDINGS",
        pr_number=87,
    )
    candidate = await MergeCandidateRepository(session).get_by_attempt_id(attempt.id)
    assert candidate is not None
    repo = PolicyFindingRepository(session)
    original = PolicyFindingCreate(
        reason_code="POLICY_SCOPE_VIOLATION",
        severity="blocking",
        subject_path="src/awf/service/controls.py",
        explanation="Path is outside the allowed scope.",
        details={"paths": ["src/awf/service/controls.py"], "policy": "docs_task"},
    )
    same_details_different_order = PolicyFindingCreate(
        reason_code="POLICY_SCOPE_VIOLATION",
        severity="blocking",
        subject_path="src/awf/service/controls.py",
        explanation="Path is outside the allowed scope.",
        details={"policy": "docs_task", "paths": ["src/awf/service/controls.py"]},
    )
    replacement = PolicyFindingCreate(
        reason_code="POLICY_SCOPE_VIOLATION",
        severity="warning",
        subject_path="docs/index.md",
        explanation="Docs file needs review.",
        details={"policy": "docs_task"},
    )

    workspace_added, workspace_resolved = await repo.replace_active_findings(
        workspace_id=workspace.id,
        candidate_id=None,
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        reason_code="POLICY_SCOPE_VIOLATION",
        findings=[original],
    )
    repeated_added, repeated_resolved = await repo.replace_active_findings(
        workspace_id=workspace.id,
        candidate_id=None,
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        reason_code="POLICY_SCOPE_VIOLATION",
        findings=[same_details_different_order],
    )
    candidate_added, candidate_resolved = await repo.replace_active_findings(
        workspace_id=workspace.id,
        candidate_id=candidate.id,
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        reason_code="POLICY_SCOPE_VIOLATION",
        findings=[original],
    )
    new_candidate_added, new_candidate_resolved = await repo.replace_active_findings(
        workspace_id=workspace.id,
        candidate_id=candidate.id,
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        reason_code="POLICY_SCOPE_VIOLATION",
        findings=[replacement],
    )
    active_by_candidate = await repo.list_active_for_candidates([candidate.id, "mc_missing"])
    active_for_candidate = await repo.list_active_for_candidate(candidate.id)
    all_for_candidate = await repo._list_for_candidate(candidate.id, status=None)
    subject_history = await repo._list_for_subject(
        workspace_id=workspace.id,
        candidate_id=candidate.id,
        reason_code="POLICY_SCOPE_VIOLATION",
        status=None,
    )
    all_for_workspace = await repo.list_for_workspace(workspace.id)
    active_for_workspace = await repo.list_active_for_workspace(workspace.id)

    assert [row.id for row in workspace_added] == [workspace_added[0].id]
    assert workspace_resolved == []
    assert repeated_added == []
    assert repeated_resolved == []
    assert [row.id for row in candidate_added] == [candidate_added[0].id]
    assert candidate_resolved == []
    assert [row.id for row in new_candidate_added] == [new_candidate_added[0].id]
    assert [row.id for row in new_candidate_resolved] == [candidate_added[0].id]
    assert new_candidate_resolved[0].status == "resolved"
    assert [row.id for row in active_by_candidate[candidate.id]] == [new_candidate_added[0].id]
    assert active_by_candidate["mc_missing"] == []
    assert await repo.list_active_for_candidates([]) == {}
    assert [row.id for row in active_for_candidate] == [new_candidate_added[0].id]
    assert {row.id for row in all_for_candidate} == {
        candidate_added[0].id,
        new_candidate_added[0].id,
    }
    assert {row.id for row in subject_history} == {
        candidate_added[0].id,
        new_candidate_added[0].id,
    }
    assert {row.id for row in active_for_workspace} == {
        workspace_added[0].id,
        new_candidate_added[0].id,
    }
    assert [row.status for row in all_for_workspace].count("resolved") == 1


@pytest.mark.unit
async def test_operation_repository_status_timestamps_idempotency_and_filters(
    session: AsyncSession,
) -> None:
    first_workspace = await _workspace(session, title="operation filters first")
    second_workspace = await _workspace(session, title="operation filters second")
    repo = OperationRepository(session)
    base = datetime(2026, 4, 27, 14, 0, tzinfo=UTC)
    pending = await repo.create(
        workspace_id=first_workspace.id,
        operation_type=OperationType.cancel,
        status=OperationStatus.pending,
        payload={"force": False},
        idempotency_key="operation-key",
    )
    running = await repo.create(
        workspace_id=first_workspace.id,
        operation_type=OperationType.destroy,
        status=OperationStatus.running,
        idempotency_key="operation-key",
    )
    other = await repo.create(
        workspace_id=second_workspace.id,
        operation_type="stop",
        status="failed",
    )
    pending_started_before_finish = pending.started_at
    running_started_before_finish = running.started_at
    pending.created_at = base
    running.created_at = base + timedelta(seconds=1)
    other.created_at = base + timedelta(seconds=2)
    await session.flush()

    idempotent = await repo.get_by_idempotency_key("operation-key")
    filtered = await repo.list_all(
        workspace_id=first_workspace.id,
        status=OperationStatus.running,
        operation_type=OperationType.destroy,
    )
    failed = await repo.list_for_workspace(
        second_workspace.id,
        status="failed",
        operation_type="stop",
    )
    finished_pending = await repo.finish(
        pending,
        status=OperationStatus.succeeded,
        result={"cancelled": True},
    )
    finished_running = await repo.finish(
        running,
        status=OperationStatus.failed,
        error_code="CLEANUP_FAILED",
        error_message="container removal failed",
    )

    assert pending_started_before_finish is None
    assert running_started_before_finish is not None
    assert idempotent is not None
    assert idempotent.id == pending.id
    assert [operation.id for operation in filtered] == [running.id]
    assert [operation.id for operation in failed] == [other.id]
    assert finished_pending.started_at == finished_pending.finished_at
    assert finished_pending.result == {"cancelled": True}
    assert finished_running.started_at is not None
    assert finished_running.finished_at is not None
    assert finished_running.error_code == "CLEANUP_FAILED"
    assert finished_running.error_message == "container removal failed"


@pytest.mark.unit
async def test_workspace_log_streams_are_idempotent_filterable_and_close_once(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, title="log stream metadata")
    repo = WorkspaceLogStreamRepository(session)

    validation = await repo.create_or_get(
        workspace_id=workspace.id,
        stream_id="validation.stdout",
        source="runtime",
        name="pytest",
        kind="stdout",
        path="/logs/validation.stdout.log",
    )
    same_validation = await repo.create_or_get(
        workspace_id=workspace.id,
        stream_id="validation.stdout",
        source="ignored",
        name="ignored",
        kind="stderr",
        path="/logs/ignored.log",
    )
    setup = await repo.create_or_get(
        workspace_id=workspace.id,
        stream_id="setup.env",
        source="setup",
        name="environment",
        kind="stdout",
        path="/logs/setup.env.log",
    )
    runtime = await repo.create_or_get(
        workspace_id=workspace.id,
        stream_id="agent.stdout",
        source="agent",
        name="agent",
        kind="stdout",
        path="/logs/agent.stdout.log",
    )

    missing_append = await repo.append_metadata(
        workspace_id=workspace.id,
        stream_id="missing",
        byte_delta=1,
        line_delta=1,
    )
    updated = await repo.append_metadata(
        workspace_id=workspace.id,
        stream_id="validation.stdout",
        byte_delta=120,
        line_delta=4,
    )
    missing_close = await repo.close(workspace_id=workspace.id, stream_id="missing")
    closed = await repo.close(workspace_id=workspace.id, stream_id="validation.stdout")
    first_closed_at = closed.closed_at if closed is not None else None
    closed_again = await repo.close(workspace_id=workspace.id, stream_id="validation.stdout")
    all_streams = await repo.list_for_workspace(workspace.id)
    validation_streams = await repo.list_validation_for_workspace(workspace.id)

    assert same_validation.id == validation.id
    assert same_validation.source == "runtime"
    assert missing_append is None
    assert updated is not None
    assert updated.byte_count == 120
    assert updated.line_count == 4
    assert missing_close is None
    assert closed is not None
    assert closed_again is not None

    assert closed_again.closed_at == first_closed_at
    await session.refresh(workspace)
    assert workspace.last_log_at is not None
    assert workspace.last_activity_at is not None
    assert {stream.id for stream in all_streams} == {validation.id, setup.id, runtime.id}
    assert {stream.id for stream in validation_streams} == {validation.id, setup.id}


@pytest.mark.unit
async def test_append_after_close_reopens_stream(session: AsyncSession) -> None:
    workspace = await _workspace(session, title="reopen stream")
    repo = WorkspaceLogStreamRepository(session)

    await repo.create_or_get(
        workspace_id=workspace.id,
        stream_id="agent.stdout",
        source="agent",
        name="agent",
        kind="stdout",
        path="/logs/agent.stdout.log",
    )

    await repo.close(workspace_id=workspace.id, stream_id="agent.stdout")
    closed_stream = await repo.get(workspace_id=workspace.id, stream_id="agent.stdout")
    assert closed_stream is not None
    assert closed_stream.closed_at is not None

    await repo.append_metadata(
        workspace_id=workspace.id,
        stream_id="agent.stdout",
        byte_delta=10,
        line_delta=1,
    )
    reopened_stream = await repo.get(workspace_id=workspace.id, stream_id="agent.stdout")
    assert reopened_stream is not None
    assert reopened_stream.closed_at is None
    assert reopened_stream.byte_count == 10
    assert reopened_stream.line_count == 1


@pytest.mark.unit
async def test_workspace_transition_if_current_releases_resources_and_claims_are_owned(
    session: AsyncSession,
) -> None:
    workspace_repo = WorkspaceRepository(session)
    task, attempt, workspace = await _attempt(
        session,
        external_id="WORKSPACE-CLAIMS",
        title="workspace claim ownership",
        status=WorkspaceStatus.pushing,
    )
    assert task.id == attempt.task_id
    reservation = await ResourceReservationRepository(session).create(
        workspace_id=workspace.id,
        attempt_id=attempt.id,
        node_id="local",
        steady_cpu=1.0,
        steady_memory_gb=2.0,
        peak_cpu=2.0,
        peak_memory_gb=4.0,
        disk_mb=1024,
        phase="workspace_lifecycle",
    )
    miss = await workspace_repo.transition_if_current(
        workspace.id,
        from_status=WorkspaceStatus.ready,
        to=WorkspaceStatus.running,
        reason_code="STALE_CLAIM",
    )
    monitoring = await workspace_repo.transition_if_current(
        workspace.id,
        from_status=WorkspaceStatus.pushing,
        to=WorkspaceStatus.monitoring_pr,
        reason_code="PR_OPENED",
    )
    completed = await workspace_repo.transition_if_current(
        workspace.id,
        from_status=WorkspaceStatus.monitoring_pr,
        to=WorkspaceStatus.completed,
        reason_code="MERGED",
    )

    assert miss is None
    assert monitoring is not None
    assert monitoring.monitor_started_at is not None
    assert completed is not None
    assert attempt.status == WorkspaceStatus.completed.value
    assert reservation.released_at is not None

    claim_workspace = await _workspace(
        session,
        title="monitor claim ownership",
        status=WorkspaceStatus.monitoring_pr,
    )
    now = datetime(2026, 4, 27, 15, 0, tzinfo=UTC)
    owner_expiry = now + timedelta(minutes=5)
    other_expiry = now + timedelta(minutes=10)

    assert await workspace_repo.claim_monitoring_pr(
        claim_workspace.id,
        owner_id="owner-1",
        lease_expires_at=owner_expiry,
        now=now,
    )
    assert not await workspace_repo.claim_monitoring_pr(
        claim_workspace.id,
        owner_id="owner-2",
        lease_expires_at=other_expiry,
        now=now + timedelta(minutes=1),
    )
    assert not await workspace_repo.refresh_monitoring_pr_claim(
        claim_workspace.id,
        owner_id="owner-2",
        lease_expires_at=other_expiry,
    )
    assert await workspace_repo.refresh_monitoring_pr_claim(
        claim_workspace.id,
        owner_id="owner-1",
        lease_expires_at=other_expiry,
    )
    claim_workspace.execution_claimed_by = "runner-1"
    claim_workspace.execution_claim_expires_at = owner_expiry
    await session.flush()
    assert not await workspace_repo.refresh_execution_claim(
        claim_workspace.id,
        owner_id="runner-2",
        lease_expires_at=other_expiry,
    )
    assert await workspace_repo.refresh_execution_claim(
        claim_workspace.id,
        owner_id="runner-1",
        lease_expires_at=other_expiry,
    )
    assert not await workspace_repo.release_execution_claim(
        claim_workspace.id,
        owner_id="runner-2",
    )
    assert await workspace_repo.release_execution_claim(
        claim_workspace.id,
        owner_id="runner-1",
    )
    assert not await workspace_repo.release_monitoring_pr_claim(
        claim_workspace.id,
        owner_id="owner-2",
    )
    assert await workspace_repo.release_monitoring_pr_claim(
        claim_workspace.id,
        owner_id="owner-1",
    )


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
async def test_claim_monitoring_pr_preserves_epoch_for_unexpired_execution_claim(
    session: AsyncSession,
) -> None:
    workspace_repo = WorkspaceRepository(session)
    now = datetime(2026, 5, 2, 9, 0, tzinfo=UTC)
    workspace = await _workspace(
        session,
        title="monitor claim preserves unexpired execution epoch",
        status=WorkspaceStatus.monitoring_pr,
    )
    workspace.execution_claimed_by = "live-runner"
    workspace.execution_claim_expires_at = now + timedelta(minutes=5)
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
    # An unexpired execution claim is preserved, and its epoch is left untouched.
    assert workspace.execution_claimed_by == "live-runner"
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


@pytest.mark.unit
async def test_workspace_queue_listing_and_owned_path_edges(
    session: AsyncSession,
) -> None:
    repo = WorkspaceRepository(session)
    base = datetime(2026, 4, 27, 17, 0, tzinfo=UTC)
    with_attempt = await _workspace(
        session,
        title="queue with attempt",
        status=WorkspaceStatus.monitoring_pr,
        agent=AgentRuntime.codex,
        owned_paths=["src/owned/**"],
    )
    task = await _task(session, external_id="WORKSPACE-QUEUE-WITH-ATTEMPT")
    await TaskAttemptRepository(session).create_for_workspace(
        task=task,
        workspace=with_attempt,
    )
    without_attempt = await _workspace(
        session,
        title="queue without attempt",
        status=WorkspaceStatus.monitoring_pr,
        agent=AgentRuntime.gemini,
        owned_paths=["."],
    )
    older_without_attempt = await _workspace(
        session,
        title="queue older without attempt",
        status=WorkspaceStatus.ready,
        agent=AgentRuntime.gemini,
        owned_paths=["src/awf/**"],
    )
    destroyed = await _workspace(
        session,
        title="queue destroyed",
        status=WorkspaceStatus.destroyed,
        agent=AgentRuntime.gemini,
    )
    for index, workspace in enumerate(
        [older_without_attempt, with_attempt, without_attempt, destroyed],
    ):
        workspace.pr_url = f"https://github.com/example/repository-coverage/pull/{90 + index}"
        workspace.pr_number = 90 + index
        workspace.updated_at = base + timedelta(minutes=index)
    await session.flush()

    without_attempts = await repo.list_without_task_attempts(
        status=WorkspaceStatus.monitoring_pr,
        agent=AgentRuntime.gemini,
        repo_url="git@github.com:example/repository-coverage.git",
        limit=10,
    )
    merge_queue = await repo.list_merge_queue(
        repo_url="git@github.com:example/repository-coverage.git",
        base_branch="development",
        status=WorkspaceStatus.monitoring_pr,
        before_updated_at=without_attempt.updated_at,
        before_workspace_id=without_attempt.id,
        limit=10,
    )
    merge_queue_without_candidates = await repo.list_merge_queue_without_candidates(
        repo_url="git@github.com:example/repository-coverage.git",
        base_branch="development",
        status=WorkspaceStatus.monitoring_pr,
        before_updated_at=without_attempt.updated_at,
        before_workspace_id=without_attempt.id,
        limit=10,
    )
    unfiltered_without_attempts = await repo.list_without_task_attempts(limit=10)
    unfiltered_merge_queue = await repo.list_merge_queue(limit=10)
    unfiltered_without_candidates = await repo.list_merge_queue_without_candidates(limit=10)
    conflicts = await repo.find_active_owned_path_conflicts(
        repo_url="git@github.com:example/repository-coverage.git",
        branch_base="development",
        owned_paths=["src/awf/service/controls.py"],
    )
    overlaps = await repo.find_active_owned_path_overlaps(
        repo_url="git@github.com:example/repository-coverage.git",
        branch_base="development",
        owned_paths=["src/new.py"],
    )
    zero_limit = await repo.list_schedulable_ids(status=WorkspaceStatus.ready, limit=0)

    assert [workspace.id for workspace in without_attempts] == [without_attempt.id]
    assert [workspace.id for workspace in merge_queue] == [with_attempt.id]
    assert [workspace.id for workspace in merge_queue_without_candidates] == [with_attempt.id]
    assert {workspace.id for workspace in unfiltered_without_attempts} >= {
        without_attempt.id,
        older_without_attempt.id,
        destroyed.id,
    }
    assert {workspace.id for workspace in unfiltered_merge_queue} >= {
        without_attempt.id,
        with_attempt.id,
        older_without_attempt.id,
    }
    assert {workspace.id for workspace in unfiltered_without_candidates} >= {
        without_attempt.id,
        with_attempt.id,
        older_without_attempt.id,
    }
    assert conflicts == [
        type(conflicts[0])(
            workspace_id=older_without_attempt.id,
            existing_path="src/awf/**",
            requested_path="src/awf/service/controls.py",
        )
    ]
    assert overlaps == []
    assert zero_limit == []

    transition_without_attempt = await repo.transition_if_current(
        older_without_attempt.id,
        from_status=WorkspaceStatus.ready,
        to=WorkspaceStatus.running,
        reason_code="CLAIMED_WITHOUT_ATTEMPT",
    )
    assert transition_without_attempt is not None
    assert transition_without_attempt.status == WorkspaceStatus.running.value


@pytest.mark.unit
async def test_candidate_readiness_docs_scope_and_terminal_helpers(
    session: AsyncSession,
) -> None:
    task, attempt, workspace = await _candidate(
        session,
        title="candidate readiness helpers",
        external_id="CANDIDATE-READINESS",
        pr_number=88,
    )
    candidate = await MergeCandidateRepository(session).get_by_attempt_id(attempt.id)
    assert candidate is not None
    workspace.task_class = TaskClass.docs_task.value
    workspace.owned_paths = ["README.md", "docs/index.md", "guide.mdx", "src/app.py"]
    candidate.stale = False
    candidate.stale_reason = None

    sync_candidate_readiness(
        candidate,
        workspace=workspace,
        attempt=attempt,
        recompute_stale=True,
    )
    assert candidate.stale is True
    assert candidate.stale_reason == DOCS_TASK_SCOPE_VIOLATION_STALE_REASON

    candidate.stale = True
    candidate.stale_reason = VALIDATION_INSUFFICIENT_TIER_STALE_REASON
    sync_candidate_readiness(
        candidate,
        workspace=workspace,
        attempt=attempt,
        recompute_stale=False,
    )
    assert candidate.stale_reason == VALIDATION_INSUFFICIENT_TIER_STALE_REASON

    assert task.id == attempt.task_id
    assert _candidate_terminal_close_reason(WorkspaceStatus.destroyed) == "WORKSPACE_DESTROYED"
    assert _claims_non_docs_path(["", "docs/index.md", "CHANGELOG.md", "notes.rst"]) is False
    assert _claims_non_docs_path(["docs/index.md", "src/app.py"]) is True
