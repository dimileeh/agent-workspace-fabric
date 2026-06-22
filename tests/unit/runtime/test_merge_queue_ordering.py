"""PR monitor merge-queue ordering tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.db.repositories as repositories
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceRepository,
    owned_paths_overlap,
    sync_candidate_readiness,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    _MERGE_BLOCK_ATTENTION_STATE_KEY,
    Merge,
    MonitorState,
)
from awf.service.merge_queue import list_merge_queue_blockers_for_candidate
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor import _status

REPO_URL = "git@github.com:dimileeh/aira-web.git"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a session factory backed by an isolated Postgres engine."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def cmd() -> FakeCommandRunner:
    """Return a fake command runner for monitor executions."""
    return FakeCommandRunner()


@pytest.fixture
def adapter() -> FakeAdapter:
    """Return a fake GitHub adapter for monitor executions."""
    return FakeAdapter()


@pytest.fixture
def sleep_fn() -> RecordedSleep:
    """Return a sleep recorder for monitor retry timing."""
    return RecordedSleep()


async def _seed_monitoring_candidate(
    factory: async_sessionmaker[AsyncSession],
    *,
    title: str,
    pr_number: int,
    created_at: datetime,
    status: WorkspaceStatus = WorkspaceStatus.monitoring_pr,
    owned_paths: list[str] | None = None,
    resolved_profile: dict | None = None,
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
            resolved_profile=resolved_profile,
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
        owned_paths=["src/feature-a/**", "docs/awf-plans/**"],
    )
    _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Later plan artifact",
        pr_number=82,
        created_at=now + timedelta(minutes=5),
        owned_paths=["src/feature-b/**", "docs/awf-plans/**"],
    )

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert blockers == []


@pytest.mark.unit
async def test_custom_plan_artifact_overlap_does_not_block_later_candidate(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile-configured artifact files do not block merge queue progression."""
    workspace_ids = iter(
        [
            "ws_aaaaaaaaaaaaaaaaaaaaaaaa",
            "ws_bbbbbbbbbbbbbbbbbbbbbbbb",
        ]
    )
    monkeypatch.setattr(repositories, "new_workspace_id", lambda: next(workspace_ids))
    custom_profile = {
        "planning": {
            "required": True,
            "plan_path": "docs/alternate/{workspace_id}.md",
            "conformance_report_path": "docs/alternate/{workspace_id}.json",
        },
    }
    custom_artifact_glob = "docs/alternate/ws_*.md"
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    await _seed_monitoring_candidate(
        factory,
        title="Older custom plan artifact",
        pr_number=281,
        created_at=now,
        owned_paths=[
            "src/feature-a/**",
            custom_artifact_glob,
        ],
        resolved_profile=custom_profile,
    )
    _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Later custom plan artifact",
        pr_number=282,
        created_at=now + timedelta(minutes=5),
        owned_paths=[
            "src/feature-b/**",
            custom_artifact_glob,
        ],
        resolved_profile=custom_profile,
    )

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert owned_paths_overlap(custom_artifact_glob, custom_artifact_glob) is True
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
        owned_paths=["docs/awf-plans/README.md"],
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
        owned_paths=["src/shared/**", "docs/awf-plans/**"],
    )
    _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Later real overlap",
        pr_number=84,
        created_at=now + timedelta(minutes=5),
        owned_paths=["src/shared/module.py", "docs/awf-plans/**"],
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
        owned_paths=["docs/awf-plans/**"],
    )
    _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Later source work",
        pr_number=86,
        created_at=now + timedelta(minutes=5),
        owned_paths=["src/later/**", "docs/awf-plans/**"],
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
    """Verify merge monitor waits for older blockers without human comments."""
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
    """Verify a later eligible candidate still waits behind an older one."""
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
    """Verify the monitor merges once the older candidate no longer blocks."""
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


@pytest.mark.unit
async def test_merge_queue_wait_clears_stale_awaiting_human_attention(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    """A resolved ``NotifyHuman`` episode must not leak into a merge that only
    waits on a non-human gate (#659).

    When a prior ``HUMAN_WAIT`` is resolved and the next poll returns ``Merge``,
    ``loop._execute`` skips its general attention clear for the ``Merge`` arm (so a
    branch-protection rejection's COALESCE'd episode start is not reset each poll).
    ``handle_merge_action`` must therefore clear the stale flag itself before it
    parks on a non-human gate wait — here, waiting behind an older merge-queue
    candidate — otherwise the console/KPI/metrics keep showing "awaiting human"
    with a stale ``awaiting_human_since`` while the monitor is merely queued.
    """
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    older_workspace_id, _older_attempt_id, _older_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Older candidate",
        pr_number=301,
        created_at=now,
    )
    later_workspace_id, _later_attempt_id, _later_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Later candidate",
        pr_number=302,
        created_at=now + timedelta(minutes=5),
    )

    # Seed a stable episode start from an earlier, now-resolved NotifyHuman poll.
    episode_start = datetime(2026, 4, 26, 11, 0, tzinfo=UTC)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            later_workspace_id, reason="prior human escalation", now=episode_start
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
        pr_number=302,
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

    # The monitor parked on the merge-queue wait (non-human gate), not a merge.
    assert terminal is False
    assert sleep_fn.calls == [60]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    # Still polling, but the stale awaiting-human signal is cleared.
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.awaiting_human_since is None
    assert workspace.awaiting_human_reason is None


@pytest.mark.unit
async def test_merge_queue_wait_preserves_active_branch_protection_attention(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    """A merge-queue wait must NOT clear attention the branch-protection fallback
    set while it is still active (PRRT_kwDOSJAM6s6LXscz).

    The branch-protection fallback escalates to a human *without* a sticky blocker,
    so ``decide()`` keeps returning ``Merge`` while the operator is still blocked.
    When such a poll then parks behind an older merge-queue candidate, the
    non-human gate clear must preserve that still-active ``awaiting_human_since``
    (flagged via ``merge_block_attention_active``) instead of wiping it as a
    resolved ``NotifyHuman`` episode — otherwise console KPIs and CLI attention go
    false even though the PR genuinely needs a human.
    """
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    _older_workspace_id, _older_attempt_id, _older_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Older candidate",
        pr_number=311,
        created_at=now,
    )
    later_workspace_id, _later_attempt_id, _later_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Later candidate",
        pr_number=312,
        created_at=now + timedelta(minutes=5),
    )

    # The branch-protection fallback stamped attention on an earlier poll and is
    # still blocked: ``decide()`` keeps returning ``Merge`` and the merge loop owns
    # the flag for that arm (``merge_block_attention_active``).
    episode_start = datetime(2026, 4, 26, 11, 0, tzinfo=UTC)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            later_workspace_id, reason="GitHub rejected the merge attempt", now=episode_start
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

    active_block_state = MonitorState()
    # Stamp the marker FRESH (this poll's wall-clock) so the TTL gate treats it
    # as still-blocked and the merge-queue wait preserves the signal (#663).
    active_block_state.mark_merge_block_attention(now=datetime.now(UTC))

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=later_workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef.from_url(REPO_URL),
        pr_number=312,
        status=_status(),
        state=active_block_state,
        base_branch="development",
        remote_branch=f"awf/{later_workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(later_workspace_id)
        assert workspace is not None

    # The monitor parked on the merge-queue wait (non-human gate), not a merge.
    assert terminal is False
    assert sleep_fn.calls == [60]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    # Still polling, and the active branch-protection signal is PRESERVED.
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.awaiting_human_since == episode_start
    assert workspace.awaiting_human_reason == "GitHub rejected the merge attempt"


def _stale_merge_block_state(*, episode_start: datetime) -> MonitorState:
    """Build a ``MonitorState`` carrying a STALE ``merge_block_attention`` marker.

    Simulates a branch-protection block that resolved externally between polls:
    the prior poll's fallback stamped attention + a fresh marker, but no fallback
    has fired since (marker age now exceeds the TTL), so the marker is RESOLVED
    and ``_clear_stale_merge_attention`` must clear it instead of preserving it
    (#663).
    """
    state = MonitorState()
    # Stamp the marker well outside the default 120s TTL → stale (resolved).
    stale_marker_time = episode_start - timedelta(seconds=600)
    state.mark_merge_block_attention(now=stale_marker_time)
    return state


@pytest.mark.unit
async def test_resolved_branch_protection_marker_cleared_on_merge_queue_wait(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    """#663: a branch-protection block that RESOLVED externally between polls must
    not keep surfacing "awaiting human" while the monitor only waits on the merge
    queue.

    The prior poll's fallback set attention + a fresh marker, but the block
    resolved before this poll (no fallback fired this cycle), so the marker is
    STALE (age > TTL). ``decide()`` still returns ``Merge``; when this poll parks
    behind an older merge-queue candidate, ``_clear_stale_merge_attention`` must
    clear the stale marker and the surfaced flag instead of preserving it as a
    still-active signal — only NON-human gates remain.
    """
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    _older_workspace_id, _older_attempt_id, _older_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Older candidate",
        pr_number=321,
        created_at=now,
    )
    later_workspace_id, _later_attempt_id, _later_candidate_id = await _seed_monitoring_candidate(
        factory,
        title="Later candidate",
        pr_number=322,
        created_at=now + timedelta(minutes=5),
    )

    # Seed the surfaced attention + STALE marker from a prior, now-resolved poll.
    episode_start = datetime(2026, 4, 26, 11, 0, tzinfo=UTC)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            later_workspace_id, reason="GitHub rejected the merge attempt", now=episode_start
        )
        await session.commit()
    state = _stale_merge_block_state(episode_start=episode_start)

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
        pr_number=322,
        status=_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{later_workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(later_workspace_id)
        assert workspace is not None

    # The monitor parked on the merge-queue wait (non-human gate), not a merge.
    assert terminal is False
    assert sleep_fn.calls == [60]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    # The stale marker was cleared, so the surfaced flag is gone — only
    # NON-human gates remain and "awaiting human" must not stay up.
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.awaiting_human_since is None
    assert workspace.awaiting_human_reason is None
    # The stale marker was dropped from state.
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_resolved_branch_protection_marker_cleared_on_reviewer_settle_wait(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    """#663 parity: a RESOLVED branch-protection marker is cleared when the poll
    parks on the non-check reviewer-settle wait (not just the merge queue).
    """
    pr_number = 323
    head_sha = "c" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    # The non-check reviewer settle wait only triggers for refactor tasks with
    # configured non-check reviewers.
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.task_class = TaskClass.refactor_task.value
        await session.commit()

    # Seed the surfaced attention + STALE marker from a prior, now-resolved poll.
    episode_start = datetime(2026, 4, 26, 11, 0, tzinfo=UTC)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            workspace_id, reason="GitHub rejected the merge attempt", now=episode_start
        )
        await session.commit()
    state = _stale_merge_block_state(episode_start=episode_start)

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=900,
        non_check_reviewer_logins=("greptile-apps",),
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef.from_url(REPO_URL),
        pr_number=pr_number,
        status=_status(head_sha=head_sha),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)

    # The monitor parked on the reviewer-settle wait (non-human gate), not a merge.
    assert terminal is False
    assert sleep_fn.calls == [60]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    settle_ops = [
        op
        for op in operations
        if op.type == OperationType.monitor_state.value
        and op.payload.get("reason_code") == "NON_CHECK_REVIEWER_SETTLE"
    ]
    assert len(settle_ops) == 1
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    # The stale marker was cleared, so the surfaced flag is gone.
    assert workspace.awaiting_human_since is None
    assert workspace.awaiting_human_reason is None
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_resolved_branch_protection_marker_cleared_on_initial_grace_wait(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    """#663 parity: a RESOLVED branch-protection marker is cleared when the poll
    parks on the initial-review-grace wait (not just the merge queue).
    """
    pr_number = 324
    workspace_id = await seed_monitoring_workspace(factory, pr_number=pr_number)

    # Seed the surfaced attention + STALE marker from a prior, now-resolved poll.
    episode_start = datetime(2026, 4, 26, 11, 0, tzinfo=UTC)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            workspace_id, reason="GitHub rejected the merge attempt", now=episode_start
        )
        await session.commit()
    state = _stale_merge_block_state(episode_start=episode_start)

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef.from_url(REPO_URL),
        pr_number=pr_number,
        status=_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)

    # The monitor parked on the initial-review-grace wait (non-human gate).
    assert terminal is False
    assert sleep_fn.calls == [60]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    grace_ops = [
        op
        for op in operations
        if op.type == OperationType.monitor_state.value
        and op.payload.get("reason_code") == "INITIAL_REVIEW_GRACE"
    ]
    assert len(grace_ops) == 1
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    # The stale marker was cleared, so the surfaced flag is gone.
    assert workspace.awaiting_human_since is None
    assert workspace.awaiting_human_reason is None
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids
