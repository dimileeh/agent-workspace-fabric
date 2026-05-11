"""Terminal runtime release that preserves workspace salvage evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TypeVar, cast
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.audit import redact_audit_text, redact_audit_value
from awf.common.logging import get_logger
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.node.cleanup import (
    WorkspaceCleanupResult,
    WorkspaceCleanupStepResult,
)

_log = get_logger(__name__)

TERMINAL_RUNTIME_RELEASED_EVENT_TYPE = "workspace.terminal_runtime_released"
TERMINAL_RUNTIME_RELEASE_FAILED_EVENT_TYPE = "workspace.terminal_runtime_release_failed"
TERMINAL_RUNTIME_RELEASED_REASON_CODE = "TERMINAL_RUNTIME_RELEASED"
TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE = "TERMINAL_RUNTIME_RELEASE_FAILED"
TERMINAL_RUNTIME_RELEASE_SKIPPED_REASON_CODE = "TERMINAL_RUNTIME_RELEASE_SKIPPED"
TERMINAL_RUNTIME_RELEASE_CLAIM_DENIED_REASON_CODE = "TERMINAL_RUNTIME_RELEASE_CLAIM_DENIED"
TERMINAL_RUNTIME_RELEASE_EXCEPTION_REASON_CODE = "TERMINAL_RUNTIME_RELEASE_EXCEPTION"
TERMINAL_RUNTIME_RELEASE_CLAIM_LOST_REASON_CODE = "TERMINAL_RUNTIME_RELEASE_CLAIM_LOST"
TERMINAL_RUNTIME_RELEASE_CLAIM_REFRESH_FAILED_REASON_CODE = (
    "TERMINAL_RUNTIME_RELEASE_CLAIM_REFRESH_FAILED"
)
TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX = "terminal-runtime-release:"
TERMINAL_RUNTIME_RELEASE_CLAIM_TTL_SECONDS = 15 * 60

TERMINAL_WORKSPACE_STATUSES = frozenset(
    {
        WorkspaceStatus.completed.value,
        WorkspaceStatus.failed.value,
        WorkspaceStatus.cancelled.value,
        WorkspaceStatus.destroyed.value,
    }
)
_EXPLICIT_NON_TERMINAL_RELEASE_STATUSES = frozenset({WorkspaceStatus.provisioning.value})


_T = TypeVar("_T")


class TerminalRuntimeCleaner(Protocol):
    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = False,
        remove_worktree: bool = False,
    ) -> WorkspaceCleanupResult: ...  # pragma: no cover - Protocol declaration only.


class TerminalRuntimeCleanerQuiescence(Protocol):
    async def wait_for_cleanup_quiescence(
        self,
    ) -> None: ...  # pragma: no cover - Protocol declaration only.


class TerminalRuntimeReleaserProtocol(Protocol):
    async def release(
        self,
        workspace_id: str,
        *,
        source: str,
        expected_status: WorkspaceStatus | None = None,
    ) -> TerminalRuntimeReleaseResult: ...  # pragma: no cover - Protocol declaration only.


@dataclass(frozen=True)
class TerminalRuntimeReleaseResult:
    workspace_id: str
    status: str
    reason_code: str
    cleanup: WorkspaceCleanupResult | None = None

    @property
    def ok(self) -> bool:
        cleanup_ok = self.cleanup is None or self.cleanup.ok
        if not cleanup_ok:
            return False
        if self.status == "released":
            return True
        return (
            self.status == "skipped"
            and self.reason_code == TERMINAL_RUNTIME_RELEASE_SKIPPED_REASON_CODE
        )


@dataclass(frozen=True)
class _TerminalWorkspaceSnapshot:
    workspace_id: str
    status: str
    repo_url: str
    branch_name: str | None
    remote_push_branch: str | None
    pr_url: str | None
    pr_number: int | None
    failure_reason: str | None
    failure_message: str | None
    compose_project_name: str | None
    compose_file_path: str | None
    worktree_host_path: Path | None


@dataclass(frozen=True)
class _TerminalRuntimeReleaseClaim:
    snapshot: _TerminalWorkspaceSnapshot
    owner_id: str


@dataclass(frozen=True)
class _TerminalRuntimeReleaseClaimFailure:
    reason_code: str
    error: str | None = None


class TerminalRuntimeReleaser:
    """Stop terminal workspace runtime resources without deleting salvage data."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        cleaner_factory: Callable[[], TerminalRuntimeCleaner],
        worktrees_root: Path | None = None,
        claim_refresh_interval_seconds: float | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._cleaner_factory = cleaner_factory
        self._worktrees_root = worktrees_root
        claim_refresh_interval = (
            claim_refresh_interval_seconds
            if claim_refresh_interval_seconds is not None
            else max(1.0, min(60.0, TERMINAL_RUNTIME_RELEASE_CLAIM_TTL_SECONDS / 3))
        )
        self._claim_refresh_interval_seconds = max(0.001, claim_refresh_interval)

    async def release(
        self,
        workspace_id: str,
        *,
        source: str,
        expected_status: WorkspaceStatus | None = None,
    ) -> TerminalRuntimeReleaseResult:
        allow_non_terminal_event = _allows_non_terminal_release(expected_status)
        snapshot = await self._snapshot(workspace_id, expected_status=expected_status)
        if snapshot is None:
            return TerminalRuntimeReleaseResult(
                workspace_id=workspace_id,
                status="skipped",
                reason_code=TERMINAL_RUNTIME_RELEASE_SKIPPED_REASON_CODE,
            )

        claim = await self._claim_locked_snapshot(
            workspace_id,
            expected_status=expected_status,
            worktree_host_path=snapshot.worktree_host_path,
        )
        if claim is None:
            reason_code = (
                TERMINAL_RUNTIME_RELEASE_CLAIM_DENIED_REASON_CODE
                if await self._terminal_status_still_matches(
                    workspace_id,
                    expected_status=expected_status,
                )
                else TERMINAL_RUNTIME_RELEASE_SKIPPED_REASON_CODE
            )
            return TerminalRuntimeReleaseResult(
                workspace_id=workspace_id,
                status="skipped",
                reason_code=reason_code,
            )
        snapshot = claim.snapshot

        claim_refresh_task = asyncio.create_task(
            self._refresh_terminal_runtime_claim_loop(
                workspace_id,
                owner_id=claim.owner_id,
            ),
            name=f"awf-terminal-runtime-release-claim-{workspace_id}",
        )
        cleanup_task: asyncio.Task[WorkspaceCleanupResult] | None = None
        cleaner: TerminalRuntimeCleaner | None = None
        try:
            cleaner = self._cleaner_factory()
            cleanup_task = asyncio.create_task(
                self._cleanup_snapshot(cleaner, snapshot),
                name=f"awf-terminal-runtime-release-cleanup-{workspace_id}",
            )
            cleanup, claim_failure = await self._await_release_step_or_claim_failure(
                cleanup_task,
                claim_refresh_task,
            )
            if claim_failure is not None:
                if cleanup is not None:
                    await self._record_release_event_safely(
                        workspace_id,
                        cleanup=cleanup,
                        source=source,
                        worktree_host_path=snapshot.worktree_host_path,
                        allow_non_terminal=allow_non_terminal_event,
                    )
                return _terminal_runtime_release_claim_failure_result(
                    workspace_id,
                    claim_failure,
                    cleanup=cleanup,
                )
            assert cleanup is not None

            claim_failure = await self._refresh_terminal_runtime_claim_or_failure(
                workspace_id,
                owner_id=claim.owner_id,
            )
            if claim_failure is not None:
                await self._record_release_event_safely(
                    workspace_id,
                    cleanup=cleanup,
                    source=source,
                    worktree_host_path=snapshot.worktree_host_path,
                    allow_non_terminal=allow_non_terminal_event,
                )
                return _terminal_runtime_release_post_cleanup_claim_failure_result(
                    workspace_id,
                    claim_failure,
                    cleanup=cleanup,
                )

            await self._record_release_event_safely(
                workspace_id,
                cleanup=cleanup,
                source=source,
                worktree_host_path=snapshot.worktree_host_path,
                allow_non_terminal=allow_non_terminal_event,
            )

            return TerminalRuntimeReleaseResult(
                workspace_id=workspace_id,
                status="released" if cleanup.ok else "failed",
                reason_code=(
                    TERMINAL_RUNTIME_RELEASED_REASON_CODE
                    if cleanup.ok
                    else TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE
                ),
                cleanup=cleanup,
            )
        finally:
            if cleanup_task is not None and not cleanup_task.done():
                cleanup_task.cancel()
            if cleanup_task is not None:
                with suppress(asyncio.CancelledError):
                    await cleanup_task
            quiescence_error: BaseException | None = None
            if cleaner is not None:
                try:
                    await _wait_for_terminal_runtime_cleaner_quiescence(cleaner)
                except BaseException as exc:
                    quiescence_error = exc
            claim_refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await claim_refresh_task
            if quiescence_error is None:
                await self._release_terminal_runtime_claim(
                    workspace_id,
                    owner_id=claim.owner_id,
                )
            else:
                raise quiescence_error

    async def _await_release_step_or_claim_failure(
        self,
        step_task: asyncio.Task[_T],
        claim_refresh_task: asyncio.Task[_TerminalRuntimeReleaseClaimFailure],
    ) -> tuple[_T | None, _TerminalRuntimeReleaseClaimFailure | None]:
        done, _pending = await asyncio.wait(
            {step_task, claim_refresh_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if step_task in done:
            if claim_refresh_task in done:
                return step_task.result(), claim_refresh_task.result()
            return step_task.result(), None

        claim_failure = claim_refresh_task.result()
        step_task.cancel()
        with suppress(asyncio.CancelledError):
            await step_task
        return None, claim_failure

    async def _record_release_event_safely(
        self,
        workspace_id: str,
        *,
        cleanup: WorkspaceCleanupResult,
        source: str,
        worktree_host_path: Path | None,
        allow_non_terminal: bool = False,
    ) -> None:
        try:
            async with self._session_factory() as session:
                try:
                    await record_terminal_runtime_release_event(
                        session,
                        workspace_id=workspace_id,
                        cleanup=cleanup,
                        source=source,
                        worktree_host_path=worktree_host_path,
                        allow_non_terminal=allow_non_terminal,
                    )
                    await session.commit()
                except Exception:
                    with suppress(Exception):
                        await session.rollback()
                    raise
        except Exception as exc:  # pragma: no cover - defensive; cleanup already landed.
            _log.warning(
                "terminal_runtime.release_event_record_failed",
                workspace_id=workspace_id,
                source=source,
                error=redact_audit_text(repr(exc), limit=400),
            )

    async def _cleanup_snapshot(
        self,
        cleaner: TerminalRuntimeCleaner,
        snapshot: _TerminalWorkspaceSnapshot,
    ) -> WorkspaceCleanupResult:
        try:
            return await cleaner.cleanup(
                workspace_id=snapshot.workspace_id,
                repo_url=snapshot.repo_url,
                compose_project_name=snapshot.compose_project_name,
                compose_file_path=(
                    Path(snapshot.compose_file_path) if snapshot.compose_file_path else None
                ),
                worktree_host_path=snapshot.worktree_host_path,
                remove_volumes=False,
                remove_worktree=False,
            )
        except Exception as exc:
            return WorkspaceCleanupResult(
                status="partial",
                reason_code=TERMINAL_RUNTIME_RELEASE_EXCEPTION_REASON_CODE,
                steps=(
                    WorkspaceCleanupStepResult(
                        name="terminal_runtime_release",
                        status="failed",
                        reason_code=TERMINAL_RUNTIME_RELEASE_EXCEPTION_REASON_CODE,
                        error=redact_audit_text(
                            f"{type(exc).__name__}: {exc}",
                            limit=1000,
                        ),
                    ),
                ),
            )

    async def _snapshot(
        self,
        workspace_id: str,
        *,
        expected_status: WorkspaceStatus | None,
    ) -> _TerminalWorkspaceSnapshot | None:
        async with self._session_factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            if workspace is None:
                return None
            if not _matches_release_status(workspace.status, expected_status):
                return None
            return _snapshot_for_workspace(
                workspace,
                worktree_host_path=await _resolve_worktree_host_path(
                    workspace.id,
                    worktrees_root=self._worktrees_root,
                ),
            )

    async def _terminal_status_still_matches(
        self,
        workspace_id: str,
        *,
        expected_status: WorkspaceStatus | None,
    ) -> bool:
        async with self._session_factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            return workspace is not None and _matches_release_status(
                workspace.status,
                expected_status,
            )

    async def _claim_locked_snapshot(
        self,
        workspace_id: str,
        *,
        expected_status: WorkspaceStatus | None,
        worktree_host_path: Path | None,
    ) -> _TerminalRuntimeReleaseClaim | None:
        owner_id = f"{TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX}{uuid4().hex}"
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.claim_execution_if_available(
                workspace_id,
                owner_id=owner_id,
                lease_expires_at=self._terminal_runtime_claim_expires_at(),
                statuses=_release_status_values(expected_status),
                block_active_teardown_operation=True,
            )
            if workspace is None:
                return None
            locked_worktree_host_path = worktree_host_path
            if locked_worktree_host_path is None:
                locked_worktree_host_path = await _resolve_worktree_host_path(
                    workspace.id,
                    worktrees_root=self._worktrees_root,
                )
            snapshot = _snapshot_for_workspace(
                workspace,
                worktree_host_path=locked_worktree_host_path,
            )
            await session.commit()
            return _TerminalRuntimeReleaseClaim(snapshot=snapshot, owner_id=owner_id)

    async def _release_terminal_runtime_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str,
    ) -> None:
        try:
            async with self._session_factory() as session:
                await WorkspaceRepository(session).release_execution_claim(
                    workspace_id,
                    owner_id=owner_id,
                )
                await session.commit()
        except Exception as exc:  # pragma: no cover - defensive cleanup path.
            _log.warning(
                "terminal_runtime.release_claim_clear_failed",
                workspace_id=workspace_id,
                error=redact_audit_text(repr(exc), limit=400),
            )

    async def _refresh_terminal_runtime_claim_loop(
        self,
        workspace_id: str,
        *,
        owner_id: str,
    ) -> _TerminalRuntimeReleaseClaimFailure:
        loop = asyncio.get_running_loop()
        claim_timeout_seconds = max(float(TERMINAL_RUNTIME_RELEASE_CLAIM_TTL_SECONDS), 0.001)
        last_claim_renewed_at = loop.time()
        last_safe_exception: str | None = None
        while True:
            await asyncio.sleep(self._claim_refresh_interval_seconds)
            try:
                refreshed = await self._refresh_terminal_runtime_claim(
                    workspace_id,
                    owner_id=owner_id,
                )
            except Exception as exc:
                self._observe_terminal_runtime_claim_refresh_attempt(
                    workspace_id,
                    owner_id=owner_id,
                    refreshed=None,
                )
                last_safe_exception = redact_audit_text(
                    f"{type(exc).__name__}: {exc}",
                    limit=1000,
                )
                elapsed_since_claim_renewal = loop.time() - last_claim_renewed_at
                _log.warning(
                    "terminal_runtime.release_claim_refresh_failed",
                    workspace_id=workspace_id,
                    error=redact_audit_text(repr(exc), limit=400),
                    elapsed_since_claim_renewal_seconds=round(
                        elapsed_since_claim_renewal,
                        3,
                    ),
                )
                if elapsed_since_claim_renewal >= claim_timeout_seconds:
                    _log.warning(
                        "terminal_runtime.release_claim_refresh_abandoned",
                        workspace_id=workspace_id,
                        elapsed_since_claim_renewal_seconds=round(
                            elapsed_since_claim_renewal,
                            3,
                        ),
                        claim_timeout_seconds=round(claim_timeout_seconds, 3),
                    )
                    return _TerminalRuntimeReleaseClaimFailure(
                        reason_code=TERMINAL_RUNTIME_RELEASE_CLAIM_REFRESH_FAILED_REASON_CODE,
                        error=last_safe_exception,
                    )
                continue

            self._observe_terminal_runtime_claim_refresh_attempt(
                workspace_id,
                owner_id=owner_id,
                refreshed=refreshed,
            )
            if not refreshed:
                _log.warning(
                    "terminal_runtime.release_claim_lost",
                    workspace_id=workspace_id,
                )
                return _TerminalRuntimeReleaseClaimFailure(
                    reason_code=TERMINAL_RUNTIME_RELEASE_CLAIM_LOST_REASON_CODE,
                )
            last_claim_renewed_at = loop.time()

    async def _refresh_terminal_runtime_claim_or_failure(
        self,
        workspace_id: str,
        *,
        owner_id: str,
    ) -> _TerminalRuntimeReleaseClaimFailure | None:
        try:
            refreshed = await self._refresh_terminal_runtime_claim(
                workspace_id,
                owner_id=owner_id,
            )
        except Exception as exc:
            self._observe_terminal_runtime_claim_refresh_attempt(
                workspace_id,
                owner_id=owner_id,
                refreshed=None,
            )
            _log.warning(
                "terminal_runtime.release_claim_refresh_failed",
                workspace_id=workspace_id,
                error=redact_audit_text(repr(exc), limit=400),
            )
            return _TerminalRuntimeReleaseClaimFailure(
                reason_code=TERMINAL_RUNTIME_RELEASE_CLAIM_REFRESH_FAILED_REASON_CODE,
                error=redact_audit_text(
                    f"{type(exc).__name__}: {exc}",
                    limit=1000,
                ),
            )
        self._observe_terminal_runtime_claim_refresh_attempt(
            workspace_id,
            owner_id=owner_id,
            refreshed=refreshed,
        )
        if not refreshed:
            _log.warning(
                "terminal_runtime.release_claim_lost",
                workspace_id=workspace_id,
            )
            return _TerminalRuntimeReleaseClaimFailure(
                reason_code=TERMINAL_RUNTIME_RELEASE_CLAIM_LOST_REASON_CODE,
            )
        return None

    def _observe_terminal_runtime_claim_refresh_attempt(
        self,
        workspace_id: str,
        *,
        owner_id: str,
        refreshed: bool | None,
    ) -> None:
        del workspace_id, owner_id, refreshed

    async def _refresh_terminal_runtime_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str,
    ) -> bool:
        async with self._session_factory() as session:
            refreshed = await WorkspaceRepository(session).refresh_execution_claim(
                workspace_id,
                owner_id=owner_id,
                lease_expires_at=self._terminal_runtime_claim_expires_at(),
            )
            await session.commit()
            return refreshed

    def _terminal_runtime_claim_expires_at(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=TERMINAL_RUNTIME_RELEASE_CLAIM_TTL_SECONDS)


async def _wait_for_terminal_runtime_cleaner_quiescence(
    cleaner: TerminalRuntimeCleaner,
) -> None:
    if getattr(cleaner, "wait_for_cleanup_quiescence", None) is None:
        return
    await cast(TerminalRuntimeCleanerQuiescence, cleaner).wait_for_cleanup_quiescence()


def _terminal_runtime_release_claim_failure_result(
    workspace_id: str,
    claim_failure: _TerminalRuntimeReleaseClaimFailure,
    *,
    cleanup: WorkspaceCleanupResult | None = None,
) -> TerminalRuntimeReleaseResult:
    claim_step = WorkspaceCleanupStepResult(
        name="terminal_runtime_release_claim",
        status="failed",
        reason_code=claim_failure.reason_code,
        error=claim_failure.error,
    )
    steps = (claim_step,) if cleanup is None else (*cleanup.steps, claim_step)
    return TerminalRuntimeReleaseResult(
        workspace_id=workspace_id,
        status="failed",
        reason_code=claim_failure.reason_code,
        cleanup=WorkspaceCleanupResult(
            status="partial",
            reason_code=claim_failure.reason_code,
            steps=steps,
        ),
    )


def _terminal_runtime_release_post_cleanup_claim_failure_result(
    workspace_id: str,
    claim_failure: _TerminalRuntimeReleaseClaimFailure,
    *,
    cleanup: WorkspaceCleanupResult,
) -> TerminalRuntimeReleaseResult:
    if cleanup.ok and claim_failure.reason_code == TERMINAL_RUNTIME_RELEASE_CLAIM_LOST_REASON_CODE:
        return TerminalRuntimeReleaseResult(
            workspace_id=workspace_id,
            status="released",
            reason_code=claim_failure.reason_code,
            cleanup=cleanup,
        )
    return _terminal_runtime_release_claim_failure_result(
        workspace_id,
        claim_failure,
        cleanup=cleanup,
    )


async def record_terminal_runtime_release_event(
    session: AsyncSession,
    *,
    workspace_id: str,
    cleanup: WorkspaceCleanupResult,
    source: str,
    worktree_host_path: Path | None = None,
    allow_non_terminal: bool = False,
) -> None:
    """Append release evidence for stopped workspace runtime resources."""

    repo = WorkspaceRepository(session)
    workspace = await repo.get(workspace_id)
    if workspace is None:
        return
    if not allow_non_terminal and workspace.status not in TERMINAL_WORKSPACE_STATUSES:
        return

    preserved_worktree_path = worktree_host_path
    payload = {
        "source": source,
        "workspace_status": workspace.status,
        "cleanup": _redacted_cleanup_payload(cleanup),
        "runtime": {
            key: value
            for key, value in {
                "compose_project_name": workspace.compose_project_name,
                "compose_file_path": workspace.compose_file_path,
                "remove_volumes": False,
                "remove_worktree": False,
            }.items()
            if value is not None
        },
        "preserved": {
            key: value
            for key, value in {
                "worktree_path": (
                    str(preserved_worktree_path) if preserved_worktree_path is not None else None
                ),
                "branch_name": workspace.branch_name,
                "remote_push_branch": workspace.remote_push_branch,
                "pr_url": workspace.pr_url,
                "pr_number": workspace.pr_number,
                "failure_reason": workspace.failure_reason,
                "failure_message": (
                    redact_audit_text(workspace.failure_message, limit=2048)
                    if workspace.failure_message is not None
                    else None
                ),
            }.items()
            if value is not None
        },
    }
    await repo.add_event(
        workspace,
        event_type=(
            TERMINAL_RUNTIME_RELEASED_EVENT_TYPE
            if cleanup.ok
            else TERMINAL_RUNTIME_RELEASE_FAILED_EVENT_TYPE
        ),
        reason_code=(
            TERMINAL_RUNTIME_RELEASED_REASON_CODE
            if cleanup.ok
            else TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE
        ),
        payload=payload,
    )


def _redacted_cleanup_payload(cleanup: WorkspaceCleanupResult) -> dict[str, object]:
    return cast(dict[str, object], redact_audit_value(cleanup.to_dict()))


def _snapshot_for_workspace(
    workspace: Workspace,
    *,
    worktree_host_path: Path | None,
) -> _TerminalWorkspaceSnapshot:
    return _TerminalWorkspaceSnapshot(
        workspace_id=workspace.id,
        status=workspace.status,
        repo_url=workspace.repo_url,
        branch_name=workspace.branch_name,
        remote_push_branch=workspace.remote_push_branch,
        pr_url=workspace.pr_url,
        pr_number=workspace.pr_number,
        failure_reason=workspace.failure_reason,
        failure_message=workspace.failure_message,
        compose_project_name=workspace.compose_project_name,
        compose_file_path=workspace.compose_file_path,
        worktree_host_path=worktree_host_path,
    )


async def _resolve_worktree_host_path(
    workspace_id: str,
    *,
    worktrees_root: Path | None,
) -> Path | None:
    if worktrees_root is None:
        return None
    candidate = worktrees_root / workspace_id
    return candidate if await asyncio.to_thread(candidate.exists) else None


def _matches_release_status(
    status: str,
    expected_status: WorkspaceStatus | None,
) -> bool:
    if expected_status is None:
        return status in TERMINAL_WORKSPACE_STATUSES
    if (
        expected_status.value not in TERMINAL_WORKSPACE_STATUSES
        and not _allows_non_terminal_release(expected_status)
    ):
        return False
    return status == expected_status.value


def _release_status_values(expected_status: WorkspaceStatus | None) -> tuple[str, ...]:
    if expected_status is None:
        return tuple(sorted(TERMINAL_WORKSPACE_STATUSES))
    if (
        expected_status.value not in TERMINAL_WORKSPACE_STATUSES
        and not _allows_non_terminal_release(expected_status)
    ):
        return ()
    return (expected_status.value,)


def _allows_non_terminal_release(expected_status: WorkspaceStatus | None) -> bool:
    return (
        expected_status is not None
        and expected_status.value in _EXPLICIT_NON_TERMINAL_RELEASE_STATUSES
    )


def terminal_runtime_release_claim_active(
    workspace: Workspace,
    *,
    now: datetime | None = None,
) -> bool:
    owner_id = workspace.execution_claimed_by
    expires_at = workspace.execution_claim_expires_at
    if owner_id is None or not owner_id.startswith(TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX):
        return False
    if expires_at is None:
        return False
    cutoff = now or datetime.now(UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > cutoff
