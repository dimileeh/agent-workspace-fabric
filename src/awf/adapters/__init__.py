"""Coding-CLI adapters.

AWF launches a coding CLI (Codex, Claude Code, or Gemini) inside the agent
container to do the actual code-writing work. Each adapter wraps one CLI and
produces the right ``docker compose exec`` command for it.

The shared base class handles the common scaffolding (exec -T -w /workspace,
error wrapping, structured result); each CLI-specific subclass only defines
``name`` and ``_cli_args``.
"""

from awf.adapters.base import (
    AgentAdapter,
    AgentRunError,
    AgentRunResult,
    get_adapter,
)

__all__ = [
    "AgentAdapter",
    "AgentRunError",
    "AgentRunResult",
    "get_adapter",
]
