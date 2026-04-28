"""GitHub client — thin wrappers over ``gh`` CLI + GraphQL mutations.

Every call is routed through an ``AsyncCommandRunner``. Production uses
``AsyncioSubprocessRunner``; tests inject ``FakeCommandRunner`` and queue
canned responses. No direct ``subprocess`` or ``requests`` calls in this
module — lets the runner be mocked at one seam and keeps the monitor
loop deterministic under test.

Scope for Phase 1.5 of AWF:

* ``fetch_pr_status`` — one call, returns the fully assembled ``PRStatus``
  (see ``pr_monitor.py``) by combining ``gh pr view --json …`` with a
  GraphQL query for review threads + review-level comments. Keeping the
  parsing in one place means fake data only has to match one shape.
* ``resolve_thread`` — posts the ``resolveReviewThread`` GraphQL mutation.
* ``post_comment`` — generic ``gh pr comment`` for reply-replies and
  release-PR "ready to merge" notifications.
* ``merge_pr`` — ``gh pr merge …``.

The repo argument is passed explicitly each call so the client is
stateless and thread-safe.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from awf.common.commands import AsyncCommandRunner
from awf.common.logging import get_logger
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    PRStatus,
    ReviewComment,
    ReviewThread,
)

_log = get_logger(__name__)


class GitHubClientError(Exception):
    """Raised when ``gh`` or GraphQL returns a non-zero exit / error payload."""

    def __init__(self, *, operation: str, returncode: int, stderr: str) -> None:
        self.operation = operation
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"{operation} failed (exit={returncode}): {stderr.strip() or '<no output>'}"
        )


# GraphQL: fetch PR state + review threads + review comments in one query.
# The changed-file list feeds merge policy, so it is paginated below whenever
# GitHub reports more than the first 100 paths.
_GQL_PR_STATE = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      number
      headRefOid
      mergeable
      mergeStateStatus
      isDraft
      closed
      merged
      baseRef { name target { ... on Commit { oid } } }
      commits(last: 1) {
        nodes {
          commit {
            statusCheckRollup {
              state
              contexts(first: 100) {
                nodes {
                  __typename
                  ... on CheckRun {
                    name
                    status
                    conclusion
                    startedAt
                    completedAt
                    detailsUrl
                    checkSuite {
                      app {
                        slug
                        name
                      }
                      creator {
                        login
                      }
                    }
                  }
                  ... on StatusContext {
                    context
                    state
                    targetUrl
                    creator {
                      login
                    }
                  }
                }
                pageInfo { hasNextPage }
              }
            }
          }
        }
      }
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 1) {
            nodes {
              bodyText
              author { login }
            }
          }
        }
      }
      reviews(first: 100) {
        nodes {
          databaseId
          body
          state
          author { login }
        }
      }
      comments(first: 100) {
        nodes {
          databaseId
          body
          isMinimized
          author { login }
        }
      }
      files(first: 100) {
        nodes { path }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


_GQL_PR_FILES_PAGE = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      files(first: 100, after: $cursor) {
        nodes { path }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


# GraphQL: mutation to resolve a review thread by node ID.
_GQL_RESOLVE_THREAD = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
""".strip()


@dataclass(frozen=True)
class RepoRef:
    """Owner + repo name parsed out of URLs like
    ``git@github.com:org/repo.git`` or ``https://github.com/org/repo``."""

    owner: str
    name: str

    @classmethod
    def from_url(cls, repo_url: str) -> RepoRef:
        # SSH form: ``git@github.com:owner/repo.git``
        m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url)
        if not m:
            raise ValueError(f"Cannot parse GitHub repo from URL: {repo_url!r}")
        return cls(owner=m.group(1), name=m.group(2))

    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


class GitHubClient:
    """Stateless façade over ``gh`` CLI + GraphQL. Re-entrant."""

    def __init__(self, runner: AsyncCommandRunner) -> None:
        self._runner = runner

    async def fetch_pr_status(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        base_behind_count: int,
    ) -> PRStatus:
        """Single GraphQL round-trip; assembles a ``PRStatus``.

        ``base_behind_count`` is computed by the caller via local git
        (``git rev-list --count HEAD..origin/<base>``). GitHub's
        ``mergeable`` field tells us *whether* there's a conflict but not
        the count of commits behind, so we pass it in.
        """
        payload = await self._graphql(
            query=_GQL_PR_STATE,
            variables={
                "owner": repo.owner,
                "repo": repo.name,
                "number": pr_number,
            },
        )
        pr = payload["data"]["repository"]["pullRequest"]
        if pr is None:
            raise GitHubClientError(
                operation="fetch_pr_status",
                returncode=0,
                stderr=f"PR {repo.slug()}#{pr_number} not found",
            )

        # ── Check state ────────────────────────────────────────────────
        rollup = _dig(pr, "commits", "nodes", 0, "commit", "statusCheckRollup")
        check_state_str = (rollup or {}).get("state") or "PENDING"
        check_state = _parse_check_state(check_state_str)
        checks = _parse_check_contexts(rollup)
        if _dig(rollup, "contexts", "pageInfo", "hasNextPage") is True:
            _log.warning(
                "github.check_contexts_truncated",
                repo=repo.slug(),
                pr_number=pr_number,
                fetched_contexts_limit=100,
            )

        # ── Mergeable ──────────────────────────────────────────────────
        mergeable = _parse_mergeable(pr.get("mergeable"))
        merge_state_status = _parse_merge_state_status(pr.get("mergeStateStatus"))

        # ── Review threads: inline ─────────────────────────────────────
        inline: list[ReviewThread] = []
        for node in _dig(pr, "reviewThreads", "nodes") or []:
            if node.get("isResolved") or node.get("isOutdated"):
                continue
            body = ""
            author = None
            first_comment = _dig(node, "comments", "nodes", 0)
            if first_comment:
                body = (first_comment.get("bodyText") or "")[:400]
                author = _dig(first_comment, "author", "login")
            inline.append(
                ReviewThread(
                    thread_id=node["id"],
                    path=node.get("path"),
                    line=node.get("line"),
                    body_excerpt=body,
                    author=author,
                    is_resolved=False,
                )
            )

        # ── Review-level (outside-diff) comments ───────────────────────
        # A "review" is a top-level object that may or may not carry a
        # body; we treat non-empty bodies as outside-diff comments that
        # need to be addressed too (CodeRabbit posts these).
        reviews: list[ReviewComment] = []
        for node in _dig(pr, "reviews", "nodes") or []:
            body = node.get("body") or ""
            if not body.strip():
                continue
            author = _dig(node, "author", "login")
            state = (node.get("state") or "").upper()
            if (
                state != "CHANGES_REQUESTED"
                and _is_known_bot_comment_author(author)
                and _is_non_actionable_bot_review_body(body)
            ):
                continue
            reviews.append(
                ReviewComment(
                    comment_id=str(node["databaseId"]),
                    body_excerpt=body[:400],
                    author=author,
                    is_resolved=False,
                )
            )

        # ── Top-level PR comments ──────────────────────────────────────
        # Review bots sometimes report gating state as an issue comment
        # instead of a review object. Actionable trigger-review blockers
        # still need to gate merge, but non-actionable status comments like
        # "auto reviews are disabled for this base branch" are ignored.
        for node in _dig(pr, "comments", "nodes") or []:
            body = node.get("body") or ""
            if node.get("isMinimized") or not body.strip():
                continue
            if _is_awf_status_issue_comment(body):
                continue
            author = _dig(node, "author", "login")
            if _is_known_bot_comment_author(author) and _is_non_actionable_review_skip_comment(
                body
            ):
                continue
            blocks_merge = _is_merge_blocking_issue_comment(body)
            if not blocks_merge and _is_known_bot_comment_author(author):
                continue
            reviews.append(
                ReviewComment(
                    comment_id=f"issue:{node['databaseId']}",
                    body_excerpt=body[:400],
                    author=author,
                    is_resolved=False,
                    blocks_merge=blocks_merge,
                )
            )

        changed_paths = await self._fetch_changed_paths(
            repo=repo,
            pr_number=pr_number,
            first_page=_dig(pr, "files"),
        )

        return PRStatus(
            number=pr["number"],
            head_sha=pr["headRefOid"],
            mergeable=mergeable,
            check_state=check_state,
            unresolved_inline_threads=tuple(inline),
            unresolved_review_comments=tuple(reviews),
            base_behind_count=base_behind_count,
            merge_state_status=merge_state_status,
            ci_failures=(),  # populated by fetch_failing_check_logs if needed
            checks=checks,
            changed_paths=changed_paths,
            closed=bool(pr.get("closed")),
            merged=bool(pr.get("merged")),
        )

    async def _fetch_changed_paths(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        first_page: Any,
    ) -> tuple[str, ...]:
        changed_path_items = _extract_pr_file_paths(first_page)
        files_page = first_page

        while _dig(files_page, "pageInfo", "hasNextPage") is True:
            cursor = _clean_optional_str(_dig(files_page, "pageInfo", "endCursor"))
            if cursor is None:
                raise GitHubClientError(
                    operation="fetch_pr_status",
                    returncode=0,
                    stderr=(
                        "GitHub PR files pageInfo.hasNextPage was true "
                        "without an endCursor"
                    ),
                )
            payload = await self._graphql(
                query=_GQL_PR_FILES_PAGE,
                variables={
                    "owner": repo.owner,
                    "repo": repo.name,
                    "number": pr_number,
                    "cursor": cursor,
                },
            )
            next_page = _dig(payload, "data", "repository", "pullRequest", "files")
            if not isinstance(next_page, dict):
                raise GitHubClientError(
                    operation="fetch_pr_status",
                    returncode=0,
                    stderr="GitHub PR files pagination response did not include files",
                )
            files_page = next_page
            changed_path_items.extend(_extract_pr_file_paths(files_page))

        return tuple(changed_path_items)

    async def fetch_failing_check_logs(
        self,
        *,
        repo: RepoRef,
        pr_number: int,  # noqa: ARG002 - kept for API consistency with other PR-scoped calls
        head_sha: str,
        log_tail_chars: int = 3000,
    ) -> tuple[CheckFailure, ...]:
        """Fetch logs for failing/timed-out checks via ``gh run view``.

        The GraphQL PR query only surfaces an aggregate ``statusCheckRollup``
        state. For a ``ReportCiFailure`` action we also want the per-check
        log so the coding CLI has something concrete to fix. We list the
        workflow runs for the head SHA, find the failing ones, and grab
        their failed-step logs.
        """
        runs_raw = await self._gh_json(
            [
                "gh",
                "run",
                "list",
                "--repo",
                repo.slug(),
                "--commit",
                head_sha,
                "--json",
                "databaseId,name,conclusion,status",
                "--limit",
                "20",
            ],
            operation="list_runs_for_sha",
        )
        failures: list[CheckFailure] = []
        for run in runs_raw or []:
            conclusion = run.get("conclusion") or ""
            if conclusion.upper() not in {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}:
                continue
            run_id = str(run["databaseId"])
            log = await self._run_gh(
                [
                    "gh",
                    "run",
                    "view",
                    run_id,
                    "--repo",
                    repo.slug(),
                    "--log-failed",
                ],
                operation="view_run_log",
                strict=False,  # logs may be purged; don't fail the monitor
            )
            log_text = log.stdout if log is not None else ""
            failures.append(
                CheckFailure(
                    name=run.get("name") or f"run/{run_id}",
                    conclusion=conclusion.upper(),
                    log_excerpt=_tail(log_text, log_tail_chars),
                )
            )
        return tuple(failures)

    async def resolve_thread(self, *, thread_id: str) -> None:
        await self._graphql(
            query=_GQL_RESOLVE_THREAD,
            variables={"threadId": thread_id},
        )

    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        """Post a top-level PR comment (not a reply to a thread)."""
        result = await self._runner.run(
            [
                "gh",
                "pr",
                "comment",
                str(pr_number),
                "--repo",
                repo.slug(),
                "--body",
                body,
            ],
        )
        if not result.ok:
            raise GitHubClientError(
                operation="gh pr comment",
                returncode=result.returncode,
                stderr=result.stderr,
            )

    async def merge_pr(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        method: str = "squash",
        delete_branch: bool = True,
    ) -> str:
        """Squash-merge (or merge / rebase) and return the merge commit SHA.

        Branch protection may reject the merge — that's how the runner
        decides to fall back to NotifyHuman.
        """
        args = ["gh", "pr", "merge", str(pr_number), "--repo", repo.slug(), f"--{method}"]
        if delete_branch:
            args.append("--delete-branch")
        result = await self._runner.run(args)
        if not result.ok:
            raise GitHubClientError(
                operation="gh pr merge",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        # Merge commit SHA via a follow-up query (merge output is free-form).
        sha_result = await self._runner.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo.slug(),
                "--json",
                "mergeCommit",
                "--jq",
                ".mergeCommit.oid // empty",
            ],
        )
        if not sha_result.ok:
            # Merge succeeded; the SHA fetch is best-effort for logging.
            return ""
        return sha_result.stdout.strip()

    # ── Internals ──────────────────────────────────────────────────────────

    async def _graphql(self, *, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run a GraphQL query/mutation via ``gh api graphql``.

        ``gh api graphql --raw-field query=… -F owner=… -F repo=… -F number=…``
        is the usual invocation. We pass the query via ``-f`` for strings
        and numeric vars via ``-F`` (numeric). Keeping variables typed at
        the call site avoids gh's quirky shell-escaping.
        """
        args = ["gh", "api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            flag = "-F" if isinstance(value, int) else "-f"
            args.extend([flag, f"{key}={value}"])
        result = await self._runner.run(args)
        if not result.ok:
            raise GitHubClientError(
                operation="gh api graphql",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubClientError(
                operation="gh api graphql (json parse)",
                returncode=0,
                stderr=f"{exc}; stdout was: {result.stdout[:400]}",
            ) from exc
        if payload.get("errors"):
            raise GitHubClientError(
                operation="gh api graphql",
                returncode=0,
                stderr=json.dumps(payload["errors"])[:1000],
            )
        return payload  # type: ignore[no-any-return]  # json.loads returns Any; the explicit dict type is the caller's contract

    async def _gh_json(self, args: list[str], *, operation: str) -> Any:
        result = await self._runner.run(args)
        if not result.ok:
            raise GitHubClientError(
                operation=operation,
                returncode=result.returncode,
                stderr=result.stderr,
            )
        if not result.stdout.strip():
            return None
        return json.loads(result.stdout)

    async def _run_gh(self, args: list[str], *, operation: str, strict: bool) -> Any:
        result = await self._runner.run(args)
        if not result.ok and strict:
            raise GitHubClientError(
                operation=operation,
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return result


# ── Tiny helpers kept private to avoid accidental imports ──────────────────


def _dig(obj: Any, *keys: Any) -> Any:
    """Like ``obj.get(k1, {}).get(k2, {}) ...`` but survives lists + None."""
    cur = obj
    for k in keys:
        if cur is None:
            return None
        if isinstance(k, int):
            if not isinstance(cur, list) or k >= len(cur):
                return None
            cur = cur[k]
        else:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
    return cur


def _parse_check_state(value: str) -> CheckState:
    # Rollup values per docs: EXPECTED / ERROR / FAILURE / PENDING / SUCCESS.
    upper = (value or "").upper()
    if upper == "SUCCESS":
        return CheckState.SUCCESS
    if upper in {"FAILURE", "ERROR"}:
        return CheckState.FAILURE
    if upper == "PENDING" or upper == "EXPECTED":
        return CheckState.PENDING
    return CheckState.NEUTRAL


def _parse_check_contexts(rollup: Any) -> tuple[CheckTiming, ...]:
    checks: list[CheckTiming] = []
    for node in _dig(rollup, "contexts", "nodes") or []:
        if not isinstance(node, dict):
            continue
        typename = node.get("__typename")
        if typename == "StatusContext":
            name = _clean_optional_str(node.get("context"))
            if name is None:
                continue
            checks.append(
                CheckTiming(
                    name=name,
                    status=_clean_optional_str(node.get("state")),
                    details_url=_clean_optional_str(node.get("targetUrl")),
                    creator_login=_clean_optional_str(_dig(node, "creator", "login")),
                )
            )
            continue

        name = _clean_optional_str(node.get("name") or node.get("context"))
        if name is None:
            continue
        checks.append(
            CheckTiming(
                name=name,
                status=_clean_optional_str(node.get("status") or node.get("state")),
                conclusion=_clean_optional_str(node.get("conclusion")),
                started_at=_parse_github_datetime(node.get("startedAt")),
                completed_at=_parse_github_datetime(node.get("completedAt")),
                details_url=_clean_optional_str(node.get("detailsUrl") or node.get("targetUrl")),
                app_slug=_clean_optional_str(_dig(node, "checkSuite", "app", "slug")),
                app_name=_clean_optional_str(_dig(node, "checkSuite", "app", "name")),
                creator_login=_clean_optional_str(_dig(node, "checkSuite", "creator", "login")),
            )
        )
    return tuple(checks)


def _extract_pr_file_paths(files_page: Any) -> list[str]:
    paths: list[str] = []
    for node in _dig(files_page, "nodes") or []:
        if not isinstance(node, dict):
            continue
        path = _clean_optional_str(node.get("path"))
        if path is not None:
            paths.append(path)
    return paths


def _clean_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_github_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_mergeable(value: Any) -> MergeableState:
    upper = (value or "").upper()
    if upper == "MERGEABLE":
        return MergeableState.MERGEABLE
    if upper == "CONFLICTING":
        return MergeableState.CONFLICTING
    return MergeableState.UNKNOWN


def _parse_merge_state_status(value: Any) -> MergeStateStatus:
    """GraphQL returns one of: CLEAN / BEHIND / DIRTY / BLOCKED / HAS_HOOKS
    / UNSTABLE / UNKNOWN. Default to UNKNOWN for anything we don't
    recognise — decide() treats UNKNOWN as "wait, don't act"."""
    upper = (value or "").upper()
    try:
        return MergeStateStatus(upper)
    except ValueError:
        return MergeStateStatus.UNKNOWN


def _tail(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return "…[truncated]…\n" + text[-n:]


_KNOWN_BOT_COMMENT_AUTHORS = frozenset(
    {
        "coderabbitai",
        "gemini-code-assist",
        "greptile-apps",
        "chatgpt-codex-connector",
        "github-actions",
    }
)


def _is_known_bot_comment_author(login: str | None) -> bool:
    if not login:
        return False
    lowered = login.lower()
    return lowered in _KNOWN_BOT_COMMENT_AUTHORS or lowered.endswith("[bot]")


def _is_merge_blocking_issue_comment(body: str) -> bool:
    lower = body.lower()
    return "review skipped" in lower and (
        "trigger review" in lower or "auto reviews are disabled" in lower
    )


def _is_non_actionable_review_skip_comment(body: str) -> bool:
    lower = " ".join(body.lower().split())
    return "review skipped" in lower and "auto reviews are disabled" in lower


def _is_awf_status_issue_comment(body: str) -> bool:
    lower = " ".join(body.lower().split())
    return (
        "awf did not auto-merge because" in lower
        or "all 5 awf gates are green" in lower
        or "after the blocker is cleared or a new commit lands, awf will re-verify" in lower
        or lower.startswith("fixed in commit ")
        or lower.startswith("false positive:")
        or lower.startswith("defer:")
    )


def _is_non_actionable_bot_review_body(body: str) -> bool:
    lower = " ".join(body.lower().split())
    if "no feedback" in lower or "no actionable feedback" in lower:
        return True
    if lower.startswith("## code review") and ("this pull request" in lower or "this pr" in lower):
        return True
    return lower.startswith(("this pull request introduces", "this pr introduces"))
