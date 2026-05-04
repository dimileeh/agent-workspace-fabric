"""Operator-facing doctor reason text."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from awf.service.doctor.models import DiagnosticStatus


@dataclass(frozen=True)
class _ReasonText:
    message: str
    action: str
    likely_cause: str
    related_command: str
    docs_link: str


_REASON_TEXT: dict[str, _ReasonText] = {
    "DOCKER_OK": _ReasonText(
        "Docker daemon is reachable.",
        "No action required.",
        "", "", "",
    ),
    "DOCKER_CLI_NOT_FOUND": _ReasonText(
        "Docker CLI is not installed or is not on PATH.",
        "Install Docker Desktop or make the docker CLI available to the AWF service environment.",
        "The docker CLI is missing from the host environment or not accessible to the AWF process.",
        "awf doctor",
        "https://docs.docker.com/get-docker/",
    ),
    "DOCKER_SOCKET_UNREACHABLE": _ReasonText(
        "Docker socket is not reachable.",
        "Start Docker Desktop or verify AWF_DOCKER_HOST.",
        "The Docker daemon is not running or the socket permissions are incorrect.",
        "awf doctor",
        "https://docs.docker.com/config/daemon/",
    ),
    "DOCKER_DAEMON_UNREACHABLE": _ReasonText(
        "Docker is installed but the daemon is not reachable.",
        "Start Docker Desktop or verify AWF_DOCKER_HOST.",
        "The Docker daemon is stopped, crashing, or blocking connections.",
        "awf doctor",
        "https://docs.docker.com/config/daemon/",
    ),
    "API_OK": _ReasonText(
        "AWF API health endpoint is reachable.",
        "No action required.",
        "", "", "",
    ),
    "API_UNREACHABLE": _ReasonText(
        "AWF API is not reachable.",
        "Run awf service bootstrap or inspect API logs.",
        "The local AWF service container is not running or port 8000 is blocked.",
        "awf service logs",
        "docs/REASON_CATALOG.md#api_unreachable",
    ),
    "WORKER_RUNNING": _ReasonText(
        "AWF worker container is running.",
        "No action required.",
        "", "", "",
    ),
    "WORKER_CONTAINER_MISSING": _ReasonText(
        "AWF worker container was not found in the local Compose project.",
        "Run awf service bootstrap to start the worker.",
        "The AWF service has not been bootstrapped on this machine.",
        "awf service bootstrap",
        "docs/REASON_CATALOG.md#worker_container_missing",
    ),
    "WORKER_CONTAINER_EXITED": _ReasonText(
        "AWF worker container has exited.",
        "Inspect worker logs with awf service logs --service worker.",
        "The worker process crashed due to configuration or resource limits.",
        "awf service logs --service worker",
        "docs/REASON_CATALOG.md#worker_container_exited",
    ),
    "WORKER_CONTAINER_NOT_RUNNING": _ReasonText(
        "AWF worker container is present but is not running.",
        "Run awf service bootstrap or inspect worker logs.",
        "The worker container was stopped manually or failed to start.",
        "awf service bootstrap",
        "docs/REASON_CATALOG.md#worker_container_not_running",
    ),
    "WORKER_UNHEALTHY": _ReasonText(
        "AWF worker container is running but Docker reports it unhealthy.",
        "Inspect worker logs with awf service logs --service worker.",
        "The worker background tasks are stalled or failing.",
        "awf service logs --service worker",
        "docs/REASON_CATALOG.md#worker_unhealthy",
    ),
    "WORKER_STATUS_UNAVAILABLE": _ReasonText(
        "AWF worker container status could not be inspected.",
        "Verify Docker is running and the local service Compose file exists.",
        "Docker is unresponsive or the local compose state is corrupted.",
        "docker compose ps",
        "docs/REASON_CATALOG.md#worker_status_unavailable",
    ),
    "WORKER_STATUS_UNPARSEABLE": _ReasonText(
        "AWF worker container status output could not be parsed.",
        "Upgrade Docker Compose or inspect `docker compose ps worker --format json` manually.",
        "Docker compose returned unexpected output format.",
        "docker compose ps --format json",
        "docs/REASON_CATALOG.md#worker_status_unparseable",
    ),
    "GITHUB_AUTH_OK": _ReasonText(
        "GitHub CLI auth is usable for PR operations.",
        "No action required.",
        "", "", "",
    ),
    "CODEX_AUTH_OK": _ReasonText(
        "Codex auth is usable for agent workspaces.",
        "No action required.",
        "", "", "",
    ),
    "CLAUDE_CODE_AUTH_OK": _ReasonText(
        "Claude Code auth is usable for agent workspaces.",
        "No action required.",
        "", "", "",
    ),
    "GEMINI_AUTH_OK": _ReasonText(
        "Gemini auth is usable for agent workspaces.",
        "No action required.",
        "", "", "",
    ),
    "OPENCODE_AUTH_OK": _ReasonText(
        "OpenCode/Ollama auth is usable for agent workspaces.",
        "No action required.",
        "", "", "",
    ),
    "GITHUB_TOKEN_ENV_MISSING": _ReasonText(
        "No service-visible GitHub token was found.",
        "Set AWF_GITHUB_TOKEN from `gh auth token` before starting the service.",
        "GitHub CLI is not authenticated or token is not passed to the service.",
        "gh auth login",
        "docs/REASON_CATALOG.md#github_token_env_missing",
    ),
    "GITHUB_CLI_NOT_FOUND": _ReasonText(
        "GitHub token is present, but the gh CLI is not installed.",
        "Install gh in the service image or rebuild the local service image.",
        "The gh CLI is missing from the container environment.",
        "awf service bootstrap",
        "docs/REASON_CATALOG.md#github_cli_not_found",
    ),
    "GITHUB_AUTH_UNUSABLE": _ReasonText(
        "GitHub CLI auth is not usable for local service PR operations.",
        "Run gh auth status locally and refresh AWF_GITHUB_TOKEN if needed.",
        "The GitHub token is expired, invalid, or lacks required scopes.",
        "gh auth status",
        "docs/REASON_CATALOG.md#github_auth_unusable",
    ),
    "CODEX_AUTH_MISSING": _ReasonText(
        "No Codex auth signal was visible.",
        "Mount ~/.codex or set OPENAI_API_KEY, OPENAI_API_TOKEN, CODEX_API_KEY, or CODEX_AUTH_TOKEN.",
        "Missing Codex API credentials.",
        "awf doctor",
        "docs/REASON_CATALOG.md#codex_auth_missing",
    ),
    "CLAUDE_AUTH_MISSING": _ReasonText(
        "No Claude Code auth signal was visible.",
        "Mount ~/.claude or set ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or CLAUDE_CODE_OAUTH_TOKEN.",
        "Missing Claude API credentials.",
        "awf doctor",
        "docs/REASON_CATALOG.md#claude_auth_missing",
    ),
    "GEMINI_AUTH_MISSING": _ReasonText(
        "No Gemini auth signal was visible.",
        "Mount ~/.gemini or set GEMINI_API_KEY, GOOGLE_API_KEY, or GOOGLE_APPLICATION_CREDENTIALS.",
        "Missing Gemini API credentials.",
        "awf doctor",
        "docs/REASON_CATALOG.md#gemini_auth_missing",
    ),
    "OPENCODE_OLLAMA_AUTH_MISSING": _ReasonText(
        "No OpenCode/Ollama auth signal was visible.",
        "Mount ~/.config/opencode, mount ~/.ollama auth files, or set OLLAMA_API_KEY.",
        "Missing OpenCode/Ollama credentials.",
        "awf doctor",
        "docs/REASON_CATALOG.md#opencode_ollama_auth_missing",
    ),
    "PORT_OPEN": _ReasonText(
        "Required local port is accepting connections.",
        "No action required.",
        "", "", "",
    ),
    "PORT_CLOSED": _ReasonText(
        "Required local port is not accepting connections.",
        "Start the AWF local service or free the configured port.",
        "The service is not running or the port is in use by another process.",
        "awf service bootstrap",
        "docs/REASON_CATALOG.md#port_closed",
    ),
    "PORT_CONFIG_INVALID": _ReasonText(
        "Required local port could not be derived from configuration.",
        "Fix the local AWF URL configuration and re-run doctor.",
        "Invalid AWF_API_URL or AWF_FRONTEND_URL.",
        "awf doctor",
        "docs/REASON_CATALOG.md#port_config_invalid",
    ),
    "SUFFICIENT_DISK": _ReasonText(
        "Free disk is above the configured AWF threshold.",
        "No action required.",
        "", "", "",
    ),
    "INSUFFICIENT_DISK": _ReasonText(
        "Free disk is below the configured AWF threshold.",
        "Free disk before creating new workspaces or intentionally lower AWF_MIN_FREE_DISK_BYTES.",
        "Too many stopped containers, volumes, or large workspaces.",
        "docker system prune",
        "docs/REASON_CATALOG.md#insufficient_disk",
    ),
    "DISK_USAGE_UNAVAILABLE": _ReasonText(
        "Free disk could not be inspected for the AWF work directory.",
        "Verify AWF_WORK_DIR is accessible and re-run doctor.",
        "Permission denied or path does not exist.",
        "awf doctor",
        "docs/REASON_CATALOG.md#disk_usage_unavailable",
    ),
    "NO_STRANDED_WORKSPACES": _ReasonText(
        "No stale or exited AWF workspace containers were detected.",
        "No action required.",
        "", "", "",
    ),
    "STRANDED_WORKSPACES_PRESENT": _ReasonText(
        "Stale or exited AWF workspace containers need operator review.",
        "Inspect the listed workspaces before running cleanup or recovery.",
        "Workspaces failed to tear down cleanly after task completion.",
        "awf workspace list",
        "docs/REASON_CATALOG.md#stranded_workspaces_present",
    ),
    "NO_ORPHANS": _ReasonText(
        "No orphan AWF Docker resources were detected.",
        "No action required.",
        "", "", "",
    ),
    "ORPHAN_RESOURCES_PRESENT": _ReasonText(
        "Orphan AWF Docker resources were detected.",
        "Review the listed resources before running cleanup.",
        "Networks or volumes left behind by deleted workspaces.",
        "awf service cleanup",
        "docs/REASON_CATALOG.md#orphan_resources_present",
    ),
    "NETWORK_POSTURE_NO_ACTIVE_OPEN": _ReasonText(
        "No active workspace is using open network posture.",
        "No action required.",
        "", "", "",
    ),
    "NETWORK_POSTURE_OPEN_ACTIVE": _ReasonText(
        "One or more active workspaces have unrestricted internet access.",
        "Confirm the open workspaces are trusted local work or recreate them with restricted/offline posture.",
        "Workspaces were started with --network=open.",
        "awf workspace list",
        "docs/REASON_CATALOG.md#network_posture_open_active",
    ),
    "NETWORK_POSTURE_UNAVAILABLE": _ReasonText(
        "Workspace network posture could not be inspected.",
        "Restore control-plane database access and re-run doctor.",
        "Cannot query the local database to check workspace posture.",
        "awf doctor",
        "docs/REASON_CATALOG.md#network_posture_unavailable",
    ),
    "LOCAL_CONFIG_OK": _ReasonText(
        "Local AWF configuration looks usable.",
        "No action required.",
        "", "", "",
    ),
    "LOCAL_CONFIG_INVALID": _ReasonText(
        "Local AWF configuration has issues that block reliable service use.",
        "Fix the listed environment or path settings and re-run doctor.",
        "Invalid values in .env or missing required paths.",
        "awf doctor",
        "docs/REASON_CATALOG.md#local_config_invalid",
    ),
    "SERVICE_STATUS_COLLECTION_FAILED": _ReasonText(
        "AWF service status checks could not be collected.",
        "Fix the reported local configuration error and re-run doctor.",
        "Service discovery or database connection failed.",
        "awf doctor",
        "docs/REASON_CATALOG.md#service_status_collection_failed",
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
            return _ReasonText(f"{label} check passed.", "No action required.", "", "", "")
        if status == "skipped":
            return _ReasonText(f"{label} check was skipped.", "Fix prerequisite checks first.", "Prerequisites failed.", "awf doctor", "")
        return _ReasonText(
            f"{label} check reported {status}.",
            "Inspect the diagnostic detail and the matching service status check.",
            "An unknown diagnostic check failed.",
            "awf doctor",
            "",
        )
    if reason == "API_UNREACHABLE" and context and context.get("url"):
        return _ReasonText(
            f"AWF API is not reachable at {context['url']}.",
            text.action,
            text.likely_cause,
            text.related_command,
            text.docs_link,
        )
    if reason == "PORT_OPEN" and context and context.get("endpoint"):
        return _ReasonText(f"{context['endpoint']} is accepting connections.", text.action, text.likely_cause, text.related_command, text.docs_link)
    return text
