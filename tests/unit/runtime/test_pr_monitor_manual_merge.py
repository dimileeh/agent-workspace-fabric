"""Manual-merge PR monitor regressions.

Manual/release monitor mode treats green gates as "ready for a human",
not as a terminal condition. These tests keep that contract in one place:
open PRs stay in monitoring, externally merged PRs complete and clean up,
and ordinary blockers still take precedence over readiness.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient, RepoRef
from awf.db.enums import OperationType, TaskClass, WorkspaceStatus
from awf.db.models import MergeCandidate, Workspace, WorkspaceEvent
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReviewThread,
    decide,
)
from awf.runtime.release_pr_monitor import build_release_pr_monitor
from awf.runtime.validation import ValidationRunner
from awf.service.gc import WorkspaceGCWorktreeRemoveResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    issue_comment_node,
    make_runner,
    mock_completed_compose_manager,
    pr_payload,
    seed_monitoring_workspace,
    thread_node,
)


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


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _green_status(
    *,
    head_sha: str = "abc1234567890def",
    merge_state_status: MergeStateStatus = MergeStateStatus.CLEAN,
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=merge_state_status,
    )


def _calls(cmd: FakeCommandRunner, predicate: Callable[[list[str]], bool]) -> list[list[str]]:
    return [call.args for call in cmd.calls if predicate(call.args)]


def _has_call(cmd: FakeCommandRunner, predicate: Callable[[list[str]], bool]) -> bool:
    return bool(_calls(cmd, predicate))


def _call_index(cmd: FakeCommandRunner, predicate: Callable[[list[str]], bool]) -> int:
    for index, call in enumerate(cmd.calls):
        if predicate(call.args):
            return index
    raise AssertionError(f"expected command was not called: {[call.args for call in cmd.calls]}")


def _is_pr_comment(args: list[str]) -> bool:
    return args[:3] == ["gh", "pr", "comment"]


def _is_pr_merge(args: list[str]) -> bool:
    return args[:3] == ["gh", "pr", "merge"]


def _is_docker_down(args: list[str]) -> bool:
    return args[:2] == ["docker", "compose"] and "down" in args


def _is_git_push(args: list[str]) -> bool:
    return args[:1] == ["git"] and "push" in args


def _is_resolve_thread(args: list[str]) -> bool:
    return args[:3] == ["gh", "api", "graphql"] and any(
        "resolveReviewThread" in arg for arg in args
    )


def _is_run_list(args: list[str]) -> bool:
    return args[:3] == ["gh", "run", "list"]


async def _workspace(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> Workspace:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        return workspace


async def _candidate(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> MergeCandidate:
    async with factory() as session:
        result = await session.execute(
            select(MergeCandidate).where(MergeCandidate.workspace_id == workspace_id)
        )
        return result.scalar_one()


async def _state_events(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> list[WorkspaceEvent]:
    async with factory() as session:
        result = await session.execute(
            select(WorkspaceEvent)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == "workspace.state_changed",
            )
            .order_by(WorkspaceEvent.occurred_at.asc(), WorkspaceEvent.id.asc())
        )
        return list(result.scalars())


def _action_entries(records: list[dict]) -> list[dict]:
    return [record for record in records if record.get("event") == "monitor.action"]


@pytest.mark.unit
async def test_manual_merge_green_open_pr_notifies_and_stays_monitoring(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, auto_merge=False)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
    )
    status = _green_status()
    state = MonitorState()
    action = decide(
        status,
        state,
        MonitorConfig(
            auto_merge=False,
            poll_interval_seconds=60,
            settle_interval_seconds=30,
            initial_review_grace_period_seconds=0,
            pre_merge_settle_seconds=0,
            non_check_reviewer_settle_seconds=0,
            non_check_reviewer_logins=("greptile-apps",),
            stale_pending_check_warning_seconds=900,
        ),
    )

    cmd.queue_result(returncode=0)  # gh pr comment
    terminal = await runner._execute(
        action=action,
        workspace_id=ws_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef.from_url("git@github.com:dimileeh/aira-web.git"),
        pr_number=42,
        status=status,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    ws = await _workspace(factory, ws_id)
    candidate = await _candidate(factory, ws_id)
    assert isinstance(action, NotifyHuman)
    assert terminal is False
    assert ws is not None
    assert ws.status == WorkspaceStatus.monitoring_pr.value
    assert candidate.status == "open"
    assert candidate.manual_merge_required is True
    assert candidate.completed is False
    assert len(_calls(cmd, _is_pr_comment)) == 1
    assert not _has_call(cmd, _is_pr_merge)
    assert not _has_call(cmd, _is_docker_down)
    assert sleep_fn.calls == [60]


@pytest.mark.unit
async def test_manual_ready_handoff_persists_fallback_attention_reason(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    """A clean manual-ready handoff persists a "ready to merge" attention reason (#659).

    ``decide()`` returns a message-less ``NotifyHuman()`` for a green PR with
    ``auto_merge`` off (the "ready for a human to merge" handoff), and
    ``_notify_human_reason`` returns ``None`` when nothing blocks. Without a
    fallback the ``None`` reason is subscripted by ``set_workspace_attention``'s
    length clamp and raises ``TypeError`` mid-poll (and would otherwise persist a
    null ``awaiting_human_reason``). The fallback keeps the operator-visible reason
    a sensible non-empty string AND, for this genuine manual-ready handoff, makes
    it match the "ready to merge — human action required" PR comment that
    ``_post_human_notification_once`` posts, instead of a vague "waiting for
    human" reason the operator can't reconcile with the PR (#659).
    """
    ws_id = await seed_monitoring_workspace(factory, auto_merge=False)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
    )

    cmd.queue_result(returncode=0)  # gh pr comment
    terminal = await runner._execute(
        action=NotifyHuman(),
        workspace_id=ws_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef.from_url("git@github.com:dimileeh/aira-web.git"),
        pr_number=42,
        status=_green_status(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    since, reason, status = await _read_attention(factory, ws_id)
    assert status == WorkspaceStatus.monitoring_pr.value
    assert since is not None
    assert (
        reason == "PR is ready to merge — AWF will not merge automatically; human action required."
    )
    # The console reason now matches what was posted on the PR: the clean
    # manual-ready handoff comment uses the "ready to merge" template (#659).
    comment_calls = _calls(cmd, _is_pr_comment)
    assert len(comment_calls) == 1
    body = comment_calls[0][comment_calls[0].index("--body") + 1]
    assert "ready to merge" in body
    assert "human action required" in body


@pytest.mark.unit
async def test_manual_ready_fallback_does_not_claim_ready_for_blocking_outdated_thread(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    """A reasonless ``NotifyHuman`` that is NOT ready keeps the generic reason (#659).

    An outdated inline thread downgraded to ``needs_human`` still blocks the merge
    (``decide`` gate 7) so ``decide()`` returns a message-less ``NotifyHuman()``,
    but ``_notify_human_reason`` returns ``None`` (it consults the non-outdated
    feeds only) and this is not a manual-ready handoff. The fallback must keep the
    safe generic wait reason here rather than falsely advertising "ready to merge"
    — the console reason must not over-claim readiness for a still-blocked PR.
    """
    ws_id = await seed_monitoring_workspace(factory, auto_merge=False)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
    )

    outdated_thread = ReviewThread(
        thread_id="THREAD_outdated",
        path="src/app.py",
        line=10,
        body_excerpt="please rework this",
        is_resolved=False,
        is_outdated=True,
    )
    status = PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        outdated_unresolved_inline_threads=(outdated_thread,),
    )
    state = MonitorState()
    state.mark_addressed(outdated_thread.thread_id, "needs_human")

    cmd.queue_result(returncode=0)  # gh pr comment
    terminal = await runner._execute(
        action=NotifyHuman(),
        workspace_id=ws_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef.from_url("git@github.com:dimileeh/aira-web.git"),
        pr_number=42,
        status=status,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    _, reason, ws_status = await _read_attention(factory, ws_id)
    assert ws_status == WorkspaceStatus.monitoring_pr.value
    assert reason == "PR monitor is waiting for human attention."


@pytest.mark.unit
async def test_manual_merge_green_pr_dispatches_validation_before_handoff(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, auto_merge=False)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        ws.task_class = TaskClass.refactor_task.value
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
        initial_review_grace_period_seconds=0,
    )
    status = _green_status()
    state = MonitorState()
    action = decide(
        status,
        state,
        MonitorConfig(
            auto_merge=False,
            poll_interval_seconds=60,
            settle_interval_seconds=30,
            initial_review_grace_period_seconds=0,
            pre_merge_settle_seconds=0,
            non_check_reviewer_settle_seconds=0,
            non_check_reviewer_logins=("greptile-apps",),
            stale_pending_check_warning_seconds=900,
        ),
    )

    terminal = await runner._execute(
        action=action,
        workspace_id=ws_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef.from_url("git@github.com:dimileeh/aira-web.git"),
        pr_number=42,
        status=status,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert isinstance(action, NotifyHuman)
    assert terminal is True
    assert not _has_call(cmd, _is_pr_comment)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.ready.value
        operations = await OperationRepository(session).list_all(workspace_id=ws_id)
    validate_operations = [op for op in operations if op.type == OperationType.validate.value]
    assert len(validate_operations) == 1
    assert validate_operations[0].payload["recovery_mode"] == "validate_only"
    assert validate_operations[0].payload["reason_code"] == "VALIDATION_INSUFFICIENT_TIER"


@pytest.mark.unit
@pytest.mark.parametrize(
    "merge_state_status",
    (MergeStateStatus.BLOCKED, MergeStateStatus.HAS_HOOKS),
)
async def test_manual_merge_protected_pr_dispatches_validation_before_handoff(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
    merge_state_status: MergeStateStatus,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, auto_merge=False)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        ws.task_class = TaskClass.refactor_task.value
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
        initial_review_grace_period_seconds=0,
    )
    status = _green_status(merge_state_status=merge_state_status)
    state = MonitorState()
    action = decide(
        status,
        state,
        MonitorConfig(
            auto_merge=False,
            poll_interval_seconds=60,
            settle_interval_seconds=30,
            initial_review_grace_period_seconds=0,
            pre_merge_settle_seconds=0,
            non_check_reviewer_settle_seconds=0,
            non_check_reviewer_logins=("greptile-apps",),
            stale_pending_check_warning_seconds=900,
        ),
    )

    terminal = await runner._execute(
        action=action,
        workspace_id=ws_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef.from_url("git@github.com:dimileeh/aira-web.git"),
        pr_number=42,
        status=status,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert isinstance(action, NotifyHuman)
    assert terminal is True
    assert not _has_call(cmd, _is_pr_comment)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.ready.value
        operations = await OperationRepository(session).list_all(workspace_id=ws_id)
    validate_operations = [op for op in operations if op.type == OperationType.validate.value]
    assert len(validate_operations) == 1
    assert validate_operations[0].payload["recovery_mode"] == "validate_only"
    assert validate_operations[0].payload["reason_code"] == "VALIDATION_INSUFFICIENT_TIER"


@pytest.mark.unit
async def test_manual_ready_handoff_reports_policy_block_before_human_handoff(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, auto_merge=False)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
    )

    async def _policy_blocked(**_kwargs: object) -> bool:
        return True

    runner._refresh_scope_policy_for_merge = _policy_blocked  # type: ignore[method-assign]
    cmd.queue_result(returncode=0)  # gh pr comment

    terminal = await runner._execute(
        action=NotifyHuman(),
        workspace_id=ws_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef.from_url("git@github.com:dimileeh/aira-web.git"),
        pr_number=42,
        status=_green_status(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert len(_calls(cmd, _is_pr_comment)) == 1
    assert sleep_fn.calls == [60]


@pytest.mark.unit
async def test_manual_ready_handoff_waits_for_merge_queue_before_handoff(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, auto_merge=False)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
    )
    waited: list[str] = []

    async def _queue_blockers(_workspace_id: str) -> list[object]:
        return [object()]

    async def _wait_for_queue(**_kwargs: object) -> None:
        waited.append("queue")

    runner._merge_queue_blockers_for_workspace = _queue_blockers  # type: ignore[method-assign]
    runner._wait_for_merge_queue = _wait_for_queue  # type: ignore[method-assign]

    terminal = await runner._execute(
        action=NotifyHuman(),
        workspace_id=ws_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef.from_url("git@github.com:dimileeh/aira-web.git"),
        pr_number=42,
        status=_green_status(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert waited == ["queue"]
    assert not _calls(cmd, _is_pr_comment)


@pytest.mark.unit
async def test_manual_ready_handoff_waits_for_review_settle_before_validation(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, auto_merge=False)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        ws.task_class = TaskClass.refactor_task.value
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=900,
    )

    terminal = await runner._execute(
        action=NotifyHuman(),
        workspace_id=ws_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef.from_url("git@github.com:dimileeh/aira-web.git"),
        pr_number=42,
        status=_green_status(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=ws_id)
    validate_operations = [op for op in operations if op.type == OperationType.validate.value]
    assert validate_operations == []


async def _seed_stale_attention(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> None:
    """Stamp a stable episode start from an earlier, now-resolved NotifyHuman poll."""
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            workspace_id,
            reason="prior human escalation",
            now=datetime(2026, 4, 26, 11, 0, tzinfo=UTC),
        )
        await session.commit()


async def _read_attention(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> tuple[object, str | None, str]:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        return ws.awaiting_human_since, ws.awaiting_human_reason, ws.status


@pytest.mark.unit
async def test_manual_ready_handoff_merge_queue_wait_clears_stale_attention(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    """A resolved ``NotifyHuman`` episode must not leak into a manual-ready handoff
    that only waits on the merge queue (#659).

    ``loop._execute`` excludes ``NotifyHuman`` from its general attention clear so
    the eventual ready notification keeps a stable COALESCE'd episode start. When a
    prior block is resolved and ``decide()`` returns a manual-ready ``NotifyHuman``,
    the handoff can park behind an older merge-queue candidate before posting that
    notification. Without a clear here the console/KPI surface keeps showing
    "awaiting human" with a stale ``awaiting_human_since`` while the monitor is
    merely queued — mirror ``handle_merge_action``'s ``Merge`` arm and clear it.
    """
    ws_id = await seed_monitoring_workspace(factory, auto_merge=False)
    await _seed_stale_attention(factory, ws_id)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
    )
    waited: list[str] = []

    async def _queue_blockers(_workspace_id: str) -> list[object]:
        return [object()]

    async def _wait_for_queue(**_kwargs: object) -> None:
        waited.append("queue")

    runner._merge_queue_blockers_for_workspace = _queue_blockers  # type: ignore[method-assign]
    runner._wait_for_merge_queue = _wait_for_queue  # type: ignore[method-assign]

    terminal = await runner._execute(
        action=NotifyHuman(),
        workspace_id=ws_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef.from_url("git@github.com:dimileeh/aira-web.git"),
        pr_number=42,
        status=_green_status(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    # Parked on the merge-queue wait (non-human gate), not a human handoff.
    assert waited == ["queue"]
    assert not _calls(cmd, _is_pr_comment)
    since, reason, status = await _read_attention(factory, ws_id)
    assert status == WorkspaceStatus.monitoring_pr.value
    assert since is None
    assert reason is None


@pytest.mark.unit
async def test_manual_ready_handoff_validation_settle_wait_clears_stale_attention(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    """A resolved ``NotifyHuman`` episode must not leak into a manual-ready handoff
    that only waits on reviewer settle before validation recovery (#659)."""
    ws_id = await seed_monitoring_workspace(factory, auto_merge=False)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        ws.task_class = TaskClass.refactor_task.value
        await session.commit()
    await _seed_stale_attention(factory, ws_id)

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=900,
    )

    terminal = await runner._execute(
        action=NotifyHuman(),
        workspace_id=ws_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef.from_url("git@github.com:dimileeh/aira-web.git"),
        pr_number=42,
        status=_green_status(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    # Parked on the reviewer-settle wait (non-human gate) before validation.
    assert sleep_fn.calls == [60]
    since, reason, status = await _read_attention(factory, ws_id)
    assert status == WorkspaceStatus.monitoring_pr.value
    assert since is None
    assert reason is None


@pytest.mark.unit
async def test_manual_ready_handoff_settle_wait_clears_stale_attention(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    """A resolved ``NotifyHuman`` episode must not leak into a manual-ready handoff
    that only waits on reviewer settle before the ready notification (#659)."""
    ws_id = await seed_monitoring_workspace(factory, auto_merge=False)
    await _seed_stale_attention(factory, ws_id)

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=900,
    )

    terminal = await runner._execute(
        action=NotifyHuman(),
        workspace_id=ws_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef.from_url("git@github.com:dimileeh/aira-web.git"),
        pr_number=42,
        status=_green_status(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    # Parked on the reviewer-settle wait (non-human gate); no human handoff posted.
    assert sleep_fn.calls == [60]
    assert not _calls(cmd, _is_pr_comment)
    since, reason, status = await _read_attention(factory, ws_id)
    assert status == WorkspaceStatus.monitoring_pr.value
    assert since is None
    assert reason is None


@pytest.mark.unit
async def test_manual_merge_external_merge_completes_with_monitor_done_and_cleanup(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    worktrees_root = work_dir / "git" / "worktrees"
    ws_id = await seed_monitoring_workspace(
        factory,
        auto_merge=False,
        pr_merge_sha="m" * 40,
    )
    worktree = worktrees_root / ws_id
    compose_dir = work_dir / "compose" / ws_id
    auth_dir = work_dir / "auth" / ws_id
    log_file = work_dir / "logs" / ws_id / "monitor.log"
    _write(worktree / "repo.txt", "repo")
    _write(compose_dir / "compose.yml", "compose")
    _write(auth_dir / "codex" / "auth.json", "auth")
    _write(log_file, "keep logs")

    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload())  # green, open PR
    cmd.queue_result(returncode=0)  # gh pr comment
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
        auto_merge=False,
    )
    compose_patch, compose_calls = mock_completed_compose_manager()
    with (
        compose_patch,
        patch(
            "awf.service.gc._default_worktree_remover",
            new=AsyncMock(
                return_value=WorkspaceGCWorktreeRemoveResult(
                    status="succeeded",
                    reason_code="WORKTREE_REMOVE_SUCCEEDED",
                )
            ),
        ),
        structlog.testing.capture_logs() as captured,
    ):
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=compose_dir / "compose.yml",
        )

    ws = await _workspace(factory, ws_id)
    candidate = await _candidate(factory, ws_id)
    events = await _state_events(factory, ws_id)
    actions = [entry["action"] for entry in _action_entries(captured)]
    assert actions == ["NotifyHuman", "ShortCircuitCompleted"]
    assert ws is not None
    assert ws.status == WorkspaceStatus.completed.value
    assert events[-1].new_state == WorkspaceStatus.completed.value
    assert events[-1].reason_code == "MONITOR_DONE"
    assert ws.pr_merge_sha == "mergecommit1234567890"
    assert candidate.status == "merged"
    assert candidate.merged_at is not None
    assert not candidate.manual_merge_required
    assert not _has_call(cmd, _is_docker_down)
    assert compose_calls == [(f"awf_{ws_id}", Path(f"/tmp/awf/{ws_id}/compose.yml"), ws_id, True)]
    # On a successful (externally-driven) merge the pressure dirs are reclaimed
    # immediately, bypassing the retention window; the durable record stays.
    assert not worktree.exists()
    assert not auth_dir.exists()
    assert log_file.exists()
    assert not _has_call(cmd, _is_pr_merge)
    assert any(record.get("event") == "monitor.filesystem_gc_ok" for record in captured)
    assert not any(record.get("event") == "monitor.filesystem_gc_deferred" for record in captured)


@pytest.mark.unit
async def test_manual_merge_closed_unmerged_aborts_without_cleanup(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, auto_merge=False)
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(closed=True, merged=False))

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
    )
    with structlog.testing.capture_logs() as captured:
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

    ws = await _workspace(factory, ws_id)
    candidate = await _candidate(factory, ws_id)
    events = await _state_events(factory, ws_id)
    actions = [entry["action"] for entry in _action_entries(captured)]
    assert actions == ["Abort"]
    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    assert ws.failure_message is not None
    assert "pr_closed_externally" in ws.failure_message
    assert events[-1].new_state == WorkspaceStatus.failed.value
    assert events[-1].reason_code == "pr_closed_externally"
    assert candidate.status == "closed"
    assert candidate.close_reason == "WORKSPACE_FAILED"
    assert candidate.merged_at is None
    assert not _has_call(cmd, _is_pr_comment)
    assert not _has_call(cmd, _is_docker_down)
    assert not _has_call(cmd, _is_pr_merge)
    assert sleep_fn.calls == []


@pytest.mark.unit
async def test_manual_merge_unresolved_comments_route_to_address_comments_before_completion(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, auto_merge=False)
    thread = thread_node(tid="T_manual_fix", author="human-reviewer")

    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(threads=[thread]))
    adapter.queue(stdout="fixed it")
    cmd.queue_result(returncode=0, stdout=pr_payload())  # settle fetch
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")  # git push
    cmd.queue_result(returncode=0, stdout='{"data": {}}')  # resolveReviewThread
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
    cmd.queue_result(returncode=0)  # docker compose down

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
    )
    with structlog.testing.capture_logs() as captured:
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

    ws = await _workspace(factory, ws_id)
    actions = [entry["action"] for entry in _action_entries(captured)]
    assert actions == ["AddressComments", "ShortCircuitCompleted"]
    assert adapter.workspace_ids == [ws_id]
    assert "review thread" in adapter.calls[0].lower()
    assert _has_call(cmd, _is_git_push)
    assert _has_call(cmd, _is_resolve_thread)
    assert not _has_call(cmd, _is_pr_comment)
    assert not _has_call(cmd, _is_pr_merge)
    assert ws is not None
    assert ws.status == WorkspaceStatus.completed.value


@pytest.mark.unit
async def test_manual_merge_bot_issue_feedback_and_later_comments_still_addressable(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, auto_merge=False)
    policy_comment = issue_comment_node(
        cid=1001,
        author="coderabbitai",
        body="Review skipped. Please resolve the trigger review checklist item.",
    )
    late_thread = thread_node(tid="T_after_policy", author="human-reviewer")

    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(comments=[policy_comment]))
    adapter.queue(stdout="FALSE POSITIVE: trigger-review checklist status only")
    cmd.queue_result(returncode=0, stdout=pr_payload())  # settle fetch
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")  # git push
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(threads=[late_thread]))
    adapter.queue(stdout="fixed later comment")
    cmd.queue_result(returncode=0, stdout=pr_payload())  # settle fetch
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")  # git push
    cmd.queue_result(returncode=0, stdout='{"data": {}}')  # resolveReviewThread
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
    cmd.queue_result(returncode=0)  # docker compose down

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
    )
    with structlog.testing.capture_logs() as captured:
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

    actions = [entry["action"] for entry in _action_entries(captured)]
    assert actions == ["AddressComments", "AddressComments", "ShortCircuitCompleted"]
    assert len(_calls(cmd, _is_pr_comment)) == 0
    assert adapter.workspace_ids == [ws_id, ws_id]
    assert _has_call(cmd, _is_git_push)
    assert _has_call(cmd, _is_resolve_thread)
    assert not _has_call(cmd, _is_pr_merge)
    assert sleep_fn.calls == [30, 30]


@pytest.mark.unit
async def test_manual_merge_checks_block_ready_until_green_and_merge_still_requires_observation(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, auto_merge=False)

    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(check_state="PENDING"))
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(check_state="FAILURE"))
    cmd.queue_result(
        returncode=0,
        stdout=(
            '[{"databaseId": 4242, "name": "python-full-coverage", '
            '"conclusion": "FAILURE", "status": "completed"}]'
        ),
    )  # gh run list — one actionable failing run
    cmd.queue_result(
        returncode=0,
        stdout="tests/unit/test_x.py::test_y FAILED\nE   assert 1 == 2",
    )  # gh run view --log-failed — actionable pytest evidence drives ReportCiFailure
    adapter.queue(stdout="fixed ci")
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")  # git push
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload())  # green, open PR
    cmd.queue_result(returncode=0)  # gh pr comment
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
    cmd.queue_result(returncode=0)  # docker compose down

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
    )
    with structlog.testing.capture_logs() as captured:
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

    ws = await _workspace(factory, ws_id)
    actions = [entry["action"] for entry in _action_entries(captured)]
    assert actions == ["WaitForCI", "ReportCiFailure", "NotifyHuman", "ShortCircuitCompleted"]
    assert _call_index(cmd, _is_pr_comment) > _call_index(cmd, _is_run_list)
    assert len(_calls(cmd, _is_pr_comment)) == 1
    assert adapter.workspace_ids == [ws_id]
    assert _has_call(cmd, _is_git_push)
    assert not _has_call(cmd, _is_pr_merge)
    assert sleep_fn.calls == [60, 60]
    assert ws is not None
    assert ws.status == WorkspaceStatus.completed.value


@pytest.mark.unit
async def test_release_monitor_factory_uses_manual_merge_contract(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, auto_merge=False)
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload())  # green, open PR
    cmd.queue_result(returncode=0)  # gh pr comment
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))

    runner = build_release_pr_monitor(
        session_factory=factory,
        runner=cmd,
        adapter=adapter,
        gh=GitHubClient(cmd),
        validation=ValidationRunner(runner=cmd, artifacts_dir=tmp_path / "artifacts"),
        worktrees_root=tmp_path / "worktrees",
        poll_interval_seconds=60,
        settle_interval_seconds=30,
        initial_review_grace_period_seconds=0,
        pre_merge_settle_seconds=0,
        non_check_reviewer_settle_seconds=0,
        max_outer_iterations=4,
    )
    runner._deps.sleep = sleep_fn
    compose_patch, _compose_calls = mock_completed_compose_manager()
    with compose_patch, structlog.testing.capture_logs() as captured:
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

    ws = await _workspace(factory, ws_id)
    events = await _state_events(factory, ws_id)
    actions = [entry["action"] for entry in _action_entries(captured)]
    assert actions == ["NotifyHuman", "ShortCircuitCompleted"]
    assert ws is not None
    assert ws.status == WorkspaceStatus.completed.value
    assert events[-1].reason_code == "MONITOR_DONE"
    assert len(_calls(cmd, _is_pr_comment)) == 1
    assert not _has_call(cmd, _is_docker_down)
    assert not _has_call(cmd, _is_pr_merge)
    assert any(
        record.get("event") == "monitor.compose_teardown_ok" and record.get("workspace_id") == ws_id
        for record in captured
    )
    assert sleep_fn.calls == [60]
