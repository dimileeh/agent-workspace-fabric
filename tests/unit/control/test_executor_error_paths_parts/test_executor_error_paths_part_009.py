"""Additional PR-monitor resume and task-kind executor error-path coverage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import BitbucketClientError
from awf.common.commands import FakeCommandRunner
from awf.common.forge import ForgeNotSupportedError
from awf.control.executor import monitor_handoff as executor_monitor_handoff
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
                force_recreate: bool = False,
                services: tuple[str, ...] = (),
            ) -> None:
                captured.update(
                    {
                        "project_name": project_name,
                        "compose_file": compose_file,
                        "workspace_id": workspace_id,
                        "wait": wait,
                        "compose_up_timeout_seconds": compose_up_timeout_seconds,
                        "force_recreate": force_recreate,
                        "services": services,
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
    async def test_resume_pr_monitor_retries_profile_sync_before_monitor_factory(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime_profile = WorkspaceProfile(name="runtime-unsynced")
        synced_profile = WorkspaceProfile(name="db-synced")
        sync_attempts: list[str] = []
        factory_profile_names: list[str] = []
        monitor_calls: list[str] = []

        def _profile_for_workspace(*_args: Any, **_kwargs: Any) -> WorkspaceProfile:
            return runtime_profile

        async def _sync_resolved_profile(
            self: Any,
            *,
            ws: Any,
            workspace_id: str,
            profile: WorkspaceProfile,
            planning_max_iterations_default: int,
        ) -> WorkspaceProfile:
            del planning_max_iterations_default
            sync_attempts.append(profile.name)
            if len(sync_attempts) == 1:
                raise RuntimeError("transient profile sync failure")
            snapshot = synced_profile.model_dump(mode="json", by_alias=True)
            async with self._session_factory() as session:
                stored = await WorkspaceRepository(session).get(workspace_id)
                assert stored is not None
                stored.resolved_profile = snapshot
                await session.commit()
            ws.resolved_profile = snapshot
            return synced_profile

        def _get_adapter(*_args: Any, **_kwargs: Any) -> object:
            return object()

        class _Monitor:
            async def run(
                self,
                *,
                workspace_id: str,
                compose_project: str,
                compose_file: Path,
            ) -> None:
                del compose_project, compose_file
                monitor_calls.append(workspace_id)

        def _monitor_factory(
            _adapter: Any,
            profile: WorkspaceProfile,
            _workspace: Any,
        ) -> _Monitor:
            factory_profile_names.append(profile.name)
            return _Monitor()

        monkeypatch.setattr(
            executor_monitor_handoff,
            "_profile_for_workspace",
            _profile_for_workspace,
        )
        monkeypatch.setattr(
            executor_monitor_handoff,
            "_sync_resolved_profile",
            _sync_resolved_profile,
        )
        monkeypatch.setattr(executor_monitor_handoff, "get_adapter", _get_adapter)

        ws_id = await _seed_monitoring_pr(factory)
        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        await executor.resume_pr_monitor(ws_id)

        assert sync_attempts == ["runtime-unsynced", "runtime-unsynced"]
        assert factory_profile_names == ["db-synced"]
        assert monitor_calls == [ws_id]
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
            assert ws.resolved_profile == synced_profile.model_dump(mode="json", by_alias=True)

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
    async def test_resume_pr_monitor_preserves_forge_not_supported_reason_code(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """An unsupported forge (e.g. Bitbucket) detected while the monitor
        factory builds its forge client must surface as ``FORGE_NOT_SUPPORTED``,
        not the generic ``MONITOR_RECOVERY_FAILED``.

        ``resume_pr_monitor`` is the worker-restart resume path, which bypasses
        the execute-time forge gate in ``execution_flow`` (that gate only runs
        for ``ready -> execution``). So a ``monitoring_pr`` row whose forge is
        unsupported first trips here; the stable reason code and its doctor
        guidance must be preserved end-to-end.
        """
        ws_id = await _seed_monitoring_pr(factory)

        def _factory(*_args: Any) -> object:
            raise ForgeNotSupportedError(
                message=(
                    "Bitbucket forge support is not yet implemented "
                    "(issue #345 Phase 1 adds detection only)."
                )
            )

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_factory)

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.events[-1].reason_code == "FORGE_NOT_SUPPORTED"
            assert "monitor recovery" in (ws.failure_message or "")
            assert "Bitbucket forge support is not yet implemented" in (ws.failure_message or "")
            # Must not be flattened into the generic build-failed reason/message.
            assert "failed to build PR monitor" not in (ws.failure_message or "")
            assert not [
                event for event in ws.events if event.reason_code == "MONITOR_RECOVERY_FAILED"
            ]

    @pytest.mark.unit
    async def test_resume_pr_monitor_preserves_bitbucket_auth_reason_code(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """A Bitbucket workspace missing credentials raises ``BitbucketClientError``
        (e.g. ``BITBUCKET_AUTH_NOT_CONFIGURED``) when the monitor factory builds its
        forge client. The resume path must preserve that actionable auth reason code
        instead of flattening it into the generic ``MONITOR_RECOVERY_FAILED`` —
        mirroring the initial handoff path's ``BitbucketClientError`` catch.
        """
        ws_id = await _seed_monitoring_pr(factory)

        def _factory(*_args: Any) -> object:
            raise BitbucketClientError(
                operation="bitbucket_client_from_env",
                status=None,
                body="BITBUCKET_API_TOKEN/BITBUCKET_EMAIL not configured",
                reason_code="BITBUCKET_AUTH_NOT_CONFIGURED",
            )

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_factory)

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.events[-1].reason_code == "BITBUCKET_AUTH_NOT_CONFIGURED"
            assert "monitor recovery" in (ws.failure_message or "")
            # Must not be flattened into the generic build-failed reason/message.
            assert "failed to build PR monitor" not in (ws.failure_message or "")
            assert not [
                event for event in ws.events if event.reason_code == "MONITOR_RECOVERY_FAILED"
            ]

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
