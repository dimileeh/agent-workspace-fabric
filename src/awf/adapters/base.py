"""Base adapter — shared scaffolding for coding-CLI subclasses.

Each adapter turns a prompt into a ``docker compose exec`` invocation against
the workspace's agent container. The base class owns the docker-compose part;
subclasses only decide which CLI flags to use.

The adapter does NOT handle commits, pushes, or PR creation — that's Task 7.
It just runs the CLI, captures stdout/stderr, and returns a structured result.
"""

from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awf.adapters.base_hosted_execution import (
    _HOSTED_CANCEL_DRAIN_TIMEOUT_SECONDS,
    build_hosted_exec_request,
    classify_hosted_result,
)
from awf.adapters.base_hosted_identity import (
    _buffered_output_not_streamed,
    _prepend_missing_streamed_output,
)
from awf.adapters.failure_reasons import _failure_reason_for_result
from awf.adapters.prompt_preamble import _AWF_PROMPT_PREAMBLE
from awf.adapters.provider_failures import classify_provider_failure
from awf.adapters.registry_api import _REGISTRY, get_adapter, register_adapter
from awf.adapters.run_results import AgentRunError, AgentRunResult
from awf.adapters.runtime_executor import (
    _HOSTED_TIMEOUT_RETURN_CODE,
    AgentRuntimeExecResult,
    AgentRuntimeExecutor,
    AgentRuntimeGitPreparation,
)
from awf.adapters.usage import UsageSampleContext, UsageSampler
from awf.common.commands import (
    COMMAND_TIMEOUT_REASON,
    AsyncCommandRunner,
    CommandResult,
    StreamCallback,
)
from awf.common.compose_exec import (
    DEFAULT_AGENT_WORKDIR,
    TrackedComposeExec,
    build_tracked_compose_exec,
    cleanup_compose_exec_invocation,
    cleanup_compose_exec_invocation_after_cancellation,
)
from awf.common.logging import get_logger
from awf.db.enums import AgentRuntime
from awf.profiles.compose import (
    agent_exec_env_passthrough as agent_exec_env_passthrough,
)
from awf.profiles.compose import (
    filter_hosted_env_passthrough_names as filter_hosted_env_passthrough_names,
)
from awf.profiles.compose import (
    hosted_file_auth_mount_targets as hosted_file_auth_mount_targets,
)
from awf.profiles.compose import (
    hosted_github_token_passthrough_names as hosted_github_token_passthrough_names,
)
from awf.profiles.compose import (
    hosted_profile_env_passthrough_aliases as hosted_profile_env_passthrough_aliases,
)
from awf.profiles.compose import (
    hosted_profile_env_passthrough_names as hosted_profile_env_passthrough_names,
)
from awf.profiles.compose import (
    literal_profile_env_from_compose as literal_profile_env_from_compose,
)
from awf.profiles.compose_postgres_env import (
    try_compose_agent_env_and_postgres_passwords as try_compose_agent_env_and_postgres_passwords,
)
from awf.profiles.models import WorkspaceProfile
from awf.runtime.logs import CommandLogSinks, LogStore

__all__ = (
    "AgentRunError",
    "AgentRunResult",
    "RetiredAgentAdapter",
    "_REGISTRY",
    "get_adapter",
    "register_adapter",
)
_log = get_logger(__name__)

DEFAULT_AGENT_WALL_TIMEOUT_SECONDS = 7200.0
"""Default maximum wall-clock duration for a single agent CLI run."""

DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS = 3600.0
"""Default maximum stdout/stderr silence for a single agent CLI run."""


def _discard_hosted_execute_task_result(task: asyncio.Task[AgentRuntimeExecResult]) -> None:
    """Consume a cancelled hosted-execution task's eventual result."""
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


@dataclass(frozen=True)
class AgentDefaults:
    """Default model and reasoning/thinking policy for one agent CLI."""

    model: str
    effort: str | None = None


class AgentAdapter(ABC):
    """Shared scaffolding for coding-CLI adapters."""

    def __init__(
        self,
        *,
        runner: AsyncCommandRunner,
        default_model: str | None = None,
        default_effort: str | None = None,
        log_store: LogStore | None = None,
        agent_wall_timeout_seconds: float = DEFAULT_AGENT_WALL_TIMEOUT_SECONDS,
        agent_idle_timeout_seconds: float = DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS,
        usage_sampler: UsageSampler | None = None,
        runtime_executor: AgentRuntimeExecutor | None = None,
    ) -> None:
        """Initialize the adapter runtime dependencies and timeout policy."""
        if agent_wall_timeout_seconds <= 0:
            raise ValueError("agent_wall_timeout_seconds must be positive")
        if agent_idle_timeout_seconds <= 0:
            raise ValueError("agent_idle_timeout_seconds must be positive")
        self._runner = runner
        self._default_model = default_model
        self._default_effort = default_effort
        self._log_store = log_store
        self._agent_wall_timeout_seconds = agent_wall_timeout_seconds
        self._agent_idle_timeout_seconds = agent_idle_timeout_seconds
        self._usage_sampler = usage_sampler
        self._runtime_executor = runtime_executor

    @property
    @abstractmethod
    def name(self) -> AgentRuntime | str:
        """Identity of the underlying agent runtime."""
        ...  # pragma: no cover

    @property
    def name_str(self) -> str:
        """Return string representation of adapter's runtime name."""
        name = self.name
        return name.value if isinstance(name, AgentRuntime) else str(name)

    @property
    def is_retired(self) -> bool:
        """Return True if this adapter represents a retired or unsupported agent runtime."""
        return False

    @property
    def default_model(self) -> str | None:
        """Return the default model for this adapter."""
        return self._default_model

    @property
    def runtime_scratch_paths(self) -> tuple[str, ...]:
        """Return checkout-local scratch paths this agent creates while running.

        These are agent-runtime artifacts (e.g. an agent's nested worktrees),
        not AWF artifacts or project work. AWF excludes them from a worktree's
        git ignore view before validation so its cleanliness guard does not
        mistake them for a dirty tree. Defaults to no paths; agents that create
        scratch state override this.
        """
        return ()

    @property
    def hosted_env_passthrough_names(self) -> tuple[str, ...]:
        """Return env-passthrough *names* a hosted executor should resolve.

        Names only — secret values are NEVER transported. The default is empty;
        adapters that have a hosted credential contract (e.g. Codex
        ``CODEX_API_KEY``) override this so a hosted runtime can resolve and
        inject the credential out-of-band. The compose-derived passthrough
        still applies on the local Compose path; this hook is only consulted
        on the hosted (non-compose) execution path, where
        ``filter_hosted_env_passthrough_names`` applies the same
        compose/profile-owned exclusions as the local ``docker compose exec``
        path before the request is built, so a profile-owned auth/env slot is
        not reintroduced by the hosted executor.
        """
        return ()

    @property
    def provider_recovery_default_model(self) -> str | None:
        """Return the implicit model identity provider recovery should attribute."""
        return self._selected_model_for_run(model=None)

    @property
    def is_hosted(self) -> bool:
        """Return whether this adapter delegates to the injected runtime executor.

        When true, agent runs go through the hosted path and there is no
        Compose agent service to probe or restart — monitor recovery must
        skip the Compose-service restart branch for timeouts in this mode.
        """
        return self._runtime_executor is not None

    @abstractmethod
    def get_provider(self, model: str | None) -> str:
        """Return the canonical provider identifier for a model."""
        ...  # pragma: no cover

    @abstractmethod
    def _cli_args(self, *, model: str | None) -> list[str]:
        """Return the CLI-specific argv (after ``agent`` service name).

        The wrapped prompt is streamed through stdin by ``run``. Implementations
        must not place it in argv: review comments can exceed Linux's per-arg
        length limit, and argv prompt transport leaks large prompts into process
        listings.

        ``model`` is the explicit per-run override. Implementations that should
        use a configured default model must apply ``self._default_model``
        themselves so they can still distinguish explicit overrides from
        effort-derived defaults.
        """

    def _selected_model_for_run(self, *, model: str | None) -> str | None:
        """Return the model explicitly selected for this run, if any."""
        return model or self._default_model

    async def run(
        self,
        *,
        compose_project: str,
        compose_file: Path,
        prompt: str,
        model: str | None = None,
        workspace_id: str | None = None,
        log_source: str = "agent",
        hosted_pr_identity: dict[str, Any] | None = None,
        git_preparation: AgentRuntimeGitPreparation | None = None,
        profile: WorkspaceProfile | None = None,
        worktree_path: Path | None = None,
        workdir: str = DEFAULT_AGENT_WORKDIR,
    ) -> AgentRunResult:
        """Invoke the coding CLI inside the workspace's agent container.

        Raises ``AgentRunError`` on non-zero exit.

        The user-supplied ``prompt`` is wrapped with an AWF preamble
        that encodes contract invariants the agent must honour — most
        notably "do not switch git branches". Agent CLIs (Claude Code,
        Codex) sometimes run ``git checkout -b <name>`` mid-session as
        part of their own "good git hygiene" heuristics, but AWF has
        already created the right branch on entry; drifting strands
        the agent's commits on an orphan branch and the PR ends up
        empty. Preamble + post-agent branch-drift recovery in
        ``control/executor.py`` form a belt-and-braces defence.
        """
        wrapped_prompt = _AWF_PROMPT_PREAMBLE + prompt
        prompt_input = wrapped_prompt.encode("utf-8")
        selected_model = self._selected_model_for_run(model=model)
        cli_args = self._cli_args(model=model)
        if self._runtime_executor is not None:
            return await self._run_hosted(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt_input=prompt_input,
                cli_args=cli_args,
                selected_model=selected_model,
                model=model,
                workspace_id=workspace_id,
                log_source=log_source,
                hosted_pr_identity=hosted_pr_identity,
                git_preparation=git_preparation,
                profile=profile,
                worktree_path=worktree_path,
            )

        env_passthrough = await asyncio.to_thread(
            agent_exec_env_passthrough, compose_file=compose_file
        )
        sampler_ctx: UsageSampleContext | None = None
        final_status = "failed"
        try:
            invocation = build_tracked_compose_exec(
                compose_project=compose_project,
                compose_file=compose_file,
                cli_args=cli_args,
                source=log_source,
                label=self.name_str,
                workdir=workdir,
                preserve_stdin=True,
                env_passthrough=env_passthrough,
            )
            _log.info(
                "agent.run.start",
                agent=self.name_str,
                compose_project=compose_project,
                workspace_id=workspace_id,
                model=selected_model,
                effort=self._default_effort,
                wall_timeout_seconds=self._agent_wall_timeout_seconds,
                idle_timeout_seconds=self._agent_idle_timeout_seconds,
                source=log_source,
                prompt_bytes=len(prompt_input),
            )
            sampler_ctx = await self._start_usage_sampling(
                compose_project=compose_project,
                compose_file=compose_file,
                workspace_id=workspace_id,
            )
            result = await self._run_agent_cli(
                invocation=invocation,
                args=invocation.args,
                prompt_input=prompt_input,
                model=model,
                workspace_id=workspace_id,
                log_source=log_source,
                compose_project=compose_project,
            )
            final_status = "success"
            return result
        except AgentRunError as exc:
            final_status = self._final_status_for_exception(exc)
            raise
        except asyncio.CancelledError:
            final_status = "cancelled"
            raise
        finally:
            await self._finalize_usage_sampling(
                sampler_ctx, status=final_status, workspace_id=workspace_id
            )

    async def _start_usage_sampling(
        self,
        *,
        compose_project: str,
        compose_file: Path,
        workspace_id: str | None,
    ) -> UsageSampleContext | None:
        if self._usage_sampler is None or workspace_id is None:
            return None
        try:
            return await self._usage_sampler.start(
                compose_project=compose_project,
                compose_file=compose_file,
                workspace_id=workspace_id,
                provider=self.name,
            )
        except Exception:
            _log.warning(
                "usage.collect.error",
                agent=self.name_str,
                workspace_id=workspace_id,
                phase="start",
                exc_info=True,
            )
            return None

    async def _finalize_usage_sampling(
        self,
        sampler_ctx: UsageSampleContext | None,
        *,
        status: str,
        workspace_id: str | None,
    ) -> None:
        if sampler_ctx is None:
            return
        try:
            await sampler_ctx.finalize(status=status)
        except Exception:
            _log.warning(
                "usage.collect.error",
                agent=self.name_str,
                workspace_id=workspace_id,
                phase="finalize",
                exc_info=True,
            )

    async def _run_hosted(
        self,
        *,
        compose_project: str,
        compose_file: Path,
        prompt_input: bytes,
        cli_args: list[str],
        selected_model: str | None,
        model: str | None,
        workspace_id: str | None,
        log_source: str,
        hosted_pr_identity: dict[str, Any] | None,
        git_preparation: AgentRuntimeGitPreparation | None,
        profile: WorkspaceProfile | None,
        worktree_path: Path | None = None,
    ) -> AgentRunResult:
        """Delegate agent CLI execution to the injected runtime executor."""
        runtime_executor = self._runtime_executor
        assert runtime_executor is not None  # guarded by run() dispatch

        sampler_ctx: UsageSampleContext | None = None
        final_status = "failed"
        sinks = await self._open_command_streams(workspace_id=workspace_id, log_source=log_source)
        try:
            streamed_stdout_chunks: list[str] = []
            streamed_stderr_chunks: list[str] = []

            async def _on_stdout(data: str) -> None:
                streamed_stdout_chunks.append(data)
                if sinks is not None:
                    await sinks.write_stdout(data)

            async def _on_stderr(data: str) -> None:
                streamed_stderr_chunks.append(data)
                if sinks is not None:
                    await sinks.write_stderr(data)

            on_stdout_cb: StreamCallback | None = _on_stdout
            on_stderr_cb: StreamCallback | None = _on_stderr

            request = await build_hosted_exec_request(
                self,
                compose_file=compose_file,
                compose_project=compose_project,
                prompt_input=prompt_input,
                cli_args=cli_args,
                selected_model=selected_model,
                workspace_id=workspace_id,
                log_source=log_source,
                hosted_pr_identity=hosted_pr_identity,
                git_preparation=git_preparation,
                profile=profile,
                worktree_path=worktree_path,
                on_stdout_cb=on_stdout_cb,
                on_stderr_cb=on_stderr_cb,
            )
            if self._usage_sampler is not None:
                _log.info(
                    "agent.run.hosted.usage_sampling_skipped",
                    agent=self.name_str,
                    workspace_id=workspace_id,
                )
            try:
                execute_task = asyncio.create_task(runtime_executor.execute(request))
                try:
                    done, _pending = await asyncio.wait(
                        {execute_task},
                        timeout=request.wall_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    execute_task.cancel()
                    if execute_task.done():
                        _discard_hosted_execute_task_result(execute_task)
                    else:
                        execute_task.add_done_callback(_discard_hosted_execute_task_result)
                    raise
                if execute_task in done or execute_task.done():
                    hosted_result = execute_task.result()
                else:
                    execute_task.cancel()
                    try:
                        done_after_cancel, _pending_after_cancel = await asyncio.wait(
                            {execute_task},
                            timeout=_HOSTED_CANCEL_DRAIN_TIMEOUT_SECONDS,
                        )
                    except asyncio.CancelledError:
                        execute_task.add_done_callback(_discard_hosted_execute_task_result)
                        raise
                    if execute_task in done_after_cancel or execute_task.done():
                        _discard_hosted_execute_task_result(execute_task)
                    else:
                        execute_task.add_done_callback(_discard_hosted_execute_task_result)
                    hosted_watchdog_timeout_stderr = (
                        "hosted runtime executor timed out after "
                        f"{self._agent_wall_timeout_seconds:g}s\n"
                    )
                    timeout_stderr = (
                        "".join(streamed_stderr_chunks) + hosted_watchdog_timeout_stderr
                    )
                    hosted_result = AgentRuntimeExecResult(
                        returncode=_HOSTED_TIMEOUT_RETURN_CODE,
                        stdout="".join(streamed_stdout_chunks),
                        stderr=timeout_stderr,
                        timeout_reason=COMMAND_TIMEOUT_REASON,
                        terminal_head_sha=None,
                    )
            except AgentRunError:
                raise
            except Exception as exc:
                error_stderr = f"{type(exc).__name__}: {exc}"
                raise AgentRunError(
                    agent=self.name,
                    result=CommandResult(
                        returncode=1,
                        stdout=_prepend_missing_streamed_output(
                            chunks=streamed_stdout_chunks,
                            buffered="",
                        ),
                        stderr=_prepend_missing_streamed_output(
                            chunks=streamed_stderr_chunks,
                            buffered=error_stderr,
                        ),
                    ),
                    reason_code="AGENT_HOSTED_EXECUTOR_ERROR",
                ) from exc
            if sinks is not None:
                stdout_not_streamed = _buffered_output_not_streamed(
                    chunks=streamed_stdout_chunks,
                    buffered=hosted_result.stdout,
                )
                stderr_not_streamed = _buffered_output_not_streamed(
                    chunks=streamed_stderr_chunks,
                    buffered=hosted_result.stderr,
                )
                if stdout_not_streamed:
                    await sinks.write_stdout(stdout_not_streamed)
                if stderr_not_streamed:
                    await sinks.write_stderr(stderr_not_streamed)
            hosted_result = AgentRuntimeExecResult(
                returncode=hosted_result.returncode,
                stdout=_prepend_missing_streamed_output(
                    chunks=streamed_stdout_chunks,
                    buffered=hosted_result.stdout,
                ),
                stderr=_prepend_missing_streamed_output(
                    chunks=streamed_stderr_chunks,
                    buffered=hosted_result.stderr,
                ),
                timeout_reason=hosted_result.timeout_reason,
                terminal_head_sha=hosted_result.terminal_head_sha,
            )
            result = self._classify_hosted_result(
                hosted_result=hosted_result,
                model=model,
                workspace_id=workspace_id,
            )
            final_status = "success"
            return result
        except AgentRunError as exc:
            final_status = self._final_status_for_exception(exc)
            raise
        except asyncio.CancelledError:
            final_status = "cancelled"
            raise
        finally:
            if sinks is not None:
                await sinks.close()
            await self._finalize_usage_sampling(
                sampler_ctx, status=final_status, workspace_id=workspace_id
            )

    async def _open_command_streams(
        self,
        *,
        workspace_id: str | None,
        log_source: str,
    ) -> CommandLogSinks | None:
        """Open log-store command sinks for a run, or ``None`` when unavailable."""
        if self._log_store is None or workspace_id is None:
            return None
        return await self._log_store.open_command_streams(
            workspace_id=workspace_id,
            base_stream_id=log_source,
            source=log_source,
            name=f"{log_source.capitalize()} ({self.name_str})"
            if log_source != "agent"
            else self.name_str,
        )

    def _final_status_for_exception(self, exc: AgentRunError) -> str:
        """Map an agent-run exception to a usage-sampling final status."""
        return "timeout" if exc.reason_code in {"AGENT_TIMEOUT", "AGENT_IDLE_TIMEOUT"} else "failed"

    def _classify_hosted_result(
        self,
        *,
        hosted_result: AgentRuntimeExecResult,
        model: str | None,
        workspace_id: str | None,
    ) -> AgentRunResult:
        """Map a hosted executor result through the same failure classification."""
        return classify_hosted_result(
            self,
            hosted_result=hosted_result,
            model=model,
            workspace_id=workspace_id,
        )

    async def _run_agent_cli(
        self,
        *,
        invocation: TrackedComposeExec,
        args: list[str],
        prompt_input: bytes,
        model: str | None,
        workspace_id: str | None,
        log_source: str,
        compose_project: str,
    ) -> AgentRunResult:
        """Run an agent CLI with streamed logs and tracked cancellation cleanup."""
        sinks = await self._open_command_streams(
            workspace_id=workspace_id,
            log_source=log_source,
        )
        try:
            run_streaming = getattr(self._runner, "run_streaming", None)
            try:
                if run_streaming is not None:
                    result = await run_streaming(
                        args,
                        input_bytes=prompt_input,
                        on_stdout=sinks.write_stdout if sinks is not None else None,
                        on_stderr=sinks.write_stderr if sinks is not None else None,
                        wall_timeout_seconds=self._agent_wall_timeout_seconds,
                        idle_timeout_seconds=self._agent_idle_timeout_seconds,
                    )
                else:
                    _log.warning(
                        "agent.run.watchdog_unavailable",
                        agent=self.name_str,
                        compose_project=compose_project,
                        workspace_id=workspace_id,
                        reason="runner does not support run_streaming",
                    )
                    result = await self._runner.run(args, input_bytes=prompt_input)
                    if sinks is not None:
                        await sinks.write_stdout(result.stdout)
                        await sinks.write_stderr(result.stderr)
            except asyncio.CancelledError:
                await cleanup_compose_exec_invocation_after_cancellation(
                    self._runner,
                    invocation,
                    workspace_id=workspace_id,
                )
                raise
        finally:
            if sinks is not None:
                await sinks.close()

        if not result.ok:
            provider = self.get_provider(model)
            selected_model = self._selected_model_for_run(model=model)
            reported_model = selected_model or "unknown"
            provider_failure = classify_provider_failure(
                reason_code=_failure_reason_for_result(result),
                stdout=result.stdout,
                stderr=result.stderr,
                provider=provider,
                model=selected_model,
            )
            reason_code = (
                provider_failure.reason_code
                if provider_failure is not None
                else _failure_reason_for_result(result)
            )
            if reason_code in {"AGENT_TIMEOUT", "AGENT_IDLE_TIMEOUT"}:
                await cleanup_compose_exec_invocation(
                    self._runner,
                    invocation,
                    workspace_id=workspace_id,
                )
            log_event = (
                "agent.run.timeout"
                if reason_code in {"AGENT_TIMEOUT", "AGENT_IDLE_TIMEOUT"}
                else "agent.run.failed"
            )
            _log.warning(
                log_event,
                agent=self.name_str,
                compose_project=compose_project,
                workspace_id=workspace_id,
                returncode=result.returncode,
                reason_code=reason_code,
                stdout_bytes=len(result.stdout),
                stderr_bytes=len(result.stderr),
            )
            details: dict[str, str | bool | int | dict[str, object]] | None = None
            if provider_failure is not None:
                recovery_metadata = provider_failure.to_metadata()
                details = {
                    "provider": recovery_metadata.get("provider", provider),
                    "model": recovery_metadata.get("model", reported_model),
                    "retryable": True,
                    "recommended_action": str(recovery_metadata["recommended_action"]),
                    "provider_recovery": recovery_metadata,
                }
            raise AgentRunError(
                agent=self.name,
                result=result,
                reason_code=reason_code,
                details=details,
            )

        _log.info(
            "agent.run.ok",
            agent=self.name_str,
            compose_project=compose_project,
            workspace_id=workspace_id,
            stdout_bytes=len(result.stdout),
            stderr_bytes=len(result.stderr),
        )
        return AgentRunResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


from awf.adapters.retired_adapter import RetiredAgentAdapter  # noqa: E402
