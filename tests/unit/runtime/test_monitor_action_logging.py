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
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.logs import LogStore
from tests.postgres import postgres_test_engine
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


def _action_entries(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("event") == "monitor.action"]


def _name_status_z(*paths: str) -> str:
    return "".join(f"M\0{path}\0" for path in paths)


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
        assert entry["review_feedback"] == 0
        assert entry["pending_review_feedback"] == 0
        assert entry["unresolved_reviews"] == entry["pending_review_feedback"]
        assert entry["blocking_reviews"] == 0
        assert entry["head_sha"].startswith("abc1234567")
        async with factory() as s:
            operations = await OperationRepository(s).list_all(workspace_id=ws_id, limit=20)
            attempt_events = await WorkspaceEventRepository(s).list(
                workspace_id=ws_id,
                event_type="workspace.audit.merge_attempt",
                limit=10,
            )
            result_events = await WorkspaceEventRepository(s).list(
                workspace_id=ws_id,
                event_type="workspace.audit.merge_result",
                limit=10,
            )
        merge_operation = next(
            op
            for op in operations
            if op.type == "monitor_state"
            and isinstance(op.payload, dict)
            and op.payload.get("action") == "merge"
        )
        assert len(attempt_events) == 1
        assert attempt_events[0].payload == {
            "schema": "control_audit.v1",
            "actor": "pr_monitor",
            "source": "pr_monitor",
            "action": "merge",
            "outcome": "attempted",
            "reason_code": "MERGE",
            "operation_id": merge_operation.id,
            "operation_type": "monitor_state",
            "pr_number": 42,
            "pr_url": "https://github.com/dimileeh/aira-web/pull/42",
            "source_head_sha": "abc1234567890def",
            "source_base_sha": "a" * 40,
            "target_branch": "development",
            "remote_branch": f"awf/{ws_id}",
            "branch_name": f"awf/{ws_id}",
        }
        assert len(result_events) == 1
        assert result_events[0].payload == {
            **attempt_events[0].payload,
            "outcome": "succeeded",
            "evidence": {"merge_sha": "MERGESHA"},
        }

    @pytest.mark.unit
    async def test_monitor_writes_durable_monitor_log_stream(
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
        log_store = LogStore(root=tmp_path / "logs", session_factory=factory)
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            log_store=log_store,
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

        async with factory() as s:
            streams = await WorkspaceLogStreamRepository(s).list_for_workspace(ws_id)
            monitor_stream = next(stream for stream in streams if stream.stream_id == "monitor.log")
        assert monitor_stream.source == "monitor"
        assert monitor_stream.kind == "stdout"
        assert monitor_stream.line_count >= 3
        log_text = Path(monitor_stream.path).read_text()
        assert '"event": "monitor.start"' in log_text
        assert '"event": "monitor.action"' in log_text
        assert '"action": "Merge"' in log_text
        assert '"review_feedback": 0' in log_text
        assert '"pending_review_feedback": 0' in log_text
        assert '"blocking_reviews": 0' in log_text

    @pytest.mark.unit
    async def test_pre_merge_settle_emits_started_and_completed_logs(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        # Initial monitor poll is green.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload())  # PR state
        # Pre-merge settle recheck is still green.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload())  # PR state
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="MERGESHA\n")  # sha lookup
        log_store = LogStore(root=tmp_path / "logs", session_factory=factory)
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            pre_merge_settle_seconds=5,
            log_store=log_store,
        )

        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )

        event_names = [record.get("event") for record in captured]
        entered_index = event_names.index("monitor.merge_critical_section_entered")
        started_index = event_names.index("monitor.pre_merge_settle_started")
        completed_index = event_names.index("monitor.pre_merge_settle_completed")
        assert entered_index < started_index < completed_index
        started = captured[started_index]
        completed = captured[completed_index]
        for record in (started, completed):
            assert record["workspace_id"] == ws_id
            assert record["pr_number"] == 42
            assert record["base_branch"] == "development"
            assert record["head_sha"] == "abc1234567890def"
            assert record["wait_seconds"] == 5
        assert isinstance(completed["elapsed_seconds"], (int, float))
        assert completed["elapsed_seconds"] >= 0

        async with factory() as s:
            streams = await WorkspaceLogStreamRepository(s).list_for_workspace(ws_id)
            monitor_stream = next(stream for stream in streams if stream.stream_id == "monitor.log")
        durable_records = [
            json.loads(line)
            for line in Path(monitor_stream.path).read_text().splitlines()
            if line.strip()
        ]
        durable_events = [record.get("event") for record in durable_records]
        durable_entered_index = durable_events.index("monitor.merge_critical_section_entered")
        durable_started_index = durable_events.index("monitor.pre_merge_settle_started")
        durable_completed_index = durable_events.index("monitor.pre_merge_settle_completed")
        assert durable_entered_index < durable_started_index < durable_completed_index
        durable_started = durable_records[durable_started_index]
        durable_completed = durable_records[durable_completed_index]
        for record in (durable_started, durable_completed):
            assert record["workspace_id"] == ws_id
            assert record["pr_number"] == 42
            assert record["base_branch"] == "development"
            assert record["head_sha"] == "abc1234567890def"
            assert record["wait_seconds"] == 5
        assert isinstance(durable_completed["elapsed_seconds"], (int, float))
        assert durable_completed["elapsed_seconds"] >= 0

    @pytest.mark.unit
    async def test_recovery_operation_log_indexing(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        thread = thread_node(tid="T_recov", author="reviewer")
        # Outer iter 1
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(threads=[thread]))
        adapter.queue(stdout="fixed it")
        cmd.queue_result(returncode=0, stdout=pr_payload())  # settle fetch
        cmd.queue_result(returncode=0)  # push
        cmd.queue_result(returncode=0, stdout="head2\n")  # rev-parse
        cmd.queue_result(returncode=0, stdout=json.dumps({"data": {}}))  # resolve
        # Outer iter 2: clean -> Merge.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload())
        cmd.queue_result(returncode=0)  # merge
        cmd.queue_result(returncode=0, stdout="MERGESHA\n")

        log_store = LogStore(root=tmp_path / "logs", session_factory=factory)
        adapter._log_store = log_store
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            log_store=log_store,
        )

        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

        async with factory() as s:
            streams = await WorkspaceLogStreamRepository(s).list_for_workspace(ws_id)
            recovery_stream = next(
                (stream for stream in streams if stream.source == "recovery"), None
            )
            operations = await OperationRepository(s).list_all(workspace_id=ws_id, limit=20)
            push_events = await WorkspaceEventRepository(s).list(
                workspace_id=ws_id,
                event_type="workspace.audit.git_push",
                limit=10,
            )
            resolution_events = await WorkspaceEventRepository(s).list(
                workspace_id=ws_id,
                event_type="workspace.audit.comment_resolution",
                limit=10,
            )
        assert recovery_stream is not None, (
            f"Expected a stream with source='recovery'. Streams: {[(s.stream_id, s.source) for s in streams]}. Calls: {[c.args for c in cmd.calls]}"
        )
        assert recovery_stream.kind == "stdout"
        comment_operation = next(op for op in operations if op.type == "comment_repair")
        comment_push = next(
            event
            for event in push_events
            if event.payload is not None and event.payload["action"] == "comment_repair_push"
        )
        assert comment_push.payload == {
            "schema": "control_audit.v1",
            "actor": "pr_monitor",
            "source": "pr_monitor",
            "action": "comment_repair_push",
            "outcome": "succeeded",
            "reason_code": "COMMENT_REPAIR",
            "operation_id": comment_operation.id,
            "operation_type": "comment_repair",
            "pr_number": 42,
            "pr_url": "https://github.com/dimileeh/aira-web/pull/42",
            "source_head_sha": "head2",
            "source_base_sha": "a" * 40,
            "target_branch": "development",
            "remote_branch": f"awf/{ws_id}",
            "branch_name": f"awf/{ws_id}",
            "evidence": {"log_stream_refs": {"monitor": "monitor.log"}},
        }
        assert len(resolution_events) == 1
        assert resolution_events[0].payload is not None
        assert resolution_events[0].payload["action"] == "resolve_thread"
        assert resolution_events[0].payload["outcome"] == "succeeded"
        assert resolution_events[0].payload["operation_id"] == comment_operation.id
        assert resolution_events[0].payload["evidence"] == {
            "thread_ids": ["T_recov"],
            "resolved_thread_count": 1,
            "log_stream_refs": {"monitor": "monitor.log"},
        }
        assert "tiny nit" not in repr(resolution_events[0].payload)

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
        assert adapter.workspace_ids == [ws_id]
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
    async def test_bot_issue_feedback_stays_alive_and_addresses_later_comments(
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
        # Poll 1: a review-bot issue comment is routed to the agent instead
        # of being semantically classified by AWF.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload(comments=[blocking_comment]))
        adapter.queue(stdout="FALSE POSITIVE: trigger-review status only")
        cmd.queue_result(
            returncode=0,
            stdout=pr_payload(comments=[blocking_comment]),
        )  # settle fetch
        cmd.queue_result(returncode=0, stderr="Everything up-to-date")  # git push
        # Poll 2: later actionable comments are still handled.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(
            returncode=0,
            stdout=pr_payload(comments=[blocking_comment], threads=[late_thread]),
        )
        adapter.queue(stdout="fixed")
        cmd.queue_result(returncode=0, stdout=pr_payload())  # settle fetch
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
        assert actions == ["AddressComments", "AddressComments", "ShortCircuitCompleted"]
        first_address, second_address = _action_entries(captured)[:2]
        assert first_address["review_feedback"] == 1
        assert first_address["pending_review_feedback"] == 1
        assert first_address["unresolved_reviews"] == 1
        assert second_address["review_feedback"] == 1
        assert second_address["pending_review_feedback"] == 0
        assert second_address["unresolved_reviews"] == 0
        assert len(adapter.calls) == 2
        assert "Trigger review before merging" in adapter.calls[0]
        assert "new review feedback after AWF notified human" in adapter.calls[1]

    @pytest.mark.unit
    async def test_review_disabled_comment_routes_to_agent_before_merge(
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
        adapter.queue(stdout="FALSE POSITIVE: disabled-review status only")
        cmd.queue_result(returncode=0, stdout=pr_payload())  # settle fetch
        cmd.queue_result(returncode=0, stderr="Everything up-to-date")  # git push
        # Keep the test finite by simulating an external merge after AWF
        # packages the comment for the agent.
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
        assert actions == ["AddressComments", "ShortCircuitCompleted"]
        assert sleep_fn.calls == [30]
        assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
        assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)
        assert len(adapter.calls) == 1
        assert "Auto reviews are disabled" in adapter.calls[0]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_merge_sha == "mergecommit1234567890"

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
    async def test_failed_thread_resolve_is_retried_not_merged(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """A fixed-but-still-unresolved thread must not be filtered out forever.

        GitHub can reject ``resolveReviewThread`` even after the agent has
        pushed a valid fix. The monitor should clear the addressed marker so
        the next poll retries comment handling instead of flowing through to
        Merge with an unresolved review thread.
        """
        ws_id = await seed_monitoring_workspace(factory)
        thread = thread_node(tid="T_retry", author="greptile-apps")

        # Outer iter 1: AddressComments, then resolveReviewThread fails.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(threads=[thread]))
        adapter.queue(stdout="fixed first")
        cmd.queue_result(returncode=0, stdout=pr_payload())  # settle fetch
        cmd.queue_result(returncode=0)  # push
        cmd.queue_result(returncode=0, stdout="head2\n")  # rev-parse
        cmd.queue_result(returncode=1, stderr="missing resolve permission")

        # Outer iter 2: GitHub still reports the thread open, so retry it.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=pr_payload(threads=[thread]))
        adapter.queue(stdout="fixed again")
        cmd.queue_result(returncode=0, stdout=pr_payload())  # settle fetch
        cmd.queue_result(returncode=0)  # push
        cmd.queue_result(returncode=0, stdout="head3\n")  # rev-parse
        cmd.queue_result(returncode=0, stdout=json.dumps({"data": {}}))  # resolve

        # Outer iter 3: clean → Merge.
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

        actions = [e["action"] for e in _action_entries(captured)]
        assert actions == ["AddressComments", "AddressComments", "Merge"]
        assert len(adapter.calls) == 2

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
        # Final quiet-window recheck sees late bot feedback, which is
        # routed to the agent instead of human notification.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload(comments=[blocking_comment]))
        adapter.queue(stdout="FALSE POSITIVE: trigger-review status only")
        cmd.queue_result(returncode=0, stdout=pr_payload())  # settle fetch
        cmd.queue_result(returncode=0, stderr="Everything up-to-date")  # git push
        # The monitor stays alive after the review-comment fix cycle; finish
        # by observing an external merge on the next poll.
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

        assert sleep_fn.calls == [90, 30]
        assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
        actions = [e["action"] for e in _action_entries(captured)]
        assert actions == ["Merge", "AddressComments", "ShortCircuitCompleted"]
        assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)
        assert len(adapter.calls) == 1
        assert "Trigger review before merging" in adapter.calls[0]
        merge_action = _action_entries(captured)[0]
        address_action = _action_entries(captured)[1]
        assert merge_action["unresolved_reviews"] == 0
        assert merge_action["review_feedback"] == 0
        assert merge_action["pending_review_feedback"] == 0
        assert merge_action["blocking_reviews"] == 0
        assert address_action["review_feedback"] == 1
        assert address_action["pending_review_feedback"] == 1
        assert address_action["unresolved_reviews"] == address_action["pending_review_feedback"]
        changed_action = next(
            r
            for r in captured
            if r.get("event") == "monitor.pre_merge_recheck_changed_action"
            and r.get("fresh_action") == "AddressComments"
        )
        assert changed_action["review_feedback"] == 1
        assert changed_action["pending_review_feedback"] == 1
        assert changed_action["unresolved_reviews"] == changed_action["pending_review_feedback"]
        assert changed_action["blocking_reviews"] == 0


class TestMonitorDirtyWorktreeSalvage:
    @pytest.mark.unit
    async def test_comment_agent_failure_with_dirty_changes_is_committed_and_resolved(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        thread = thread_node(tid="T_dirty", author="gemini-code-assist")
        worktrees_root = tmp_path / "worktrees"
        (worktrees_root / ws_id).mkdir(parents=True)

        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload(threads=[thread]))
        adapter.queue(stdout="changed files but failed before summary", returncode=1)
        cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
        cmd.queue_result(returncode=0, stdout="abc1234567890def\n")  # operation start HEAD
        cmd.queue_result(returncode=0, stdout=" M src/foo.py\n")  # dirty check
        cmd.queue_result(returncode=0, stdout=" M src/foo.py\n")  # stage status (untracked=all)
        cmd.queue_result(returncode=0)  # git add -A
        cmd.queue_result(returncode=1)  # git diff --cached --quiet
        cmd.queue_result(returncode=0)  # git commit
        cmd.queue_result(returncode=0, stdout=pr_payload())  # settle fetch
        cmd.queue_result(returncode=0)  # fetch remote branch for committed diff
        cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
        cmd.queue_result(
            returncode=0,
            stdout=_name_status_z("src/foo.py"),
        )  # pre-push protected-scope diff
        cmd.queue_result(returncode=0)  # push
        cmd.queue_result(returncode=0, stdout="head2\n")  # rev-parse
        cmd.queue_result(returncode=0, stdout=json.dumps({"data": {}}))  # resolve thread
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload())  # clean PR
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="MERGESHA\n")  # merge sha

        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=worktrees_root,
        )
        with structlog.testing.capture_logs() as captured:
            await runner.run(
                workspace_id=ws_id,
                compose_project="proj",
                compose_file=tmp_path / "compose.yml",
            )

        commit_calls = [
            call.args
            for call in cmd.calls
            if len(call.args) >= 5
            and call.args[-3:] == ["commit", "-m", "fix: address PR review thread T_dirty"]
        ]
        assert commit_calls
        assert any(call.args[:3] == ["gh", "api", "graphql"] for call in cmd.calls)
        actions = [e["action"] for e in _action_entries(captured)]
        assert actions == ["AddressComments", "Merge"]
        assert any(r.get("event") == "monitor.dirty_worktree_committed" for r in captured)

    @pytest.mark.unit
    async def test_comment_repair_gets_scope_correction_before_committing_protected_file(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await seed_monitoring_workspace(factory)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
            assert workspace is not None
            workspace.owned_paths = ["tests/integration/**"]
            await session.commit()

        thread = thread_node(
            tid="T_workflow",
            author="chatgpt-codex-connector",
            path="tests/integration/test_workspace_agent_git_in_workspace.py",
            body=(
                "This test silently skips when awf-agent-runtime:latest is "
                "absent. Make the test self-sufficient or wire CI to provide "
                "the image."
            ),
        )
        worktrees_root = tmp_path / "worktrees"
        (worktrees_root / ws_id).mkdir(parents=True)

        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload(threads=[thread]))
        adapter.queue(stdout="fixed by editing CI")
        cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
        cmd.queue_result(returncode=0, stdout="abc1234567890def\n")  # operation start HEAD
        cmd.queue_result(
            returncode=0,
            stdout=(
                " M .github/workflows/ci.yml\n"
                " M tests/integration/test_workspace_agent_git_in_workspace.py\n"
            ),
        )  # dirty check after first repair
        cmd.queue_result(returncode=128, stderr="path missing")  # git show protected workflow
        cmd.queue_result(returncode=0)  # ls-tree confirms protected workflow is absent
        adapter.queue(stdout="removed workflow edit; fixed test instead")
        cmd.queue_result(
            returncode=0,
            stdout=" M tests/integration/test_workspace_agent_git_in_workspace.py\n",
        )  # dirty check after scope correction
        cmd.queue_result(
            returncode=0,
            stdout=" M tests/integration/test_workspace_agent_git_in_workspace.py\n",
        )  # stage status (untracked=all)
        cmd.queue_result(returncode=0)  # git add -A
        cmd.queue_result(returncode=1)  # git diff --cached --quiet
        cmd.queue_result(returncode=0)  # git commit
        cmd.queue_result(returncode=0, stdout=pr_payload())  # settle fetch
        cmd.queue_result(returncode=0)  # fetch remote branch for committed diff
        cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
        cmd.queue_result(
            returncode=0,
            stdout=_name_status_z("tests/integration/test_workspace_agent_git_in_workspace.py"),
        )  # pre-push protected-scope diff
        cmd.queue_result(returncode=0)  # push
        cmd.queue_result(returncode=0, stdout="head2\n")  # rev-parse
        cmd.queue_result(returncode=0, stdout=json.dumps({"data": {}}))  # resolve thread
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload())  # clean PR
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="MERGESHA\n")  # merge sha

        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=worktrees_root,
        )

        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

        assert len(adapter.calls) == 2
        assert "outside this workspace's declared owned_paths" in adapter.calls[1]
        assert ".github/workflows/ci.yml" in adapter.calls[1]
        commit_calls = [
            call.args
            for call in cmd.calls
            if len(call.args) >= 5
            and call.args[-3:] == ["commit", "-m", "fix: address PR review thread T_workflow"]
        ]
        assert len(commit_calls) == 1
