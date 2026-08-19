"""Bitbucket Cloud client — REST API v2.0 over an injected ``httpx.AsyncClient``.

The provider-neutral ``ForgeClient`` peer of ``GitHubClient`` (issue #345 Part 2).
Where GitHub shells out to ``gh`` through an ``AsyncCommandRunner``, Bitbucket Cloud
has no first-party CLI for these operations, so this client talks HTTPS directly via
an injected ``httpx.AsyncClient``. Tests inject ``httpx.MockTransport`` and queue
canned responses — the Bitbucket parity of the GitHub ``FakeCommandRunner`` seam.

Architecture decisions (issue #345, locked):

* **D1 transport = httpx.** ``__init__`` takes the client + auth; :meth:`from_env`
  builds the production client (``https://api.bitbucket.org``) and auth.
* **D2 shared ``_request``** (implemented in ``bitbucket_client_http``) with
  exponential backoff honoring ``Retry-After`` on
  HTTP 429, full cursor pagination following Bitbucket's ``next`` links, ETag /
  ``If-None-Match`` conditional requests (304 → cached body) on cacheable GETs, and a
  proactive slow-down when ``X-RateLimit-NearLimit`` is set. The ETag cache is bounded
  and keyed per ``(method, path, params)`` (i.e. per repo/PR/resource). Bitbucket rate
  limits are per user/token, not per repo.
* **D6 auth = explicit credential mode.** ``basic`` (``email:api_token``) or ``bearer``
  (``Authorization: Bearer <token>``); app passwords are NOT supported. The token is
  registered as an extra redaction secret and the ``Authorization`` header is never
  logged.

Several ``ForgeClient`` methods (``resolve_thread``, ``create_issue``,
``fetch_repo_merge_methods``, ``fetch_branch_*``) carry no PR context in their
signatures. Bitbucket needs it, so ``fetch_pr_status`` remembers per-repo PR context
(branches, commits, merge strategies) on the instance, and ``resolve_thread`` recovers
repo/PR/comment from the neutral ``thread_id`` string those statuses encode.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

import httpx

from awf.common.bitbucket_client_errors import (
    BITBUCKET_API_ERROR,
    BITBUCKET_AUTH_FAILED,
    BITBUCKET_AUTH_NOT_CONFIGURED,
    BITBUCKET_COMMIT_RESOLVE_FAILED,
    BITBUCKET_ISSUE_CAPTURE_FAILED,
    BITBUCKET_ISSUE_TRACKER_DISABLED,
    BITBUCKET_MERGE_IN_PROGRESS,
    BITBUCKET_MERGE_METHOD_UNSUPPORTED,
    BITBUCKET_MERGE_TASK_TIMEOUT,
    BITBUCKET_PIPELINE_FULL_RERUN,
    BITBUCKET_PIPELINE_NOT_RERUNNABLE,
    BITBUCKET_RATE_LIMITED,
    BITBUCKET_TASK_RESOLVE_FORBIDDEN,
    BITBUCKET_TRANSPORT_ERROR,
    BitbucketAuth,
    BitbucketClientError,
    _is_bitbucket_merge_in_progress_body,
)
from awf.common.bitbucket_client_http import _BitbucketHttpMixin
from awf.common.bitbucket_client_parsing import (
    _as_dict,
    _clean_optional_str,
    _PRContext,
    _tail,
    bb_merge_strategy_for_method,
    build_blocking_reviews,
    build_general_review_comments,
    build_unresolved_task_threads,
    decode_task_id,
    decode_thread_id,
    effective_merge_strategies,
    extract_diffstat_paths,
    html_href,
    is_pipeline_owned_status,
    is_task_thread_id,
    latest_external_review_activity,
    map_bb_merge_methods,
    merge_state_status_for,
    mergeable_state_for,
    parse_bb_datetime,
    parse_check_state,
    parse_check_timings,
    parse_pr_terminal_state,
    partition_inline_review_threads,
    pipeline_has_ref_info,
    pipeline_targets_branch,
    pipeline_targets_pr,
)
from awf.common.bitbucket_client_paths import _BitbucketUrlsMixin
from awf.common.forge_lifecycle import PullRequestLifecycle
from awf.common.github_client import RepoRef
from awf.common.github_client_parsing import _quiet_period_anchor
from awf.common.logging import get_logger
from awf.runtime.ci_failure_evidence import extract_ci_failure_evidence, redact_ci_log
from awf.runtime.pr_monitor import CheckFailure, CheckFailureLogResult, CheckTiming, PRStatus

_log = get_logger(__name__)

# Public surface. ``BitbucketClientError``, ``BitbucketAuth``, and the reason codes
# now live in ``bitbucket_client_errors`` (file-size guardrail); the shared HTTP
# transport core lives in ``bitbucket_client_http`` for the same reason. Re-export the
# error surface here so ``from awf.common.bitbucket_client import …`` keeps working and
# mypy treats them as explicit exports.
__all__ = [
    "BITBUCKET_API_ERROR",
    "BITBUCKET_AUTH_FAILED",
    "BITBUCKET_AUTH_NOT_CONFIGURED",
    "BITBUCKET_COMMIT_RESOLVE_FAILED",
    "BITBUCKET_ISSUE_CAPTURE_FAILED",
    "BITBUCKET_ISSUE_TRACKER_DISABLED",
    "BITBUCKET_MERGE_IN_PROGRESS",
    "BITBUCKET_MERGE_METHOD_UNSUPPORTED",
    "BITBUCKET_MERGE_TASK_TIMEOUT",
    "BITBUCKET_PIPELINE_FULL_RERUN",
    "BITBUCKET_PIPELINE_NOT_RERUNNABLE",
    "BITBUCKET_RATE_LIMITED",
    "BITBUCKET_TASK_RESOLVE_FORBIDDEN",
    "BITBUCKET_TRANSPORT_ERROR",
    "BitbucketAuth",
    "BitbucketClient",
    "BitbucketClientError",
]

_DEFAULT_BASE_URL = "https://api.bitbucket.org"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE_SECONDS = 1.0
_DEFAULT_NEAR_LIMIT_DELAY_SECONDS = 1.0
_DEFAULT_ETAG_CACHE_SIZE = 128
# BB Cloud returns at most 100 items/page and the endpoints we paginate (comments,
# statuses, diffstat, pipeline steps) have small real-world counts, so 50 pages is
# never reached in practice — it only bounds a runaway/adversarial ``next`` chain.
_DEFAULT_MAX_PAGES = 50
# Some BB Cloud GETs (the PR ``diffstat``/``diff`` endpoints) answer with a 302 to the
# resolved repo-level resource; the shared client does not auto-follow (so each hop is
# origin-checked first). A single hop is the documented shape — the cap only bounds a
# degraded/adversarial redirect chain.
_DEFAULT_MAX_REDIRECTS = 5
# Bitbucket Cloud performs every merge asynchronously: a fast merge answers 200 with
# the merged PR, but a slow one answers 202 with a ``Location`` header pointing at
# ``merge/task-status/{task_id}`` that must be polled to a terminal status. These bound
# that poll loop so a stuck task cannot hang the monitor.
_DEFAULT_MAX_MERGE_POLLS = 30
_DEFAULT_MERGE_POLL_DELAY_SECONDS = 2.0
_TERMINAL_STATUS_STATES = frozenset({"FAILED", "STOPPED", "SUCCESSFUL"})


def _has_non_terminal_status(statuses: Sequence[Mapping[str, Any]]) -> bool:
    """Return whether any commit status is still pending or in progress."""
    return any(
        str(status.get("state") or "").upper() not in _TERMINAL_STATUS_STATES for status in statuses
    )


class BitbucketClient(_BitbucketHttpMixin, _BitbucketUrlsMixin):
    """Stateful façade over Bitbucket Cloud REST v2.0. Re-entrant per repo."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        auth: BitbucketAuth,
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
        self._etag_cache = OrderedDict()
        self._pr_context: dict[str, _PRContext] = {}
        self._account_id: str | None = None
        self._account_id_fetched = False
        if sleep is None:
            import asyncio

            self._sleep = asyncio.sleep
        else:
            self._sleep = sleep

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> BitbucketClient:
        """Build the production client + auth from the Bitbucket env contract."""
        resolved_env = os.environ if env is None else env
        auth = BitbucketAuth.from_env(resolved_env)
        client = httpx.AsyncClient(base_url=_DEFAULT_BASE_URL, timeout=_DEFAULT_TIMEOUT)
        return cls(client=client, auth=auth)

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` so its connections release.

        Callers that build a client via :meth:`from_env` should ``aclose()`` it
        (or use ``async with``) when done; an injected client is owned by the
        caller but closing it here is idempotent and harmless.
        """
        await self._client.aclose()

    async def __aenter__(self) -> BitbucketClient:
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
            raise BitbucketClientError(
                operation="bitbucket create_pull_request",
                status=None,
                body="Bitbucket create-PR response omitted links.html.href",
            )
        return url

    async def fetch_pr_status(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        base_behind_count: int,
        retry: bool = True,
    ) -> PRStatus:
        """Assemble a ``PRStatus`` from the PR, commit statuses, and comments.

        ``base_behind_count`` is computed by the caller (local git), matching the
        GitHub contract — Bitbucket Cloud has no GitHub-style merge-state signal.
        ``retry=False`` (the pre-merge recheck) must suppress the 429 backoff for
        the WHOLE status snapshot, not just the initial PR GET: the commit-SHA
        resolve, every paginated read (statuses, comments, diffstat, tasks), and
        the account-id lookup all thread ``retry`` through, or a transient 429 on
        a later request would still sleep on ``Retry-After`` while the merge
        critical section is meant to fail fast (mirrors the GitHub contract).
        """
        pr = await self._request_json(
            "GET",
            self._pr_path(repo, pr_number),
            operation="bitbucket fetch_pr_status",
            cache=True,
            retry=retry,
        )
        if not isinstance(pr, dict):
            raise BitbucketClientError(
                operation="bitbucket fetch_pr_status",
                status=None,
                body=f"PR {repo.slug()}#{pr_number} not found",
            )
        head_sha = self._pr_head_sha(pr)
        if head_sha is None:
            raise BitbucketClientError(
                operation="bitbucket fetch_pr_status",
                status=None,
                body=f"PR {repo.slug()}#{pr_number} has no source commit hash",
            )
        # Bitbucket Cloud serves ``source.commit.hash`` in the abbreviated 12-char
        # form, but AWF's pre-merge validation-provenance gate matches the PR head
        # against the full 40-char ``ValidationRun.target_head_sha`` by exact
        # equality (#477). Resolve the full hash here so an abbreviated SHA never
        # escapes the adapter: it is what lands on ``PRStatus.head_sha``, keys the
        # commit-statuses fetch below, and is remembered as the rerun pipeline
        # target — all consistently full (the per-commit endpoint accepts both).
        head_sha = await self._resolve_full_commit_sha(repo, head_sha, retry=retry)
        self._remember_pr(repo, pr_number, pr, head_sha=head_sha)
        source_branch = _clean_optional_str(
            _as_dict(_as_dict(pr.get("source")).get("branch")).get("name")
        )
        destination_branch = _clean_optional_str(
            _as_dict(_as_dict(pr.get("destination")).get("branch")).get("name")
        )
        statuses = await self._paginate(
            f"{self._repo_path(repo)}/commit/{quote(head_sha, safe='')}/statuses",
            operation="bitbucket fetch_pr_status statuses",
            params={"refname": source_branch} if source_branch else None,
            retry=retry,
        )
        comments = await self._paginate(
            f"{self._pr_path(repo, pr_number)}/comments",
            operation="bitbucket fetch_pr_status comments",
            cache=True,
            retry=retry,
        )
        diffstat = await self._paginate(
            f"{self._pr_path(repo, pr_number)}/diffstat",
            operation="bitbucket fetch_pr_status diffstat",
            retry=retry,
        )
        # Reviewer tasks are exposed separately from comments; a PR with open tasks
        # but no comments would otherwise assemble empty feedback and reach Merge
        # (issue #445). Cache like comments (it is conditional-request friendly).
        tasks = await self._paginate(
            f"{self._pr_path(repo, pr_number)}/tasks",
            operation="bitbucket fetch_pr_status tasks",
            cache=True,
            retry=retry,
        )
        account_id = await self._current_account_id(retry=retry)
        merged, closed, merge_commit_sha = parse_pr_terminal_state(pr)
        latest_review_at, latest_review_source = latest_external_review_activity(
            comments, account_id=account_id, tasks=tasks
        )
        quiet_anchor_at, quiet_anchor_source = _quiet_period_anchor(
            latest_external_review_activity_at=latest_review_at,
            latest_external_review_activity_source=latest_review_source,
            pr_created_at=parse_bb_datetime(pr.get("created_on")),
            pr_updated_at=parse_bb_datetime(pr.get("updated_on")),
            head_committed_at=None,
        )
        # Single pass over the inline comments yields both feeds: the actionable
        # threads that gate the merge and the outdated-but-unresolved ones the
        # monitor resolves for hygiene (#473). Building them separately would run
        # the group/sort/map pipeline twice per poll.
        actionable_inline_threads, outdated_inline_threads = partition_inline_review_threads(
            comments, repo=repo, pr_number=pr_number, account_id=account_id
        )
        return PRStatus(
            number=int(pr.get("id") or pr_number),
            head_ref=source_branch,
            base_ref=destination_branch,
            head_sha=head_sha,
            mergeable=mergeable_state_for(merged=merged, closed=closed),
            check_state=parse_check_state(statuses),
            # Reviewer tasks join the inline-thread feed (not the comment feed): that
            # gate routes every item to AddressComments regardless of author and
            # resolves through ``resolve_thread`` — which dispatches the ``bbtask:``
            # id to the task PUT. This makes open tasks block merge until addressed.
            unresolved_inline_threads=actionable_inline_threads
            + build_unresolved_task_threads(
                tasks, repo=repo, pr_number=pr_number, account_id=account_id
            ),
            unresolved_review_comments=build_general_review_comments(
                comments, account_id=account_id
            ),
            blocking_reviews=build_blocking_reviews(pr, account_id=account_id),
            base_behind_count=base_behind_count,
            merge_state_status=merge_state_status_for(merged=merged, closed=closed),
            ci_failures=(),  # populated by fetch_failing_check_logs when needed
            checks=parse_check_timings(statuses),
            # Authoritative "no checks observed": the commit-statuses list was
            # fully paginated above, so an empty list means Bitbucket reported
            # zero commit statuses for this head (for example Pipelines disabled).
            no_checks_observed=not statuses,
            changed_paths=extract_diffstat_paths(diffstat),
            closed=closed,
            merged=merged,
            merge_commit_sha=merge_commit_sha,
            latest_external_review_activity_at=latest_review_at,
            latest_external_review_activity_source=latest_review_source,
            quiet_period_anchor_at=quiet_anchor_at,
            quiet_period_anchor_source=quiet_anchor_source,
            # Outdated-but-unresolved inline threads (addressed elsewhere) are
            # dropped from the actionable feed above; surface them so the monitor
            # can resolve the ones it addressed (#473). Reviewer tasks have no
            # outdated concept, so they are intentionally not included here.
            outdated_unresolved_inline_threads=outdated_inline_threads,
        )

    async def fetch_pull_request_lifecycle(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
    ) -> PullRequestLifecycle:
        """Return a PR's lifecycle using one retrying PR read."""
        try:
            pr = await self._request_json(
                "GET",
                self._pr_path(repo, pr_number),
                operation="bitbucket fetch_pull_request_lifecycle",
                cache=True,
            )
        except BitbucketClientError as exc:
            if exc.status == 404:
                return PullRequestLifecycle.missing
            raise
        if not isinstance(pr, dict):
            raise BitbucketClientError(
                operation="bitbucket fetch_pull_request_lifecycle",
                status=None,
                body=f"PR {repo.slug()}#{pr_number} returned an invalid response",
            )
        state = str(pr.get("state") or "").upper()
        if state not in {"OPEN", "MERGED", "DECLINED", "SUPERSEDED"}:
            raise BitbucketClientError(
                operation="bitbucket fetch_pull_request_lifecycle",
                status=None,
                body=f"PR {repo.slug()}#{pr_number} returned unknown state {state or '<empty>'}",
            )
        if state == "OPEN":
            return PullRequestLifecycle.open
        if state == "MERGED":
            return PullRequestLifecycle.merged
        return PullRequestLifecycle.closed

    async def fetch_failing_check_logs(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        head_sha: str,
        log_tail_chars: int = 3000,
        pytest_fallback_commands: Sequence[str] = (),
        rollup_checks: Sequence[CheckTiming] = (),  # noqa: ARG002 - GitHub-only fallback input
    ) -> CheckFailureLogResult:
        """Fetch logs for failing checks via the Bitbucket pipeline-lookup chain.

        Commit statuses do not carry pipeline/step UUIDs, so for FAILED statuses we
        locate the pipeline by commit, find its failing steps, and tail each step
        log. External (non-Pipelines) failing statuses have no Bitbucket logs and
        fall back to ``pytest_fallback_commands`` (same evidence path as GitHub).
        """
        ctx = self._pr_context.get(repo.slug())
        source_branch = ctx.source_branch if ctx is not None else None
        statuses = await self._paginate(
            f"{self._repo_path(repo)}/commit/{quote(head_sha, safe='')}/statuses",
            operation="bitbucket fetch_failing_check_logs statuses",
            params={"refname": source_branch} if source_branch else None,
        )
        runs_in_progress = _has_non_terminal_status(statuses)
        failed = [s for s in statuses if str(s.get("state") or "").upper() in {"FAILED", "STOPPED"}]
        if not failed:
            return CheckFailureLogResult(runs_in_progress=runs_in_progress)
        # Active sibling statuses should not hide failure evidence from the
        # repair loop. Only the no-failure snapshot above is a wait signal.

        pipeline = await self._find_pipeline_for_commit(repo, head_sha, pr_number, source_branch)
        if pipeline is None:
            return CheckFailureLogResult(
                failures=tuple(
                    self._external_status_failure(status, pytest_fallback_commands)
                    for status in failed
                ),
            )

        pipeline_uuid = _clean_optional_str(pipeline.get("uuid"))
        failing_steps = (
            await self._failing_pipeline_steps(repo, pipeline_uuid)
            if pipeline_uuid is not None
            else []
        )
        if not failing_steps:
            return CheckFailureLogResult(
                failures=tuple(
                    self._external_status_failure(status, pytest_fallback_commands)
                    for status in failed
                ),
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
            if not is_pipeline_owned_status(status)
        )
        return CheckFailureLogResult(
            failures=tuple(failures),
        )

    async def rerun_failed_workflow_jobs(
        self,
        *,
        repo: RepoRef,
        run_id: str,  # noqa: ARG002 - Bitbucket reconstructs the PR target, not a run id
    ) -> None:
        """Rerun the PR pipeline.

        Bitbucket Cloud has no failed-only rerun API (UI-only), so this reruns the
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
            raise BitbucketClientError(
                operation="bitbucket rerun_failed_workflow_jobs",
                status=None,
                body=(
                    "Bitbucket PR pipeline target could not be reconstructed "
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
        """Resolve a review thread or reviewer task from its neutral id.

        Forge-neutral dispatch: a ``bbtask:`` id is a reviewer *task*, resolved with
        ``PUT .../tasks/{id}`` ``{"state": "RESOLVED"}``; a ``bb:`` id is an inline
        comment thread, resolved with ``POST .../comments/{id}/resolve`` (DELETE would
        REOPEN — never used). The PUT only commits the resolution *after* the agent has
        addressed the task content (the fix-cycle calls this only for a resolvable
        verdict), and a failed PUT raises so the addressed-state is rolled back and the
        task re-surfaces as still-blocking next poll.
        """
        if is_task_thread_id(thread_id):
            await self._resolve_task(thread_id)
            return
        try:
            owner, name, pr_number, comment_id = decode_thread_id(thread_id)
        except ValueError as exc:
            raise BitbucketClientError(
                operation="bitbucket resolve_thread",
                status=None,
                body=f"unrecognized Bitbucket thread id: {thread_id!r}",
            ) from exc
        path = (
            f"/2.0/repositories/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/pullrequests/{pr_number}/comments/{comment_id}/resolve"
        )
        await self._request_json("POST", path, operation="bitbucket resolve_thread")

    async def _resolve_task(self, thread_id: str) -> None:
        """Mark a reviewer task RESOLVED via ``PUT .../tasks/{id}``.

        A 403 (the token lacks task-resolution permission) is re-raised with the stable
        ``BITBUCKET_TASK_RESOLVE_FORBIDDEN`` reason code — distinct from the generic API
        error so the escalation is diagnosable — because the agent re-addressing the task
        cannot grant a missing scope. Any other failure propagates with its native reason
        code. Either way the PUT raised, so the task stays UNRESOLVED and keeps blocking
        merge. The fix-cycle requeues only transient blips; every *permanent* task-resolve
        failure (403 or otherwise) is downgraded to ``needs_human`` and kept addressed so
        it does NOT re-route to the agent (tasks live in the inline-thread feed, so
        clearing the addressed marker would re-fire ``AddressComments`` against a fault
        the agent cannot fix — a retry storm), while ``decide()`` escalates to NotifyHuman.
        """
        owner, name, pr_number, task_id = decode_task_id(thread_id)
        path = (
            f"/2.0/repositories/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/pullrequests/{pr_number}/tasks/{quote(task_id, safe='')}"
        )
        try:
            await self._request_json(
                "PUT",
                path,
                operation="bitbucket resolve_task",
                json_body={"state": "RESOLVED"},
            )
        except BitbucketClientError as exc:
            if exc.status == 403:
                raise BitbucketClientError(
                    operation="bitbucket resolve_task",
                    status=exc.status,
                    body=exc.body,
                    reason_code=BITBUCKET_TASK_RESOLVE_FORBIDDEN,
                ) from exc
            raise

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
        also fails (e.g. a 403 lacking comment permission), or there is no remembered
        PR context to comment on, the ``BitbucketClientError`` propagates rather than
        returning a PR- or issues-page URL: nothing durable was captured, so the
        deferred-capture caller must treat it as a failure and downgrade to human
        attention instead of resolving the thread (fail safe, mirroring the GitHub path).
        The no-PR-context case carries ``BITBUCKET_ISSUE_CAPTURE_FAILED`` rather than
        ``BITBUCKET_ISSUE_TRACKER_DISABLED`` so the propagated reason code does not
        falsely imply the note was captured on the PR.
        """
        try:
            data = await self._request_json(
                "POST",
                f"{self._repo_path(repo)}/issues",
                operation="bitbucket create_issue",
                json_body={"title": title, "content": {"raw": body}},
            )
        except BitbucketClientError as exc:
            if exc.status == 404:
                return await self._issue_fallback_to_comment(repo, title, body)
            raise
        # Prefer the issue's own ``links.html.href``. If a successful create omits
        # it, build the canonical issue URL from the returned ``id`` so deferred
        # capture still points at the filed item; only when neither is available
        # do we degrade to the generic issues list (better than nothing, but it
        # has no guaranteed link to the specific issue).
        return html_href(data) or self._issue_url_from_id(data, repo) or self._issues_page_url(repo)

    async def fetch_repo_merge_methods(self, *, repo: RepoRef) -> tuple[str, ...]:
        """Return enabled merge methods from the remembered PR destination branch.

        The Bitbucket repo object does not expose merge settings, so these come from
        the PR's ``destination.branch.merge_strategies`` captured by
        ``fetch_pr_status``, falling back to ``default_merge_strategy`` when the
        explicit list is absent or empty. Without any PR context (no prior status
        fetch), returns an empty tuple so the merge gate fails conservatively rather
        than mis-merging.
        """
        ctx = self._pr_context.get(repo.slug())
        if ctx is None:
            _log.warning(
                "bitbucket.merge_methods_without_pr_context",
                repo=repo.slug(),
            )
            return ()
        return map_bb_merge_methods(effective_merge_strategies(ctx))

    async def fetch_branch_pull_request_allowed_merge_methods(
        self,
        *,
        repo: RepoRef,
        branch: str,  # noqa: ARG002 - Bitbucket strategies come from the PR dest branch
    ) -> tuple[str, ...] | None:
        """Return base-branch merge-method constraints, or ``None`` without PR context.

        Bitbucket models this as the destination branch's ``merge_strategies`` (NOT
        branch-restrictions, which are permissions/merge-checks), falling back to
        ``default_merge_strategy`` when that list is absent or empty. Absent both →
        Bitbucket Cloud's default allowed set (an unrestricted repo enumerates no
        strategies but allows all three, #479); ``None`` only without PR context.
        """
        ctx = self._pr_context.get(repo.slug())
        if ctx is None:
            return None
        return map_bb_merge_methods(effective_merge_strategies(ctx))

    async def merge_pr(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        method: str = "squash",
        delete_branch: bool = True,
    ) -> str:
        """Merge a PR with the given method and return the merge commit hash.

        Bitbucket Cloud runs every merge asynchronously: a fast merge answers 200
        with the merged PR object, but a slow one answers 202 Accepted with a
        ``Location`` header pointing at ``merge/task-status/{task_id}`` and no
        ``merge_commit.hash`` yet. That 202 path is polled to a terminal task
        status before deciding success/failure — otherwise a valid long-running
        merge would be misrecorded as a missing-hash ``BitbucketClientError`` even
        though Bitbucket may still complete it.
        """
        strategy = bb_merge_strategy_for_method(method)
        if strategy is None:
            raise BitbucketClientError(
                operation="bitbucket merge_pr",
                status=None,
                body=f"unsupported merge method for Bitbucket: {method!r}",
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
        except BitbucketClientError as exc:
            if exc.status == 409 and _is_bitbucket_merge_in_progress_body(exc.body):
                # A 409 whose body marks an in-flight merge means Bitbucket already
                # has a merge running for this PR — typically a prior 202 async
                # merge whose task-status poll was interrupted by a transient fault
                # and re-issued when the monitor re-entered the loop — or that the PR
                # has already been merged. Re-raise with the transient
                # ``BITBUCKET_MERGE_IN_PROGRESS`` reason so the monitor waits and
                # re-polls ``fetch_pr_status`` — observing the eventual MERGED state
                # — instead of terminating the workspace on a merge that may still be
                # completing. Bitbucket overloads 409 for non-recoverable failures
                # too (conflicts, unmet merge checks); those lack the in-progress
                # signal and fall through to the deterministic ``raise`` below so
                # they surface ``BITBUCKET_API_ERROR`` and notify a human rather
                # than polling forever.
                raise BitbucketClientError(
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
        # the monitor's BitbucketClientError handling rather than masking success.
        raise BitbucketClientError(
            operation="bitbucket merge_pr",
            status=None,
            body="Bitbucket merge response omitted merge_commit.hash",
        )

    async def _poll_merge_task(self, response: httpx.Response, merge_path: str) -> Any:
        """Poll an async (202) merge task until it reaches a terminal status.

        Returns the ``merge_result`` PR object on ``SUCCESS``; raises a
        ``BitbucketClientError`` if the task reports a non-success terminal status,
        answers with an error envelope (a failed merge — conflict, unmet merge
        checks), or does not complete within the bounded poll budget. The poll loop
        is bounded so a stuck task cannot hang the monitor.
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
            error_envelope: Any = None
            if isinstance(status, dict):
                task_status = str(status.get("task_status") or "").upper()
                merge_result = status.get("merge_result")
                error_envelope = status.get("error")
                if not isinstance(error_envelope, dict) and (
                    str(status.get("type") or "").lower() == "error"
                ):
                    error_envelope = status
            if task_status == "SUCCESS":
                return merge_result
            if not task_status and isinstance(error_envelope, dict):
                # A failed Bitbucket async merge (conflict, unmet merge checks)
                # answers the task-status endpoint with an error envelope instead of
                # a ``task_status`` value (per Atlassian's task-status contract). An
                # HTTP-level error would already have raised in ``_request``; a 200
                # carrying an error body would otherwise leave ``task_status`` empty,
                # poll to budget exhaustion, and surface a generic timeout that masks
                # the actionable merge-failure reason. Treat it as an immediate
                # terminal non-success so operators see the real message. The HTTP
                # status is omitted (the poll GET was 200, not the failure) for the
                # same reason as the non-success and timeout branches below.
                message = _clean_optional_str(error_envelope.get("message"))
                detail = f": {message}" if message else ""
                raise BitbucketClientError(
                    operation=operation,
                    status=None,
                    body=f"Bitbucket merge task reported an error{detail}",
                )
            if task_status and task_status != "PENDING":
                # ``response`` is the original 202 POST; the poll GET that surfaced
                # this terminal status carried its own (200) status, so reusing
                # ``response.status_code`` here would misreport every task failure
                # as ``status=202`` and make it indistinguishable from a poll-budget
                # timeout. The diagnostic signal is the task status itself, so omit
                # the HTTP status (rendered ``n/a``) and carry ``task_status`` in the body.
                raise BitbucketClientError(
                    operation=operation,
                    status=None,
                    body=f"Bitbucket merge task ended in non-success status {task_status!r}",
                )
        # ``response`` is the original 202 merge POST, not the final poll: reusing
        # ``response.status_code`` would label a poll-budget timeout ``status=202``,
        # indistinguishable from a 202 Accepted and from a non-success terminal status
        # (which already omits the HTTP status above). Omit it (rendered ``n/a``); the
        # diagnostic signal is the exhausted poll budget carried in the body.
        raise BitbucketClientError(
            operation=operation,
            status=None,
            body=f"Bitbucket merge task did not complete within {self._max_merge_polls} polls",
            reason_code=BITBUCKET_MERGE_TASK_TIMEOUT,
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
        raise BitbucketClientError(
            operation=operation,
            status=response.status_code,
            body="Bitbucket 202 merge response carried no task-status poll location",
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
        pr_matched = [p for p in pipelines if pipeline_targets_pr(p, pr_number)]
        if pr_matched:
            return pr_matched[0]
        if source_branch is None:
            return pipelines[0]
        matched = [p for p in pipelines if pipeline_targets_branch(p, source_branch)]
        if matched:
            return matched[0]
        if any(pipeline_has_ref_info(p) for p in pipelines):
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
        """Build a ``CheckFailure`` for an external status with no Bitbucket logs."""
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
        if ctx is None:
            # No PR context to comment on, and the tracker is disabled: nothing
            # durable can be captured (no issue filed, no comment posted).
            # Returning a repo issues-page URL here would let the deferred-capture
            # call site in fix_cycle.py record a capture that never happened and
            # resolve the reviewer's thread, dropping the follow-up. Raise instead
            # so create_issue propagates and the call site's BitbucketClientError
            # handler downgrades to needs_human (this fault is non-transient, so it
            # blocks the merge), matching create_issue's documented fail-safe
            # contract — same reasoning as the comment-POST-failure path below.
            #
            # Carry BITBUCKET_ISSUE_CAPTURE_FAILED, NOT BITBUCKET_ISSUE_TRACKER_DISABLED:
            # the latter is catalogued as "note captured on the PR — no action
            # required", which is false here (no comment ran). A distinct code keeps
            # operators from mistaking a total capture failure for a benign fallback.
            _log.warning(
                "bitbucket.issue_capture_failed",
                repo=repo.slug(),
                reason_code=BITBUCKET_ISSUE_CAPTURE_FAILED,
                has_pr_context=False,
            )
            raise BitbucketClientError(
                operation="bitbucket create_issue comment-fallback",
                status=404,
                body="issue tracker disabled and no PR context to comment on",
                reason_code=BITBUCKET_ISSUE_CAPTURE_FAILED,
            )
        _log.warning(
            "bitbucket.issue_tracker_disabled",
            repo=repo.slug(),
            reason_code=BITBUCKET_ISSUE_TRACKER_DISABLED,
            has_pr_context=True,
        )
        comment_body = f"{title}\n\n{body}"
        try:
            data = await self._request_json(
                "POST",
                f"{self._pr_path(repo, ctx.pr_number)}/comments",
                operation="bitbucket create_issue comment-fallback",
                json_body={"content": {"raw": comment_body}},
            )
        except BitbucketClientError as exc:
            # The fallback POST can itself fail (e.g. 403 when the bot lacks comment
            # permission, or a transport error). Do NOT swallow it: no issue was filed
            # and no comment was posted, so nothing durable was captured. Returning a
            # PR-page URL here would let the deferred-capture call site in fix_cycle.py
            # record a capture that never happened and resolve the reviewer's thread,
            # dropping the follow-up. Propagate instead — create_issue re-raises and the
            # call site's BitbucketClientError handler requeues transient blips or
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

    async def _current_account_id(self, *, retry: bool = True) -> str | None:
        """Return the authenticated account id (cached) to filter own comments.

        Propagates ``BitbucketClientError`` instead of swallowing it. A silent
        ``/2.0/user`` failure leaves ``account_id`` unset, so the comment parsers
        cannot mark ``viewer_did_author`` and AWF's own PR comments look like
        unresolved external feedback — sending the agent into needless comment
        cycles. Letting the error surface routes transient faults
        (5xx/transport/rate-limit) through the monitor's retry path and fails
        auth/4xx faults fast. Only a successful lookup is cached, so a later poll
        retries after a transient blip.

        ``retry`` is threaded through so a ``retry=False`` caller (the pre-merge
        recheck) fails fast here too instead of running a 429 backoff inside the
        merge critical section.
        """
        if self._account_id_fetched:
            return self._account_id
        data = await self._request_json(
            "GET", "/2.0/user", operation="bitbucket current_user", cache=True, retry=retry
        )
        if isinstance(data, dict):
            self._account_id = _clean_optional_str(data.get("account_id") or data.get("uuid"))
        # Cache only a successful identity resolution; a malformed 200 (non-dict or
        # missing account_id/uuid) must not terminally disable viewer-self filtering,
        # so a later poll retries /2.0/user — matching this method's docstring contract.
        self._account_id_fetched = self._account_id is not None
        return self._account_id

    def _remember_pr(
        self, repo: RepoRef, pr_number: int, pr: dict[str, Any], *, head_sha: str
    ) -> None:
        """Capture per-repo PR context for the repo-less Protocol methods.

        ``head_sha`` is the already-resolved full 40-char source commit SHA, not
        the abbreviated ``source.commit.hash`` on the raw payload — it is stored as
        ``source_sha`` so ``rerun_failed_workflow_jobs`` reconstructs the pipeline
        target with the full hash, consistent with ``PRStatus.head_sha`` (#477).
        """
        source = _as_dict(pr.get("source"))
        destination = _as_dict(pr.get("destination"))
        dest_branch = _as_dict(destination.get("branch"))
        merge_strategies = dest_branch.get("merge_strategies")
        self._pr_context[repo.slug()] = _PRContext(
            pr_number=pr_number,
            source_branch=_clean_optional_str(_as_dict(source.get("branch")).get("name")),
            source_sha=head_sha,
            dest_branch=_clean_optional_str(dest_branch.get("name")),
            dest_sha=_clean_optional_str(_as_dict(destination.get("commit")).get("hash")),
            merge_strategies=merge_strategies if isinstance(merge_strategies, list) else None,
            default_merge_strategy=_clean_optional_str(dest_branch.get("default_merge_strategy")),
        )

    async def _resolve_full_commit_sha(self, repo: RepoRef, sha: str, *, retry: bool = True) -> str:
        """Resolve an abbreviated Bitbucket commit hash to its full 40-char SHA.

        Bitbucket Cloud's PR GET serves ``source.commit.hash`` abbreviated (e.g.
        12 chars), but AWF assumes full 40-char SHAs everywhere it matches a head
        (the pre-merge validation-provenance gate compares by exact equality). A
        hash already ``>= 40`` chars is returned unchanged with NO HTTP call; an
        abbreviated one is resolved via the per-commit endpoint (one cached GET),
        which echoes the full ``hash`` and accepts either form. A non-dict /
        missing / too-short payload — or a full hash that does not extend the
        abbreviation we asked for — raises a deterministic reason-coded error
        rather than silently falling back to the abbreviated hash (#477).

        ``retry`` is threaded through so a ``retry=False`` caller (the pre-merge
        recheck) fails fast on this GET too instead of running a 429 backoff
        inside the merge critical section.
        """
        if len(sha) >= 40:
            return sha
        data = await self._request_json(
            "GET",
            f"{self._repo_path(repo)}/commit/{quote(sha, safe='')}",
            operation="bitbucket resolve_commit_sha",
            cache=True,
            retry=retry,
        )
        resolved = _clean_optional_str(_as_dict(data).get("hash"))
        # The resolved full SHA must extend the abbreviation we asked for. A
        # 40-char hash that does not start with ``sha`` (an ambiguous/misresolved
        # prefix, or a stale/mock response) would otherwise be accepted as the PR
        # head, recording statuses and validation provenance for the WRONG commit
        # and letting the monitor make merge decisions for a different head.
        if resolved is None or len(resolved) < 40 or not resolved.lower().startswith(sha.lower()):
            raise BitbucketClientError(
                operation="bitbucket resolve_commit_sha",
                status=None,
                body=(
                    f"commit {sha} in {repo.slug()} resolved to an unusable hash "
                    f"{resolved!r}; expected a full 40-char commit SHA extending {sha!r}"
                ),
                reason_code=BITBUCKET_COMMIT_RESOLVE_FAILED,
            )
        return resolved
