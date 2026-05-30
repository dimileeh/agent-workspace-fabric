"""PR monitor merge-queue ordering tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceRepository,
    sync_candidate_readiness,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import Merge, MonitorState
from awf.service.merge_queue import list_merge_queue_blockers_for_candidate
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import FakeAdapter, RecordedSleep, make_runner
from tests.unit.runtime.test_pr_monitor import _status

REPO_URL = "git@github.com:dimileeh/aira-web.git"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def cmd() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def sleep_fn() -> RecordedSleep:
    return RecordedSleep()


async def _seed_monitoring_candidate(
    factory: async_sessionmaker[AsyncSession],
    *,
    title: str,
    pr_number: int,
    created_at: datetime,
    status: WorkspaceStatus = WorkspaceStatus.monitoring_pr,
    owned_paths: list[str] | None = None,
) -> tuple[str, str, str]:
    """Seed a merge-ready candidate with optional owned paths."""
    resolved_owned_paths = ["src/shared/**"] if owned_paths is None else list(owned_paths)
    async with factory() as session:
        workspace_repo = WorkspaceRepository(session)
        workspace = await workspace_repo.create(
            repo_url=REPO_URL,
            branch_base="development",
            task_title=title,
            task_prompt=f"Implement {title}.",
            task_external_id=f"RUNTIME-QUEUE-{pr_number}",
            owned_paths=resolved_owned_paths,
            auto_merge=True,
            agent=AgentRuntime.claude_code.value,
            test_commands=["pytest -q"],
        )
        workspace.status = status.value
        workspace.branch_name = f"awf/{workspace.id}"
        workspace.remote_push_branch = workspace.branch_name
        workspace.base_commit = "a" * 40
        workspace.compose_project_name = f"awf_{workspace.id}"
        workspace.compose_file_path = "/tmp/compose.yml"
        workspace.pr_url = f"https://github.com/dimileeh/aira-web/pull/{pr_number}"
        workspace.pr_number = pr_number
        workspace.monitor_started_at = created_at

        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=workspace.task_external_id,
            idempotency_key=None,
            task_class=None,
            owned_paths=resolved_owned_paths,
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=workspace,
        )
        attempt.is_canonical_for_merge = True
        candidate = await MergeCandidateRepository(session).create_or_update_open_for_attempt(
            task=task,
            attempt=attempt,
            workspace=workspace,
            head_sha="abc123",
            base_sha="base",
        )
        validation_repo = ValidationRunRepository(session)
        validation_run = await validation_repo.start(
            workspace_id=workspace.id,
            attempt_id=attempt.id,
            tier=1,
            commands=[],
            base_commit="base",
            target_branch=workspace.remote_push_branch,
            workspace_head_sha="abc123",
            target_head_sha="abc123",
            log_stream_refs={},
            started_at=created_at + timedelta(seconds=1),
        )
        await validation_repo.finish(
            validation_run.id,
            status="succeeded",
            reason_code="VALIDATION_OK",
            finished_at=created_at + timedelta(seconds=2),
        )
        sync_candidate_readiness(candidate, workspace=workspace, attempt=attempt)
        candidate.created_at = created_at
        candidate.updated_at = created_at
        await session.commit()
        return workspace.id, attempt.id, candidate.id


@pytest.mark.unit
async def test_plan_artifact_only_overlap_does_not_block_later_candidate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Plan-artifact-only overlaps do not block merge queue progression."""
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    await _seed_monitoring_candidate(
        factory,
        title="Older plan artifact",
        pr_number=81,
        created_at=now,
        owned_paths=["src/feature-a/**", "docs/awf-plans/ws_*.md"],
    )
    _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Later plan artifact",
        pr_number=82,
        created_at=now + timedelta(minutes=5),
        owned_paths=["src/feature-b/**", "docs/awf-plans/ws_*.md"],
    )

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert blockers == []


@pytest.mark.unit
async def test_awf_plans_readme_overlap_blocks_later_candidate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The tracked awf-plans README still participates in merge ordering."""
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    older_workspace_id, _older_attempt_id, _older_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Older awf-plans docs",
        pr_number=181,
        created_at=now,
        owned_paths=["docs/awf-plans/**"],
    )
    _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Later awf-plans README",
        pr_number=182,
        created_at=now + timedelta(minutes=5),
        owned_paths=["docs/awf-plans/README.md"],
    )

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert [blocker.workspace_id for blocker in blockers] == [older_workspace_id]


@pytest.mark.unit
async def test_plan_artifact_overlap_does_not_hide_real_merge_queue_overlap(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Real source overlaps still block when plan artifacts also overlap."""
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    older_workspace_id, _older_attempt_id, _older_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Older real overlap",
        pr_number=83,
        created_at=now,
        owned_paths=["src/shared/**", "docs/awf-plans/ws_*.md"],
    )
    _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Later real overlap",
        pr_number=84,
        created_at=now + timedelta(minutes=5),
        owned_paths=["src/shared/module.py", "docs/awf-plans/ws_*.md"],
    )

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert [blocker.workspace_id for blocker in blockers] == [older_workspace_id]


@pytest.mark.unit
async def test_candidate_with_only_plan_artifact_path_does_not_block_merge_queue(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Candidates owning only internal plan artifacts block no merge target."""
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    await _seed_monitoring_candidate(
        factory,
        title="Older plan only",
        pr_number=85,
        created_at=now,
        owned_paths=["docs/awf-plans/ws_*.md"],
    )
    _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Later source work",
        pr_number=86,
        created_at=now + timedelta(minutes=5),
        owned_paths=["src/later/**", "docs/awf-plans/ws_*.md"],
    )

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert blockers == []


@pytest.mark.unit
async def test_candidate_with_explicit_empty_owned_paths_does_not_use_default(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Explicit empty owned paths do not fall back to the source default."""
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    await _seed_monitoring_candidate(
        factory,
        title="Older no owned paths",
        pr_number=87,
        created_at=now,
        owned_paths=[],
    )
    _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Later default source work",
        pr_number=88,
        created_at=now + timedelta(minutes=5),
    )

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert blockers == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("older_status", "recovery_operation"),
    [
        (WorkspaceStatus.monitoring_pr, False),
        (WorkspaceStatus.ready, True),
    ],
)
async def test_monitor_waits_for_older_candidate_without_notify_human(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
    older_status: WorkspaceStatus,
    recovery_operation: bool,
) -> None:
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    older_workspace_id, _older_attempt_id, older_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Older candidate",
        pr_number=101,
        created_at=now,
        status=older_status,
    )
    later_workspace_id, _later_attempt_id, _later_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Later candidate",
        pr_number=102,
        created_at=now + timedelta(minutes=5),
    )
    if recovery_operation:
        async with factory() as session:
            await OperationRepository(session).create(
                workspace_id=older_workspace_id,
                operation_type=OperationType.validate,
                status=OperationStatus.pending,
                payload={"source": "pr_monitor", "reason": "validation_insufficient_tier"},
            )
            await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=later_workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef.from_url(REPO_URL),
        pr_number=102,
        status=_status(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{later_workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(later_workspace_id)
        assert workspace is not None
        queue_wait_events = [
            event
            for event in workspace.events
            if event.reason_code == "MERGE_QUEUE_WAITING_FOR_OLDER_CANDIDATE"
        ]

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)
    assert len(queue_wait_events) == 1
    assert queue_wait_events[0].event_type == "workspace.merge_queue_waiting"
    assert queue_wait_events[0].payload == {
        "reason_code": "MERGE_QUEUE_WAITING_FOR_OLDER_CANDIDATE",
        "repo_url": REPO_URL,
        "base_branch": "development",
        "blocker_candidate_id": older_candidate_id,
        "blocker_workspace_id": older_workspace_id,
        "blocker_pr_url": "https://github.com/dimileeh/aira-web/pull/101",
        "blocker_pr_number": 101,
        "blocker_title": "Older candidate",
        "blocker_status": older_status.value,
        "blocker_state": "monitor_owned_recovery" if recovery_operation else "merge_eligible",
    }


@pytest.mark.unit
async def test_later_eligible_candidate_still_waits_for_older_candidate(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    older_workspace_id, _older_attempt_id, older_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Older validated candidate",
        pr_number=151,
        created_at=now,
    )
    later_workspace_id, _later_attempt_id, _later_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Later validated candidate",
        pr_number=152,
        created_at=now + timedelta(minutes=5),
    )

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=later_workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef.from_url(REPO_URL),
        pr_number=152,
        status=_status(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{later_workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(later_workspace_id)
        assert workspace is not None
        queue_wait_events = [
            event
            for event in workspace.events
            if event.reason_code == "MERGE_QUEUE_WAITING_FOR_OLDER_CANDIDATE"
        ]

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)
    assert len(queue_wait_events) == 1
    assert queue_wait_events[0].payload is not None
    assert queue_wait_events[0].payload["blocker_candidate_id"] == older_candidate_id
    assert queue_wait_events[0].payload["blocker_workspace_id"] == older_workspace_id


@pytest.mark.unit
@pytest.mark.parametrize("clearing_state", ["merged", "closed", "non_canonical"])
async def test_monitor_merges_once_older_candidate_stops_blocking(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
    clearing_state: str,
) -> None:
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    older_workspace_id, older_attempt_id, _older_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Older candidate",
        pr_number=201,
        created_at=now,
    )
    later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Later candidate",
        pr_number=202,
        created_at=now + timedelta(minutes=5),
    )
    async with factory() as session:
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

    cmd.queue_result(returncode=0)  # gh pr merge
    cmd.queue_result(returncode=0, stdout="MERGESHA\n")  # merge SHA lookup

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=later_workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef.from_url(REPO_URL),
        pr_number=202,
        status=_status(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{later_workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        candidate = await MergeCandidateRepository(session).get_by_attempt_id(
            _later_attempt_id,
        )
        workspace = await WorkspaceRepository(session).get(later_workspace_id)

    assert terminal is True
    assert any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    assert sleep_fn.calls == []
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.completed.value
    assert candidate is not None
    assert candidate.id == later_candidate_id
    assert candidate.status == "merged"
