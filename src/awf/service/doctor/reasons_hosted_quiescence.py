"""Operator-facing doctor reason text for hosted-node quiescence cleanup.

Kept beside ``reasons`` so that module stays within the first-party file-line
guardrail; the entries are merged into ``_REASON_TEXT`` at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from awf.service.doctor.reasons import _ReasonText


def get_hosted_quiescence_reasons(
    reason_text_cls: type[_ReasonText],
) -> dict[str, _ReasonText]:
    """Return the hosted quiescence catalog reason text entries."""
    return {
        "HOSTED_RUNTIME_STOPPED": reason_text_cls(
            (
                "The hosted node stopped this workspace because its consumer authority "
                "was explicitly denied (revoked grant, missing receipt, binding mismatch, "
                "or inactive workspace)."
            ),
            (
                "Renew or re-bind the workspace credentials from the controller and create "
                "a new workspace; unfinished monitor operations and validations were settled "
                "as failed, never as succeeded."
            ),
            (
                "Credential material was revoked or otherwise denied while the workspace was "
                "executing or monitoring its PR, so the hosted runtime tore down its stack."
            ),
            "awf workspace show <workspace_id>",
            "docs/REASON_CATALOG.md#hosted_runtime_stopped",
        ),
        "HOSTED_AUTHORITY_UNAVAILABLE": reason_text_cls(
            (
                "The hosted node stopped this workspace after authority could not be "
                "confirmed for the full grace window (lapsed materials or transient "
                "node-local check errors)."
            ),
            (
                "Inspect controller renewal health and node database/storage connectivity; "
                "create a new workspace once materials can be renewed. Unfinished monitor "
                "operations and validations were settled as failed."
            ),
            (
                "Protected material lapsed without a timely successor, or consecutive "
                "SQLAlchemy/OS/timeout errors exhausted hosted_authority_grace_seconds."
            ),
            "awf workspace show <workspace_id>",
            "docs/REASON_CATALOG.md#hosted_authority_unavailable",
        ),
        "PROTECTED_MATERIAL_LAPSED": reason_text_cls(
            (
                "Current protected material expired without a renewed successor; the grant "
                "is otherwise healthy (not revoked, receipt present, bindings match)."
            ),
            (
                "Ensure the controller renews materials before expiry. Within "
                "hosted_authority_grace_seconds the node keeps the stack but will not "
                "issue or republish lapsed material bytes."
            ),
            (
                "The consumer did not renew short-lived materials before expires_at, or "
                "the receiver reported the current binding as expired without revocation."
            ),
            "awf workspace show <workspace_id>",
            "docs/REASON_CATALOG.md#protected_material_lapsed",
        ),
        "TERMINAL_OWNED_ROW_REAPED": reason_text_cls(
            (
                "A row still open for a workspace that finished on this node at least ten "
                "minutes ago was closed so the idle node can park."
            ),
            (
                "No action is required. The workspace event lists the row kind, action and "
                "ids; inspect them if the leftover row points at a lifecycle bug."
            ),
            (
                "A terminal transition left an expired claim, operation, validation, secret "
                "lease, reservation, task attempt or undeliverable callback behind."
            ),
            "awf workspace show <workspace_id>",
            "docs/REASON_CATALOG.md#terminal_owned_row_reaped",
        ),
        "TERMINAL_OWNED_ROW_REAP_FAILED": reason_text_cls(
            "The hosted worker's terminal-owned-row reaper failed unexpectedly.",
            (
                "Inspect the worker logs for the failure; the reaper retries on its next "
                "interval and leaves the node non-quiescent until the rows are closed."
            ),
            "An unexpected database or programming error occurred during the sweep.",
            "awf service logs --follow --service worker",
            "docs/REASON_CATALOG.md#terminal_owned_row_reap_failed",
        ),
        "HOSTED_REAUTHORIZATION_REQUIRED": reason_text_cls(
            (
                "A hosted guide without a re-authorization pair tried to resume paused or "
                "failed work, or carried grants that require fresh quiet-state materials."
            ),
            (
                "Retry the guide with a re-authorization pair once the workspace is quiet, "
                "or guide only while status remains monitoring_pr without grants."
            ),
            (
                "Pair-less hosted guides are limited to an active monitoring_pr monitor; "
                "blocked, failed, and grant-bearing guides need authorize_rebinding."
            ),
            "awf workspace show <workspace_id>",
            "docs/REASON_CATALOG.md#hosted_reauthorization_required",
        ),
    }
