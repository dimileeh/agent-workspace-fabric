"""Usage-sampling protocols shared by the agent adapter and collectors.

This is a leaf module: it depends only on stdlib, ``awf.common``, and
``awf.db.enums`` so the agent adapter (``awf.adapters.base``) can accept an
optional usage sampler without importing any ``awf.service`` code (which would
create an import cycle, since the service layer imports adapters). The concrete
implementation lives in ``awf.service.usage_collection``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from awf.common.compose_exec import TrackedIsolatedComposeRun
from awf.db.enums import AgentRuntime


class UsageSampleContext(Protocol):
    """Handle for a single agent run's in-progress usage sampling."""

    async def finalize(  # pragma: no cover - Protocol method declaration only.
        self, *, status: str
    ) -> None:
        """Stop periodic sampling and record one final usage sample.

        ``status`` is the agent run outcome (``"success"``/``"failed"``/
        ``"timeout"``/``"cancelled"``). Implementations must never raise — the
        agent outcome must not be masked by usage-collection errors.
        """


class UsageSampler(Protocol):
    """Starts usage sampling for an agent run inside its workspace container."""

    async def start(  # pragma: no cover - Protocol method declaration only.
        self,
        *,
        compose_project: str,
        compose_file: Path,
        workspace_id: str,
        provider: AgentRuntime | str,
    ) -> UsageSampleContext: ...


class IsolatedUsageSampleContext(UsageSampleContext, Protocol):
    """Usage capture configuration for a disposable clarification container."""

    async def capture_baseline_before_agent(self, *, invocation: TrackedIsolatedComposeRun) -> None:
        """Capture the baseline before the agent CLI watchdog starts."""

    async def capture_final_before_cleanup(self, *, container_name: str) -> None:
        """Capture usage while the disposable container is still running."""

    @property
    def baseline_cli_args(self) -> list[str] | None:
        """Return a standalone baseline probe command when capture is available."""

    @property
    def cli_args(self) -> list[str]:
        """Return the configured agent command."""

    @property
    def volume_binds(self) -> tuple[tuple[Path, str], ...]:
        """Return optional additional host paths for the isolated invocation."""


@runtime_checkable
class IsolatedUsageSampler(Protocol):
    """Optionally samples usage from an isolated one-off container."""

    async def start_isolated(  # pragma: no cover - Protocol method declaration only.
        self,
        *,
        compose_project: str,
        compose_file: Path,
        workspace_id: str,
        provider: AgentRuntime | str,
        cli_args: list[str],
    ) -> IsolatedUsageSampleContext:
        """Start usage collection for a disposable agent invocation."""
        ...
