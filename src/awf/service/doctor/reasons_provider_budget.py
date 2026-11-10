"""Operator-facing reason text for managed OpenRouter keys and budgets.

Kept beside ``reasons`` so that module stays within the first-party file-line
guardrail; the entries are merged into ``_REASON_TEXT`` at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from awf.service.doctor.reasons_helpers import reason_catalog_link

if TYPE_CHECKING:
    from awf.service.doctor.reasons import _ReasonText

_RAISE = "awf workspace provider-budget <workspace_id> --usd <amount>"
_SHOW = "awf workspace show <workspace_id>"
_STATUS = "awf service status --format json"

# code: (message, action, likely cause, related command)
_ENTRIES: dict[str, tuple[str, str, str, str]] = {
    "PROVIDER_BUDGET_EXHAUSTED": (
        "The workspace's managed OpenRouter key reached its USD budget; the workspace is "
        "parked (blocked) for a human instead of failing.",
        "Check the exact provider spend, then raise the budget to resume where it stopped. "
        "AWF never retries blindly or falls back to another provider for this reason.",
        "The agent's OpenRouter calls returned HTTP 402 / limit reached, or the authoritative "
        "key read showed no remaining limit.",
        _RAISE,
    ),
    "PROVIDER_BUDGET_RAISED": (
        "An operator set a new managed provider budget for the workspace.",
        "No action is needed; a provider-budget block resumes automatically.",
        "`awf workspace provider-budget` (REST/MCP equivalent) updated the frozen budget and "
        "the live key's limit.",
        _SHOW,
    ),
    "PROVIDER_BUDGET_INVALID": (
        "The requested provider budget is not a USD amount above the recorded spend.",
        "Choose a budget greater than the workspace's exact provider spend and below "
        "1,000,000 USD.",
        "The amount was zero, negative, non-numeric, too large or at/below the spend AWF "
        "already recorded.",
        _SHOW,
    ),
    "PROVIDER_BUDGET_UNSUPPORTED": (
        "This workspace has no managed OpenRouter budget to set.",
        "Provider budgets apply only to live workspaces whose OpenRouter selection uses an "
        "AWF-minted key; configure OPENROUTER_MANAGEMENT_KEY before creating them.",
        "No management key was configured at creation, the selection is not OpenRouter, or "
        "the workspace is already terminal.",
        _SHOW,
    ),
    "PROVIDER_MANAGED_KEY_UNAVAILABLE": (
        "AWF could not mint a managed OpenRouter key; only this selection is parked.",
        "Inspect the management reason code (or provider_budget.reserved_usd) in the park "
        "event, fix the OpenRouter account or management key, then raise or re-set the "
        "budget to resume.",
        "The OpenRouter management API rejected or failed the key creation (auth, rate "
        "limit, outage), the key could not be recorded, or keys AWF could not finalize yet "
        "still hold the budget that spend leaves.",
        _SHOW,
    ),
    "PROVIDER_MANAGEMENT_KEY_MISSING": (
        "A managed OpenRouter workspace ran on an API/worker without a management key.",
        "Set OPENROUTER_MANAGEMENT_KEY for the API and worker services and restart them; the "
        "workspace never falls back to the static OPENROUTER_API_KEY.",
        "The management key was removed after the workspace froze a managed selection.",
        _STATUS,
    ),
    "PROVIDER_MANAGED_KEY_HOSTED_UNSUPPORTED": (
        "Managed OpenRouter keys are not supported on hosted runtimes.",
        "Run managed OpenRouter workspaces on local Core, or create the hosted workspace "
        "without a management key configured.",
        "Hosted execution cannot receive per-invocation exec-time credentials.",
        _SHOW,
    ),
    "PROVIDER_MANAGED_KEY_ORPHAN_DELETED": (
        "The reconciler deleted an AWF-minted OpenRouter key with no live binding.",
        "No action is needed; its usage was recorded before deletion.",
        "A worker restart, crash or terminal transition left the key behind.",
        _SHOW,
    ),
    "PROVIDER_MANAGED_KEY_RECONCILE_FAILED": (
        "The periodic managed-key reconciliation or terminal finalization failed.",
        "Check worker logs for the management reason code; the sweep retries on its next "
        "interval and never blocks other work.",
        "The OpenRouter management API or the database was unavailable during the sweep.",
        _STATUS,
    ),
    "PROVIDER_MANAGED_KEY_NOT_FOUND": (
        "OpenRouter no longer has the managed key AWF recorded.",
        "No action is needed; AWF treats the key as deleted.",
        "The key was already deleted by AWF, the reconciler or an operator.",
        _SHOW,
    ),
    "PROVIDER_MANAGED_KEY_PERSIST_FAILED": (
        "AWF could not record a managed key update in the database.",
        "Check database health; the next finalize or reconcile pass records the usage.",
        "A transient database error during a usage read or key release.",
        _STATUS,
    ),
    "PROVIDER_MANAGED_KEYS_DOWNGRADE_UNSAFE": (
        "The schema downgrade would drop recorded managed-key spend.",
        "Keep the compatible code and schema; finalize and archive managed keys before an "
        "administrative downgrade.",
        "provider_managed_keys still has rows.",
        "alembic current",
    ),
    "PROVIDER_MANAGEMENT_API_UNAUTHORIZED": (
        "OpenRouter rejected the management key (HTTP 401/403).",
        "Replace OPENROUTER_MANAGEMENT_KEY with a valid, unexpired management key.",
        "The management key expired, was revoked or lacks key-management permission.",
        _STATUS,
    ),
    "PROVIDER_MANAGEMENT_API_RATE_LIMITED": (
        "The OpenRouter management API rate-limited AWF (HTTP 429).",
        "Wait and retry; reduce concurrent managed workspaces if this persists.",
        "Many concurrent key mints, reads or deletions.",
        _STATUS,
    ),
    "PROVIDER_MANAGEMENT_API_UNAVAILABLE": (
        "The OpenRouter management API was unreachable or failed (5xx, timeout).",
        "Retry later; affected workspaces park and resume once a budget is re-set.",
        "An OpenRouter outage, network failure or the 10-second request bound.",
        _STATUS,
    ),
    "PROVIDER_MANAGEMENT_API_REJECTED": (
        "The OpenRouter management API rejected the request (HTTP 4xx).",
        "Inspect the operation and HTTP status; the request shape or account state is invalid.",
        "An invalid limit, a conflicting key or an account restriction.",
        _STATUS,
    ),
    "PROVIDER_MANAGEMENT_API_RESPONSE_INVALID": (
        "The OpenRouter management API returned an invalid or oversized response.",
        "Retry later; report the endpoint behaviour if it persists.",
        "Bad JSON, an unexpected shape, a redirect or a response above 2 MB.",
        _STATUS,
    ),
    "PROVIDER_MANAGEMENT_CREDENTIAL_FORBIDDEN": (
        "A profile tried to reference the OpenRouter management key.",
        "Remove OPENROUTER_MANAGEMENT_KEY references from runtime/service/companion "
        "environment values and env-secret declarations.",
        "Workspace stacks must never receive the control-plane management key.",
        "awf profile preview . --profile auto",
    ),
}


def get_provider_budget_reasons(reason_text_cls: type[_ReasonText]) -> dict[str, _ReasonText]:
    """Return the managed-key and provider-budget catalog entries."""
    return {
        code: reason_text_cls(message, action, cause, command, reason_catalog_link(code))
        for code, (message, action, cause, command) in _ENTRIES.items()
    }
