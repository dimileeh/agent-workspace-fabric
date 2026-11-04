"""PR head not synchronized by the forge after an accepted push (#1167).

GitHub sometimes accepts a push to the PR head branch but never synchronizes the
pull request: the branch tip moves to AWF's commit while the PR keeps reporting
the old ``headRefOid`` (and, after a sync-base push, ``DIRTY``). The monitor used
to count every later no-op SyncBase as no progress and abort with
``merge_conflict_not_reproduced``.

The runner now gathers the remote-tip evidence (``observe_pr_head_sync``) and
stamps it on ``PRStatus``; the pure ``decide`` waits within a bounded grace window
and then parks for a human with ``PR_HEAD_NOT_SYNCHRONIZED``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.enums import OperationType, WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    Abort,
    AbortReason,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ShortCircuitCompleted,
    SyncBase,
    WaitForCI,
    decide,
    sync_base_no_progress_signature,
)
from awf.runtime.pr_monitor_actions import (
    PR_HEAD_NOT_SYNCHRONIZED_REASON,
    PR_HEAD_SYNC_REMEDY,
)
from awf.runtime.pr_monitor_runner import loop
from awf.runtime.pr_monitor_runner.constants import (
    _SYNC_BASE_NO_PROGRESS_COUNT_KEY,
    _SYNC_BASE_NO_PROGRESS_SIGNATURE_KEY,
)
from awf.runtime.pr_monitor_runner.pr_head_sync import (
    PR_HEAD_NOT_SYNCHRONIZED_EVENT,
    observe_pr_head_sync,
    pr_head_sync_first_seen_key,
    pr_head_sync_parked_key,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)

_OLD_HEAD = "4ac31db6f749f18652716e9c83e24f130524205d"
_PUSHED_HEAD = "2bb16a374667250e685e5bc326d033039f37885d"
_OTHER_HEAD = "9f0e1d2c3b4a59687766554433221100ffeeddcc"
_MERGED_BASE = "b" * 40
_NOW = datetime(2026, 11, 4, 9, 0, 0, tzinfo=UTC)
_GRACE = 600.0
_PR_HEAD_SYNC_KEY_PREFIX = "__awf_pr_head_sync"


def _dirty_status(**overrides: Any) -> PRStatus:
    fields: dict[str, Any] = {
        "number": 42,
        "head_sha": _OLD_HEAD,
        "mergeable": MergeableState.CONFLICTING,
        "check_state": CheckState.SUCCESS,
        "unresolved_inline_threads": (),
        "unresolved_review_comments": (),
        "base_behind_count": 0,
        "merge_state_status": MergeStateStatus.DIRTY,
    }
    fields.update(overrides)
    return PRStatus(**fields)


def _exhausted_state(status: PRStatus, **overrides: Any) -> MonitorState:
    """No-progress SyncBase state already at the abort threshold for ``status``."""
    fields: dict[str, Any] = {
        "sync_base_no_progress_signature": sync_base_no_progress_signature(status),
        "sync_base_no_progress_count": 3,
    }
    fields.update(overrides)
    return MonitorState(**fields)


def _pr_head_sync_keys(threads_addressed: dict[str, str]) -> list[str]:
    return [key for key in threads_addressed if key.startswith(_PR_HEAD_SYNC_KEY_PREFIX)]


# ── A. Pure decide() gate ───────────────────────────────────────────────────


class TestDecidePrHeadNotSynchronized:
    @pytest.mark.unit
    def test_unsynchronized_pr_head_within_grace_waits_instead_of_counting_no_progress(
        self,
    ) -> None:
        status = _dirty_status(
            unsynchronized_remote_head_sha=_PUSHED_HEAD,
            pr_head_sync_grace_active=True,
        )
        action = decide(status, _exhausted_state(status), MonitorConfig())

        assert action == WaitForCI(reason="pr_head_not_synchronized")

    @pytest.mark.unit
    def test_unsynchronized_pr_head_past_grace_parks_for_human_not_abort(self) -> None:
        status = _dirty_status(unsynchronized_remote_head_sha=_PUSHED_HEAD)
        action = decide(status, _exhausted_state(status), MonitorConfig())

        assert isinstance(action, NotifyHuman)
        assert action.reason_code == PR_HEAD_NOT_SYNCHRONIZED_REASON
        assert action.message is not None
        assert _PUSHED_HEAD in action.message
        assert _OLD_HEAD in action.message
        assert PR_HEAD_SYNC_REMEDY in action.message
        # Closing the PR ends monitoring (gate 0), so the remedy carries a caution.
        assert "reopen it right away" in action.message
        assert PR_HEAD_SYNC_REMEDY == (
            "GitHub usually re-synchronizes on a new push, or on closing and reopening the PR"
        )

    @pytest.mark.unit
    def test_genuinely_unreproducible_conflict_still_aborts_merge_conflict_not_reproduced(
        self,
    ) -> None:
        # Merge clean locally, PR head == local HEAD (so the runner stamps
        # nothing), PR still DIRTY after the no-progress budget → same abort as
        # before #1167.
        status = _dirty_status()
        action = decide(status, _exhausted_state(status), MonitorConfig())

        assert action == Abort(reason=AbortReason.merge_conflict_not_reproduced)

    @pytest.mark.unit
    def test_merged_or_closed_pr_outranks_unsynchronized_head(self) -> None:
        stamped = _dirty_status(unsynchronized_remote_head_sha=_PUSHED_HEAD)

        merged = decide(replace(stamped, merged=True), MonitorState(), MonitorConfig())
        closed = decide(replace(stamped, closed=True), MonitorState(), MonitorConfig())

        assert isinstance(merged, ShortCircuitCompleted)
        assert closed == Abort(reason=AbortReason.pr_closed_externally)

    @pytest.mark.unit
    def test_default_notify_human_carries_no_reason_code(self) -> None:
        assert NotifyHuman().reason_code is None


# ── B. Runner probe: observe_pr_head_sync ───────────────────────────────────


class _ProbeRunner:
    """Minimal runner surface ``observe_pr_head_sync`` touches."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        local_head: str | None,
        grace_seconds: float = _GRACE,
    ) -> None:
        self._worktrees_root = tmp_path
        self._config = MonitorConfig(pr_head_sync_grace_seconds=grace_seconds)
        self.now = _NOW
        self.cmd = FakeCommandRunner()
        self._deps = SimpleNamespace(runner=self.cmd, now=lambda: self.now)
        self.local_head = local_head
        self.rev_parse_calls: list[Path] = []
        self.persisted: list[dict[str, str]] = []
        self.events: list[Any] = []
        self.monitor_logs: list[dict[str, Any]] = []

    async def _rev_parse_head(self, worktree_path: Path) -> str | None:
        self.rev_parse_calls.append(worktree_path)
        return self.local_head

    async def _persist_state(self, _workspace_id: str, state: MonitorState) -> None:
        self.persisted.append(dict(state.threads_addressed_ids))

    async def _append_workspace_events(self, *, workspace_id: str, events: list[Any]) -> None:
        del workspace_id
        self.events.extend(events)

    async def _write_monitor_log(self, _sink: object, payload: dict[str, Any]) -> None:
        self.monitor_logs.append(payload)

    def ls_remote_calls(self) -> list[list[str]]:
        return [call.args for call in self.cmd.calls if "ls-remote" in call.args]


async def _observe(
    runner: _ProbeRunner,
    status: PRStatus,
    state: MonitorState,
    *,
    remote_push_url: str | None = None,
) -> PRStatus:
    return await observe_pr_head_sync(
        runner,
        workspace_id="ws_sync",
        status=status,
        state=state,
        remote_branch="awf/ws_sync",
        remote_push_url=remote_push_url,
        monitor_log=None,
    )


def _ls_remote_stdout(sha: str, branch: str = "awf/ws_sync") -> str:
    return f"{sha}\trefs/heads/{branch}\n"


class TestObservePrHeadSync:
    @pytest.mark.unit
    async def test_not_suspicious_runs_no_commands_and_returns_status_unchanged(
        self, tmp_path: Path
    ) -> None:
        runner = _ProbeRunner(tmp_path, local_head=_PUSHED_HEAD)
        status = _dirty_status()

        result = await _observe(runner, status, MonitorState())

        assert result is status
        assert runner.rev_parse_calls == []
        assert runner.cmd.calls == []
        assert runner.persisted == []

    @pytest.mark.unit
    async def test_accepted_push_with_stuck_pr_head_stamps_within_grace(
        self, tmp_path: Path
    ) -> None:
        runner = _ProbeRunner(tmp_path, local_head=_PUSHED_HEAD)
        runner.cmd.queue_result(stdout=_ls_remote_stdout(_PUSHED_HEAD))
        state = MonitorState(last_push_sha=_PUSHED_HEAD)

        result = await _observe(runner, _dirty_status(), state)

        assert result.unsynchronized_remote_head_sha == _PUSHED_HEAD
        assert result.pr_head_sync_grace_active is True
        assert runner.rev_parse_calls == [tmp_path / "ws_sync"]
        [ls_remote] = runner.ls_remote_calls()
        assert ls_remote[-2:] == ["origin", "refs/heads/awf/ws_sync"]
        # The first-seen marker is persisted: run() reloads state every poll.
        key = pr_head_sync_first_seen_key(_PUSHED_HEAD)
        assert float(state.threads_addressed_ids[key]) == pytest.approx(_NOW.timestamp())
        assert runner.persisted and key in runner.persisted[-1]
        assert runner.events == []

    @pytest.mark.unit
    async def test_ls_remote_targets_the_push_remote_when_set(self, tmp_path: Path) -> None:
        runner = _ProbeRunner(tmp_path, local_head=_PUSHED_HEAD)
        runner.cmd.queue_result(stdout=_ls_remote_stdout(_PUSHED_HEAD))

        await _observe(
            runner,
            _dirty_status(),
            MonitorState(last_push_sha=_PUSHED_HEAD),
            remote_push_url="https://github.com/fork/repo.git",
        )

        [ls_remote] = runner.ls_remote_calls()
        assert ls_remote[-2:] == ["https://github.com/fork/repo.git", "refs/heads/awf/ws_sync"]

    @pytest.mark.unit
    async def test_accepted_push_with_pr_head_stuck_past_grace_parks_once(
        self, tmp_path: Path
    ) -> None:
        runner = _ProbeRunner(tmp_path, local_head=_PUSHED_HEAD)
        runner.cmd.respond_when(
            lambda args: "ls-remote" in args, stdout=_ls_remote_stdout(_PUSHED_HEAD)
        )
        state = _exhausted_state(_dirty_status(), last_push_sha=_PUSHED_HEAD)

        first = await _observe(runner, _dirty_status(), state)
        assert first.pr_head_sync_grace_active is True

        runner.now = _NOW + timedelta(seconds=_GRACE + 1)
        expired = await _observe(runner, _dirty_status(), state)

        assert expired.unsynchronized_remote_head_sha == _PUSHED_HEAD
        assert expired.pr_head_sync_grace_active is False
        [event] = runner.events
        assert event.event_type == PR_HEAD_NOT_SYNCHRONIZED_EVENT
        assert event.reason_code == PR_HEAD_NOT_SYNCHRONIZED_REASON
        assert event.payload["remote_branch"] == "awf/ws_sync"
        assert event.payload["remote_branch_head_sha"] == _PUSHED_HEAD
        assert event.payload["pr_head_sha"] == _OLD_HEAD
        assert event.payload["remedy"] == PR_HEAD_SYNC_REMEDY
        assert event.payload["grace_seconds"] == _GRACE
        assert pr_head_sync_parked_key(_PUSHED_HEAD) in runner.persisted[-1]
        assert runner.monitor_logs[-1]["event"] == PR_HEAD_NOT_SYNCHRONIZED_EVENT

        # Later polls keep parking without a duplicate event.
        runner.now = _NOW + timedelta(seconds=_GRACE + 120)
        again = await _observe(runner, _dirty_status(), state)
        assert again.pr_head_sync_grace_active is False
        assert len(runner.events) == 1

        # The decision core parks for a human instead of aborting.
        action = decide(expired, state, MonitorConfig())
        assert isinstance(action, NotifyHuman)
        assert action.reason_code == PR_HEAD_NOT_SYNCHRONIZED_REASON

    @pytest.mark.unit
    async def test_non_positive_grace_parks_on_first_observation(self, tmp_path: Path) -> None:
        runner = _ProbeRunner(tmp_path, local_head=_PUSHED_HEAD, grace_seconds=0)
        runner.cmd.queue_result(stdout=_ls_remote_stdout(_PUSHED_HEAD))

        result = await _observe(runner, _dirty_status(), MonitorState(last_push_sha=_PUSHED_HEAD))

        assert result.unsynchronized_remote_head_sha == _PUSHED_HEAD
        assert result.pr_head_sync_grace_active is False
        assert len(runner.events) == 1

    @pytest.mark.unit
    async def test_pr_head_catching_up_within_grace_clears_markers_and_resumes(
        self, tmp_path: Path
    ) -> None:
        runner = _ProbeRunner(tmp_path, local_head=_PUSHED_HEAD)
        runner.cmd.queue_result(stdout=_ls_remote_stdout(_PUSHED_HEAD))
        state = MonitorState(last_push_sha=_PUSHED_HEAD)
        stuck = await _observe(runner, _dirty_status(), state)
        assert stuck.pr_head_sync_grace_active is True

        # GitHub synchronized the PR: it now reports the pushed head, BEHIND.
        runner.now = _NOW + timedelta(seconds=30)
        caught_up = _dirty_status(
            head_sha=_PUSHED_HEAD,
            mergeable=MergeableState.MERGEABLE,
            merge_state_status=MergeStateStatus.BEHIND,
            base_behind_count=1,
        )
        calls_before = len(runner.cmd.calls)
        result = await _observe(runner, caught_up, state)

        assert result is caught_up
        assert len(runner.cmd.calls) == calls_before  # no probe for a synced head
        assert _pr_head_sync_keys(state.threads_addressed_ids) == []
        assert _pr_head_sync_keys(runner.persisted[-1]) == []
        assert runner.events == []
        # Normal flow continues.
        assert isinstance(decide(result, state, MonitorConfig()), SyncBase)

    @pytest.mark.unit
    async def test_remote_tip_moved_elsewhere_is_not_a_stall(self, tmp_path: Path) -> None:
        # Somebody else pushed: the branch tip is neither AWF's HEAD nor the PR head.
        runner = _ProbeRunner(tmp_path, local_head=_PUSHED_HEAD)
        runner.cmd.queue_result(stdout=_ls_remote_stdout(_OTHER_HEAD))
        state = MonitorState(
            last_push_sha=_PUSHED_HEAD,
            threads_addressed_ids={pr_head_sync_first_seen_key(_PUSHED_HEAD): "1.0"},
        )

        status = _dirty_status()
        result = await _observe(runner, status, state)

        assert result is status
        assert state.threads_addressed_ids == {}
        assert runner.persisted == [{}]

    @pytest.mark.unit
    async def test_local_head_equal_to_pr_head_skips_ls_remote(self, tmp_path: Path) -> None:
        runner = _ProbeRunner(tmp_path, local_head=_OLD_HEAD.upper())
        status = _dirty_status()

        result = await _observe(runner, status, MonitorState(last_push_sha=_PUSHED_HEAD))

        assert result is status
        assert runner.ls_remote_calls() == []

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("local_head", "ls_remote_rc", "ls_remote_stdout"),
        [
            (None, 0, _ls_remote_stdout(_PUSHED_HEAD)),
            (_PUSHED_HEAD, 128, ""),
            (_PUSHED_HEAD, 0, ""),
            (_PUSHED_HEAD, 0, "not-a-sha\trefs/heads/awf/ws_sync\n"),
            (_PUSHED_HEAD, 0, _ls_remote_stdout(_PUSHED_HEAD, branch="awf/other")),
        ],
    )
    async def test_probe_failure_after_an_accepted_push_waits_instead_of_counting_no_progress(
        self,
        tmp_path: Path,
        local_head: str | None,
        ls_remote_rc: int,
        ls_remote_stdout: str,
    ) -> None:
        # AWF's push was accepted but the PR head differs and the remote tip is
        # unknown: a stale-snapshot SyncBase would spend the no-progress budget.
        runner = _ProbeRunner(tmp_path, local_head=local_head)
        runner.cmd.queue_result(returncode=ls_remote_rc, stdout=ls_remote_stdout, stderr="boom")
        status = _dirty_status()
        state = _exhausted_state(status, last_push_sha=_PUSHED_HEAD)

        with structlog.testing.capture_logs() as captured:
            result = await _observe(runner, status, state)

        assert result.unsynchronized_remote_head_sha == _PUSHED_HEAD
        assert result.pr_head_sync_grace_active is True
        assert runner.events == []
        assert any(r.get("event") == "monitor.pr_head_sync_probe_failed" for r in captured)
        assert decide(result, state, MonitorConfig()) == WaitForCI(
            reason="pr_head_not_synchronized"
        )

    @pytest.mark.unit
    async def test_probe_failing_past_grace_after_an_accepted_push_parks_not_aborts(
        self, tmp_path: Path
    ) -> None:
        runner = _ProbeRunner(tmp_path, local_head=_PUSHED_HEAD)
        runner.cmd.respond_when(lambda args: "ls-remote" in args, returncode=128, stderr="boom")
        status = _dirty_status()
        state = _exhausted_state(status, last_push_sha=_PUSHED_HEAD)
        await _observe(runner, status, state)

        runner.now = _NOW + timedelta(seconds=_GRACE + 1)
        result = await _observe(runner, status, state)

        assert result.unsynchronized_remote_head_sha == _PUSHED_HEAD
        assert result.pr_head_sync_grace_active is False
        [event] = runner.events
        assert event.reason_code == PR_HEAD_NOT_SYNCHRONIZED_REASON
        action = decide(result, state, MonitorConfig())
        assert isinstance(action, NotifyHuman)
        assert action.reason_code == PR_HEAD_NOT_SYNCHRONIZED_REASON

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("local_head", "state_fields"),
        [
            # Re-adoption no-op SyncBase with no AWF push recorded.
            (_PUSHED_HEAD, {"last_push_sha": None, "sync_base_no_progress_count": 1}),
            # Local HEAD moved past the accepted push: the push is not the tip.
            (_OTHER_HEAD, {"last_push_sha": _PUSHED_HEAD}),
        ],
    )
    async def test_probe_failure_without_accepted_push_evidence_leaves_status_unchanged(
        self, tmp_path: Path, local_head: str, state_fields: dict[str, Any]
    ) -> None:
        runner = _ProbeRunner(tmp_path, local_head=local_head)
        runner.cmd.queue_result(returncode=128, stderr="boom")
        state = MonitorState(**state_fields)
        status = _dirty_status()

        with structlog.testing.capture_logs() as captured:
            result = await _observe(runner, status, state)

        assert result is status
        assert state.threads_addressed_ids == {}
        assert runner.events == []
        assert any(r.get("event") == "monitor.pr_head_sync_probe_failed" for r in captured)

    @pytest.mark.unit
    @pytest.mark.parametrize("local_head", [None, _PUSHED_HEAD])
    async def test_probe_failure_during_an_observed_stall_keeps_the_stall_stamped(
        self, tmp_path: Path, local_head: str | None
    ) -> None:
        # A transient probe failure mid-stall must not fall back to SyncBase:
        # each no-op SyncBase would count toward merge_conflict_not_reproduced.
        runner = _ProbeRunner(tmp_path, local_head=local_head)
        runner.cmd.queue_result(returncode=128, stderr="remote hung up")
        first_seen = (_NOW - timedelta(seconds=_GRACE + 60)).timestamp()
        state = _exhausted_state(
            _dirty_status(),
            last_push_sha=_PUSHED_HEAD,
            threads_addressed_ids={
                pr_head_sync_first_seen_key(_PUSHED_HEAD): f"{first_seen:.6f}",
                pr_head_sync_parked_key(_PUSHED_HEAD): "1",
            },
        )

        result = await _observe(runner, _dirty_status(), state)

        assert result.unsynchronized_remote_head_sha == _PUSHED_HEAD
        assert result.pr_head_sync_grace_active is False
        assert runner.events == []  # already parked for this tip
        assert isinstance(decide(result, state, MonitorConfig()), NotifyHuman)

    @pytest.mark.unit
    async def test_probe_failure_after_the_pr_caught_up_does_not_stamp(
        self, tmp_path: Path
    ) -> None:
        runner = _ProbeRunner(tmp_path, local_head=None)
        caught_up = _dirty_status(head_sha=_PUSHED_HEAD)
        state = MonitorState(
            last_push_sha=_OTHER_HEAD,
            threads_addressed_ids={pr_head_sync_first_seen_key(_PUSHED_HEAD): "1.0"},
        )

        assert await _observe(runner, caught_up, state) is caught_up

    @pytest.mark.unit
    async def test_ls_remote_is_time_bounded(self, tmp_path: Path) -> None:
        runner = _ProbeRunner(tmp_path, local_head=_PUSHED_HEAD)
        runner.cmd.queue_result(stdout=_ls_remote_stdout(_PUSHED_HEAD))

        await _observe(runner, _dirty_status(), MonitorState(last_push_sha=_PUSHED_HEAD))

        [call] = [c for c in runner.cmd.calls if "ls-remote" in c.args]
        assert call.timeout_seconds is not None and call.timeout_seconds > 0

    @pytest.mark.unit
    async def test_readoption_after_resync_is_suspicious_without_a_fresh_push(
        self, tmp_path: Path
    ) -> None:
        # Adoption 2 of the incident: a non-fast-forward resync moved the
        # worktree to the already-pushed merge commit, then a no-op SyncBase.
        runner = _ProbeRunner(tmp_path, local_head=_PUSHED_HEAD)
        runner.cmd.queue_result(stdout=_ls_remote_stdout(_PUSHED_HEAD))
        status = _dirty_status()
        state = MonitorState(
            last_push_sha=_OLD_HEAD,
            sync_base_no_progress_signature=sync_base_no_progress_signature(status),
            sync_base_no_progress_count=1,
        )

        result = await _observe(runner, status, state)

        assert result.unsynchronized_remote_head_sha == _PUSHED_HEAD

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "terminal", [{"merged": True}, {"closed": True}, {"base_ref_drifted": True}]
    )
    async def test_terminal_or_retargeted_pr_skips_probe_and_clears_markers(
        self, tmp_path: Path, terminal: dict[str, bool]
    ) -> None:
        runner = _ProbeRunner(tmp_path, local_head=_PUSHED_HEAD)
        state = MonitorState(
            last_push_sha=_PUSHED_HEAD,
            threads_addressed_ids={pr_head_sync_parked_key(_PUSHED_HEAD): "1"},
        )
        status = _dirty_status(**terminal)

        result = await _observe(runner, status, state)

        assert result is status
        assert runner.rev_parse_calls == []
        assert state.threads_addressed_ids == {}

    @pytest.mark.unit
    async def test_corrupt_first_seen_marker_restarts_the_window(self, tmp_path: Path) -> None:
        runner = _ProbeRunner(tmp_path, local_head=_PUSHED_HEAD)
        runner.cmd.queue_result(stdout=_ls_remote_stdout(_PUSHED_HEAD))
        key = pr_head_sync_first_seen_key(_PUSHED_HEAD)
        state = MonitorState(last_push_sha=_PUSHED_HEAD, threads_addressed_ids={key: "garbage"})

        result = await _observe(runner, _dirty_status(), state)

        assert result.pr_head_sync_grace_active is True
        assert float(state.threads_addressed_ids[key]) == pytest.approx(_NOW.timestamp())


# ── C. Runner wiring through run() ──────────────────────────────────────────


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _seed_stuck_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    threads_addressed: dict[str, str],
) -> str:
    ws_id = await seed_monitoring_workspace(factory, head_sha=_OLD_HEAD)
    async with factory() as s:
        ws = await WorkspaceRepository(s).get_for_update(ws_id)
        assert ws is not None
        ws.monitor_last_commit_sha = _PUSHED_HEAD
        ws.monitor_threads_addressed = threads_addressed
        await s.commit()
    return ws_id


def _stuck_command_runner(ws_id: str) -> FakeCommandRunner:
    """Accepted push: branch tip and local HEAD are AWF's commit, PR head is not."""
    cmd = FakeCommandRunner()
    cmd.respond_when(
        lambda args: "ls-remote" in args,
        stdout=_ls_remote_stdout(_PUSHED_HEAD, branch=f"awf/{ws_id}"),
    )
    cmd.respond_when(lambda args: args[-2:] == ["rev-parse", "HEAD"], stdout=f"{_PUSHED_HEAD}\n")
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(
        stdout=pr_payload(head_sha=_OLD_HEAD, mergeable="CONFLICTING", merge_state_status="DIRTY")
    )
    return cmd


class TestPrHeadSyncRunnerWiring:
    @pytest.mark.unit
    async def test_stuck_pr_head_past_grace_parks_needs_human_not_failed(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        status = _dirty_status()
        expired = (datetime.now(UTC) - timedelta(seconds=_GRACE + 100)).timestamp()
        ws_id = await _seed_stuck_workspace(
            factory,
            threads_addressed={
                pr_head_sync_first_seen_key(_PUSHED_HEAD): f"{expired:.6f}",
                # The incident's exhausted no-progress budget for the stale snapshot.
                _SYNC_BASE_NO_PROGRESS_SIGNATURE_KEY: sync_base_no_progress_signature(status),
                _SYNC_BASE_NO_PROGRESS_COUNT_KEY: "3",
            },
        )
        cmd = _stuck_command_runner(ws_id)
        cmd.queue_result(returncode=0)  # gh pr comment (human notification)
        parked_rows: list[tuple[str, str | None, str | None]] = []

        async def _sleep_and_snapshot(_seconds: float) -> None:
            # The park's poll sleep: the row must still be live, not failed.
            async with factory() as s:
                ws = await WorkspaceRepository(s).get(ws_id)
                assert ws is not None
                parked_rows.append((ws.status, ws.failure_reason, ws.awaiting_human_reason))

        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=FakeAdapter(),
            sleep_fn=_sleep_and_snapshot,  # type: ignore[arg-type]
            worktrees_root=tmp_path / "worktrees",
            max_outer_iterations=1,
        )

        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )

        actions = [r["action"] for r in captured if r.get("event") == "monitor.action"]
        assert actions == ["NotifyHuman"]
        comments = [c.args for c in cmd.calls if c.args[:3] == ["gh", "pr", "comment"]]
        assert len(comments) == 1
        assert PR_HEAD_SYNC_REMEDY in " ".join(comments[0])

        [(status_value, failure_reason, awaiting_reason)] = parked_rows
        assert status_value == WorkspaceStatus.monitoring_pr.value
        assert failure_reason is None
        assert awaiting_reason is not None
        assert _PUSHED_HEAD in awaiting_reason
        assert PR_HEAD_NOT_SYNCHRONIZED_REASON in awaiting_reason
        async with factory() as s:
            events = (
                await s.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == ws_id,
                        WorkspaceEvent.event_type == PR_HEAD_NOT_SYNCHRONIZED_EVENT,
                    )
                )
            ).scalars()
            [park_event] = list(events)
            operations = await OperationRepository(s).list_all(
                workspace_id=ws_id, operation_type=OperationType.human_wait, limit=20
            )
        assert park_event.reason_code == PR_HEAD_NOT_SYNCHRONIZED_REASON
        assert park_event.payload["remote_branch_head_sha"] == _PUSHED_HEAD
        assert park_event.payload["pr_head_sha"] == _OLD_HEAD
        assert park_event.payload["remedy"] == PR_HEAD_SYNC_REMEDY
        [operation] = operations
        assert operation.payload is not None
        assert operation.payload["reason_code"] == PR_HEAD_NOT_SYNCHRONIZED_REASON

    @pytest.mark.unit
    async def test_stuck_pr_head_within_grace_waits_and_persists_marker(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_stuck_workspace(factory, threads_addressed={})
        cmd = _stuck_command_runner(ws_id)
        sleep_fn = RecordedSleep()
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=FakeAdapter(),
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            max_outer_iterations=1,
        )

        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )

        actions = [r["action"] for r in captured if r.get("event") == "monitor.action"]
        assert actions == ["WaitForCI"]
        assert sleep_fn.calls == [60]
        assert not any(c.args[:3] == ["gh", "pr", "comment"] for c in cmd.calls)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert pr_head_sync_first_seen_key(_PUSHED_HEAD) in (ws.monitor_threads_addressed or {})
            operations = await OperationRepository(s).list_all(
                workspace_id=ws_id, operation_type=OperationType.monitor_state, limit=20
            )
        [wait_op] = [
            op
            for op in operations
            if isinstance(op.payload, dict) and op.payload.get("reason_code") == "CHECK_WAIT"
        ]
        assert wait_op.payload is not None
        assert wait_op.payload["wait_reason"] == "pr_head_not_synchronized"
        assert wait_op.payload["reason"] == (
            "GitHub has not synchronized the PR head with the pushed branch yet."
        )


# ── D. sync_base_push audit reports the merged base (requirement c) ─────────


class _SyncBaseLoopHarness:
    def __init__(self, push_result: _GitPushResult) -> None:
        self.push_result = push_result
        self.audits: list[dict[str, object]] = []

    async def _write_monitor_log(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def _clear_workspace_attention(self, _workspace_id: str) -> None:
        return None

    async def _begin_monitor_operation(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(operation_id="op-sync-base")

    async def _run_sync_base(self, **_kwargs: object) -> _GitPushResult:
        return self.push_result

    async def _finish_monitor_operation(self, _operation: object, **_kwargs: object) -> None:
        return None

    async def _record_pr_monitor_audit_event(self, **kwargs: object) -> None:
        self.audits.append(dict(kwargs))

    async def _terminate_failed(self, _workspace_id: str, **_kwargs: object) -> None:
        return None

    def _record_sync_base_progress(self, **_kwargs: object) -> None:
        return None

    async def _refresh_staleness_after_sync_base(self, **_kwargs: object) -> None:
        return None


async def _execute_sync_base(harness: _SyncBaseLoopHarness) -> None:
    await loop._execute(
        harness,  # type: ignore[arg-type]
        action=SyncBase(),
        workspace_id="ws_sync",
        repo_url="git@github.com:owner/repo.git",
        repo=SimpleNamespace(),  # type: ignore[arg-type]
        pr_number=42,
        status=_dirty_status(),
        state=MonitorState(),
        base_branch="development",
        remote_branch="awf/ws_sync",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        monitor_log=None,
    )


class TestSyncBasePushAuditMergedBase:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("push_result", "outcome"),
        [
            (_GitPushResult(pushed=True, failed=False, returncode=0), "succeeded"),
            (_GitPushResult(pushed=False, failed=True, returncode=1, stderr="nope"), "failed"),
        ],
    )
    async def test_sync_base_push_audit_carries_the_merged_base_sha(
        self, push_result: _GitPushResult, outcome: str
    ) -> None:
        harness = _SyncBaseLoopHarness(replace(push_result, merged_base_sha=_MERGED_BASE))

        await _execute_sync_base(harness)

        [audit] = [a for a in harness.audits if a["action"] == "sync_base_push"]
        assert audit["outcome"] == outcome
        assert audit["source_base_sha"] == _MERGED_BASE

    @pytest.mark.unit
    async def test_unresolved_merged_base_falls_back_to_the_recorded_base(self) -> None:
        harness = _SyncBaseLoopHarness(_GitPushResult(pushed=True, failed=False, returncode=0))

        await _execute_sync_base(harness)

        [audit] = harness.audits
        # ``None`` lets ``_record_pr_monitor_audit_event`` fall back to
        # ``workspace.base_commit`` exactly as before.
        assert audit["source_base_sha"] is None

    @pytest.mark.unit
    def test_merged_base_sha_is_not_failure_evidence(self) -> None:
        result = _GitPushResult(
            pushed=False, failed=True, returncode=1, merged_base_sha=_MERGED_BASE
        )

        assert "merged_base_sha" not in result.failure_evidence()
