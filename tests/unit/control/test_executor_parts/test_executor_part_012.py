"""Monitor handoff and idempotency coverage split from executor part_005."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import COMMAND_IDLE_TIMEOUT_REASON, FakeCommandRunner
from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner
from tests.unit.control.test_executor_parts.test_executor_part_005 import (
    _TEMPLATE,
    _insert_validate_handoff_recovery_operation,
    _json_value,
    _queue_pre_push_diagnostics,
    _queue_validation_head,
    _seed_ready_workspace,
    executor,
    factory,
    fake,
)

_IMPORTED_FIXTURES = (executor, factory, fake)


class TestMonitorHandoff:
    """When a PR monitor is wired, the executor transitions ``pushing →
    monitoring_pr`` and delegates the final transition to the monitor."""

    @pytest.mark.unit
    async def test_hands_off_to_monitor_and_records_pr_number(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        from awf.db.enums import WorkspaceStatus as _WS  # noqa: N814

        class _StubMonitor:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._factory = factory

            async def run(
                self,
                *,
                workspace_id: str,
                compose_project: str,
                compose_file: Path,
            ) -> None:
                self.calls.append(
                    {
                        "workspace_id": workspace_id,
                        "compose_project": compose_project,
                        "compose_file": compose_file,
                    }
                )
                # Pretend the monitor merged + flipped state to completed.
                async with self._factory() as s:
                    ws = await WorkspaceRepository(s).get(workspace_id)
                    assert ws is not None
                    assert ws.status == _WS.monitoring_pr.value
                    await WorkspaceRepository(s).transition(
                        ws, to=_WS.completed, reason_code="STUB_MERGE"
                    )
                    ws.pr_merge_sha = "stub_merge_sha"
                    await s.commit()

        monitor = _StubMonitor()
        compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        validation = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
        pr = PullRequestCreator(fake)
        ex = WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=compose,
            validation=validation,
            pr_creator=pr,
            config=ExecutorConfig(
                worktrees_root=tmp_path / "work" / "worktrees",
                compose_projects_root=tmp_path / "work" / "compose",
                default_models={
                    AgentRuntime.codex: "gpt-5",
                    AgentRuntime.claude_code: "sonnet",
                    AgentRuntime.gemini: "gemini-2.5-pro",
                },
            ),
            pr_monitor=monitor,
        )

        stored_compose_file = tmp_path / "rendered-compose" / "ws" / "compose.yml"
        ws_id = await _seed_ready_workspace(
            factory,
            compose_file_path=str(stored_compose_file),
        )
        # 9-step sequence (same as happy path).
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation cmd
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # push
        fake.queue_result(
            returncode=0,
            stdout="https://github.com/dimileeh/aira-web/pull/7777\n",
        )

        await ex.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == _WS.completed.value
            assert ws.pr_url == "https://github.com/dimileeh/aira-web/pull/7777"
            assert ws.pr_number == 7777
            assert ws.remote_push_branch == f"awf/{ws_id}"
            assert ws.pr_merge_sha == "stub_merge_sha"
            transitions = [(e.old_state, e.new_state) for e in ws.events]
            assert ("pushing", "monitoring_pr") in transitions
            assert ("monitoring_pr", "completed") in transitions
        # Monitor received the hand-off call with the right IDs.
        assert len(monitor.calls) == 1
        assert monitor.calls[0]["workspace_id"] == ws_id
        assert monitor.calls[0]["compose_file"] == stored_compose_file


class TestPrNumberExtraction:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://github.com/dimileeh/aira-web/pull/123", 123),
            ("https://github.com/dimileeh/aira-web/pull/123/", 123),
            ("https://github.com/dimileeh/aira-web/pull/123/files", 123),
            ("https://github.com/dimileeh/aira-web/pull/123?notification_referrer_id=abc", 123),
            ("https://github.com/dimileeh/aira-web/pull/123#discussion_r3275054005", 123),
            # Bitbucket PRs use ``/pull-requests/<n>`` — the forge-neutral
            # creation path persists this URL verbatim, so extraction must
            # accept it or the monitor fails with ``missing_pr_number``.
            ("https://bitbucket.org/workspace/repo/pull-requests/7", 7),
            ("https://bitbucket.org/workspace/repo/pull-requests/7/", 7),
            ("https://bitbucket.org/workspace/repo/pull-requests/7/diff", 7),
            ("https://bitbucket.org/workspace/repo/pull-requests/7?foo=bar", 7),
            ("https://bitbucket.org/workspace/repo/pull-requests/7#comment-1", 7),
            ("not a url", None),
            ("https://github.com/dimileeh/aira-web/issues/5", None),
        ],
    )
    def test_extract_pr_number(self, url: str, expected: int | None) -> None:
        from awf.control.executor.helpers import _extract_pr_number

        assert _extract_pr_number(url) == expected


class TestIdempotency:
    @pytest.mark.unit
    async def test_refuses_to_run_on_non_ready_workspace(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Seed then drive to completed via a first execute call.
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # push
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")  # gh pr create
        await executor.execute(ws_id)

        # Second call must be a no-op — status is completed.
        calls_before = len(fake.calls)
        await executor.execute(ws_id)
        assert len(fake.calls) == calls_before

    @pytest.mark.unit
    async def test_unknown_workspace_is_silent_noop(
        self, executor: WorkspaceExecutor, fake: FakeCommandRunner
    ) -> None:
        await executor.execute("ws_never_existed")
        assert fake.calls == []


class TestPlanningValidationHandoffCleanup:
    @pytest.mark.unit
    async def test_planning_validation_handoff_cleanup_failure_finishes_validate_operation(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned-recovery",
                "planning": {
                    "required": True,
                    "plan_path": "docs/awf-plans/{workspace_id}.md",
                    "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
                    "max_iterations": 2,
                },
                "phases": {"validate": ["pytest -q"]},
            },
        )
        await _insert_validate_handoff_recovery_operation(
            factory,
            workspace_id=ws_id,
            operation_id="op_validate_handoff_cleanup_failed",
        )

        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation
        fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # conformance scope HEAD
        fake.queue_result(
            returncode=124,
            stderr="idle timeout exceeded",
            reason_code=COMMAND_IDLE_TIMEOUT_REASON,
        )
        fake.queue_result(returncode=1, stderr="cleanup still saw tagged processes")

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            run = (
                (
                    await s.execute(
                        text(
                            """
                            SELECT status, reason_code
                            FROM validation_runs
                            WHERE workspace_id = :workspace_id
                            """
                        ),
                        {"workspace_id": ws_id},
                    )
                )
                .mappings()
                .one()
            )
            operation = (
                (
                    await s.execute(
                        text(
                            """
                            SELECT status, error_code, error_message, result, finished_at
                            FROM operations
                            WHERE id = 'op_validate_handoff_cleanup_failed'
                            """
                        )
                    )
                )
                .mappings()
                .one()
            )

        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert EXEC_PROCESS_CLEANUP_FAILED in (ws.failure_message or "")
        assert run == {"status": "succeeded", "reason_code": "VALIDATION_OK"}
        assert operation["status"] == "failed"
        assert operation["error_code"] == EXEC_PROCESS_CLEANUP_FAILED
        assert operation["finished_at"] is not None
        assert EXEC_PROCESS_CLEANUP_FAILED in operation["error_message"]
        result = _json_value(operation["result"])
        assert result["reason_code"] == EXEC_PROCESS_CLEANUP_FAILED
        assert result["validation_run_id"]
