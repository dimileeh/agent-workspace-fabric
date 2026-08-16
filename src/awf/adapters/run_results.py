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
