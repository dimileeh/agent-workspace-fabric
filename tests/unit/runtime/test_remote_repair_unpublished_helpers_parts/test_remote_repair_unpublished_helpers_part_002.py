"""Hosted push-tracking reconcile regressions for unpublished-repair recovery (part 002)."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import FakeCommandRunner
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import remote_repair_unpublished
from tests.unit.runtime.test_remote_repair_unpublished_helpers_parts._helpers import (
    _ORPHANED_HOSTED_TERMINAL,
    _PUBLISHED_PR_HEAD,
    _allow_repair_prerequisites,
    _allow_repair_provenance,
    _hosted_orphan_monitor_state,
    _repair_runner,
    _repair_worktree,
)


@pytest.mark.unit
async def test_matching_heads_reconciles_orphaned_hosted_last_push_sha(
    tmp_path: Path,
) -> None:
    """Equality short-circuit must still clear orphaned hosted push-tracking.

    After a successful reset that crashes before ``_persist_state``, or when
    upgrading an already-affected workspace, local HEAD already equals the
    expected remote tip while ``last_push_sha`` still advertises the abandoned
    unpublished SHA. Reconcile on that verified-equality path so hosted
    identity cannot keep failing closed.
    """
    from awf.runtime.hosted_pr_identity import hosted_pr_identity_for_workspace

    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=remote,
        state=state,
    )
    assert result is None
    assert restored == remote
    assert state.last_push_sha == remote
    assert state.hosted_terminal_head_advanced is False
    workspace = SimpleNamespace(
        repo_url="https://github.com/example/repo",
        pr_url="https://github.com/example/repo/pull/1",
        pr_number=1,
        branch_base="main",
        remote_push_branch="awf/ws_repair",
        owned_paths=[],
        task_policy={},
        monitor_last_commit_sha=_ORPHANED_HOSTED_TERMINAL,
    )
    assert hosted_pr_identity_for_workspace(workspace, state=state)["expected_head_sha"] == remote


@pytest.mark.unit
async def test_matching_heads_race_preserves_orphaned_hosted_push_tracking(
    tmp_path: Path,
) -> None:
    """Stale local==expected must not reconcile when live HEAD advanced under race.

    Reset paths recheck HEAD under the writer lock before mutating state; the
    equality short-circuit must do the same so a concurrent writer cannot leave
    push-tracking rewound while the checkout already moved past the accepted tip.
    """
    worktree = _repair_worktree(tmp_path)
    published = _PUBLISHED_PR_HEAD
    advanced = "dd" * 20
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{advanced}\n")
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=published,
        local_head=published,
        state=state,
    )
    assert restored == published
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_reconcile_under_lock_require_clean_refuses_dirty_worktree(
    tmp_path: Path,
) -> None:
    worktree = _repair_worktree(tmp_path)
    accepted = _PUBLISHED_PR_HEAD
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{accepted}\n")
    cmd.queue_result(returncode=0, stdout=" M\0src/a.py\0")
    outcome = await remote_repair_unpublished._reconcile_push_tracking_under_live_equality_lock(
        cmd,
        worktree_path=worktree,
        expected_head=accepted,
        state=state,
        git_env={},
        require_clean=True,
    )
    assert outcome.reconciled is False
    assert outcome.worktree_dirty is True
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_abandon_unpublished_post_reset_race_preserves_hosted_push_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After reset unlocks, a concurrent HEAD advance must not clear hosted orphan markers."""
    _allow_repair_prerequisites(monkeypatch)
    _allow_repair_provenance(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = _ORPHANED_HOSTED_TERMINAL
    advanced = "dd" * 20
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/a.py\0")
    cmd.queue_result(returncode=0, stdout=f"{advanced}\n")

    async def _reset(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=True,
        )

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _reset,
    )
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=local,
        state=state,
    )
    assert restored == local
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_ROLLBACK_FAILED"
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_abandon_behind_remote_post_reset_race_preserves_hosted_push_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_repair_prerequisites(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = "aa" * 20
    advanced = "dd" * 20
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=f"{advanced}\n")

    async def _reset(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=True,
        )

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _reset,
    )
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=local,
        state=state,
    )
    assert restored == local
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_ROLLBACK_FAILED"
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_matching_heads_writer_lock_failure_preserves_push_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = _repair_worktree(tmp_path)
    published = _PUBLISHED_PR_HEAD
    state = _hosted_orphan_monitor_state()

    @contextlib.asynccontextmanager
    async def _lock_fails(_path: Path):  # type: ignore[no-untyped-def]
        raise OSError("lock unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(
        remote_repair_unpublished,
        "hold_exclusive_worktree_writer_lock",
        _lock_fails,
    )
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, FakeCommandRunner()),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=published,
        local_head=published,
        state=state,
    )
    assert restored == published
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_ROLLBACK_FAILED"
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_matching_heads_live_rev_parse_failure_preserves_push_tracking(
    tmp_path: Path,
) -> None:
    worktree = _repair_worktree(tmp_path)
    published = _PUBLISHED_PR_HEAD
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stdout="", stderr="rev-parse failed")
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=published,
        local_head=published,
        state=state,
    )
    assert restored == published
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_abandon_unpublished_reconciles_hosted_push_tracking_to_fetched_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted terminal sync + failed push must not leave last_push_sha orphaned.

    Reproduces production: state/last_push_sha=e7, local unpublished e7, fetched
    PR head=5c. After verified abandon reset, push-tracking and next hosted
    identity must advertise 5c (not the abandoned orphan).
    """
    from awf.runtime.hosted_pr_identity import hosted_pr_identity_for_workspace

    _allow_repair_prerequisites(monkeypatch)
    _allow_repair_provenance(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = _ORPHANED_HOSTED_TERMINAL
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/a.py\0")
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0, stdout="")

    async def _reset(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=True,
        )

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _reset,
    )
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=local,
        state=state,
    )
    assert result is None
    assert restored == remote
    assert state.last_push_sha == remote
    assert state.hosted_terminal_head_advanced is False

    workspace = SimpleNamespace(
        repo_url="https://github.com/example/repo",
        pr_url="https://github.com/example/repo/pull/1",
        pr_number=1,
        branch_base="main",
        remote_push_branch="awf/ws_repair",
        owned_paths=[],
        task_policy={},
        # Persist may still hold the orphan until the next _persist_state; identity
        # must prefer the reconciled in-memory MonitorState.
        monitor_last_commit_sha=_ORPHANED_HOSTED_TERMINAL,
    )
    identity = hosted_pr_identity_for_workspace(workspace, state=state)
    assert identity["expected_head_sha"] == remote


@pytest.mark.unit
async def test_abandon_unpublished_reconciles_even_when_event_append_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Event-append failure must not skip push-tracking reconcile after reset.

    Worktree is already at fetched PR head when events are written. Reconcile
    must run before append so same-cycle hosted identity is correct even if
    append raises. Append failure must still fail the cycle, stash a pending
    audit payload, and durably ``_persist_state`` before returning so a crash
    or finish-op fault cannot lose the retry marker (PRRT_kwDOSJAM6s6dy5TU).
    """
    import json

    _allow_repair_prerequisites(monkeypatch)
    _allow_repair_provenance(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = _ORPHANED_HOSTED_TERMINAL
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/a.py\0")
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0, stdout="")

    async def _reset(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=True,
        )

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _reset,
    )

    async def _append_raises(**_kwargs: object) -> None:
        raise RuntimeError("event sink unavailable")

    persisted: list[tuple[str, MonitorState]] = []

    async def _persist_state(workspace_id: str, persist_state: MonitorState) -> None:
        persisted.append((workspace_id, persist_state))

    runner = _repair_runner(tmp_path, cmd)
    runner._append_workspace_events = _append_raises
    runner._persist_state = _persist_state

    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=local,
        state=state,
    )
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_UNPUBLISHED_ABANDON_EVENT_FAILED"
    assert restored == remote
    assert state.last_push_sha == remote
    assert state.hosted_terminal_head_advanced is False
    pending = state.threads_addressed_ids.get(
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
    )
    assert pending is not None
    payload = json.loads(pending)
    assert payload["abandoned_local_head"] == local
    assert payload["restored_remote_head"] == remote
    # Stash must be durable before returning; otherwise a crash or
    # ``_finish_monitor_operation`` fault before the outer-loop persist loses the
    # retry marker and the abandonment audit is gone forever.
    assert persisted
    assert persisted[0] == ("ws_repair", state)
    assert remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY in (
        persisted[0][1].threads_addressed_ids
    )


@pytest.mark.unit
async def test_abandon_unpublished_stages_pending_before_cancellable_event_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CancelledError during event commit must not drop the abandonment audit.

    ``asyncio.CancelledError`` bypasses ``except Exception``. If the pending
    marker is only stashed in that handler, cancel after reset/reconcile while
    the event transaction awaits rolls back with no durable payload. On restart
    HEAD already equals remote and the equality path has nothing to flush
    (PRRT_kwDOSJAM6s6d0tKy). Stage the marker before the cancellable window.
    """
    import json

    _allow_repair_prerequisites(monkeypatch)
    _allow_repair_provenance(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = _ORPHANED_HOSTED_TERMINAL
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/a.py\0")
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0, stdout="")

    async def _reset(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=True,
        )

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _reset,
    )

    persisted: list[dict[str, object]] = []

    async def _persist_state(workspace_id: str, persist_state: MonitorState) -> None:
        assert workspace_id == "ws_repair"
        persisted.append(dict(persist_state.threads_addressed_ids))

    async def _append_cancelled(**_kwargs: object) -> None:
        raise asyncio.CancelledError()

    runner = _repair_runner(tmp_path, cmd)
    runner._append_workspace_events = _append_cancelled
    runner._persist_state = _persist_state

    with pytest.raises(asyncio.CancelledError):
        await remote_repair_unpublished._abandon_unpublished_comment_repairs(
            runner,
            workspace_id="ws_repair",
            worktree_path=worktree,
            remote_branch="fix/review",
            expected_remote_head=remote,
            local_head=local,
            state=state,
        )

    assert persisted, "pending abandon marker must be durably staged before event commit"
    pending_key = remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
    assert pending_key in persisted[0]
    payload = json.loads(str(persisted[0][pending_key]))
    assert payload["abandoned_local_head"] == local
    assert payload["restored_remote_head"] == remote
    assert pending_key in state.threads_addressed_ids


@pytest.mark.unit
async def test_matching_heads_flushes_pending_unpublished_abandon_event(
    tmp_path: Path,
) -> None:
    """Equality short-circuit must retry a stashed abandonment audit event."""
    import json

    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = _ORPHANED_HOSTED_TERMINAL
    state = MonitorState(last_push_sha=remote)
    pending_payload = {
        "abandoned_local_head": local,
        "restored_remote_head": remote,
        "abandoned_paths": ["src/a.py"],
        "rollback_strategy": "git_reset_hard_to_verified_remote_pr_head",
        "pushed": False,
    }
    state.threads_addressed_ids[
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
    ] = json.dumps(pending_payload)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    appended: list[object] = []
    persisted: list[tuple[str, MonitorState]] = []

    async def _append(*, workspace_id: str, events: list[object]) -> None:
        assert workspace_id == "ws_repair"
        appended.extend(events)

    async def _persist_state(workspace_id: str, persist_state: MonitorState) -> None:
        persisted.append((workspace_id, persist_state))

    runner = _repair_runner(tmp_path, cmd)
    runner._append_workspace_events = _append
    runner._persist_state = _persist_state

    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=remote,
        state=state,
    )
    assert result is None
    assert restored == remote
    assert len(appended) == 1
    event = appended[0]
    assert event.event_type == "monitor.comment_repair_unpublished_abandoned"
    assert event.payload == pending_payload
    assert (
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
        not in state.threads_addressed_ids
    )
    # Clear must be durable before returning; otherwise a crash before the
    # outer-loop persist keeps the DB marker and the next equality flush
    # appends a duplicate audit event (PRRT_kwDOSJAM6s6dzTXI).
    assert persisted == [("ws_repair", state)]
    assert (
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
        not in persisted[0][1].threads_addressed_ids
    )


@pytest.mark.unit
async def test_commit_unpublished_abandon_event_clears_pending_in_same_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Append + durable marker clear must share one commit (PRRT_kwDOSJAM6s6dzTXI)."""
    import json

    pending_key = remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
    event_payload = {
        "abandoned_local_head": _ORPHANED_HOSTED_TERMINAL,
        "restored_remote_head": _PUBLISHED_PR_HEAD,
        "abandoned_paths": ["src/a.py"],
        "rollback_strategy": "git_reset_hard_to_verified_remote_pr_head",
        "pushed": False,
    }
    state = MonitorState()
    state.threads_addressed_ids[pending_key] = json.dumps(event_payload)

    workspace = SimpleNamespace(
        id="ws_repair",
        status="monitoring_pr",
        monitor_threads_addressed={pending_key: state.threads_addressed_ids[pending_key]},
        events=[],
    )
    commits: list[str] = []
    add_events_calls: list[list[object]] = []

    class _Session:
        async def commit(self) -> None:
            commits.append("commit")

    class _SessionContext:
        async def __aenter__(self) -> _Session:
            return _Session()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Repository:
        def __init__(self, _session: object) -> None:
            pass

        async def get_for_update(self, workspace_id: str) -> object:
            assert workspace_id == "ws_repair"
            return workspace

        async def add_events(self, ws: object, *, events: list[object]) -> list[object]:
            assert ws is workspace
            # Marker must still be present when the event is written so a crash
            # before commit rolls back both the event and the clear.
            assert pending_key in workspace.monitor_threads_addressed
            assert commits == []
            add_events_calls.append(events)
            return []

    monkeypatch.setattr(remote_repair_unpublished, "WorkspaceRepository", _Repository)

    async def _append_must_not_run(**_kwargs: object) -> None:
        raise AssertionError("transactional path must not use _append_workspace_events")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(session_factory=_SessionContext),
        _append_workspace_events=_append_must_not_run,
    )

    await remote_repair_unpublished._commit_unpublished_abandon_event_and_clear_pending(
        runner,
        workspace_id="ws_repair",
        state=state,
        event_payload=event_payload,
    )

    assert len(add_events_calls) == 1
    assert add_events_calls[0][0].event_type == "monitor.comment_repair_unpublished_abandoned"
    assert add_events_calls[0][0].payload == event_payload
    assert pending_key not in workspace.monitor_threads_addressed
    assert commits == ["commit"]
    assert pending_key not in state.threads_addressed_ids


@pytest.mark.unit
async def test_matching_heads_propagates_pending_abandon_event_flush_failure(
    tmp_path: Path,
) -> None:
    """Failed pending-event flush must not clear the marker or report success."""
    import json

    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    state = MonitorState(last_push_sha=remote)
    state.threads_addressed_ids[
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
    ] = json.dumps(
        {
            "abandoned_local_head": _ORPHANED_HOSTED_TERMINAL,
            "restored_remote_head": remote,
            "abandoned_paths": ["src/a.py"],
            "rollback_strategy": "git_reset_hard_to_verified_remote_pr_head",
            "pushed": False,
        }
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")

    async def _append_raises(**_kwargs: object) -> None:
        raise RuntimeError("event sink still unavailable")

    runner = _repair_runner(tmp_path, cmd)
    runner._append_workspace_events = _append_raises

    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=remote,
        state=state,
    )
    assert restored == remote
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_UNPUBLISHED_ABANDON_EVENT_FAILED"
    assert (
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
        in state.threads_addressed_ids
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "failure_kind",
    ["dirty", "head_race", "reset_failure", "verification_failure"],
)
async def test_abandon_unpublished_leaves_push_tracking_on_failed_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    _allow_repair_prerequisites(monkeypatch)
    _allow_repair_provenance(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = _ORPHANED_HOSTED_TERMINAL
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/a.py\0")
    if failure_kind == "verification_failure":
        cmd.queue_result(returncode=1, stderr="verify failed")
        cmd.queue_result(returncode=0, stdout="")

    async def _reset(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        if failure_kind == "dirty":
            return remote_repair_unpublished._RecoveryResetOutcome(
                ready=False,
                live_head=local,
                worktree_dirty=True,
                reset_ok=False,
            )
        if failure_kind == "head_race":
            return remote_repair_unpublished._RecoveryResetOutcome(
                ready=False,
                live_head="aa" * 20,
                worktree_dirty=False,
                reset_ok=False,
            )
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=failure_kind != "reset_failure",
            reset_stderr="reset failed" if failure_kind == "reset_failure" else "",
        )

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _reset,
    )
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=local,
        state=state,
    )
    assert restored == local
    assert result is not None
    assert result.failed is True
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_abandon_behind_remote_ff_reconciles_hosted_push_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_repair_prerequisites(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = "aa" * 20
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0, stdout="")

    async def _reset(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=True,
        )

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _reset,
    )
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=local,
        state=state,
    )
    assert result is None
    assert restored == remote
    assert state.last_push_sha == remote
    assert state.hosted_terminal_head_advanced is False


@pytest.mark.unit
async def test_abandon_behind_remote_ff_flushes_pending_unpublished_abandon_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behind-remote FF success must retry a stashed abandonment audit event.

    Regression for PRRT_kwDOSJAM6s6dzTXE: after an abandon-event failure leaves
    the durable retry marker, a later cycle can take the behind-remote
    fast-forward path (remote advanced) and must flush before returning success
    — otherwise a subsequent repair that clears actionable threads can leave
    the audit permanently un-emitted.
    """
    import json

    _allow_repair_prerequisites(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = "aa" * 20
    pending_payload = {
        "abandoned_local_head": _ORPHANED_HOSTED_TERMINAL,
        "restored_remote_head": remote,
        "abandoned_paths": ["src/a.py"],
        "rollback_strategy": "git_reset_hard_to_verified_remote_pr_head",
        "pushed": False,
    }
    state = MonitorState(last_push_sha=remote)
    state.threads_addressed_ids[
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
    ] = json.dumps(pending_payload)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0, stdout="")
    appended: list[object] = []

    async def _append(*, workspace_id: str, events: list[object]) -> None:
        assert workspace_id == "ws_repair"
        appended.extend(events)

    async def _persist_state(workspace_id: str, persist_state: MonitorState) -> None:
        del workspace_id, persist_state

    async def _reset(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=True,
        )

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _reset,
    )
    runner = _repair_runner(tmp_path, cmd)
    runner._append_workspace_events = _append
    runner._persist_state = _persist_state

    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=local,
        state=state,
    )
    assert result is None
    assert restored == remote
    assert len(appended) == 1
    event = appended[0]
    assert event.event_type == "monitor.comment_repair_unpublished_abandoned"
    assert event.payload == pending_payload
    assert (
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
        not in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_abandon_behind_remote_ff_propagates_pending_abandon_event_flush_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed pending-event flush on FF success must preserve the marker."""
    import json

    _allow_repair_prerequisites(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = "aa" * 20
    state = MonitorState(last_push_sha=remote)
    state.threads_addressed_ids[
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
    ] = json.dumps(
        {
            "abandoned_local_head": _ORPHANED_HOSTED_TERMINAL,
            "restored_remote_head": remote,
            "abandoned_paths": ["src/a.py"],
            "rollback_strategy": "git_reset_hard_to_verified_remote_pr_head",
            "pushed": False,
        }
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=0, stdout="")

    async def _append_raises(**_kwargs: object) -> None:
        raise RuntimeError("event sink still unavailable")

    async def _reset(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=True,
        )

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _reset,
    )
    runner = _repair_runner(tmp_path, cmd)
    runner._append_workspace_events = _append_raises

    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=local,
        state=state,
    )
    assert restored == remote
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_UNPUBLISHED_ABANDON_EVENT_FAILED"
    assert (
        remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
        in state.threads_addressed_ids
    )


@pytest.mark.unit
@pytest.mark.parametrize("failure_kind", ["dirty", "head_race", "reset_failure"])
async def test_abandon_behind_remote_ff_leaves_push_tracking_on_refused_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    _allow_repair_prerequisites(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    local = "aa" * 20
    state = _hosted_orphan_monitor_state()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0)

    async def _reset(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        if failure_kind == "dirty":
            return remote_repair_unpublished._RecoveryResetOutcome(
                ready=False,
                live_head=local,
                worktree_dirty=True,
                reset_ok=False,
            )
        if failure_kind == "head_race":
            return remote_repair_unpublished._RecoveryResetOutcome(
                ready=False,
                live_head="dd" * 20,
                worktree_dirty=False,
                reset_ok=False,
            )
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=False,
            reset_stderr="reset failed",
        )

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _reset,
    )
    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _repair_runner(tmp_path, cmd),
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=local,
        state=state,
    )
    assert restored == local
    assert result is not None
    assert result.failed is True
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True
