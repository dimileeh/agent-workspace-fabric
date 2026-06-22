"""Bounded grace before HUMAN_WAIT on transient required-CI absence (#655).

Right after a monitor push the forge has not started CI on the new head yet, so
the head shows an EMPTY check set (``no_checks_observed=True``) while branch
protection reports ``merge_state_status == BLOCKED`` (the required context is
absent). The empty rollup parses to a non-PENDING ``check_state`` so the pending
gate misses it, and ``decide`` used to escalate ``NotifyHuman`` immediately — a
premature human ping the monitor self-recovers from once CI lands.

The runner derives a per-head grace flag
(``_awaiting_required_checks_grace`` →
``PRStatus.awaiting_required_checks_grace_active``); ``decide`` defers to a
bounded ``WaitForCI("awaiting_required_checks")`` while the window is active and
escalates only after it expires. These tests lock the runner timing helper and
the end-to-end loop wiring (the pure ``decide`` matrix lives in
``test_pr_monitor_require_ci.py``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.enums import OperationType
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _awaiting_required_checks_first_seen_key,
    _awaiting_required_checks_grace,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)

_NOW = datetime(2026, 6, 22, 8, 30, 0, tzinfo=UTC)


def _status(
    *,
    head_sha: str = "head1",
    no_checks_observed: bool = True,
    check_state: CheckState = CheckState.SUCCESS,
    merge_state_status: MergeStateStatus = MergeStateStatus.BLOCKED,
) -> PRStatus:
    return PRStatus(
        number=7,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=check_state,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=merge_state_status,
        no_checks_observed=no_checks_observed,
    )


# ── B. Runner timing-helper unit tests (no DB) ─────────────────────────────


class TestAwaitingRequiredChecksGrace:
    @pytest.mark.unit
    def test_first_observation_records_marker_and_is_within_grace(self) -> None:
        state = MonitorState()

        active, changed = _awaiting_required_checks_grace(
            _status(), state, MonitorConfig(), now=_NOW
        )

        assert (active, changed) == (True, True)
        key = _awaiting_required_checks_first_seen_key("head1")
        assert key in state.threads_addressed_ids
        assert float(state.threads_addressed_ids[key]) == pytest.approx(_NOW.timestamp())
        # The first record must be reported as a change so the runner persists it.
        assert key in state.changed_thread_ids()

    @pytest.mark.unit
    def test_same_head_within_grace_stays_active_without_rerecording(self) -> None:
        state = MonitorState()
        config = MonitorConfig()
        _awaiting_required_checks_grace(_status(), state, config, now=_NOW)
        first_value = state.threads_addressed_ids[_awaiting_required_checks_first_seen_key("head1")]

        active, changed = _awaiting_required_checks_grace(
            _status(), state, config, now=_NOW + timedelta(seconds=300)
        )

        assert (active, changed) == (True, False)
        # The marker is untouched on a within-grace re-poll.
        assert (
            state.threads_addressed_ids[_awaiting_required_checks_first_seen_key("head1")]
            == first_value
        )

    @pytest.mark.unit
    def test_same_head_after_grace_flips_inactive(self) -> None:
        state = MonitorState()
        config = MonitorConfig()
        _awaiting_required_checks_grace(_status(), state, config, now=_NOW)

        active, changed = _awaiting_required_checks_grace(
            _status(),
            state,
            config,
            now=_NOW + timedelta(seconds=config.awaiting_required_checks_grace_seconds),
        )

        assert (active, changed) == (False, False)

    @pytest.mark.unit
    def test_new_head_resets_window(self) -> None:
        state = MonitorState()
        config = MonitorConfig()
        # Old head observed long ago and now expired.
        _awaiting_required_checks_grace(_status(head_sha="old"), state, config, now=_NOW)

        active, changed = _awaiting_required_checks_grace(
            _status(head_sha="new"),
            state,
            config,
            now=_NOW + timedelta(seconds=10_000),
        )

        assert (active, changed) == (True, True)
        new_key = _awaiting_required_checks_first_seen_key("new")
        assert float(state.threads_addressed_ids[new_key]) == pytest.approx(
            (_NOW + timedelta(seconds=10_000)).timestamp()
        )

    @pytest.mark.unit
    def test_require_ci_false_records_nothing(self) -> None:
        state = MonitorState()

        active, changed = _awaiting_required_checks_grace(
            _status(), state, MonitorConfig(require_ci=False), now=_NOW
        )

        assert (active, changed) == (False, False)
        assert state.threads_addressed_ids == {}

    @pytest.mark.unit
    def test_checks_present_records_nothing(self) -> None:
        state = MonitorState()

        active, changed = _awaiting_required_checks_grace(
            _status(no_checks_observed=False), state, MonitorConfig(), now=_NOW
        )

        assert (active, changed) == (False, False)
        assert state.threads_addressed_ids == {}

    @pytest.mark.unit
    def test_non_positive_grace_disables_and_records_nothing(self) -> None:
        state = MonitorState()

        active, changed = _awaiting_required_checks_grace(
            _status(),
            state,
            MonitorConfig(awaiting_required_checks_grace_seconds=0),
            now=_NOW,
        )

        assert (active, changed) == (False, False)
        assert state.threads_addressed_ids == {}

    @pytest.mark.unit
    def test_corrupt_marker_is_rerecorded_within_grace(self) -> None:
        state = MonitorState()
        key = _awaiting_required_checks_first_seen_key("head1")
        state.threads_addressed_ids[key] = "not-a-float"

        active, changed = _awaiting_required_checks_grace(
            _status(), state, MonitorConfig(), now=_NOW
        )

        assert (active, changed) == (True, True)
        assert float(state.threads_addressed_ids[key]) == pytest.approx(_NOW.timestamp())


# ── C. Runner integration tests (full loop wiring + loop.py message) ────────


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


def _absent_ci_blocked_payload() -> str:
    # A present-but-empty rollup (totalCount == 0) with a non-PENDING state is the
    # transient post-push CI-start window: no_checks_observed=True, BLOCKED.
    return pr_payload(
        check_state="SUCCESS",
        check_contexts=[],
        check_contexts_total_count=0,
        merge_state_status="BLOCKED",
    )


class TestAwaitingRequiredChecksRunner:
    @pytest.mark.unit
    async def test_within_grace_waits_for_ci_and_records_marker(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_absent_ci_blocked_payload())
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
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
        # The transient gap must NOT page a human.
        assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            # First-seen marker persisted so the window can actually expire.
            assert _awaiting_required_checks_first_seen_key("abc1234567890def") in (
                ws.monitor_threads_addressed or {}
            )
            operations = await OperationRepository(s).list_all(
                workspace_id=ws_id,
                operation_type=OperationType.monitor_state,
                limit=20,
            )
        wait_ops = [
            op
            for op in operations
            if isinstance(op.payload, dict) and op.payload.get("reason_code") == "CHECK_WAIT"
        ]
        assert len(wait_ops) == 1
        payload = wait_ops[0].payload
        assert payload is not None
        assert payload["wait_reason"] == "awaiting_required_checks"
        assert payload["reason"] == "Required CI has not started on the new head yet."

    @pytest.mark.unit
    async def test_grace_expired_escalates_to_notify_human(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        # Pre-seed an EXPIRED first-seen marker for the current head: observed
        # longer ago than the default 600s grace.
        expired_epoch = (datetime.now(UTC) - timedelta(seconds=700)).timestamp()
        async with factory() as s:
            ws = await WorkspaceRepository(s).get_for_update(ws_id)
            assert ws is not None
            ws.monitor_threads_addressed = {
                _awaiting_required_checks_first_seen_key("abc1234567890def"): (
                    f"{expired_epoch:.6f}"
                )
            }
            await s.commit()

        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_absent_ci_blocked_payload())
        cmd.queue_result(returncode=0)  # gh pr comment (human notification)
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
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
        assert actions == ["NotifyHuman"]
        # The expired head genuinely never got CI → a human comment IS posted.
        assert any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)

    @pytest.mark.unit
    async def test_new_head_gets_its_own_grace_window(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        # Two polls with DIFFERENT head SHAs: each push reopens the CI-start gap,
        # so each head must wait within its own fresh grace window.
        for head_sha in ("abc1234567890def", "def4567890abcdef0"):
            cmd.queue_result(returncode=0)  # git fetch origin <base>
            cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
            cmd.queue_result(
                returncode=0,
                stdout=pr_payload(
                    head_sha=head_sha,
                    check_state="SUCCESS",
                    check_contexts=[],
                    check_contexts_total_count=0,
                    merge_state_status="BLOCKED",
                ),
            )
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            max_outer_iterations=2,
        )

        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )

        actions = [r["action"] for r in captured if r.get("event") == "monitor.action"]
        assert actions == ["WaitForCI", "WaitForCI"]
        assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
        markers = ws.monitor_threads_addressed or {}
        assert _awaiting_required_checks_first_seen_key("abc1234567890def") in markers
        assert _awaiting_required_checks_first_seen_key("def4567890abcdef0") in markers

    @pytest.mark.unit
    async def test_pre_merge_recheck_applies_grace_instead_of_paging_human(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        # #656 (PRRT_kwDOSJAM6s6LPoZx): the outer poll observed an all-green head
        # and chose Merge, but during the pre-merge settle window the freshly
        # fetched status shows the transient empty-checks + BLOCKED CI-start gap
        # (the head changed, or GitHub recomputed mergeability). The recheck
        # ``decide`` must apply the same per-head required-CI grace as the outer
        # loop; otherwise ``awaiting_required_checks_grace_active`` stays its
        # default False, gate 8b is skipped, and the monitor pages a human
        # prematurely before the next outer poll can recover.
        ws_id = await seed_monitoring_workspace(factory)
        # Outer poll: all green -> Merge.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload())
        # Pre-merge settle recheck: transient required-CI-absent + BLOCKED gap.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_absent_ci_blocked_payload())
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            pre_merge_settle_seconds=90,
            max_outer_iterations=1,
        )

        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )

        actions = [r["action"] for r in captured if r.get("event") == "monitor.action"]
        # The recheck defers to the bounded grace wait, NOT a premature human ping.
        assert actions == ["Merge", "WaitForCI"]
        assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)
        assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
        # 90s pre-merge settle, then the 60s poll-interval grace wait.
        assert sleep_fn.calls == [90, 60]

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
        # The recheck persisted the first-seen marker so the grace window can
        # actually expire on a head that genuinely never gets CI.
        assert _awaiting_required_checks_first_seen_key("abc1234567890def") in (
            ws.monitor_threads_addressed or {}
        )
