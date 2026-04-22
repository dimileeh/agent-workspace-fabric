"""CLI prompt templates used by the PR monitor runner.

Each helper returns a plain string — ``docker compose exec agent
<cli> -p <prompt>``. The prompts are the only fixed vocabulary between
AWF and the coding CLI for post-agent work, so keep them:

* **Terse.** The CLI's context window is finite; each iteration should
  leave room for the CLI to read its own prior work.
* **Concrete.** Always name the PR number, the thread/check, the file +
  line anchor. Nothing decorative.
* **Prescriptive on the return shape.** The CLI should either (a) push a
  fix commit and a one-line reply quoting the resolve or (b) reply with
  a short justification that starts with ``FALSE POSITIVE:``. The
  runner parses the reply to mark the thread ``fix_committed`` or
  ``false_positive`` respectively.
* **Non-negotiable on git hygiene.** Every prompt ends with a "do not
  push" reminder — AWF handles the push once the comment burst settles.
"""

from __future__ import annotations

from awf.runtime.pr_monitor import CheckFailure, ReviewComment, ReviewThread

_FOOTER = (
    "\n\nDo NOT push — AWF handles the push once this fix cycle settles.\n"
    "Commit locally using conventional commits; each thread/comment fix is "
    "its own commit so the diff is easy to review."
)


def address_thread_prompt(
    *, pr_number: int, repo_slug: str, thread: ReviewThread
) -> str:
    """Prompt the CLI to address a single inline review thread."""
    line_hint = (
        f"line {thread.line} of {thread.path}"
        if thread.path and thread.line
        else "inside the file under review"
    )
    author = thread.author or "reviewer"
    return (
        f"An inline review thread on PR #{pr_number} ({repo_slug}) at "
        f"{line_hint} (thread id {thread.thread_id}) needs to be resolved. "
        f"{author} wrote:\n\n"
        f"---\n{thread.body_excerpt}\n---\n\n"
        "Decide in this order:\n"
        "  (1) If the reviewer is right, make the fix, stage only the files "
        "you actually changed, and commit with a message like "
        '"fix: address <thread.thread_id> — <short summary>". Then reply '
        f"to the thread (via `gh pr review-thread reply` or equivalent) "
        '"fixed in commit <sha>" so the reviewer can see the link.\n'
        "  (2) If the feedback is wrong, do NOT change code. Reply to the "
        'thread starting with "FALSE POSITIVE:" followed by a concise '
        "one-sentence justification (why the existing code is correct).\n"
        "  (3) If you genuinely need information you don't have (e.g. "
        'a design decision from the user), reply "DEFER: <what you need>" '
        "and exit — AWF will surface it to the human.\n"
        f"{_FOOTER}"
    )


def address_review_comment_prompt(
    *, pr_number: int, repo_slug: str, comment: ReviewComment
) -> str:
    """Prompt for a review-level (outside-diff) comment — CodeRabbit summaries etc."""
    author = comment.author or "reviewer"
    return (
        f"A review-level (outside-diff) comment on PR #{pr_number} ({repo_slug}) "
        f"from {author} (comment id {comment.comment_id}) needs to be addressed. "
        "These are usually summary / architecture remarks from CodeRabbit or similar. "
        f"Body:\n\n---\n{comment.body_excerpt}\n---\n\n"
        "Use the same decision tree as for inline threads: either fix and reply "
        '"fixed in commit <sha>", or reply "FALSE POSITIVE: <one-sentence reason>", '
        'or reply "DEFER: <what you need>". Comment replies should be made with '
        "`gh pr comment` so the review record reflects the resolution."
        f"{_FOOTER}"
    )


def sync_base_conflict_prompt(
    *, pr_number: int, repo_slug: str, base_branch: str, conflicting_files: tuple[str, ...]
) -> str:
    """Prompt when ``git merge origin/<base>`` fails with conflicts."""
    files_block = "\n".join(f"  - {p}" for p in conflicting_files) or "  (run git status for the list)"
    return (
        f"PR #{pr_number} ({repo_slug}) has merge conflicts with base branch "
        f"`{base_branch}`. AWF just ran `git merge origin/{base_branch}` and it "
        "stopped on conflicts in these files:\n\n"
        f"{files_block}\n\n"
        "Resolve each conflict by preserving the intent of BOTH sides (the base "
        "branch's recent commits and this PR's changes). When unsure which side to "
        "favour for a given hunk, prefer the base-branch semantics — reviewers on "
        "this PR will catch the regression in the next round. After resolving, "
        "`git add` the touched files and `git commit` with a message like "
        f'"chore: merge origin/{base_branch} into feature branch".'
        f"{_FOOTER}"
    )


def fix_ci_prompt(
    *, pr_number: int, repo_slug: str, failures: tuple[CheckFailure, ...]
) -> str:
    """Prompt when CI is red. Includes truncated logs for each failing check."""
    if not failures:
        body = (
            "(AWF couldn't retrieve per-check logs — inspect recent workflow runs "
            f"for {repo_slug}#{pr_number} via `gh run list --commit HEAD` and fix.)"
        )
    else:
        parts = []
        for f in failures:
            log = f.log_excerpt or "(no log available)"
            parts.append(
                f"### {f.name} ({f.conclusion})\n\n"
                "```\n"
                f"{log}\n"
                "```"
            )
        body = "\n\n".join(parts)
    return (
        f"PR #{pr_number} ({repo_slug}) has failing CI checks. Fix them. "
        "Per-check logs below (tails only):\n\n"
        f"{body}\n\n"
        "Commit the fix with a message like "
        '"fix(ci): <which check> — <one-sentence root cause>". '
        "Do not disable, skip, or weaken the check — treat every failure as a real bug."
        f"{_FOOTER}"
    )


def ready_to_merge_comment(*, pr_number: int, head_sha: str) -> str:
    """Body used by the release-PR variant to notify the human."""
    return (
        f"✅ PR #{pr_number} is ready to merge at commit `{head_sha[:10]}`.\n\n"
        "All 5 AWF gates are green:\n"
        "1. Inline comments resolved.\n"
        "2. Outside-diff comments addressed.\n"
        "3. CI checks all SUCCESS or SKIPPED.\n"
        "4. Mergeable.\n"
        "5. Base merged into head.\n\n"
        "AWF will not merge this PR automatically — human action required. "
        "If new commits land here, AWF will re-verify all 5 gates and re-post "
        "this message on the new head SHA."
    )
