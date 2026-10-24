"""Task-local authority around the existing worker's consumer operations."""

import asyncio
import inspect
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from awf.common.commands import (
    ActivityProbe,
    AsyncStreamingCommandRunner,
    CommandResult,
    StreamCallback,
)
from awf.common.compose_exec import DEFAULT_AGENT_WORKDIR
from awf.common.redaction import redact_exact_secret_bytes, redact_secrets
from awf.hosted_node.identity import MaterialDeniedError, WorkspaceScopeDeniedError
from awf.hosted_node.runtime_authority import WorkspaceAuthority
from awf.hosted_node.runtime_filesystem import share_checkout
from awf.service.logs import _stream_redaction_flushable_length


async def _no_stop(_workspace_id: str) -> None:
    return None


class WorkspaceConsumers:
    def __init__(self, authority: WorkspaceAuthority, work_dir: Path):
        self.authority = authority
        self.work_dir = work_dir
        self.workspace_id: ContextVar[str | None] = ContextVar("hosted_workspace", default=None)
        self.stop_workspace: Callable[[str], Awaitable[None]] = _no_stop
        self.poll_seconds = 2.0
        self._retained_cursor = ""
        self._history_cursor = ""
        self._retry_cursor = ""
        self._retained_stop_failures: set[str] = set()
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
        existing = self.workspace_id.get()
        if existing is not None:
            if existing != workspace_id:
                raise MaterialDeniedError("WORKSPACE_SCOPE_MISMATCH", 403)
            await self.authority.workspace(workspace_id)
            yield
            return
        try:
            await self.authority.workspace(workspace_id)
        except (MaterialDeniedError, SQLAlchemyError, OSError, TimeoutError) as error:
            await self._stop(workspace_id)
            if isinstance(error, MaterialDeniedError):
                raise WorkspaceScopeDeniedError(error.reason, error.status) from error
            raise
        token = self.workspace_id.set(workspace_id)
        parent = asyncio.current_task()
        assert parent is not None
        failure: Exception | None = None
        cancelled = False
        caller_cancelled = False
        finished = asyncio.Event()

        async def watch() -> None:
            nonlocal failure
            try:
                while True:
                    try:
                        await asyncio.wait_for(finished.wait(), timeout=self.poll_seconds)
                        return
                    except TimeoutError:
                        pass
                    await self.authority.workspace(workspace_id)
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
            with suppress(asyncio.CancelledError):
                await watcher
            self.workspace_id.reset(token)
            try:
                await self.authority.workspace(workspace_id)
            except (MaterialDeniedError, SQLAlchemyError, OSError, TimeoutError) as error:
                if failure is None or isinstance(failure, MaterialDeniedError):
                    failure = error
            if failure is not None or cancelled:
                await self._stop(workspace_id)
            if failure is not None and not caller_cancelled:
                if isinstance(failure, MaterialDeniedError):
                    raise WorkspaceScopeDeniedError(failure.reason, failure.status) from failure
                raise failure

    async def _stop(self, workspace_id: str) -> None:
        token = self.workspace_id.set(None)
        try:
            try:
                await self.stop_workspace(workspace_id)
            finally:
                await self.authority.retract(workspace_id)
        finally:
            self.workspace_id.reset(token)

    async def environment(self) -> dict[str, str]:
        workspace_id = self.workspace_id.get()
        if workspace_id is None:
            raise MaterialDeniedError("WORKSPACE_SCOPE_REQUIRED", 403)
        material = await self.authority.workspace(workspace_id)
        return {**self.base_environment, **material.environment}

    async def admission_check(self) -> bool:
        try:
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
                    try:
                        owned = await self.authority.reconcile_workspace(workspace_id)
                    except (MaterialDeniedError, SQLAlchemyError, OSError, TimeoutError):
                        await self._stop(workspace_id)
                    else:
                        if not owned and workspace_id in retry:
                            # Finish this identity's failed retraction, without
                            # stopping or touching a successor identity's view.
                            await self.authority.retract(workspace_id)
                except (MaterialDeniedError, SQLAlchemyError, OSError, TimeoutError):
                    # Keep admission closed across pages without starving later
                    # cleanup. The bounded cursor wraps and retries each failure.
                    self._retained_stop_failures.add(workspace_id)
                else:
                    self._retained_stop_failures.discard(workspace_id)
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
        finally:
            await stdout.flush()
            await stderr.flush()
        return self._result(result, secrets)
