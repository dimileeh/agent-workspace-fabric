"""Pull-request creator — pushes the feature branch and opens a PR via ``gh``.

Two responsibilities:

1. ``git -C <worktree> push -u origin <branch>`` — uploads the commits the
   coding CLI just made to the remote.
2. ``gh pr create --base <base> --head <branch> --title ... --body ...`` —
   opens the PR on GitHub. We capture stdout which contains the PR URL.

We shell out to the ``gh`` CLI (not the GitHub REST API directly) because:
- ``gh`` already handles auth via stored tokens / keyrings / env vars, so
  AWF doesn't need to re-implement that.
- Error messages are familiar to operators who already use ``gh``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from awf.common.commands import AsyncCommandRunner
from awf.common.logging import get_logger

_log = get_logger(__name__)

# gh prints the PR URL as the only non-empty, non-warning line of stdout.
# Matching anywhere tolerates leading "Creating pull request..." noise from
# future gh versions without tight-coupling to a specific release.
_PR_URL_PATTERN = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")


@dataclass(frozen=True)
class PullRequestResult:
    url: str
    branch: str


class PullRequestError(Exception):
    """Raised when push or ``gh pr create`` fails."""

    def __init__(self, *, operation: str, returncode: int, stderr: str) -> None:
        self.operation = operation
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"{operation} failed (exit={returncode}): {stderr.strip() or '<no output>'}"
        )


class PullRequestCreator:
    """Pushes a feature branch and opens a PR via ``gh``."""

    def __init__(self, runner: AsyncCommandRunner) -> None:
        self._runner = runner

    async def push_and_open(
        self,
        *,
        worktree_path: Path,
        branch_name: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> PullRequestResult:
        # Step 1: push the branch.
        push = await self._runner.run(
            ["git", "-C", str(worktree_path), "push", "-u", "origin", branch_name],
        )
        if not push.ok:
            raise PullRequestError(
                operation="git push", returncode=push.returncode, stderr=push.stderr
            )

        # Step 2: open the PR. gh reads auth from ~/.config/gh by default.
        pr = await self._runner.run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                base_branch,
                "--head",
                branch_name,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=str(worktree_path),
        )
        if not pr.ok:
            raise PullRequestError(
                operation="gh pr create", returncode=pr.returncode, stderr=pr.stderr
            )

        url_match = _PR_URL_PATTERN.search(pr.stdout)
        if url_match is None:
            raise PullRequestError(
                operation="gh pr create (no URL in stdout)",
                returncode=0,
                stderr=f"unexpected gh output: {pr.stdout[:500]}",
            )

        url = url_match.group(0)
        _log.info("pr.created", branch=branch_name, url=url)
        return PullRequestResult(url=url, branch=branch_name)
