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

from dataclasses import dataclass, field
from typing import Protocol

from awf.common.commands import (
    COMMAND_IDLE_TIMEOUT_REASON,
    COMMAND_TIMEOUT_REASON,
    StreamCallback,
)
from awf.db.enums import AgentRuntime

#: The conventional ``timeout(1)`` exit code the hosted executor uses to signal
#: a watchdog termination, mirroring the Compose path.
_HOSTED_TIMEOUT_RETURN_CODE = 124

#: Reason codes a hosted executor may set on ``AgentRuntimeExecResult`` to
#: distinguish wall-clock vs idle timeouts. Other values are reserved; the
#: adapter treats any non-empty ``timeout_reason`` as authoritative.
_HOSTED_TIMEOUT_REASONS = frozenset({COMMAND_TIMEOUT_REASON, COMMAND_IDLE_TIMEOUT_REASON})


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

    Streaming contract: when ``on_stdout`` / ``on_stderr`` callbacks are
    supplied, implementations SHOULD invoke them with stdout/stderr chunks as
    they arrive so the log store fills *during* execution (mirroring the
    Compose ``run_streaming`` path). Implementations that do not stream may
    leave these ``None`` / unused and rely on the buffered
    ``AgentRuntimeExecResult`` returned from ``execute()`` — the adapter
    writes the buffered output to the sinks after ``execute()`` returns when
    and only when the corresponding callback was not invoked. Either way,
    secret values MUST NEVER be passed to the callbacks or persisted.
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
    on_stdout: StreamCallback | None = None
    on_stderr: StreamCallback | None = None


@dataclass(frozen=True)
class AgentRuntimeExecResult:
    """Result of one hosted agent CLI run.

    Implementations signal a watchdog termination with ``returncode == 124``
    (the conventional ``timeout(1)`` exit). Wall-clock and idle timeouts share
    that exit code on the Compose path but are separated via ``reason_code``;
    the hosted result carries the same distinction on the ``timeout_reason``
    field so the adapter can map idle timeouts to ``AGENT_IDLE_TIMEOUT``
    instead of collapsing every 124 into a wall-clock timeout.
    ``timeout_reason`` defaults to wall-clock (``COMMAND_TIMEOUT_REASON``)
    when unset to preserve the pre-existing contract for executors that only
    set ``returncode``.
    """

    returncode: int
    stdout: str
    stderr: str
    timeout_reason: str = field(default=COMMAND_TIMEOUT_REASON)


class AgentRuntimeExecutor(Protocol):
    """Cloud-neutral execution backend for agent CLI runs.

    Default absence preserves the exact tracked ``docker compose exec`` path.
    A hosted AWF Cloud deployment implements this with Kubernetes Jobs.
    Core ships no Kubernetes implementation. Implementations MUST stream the
    prompt via stdin/context, never argv, and MUST NOT log or persist secret
    values (resolve them out-of-band from ``env_passthrough_names``).

    Streaming contract: when the caller supplies ``request.on_stdout`` /
    ``request.on_stderr`` callbacks, implementations SHOULD invoke them with
    stdout/stderr chunks as they arrive so operators and monitor diagnostics
    see live output and last-progress evidence — not just buffered output
    after ``execute()`` returns. An implementation that only returns buffered
    output is still supported (the adapter writes the buffered output to the
    sinks after ``execute()`` when the matching callback was not used), but a
    long-running hosted run that does not stream leaves the log stream empty
    during execution, which loses live output for long monitor repairs.
    """

    async def execute(
        self, request: AgentRuntimeExecRequest
    ) -> AgentRuntimeExecResult: ...  # pragma: no cover
