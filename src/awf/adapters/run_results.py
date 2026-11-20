"""Result and failure values shared by coding-agent adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from awf.common.commands import CommandResult
from awf.db.enums import AgentRuntime


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
    """Raised when the coding CLI exits non-zero with its command result."""

    def __init__(
        self,
        *,
        agent: AgentRuntime | str,
        result: CommandResult,
        reason_code: str = "AGENT_CLI_FAILED",
        details: dict[str, Any] | None = None,
        agent_stdout: str | None = None,
    ) -> None:
        """Initialize adapter error metadata from a failed CLI execution.

        ``agent_stdout`` is optional preserved agent CLI stdout when a later
        failure (e.g. hosted terminal-head sync) replaces ``result.stdout`` with
        non-agent output. Callers that parse ``result.stdout`` keep doing so;
        only the CI-repair conflict check reads ``agent_stdout``.
        """
        self.agent = agent
        self.result = result
        self.reason_code = reason_code
        self.details = details or {}
        self.agent_stdout = agent_stdout
        agent_str = agent.value if isinstance(agent, AgentRuntime) else str(agent)
        super().__init__(
            f"{agent_str} exited {result.returncode} ({reason_code}): "
            f"{result.stderr.strip() or result.stdout.strip() or '<no output>'}"
        )
