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
import os
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awf.adapters.provider_failures import classify_provider_failure
from awf.adapters.runtime_executor import (
    _HOSTED_TIMEOUT_REASONS,
    _HOSTED_TIMEOUT_RETURN_CODE,
    AgentRuntimeExecRequest,
    AgentRuntimeExecResult,
    AgentRuntimeExecutor,
    AgentRuntimeGitPreparation,
)
from awf.adapters.usage import (
    IsolatedUsageSampleContext,
    IsolatedUsageSampler,
    UsageSampleContext,
    UsageSampler,
)
from awf.common.commands import (
    COMMAND_IDLE_TIMEOUT_REASON,
    COMMAND_TIMEOUT_REASON,
    AsyncCommandRunner,
    CommandResult,
    StreamCallback,
)
from awf.common.compose_exec import (
    DEFAULT_AGENT_WORKDIR,
    TrackedComposeExec,
    TrackedIsolatedComposeRun,
    build_isolated_tracked_compose_run,
    build_tracked_compose_exec,
    cleanup_compose_exec_invocation,
    cleanup_compose_exec_invocation_after_cancellation,
)
from awf.common.logging import get_logger
from awf.db.enums import AgentRuntime
from awf.node.compose_manager import upgrade_persisted_clarification_service
from awf.profiles.compose import (
    agent_exec_env_passthrough,
    filter_hosted_env_passthrough_names,
    hosted_file_auth_mount_targets,
    hosted_github_token_passthrough_names,
    hosted_profile_env_passthrough_aliases,
    hosted_profile_env_passthrough_names,
    literal_profile_env_from_compose,
)
from awf.profiles.compose_postgres_env import try_compose_agent_env_and_postgres_passwords
from awf.profiles.models import WorkspaceProfile
from awf.runtime.logs import CommandLogSinks, LogStore

_log = get_logger(__name__)


DEFAULT_AGENT_WALL_TIMEOUT_SECONDS = 7200.0
"""Default maximum wall-clock duration for a single agent CLI run."""

DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS = 3600.0
"""Default maximum stdout/stderr silence for a single agent CLI run."""

_HOSTED_CANCEL_DRAIN_TIMEOUT_SECONDS = 1.0
"""Maximum time to wait for hosted executor cleanup after adapter timeout."""

_HOSTED_FILE_BACKED_ENV_ONLY_UNSUPPORTED_NAMES = frozenset(
    {
        # ADC is a filesystem path whose local Compose contract includes an auth
        # bind mount. Hosted requests currently carry env values only, so profile
        # passthrough must not re-add the name after adapters omit it.
        "GOOGLE_APPLICATION_CREDENTIALS",
    }
)


def _restore_compose_file(*, compose_file: Path, contents: bytes) -> None:
    """Atomically restore a Compose file after its sidecar update fails."""
    temporary_path: Path | None = None
    try:
        file_mode = compose_file.stat().st_mode & 0o777
        fd, raw_temporary_path = tempfile.mkstemp(
            prefix=f".{compose_file.name}.", suffix=".tmp", dir=compose_file.parent
        )
        temporary_path = Path(raw_temporary_path)
        with os.fdopen(fd, "wb") as temporary_file:
            temporary_file.write(contents)
        temporary_path.chmod(file_mode)
        temporary_path.replace(compose_file)
    finally:
        if temporary_path is not None:
            with contextlib.suppress(OSError):
                temporary_path.unlink()


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
6. **Keep changes minimal and scoped.** Fix only what THIS task asks.
   Do not refactor, rename, or restructure unrelated code, split files,
   or introduce new abstractions unless the fix strictly requires it. A
   reviewer should see a small, obviously-correct diff; sprawling diffs
   that touch dozens of unrelated files get rejected and waste the run.
7. **Cover what you change; never pad coverage.** AWF enforces a hard
   coverage gate after you finish. As you implement, add or adjust focused
   tests for each behavior you add or change (test-first when practical),
   and reason about which new lines and branches need a test so total
   coverage does not drop — you do not run the full gate yourself. For
   genuinely non-behavioral, unreachable, or type-only code (e.g. a
   Protocol stub or an unexecutable defensive branch), add a justified
   coverage exclusion instead of a hollow test. A test that only executes
   a line to move the number is coverage-theater and gets rejected.

---

"""


def _discard_hosted_execute_task_result(task: asyncio.Task[AgentRuntimeExecResult]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


def _prepend_missing_streamed_output(*, chunks: list[str], buffered: str) -> str:
    streamed = "".join(chunks)
    if not streamed:
        return buffered
    if buffered.startswith(streamed):
        return buffered
    if streamed.startswith(buffered):
        return streamed
    return streamed + buffered


def _buffered_output_not_streamed(*, chunks: list[str], buffered: str) -> str:
    if not buffered:
        return ""
    streamed = "".join(chunks)
    if not streamed:
        return buffered
    if buffered.startswith(streamed):
        return buffered[len(streamed) :]
    if streamed.startswith(buffered):
        return ""
    return buffered


def _hosted_identity_str(identity: dict[str, Any] | None, key: str) -> str | None:
    if identity is None:
        return None
    value = identity.get(key)
    return value if isinstance(value, str) and value else None


def _hosted_identity_int(identity: dict[str, Any] | None, key: str) -> int | None:
    if identity is None:
        return None
    value = identity.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _hosted_identity_str_tuple(identity: dict[str, Any] | None, key: str) -> tuple[str, ...]:
    if identity is None:
        return ()
    value = identity.get(key)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


@dataclass(frozen=True)
class AgentRunResult:
    """Structured result of one coding-CLI run."""

    returncode: int
    stdout: str
    stderr: str
    terminal_head_sha: str | None = None

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
    def name(self) -> AgentRuntime:
        """Identity of the underlying agent runtime."""
        ...  # pragma: no cover

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

    def _isolated_cli_args(self, *, model: str | None) -> list[str]:
        """Return CLI arguments for a disposable isolated worktree run."""
        return self._cli_args(model=model)

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
        isolated_worktree_host_path: Path | None = None,
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
        cli_args = (
            self._isolated_cli_args(model=model)
            if isolated_worktree_host_path is not None
            else self._cli_args(model=model)
        )
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
        if isolated_worktree_host_path is not None and compose_file.exists():
            # Existing PR monitors deliberately reuse their persisted Compose
            # definition. Upgrade that file just before the isolated re-ask so
            # stacks launched before the clarification service existed can
            # still run the follow-up without re-rendering profile state.
            original_compose_file = await asyncio.to_thread(compose_file.read_bytes)
            clarification_model_services = await asyncio.to_thread(
                upgrade_persisted_clarification_service,
                compose_file=compose_file,
                workspace_id=workspace_id or compose_project.removeprefix("awf_"),
                agent_runtime=self.name,
                agent_model=selected_model,
            )
            if clarification_model_services:
                # ``docker compose run --no-deps`` below starts only the
                # clarification container. Recreate the selected legacy model
                # services first so Docker applies the newly-rendered dedicated
                # model network to their already-running containers.
                model_service_update = await self._runner.run(
                    [
                        "docker",
                        "compose",
                        "-p",
                        compose_project,
                        "-f",
                        str(compose_file),
                        "up",
                        "-d",
                        "--no-deps",
                        "--force-recreate",
                        "--wait",
                        *clarification_model_services,
                    ]
                )
                if not model_service_update.ok:
                    # The sidecars did not receive the new network, so roll
                    # back the persisted migration. A later re-ask can then
                    # perform the migration again instead of running against
                    # a clarification network with no reachable model.
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(
                            _restore_compose_file,
                            compose_file=compose_file,
                            contents=original_compose_file,
                        )
                    raise AgentRunError(
                        agent=self.name,
                        result=model_service_update,
                        reason_code="CLARIFICATION_MODEL_SERVICE_UPDATE_FAILED",
                        details={"services": clarification_model_services},
                    )
        # ``agent_exec_env_passthrough`` reads + YAML-parses the compose file
        # synchronously; run it in a worker thread so the blocking I/O never
        # stalls the event loop when concurrent agent runs overlap.
        env_passthrough = await asyncio.to_thread(
            agent_exec_env_passthrough, compose_file=compose_file
        )
        isolated_sampler_ctx: IsolatedUsageSampleContext | None = None
        if isolated_worktree_host_path is not None:
            isolated_sampler_ctx = await self._start_isolated_usage_sampling(
                compose_project=compose_project,
                compose_file=compose_file,
                workspace_id=workspace_id,
                cli_args=cli_args,
            )
            if isolated_sampler_ctx is not None:
                cli_args = isolated_sampler_ctx.cli_args
        # Wrap the agent run with optional usage sampling. Ordinary sampling
        # captures a baseline + periodic samples; isolated clarification sampling
        # instead prepares an in-container capture before the invocation is built.
        # Either context is finalized in *every* exit path, so the final usage
        # sample is recorded on success, failure/timeout, and cancellation — never
        # masking the agent outcome. _start_usage_sampling stays *inside* the try:
        # cancellation during baseline capture re-raises CancelledError (a
        # BaseException its own except-Exception guard can't catch), so only the
        # enclosing try/finally still reaches finalization.
        sampler_ctx: UsageSampleContext | None = isolated_sampler_ctx
        final_status = "failed"
        try:
            invocation: TrackedComposeExec | TrackedIsolatedComposeRun
            if isolated_worktree_host_path is None:
                invocation = build_tracked_compose_exec(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    cli_args=cli_args,
                    source=log_source,
                    label=self.name.value,
                    workdir=workdir,
                    preserve_stdin=True,
                    env_passthrough=env_passthrough,
                )
            else:
                invocation = build_isolated_tracked_compose_run(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    cli_args=cli_args,
                    source=log_source,
                    label=self.name.value,
                    worktree_host_path=isolated_worktree_host_path,
                    preserve_stdin=True,
                    extra_volume_binds=(
                        () if isolated_sampler_ctx is None else isolated_sampler_ctx.volume_binds
                    ),
                )
            args = invocation.args
            _log.info(
                "agent.run.start",
                agent=self.name.value,
                compose_project=compose_project,
                workspace_id=workspace_id,
                model=selected_model,
                effort=self._default_effort,
                wall_timeout_seconds=self._agent_wall_timeout_seconds,
                idle_timeout_seconds=self._agent_idle_timeout_seconds,
                source=log_source,
                prompt_bytes=len(prompt_input),
            )
            if sampler_ctx is None:
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
                agent=self.name.value,
                workspace_id=workspace_id,
                phase="start",
                exc_info=True,
            )
            return None

    async def _start_isolated_usage_sampling(
        self,
        *,
        compose_project: str,
        compose_file: Path,
        workspace_id: str | None,
        cli_args: list[str],
    ) -> IsolatedUsageSampleContext | None:
        sampler = self._usage_sampler
        if sampler is None or workspace_id is None or not isinstance(sampler, IsolatedUsageSampler):
            return None
        start_task = asyncio.create_task(
            sampler.start_isolated(
                compose_project=compose_project,
                compose_file=compose_file,
                workspace_id=workspace_id,
                provider=self.name,
                cli_args=cli_args,
            )
        )
        try:
            return await asyncio.shield(start_task)
        except asyncio.CancelledError:
            # ``start_isolated`` creates its capture directory in a worker
            # thread. Keep the task alive through cancellation so a context
            # returned after that worker finishes can remove the directory.
            while not start_task.done():
                with contextlib.suppress(BaseException):
                    await asyncio.shield(start_task)
            try:
                sampler_ctx = start_task.result()
            except (Exception, asyncio.CancelledError):
                pass
            else:
                finalize_task = asyncio.create_task(
                    self._finalize_usage_sampling(
                        sampler_ctx, status="cancelled", workspace_id=workspace_id
                    )
                )
                while not finalize_task.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.shield(finalize_task)
            raise
        except Exception:
            _log.warning(
                "usage.collect.error",
                agent=self.name.value,
                workspace_id=workspace_id,
                phase="start_isolated",
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
        """Delegate agent CLI execution to the injected runtime executor.

        The hosted path replaces the tracked ``docker compose exec`` invocation
        with a cloud-neutral executor call. The same prompt-via-stdin contract,
        usage sampling, log-store streaming, and provider-failure
        classification apply so monitor decision semantics are unchanged. The
        hosted executor owns its own process cleanup; this path does NOT run
        the compose cleanup helpers.

        Log-store streaming: when sinks are available, ``on_stdout`` /
        ``on_stderr`` callbacks are passed into the request so a streaming
        executor writes chunks to the log store *during* execution — mirroring
        the Compose ``run_streaming`` path — so a long-running hosted run
        (e.g. a monitor repair that idles until its watchdog fires) no longer
        leaves the log stream empty until completion. When the executor does
        not stream, the buffered ``AgentRuntimeExecResult`` stdout/stderr is
        written to the sinks after ``execute()`` returns, preserving the prior
        buffered-output contract for non-streaming executors.
        """
        runtime_executor = self._runtime_executor
        assert runtime_executor is not None  # guarded by run() dispatch
        # Parse the rendered compose file once in a worker thread. The helpers
        # below can then reuse the immutable agent env map, avoiding duplicate
        # synchronous YAML reads while preserving the postgres-password
        # redaction context needed by ``literal_profile_env_from_compose``.
        compose_env, postgres_passwords = await asyncio.to_thread(
            try_compose_agent_env_and_postgres_passwords,
            compose_file,
            worker_env=os.environ,
        )
        env_passthrough_names: tuple[str, ...]
        env_passthrough_aliases: tuple[tuple[str, str], ...]
        profile_env: tuple[tuple[str, str], ...]
        file_auth_mount_targets: tuple[str, ...]
        if compose_env is None:
            env_passthrough_names = ()
            env_passthrough_aliases = ()
            profile_env = ()
            file_auth_mount_targets = ()
        else:
            env_passthrough_names = await asyncio.to_thread(
                filter_hosted_env_passthrough_names,
                self.hosted_env_passthrough_names,
                compose_file=compose_file,
                compose_env=compose_env,
            )
            profile_env_passthrough_names = await asyncio.to_thread(
                hosted_profile_env_passthrough_names,
                compose_file,
                compose_env=compose_env,
            )
            env_passthrough_aliases = await asyncio.to_thread(
                hosted_profile_env_passthrough_aliases,
                compose_file,
                compose_env=compose_env,
            )
            if profile_env_passthrough_names:
                # Include non-adapter profile env secrets that local Compose
                # resolved at stack launch (e.g. ``NPM_TOKEN: ${NPM_TOKEN}``). The
                # helper returns names only; worker-resolved values still stay out
                # of ``profile_env`` and the request payload.
                existing_names = set(env_passthrough_names)
                env_passthrough_names = env_passthrough_names + tuple(
                    name
                    for name in profile_env_passthrough_names
                    if name not in existing_names
                    and name not in _HOSTED_FILE_BACKED_ENV_ONLY_UNSUPPORTED_NAMES
                )
            # Surface GitHub token names only when they are not already represented
            # by alias mappings. Hosted executors resolve plain names by their own
            # name, while aliases preserve the source->target mapping needed when
            # the worker only has ``AWF_GITHUB_TOKEN``.
            github_token_names = await asyncio.to_thread(
                hosted_github_token_passthrough_names,
                compose_file,
                compose_env=compose_env,
            )
            if github_token_names:
                # Union after the filter: the filter excludes compose-declared
                # profile-owned slots, and the helper already skips profile-owned
                # aliases. De-duplicate preserving filter order while keeping alias
                # targets and sources out of plain passthrough names.
                existing_names = set(env_passthrough_names)
                alias_targets = {target for target, _source in env_passthrough_aliases}
                alias_sources = {source for _target, source in env_passthrough_aliases}
                env_passthrough_names = env_passthrough_names + tuple(
                    name
                    for name in github_token_names
                    if name not in existing_names
                    and name not in alias_targets
                    and name not in alias_sources
                )
            # Carry profile-owned env values to the hosted executor. The local
            # ``docker compose exec`` path does not forward profile-owned env
            # because the running container already has it (substituted from the
            # compose env block at stack launch); the hosted path has no compose env
            # block, so without these values a profile-owned endpoint (e.g.
            # ``OLLAMA_HOST``) never reaches the hosted job and OpenCode falls back to
            # the default daemon. Compose interpolation is rendered against the
            # worker env so the hosted job receives the same concrete value the
            # local container gets at stack launch: a defaulted
            # ``${NAME:-default}`` with ``NAME`` unset is carried as the default, an
            # escaped ``$$`` is carried as a single ``$``, and a pure literal is
            # carried verbatim. Bare ``${NAME}`` / ``$NAME`` worker-resolved slots are
            # skipped (the profile owns them locally; the hosted path resolves
            # credentials via its own adapter contract, not from the worker).
            profile_env = await asyncio.to_thread(
                literal_profile_env_from_compose,
                compose_file,
                compose_env=compose_env,
                postgres_passwords=postgres_passwords,
            )
            file_auth_mount_targets = await asyncio.to_thread(
                hosted_file_auth_mount_targets,
                compose_file,
                compose_env=compose_env,
            )
        sampler_ctx: UsageSampleContext | None = None
        final_status = "failed"
        sinks = await self._open_command_streams(workspace_id=workspace_id, log_source=log_source)
        # Streaming callbacks: buffer stdout/stderr chunks so adapter watchdog
        # timeouts can still report partial output, and when sinks are available
        # forward chunks to the log store *during* execution (mirroring the
        # Compose ``run_streaming`` path). When the executor does not stream,
        # the buffered ``AgentRuntimeExecResult`` is written to the sinks after
        # ``execute()`` returns instead — see the post-execute write guard
        # below. Either way the log store ends up with the run's output; the
        # streaming path additionally fills it live, so a long-running hosted
        # monitor repair no longer leaves the log stream empty until completion.
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

        request = AgentRuntimeExecRequest(
            workspace_id=workspace_id,
            agent_runtime=self.name,
            cli_args=tuple(cli_args),
            prompt_stdin=prompt_input,
            log_source=log_source,
            model=selected_model,
            effort=self._default_effort,
            env_passthrough_names=env_passthrough_names,
            env_passthrough_aliases=env_passthrough_aliases,
            file_auth_mount_targets=file_auth_mount_targets,
            profile_env=profile_env,
            profile=profile,
            compose_project=compose_project,
            compose_file=compose_file,
            worktree_path=worktree_path,
            wall_timeout_seconds=self._agent_wall_timeout_seconds,
            idle_timeout_seconds=self._agent_idle_timeout_seconds,
            repo_url=_hosted_identity_str(hosted_pr_identity, "repo_url"),
            pr_url=_hosted_identity_str(hosted_pr_identity, "pr_url"),
            pr_number=_hosted_identity_int(hosted_pr_identity, "pr_number"),
            base_ref=_hosted_identity_str(hosted_pr_identity, "base_ref"),
            head_ref=_hosted_identity_str(hosted_pr_identity, "head_ref"),
            head_repo_url=_hosted_identity_str(hosted_pr_identity, "head_repo_url"),
            head_repo_slug=_hosted_identity_str(hosted_pr_identity, "head_repo_slug"),
            owned_paths=_hosted_identity_str_tuple(hosted_pr_identity, "owned_paths"),
            expected_head_sha=_hosted_identity_str(hosted_pr_identity, "expected_head_sha"),
            git_preparation=git_preparation,
            on_stdout=on_stdout_cb,
            on_stderr=on_stderr_cb,
        )
        _log.info(
            "agent.run.hosted.start",
            agent=self.name.value,
            workspace_id=workspace_id,
            model=selected_model,
            effort=self._default_effort,
            wall_timeout_seconds=self._agent_wall_timeout_seconds,
            idle_timeout_seconds=self._agent_idle_timeout_seconds,
            source=log_source,
            prompt_bytes=len(prompt_input),
            env_passthrough_names=list(env_passthrough_names),
            env_passthrough_aliases=[
                {"target": target, "source": source} for target, source in env_passthrough_aliases
            ],
            file_auth_mount_targets=list(file_auth_mount_targets),
            # Log profile_env *keys* only — values are literal profile config
            # (e.g. an Ollama daemon URL) but never secret placeholders; still,
            # a value could be sensitive config, so do not log values.
            profile_env_keys=[key for key, _ in profile_env],
        )
        try:
            if self._usage_sampler is not None:
                _log.info(
                    "agent.run.hosted.usage_sampling_skipped",
                    agent=self.name.value,
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
                # Buffered fallback: write only the hosted executor output not
                # already streamed during execution.
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
        """Open log-store command sinks for a run, or ``None`` when unavailable.

        Shared by the Compose and hosted execution paths so both stream to the
        same log-store sink with identical naming.
        """
        if self._log_store is None or workspace_id is None:
            return None
        return await self._log_store.open_command_streams(
            workspace_id=workspace_id,
            base_stream_id=log_source,
            source=log_source,
            name=f"{log_source.capitalize()} ({self.name.value})"
            if log_source != "agent"
            else self.name.value,
        )

    def _final_status_for_exception(self, exc: AgentRunError) -> str:
        """Map an agent-run exception to a usage-sampling final status.

        Shared by ``run()`` and ``_run_hosted`` so the ``AgentRunError`` →
        ``final_status`` mapping stays identical across execution paths. The
        ``asyncio.CancelledError`` case is handled inline by each caller's own
        ``except asyncio.CancelledError`` handler (``final_status = "cancelled"``),
        so this helper only ever receives an ``AgentRunError``.
        """
        return "timeout" if exc.reason_code in {"AGENT_TIMEOUT", "AGENT_IDLE_TIMEOUT"} else "failed"

    def _classify_hosted_result(
        self,
        *,
        hosted_result: AgentRuntimeExecResult,
        model: str | None,
        workspace_id: str | None,
    ) -> AgentRunResult:
        """Map a hosted executor result through the same failure classification.

        Reuses ``_failure_reason_for_result`` so non-zero exits, timeouts, and
        provider auth failures classify identically to the Compose path —
        monitor recovery semantics stay unchanged. The hosted executor signals
        a timeout by returning ``returncode == 124`` (the conventional
        ``timeout(1)`` exit) and carries the wall-vs-idle distinction on
        ``AgentRuntimeExecResult.timeout_reason``; an idle timeout maps to
        ``AGENT_IDLE_TIMEOUT`` while a wall-clock timeout maps to
        ``AGENT_TIMEOUT``. A ``124`` without an explicit, valid
        ``timeout_reason`` is NOT classified as a timeout — it falls through to
        ordinary CLI-failure classification, mirroring the Compose path where
        only a watchdog-set ``reason_code`` yields ``AGENT_TIMEOUT``.
        """
        # Only classify a hosted ``124`` as a timeout when the executor
        # signals an explicit, valid timeout reason. A ``124`` without such a
        # reason is treated as an ordinary CLI failure instead of being forced
        # into ``COMMAND_TIMEOUT``.
        timeout_reason = (
            hosted_result.timeout_reason
            if hosted_result.returncode == _HOSTED_TIMEOUT_RETURN_CODE
            and hosted_result.timeout_reason in _HOSTED_TIMEOUT_REASONS
            else None
        )
        command_result = CommandResult(
            returncode=hosted_result.returncode,
            stdout=hosted_result.stdout,
            stderr=hosted_result.stderr,
            reason_code=timeout_reason,
        )
        if command_result.ok:
            _log.info(
                "agent.run.hosted.ok",
                agent=self.name.value,
                workspace_id=workspace_id,
                stdout_bytes=len(command_result.stdout),
                stderr_bytes=len(command_result.stderr),
            )
            return AgentRunResult(
                returncode=command_result.returncode,
                stdout=command_result.stdout,
                stderr=command_result.stderr,
                terminal_head_sha=hosted_result.terminal_head_sha,
            )
        provider = self.get_provider(model)
        selected_model = self._selected_model_for_run(model=model)
        reported_model = selected_model or "unknown"
        base_reason = _failure_reason_for_result(command_result)
        provider_failure = classify_provider_failure(
            reason_code=base_reason,
            stdout=command_result.stdout,
            stderr=command_result.stderr,
            provider=provider,
            model=selected_model,
        )
        reason_code = provider_failure.reason_code if provider_failure is not None else base_reason
        log_event = (
            "agent.run.hosted.timeout"
            if reason_code in {"AGENT_TIMEOUT", "AGENT_IDLE_TIMEOUT"}
            else "agent.run.hosted.failed"
        )
        _log.warning(
            log_event,
            agent=self.name.value,
            workspace_id=workspace_id,
            returncode=command_result.returncode,
            reason_code=reason_code,
            stdout_bytes=len(command_result.stdout),
            stderr_bytes=len(command_result.stderr),
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
        if hosted_result.terminal_head_sha:
            if details is None:
                details = {}
            details["terminal_head_sha"] = hosted_result.terminal_head_sha
        raise AgentRunError(
            agent=self.name,
            result=command_result,
            reason_code=reason_code,
            details=details,
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
        sinks = await self._open_command_streams(workspace_id=workspace_id, log_source=log_source)

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

# Populated by awf.adapters.codex / .claude_code / .cursor / .gemini / .opencode / .grok on
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
    runtime_executor: AgentRuntimeExecutor | None = None,
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
        runtime_executor=runtime_executor,
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
