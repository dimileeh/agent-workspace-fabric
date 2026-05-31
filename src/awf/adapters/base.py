"""Base adapter — shared scaffolding for coding-CLI subclasses.

Each adapter turns a prompt into a ``docker compose exec`` invocation against
the workspace's agent container. The base class owns the docker-compose part;
subclasses only decide which CLI flags to use.

The adapter does NOT handle commits, pushes, or PR creation — that's Task 7.
It just runs the CLI, captures stdout/stderr, and returns a structured result.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awf.adapters.provider_failures import classify_provider_failure
from awf.adapters.usage import UsageSampleContext, UsageSampler
from awf.common.commands import (
    COMMAND_IDLE_TIMEOUT_REASON,
    COMMAND_TIMEOUT_REASON,
    AsyncCommandRunner,
    CommandResult,
)
from awf.common.compose_exec import (
    build_tracked_compose_exec,
    cleanup_compose_exec_invocation,
    cleanup_compose_exec_invocation_after_cancellation,
)
from awf.common.logging import get_logger
from awf.db.enums import AgentRuntime
from awf.runtime.logs import LogStore

_log = get_logger(__name__)


DEFAULT_AGENT_WALL_TIMEOUT_SECONDS = 7200.0
"""Default maximum wall-clock duration for a single agent CLI run."""

DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS = 3600.0
"""Default maximum stdout/stderr silence for a single agent CLI run."""


# Prepended to every agent prompt. Encodes contract invariants the
# agent must honour inside an AWF workspace.
_AWF_PROMPT_PREAMBLE = """\
## AWF workspace contract (DO NOT VIOLATE)

You are inside an AWF-managed Docker workspace at /workspace, on a git
branch that AWF has already created for you. Your contract:

1. **DO NOT switch git branches.** Do not run `git checkout -b <name>`,
   `git switch -c <name>`, `git branch <name>`, `git checkout <name>`,
   or any equivalent. Commit ALL work on the current branch. AWF owns
   branch management. Drifting to a "properly named" feature branch
   strands your commits and the PR ends up empty.
2. **DO NOT push, rebase onto origin, or force-push.** AWF handles
   push + PR creation after you exit.
3. Commit your work locally as you go (`git add` + `git commit` is
   fine). AWF's post-agent step will also capture any uncommitted
   changes, but commits with good messages are preferred.
4. **DO NOT run AWF/GitHub-owned broad validation inside the agent
   phase.** Do not run the full `.awf/workspace.yml` validation suite,
   whole-repository test suites, full coverage gates such as
   `pytest --cov` / `--cov-fail-under`, full frontend builds, or CI-
   equivalent commands unless the operator explicitly asks for that
   exact diagnostic action in this task. AWF and GitHub CI own broad
   validation, provenance, logs, timeouts, and merge gating after you
   finish the code.
5. Focus your local checks. Run targeted tests, focused lint/type checks,
   or small repro commands only for the files and behavior you changed.
   When a plan or validation document needs evidence, record those
   focused checks and state that full AWF/GitHub validation is managed by
   AWF after agent completion; do not execute the broad suite yourself.

---

"""


@dataclass(frozen=True)
class AgentRunResult:
    """Structured result of one coding-CLI run."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Whether the adapter completed successfully."""
        return self.returncode == 0


class AgentRunError(Exception):
    """Raised when the coding CLI exits non-zero.

    Carries the full CommandResult so the validation runner or operator can
    see exactly what the CLI printed.
    """

    def __init__(
        self,
        *,
        agent: AgentRuntime,
        result: CommandResult,
        reason_code: str = "AGENT_CLI_FAILED",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize adapter error metadata from a failed CLI execution."""
        self.agent = agent
        self.result = result
        self.reason_code = reason_code
        self.details = details or {}
        super().__init__(
            f"{agent.value} exited {result.returncode} ({reason_code}): "
            f"{result.stderr.strip() or result.stdout.strip() or '<no output>'}"
        )


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

    @property
    @abstractmethod
    def name(self) -> AgentRuntime:
        """Identity of the underlying agent runtime."""
        ...  # pragma: no cover

    @property
    def default_model(self) -> str | None:
        """Return the default model for this adapter."""
        return self._default_model

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

    async def run(
        self,
        *,
        compose_project: str,
        compose_file: Path,
        prompt: str,
        model: str | None = None,
        workspace_id: str | None = None,
        log_source: str = "agent",
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
        cli_args = self._cli_args(model=model)
        invocation = build_tracked_compose_exec(
            compose_project=compose_project,
            compose_file=compose_file,
            cli_args=cli_args,
            source=log_source,
            label=self.name.value,
            preserve_stdin=True,
        )
        args = invocation.args
        _log.info(
            "agent.run.start",
            agent=self.name.value,
            compose_project=compose_project,
            workspace_id=workspace_id,
            model=model or self._default_model,
            effort=self._default_effort,
            wall_timeout_seconds=self._agent_wall_timeout_seconds,
            idle_timeout_seconds=self._agent_idle_timeout_seconds,
            source=log_source,
            prompt_bytes=len(prompt_input),
        )
        # Wrap the agent run with optional usage sampling. The sampler captures a
        # baseline + periodic samples and is finalized in *every* exit path so the
        # final usage sample is recorded on success, failure/timeout, and
        # cancellation — never masking the agent outcome. _start_usage_sampling
        # stays *inside* the try: cancellation during baseline capture re-raises
        # CancelledError (a BaseException its own except-Exception guard can't
        # catch), so only the enclosing try/finally still reaches finalization.
        sampler_ctx: UsageSampleContext | None = None
        final_status = "failed"
        try:
            sampler_ctx = await self._start_usage_sampling(
                compose_project=compose_project,
                compose_file=compose_file,
                workspace_id=workspace_id,
            )
            result = await self._run_agent_cli(
                invocation=invocation,
                args=args,
                prompt_input=prompt_input,
                model=model,
                workspace_id=workspace_id,
                log_source=log_source,
                compose_project=compose_project,
            )
            final_status = "success"
            return result
        except AgentRunError as exc:
            final_status = (
                "timeout"
                if exc.reason_code in {"AGENT_TIMEOUT", "AGENT_IDLE_TIMEOUT"}
                else "failed"
            )
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
                agent=self.name.value,
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
                agent=self.name.value,
                workspace_id=workspace_id,
                phase="finalize",
                exc_info=True,
            )

    async def _run_agent_cli(
        self,
        *,
        invocation: Any,
        args: list[str],
        prompt_input: bytes,
        model: str | None,
        workspace_id: str | None,
        log_source: str,
        compose_project: str,
    ) -> AgentRunResult:
        # Stream the prompt on stdin and close it explicitly. This avoids OS
        # argv length limits for large review comments while still preventing
        # CLIs from waiting forever for inherited interactive input.
        sinks = None
        if self._log_store is not None and workspace_id is not None:
            sinks = await self._log_store.open_command_streams(
                workspace_id=workspace_id,
                base_stream_id=log_source,
                source=log_source,
                name=f"{log_source.capitalize()} ({self.name.value})"
                if log_source != "agent"
                else self.name.value,
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
                        agent=self.name.value,
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
            selected_model = model or self._default_model or "unknown"
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
                agent=self.name.value,
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
                    "model": recovery_metadata.get("model", selected_model),
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
            agent=self.name.value,
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


# ── Registry ──────────────────────────────────────────────────────────────

# Populated by awf.adapters.codex / .claude_code / .cursor / .gemini / .opencode on
# import. Keyed by AgentRuntime enum so callers that receive a Workspace.agent
# string just map through the enum.
_REGISTRY: dict[AgentRuntime, type[AgentAdapter]] = {}


def register_adapter(cls: type[AgentAdapter]) -> type[AgentAdapter]:
    """Class decorator used by each adapter module to self-register."""
    instance = cls.__new__(cls)  # bypass __init__ just to read .name
    # We can't call name via instance without runner; require subclasses to
    # override via a class-level _REGISTERED_NAME as well. Simpler: read
    # ``cls.runtime`` which subclasses set as a class attribute.
    runtime: AgentRuntime = getattr(cls, "runtime")  # noqa: B009 - structural check
    _REGISTRY[runtime] = cls
    del instance
    return cls


def get_adapter(
    runtime: AgentRuntime,
    *,
    runner: AsyncCommandRunner,
    default_model: str | None = None,
    default_effort: str | None = None,
    defaults: AgentDefaults | None = None,
    log_store: LogStore | None = None,
    agent_wall_timeout_seconds: float = DEFAULT_AGENT_WALL_TIMEOUT_SECONDS,
    agent_idle_timeout_seconds: float = DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS,
    usage_sampler: UsageSampler | None = None,
) -> AgentAdapter:
    """Instantiate the adapter for the given runtime.

    Raises ``KeyError`` if no adapter is registered — can happen only if a
    subclass forgot to import. Tests verify the registry is populated.
    """
    cls = _REGISTRY[runtime]
    if defaults is not None:
        default_model = defaults.model
        default_effort = defaults.effort
    return cls(
        runner=runner,
        default_model=default_model,
        default_effort=default_effort,
        log_store=log_store,
        agent_wall_timeout_seconds=agent_wall_timeout_seconds,
        agent_idle_timeout_seconds=agent_idle_timeout_seconds,
        usage_sampler=usage_sampler,
    )


def _failure_reason_for_result(result: CommandResult) -> str:
    """Normalize command timeout/provider failure reason codes for retries."""
    if result.reason_code == COMMAND_TIMEOUT_REASON:
        return "AGENT_TIMEOUT"
    if result.reason_code == COMMAND_IDLE_TIMEOUT_REASON:
        return "AGENT_IDLE_TIMEOUT"
    provider_failure = classify_provider_failure(
        reason_code=None,
        stdout=result.stdout,
        stderr=result.stderr,
        provider=None,
        model=None,
    )
    if provider_failure is not None:
        return provider_failure.reason_code
    return "AGENT_CLI_FAILED"
