"""Pre-agent Ollama model discovery + auto-pull step (issue #552).

For OpenCode/Ollama workspaces the requested model must be served by the host
Ollama daemon before the agent runs. AWF discovers from ``/api/tags``, treats
``:cloud`` models as served remotely (no pull), and auto-pulls an absent
non-cloud model via ``POST /api/pull`` — streaming redacted progress to the
workspace logs/events. A pull or daemon failure here marks the workspace failed
with a clear ``OLLAMA_MODEL_PULL_FAILED`` / ``OLLAMA_MODEL_PROBE_FAILED`` reason
code rather than letting OpenCode reject the model and surface a confusing
``AGENT_CLI_FAILED``.
"""

from __future__ import annotations

import asyncio
import os
from collections import deque
from collections.abc import Mapping
from typing import Any

from awf.adapters.opencode import OPENCODE_OLLAMA_CLOUD_MODELS
from awf.control.executor.helpers import (
    _agent_defaults_for_workspace,
    _agent_run_model_for_workspace,
)
from awf.control.executor.quality_gates import _log
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.service.provider_readiness import (
    ensure_ollama_model_available,
    is_secret_env_key,
    overlay_profile_ollama_base_url,
)
from awf.service.provider_readiness_helpers import (
    _http_get,
    _http_post_stream,
    _ollama_pull_urls,
    _ollama_tags_urls,
    _ollama_url_host_reachable_from_worker,
)

# Keep the persisted progress tail bounded; the live stream is logged in full.
_PULL_PROGRESS_EVENT_LIMIT = 20


def _environ_secret_values(environ: Mapping[str, str]) -> frozenset[str]:
    return frozenset(
        value for key, value in environ.items() if is_secret_env_key(key) and len(value) >= 4
    )


def _effective_ollama_environ(ws: Any) -> Mapping[str, str]:
    """Return the worker env overlaid with the workspace's agent Ollama base URL.

    Delegates to the shared ``overlay_profile_ollama_base_url`` so this pre-agent
    step and create-time provider readiness resolve the daemon identically; a
    workspace without a resolved profile falls back to the worker env.
    """

    return overlay_profile_ollama_base_url(os.environ, getattr(ws, "resolved_profile", None))


def _targets_non_ollama_provider(model: str | None) -> bool:
    """Return whether the resolved OpenCode model names a non-Ollama provider.

    Mirrors ``OpenCodeAdapter.get_provider``: a provider-qualified model such as
    ``openai/gpt-oss`` or ``anthropic/claude-sonnet`` carries a ``<provider>/``
    prefix, while a bare or ``ollama/``-prefixed reference targets the local
    Ollama daemon. Only the latter should be probed/pulled against Ollama; a
    non-Ollama provider model must skip the Ollama preflight so OpenCode can use
    the selected provider instead of failing with an Ollama reason code.
    """

    raw = (model or "").strip()
    provider, slash, _remainder = raw.partition("/")
    return bool(slash) and provider != "ollama"


async def _ensure_ollama_model_or_mark_failed(
    self: Any,
    *,
    workspace_id: str,
    ws: Any,
    from_status: WorkspaceStatus = WorkspaceStatus.running,
) -> bool:
    """Ensure the OpenCode/Ollama model is available; pull it if needed.

    Returns ``True`` when the agent run may proceed (model in ``/api/tags``,
    ``:cloud`` model, or a successful pull) and ``False`` after marking the
    workspace failed. A no-op (returns ``True``) for non-OpenCode runtimes.
    """

    agent = AgentRuntime(ws.agent)
    if agent is not AgentRuntime.opencode:
        return True

    # Resolve the model through the exact path the adapter uses so ``ensure``
    # validates/pulls what the agent will actually run. ``effective_agent_identity``
    # only consults the static ``DEFAULT_AGENT_DEFAULTS`` and would ignore any
    # ``ExecutorConfig`` model/defaults overrides, letting this step probe or
    # skip-pull the wrong model when no explicit ``agent_model`` is in task policy.
    adapter_defaults = _agent_defaults_for_workspace(ws, self._defaults_for(agent))
    # Mirror ``OpenCodeAdapter._cli_args`` exactly: it resolves the launch model
    # as ``agent_model or adapter default or OPENCODE_OLLAMA_CLOUD_MODELS[0]``.
    # Without the final fallback this step would treat a model-less workspace as
    # ``MODEL_NOT_SELECTED`` and fail it, even though the adapter would still run
    # using that cloud default — so probe/pull what the agent will actually launch.
    model = (
        _agent_run_model_for_workspace(ws)
        or (adapter_defaults.model if adapter_defaults is not None else None)
        or OPENCODE_OLLAMA_CLOUD_MODELS[0]
    )
    # OpenCode can run a provider-qualified non-Ollama model (e.g. ``openai/...``
    # or ``anthropic/...``). The Ollama preflight only knows how to probe/pull
    # against the local daemon, so running it for such a model would probe a
    # name the daemon never has and fail the workspace with an Ollama reason
    # before OpenCode could use the selected provider. Skip the preflight there.
    if _targets_non_ollama_provider(model):
        _log.info(
            "executor.ollama_model_skip_non_ollama_provider",
            workspace_id=workspace_id,
            model=model,
        )
        return True
    environ = _effective_ollama_environ(ws)
    # #569: the worker runs off ``awf_net`` and cannot reach a workspace Compose
    # service DNS name (e.g. ``http://ollama-sidecar:11434``). Probing it from here
    # would falsely fail with ``OLLAMA_MODEL_PROBE_FAILED`` and block an otherwise
    # valid sidecar-Ollama workspace. Skip the worker-side reachability/model probe
    # for a non-host-reachable URL and let validation defer to the agent container
    # where the sidecar daemon IS reachable.
    if not _ollama_url_host_reachable_from_worker(environ):
        _log.info(
            "executor.ollama_model_skip_non_host_reachable",
            workspace_id=workspace_id,
            model=model,
        )
        return True
    secrets = _environ_secret_values(environ)
    # Bound the buffered tail: only the last ``_PULL_PROGRESS_EVENT_LIMIT`` lines
    # are ever persisted, so a long pull cannot grow this without limit.
    progress: deque[str] = deque(maxlen=_PULL_PROGRESS_EVENT_LIMIT)

    def _on_progress(line: str) -> None:
        progress.append(line)
        _log.info(
            "executor.ollama_model_pull_progress",
            workspace_id=workspace_id,
            progress=line,
        )

    result = await asyncio.to_thread(
        ensure_ollama_model_available,
        model=model,
        tags_urls=_ollama_tags_urls(environ),
        pull_urls=_ollama_pull_urls(environ),
        http_get=_http_get,
        http_post_stream=_http_post_stream,
        secrets=secrets,
        on_progress=_on_progress,
    )

    reason_code = str(result.get("reason_code") or "OLLAMA_MODEL_PROBE_FAILED")
    if str(result.get("status") or "") == "ok":
        if reason_code == "OLLAMA_MODEL_PULLED":
            await _record_ollama_model_event(
                self,
                workspace_id=workspace_id,
                event_type="workspace.ollama_model_pulled",
                reason_code=reason_code,
                payload={
                    "model": model,
                    "message": str(result.get("message") or "")[:1000],
                    "progress": list(progress),
                },
            )
        else:
            _log.info(
                "executor.ollama_model_ready",
                workspace_id=workspace_id,
                reason_code=reason_code,
                model=model,
            )
        return True

    message = str(result.get("message") or "Ollama model could not be made available.")
    detail = result.get("detail")
    if isinstance(detail, str) and detail:
        message = f"{message} {detail}"
    await self._mark_failed(
        workspace_id=workspace_id,
        from_status=from_status,
        failure_reason=FailureReason.infrastructure_failure,
        message=message[:2000],
        reason_code=reason_code,
        details={
            "model": model,
            "progress": list(progress),
        },
    )
    return False


async def _record_ollama_model_event(
    self: Any,
    *,
    workspace_id: str,
    event_type: str,
    reason_code: str,
    payload: Mapping[str, Any],
) -> None:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None:  # pragma: no cover - destroyed mid-flight
            return
        await repo.add_event(
            ws,
            event_type=event_type,
            reason_code=reason_code,
            payload=dict(payload),
        )
        await session.commit()
