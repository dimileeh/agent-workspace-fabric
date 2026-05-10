"""Workspace control lifecycle behavior tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy import update as sa_update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import MergeCandidate, Operation, Workspace, WorkspaceEvent
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    SecretLeaseIssue,
    SecretLeaseRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
    WorkspaceTransitionBlockedByActiveOperationError,
)
from awf.db.session import make_session_factory
from awf.node.cleanup import (
    COMPOSE_DOWN_SUCCEEDED,
    WorkspaceCleanupResult,
    WorkspaceCleanupStepResult,
)
from awf.service import controls as controls_module
from awf.service.controls import (
    _OPERATION_ERROR_MESSAGE_MAX_LENGTH,
    ActiveWorkspaceDestroyError,
    IdempotencyConflictError,
    VersionConflictError,
    WorkspaceActiveOperationConflictError,
    WorkspaceControlService,
    WorkspaceNotFoundError,
    WorkspaceRebaseActiveConflictError,
    WorkspaceRebaseMissingCandidateError,
    WorkspaceRebaseMissingPrUrlError,
    WorkspaceRebaseStateError,
    WorkspaceRefreshStateError,
    WorkspaceRemonitorMissingPrUrlError,
    WorkspaceRemonitorStateError,
    WorkspaceStackStopError,
    WorkspaceValidateMissingPrUrlError,
    WorkspaceValidateStateError,
    _json_datetime,
    default_cleaner,
    stop_project_containers,
)
from awf.service.terminal_runtime import (
    TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX,
    TERMINAL_RUNTIME_RELEASE_SKIPPED_REASON_CODE,
    TerminalRuntimeReleaser,
    TerminalRuntimeReleaseResult,
    terminal_runtime_release_claim_active,
)
from tests.postgres import postgres_test_engine, postgres_test_session

CONTROL_ASYNC_TEST_TIMEOUT_SECONDS = 30.0


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


@dataclass
class RecordingStopper:
    calls: list[str | None] = field(default_factory=list)

    async def __call__(self, compose_project_name: str | None) -> None:
        self.calls.append(compose_project_name)


@dataclass
class FailingStopper:
    calls: list[str | None] = field(default_factory=list)

    async def __call__(self, compose_project_name: str | None) -> None:
        self.calls.append(compose_project_name)
        raise WorkspaceStackStopError(
            operation="stop",
            returncode=17,
            stdout="",
            stderr="compose stop denied",
        )


@dataclass
class CleanupCall:
    workspace_id: str
    repo_url: str
    compose_project_name: str | None
    compose_file_path: Path | None
    worktree_host_path: Path | None
    remove_volumes: bool
    remove_worktree: bool


@dataclass
class RecordingCleaner:
    failures: list[str] = field(default_factory=list)
    calls: list[CleanupCall] = field(default_factory=list)

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
    ) -> list[str]:
        self.calls.append(
            CleanupCall(
                workspace_id=workspace_id,
                repo_url=repo_url,
                compose_project_name=compose_project_name,
                compose_file_path=compose_file_path,
                worktree_host_path=worktree_host_path,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
            )
        )
        return list(self.failures)


@dataclass
class RaisingCleaner(RecordingCleaner):
    error_message: str = "cleanup exploded"

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
    ) -> list[str]:
        self.calls.append(
            CleanupCall(
                workspace_id=workspace_id,
                repo_url=repo_url,
                compose_project_name=compose_project_name,
                compose_file_path=compose_file_path,
                worktree_host_path=worktree_host_path,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
            )
        )
        raise RuntimeError(self.error_message)


@dataclass
class CancelledCleaner(RecordingCleaner):
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
    ) -> list[str]:
        self.calls.append(
            CleanupCall(
                workspace_id=workspace_id,
                repo_url=repo_url,
                compose_project_name=compose_project_name,
                compose_file_path=compose_file_path,
                worktree_host_path=worktree_host_path,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
            )
        )
        raise asyncio.CancelledError


@dataclass
class StaleCallbackCleaner(RecordingCleaner):
    session: AsyncSession | None = None
    final_status: WorkspaceStatus = WorkspaceStatus.cancelled

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
    ) -> list[str]:
        result = await super().cleanup(
            workspace_id=workspace_id,
            repo_url=repo_url,
            compose_project_name=compose_project_name,
            compose_file_path=compose_file_path,
            worktree_host_path=worktree_host_path,
            remove_volumes=remove_volumes,
            remove_worktree=remove_worktree,
        )
        assert self.session is not None
        await self.session.execute(
            sa_update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(
                status=self.final_status.value,
                failure_reason=(
                    "operator_failure" if self.final_status == WorkspaceStatus.failed else None
                ),
                failure_message=(
                    "operator moved workspace"
                    if self.final_status == WorkspaceStatus.failed
                    else None
                ),
            )
            .execution_options(synchronize_session=False)
        )
        await self.session.flush()
        return result


async def _assert_workspace_row_unlocked_nowait(
    session: AsyncSession,
    workspace_id: str,
) -> None:
    await session.execute(
        text("SELECT id FROM workspaces WHERE id = :workspace_id FOR UPDATE NOWAIT"),
        {"workspace_id": workspace_id},
    )


@dataclass
class LockObservingStopper:
    session_factory: async_sessionmaker[AsyncSession]
    workspace_id: str
    subphase: str
    calls: list[str | None] = field(default_factory=list)

    async def __call__(self, compose_project_name: str | None) -> None:
        self.calls.append(compose_project_name)
        async with self.session_factory() as observer:
            await _assert_workspace_row_unlocked_nowait(observer, self.workspace_id)
            await WorkspaceRepository(observer).update_activity(
                self.workspace_id,
                subphase=self.subphase,
            )
            await observer.commit()


class LockObservingCleaner(RecordingCleaner):
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        workspace_id: str,
        subphase: str,
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self._subphase = subphase

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
    ) -> list[str]:
        async with self._session_factory() as observer:
            await _assert_workspace_row_unlocked_nowait(observer, self._workspace_id)
            await WorkspaceRepository(observer).update_activity(
                self._workspace_id,
                subphase=self._subphase,
            )
            await observer.commit()
        return await super().cleanup(
            workspace_id=workspace_id,
            repo_url=repo_url,
            compose_project_name=compose_project_name,
            compose_file_path=compose_file_path,
            worktree_host_path=worktree_host_path,
            remove_volumes=remove_volumes,
            remove_worktree=remove_worktree,
        )


@dataclass
class ConcurrentTransitionStopper:
    session_factory: async_sessionmaker[AsyncSession]
    workspace_id: str
    to_status: WorkspaceStatus
    calls: list[str | None] = field(default_factory=list)
    blocked_operation_type: str | None = None

    async def __call__(self, compose_project_name: str | None) -> None:
        self.calls.append(compose_project_name)
        async with self.session_factory() as actor:
            await _assert_workspace_row_unlocked_nowait(actor, self.workspace_id)
            repo = WorkspaceRepository(actor)
            workspace = await asyncio.wait_for(
                repo.get_for_update(self.workspace_id),
                timeout=CONTROL_ASYNC_TEST_TIMEOUT_SECONDS,
            )
            assert workspace is not None
            try:
                await repo.transition(
                    workspace,
                    to=self.to_status,
                    reason_code="TEST_CONCURRENT_TRANSITION",
                    payload={"source": "unit_test"},
                )
            except WorkspaceTransitionBlockedByActiveOperationError as exc:
                self.blocked_operation_type = exc.operation.type
                await actor.rollback()
            else:
                await actor.commit()


@dataclass
class LeaseHeartbeatWaitingStopper:
    session_factory: async_sessionmaker[AsyncSession]
    workspace_id: str
    operation_type: OperationType
    calls: list[str | None] = field(default_factory=list)
    heartbeat_seen_at: datetime | None = None

    async def __call__(self, compose_project_name: str | None) -> None:
        self.calls.append(compose_project_name)
        deadline = asyncio.get_running_loop().time() + CONTROL_ASYNC_TEST_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            async with self.session_factory() as observer:
                operations = await OperationRepository(observer).list_for_workspace(
                    self.workspace_id,
                    status=OperationStatus.running,
                    operation_type=self.operation_type,
                    limit=1,
                )
                if operations and operations[0].lease_renewed_at is not None:
                    self.heartbeat_seen_at = operations[0].lease_renewed_at
                    return
            await asyncio.sleep(0.01)
        raise AssertionError("teardown operation lease heartbeat was not renewed")


@dataclass
class LeaseLossBlockingStopper:
    calls: list[str | None] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False

    async def __call__(self, compose_project_name: str | None) -> None:
        self.calls.append(compose_project_name)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@dataclass
class ConcurrentTerminalRuntimeReleaseClaimStopper:
    session_factory: async_sessionmaker[AsyncSession]
    workspace_id: str
    calls: list[str | None] = field(default_factory=list)

    async def __call__(self, compose_project_name: str | None) -> None:
        self.calls.append(compose_project_name)
        async with self.session_factory() as actor:
            await _assert_workspace_row_unlocked_nowait(actor, self.workspace_id)
            workspace = await asyncio.wait_for(
                WorkspaceRepository(actor).get_for_update(self.workspace_id),
                timeout=CONTROL_ASYNC_TEST_TIMEOUT_SECONDS,
            )
            assert workspace is not None
            workspace.execution_claimed_by = (
                f"{TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX}existing"
            )
            workspace.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            await actor.commit()


class ConcurrentTerminalRuntimeReleaserCleaner(RecordingCleaner):
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        workspace_id: str,
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self.release_cleaner = RecordingCleaner()
        self.claim_active_during_cleanup: bool | None = None
        self.release_result: TerminalRuntimeReleaseResult | None = None

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
    ) -> list[str]:
        async with self._session_factory() as observer:
            workspace = await WorkspaceRepository(observer).get(self._workspace_id)
            assert workspace is not None
            self.claim_active_during_cleanup = terminal_runtime_release_claim_active(workspace)

        releaser = TerminalRuntimeReleaser(
            session_factory=self._session_factory,
            cleaner_factory=lambda: self.release_cleaner,
        )
        self.release_result = await releaser.release(
            self._workspace_id,
            source="test.concurrent-control-cleanup",
            expected_status=WorkspaceStatus.cancelled,
        )
        return await super().cleanup(
            workspace_id=workspace_id,
            repo_url=repo_url,
            compose_project_name=compose_project_name,
            compose_file_path=compose_file_path,
            worktree_host_path=worktree_host_path,
            remove_volumes=remove_volumes,
            remove_worktree=remove_worktree,
        )


class TerminalRuntimeClaimHeartbeatWaitingCleaner(RecordingCleaner):
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        workspace_id: str,
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self.initial_claim_expires_at: datetime | None = None
        self.heartbeat_seen_at: datetime | None = None

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
    ) -> list[str]:
        self.calls.append(
            CleanupCall(
                workspace_id=workspace_id,
                repo_url=repo_url,
                compose_project_name=compose_project_name,
                compose_file_path=compose_file_path,
                worktree_host_path=worktree_host_path,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
            )
        )
        deadline = asyncio.get_running_loop().time() + CONTROL_ASYNC_TEST_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            async with self._session_factory() as observer:
                workspace = await WorkspaceRepository(observer).get(self._workspace_id)
                assert workspace is not None
                if self.initial_claim_expires_at is None:
                    self.initial_claim_expires_at = workspace.execution_claim_expires_at
                if (
                    self.initial_claim_expires_at is not None
                    and workspace.execution_claim_expires_at is not None
                    and workspace.execution_claim_expires_at > self.initial_claim_expires_at
                ):
                    self.heartbeat_seen_at = workspace.execution_claim_expires_at
                    return list(self.failures)
            await asyncio.sleep(0.01)
        raise AssertionError("terminal runtime release claim heartbeat was not renewed")


async def _workspace(
    session: AsyncSession,
    *,
    status: WorkspaceStatus,
    title: str = "control lifecycle",
) -> Workspace:
    workspace = await WorkspaceRepository(session).create(
        repo_url="git@github.com:example/control-lifecycle.git",
        branch_base="development",
        task_title=title,
        task_prompt="Exercise control lifecycle behavior.",
        agent=AgentRuntime.codex.value,
        test_commands=["pytest -q"],
    )
    workspace.status = status.value
    workspace.compose_project_name = f"awf_{workspace.id}"
    workspace.compose_file_path = f"/tmp/{workspace.id}/compose.yml"
    await session.flush()
    return workspace


async def _workspace_with_candidate(
    session: AsyncSession,
    *,
    status: WorkspaceStatus = WorkspaceStatus.monitoring_pr,
    title: str = "rebase lifecycle",
) -> tuple[Workspace, MergeCandidate]:
    workspace = await _workspace(session, status=status, title=title)
    workspace.branch_name = f"awf/{workspace.id}"
    workspace.remote_push_branch = workspace.branch_name
    workspace.base_commit = "a" * 40
    workspace.monitor_last_commit_sha = "h" * 40
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/50"
    workspace.pr_number = 50
    task = await TaskRepository(session).create_or_get(
        repo_url=workspace.repo_url,
        base_branch=workspace.branch_base,
        title=workspace.task_title,
        prompt=workspace.task_prompt,
        external_id=workspace.task_external_id,
        idempotency_key=None,
        task_class=workspace.task_class,
        owned_paths=list(workspace.owned_paths),
    )
    attempt = await TaskAttemptRepository(session).create_for_workspace(
        task=task,
        workspace=workspace,
    )
    candidate = await MergeCandidateRepository(session).create_or_update_open_for_attempt(
        task=task,
        attempt=attempt,
        workspace=workspace,
        head_sha=workspace.monitor_last_commit_sha,
        base_sha=workspace.base_commit,
    )
    await session.flush()
    return workspace, candidate


def _service(
    session: AsyncSession,
    *,
    stopper: RecordingStopper | None = None,
    cleaner: RecordingCleaner | None = None,
    worktrees_root: Path | None = None,
) -> tuple[WorkspaceControlService, RecordingStopper, RecordingCleaner]:
    stopper = stopper or RecordingStopper()
    cleaner = cleaner or RecordingCleaner()
    return (
        WorkspaceControlService(
            session,
            project_stopper=stopper,
            cleaner_factory=lambda: cleaner,
            worktrees_root=worktrees_root,
        ),
        stopper,
        cleaner,
    )


async def _issue_control_secret_lease(
    session: AsyncSession,
    workspace: Workspace,
    *,
    now: datetime | None = None,
) -> None:
    issued_at = now or datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    await SecretLeaseRepository(session).issue_declared_leases(
        workspace,
        leases=[
            SecretLeaseIssue(
                secret_name="api-token",
                kind="env",
                target="API_TOKEN",
                mode="ro",
                required=True,
                provider="env",
                ref_digest="sha256:" + "d" * 64,
                expires_at=issued_at + timedelta(hours=1),
                issue_metadata={"profile": "control-lifecycle", "declaration_index": 0},
            )
        ],
        now=issued_at,
    )


async def _operations(session: AsyncSession, workspace_id: str) -> list[Operation]:
    return await OperationRepository(session).list_for_workspace(workspace_id, limit=20)


async def _events(session: AsyncSession, workspace_id: str) -> list[WorkspaceEvent]:
    return await WorkspaceEventRepository(session).list(workspace_id=workspace_id, limit=20)


@pytest.mark.unit
async def test_cancel_active_workspace_stops_stack_transitions_and_replays(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    service, stopper, _cleaner = _service(session)
    expected_version = workspace.version

    response = await service.cancel_workspace(
        workspace.id,
        reason="operator requested",
        stop_stack=True,
        idempotency_key="cancel-same-key",
        expected_version=expected_version,
    )
    replay = await service.cancel_workspace(
        workspace.id,
        reason="operator requested",
        stop_stack=True,
        idempotency_key="cancel-same-key",
        expected_version=expected_version,
    )
    operations = await _operations(session, workspace.id)
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert response.operation_id == replay.operation_id
    assert response.message == "workspace cancellation requested"
    assert response.status == WorkspaceStatus.cancelled
    assert workspace.status == WorkspaceStatus.cancelled.value
    assert stopper.calls == [workspace.compose_project_name]
    assert [operation.type for operation in operations] == [OperationType.cancel.value]
    assert operations[0].status == "succeeded"
    assert operations[0].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "operator requested",
        "reason_code": "OPERATOR_CANCEL",
        "requested_action": "cancel",
        "stop_stack": True,
        "expected_version": 1,
    }
    assert operations[0].result == {"status": WorkspaceStatus.cancelled.value}
    assert len(audit_events) == 1
    assert audit_events[0].payload == {
        "schema": "control_audit.v1",
        "actor": "operator_api",
        "source": "operator_api",
        "action": "cancel",
        "outcome": "succeeded",
        "reason_code": "OPERATOR_CANCEL",
        "operation_id": operations[0].id,
        "operation_type": "cancel",
        "stop_stack": True,
        "expected_version": 1,
    }


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_release_workspace_lock_before_stack_stop(
    action: str,
) -> None:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            workspace = await _workspace(session, status=WorkspaceStatus.ready)
            workspace_id = workspace.id
            compose_project_name = workspace.compose_project_name
            await session.commit()

        stopper = LockObservingStopper(
            session_factory=session_factory,
            workspace_id=workspace_id,
            subphase=f"{action}-stack-stop-observed-unlocked-row",
        )
        async with session_factory() as session:
            service, _stopper, _cleaner = _service(session, stopper=stopper)
            if action == "cancel":
                response = await service.cancel_workspace(
                    workspace_id,
                    reason="operator requested",
                    stop_stack=True,
                )
            else:
                response = await service.stop_workspace(
                    workspace_id,
                    reason="operator requested",
                )
            await session.commit()

        async with session_factory() as session:
            persisted = await WorkspaceRepository(session).get(workspace_id)

    assert response.status == WorkspaceStatus.cancelled
    assert stopper.calls == [compose_project_name]
    assert persisted is not None
    assert persisted.subphase == f"{action}-stack-stop-observed-unlocked-row"


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_release_workspace_lock_before_terminal_runtime_cleanup(
    action: str,
) -> None:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            workspace = await _workspace(session, status=WorkspaceStatus.ready)
            workspace_id = workspace.id
            compose_project_name = workspace.compose_project_name
            await session.commit()

        cleaner = LockObservingCleaner(
            session_factory=session_factory,
            workspace_id=workspace_id,
            subphase=f"{action}-terminal-cleanup-observed-unlocked-row",
        )
        async with session_factory() as session:
            service, stopper, _cleaner = _service(session, cleaner=cleaner)
            if action == "cancel":
                response = await service.cancel_workspace(
                    workspace_id,
                    reason="operator requested",
                    stop_stack=True,
                )
            else:
                response = await service.stop_workspace(
                    workspace_id,
                    reason="operator requested",
                )
            await session.commit()

        async with session_factory() as session:
            persisted = await WorkspaceRepository(session).get(workspace_id)
            events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)

    release_events = [
        event for event in events if event.event_type == "workspace.terminal_runtime_released"
    ]
    assert response.status == WorkspaceStatus.cancelled
    assert stopper.calls == [compose_project_name]
    assert len(cleaner.calls) == 1
    assert persisted is not None
    assert persisted.subphase == f"{action}-terminal-cleanup-observed-unlocked-row"
    assert len(release_events) == 1


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_skip_cleanup_when_terminal_release_claim_is_active(
    action: str,
) -> None:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            workspace = await _workspace(session, status=WorkspaceStatus.ready)
            workspace_id = workspace.id
            compose_project_name = workspace.compose_project_name
            await session.commit()

        stopper = ConcurrentTerminalRuntimeReleaseClaimStopper(
            session_factory=session_factory,
            workspace_id=workspace_id,
        )
        async with session_factory() as session:
            service, _stopper, cleaner = _service(session, stopper=stopper)
            if action == "cancel":
                response = await service.cancel_workspace(
                    workspace_id,
                    reason="operator requested",
                    stop_stack=True,
                )
            else:
                response = await service.stop_workspace(
                    workspace_id,
                    reason="operator requested",
                )
            await session.commit()

        async with session_factory() as session:
            persisted = await WorkspaceRepository(session).get(workspace_id)
            events = await _events(session, workspace_id)

    release_events = [
        event
        for event in events
        if event.event_type.startswith("workspace.terminal_runtime_release")
    ]
    assert response.status == WorkspaceStatus.cancelled
    assert stopper.calls == [compose_project_name]
    assert cleaner.calls == []
    assert persisted is not None
    assert persisted.status == WorkspaceStatus.cancelled.value
    assert persisted.execution_claimed_by == (
        f"{TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX}existing"
    )
    assert release_events == []


@pytest.mark.unit
async def test_cancel_terminal_workspace_claims_release_before_cleanup() -> None:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            workspace = await _workspace(session, status=WorkspaceStatus.cancelled)
            workspace_id = workspace.id
            await session.commit()

        cleaner = ConcurrentTerminalRuntimeReleaserCleaner(
            session_factory=session_factory,
            workspace_id=workspace_id,
        )
        async with session_factory() as session:
            service, stopper, _cleaner = _service(session, cleaner=cleaner)
            response = await service.cancel_workspace(
                workspace_id,
                reason="repeat cancel",
                stop_stack=True,
            )
            await session.commit()

        async with session_factory() as session:
            persisted = await WorkspaceRepository(session).get(workspace_id)
            events = await _events(session, workspace_id)

    release_events = [
        event
        for event in events
        if event.event_type.startswith("workspace.terminal_runtime_release")
    ]
    assert response.status == WorkspaceStatus.cancelled
    assert stopper.calls == [f"awf_{workspace_id}"]
    assert cleaner.claim_active_during_cleanup is True
    assert cleaner.release_result is not None
    assert cleaner.release_result.reason_code == TERMINAL_RUNTIME_RELEASE_SKIPPED_REASON_CODE
    assert cleaner.release_cleaner.calls == []
    assert persisted is not None
    assert persisted.execution_claimed_by is None
    assert persisted.execution_claim_expires_at is None
    assert [event.event_type for event in release_events] == ["workspace.terminal_runtime_released"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("action", "status"),
    [
        ("cancel", WorkspaceStatus.cancelled),
        ("stop", WorkspaceStatus.completed),
    ],
)
async def test_cancel_and_stop_refresh_terminal_release_claim_during_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    status: WorkspaceStatus,
) -> None:
    monkeypatch.setattr(
        controls_module,
        "_terminal_runtime_release_claim_heartbeat_interval_seconds",
        lambda: 0.001,
    )
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            workspace = await _workspace(session, status=status)
            workspace_id = workspace.id
            compose_project_name = workspace.compose_project_name
            await session.commit()

        cleaner = TerminalRuntimeClaimHeartbeatWaitingCleaner(
            session_factory=session_factory,
            workspace_id=workspace_id,
        )
        async with session_factory() as session:
            service, stopper, _cleaner = _service(session, cleaner=cleaner)
            if action == "cancel":
                response = await service.cancel_workspace(
                    workspace_id,
                    reason="repeat cancel",
                    stop_stack=True,
                )
            else:
                response = await service.stop_workspace(
                    workspace_id,
                    reason="repeat stop",
                )
            await session.commit()

    assert response.status == status
    assert stopper.calls == [compose_project_name]
    assert len(cleaner.calls) == 1
    assert cleaner.initial_claim_expires_at is not None
    assert cleaner.heartbeat_seen_at is not None
    assert cleaner.heartbeat_seen_at > cleaner.initial_claim_expires_at


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_fence_external_stack_io_from_concurrent_version_bump(
    action: str,
) -> None:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            workspace = await _workspace(session, status=WorkspaceStatus.ready)
            workspace_id = workspace.id
            compose_project_name = workspace.compose_project_name
            expected_version = workspace.version
            await session.commit()

        stopper = ConcurrentTransitionStopper(
            session_factory=session_factory,
            workspace_id=workspace_id,
            to_status=WorkspaceStatus.running,
        )
        async with session_factory() as session:
            service, _stopper, cleaner = _service(session, stopper=stopper)
            if action == "cancel":
                response = await service.cancel_workspace(
                    workspace_id,
                    reason="operator requested",
                    stop_stack=True,
                    expected_version=expected_version,
                )
            else:
                response = await service.stop_workspace(
                    workspace_id,
                    reason="operator requested",
                    expected_version=expected_version,
                )

        async with session_factory() as session:
            persisted = await WorkspaceRepository(session).get(workspace_id)
            operations = await _operations(session, workspace_id)
            events = await _events(session, workspace_id)

    release_events = [
        event for event in events if event.event_type == "workspace.terminal_runtime_released"
    ]
    assert response.status == WorkspaceStatus.cancelled
    assert stopper.calls == [compose_project_name]
    assert stopper.blocked_operation_type == action
    assert len(cleaner.calls) == 1
    assert len(release_events) == 1
    assert release_events[0].payload is not None
    assert release_events[0].payload["source"] == f"service.controls.{action}"
    assert release_events[0].payload["workspace_status"] == WorkspaceStatus.cancelled.value
    assert persisted is not None
    assert persisted.status == WorkspaceStatus.cancelled.value
    assert persisted.version == expected_version + 1
    assert len(operations) == 1
    assert operations[0].status == OperationStatus.succeeded.value
    assert operations[0].result == {"status": WorkspaceStatus.cancelled.value}


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_renew_teardown_operation_lease_during_external_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    monkeypatch.setattr(
        controls_module,
        "_RUNTIME_TEARDOWN_OPERATION_HEARTBEAT_INTERVAL_SECONDS",
        0.01,
    )
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            workspace = await _workspace(session, status=WorkspaceStatus.ready)
            workspace_id = workspace.id
            compose_project_name = workspace.compose_project_name
            await session.commit()

        operation_type = OperationType.cancel if action == "cancel" else OperationType.stop
        stopper = LeaseHeartbeatWaitingStopper(
            session_factory=session_factory,
            workspace_id=workspace_id,
            operation_type=operation_type,
        )
        async with session_factory() as session:
            service, _stopper, cleaner = _service(session, stopper=stopper)
            if action == "cancel":
                response = await service.cancel_workspace(
                    workspace_id,
                    reason="operator requested",
                    stop_stack=True,
                )
            else:
                response = await service.stop_workspace(
                    workspace_id,
                    reason="operator requested",
                )

        async with session_factory() as session:
            operations = await _operations(session, workspace_id)

    assert response.status == WorkspaceStatus.cancelled
    assert stopper.calls == [compose_project_name]
    assert stopper.heartbeat_seen_at is not None
    assert len(cleaner.calls) == 1
    assert [operation.type for operation in operations] == [operation_type.value]
    assert operations[0].lease_renewed_at is not None
    assert operations[0].lease_renewed_at >= stopper.heartbeat_seen_at
    assert operations[0].started_at is not None
    assert operations[0].lease_renewed_at >= operations[0].started_at


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_abort_external_cleanup_when_teardown_lease_is_lost(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    monkeypatch.setattr(
        controls_module,
        "_RUNTIME_TEARDOWN_OPERATION_HEARTBEAT_INTERVAL_SECONDS",
        0.01,
    )
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    workspace_id = workspace.id
    compose_project_name = workspace.compose_project_name
    lease_renew_attempts = 0

    async def _lose_teardown_lease(
        repo: OperationRepository,
        operation_id: str,
        *,
        now: datetime | None = None,
    ) -> Operation | None:
        nonlocal lease_renew_attempts
        del repo, operation_id, now
        lease_renew_attempts += 1
        return None

    monkeypatch.setattr(
        OperationRepository,
        "renew_teardown_lease",
        _lose_teardown_lease,
    )
    stopper = LeaseLossBlockingStopper()
    service, _stopper, cleaner = _service(session, stopper=stopper)  # type: ignore[arg-type]
    expected_runtime_message = (
        "teardown operation lease heartbeat stopped before external runtime work "
        "completed: operation lease is no longer active"
    )

    with pytest.raises(
        RuntimeError,
        match="operation lease is no longer active",
    ):
        if action == "cancel":
            await asyncio.wait_for(
                service.cancel_workspace(
                    workspace_id,
                    reason="operator requested",
                    stop_stack=True,
                ),
                timeout=CONTROL_ASYNC_TEST_TIMEOUT_SECONDS,
            )
        else:
            await asyncio.wait_for(
                service.stop_workspace(
                    workspace_id,
                    reason="operator requested",
                ),
                timeout=CONTROL_ASYNC_TEST_TIMEOUT_SECONDS,
            )

    await session.rollback()
    operations = await _operations(session, workspace_id)
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace_id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )
    release_events = [
        event
        for event in await _events(session, workspace_id)
        if event.event_type.startswith("workspace.terminal_runtime_release")
    ]

    assert lease_renew_attempts == 1
    assert stopper.started.is_set()
    assert stopper.cancelled is True
    assert stopper.calls == [compose_project_name]
    assert cleaner.calls == []
    assert len(operations) == 1
    assert operations[0].type == action
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "CONTROL_OPERATION_FAILED"
    assert operations[0].error_message == f"RuntimeError: {expected_runtime_message}"
    assert operations[0].result == {"status": WorkspaceStatus.ready.value}
    assert operations[0].finished_at is not None
    assert len(audit_events) == 1
    assert audit_events[0].reason_code == "CONTROL_OPERATION_FAILED"
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["action"] == action
    assert audit_events[0].payload["outcome"] == "failed"
    assert audit_events[0].payload["evidence"] == {
        "error_type": "RuntimeError",
        "error_message": f"RuntimeError: {expected_runtime_message}",
    }
    assert release_events == []


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_abort_external_cleanup_when_teardown_lease_renew_fails(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    monkeypatch.setattr(
        controls_module,
        "_RUNTIME_TEARDOWN_OPERATION_HEARTBEAT_INTERVAL_SECONDS",
        0.01,
    )
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    workspace_id = workspace.id
    compose_project_name = workspace.compose_project_name
    lease_renew_attempts = 0

    async def _fail_teardown_lease_renew(
        repo: OperationRepository,
        operation_id: str,
        *,
        now: datetime | None = None,
    ) -> Operation | None:
        nonlocal lease_renew_attempts
        del repo, operation_id, now
        lease_renew_attempts += 1
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(
        OperationRepository,
        "renew_teardown_lease",
        _fail_teardown_lease_renew,
    )
    stopper = LeaseLossBlockingStopper()
    service, _stopper, cleaner = _service(session, stopper=stopper)  # type: ignore[arg-type]
    expected_runtime_message = (
        "teardown operation lease heartbeat stopped before external runtime work "
        "completed: lease renewal failed: SQLAlchemyError: database unavailable"
    )

    with pytest.raises(
        RuntimeError,
        match="lease renewal failed: SQLAlchemyError: database unavailable",
    ):
        if action == "cancel":
            await asyncio.wait_for(
                service.cancel_workspace(
                    workspace_id,
                    reason="operator requested",
                    stop_stack=True,
                ),
                timeout=CONTROL_ASYNC_TEST_TIMEOUT_SECONDS,
            )
        else:
            await asyncio.wait_for(
                service.stop_workspace(
                    workspace_id,
                    reason="operator requested",
                ),
                timeout=CONTROL_ASYNC_TEST_TIMEOUT_SECONDS,
            )

    await session.rollback()
    operations = await _operations(session, workspace_id)
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace_id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert lease_renew_attempts == 1
    assert stopper.started.is_set()
    assert stopper.cancelled is True
    assert stopper.calls == [compose_project_name]
    assert cleaner.calls == []
    assert len(operations) == 1
    assert operations[0].type == action
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "CONTROL_OPERATION_FAILED"
    assert operations[0].error_message == f"RuntimeError: {expected_runtime_message}"
    assert operations[0].result == {"status": WorkspaceStatus.ready.value}
    assert len(audit_events) == 1
    assert audit_events[0].reason_code == "CONTROL_OPERATION_FAILED"
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["evidence"]["error_message"] == (
        f"RuntimeError: {expected_runtime_message}"
    )


@pytest.mark.unit
async def test_active_runtime_teardown_blocks_control_requests_and_atomic_transitions(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/77"
    active_stop = await OperationRepository(session).create(
        workspace_id=workspace.id,
        operation_type=OperationType.stop,
        status=OperationStatus.running,
        payload={"source": "operator_api"},
    )
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceActiveOperationConflictError) as conflict:
        await service.request_validate_workspace(workspace.id, reason="rerun validation")

    with pytest.raises(WorkspaceTransitionBlockedByActiveOperationError) as blocked:
        await WorkspaceRepository(session).transition_if_current(
            workspace.id,
            from_status=WorkspaceStatus.monitoring_pr,
            to=WorkspaceStatus.completed,
            reason_code="TEST_CONCURRENT_TRANSITION",
        )

    assert conflict.value.detail == {
        "operation_id": active_stop.id,
        "operation_type": OperationType.stop.value,
        "operation_status": OperationStatus.running.value,
    }
    assert blocked.value.operation.id == active_stop.id
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.version == 1


@pytest.mark.unit
async def test_cancel_releases_terminal_runtime_with_preserved_worktree_path(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    worktrees_root = tmp_path / "git" / "worktrees"
    worktree_path = worktrees_root / workspace.id
    worktree_path.mkdir(parents=True)
    service, _stopper, cleaner = _service(session, worktrees_root=worktrees_root)

    await service.cancel_workspace(
        workspace.id,
        reason="operator requested",
        stop_stack=True,
    )

    events = await _events(session, workspace.id)
    release_event = next(
        event for event in events if event.event_type == "workspace.terminal_runtime_released"
    )
    assert cleaner.calls[0].worktree_host_path == worktree_path
    assert cleaner.calls[0].remove_volumes is False
    assert cleaner.calls[0].remove_worktree is False
    assert release_event.payload is not None
    assert release_event.payload["preserved"]["worktree_path"] == str(worktree_path)


@pytest.mark.unit
async def test_cancel_omits_missing_terminal_runtime_worktree_path(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    worktrees_root = tmp_path / "git" / "worktrees"
    worktrees_root.mkdir(parents=True)
    service, _stopper, cleaner = _service(session, worktrees_root=worktrees_root)

    await service.cancel_workspace(
        workspace.id,
        reason="operator requested",
        stop_stack=True,
    )

    events = await _events(session, workspace.id)
    release_event = next(
        event for event in events if event.event_type == "workspace.terminal_runtime_released"
    )
    assert cleaner.calls[0].worktree_host_path is None
    assert release_event.payload is not None
    assert "worktree_path" not in release_event.payload["preserved"]


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_omit_terminal_runtime_worktree_path_when_root_absent(
    session: AsyncSession,
    tmp_path: Path,
    action: str,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    workspace.compose_file_path = None
    worktrees_root = tmp_path / "git" / "worktrees"
    service, _stopper, cleaner = _service(session, worktrees_root=worktrees_root)

    if action == "cancel":
        await service.cancel_workspace(
            workspace.id,
            reason="operator requested",
            stop_stack=True,
        )
    else:
        await service.stop_workspace(
            workspace.id,
            reason="operator requested",
        )

    events = await _events(session, workspace.id)
    release_event = next(
        event for event in events if event.event_type == "workspace.terminal_runtime_released"
    )
    assert not worktrees_root.exists()
    assert cleaner.calls[0].compose_file_path is None
    assert cleaner.calls[0].worktree_host_path is None
    assert release_event.payload is not None
    assert "compose_file_path" not in release_event.payload["runtime"]
    assert "worktree_path" not in release_event.payload["preserved"]


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_record_terminal_release_failure_when_cleanup_raises(
    session: AsyncSession,
    action: str,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    cleaner = RaisingCleaner(error_message=f"{action} cleanup exploded")
    service, stopper, _cleaner = _service(session, cleaner=cleaner)

    if action == "cancel":
        response = await service.cancel_workspace(
            workspace.id,
            reason="operator requested",
            stop_stack=True,
        )
    else:
        response = await service.stop_workspace(
            workspace.id,
            reason="operator requested",
        )
    operations = await _operations(session, workspace.id)
    release_event = next(
        event
        for event in await _events(session, workspace.id)
        if event.event_type == "workspace.terminal_runtime_release_failed"
    )

    assert response.status == WorkspaceStatus.cancelled
    assert workspace.status == WorkspaceStatus.cancelled.value
    assert stopper.calls == [workspace.compose_project_name]
    assert len(cleaner.calls) == 1
    assert operations[0].status == OperationStatus.succeeded.value
    assert operations[0].result == {"status": WorkspaceStatus.cancelled.value}
    assert release_event.reason_code == "TERMINAL_RUNTIME_RELEASE_FAILED"
    assert release_event.payload is not None
    cleanup = release_event.payload["cleanup"]
    assert cleanup["status"] == "partial"
    assert cleanup["reason_code"] == "TERMINAL_RUNTIME_RELEASE_EXCEPTION"
    assert cleanup["failed_steps"][0]["name"] == "terminal_runtime_release"
    assert cleanup["failed_steps"][0]["reason_code"] == "TERMINAL_RUNTIME_RELEASE_EXCEPTION"
    assert cleanup["failed_steps"][0]["error"] == (f"RuntimeError: {action} cleanup exploded")


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_redact_terminal_release_cleanup_exception_evidence(
    session: AsyncSession,
    action: str,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    credentialed_url = "https://svc-user:super-secret-token@github.com/example/private.git"
    api_token = "ghp_1234567890abcdef"
    cleaner = RaisingCleaner(
        error_message=(
            f"{action} cleanup failed for {credentialed_url} with GITHUB_TOKEN={api_token}"
        )
    )
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    if action == "cancel":
        await service.cancel_workspace(
            workspace.id,
            reason="operator requested",
            stop_stack=True,
        )
    else:
        await service.stop_workspace(
            workspace.id,
            reason="operator requested",
        )
    release_event = next(
        event
        for event in await _events(session, workspace.id)
        if event.event_type == "workspace.terminal_runtime_release_failed"
    )

    assert release_event.payload is not None
    failed_step = release_event.payload["cleanup"]["failed_steps"][0]
    assert "super-secret-token" not in failed_step["error"]
    assert api_token not in failed_step["error"]
    assert "https://[redacted]@github.com/example/private.git" in failed_step["error"]
    assert "GITHUB_TOKEN=[redacted]" in failed_step["error"]


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_propagate_terminal_release_cancellation_without_success(
    session: AsyncSession,
    action: str,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    workspace_id = workspace.id
    compose_project_name = workspace.compose_project_name
    await session.commit()
    cleaner = CancelledCleaner()
    service, stopper, _cleaner = _service(session, cleaner=cleaner)

    with pytest.raises(asyncio.CancelledError):
        if action == "cancel":
            await service.cancel_workspace(
                workspace_id,
                reason="operator requested",
                stop_stack=True,
            )
        else:
            await service.stop_workspace(
                workspace_id,
                reason="operator requested",
            )

    await session.rollback()
    persisted = await WorkspaceRepository(session).get(workspace_id)
    operations = await _operations(session, workspace_id)
    release_events = [
        event
        for event in await _events(session, workspace_id)
        if event.event_type.startswith("workspace.terminal_runtime_release")
    ]

    assert persisted is not None
    assert persisted.status == WorkspaceStatus.ready.value
    assert stopper.calls == [compose_project_name]
    assert len(cleaner.calls) == 1
    assert len(operations) == 1
    assert operations[0].type == action
    assert operations[0].status == OperationStatus.running.value
    assert operations[0].finished_at is None
    assert operations[0].error_code is None
    assert operations[0].error_message is None
    assert operations[0].result is None
    assert release_events == []

    with pytest.raises(WorkspaceTransitionBlockedByActiveOperationError) as blocked:
        await WorkspaceRepository(session).transition(
            persisted,
            to=WorkspaceStatus.running,
            reason_code="TEST_AFTER_CANCELLED_TEARDOWN",
        )
    assert blocked.value.operation.id == operations[0].id


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_retry_expired_idempotent_teardown_operation(
    session: AsyncSession,
    action: str,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    workspace_id = workspace.id
    compose_project_name = workspace.compose_project_name
    reason = "operator requested"
    idempotency_key = f"{action}-expired-teardown"
    operation_type = OperationType.cancel if action == "cancel" else OperationType.stop
    payload = {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": reason,
        "reason_code": "OPERATOR_CANCEL" if action == "cancel" else "OPERATOR_STOP",
        "requested_action": action,
    }
    if action == "cancel":
        payload["stop_stack"] = True
    operation = await OperationRepository(session).create(
        workspace_id=workspace_id,
        operation_type=operation_type,
        status=OperationStatus.running,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    stale_started_at = datetime.now(UTC) - timedelta(days=30)
    operation.started_at = stale_started_at
    await session.commit()
    service, stopper, cleaner = _service(session)

    if action == "cancel":
        response = await service.cancel_workspace(
            workspace_id,
            reason=reason,
            stop_stack=True,
            idempotency_key=idempotency_key,
        )
    else:
        response = await service.stop_workspace(
            workspace_id,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    operations = await _operations(session, workspace_id)

    assert response.operation_id == operation.id
    assert response.operation_status == OperationStatus.succeeded.value
    assert response.status == WorkspaceStatus.cancelled
    assert stopper.calls == [compose_project_name]
    assert len(cleaner.calls) == 1
    assert [row.id for row in operations] == [operation.id]
    assert operations[0].status == OperationStatus.succeeded.value
    assert operations[0].started_at == stale_started_at


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_preserve_precommitted_operation_when_cancelled_again(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    workspace_id = workspace.id
    cleaner = CancelledCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)
    original_rollback = session.rollback
    cancel_task: asyncio.Task[None] | None = None
    rollback_calls = 0

    async def _rollback_with_second_cancel() -> None:
        nonlocal rollback_calls
        rollback_calls += 1
        if rollback_calls == 1:
            assert cancel_task is not None
            cancel_task.cancel()
            await asyncio.sleep(0)
        await original_rollback()

    monkeypatch.setattr(session, "rollback", _rollback_with_second_cancel)

    async def _cancel_or_stop() -> None:
        if action == "cancel":
            await service.cancel_workspace(
                workspace_id,
                reason="operator requested",
                stop_stack=True,
                idempotency_key="cancel-double-cancel",
            )
        else:
            await service.stop_workspace(
                workspace_id,
                reason="operator requested",
                idempotency_key="stop-double-cancel",
            )

    cancel_task = asyncio.create_task(_cancel_or_stop())
    with pytest.raises(asyncio.CancelledError):
        await cancel_task

    await session.rollback()
    operations = await _operations(session, workspace_id)

    assert cleaner.calls
    assert len(operations) == 1
    assert operations[0].type == action
    assert operations[0].status == OperationStatus.running.value
    assert operations[0].finished_at is None


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_fail_precommitted_operation_when_claim_refresh_errors(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    workspace_id = workspace.id
    compose_project_name = workspace.compose_project_name
    claim_check_failures = 0
    original_get_for_update = WorkspaceRepository.get_for_update

    class ClaimCheckFailingStopper(RecordingStopper):
        async def __call__(self, compose_project_name: str | None) -> None:
            await super().__call__(compose_project_name)

            async def _raise_claim_check_failure(
                repo: WorkspaceRepository,
                requested_workspace_id: str,
            ) -> Workspace | None:
                nonlocal claim_check_failures
                if claim_check_failures == 0 and requested_workspace_id == workspace_id:
                    claim_check_failures += 1
                    raise SQLAlchemyError("terminal claim refresh failed")
                return await original_get_for_update(repo, requested_workspace_id)

            monkeypatch.setattr(
                WorkspaceRepository,
                "get_for_update",
                _raise_claim_check_failure,
            )

    service, stopper, cleaner = _service(session, stopper=ClaimCheckFailingStopper())

    with pytest.raises(SQLAlchemyError, match="terminal claim refresh failed"):
        if action == "cancel":
            await service.cancel_workspace(
                workspace_id,
                reason="operator requested",
                stop_stack=True,
            )
        else:
            await service.stop_workspace(
                workspace_id,
                reason="operator requested",
            )

    await session.rollback()
    operations = await _operations(session, workspace_id)
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace_id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )
    release_events = [
        event
        for event in await _events(session, workspace_id)
        if event.event_type.startswith("workspace.terminal_runtime_release")
    ]

    assert claim_check_failures == 1
    assert stopper.calls == [compose_project_name]
    assert cleaner.calls == []
    assert len(operations) == 1
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "CONTROL_OPERATION_FAILED"
    assert operations[0].error_message == "SQLAlchemyError: terminal claim refresh failed"
    assert operations[0].result == {"status": WorkspaceStatus.ready.value}
    assert operations[0].finished_at is not None
    assert len(audit_events) == 1
    assert audit_events[0].reason_code == "CONTROL_OPERATION_FAILED"
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["action"] == action
    assert audit_events[0].payload["outcome"] == "failed"
    assert audit_events[0].payload["evidence"] == {
        "error_type": "SQLAlchemyError",
        "error_message": "SQLAlchemyError: terminal claim refresh failed",
    }
    assert release_events == []


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_fail_precommitted_operation_when_post_teardown_db_errors(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    workspace_id = workspace.id
    compose_project_name = workspace.compose_project_name
    service, stopper, cleaner = _service(session)
    transition_calls = 0

    async def _raise_transition_failure(
        self: WorkspaceRepository,
        workspace: Workspace,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal transition_calls
        transition_calls += 1
        raise SQLAlchemyError("post teardown transition failed")

    monkeypatch.setattr(WorkspaceRepository, "transition", _raise_transition_failure)

    with pytest.raises(SQLAlchemyError, match="post teardown transition failed"):
        if action == "cancel":
            await service.cancel_workspace(
                workspace_id,
                reason="operator requested",
                stop_stack=True,
            )
        else:
            await service.stop_workspace(
                workspace_id,
                reason="operator requested",
            )

    await session.rollback()
    operations = await _operations(session, workspace_id)
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace_id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert transition_calls == 1
    assert stopper.calls == [compose_project_name]
    assert len(cleaner.calls) == 1
    assert len(operations) == 1
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "CONTROL_OPERATION_FAILED"
    assert operations[0].error_message == "SQLAlchemyError: post teardown transition failed"
    terminal_runtime_release = {
        "cleanup": {
            "status": "succeeded",
            "reason_code": "CLEANUP_SUCCEEDED",
            "steps": [],
            "failed_steps": [],
            "completed_steps": [],
        },
        "preserved": {},
    }
    assert operations[0].result == {
        "status": WorkspaceStatus.ready.value,
        "terminal_runtime_release": terminal_runtime_release,
    }
    assert operations[0].finished_at is not None
    assert len(audit_events) == 1
    assert audit_events[0].reason_code == "CONTROL_OPERATION_FAILED"
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["action"] == action
    assert audit_events[0].payload["outcome"] == "failed"
    assert audit_events[0].payload["terminal_runtime_release"] == {
        "cleanup_status": "succeeded",
        "cleanup_reason_code": "CLEANUP_SUCCEEDED",
    }
    assert audit_events[0].payload["evidence"] == {
        "error_type": "SQLAlchemyError",
        "error_message": "SQLAlchemyError: post teardown transition failed",
        "terminal_runtime_release": terminal_runtime_release,
    }


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_fail_precommitted_operation_when_cancelled_after_teardown(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    workspace_id = workspace.id
    compose_project_name = workspace.compose_project_name
    service, stopper, cleaner = _service(session)
    transition_calls = 0

    async def _cancel_during_transition(
        self: WorkspaceRepository,
        workspace: Workspace,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal transition_calls
        del self, workspace, args, kwargs
        transition_calls += 1
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)

    monkeypatch.setattr(WorkspaceRepository, "transition", _cancel_during_transition)

    with pytest.raises(asyncio.CancelledError):
        if action == "cancel":
            await service.cancel_workspace(
                workspace_id,
                reason="operator requested",
                stop_stack=True,
            )
        else:
            await service.stop_workspace(
                workspace_id,
                reason="operator requested",
            )

    await session.rollback()
    persisted = await WorkspaceRepository(session).get(workspace_id)
    operations = await _operations(session, workspace_id)
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace_id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert persisted is not None
    assert persisted.status == WorkspaceStatus.ready.value
    assert transition_calls == 1
    assert stopper.calls == [compose_project_name]
    assert len(cleaner.calls) == 1
    assert len(operations) == 1
    assert operations[0].type == action
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "CONTROL_OPERATION_FAILED"
    assert operations[0].error_message == "CancelledError: operation was cancelled"
    terminal_runtime_release = {
        "cleanup": {
            "status": "succeeded",
            "reason_code": "CLEANUP_SUCCEEDED",
            "steps": [],
            "failed_steps": [],
            "completed_steps": [],
        },
        "preserved": {},
    }
    assert operations[0].result == {
        "status": WorkspaceStatus.ready.value,
        "terminal_runtime_release": terminal_runtime_release,
    }
    assert operations[0].finished_at is not None
    assert len(audit_events) == 1
    assert audit_events[0].reason_code == "CONTROL_OPERATION_FAILED"
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["action"] == action
    assert audit_events[0].payload["outcome"] == "failed"
    assert audit_events[0].payload["terminal_runtime_release"] == {
        "cleanup_status": "succeeded",
        "cleanup_reason_code": "CLEANUP_SUCCEEDED",
    }
    assert audit_events[0].payload["evidence"] == {
        "error_type": "CancelledError",
        "error_message": "CancelledError: operation was cancelled",
        "terminal_runtime_release": terminal_runtime_release,
    }


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_complete_when_terminal_release_event_recording_raises(
    session: AsyncSession,
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    service, stopper, cleaner = _service(session)

    async def _raise_event_recording_failure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("terminal runtime release event insert failed")

    monkeypatch.setattr(
        "awf.service.controls.record_terminal_runtime_release_event",
        _raise_event_recording_failure,
    )

    with capture_logs() as captured:
        if action == "cancel":
            response = await service.cancel_workspace(
                workspace.id,
                reason="operator requested",
                stop_stack=True,
            )
        else:
            response = await service.stop_workspace(
                workspace.id,
                reason="operator requested",
            )

    operations = await _operations(session, workspace.id)
    release_events = [
        event
        for event in await _events(session, workspace.id)
        if event.event_type.startswith("workspace.terminal_runtime_release")
    ]

    assert response.status == WorkspaceStatus.cancelled
    assert workspace.status == WorkspaceStatus.cancelled.value
    assert stopper.calls == [workspace.compose_project_name]
    assert len(cleaner.calls) == 1
    assert operations[0].status == OperationStatus.succeeded.value
    assert operations[0].result == {"status": WorkspaceStatus.cancelled.value}
    assert release_events == []
    assert any(
        entry["event"] == "controls.terminal_runtime_release_event_record_failed"
        and entry["workspace_id"] == workspace.id
        and entry["source"] == f"service.controls.{action}"
        for entry in captured
    )


@pytest.mark.unit
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_cancel_and_stop_complete_when_terminal_release_event_recording_hits_db_error(
    session: AsyncSession,
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    service, stopper, cleaner = _service(session)

    async def _raise_db_event_recording_failure(
        session: AsyncSession,
        *args: object,
        **kwargs: object,
    ) -> None:
        await session.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(
        "awf.service.controls.record_terminal_runtime_release_event",
        _raise_db_event_recording_failure,
    )

    with capture_logs() as captured:
        if action == "cancel":
            response = await service.cancel_workspace(
                workspace.id,
                reason="operator requested",
                stop_stack=True,
            )
        else:
            response = await service.stop_workspace(
                workspace.id,
                reason="operator requested",
            )

    operations = await _operations(session, workspace.id)
    release_events = [
        event
        for event in await _events(session, workspace.id)
        if event.event_type.startswith("workspace.terminal_runtime_release")
    ]

    assert response.status == WorkspaceStatus.cancelled
    assert workspace.status == WorkspaceStatus.cancelled.value
    assert stopper.calls == [workspace.compose_project_name]
    assert len(cleaner.calls) == 1
    assert operations[0].status == OperationStatus.succeeded.value
    assert operations[0].result == {"status": WorkspaceStatus.cancelled.value}
    assert release_events == []
    assert any(
        entry["event"] == "controls.terminal_runtime_release_event_record_failed"
        and entry["workspace_id"] == workspace.id
        and entry["source"] == f"service.controls.{action}"
        for entry in captured
    )


@pytest.mark.unit
async def test_terminal_runtime_release_event_flush_failure_is_logged_without_raising(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.cancelled)
    service, _stopper, _cleaner = _service(session)
    release = controls_module._ControlTerminalRuntimeCleanup(  # noqa: SLF001
        cleanup=WorkspaceCleanupResult.from_steps(
            [
                WorkspaceCleanupStepResult(
                    name="compose_down",
                    status="succeeded",
                    reason_code=COMPOSE_DOWN_SUCCEEDED,
                )
            ]
        ),
        preserved_worktree_host_path=None,
    )

    async def _raise_flush_failure(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("deferred flush failed")

    monkeypatch.setattr(session, "flush", _raise_flush_failure)

    with capture_logs() as captured:
        await service._record_terminal_runtime_release_for_control(  # noqa: SLF001
            workspace,
            release=release,
            source="service.controls.test",
        )

    assert any(
        entry["event"] == "controls.terminal_runtime_release_event_record_failed"
        and entry["workspace_id"] == workspace.id
        and entry["source"] == "service.controls.test"
        and "deferred flush failed" in entry["error"]
        for entry in captured
    )


@pytest.mark.unit
async def test_cancel_with_no_stop_stack_skips_terminal_runtime_release(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    worktrees_root = tmp_path / "git" / "worktrees"
    service, stopper, cleaner = _service(session, worktrees_root=worktrees_root)

    response = await service.cancel_workspace(
        workspace.id,
        reason="operator requested",
        stop_stack=False,
    )
    events = await _events(session, workspace.id)

    assert response.status == WorkspaceStatus.cancelled
    assert workspace.status == WorkspaceStatus.cancelled.value
    assert stopper.calls == []
    assert cleaner.calls == []
    assert not any(
        event.event_type.startswith("workspace.terminal_runtime_release") for event in events
    )


@pytest.mark.unit
async def test_cancel_already_cancelled_workspace_with_stop_stack_releases_terminal_runtime(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.cancelled)
    worktrees_root = tmp_path / "git" / "worktrees"
    service, stopper, cleaner = _service(session, worktrees_root=worktrees_root)

    response = await service.cancel_workspace(
        workspace.id,
        reason="repeat cancel",
        stop_stack=True,
    )
    events = await _events(session, workspace.id)

    assert response.status == WorkspaceStatus.cancelled
    assert workspace.status == WorkspaceStatus.cancelled.value
    assert stopper.calls == [workspace.compose_project_name]
    assert len(cleaner.calls) == 1
    assert cleaner.calls[0].remove_volumes is False
    assert cleaner.calls[0].remove_worktree is False
    cancel_event = next(
        event for event in events if event.event_type == "workspace.cancel_requested"
    )
    release_event = next(
        event for event in events if event.event_type == "workspace.terminal_runtime_released"
    )
    assert cancel_event.payload == {"reason": "repeat cancel", "stop_stack": True}
    assert release_event.reason_code == "TERMINAL_RUNTIME_RELEASED"


@pytest.mark.unit
async def test_cancel_terminal_workspace_records_request_event_without_transition(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.completed)
    service, stopper, _cleaner = _service(session)

    response = await service.cancel_workspace(
        workspace.id,
        reason=None,
        stop_stack=False,
    )
    events = await _events(session, workspace.id)

    assert response.status == WorkspaceStatus.completed
    assert stopper.calls == []
    assert workspace.status == WorkspaceStatus.completed.value
    cancel_event = next(
        event for event in events if event.event_type == "workspace.cancel_requested"
    )
    assert cancel_event.reason_code == "OPERATOR_CANCEL"
    assert cancel_event.payload == {"reason": None, "stop_stack": False}


@pytest.mark.unit
async def test_stop_active_workspace_cancels_and_terminal_workspace_records_event(
    session: AsyncSession,
) -> None:
    active = await _workspace(session, status=WorkspaceStatus.running, title="active stop")
    terminal = await _workspace(
        session,
        status=WorkspaceStatus.completed,
        title="terminal stop",
    )
    service, stopper, _cleaner = _service(session)

    active_response = await service.stop_workspace(active.id, reason="halt")
    terminal_response = await service.stop_workspace(terminal.id, reason="already done")
    terminal_events = await _events(session, terminal.id)
    terminal_audit = await WorkspaceEventRepository(session).list(
        workspace_id=terminal.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert active_response.status == WorkspaceStatus.cancelled
    assert terminal_response.status == WorkspaceStatus.completed
    assert active.status == WorkspaceStatus.cancelled.value
    assert terminal.status == WorkspaceStatus.completed.value
    assert stopper.calls == [active.compose_project_name, terminal.compose_project_name]
    stack_event = next(
        event for event in terminal_events if event.event_type == "workspace.stack_stopped"
    )
    assert stack_event.payload == {"reason": "already done"}
    assert len(terminal_audit) == 1
    assert terminal_audit[0].payload is not None
    assert terminal_audit[0].payload["actor"] == "operator_api"
    assert terminal_audit[0].payload["action"] == "stop"
    assert terminal_audit[0].payload["outcome"] == "succeeded"
    assert terminal_audit[0].payload["reason_code"] == "OPERATOR_STOP"


@pytest.mark.unit
async def test_stop_workspace_replays_existing_idempotent_operation(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.completed)
    service, stopper, _cleaner = _service(session)

    response = await service.stop_workspace(
        workspace.id,
        reason="first stop",
        idempotency_key="stop-replay",
    )
    replay = await service.stop_workspace(
        workspace.id,
        reason="first stop",
        idempotency_key="stop-replay",
    )
    operations = await _operations(session, workspace.id)

    assert response.operation_id == replay.operation_id
    assert replay.message == "workspace stack stopped"
    assert replay.status == WorkspaceStatus.completed
    assert stopper.calls == [workspace.compose_project_name]
    assert [operation.id for operation in operations] == [response.operation_id]


@pytest.mark.unit
async def test_terminal_cancel_stop_events_include_expected_version(
    session: AsyncSession,
) -> None:
    cancel = await _workspace(
        session,
        status=WorkspaceStatus.completed,
        title="terminal cancel with expected version",
    )
    stop = await _workspace(
        session,
        status=WorkspaceStatus.completed,
        title="terminal stop with expected version",
    )
    service, _stopper, _cleaner = _service(session)

    await service.cancel_workspace(
        cancel.id,
        reason="cancel audit",
        stop_stack=False,
        expected_version=cancel.version,
    )
    await service.stop_workspace(
        stop.id,
        reason="stop audit",
        expected_version=stop.version,
    )
    cancel_event = next(
        event
        for event in await _events(session, cancel.id)
        if event.event_type == "workspace.cancel_requested"
    )
    stop_event = next(
        event
        for event in await _events(session, stop.id)
        if event.event_type == "workspace.stack_stopped"
    )

    assert cancel_event.payload == {
        "reason": "cancel audit",
        "stop_stack": False,
        "expected_version": 1,
    }
    assert stop_event.payload == {
        "reason": "stop audit",
        "expected_version": 1,
    }


@pytest.mark.unit
async def test_idempotent_replay_returns_original_operation_audit_unchanged(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.completed)
    service, _stopper, _cleaner = _service(session)

    first = await service.stop_workspace(
        workspace.id,
        reason="preserve audit",
        idempotency_key="stop-audit-replay",
    )
    operation = (await _operations(session, workspace.id))[0]
    original_payload = dict(operation.payload or {})
    original_result = dict(operation.result or {})
    original_started_at = operation.started_at
    original_finished_at = operation.finished_at
    replay = await service.stop_workspace(
        workspace.id,
        reason="preserve audit",
        idempotency_key="stop-audit-replay",
    )
    replayed = (await _operations(session, workspace.id))[0]
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert replay.operation_id == first.operation_id
    assert replayed.id == operation.id
    assert (
        replayed.payload
        == original_payload
        == {
            "owner": "operator_api",
            "source": "operator_api",
            "reason": "preserve audit",
            "reason_code": "OPERATOR_STOP",
            "requested_action": "stop",
        }
    )
    assert replayed.result == original_result
    assert replayed.started_at == original_started_at
    assert replayed.finished_at == original_finished_at
    assert replayed.idempotency_key == "stop-audit-replay"
    assert len(audit_events) == 1
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["operation_id"] == first.operation_id


@pytest.mark.unit
async def test_stop_stack_failure_finishes_operation_failed_with_audit(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    stopper = FailingStopper()
    service, _stopper, _cleaner = _service(session, stopper=stopper)

    with pytest.raises(WorkspaceStackStopError) as exc_info:
        await service.stop_workspace(
            workspace.id,
            reason="operator stop",
            idempotency_key="stop-fails",
        )
    operations = await _operations(session, workspace.id)
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert exc_info.value.error_code == "STACK_STOP_FAILED"
    assert stopper.calls == [workspace.compose_project_name]
    assert workspace.status == WorkspaceStatus.ready.value
    assert len(operations) == 1
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "STACK_STOP_FAILED"
    assert "compose stop denied" in (operations[0].error_message or "")
    assert operations[0].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "operator stop",
        "reason_code": "OPERATOR_STOP",
        "requested_action": "stop",
    }
    assert operations[0].started_at is not None
    assert operations[0].finished_at is not None
    assert len(audit_events) == 1
    assert audit_events[0].reason_code == "STACK_STOP_FAILED"
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["action"] == "stop"
    assert audit_events[0].payload["outcome"] == "failed"
    assert audit_events[0].payload["operation_id"] == operations[0].id
    assert audit_events[0].payload["evidence"]["error_message"] == (
        "docker stop failed (exit=17): compose stop denied"
    )


@pytest.mark.unit
async def test_cancel_stack_failure_finishes_operation_failed_with_audit(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    stopper = FailingStopper()
    service, _stopper, _cleaner = _service(session, stopper=stopper)

    with pytest.raises(WorkspaceStackStopError):
        await service.cancel_workspace(
            workspace.id,
            reason="operator cancel",
            stop_stack=True,
            idempotency_key="cancel-fails",
        )
    operations = await _operations(session, workspace.id)
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert stopper.calls == [workspace.compose_project_name]
    assert workspace.status == WorkspaceStatus.ready.value
    assert len(operations) == 1
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "STACK_STOP_FAILED"
    assert "compose stop denied" in (operations[0].error_message or "")
    assert operations[0].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "operator cancel",
        "reason_code": "OPERATOR_CANCEL",
        "requested_action": "cancel",
        "stop_stack": True,
    }
    assert len(audit_events) == 1
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["action"] == "cancel"
    assert audit_events[0].payload["outcome"] == "failed"
    assert audit_events[0].payload["reason_code"] == "STACK_STOP_FAILED"


@pytest.mark.unit
async def test_control_prepare_operation_rejects_missing_conflicting_and_stale_requests(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    service, stopper, _cleaner = _service(session)

    await service.cancel_workspace(
        workspace.id,
        reason="first",
        stop_stack=False,
        idempotency_key="control-conflict",
    )

    with pytest.raises(IdempotencyConflictError) as conflict:
        await service.cancel_workspace(
            workspace.id,
            reason="different",
            stop_stack=False,
            idempotency_key="control-conflict",
        )
    with pytest.raises(VersionConflictError) as version:
        await service.stop_workspace(
            workspace.id,
            reason="stale",
            expected_version=999,
        )
    with pytest.raises(WorkspaceNotFoundError) as missing:
        await service.cancel_workspace(
            "ws_missing",
            reason=None,
            stop_stack=False,
        )

    assert conflict.value.error_code == "IDEMPOTENCY_CONFLICT"
    assert version.value.detail == {"expected_version": 999, "actual_version": 2}
    assert missing.value.error_code == "NOT_FOUND"
    assert stopper.calls == []


@pytest.mark.unit
async def test_control_prepare_operation_treats_blank_idempotency_key_as_absent(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    service, _stopper, _cleaner = _service(session)

    await service.cancel_workspace(
        workspace.id,
        reason="blank-key-should-be-absent",
        stop_stack=False,
        idempotency_key="   ",
    )

    operations = await _operations(session, workspace.id)
    assert len(operations) == 1
    assert operations[0].idempotency_key is None


@pytest.mark.unit
async def test_control_require_workspace_reports_missing_workspace(
    session: AsyncSession,
) -> None:
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceNotFoundError) as missing:
        await service._require_workspace(WorkspaceRepository(session), "ws_missing")

    assert missing.value.error_code == "NOT_FOUND"


@pytest.mark.unit
async def test_remonitor_rejects_wrong_state_and_missing_pr_before_creating_operation(
    session: AsyncSession,
) -> None:
    requested = await _workspace(session, status=WorkspaceStatus.requested)
    missing_pr = await _workspace(
        session,
        status=WorkspaceStatus.monitoring_pr,
        title="monitoring without pr",
    )
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceRemonitorStateError) as wrong_state:
        await service.remonitor_workspace(requested.id, reason="retry monitor")
    with pytest.raises(WorkspaceRemonitorMissingPrUrlError) as missing_pr_error:
        await service.remonitor_workspace(missing_pr.id, reason="retry monitor")

    assert wrong_state.value.detail == {
        "status": WorkspaceStatus.requested.value,
        "eligible_statuses": [
            WorkspaceStatus.monitoring_pr.value,
            WorkspaceStatus.failed.value,
        ],
    }
    assert missing_pr_error.value.detail == {"status": WorkspaceStatus.monitoring_pr.value}
    assert await _operations(session, requested.id) == []
    assert await _operations(session, missing_pr.id) == []


@pytest.mark.unit
async def test_remonitor_resets_claims_records_snapshot_and_replays(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    monitor_expiry = datetime(2026, 4, 27, 16, 0, tzinfo=UTC)
    execution_expiry = monitor_expiry + timedelta(minutes=10)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/42"
    workspace.pr_number = 42
    workspace.base_commit = "b" * 40
    workspace.monitor_last_commit_sha = "h" * 40
    workspace.monitor_claimed_by = "monitor-worker"
    workspace.monitor_claim_expires_at = monitor_expiry
    workspace.execution_claimed_by = "execution-worker"
    workspace.execution_claim_expires_at = execution_expiry
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason="worker restarted",
        idempotency_key="remonitor-same-key",
        expected_version=workspace.version,
    )
    replay = await service.remonitor_workspace(
        workspace.id,
        reason="worker restarted",
        idempotency_key="remonitor-same-key",
        expected_version=workspace.version - 1,
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    expected_snapshot = {
        "monitor_claimed_by": "monitor-worker",
        "monitor_claim_expires_at": monitor_expiry.isoformat(),
        "execution_claimed_by": "execution-worker",
        "execution_claim_expires_at": execution_expiry.isoformat(),
    }

    assert response.operation_id == replay.operation_id
    assert response.status == WorkspaceStatus.monitoring_pr
    assert workspace.version == 2
    assert workspace.monitor_claimed_by is None
    assert workspace.monitor_claim_expires_at is None
    assert workspace.execution_claimed_by is None
    assert workspace.execution_claim_expires_at is None
    assert operations[0].type == OperationType.remonitor.value
    assert operations[0].status == "succeeded"
    assert operations[0].result == {
        "status": WorkspaceStatus.monitoring_pr.value,
        "claims_reset": expected_snapshot,
        "pr_number": 42,
        "pr_url": "https://github.com/example/control-lifecycle/pull/42",
        "source_head_sha": "h" * 40,
        "source_base_sha": "b" * 40,
    }
    assert events[0].event_type == "workspace.remonitor_requested"
    assert events[0].payload == {
        "reason": "worker restarted",
        "operation_id": operations[0].id,
        "claims_reset": expected_snapshot,
        "expected_version": 1,
    }


@pytest.mark.unit
async def test_cancel_stop_destroy_remonitor_payloads_include_operator_audit(
    session: AsyncSession,
) -> None:
    cancel = await _workspace(session, status=WorkspaceStatus.completed, title="cancel audit")
    stop = await _workspace(session, status=WorkspaceStatus.completed, title="stop audit")
    destroy = await _workspace(session, status=WorkspaceStatus.destroyed, title="destroy audit")
    remonitor = await _workspace(
        session, status=WorkspaceStatus.monitoring_pr, title="remonitor audit"
    )
    remonitor.pr_url = "https://github.com/example/control-lifecycle/pull/45"
    remonitor.pr_number = 45
    remonitor.base_commit = "b" * 40
    remonitor.monitor_last_commit_sha = "h" * 40
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    await service.cancel_workspace(cancel.id, reason="cancel it", stop_stack=False)
    await service.stop_workspace(stop.id, reason="stop it")
    await service.destroy_workspace(
        destroy.id,
        force=False,
        remove_volumes=True,
        remove_worktree=False,
    )
    await service.remonitor_workspace(remonitor.id, reason="rerun monitor")

    operations_by_type = {
        operation.type: operation
        for operation in await OperationRepository(session).list_all(limit=20)
    }

    assert operations_by_type[OperationType.cancel.value].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "cancel it",
        "reason_code": "OPERATOR_CANCEL",
        "requested_action": "cancel",
        "stop_stack": False,
    }
    assert operations_by_type[OperationType.stop.value].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "stop it",
        "reason_code": "OPERATOR_STOP",
        "requested_action": "stop",
    }
    assert operations_by_type[OperationType.destroy.value].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": None,
        "reason_code": "OPERATOR_DESTROY",
        "requested_action": "destroy",
        "force": False,
        "remove_volumes": True,
        "remove_worktree": False,
    }
    assert operations_by_type[OperationType.remonitor.value].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "rerun monitor",
        "reason_code": "OPERATOR_REMONITOR",
        "requested_action": "remonitor",
        "pr_number": 45,
        "pr_url": "https://github.com/example/control-lifecycle/pull/45",
        "source_head_sha": "h" * 40,
        "source_base_sha": "b" * 40,
    }


@pytest.mark.unit
async def test_refresh_active_workspace_creates_pending_operation_and_coalesces_by_reason(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_refresh_workspace(
        workspace.id,
        reason="stale merge queue",
        idempotency_key="refresh-first",
        expected_version=workspace.version,
    )
    replay = await service.request_refresh_workspace(
        workspace.id,
        reason="stale merge queue",
        idempotency_key="refresh-fresh-key",
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    refresh_event = next(
        event for event in events if event.event_type == "workspace.refresh_requested"
    )

    assert replay.id == operation.id
    assert workspace.status == WorkspaceStatus.ready.value
    assert [row.id for row in operations] == [operation.id]
    assert operation.type == OperationType.refresh.value
    assert operation.status == OperationStatus.pending.value
    assert operation.idempotency_key == "refresh-first"
    assert operation.payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "stale merge queue",
        "reason_code": "OPERATOR_REFRESH",
        "requested_action": "refresh",
        "expected_version": 1,
    }
    assert refresh_event.reason_code == "OPERATOR_REFRESH"
    assert refresh_event.payload == {
        "source": "operator_api",
        "reason": "stale merge queue",
        "operation_id": operation.id,
        "expected_version": 1,
    }


@pytest.mark.unit
async def test_refresh_fresh_key_with_stale_if_match_does_not_coalesce_active_operation(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_refresh_workspace(
        workspace.id,
        reason="stale merge queue",
        idempotency_key="refresh-original",
        expected_version=workspace.version,
    )
    workspace.version += 1
    await session.flush()

    replay = await service.request_refresh_workspace(
        workspace.id,
        reason="stale merge queue",
        idempotency_key="refresh-original",
        expected_version=1,
    )
    with pytest.raises(VersionConflictError) as exc_info:
        await service.request_refresh_workspace(
            workspace.id,
            reason="stale merge queue",
            idempotency_key="refresh-fresh-stale-version",
            expected_version=1,
        )

    assert replay.id == operation.id
    assert exc_info.value.detail == {"expected_version": 1, "actual_version": 2}
    assert [row.id for row in await _operations(session, workspace.id)] == [operation.id]


@pytest.mark.unit
async def test_remonitor_failed_workspace_with_pr_reenters_monitoring(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/43"
    workspace.pr_number = 43
    workspace.failure_reason = "infrastructure_failure"
    workspace.failure_message = "old worker died during rebase recovery"
    workspace.monitor_iter_count = 8
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason="reattach failed PR",
        idempotency_key="remonitor-failed-pr",
        expected_version=workspace.version,
    )
    events = await _events(session, workspace.id)

    assert response.status == WorkspaceStatus.monitoring_pr
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.failure_reason is None
    assert workspace.failure_message is None
    assert workspace.monitor_iter_count == 0
    assert events[0].event_type == "workspace.remonitor_requested"
    assert events[0].old_state == WorkspaceStatus.failed.value
    assert events[0].new_state == WorkspaceStatus.monitoring_pr.value
    assert events[0].payload["state_reset"] == {
        "from": WorkspaceStatus.failed.value,
        "to": WorkspaceStatus.monitoring_pr.value,
        "monitor_iter_count_reset_from": 8,
        "candidate_reopened": False,
    }


@pytest.mark.unit
async def test_remonitor_failed_workspace_cancels_stale_pr_monitor_recovery_ops(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/44"
    workspace.pr_number = 44
    workspace.failure_reason = "infrastructure_failure"
    workspace.failure_message = "monitor process died during validation recovery"
    operation_repo = OperationRepository(session)
    stale_validate = await operation_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.validate,
        status=OperationStatus.running,
        payload={
            "owner": "pr_monitor",
            "source": "pr_monitor",
            "recovery_mode": "validate_only",
            "reason_code": "VALIDATION_INSUFFICIENT_TIER",
        },
    )
    operator_validate = await operation_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.validate,
        status=OperationStatus.running,
        payload={
            "owner": "operator",
            "source": "operator_api",
            "recovery_mode": "validate_only",
            "reason_code": "OPERATOR_VALIDATE",
        },
    )
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason="reattach after stale validation op",
        idempotency_key="remonitor-cancel-stale-recovery",
        expected_version=workspace.version,
    )
    operations = {op.id: op for op in await _operations(session, workspace.id)}
    events = await _events(session, workspace.id)

    assert response.status == WorkspaceStatus.monitoring_pr
    assert operations[stale_validate.id].status == OperationStatus.cancelled.value
    assert operations[stale_validate.id].error_code == "OPERATOR_REMONITOR"
    assert operations[stale_validate.id].result == {
        "status": "cancelled",
        "reason_code": "OPERATOR_REMONITOR",
        "requested_action": "remonitor",
    }
    assert operations[operator_validate.id].status == OperationStatus.running.value
    remonitor_event = next(
        event for event in events if event.event_type == "workspace.remonitor_requested"
    )
    assert remonitor_event.payload["cancelled_recovery_operations"] == [
        {
            "operation_id": stale_validate.id,
            "operation_type": OperationType.validate.value,
            "operation_status": OperationStatus.running.value,
        }
    ]


@pytest.mark.unit
async def test_refresh_replays_same_idempotency_key_after_destroying_state(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_refresh_workspace(
        workspace.id,
        reason="stale merge queue",
        idempotency_key="refresh-before-destroy",
    )
    workspace.status = WorkspaceStatus.destroying.value
    await session.flush()

    replay = await service.request_refresh_workspace(
        workspace.id,
        reason="stale merge queue",
        idempotency_key="refresh-before-destroy",
    )
    operations = await _operations(session, workspace.id)

    assert replay.id == operation.id
    assert [row.id for row in operations] == [operation.id]


@pytest.mark.unit
async def test_validate_monitoring_pr_creates_validate_only_operation_and_coalesces(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/44"
    workspace.pr_number = 44
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_validate_workspace(
        workspace.id,
        reason="rerun required validation",
        requested_tier=2,
        idempotency_key="validate-first",
        expected_version=workspace.version,
    )
    replay = await service.request_validate_workspace(
        workspace.id,
        reason="rerun required validation",
        requested_tier=2,
        idempotency_key="validate-fresh-key",
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    validate_event = next(
        event for event in events if event.event_type == "workspace.validate_requested"
    )
    state_event = next(
        event
        for event in events
        if event.event_type == "workspace.state_changed"
        and event.reason_code == "OPERATOR_VALIDATE"
    )

    assert replay.id == operation.id
    assert workspace.status == WorkspaceStatus.ready.value
    assert workspace.version == 2
    assert [row.id for row in operations] == [operation.id]
    assert operation.type == OperationType.validate.value
    assert operation.status == OperationStatus.pending.value
    assert operation.idempotency_key == "validate-first"
    assert operation.payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "rerun required validation",
        "reason_code": "OPERATOR_VALIDATE",
        "requested_action": "validate",
        "recovery_mode": "validate_only",
        "requested_tier": 2,
        "expected_version": 1,
    }
    assert validate_event.reason_code == "OPERATOR_VALIDATE"
    assert validate_event.payload == {
        "source": "operator_api",
        "reason": "rerun required validation",
        "operation_id": operation.id,
        "recovery_mode": "validate_only",
        "requested_tier": 2,
        "expected_version": 1,
    }
    assert state_event.old_state == WorkspaceStatus.monitoring_pr.value
    assert state_event.new_state == WorkspaceStatus.ready.value


@pytest.mark.unit
async def test_validate_translates_teardown_race_during_ready_transition(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/49"
    await session.flush()
    original_add_event = WorkspaceRepository.add_event
    active_stop: Operation | None = None

    async def add_event_and_start_teardown(
        self: WorkspaceRepository,
        target: Workspace,
        *,
        event_type: str,
        reason_code: str,
        payload: dict[str, object] | None = None,
    ) -> WorkspaceEvent:
        nonlocal active_stop
        event = await original_add_event(
            self,
            target,
            event_type=event_type,
            reason_code=reason_code,
            payload=payload,
        )
        if event_type == "workspace.validate_requested":
            active_stop = await OperationRepository(session).create(
                workspace_id=target.id,
                operation_type=OperationType.stop,
                status=OperationStatus.running,
                payload={"source": "operator_api"},
            )
        return event

    monkeypatch.setattr(WorkspaceRepository, "add_event", add_event_and_start_teardown)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceActiveOperationConflictError) as exc_info:
        await service.request_validate_workspace(
            workspace.id,
            reason="rerun required validation",
        )

    assert active_stop is not None
    assert exc_info.value.detail == {
        "operation_id": active_stop.id,
        "operation_type": OperationType.stop.value,
        "operation_status": OperationStatus.running.value,
    }
    assert workspace.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_validate_fresh_key_with_stale_if_match_does_not_coalesce_active_operation(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/48"
    workspace.pr_number = 48
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_validate_workspace(
        workspace.id,
        reason="rerun required validation",
        requested_tier=2,
        idempotency_key="validate-original",
        expected_version=workspace.version,
    )

    replay = await service.request_validate_workspace(
        workspace.id,
        reason="rerun required validation",
        requested_tier=2,
        idempotency_key="validate-original",
        expected_version=1,
    )
    with pytest.raises(VersionConflictError) as exc_info:
        await service.request_validate_workspace(
            workspace.id,
            reason="rerun required validation",
            requested_tier=2,
            idempotency_key="validate-fresh-stale-version",
            expected_version=1,
        )

    assert replay.id == operation.id
    assert workspace.version == 2
    assert exc_info.value.detail == {"expected_version": 1, "actual_version": 2}
    assert [row.id for row in await _operations(session, workspace.id)] == [operation.id]


@pytest.mark.unit
@pytest.mark.parametrize(
    "transient_status",
    [WorkspaceStatus.running, WorkspaceStatus.validating],
)
async def test_validate_replay_coalesces_during_executor_transient_states(
    session: AsyncSession,
    transient_status: WorkspaceStatus,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/47"
    workspace.pr_number = 47
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_validate_workspace(
        workspace.id,
        reason="rerun required validation",
        requested_tier=2,
    )
    operation.status = OperationStatus.running.value
    workspace.status = transient_status.value
    await session.flush()

    replay = await service.request_validate_workspace(
        workspace.id,
        reason="rerun required validation",
        requested_tier=2,
    )

    assert replay.id == operation.id
    assert [row.id for row in await _operations(session, workspace.id)] == [operation.id]


@pytest.mark.unit
async def test_validate_without_requested_tier_omits_tier_from_payload(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/46"
    workspace.pr_number = 46
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_validate_workspace(
        workspace.id,
        reason="rerun default validation",
    )
    events = await _events(session, workspace.id)
    validate_event = next(
        event for event in events if event.event_type == "workspace.validate_requested"
    )

    assert operation.payload is not None
    assert "requested_tier" not in operation.payload
    assert validate_event.payload is not None
    assert "requested_tier" not in validate_event.payload


@pytest.mark.unit
async def test_validate_same_key_with_different_requested_tier_conflicts(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/45"
    workspace.pr_number = 45
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    await service.request_validate_workspace(
        workspace.id,
        reason="rerun required validation",
        requested_tier=2,
        idempotency_key="validate-tier-conflict",
    )

    with pytest.raises(IdempotencyConflictError):
        await service.request_validate_workspace(
            workspace.id,
            reason="rerun required validation",
            requested_tier=3,
            idempotency_key="validate-tier-conflict",
        )


@pytest.mark.unit
async def test_rebase_monitoring_pr_creates_rebase_operation_and_replays_exact_key(
    session: AsyncSession,
) -> None:
    workspace, candidate = await _workspace_with_candidate(session)
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_rebase_workspace(
        workspace.id,
        reason="base branch advanced",
        idempotency_key="rebase-first",
        expected_version=workspace.version,
    )
    replay = await service.request_rebase_workspace(
        workspace.id,
        reason="base branch advanced",
        idempotency_key="rebase-first",
        expected_version=1,
    )
    with pytest.raises(WorkspaceRebaseStateError) as fresh_key_error:
        await service.request_rebase_workspace(
            workspace.id,
            reason="base branch advanced",
            idempotency_key="rebase-fresh-key",
        )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    rebase_event = next(
        event for event in events if event.event_type == "workspace.rebase_requested"
    )
    state_event = next(
        event
        for event in events
        if event.event_type == "workspace.state_changed" and event.reason_code == "OPERATOR_REBASE"
    )

    assert replay.id == operation.id
    assert fresh_key_error.value.detail == {
        "status": WorkspaceStatus.ready.value,
        "eligible_statuses": [WorkspaceStatus.monitoring_pr.value],
    }
    assert workspace.status == WorkspaceStatus.ready.value
    assert workspace.version == 2
    assert [row.id for row in operations] == [operation.id]
    assert operation.type == OperationType.rebase.value
    assert operation.status == OperationStatus.pending.value
    assert operation.idempotency_key == "rebase-first"
    assert operation.payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "base branch advanced",
        "reason_code": "OPERATOR_REBASE",
        "requested_action": "rebase",
        "recovery_mode": "rebase_only",
        "candidate_id": candidate.id,
        "attempt_id": candidate.attempt_id,
        "task_id": candidate.task_id,
        "pr_number": 50,
        "pr_url": "https://github.com/example/control-lifecycle/pull/50",
        "source_head_sha": "h" * 40,
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace.id}",
        "expected_version": 1,
    }
    assert rebase_event.reason_code == "OPERATOR_REBASE"
    assert rebase_event.payload == {
        "source": "operator_api",
        "reason": "base branch advanced",
        "operation_id": operation.id,
        "recovery_mode": "rebase_only",
        "candidate_id": candidate.id,
        "expected_version": 1,
    }
    assert state_event.old_state == WorkspaceStatus.monitoring_pr.value
    assert state_event.new_state == WorkspaceStatus.ready.value


@pytest.mark.unit
async def test_rebase_fresh_key_with_stale_if_match_does_not_coalesce_active_operation(
    session: AsyncSession,
) -> None:
    workspace, _candidate = await _workspace_with_candidate(session)
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_rebase_workspace(
        workspace.id,
        reason="base branch advanced",
        idempotency_key="rebase-original",
        expected_version=workspace.version,
    )

    replay = await service.request_rebase_workspace(
        workspace.id,
        reason="base branch advanced",
        idempotency_key="rebase-original",
        expected_version=1,
    )
    with pytest.raises(VersionConflictError) as exc_info:
        await service.request_rebase_workspace(
            workspace.id,
            reason="base branch advanced",
            idempotency_key="rebase-fresh-stale-version",
            expected_version=1,
        )

    assert replay.id == operation.id
    assert workspace.version == 2
    assert exc_info.value.detail == {"expected_version": 1, "actual_version": 2}
    assert [row.id for row in await _operations(session, workspace.id)] == [operation.id]


@pytest.mark.unit
async def test_rebase_same_key_with_different_reason_conflicts(
    session: AsyncSession,
) -> None:
    workspace, _candidate = await _workspace_with_candidate(session)
    service, _stopper, _cleaner = _service(session)

    await service.request_rebase_workspace(
        workspace.id,
        reason="base branch advanced",
        idempotency_key="rebase-reason-conflict",
    )

    with pytest.raises(IdempotencyConflictError):
        await service.request_rebase_workspace(
            workspace.id,
            reason="different base branch reason",
            idempotency_key="rebase-reason-conflict",
        )


@pytest.mark.unit
async def test_rebase_same_key_with_different_expected_version_conflicts_without_duplicate_rows(
    session: AsyncSession,
) -> None:
    workspace, _candidate = await _workspace_with_candidate(session)
    service, _stopper, _cleaner = _service(session)
    original_version = workspace.version

    operation = await service.request_rebase_workspace(
        workspace.id,
        reason="base branch advanced",
        idempotency_key="rebase-if-match-conflict",
        expected_version=original_version,
    )
    before_operation_ids = [row.id for row in await _operations(session, workspace.id)]
    before_event_ids = [row.id for row in await _events(session, workspace.id)]

    with pytest.raises(IdempotencyConflictError):
        await service.request_rebase_workspace(
            workspace.id,
            reason="base branch advanced",
            idempotency_key="rebase-if-match-conflict",
            expected_version=original_version + 1,
        )

    assert before_operation_ids == [operation.id]
    assert [row.id for row in await _operations(session, workspace.id)] == before_operation_ids
    assert [row.id for row in await _events(session, workspace.id)] == before_event_ids


@pytest.mark.unit
async def test_rebase_active_incompatible_payload_conflicts_without_duplicate_operation(
    session: AsyncSession,
) -> None:
    workspace, _candidate = await _workspace_with_candidate(session)
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_rebase_workspace(
        workspace.id,
        reason="base branch advanced",
        idempotency_key="rebase-original",
    )

    with pytest.raises(WorkspaceRebaseActiveConflictError) as exc_info:
        await service.request_rebase_workspace(
            workspace.id,
            reason="different base branch reason",
            idempotency_key="rebase-conflicting",
        )

    assert exc_info.value.error_code == "WORKSPACE_REBASE_CONFLICT"
    assert exc_info.value.detail == {
        "operation_id": operation.id,
        "operation_type": OperationType.rebase.value,
        "operation_status": OperationStatus.pending.value,
    }
    assert [row.id for row in await _operations(session, workspace.id)] == [operation.id]


@pytest.mark.unit
async def test_rebase_rejects_missing_pr_candidate_state_and_destructive_conflicts(
    session: AsyncSession,
) -> None:
    wrong_state, _wrong_candidate = await _workspace_with_candidate(
        session,
        status=WorkspaceStatus.completed,
        title="rebase completed",
    )
    missing_pr = await _workspace(
        session,
        status=WorkspaceStatus.monitoring_pr,
        title="rebase missing pr",
    )
    missing_candidate = await _workspace(
        session,
        status=WorkspaceStatus.monitoring_pr,
        title="rebase missing candidate",
    )
    missing_candidate.pr_url = "https://github.com/example/control-lifecycle/pull/51"
    missing_candidate.pr_number = 51
    destructive, _destructive_candidate = await _workspace_with_candidate(
        session,
        title="rebase destructive conflict",
    )
    conflict = await OperationRepository(session).create(
        workspace_id=destructive.id,
        operation_type=OperationType.destroy,
        status=OperationStatus.running,
        payload={"source": "operator_api"},
    )
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceRebaseStateError) as wrong_state_error:
        await service.request_rebase_workspace(wrong_state.id, reason="rebase")
    with pytest.raises(WorkspaceRebaseMissingPrUrlError) as missing_pr_error:
        await service.request_rebase_workspace(missing_pr.id, reason="rebase")
    with pytest.raises(WorkspaceRebaseMissingCandidateError) as missing_candidate_error:
        await service.request_rebase_workspace(missing_candidate.id, reason="rebase")
    with pytest.raises(WorkspaceRebaseActiveConflictError) as conflict_error:
        await service.request_rebase_workspace(destructive.id, reason="rebase")

    assert wrong_state_error.value.detail == {
        "status": WorkspaceStatus.completed.value,
        "eligible_statuses": [WorkspaceStatus.monitoring_pr.value],
    }
    assert missing_pr_error.value.detail == {"status": WorkspaceStatus.monitoring_pr.value}
    assert missing_candidate_error.value.detail == {
        "workspace_id": missing_candidate.id,
        "pr_url": "https://github.com/example/control-lifecycle/pull/51",
    }
    assert conflict_error.value.detail == {
        "operation_id": conflict.id,
        "operation_type": OperationType.destroy.value,
        "operation_status": OperationStatus.running.value,
    }
    assert await _operations(session, wrong_state.id) == []
    assert await _operations(session, missing_pr.id) == []
    assert await _operations(session, missing_candidate.id) == []
    assert [row.id for row in await _operations(session, destructive.id)] == [conflict.id]


@pytest.mark.unit
async def test_validate_rejects_missing_pr_url_before_creating_operation(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceValidateMissingPrUrlError) as exc_info:
        await service.request_validate_workspace(
            workspace.id,
            reason="rerun without pr",
        )

    assert exc_info.value.error_code == "WORKSPACE_PR_URL_REQUIRED"
    assert exc_info.value.detail == {"status": WorkspaceStatus.monitoring_pr.value}
    assert await _operations(session, workspace.id) == []


@pytest.mark.unit
async def test_validate_replay_rejects_workspace_that_left_replay_states(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.completed)
    payload = {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "rerun after completion",
        "reason_code": "OPERATOR_VALIDATE",
        "requested_action": OperationType.validate.value,
        "recovery_mode": "validate_only",
    }
    operation = await OperationRepository(session).create(
        workspace_id=workspace.id,
        operation_type=OperationType.validate,
        status=OperationStatus.pending,
        payload=payload,
    )
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceValidateStateError) as exc_info:
        await service.request_validate_workspace(
            workspace.id,
            reason="rerun after completion",
        )

    assert exc_info.value.detail == {
        "status": WorkspaceStatus.completed.value,
        "eligible_statuses": [WorkspaceStatus.monitoring_pr.value],
    }
    assert [row.id for row in await _operations(session, workspace.id)] == [operation.id]


@pytest.mark.unit
async def test_refresh_rejects_destroying_or_destroyed_without_creating_operation(
    session: AsyncSession,
) -> None:
    destroying = await _workspace(
        session,
        status=WorkspaceStatus.destroying,
        title="refresh destroying",
    )
    destroyed = await _workspace(
        session,
        status=WorkspaceStatus.destroyed,
        title="refresh destroyed",
    )
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceRefreshStateError) as destroying_error:
        await service.request_refresh_workspace(destroying.id, reason="refresh")
    with pytest.raises(WorkspaceRefreshStateError) as destroyed_error:
        await service.request_refresh_workspace(destroyed.id, reason="refresh")

    assert destroying_error.value.error_code == "WORKSPACE_STATE_NOT_REFRESHABLE"
    assert destroying_error.value.detail == {"status": WorkspaceStatus.destroying.value}
    assert destroyed_error.value.detail == {"status": WorkspaceStatus.destroyed.value}
    assert await _operations(session, destroying.id) == []
    assert await _operations(session, destroyed.id) == []


@pytest.mark.unit
async def test_validate_rejects_ineligible_state_before_creating_operation(
    session: AsyncSession,
) -> None:
    completed = await _workspace(session, status=WorkspaceStatus.completed)
    destroying = await _workspace(
        session,
        status=WorkspaceStatus.destroying,
        title="validate destroying",
    )
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceValidateStateError) as completed_error:
        await service.request_validate_workspace(
            completed.id,
            reason="rerun after merge",
        )
    with pytest.raises(WorkspaceValidateStateError) as destroying_error:
        await service.request_validate_workspace(
            destroying.id,
            reason="rerun while deleting",
        )

    assert completed_error.value.error_code == "WORKSPACE_STATE_NOT_VALIDATABLE"
    assert completed_error.value.detail == {
        "status": WorkspaceStatus.completed.value,
        "eligible_statuses": [WorkspaceStatus.monitoring_pr.value],
    }
    assert destroying_error.value.detail == {
        "status": WorkspaceStatus.destroying.value,
        "eligible_statuses": [WorkspaceStatus.monitoring_pr.value],
    }
    assert await _operations(session, completed.id) == []
    assert await _operations(session, destroying.id) == []


@pytest.mark.unit
async def test_destroy_rejects_active_workspace_without_force_before_cleanup(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.running)
    cleaner = RecordingCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    with pytest.raises(ActiveWorkspaceDestroyError) as exc_info:
        await service.destroy_workspace(
            workspace.id,
            force=False,
            remove_volumes=True,
            remove_worktree=True,
        )

    assert exc_info.value.error_code == "WORKSPACE_ACTIVE"
    assert cleaner.calls == []
    assert await _operations(session, workspace.id) == []


@pytest.mark.unit
async def test_destroy_already_cancelled_workspace_runs_cleanup_and_records_destroy_contract(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.cancelled)
    cleaner = RecordingCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=False,
        idempotency_key="destroy-cancelled",
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    state_events = [event for event in events if event.event_type == "workspace.state_changed"]

    assert response.operation_id == operations[0].id
    assert response.status == WorkspaceStatus.destroyed
    assert response.message == "workspace destroyed"
    assert workspace.status == WorkspaceStatus.destroyed.value
    assert len(cleaner.calls) == 1
    assert cleaner.calls[0] == CleanupCall(
        workspace_id=workspace.id,
        repo_url=workspace.repo_url,
        compose_project_name=workspace.compose_project_name,
        compose_file_path=Path(workspace.compose_file_path),
        worktree_host_path=None,
        remove_volumes=True,
        remove_worktree=False,
    )
    assert operations[0].type == OperationType.destroy.value
    assert operations[0].status == OperationStatus.succeeded.value
    assert operations[0].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": None,
        "reason_code": "OPERATOR_DESTROY",
        "requested_action": "destroy",
        "force": False,
        "remove_volumes": True,
        "remove_worktree": False,
    }
    assert operations[0].result == {
        "status": WorkspaceStatus.destroyed.value,
        "cleanup": {
            "status": "succeeded",
            "reason_code": "CLEANUP_SUCCEEDED",
            "steps": [],
            "failed_steps": [],
            "completed_steps": [],
        },
    }
    assert [event.new_state for event in state_events] == [
        WorkspaceStatus.destroyed.value,
        WorkspaceStatus.destroying.value,
    ]
    assert state_events[1].old_state == WorkspaceStatus.cancelled.value
    assert state_events[1].payload == {
        "force": False,
        "remove_volumes": True,
        "remove_worktree": False,
    }
    assert state_events[0].old_state == WorkspaceStatus.destroying.value
    assert state_events[0].payload is not None
    assert state_events[0].payload["cleanup"] == operations[0].result["cleanup"]
    assert not any(event.event_type == "workspace.stale_callback_ignored" for event in events)


@pytest.mark.unit
async def test_force_destroy_active_workspace_runs_cleanup_and_marks_destroyed(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    cleaner = RecordingCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=True,
        remove_volumes=False,
        remove_worktree=True,
        idempotency_key="destroy-active",
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert response.status == WorkspaceStatus.destroyed
    assert response.message == "workspace destroyed"
    assert workspace.status == WorkspaceStatus.destroyed.value
    assert len(cleaner.calls) == 1
    assert cleaner.calls[0] == CleanupCall(
        workspace_id=workspace.id,
        repo_url=workspace.repo_url,
        compose_project_name=workspace.compose_project_name,
        compose_file_path=Path(workspace.compose_file_path),
        worktree_host_path=None,
        remove_volumes=False,
        remove_worktree=True,
    )
    assert operations[0].status == "succeeded"
    assert operations[0].result == {
        "status": WorkspaceStatus.destroyed.value,
        "cleanup": {
            "status": "succeeded",
            "reason_code": "CLEANUP_SUCCEEDED",
            "steps": [],
            "failed_steps": [],
            "completed_steps": [],
        },
    }
    state_events = [event for event in events if event.event_type == "workspace.state_changed"]
    assert [event.new_state for event in state_events[:3]] == [
        WorkspaceStatus.destroyed.value,
        WorkspaceStatus.destroying.value,
        WorkspaceStatus.cancelled.value,
    ]
    assert state_events[0].payload is not None
    assert state_events[0].payload["cleanup"] == operations[0].result["cleanup"]
    assert len(audit_events) == 1
    assert audit_events[0].payload == {
        "schema": "control_audit.v1",
        "actor": "operator_api",
        "source": "operator_api",
        "action": "destroy",
        "outcome": "succeeded",
        "reason_code": "OPERATOR_DESTROY",
        "operation_id": operations[0].id,
        "operation_type": "destroy",
        "force": True,
        "remove_volumes": False,
        "remove_worktree": True,
        "evidence": {"cleanup": operations[0].result["cleanup"]},
    }


@pytest.mark.unit
async def test_destroy_workspace_revokes_active_secret_leases_before_cleanup(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    await _issue_control_secret_lease(session, workspace)
    cleanup_seen_statuses: list[list[str]] = []

    class LeaseCheckingCleaner(RecordingCleaner):
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
        ) -> list[str]:
            leases = await SecretLeaseRepository(session).list_for_workspace(workspace_id)
            cleanup_seen_statuses.append([lease.status for lease in leases])
            return await super().cleanup(
                workspace_id=workspace_id,
                repo_url=repo_url,
                compose_project_name=compose_project_name,
                compose_file_path=compose_file_path,
                worktree_host_path=worktree_host_path,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
            )

    cleaner = LeaseCheckingCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=True,
        remove_volumes=True,
        remove_worktree=True,
        idempotency_key="destroy-with-secret-lease",
    )
    operations = await _operations(session, workspace.id)
    leases = await SecretLeaseRepository(session).list_for_workspace(workspace.id)
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert response.status == WorkspaceStatus.destroyed
    assert cleanup_seen_statuses == [["revoked"]]
    assert leases[0].status == "revoked"
    assert leases[0].revoke_reason_code == "TERMINAL_CLEANUP"
    assert operations[0].result is not None
    assert operations[0].result["secret_leases"] == {
        "revoked_count": 1,
        "reason_code": "TERMINAL_CLEANUP",
    }
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["evidence"]["lease_revocations"] == {
        "revoked_count": 1,
        "reason_code": "TERMINAL_CLEANUP",
    }


@pytest.mark.unit
async def test_destroy_workspace_replay_keeps_secret_lease_revocation_idempotent(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    await _issue_control_secret_lease(session, workspace)
    service, _stopper, _cleaner = _service(session)

    first = await service.destroy_workspace(
        workspace.id,
        force=True,
        remove_volumes=True,
        remove_worktree=True,
        idempotency_key="destroy-secret-replay",
    )
    replay = await service.destroy_workspace(
        workspace.id,
        force=True,
        remove_volumes=True,
        remove_worktree=True,
        idempotency_key="destroy-secret-replay",
    )
    leases = await SecretLeaseRepository(session).list_for_workspace(workspace.id)
    events = await _events(session, workspace.id)

    assert replay.operation_id == first.operation_id
    assert leases[0].status == "revoked"
    assert leases[0].revoke_reason_code == "TERMINAL_CLEANUP"
    assert [event.reason_code for event in events].count("SECRET_LEASE_REVOKED") == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "final_status",
    [
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroyed,
        WorkspaceStatus.completed,
        WorkspaceStatus.failed,
    ],
)
async def test_destroy_cleanup_callback_does_not_override_terminal_state(
    session: AsyncSession,
    final_status: WorkspaceStatus,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.completed)
    cleaner = StaleCallbackCleaner(session=session, final_status=final_status)
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)

    assert response.status == final_status
    assert response.message == "workspace destroy callback ignored"
    assert workspace.status == final_status.value
    if final_status == WorkspaceStatus.failed:
        assert workspace.failure_reason == "operator_failure"
        assert workspace.failure_message == "operator moved workspace"
    assert operations[0].status == OperationStatus.cancelled.value
    assert operations[0].result == {
        "status": final_status.value,
        "cleanup": {
            "status": "succeeded",
            "reason_code": "CLEANUP_SUCCEEDED",
            "steps": [],
            "failed_steps": [],
            "completed_steps": [],
        },
        "ignored_callback": {
            "reason_code": "STALE_CALLBACK_IGNORED",
            "callback_source": "service.controls",
            "callback_action": "destroy_cleanup",
            "expected_status": WorkspaceStatus.destroying.value,
            "actual_status": final_status.value,
            "requested_status": WorkspaceStatus.destroyed.value,
            "operation_id": operations[0].id,
        },
    }
    ignored_events = [
        event for event in events if event.event_type == "workspace.stale_callback_ignored"
    ]
    assert ignored_events[-1].payload == operations[0].result["ignored_callback"]


@pytest.mark.unit
async def test_destroy_already_destroyed_workspace_succeeds_without_cleanup_and_replays(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.destroyed)
    cleaner = RecordingCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=False,
        idempotency_key="destroyed-replay",
    )
    replay = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=False,
        idempotency_key="destroyed-replay",
    )
    operations = await _operations(session, workspace.id)
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert response.operation_id == replay.operation_id
    assert response.message == "workspace already destroyed"
    assert replay.message == "workspace already destroyed"
    assert response.status == WorkspaceStatus.destroyed
    assert cleaner.calls == []
    assert [operation.type for operation in operations] == [OperationType.destroy.value]
    assert operations[0].result == {
        "status": WorkspaceStatus.destroyed.value,
        "cleanup": {
            "status": "skipped",
            "reason_code": "WORKSPACE_ALREADY_DESTROYED",
            "steps": [],
            "failed_steps": [],
            "completed_steps": [],
        },
    }
    assert len(audit_events) == 1
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["action"] == "destroy"
    assert audit_events[0].payload["outcome"] == "skipped"
    assert audit_events[0].payload["operation_id"] == operations[0].id
    assert audit_events[0].payload["evidence"]["cleanup"] == operations[0].result["cleanup"]


@pytest.mark.unit
async def test_destroy_cleanup_failures_mark_operation_failed_and_workspace_failed(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    cleaner = RecordingCleaner(failures=["compose down failed", "worktree removal failed"])
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
    )
    operations = await _operations(session, workspace.id)
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert response.status == WorkspaceStatus.failed
    assert response.message == "workspace cleanup failed"
    assert workspace.status == WorkspaceStatus.failed.value
    assert workspace.failure_reason == "cleanup_failure"
    assert workspace.failure_message == "compose down failed, worktree removal failed"
    assert len(cleaner.calls) == 1
    assert operations[0].status == "failed"
    assert operations[0].error_code == "CLEANUP_FAILED"
    assert operations[0].error_message == "compose down failed, worktree removal failed"
    assert operations[0].result == {
        "status": WorkspaceStatus.failed.value,
        "cleanup": {
            "status": "partial",
            "reason_code": "CLEANUP_PARTIAL",
            "steps": [
                {
                    "name": "compose down failed",
                    "status": "failed",
                    "reason_code": "CLEANUP_STEP_FAILED",
                    "error": "compose down failed",
                },
                {
                    "name": "worktree removal failed",
                    "status": "failed",
                    "reason_code": "CLEANUP_STEP_FAILED",
                    "error": "worktree removal failed",
                },
            ],
            "failed_steps": [
                {
                    "name": "compose down failed",
                    "status": "failed",
                    "reason_code": "CLEANUP_STEP_FAILED",
                    "error": "compose down failed",
                },
                {
                    "name": "worktree removal failed",
                    "status": "failed",
                    "reason_code": "CLEANUP_STEP_FAILED",
                    "error": "worktree removal failed",
                },
            ],
            "completed_steps": [],
        },
    }
    assert len(audit_events) == 1
    assert audit_events[0].reason_code == "CLEANUP_FAILED"
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["action"] == "destroy"
    assert audit_events[0].payload["outcome"] == "failed"
    assert audit_events[0].payload["operation_id"] == operations[0].id
    assert audit_events[0].payload["evidence"]["cleanup"] == operations[0].result["cleanup"]
    assert (
        audit_events[0].payload["evidence"]["error_message"]
        == "compose down failed, worktree removal failed"
    )


@pytest.mark.unit
async def test_destroy_cleanup_failure_message_is_bounded(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    cleanup_failure = "cleanup failed: " + ("x" * _OPERATION_ERROR_MESSAGE_MAX_LENGTH)
    cleaner = RecordingCleaner(failures=[cleanup_failure])
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
    )
    operations = await _operations(session, workspace.id)

    expected = cleanup_failure[:_OPERATION_ERROR_MESSAGE_MAX_LENGTH]
    assert workspace.failure_message == expected
    assert operations[0].error_message == expected


@pytest.mark.unit
async def test_destroy_replay_uses_in_progress_message_for_non_destroyed_workspace(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    payload = {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": None,
        "reason_code": "OPERATOR_DESTROY",
        "requested_action": "destroy",
        "force": False,
        "remove_volumes": True,
        "remove_worktree": True,
    }
    operation = await OperationRepository(session).create(
        workspace_id=workspace.id,
        operation_type=OperationType.destroy,
        status="running",
        payload=payload,
        idempotency_key="destroy-in-progress",
    )
    service, _stopper, cleaner = _service(session)

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
        idempotency_key="destroy-in-progress",
    )

    assert response.operation_id == operation.id
    assert response.message == "workspace destroy requested"
    assert response.status == WorkspaceStatus.failed
    assert cleaner.calls == []


@pytest.mark.unit
async def test_destroy_destroying_workspace_runs_cleanup_without_retransition(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.destroying)
    cleaner = RecordingCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
    )
    events = await _events(session, workspace.id)

    assert response.status == WorkspaceStatus.destroyed
    assert workspace.status == WorkspaceStatus.destroyed.value
    assert len(cleaner.calls) == 1
    state_change_events = [
        event for event in events if event.event_type == "workspace.state_changed"
    ]
    assert [event.new_state for event in state_change_events] == [WorkspaceStatus.destroyed.value]


@pytest.mark.unit
async def test_default_stack_helpers_handle_noop_and_construct_cleaner() -> None:
    await stop_project_containers(None)

    cleaner = default_cleaner()

    assert hasattr(cleaner, "cleanup")
    assert _json_datetime(None) is None
