"""Shared queue builders for the PR-monitor runner integration parts.

The fakes, fixtures and ``_pr_payload`` itself live in
``tests.integration.runtime._pr_monitor_runner_fixtures``, which every part
loads as a pytest plugin. This module holds the queue scripting that is
specific to the three part files, so the parts stay under the 1,500-line
maintainability cap.
"""

from __future__ import annotations

from awf.common.commands import FakeCommandRunner
from tests.integration.runtime._pr_monitor_runner_fixtures import _pr_payload


def _queue_post_action_recheck(cmd: FakeCommandRunner) -> None:
    """Queue the post-action PR terminal re-read that precedes a push/notify (#910).

    ``_post_action_pr_terminal_state`` re-fetches PR state before the monitor
    pushes, pauses into ``blocked``, or posts a needs-human comment, so a
    positional queue has to model that extra read. An open PR is what makes the
    guard fail open and leave the seam's pre-#910 behavior intact.
    """
    cmd.queue_result(returncode=0, stdout=_pr_payload())
