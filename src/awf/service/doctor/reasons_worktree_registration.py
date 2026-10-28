"""Operator-facing doctor reason text for mirror worktree registrations.

Kept beside ``reasons`` so that module stays within the first-party file-line
guardrail; the entries are merged into ``_REASON_TEXT`` at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from awf.service.doctor.reasons_helpers import reason_catalog_link

if TYPE_CHECKING:
    from awf.service.doctor.reasons import _ReasonText


def get_worktree_registration_reasons(
    reason_text_cls: type[_ReasonText],
) -> dict[str, _ReasonText]:
    """Return the worktree-registration lock/reap/repair catalog reason text entries."""
    return {
        "GIT_WORKTREE_REGISTRATION_LOCK_FAILED": reason_text_cls(
            (
                "AWF could not lock a worktree registration on the shared git mirror; "
                "the workspace continues with an unlocked registration."
            ),
            (
                "Inspect the worker log for the git stderr and fix ownership or "
                "permissions of `<mirror>/worktrees/<id>`. AWF retries the lock the "
                "next time it reuses the checkout. Until then, avoid running "
                "`git worktree prune` against that mirror."
            ),
            (
                "The mirror's worktree admin dir is unwritable or its metadata is "
                "damaged, so `git worktree lock` failed. An unlocked registration can "
                "be pruned by a container that cannot see the checkout."
            ),
            "awf service logs --service worker",
            reason_catalog_link("GIT_WORKTREE_REGISTRATION_LOCK_FAILED"),
        ),
        "GIT_WORKTREE_REGISTRATION_REAPED": reason_text_cls(
            (
                "AWF unlocked a registration whose managed checkout was gone so "
                "`git worktree prune` could remove it."
            ),
            (
                "No action required. This records normal cleanup of an AWF-locked "
                "registration whose worktree directory no longer has a `.git`."
            ),
            (
                "A managed checkout under the AWF worktrees root was deleted or "
                "half-removed while its AWF-locked registration stayed on the mirror."
            ),
            "awf service logs --service worker",
            reason_catalog_link("GIT_WORKTREE_REGISTRATION_REAPED"),
        ),
        "GIT_WORKTREE_REGISTRATION_REAP_FAILED": reason_text_cls(
            (
                "AWF could not unlock a dead AWF-locked registration, so "
                "`git worktree prune` kept it on the mirror."
            ),
            (
                "Check ownership and permissions of `<mirror>/worktrees/<id>/locked`, "
                "then rerun `awf service gc` so the in-container worker retries the "
                "cleanup. Do not delete mirror metadata from the host shell."
            ),
            (
                "The `locked` file of a registration whose checkout is gone could not "
                "be removed, usually because of a permission or filesystem error."
            ),
            "awf service gc",
            reason_catalog_link("GIT_WORKTREE_REGISTRATION_REAP_FAILED"),
        ),
        "MIRROR_WORKTREE_REGISTRATION_DEAD": reason_text_cls(
            "Hooks-path repair skipped a mirror worktree registration that git cannot open.",
            (
                "No action is needed for healthy workspaces on the mirror. To clear the "
                "stale entry, run `awf service gc`; if it persists, inspect "
                "`<mirror>/worktrees/<id>` and destroy the owning workspace through AWF."
            ),
            (
                "The registration's admin dir lost `HEAD` or `commondir`, or holds "
                "metadata git rejects, typically after an interrupted worktree removal. "
                "Git cannot open it, so it cannot carry a poisoned hooks path."
            ),
            "awf service gc",
            reason_catalog_link("MIRROR_WORKTREE_REGISTRATION_DEAD"),
        ),
        "MIRROR_WORKTREE_REGISTRATION_VANISHED": reason_text_cls(
            (
                "Hooks-path repair skipped a mirror worktree registration whose checkout "
                "or admin dir disappeared during the repair."
            ),
            (
                "No action required. Another workspace removed or recreated its "
                "worktree concurrently; run `awf service gc` if stale entries remain."
            ),
            (
                "The checkout, its `.git`, or the mirror admin dir was removed between "
                "registration enumeration and the hooks-path probe."
            ),
            "awf service gc",
            reason_catalog_link("MIRROR_WORKTREE_REGISTRATION_VANISHED"),
        ),
    }
