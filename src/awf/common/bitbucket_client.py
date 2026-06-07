"""BitBucket Cloud client — REST API v2.0 over an injected ``httpx.AsyncClient``.

The provider-neutral ``ForgeClient`` peer of ``GitHubClient`` (issue #345 Part 2).
Where GitHub shells out to ``gh`` through an ``AsyncCommandRunner``, BitBucket Cloud
has no first-party CLI for these operations, so this client talks HTTPS directly via
an injected ``httpx.AsyncClient``. Tests inject ``httpx.MockTransport`` and queue
canned responses — the BitBucket parity of the GitHub ``FakeCommandRunner`` seam.

Architecture decisions (issue #345, locked):

* **D1 transport = httpx.** ``__init__`` takes the client + auth; :meth:`from_env`
  builds the production client (``https://api.bitbucket.org``) and auth.
* **D2 shared ``_request``** with exponential backoff honoring ``Retry-After`` on
  HTTP 429, full cursor pagination following BitBucket's ``next`` links, ETag /
  ``If-None-Match`` conditional requests (304 → cached body) on cacheable GETs, and a
  proactive slow-down when ``X-RateLimit-NearLimit`` is set. The ETag cache is bounded
  and keyed per ``(method, path, params)`` (i.e. per repo/PR/resource). BitBucket rate
  limits are per user/token, not per repo.
* **D6 auth = explicit credential mode.** ``basic`` (``email:api_token``) or ``bearer``
  (``Authorization: Bearer <token>``); app passwords are NOT supported. The token is
  registered as an extra redaction secret and the ``Authorization`` header is never
  logged.

Several ``ForgeClient`` methods (``resolve_thread``, ``create_issue``,
``fetch_repo_merge_methods``, ``fetch_branch_*``) carry no PR context in their
signatures. BitBucket needs it, so ``fetch_pr_status`` remembers per-repo PR context
(branches, commits, merge strategies) on the instance, and ``resolve_thread`` recovers
repo/PR/comment from the neutral ``thread_id`` string those statuses encode.
"""

from __future__ import annotations

import base64
import json
import os
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from awf.common.audit import redact_audit_text
from awf.common.bitbucket_client_parsing import (
    _clean_optional_str,
    _tail,
    bb_merge_strategy_for_method,
    build_blocking_reviews,
    build_general_review_comments,
    build_review_threads,
    decode_thread_id,
    extract_diffstat_paths,
    html_href,
    map_bb_merge_methods,
    merge_state_status_for,
    mergeable_state_for,
    parse_check_state,
    parse_check_timings,
    parse_pr_terminal_state,
)
from awf.common.github_client import RepoRef
from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets
from awf.runtime.ci_failure_evidence import extract_ci_failure_evidence, redact_ci_log
from awf.runtime.pr_monitor import CheckFailure, PRStatus

_log = get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.bitbucket.org"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE_SECONDS = 1.0
_DEFAULT_NEAR_LIMIT_DELAY_SECONDS = 1.0
_DEFAULT_ETAG_CACHE_SIZE = 128
_ERROR_BODY_LIMIT = 2000
# BB Cloud returns at most 100 items/page and the endpoints we paginate (comments,
# statuses, diffstat, pipeline steps) have small real-world counts, so 50 pages is
# never reached in practice — it only bounds a runaway/adversarial ``next`` chain.
_DEFAULT_MAX_PAGES = 50
# Some BB Cloud GETs (the PR ``diffstat``/``diff`` endpoints) answer with a 302 to the
# resolved repo-level resource; the shared client does not auto-follow (so each hop is
# origin-checked first). A single hop is the documented shape — the cap only bounds a
# degraded/adversarial redirect chain.
_DEFAULT_MAX_REDIRECTS = 5
# BitBucket Cloud performs every merge asynchronously: a fast merge answers 200 with
# the merged PR, but a slow one answers 202 with a ``Location`` header pointing at
# ``merge/task-status/{task_id}`` that must be polled to a terminal status. These bound
# that poll loop so a stuck task cannot hang the monitor.
_DEFAULT_MAX_MERGE_POLLS = 30
_DEFAULT_MERGE_POLL_DELAY_SECONDS = 2.0

# Reason codes (flow end-to-end: exception → log field → event → policy).
BITBUCKET_AUTH_NOT_CONFIGURED = "BITBUCKET_AUTH_NOT_CONFIGURED"
BITBUCKET_AUTH_FAILED = "BITBUCKET_AUTH_FAILED"
BITBUCKET_API_ERROR = "BITBUCKET_API_ERROR"
# Transport-level failure (connection reset/refused, timeout, DNS) with no HTTP
# status. Distinct from ``BITBUCKET_API_ERROR`` so the PR monitor can tell a
# recoverable network blip apart from a deterministic abort (pagination cap,
# SSRF origin guard) — those also carry ``status=None`` but must fail fast.
BITBUCKET_TRANSPORT_ERROR = "BITBUCKET_TRANSPORT_ERROR"
BITBUCKET_RATE_LIMITED = "BITBUCKET_RATE_LIMITED"
BITBUCKET_PIPELINE_FULL_RERUN = "BITBUCKET_PIPELINE_FULL_RERUN"
BITBUCKET_PIPELINE_NOT_RERUNNABLE = "BITBUCKET_PIPELINE_NOT_RERUNNABLE"
BITBUCKET_ISSUE_TRACKER_DISABLED = "BITBUCKET_ISSUE_TRACKER_DISABLED"
BITBUCKET_MERGE_METHOD_UNSUPPORTED = "BITBUCKET_MERGE_METHOD_UNSUPPORTED"
# BitBucket answers 409 Conflict on the merge POST when a merge for this PR is
# already in flight (typically a prior 202 async merge whose task-status poll was
# interrupted by a transient fault and re-issued by the monitor). It is recoverable
# — re-polling ``fetch_pr_status`` observes the original merge's eventual MERGED
# state — so it is classified transient rather than mapped to ``BITBUCKET_API_ERROR``.
BITBUCKET_MERGE_IN_PROGRESS = "BITBUCKET_MERGE_IN_PROGRESS"

_FrozenParams = tuple[tuple[str, str], ...]


class BitBucketClientError(Exception):
    """Raised when BitBucket Cloud returns an error or an unusable payload.

    Mirrors ``GitHubClientError``: it carries a redacted body, the failing
    operation, the HTTP status (``None`` for transport errors), and a stable
    ``reason_code`` so the failure flows end-to-end. Catch this specifically,
    never bare ``Exception``.
    """

    def __init__(
        self,
        *,
        operation: str,
        status: int | None,
        body: str,
        reason_code: str = BITBUCKET_API_ERROR,
    ) -> None:
        """Store redacted failure context for monitor diagnostics."""
        self.operation = operation
        self.status = status
        self.reason_code = reason_code
        self.body = redact_audit_text(body)
        status_label = "n/a" if status is None else str(status)
        super().__init__(
            f"{operation} failed (status={status_label}): {self.body.strip() or '<no output>'}"
        )


@dataclass(frozen=True)
class BitBucketAuth:
    """Explicit BitBucket Cloud credential mode (issue #345 decision D6)."""

    mode: str
    api_token: str
    email: str | None = None

    def header_value(self) -> str:
        """Return the ``Authorization`` header value for this credential mode."""
        if self.mode == "basic":
            raw = f"{self.email}:{self.api_token}".encode()
            return "Basic " + base64.b64encode(raw).decode("ascii")
        return f"Bearer {self.api_token}"

    def secret_values(self) -> tuple[str, ...]:
        """Return secret strings that must be redacted from any logged text."""
        secrets = [self.api_token]
        if self.mode == "basic" and self.email:
            encoded = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode("ascii")
            secrets.append(encoded)
        return tuple(secret for secret in secrets if secret)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> BitBucketAuth:
        """Build auth from the env contract, failing with a reason code if invalid."""
        mode = (env.get("BITBUCKET_AUTH_MODE") or "basic").strip().lower()
        token = (env.get("BITBUCKET_API_TOKEN") or "").strip()
        email = (env.get("BITBUCKET_EMAIL") or "").strip() or None
        if mode not in {"basic", "bearer"}:
            raise BitBucketClientError(
                operation="bitbucket auth",
                status=None,
                body=(
                    f"BITBUCKET_AUTH_MODE must be 'basic' or 'bearer', got {mode!r}. "
                    "App passwords are not supported."
                ),
                reason_code=BITBUCKET_AUTH_NOT_CONFIGURED,
            )
        if not token:
            raise BitBucketClientError(
                operation="bitbucket auth",
                status=None,
                body="BITBUCKET_API_TOKEN is required.",
                reason_code=BITBUCKET_AUTH_NOT_CONFIGURED,
            )
        if mode == "basic" and email is None:
            raise BitBucketClientError(
                operation="bitbucket auth",
                status=None,
                body="BITBUCKET_EMAIL is required for basic auth mode.",
                reason_code=BITBUCKET_AUTH_NOT_CONFIGURED,
            )
        return cls(mode=mode, api_token=token, email=email)


@dataclass(frozen=True)
class _PRContext:
    """Per-repo PR context remembered so repo-less Protocol methods can act."""

    pr_number: int
    source_branch: str | None
    source_sha: str | None
    dest_branch: str | None
    dest_sha: str | None
    merge_strategies: list[str] | None
    default_merge_strategy: str | None

    def is_rerunnable(self) -> bool:
        """True only when a PR pipeline target can be safely reconstructed."""
        return all(
            value is not None
            for value in (
                self.source_branch,
                self.dest_branch,
                self.source_sha,
                self.dest_sha,
            )
        )


class BitBucketClient:
    """Stateful façade over BitBucket Cloud REST v2.0. Re-entrant per repo."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        auth: BitBucketAuth,
        *,
        sleep: Any = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_base_seconds: float = _DEFAULT_BACKOFF_BASE_SECONDS,
        near_limit_delay_seconds: float = _DEFAULT_NEAR_LIMIT_DELAY_SECONDS,
        etag_cache_size: int = _DEFAULT_ETAG_CACHE_SIZE,
        max_pages: int = _DEFAULT_MAX_PAGES,
        max_redirects: int = _DEFAULT_MAX_REDIRECTS,
        max_merge_polls: int = _DEFAULT_MAX_MERGE_POLLS,
        merge_poll_delay_seconds: float = _DEFAULT_MERGE_POLL_DELAY_SECONDS,
    ) -> None:
        """Store the injected HTTP client, auth, and retry/cache policy."""
        self._client = client
        self._auth = auth
        self._secret_values = auth.secret_values()
        self._max_retries = max_retries
        self._max_pages = max_pages
        self._max_redirects = max_redirects
        self._max_merge_polls = max_merge_polls
        self._merge_poll_delay_seconds = merge_poll_delay_seconds
        self._backoff_base_seconds = backoff_base_seconds
        self._near_limit_delay_seconds = near_limit_delay_seconds
        self._etag_cache_size = etag_cache_size
        self._etag_cache: OrderedDict[tuple[str, str, _FrozenParams], tuple[str, Any]] = (
            OrderedDict()
        )
        self._pr_context: dict[str, _PRContext] = {}
        self._account_id: str | None = None
        self._account_id_fetched = False
        if sleep is None:
            import asyncio

            self._sleep = asyncio.sleep
        else:
            self._sleep = sleep

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> BitBucketClient:
        """Build the production client + auth from the BitBucket env contract."""
        resolved_env = os.environ if env is None else env
        auth = BitBucketAuth.from_env(resolved_env)
        client = httpx.AsyncClient(base_url=_DEFAULT_BASE_URL, timeout=_DEFAULT_TIMEOUT)
        return cls(client=client, auth=auth)

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` so its connections release.

        Callers that build a client via :meth:`from_env` should ``aclose()`` it
        (or use ``async with``) when done; an injected client is owned by the
        caller but closing it here is idempotent and harmless.
        """
        await self._client.aclose()

    async def __aenter__(self) -> BitBucketClient:
        """Enter an ``async with`` block, returning this client unchanged."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the underlying HTTP client on ``async with`` exit."""
        await self.aclose()

    # ── Public ForgeClient surface ─────────────────────────────────────────

    async def create_pull_request(
        self,
        *,
        repo: RepoRef,
        base: str,
        head: str,
        title: str,
        body: str,
    ) -> str:
        """Open a PR for ``head`` against ``base`` and return its web URL."""
        payload = {
            "title": title,
            "source": {"branch": {"name": head}},
            "destination": {"branch": {"name": base}},
            "description": body,
        }
        data = await self._request_json(
            "POST",
            self._pr_collection_path(repo),
            operation="bitbucket create_pull_request",
            json_body=payload,
        )
        url = html_href(data)
        if url is None:
            raise BitBucketClientError(
                operation="bitbucket create_pull_request",
                status=None,
                body="BitBucket create-PR response omitted links.html.href",
            )
        return url

    async def fetch_pr_status(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        base_behind_count: int,
    ) -> PRStatus:
        """Assemble a ``PRStatus`` from the PR, commit statuses, and comments.

        ``base_behind_count`` is computed by the caller (local git), matching the
        GitHub contract — BitBucket Cloud has no GitHub-style merge-state signal.
        """
        pr = await self._request_json(
            "GET",
            self._pr_path(repo, pr_number),
            operation="bitbucket fetch_pr_status",
            cache=True,
        )
        if not isinstance(pr, dict):
            raise BitBucketClientError(
                operation="bitbucket fetch_pr_status",
                status=None,
                body=f"PR {repo.slug()}#{pr_number} not found",
            )
        self._remember_pr(repo, pr_number, pr)
        head_sha = self._pr_head_sha(pr)
        if head_sha is None:
            raise BitBucketClientError(
                operation="bitbucket fetch_pr_status",
                status=None,
                body=f"PR {repo.slug()}#{pr_number} has no source commit hash",
            )
        source_branch = _clean_optional_str(
            _as_dict(_as_dict(pr.get("source")).get("branch")).get("name")
        )
        statuses = await self._paginate(
            f"{self._repo_path(repo)}/commit/{quote(head_sha, safe='')}/statuses",
            operation="bitbucket fetch_pr_status statuses",
            params={"refname": source_branch} if source_branch else None,
        )
        comments = await self._paginate(
            f"{self._pr_path(repo, pr_number)}/comments",
            operation="bitbucket fetch_pr_status comments",
            cache=True,
        )
        diffstat = await self._paginate(
            f"{self._pr_path(repo, pr_number)}/diffstat",
            operation="bitbucket fetch_pr_status diffstat",
        )
        account_id = await self._current_account_id()
        merged, closed, merge_commit_sha = parse_pr_terminal_state(pr)
        return PRStatus(
            number=int(pr.get("id") or pr_number),
            head_sha=head_sha,
            mergeable=mergeable_state_for(merged=merged, closed=closed),
            check_state=parse_check_state(statuses),
            unresolved_inline_threads=build_review_threads(
                comments, repo=repo, pr_number=pr_number, account_id=account_id
            ),
            unresolved_review_comments=build_general_review_comments(
                comments, account_id=account_id
            ),
            blocking_reviews=build_blocking_reviews(pr, account_id=account_id),
            base_behind_count=base_behind_count,
            merge_state_status=merge_state_status_for(merged=merged, closed=closed),
            ci_failures=(),  # populated by fetch_failing_check_logs when needed
            checks=parse_check_timings(statuses),
            changed_paths=extract_diffstat_paths(diffstat),
            closed=closed,
            merged=merged,
            merge_commit_sha=merge_commit_sha,
        )

    async def fetch_failing_check_logs(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        head_sha: str,
        log_tail_chars: int = 3000,
        pytest_fallback_commands: Sequence[str] = (),
    ) -> tuple[CheckFailure, ...]:
        """Fetch logs for failing checks via the BitBucket pipeline-lookup chain.

        Commit statuses do not carry pipeline/step UUIDs, so for FAILED statuses we
        locate the pipeline by commit, find its failing steps, and tail each step
        log. External (non-Pipelines) failing statuses have no BitBucket logs and
        fall back to ``pytest_fallback_commands`` (same evidence path as GitHub).
        """
        ctx = self._pr_context.get(repo.slug())
        source_branch = ctx.source_branch if ctx is not None else None
        statuses = await self._paginate(
            f"{self._repo_path(repo)}/commit/{quote(head_sha, safe='')}/statuses",
            operation="bitbucket fetch_failing_check_logs statuses",
            params={"refname": source_branch} if source_branch else None,
        )
        failed = [s for s in statuses if str(s.get("state") or "").upper() in {"FAILED", "STOPPED"}]
        if not failed:
            return ()

        pipeline = await self._find_pipeline_for_commit(repo, head_sha, pr_number, source_branch)
        if pipeline is None:
            return tuple(
                self._external_status_failure(status, pytest_fallback_commands) for status in failed
            )

        pipeline_uuid = _clean_optional_str(pipeline.get("uuid"))
        failing_steps = (
            await self._failing_pipeline_steps(repo, pipeline_uuid)
            if pipeline_uuid is not None
            else []
        )
        if not failing_steps:
            return tuple(
                self._external_status_failure(status, pytest_fallback_commands) for status in failed
            )

        failures: list[CheckFailure] = []
        for step in failing_steps:
            step_uuid = _clean_optional_str(step.get("uuid"))
            raw_log = (
                await self._fetch_step_log(repo, pipeline_uuid, step_uuid, log_tail_chars)
                if step_uuid is not None
                else ""
            )
            step_name = _clean_optional_str(step.get("name")) or f"step/{step_uuid or 'unknown'}"
            evidence = extract_ci_failure_evidence(
                raw_log,
                check_name=step_name,
                pytest_fallback_commands=pytest_fallback_commands,
            )
            failures.append(
                CheckFailure(
                    name=step_name,
                    conclusion="FAILURE",
                    log_excerpt=_tail(redact_ci_log(raw_log), log_tail_chars),
                    run_id=pipeline_uuid,
                    failing_commands=evidence.failing_commands,
                    test_node_ids=evidence.test_node_ids,
                    assertion_snippets=evidence.assertion_snippets,
                    error_summaries=evidence.error_summaries,
                    suggested_repro_commands=evidence.suggested_repro_commands,
                    evidence_warnings=evidence.evidence_warnings,
                )
            )
        # External (non-Pipelines) FAILED/STOPPED statuses — e.g. third-party
        # linters — have no pipeline step backing them, so the per-step pass
        # above never surfaces them. Add them too (via the pytest fallback path)
        # so they still become triageable CheckFailure rows, skipping the
        # pipeline's own commit status which the steps already cover.
        failures.extend(
            self._external_status_failure(status, pytest_fallback_commands)
            for status in failed
            if not _is_pipeline_owned_status(status)
        )
        return tuple(failures)

    async def rerun_failed_workflow_jobs(
        self,
        *,
        repo: RepoRef,
        run_id: str,  # noqa: ARG002 - BitBucket reconstructs the PR target, not a run id
    ) -> None:
        """Rerun the PR pipeline.

        BitBucket Cloud has no failed-only rerun API (UI-only), so this reruns the
        WHOLE pipeline by reconstructing the ``pipeline_pullrequest_target`` from the
        remembered PR context, emitting ``BITBUCKET_PIPELINE_FULL_RERUN``. If the
        target cannot be safely reconstructed, it emits
        ``BITBUCKET_PIPELINE_NOT_RERUNNABLE`` and triggers nothing.
        """
        ctx = self._pr_context.get(repo.slug())
        if ctx is None or not ctx.is_rerunnable():
            _log.warning(
                "bitbucket.pipeline_not_rerunnable",
                repo=repo.slug(),
                reason_code=BITBUCKET_PIPELINE_NOT_RERUNNABLE,
                has_context=ctx is not None,
            )
            raise BitBucketClientError(
                operation="bitbucket rerun_failed_workflow_jobs",
                status=None,
                body=(
                    "BitBucket PR pipeline target could not be reconstructed "
                    "(custom/manual pipeline or missing commit metadata)."
                ),
                reason_code=BITBUCKET_PIPELINE_NOT_RERUNNABLE,
            )
        payload = {
            "target": {
                "type": "pipeline_pullrequest_target",
                "source": ctx.source_branch,
                "destination": ctx.dest_branch,
                "destination_commit": {"hash": ctx.dest_sha},
                "commit": {"type": "commit", "hash": ctx.source_sha},
                "pullrequest": {"id": ctx.pr_number},
                "selector": {"type": "pull-requests", "pattern": "**"},
            }
        }
        await self._request_json(
            "POST",
            f"{self._repo_path(repo)}/pipelines/",
            operation="bitbucket rerun_failed_workflow_jobs",
            json_body=payload,
        )
        _log.info(
            "bitbucket.pipeline_full_rerun",
            repo=repo.slug(),
            pr_number=ctx.pr_number,
            reason_code=BITBUCKET_PIPELINE_FULL_RERUN,
        )

    async def resolve_thread(self, *, thread_id: str) -> None:
        """Resolve a review thread (POST resolves; DELETE would REOPEN — never used)."""
        try:
            owner, name, pr_number, comment_id = decode_thread_id(thread_id)
        except ValueError as exc:
            raise BitBucketClientError(
                operation="bitbucket resolve_thread",
                status=None,
                body=f"unrecognized BitBucket thread id: {thread_id!r}",
            ) from exc
        path = (
            f"/2.0/repositories/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/pullrequests/{pr_number}/comments/{comment_id}/resolve"
        )
        await self._request_json("POST", path, operation="bitbucket resolve_thread")

    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        """Post a top-level PR comment."""
        await self._request_json(
            "POST",
            f"{self._pr_path(repo, pr_number)}/comments",
            operation="bitbucket post_comment",
            json_body={"content": {"raw": body}},
        )

    async def create_issue(self, *, repo: RepoRef, title: str, body: str) -> str:
        """Open a tracking issue and return its URL.

        If the repository issue tracker is disabled (404), fall back to posting the
        content as a PR comment (using remembered PR context), returning that comment
        URL and emitting ``BITBUCKET_ISSUE_TRACKER_DISABLED``. If that fallback POST
        also fails (e.g. a 403 lacking comment permission), the ``BitBucketClientError``
        propagates rather than returning a PR-page URL: nothing durable was captured, so
        the deferred-capture caller must treat it as a failure and downgrade to human
        attention instead of resolving the thread (fail safe, mirroring the GitHub path).
        """
        try:
            data = await self._request_json(
                "POST",
                f"{self._repo_path(repo)}/issues",
                operation="bitbucket create_issue",
                json_body={"title": title, "content": {"raw": body}},
            )
        except BitBucketClientError as exc:
            if exc.status == 404:
                return await self._issue_fallback_to_comment(repo, title, body)
            raise
        return html_href(data) or self._issues_page_url(repo)

    async def fetch_repo_merge_methods(self, *, repo: RepoRef) -> tuple[str, ...]:
        """Return enabled merge methods from the remembered PR destination branch.

        The BitBucket repo object does not expose merge settings, so these come from
        the PR's ``destination.branch.merge_strategies`` captured by
        ``fetch_pr_status``. Without that context (no prior status fetch), returns an
        empty tuple so the merge gate fails conservatively rather than mis-merging.
        """
        ctx = self._pr_context.get(repo.slug())
        if ctx is None:
            _log.warning(
                "bitbucket.merge_methods_without_pr_context",
                repo=repo.slug(),
            )
            return ()
        return map_bb_merge_methods(ctx.merge_strategies)

    async def fetch_branch_pull_request_allowed_merge_methods(
        self,
        *,
        repo: RepoRef,
        branch: str,  # noqa: ARG002 - BitBucket strategies come from the PR dest branch
    ) -> tuple[str, ...] | None:
        """Return base-branch merge-method constraints, or ``None`` when unconstrained.

        BitBucket models this as the destination branch's ``merge_strategies`` (NOT
        branch-restrictions, which are permissions/merge-checks). Absent → ``None``.
        """
        ctx = self._pr_context.get(repo.slug())
        if ctx is None or ctx.merge_strategies is None:
            return None
        return map_bb_merge_methods(ctx.merge_strategies)

    async def merge_pr(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        method: str = "squash",
        delete_branch: bool = True,
    ) -> str:
        """Merge a PR with the given method and return the merge commit hash.

        BitBucket Cloud runs every merge asynchronously: a fast merge answers 200
        with the merged PR object, but a slow one answers 202 Accepted with a
        ``Location`` header pointing at ``merge/task-status/{task_id}`` and no
        ``merge_commit.hash`` yet. That 202 path is polled to a terminal task
        status before deciding success/failure — otherwise a valid long-running
        merge would be misrecorded as a missing-hash ``BitBucketClientError`` even
        though BitBucket may still complete it.
        """
        strategy = bb_merge_strategy_for_method(method)
        if strategy is None:
            raise BitBucketClientError(
                operation="bitbucket merge_pr",
                status=None,
                body=f"unsupported merge method for BitBucket: {method!r}",
                reason_code=BITBUCKET_MERGE_METHOD_UNSUPPORTED,
            )
        merge_path = f"{self._pr_path(repo, pr_number)}/merge"
        try:
            response = await self._request(
                "POST",
                merge_path,
                operation="bitbucket merge_pr",
                json_body={"merge_strategy": strategy, "close_source_branch": delete_branch},
            )
        except BitBucketClientError as exc:
            if exc.status == 409:
                # A 409 means BitBucket already has a merge in flight for this PR.
                # This happens when a prior 202 async merge's task-status poll was
                # interrupted by a transient fault and the monitor re-entered the
                # loop and re-issued the merge. Re-raise with the transient
                # ``BITBUCKET_MERGE_IN_PROGRESS`` reason so the monitor waits and
                # re-polls ``fetch_pr_status`` — observing the original merge's
                # eventual MERGED state — instead of terminating the workspace on a
                # merge that may still be completing.
                raise BitBucketClientError(
                    operation="bitbucket merge_pr",
                    status=exc.status,
                    body=exc.body,
                    reason_code=BITBUCKET_MERGE_IN_PROGRESS,
                ) from exc
            raise
        if response.status_code == 202:
            data: Any = await self._poll_merge_task(response, merge_path)
        else:
            data = self._parse_json(response, "bitbucket merge_pr")
        merge_commit = data.get("merge_commit") if isinstance(data, dict) else None
        if isinstance(merge_commit, dict):
            sha = _clean_optional_str(merge_commit.get("hash"))
            if sha:
                return sha
        # A terminal merge that returns no commit hash is an unusable payload: a
        # silent ``""`` would be recorded downstream as a successful merge with an
        # empty marker. Raise instead so the miss is diagnosable and routes through
        # the monitor's BitBucketClientError handling rather than masking success.
        raise BitBucketClientError(
            operation="bitbucket merge_pr",
            status=None,
            body="BitBucket merge response omitted merge_commit.hash",
        )

    async def _poll_merge_task(self, response: httpx.Response, merge_path: str) -> Any:
        """Poll an async (202) merge task until it reaches a terminal status.

        Returns the ``merge_result`` PR object on ``SUCCESS``; raises a
        ``BitBucketClientError`` if the task reports a non-success terminal status
        or does not complete within the bounded poll budget. The poll loop is
        bounded so a stuck task cannot hang the monitor.
        """
        operation = "bitbucket merge_pr (task-status)"
        poll_url = self._merge_task_poll_url(response, merge_path, operation)
        for attempt in range(self._max_merge_polls):
            if attempt:
                await self._sleep(self._merge_poll_delay_seconds)
            status = self._parse_json(
                await self._request("GET", poll_url, operation=operation),
                operation,
            )
            task_status = ""
            merge_result: Any = None
            if isinstance(status, dict):
                task_status = str(status.get("task_status") or "").upper()
                merge_result = status.get("merge_result")
            if task_status == "SUCCESS":
                return merge_result
            if task_status and task_status != "PENDING":
                # ``response`` is the original 202 POST; the poll GET that surfaced
                # this terminal status carried its own (200) status, so reusing
                # ``response.status_code`` here would misreport every task failure
                # as ``status=202`` and make it indistinguishable from a poll-budget
                # timeout. The diagnostic signal is the task status itself, so omit
                # the HTTP status (rendered ``n/a``) and carry ``task_status`` in the body.
                raise BitBucketClientError(
                    operation=operation,
                    status=None,
                    body=f"BitBucket merge task ended in non-success status {task_status!r}",
                )
        raise BitBucketClientError(
            operation=operation,
            status=response.status_code,
            body=f"BitBucket merge task did not complete within {self._max_merge_polls} polls",
        )

    def _merge_task_poll_url(
        self, response: httpx.Response, merge_path: str, operation: str
    ) -> str:
        """Resolve the merge task-status poll URL from a 202 response.

        Prefers the documented ``Location`` header (origin-checked against the
        forge host, same SSRF guard as pagination ``next``); falls back to a
        ``task_id`` in the body. Raises if neither is present.
        """
        location: str | None = response.headers.get("Location")
        if location:
            self._assert_forge_origin(location, operation, what="merge task-status Location")
            return location
        body = self._parse_json(response, operation)
        task_id = _clean_optional_str(body.get("task_id")) if isinstance(body, dict) else None
        if task_id:
            return f"{merge_path}/task-status/{quote(task_id, safe='')}"
        raise BitBucketClientError(
            operation=operation,
            status=response.status_code,
            body="BitBucket 202 merge response carried no task-status poll location",
        )

    # ── Pipeline-chain internals ───────────────────────────────────────────

    async def _find_pipeline_for_commit(
        self, repo: RepoRef, commit_sha: str, pr_number: int, source_branch: str | None = None
    ) -> dict[str, Any] | None:
        """Return the most recent pipeline for a commit, scoped to the PR.

        Multiple pipelines can target the same commit (a branch pipeline plus a PR
        pipeline, or a later manual run). The failing commit status was already
        scoped by ``refname`` to the PR source branch, so the pipeline lookup must
        apply the same ref context — otherwise the newest pipeline by commit hash
        alone may belong to a different ref and yield step logs that do not match
        the failing status.

        Branch and PR pipelines for the same commit share the source branch, so a
        branch-only match could still pick the wrong (branch) pipeline when it is
        newer. Prefer the pipeline whose ``target.pullrequest.id`` is this PR
        first; only then fall back to branch-ref matching. When candidate pipelines
        expose ref metadata but none match, return ``None`` so the caller falls
        back to the external-status path rather than mis-attributing a wrong-ref
        log; when no pipeline exposes ref metadata, keep the newest as before.
        """
        pipelines = await self._paginate(
            f"{self._repo_path(repo)}/pipelines/",
            operation="bitbucket fetch_failing_check_logs pipelines",
            params={"target.commit.hash": commit_sha, "sort": "-created_on"},
        )
        if not pipelines:
            return None
        pr_matched = [p for p in pipelines if _pipeline_targets_pr(p, pr_number)]
        if pr_matched:
            return pr_matched[0]
        if source_branch is None:
            return pipelines[0]
        matched = [p for p in pipelines if _pipeline_targets_branch(p, source_branch)]
        if matched:
            return matched[0]
        if any(_pipeline_has_ref_info(p) for p in pipelines):
            return None
        return pipelines[0]

    async def _failing_pipeline_steps(
        self, repo: RepoRef, pipeline_uuid: str
    ) -> list[dict[str, Any]]:
        """Return the steps of a pipeline that did not succeed.

        Matches FAILED, plus STOPPED (manually cancelled) and ERROR (infrastructure)
        results, mirroring the ``{"FAILED", "STOPPED"}`` commit-status filter in
        ``fetch_failing_check_logs``. Without STOPPED/ERROR here a stopped pipeline
        finds no failing steps and falls back to ``_external_status_failure``, which
        discards the pipeline UUID and any partial step log.
        """
        steps = await self._paginate(
            f"{self._repo_path(repo)}/pipelines/{quote(pipeline_uuid, safe='')}/steps/",
            operation="bitbucket fetch_failing_check_logs steps",
        )
        failing: list[dict[str, Any]] = []
        for step in steps:
            state = step.get("state")
            result = state.get("result") if isinstance(state, dict) else None
            result_name = result.get("name") if isinstance(result, dict) else None
            if str(result_name or "").upper() in {"FAILED", "STOPPED", "ERROR"}:
                failing.append(step)
        return failing

    async def _fetch_step_log(
        self,
        repo: RepoRef,
        pipeline_uuid: str | None,
        step_uuid: str,
        log_tail_chars: int,
    ) -> str:
        """Tail a pipeline step log via a Range request (best-effort, never raises)."""
        if pipeline_uuid is None:  # pragma: no cover - guarded by caller
            return ""
        path = (
            f"{self._repo_path(repo)}/pipelines/{quote(pipeline_uuid, safe='')}"
            f"/steps/{quote(step_uuid, safe='')}/log"
        )
        return await self._request_text(
            "GET",
            path,
            operation="bitbucket fetch_step_log",
            extra_headers={"Range": f"bytes=-{max(log_tail_chars, 1)}"},
        )

    def _external_status_failure(
        self,
        status: dict[str, Any],
        pytest_fallback_commands: Sequence[str],
    ) -> CheckFailure:
        """Build a ``CheckFailure`` for an external status with no BitBucket logs."""
        name = _clean_optional_str(status.get("name") or status.get("key")) or "external-check"
        evidence = extract_ci_failure_evidence(
            "",
            check_name=name,
            pytest_fallback_commands=pytest_fallback_commands,
        )
        return CheckFailure(
            name=name,
            conclusion="FAILURE",
            log_excerpt="",
            run_id=None,
            failing_commands=evidence.failing_commands,
            test_node_ids=evidence.test_node_ids,
            assertion_snippets=evidence.assertion_snippets,
            error_summaries=evidence.error_summaries,
            suggested_repro_commands=evidence.suggested_repro_commands,
            evidence_warnings=evidence.evidence_warnings,
        )

    # ── Issue-fallback + context internals ─────────────────────────────────

    async def _issue_fallback_to_comment(self, repo: RepoRef, title: str, body: str) -> str:
        """Post the issue content as a PR comment when the tracker is disabled."""
        ctx = self._pr_context.get(repo.slug())
        _log.warning(
            "bitbucket.issue_tracker_disabled",
            repo=repo.slug(),
            reason_code=BITBUCKET_ISSUE_TRACKER_DISABLED,
            has_pr_context=ctx is not None,
        )
        if ctx is None:
            # No PR context to comment on; return a usable URL without crashing.
            return self._issues_page_url(repo)
        comment_body = f"{title}\n\n{body}"
        try:
            data = await self._request_json(
                "POST",
                f"{self._pr_path(repo, ctx.pr_number)}/comments",
                operation="bitbucket create_issue comment-fallback",
                json_body={"content": {"raw": comment_body}},
            )
        except BitBucketClientError as exc:
            # The fallback POST can itself fail (e.g. 403 when the bot lacks comment
            # permission, or a transport error). Do NOT swallow it: no issue was filed
            # and no comment was posted, so nothing durable was captured. Returning a
            # PR-page URL here would let the deferred-capture call site in fix_cycle.py
            # record a capture that never happened and resolve the reviewer's thread,
            # dropping the follow-up. Propagate instead — create_issue re-raises and the
            # call site's BitBucketClientError handler requeues transient blips or
            # downgrades permanent faults to needs_human, matching create_issue's
            # documented fail-safe contract (leave the thread unresolved on failure).
            _log.warning(
                "bitbucket.issue_fallback_comment_failed",
                repo=repo.slug(),
                reason_code=exc.reason_code,
                status=exc.status,
            )
            raise
        return html_href(data) or self._pr_page_url(repo, ctx.pr_number)

    async def _current_account_id(self) -> str | None:
        """Return the authenticated account id (cached) to filter own comments.

        Propagates ``BitBucketClientError`` instead of swallowing it. A silent
        ``/2.0/user`` failure leaves ``account_id`` unset, so the comment parsers
        cannot mark ``viewer_did_author`` and AWF's own PR comments look like
        unresolved external feedback — sending the agent into needless comment
        cycles. Letting the error surface routes transient faults
        (5xx/transport/rate-limit) through the monitor's retry path and fails
        auth/4xx faults fast. Only a successful lookup is cached, so a later poll
        retries after a transient blip.
        """
        if self._account_id_fetched:
            return self._account_id
        data = await self._request_json(
            "GET", "/2.0/user", operation="bitbucket current_user", cache=True
        )
        if isinstance(data, dict):
            self._account_id = _clean_optional_str(data.get("account_id") or data.get("uuid"))
        # Cache only a successful identity resolution; a malformed 200 (non-dict or
        # missing account_id/uuid) must not terminally disable viewer-self filtering,
        # so a later poll retries /2.0/user — matching this method's docstring contract.
        self._account_id_fetched = self._account_id is not None
        return self._account_id

    def _remember_pr(self, repo: RepoRef, pr_number: int, pr: dict[str, Any]) -> None:
        """Capture per-repo PR context for the repo-less Protocol methods."""
        source = _as_dict(pr.get("source"))
        destination = _as_dict(pr.get("destination"))
        dest_branch = _as_dict(destination.get("branch"))
        merge_strategies = dest_branch.get("merge_strategies")
        self._pr_context[repo.slug()] = _PRContext(
            pr_number=pr_number,
            source_branch=_clean_optional_str(_as_dict(source.get("branch")).get("name")),
            source_sha=_clean_optional_str(_as_dict(source.get("commit")).get("hash")),
            dest_branch=_clean_optional_str(dest_branch.get("name")),
            dest_sha=_clean_optional_str(_as_dict(destination.get("commit")).get("hash")),
            merge_strategies=merge_strategies if isinstance(merge_strategies, list) else None,
            default_merge_strategy=_clean_optional_str(dest_branch.get("default_merge_strategy")),
        )

    @staticmethod
    def _pr_head_sha(pr: dict[str, Any]) -> str | None:
        return _clean_optional_str(_as_dict(_as_dict(pr.get("source")).get("commit")).get("hash"))

    # ── Path builders ──────────────────────────────────────────────────────

    def _repo_path(self, repo: RepoRef) -> str:
        return f"/2.0/repositories/{quote(repo.owner, safe='')}/{quote(repo.name, safe='')}"

    def _pr_collection_path(self, repo: RepoRef) -> str:
        return f"{self._repo_path(repo)}/pullrequests"

    def _pr_path(self, repo: RepoRef, pr_number: int) -> str:
        return f"{self._pr_collection_path(repo)}/{pr_number}"

    def _issues_page_url(self, repo: RepoRef) -> str:
        return f"https://bitbucket.org/{repo.owner}/{repo.name}/issues"

    def _pr_page_url(self, repo: RepoRef, pr_number: int) -> str:
        return f"https://bitbucket.org/{repo.owner}/{repo.name}/pull-requests/{pr_number}"

    # ── HTTP core (D2) ─────────────────────────────────────────────────────

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: Any = None,
        params: Mapping[str, str] | None = None,
        cache: bool = False,
    ) -> Any:
        """Send a request and decode JSON, honoring ETag conditional requests."""
        extra_headers: dict[str, str] = {}
        cache_key = (method.upper(), path, _freeze_params(params)) if cache else None
        cached: tuple[str, Any] | None = None
        if cache_key is not None:
            cached = self._etag_cache.get(cache_key)
            if cached is not None:
                extra_headers["If-None-Match"] = cached[0]
        response = await self._request(
            method,
            path,
            operation=operation,
            json_body=json_body,
            params=params,
            extra_headers=extra_headers or None,
        )
        if response.status_code == 304:
            # Use the entry captured before the await: a concurrent task may have
            # evicted it from the bounded cache while the request was in flight.
            if cached is not None:
                if cache_key is not None:
                    self._etag_cache_set(cache_key, cached[0], cached[1])
                return cached[1]
            raise BitBucketClientError(
                operation=operation,
                status=304,
                body="BitBucket returned 304 without a cached body",
            )
        parsed = self._parse_json(response, operation)
        if cache_key is not None:
            etag = response.headers.get("ETag")
            if etag:
                self._etag_cache_set(cache_key, etag, parsed)
        return parsed

    async def _paginate(
        self,
        path: str,
        *,
        operation: str,
        params: Mapping[str, str] | None = None,
        cache: bool = False,
    ) -> list[dict[str, Any]]:
        """Follow BitBucket ``next`` cursor links, collecting all ``values``.

        The traversal is bounded two ways so a misbehaving or adversarial response
        cannot hang or redirect the monitor: a hard page cap (``max_pages``) and an
        origin check on each absolute ``next`` URL (see :meth:`_validate_next_url`).
        """
        values: list[dict[str, Any]] = []
        page = await self._request_json(
            "GET", path, operation=operation, params=params, cache=cache
        )
        pages = 1
        while isinstance(page, dict):
            for value in page.get("values") or []:
                if isinstance(value, dict):
                    values.append(value)
            next_url = page.get("next")
            if not isinstance(next_url, str) or not next_url:
                break
            if pages >= self._max_pages:
                raise BitBucketClientError(
                    operation=operation,
                    status=None,
                    body=(
                        f"BitBucket pagination exceeded the {self._max_pages}-page cap; "
                        "aborting a likely runaway 'next' chain"
                    ),
                    reason_code=BITBUCKET_API_ERROR,
                )
            self._validate_next_url(next_url, operation)
            pages += 1
            page = await self._request_json("GET", next_url, operation=operation)
        return values

    def _validate_next_url(self, next_url: str, operation: str) -> None:
        """Reject a cursor ``next`` link that points off the configured forge host.

        BitBucket's ``next`` is an absolute URL that httpx honors verbatim, bypassing
        ``base_url`` — so a compromised response could steer an authenticated request
        (carrying the ``Authorization`` header) at an internal service (SSRF). A
        relative ``next`` is resolved against the safe ``base_url`` and is allowed.
        """
        self._assert_forge_origin(next_url, operation, what="pagination 'next'")

    def _is_forge_origin(self, url: str) -> bool:
        """Return whether ``url`` is relative or points at the configured forge host.

        A relative ``url`` is resolved against the safe ``base_url`` and is allowed; an
        absolute ``url`` is allowed only when its host (and scheme, if present) match the
        forge ``base_url``.
        """
        parsed = urlsplit(url)
        if not parsed.netloc:
            return True
        base = self._client.base_url
        return parsed.hostname == base.host and (not parsed.scheme or parsed.scheme == base.scheme)

    def _assert_forge_origin(self, url: str, operation: str, *, what: str) -> None:
        """Reject an absolute ``url`` that points off the configured forge host.

        Shared SSRF guard for both pagination ``next`` links and 3xx ``Location``
        targets: either is an absolute URL httpx would honor verbatim (bypassing
        ``base_url``) while still carrying the ``Authorization`` header, so a foreign
        origin must be refused *before* the request is issued. A relative ``url`` is
        resolved against the safe ``base_url`` and is allowed.
        """
        if self._is_forge_origin(url):
            return
        parsed = urlsplit(url)
        raise BitBucketClientError(
            operation=operation,
            status=None,
            body=(
                f"BitBucket {what} pointed at unexpected origin "
                f"{parsed.scheme}://{parsed.hostname}; expected host {self._client.base_url.host}"
            ),
            reason_code=BITBUCKET_API_ERROR,
        )

    async def _request_text(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        extra_headers: Mapping[str, str] | None = None,
    ) -> str:
        """Fetch a raw text body (logs); returns ``""`` on any error response.

        BitBucket's pipeline step-log endpoint answers a documented 307 redirect to an
        off-origin signed log-storage URL. The shared redirect path origin-checks every
        ``Location`` (SSRF guard) and would reject that hop, so log fetches enable
        ``allow_log_redirect`` to follow it — but the forge ``Authorization`` header is
        stripped before the off-origin hop, so credentials never reach the storage host.

        Log fetching is best-effort: a transport timeout/reset on the step-log endpoint
        or its signed storage redirect raises ``BitBucketClientError``, which is tolerated
        the same way a 4xx/5xx response is — by returning ``""``. Without this, the error
        escapes to ``_fetch_status_for_decision``, which treats the whole PR status poll
        as a transient BitBucket fault and retries indefinitely; a persistent log-storage
        outage would then block the monitor from acting on the failing CI.
        """
        try:
            response = await self._request(
                method,
                path,
                operation=operation,
                extra_headers=extra_headers,
                strict=False,
                allow_log_redirect=True,
            )
        except BitBucketClientError:
            return ""
        if response.status_code >= 400:
            return ""
        return response.text

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: Any = None,
        params: Mapping[str, str] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        strict: bool = True,
        allow_log_redirect: bool = False,
    ) -> httpx.Response:
        """Single request with 429/Retry-After backoff and near-limit slow-down."""
        headers = {"Accept": "application/json", "Authorization": self._auth.header_value()}
        if extra_headers:
            headers.update(extra_headers)
        target = path
        target_params = params
        redirects = 0
        while True:
            attempt = 0
            while True:
                try:
                    response = await self._client.request(
                        method, target, json=json_body, params=target_params, headers=headers
                    )
                except httpx.RequestError as exc:
                    raise BitBucketClientError(
                        operation=operation,
                        status=None,
                        body=self._redact(str(exc)),
                        reason_code=BITBUCKET_TRANSPORT_ERROR,
                    ) from exc
                if response.status_code == 429 and attempt < self._max_retries:
                    await self._sleep(self._retry_after_seconds(response, attempt))
                    attempt += 1
                    continue
                break
            await self._maybe_slow_for_near_limit(response)
            location = response.headers.get("Location")
            if not (response.is_redirect and location):
                break
            # BB Cloud's PR diffstat/diff GETs answer 302 → resolved resource. The
            # shared client does not auto-follow, so each hop is origin-checked
            # (SSRF, same guard as pagination ``next``) before being re-issued with
            # the Authorization header. ``Location`` already carries the resolved
            # query, so the original params are dropped on the next hop.
            if redirects >= self._max_redirects:
                raise BitBucketClientError(
                    operation=operation,
                    status=None,
                    body=(
                        f"BitBucket redirects exceeded the {self._max_redirects}-hop cap; "
                        "aborting a likely runaway redirect chain"
                    ),
                    reason_code=BITBUCKET_API_ERROR,
                )
            if allow_log_redirect and not self._is_forge_origin(location):
                # BB's step-log endpoint answers a documented 307 to an off-origin
                # signed storage URL. The redirect comes from an already-authenticated
                # api.bitbucket.org response, so follow it — but drop the forge
                # ``Authorization`` header first so the credential never reaches the
                # storage host (the SSRF guard's core concern). The redirected body is
                # only read as a redacted, truncated log excerpt and never re-acted on.
                headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
            else:
                # All other 3xx hops stay on the forge: each Location is origin-checked
                # (same SSRF guard as pagination ``next``) before being re-issued.
                self._assert_forge_origin(location, operation, what="redirect Location")
            redirects += 1
            target = location
            target_params = None
            # Drop the request body on every redirect hop. ``Location`` already
            # carries the resolved resource, and RFC 7231 §6.4 semantics drop the
            # body for 302/303 redirects. The only body-carrying requests are
            # POSTs (create PR, merge, post comment); none are expected to
            # redirect, but clearing ``json_body`` removes any chance of an action
            # payload (e.g. ``merge_pr``'s ``{"merge_strategy": ...}``) being
            # re-issued verbatim to a redirected URL.
            json_body = None
        if strict and response.status_code >= 400:
            raise self._error_for(response, operation)
        return response

    def _retry_after_seconds(self, response: httpx.Response, attempt: int) -> float:
        """Return the 429 backoff delay, honoring the ``Retry-After`` header."""
        header = response.headers.get("Retry-After")
        if header:
            try:
                delay: float = float(header)
            except ValueError:
                delay = -1.0
            if delay >= 0:
                return delay
        return float(self._backoff_base_seconds * (2**attempt))

    async def _maybe_slow_for_near_limit(self, response: httpx.Response) -> None:
        """Proactively slow down when BitBucket signals it is near the rate limit."""
        near_limit = response.headers.get("X-RateLimit-NearLimit")
        if near_limit and near_limit.strip().lower() in {"true", "1", "yes"}:
            _log.info("bitbucket.rate_limit_near", delay_seconds=self._near_limit_delay_seconds)
            await self._sleep(self._near_limit_delay_seconds)

    def _parse_json(self, response: httpx.Response, operation: str) -> Any:
        """Decode a JSON body, or ``None`` for an empty body."""
        text = response.text
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise BitBucketClientError(
                operation=f"{operation} (json parse)",
                status=response.status_code,
                body=self._redact(f"{exc}; body was: {text[:400]}"),
            ) from exc

    def _error_for(self, response: httpx.Response, operation: str) -> BitBucketClientError:
        """Build a redacted ``BitBucketClientError`` for a non-2xx response.

        A 429 that survives retry exhaustion is mapped to the dedicated
        ``BITBUCKET_RATE_LIMITED`` reason code so quota exhaustion is diagnosably
        distinct from a generic API fault in logs and policy.
        """
        if response.status_code in {401, 403}:
            reason = BITBUCKET_AUTH_FAILED
        elif response.status_code == 429:
            reason = BITBUCKET_RATE_LIMITED
        else:
            reason = BITBUCKET_API_ERROR
        return BitBucketClientError(
            operation=operation,
            status=response.status_code,
            body=self._redact(response.text[:_ERROR_BODY_LIMIT]),
            reason_code=reason,
        )

    def _redact(self, text: str) -> str:
        """Redact credentials/secrets from text before it lands in logs/errors."""
        return redact_secrets(text, extra_secrets=self._secret_values)

    def _etag_cache_set(self, key: tuple[str, str, _FrozenParams], etag: str, body: Any) -> None:
        """Store an ETag + parsed body in the bounded LRU cache."""
        self._etag_cache[key] = (etag, body)
        self._etag_cache.move_to_end(key)
        while len(self._etag_cache) > self._etag_cache_size:
            self._etag_cache.popitem(last=False)


def _as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a dict, else an empty dict (payload guard)."""
    return value if isinstance(value, dict) else {}


def _is_pipeline_owned_status(status: dict[str, Any]) -> bool:
    """True when a commit build-status belongs to BitBucket Pipelines itself.

    Pipelines posts its own commit status (key ``PIPELINE``, url pointing at the
    repo's ``/pipelines/`` results); that status is already covered by the
    per-step failures, so it must not be re-emitted as an external check. Any
    other FAILED/STOPPED status (third-party linters, deploy gates, …) is
    external and should still surface for triage.
    """
    if str(status.get("key") or "").upper() == "PIPELINE":
        return True
    return "/pipelines/" in str(status.get("url") or "")


def _pipeline_targets_branch(pipeline: dict[str, Any], source_branch: str) -> bool:
    """True when a pipeline's target ref is the PR source branch.

    Matches both branch pipelines (``target.ref_name``) and PR pipelines
    (``target.source``), the two ways a pipeline records the ref it ran on.
    """
    target = _as_dict(pipeline.get("target"))
    return source_branch in {
        _clean_optional_str(target.get("ref_name")),
        _clean_optional_str(target.get("source")),
    }


def _pipeline_has_ref_info(pipeline: dict[str, Any]) -> bool:
    """True when a pipeline's target exposes any identity to match against.

    Mirrors every dimension the lookup matches on — ``target.ref_name`` and
    ``target.source`` (branch), plus ``target.pullrequest.id`` (PR). A PR-only
    pipeline row that carries just a ``pullrequest.id`` (for a different PR) is
    still identifiable, so the wrong-ref guard must count it; otherwise it reads
    as "no ref info" and the caller falls back to ``pipelines[0]``, risking
    attaching another PR's step logs to this monitor's failing checks.
    """
    target = _as_dict(pipeline.get("target"))
    if any(_clean_optional_str(target.get(key)) is not None for key in ("ref_name", "source")):
        return True
    return _clean_optional_str(_as_dict(target.get("pullrequest")).get("id")) is not None


def _pipeline_targets_pr(pipeline: dict[str, Any], pr_number: int) -> bool:
    """True when a pipeline ran for THIS pull request.

    Bitbucket PR pipelines carry ``target.pullrequest.id``; branch and manual
    pipelines do not. Matching it distinguishes the PR pipeline from a branch
    pipeline that ran on the same commit and source branch — both match the
    branch ref, so without this their steps could be mis-attributed to the PR.
    """
    pr_id = _as_dict(_as_dict(pipeline.get("target")).get("pullrequest")).get("id")
    return _clean_optional_str(pr_id) == str(pr_number)


def _freeze_params(params: Mapping[str, str] | None) -> _FrozenParams:
    """Freeze query params into a hashable, order-stable cache-key component."""
    if not params:
        return ()
    return tuple(sorted((str(key), str(value)) for key, value in params.items()))
