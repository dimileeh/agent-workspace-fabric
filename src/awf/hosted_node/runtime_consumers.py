"""Task-local authority around the existing worker's consumer operations."""

import asyncio
import inspect
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, nullcontext, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from sqlalchemy.exc import SQLAlchemyError

from awf.common.commands import (
    COMMAND_IDLE_TIMEOUT_REASON,
    COMMAND_TIMEOUT_REASON,
    ActivityProbe,
    AsyncStreamingCommandRunner,
    CommandResult,
    StreamCallback,
    mark_masked_command_reason_code,
)
from awf.common.compose_exec import DEFAULT_AGENT_WORKDIR
from awf.common.logging import get_logger
from awf.common.redaction import redact_exact_secret_bytes, redact_secrets
from awf.common.workspace_scope import skip_authority_grace_wait
from awf.hosted_node.identity import (
    MaterialDeniedError,
    MaterialLapsedError,
    WorkspaceAuthorityDeferredError,
    WorkspaceScopeDeniedError,
)
from awf.hosted_node.runtime_authority import WorkspaceAuthority
from awf.hosted_node.runtime_filesystem import share_checkout
from awf.hosted_node.runtime_views import owned_publication, track_publications
from awf.node.secret_mounts import LocalSecretLeaseResolution
from awf.profiles.models import WorkspaceProfile
from awf.runtime.inspection import inspection_environment
from awf.service.logs import _stream_redaction_flushable_length

_log = get_logger(__name__)
_TIMEOUT_REASONS = (COMMAND_TIMEOUT_REASON, COMMAND_IDLE_TIMEOUT_REASON)
_GraceAction = Literal["defer", "stop_exhausted", "stop_denied"]
_AUTHORITY_UNAVAILABLE = "HOSTED_AUTHORITY_UNAVAILABLE"
# One independent grace window per (workspace, checker). profile_mounts,
# resource_limits, and reconcile never cross-clear. The four checkers that
# call authority.workspace() form a cohort: success in any clears all four.
_CHECKER_SCOPE_ENTRY = "scope_entry"
_CHECKER_NESTED_ENTRY = "nested_entry"
_CHECKER_WATCHER = "watcher"
_CHECKER_COMMAND_PREPARE = "command_prepare"
_CHECKER_PROFILE_MOUNTS = "profile_mounts"
_CHECKER_RESOURCE_LIMITS = "resource_limits"
_CHECKER_RECONCILE = "reconcile"
_WORKSPACE_AUTHORITY_CHECKERS = frozenset(
    {
        _CHECKER_SCOPE_ENTRY,
        _CHECKER_NESTED_ENTRY,
        _CHECKER_WATCHER,
        _CHECKER_COMMAND_PREPARE,
    }
)
_SCOPE_BOUND_CHECKERS = frozenset(
    {
        *_WORKSPACE_AUTHORITY_CHECKERS,
        _CHECKER_PROFILE_MOUNTS,
        _CHECKER_RESOURCE_LIMITS,
    }
)


@dataclass
class _AuthorityGrace:
    started_at: datetime
    reason_code: str
    expires_at: datetime | None = None


@dataclass(frozen=True)
class _RetainedStop:
    """Pending teardown retry metadata after a failed Compose stop.

    Preserve exhaustion (or other) reason/message across retries so a later
    successful teardown does not default to ``HOSTED_RUNTIME_STOPPED``.
    """

    reason_code: str | None = None
    message: str | None = None


class _StopWorkspace(Protocol):
    def __call__(
        self,
        workspace_id: str,
        *,
        authority_lost: bool = True,
        reason_code: str | None = None,
        message: str | None = None,
    ) -> Awaitable[None]: ...


async def _no_stop(
    workspace_id: str,
    *,
    authority_lost: bool = True,
    reason_code: str | None = None,
    message: str | None = None,
) -> None:
    del workspace_id, authority_lost, reason_code, message


class WorkspaceConsumers:
    def __init__(self, authority: WorkspaceAuthority, work_dir: Path):
        self.authority = authority
        self.work_dir = work_dir
        self.workspace_id: ContextVar[str | None] = ContextVar("hosted_workspace", default=None)
        self.stop_workspace: _StopWorkspace = _no_stop
        self.poll_seconds = 2.0
        configured = getattr(
            getattr(getattr(authority, "receiver", None), "config", None),
            "hosted_authority_grace_seconds",
            600,
        )
        self.authority_grace_seconds = configured if isinstance(configured, int) else 600
        # Independent window per (workspace_id, checker). Nested dict keeps
        # ``workspace_id in _authority_grace`` meaning any checker is open.
        self._authority_grace: dict[str, dict[str, _AuthorityGrace]] = {}
        self._retained_cursor = ""
        self._history_cursor = ""
        self._retry_cursor = ""
        self._retained_stop_failures: dict[str, _RetainedStop] = {}
        self.base_environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "LANG": "C.UTF-8",
            "HOME": str(work_dir / "hosted-empty-home"),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "!gh auth git-credential",
            "GIT_CONFIG_KEY_1": "core.sharedRepository",
            "GIT_CONFIG_VALUE_1": "group",
        }
        if os.environ.get("DOCKER_HOST"):
            self.base_environment["DOCKER_HOST"] = os.environ["DOCKER_HOST"]

    def _clear_authority_grace(self, workspace_id: str, *, checker: str | None = None) -> None:
        """Clear one checker window, or every window for the workspace.

        ``checker=None`` drops all windows (denial / authority-lost stop). A
        named checker clears only that window; other checkers keep their own
        deadlines. Prefer ``_clear_authority_check_success`` after a successful
        poll so workspace-authority cohort clears stay correct.
        """
        windows = self._authority_grace.get(workspace_id)
        if windows is None:
            return
        if checker is not None:
            window = windows.pop(checker, None)
            if window is None:
                return
            if not windows:
                self._authority_grace.pop(workspace_id, None)
            _log.info(
                "hosted_node.authority_grace_cleared",
                workspace_id=workspace_id,
                checker=checker,
                reason_code=window.reason_code,
                expires_at=None if window.expires_at is None else window.expires_at.isoformat(),
            )
            return
        self._authority_grace.pop(workspace_id, None)
        for name, window in windows.items():
            _log.info(
                "hosted_node.authority_grace_cleared",
                workspace_id=workspace_id,
                checker=name,
                reason_code=window.reason_code,
                expires_at=None if window.expires_at is None else window.expires_at.isoformat(),
            )

    def _clear_authority_check_success(self, workspace_id: str, *, checker: str) -> None:
        """Clear grace after a successful authority poll for ``checker``.

        The four checkers that call ``authority.workspace()`` share one
        confirmation of workspace authority: success in any clears all four so
        a leftover deferred-finalizer watcher clock cannot exhaust on the next
        brief lapse. ``profile_mounts``, ``resource_limits``, and ``reconcile``
        stay independent.
        """
        if checker in _WORKSPACE_AUTHORITY_CHECKERS:
            for name in _WORKSPACE_AUTHORITY_CHECKERS:
                self._clear_authority_grace(workspace_id, checker=name)
            return
        self._clear_authority_grace(workspace_id, checker=checker)

    def _clear_scope_bound_grace(self, workspace_id: str) -> None:
        """Drop checkers that end when an outer scope exits without a stop."""
        windows = self._authority_grace.get(workspace_id)
        if windows is None:
            return
        for name in list(windows):
            if name in _SCOPE_BOUND_CHECKERS:
                self._clear_authority_grace(workspace_id, checker=name)

    def _classify_authority_failure(
        self, workspace_id: str, error: BaseException, *, checker: str
    ) -> _GraceAction:
        if isinstance(error, MaterialDeniedError) and not isinstance(error, MaterialLapsedError):
            self._authority_grace.pop(workspace_id, None)
            return "stop_denied"
        if not isinstance(error, (MaterialLapsedError, SQLAlchemyError, OSError, TimeoutError)):
            self._authority_grace.pop(workspace_id, None)
            return "stop_denied"
        if self.authority_grace_seconds <= 0:
            self._authority_grace.pop(workspace_id, None)
            return "stop_denied"
        now = datetime.now(UTC)
        material_expires_at = error.expires_at if isinstance(error, MaterialLapsedError) else None
        reason_code = (
            error.reason if isinstance(error, MaterialDeniedError) else type(error).__name__
        )
        windows = self._authority_grace.setdefault(workspace_id, {})
        window = windows.get(checker)
        if window is None:
            started_at = now
            if material_expires_at is not None and material_expires_at < started_at:
                started_at = material_expires_at
            window = _AuthorityGrace(started_at, reason_code, material_expires_at)
            windows[checker] = window
            _log.info(
                "hosted_node.authority_grace_started",
                workspace_id=workspace_id,
                checker=checker,
                reason_code=reason_code,
                expires_at=(
                    None if material_expires_at is None else material_expires_at.isoformat()
                ),
            )
        elif material_expires_at is not None and material_expires_at < window.started_at:
            # A later lapse must not keep a transient-started clock when the
            # material already expired earlier; otherwise failure-type switches
            # extend the permitted lapse interval.
            window.started_at = material_expires_at
            window.expires_at = material_expires_at
            window.reason_code = reason_code
        elapsed = (now - window.started_at).total_seconds()
        if elapsed >= self.authority_grace_seconds:
            # Keep the exhausted window until teardown succeeds or a later
            # successful authority check clears it. Dropping it here lets a
            # failed stop reopen a full grace period on the next transient.
            _log.info(
                "hosted_node.authority_grace_exhausted",
                workspace_id=workspace_id,
                checker=checker,
                reason_code=window.reason_code,
                expires_at=(None if window.expires_at is None else window.expires_at.isoformat()),
            )
            return "stop_exhausted"
        return "defer"

    def _authority_unavailable_message(self) -> str:
        return (
            f"Hosted authority could not be confirmed for {self.authority_grace_seconds} seconds."
        )

    async def _await_with_authority_grace[T](
        self,
        workspace_id: str,
        operation: Callable[[], Awaitable[T]],
        *,
        checker: str,
    ) -> T:
        """Wait out a grace-eligible lapse/transient, or raise after exhaustion.

        Used by nested same-workspace ``_scope`` re-entry, command prepare
        (``environment``), and stack-prepare callbacks (``profile_mounts`` /
        ``resource_limits``). Poll-inline recovery opts out via
        ``without_authority_grace_wait`` and gets
        ``WorkspaceAuthorityDeferredError`` instead of sleeping.

        ``checker`` names this wait's grace window. Success clears via
        ``_clear_authority_check_success`` (workspace-authority cohort or sole
        independent checker). Cancel and opt-out
        ``WorkspaceAuthorityDeferredError`` exit without success and without a
        stop, so they clear only this checker's window. Classified stops keep
        the window for exhaustion / teardown (cleared when the authority-lost
        stop completes).
        """
        holding_claim = False
        try:
            while True:
                try:
                    result = await operation()
                except (MaterialDeniedError, SQLAlchemyError, OSError, TimeoutError) as error:
                    action = self._classify_authority_failure(workspace_id, error, checker=checker)
                    if action == "defer":
                        holding_claim = True
                        if skip_authority_grace_wait.get():
                            # Convert to the same defer signal so callers requeue
                            # instead of sleeping. Re-raising the raw lapse/transient
                            # would reach terminal failure handlers.
                            raise WorkspaceAuthorityDeferredError(error) from error
                        await asyncio.sleep(self.poll_seconds)
                        continue
                    # Stop owns the window (exhausted retain / denied already
                    # cleared in classify). Do not release in finally.
                    holding_claim = False
                    # Stop with the classified reason before exposing the raw error.
                    # Command/stack-prepare run inside ``@workspace_consumer`` after
                    # scope entry, so "in scope" is the production path — not an
                    # out-of-scope edge. A reservation/mount denial may leave
                    # ``workspace()`` healthy, so the scope finalizer would not
                    # stop on its own check. Generic in-scope handlers can also
                    # mark the row failed first and skip a later finalizer stop as
                    # already-terminal (losing HOSTED_RUNTIME_STOPPED /
                    # HOSTED_AUTHORITY_UNAVAILABLE). ownership.stopped then makes
                    # the finalizer's later _stop a no-op (one stop per failure).
                    # Fence closing first: the watcher may still be polling, and
                    # association() does not treat failed as inactive.
                    ownership = owned_publication(self.authority.views, workspace_id)
                    if ownership is not None:
                        ownership.closing = True
                    await self._stop(
                        workspace_id,
                        reason_code=(
                            _AUTHORITY_UNAVAILABLE if action == "stop_exhausted" else None
                        ),
                        message=(
                            self._authority_unavailable_message()
                            if action == "stop_exhausted"
                            else None
                        ),
                    )
                    raise
                self._clear_authority_check_success(workspace_id, checker=checker)
                holding_claim = False
                return result
        finally:
            if holding_claim:
                self._clear_authority_grace(workspace_id, checker=checker)

    async def share_checkout(self, mirror: Path, worktree: Path) -> None:
        await self.environment()
        await asyncio.to_thread(
            share_checkout,
            self.work_dir / "git",
            mirror,
            worktree,
            self.authority.views.gid,
        )

    @asynccontextmanager
    async def scope(self, workspace_id: str) -> AsyncIterator[None]:
        ownership = (
            track_publications(self.authority.views, workspace_id)
            if self.workspace_id.get() is None
            else nullcontext()
        )
        with inspection_environment(self.base_environment), ownership:
            async with self._scope(workspace_id):
                yield

    @asynccontextmanager
    async def _scope(self, workspace_id: str) -> AsyncIterator[None]:
        existing = self.workspace_id.get()
        if existing is not None:
            if existing != workspace_id:
                raise MaterialDeniedError("WORKSPACE_SCOPE_MISMATCH", 403)
            # Nested same-workspace re-entry (e.g. monitor-resume outer consumer
            # then Provisioner.ensure_hosted_monitor_worktree) waits under its
            # own checker window. A raw authority.workspace() raise here
            # bypasses grace and can mark the workspace failed from a generic
            # caller handler.
            await self._await_with_authority_grace(
                workspace_id,
                lambda: self.authority.workspace(workspace_id),
                checker=_CHECKER_NESTED_ENTRY,
            )
            yield
            return
        ownership = owned_publication(self.authority.views, workspace_id)
        assert ownership is not None

        async def snapshot() -> None:
            ownership.publication = await self.authority.publication(workspace_id)

        entry = asyncio.create_task(snapshot())
        waiting_for_grace = False
        try:
            # Publication (association+materials) and the follow-up workspace
            # check share one classify/wait/stop path under scope_entry. A
            # denial or lapse usually surfaces on the entry snapshot first;
            # defer sleeps then starts a fresh snapshot so cancel cleanup
            # always joins the latest task. Poll-inline recovery opts out via
            # ContextVar.
            while True:
                try:
                    await asyncio.shield(entry)
                    await self.authority.workspace(workspace_id)
                    self._clear_authority_check_success(workspace_id, checker=_CHECKER_SCOPE_ENTRY)
                    break
                except (MaterialDeniedError, SQLAlchemyError, OSError, TimeoutError) as error:
                    soft = isinstance(error, MaterialDeniedError) and error.reason in {
                        "RECOVERY_ATTENTION_REQUIRED",
                        "NODE_ADMISSION_CLOSED",
                    }
                    action: _GraceAction | None = None
                    if not soft:
                        action = self._classify_authority_failure(
                            workspace_id, error, checker=_CHECKER_SCOPE_ENTRY
                        )
                        if action == "defer":
                            if skip_authority_grace_wait.get():
                                raise WorkspaceAuthorityDeferredError(error) from error
                            waiting_for_grace = True
                            await asyncio.sleep(self.poll_seconds)
                            entry = asyncio.create_task(snapshot())
                            continue
                        await self._stop(
                            workspace_id,
                            reason_code=(
                                _AUTHORITY_UNAVAILABLE if action == "stop_exhausted" else None
                            ),
                            message=(
                                self._authority_unavailable_message()
                                if action == "stop_exhausted"
                                else None
                            ),
                        )
                    if isinstance(error, MaterialDeniedError):
                        raise WorkspaceScopeDeniedError(error.reason, error.status) from error
                    raise
        except asyncio.CancelledError:
            # Entry can be cancelled before the watcher/finalizer exists. Do
            # not republish while deciding whether the old warm stack may stay.
            # A cancel during a grace wait must not turn a grace-eligible lapse
            # into an authority-lost stop.
            cleanup = asyncio.create_task(
                self._cancelled_start(
                    workspace_id,
                    entry,
                    preserve_on_grace=waiting_for_grace,
                ),
                name=f"hosted-initial-retract-{workspace_id}",
            )
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise
        token = self.workspace_id.set(workspace_id)
        parent = asyncio.current_task()
        assert parent is not None
        failure: Exception | None = None
        stop_reason: str | None = None
        stop_message: str | None = None
        cancelled = False
        caller_cancelled = False
        finished = asyncio.Event()

        async def watch() -> None:
            nonlocal failure, stop_reason, stop_message
            try:
                while True:
                    try:
                        await asyncio.wait_for(finished.wait(), timeout=self.poll_seconds)
                        return
                    except TimeoutError:
                        pass
                    try:
                        await self.authority.workspace(workspace_id)
                        self._clear_authority_check_success(workspace_id, checker=_CHECKER_WATCHER)
                    except (MaterialDeniedError, SQLAlchemyError, OSError, TimeoutError) as error:
                        action = self._classify_authority_failure(
                            workspace_id, error, checker=_CHECKER_WATCHER
                        )
                        if action == "defer":
                            continue
                        failure = error
                        if action == "stop_exhausted":
                            stop_reason = _AUTHORITY_UNAVAILABLE
                            stop_message = self._authority_unavailable_message()
                        parent.cancel()
                        return
            except Exception as error:
                failure = error
                parent.cancel()

        watcher = asyncio.create_task(watch(), name=f"hosted-authority-{workspace_id}")
        try:
            yield
        except asyncio.CancelledError:
            cancelled = True
            caller_cancelled = failure is None
            raise
        finally:
            finished.set()
            self.workspace_id.reset(token)
            ownership.closing = True
            late_cancellation: asyncio.CancelledError | None = None
            deferred_authority = False

            async def finish() -> None:
                nonlocal failure, stop_reason, stop_message, deferred_authority
                with suppress(asyncio.CancelledError):
                    await watcher
                try:
                    await self.authority.workspace(workspace_id)
                    self._clear_authority_check_success(workspace_id, checker=_CHECKER_WATCHER)
                except (MaterialDeniedError, SQLAlchemyError, OSError, TimeoutError) as error:
                    action = self._classify_authority_failure(
                        workspace_id, error, checker=_CHECKER_WATCHER
                    )
                    if action == "defer":
                        # Keep the watcher window: a clean-exit clear would
                        # reset TimeoutError/OSError/SQLAlchemyError clocks
                        # (no expires_at backdate) past hosted_authority_grace_seconds.
                        deferred_authority = True
                        return
                    if failure is None or isinstance(failure, MaterialDeniedError):
                        failure = error
                    if action == "stop_exhausted":
                        stop_reason = _AUTHORITY_UNAVAILABLE
                        stop_message = self._authority_unavailable_message()

            async def join(cleanup: asyncio.Task[None]) -> None:
                nonlocal cancelled, caller_cancelled, late_cancellation
                while True:
                    try:
                        await asyncio.shield(cleanup)
                        return
                    except asyncio.CancelledError as error:
                        if not cancelled:
                            cancelled = True
                            caller_cancelled = failure is None
                            late_cancellation = error
                        if cleanup.done():
                            cleanup.result()
                            return

            # Join the existing checks/teardown across repeated cancellation.
            # Decide cleanup in the caller: cancellation can arrive after the
            # check task finishes but before this caller wakes up.
            await join(asyncio.create_task(finish(), name=f"hosted-finalize-{workspace_id}"))
            if failure is not None or cancelled:
                await join(
                    asyncio.create_task(
                        self._stop(
                            workspace_id,
                            authority_lost=failure is not None,
                            reason_code=stop_reason if failure is not None else None,
                            message=stop_message if failure is not None else None,
                        ),
                        name=f"hosted-retract-{workspace_id}",
                    )
                )
            elif not deferred_authority:
                # Scope ended without a stop after confirmed authority: drop
                # checkers that end with it. Deferred finalizers keep the
                # watcher window for the in-progress grace clock.
                self._clear_scope_bound_grace(workspace_id)
            if failure is not None and not caller_cancelled:
                if isinstance(failure, MaterialDeniedError):
                    raise WorkspaceScopeDeniedError(failure.reason, failure.status) from failure
                raise failure
            if late_cancellation is not None:
                raise late_cancellation

    async def _cancelled_start(
        self,
        workspace_id: str,
        entry: asyncio.Task[None],
        *,
        preserve_on_grace: bool = False,
    ) -> None:
        authorized = False
        grace_defer = preserve_on_grace
        stop_reason: str | None = None
        stop_message: str | None = None
        try:
            # Join the entry snapshot without letting its failure skip the
            # fresh authority recheck. A transient OSError on publication can
            # leave entry already failed; awaiting it in the same try as
            # association()/materials() would classify that stale exception
            # and never discover a revocation that landed during the grace wait.
            with suppress(MaterialDeniedError, SQLAlchemyError, OSError, TimeoutError):
                await entry
            association, agent = await self.authority.association(workspace_id)
            await self.authority.materials(association, agent, current=True)
            authorized = True
        except (MaterialDeniedError, SQLAlchemyError, OSError, TimeoutError) as error:
            soft = isinstance(error, MaterialDeniedError) and error.reason in {
                "RECOVERY_ATTENTION_REQUIRED",
                "NODE_ADMISSION_CLOSED",
            }
            if not soft:
                # Always classify the latest cleanup failure. An earlier
                # grace-wait decision must not override a subsequent denial
                # or grace exhaustion discovered here.
                action = self._classify_authority_failure(
                    workspace_id, error, checker=_CHECKER_SCOPE_ENTRY
                )
                grace_defer = action == "defer"
                if action == "stop_exhausted":
                    stop_reason = _AUTHORITY_UNAVAILABLE
                    stop_message = self._authority_unavailable_message()
        finally:
            # Grace-eligible cancel: retract without treating the lapse as an
            # authority-lost stop (preserve_on_grace / classifier agree).
            authority_lost = not authorized and not grace_defer
            await self._stop(
                workspace_id,
                authority_lost=authority_lost,
                reason_code=stop_reason if authority_lost else None,
                message=stop_message if authority_lost else None,
            )

    async def _stop(
        self,
        workspace_id: str,
        *,
        authority_lost: bool = True,
        reason_code: str | None = None,
        message: str | None = None,
    ) -> None:
        ownership = owned_publication(self.authority.views, workspace_id)
        if ownership is not None and ownership.stopped:
            # Early grace-exhaustion stop already completed for this ownership;
            # the scope finalizer must not stop_workspace a second time.
            return
        token = self.workspace_id.set(None)
        preserve_directory = False
        try:
            try:
                if authority_lost:
                    if reason_code is None and message is None:
                        await self.stop_workspace(workspace_id)
                    else:
                        await self.stop_workspace(
                            workspace_id,
                            reason_code=reason_code,
                            message=message,
                        )
                else:
                    await self.stop_workspace(workspace_id, authority_lost=False)
                    preserve_directory = True
            finally:
                if preserve_directory:
                    if ownership is not None:
                        await self.authority.retract_publication(
                            workspace_id, ownership.publication
                        )
                    else:
                        await self.authority.retract(workspace_id, preserve_directory=True)
                else:
                    await self.authority.retract(workspace_id)
            # Keep the grace clock when cancellation (or another non-teardown
            # path) preserves the warm stack. Clearing here would let the next
            # scope/sweep reopen a full allowance despite no confirmed recovery
            # or actual runtime stop. Successful authority checks and
            # authority-lost teardown clear the window elsewhere / below.
            if authority_lost:
                self._clear_authority_grace(workspace_id)
            if ownership is not None:
                ownership.stopped = True
        finally:
            self.workspace_id.reset(token)

    async def environment(self) -> dict[str, str]:
        workspace_id = self.workspace_id.get()
        if workspace_id is None:
            raise MaterialDeniedError("WORKSPACE_SCOPE_REQUIRED", 403)
        # Every command prepare re-reads current material under its own
        # command_prepare window. A brief consumer renewal gap must not fail
        # an active monitor/exec; wait until material is current again, the
        # window is exhausted, or an explicit denial lands. Never return
        # values from a lapsed or failed check — including when called under a
        # shared git mirror lock (operator-accepted trade-off: the bounded
        # wait may delay peers on the same mirror; failing here would fail the
        # workspace). Poll-inline preserved recovery opts out via
        # without_authority_grace_wait and raises DeferredError instead.
        material = await self._await_with_authority_grace(
            workspace_id,
            lambda: self.authority.workspace(workspace_id),
            checker=_CHECKER_COMMAND_PREPARE,
        )
        return {**self.base_environment, **material.environment}

    async def profile_mounts(
        self, profile: WorkspaceProfile, workspace_id: str
    ) -> LocalSecretLeaseResolution:
        """Resolve protected mounts for stack launch with the same grace wait.

        ``ComposeStackLauncher`` invokes this after successful scope entry.
        A renewal gap during stack preparation must not fail provisioning
        while grace remains; raw authority errors would hit the provisioner's
        generic failure handler.
        """
        return await self._await_with_authority_grace(
            workspace_id,
            lambda: self.authority.profile_mounts(profile, workspace_id),
            checker=_CHECKER_PROFILE_MOUNTS,
        )

    async def resource_limits(self, workspace_id: str) -> tuple[float, float]:
        """Read reservation limits for stack launch with the same grace wait."""
        return await self._await_with_authority_grace(
            workspace_id,
            lambda: self.authority.resource_limits(workspace_id),
            checker=_CHECKER_RESOURCE_LIMITS,
        )

    async def admission_check(self) -> bool:
        try:
            accepted = True
            try:
                await self.authority.admission()
            except MaterialDeniedError:
                accepted = False
            # Reuse the normal worker poll, including after restart. Retained
            # views and stacks outlive the scope's active-task watcher.
            # Retry known stop failures even if a row left the owned query.
            # Reconciliation rechecks ownership and never stops a successor.
            pending = sorted(self._retained_stop_failures)
            retry = [item for item in pending if item > self._retry_cursor][:16]
            if not retry:
                retry = pending[:16]
            # Retry/history together get at most half the same 64-row budget;
            # independent cursors cannot delay or reset the live sweep.
            history_limit = 32 - len(retry)
            history = await self.authority.retained_workspaces(
                self._history_cursor, historical=True, limit=history_limit
            )
            if not history and self._history_cursor:
                self._history_cursor = ""
                history = await self.authority.retained_workspaces(
                    historical=True, limit=history_limit
                )
            limit = 64 - len(retry) - len(history)
            retained = await self.authority.retained_workspaces(self._retained_cursor, limit=limit)
            if not retained and self._retained_cursor:
                self._retained_cursor = ""
                retained = await self.authority.retained_workspaces(limit=limit)
            for workspace_id in dict.fromkeys([*retry, *retained, *history]):
                try:
                    if not accepted:
                        # No ordinary stop or reconciliation before acceptance.
                        # Retraction touches only this identity's view and an
                        # exact older binding; an obsolete process cannot
                        # retract its successor's publication.
                        await self.authority.retract_retired(workspace_id)
                    else:
                        try:
                            owned = await self.authority.reconcile_workspace(workspace_id)
                        except (
                            MaterialDeniedError,
                            SQLAlchemyError,
                            OSError,
                            TimeoutError,
                        ) as error:
                            action = self._classify_authority_failure(
                                workspace_id, error, checker=_CHECKER_RECONCILE
                            )
                            if action == "defer":
                                continue
                            reason_code = (
                                _AUTHORITY_UNAVAILABLE if action == "stop_exhausted" else None
                            )
                            message = (
                                self._authority_unavailable_message()
                                if action == "stop_exhausted"
                                else None
                            )
                            # Stash before teardown so a failed Compose stop still
                            # retries with HOSTED_AUTHORITY_UNAVAILABLE, not the
                            # stop_workspace default HOSTED_RUNTIME_STOPPED.
                            if reason_code is not None or message is not None:
                                self._retained_stop_failures[workspace_id] = _RetainedStop(
                                    reason_code=reason_code,
                                    message=message,
                                )
                            await self._stop(
                                workspace_id,
                                reason_code=reason_code,
                                message=message,
                            )
                        else:
                            # Reconcile recovery clears only its own window.
                            self._clear_authority_grace(workspace_id, checker=_CHECKER_RECONCILE)
                            if workspace_id in retry:
                                if owned:
                                    # A prior stop removed the view (refresh write
                                    # failure already deleted it, or retract ran
                                    # before Docker teardown failed). Healthy
                                    # material without a publication must not clear
                                    # the pending stop — retry teardown.
                                    pending_stop = self._retained_stop_failures.get(
                                        workspace_id, _RetainedStop()
                                    )
                                    await self._stop(
                                        workspace_id,
                                        reason_code=pending_stop.reason_code,
                                        message=pending_stop.message,
                                    )
                                else:
                                    # Finish this identity's failed retraction, without
                                    # stopping or touching a successor identity's view.
                                    await self.authority.retract(workspace_id)
                except (MaterialDeniedError, SQLAlchemyError, OSError, TimeoutError):
                    # Keep admission closed across pages without starving later
                    # cleanup. The bounded cursor wraps and retries each failure.
                    self._retained_stop_failures.setdefault(workspace_id, _RetainedStop())
                else:
                    self._retained_stop_failures.pop(workspace_id, None)
            if retained:
                self._retained_cursor = retained[-1]
            if history:
                self._history_cursor = history[-1]
            if retry:
                self._retry_cursor = retry[-1]
            await self.authority.admission()
            return not self._retained_stop_failures
        except (MaterialDeniedError, SQLAlchemyError, OSError, TimeoutError):
            return False

    async def git_environment(self) -> dict[str, str]:
        if self.workspace_id.get() is None:
            return dict(self.base_environment)
        return await self.environment()

    async def compose_environment(self) -> dict[str, str]:
        if self.workspace_id.get() is not None:
            await self.environment()
        # Secrets are injected only for an authorized exec, never persisted in
        # a rendered Compose configuration or the container's initial env.
        return dict(self.base_environment)


class _Output:
    _MAX_PENDING = 65536

    def __init__(self, callback: StreamCallback | None, secrets: tuple[str, ...]):
        self.callback, self.secrets, self.pending = callback, secrets, ""
        self.discarding = False
        self.discarding_multiline = False

    async def emit(self, text: str) -> None:
        if self.callback is not None and text:
            result = self.callback(text)
            if inspect.isawaitable(result):
                await result

    async def write(self, text: str) -> None:
        # Keep the raw record tail: redacting an incomplete token destroys its
        # prefix and lets a later chunk's suffix escape recognition. An oversized
        # unterminated record is suppressed, not emitted in unsafe fragments.
        for record in text.splitlines(keepends=True):
            complete = record.endswith(("\n", "\r"))
            if not self.discarding:
                if len(self.pending) + len(record) > self._MAX_PENDING:
                    self.discarding_multiline = "\n" in self.pending or "\r" in self.pending
                    self.pending = ""
                    self.discarding = True
                    await self.emit("<redacted>")
                else:
                    self.pending += record
                    if complete:
                        split = _stream_redaction_flushable_length(self.pending, self.secrets)
                        await self.emit(
                            redact_secrets(self.pending[:split], extra_secrets=self.secrets)
                        )
                        self.pending = self.pending[split:]
            if complete and self.discarding and not self.discarding_multiline:
                self.discarding = False
                await self.emit("\n")

    async def flush(self) -> None:
        split = _stream_redaction_flushable_length(self.pending, self.secrets)
        await self.emit(redact_secrets(self.pending[:split], extra_secrets=self.secrets))
        if split < len(self.pending):
            await self.emit("<redacted>")
        self.pending = ""


class HostedCommandRunner:
    def __init__(self, consumers: WorkspaceConsumers, delegate: AsyncStreamingCommandRunner):
        self.consumers, self.delegate = consumers, delegate

    @staticmethod
    def _exec_service(command: list[str], start: int) -> int:
        # These are the options used by Core's normal tracked compose exec.
        # Do not mistake a command argument named "agent" for the service.
        index = start
        while index < len(command) and command[index].startswith("-"):
            option = command[index]
            if option in {"-T", "--no-TTY", "--interactive", "-d", "--detach"}:
                index += 1
            elif option in {"-w", "--workdir", "-e", "--env", "-u", "--user", "--index"}:
                index += 2
            else:
                raise MaterialDeniedError("UNSUPPORTED_HOSTED_EXEC_OPTION")
        if index >= len(command):
            raise MaterialDeniedError("HOSTED_EXEC_SERVICE_REQUIRED")
        return index

    async def _prepare(
        self,
        args: list[str],
        env: Mapping[str, str] | None,
    ) -> tuple[list[str], dict[str, str], tuple[str, ...]]:
        scoped = await self.consumers.environment()
        secrets = tuple(
            scoped[key]
            for key in ("GH_TOKEN", "CODEX_API_KEY", "ANTHROPIC_API_KEY")
            if key in scoped
        )
        # Caller-supplied execution settings cannot replace the scoped identity,
        # credential helper, HOME, or credentials with inherited host authority.
        if env:
            for key, value in env.items():
                if key not in scoped and key in {"COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "TERM"}:
                    scoped[key] = value
            # Core's object/provenance probes must ignore poisoned replace refs,
            # grafts and system attributes/config. Only their fixed safe values
            # survive; this is not a general Git environment passthrough.
            if args[:1] == ["git"]:
                for key, value in {
                    "GIT_NO_REPLACE_OBJECTS": "1",
                    "GIT_GRAFT_FILE": os.devnull,
                    "GIT_ATTR_NOSYSTEM": "1",
                    "GIT_CONFIG_SYSTEM": os.devnull,
                }.items():
                    if env.get(key) == value:
                        scoped[key] = value
        command = list(args)
        if command[:2] == ["docker", "compose"] and "exec" in command:
            index = command.index("exec") + 1
            service = self._exec_service(command, index)
            if command[service] != "agent":
                for key in ("GH_TOKEN", "GITHUB_TOKEN", "CODEX_API_KEY", "ANTHROPIC_API_KEY"):
                    scoped.pop(key, None)
                return command, scoped, secrets
            scoped.update(
                GIT_CONFIG_COUNT="3",
                GIT_CONFIG_KEY_2="safe.directory",
                GIT_CONFIG_VALUE_2=DEFAULT_AGENT_WORKDIR,
            )
            command[service + 1 : service + 1] = [
                "sh",
                "-c",
                'umask 0002; exec "$@"',
                "awf-scoped-exec",
            ]
            flags = [
                part
                for key in ("GH_TOKEN", "GITHUB_TOKEN", "CODEX_API_KEY", "ANTHROPIC_API_KEY")
                if key in scoped
                for part in ("-e", key)
            ]
            flags.extend(
                part for key in scoped if key.startswith("GIT_CONFIG_") for part in ("-e", key)
            )
            command[index:index] = flags
        return command, scoped, secrets

    @staticmethod
    def _result(result: CommandResult, secrets: tuple[str, ...]) -> CommandResult:
        return replace(
            result,
            stdout=redact_secrets(result.stdout, extra_secrets=secrets),
            stderr=redact_secrets(result.stderr, extra_secrets=secrets),
            stdout_bytes=None
            if result.stdout_bytes is None
            else redact_exact_secret_bytes(
                result.stdout_bytes,
                extra_secrets=secrets,
            ),
        )

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        args, scoped, secrets = await self._prepare(args, env)
        result = await self.delegate.run(
            args,
            input_bytes=input_bytes,
            cwd=cwd,
            env=scoped,
            timeout_seconds=timeout_seconds,
        )
        return self._result(result, secrets)

    async def run_streaming(
        self,
        args: list[str],
        *,
        on_stdout: StreamCallback | None = None,
        on_stderr: StreamCallback | None = None,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        wall_timeout_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
        activity_probe: ActivityProbe | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        args, scoped, secrets = await self._prepare(args, env)
        stdout, stderr = _Output(on_stdout, secrets), _Output(on_stderr, secrets)
        failed = False
        reason_code: str | None = None
        try:
            result = await self.delegate.run_streaming(
                args,
                on_stdout=stdout.write,
                on_stderr=stderr.write,
                input_bytes=input_bytes,
                cwd=cwd,
                env=scoped,
                wall_timeout_seconds=wall_timeout_seconds,
                idle_timeout_seconds=idle_timeout_seconds,
                activity_probe=activity_probe,
            )
            if result.reason_code in _TIMEOUT_REASONS:
                reason_code = result.reason_code
        except BaseException as error:
            failed = True
            masked = getattr(error, "command_reason_code", None)
            if isinstance(masked, str) and masked in _TIMEOUT_REASONS:
                reason_code = masked
            raise
        finally:
            # Final log delivery is secondary to a command exception or an
            # already classified timeout. New cancellation still propagates.
            flush_error: Exception | None = None
            for output in (stdout, stderr):
                try:
                    await output.flush()
                except asyncio.CancelledError as error:
                    if reason_code is not None:
                        mark_masked_command_reason_code(error, reason_code)
                    raise
                except Exception as error:  # noqa: BLE001 - preserve primary outcome below.
                    if not failed and reason_code is None and flush_error is None:
                        flush_error = error
                    _log.warning(
                        "hosted.command_output_flush_failed",
                        exc_type=type(error).__name__,
                        reason_code=reason_code,
                    )
            if flush_error is not None:
                raise flush_error
        return self._result(result, secrets)
