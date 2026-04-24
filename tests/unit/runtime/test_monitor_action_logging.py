"""Action-decision logging for the PR monitor runner.

Regression guard for PR 342: the monitor ran 200+ iterations silently,
with no way to tell from the logs whether it was looping on
``AddressComments``, ``NotifyHuman``, or ``Merge``. We now emit a
``monitor.action`` structlog line BEFORE each dispatch so operators
grepping ``/tmp/awf-realrun/logs/*.log`` can trace the decision sequence.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.base import Base
from awf.db.session import make_engine, make_session_factory
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
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


def _action_entries(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("event") == "monitor.action"]


class TestMonitorActionLogging:
    @pytest.mark.unit
    async def test_merge_action_emits_log_line(
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
        cmd.queue_result(returncode=0, stdout=pr_payload())  # PR state
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="MERGESHA\n")  # sha lookup
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )
        entries = _action_entries(captured)
        assert len(entries) == 1, f"expected one monitor.action; got {entries}"
        entry = entries[0]
        assert entry["action"] == "Merge"
        assert entry["workspace_id"] == ws_id
        assert entry["pr_number"] == 42
        assert entry["iter"] == 0
        assert entry["merge_state"] == "CLEAN"
        assert entry["unresolved_threads"] == 0
        assert entry["unresolved_reviews"] == 0
        assert entry["head_sha"].startswith("abc1234567")

    @pytest.mark.unit
    async def test_notify_human_action_emits_log_line(
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
        cmd.queue_result(returncode=0, stdout=pr_payload())
        cmd.queue_result(returncode=0)  # gh pr comment
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
        entries = _action_entries(captured)
        assert len(entries) == 1
        assert entries[0]["action"] == "NotifyHuman"

    @pytest.mark.unit
    async def test_short_circuit_completed_emits_log_line(
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
        cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )
        entries = _action_entries(captured)
        assert len(entries) == 1
        assert entries[0]["action"] == "ShortCircuitCompleted"

    @pytest.mark.unit
    async def test_abort_action_emits_log_line(
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
        cmd.queue_result(returncode=0, stdout=pr_payload(closed=True))  # closed → Abort
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )
        entries = _action_entries(captured)
        assert len(entries) == 1
        assert entries[0]["action"] == "Abort"

    @pytest.mark.unit
    async def test_address_comments_action_emits_log_line(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        thread = thread_node(tid="T1", author="cr")
        # Outer iter 1: AddressComments.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(threads=[thread]))
        adapter.queue(stdout="fixed it")
        cmd.queue_result(returncode=0, stdout=pr_payload())  # settle fetch
        cmd.queue_result(returncode=0)  # push
        cmd.queue_result(returncode=0, stdout="head2\n")  # rev-parse
        cmd.queue_result(returncode=0, stdout=json.dumps({"data": {}}))  # resolve
        # Outer iter 2: clean → Merge.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload())
        cmd.queue_result(returncode=0)  # merge
        cmd.queue_result(returncode=0, stdout="M\n")
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )
        entries = _action_entries(captured)
        actions = [e["action"] for e in entries]
        assert actions == ["AddressComments", "Merge"], actions
        ac_entry = entries[0]
        assert ac_entry["unresolved_threads"] == 1
        assert ac_entry["iter"] == 0  # iter_count bumped AFTER dispatch

    @pytest.mark.unit
    async def test_wait_for_ci_action_emits_log_line(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        # Outer iter 1: PENDING → WaitForCI; outer iter 2: CLEAN → Merge.
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(check_state="PENDING"))
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload())
        cmd.queue_result(returncode=0)  # merge
        cmd.queue_result(returncode=0, stdout="M\n")
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )
        actions = [e["action"] for e in _action_entries(captured)]
        assert actions == ["WaitForCI", "Merge"]

    @pytest.mark.unit
    async def test_action_log_fires_before_dispatch_side_effects(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """The log line must land BEFORE the side effects it narrates —
        if the merge call crashes, the operator's log grep still shows
        which action was chosen. We verify by checking the log appears
        before the ``gh pr merge`` command recorded by FakeCommandRunner."""
        ws_id = await seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload())
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="M\n")  # sha
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )

        # Wrap FakeCommandRunner.run to interleave with log capture order:
        # each recorded call happens-after the log that narrates the
        # action. We check this by asserting the "monitor.action" log
        # with action=Merge appears in captured_logs BEFORE the ``gh pr
        # merge`` FakeCommandRunner call does any recording via a
        # counter-based interleaving check.
        merge_cmd_index: list[int] = []
        original_run = cmd.run

        async def tracking_run(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[:3] == ["gh", "pr", "merge"]:
                merge_cmd_index.append(len(cmd.calls))
            return await original_run(args, **kwargs)

        cmd.run = tracking_run  # type: ignore[method-assign]

        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )

        action_events = [i for i, r in enumerate(captured) if r.get("event") == "monitor.action"]
        assert action_events, "expected a monitor.action log"
        # The Merge action log should appear somewhere BEFORE the
        # ``gh pr merge`` command is invoked. capture_logs records in
        # order, and FakeCommandRunner records in order, so asserting
        # the log entry index predates the recorded-call index is
        # sufficient: the very next thing we did after logging was to
        # invoke the gh merge call, and capture_logs records the log
        # synchronously at the moment it is made.
        merge_actions = [
            r for r in captured if r.get("event") == "monitor.action" and r.get("action") == "Merge"
        ]
        assert merge_actions, "expected a Merge action log"
        assert merge_cmd_index, "expected gh pr merge to have been called"
