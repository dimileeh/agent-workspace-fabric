"""Operator-facing doctor reason text."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from awf.service.doctor.models import DiagnosticStatus


@dataclass(frozen=True)
class _ReasonText:
    message: str
    action: str


_REASON_TEXT: dict[str, _ReasonText] = {
    "DOCKER_OK": _ReasonText(
        "Docker daemon is reachable.",
        "No action required.",
    ),
    "DOCKER_CLI_NOT_FOUND": _ReasonText(
        "Docker CLI is not installed or is not on PATH.",
        "Install Docker Desktop or make the docker CLI available to the AWF service environment.",
    ),
    "DOCKER_SOCKET_UNREACHABLE": _ReasonText(
        "Docker socket is not reachable.",
        "Start Docker Desktop or verify AWF_DOCKER_HOST.",
    ),
    "DOCKER_DAEMON_UNREACHABLE": _ReasonText(
        "Docker is installed but the daemon is not reachable.",
        "Start Docker Desktop or verify AWF_DOCKER_HOST.",
    ),
    "API_OK": _ReasonText(
        "AWF API health endpoint is reachable.",
        "No action required.",
    ),
    "API_UNREACHABLE": _ReasonText(
        "AWF API is not reachable.",
        "Run awf service bootstrap or inspect API logs.",
    ),
    "WORKER_RUNNING": _ReasonText(
        "AWF worker container is running.",
        "No action required.",
    ),
    "WORKER_CONTAINER_MISSING": _ReasonText(
        "AWF worker container was not found in the local Compose project.",
        "Run awf service bootstrap to start the worker.",
    ),
    "WORKER_CONTAINER_EXITED": _ReasonText(
        "AWF worker container has exited.",
        "Inspect worker logs with awf service logs --service worker.",
    ),
    "WORKER_CONTAINER_NOT_RUNNING": _ReasonText(
        "AWF worker container is present but is not running.",
        "Run awf service bootstrap or inspect worker logs.",
    ),
    "WORKER_UNHEALTHY": _ReasonText(
        "AWF worker container is running but Docker reports it unhealthy.",
        "Inspect worker logs with awf service logs --service worker.",
    ),
    "WORKER_STATUS_UNAVAILABLE": _ReasonText(
        "AWF worker container status could not be inspected.",
        "Verify Docker is running and the local service Compose file exists.",
    ),
    "WORKER_STATUS_UNPARSEABLE": _ReasonText(
        "AWF worker container status output could not be parsed.",
        "Upgrade Docker Compose or inspect `docker compose ps worker --format json` manually.",
    ),
    "GITHUB_AUTH_OK": _ReasonText(
        "GitHub CLI auth is usable for PR operations.",
        "No action required.",
    ),
    "CODEX_AUTH_OK": _ReasonText(
        "Codex auth is usable for agent workspaces.",
        "No action required.",
    ),
    "CLAUDE_CODE_AUTH_OK": _ReasonText(
        "Claude Code auth is usable for agent workspaces.",
        "No action required.",
    ),
    "GEMINI_AUTH_OK": _ReasonText(
        "Gemini auth is usable for agent workspaces.",
        "No action required.",
    ),
    "OPENCODE_AUTH_OK": _ReasonText(
        "OpenCode/Ollama auth is usable for agent workspaces.",
        "No action required.",
    ),
    "GITHUB_TOKEN_ENV_MISSING": _ReasonText(
        "No service-visible GitHub token was found.",
        "Set AWF_GITHUB_TOKEN from `gh auth token` before starting the service.",
    ),
    "GITHUB_CLI_NOT_FOUND": _ReasonText(
        "GitHub token is present, but the gh CLI is not installed.",
        "Install gh in the service image or rebuild the local service image.",
    ),
    "GITHUB_AUTH_UNUSABLE": _ReasonText(
        "GitHub CLI auth is not usable for local service PR operations.",
        "Run gh auth status locally and refresh AWF_GITHUB_TOKEN if needed.",
    ),
    "CODEX_AUTH_MISSING": _ReasonText(
        "No Codex auth signal was visible.",
        "Mount ~/.codex or set OPENAI_API_KEY, OPENAI_API_TOKEN, CODEX_API_KEY, or CODEX_AUTH_TOKEN.",
    ),
    "CLAUDE_AUTH_MISSING": _ReasonText(
        "No Claude Code auth signal was visible.",
        "Mount ~/.claude or set ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or CLAUDE_CODE_OAUTH_TOKEN.",
    ),
    "GEMINI_AUTH_MISSING": _ReasonText(
        "No Gemini auth signal was visible.",
        "Mount ~/.gemini or set GEMINI_API_KEY, GOOGLE_API_KEY, or GOOGLE_APPLICATION_CREDENTIALS.",
    ),
    "OPENCODE_OLLAMA_AUTH_MISSING": _ReasonText(
        "No OpenCode/Ollama auth signal was visible.",
        "Mount ~/.config/opencode, mount ~/.ollama auth files, or set OLLAMA_API_KEY.",
    ),
    "PORT_OPEN": _ReasonText(
        "Required local port is accepting connections.",
        "No action required.",
    ),
    "PORT_CLOSED": _ReasonText(
        "Required local port is not accepting connections.",
        "Start the AWF local service or free the configured port.",
    ),
    "PORT_CONFIG_INVALID": _ReasonText(
        "Required local port could not be derived from configuration.",
        "Fix the local AWF URL configuration and re-run doctor.",
    ),
    "SUFFICIENT_DISK": _ReasonText(
        "Free disk is above the configured AWF threshold.",
        "No action required.",
    ),
    "INSUFFICIENT_DISK": _ReasonText(
        "Free disk is below the configured AWF threshold.",
        "Free disk before creating new workspaces or intentionally lower AWF_MIN_FREE_DISK_BYTES.",
    ),
    "DISK_USAGE_UNAVAILABLE": _ReasonText(
        "Free disk could not be inspected for the AWF work directory.",
        "Verify AWF_WORK_DIR is accessible and re-run doctor.",
    ),
    "NO_STRANDED_WORKSPACES": _ReasonText(
        "No stale or exited AWF workspace containers were detected.",
        "No action required.",
    ),
    "STRANDED_WORKSPACES_PRESENT": _ReasonText(
        "Stale or exited AWF workspace containers need operator review.",
        "Inspect the listed workspaces before running cleanup or recovery.",
    ),
    "NO_ORPHANS": _ReasonText(
        "No orphan AWF Docker resources were detected.",
        "No action required.",
    ),
    "ORPHAN_RESOURCES_PRESENT": _ReasonText(
        "Orphan AWF Docker resources were detected.",
        "Review the listed resources before running cleanup.",
    ),
    "NETWORK_POSTURE_NO_ACTIVE_OPEN": _ReasonText(
        "No active workspace is using open network posture.",
        "No action required.",
    ),
    "NETWORK_POSTURE_OPEN_ACTIVE": _ReasonText(
        "One or more active workspaces have unrestricted internet access.",
        "Confirm the open workspaces are trusted local work or recreate them with restricted/offline posture.",
    ),
    "NETWORK_POSTURE_UNAVAILABLE": _ReasonText(
        "Workspace network posture could not be inspected.",
        "Restore control-plane database access and re-run doctor.",
    ),
    "LOCAL_CONFIG_OK": _ReasonText(
        "Local AWF configuration looks usable.",
        "No action required.",
    ),
    "LOCAL_CONFIG_INVALID": _ReasonText(
        "Local AWF configuration has issues that block reliable service use.",
        "Fix the listed environment or path settings and re-run doctor.",
    ),
    "SERVICE_STATUS_COLLECTION_FAILED": _ReasonText(
        "AWF service status checks could not be collected.",
        "Fix the reported local configuration error and re-run doctor.",
    ),
}


def _reason_text(
    reason: str,
    *,
    label: str,
    status: DiagnosticStatus,
    context: Mapping[str, str] | None = None,
) -> _ReasonText:
    text = _REASON_TEXT.get(reason)
    if text is None:
        if status == "ok":
            return _ReasonText(f"{label} check passed.", "No action required.")
        if status == "skipped":
            return _ReasonText(f"{label} check was skipped.", "Fix prerequisite checks first.")
        return _ReasonText(
            f"{label} check reported {status}.",
            "Inspect the diagnostic detail and the matching service status check.",
        )
    if reason == "API_UNREACHABLE" and context and context.get("url"):
        return _ReasonText(
            f"AWF API is not reachable at {context['url']}.",
            text.action,
        )
    if reason == "PORT_OPEN" and context and context.get("endpoint"):
        return _ReasonText(f"{context['endpoint']} is accepting connections.", text.action)
    return text
