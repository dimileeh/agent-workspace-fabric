"""Shared payload/queue builders for the PR-monitor runner integration parts.

The three part files script the same ``FakeCommandRunner`` FIFO against the same
GraphQL PR-state shape, so both builders live here rather than being duplicated
three ways — and the parts stay under the 1,500-line maintainability cap.
"""

from __future__ import annotations

import json

from awf.common.commands import FakeCommandRunner


def _pr_payload(
    *,
    closed: bool = False,
    merged: bool = False,
    merge_commit_sha: str = "mergecommit1234567890",
    mergeable: str = "MERGEABLE",
    merge_state_status: str = "CLEAN",
    check_state: str = "SUCCESS",
    threads: list[dict] | None = None,
    reviews: list[dict] | None = None,
    comments: list[dict] | None = None,
) -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 42,
                        "headRefOid": "abc123",
                        "mergeable": mergeable,
                        "mergeStateStatus": merge_state_status,
                        "isDraft": False,
                        "closed": closed,
                        "merged": merged,
                        "mergeCommit": {"oid": merge_commit_sha} if merged else None,
                        "baseRef": {"name": "development", "target": {"oid": "base0"}},
                        "commits": {
                            "nodes": [{"commit": {"statusCheckRollup": {"state": check_state}}}]
                        },
                        "reviewThreads": {"nodes": threads or []},
                        "reviews": {"nodes": reviews or []},
                        "comments": {"nodes": comments or []},
                    }
                }
            }
        }
    )


def _queue_post_action_recheck(cmd: FakeCommandRunner) -> None:
    """Queue the post-action PR terminal re-read that precedes a push/notify (#910).

    ``_post_action_pr_terminal_state`` re-fetches PR state before the monitor
    pushes, pauses into ``blocked``, or posts a needs-human comment, so a
    positional queue has to model that extra read. An open PR is what makes the
    guard fail open and leave the seam's pre-#910 behavior intact.
    """
    cmd.queue_result(returncode=0, stdout=_pr_payload())
