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
                "(credential binding or grant) was revoked or expired."
            ),
            (
                "Renew or re-bind the workspace credentials from the controller and create "
                "a new workspace; unfinished monitor operations and validations were settled "
                "as failed, never as succeeded."
            ),
            (
                "Credential material renewal was denied while the workspace was executing "
                "or monitoring its PR, so the hosted runtime tore down its stack."
            ),
            "awf workspace show <workspace_id>",
            "docs/REASON_CATALOG.md#hosted_runtime_stopped",
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
                "lease, reservation, task attempt or overdue callback behind."
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
    }
