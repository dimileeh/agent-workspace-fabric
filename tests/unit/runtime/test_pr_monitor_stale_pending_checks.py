"""Stale pending-check observability for the PR monitor runner."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    CheckTiming,
    MergeableState,
    MonitorState,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _is_pending_check,
    _stale_pending_check_warning_key,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
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


def _pending_check(
    *,
    name: str = "Greptile",
    started_at: datetime | None,
    details_url: str = "https://checks.example/greptile",
) -> dict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": "IN_PROGRESS",
        "conclusion": None,
        "startedAt": started_at.isoformat() if started_at is not None else None,
        "completedAt": None,
        "detailsUrl": details_url,
    }


async def _pending_check_events(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> list:
    async with factory() as s:
        return await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.pending_check_stale",
        )


class CountingSessionFactory:
    def __init__(self, wrapped: async_sessionmaker[AsyncSession]) -> None:
        self._wrapped = wrapped
        self.calls = 0

    def __call__(self) -> AsyncSession:
        self.calls += 1
        return self._wrapped()


class TestStalePendingCheckWarnings:
    @pytest.mark.unit
    def test_unknown_nonterminal_status_is_pending(self) -> None:
        check = CheckTiming(name="Future CI", status="AWAITING_RUN", conclusion=None)

        assert _is_pending_check(check)

    @pytest.mark.unit
    def test_terminal_conclusion_overrides_unknown_status(self) -> None:
        check = CheckTiming(name="Future CI", status="AWAITING_RUN", conclusion="SUCCESS")

        assert not _is_pending_check(check)

    @pytest.mark.unit
    def test_warning_key_encodes_colons_in_check_name(self) -> None:
        key_with_colon_check = _stale_pending_check_warning_key(
            workspace_id="ws_1",
            head_sha="sha1",
            check_name="a:b",
            threshold_seconds=60.0,
            threshold_window=1,
        )
        boundary_shifted_key = _stale_pending_check_warning_key(
            workspace_id="ws_1",
            head_sha="sha1:a",
            check_name="b",
            threshold_seconds=60.0,
            threshold_window=1,
        )

        assert key_with_colon_check != boundary_shifted_key

    @pytest.mark.unit
    async def test_no_event_before_threshold(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        started = datetime.now(UTC) - timedelta(seconds=30)
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(
            returncode=0,
            stdout=pr_payload(
                check_state="PENDING",
                check_contexts=[_pending_check(started_at=started)],
            ),
        )
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
        cmd.queue_result(returncode=0)
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            stale_pending_check_warning_seconds=60,
        )

        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )

        assert not [r for r in captured if r.get("event") == "workspace.pending_check_stale"]
        assert await _pending_check_events(factory, ws_id) == []
        assert sleep_fn.calls == [60]

    @pytest.mark.unit
    async def test_event_after_threshold(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        started = datetime.now(UTC) - timedelta(seconds=75)
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(
            returncode=0,
            stdout=pr_payload(
                check_state="PENDING",
                check_contexts=[_pending_check(started_at=started)],
            ),
        )
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
        cmd.queue_result(returncode=0)
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            stale_pending_check_warning_seconds=60,
        )

        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )

        log_entries = [r for r in captured if r.get("event") == "workspace.pending_check_stale"]
        assert len(log_entries) == 1
        assert log_entries[0]["check_name"] == "Greptile"
        assert log_entries[0]["pr_number"] == 42
        assert log_entries[0]["head_sha"] == "abc1234567890def"
        assert log_entries[0]["age_seconds"] >= 60

        events = await _pending_check_events(factory, ws_id)
        assert len(events) == 1
        payload = events[0].payload
        assert payload is not None
        assert payload["check_name"] == "Greptile"
        assert payload["pr_number"] == 42
        assert payload["head_sha"] == "abc1234567890def"
        assert payload["age_seconds"] >= 60

    @pytest.mark.unit
    async def test_multiple_stale_pending_checks_use_one_event_session(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        counted_factory = CountingSessionFactory(factory)
        started = datetime.now(UTC) - timedelta(seconds=75)
        runner = make_runner(
            factory=counted_factory,  # type: ignore[arg-type]
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            stale_pending_check_warning_seconds=60,
        )
        status = PRStatus(
            number=42,
            head_sha="abc1234567890def",
            mergeable=MergeableState.MERGEABLE,
            check_state=CheckState.PENDING,
            unresolved_inline_threads=(),
            unresolved_review_comments=(),
            base_behind_count=0,
            checks=(
                CheckTiming(
                    name="Greptile",
                    status="IN_PROGRESS",
                    started_at=started,
                    details_url="https://checks.example/greptile",
                ),
                CheckTiming(
                    name="Unit Tests",
                    status="QUEUED",
                    started_at=started,
                    details_url="https://checks.example/unit",
                ),
            ),
        )

        emitted = await runner._record_stale_pending_check_warnings(
            workspace_id=ws_id,
            status=status,
            state=MonitorState(),
            monitor_log=None,
        )

        assert emitted is True
        assert counted_factory.calls == 1
        events = await _pending_check_events(factory, ws_id)
        assert {e.payload["check_name"] for e in events if e.payload is not None} == {
            "Greptile",
            "Unit Tests",
        }

    @pytest.mark.unit
    async def test_duplicate_polls_do_not_spam_same_threshold_window(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        started = datetime.now(UTC) - timedelta(seconds=75)
        for _ in range(2):
            cmd.queue_result(returncode=0)
            cmd.queue_result(returncode=0, stdout="0\n")
            cmd.queue_result(
                returncode=0,
                stdout=pr_payload(
                    check_state="PENDING",
                    check_contexts=[_pending_check(started_at=started)],
                ),
            )
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
        cmd.queue_result(returncode=0)
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            stale_pending_check_warning_seconds=60,
        )

        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )

        assert len([r for r in captured if r.get("event") == "workspace.pending_check_stale"]) == 1
        assert len(await _pending_check_events(factory, ws_id)) == 1
        assert sleep_fn.calls == [60, 60]

    @pytest.mark.unit
    async def test_new_head_can_emit_again(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        started = datetime.now(UTC) - timedelta(seconds=75)
        for head_sha in ("abc1234567890def", "def4567890abcdef"):
            cmd.queue_result(returncode=0)
            cmd.queue_result(returncode=0, stdout="0\n")
            cmd.queue_result(
                returncode=0,
                stdout=pr_payload(
                    head_sha=head_sha,
                    check_state="PENDING",
                    check_contexts=[_pending_check(started_at=started)],
                ),
            )
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
        cmd.queue_result(returncode=0)
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            stale_pending_check_warning_seconds=60,
        )

        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

        events = await _pending_check_events(factory, ws_id)
        assert [e.payload["head_sha"] for e in events if e.payload is not None] == [
            "def4567890abcdef",
            "abc1234567890def",
        ]

    @pytest.mark.unit
    async def test_missing_started_at_is_ignored_without_crashing(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(
            returncode=0,
            stdout=pr_payload(
                check_state="PENDING",
                check_contexts=[_pending_check(started_at=None)],
            ),
        )
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
        cmd.queue_result(returncode=0)
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            stale_pending_check_warning_seconds=60,
        )

        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

        assert await _pending_check_events(factory, ws_id) == []
        assert sleep_fn.calls == [60]

    @pytest.mark.unit
    async def test_stale_pending_check_still_waits_for_ci(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        started = datetime.now(UTC) - timedelta(seconds=75)
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(
            returncode=0,
            stdout=pr_payload(
                check_state="PENDING",
                check_contexts=[_pending_check(started_at=started)],
            ),
        )
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            stale_pending_check_warning_seconds=60,
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
        assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_message is not None
            assert "max_outer_iterations" in ws.failure_message
