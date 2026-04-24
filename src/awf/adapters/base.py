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

from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.common.logging import get_logger
from awf.db.enums import AgentRuntime

_log = get_logger(__name__)


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


class AgentAdapter(ABC):
    """Shared scaffolding for coding-CLI adapters."""

    def __init__(
        self,
        *,
        runner: AsyncCommandRunner,
        default_model: str | None = None,
    ) -> None:
        self._runner = runner
        self._default_model = default_model

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
            model=model or self._default_model,
        )
        # Close stdin explicitly. Some CLIs (Codex in particular) read
        # "additional input" from stdin after argv parsing; if AWF is
        # launched from an interactive terminal, inheriting that open
        # stream makes the agent wait forever for EOF.
        result = await self._runner.run(args, input_bytes=b"")

        if not result.ok:
            raise AgentRunError(agent=self.name, result=result)

        _log.info(
            "agent.run.ok",
            agent=self.name.value,
            compose_project=compose_project,
            stdout_bytes=len(result.stdout),
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
) -> AgentAdapter:
    """Instantiate the adapter for the given runtime.

    Raises ``KeyError`` if no adapter is registered — can happen only if a
    subclass forgot to import. Tests verify the registry is populated.
    """
    cls = _REGISTRY[runtime]
    return cls(runner=runner, default_model=default_model)
