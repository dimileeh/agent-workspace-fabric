"""Helper entries for doctor reasons catalog."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from awf.service.doctor.models import DiagnosticStatus

if TYPE_CHECKING:
    from awf.service.doctor.reasons import _ReasonText


def reason_catalog_link(reason_code: str) -> str:
    """Return the local reason catalog anchor for a stable reason code."""
    return f"docs/REASON_CATALOG.md#{reason_code.lower()}"


def get_salvage_and_monitor_reasons(
    reason_text_cls: type[_ReasonText],
) -> dict[str, _ReasonText]:
    """Return extra catalog reason text entries."""
    return {
        "POST_AGENT_FORMAT_REPAIR_FAILED": reason_text_cls(
            (
                "AWF detected a repairable post-agent pre-commit failure but the "
                "repair pipeline itself exited non-zero before the retry commit could run."
            ),
            "Inspect the workspace logs for the repair sub-step stderr, fix the toolchain or git state, and remonitor.",
            (
                "The workspace image is missing `uv` or dev extras, the pinned Python "
                "version is unavailable, `ruff` crashed on flagged paths, or the "
                "post-repair `git add` failed. The corresponding "
                "``workspace.post_agent_commit_repair`` event records "
                '``retry_outcome="error"``.'
            ),
            "awf workspace logs <workspace_id>",
            reason_catalog_link("POST_AGENT_FORMAT_REPAIR_FAILED"),
        ),
        "POST_AGENT_GIT_ADD_FAILED": reason_text_cls(
            (
                "``git add -A`` failed during post-agent salvage (e.g. exit 128 with "
                "``fatal: not a git repository``)."
            ),
            "Inspect the worktree, recover any salvageable files manually, and recreate the workspace.",
            (
                "The agent damaged the worktree's git metadata or removed ``.git``; "
                "no commit could be attempted to capture work."
            ),
            "awf workspace logs <workspace_id>",
            reason_catalog_link("POST_AGENT_GIT_ADD_FAILED"),
        ),
        "PROVIDER_AUTH_FAILED": reason_text_cls(
            (
                "A workspace agent or PR monitor could not run because the selected "
                "LLM provider authentication failed."
            ),
            (
                "Refresh the provider credentials, restart or rebuild the AWF "
                "service/runtime if credentials are mounted into containers, then "
                "remonitor or reschedule the workspace."
            ),
            (
                "The provider token is expired, reused, missing inside the workspace "
                "runtime, or rejected by the provider CLI/API."
            ),
            "awf service doctor",
            reason_catalog_link("PROVIDER_AUTH_FAILED"),
        ),
        "OLLAMA_MODEL_PULL_FAILED": reason_text_cls(
            (
                "AWF could not make the requested OpenCode/Ollama model available: the "
                "host daemon failed to pull it before the agent run."
            ),
            (
                "Check the host Ollama daemon (`ollama ls`, `ollama pull <model>`), "
                "confirm the model name and registry reachability, then recreate or "
                "remonitor the workspace once the model can be pulled."
            ),
            (
                "The requested model is not present in the daemon's `/api/tags`, is not "
                "an Ollama Cloud (`:cloud`) model, and the streamed `POST /api/pull` "
                "reported an error, timed out, or left the model still missing — for "
                "example a misspelled model name or an unreachable model registry."
            ),
            "awf workspace logs <workspace_id>",
            reason_catalog_link("OLLAMA_MODEL_PULL_FAILED"),
        ),
        "MONITOR_RECOVERY_SUPERSEDED": reason_text_cls(
            (
                "AWF cancelled a PR-monitor recovery operation because another worker "
                "claimed the monitor lease and started a replacement recovery operation."
            ),
            (
                "No action is usually required if another recovery operation is already "
                "running. If the workspace is stuck without an active monitor, remonitor it."
            ),
            (
                "This worker lost the monitoring_pr claim to another worker that registered "
                "a replacement remonitor recovery operation before this worker could finalize."
            ),
            "awf workspace show <workspace_id>",
            reason_catalog_link("MONITOR_RECOVERY_SUPERSEDED"),
        ),
        "MONITOR_RECOVERY_CHECKOUT_RESTORE_FAILED": reason_text_cls(
            (
                "Hosted PR-monitor resume could not restore the managed worktree checkout "
                "after a worker pod replacement (missing adoption tip, repo, or git failure)."
            ),
            (
                "Confirm the workspace still has durable PR/adoption metadata and that "
                "GitHub auth can fetch refs/pull/<n>/head (or the forge-neutral head "
                "branch), then remonitor. Do not expect Compose metadata on hosted rows."
            ),
            (
                "The replacement control-worker pod has no pod-local worktree under "
                "<work_dir>/git/worktrees, and checkout reconstruction failed closed."
            ),
            "awf workspace remonitor <workspace_id>",
            reason_catalog_link("MONITOR_RECOVERY_CHECKOUT_RESTORE_FAILED"),
        ),
    }


def resolve_reason_text(
    text: _ReasonText | None,
    reason_text_cls: type[_ReasonText],
    reason: str,
    *,
    label: str,
    status: DiagnosticStatus,
    context: Mapping[str, str] | None = None,
) -> _ReasonText:
    """Return catalog text or fallback diagnostic guidance for a reason."""
    if text is None:
        msg = (
            f"{label} check passed."
            if status == "ok"
            else (
                f"{label} check was skipped."
                if status == "skipped"
                else f"{label} check reported {status}."
            )
        )
        act = (
            "No action required."
            if status == "ok"
            else (
                "Fix prerequisite checks first."
                if status == "skipped"
                else "Inspect diagnostic detail and matching check."
            )
        )
        cause = (
            ""
            if status == "ok"
            else ("Prerequisites failed." if status == "skipped" else "An unknown check failed.")
        )
        cmd = "" if status == "ok" else "awf service doctor"
        return reason_text_cls(msg, act, cause, cmd, "")
    if context and reason in ("API_UNREACHABLE", "PORT_OPEN"):
        val = context.get("url" if reason == "API_UNREACHABLE" else "endpoint")
        if val:
            summary = (
                f"AWF API is not reachable at {val}."
                if reason == "API_UNREACHABLE"
                else f"{val} is accepting connections."
            )
            return reason_text_cls(
                summary, text.action, text.likely_cause, text.related_command, text.docs_link
            )
    return text
