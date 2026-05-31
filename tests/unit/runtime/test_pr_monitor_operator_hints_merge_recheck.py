"""Operator hint persistence and merge-recheck regressions."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime import operator_hints
from awf.runtime.operator_hints import (
    OPERATOR_HINT_STATE_KEY,
    mark_operator_hint_processed,
    persist_operator_hint,
)
from awf.runtime.pr_monitor import (
    CheckTiming,
    Merge,
    MonitorConfig,
    MonitorState,
    OperatorHint,
    PRStatus,
)
from awf.runtime.pr_monitor_runner import helpers as runner_helpers
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import BaseFetchError
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_operator_hints import REPO_URL, _ready_status


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_persist_state_drops_stale_done_marker_when_freeze_started_matches(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    head_sha = "f" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=42,
        head_sha=head_sha,
    )
    initial_done_key = runner_helpers._initial_review_grace_done_key(42)
    initial_started_key = runner_helpers._initial_review_grace_started_key(42)
    settle_done_key = runner_helpers._non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=head_sha,
    )
    settle_started_key = runner_helpers._non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha=head_sha,
    )
    freeze_started_value = runner_helpers._initial_review_grace_wall_started_value_from_datetime(
        datetime(2026, 5, 31, 4, 30, 46, tzinfo=UTC)
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_last_commit_sha = head_sha
        workspace.monitor_threads_addressed = {
            initial_started_key: freeze_started_value,
            settle_started_key: freeze_started_value,
        }
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    stale_state = MonitorState(
        threads_addressed_ids={
            initial_started_key: freeze_started_value,
            initial_done_key: "elapsed",
            settle_started_key: freeze_started_value,
            settle_done_key: "elapsed",
            "review-thread": "fix_committed",
        }
    )

    await runner._persist_state(workspace_id, stale_state)

    async with factory() as session:
        persisted = await WorkspaceRepository(session).get(workspace_id)

    assert persisted is not None
    monitor_state = dict(persisted.monitor_threads_addressed)
    assert monitor_state[initial_started_key] == freeze_started_value
    assert monitor_state[settle_started_key] == freeze_started_value
    assert initial_done_key not in monitor_state
    assert settle_done_key not in monitor_state
    assert monitor_state["review-thread"] == "fix_committed"


@pytest.mark.unit
async def test_persist_state_preserves_newly_elapsed_settle_done_marker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    head_sha = "f" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=42,
        head_sha=head_sha,
    )
    settle_done_key = runner_helpers._non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=head_sha,
    )
    settle_started_key = runner_helpers._non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha=head_sha,
    )
    settle_freeze_key = runner_helpers._non_check_reviewer_settle_freeze_key(
        pr_number=42,
        head_sha=head_sha,
    )
    started_value = runner_helpers._initial_review_grace_wall_started_value_from_datetime(
        datetime.now(UTC) - timedelta(seconds=300)
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_last_commit_sha = head_sha
        workspace.monitor_threads_addressed = {
            settle_started_key: started_value,
            settle_freeze_key: "armed",
        }
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    workspace = await runner._load_workspace(workspace_id)
    state = runner._load_state(workspace)
    decision = runner_helpers._non_check_reviewer_settle_decision(
        _ready_status(head_sha=head_sha),
        state,
        MonitorConfig(
            auto_merge=True,
            poll_interval_seconds=60,
            non_check_reviewer_settle_seconds=180,
            non_check_reviewer_logins=("greptile-apps",),
        ),
        pr_number=42,
        now=time.monotonic(),
    )
    assert decision.action == "elapsed"
    assert state.threads_addressed_ids[settle_done_key] == "elapsed"
    assert settle_freeze_key not in state.threads_addressed_ids

    await runner._persist_state(workspace_id, state)

    async with factory() as session:
        persisted = await WorkspaceRepository(session).get(workspace_id)

    assert settle_done_key not in state.changed_thread_ids()
    assert persisted is not None
    monitor_state = dict(persisted.monitor_threads_addressed)
    assert monitor_state[settle_done_key] == "elapsed"
    assert settle_freeze_key not in monitor_state
    assert (
        runner_helpers._initial_review_grace_wall_seconds(monitor_state[settle_started_key])
        is not None
    )


@pytest.mark.unit
async def test_persist_state_drops_newly_elapsed_settle_done_after_concurrent_rearm(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    head_sha = "f" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=42,
        head_sha=head_sha,
    )
    settle_done_key = runner_helpers._non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=head_sha,
    )
    settle_started_key = runner_helpers._non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha=head_sha,
    )
    old_started_value = runner_helpers._initial_review_grace_wall_started_value_from_datetime(
        datetime.now(UTC) - timedelta(seconds=300)
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_last_commit_sha = head_sha
        workspace.monitor_threads_addressed = {settle_started_key: old_started_value}
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    workspace = await runner._load_workspace(workspace_id)
    state = runner._load_state(workspace)
    decision = runner_helpers._non_check_reviewer_settle_decision(
        _ready_status(head_sha=head_sha),
        state,
        MonitorConfig(
            auto_merge=True,
            poll_interval_seconds=60,
            non_check_reviewer_settle_seconds=180,
            non_check_reviewer_logins=("greptile-apps",),
        ),
        pr_number=42,
        now=time.monotonic(),
    )
    assert decision.action == "elapsed"
    assert state.threads_addressed_ids[settle_done_key] == "elapsed"

    rearmed_started_value = runner_helpers._initial_review_grace_wall_started_value_from_datetime(
        datetime.now(UTC)
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = {settle_started_key: rearmed_started_value}
        await session.commit()

    await runner._persist_state(workspace_id, state)

    async with factory() as session:
        persisted = await WorkspaceRepository(session).get(workspace_id)

    assert persisted is not None
    monitor_state = dict(persisted.monitor_threads_addressed)
    assert monitor_state[settle_started_key] == rearmed_started_value
    assert settle_done_key not in monitor_state


@pytest.mark.unit
async def test_merge_rechecks_persisted_operator_hint_before_merge_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    stale_state = MonitorState()
    hint = OperatorHint(
        reason="operator warning arrived after the monitor loaded state",
        operation_id="op_merge_recheck",
        requested_at="2026-05-30T23:55:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint(
            dict(workspace.monitor_threads_addressed or {}),
            hint,
        )
        await session.commit()

    calls: list[OperatorHint] = []

    async def _record_operator_hint_cycle(**kwargs: object) -> _GitPushResult:
        called_hint = kwargs["hint"]
        state_arg = kwargs["state"]
        assert isinstance(called_hint, OperatorHint)
        assert isinstance(state_arg, MonitorState)
        calls.append(called_hint)
        mark_operator_hint_processed(state_arg)
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _record_operator_hint_cycle)

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
        state=stale_state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert calls == [hint]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)


@pytest.mark.unit
async def test_merge_recheck_preserves_remote_push_url_for_persisted_operator_hint(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    stale_state = MonitorState()
    hint = OperatorHint(
        reason="operator warning arrived after a fork PR remote was selected",
        operation_id="op_merge_recheck_remote",
        requested_at="2026-05-31T00:25:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint(
            dict(workspace.monitor_threads_addressed or {}),
            hint,
        )
        await session.commit()

    remote_push_url = "https://github.com/fork-owner/aira-web.git"
    captured_remote_push_urls: list[str | None] = []

    async def _record_operator_hint_cycle(**kwargs: object) -> _GitPushResult:
        captured_remote_push_urls.append(kwargs["remote_push_url"])
        state_arg = kwargs["state"]
        assert isinstance(state_arg, MonitorState)
        mark_operator_hint_processed(state_arg)
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _record_operator_hint_cycle)

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
        state=stale_state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=remote_push_url,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert captured_remote_push_urls == [remote_push_url]


@pytest.mark.unit
async def test_merge_recheck_dispatches_persisted_operator_hint_before_pre_merge_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
        pre_merge_settle_seconds=2,
    )
    stale_state = MonitorState()
    hint = OperatorHint(
        reason="operator warning arrived during the pre-merge settle window",
        operation_id="op_merge_recheck_error",
        requested_at="2026-05-31T00:10:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint(
            dict(workspace.monitor_threads_addressed or {}),
            hint,
        )
        await session.commit()

    calls: list[OperatorHint] = []

    async def _raise_pre_merge_base_fetch_error(**_kwargs: object) -> PRStatus:
        raise BaseFetchError("base fetch failed while operator hint was pending")

    async def _record_operator_hint_cycle(**kwargs: object) -> _GitPushResult:
        called_hint = kwargs["hint"]
        state_arg = kwargs["state"]
        assert isinstance(called_hint, OperatorHint)
        assert isinstance(state_arg, MonitorState)
        calls.append(called_hint)
        mark_operator_hint_processed(state_arg)
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    monkeypatch.setattr(
        runner,
        "_fetch_status_for_decision",
        _raise_pre_merge_base_fetch_error,
    )
    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _record_operator_hint_cycle)

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
        state=stale_state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert calls == [hint]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_merge_rechecks_freeze_only_remonitor_before_merge_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    head_sha = "f" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=42,
        head_sha=head_sha,
    )
    initial_done_key = runner_helpers._initial_review_grace_done_key(42)
    initial_started_key = runner_helpers._initial_review_grace_started_key(42)
    settle_done_key = runner_helpers._non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=head_sha,
    )
    settle_started_key = runner_helpers._non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha=head_sha,
    )
    stale_state = MonitorState(
        threads_addressed_ids={
            initial_done_key: "elapsed",
            settle_done_key: "elapsed",
        }
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        monitor_state = dict(workspace.monitor_threads_addressed or {})
        operator_hints.arm_operator_hint_freeze(
            monitor_state,
            pr_number=42,
            head_sha=head_sha,
            now=datetime.now(UTC),
        )
        workspace.monitor_threads_addressed = monitor_state
        await session.commit()

    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(head_sha=head_sha),
        state=stale_state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert OPERATOR_HINT_STATE_KEY not in stale_state.threads_addressed_ids
    assert initial_done_key not in stale_state.threads_addressed_ids
    assert settle_done_key not in stale_state.threads_addressed_ids
    assert initial_started_key in stale_state.threads_addressed_ids
    assert settle_started_key in stale_state.threads_addressed_ids
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)


@pytest.mark.unit
async def test_merge_final_recheck_blocks_hint_written_after_locked_gate(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    state = MonitorState()
    hint = OperatorHint(
        reason="operator warning arrived after the locked merge gate",
        operation_id="op_final_merge_recheck",
        requested_at="2026-05-31T05:05:00+00:00",
    )
    original_merge_gate = runner._merge_gate_with_legacy_head_support
    merge_gate_calls = 0

    async def _write_hint_after_locked_gate(*args: object, **kwargs: object) -> object:
        nonlocal merge_gate_calls
        merge_gate_calls += 1
        result = await original_merge_gate(*args, **kwargs)
        if merge_gate_calls == 3:
            async with factory() as session:
                workspace = await WorkspaceRepository(session).get(workspace_id)
                assert workspace is not None
                workspace.monitor_threads_addressed = persist_operator_hint(
                    dict(workspace.monitor_threads_addressed or {}),
                    hint,
                )
                await session.commit()
        return result

    calls: list[OperatorHint] = []

    async def _record_operator_hint_cycle(**kwargs: object) -> _GitPushResult:
        called_hint = kwargs["hint"]
        state_arg = kwargs["state"]
        assert isinstance(called_hint, OperatorHint)
        assert isinstance(state_arg, MonitorState)
        calls.append(called_hint)
        mark_operator_hint_processed(state_arg)
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    monkeypatch.setattr(
        runner,
        "_merge_gate_with_legacy_head_support",
        _write_hint_after_locked_gate,
    )
    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _record_operator_hint_cycle)

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert calls == [hint]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)


@pytest.mark.unit
async def test_merge_last_chance_recheck_blocks_hint_written_after_final_refresh(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    state = MonitorState()
    hint = OperatorHint(
        reason="operator warning arrived after the final merge refresh",
        operation_id="op_last_chance_merge_recheck",
        requested_at="2026-05-31T11:55:00+00:00",
    )
    original_refresh = runner._refresh_operator_state_from_workspace
    refresh_calls = 0

    async def _write_hint_after_final_refresh(*args: object, **kwargs: object) -> bool:
        nonlocal refresh_calls
        refresh_calls += 1
        changed = await original_refresh(*args, **kwargs)
        if refresh_calls == 2:
            async with factory() as session:
                workspace = await WorkspaceRepository(session).get(workspace_id)
                assert workspace is not None
                workspace.monitor_threads_addressed = persist_operator_hint(
                    dict(workspace.monitor_threads_addressed or {}),
                    hint,
                )
                await session.commit()
        return changed

    calls: list[OperatorHint] = []

    async def _record_operator_hint_cycle(**kwargs: object) -> _GitPushResult:
        called_hint = kwargs["hint"]
        state_arg = kwargs["state"]
        assert isinstance(called_hint, OperatorHint)
        assert isinstance(state_arg, MonitorState)
        calls.append(called_hint)
        mark_operator_hint_processed(state_arg)
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    monkeypatch.setattr(
        runner,
        "_refresh_operator_state_from_workspace",
        _write_hint_after_final_refresh,
    )
    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _record_operator_hint_cycle)

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert refresh_calls == 3
    assert calls == [hint]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)


@pytest.mark.unit
async def test_merge_final_recheck_waits_on_freeze_written_after_locked_gate(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head_sha = "d" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=42,
        head_sha=head_sha,
    )
    initial_done_key = runner_helpers._initial_review_grace_done_key(42)
    settle_done_key = runner_helpers._non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=head_sha,
    )
    settle_started_key = runner_helpers._non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha=head_sha,
    )
    state = MonitorState(
        threads_addressed_ids={
            initial_done_key: "elapsed",
            settle_done_key: "elapsed",
        }
    )
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )
    original_merge_gate = runner._merge_gate_with_legacy_head_support
    merge_gate_calls = 0

    async def _write_freeze_after_locked_gate(*args: object, **kwargs: object) -> object:
        nonlocal merge_gate_calls
        merge_gate_calls += 1
        result = await original_merge_gate(*args, **kwargs)
        if merge_gate_calls == 3:
            async with factory() as session:
                workspace = await WorkspaceRepository(session).get(workspace_id)
                assert workspace is not None
                monitor_state = dict(workspace.monitor_threads_addressed or {})
                operator_hints.arm_operator_hint_freeze(
                    monitor_state,
                    pr_number=42,
                    head_sha=head_sha,
                    now=datetime.now(UTC),
                )
                workspace.monitor_threads_addressed = monitor_state
                await session.commit()
        return result

    monkeypatch.setattr(
        runner,
        "_merge_gate_with_legacy_head_support",
        _write_freeze_after_locked_gate,
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(head_sha=head_sha),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert settle_done_key not in state.threads_addressed_ids
    assert settle_started_key in state.threads_addressed_ids
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)


@pytest.mark.unit
async def test_merge_rechecks_initial_grace_after_visible_reviewer_freeze(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    head_sha = "e" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=42,
        head_sha=head_sha,
    )
    initial_done_key = runner_helpers._initial_review_grace_done_key(42)
    initial_started_key = runner_helpers._initial_review_grace_started_key(42)
    settle_done_key = runner_helpers._non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=head_sha,
    )
    stale_state = MonitorState(
        threads_addressed_ids={
            initial_done_key: "elapsed",
            settle_done_key: "elapsed",
        }
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        monitor_state = dict(workspace.monitor_threads_addressed or {})
        operator_hints.arm_operator_hint_freeze(
            monitor_state,
            pr_number=42,
            head_sha=head_sha,
            now=datetime.now(UTC),
        )
        workspace.monitor_threads_addressed = monitor_state
        await session.commit()

    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path,
        initial_review_grace_period_seconds=180,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(
            head_sha=head_sha,
            checks=(CheckTiming(name="greptile-apps", conclusion="SUCCESS"),),
        ),
        state=stale_state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert initial_done_key not in stale_state.threads_addressed_ids
    assert initial_started_key in stale_state.threads_addressed_ids
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
