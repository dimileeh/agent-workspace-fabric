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
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
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
        entries = _action_entries(captured)
        assert len(entries) == 2
        assert entries[0]["action"] == "NotifyHuman"
        assert entries[1]["action"] == "ShortCircuitCompleted"
        assert sleep_fn.calls == [60]

    @pytest.mark.unit
    async def test_notify_human_keeps_polling_and_addresses_later_comments(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        late_thread = thread_node(
            tid="T_late",
            author="gemini-code-assist",
            body="late actionable review",
        )
        # Poll 1: all green, but release/manual mode posts a human notice.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload())
        cmd.queue_result(returncode=0)  # gh pr comment
        # Poll 2: a review comment arrived after the human notice.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload(threads=[late_thread]))
        adapter.queue(stdout="fixed")
        cmd.queue_result(returncode=0, stdout=pr_payload())  # settle re-poll
        cmd.queue_result(returncode=0)  # git push
        cmd.queue_result(returncode=0, stdout="def456\n")  # git rev-parse
        cmd.queue_result(returncode=0)  # resolveReviewThread
        # Poll 3: maintainer merged externally; only now may the workspace complete.
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

        actions = [e["action"] for e in _action_entries(captured)]
        assert actions == ["NotifyHuman", "AddressComments", "ShortCircuitCompleted"]
        assert len(adapter.calls) == 1
        assert "late actionable review" in adapter.calls[0]
        assert sleep_fn.calls == [60, 30]
        comment_calls = [
            call.args for call in cmd.calls if call.args[:3] == ["gh", "pr", "comment"]
        ]
        assert len(comment_calls) == 1
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value

    @pytest.mark.unit
    async def test_policy_blocker_waits_alive_and_addresses_later_comments(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        blocking_comment = issue_comment_node(
            cid=77,
            author="coderabbitai",
            body=(
                "## Review skipped\n\n"
                "Required review has not run yet. Trigger review before merging.\n"
                "- [ ] Trigger review"
            ),
        )
        late_thread = thread_node(
            tid="T_after_notify",
            author="gemini-code-assist",
            body="new review feedback after AWF notified human",
        )
        # Poll 1: only a non-code policy blocker exists, so AWF notifies.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload(comments=[blocking_comment]))
        cmd.queue_result(returncode=0)  # gh pr comment
        # Poll 2: actionable comments arrive after the notification.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(
            returncode=0,
            stdout=pr_payload(comments=[blocking_comment], threads=[late_thread]),
        )
        adapter.queue(stdout="fixed")
        cmd.queue_result(returncode=0, stdout=pr_payload(comments=[blocking_comment]))
        cmd.queue_result(returncode=0)  # git push
        cmd.queue_result(returncode=0, stdout="def456\n")  # git rev-parse
        cmd.queue_result(returncode=0)  # resolveReviewThread
        # Poll 3: external merge is the terminal condition.
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
        )
        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )

        actions = [e["action"] for e in _action_entries(captured)]
        assert actions == ["NotifyHuman", "AddressComments", "ShortCircuitCompleted"]
        assert len(adapter.calls) == 1
        assert "new review feedback after AWF notified human" in adapter.calls[0]

    @pytest.mark.unit
    async def test_non_actionable_review_disabled_comment_waits_for_initial_grace(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        disabled_review_comment = issue_comment_node(
            cid=78,
            author="coderabbitai",
            body=(
                "## Review skipped\n\n"
                "Auto reviews are disabled on base/target branches other than development.\n"
                "- [ ] Trigger review"
            ),
        )
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload(comments=[disabled_review_comment]))
        # Keep the test finite by simulating an external merge while AWF waits
        # out the initial review grace window.
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
            initial_review_grace_period_seconds=900,
        )
        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )

        actions = [e["action"] for e in _action_entries(captured)]
        assert actions == ["Merge", "ShortCircuitCompleted"]
        assert sleep_fn.calls == [60]
        assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
        assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_merge_sha is None

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

        # Wrap FakeCommandRunner.run so that when ``gh pr merge`` is
        # invoked, we snapshot whether the Merge action log has already
        # been emitted. capture_logs' list is appended to synchronously
        # by structlog, so peeking at it inside tracking_run observes
        # exactly the logs that existed at dispatch time.
        saw_merge_log_before_call: list[bool] = []
        original_run = cmd.run

        with structlog.testing.capture_logs() as captured:

            async def tracking_run(args, **kwargs):  # type: ignore[no-untyped-def]
                if args[:3] == ["gh", "pr", "merge"]:
                    saw_merge_log_before_call.append(
                        any(
                            r.get("event") == "monitor.action" and r.get("action") == "Merge"
                            for r in captured
                        )
                    )
                return await original_run(args, **kwargs)

            cmd.run = tracking_run  # type: ignore[method-assign]

            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )

        action_events = [i for i, r in enumerate(captured) if r.get("event") == "monitor.action"]
        assert action_events, "expected a monitor.action log"
        merge_actions = [
            r for r in captured if r.get("event") == "monitor.action" and r.get("action") == "Merge"
        ]
        assert merge_actions, "expected a Merge action log"
        # If monitor.action moved below the dispatch, this would be
        # [False] and the test would fail — which is the regression we
        # guard against.
        assert saw_merge_log_before_call == [True], (
            "expected the Merge monitor.action log to be emitted before `gh pr merge` was invoked"
        )

    @pytest.mark.unit
    async def test_pre_merge_recheck_can_block_merge_on_late_review_comment(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        blocking_comment = issue_comment_node(
            cid=77,
            author="coderabbitai",
            body=(
                "## Review skipped\n\n"
                "Required review has not run yet. Trigger review before merging.\n"
                "- [ ] Trigger review"
            ),
        )

        # Initial poll looks mergeable.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload())
        # Final quiet-window recheck sees late bot/checklist feedback.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload(comments=[blocking_comment]))
        cmd.queue_result(returncode=0)  # gh pr comment from NotifyHuman
        # The monitor stays alive after NotifyHuman; finish by observing an
        # external merge on the next poll.
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
            pre_merge_settle_seconds=90,
        )
        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )

        assert sleep_fn.calls == [90, 60]
        assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
        actions = [e["action"] for e in _action_entries(captured)]
        assert actions == ["Merge", "NotifyHuman", "ShortCircuitCompleted"]
        comment_calls = [
            call.args for call in cmd.calls if call.args[:3] == ["gh", "pr", "comment"]
        ]
        assert len(comment_calls) == 1
        body = comment_calls[0][comment_calls[0].index("--body") + 1]
        assert "needs human attention" in body
        assert "review was skipped" in body
        assert "All 5 AWF gates are green" not in body
        assert any(
            r.get("event") == "monitor.pre_merge_recheck_changed_action"
            and r.get("fresh_action") == "NotifyHuman"
            for r in captured
        )
