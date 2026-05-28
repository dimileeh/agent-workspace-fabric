"""Additional PR-monitor resume and task-kind executor error-path coverage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.profiles.models import ProfileDocker, WorkspaceProfile
from awf.runtime.pr_creator import PullRequestResult
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_006 import (
    _make_executor,
    _RecordingValidation,
    _seed_monitoring_pr,
    _seed_ready,
    factory,
    fake,
)

_IMPORTED_FIXTURES = (factory, fake)


class TestExecutorCoverageEdgesPart003:
    @pytest.mark.unit
    async def test_resume_pr_monitor_preserves_profile_compose_timeout_when_companion_resolution_fails(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        captured: dict[str, Any] = {}

        class _RecordingCompose:
            async def ensure_project_up(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                wait: bool = True,
                compose_up_timeout_seconds: int = 300,
            ) -> None:
                captured.update(
                    {
                        "project_name": project_name,
                        "compose_file": compose_file,
                        "workspace_id": workspace_id,
                        "wait": wait,
                        "compose_up_timeout_seconds": compose_up_timeout_seconds,
                    }
                )

        class _Monitor:
            async def run(
                self,
                *,
                workspace_id: str,
                compose_project: str,
                compose_file: Path,
            ) -> None:
                del workspace_id, compose_project, compose_file

        ws_id = await _seed_monitoring_pr(
            factory,
            resolved_profile=WorkspaceProfile(
                name="profile-timeout",
                docker=ProfileDocker(startup_timeout_seconds=720),
            ).model_dump(mode="json"),
            task_policy={"companions": [{"name": "broken-companion"}]},
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            compose=_RecordingCompose(),
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        await executor.resume_pr_monitor(ws_id)

        assert captured["workspace_id"] == ws_id
        assert captured["compose_up_timeout_seconds"] == 720

    @pytest.mark.unit
    async def test_resume_pr_monitor_never_recreates_pr_or_runs_feature_agent(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_calls: list[str] = []
        validation = _RecordingValidation()

        class _UnexpectedPrCreator:
            async def push_and_open(self, **_kwargs: Any) -> PullRequestResult:
                raise AssertionError("resume_pr_monitor must not push or create a PR")

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                assert compose_project == "awf_x"
                assert compose_file == Path("/tmp/awf/x/compose.yml")
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(factory)
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_creator=_UnexpectedPrCreator(),
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        await executor.resume_pr_monitor(ws_id)

        assert monitor_calls == [ws_id]
        assert validation.calls == []
        assert fake.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.pr_url == "https://github.com/x/y/pull/42"

    @pytest.mark.unit
    async def test_resume_pr_monitor_factory_failure_marks_recovery_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)

        def _factory(*_args: Any) -> object:
            raise RuntimeError("factory broke")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_factory)

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "failed to build PR monitor" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "MONITOR_RECOVERY_FAILED"

    @pytest.mark.unit
    async def test_resume_pr_monitor_without_configured_monitor_fails_cleanly(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        executor = _make_executor(fake, factory, tmp_path)

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message == "monitor recovery: no PR monitor configured"
            assert ws.events[-1].reason_code == "MONITOR_RECOVERY_FAILED"
            assert not [
                event
                for event in ws.events
                if event.event_type == "workspace.remote_push_branch_recovered"
            ]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "field",
        [
            "pr_number",
            "pr_url",
            "compose_project_name",
            "compose_file_path",
        ],
    )
    async def test_missing_monitor_recovery_metadata_fails_cleanly(
        self,
        field: str,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        kwargs: dict[str, Any] = {field: None}
        ws_id = await _seed_monitoring_pr(factory, **kwargs)

        def _monitor_factory(*_args: Any) -> object:
            raise AssertionError("monitor factory must not run for invalid recovery rows")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert field in (ws.failure_message or "")
            assert "monitor recovery" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "MONITOR_RECOVERY_METADATA_MISSING"

    @pytest.mark.unit
    @pytest.mark.parametrize("task_kind", ["sync_release_pr", "sync_feature_pr"])
    async def test_sync_and_release_resume_fail_when_remote_push_branch_is_unknown(
        self,
        task_kind: str,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_pr(
            factory,
            task_kind=task_kind,
            branch_name="release-sync/local-only",
            remote_push_branch=None,
        )

        def _monitor_factory(*_args: Any) -> object:
            raise AssertionError("monitor factory must not run without a safe remote branch")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "remote_push_branch" in (ws.failure_message or "")
            assert task_kind in (ws.failure_message or "")
            assert ws.remote_push_branch is None


class TestTaskKindFailFast:
    @pytest.mark.unit
    async def test_legacy_monitor_release_pr_fails_fast_without_feature_work(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        validation = _RecordingValidation()

        class _UnexpectedPrCreator:
            async def push_and_open(self, **_kwargs: Any) -> PullRequestResult:
                raise AssertionError("deprecated kinds must not create a PR")

        ws_id = await _seed_ready(factory, task_kind="monitor_release_pr")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_creator=_UnexpectedPrCreator(),
        )

        await executor.execute(ws_id)

        assert validation.calls == []
        assert fake.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "policy_failure"
            assert "deprecated" in (ws.failure_message or "")
            assert "auto_merge=false" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "DEPRECATED_TASK_KIND"

    @pytest.mark.unit
    async def test_unknown_task_kind_fails_fast_without_feature_work(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        validation = _RecordingValidation()
        ws_id = await _seed_ready(factory, task_kind="totally_made_up")

        executor = _make_executor(fake, factory, tmp_path, validation=validation)

        await executor.execute(ws_id)

        assert validation.calls == []
        assert fake.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "policy_failure"
            assert "unsupported task kind" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "UNSUPPORTED_TASK_KIND"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("task_kind", "message_fragment", "reason_code"),
        [
            ("monitor_release_pr", "deprecated", "DEPRECATED_TASK_KIND"),
            ("totally_made_up", "unsupported task kind", "UNSUPPORTED_TASK_KIND"),
        ],
    )
    async def test_resume_pr_monitor_fails_fast_on_unsupported_task_kind(
        self,
        task_kind: str,
        message_fragment: str,
        reason_code: str,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """A legacy deprecated/unsupported row already in ``monitoring_pr`` must
        fail fast on the worker-restart resume path instead of being monitored
        forever. The guard is shared with execute() but transitions from
        ``monitoring_pr`` here, so the failure actually lands.
        """
        ws_id = await _seed_monitoring_pr(factory, task_kind=task_kind)

        def _monitor_factory(*_args: Any) -> object:
            raise AssertionError("resume must not build a monitor for unsupported kinds")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        await executor.resume_pr_monitor(ws_id)

        assert fake.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "policy_failure"
            assert message_fragment in (ws.failure_message or "")
            assert ws.events[-1].reason_code == reason_code

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("task_kind", "reason_code"),
        [
            ("monitor_release_pr", "DEPRECATED_TASK_KIND"),
            ("totally_made_up", "UNSUPPORTED_TASK_KIND"),
        ],
    )
    async def test_reject_unsupported_task_kind_finalizes_active_recovery(
        self,
        task_kind: str,
        reason_code: str,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """A reclaimed workspace whose deprecated/unsupported kind is rejected
        must also finalize any active validate/rebase recovery operation. This is
        the worker-restart salvage of a stale ``running`` claim: failing the
        workspace without closing the recovery row would strand it pending/running
        forever. The fail-fast guard runs before recovery dispatch, so it owns the
        cleanup itself.
        """
        ws_id = await _seed_ready(factory, task_kind=task_kind)
        async with factory() as s:
            op = await OperationRepository(s).create(
                workspace_id=ws_id,
                operation_type=OperationType.validate,
                status=OperationStatus.running,
                payload={"source": "worker_restart", "recovery_mode": "validate_only"},
                idempotency_key=f"worker_restart:validate_only:{ws_id}",
            )
            op_id = op.id
            await s.commit()

        executor = _make_executor(fake, factory, tmp_path)

        await executor.execute(ws_id)

        assert fake.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "policy_failure"
            assert ws.events[-1].reason_code == reason_code
            op = await OperationRepository(s).get(op_id)
            assert op is not None
            assert op.status == OperationStatus.failed.value
            assert op.error_code == reason_code
            assert op.result == {"reason_code": reason_code}
            assert op.finished_at is not None
