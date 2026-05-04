"""Manual-merge PR monitor regressions.

Manual/release monitor mode treats green gates as "ready for a human",
not as a terminal condition. These tests keep that contract in one place:
open PRs stay in monitoring, externally merged PRs complete and clean up,
and ordinary blockers still take precedence over readiness.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient, RepoRef
from awf.db.base import Base
from awf.db.enums import WorkspaceStatus
from awf.db.models import MergeCandidate, Workspace, WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    decide,
)
from awf.runtime.release_pr_monitor import build_release_pr_monitor
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    issue_comment_node,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
    thread_node,
)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'awf.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


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


def _green_status(*, head_sha: str = "abc1234567890def") -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
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
    return len(args) >= 4 and args[0] == "git" and args[3] == "push"


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
    cmd.queue_result(returncode=0)  # docker compose down

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
        auto_merge=False,
    )
    with structlog.testing.capture_logs() as captured:
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
    assert _call_index(cmd, _is_docker_down) > _call_index(cmd, _is_pr_comment)
    docker_down = _calls(cmd, _is_docker_down)[0]
    assert docker_down[-3:] == ["down", "--remove-orphans", "--volumes"]
    assert worktree.exists()
    assert compose_dir.exists()
    assert auth_dir.exists()
    assert log_file.exists()
    assert not _has_call(cmd, _is_pr_merge)
    assert any(record.get("event") == "monitor.compose_teardown_ok" for record in captured)
    assert any(
        record.get("event") == "monitor.filesystem_gc_deferred"
        and record.get("reason_code") == "WORKSPACE_WITHIN_RETENTION"
        for record in captured
    )


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
async def test_manual_merge_policy_blocker_notifies_and_later_comments_still_addressable(
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
    cmd.queue_result(returncode=0)  # gh pr comment
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
    assert actions == ["NotifyHuman", "AddressComments", "ShortCircuitCompleted"]
    assert len(_calls(cmd, _is_pr_comment)) == 1
    assert adapter.workspace_ids == [ws_id]
    assert _has_call(cmd, _is_git_push)
    assert _has_call(cmd, _is_resolve_thread)
    assert not _has_call(cmd, _is_pr_merge)
    assert sleep_fn.calls == [60, 30]


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
    cmd.queue_result(returncode=0, stdout="[]")  # gh run list
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
    cmd.queue_result(returncode=0)  # docker compose down

    runner = build_release_pr_monitor(
        session_factory=factory,
        runner=cmd,
        adapter=adapter,
        gh=GitHubClient(cmd),
        worktrees_root=tmp_path / "worktrees",
        poll_interval_seconds=60,
        settle_interval_seconds=30,
        initial_review_grace_period_seconds=0,
        pre_merge_settle_seconds=0,
        non_check_reviewer_settle_seconds=0,
        max_outer_iterations=4,
    )
    runner._deps.sleep = sleep_fn
    with structlog.testing.capture_logs() as captured:
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
    assert _has_call(cmd, _is_docker_down)
    assert not _has_call(cmd, _is_pr_merge)
    assert sleep_fn.calls == [60]
