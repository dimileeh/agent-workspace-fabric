"""Provisioner configuration and diagnostics protocol."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from awf.adapters.base import AgentDefaults
from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS
from awf.db.enums import AgentRuntime
from awf.node.compose_manager import DEFAULT_SERVICE_STARTUP_LOG_TAIL_LINES


class ServiceStartupDiagnosticsCapturer(Protocol):
    """Best-effort capturer of companion diagnostics on a service-startup failure.

    Consumer-side structural protocol: ``ComposeManager`` satisfies it without
    importing this module. The implementation must never raise and must return
    an already-redacted payload safe to persist into a ``WorkspaceEvent``.
    """

    async def capture_companion_diagnostics(
        self,
        *,
        project_name: str,
        workspace_id: str,
        tail_lines: int = ...,
    ) -> dict[str, Any]:
        """Return redacted diagnostics for unhealthy companions in a project."""
        ...


@dataclass(frozen=True)
class ProvisionerConfig:
    """Configuration the provisioner needs that isn't per-workspace state."""

    node_id: str
    """Identifier for the host running this provisioner (e.g. hostname)."""

    branch_prefix: str = "awf"
    """Prefix for feature branches; full branch = ``<prefix>/<workspace_id>``."""

    service_startup_log_tail_lines: int = DEFAULT_SERVICE_STARTUP_LOG_TAIL_LINES
    """How many companion log lines to capture on a service-startup failure (must be > 0)."""

    agent_defaults: Mapping[AgentRuntime, AgentDefaults] = DEFAULT_AGENT_DEFAULTS
    """Resolved agent defaults shared with the executor's runtime configuration."""

    def __post_init__(self) -> None:
        """Enforce the ``gt=0`` guard pydantic Settings applies on the env-var path.

        Direct callers (tests, other code) bypass ``Settings`` validation, so a
        zero/negative tail would otherwise reach ``docker logs --tail N`` and
        produce empty output (``--tail 0``) or a CLI error (``--tail -1``).
        """
        if self.service_startup_log_tail_lines <= 0:
            raise ValueError(
                "service_startup_log_tail_lines must be > 0, "
                f"got {self.service_startup_log_tail_lines}"
            )
