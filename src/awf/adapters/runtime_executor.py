"""Cloud-neutral agent runtime execution seam.

Default absence preserves the exact tracked ``docker compose exec`` path in
:mod:`awf.adapters.base`. A hosted AWF Cloud deployment injects an
``AgentRuntimeExecutor`` implementation (e.g. one that launches Kubernetes
Jobs) so PR monitor repair is not hard-wired to Docker Compose.

Core ships NO Kubernetes implementation here — only the cloud-neutral seam.
The seam carries structured, secret-free context so a hosted runner can
launch the same coding CLI against a prepared workspace checkout without
Docker Compose.

Secret *names* may be passed as env-passthrough intent (e.g.
``CODEX_API_KEY``); secret *values* are NEVER included — the hosted runtime
resolves them out-of-band. Implementations MUST stream the prompt via stdin
(``prompt_stdin``), never argv, and MUST NOT log or persist secret values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from awf.db.enums import AgentRuntime


@dataclass(frozen=True)
class AgentRuntimeExecRequest:
    """Secret-free structured context for one agent CLI run.

    Carries everything a non-compose runner needs to launch the same CLI
    against a prepared workspace checkout. Secret *names* may be passed as
    env-passthrough intent (e.g. ``CODEX_API_KEY``); secret *values* are
    NEVER included — the hosted runtime resolves them out-of-band from
    ``env_passthrough_names``.

    Hosted Codex contract: ``env_passthrough_names`` should include
    ``CODEX_API_KEY`` so a hosted executor can resolve and inject it.
    ``OPENAI_API_KEY`` may remain a *source* credential in deployment
    systems, but ``codex exec`` itself must not require a workstation
    ``~/.codex`` directory in hosted mode.
    """

    workspace_id: str | None
    agent_runtime: AgentRuntime
    cli_args: tuple[str, ...]
    prompt_stdin: bytes
    log_source: str
    model: str | None
    effort: str | None
    env_passthrough_names: tuple[str, ...] = ()
    wall_timeout_seconds: float | None = None
    idle_timeout_seconds: float | None = None


@dataclass(frozen=True)
class AgentRuntimeExecResult:
    """Result of one hosted agent CLI run."""

    returncode: int
    stdout: str
    stderr: str


class AgentRuntimeExecutor(Protocol):
    """Cloud-neutral execution backend for agent CLI runs.

    Default absence preserves the exact tracked ``docker compose exec`` path.
    A hosted AWF Cloud deployment implements this with Kubernetes Jobs.
    Core ships no Kubernetes implementation. Implementations MUST stream the
    prompt via stdin/context, never argv, and MUST NOT log or persist secret
    values (resolve them out-of-band from ``env_passthrough_names``).
    """

    async def execute(
        self, request: AgentRuntimeExecRequest
    ) -> AgentRuntimeExecResult: ...  # pragma: no cover
