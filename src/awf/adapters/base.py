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
from typing import Any, NoReturn

from awf.adapters.base_hosted_execution import (
    _HOSTED_CANCEL_DRAIN_TIMEOUT_SECONDS,
    build_hosted_exec_request,
    classify_hosted_result,
)
from awf.adapters.base_hosted_identity import (
    _buffered_output_not_streamed,
    _prepend_missing_streamed_output,
)
from awf.adapters.failure_reasons import (
    _failure_reason_for_result,
    masked_agent_timeout_reason_code,
)
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
from awf.adapters.worktree_activity import make_worktree_activity_probe
from awf.common.commands import (
    COMMAND_TIMEOUT_REASON,
    AsyncCommandRunner,
    CommandResult,
    StreamCallback,
)
from awf.common.compose_exec import (
    DEFAULT_AGENT_WORKDIR,
    ComposeExecCleanupError,
    TrackedComposeExec,
    build_tracked_compose_exec,
    cleanup_compose_exec_invocation,
    cleanup_compose_exec_invocation_after_cancellation,
    mark_masked_agent_reason_code,
)
from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets
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
        masked_reason_code: str | None = None
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
                worktree_path=worktree_path,
            )
            final_status = "success"
            return result
        except AgentRunError as exc:
            final_status = self._final_status_for_exception(exc)
            if final_status == "timeout":
                masked_reason_code = exc.reason_code
            raise
        except ComposeExecCleanupError as cleanup_exc:
            masked_reason_code = cleanup_exc.agent_reason_code
            raise
        except asyncio.CancelledError as cancel_exc:
            final_status = "cancelled"
            masked = getattr(cancel_exc, "agent_reason_code", None)
            masked_reason_code = masked if isinstance(masked, str) else None
            raise
        finally:
            try:
                await self._finalize_usage_sampling(
                    sampler_ctx, status=final_status, workspace_id=workspace_id
                )
            except asyncio.CancelledError as finalize_cancel:
                # ``UsageSampleContext.finalize`` shields its final sample and
                # then re-raises the cancellation it consumed, so this ``finally``
                # can escape with a ``CancelledError`` that *replaces* whatever
                # the run was already raising — dropping that exception's
                # watchdog classification and sending the verdict protocol's
                # cancellation handler down the rollback path that deletes the
                # timed-out run's work. Every timeout-classified exception the
                # body can raise is captured above so the tag survives the
                # replacement: the tagged ``CancelledError`` from a cancelled
                # post-timeout cleanup, the ``ComposeExecCleanupError`` from a
                # cleanup that could not prove the process tree gone, and the
                # mainline ``AgentRunError`` timeout itself
                # (PRRT_kwDOSJAM6s6f1F2D).
                if masked_reason_code is not None:
                    mark_masked_agent_reason_code(finalize_cancel, masked_reason_code)
                raise

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

    async def _sweep_cancelled_timeout_cleanup(
        self,
        *,
        invocation: TrackedComposeExec,
        workspace_id: str | None,
        compose_project: str,
        reason_code: str,
    ) -> None:
        """Finish a post-timeout cleanup that worker cancellation interrupted.

        Never raises: the caller is mid-cancellation and re-raises the tagged
        ``CancelledError`` immediately after, so a failed or re-cancelled sweep
        must not displace it (PRRT_kwDOSJAM6s6f0n6B). That includes a sweep that
        fails in an ordinary way rather than with ``ComposeExecCleanupError`` —
        the cleanup shells out, so it can also fail to spawn
        (PRRT_kwDOSJAM6s6f1JoH).
        """
        try:
            await cleanup_compose_exec_invocation_after_cancellation(
                self._runner,
                invocation,
                workspace_id=workspace_id,
            )
        except (Exception, asyncio.CancelledError) as sweep_exc:
            _log.warning(
                "agent.run.timeout_cleanup_cancelled_sweep_failed",
                agent=self.name_str,
                compose_project=compose_project,
                workspace_id=workspace_id,
                reason_code=reason_code,
                sweep_error=type(sweep_exc).__name__,
            )

    def _escalated_timeout_cleanup_error(
        self,
        *,
        cleanup_error: Exception,
        invocation: TrackedComposeExec,
        workspace_id: str | None,
        compose_project: str,
        reason_code: str,
        summary: str = "cleanup could not run",
    ) -> ComposeExecCleanupError:
        """Escalate a cleanup that could not run into a tagged cleanup failure.

        The cleanup shells out, so it can fail *before* it can judge the process
        tree — an ``OSError`` from a cleanup process that cannot be spawned,
        say — instead of raising the ``ComposeExecCleanupError`` it raises when
        the tracked process survives. Raw, that escapes untagged and the verdict
        protocol's generic exception branch rewinds to its rollback floor,
        deleting the timed-out run's edits and commits. It is a cleanup AWF could
        not complete, so escalate it as one and carry the watchdog classification
        the same way, keeping the timeout-preservation path reachable
        (PRRT_kwDOSJAM6s6f1JoH).

        The escalation stands in for the original error everywhere the failure is
        reported — the raised message, the log, and the ``WorkspaceEvent`` built
        from them — so it carries the original text too; only the traceback keeps
        ``__cause__``, and without the detail an operator cannot tell a missing
        docker binary from an exhausted host. A spawn failure can quote the
        command environment, so redact it like any other runtime log field.

        ``summary`` names *which* step failed. The escalation type is fixed —
        ``ComposeExecCleanupError`` is the vocabulary the preserve path reads —
        but callers also reach here for a failure that is not the cleanup's own
        (a teardown that failed while the cleanup itself succeeded), and blaming
        the cleanup in the message and the event would send an operator to debug
        a step that worked.
        """
        cleanup_detail = redact_secrets(str(cleanup_error))
        _log.warning(
            "agent.run.timeout_cleanup_error",
            agent=self.name_str,
            compose_project=compose_project,
            workspace_id=workspace_id,
            reason_code=reason_code,
            failure_summary=summary,
            cleanup_error=type(cleanup_error).__name__,
            cleanup_error_detail=cleanup_detail,
        )
        escalated_message = f"{summary}: {type(cleanup_error).__name__}"
        if cleanup_detail:
            escalated_message = f"{escalated_message}: {cleanup_detail}"
        escalated = ComposeExecCleanupError(
            invocation_id=invocation.invocation_id,
            source=invocation.source,
            label=invocation.label,
            message=escalated_message,
        )
        escalated.agent_reason_code = reason_code
        return escalated

    async def _cleanup_cancelled_run_preserving_timeout(
        self,
        *,
        invocation: TrackedComposeExec,
        workspace_id: str | None,
        compose_project: str,
        masked_reason_code: str | None,
    ) -> None:
        """Tear down a cancelled run's tracked exec, keeping any watchdog tag.

        This teardown runs while the tagged ``CancelledError`` is still in flight
        and it can fail — the tracked process can outlive it, and the cleanup
        shells out so it can also fail to spawn. Either failure replaces the
        cancellation the caller re-raises right after, and untagged the
        replacement reads as an ordinary cleanup failure: the verdict protocol
        resets to the rollback floor and deletes the timed-out agent's work.
        Carry the tag onto the replacement exactly as the post-result cleanup
        path does (PRRT_kwDOSJAM6s6f8cgU). An untagged cancellation classified
        nothing, so its failures surface unchanged.
        """
        try:
            await cleanup_compose_exec_invocation_after_cancellation(
                self._runner,
                invocation,
                workspace_id=workspace_id,
            )
        except ComposeExecCleanupError as cleanup_exc:
            if masked_reason_code is None:
                raise
            cleanup_exc.agent_reason_code = masked_reason_code
            _log.warning(
                "agent.run.timeout_cleanup_failed",
                agent=self.name_str,
                compose_project=compose_project,
                workspace_id=workspace_id,
                reason_code=masked_reason_code,
                cleanup_reason_code=cleanup_exc.reason_code,
            )
            raise
        except Exception as cleanup_error:
            if masked_reason_code is None:
                raise
            raise self._escalated_timeout_cleanup_error(
                cleanup_error=cleanup_error,
                invocation=invocation,
                workspace_id=workspace_id,
                compose_project=compose_project,
                reason_code=masked_reason_code,
            ) from cleanup_error

    async def _close_command_streams_preserving_timeout(
        self,
        sinks: CommandLogSinks,
        *,
        masked_reason_code: str | None,
        workspace_id: str | None,
        compose_project: str,
    ) -> None:
        """Close a run's log sinks without displacing a tagged timeout failure.

        The sinks close in a ``finally``, so a close that raises *replaces* the
        exception on its way out — and the likeliest reason it raises is that the
        failure being carried out was the log sink itself. The replacement is an
        untagged ordinary error, so the verdict protocol's generic branch rewinds
        to the rollback floor and deletes the timed-out run's edits and commits,
        undoing every tag the teardown hops just carried (PRRT_kwDOSJAM6s6f9jKk).

        Once a watchdog verdict is in flight the close failure is therefore logged
        rather than propagated, exactly as the cancelled-cleanup sweep logs its own
        (PRRT_kwDOSJAM6s6f0n6B): flushing the log stream is bookkeeping next to the
        classification the caller must see. With nothing classified in flight the
        close failure is the run's own outcome and surfaces unchanged.
        """
        try:
            await sinks.close()
        except Exception as close_error:
            if masked_reason_code is None:
                raise
            _log.warning(
                "agent.run.timeout_log_close_failed",
                agent=self.name_str,
                compose_project=compose_project,
                workspace_id=workspace_id,
                reason_code=masked_reason_code,
                close_error=type(close_error).__name__,
                close_error_detail=redact_secrets(str(close_error)),
            )

    async def _escalate_timed_out_stream_failure(
        self,
        *,
        stream_error: Exception,
        invocation: TrackedComposeExec,
        workspace_id: str | None,
        compose_project: str,
        reason_code: str,
    ) -> NoReturn:
        """Re-raise a classified run's ordinary teardown failure as a tagged one.

        ``run_streaming`` tags *every* exception that escapes after its watchdog
        classified the run — ordinary failures as much as cancellations
        (``mark_masked_command_reason_code``): terminating and reaping the timed-out
        child can raise an ``OSError``, and the last line it flushes can make the
        caller's log sink raise. Only the cancellation is republished by the caller,
        so an ordinary failure would reach the verdict protocol as an unclassified
        error and its generic branch rewinds to the rollback floor, deleting the
        timed-out run's edits and commits (PRRT_kwDOSJAM6s6f9RQl).

        The teardown that failed is the one that should have killed the child, so the
        tracked exec is torn down here exactly as the post-result timeout path tears
        it down — a surviving agent would keep writing into the worktree the caller
        is about to preserve — and the failure is then escalated in the cleanup-error
        vocabulary that preserve path reads, named for the teardown that actually
        failed rather than for the cleanup that succeeded.
        """
        try:
            await cleanup_compose_exec_invocation(
                self._runner,
                invocation,
                workspace_id=workspace_id,
            )
        except ComposeExecCleanupError as cleanup_exc:
            cleanup_exc.agent_reason_code = reason_code
            _log.warning(
                "agent.run.timeout_cleanup_failed",
                agent=self.name_str,
                compose_project=compose_project,
                workspace_id=workspace_id,
                reason_code=reason_code,
                cleanup_reason_code=cleanup_exc.reason_code,
            )
            raise cleanup_exc from stream_error
        except asyncio.CancelledError as cancel_exc:
            # The cancellation replaces the escalation below, so it carries the
            # classification out instead, and the interrupted teardown is still
            # finished under a shield (PRRT_kwDOSJAM6s6f0n6B).
            mark_masked_agent_reason_code(cancel_exc, reason_code)
            await self._sweep_cancelled_timeout_cleanup(
                invocation=invocation,
                workspace_id=workspace_id,
                compose_project=compose_project,
                reason_code=reason_code,
            )
            _log.warning(
                "agent.run.timeout_cleanup_cancelled",
                agent=self.name_str,
                compose_project=compose_project,
                workspace_id=workspace_id,
                reason_code=reason_code,
            )
            raise
        except Exception as cleanup_error:
            raise self._escalated_timeout_cleanup_error(
                cleanup_error=cleanup_error,
                invocation=invocation,
                workspace_id=workspace_id,
                compose_project=compose_project,
                reason_code=reason_code,
            ) from cleanup_error
        raise self._escalated_timeout_cleanup_error(
            cleanup_error=stream_error,
            invocation=invocation,
            workspace_id=workspace_id,
            compose_project=compose_project,
            reason_code=reason_code,
            summary="run teardown failed after the watchdog timeout",
        ) from stream_error

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
        worktree_path: Path | None = None,
    ) -> AgentRunResult:
        """Run an agent CLI with streamed logs and tracked cancellation cleanup."""
        sinks = await self._open_command_streams(
            workspace_id=workspace_id,
            log_source=log_source,
        )
        # The sink close below runs in a ``finally`` and can raise over whatever
        # is escaping, so the classification the handlers tag onto their failures
        # is recorded here too — see
        # ``_close_command_streams_preserving_timeout``.
        masked_timeout_reason_code: str | None = None
        try:
            run_streaming = getattr(self._runner, "run_streaming", None)
            # Print-mode CLIs emit nothing until they finish, so the idle
            # watchdog must also count worktree writes as liveness (#932). Only
            # pass the kwarg when a probe exists so runners that predate it
            # (and the non-worktree call sites) keep the old signature.
            activity_probe = await make_worktree_activity_probe(worktree_path)
            probe_kwargs: dict[str, Any] = (
                {"activity_probe": activity_probe} if activity_probe is not None else {}
            )
            try:
                if run_streaming is not None:
                    result = await run_streaming(
                        args,
                        input_bytes=prompt_input,
                        on_stdout=sinks.write_stdout if sinks is not None else None,
                        on_stderr=sinks.write_stderr if sinks is not None else None,
                        wall_timeout_seconds=self._agent_wall_timeout_seconds,
                        idle_timeout_seconds=self._agent_idle_timeout_seconds,
                        **probe_kwargs,
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
            except asyncio.CancelledError as cancel_exc:
                # The watchdog can have classified this run as a timeout before
                # the cancellation landed: the runner writes its synthetic
                # timeout diagnostic to the sink passed above, and that write
                # awaits. Republish the runner's tag in agent vocabulary so the
                # verdict protocol's cancellation handler preserves the timed-out
                # run's work instead of rewinding over it, exactly as it does for
                # a cancellation inside the post-timeout cleanup below
                # (PRRT_kwDOSJAM6s6f7rCe).
                masked_reason_code = masked_agent_timeout_reason_code(cancel_exc)
                if masked_reason_code is not None:
                    mark_masked_agent_reason_code(cancel_exc, masked_reason_code)
                    masked_timeout_reason_code = masked_reason_code
                # The teardown itself can fail and replace the tagged
                # cancellation, so it carries the tag onto its own failure
                # (PRRT_kwDOSJAM6s6f8cgU).
                await self._cleanup_cancelled_run_preserving_timeout(
                    invocation=invocation,
                    workspace_id=workspace_id,
                    compose_project=compose_project,
                    masked_reason_code=masked_reason_code,
                )
                raise
            except Exception as stream_exc:
                # Same masking hazard one exception class over: the runner tags an
                # *ordinary* failure raised in that same post-classification window
                # too (a reap that errors, a raising log sink). A cancellation can
                # be republished in place because the caller has a handler for it;
                # an untranslated ordinary failure has none, so it reaches the
                # verdict protocol's generic branch and the timed-out run's work is
                # rewound. Escalate it into the vocabulary the preserve path reads
                # (PRRT_kwDOSJAM6s6f9RQl). Without a watchdog verdict the failure is
                # the run's own outcome and surfaces unchanged.
                stream_reason_code = masked_agent_timeout_reason_code(stream_exc)
                if stream_reason_code is None:
                    raise
                masked_timeout_reason_code = stream_reason_code
                await self._escalate_timed_out_stream_failure(
                    stream_error=stream_exc,
                    invocation=invocation,
                    workspace_id=workspace_id,
                    compose_project=compose_project,
                    reason_code=stream_reason_code,
                )
        finally:
            if sinks is not None:
                await self._close_command_streams_preserving_timeout(
                    sinks,
                    masked_reason_code=masked_timeout_reason_code,
                    workspace_id=workspace_id,
                    compose_project=compose_project,
                )

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
                try:
                    await cleanup_compose_exec_invocation(
                        self._runner,
                        invocation,
                        workspace_id=workspace_id,
                    )
                except ComposeExecCleanupError as cleanup_exc:
                    # The cleanup failure replaces the ``AgentRunError`` below, so
                    # callers that preserve timed-out work instead of rolling it
                    # back (#932) would never see the timeout. Carry the watchdog
                    # classification on the escalating cleanup error.
                    #
                    # The tag has two consumers: the PR-monitor service-recovery
                    # loop (``runtime/pr_monitor_runner/agent_service_recovery.py``)
                    # and the control-plane executor recovery
                    # (``control/executor/agent_service_recovery.py``), which
                    # republishes it as the ``source_reason_code`` of a
                    # give-up so the failure records the watchdog timeout rather
                    # than the EXEC_PROCESS_CLEANUP_FAILED mask. When either
                    # recovers a tagged cleanup error it must publish the timeout
                    # rollback floor and sink the timed-out run's dirty worktree
                    # *before* any rerun, and give the rerun up when that
                    # preservation is not secured.
                    # Untagged cleanup errors reach the caller's preserve handler
                    # as plain service failures, so do not drop this assignment.
                    cleanup_exc.agent_reason_code = reason_code
                    _log.warning(
                        "agent.run.timeout_cleanup_failed",
                        agent=self.name_str,
                        compose_project=compose_project,
                        workspace_id=workspace_id,
                        reason_code=reason_code,
                        cleanup_reason_code=cleanup_exc.reason_code,
                    )
                    raise
                except asyncio.CancelledError as cancel_exc:
                    # Worker cancellation while this cleanup awaits bypasses the
                    # handler above and escapes before any ``AgentRunError`` is
                    # raised, so the run reads as an ordinary cancellation and the
                    # verdict protocol's cancellation handler rewinds to its
                    # rollback floor — deleting the timed-out run's commits and
                    # edits. Propagate the watchdog classification through the
                    # cancellation, exactly as the cleanup error above carries it,
                    # so that handler protects the work instead
                    # (PRRT_kwDOSJAM6s6f0n6B).
                    mark_masked_agent_reason_code(cancel_exc, reason_code)
                    # The tag alone protects the work from the rollback but not
                    # from the timed-out agent itself: the cancelled cleanup
                    # above never killed the tracked process, so it survives and
                    # keeps writing into the very worktree the caller is about to
                    # preserve. Finish the teardown under a shield first.
                    await self._sweep_cancelled_timeout_cleanup(
                        invocation=invocation,
                        workspace_id=workspace_id,
                        compose_project=compose_project,
                        reason_code=reason_code,
                    )
                    _log.warning(
                        "agent.run.timeout_cleanup_cancelled",
                        agent=self.name_str,
                        compose_project=compose_project,
                        workspace_id=workspace_id,
                        reason_code=reason_code,
                    )
                    raise
                except Exception as cleanup_error:
                    # The cleanup can also fail *before* it can judge the process
                    # tree, instead of raising the ``ComposeExecCleanupError``
                    # handled above; escalate it as the cleanup failure it is so
                    # it reaches the caller carrying the watchdog classification
                    # (PRRT_kwDOSJAM6s6f1JoH).
                    raise self._escalated_timeout_cleanup_error(
                        cleanup_error=cleanup_error,
                        invocation=invocation,
                        workspace_id=workspace_id,
                        compose_project=compose_project,
                        reason_code=reason_code,
                    ) from cleanup_error
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
