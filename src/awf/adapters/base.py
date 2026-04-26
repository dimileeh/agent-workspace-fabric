"""Base adapter — shared scaffolding for coding-CLI subclasses.

Each adapter turns a prompt into a ``docker compose exec`` invocation against
the workspace's agent container. The base class owns the docker-compose part;
subclasses only decide which CLI flags to use.

The adapter does NOT handle commits, pushes, or PR creation — that's Task 7.
It just runs the CLI, captures stdout/stderr, and returns a structured result.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from awf.common.commands import (
    COMMAND_IDLE_TIMEOUT_REASON,
    COMMAND_TIMEOUT_REASON,
    AsyncCommandRunner,
    CommandResult,
)
from awf.common.logging import get_logger
from awf.db.enums import AgentRuntime
from awf.runtime.logs import LogStore

_log = get_logger(__name__)


DEFAULT_AGENT_WALL_TIMEOUT_SECONDS = 7200.0
"""Default maximum wall-clock duration for a single agent CLI run."""

DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS = 900.0
"""Default maximum stdout/stderr silence for a single agent CLI run."""


_AUTH_FAILURE_MARKERS = (
    "not logged in",
    "please run /login",
    "please set an auth method",
    "manual authorization is required",
    "could not authenticate",
    "error authenticating",
    "invalid_grant",
    "anthropic_api_key",
    "gemini_api_key",
    "google_api_key",
    "google_genai_use_vertexai",
    "google_genai_use_gca",
)


# Prepended to every agent prompt. Encodes contract invariants the
# agent must honour inside an AWF workspace. Kept short — most agent
# CLIs accept prompts as command-line args and some have length caps.
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
    ) -> None:
        self.agent = agent
        self.result = result
        self.reason_code = reason_code
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
    ) -> None:
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

    @property
    @abstractmethod
    def name(self) -> AgentRuntime: ...

    @abstractmethod
    def _cli_args(self, *, prompt: str, model: str | None) -> list[str]:
        """Return the CLI-specific argv (after ``agent`` service name).

        Example for Codex: ``["codex", "exec", "--model", model, prompt]``.
        """

    async def run(
        self,
        *,
        compose_project: str,
        compose_file: Path,
        prompt: str,
        model: str | None = None,
        workspace_id: str | None = None,
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
        cli_args = self._cli_args(prompt=wrapped_prompt, model=model or self._default_model)
        args = [
            "docker",
            "compose",
            "--project-name",
            compose_project,
            "--file",
            str(compose_file),
            "exec",
            "-T",  # no tty; we're not attached
            "-w",
            "/workspace",
            "agent",
            *cli_args,
        ]
        _log.info(
            "agent.run.start",
            agent=self.name.value,
            compose_project=compose_project,
            workspace_id=workspace_id,
            model=model or self._default_model,
            effort=self._default_effort,
            wall_timeout_seconds=self._agent_wall_timeout_seconds,
            idle_timeout_seconds=self._agent_idle_timeout_seconds,
        )
        # Close stdin explicitly. Some CLIs (Codex in particular) read
        # "additional input" from stdin after argv parsing; if AWF is
        # launched from an interactive terminal, inheriting that open
        # stream makes the agent wait forever for EOF.
        sinks = None
        if self._log_store is not None and workspace_id is not None:
            sinks = await self._log_store.open_command_streams(
                workspace_id=workspace_id,
                base_stream_id="agent",
                source="agent",
                name=self.name.value,
            )

        try:
            run_streaming = getattr(self._runner, "run_streaming", None)
            if run_streaming is not None:
                result = await run_streaming(
                    args,
                    input_bytes=b"",
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
                result = await self._runner.run(args, input_bytes=b"")
                if sinks is not None:
                    await sinks.write_stdout(result.stdout)
                    await sinks.write_stderr(result.stderr)
        finally:
            if sinks is not None:
                await sinks.close()

        if not result.ok:
            reason_code = _failure_reason_for_result(result)
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
            raise AgentRunError(
                agent=self.name,
                result=result,
                reason_code=reason_code,
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

# Populated by awf.adapters.codex / .claude_code / .gemini on import. Keyed by
# AgentRuntime enum so callers that receive a Workspace.agent string just map
# through the enum.
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
    )


def _failure_reason_for_result(result: CommandResult) -> str:
    if result.reason_code == COMMAND_TIMEOUT_REASON:
        return "AGENT_TIMEOUT"
    if result.reason_code == COMMAND_IDLE_TIMEOUT_REASON:
        return "AGENT_IDLE_TIMEOUT"
    output = f"{result.stderr}\n{result.stdout}".lower()
    if any(marker in output for marker in _AUTH_FAILURE_MARKERS):
        return "AGENT_AUTH_FAILED"
    return "AGENT_CLI_FAILED"
