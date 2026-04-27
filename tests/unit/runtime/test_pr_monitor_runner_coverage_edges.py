"""Focused branch-coverage tests for PR monitor runner edge behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.base import Base
from awf.db.enums import OperationType, TaskClass, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceEventCreate, WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewComment,
)
from awf.runtime.pr_monitor_runner import (
    _initial_review_grace_done_key,
    _initial_review_grace_started_key,
    _initial_review_grace_state_for_persistence,
    _initial_review_grace_state_for_runtime,
    _merge_rejection_reason,
    _notify_human_reason,
    _stale_pending_check_warnings,
    _target_reconcile_payload,
)
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'monitor.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


def _status_for_helpers(
    *,
    reviews: tuple[ReviewComment, ...] = (),
    checks: tuple[CheckTiming, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha="abc123",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=reviews,
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=checks,
    )


class _FailingLogSink:
    async def write(self, data: str) -> None:
        del data
        raise RuntimeError("log sink unavailable")


async def _mark_refactor_task(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    auto_merge: bool,
) -> None:
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        ws.task_class = TaskClass.refactor_task.value
        ws.auto_merge = auto_merge
        await s.commit()


@pytest.mark.unit
async def test_stale_merge_without_auto_merge_aborts_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=False)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload())

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_message == "monitor: abort (stale)"


@pytest.mark.unit
async def test_stale_auto_merge_dispatches_validation_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload())

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.ready.value
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
    assert [(op.type, op.payload) for op in operations] == [
        (
            OperationType.validate.value,
            {"source": "pr_monitor", "reason": "validation_insufficient_tier"},
        )
    ]


@pytest.mark.unit
async def test_best_effort_monitor_log_and_missing_workspace_event_append_do_not_raise(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner._write_monitor_log(_FailingLogSink(), {"event": "monitor.test"})  # type: ignore[arg-type]
    await runner._append_workspace_events(
        workspace_id="ws_missing",
        events=[
            WorkspaceEventCreate(
                event_type="workspace.test",
                reason_code="TEST",
                payload={"ok": True},
            )
        ],
    )


@pytest.mark.unit
async def test_post_human_notification_dedup_skips_github_call(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    status = _status_for_helpers()
    state = MonitorState(threads_addressed_ids={"__awf_notify__:abc123:manual": "notified"})

    await runner._post_human_notification_once(
        repo=RepoRef(owner="example", name="repo"),
        pr_number=42,
        status=status,
        state=state,
        blocker_reason="manual",
    )

    assert cmd.calls == []


@pytest.mark.unit
async def test_defer_signal_write_failure_is_best_effort(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    artifact_file = tmp_path / "artifacts-file"
    artifact_file.write_text("not a directory", encoding="utf-8")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifact_file,
    )

    runner._write_defer_signal(
        workspace_id="ws_defer",
        pr_number=42,
        terminal_action="Abort",
        merged=False,
        status=_status_for_helpers(),
        state=MonitorState(),
    )

    assert artifact_file.read_text(encoding="utf-8") == "not a directory"


@pytest.mark.unit
async def test_target_branch_reconcile_failure_appends_workspace_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)

    async def failing_reconciler(*, repo_url: str, branch: str) -> object:
        assert repo_url == "git@github.com:dimileeh/aira-web.git"
        assert branch == "development"
        raise RuntimeError("target branch locked")

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        post_merge_target_reconciler=failing_reconciler,
    )

    await runner._reconcile_target_branch_after_merge(
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        base_branch="development",
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.events[-1].event_type == "target_branch.reconcile_failed"
        assert ws.events[-1].reason_code == "TARGET_BRANCH_RECONCILE_FAILED"
        assert ws.events[-1].payload["error"] == "target branch locked"


@pytest.mark.unit
async def test_completed_filesystem_gc_exception_is_logged_and_swallowed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    class _ExplodingSessionFactory:
        def __call__(self) -> object:
            raise RuntimeError("database unavailable")

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.session_factory = _ExplodingSessionFactory()  # type: ignore[assignment]

    with structlog.testing.capture_logs() as captured:
        await runner._gc_completed_workspace_filesystem("ws_gc")

    assert any(
        event.get("event") == "monitor.filesystem_gc_raised"
        and event.get("workspace_id") == "ws_gc"
        for event in captured
    )


@pytest.mark.unit
async def test_dirty_worktree_helper_returns_false_for_non_commit_cases(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    case_number = 0

    async def run_case(
        *,
        workspace_exists: bool = True,
        queued: list[tuple[int, str, str]],
    ) -> tuple[bool, list[list[str]]]:
        nonlocal case_number
        case_number += 1
        cmd = FakeCommandRunner()
        for returncode, stdout, stderr in queued:
            cmd.queue_result(returncode=returncode, stdout=stdout, stderr=stderr)
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=FakeAdapter(),
            sleep_fn=RecordedSleep(),
            worktrees_root=tmp_path / "worktrees",
        )
        workspace_id = f"ws_dirty_{case_number}"
        if workspace_exists:
            (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
        result = await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="fix: dirty",
        )
        return result, [call.args for call in cmd.calls]

    missing_result, missing_calls = await run_case(workspace_exists=False, queued=[])
    status_result, status_calls = await run_case(queued=[(1, "", "status failed")])
    clean_result, clean_calls = await run_case(queued=[(0, "", "")])
    add_result, add_calls = await run_case(queued=[(0, " M a.py\n", ""), (1, "", "add failed")])
    cached_result, cached_calls = await run_case(
        queued=[(0, " M a.py\n", ""), (0, "", ""), (0, "", "")]
    )
    commit_result, commit_calls = await run_case(
        queued=[(0, " M a.py\n", ""), (0, "", ""), (1, "", ""), (1, "", "commit failed")]
    )

    assert missing_result is False
    assert missing_calls == []
    assert status_result is False
    assert [args[-2:] for args in status_calls] == [["status", "--porcelain"]]
    assert clean_result is False
    assert [args[-2:] for args in clean_calls] == [["status", "--porcelain"]]
    assert add_result is False
    assert [args[-2:] for args in add_calls] == [["status", "--porcelain"], ["add", "-A"]]
    assert cached_result is False
    assert [args[-2:] for args in cached_calls[:2]] == [["status", "--porcelain"], ["add", "-A"]]
    assert cached_calls[2][-3:] == ["diff", "--cached", "--quiet"]
    assert commit_result is False
    assert commit_calls[-1][-3:] == ["commit", "-m", "fix: dirty"]


@pytest.mark.unit
def test_stale_pending_check_warning_helpers_cover_disabled_and_terminal_cases() -> None:
    started = datetime.now(UTC) - timedelta(seconds=120)
    pending = CheckTiming(name="slow", status="IN_PROGRESS", started_at=started)
    terminal_status = CheckTiming(name="done", status="COMPLETED", started_at=started)
    terminal_conclusion = CheckTiming(
        name="done-conclusion",
        status="MYSTERY",
        conclusion="SUCCESS",
        started_at=started,
    )
    missing_started = CheckTiming(name="missing-started", status="IN_PROGRESS")

    assert (
        _stale_pending_check_warnings(
            _status_for_helpers(checks=(pending,)),
            now=datetime.now(UTC),
            threshold_seconds=0,
        )
        == ()
    )
    assert (
        _stale_pending_check_warnings(
            _status_for_helpers(checks=(terminal_status, terminal_conclusion, missing_started)),
            now=datetime.now(UTC),
            threshold_seconds=60,
        )
        == ()
    )
    warnings = _stale_pending_check_warnings(
        _status_for_helpers(checks=(pending,)),
        now=datetime.now(UTC),
        threshold_seconds=60,
    )
    assert len(warnings) == 1
    assert warnings[0].check_name == "slow"
    assert warnings[0].threshold_window >= 1


@pytest.mark.unit
def test_notify_human_reason_and_merge_rejection_detail() -> None:
    blocking_review = ReviewComment(
        comment_id="C1",
        body_excerpt="review skipped",
        author="coderabbitai",
        blocks_merge=True,
    )
    status = _status_for_helpers(reviews=(blocking_review,))

    assert "review bot reported" in (_notify_human_reason(status, MonitorState()) or "")
    assert _merge_rejection_reason("") == "GitHub rejected the merge attempt"
    assert _merge_rejection_reason("  protected\n branch  ") == (
        "GitHub rejected the merge attempt: protected branch"
    )


@pytest.mark.unit
def test_initial_review_grace_state_converts_wall_and_legacy_values() -> None:
    started_key = _initial_review_grace_started_key(42)
    done_key = _initial_review_grace_done_key(42)
    wall_started = datetime(2026, 4, 27, 12, 0, tzinfo=UTC).timestamp()

    runtime_state = _initial_review_grace_state_for_runtime(
        {started_key: f"{wall_started:.6f}", done_key: "elapsed"},
        pr_number=42,
        now_monotonic=1_100,
        now_wall_seconds=wall_started + 60,
    )
    assert runtime_state[started_key] == "1040.000000"
    assert runtime_state[done_key] == "elapsed"

    legacy_state = _initial_review_grace_state_for_runtime(
        {started_key: "500.0"},
        pr_number=42,
        now_monotonic=1_100,
        now_wall_seconds=wall_started + 60,
        legacy_monotonic_fallback=900,
    )
    assert legacy_state[started_key] == "900.000000"

    persisted = _initial_review_grace_state_for_persistence(
        {started_key: "1040.000000"},
        pr_number=42,
        now_monotonic=1_100,
        now_wall_seconds=wall_started + 60,
    )
    assert persisted[started_key] == f"{wall_started:.6f}"

    invalid = _initial_review_grace_state_for_persistence(
        {started_key: "not-a-number"},
        pr_number=42,
        now_monotonic=1_100,
        now_wall_seconds=wall_started,
    )
    assert invalid[started_key] == "not-a-number"


@pytest.mark.unit
def test_target_reconcile_payload_supports_dict_to_dict_and_fallback() -> None:
    class _ToDict:
        def to_dict(self) -> dict[str, object]:
            return {"status": "clean"}

    class _BadToDict:
        def to_dict(self) -> list[str]:
            return ["not", "a", "dict"]

    bad = _BadToDict()
    assert _target_reconcile_payload({"status": "ok"}) == {"status": "ok"}
    assert _target_reconcile_payload(_ToDict()) == {"status": "clean"}
    assert _target_reconcile_payload(bad) == {"result": str(bad)}
