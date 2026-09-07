"""Operator-facing doctor reason text for the PR-comment repair paths.

Extracted from ``reasons`` so that module stays within the first-party file-line
guardrail (``tests/unit/test_core_decomposition_maintainability.py``). Behavior is
unchanged: the entries are merged back into ``_REASON_TEXT`` at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from awf.service.doctor.reasons_helpers import reason_catalog_link

if TYPE_CHECKING:
    from awf.service.doctor.reasons import _ReasonText


def get_comment_repair_reasons(
    reason_text_cls: type[_ReasonText],
) -> dict[str, _ReasonText]:
    """Return the comment-repair catalog reason text entries."""
    return {
        "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED": reason_text_cls(
            "AWF could not verify the remote PR head before abandoning unpublished comment repairs.",
            (
                "Restore forge connectivity and credentials, verify the PR head, then remonitor the "
                "workspace."
            ),
            (
                "A comment-repair cycle stopped before publication, and the forge head could not be "
                "read or did not match the expected repository identity."
            ),
            "awf workspace logs <workspace_id>",
            reason_catalog_link("COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"),
        ),
        "COMMENT_REPAIR_ROLLBACK_FAILED": reason_text_cls(
            "AWF could not reset unpublished comment repairs to the verified remote PR head.",
            (
                "Preserve the workspace for diagnosis, inspect Git and ownership errors in worker logs, "
                "then repair the worktree before remonitoring."
            ),
            (
                "AWF verified the remote head but the local hard reset or post-reset verification failed."
            ),
            "awf service logs --service worker",
            reason_catalog_link("COMMENT_REPAIR_ROLLBACK_FAILED"),
        ),
        "COMMENT_REPAIR_UNPUBLISHED_ABANDONED": reason_text_cls(
            "AWF discarded an interrupted set of unpublished PR-comment repair commits.",
            (
                "No action is normally required. If monitoring does not resume, inspect workspace logs "
                "and remonitor the workspace."
            ),
            (
                "The repair cycle ended before push, so AWF reset the local worktree to the verified "
                "remote PR head to prevent stale unpublished commits from contaminating a later cycle."
            ),
            "awf workspace show <workspace_id>",
            reason_catalog_link("COMMENT_REPAIR_UNPUBLISHED_ABANDONED"),
        ),
        "COMMENT_REPAIR_UNPUBLISHED_PRESERVED": reason_text_cls(
            "AWF resumed an interrupted PR-comment repair from its unpushed local commits.",
            (
                "No action is required. The commits are pushed with the next monitor cycle; watch "
                "the PR for the repair push."
            ),
            (
                "A restart interrupted the repair batch mid-way. The local commits ahead of the PR "
                "head carry comment-repair provenance, so AWF kept them and continued the batch "
                "instead of discarding accepted review fixes."
            ),
            "awf workspace show <workspace_id>",
            reason_catalog_link("COMMENT_REPAIR_UNPUBLISHED_PRESERVED"),
        ),
        "COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING": reason_text_cls(
            (
                "AWF parked comment repair for a human: unpushed local commits could not be "
                "attributed to this repair batch. The commits are preserved."
            ),
            (
                "Inspect the named commits in the workspace worktree, then either keep them (push or "
                "let AWF resume) or drop them manually, and remonitor the workspace."
            ),
            (
                "Local HEAD advanced past the remote PR head without matching comment-repair "
                "provenance, or with conflicting non-comment repair provenance. AWF refused to reset "
                "or push those commits and left the worktree untouched instead of failing the "
                "workspace."
            ),
            "awf workspace logs <workspace_id>",
            reason_catalog_link("COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING"),
        ),
    }
