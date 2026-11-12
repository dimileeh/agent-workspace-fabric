"""Operator-facing doctor reason text for hosted-node profile secret handling.

Kept beside ``reasons`` so that module stays within the first-party file-line
guardrail; the entries are merged into ``_REASON_TEXT`` at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from awf.service.doctor.reasons import _ReasonText


def get_hosted_profile_secret_reasons(
    reason_text_cls: type[_ReasonText],
) -> dict[str, _ReasonText]:
    """Return the hosted profile-secret catalog reason text entries."""
    return {
        "PROTECTED_DECLARATION_UNSUPPORTED": reason_text_cls(
            (
                "The hosted node cannot provide a required profile secret, so the "
                "workspace failed at provisioning before any agent run."
            ),
            (
                "Mark the named secret `required: false` if only local test containers "
                "use it, or remove it from the profile; then create a new workspace."
            ),
            (
                "The profile declares a required secret other than the protected "
                "`agent`/`github` mounts, which are the only secrets a hosted node provides."
            ),
            "awf profile preview . --profile auto",
            "docs/REASON_CATALOG.md#protected_declaration_unsupported",
        ),
        "PROTECTED_DECLARATION_MISMATCH": reason_text_cls(
            (
                "A protected profile secret declaration does not match the hosted "
                "node's protected binding, so the workspace failed at provisioning."
            ),
            (
                "Declare `agent`/`github` secrets as `provider: protected`, `kind: mount`, "
                "`mode: ro`, without `ref`, and with the bound target; then create a new "
                "workspace."
            ),
            (
                "An `agent`/`github` (or `provider: protected`) declaration uses the wrong "
                "provider, kind, mode, ref, or target."
            ),
            "awf profile preview . --profile auto",
            "docs/REASON_CATALOG.md#protected_declaration_mismatch",
        ),
        "HOSTED_OPTIONAL_SECRET_SKIPPED": reason_text_cls(
            (
                "The hosted node skipped optional profile secrets it cannot provide; "
                "they were not materialized and no env var was set."
            ),
            (
                "No action needed unless the workspace needs these secrets; the event "
                "lists the skipped secret names."
            ),
            (
                "The profile declares `required: false` secrets (for example for local "
                "test containers) that only a local node can resolve."
            ),
            "awf workspace logs <workspace_id>",
            "docs/REASON_CATALOG.md#hosted_optional_secret_skipped",
        ),
    }
