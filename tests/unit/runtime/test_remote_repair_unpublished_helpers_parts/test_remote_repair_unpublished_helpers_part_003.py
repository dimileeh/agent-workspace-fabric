"""Abandon-audit edge and post-recovery writer-lock regressions (part 003)."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

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
@pytest.mark.parametrize(
    "corrupt_value",
    ["{not-json", json.dumps(["not", "a", "dict"]), json.dumps("string")],
)
async def test_matching_heads_drops_corrupt_pending_abandon_event_marker(
    tmp_path: Path,
    corrupt_value: str,
) -> None:
    """Corrupt durable retry markers must be cleared so equality cannot wedge.

    Invalid JSON and non-object JSON both fail payload parse; flush must drop the
    marker and persist the clear before returning success.
    """
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    state = MonitorState(last_push_sha=remote)
    pending_key = remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
    state.threads_addressed_ids[pending_key] = corrupt_value
    # Force the defensive non-set branch in ``_clear_pending_...``.
    object.__setattr__(state, "_changed_thread_ids", None)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")
    persisted: list[tuple[str, MonitorState]] = []

    async def _persist_state(workspace_id: str, persist_state: MonitorState) -> None:
        persisted.append((workspace_id, persist_state))

    runner = _repair_runner(tmp_path, cmd)
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
    assert pending_key not in state.threads_addressed_ids
    assert persisted == [("ws_repair", state)]


@pytest.mark.unit
async def test_matching_heads_drops_corrupt_pending_marker_without_persist_hook(
    tmp_path: Path,
) -> None:
    """Corrupt markers clear in memory even when the runner has no ``_persist_state``.

    Unit stubs and degraded runners may omit durable persistence; flush must still
    drop the bad marker so equality reconciliation cannot wedge forever.
    """
    worktree = _repair_worktree(tmp_path)
    remote = _PUBLISHED_PR_HEAD
    state = MonitorState(last_push_sha=remote)
    pending_key = remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
    state.threads_addressed_ids[pending_key] = "{not-json"
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{remote}\n")

    runner = _repair_runner(tmp_path, cmd)
    # Explicitly drop the persist hook so the flush takes the non-callable arm.
    if hasattr(runner, "_persist_state"):
        delattr(runner, "_persist_state")

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
    assert pending_key not in state.threads_addressed_ids


@pytest.mark.unit
async def test_commit_unpublished_abandon_event_clears_pending_when_workspace_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transactional flush must clear the in-memory marker if the workspace row vanished."""
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
    commits: list[str] = []
    add_events_calls: list[list[object]] = []
    warning_calls: list[tuple[str, dict[str, object]]] = []

    def _warning(event: str, **kwargs: object) -> None:
        warning_calls.append((event, kwargs))

    monkeypatch.setattr(remote_repair_unpublished._log, "warning", _warning)

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

        async def get_for_update(self, workspace_id: str) -> object | None:
            assert workspace_id == "ws_repair"
            missing_workspace: object | None = None
            return missing_workspace

        async def add_events(self, *_args: object, **_kwargs: object) -> list[object]:
            add_events_calls.append([])
            return []

    monkeypatch.setattr(remote_repair_unpublished, "WorkspaceRepository", _Repository)

    runner = SimpleNamespace(
        _deps=SimpleNamespace(session_factory=_SessionContext),
    )

    await remote_repair_unpublished._commit_unpublished_abandon_event_and_clear_pending(
        runner,
        workspace_id="ws_repair",
        state=state,
        event_payload=event_payload,
    )

    assert pending_key not in state.threads_addressed_ids
    assert add_events_calls == []
    assert commits == []
    assert warning_calls == [
        (
            "monitor.comment_repair_unpublished_abandoned_event_dropped",
            {
                "workspace_id": "ws_repair",
                "reason_code": remote_repair_unpublished._COMMENT_REPAIR_UNPUBLISHED_ABANDONED,
                "reason": "workspace_row_missing",
            },
        )
    ]


@pytest.mark.unit
async def test_flush_pending_unpublished_abandon_event_propagates_programming_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Narrowed DB/sink catches must not swallow TypeError into a silent retry."""
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

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise TypeError("programming error in abandon flush")

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_commit_unpublished_abandon_event_and_clear_pending",
        _boom,
    )

    with pytest.raises(TypeError, match="programming error in abandon flush"):
        await remote_repair_unpublished._flush_pending_unpublished_abandon_event(
            SimpleNamespace(),
            workspace_id="ws_repair",
            state=state,
        )

    assert pending_key in state.threads_addressed_ids


@pytest.mark.unit
async def test_published_head_stale_snapshot_propagates_pending_abandon_flush_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Already-published local HEAD on a stale snapshot must still fail closed on flush."""
    _allow_repair_prerequisites(monkeypatch)
    worktree = _repair_worktree(tmp_path)
    stale_snapshot = "aa" * 20
    published = _PUBLISHED_PR_HEAD
    pending_key = remote_repair_unpublished._UNPUBLISHED_ABANDON_EVENT_PENDING_KEY
    state = MonitorState(last_push_sha=published)
    state.threads_addressed_ids[pending_key] = json.dumps(
        {
            "abandoned_local_head": _ORPHANED_HOSTED_TERMINAL,
            "restored_remote_head": published,
            "abandoned_paths": ["src/a.py"],
            "rollback_strategy": "git_reset_hard_to_verified_remote_pr_head",
            "pushed": False,
        }
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{published}\n")
    # merge-base --is-ancestor stale → published
    cmd.queue_result(returncode=0)

    async def _append_raises(**_kwargs: object) -> None:
        raise SQLAlchemyError("event sink unavailable")

    runner = _repair_runner(tmp_path, cmd)
    runner._append_workspace_events = _append_raises

    restored, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id="ws_repair",
        worktree_path=worktree,
        remote_branch="fix/review",
        expected_remote_head=stale_snapshot,
        local_head=published,
        state=state,
    )
    assert restored == published
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_UNPUBLISHED_ABANDON_EVENT_FAILED"
    assert pending_key in state.threads_addressed_ids


@pytest.mark.unit
async def test_abandon_unpublished_post_reset_writer_lock_failure_preserves_push_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-reset reconcile lock failure must not mutate hosted push-tracking."""
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

    async def _reset(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=True,
        )

    @contextlib.asynccontextmanager
    async def _lock_fails(_path: Path):  # type: ignore[no-untyped-def]
        raise OSError("lock unavailable after reset")
        yield  # pragma: no cover

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _reset,
    )
    monkeypatch.setattr(
        remote_repair_unpublished,
        "hold_exclusive_worktree_writer_lock",
        _lock_fails,
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
    assert "writer lock" in result.stderr.lower()
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_abandon_behind_remote_ff_post_reset_writer_lock_failure_preserves_push_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-FF reconcile lock failure must not mutate hosted push-tracking."""
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
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=local,
            worktree_dirty=False,
            reset_ok=True,
        )

    @contextlib.asynccontextmanager
    async def _lock_fails(_path: Path):  # type: ignore[no-untyped-def]
        raise OSError("lock unavailable after fast-forward")
        yield  # pragma: no cover

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _reset,
    )
    monkeypatch.setattr(
        remote_repair_unpublished,
        "hold_exclusive_worktree_writer_lock",
        _lock_fails,
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
    assert "writer lock" in result.stderr.lower()
    assert "fast-forward" in result.stderr.lower()
    assert state.last_push_sha == _ORPHANED_HOSTED_TERMINAL
    assert state.hosted_terminal_head_advanced is True
