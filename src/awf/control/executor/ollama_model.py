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
from collections.abc import Mapping
from typing import Any

from awf.control.executor.quality_gates import _log
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.service.provider_readiness import (
    ensure_ollama_model_available,
    is_secret_env_key,
)
from awf.service.provider_readiness_helpers import (
    _http_get,
    _http_post_stream,
    _ollama_pull_urls,
    _ollama_tags_urls,
)
from awf.service.workspace_observability import effective_agent_identity_for_workspace

# Keep the persisted progress tail bounded; the live stream is logged in full.
_PULL_PROGRESS_EVENT_LIMIT = 20


def _environ_secret_values(environ: Mapping[str, str]) -> frozenset[str]:
    return frozenset(
        value for key, value in environ.items() if is_secret_env_key(key) and len(value) >= 4
    )


async def _ensure_ollama_model_or_mark_failed(
    self: Any,
    *,
    workspace_id: str,
    ws: Any,
) -> bool:
    """Ensure the OpenCode/Ollama model is available; pull it if needed.

    Returns ``True`` when the agent run may proceed (model in ``/api/tags``,
    ``:cloud`` model, or a successful pull) and ``False`` after marking the
    workspace failed. A no-op (returns ``True``) for non-OpenCode runtimes.
    """

    if AgentRuntime(ws.agent) is not AgentRuntime.opencode:
        return True

    model = effective_agent_identity_for_workspace(ws).model
    environ = os.environ
    secrets = _environ_secret_values(environ)
    progress: list[str] = []

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
    if str(result.get("status") or "fail") != "fail":
        if reason_code == "OLLAMA_MODEL_PULLED":
            await _record_ollama_model_event(
                self,
                workspace_id=workspace_id,
                event_type="workspace.ollama_model_pulled",
                reason_code=reason_code,
                payload={
                    "model": model,
                    "message": str(result.get("message") or "")[:1000],
                    "progress": progress[-_PULL_PROGRESS_EVENT_LIMIT:],
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
        from_status=WorkspaceStatus.running,
        failure_reason=FailureReason.infrastructure_failure,
        message=message[:2000],
        reason_code=reason_code,
        details={
            "model": model,
            "progress": progress[-_PULL_PROGRESS_EVENT_LIMIT:],
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
