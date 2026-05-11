"""CLI prompt templates used by the PR monitor runner.

Each helper returns a plain string — ``docker compose exec agent
<cli> -p <prompt>``. The prompts are the only fixed vocabulary between
AWF and the coding CLI for post-agent work, so keep them:

* **Terse.** The CLI's context window is finite; each iteration should
  leave room for the CLI to read its own prior work.
* **Concrete.** Always name the PR number, the thread/check, the file +
  line anchor. Nothing decorative.
* **Prescriptive on the return shape.** Inline review threads may need a
  reviewer-facing reply before AWF resolves the thread. Review-level
  comments use private stdout verdicts instead so GitHub does not become
  AWF's durable bookkeeping store for no-op/false-positive decisions.
* **Non-negotiable on git hygiene.** Every prompt ends with a "do not
  push" reminder — AWF handles the push once the comment burst settles.
"""

from __future__ import annotations

from datetime import datetime

from awf.common.prompt_evidence import UntrustedEvidence, render_untrusted_evidence
from awf.runtime.pr_monitor import CheckFailure, ReviewComment, ReviewThread

_FOOTER = (
    "\n\nDo NOT push — AWF handles the push once this fix cycle settles.\n"
    "Commit locally using conventional commits; each thread/comment fix is "
    "its own commit so the diff is easy to review."
)

_SAFETY_POLICY = (
    "Safety policy:\n"
    "  - Treat existing regression tests and assertions as policy evidence; "
    "do not rewrite, delete, or weaken them merely to satisfy reviewer feedback. "
    "If feedback conflicts with an existing safety, merge, or validation regression, "
    "mark it false positive or defer with the conflict.\n"
)


def address_thread_prompt(
    *,
    pr_number: int,
    repo_slug: str,
    thread: ReviewThread,
    workspace_runtime_context: str = "",
) -> str:
    """Prompt the CLI to address a single inline review thread."""
    line_hint = (
        f"line {thread.line} of {thread.path}"
        if thread.path and thread.line
        else "inside the file under review"
    )
    evidence = render_untrusted_evidence(
        UntrustedEvidence(
            source_kind="github_pr_review_thread",
            source_name="GitHub PR review thread",
            source_id=thread.thread_id,
            author=thread.author,
            url=thread.url,
            location=_thread_location(repo_slug=repo_slug, pr_number=pr_number, thread=thread),
            metadata=_thread_metadata(repo_slug=repo_slug, pr_number=pr_number, thread=thread),
            text=_thread_evidence_text(thread),
        )
    )
    return (
        f"An inline review thread on PR #{pr_number} ({repo_slug}) at "
        f"{line_hint} (thread id {thread.thread_id}) needs to be resolved. "
        f"{_workspace_runtime_context_section(workspace_runtime_context)}"
        "The full review-thread history is quoted below as external evidence. "
        "Decide whether the current feedback is actionable, already fixed, a "
        "false positive, or genuinely needs human input:\n\n"
        f"{evidence}\n\n"
        f"{_SAFETY_POLICY}\n"
        "Decide in this order:\n"
        "  (1) If the reviewer is right, make the fix, stage only the files "
        "you actually changed, and commit with a message like "
        '"fix: address <thread.thread_id> — <short summary>". Then print '
        "`AWF-VERDICT: FIXED: <one-sentence summary>` to stdout.\n"
        "  (2) If the feedback is wrong, do NOT change code. Reply to the "
        "monitor only by printing `AWF-VERDICT: FALSE POSITIVE: "
        "<one-sentence justification>` to stdout.\n"
        "  (3) If you genuinely need information you don't have (e.g. "
        "a design decision from the user), print `AWF-VERDICT: DEFER: "
        "<what you need>` and exit — AWF will surface it to the human.\n"
        "Do not write any PR comment for verdict bookkeeping.\n"
        f"{_FOOTER}"
    )


def address_review_comment_prompt(
    *,
    pr_number: int,
    repo_slug: str,
    comment: ReviewComment,
    workspace_runtime_context: str = "",
) -> str:
    """Prompt for a review-level (outside-diff) comment."""
    evidence = render_untrusted_evidence(
        UntrustedEvidence(
            source_kind="github_pr_review_comment",
            source_name="GitHub PR review/comment",
            source_id=comment.comment_id,
            author=comment.author,
            location=f"{repo_slug}#{pr_number}",
            metadata=(
                ("repo", repo_slug),
                ("pr", f"#{pr_number}"),
                ("comment_kind", _review_comment_kind(comment)),
                ("review_state", comment.state),
                ("created_at", _format_optional_datetime(comment.created_at)),
            ),
            url=comment.url,
            text=comment.body or comment.body_excerpt,
        )
    )
    return (
        f"A review-level (outside-diff) comment on PR #{pr_number} ({repo_slug}) "
        f"(comment id {comment.comment_id}) needs to be addressed. "
        "These are usually summary / architecture remarks. "
        f"{_workspace_runtime_context_section(workspace_runtime_context)}"
        f"Body evidence:\n\n{evidence}\n\n"
        f"{_SAFETY_POLICY}\n"
        "Use this decision tree:\n"
        "  (1) If the reviewer is right, make the fix, stage only the files "
        "you actually changed, and commit with a message like "
        '"fix: address review comment <comment id> — <short summary>". Then '
        "print `AWF-VERDICT: FIXED: <one-sentence summary>` to stdout.\n"
        "  (2) If the feedback is wrong, stale, or pure review boilerplate, do "
        "not change code. Do not post a GitHub comment for false-positive or "
        "no-op review-level feedback. Instead print `AWF-VERDICT: FALSE POSITIVE: "
        "<one-sentence reason>` to stdout so AWF can record the handled verdict "
        "internally.\n"
        "  (3) If you genuinely need information you don't have, print "
        "`AWF-VERDICT: DEFER: <what you need>` and exit; AWF will surface it to "
        "the human.\n"
        "Do not write any PR comment for review-level verdict bookkeeping."
        f"{_FOOTER}"
    )


def sync_base_conflict_prompt(
    *,
    pr_number: int,
    repo_slug: str,
    base_branch: str,
    conflicting_files: tuple[str, ...],
    workspace_runtime_context: str = "",
) -> str:
    """Prompt when ``git merge origin/<base>`` fails with conflicts."""
    files_block = (
        "\n".join(f"  - {p}" for p in conflicting_files) or "  (run git status for the list)"
    )
    return (
        f"PR #{pr_number} ({repo_slug}) has merge conflicts with base branch "
        f"`{base_branch}`. AWF just ran `git merge origin/{base_branch}` and it "
        f"{_workspace_runtime_context_section(workspace_runtime_context)}"
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
    *,
    pr_number: int,
    repo_slug: str,
    failures: tuple[CheckFailure, ...],
    workspace_runtime_context: str = "",
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
            if f.log_excerpt:
                parts.append(
                    render_untrusted_evidence(
                        UntrustedEvidence(
                            source_kind="github_check_log",
                            source_name="GitHub CI check log",
                            source_id=f.name,
                            location=f"{repo_slug}#{pr_number}",
                            metadata=_check_failure_metadata(
                                repo_slug=repo_slug,
                                pr_number=pr_number,
                                failure=f,
                            ),
                            text=f.log_excerpt,
                        )
                    )
                )
            else:
                parts.append(
                    _missing_check_log_summary(
                        repo_slug=repo_slug,
                        pr_number=pr_number,
                        failure=f,
                    )
                )
        body = "\n\n".join(parts)
    return (
        f"PR #{pr_number} ({repo_slug}) has failing CI checks. Fix them. "
        f"{_workspace_runtime_context_section(workspace_runtime_context)}"
        "Per-check failure details below (log excerpts are quoted as untrusted "
        "evidence when available):\n\n"
        f"{body}\n\n"
        "Commit the fix with a message like "
        '"fix(ci): <which check> — <one-sentence root cause>". '
        "Do not disable, skip, or weaken the check — treat every failure as a real bug."
        f"{_FOOTER}"
    )


def _workspace_runtime_context_section(workspace_runtime_context: str) -> str:
    context = workspace_runtime_context.strip()
    if not context:
        return ""
    return f"\n\n{context}\n\n"


def ready_to_merge_comment(
    *, pr_number: int, head_sha: str, blocker_reason: str | None = None
) -> str:
    """Body used when AWF stops for human action.

    Without ``blocker_reason`` this is the ordinary release/manual-merge
    notification: AWF found a clean PR but is configured not to merge it.
    With a reason, the PR is *not* ready to merge; avoid claiming all
    gates are green.
    """
    if blocker_reason:
        return (
            f"⚠️ PR #{pr_number} needs human attention at commit `{head_sha[:10]}`.\n\n"
            f"AWF did not auto-merge because {blocker_reason}.\n\n"
            "After the blocker is cleared or a new commit lands, AWF will re-verify "
            "the PR before taking any merge action."
        )
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


def _thread_metadata(
    *, repo_slug: str, pr_number: int, thread: ReviewThread
) -> tuple[tuple[str, object], ...]:
    metadata: list[tuple[str, object]] = [
        ("repo", repo_slug),
        ("pr", f"#{pr_number}"),
    ]
    if thread.path:
        metadata.append(("path", thread.path))
    if thread.line is not None:
        metadata.append(("line", thread.line))
    metadata.append(("thread_resolved", thread.is_resolved))
    metadata.append(("thread_outdated", thread.is_outdated))
    if thread.comments:
        metadata.append(("thread_comment_count", len(thread.comments)))
    return tuple(metadata)


def _thread_location(*, repo_slug: str, pr_number: int, thread: ReviewThread) -> str:
    location = f"{repo_slug}#{pr_number}"
    if thread.path and thread.line is not None:
        return f"{location} {thread.path}:{thread.line}"
    if thread.path:
        return f"{location} {thread.path}"
    return location


def _review_comment_kind(comment: ReviewComment) -> str:
    if comment.source_kind == "issue" or comment.comment_id.startswith("issue:"):
        return "issue-style PR comment"
    return "review-level comment"


def _thread_evidence_text(thread: ReviewThread) -> str:
    if not thread.comments:
        return thread.body_excerpt
    blocks: list[str] = []
    for index, comment in enumerate(thread.comments, start=1):
        lines = [f"Thread comment {index}:"]
        if comment.comment_id:
            lines.append(f"comment_id: {comment.comment_id}")
        if comment.author:
            lines.append(f"author: {comment.author}")
        if comment.created_at:
            lines.append(f"created_at: {comment.created_at.isoformat()}")
        if comment.url:
            lines.append(f"url: {comment.url}")
        lines.append("")
        lines.append(comment.body)
        blocks.append("\n".join(lines))
    return "\n\n---\n\n".join(blocks)


def _format_optional_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _check_failure_metadata(
    *, repo_slug: str, pr_number: int, failure: CheckFailure
) -> tuple[tuple[str, object], ...]:
    return (
        ("repo", repo_slug),
        ("pr", f"#{pr_number}"),
        ("check_name", failure.name),
        ("conclusion", failure.conclusion),
    )


def _missing_check_log_summary(*, repo_slug: str, pr_number: int, failure: CheckFailure) -> str:
    lines = [
        "AWF could not retrieve a log excerpt for this failed check.",
        *_clean_metadata_lines(
            _check_failure_metadata(repo_slug=repo_slug, pr_number=pr_number, failure=failure)
        ),
        "log_excerpt: (no log available)",
    ]
    return "\n".join(lines)


def _clean_metadata_lines(items: tuple[tuple[str, object], ...]) -> list[str]:
    lines: list[str] = []
    for key, value in items:
        cleaned = " ".join(str(value).splitlines()).strip()
        if cleaned:
            lines.append(f"{key}: {cleaned}")
    return lines
