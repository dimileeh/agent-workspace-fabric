"""Terminal runtime release preserves salvage evidence while stopping live resources."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.cleanup import (
    CLEANUP_PARTIAL,
    COMPOSE_DOWN_SUCCEEDED,
    WorkspaceCleanupResult,
    WorkspaceCleanupStepResult,
)
from awf.service.controls import WorkspaceControlError, WorkspaceControlService
from awf.service.terminal_runtime import (
    TERMINAL_RUNTIME_RELEASE_CLAIM_LOST_REASON_CODE,
    TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX,
    TERMINAL_RUNTIME_RELEASE_CLAIM_REFRESH_FAILED_REASON_CODE,
    TERMINAL_RUNTIME_RELEASE_SKIPPED_REASON_CODE,
    TerminalRuntimeCleaner,
    TerminalRuntimeReleaser,
    _terminal_runtime_release_post_cleanup_claim_failure_result,
    _TerminalRuntimeReleaseClaimFailure,
    record_terminal_runtime_release_event,
    terminal_runtime_release_claim_active,
)
from tests.postgres import postgres_test_engine

TERMINAL_RUNTIME_TEST_TIMEOUT_SECONDS = 30.0


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _RecordingCleaner:
    def __init__(self, result: WorkspaceCleanupResult | None = None) -> None:
        self.result = result or WorkspaceCleanupResult.from_steps(
            [
                WorkspaceCleanupStepResult(
                    name="compose_down",
                    status="succeeded",
                    reason_code=COMPOSE_DOWN_SUCCEEDED,
                )
            ]
        )
        self.calls: list[dict[str, object]] = []

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> WorkspaceCleanupResult:
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "repo_url": repo_url,
                "compose_project_name": compose_project_name,
                "compose_file_path": compose_file_path,
                "worktree_host_path": worktree_host_path,
                "remove_volumes": remove_volumes,
                "remove_worktree": remove_worktree,
            }
        )
        return self.result


@pytest.mark.unit
def test_terminal_runtime_cleaner_protocol_defaults_preserve_salvage() -> None:
    cleanup_parameters = signature(TerminalRuntimeCleaner.cleanup).parameters

    assert cleanup_parameters["remove_volumes"].default is False
    assert cleanup_parameters["remove_worktree"].default is False


class _FailingCleaner:
    def __init__(self, message: str) -> None:
        self.message = message
        self.calls = 0

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> WorkspaceCleanupResult:
        del (
            workspace_id,
            repo_url,
            compose_project_name,
            compose_file_path,
            worktree_host_path,
            remove_volumes,
            remove_worktree,
        )
        self.calls += 1
        raise RuntimeError(self.message)


class _WorkspaceWriterCleaner:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        workspace_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self.calls = 0

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> WorkspaceCleanupResult:
        del (
            workspace_id,
            repo_url,
            compose_project_name,
            compose_file_path,
            worktree_host_path,
            remove_volumes,
            remove_worktree,
        )
        self.calls += 1
        async with self._session_factory() as session:
            await session.execute(
                text("SELECT id FROM workspaces WHERE id = :workspace_id FOR UPDATE NOWAIT"),
                {"workspace_id": self._workspace_id},
            )
            await WorkspaceRepository(session).update_activity(
                self._workspace_id,
                subphase="cleanup-observed-unlocked-row",
            )
            await session.commit()
        return WorkspaceCleanupResult.from_steps(
            [
                WorkspaceCleanupStepResult(
                    name="compose_down",
                    status="succeeded",
                    reason_code=COMPOSE_DOWN_SUCCEEDED,
                )
            ]
        )


class _RemonitoringCleaner:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        workspace_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self.calls = 0
        self.remonitor_error_code: str | None = None

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> WorkspaceCleanupResult:
        del (
            workspace_id,
            repo_url,
            compose_project_name,
            compose_file_path,
            worktree_host_path,
            remove_volumes,
            remove_worktree,
        )
        self.calls += 1
        async with self._session_factory() as session:
            service = WorkspaceControlService(
                session,
                project_stopper=lambda _compose_project_name: None,
                cleaner_factory=lambda: _RecordingCleaner(),
            )
            try:
                await service.remonitor_workspace(
                    self._workspace_id,
                    reason="operator remonitor raced terminal cleanup",
                )
            except WorkspaceControlError as exc:
                self.remonitor_error_code = exc.error_code
            else:
                self.remonitor_error_code = None
            await session.commit()
        return WorkspaceCleanupResult.from_steps(
            [
                WorkspaceCleanupStepResult(
                    name="compose_down",
                    status="succeeded",
                    reason_code=COMPOSE_DOWN_SUCCEEDED,
                )
            ]
        )


class _ExpiringClaimRemonitoringCleaner:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        workspace_id: str,
        refresh_observed: asyncio.Event,
    ) -> None:
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self._refresh_observed = refresh_observed
        self.calls = 0
        self.remonitor_error_code: str | None = None

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> WorkspaceCleanupResult:
        del (
            workspace_id,
            repo_url,
            compose_project_name,
            compose_file_path,
            worktree_host_path,
            remove_volumes,
            remove_worktree,
        )
        self.calls += 1
        async with self._session_factory() as session:
            workspace = await WorkspaceRepository(session).get(self._workspace_id)
            assert workspace is not None
            workspace.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        self._refresh_observed.clear()
        await self._refresh_observed.wait()

        async with self._session_factory() as session:
            service = WorkspaceControlService(
                session,
                project_stopper=lambda _compose_project_name: None,
                cleaner_factory=lambda: _RecordingCleaner(),
            )
            try:
                await service.remonitor_workspace(
                    self._workspace_id,
                    reason="operator remonitor raced long terminal cleanup",
                )
            except WorkspaceControlError as exc:
                self.remonitor_error_code = exc.error_code
            else:
                self.remonitor_error_code = None
            await session.commit()
        return WorkspaceCleanupResult.from_steps(
            [
                WorkspaceCleanupStepResult(
                    name="compose_down",
                    status="succeeded",
                    reason_code=COMPOSE_DOWN_SUCCEEDED,
                )
            ]
        )


class _StealingClaimCleaner:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        workspace_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self.calls = 0
        self.cancelled = False
        self.completed = False
        self._cancel_gate = asyncio.Event()
        self.stolen_owner_id = f"{TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX}stolen"

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> WorkspaceCleanupResult:
        del (
            workspace_id,
            repo_url,
            compose_project_name,
            compose_file_path,
            worktree_host_path,
            remove_volumes,
            remove_worktree,
        )
        self.calls += 1
        async with self._session_factory() as session:
            workspace = await WorkspaceRepository(session).get(self._workspace_id)
            assert workspace is not None
            assert workspace.execution_claimed_by is not None
            assert workspace.execution_claimed_by.startswith(
                TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX
            )
            workspace.execution_claimed_by = self.stolen_owner_id
            workspace.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            await session.commit()

        try:
            await self._cancel_gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.completed = True
        return WorkspaceCleanupResult.from_steps(
            [
                WorkspaceCleanupStepResult(
                    name="compose_down",
                    status="succeeded",
                    reason_code=COMPOSE_DOWN_SUCCEEDED,
                )
            ]
        )


class _SleepingCleaner:
    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = False
        self.completed = False

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> WorkspaceCleanupResult:
        del (
            workspace_id,
            repo_url,
            compose_project_name,
            compose_file_path,
            worktree_host_path,
            remove_volumes,
            remove_worktree,
        )
        self.calls += 1
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.completed = True
        return WorkspaceCleanupResult.from_steps(
            [
                WorkspaceCleanupStepResult(
                    name="compose_down",
                    status="succeeded",
                    reason_code=COMPOSE_DOWN_SUCCEEDED,
                )
            ]
        )


class _BlockingCleaner:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.completed = False

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> WorkspaceCleanupResult:
        del (
            workspace_id,
            repo_url,
            compose_project_name,
            compose_file_path,
            worktree_host_path,
            remove_volumes,
            remove_worktree,
        )
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.completed = True
        return WorkspaceCleanupResult.from_steps(
            [
                WorkspaceCleanupStepResult(
                    name="compose_down",
                    status="succeeded",
                    reason_code=COMPOSE_DOWN_SUCCEEDED,
                )
            ]
        )


class _RacingTerminalRuntimeReleaser(TerminalRuntimeReleaser):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.snapshot_calls = 0

    async def _snapshot(
        self,
        workspace_id: str,
        *,
        expected_status: WorkspaceStatus | None,
    ) -> Any | None:
        snapshot = await super()._snapshot(workspace_id, expected_status=expected_status)
        self.snapshot_calls += 1
        if self.snapshot_calls == 1 and snapshot is not None:
            async with self._session_factory() as session:
                workspace = await WorkspaceRepository(session).get(snapshot.workspace_id)
                assert workspace is not None
                workspace.status = WorkspaceStatus.monitoring_pr.value
                await session.commit()
        return snapshot


class _UnexpectedStatusRecheckTerminalRuntimeReleaser(TerminalRuntimeReleaser):
    async def _terminal_status_still_matches(
        self,
        workspace_id: str,
        *,
        expected_status: WorkspaceStatus | None,
    ) -> bool:
        del workspace_id, expected_status
        raise AssertionError("release should not perform a redundant status-only recheck")


class _RefreshFailingTerminalRuntimeReleaser(TerminalRuntimeReleaser):
    async def _refresh_terminal_runtime_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str,
    ) -> bool:
        del workspace_id, owner_id
        raise RuntimeError("claim refresh failed")


class _RefreshObservingTerminalRuntimeReleaser(TerminalRuntimeReleaser):
    def __init__(
        self,
        *,
        refresh_observed: asyncio.Event,
        claim_lost_observed: asyncio.Event | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._refresh_observed = refresh_observed
        self._claim_lost_observed = claim_lost_observed

    def _observe_terminal_runtime_claim_refresh_attempt(
        self,
        workspace_id: str,
        *,
        owner_id: str,
        refreshed: bool | None,
    ) -> None:
        del workspace_id, owner_id
        self._refresh_observed.set()
        if refreshed is False and self._claim_lost_observed is not None:
            self._claim_lost_observed.set()


class _ClaimReleaseObservingTerminalRuntimeReleaser(TerminalRuntimeReleaser):
    def __init__(self, *, cleaner: _BlockingCleaner, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._cleaner = cleaner
        self.cleanup_cancelled_before_claim_release: bool | None = None

    async def _release_terminal_runtime_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str,
    ) -> None:
        self.cleanup_cancelled_before_claim_release = self._cleaner.cancelled
        await super()._release_terminal_runtime_claim(
            workspace_id,
            owner_id=owner_id,
        )


class _ImmediateClaimFailureTerminalRuntimeReleaser(TerminalRuntimeReleaser):
    async def _refresh_terminal_runtime_claim_loop(
        self,
        workspace_id: str,
        *,
        owner_id: str,
    ) -> _TerminalRuntimeReleaseClaimFailure:
        del workspace_id, owner_id
        return _TerminalRuntimeReleaseClaimFailure(
            reason_code=TERMINAL_RUNTIME_RELEASE_CLAIM_REFRESH_FAILED_REASON_CODE,
            error="claim refresh failed",
        )

    async def _await_release_step_or_claim_failure(
        self,
        step_task: asyncio.Task[Any],
        claim_refresh_task: asyncio.Task[_TerminalRuntimeReleaseClaimFailure],
    ) -> tuple[Any | None, _TerminalRuntimeReleaseClaimFailure | None]:
        cleanup, claim_failure = await asyncio.gather(step_task, claim_refresh_task)
        return cleanup, claim_failure


async def _seed_failed_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    worktree: Path,
) -> str:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="terminal cleanup",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="SEED")
        workspace.branch_name = f"awf/{workspace.id}"
        workspace.remote_push_branch = workspace.branch_name
        workspace.base_commit = "a" * 40
        workspace.compose_project_name = f"awf_{workspace.id}"
        workspace.compose_file_path = f"/tmp/awf/{workspace.id}/compose.yml"
        workspace.pr_url = "https://github.com/example/repo/pull/12"
        workspace.pr_number = 12
        await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = "primary conformance failure"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="PLAN_CONFORMANCE_UNSATISFIED",
            payload={"details": {"conformance": {"gaps": ["tests"]}}},
        )
        await session.commit()
        workspace_id = workspace.id
    worktree.mkdir(parents=True)
    return workspace_id


@pytest.mark.unit
async def test_terminal_runtime_release_skips_missing_or_unexpected_status_workspaces(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cleaner = _RecordingCleaner()
    releaser = TerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=lambda: cleaner,
    )

    missing = await releaser.release(
        "ws_missing",
        source="test.missing",
        expected_status=WorkspaceStatus.failed,
    )
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=tmp_path / "worktrees" / "status-mismatch",
    )
    mismatch = await releaser.release(
        workspace_id,
        source="test.mismatch",
        expected_status=WorkspaceStatus.completed,
    )

    assert missing.status == "skipped"
    assert missing.reason_code == TERMINAL_RUNTIME_RELEASE_SKIPPED_REASON_CODE
    assert mismatch.status == "skipped"
    assert mismatch.reason_code == TERMINAL_RUNTIME_RELEASE_SKIPPED_REASON_CODE
    assert cleaner.calls == []


@pytest.mark.unit
async def test_terminal_runtime_release_event_skips_missing_and_nonterminal_workspaces(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cleanup = WorkspaceCleanupResult(status="succeeded", reason_code="CLEANUP_SUCCEEDED")
    async with session_factory() as session:
        await record_terminal_runtime_release_event(
            session,
            workspace_id="ws_missing",
            cleanup=cleanup,
            source="test.missing",
        )
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="ready workspace",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.ready.value
        await record_terminal_runtime_release_event(
            session,
            workspace_id=workspace.id,
            cleanup=cleanup,
            source="test.nonterminal",
        )
        events = [
            event
            for event in await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
            if event.event_type.startswith("workspace.terminal_runtime_release")
        ]

    assert events == []


@pytest.mark.unit
def test_terminal_runtime_release_claim_active_handles_missing_and_naive_expiry() -> None:
    no_expiry = SimpleNamespace(
        execution_claimed_by=f"{TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX}owner",
        execution_claim_expires_at=None,
    )
    naive_future = SimpleNamespace(
        execution_claimed_by=f"{TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX}owner",
        execution_claim_expires_at=datetime.now() + timedelta(minutes=1),
    )

    assert not terminal_runtime_release_claim_active(no_expiry)  # type: ignore[arg-type]
    assert terminal_runtime_release_claim_active(naive_future)  # type: ignore[arg-type]


@pytest.mark.unit
def test_post_cleanup_claim_failure_preserves_failed_cleanup_status() -> None:
    cleanup = WorkspaceCleanupResult(
        status="partial",
        reason_code=CLEANUP_PARTIAL,
        steps=(
            WorkspaceCleanupStepResult(
                name="compose_down",
                status="failed",
                reason_code=CLEANUP_PARTIAL,
            ),
        ),
    )

    result = _terminal_runtime_release_post_cleanup_claim_failure_result(
        "ws_claim_lost",
        _TerminalRuntimeReleaseClaimFailure(
            reason_code=TERMINAL_RUNTIME_RELEASE_CLAIM_LOST_REASON_CODE,
        ),
        cleanup=cleanup,
    )

    assert result.status == "failed"
    assert result.reason_code == TERMINAL_RUNTIME_RELEASE_CLAIM_LOST_REASON_CODE
    assert result.cleanup is not None
    assert result.cleanup.reason_code == TERMINAL_RUNTIME_RELEASE_CLAIM_LOST_REASON_CODE
    assert result.cleanup.steps[-1].name == "terminal_runtime_release_claim"


@pytest.mark.unit
async def test_terminal_runtime_release_stops_compose_without_destroying_salvage(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    actual_worktree = worktrees_root / workspace_id
    actual_worktree.mkdir(parents=True, exist_ok=True)
    cleaner = _RecordingCleaner()
    releaser = TerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=lambda: cleaner,
        worktrees_root=worktrees_root,
    )

    result = await releaser.release(
        workspace_id,
        source="test",
        expected_status=WorkspaceStatus.failed,
    )

    assert result.ok
    assert cleaner.calls == [
        {
            "workspace_id": workspace_id,
            "repo_url": "git@github.com:example/repo.git",
            "compose_project_name": f"awf_{workspace_id}",
            "compose_file_path": Path(f"/tmp/awf/{workspace_id}/compose.yml"),
            "worktree_host_path": actual_worktree,
            "remove_volumes": False,
            "remove_worktree": False,
        }
    ]
    assert actual_worktree.exists()
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.failed.value
        assert workspace.failure_reason == FailureReason.agent_failure.value
        assert workspace.failure_message == "primary conformance failure"
        assert workspace.branch_name == f"awf/{workspace_id}"
        assert workspace.remote_push_branch == f"awf/{workspace_id}"
        assert workspace.pr_url == "https://github.com/example/repo/pull/12"
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.terminal_runtime_released",
        )
    assert len(events) == 1
    assert events[0].reason_code == "TERMINAL_RUNTIME_RELEASED"
    assert events[0].payload["cleanup"]["reason_code"] == "CLEANUP_SUCCEEDED"
    assert events[0].payload["preserved"]["worktree_path"] == str(actual_worktree)


@pytest.mark.unit
async def test_terminal_runtime_release_resolves_worktree_path_once_before_claim(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    actual_worktree = worktrees_root / workspace_id
    actual_worktree.mkdir(parents=True, exist_ok=True)
    exists_calls = 0
    original_exists = Path.exists

    def count_actual_worktree_exists(path: Path) -> bool:
        nonlocal exists_calls
        if path == actual_worktree:
            exists_calls += 1
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", count_actual_worktree_exists)
    cleaner = _RecordingCleaner()
    releaser = TerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=lambda: cleaner,
        worktrees_root=worktrees_root,
    )

    result = await releaser.release(
        workspace_id,
        source="test",
        expected_status=WorkspaceStatus.failed,
    )

    assert result.ok
    assert exists_calls == 1
    assert cleaner.calls[0]["worktree_host_path"] == actual_worktree


@pytest.mark.unit
async def test_terminal_runtime_release_omits_absent_preserved_worktree_path(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    missing_worktree = worktrees_root / workspace_id
    cleaner = _RecordingCleaner()
    releaser = TerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=lambda: cleaner,
        worktrees_root=worktrees_root,
    )

    result = await releaser.release(
        workspace_id,
        source="test",
        expected_status=WorkspaceStatus.failed,
    )

    assert result.ok
    assert not missing_worktree.exists()
    assert cleaner.calls[0]["worktree_host_path"] is None
    async with session_factory() as session:
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.terminal_runtime_released",
        )
    assert len(events) == 1
    assert events[0].payload["preserved"] == {
        "branch_name": f"awf/{workspace_id}",
        "remote_push_branch": f"awf/{workspace_id}",
        "pr_url": "https://github.com/example/repo/pull/12",
        "pr_number": 12,
        "failure_reason": FailureReason.agent_failure.value,
        "failure_message": "primary conformance failure",
    }


@pytest.mark.unit
async def test_terminal_runtime_release_rechecks_locked_snapshot_before_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    cleaner = _RecordingCleaner()
    cleaner_factory_calls: list[str] = []

    def cleaner_factory() -> _RecordingCleaner:
        cleaner_factory_calls.append("called")
        return cleaner

    releaser = _RacingTerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=cleaner_factory,
        worktrees_root=worktrees_root,
    )

    result = await releaser.release(
        workspace_id,
        source="test",
        expected_status=WorkspaceStatus.failed,
    )

    assert result.status == "skipped"
    assert result.reason_code == TERMINAL_RUNTIME_RELEASE_SKIPPED_REASON_CODE
    assert releaser.snapshot_calls == 1
    assert cleaner_factory_calls == []
    assert cleaner.calls == []
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.monitoring_pr.value
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.terminal_runtime_released",
        )
    assert events == []


@pytest.mark.unit
async def test_terminal_runtime_release_uses_locked_snapshot_without_extra_status_recheck(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    cleaner = _RecordingCleaner()
    cleaner_factory_calls: list[str] = []

    def cleaner_factory() -> _RecordingCleaner:
        cleaner_factory_calls.append("called")
        return cleaner

    releaser = _UnexpectedStatusRecheckTerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=cleaner_factory,
        worktrees_root=worktrees_root,
    )

    result = await releaser.release(
        workspace_id,
        source="test",
        expected_status=WorkspaceStatus.failed,
    )

    assert result.status == "released"
    assert result.ok
    assert cleaner_factory_calls == ["called"]
    assert cleaner.calls == [
        {
            "workspace_id": workspace_id,
            "repo_url": "git@github.com:example/repo.git",
            "compose_project_name": f"awf_{workspace_id}",
            "compose_file_path": Path(f"/tmp/awf/{workspace_id}/compose.yml"),
            "worktree_host_path": None,
            "remove_volumes": False,
            "remove_worktree": False,
        }
    ]
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.failed.value
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.terminal_runtime_released",
        )
    assert len(events) == 1


@pytest.mark.unit
async def test_terminal_runtime_release_does_not_hold_row_lock_during_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    cleaner = _WorkspaceWriterCleaner(
        session_factory=session_factory,
        workspace_id=workspace_id,
    )
    releaser = TerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=lambda: cleaner,
        worktrees_root=worktrees_root,
    )

    result = await releaser.release(
        workspace_id,
        source="test",
        expected_status=WorkspaceStatus.failed,
    )

    assert result.ok
    assert cleaner.calls == 1
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.subphase == "cleanup-observed-unlocked-row"
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.terminal_runtime_released",
        )
    assert len(events) == 1


@pytest.mark.unit
async def test_terminal_runtime_release_claim_blocks_remonitor_during_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    cleaner = _RemonitoringCleaner(
        session_factory=session_factory,
        workspace_id=workspace_id,
    )
    releaser = TerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=lambda: cleaner,
        worktrees_root=worktrees_root,
    )

    result = await releaser.release(
        workspace_id,
        source="test",
        expected_status=WorkspaceStatus.failed,
    )

    assert result.ok
    assert cleaner.calls == 1
    assert cleaner.remonitor_error_code == "WORKSPACE_TERMINAL_RUNTIME_RELEASE_IN_PROGRESS"
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.failed.value
        assert workspace.execution_claimed_by is None
        assert workspace.execution_claim_expires_at is None
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.terminal_runtime_released",
        )
    assert len(events) == 1


@pytest.mark.unit
async def test_terminal_runtime_release_skips_active_release_claim(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    existing_owner_id = f"{TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX}existing"
    existing_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.execution_claimed_by = existing_owner_id
        workspace.execution_claim_expires_at = existing_expires_at
        await session.commit()
    cleaner = _RecordingCleaner()
    releaser = TerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=lambda: cleaner,
        worktrees_root=worktrees_root,
    )

    result = await releaser.release(
        workspace_id,
        source="test",
        expected_status=WorkspaceStatus.failed,
    )

    assert result.status == "skipped"
    assert result.reason_code == TERMINAL_RUNTIME_RELEASE_SKIPPED_REASON_CODE
    assert cleaner.calls == []
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.execution_claimed_by == existing_owner_id
        assert workspace.execution_claim_expires_at is not None
        assert workspace.execution_claim_expires_at.replace(tzinfo=UTC) == (existing_expires_at)
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.terminal_runtime_released",
        )
    assert events == []


@pytest.mark.unit
async def test_terminal_runtime_release_refreshes_claim_during_long_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    refresh_observed = asyncio.Event()
    cleaner = _ExpiringClaimRemonitoringCleaner(
        session_factory=session_factory,
        workspace_id=workspace_id,
        refresh_observed=refresh_observed,
    )
    releaser = _RefreshObservingTerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=lambda: cleaner,
        worktrees_root=worktrees_root,
        claim_refresh_interval_seconds=0.01,
        refresh_observed=refresh_observed,
    )

    result = await asyncio.wait_for(
        releaser.release(
            workspace_id,
            source="test",
            expected_status=WorkspaceStatus.failed,
        ),
        timeout=TERMINAL_RUNTIME_TEST_TIMEOUT_SECONDS,
    )

    assert result.ok
    assert cleaner.calls == 1
    assert cleaner.remonitor_error_code == "WORKSPACE_TERMINAL_RUNTIME_RELEASE_IN_PROGRESS"
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.failed.value
        assert workspace.execution_claimed_by is None
        assert workspace.execution_claim_expires_at is None
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.terminal_runtime_released",
        )
    assert len(events) == 1


@pytest.mark.unit
async def test_terminal_runtime_release_fails_when_claim_is_lost_during_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    cleaner = _StealingClaimCleaner(
        session_factory=session_factory,
        workspace_id=workspace_id,
    )
    refresh_observed = asyncio.Event()
    claim_lost_observed = asyncio.Event()
    releaser = _RefreshObservingTerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=lambda: cleaner,
        worktrees_root=worktrees_root,
        claim_refresh_interval_seconds=0.01,
        refresh_observed=refresh_observed,
        claim_lost_observed=claim_lost_observed,
    )

    release_task = asyncio.create_task(
        releaser.release(
            workspace_id,
            source="test",
            expected_status=WorkspaceStatus.failed,
        )
    )
    try:
        await asyncio.wait_for(
            claim_lost_observed.wait(),
            timeout=TERMINAL_RUNTIME_TEST_TIMEOUT_SECONDS,
        )
        result = await asyncio.wait_for(
            release_task,
            timeout=TERMINAL_RUNTIME_TEST_TIMEOUT_SECONDS,
        )
    finally:
        if not release_task.done():
            release_task.cancel()
            with suppress(asyncio.CancelledError):
                await release_task

    assert not result.ok
    assert result.status == "failed"
    assert result.reason_code == TERMINAL_RUNTIME_RELEASE_CLAIM_LOST_REASON_CODE
    assert cleaner.calls == 1
    assert cleaner.cancelled
    assert not cleaner.completed
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.execution_claimed_by == cleaner.stolen_owner_id
        assert workspace.execution_claim_expires_at is not None
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)
    assert [
        event.event_type
        for event in events
        if event.event_type.startswith("workspace.terminal_runtime_release")
    ] == []


@pytest.mark.unit
async def test_terminal_runtime_release_fails_when_claim_refresh_fails_during_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    cleaner = _SleepingCleaner()
    releaser = _RefreshFailingTerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=lambda: cleaner,
        worktrees_root=worktrees_root,
        claim_refresh_interval_seconds=0.01,
    )

    result = await releaser.release(
        workspace_id,
        source="test",
        expected_status=WorkspaceStatus.failed,
    )

    assert not result.ok
    assert result.status == "failed"
    assert result.reason_code == TERMINAL_RUNTIME_RELEASE_CLAIM_REFRESH_FAILED_REASON_CODE
    assert cleaner.calls == 1
    assert cleaner.cancelled
    assert not cleaner.completed
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.execution_claimed_by is None
        assert workspace.execution_claim_expires_at is None
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)
    assert [
        event.event_type
        for event in events
        if event.event_type.startswith("workspace.terminal_runtime_release")
    ] == []


@pytest.mark.unit
async def test_terminal_runtime_release_succeeds_when_final_claim_refresh_fails_after_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    cleaner = _RecordingCleaner()
    releaser = _RefreshFailingTerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=lambda: cleaner,
        worktrees_root=worktrees_root,
    )

    result = await releaser.release(
        workspace_id,
        source="test",
        expected_status=WorkspaceStatus.failed,
    )

    assert result.ok
    assert result.status == "released"
    assert result.reason_code == TERMINAL_RUNTIME_RELEASE_CLAIM_REFRESH_FAILED_REASON_CODE
    assert result.cleanup is not None
    assert result.cleanup.ok
    assert cleaner.calls == [
        {
            "workspace_id": workspace_id,
            "repo_url": "git@github.com:example/repo.git",
            "compose_project_name": f"awf_{workspace_id}",
            "compose_file_path": Path(f"/tmp/awf/{workspace_id}/compose.yml"),
            "worktree_host_path": None,
            "remove_volumes": False,
            "remove_worktree": False,
        }
    ]
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.execution_claimed_by is None
        assert workspace.execution_claim_expires_at is None
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.terminal_runtime_released",
        )
    assert len(events) == 1
    assert events[0].payload["cleanup"]["reason_code"] == "CLEANUP_SUCCEEDED"


@pytest.mark.unit
async def test_terminal_runtime_release_reports_simultaneous_claim_failure() -> None:
    releaser = TerminalRuntimeReleaser(
        session_factory=None,  # type: ignore[arg-type] - helper does not touch storage.
        cleaner_factory=_RecordingCleaner,
    )
    cleanup = WorkspaceCleanupResult.from_steps(
        [
            WorkspaceCleanupStepResult(
                name="compose_down",
                status="succeeded",
                reason_code=COMPOSE_DOWN_SUCCEEDED,
            )
        ]
    )

    async def _return(value: Any) -> Any:
        return value

    claim_failure = _TerminalRuntimeReleaseClaimFailure(
        reason_code=TERMINAL_RUNTIME_RELEASE_CLAIM_REFRESH_FAILED_REASON_CODE,
        error="claim refresh failed",
    )
    cleanup_task = asyncio.create_task(_return(cleanup))
    claim_failure_task = asyncio.create_task(_return(claim_failure))
    await asyncio.gather(cleanup_task, claim_failure_task)

    cleanup_result, observed_claim_failure = await releaser._await_release_step_or_claim_failure(
        cleanup_task,
        claim_failure_task,
    )

    assert cleanup_result is cleanup
    assert observed_claim_failure is claim_failure


@pytest.mark.unit
async def test_terminal_runtime_release_records_cleanup_evidence_on_simultaneous_claim_failure(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    cleaner = _RecordingCleaner()
    releaser = _ImmediateClaimFailureTerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=lambda: cleaner,
        worktrees_root=worktrees_root,
    )

    result = await releaser.release(
        workspace_id,
        source="test",
        expected_status=WorkspaceStatus.failed,
    )

    assert not result.ok
    assert result.reason_code == TERMINAL_RUNTIME_RELEASE_CLAIM_REFRESH_FAILED_REASON_CODE
    assert cleaner.calls == [
        {
            "workspace_id": workspace_id,
            "repo_url": "git@github.com:example/repo.git",
            "compose_project_name": f"awf_{workspace_id}",
            "compose_file_path": Path(f"/tmp/awf/{workspace_id}/compose.yml"),
            "worktree_host_path": None,
            "remove_volumes": False,
            "remove_worktree": False,
        }
    ]
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.execution_claimed_by is None
        assert workspace.execution_claim_expires_at is None
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.terminal_runtime_released",
        )
    assert len(events) == 1
    assert events[0].payload["cleanup"]["reason_code"] == "CLEANUP_SUCCEEDED"


@pytest.mark.unit
async def test_terminal_runtime_release_cancellation_cancels_cleanup_before_claim_release(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    cleaner = _BlockingCleaner()
    releaser = _ClaimReleaseObservingTerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=lambda: cleaner,
        worktrees_root=worktrees_root,
        cleaner=cleaner,
    )

    release_task = asyncio.create_task(
        releaser.release(
            workspace_id,
            source="test",
            expected_status=WorkspaceStatus.failed,
        )
    )
    await asyncio.wait_for(
        cleaner.started.wait(),
        timeout=TERMINAL_RUNTIME_TEST_TIMEOUT_SECONDS,
    )

    release_task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await release_task

        assert cleaner.calls == 1
        assert cleaner.cancelled
        assert not cleaner.completed
        assert releaser.cleanup_cancelled_before_claim_release is True
        async with session_factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            assert workspace.execution_claimed_by is None
            assert workspace.execution_claim_expires_at is None
    finally:
        cleaner.release.set()
        await asyncio.sleep(0)


@pytest.mark.unit
async def test_terminal_runtime_release_failure_preserves_primary_failure_cause(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    cleaner = _RecordingCleaner(
        WorkspaceCleanupResult(
            status="partial",
            reason_code=CLEANUP_PARTIAL,
            steps=(
                WorkspaceCleanupStepResult(
                    name="compose_down",
                    status="failed",
                    reason_code="DOCKER_UNAVAILABLE",
                    error="cannot reach docker",
                ),
            ),
        )
    )
    releaser = TerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=lambda: cleaner,
        worktrees_root=worktrees_root,
    )

    result = await releaser.release(
        workspace_id,
        source="test",
        expected_status=WorkspaceStatus.failed,
    )

    assert not result.ok
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.failed.value
        assert workspace.failure_reason == FailureReason.agent_failure.value
        assert workspace.failure_message == "primary conformance failure"
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.terminal_runtime_release_failed",
        )
    assert len(events) == 1
    assert events[0].reason_code == "TERMINAL_RUNTIME_RELEASE_FAILED"
    assert events[0].payload["cleanup"]["failed_steps"][0]["reason_code"] == "DOCKER_UNAVAILABLE"


@pytest.mark.unit
async def test_terminal_runtime_release_redacts_cleanup_exception_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    credentialed_url = "https://svc-user:super-secret-token@github.com/example/private.git"
    api_token = "ghp_1234567890abcdef"
    cleaner = _FailingCleaner(
        f"cleanup failed for {credentialed_url} with GITHUB_TOKEN={api_token}"
    )
    releaser = TerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=lambda: cleaner,
        worktrees_root=worktrees_root,
    )

    result = await releaser.release(
        workspace_id,
        source="test",
        expected_status=WorkspaceStatus.failed,
    )

    assert not result.ok
    assert cleaner.calls == 1
    assert result.cleanup is not None
    failed_step = result.cleanup.failed_steps[0]
    assert failed_step.error is not None
    assert "super-secret-token" not in failed_step.error
    assert api_token not in failed_step.error
    assert "https://[redacted]@github.com/example/private.git" in failed_step.error
    assert "GITHUB_TOKEN=[redacted]" in failed_step.error
    async with session_factory() as session:
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.terminal_runtime_release_failed",
        )
    assert len(events) == 1
    persisted_error = events[0].payload["cleanup"]["failed_steps"][0]["error"]
    assert persisted_error == failed_step.error


@pytest.mark.unit
async def test_terminal_runtime_release_returns_result_when_event_recording_fails(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktrees_root = tmp_path / "git" / "worktrees"
    workspace_id = await _seed_failed_workspace(
        session_factory,
        worktree=worktrees_root / "ws-placeholder",
    )
    cleaner = _RecordingCleaner()
    releaser = TerminalRuntimeReleaser(
        session_factory=session_factory,
        cleaner_factory=lambda: cleaner,
        worktrees_root=worktrees_root,
    )

    async def _raise_event_recording_failure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("release event store unavailable")

    monkeypatch.setattr(
        "awf.service.terminal_runtime.record_terminal_runtime_release_event",
        _raise_event_recording_failure,
    )

    with capture_logs() as captured:
        result = await releaser.release(
            workspace_id,
            source="test",
            expected_status=WorkspaceStatus.failed,
        )

    assert result.ok
    assert cleaner.calls
    async with session_factory() as session:
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.terminal_runtime_released",
        )
    assert events == []
    assert any(
        entry["event"] == "terminal_runtime.release_event_record_failed"
        and entry["workspace_id"] == workspace_id
        for entry in captured
    )


@pytest.mark.unit
async def test_terminal_runtime_release_event_omits_absent_optional_metadata(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cleanup = WorkspaceCleanupResult.from_steps(
        [
            WorkspaceCleanupStepResult(
                name="compose_down",
                status="succeeded",
                reason_code=COMPOSE_DOWN_SUCCEEDED,
            )
        ]
    )
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="terminal cleanup without optional metadata",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.failed, reason_code="SEED")
        workspace_id = workspace.id

        await record_terminal_runtime_release_event(
            session,
            workspace_id=workspace_id,
            cleanup=cleanup,
            source="test",
        )
        await session.commit()

    async with session_factory() as session:
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.terminal_runtime_released",
        )

    assert len(events) == 1
    payload = events[0].payload
    assert payload["runtime"] == {
        "remove_volumes": False,
        "remove_worktree": False,
    }
    assert payload["preserved"] == {}
