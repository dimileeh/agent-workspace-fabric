"""Operator-facing doctor reason text for plan conformance."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from awf.service.doctor.reasons import _ReasonText


def get_plan_conformance_reasons(reason_text_cls: type[_ReasonText]) -> dict[str, _ReasonText]:
    """Return the plan-conformance reason catalog entries."""
    return {
        "PLAN_CONFORMANCE_GAPS_CARRIED": reason_text_cls(
            "Plan conformance exhausted its iterations with gaps and committed workspace changes.",
            "Review the unresolved gaps on the workspace timeline and on the pull request after publication.",
            "The final compare reported gaps while the branch had user-visible changes relative to its base.",
            "awf workspace events <workspace_id>",
            "docs/REASON_CATALOG.md#plan_conformance_gaps_carried",
        ),
        "PR_CONFORMANCE_COMMENT_FAILED": reason_text_cls(
            "AWF could not publish unresolved plan-conformance gaps on the pull request.",
            "Read the forge error and carried gaps in workspace events. Publish the gap notice before resuming PR review.",
            "The forge comment request failed after the branch was pushed.",
            "awf workspace events <workspace_id>",
            "docs/REASON_CATALOG.md#pr_conformance_comment_failed",
        ),
        "PLAN_CONFORMANCE_NEEDS_HUMAN": reason_text_cls(
            "Plan conformance is paused for a specific operator decision.",
            (
                "Read conformance.json or the workspace transition event, then supply the decision "
                "using guide --directive. A file grant alone cannot resume this pause; normal "
                "validation and merge gates remain required."
            ),
            "The agent reported missing operator authority or external dependency input.",
            "awf workspace guide <workspace_id> --directive '<your decision>'",
            "docs/REASON_CATALOG.md#plan_conformance_needs_human",
        ),
        "CONFORMANCE_REQUIRES_AWF_VALIDATION": reason_text_cls(
            (
                "Plan conformance found no deterministic implementation gap, but "
                "AWF-owned validation evidence is missing, stale, or insufficient."
            ),
            (
                "Let AWF run validation and rerun conformance against the persisted "
                "validation provenance and log stream references."
            ),
            (
                "The agent completed the implementation before AWF ran or persisted "
                "the required profile validation gates."
            ),
            "awf workspace show <workspace_id>",
            "docs/REASON_CATALOG.md#conformance_requires_awf_validation",
        ),
    }
