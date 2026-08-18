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
``CODEX_API_KEY``); cross-name env aliases may be passed as source/target
names; file-backed auth may be represented by container mount targets; secret
*values* and host source paths are NEVER included — the hosted runtime resolves
them out-of-band. Implementations MUST stream the prompt via stdin
(``prompt_stdin``), never argv, and MUST NOT log or persist secret values.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from awf.common.commands import (
    COMMAND_IDLE_TIMEOUT_REASON,
    COMMAND_TIMEOUT_REASON,
    StreamCallback,
)
from awf.db.enums import AgentRuntime

if TYPE_CHECKING:
    from awf.profiles.models import WorkspaceProfile

#: The conventional ``timeout(1)`` exit code the hosted executor uses to signal
#: a watchdog termination, mirroring the Compose path.
_HOSTED_TIMEOUT_RETURN_CODE = 124

#: Reason codes a hosted executor may set on ``AgentRuntimeExecResult`` to
#: distinguish wall-clock vs idle timeouts. Other values are reserved; the
#: adapter treats any non-empty ``timeout_reason`` as authoritative.
_HOSTED_TIMEOUT_REASONS = frozenset({COMMAND_TIMEOUT_REASON, COMMAND_IDLE_TIMEOUT_REASON})


@dataclass(frozen=True)
class AgentRuntimeGitPreparation:
    """Explicit, secret-free checkout preparation for a hosted agent run.

    Normal runs omit this value. ``merge_base`` asks the hosted runtime to
    merge the trusted base ref pinned to the exact 40-character lowercase SHA
    Core already fetched.
    """

    mode: Literal["merge_base"]
    base_ref: str
    expected_base_sha: str


@dataclass(frozen=True)
class AgentRuntimeExecRequest:
    """Secret-free structured context for one agent CLI run.

    Carries everything a non-compose runner needs to launch the same CLI
    against a prepared workspace checkout. Secret *names* may be passed as
    env-passthrough intent (e.g. ``CODEX_API_KEY``); secret *values* are
    NEVER included — the hosted runtime resolves them out-of-band from
    ``env_passthrough_names``. ``env_passthrough_aliases`` carries
    ``(target_name, source_name)`` pairs for Compose placeholders such as
    ``TARGET: ${SOURCE}``; hosted executors resolve ``source_name`` out-of-band
    and inject it into the job as ``target_name``.

    Hosted Codex contract: ``env_passthrough_names`` should include
    ``CODEX_API_KEY`` so a hosted executor can resolve and inject it.
    ``OPENAI_API_KEY`` may remain a *source* credential in deployment
    systems, but ``codex exec`` itself must not require a workstation
    ``~/.codex`` directory in hosted mode.

    Profile-owned env contract: ``profile_env`` carries literal
    profile-owned env values the local ``docker compose exec`` path does
    NOT forward (the running agent container already has them, substituted
    from the compose env block at stack launch). The hosted (non-compose)
    path has no compose env block, so the hosted executor must inject these
    values directly or the job launches without them — e.g. a profile-owned
    ``OLLAMA_HOST`` daemon the OpenCode launcher would otherwise fail to
    resolve, falling back to the default daemon. Only literal values are
    carried; Compose ``${NAME}`` interpolation placeholders are worker-resolved
    secrets and stay in ``env_passthrough_names`` for out-of-band resolution
    (never carried in ``profile_env``).

    File-auth contract: ``file_auth_mount_targets`` carries recognized
    container target paths for local provider auth mounts (for example
    ``/home/agent/.codex``), plus dynamic file targets such as the path named
    by ``GOOGLE_APPLICATION_CREDENTIALS``. It never carries credential
    contents; hosted executors resolve any equivalent secret or file mount
    out-of-band from those target identifiers.

    Service context contract: ``profile`` carries a resolved workspace profile
    when the caller has one, and ``compose_project`` / ``compose_file`` identify
    the rendered local Compose stack. Hosted executors that need sidecars should
    use sanitized profile/rendered-stack metadata derived from these fields,
    not host-local secret values or bind-mount sources.

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
    agent_runtime: AgentRuntime | str
    cli_args: tuple[str, ...]
    prompt_stdin: bytes
    log_source: str
    model: str | None
    effort: str | None
    env_passthrough_names: tuple[str, ...] = ()
    env_passthrough_aliases: tuple[tuple[str, str], ...] = ()
    file_auth_mount_targets: tuple[str, ...] = ()
    profile_env: tuple[tuple[str, str], ...] = ()
    wall_timeout_seconds: float | None = None
    idle_timeout_seconds: float | None = None
    repo_url: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    head_repo_url: str | None = None
    head_repo_slug: str | None = None
    owned_paths: tuple[str, ...] = ()
    expected_head_sha: str | None = None
    git_preparation: AgentRuntimeGitPreparation | None = None
    read_only: bool = False
    on_stdout: StreamCallback | None = None
    on_stderr: StreamCallback | None = None
    profile: WorkspaceProfile | None = None
    compose_project: str | None = None
    compose_file: Path | None = None
    # Local worktree for profile env_file password scanning only; never sent to Cloud.
    worktree_path: Path | None = None


@dataclass(frozen=True)
class AgentRuntimeExecResult:
    """Result of one hosted agent CLI run.

    Implementations signal a watchdog termination with ``returncode == 124``
    (the conventional ``timeout(1)`` exit). Wall-clock and idle timeouts share
    that exit code on the Compose path but are separated via ``reason_code``;
    the hosted result carries the same distinction on the ``timeout_reason``
    field so the adapter can map idle timeouts to ``AGENT_IDLE_TIMEOUT``
    instead of collapsing every 124 into a wall-clock timeout.
    ``timeout_reason`` is empty by default; hosted executors must set it
    explicitly when the hosted runner, not the agent CLI or an inner wrapper,
    enforced a watchdog timeout.
    """

    returncode: int
    stdout: str
    stderr: str
    timeout_reason: str = ""
    terminal_head_sha: str | None = None


class AgentRuntimeExecutor(Protocol):
    """Cloud-neutral execution backend for agent CLI runs.

    Default absence preserves the exact tracked ``docker compose exec`` path.
    A hosted AWF Cloud deployment implements this with Kubernetes Jobs.
    Core ships no Kubernetes implementation. Implementations MUST stream the
    prompt via stdin/context, never argv, and MUST NOT log or persist secret
    values (resolve them out-of-band from ``env_passthrough_names`` and
    ``env_passthrough_aliases`` / ``file_auth_mount_targets``).
    ``request.profile_env`` carries literal profile-owned env values the
    executor injects directly (not worker-resolved); those are non-secret
    profile configuration (e.g. ``OLLAMA_HOST``) and may be set on the job env.

    Read-only clarification contract: when ``request.read_only`` is true, the
    executor MUST run the agent against an isolated, immutable checkout pinned
    to ``request.expected_head_sha`` and reject the request if it cannot do so.
    It must not grant push-capable Git credentials or otherwise let the run
    advance the PR head.

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

    async def execute(self, request: AgentRuntimeExecRequest) -> AgentRuntimeExecResult:
        """Run one hosted agent CLI request and return its captured result."""
        ...  # pragma: no cover
