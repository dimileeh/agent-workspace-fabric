"""Merge queue ordering policy tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.service.merge_queue as merge_queue
from awf.db.enums import (
    AgentRuntime,
    OperationStatus,
    OperationType,
    WorkspaceStatus,
)
from awf.db.models import MergeCandidate, Operation, TaskAttempt, Workspace
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service.merge_queue import (
    MergeQueueBlocker,
    list_merge_queue_blockers_for_candidate,
    list_merge_queue_blockers_for_workspace,
)
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
def test_blocker_event_payload_uses_blocker_field_names() -> None:
    blocker = MergeQueueBlocker(
        candidate_id="candidate-older",
        workspace_id="workspace-older",
        attempt_id="attempt-older",
        task_id="task-older",
        title="Older candidate",
        pr_url="https://github.com/example/service/pull/11",
        pr_number=11,
        status=WorkspaceStatus.monitoring_pr.value,
        blocker_state="merge_eligible",
    )

    payload = blocker.event_payload(
        repo_url="git@github.com:example/service.git",
        base_branch="development",
    )

    assert payload == {
        "reason_code": "MERGE_QUEUE_WAITING_FOR_OLDER_CANDIDATE",
        "repo_url": "git@github.com:example/service.git",
        "base_branch": "development",
        "blocker_candidate_id": "candidate-older",
        "blocker_workspace_id": "workspace-older",
        "blocker_pr_url": "https://github.com/example/service/pull/11",
        "blocker_pr_number": 11,
        "blocker_title": "Older candidate",
        "blocker_status": WorkspaceStatus.monitoring_pr.value,
        "blocker_state": "merge_eligible",
    }
    assert not any(key.startswith("blocked_") for key in payload)


async def _seed_candidate(
    session: AsyncSession,
    *,
    title: str,
    pr_number: int,
    created_at: datetime,
    status: WorkspaceStatus = WorkspaceStatus.monitoring_pr,
    repo_url: str = "git@github.com:example/service.git",
    base_branch: str = "development",
    canonical: bool = True,
    owned_paths: list[str] | None = None,
    task_kind: str = "feature_branch_pr",
) -> tuple[str, str, str]:
    declared_owned_paths = ["src/shared/**"] if owned_paths is None else owned_paths
    workspace_repo = WorkspaceRepository(session)
    workspace = await workspace_repo.create(
        repo_url=repo_url,
        branch_base=base_branch,
        task_title=title,
        task_prompt=f"Implement {title}.",
        task_external_id=f"QUEUE-{pr_number}",
        task_kind=task_kind,
        owned_paths=declared_owned_paths,
        auto_merge=True,
        agent=AgentRuntime.codex.value,
        test_commands=[],
    )
    workspace.status = status.value
    workspace.branch_name = f"awf/{workspace.id}"
    workspace.remote_push_branch = workspace.branch_name
    workspace.pr_url = f"https://github.com/example/service/pull/{pr_number}"
    workspace.pr_number = pr_number

    task = await TaskRepository(session).create_or_get(
        repo_url=repo_url,
        base_branch=base_branch,
        title=title,
        prompt=f"Implement {title}.",
        external_id=f"QUEUE-{pr_number}",
        idempotency_key=None,
        task_class=None,
        owned_paths=declared_owned_paths,
    )
    attempt = await TaskAttemptRepository(session).create_for_workspace(
        task=task,
        workspace=workspace,
    )
    attempt.is_canonical_for_merge = canonical
    candidate = await MergeCandidateRepository(session).create_or_update_open_for_attempt(
        task=task,
        attempt=attempt,
        workspace=workspace,
        head_sha=f"head-{pr_number}",
        base_sha="base",
    )
    candidate.created_at = created_at
    candidate.updated_at = created_at
    await session.flush()
    return workspace.id, attempt.id, candidate.id


@pytest.mark.unit
@pytest.mark.parametrize(
    ("older_status", "operation_payload", "expected_state"),
    [
        (WorkspaceStatus.monitoring_pr, None, "merge_eligible"),
        (
            WorkspaceStatus.ready,
            {"source": "pr_monitor", "reason": "validation_insufficient_tier"},
            "monitor_owned_recovery",
        ),
    ],
)
async def test_older_open_candidate_blocks_later_same_repo_base_candidate(
    factory: async_sessionmaker[AsyncSession],
    older_status: WorkspaceStatus,
    operation_payload: dict[str, str] | None,
    expected_state: str,
) -> None:
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    async with factory() as session:
        older_workspace_id, _older_attempt_id, older_candidate_id = await _seed_candidate(
            session,
            title="Older candidate",
            pr_number=11,
            created_at=now,
            status=older_status,
        )
        _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_candidate(
            session,
            title="Later candidate",
            pr_number=12,
            created_at=now + timedelta(minutes=5),
        )
        if operation_payload is not None:
            await OperationRepository(session).create(
                workspace_id=older_workspace_id,
                operation_type=OperationType.validate,
                status=OperationStatus.pending,
                payload=operation_payload,
            )
        await session.commit()

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert len(blockers) == 1
    assert blockers[0].candidate_id == older_candidate_id
    assert blockers[0].workspace_id == older_workspace_id
    assert blockers[0].blocker_state == expected_state
    assert blockers[0].reason_code == "MERGE_QUEUE_WAITING_FOR_OLDER_CANDIDATE"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("older_owned_paths", "later_owned_paths"),
    [
        ([], ["src/shared/**"]),
        (["src/shared/**"], []),
        ([], []),
    ],
)
async def test_missing_owned_paths_do_not_block_later_candidate(
    factory: async_sessionmaker[AsyncSession],
    older_owned_paths: list[str],
    later_owned_paths: list[str],
) -> None:
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    async with factory() as session:
        _older_workspace_id, _older_attempt_id, _older_candidate_id = await _seed_candidate(
            session,
            title="Older unscoped candidate",
            pr_number=17,
            created_at=now,
            owned_paths=older_owned_paths,
        )
        _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_candidate(
            session,
            title="Later candidate",
            pr_number=18,
            created_at=now + timedelta(minutes=5),
            owned_paths=later_owned_paths,
        )
        await session.commit()

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert blockers == []


@pytest.mark.unit
async def test_disjoint_owned_paths_do_not_block_later_candidate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    async with factory() as session:
        await _seed_candidate(
            session,
            title="Older docs candidate",
            pr_number=15,
            created_at=now,
            owned_paths=["docs/**"],
        )
        _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_candidate(
            session,
            title="Later API candidate",
            pr_number=16,
            created_at=now + timedelta(minutes=5),
            owned_paths=["src/awf/api/**"],
        )
        await session.commit()

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert blockers == []


@pytest.mark.unit
async def test_non_monitor_recovery_operation_does_not_block_later_candidate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    async with factory() as session:
        older_workspace_id, _older_attempt_id, _older_candidate_id = await _seed_candidate(
            session,
            title="Older manual recovery",
            pr_number=13,
            created_at=now,
            status=WorkspaceStatus.ready,
        )
        _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_candidate(
            session,
            title="Later candidate",
            pr_number=14,
            created_at=now + timedelta(minutes=5),
        )
        await OperationRepository(session).create(
            workspace_id=older_workspace_id,
            operation_type=OperationType.validate,
            status=OperationStatus.pending,
            payload={"source": "operator", "reason": "manual_validation"},
        )
        await session.commit()

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert blockers == []


@pytest.mark.unit
async def test_workspace_without_candidate_has_no_merge_queue_blockers(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/service.git",
            branch_base="development",
            task_title="No candidate",
            task_prompt="No candidate yet.",
            agent=AgentRuntime.codex.value,
            test_commands=[],
        )
        await session.commit()
        workspace_id = workspace.id

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_workspace(
            session,
            workspace_id=workspace_id,
        )

    assert blockers == []


@pytest.mark.unit
def test_merge_queue_response_helpers_cover_legacy_and_advisory_edges() -> None:
    now = datetime(2026, 4, 29, 16, 0, tzinfo=UTC)
    workspace = SimpleNamespace(
        events=[
            SimpleNamespace(
                event_type="workspace.state_changed",
                new_state=WorkspaceStatus.completed.value,
                occurred_at=now - timedelta(minutes=1),
            ),
            SimpleNamespace(
                event_type="workspace.state_changed",
                new_state=WorkspaceStatus.completed.value,
                occurred_at=now,
            ),
        ],
        status=WorkspaceStatus.completed.value,
        updated_at=now + timedelta(minutes=1),
    )
    assert merge_queue._legacy_workspace_merged_at(workspace) == now

    candidate = SimpleNamespace(
        completed=False,
        failed_or_cancelled=False,
        not_canonical=False,
        policy_blocked=False,
        stale=True,
        stale_reason="ADVISORY_PLAN_ARTIFACT_OVERLAP",
        manual_merge_required=False,
        waiting_for_monitor=False,
        ready=False,
    )
    assert merge_queue._merge_blocker_reason(
        candidate,
        stale_reasons=[],
        policy_findings=[],
        queue_blockers=[],
    ) == ("workspace_not_terminal", None)


@pytest.mark.unit
@pytest.mark.parametrize("clearing_state", ["merged", "closed", "non_canonical"])
async def test_blocker_clears_when_older_candidate_is_not_open_canonical(
    factory: async_sessionmaker[AsyncSession],
    clearing_state: str,
) -> None:
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    async with factory() as session:
        older_workspace_id, older_attempt_id, _older_candidate_id = await _seed_candidate(
            session,
            title="Older candidate",
            pr_number=21,
            created_at=now,
        )
        _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_candidate(
            session,
            title="Later candidate",
            pr_number=22,
            created_at=now + timedelta(minutes=5),
        )
        candidate_repo = MergeCandidateRepository(session)
        if clearing_state == "merged":
            await candidate_repo.mark_workspace_merged(older_workspace_id)
        elif clearing_state == "closed":
            await candidate_repo.close_open_for_workspace(
                older_workspace_id,
                close_reason="TEST_CLOSED",
            )
        else:
            attempt = await TaskAttemptRepository(session).get_by_workspace_id(
                older_workspace_id,
            )
            assert attempt is not None
            assert attempt.id == older_attempt_id
            attempt.is_canonical_for_merge = False
        await session.commit()

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert blockers == []


@pytest.mark.unit
async def test_candidates_on_other_repo_or_base_do_not_block(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    async with factory() as session:
        await _seed_candidate(
            session,
            title="Other repo",
            pr_number=31,
            created_at=now,
            repo_url="git@github.com:example/other.git",
        )
        await _seed_candidate(
            session,
            title="Other base",
            pr_number=32,
            created_at=now + timedelta(minutes=1),
            base_branch="main",
        )
        _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_candidate(
            session,
            title="Later candidate",
            pr_number=33,
            created_at=now + timedelta(minutes=5),
        )
        await session.commit()

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert blockers == []


@pytest.mark.unit
async def test_single_blocker_lookup_returns_empty_for_missing_candidate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id="missing",
        )

    assert blockers == []


@pytest.mark.unit
async def test_older_open_candidate_pool_handles_empty_ready_candidate_list() -> None:
    blockers = await merge_queue._load_older_open_candidate_pool(  # noqa: SLF001
        object(),  # type: ignore[arg-type]
        [],
    )

    assert blockers == []


def _candidate(
    *,
    candidate_id: str,
    created_at: datetime,
    status: str = "open",
    workspace_status: WorkspaceStatus | str = WorkspaceStatus.monitoring_pr,
    auto_merge: bool = True,
    canonical: bool = True,
    stale: bool = False,
    repo_url: str = "git@github.com:example/service.git",
    base_branch: str = "development",
    task_kind: str = "feature_branch_pr",
    operations: list[Operation] | None = None,
    owned_paths: list[str] | None = None,
) -> MergeCandidate:
    declared_owned_paths = ["src/shared/**"] if owned_paths is None else owned_paths
    workspace_status_value = (
        workspace_status.value
        if isinstance(workspace_status, WorkspaceStatus)
        else workspace_status
    )
    workspace = Workspace(
        id=f"ws_{candidate_id}",
        status=workspace_status_value,
        repo_url=repo_url,
        branch_base=base_branch,
        task_title=f"Candidate {candidate_id}",
        task_prompt="Merge me",
        agent=AgentRuntime.codex.value,
        auto_merge=auto_merge,
        task_kind=task_kind,
        owned_paths=declared_owned_paths,
    )
    workspace.operations = operations or []
    attempt = TaskAttempt(
        id=f"att_{candidate_id}",
        task_id=f"task_{candidate_id}",
        workspace_id=workspace.id,
        attempt_number=1,
        agent=AgentRuntime.codex.value,
        repo_url=repo_url,
        base_branch=base_branch,
        title=workspace.task_title,
        status=workspace.status,
        owned_paths=declared_owned_paths,
        is_canonical_for_merge=canonical,
        created_at=created_at,
        updated_at=created_at,
    )
    candidate = MergeCandidate(
        id=candidate_id,
        task_id=attempt.task_id,
        attempt_id=attempt.id,
        workspace_id=workspace.id,
        pr_url=f"https://github.com/example/service/pull/{candidate_id}",
        pr_number=1,
        repo_url=repo_url,
        base_branch=base_branch,
        status=status,
        stale=stale,
        created_at=created_at,
        updated_at=created_at,
    )
    candidate.workspace = workspace
    candidate.attempt = attempt
    return candidate


@pytest.mark.unit
async def test_batch_blocker_lookup_handles_empty_candidate_list() -> None:
    blockers = await merge_queue.list_merge_queue_blockers_for_candidates(
        object(),  # type: ignore[arg-type]
        candidate_ids=[],
    )

    assert blockers == {}


@pytest.mark.unit
async def test_batch_blocker_lookup_returns_empty_for_non_ready_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)

    async def fake_load_candidates(
        _session: object,
        candidate_ids: list[str],
    ) -> list[MergeCandidate]:
        assert candidate_ids == ["later"]
        return [
            _candidate(
                candidate_id="later",
                created_at=now,
                workspace_status=WorkspaceStatus.running,
            )
        ]

    monkeypatch.setattr(merge_queue, "_load_candidates", fake_load_candidates)

    blockers = await merge_queue.list_merge_queue_blockers_for_candidates(
        object(),  # type: ignore[arg-type]
        candidate_ids=["later", "later"],
    )

    assert blockers == {"later": []}


@pytest.mark.unit
async def test_batch_blocker_lookup_filters_same_candidate_newer_and_nonblocking_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    target = _candidate(candidate_id="later", created_at=now + timedelta(minutes=5))
    older_blocker = _candidate(candidate_id="older", created_at=now)
    same_candidate = _candidate(candidate_id="later", created_at=now - timedelta(minutes=1))
    newer_candidate = _candidate(candidate_id="newer", created_at=now + timedelta(minutes=10))
    disjoint_candidate = _candidate(
        candidate_id="disjoint",
        created_at=now - timedelta(minutes=3),
        owned_paths=["docs/**"],
    )
    nonblocking_candidate = _candidate(
        candidate_id="nonblocking",
        created_at=now - timedelta(minutes=2),
        workspace_status=WorkspaceStatus.completed,
    )

    async def fake_load_candidates(
        _session: object,
        _candidate_ids: list[str],
    ) -> list[MergeCandidate]:
        return [target]

    async def fake_blocker_pool(
        _session: object,
        _candidates: list[MergeCandidate],
    ) -> list[MergeCandidate]:
        return [
            same_candidate,
            newer_candidate,
            disjoint_candidate,
            nonblocking_candidate,
            older_blocker,
        ]

    monkeypatch.setattr(merge_queue, "_load_candidates", fake_load_candidates)
    monkeypatch.setattr(merge_queue, "_load_older_open_candidate_pool", fake_blocker_pool)

    blockers = await merge_queue.list_merge_queue_blockers_for_candidates(
        object(),  # type: ignore[arg-type]
        candidate_ids=["later"],
    )

    assert [blocker.candidate_id for blocker in blockers["later"]] == ["older"]
    assert blockers["later"][0].blocker_state == "merge_eligible"


@pytest.mark.unit
def test_merge_queue_private_policy_helpers_cover_policy_edges() -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    target = _candidate(candidate_id="later", created_at=now)

    assert merge_queue._is_older_candidate(
        _candidate(candidate_id="older", created_at=now - timedelta(seconds=1)),
        target,
    )
    assert not merge_queue._is_older_candidate(
        _candidate(candidate_id="newer", created_at=now + timedelta(seconds=1)),
        target,
    )
    assert (
        merge_queue._workspace_status(
            _candidate(candidate_id="bad", created_at=now, workspace_status="unknown").workspace
        )
        is None
    )
    assert not merge_queue._is_merge_ready_candidate(
        _candidate(candidate_id="closed", created_at=now, status="closed")
    )
    assert not merge_queue._is_merge_ready_candidate(
        _candidate(candidate_id="manual", created_at=now, auto_merge=False)
    )
    assert not merge_queue._is_merge_ready_candidate(
        _candidate(candidate_id="stale", created_at=now, stale=True)
    )
    assert (
        merge_queue._blocking_state(
            _candidate(
                candidate_id="completed",
                created_at=now,
                workspace_status=WorkspaceStatus.completed,
            )
        )
        is None
    )
    assert not merge_queue._candidate_blocks_target(  # noqa: SLF001
        _candidate(candidate_id="unowned", created_at=now, owned_paths=[]),
        target,
    )
    assert not merge_queue._candidate_blocks_target(  # noqa: SLF001
        target,
        _candidate(candidate_id="unowned", created_at=now, owned_paths=[]),
    )


@pytest.mark.unit
def test_merge_queue_candidate_dependency_falls_back_to_attempt_owned_paths() -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    blocker = _candidate(candidate_id="older", created_at=now, owned_paths=[])
    target = _candidate(candidate_id="later", created_at=now + timedelta(minutes=5), owned_paths=[])
    blocker.attempt.owned_paths = ["src/shared/**"]
    target.attempt.owned_paths = ["src/shared/file.py"]

    assert merge_queue._candidate_blocks_target(blocker, target)  # noqa: SLF001


@pytest.mark.unit
def test_merge_queue_candidate_dependency_falls_back_when_workspace_paths_filter_empty() -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    blocker = _candidate(
        candidate_id="older",
        created_at=now,
        owned_paths=["docs/awf-plans/**"],
    )
    target = _candidate(
        candidate_id="later",
        created_at=now + timedelta(minutes=5),
        owned_paths=["docs/awf-plans/**"],
    )
    blocker.attempt.owned_paths = ["src/shared/**"]
    target.attempt.owned_paths = ["src/shared/file.py"]

    assert merge_queue._candidate_blocks_target(blocker, target)  # noqa: SLF001


@pytest.mark.unit
def test_monitor_recovery_operation_policy_false_and_true_paths() -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)

    assert not merge_queue._is_monitor_recovery_operation(
        Operation(
            type="cancel",
            status=OperationStatus.pending.value,
            payload={"source": "pr_monitor"},
        )
    )
    assert not merge_queue._is_monitor_recovery_operation(
        Operation(type=OperationType.validate.value, status=OperationStatus.succeeded.value)
    )
    assert not merge_queue._is_monitor_recovery_operation(
        Operation(type=OperationType.validate.value, status=OperationStatus.pending.value)
    )
    assert not merge_queue._is_monitor_recovery_operation(
        Operation(
            type=OperationType.validate.value,
            status=OperationStatus.pending.value,
            payload={"source": "operator"},
        )
    )
    recovery_candidate = _candidate(
        candidate_id="recovery",
        created_at=now,
        workspace_status=WorkspaceStatus.validating,
        operations=[
            Operation(
                type=OperationType.rebase.value,
                status=OperationStatus.running.value,
                payload={"source": "pr_monitor"},
            )
        ],
    )
    assert merge_queue._is_monitor_owned_recovery(recovery_candidate)
    assert merge_queue._blocking_state(recovery_candidate) == "monitor_owned_recovery"


@pytest.mark.unit
def test_provider_recovery_state_response_includes_fallback_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = SimpleNamespace(
        action="fallback",
        reason_code="PROVIDER_FALLBACK_SELECTED",
        source_provider="anthropic",
        source_model="claude-sonnet-4.5",
        retry_attempt_number=0,
        fallback_attempt_number=1,
        cooldown_until=None,
        next_eligible_at=None,
        fallback_target=SimpleNamespace(
            agent=AgentRuntime.codex.value,
            provider="openai",
            model="gpt-5.3-codex",
        ),
        source_workspace_id="ws_source",
        source_attempt_id="att_source",
        recommended_action="Run fallback workspace.",
        terminal=False,
    )

    monkeypatch.setattr(
        merge_queue,
        "provider_recovery_state_for_workspace",
        lambda _workspace: view,
    )

    response = merge_queue._provider_recovery_state_response(object())

    assert response is not None
    assert response.action == "fallback"
    assert response.fallback_target is not None
    assert response.fallback_target.agent == AgentRuntime.codex.value
    assert response.fallback_target.provider == "openai"
    assert response.fallback_target.model == "gpt-5.3-codex"
