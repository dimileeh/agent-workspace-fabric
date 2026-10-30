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
        "AGENT_VERDICT_ITEM_START_GIT_CONFIG_SNAPSHOT_FAILED": reason_text_cls(
            (
                "AWF could not save the worktree's Git settings at item start, so it parked the "
                "PR monitor review item without running the agent."
            ),
            (
                "Fix the worktree condition (trim the non-ignored entries, remove the unreadable "
                "or symlinked path, or repair the gitdir linkage), then guide or remonitor the "
                "workspace."
            ),
            (
                "The item-start Git config snapshot failed: the worktree has more than 100k "
                "non-ignored entries, the walk hit an unreadable or symlinked path, or the gitdir "
                "linkage is unusual. Without that baseline AWF cannot safely undo an agent run, "
                "so the item was not attempted and was parked as needs_human instead of failing "
                "the workspace."
            ),
            "awf workspace logs <workspace_id>",
            reason_catalog_link("AGENT_VERDICT_ITEM_START_GIT_CONFIG_SNAPSHOT_FAILED"),
        ),
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
        "REVIEW_THREAD_AUDIT_UNAVAILABLE": reason_text_cls(
            "AWF could not enumerate the PR's review threads to verify thread dispositions.",
            (
                "Check forge connectivity and the token's permissions in the monitor log. AWF "
                "re-runs the audit on the next poll; no READY_TO_MERGE or merge happens meanwhile."
            ),
            (
                "Before READY_TO_MERGE (release) or merge (feature), AWF re-reads every review "
                "thread with exhaustive pagination. A permanent forge fault, an exhausted "
                "transient retry budget, a truncated page, or a missing viewer login fails "
                "closed instead of certifying from a partial list."
            ),
            "awf workspace logs <workspace_id>",
            reason_catalog_link("REVIEW_THREAD_AUDIT_UNAVAILABLE"),
        ),
        "REVIEW_THREAD_DISPOSITION_LEGACY_BASELINE": reason_text_cls(
            (
                "AWF's first disposition audit on this workspace found review threads resolved by "
                "its identity without an AWF disposition reply and recorded them as a legacy "
                "baseline."
            ),
            (
                "No action is required to merge: baseline threads do not block and the "
                "READY_TO_MERGE certificate counts them separately. Inspect the thread ids in the "
                "audit event if any of them should be reopened."
            ),
            (
                "Those threads were resolved before AWF replied in every thread before resolving "
                "it (the #1018 rollout). Only the workspace's first audit records a baseline; any "
                "later unmarked resolution blocks as REVIEW_THREAD_DISPOSITION_UNVERIFIED."
            ),
            "awf workspace events <workspace_id>",
            reason_catalog_link("REVIEW_THREAD_DISPOSITION_LEGACY_BASELINE"),
        ),
        "REVIEW_THREAD_DISPOSITION_AUTO_REOPENED": reason_text_cls(
            (
                "AWF reopened review threads its disposition audit could not verify, so the "
                "next poll re-triages them."
            ),
            (
                "No action is required. AWF re-triages each reopened thread through the normal "
                "fix cycle and posts a fresh disposition in the thread before resolving it. It "
                "reopens a thread at most twice; after that it asks you to reopen it."
            ),
            (
                "A thread AWF's identity resolved had no live disposition: for example a review "
                "bot replied in it after AWF's disposition reply, or a re-adoption on a moved "
                "head retired that reply."
            ),
            "awf workspace events <workspace_id>",
            reason_catalog_link("REVIEW_THREAD_DISPOSITION_AUTO_REOPENED"),
        ),
        "REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_EXHAUSTED": reason_text_cls(
            (
                "AWF did not reopen an unverified review thread itself because it already "
                "reopened it twice on this PR."
            ),
            (
                "Inspect the thread and reopen it if its disposition should be redone; AWF then "
                "re-triages it. If a bot keeps replying after every disposition, resolve the "
                "conversation with the bot's owner first."
            ),
            (
                "Each thread is reopened automatically at most twice over the PR's life, across "
                "re-adoptions, so a thread that keeps losing its disposition cannot loop."
            ),
            "awf workspace events <workspace_id>",
            reason_catalog_link("REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_EXHAUSTED"),
        ),
        "REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_UNSUPPORTED": reason_text_cls(
            (
                "AWF did not reopen an unverified review thread itself because the forge has no "
                "thread-reopen API."
            ),
            "Reopen the listed threads by hand; AWF then re-triages each one.",
            (
                "Automatic reopen needs a forge call to unresolve a review thread; this forge "
                "client does not provide one."
            ),
            "awf workspace events <workspace_id>",
            reason_catalog_link("REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_UNSUPPORTED"),
        ),
        "REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_FAILED": reason_text_cls(
            "AWF tried to reopen an unverified review thread and the forge rejected the call.",
            (
                "Check the token's permissions and the forge error in the monitor log. Reopen "
                "the thread by hand, or wait: AWF retries the reopen on a later poll."
            ),
            (
                "The unresolve mutation failed (for example a permission error or a forge "
                "outage). The workspace keeps monitoring."
            ),
            "awf workspace logs <workspace_id>",
            reason_catalog_link("REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_FAILED"),
        ),
        "REVIEW_THREAD_DISPOSITION_UNVERIFIED": reason_text_cls(
            (
                "AWF refused READY_TO_MERGE / merge because review threads resolved by AWF's "
                "identity carry no AWF disposition reply."
            ),
            (
                "AWF reopens such a thread itself, at most twice, and re-triages it. When the "
                "blocker names a REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_* code, inspect the listed "
                "threads and reopen each one. AWF then re-triages it through the normal fix "
                "cycle and posts a fresh disposition in the thread before resolving it. Do not "
                "resolve it again by hand under AWF's account."
            ),
            (
                "A thread resolved by AWF's own forge identity has no live in-thread "
                "`AWF:thread-disposition` marker — it was resolved silently (for example before "
                "AWF replied before every resolve, or by hand under the same account), a bot "
                "replied after AWF's reply, or a moved-head re-adoption retired that reply. "
                "Threads resolved by other logins do not block."
            ),
            "awf workspace events <workspace_id>",
            reason_catalog_link("REVIEW_THREAD_DISPOSITION_UNVERIFIED"),
        ),
        "THREAD_DISPOSITION_EVIDENCE_MISSING": reason_text_cls(
            (
                "AWF did not resolve a review thread because its FIXED disposition had no fix "
                "commit (or its DEFER had no tracking issue) to cite."
            ),
            (
                "Guide the workspace naming the thread id so the agent re-triages it; do not "
                "resolve the thread by hand. The pushed commits are kept."
            ),
            (
                "AWF replies in the thread with its disposition before every resolve. The fix "
                "commit for this item was not recorded (for example a verdict carried over from "
                "before the reply-before-resolve change), so there was nothing durable to cite."
            ),
            "awf workspace guide <workspace_id> --directive '<thread id>: ...'",
            reason_catalog_link("THREAD_DISPOSITION_EVIDENCE_MISSING"),
        ),
        "THREAD_DISPOSITION_REASON_MISSING": reason_text_cls(
            (
                "AWF did not resolve a review thread because its FALSE POSITIVE or DEFER "
                "disposition had no reason to post."
            ),
            (
                "Guide the workspace naming the thread id so the agent re-triages it with a "
                "one-line reason; do not resolve the thread by hand."
            ),
            (
                "AWF publishes the agent's stated reason in the thread before resolving it. The "
                "verdict carried no reason (for example one recorded before the reason was "
                "captured), so the thread was parked instead of being closed silently."
            ),
            "awf workspace guide <workspace_id> --directive '<thread id>: ...'",
            reason_catalog_link("THREAD_DISPOSITION_REASON_MISSING"),
        ),
        "THREAD_DISPOSITION_REPLY_FAILED": reason_text_cls(
            (
                "AWF could not post the disposition reply in a review thread, so it left the "
                "thread open and parked it."
            ),
            (
                "Check forge token scopes and connectivity in the monitor log, then guide the "
                "workspace naming the thread id. The pushed commits are kept."
            ),
            (
                "The forge rejected the in-thread reply (permissions, a locked conversation, or "
                "an outage). A thread is never resolved without its reply, and the reply is not "
                "retried blindly because it could be posted twice."
            ),
            "awf workspace logs <workspace_id>",
            reason_catalog_link("THREAD_DISPOSITION_REPLY_FAILED"),
        ),
        "THREAD_DISPOSITION_UNREAD_FEEDBACK": reason_text_cls(
            (
                "AWF did not resolve a review thread because a comment by its own forge "
                "identity, without an AWF disposition marker, preceded its disposition reply."
            ),
            (
                "Read that comment in the thread, then guide the workspace naming the thread id "
                "with what it asks; do not resolve the thread by hand. The pushed commits are kept."
            ),
            (
                "AWF's identity can be the operator's own account. The monitor never reads that "
                "identity's comments as feedback, so the triage behind the reply did not see the "
                "comment; resolving would have closed it unread. The reply was retired."
            ),
            "awf workspace guide <workspace_id> --directive '<thread id>: ...'",
            reason_catalog_link("THREAD_DISPOSITION_UNREAD_FEEDBACK"),
        ),
    }
