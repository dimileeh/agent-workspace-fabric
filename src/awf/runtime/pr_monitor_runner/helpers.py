"""Pull request monitor helper functions.

Mechanically extracted from the original orchestrator; behavior is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import (
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, replace
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from pathlib import Path
from typing import Any

from awf.common.github_client import (
    GitHubClientError,
)
from awf.control.protected_file_diffs import (
    changed_paths_from_name_status_z as _parse_name_status_z,
)
from awf.control.quality_gates import QualityGateViolation
from awf.control.state_machine import WorkspaceStateMachine
from awf.db.enums import (
    OperationStatus,
    OperationType,
    WorkspaceStatus,
)
from awf.db.models import (
    Operation,
    Workspace,
)
from awf.db.repositories import (
    WorkspaceRepository,
    pr_feedback_body_hash,
)
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckTiming,
    Merge,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReviewComment,
    _agent_can_triage_review_comment,
    _ci_transient_rerun_count,
    _ci_transient_rerun_state_key,
    _is_bot_author,
    _is_bot_review_thread,
    _needs_comment_attention,
    _review_thread_body_hash,
    _review_thread_body_state_key,
    decide,
)
from awf.runtime.pr_monitor_runner.comments import (
    Verdict,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.constants import (
    _AUTHORIZATION_BEARER_RE,
    _AWF_VERDICT,
    _BASE_FETCH_RETRY_COUNT_KEY_PREFIX,
    _NON_TRANSIENT_GITHUB_ERROR_MARKERS,
    _PENDING_CHECK_STATUSES,
    _PR_MONITOR_REASON_CODES_BY_STALE_REASON,
    _PR_MONITOR_STALE_REASON_MESSAGES,
    _REDACTION,
    _REMOTE_TRACKING_REF_LOCK_RACE_RE,
    _TERMINAL_CHECK_CONCLUSIONS,
    _TERMINAL_CHECK_STATUSES,
    _TOKEN_RE,
    _TRANSIENT_GITHUB_ERROR_MARKERS,
    _URL_CREDENTIAL_RE,
    _VALIDATION_RECOVERY_STALE_REASONS,
    _VERDICT_DEFER,
    _VERDICT_FALSE_POSITIVE,
)
from awf.runtime.pr_monitor_runner.gates import (
    _MergeGateResult,
    _NonCheckReviewerSettleDecision,
    _NonCheckReviewerSettleWaitOperationContext,
)
from awf.runtime.pr_monitor_runner.types import (
    BaseFetchError,
    ProtectedScopeDiffError,
)


async def _record_ignored_monitor_terminal_callback(
    repo: WorkspaceRepository,
    workspace: Workspace,
    *,
    requested_status: WorkspaceStatus,
    reason_code: str,
) -> None:
    await repo.record_ignored_stale_callback(
        workspace,
        callback_source="pr_monitor",
        callback_action=(
            "terminal_completed"
            if requested_status == WorkspaceStatus.completed
            else "terminal_failed"
        ),
        expected_status=WorkspaceStatus.monitoring_pr,
        requested_status=requested_status,
        reason_code=reason_code,
    )


def _is_callback_terminal_workspace_status(status: str) -> bool:
    try:
        workspace_status = WorkspaceStatus(status)
    except ValueError:  # pragma: no cover - defensive for legacy bad rows
        return False
    return WorkspaceStateMachine.is_callback_terminal(workspace_status)


def _parse_verdict(stdout: str) -> Verdict:
    """Map the CLI's final message to a structured verdict.

    The prompt templates instruct the CLI to report a structured stdout
    verdict. Anything else counts as a fix commit (the default happy path).
    """
    return _parse_verdict_result(stdout).verdict


def _parse_verdict_result(stdout: str) -> VerdictResult:
    if not stdout.strip():
        # Empty or whitespace-only agent output is a failure to produce, not a
        # considered deferral. Treat it as needs_human so it blocks the merge
        # instead of
        # triggering the follow-up defer capture (comment + filed issue +
        # resolve) on a thread the agent never actually addressed (#305).
        return VerdictResult(verdict="needs_human")
    awf_match = _AWF_VERDICT.search(stdout)
    if awf_match is not None:
        label = re.sub(r"\s+", " ", awf_match.group("label").strip().lower())
        reason = awf_match.group("reason").strip() or None
        if label == "false positive":
            return VerdictResult(verdict="false_positive", reason=reason)
        if label == "needs_human":
            return VerdictResult(verdict="needs_human", reason=reason)
        if label == "defer":
            return VerdictResult(verdict="defer", reason=reason)
        return VerdictResult(verdict="fix_committed", reason=reason)
    if _VERDICT_FALSE_POSITIVE.search(stdout):
        return VerdictResult(verdict="false_positive", reason=_verdict_reason(stdout))
    if _VERDICT_DEFER.search(stdout):
        return VerdictResult(verdict="defer", reason=_verdict_reason(stdout))
    return VerdictResult(verdict="fix_committed")


def _verdict_reason(stdout: str) -> str | None:
    _prefix, _separator, reason = stdout.partition(":")
    cleaned = reason.strip()
    return cleaned or None


def _review_comment_resolution_body(comment: ReviewComment) -> str:
    return comment.body or comment.body_excerpt or ""


def _review_comment_body_state_key(comment_id: str) -> str:
    return f"__review_comment_body_hash__:{comment_id}"


def _review_comment_body_hash(comment: ReviewComment) -> str:
    return pr_feedback_body_hash(_review_comment_resolution_body(comment))


def _mark_review_comment_addressed(
    state: MonitorState,
    comment: ReviewComment,
    verdict: str,
) -> None:
    state.mark_addressed(comment.comment_id, verdict)
    state.mark_addressed(
        _review_comment_body_state_key(comment.comment_id),
        _review_comment_body_hash(comment),
    )


def _clear_addressed_state_by_id(state: MonitorState, item_id: str) -> None:
    state.threads_addressed_ids.pop(item_id, None)
    state.threads_addressed_ids.pop(_review_thread_body_state_key(item_id), None)
    state.threads_addressed_ids.pop(_review_comment_body_state_key(item_id), None)


def _drop_stale_review_thread_addressed_state(
    status: PRStatus,
    state: MonitorState,
) -> bool:
    changed = False
    for thread in status.unresolved_inline_threads:
        verdict = state.threads_addressed_ids.get(thread.thread_id)
        if _needs_comment_attention(verdict):
            continue
        if state.threads_addressed_ids.get(
            _review_thread_body_state_key(thread.thread_id)
        ) == _review_thread_body_hash(thread):
            continue
        _clear_addressed_state_by_id(state, thread.thread_id)
        changed = True
    return changed


def _review_comment_needs_attention(state: MonitorState, comment: ReviewComment) -> bool:
    verdict = state.threads_addressed_ids.get(comment.comment_id)
    if _needs_comment_attention(verdict):
        return True
    return state.threads_addressed_ids.get(
        _review_comment_body_state_key(comment.comment_id)
    ) != _review_comment_body_hash(comment)


def _drop_stale_review_comment_addressed_state(
    status: PRStatus,
    state: MonitorState,
) -> bool:
    changed = False
    for comment in status.unresolved_review_comments:
        verdict = state.threads_addressed_ids.get(comment.comment_id)
        if _needs_comment_attention(verdict):
            continue
        if state.threads_addressed_ids.get(
            _review_comment_body_state_key(comment.comment_id)
        ) == _review_comment_body_hash(comment):
            continue
        _clear_addressed_state_by_id(state, comment.comment_id)
        changed = True
    return changed


def _monitor_state_verdict(verdict: str) -> Verdict:
    normalized = verdict.strip().lower()
    if normalized == "false_positive":
        return "false_positive"
    if normalized == "needs_human":
        return "needs_human"
    if normalized == "defer":
        return "defer"
    if normalized == "agent_failed":
        return "agent_failed"
    return "fix_committed"


def _with_ci_failures(status: PRStatus, failures: tuple[CheckFailure, ...]) -> PRStatus:
    """Immutable-replace ci_failures on a ``PRStatus`` (frozen dataclass)."""
    # Import dataclasses.replace locally to keep the top-level imports tight.
    from dataclasses import replace

    return replace(status, ci_failures=failures)


@dataclass(frozen=True)
class _StalePendingCheckWarning:
    check_name: str
    age_seconds: int
    head_sha: str
    pr_number: int
    threshold_seconds: float
    threshold_window: int
    check_status: str | None
    check_conclusion: str | None
    details_url: str | None

    def payload(self: Any) -> dict[str, object]:
        return {
            "check_name": self.check_name,
            "age_seconds": self.age_seconds,
            "head_sha": self.head_sha,
            "pr_number": self.pr_number,
            "threshold_seconds": self.threshold_seconds,
            "threshold_window": self.threshold_window,
            "check_status": self.check_status,
            "check_conclusion": self.check_conclusion,
            "details_url": self.details_url,
        }


def _stale_pending_check_warnings(
    status: PRStatus,
    *,
    now: datetime,
    threshold_seconds: float,
) -> tuple[_StalePendingCheckWarning, ...]:
    if threshold_seconds <= 0:
        return ()
    now_utc = _as_utc(now)
    warnings: list[_StalePendingCheckWarning] = []
    for check in status.checks:
        if not _is_pending_check(check) or check.started_at is None:
            continue
        age_float = (now_utc - _as_utc(check.started_at)).total_seconds()
        if age_float <= threshold_seconds:
            continue
        warnings.append(
            _StalePendingCheckWarning(
                check_name=check.name,
                age_seconds=max(0, int(age_float)),
                head_sha=status.head_sha,
                pr_number=status.number,
                threshold_seconds=threshold_seconds,
                threshold_window=max(1, int(age_float // threshold_seconds)),
                check_status=check.status,
                check_conclusion=check.conclusion,
                details_url=check.details_url,
            )
        )
    return tuple(warnings)


def _is_pending_check(check: CheckTiming) -> bool:
    status = _normalized_check_value(check.status)
    conclusion = _normalized_check_value(check.conclusion)
    if status in _PENDING_CHECK_STATUSES:
        return True
    if status in _TERMINAL_CHECK_STATUSES:
        return False
    if conclusion in _TERMINAL_CHECK_CONCLUSIONS:
        return False
    # Preserve stale-check observability for future GitHub/provider states:
    # unknown populated values are non-terminal until an explicit terminal
    # status or conclusion says otherwise.
    return bool(status or conclusion)


def _normalized_check_value(value: str | None) -> str:
    return (value or "").strip().upper()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _infer_service_work_dir(worktrees_root: Path) -> Path:
    if worktrees_root.name == "worktrees" and worktrees_root.parent.name == "git":
        return worktrees_root.parent.parent
    return worktrees_root.parent


def _stale_pending_check_warning_key(
    *,
    workspace_id: str,
    head_sha: str,
    check_name: str,
    threshold_seconds: float,
    threshold_window: int,
) -> str:
    return "__awf_pending_check_stale__:" + json.dumps(
        [workspace_id, head_sha, check_name, f"{threshold_seconds:g}", threshold_window],
        separators=(",", ":"),
    )


def _notify_human_reason(status: PRStatus, state: MonitorState) -> str | None:
    if status.blocking_reviews:
        return "a merge-blocking changes-requested review remains unresolved"
    bot_items, human_deferred = _collect_defer_items(status, state)
    if human_deferred:
        return "human review feedback was deferred by the agent and remains unresolved"
    # #305: a bot inline thread (``defer``/``needs_human``) or a bot
    # ``needs_human`` comment also blocks the merge in ``pr_monitor.decide``
    # even though it isn't human-authored. Surface it as a reason instead of
    # letting the caller emit a false "ready to merge" notification.
    if any(item["kind"] == "thread" or item.get("verdict") == "needs_human" for item in bot_items):
        return "review feedback needs human input and remains unresolved on GitHub"
    if status.merge_state_status in (MergeStateStatus.BLOCKED, MergeStateStatus.HAS_HOOKS):
        return (
            f"GitHub reports merge state {status.merge_state_status.value}; "
            "required protection or review hooks need a human"
        )
    return None


def _merge_rejection_reason(stderr: str) -> str:
    detail = " ".join(_redact_and_truncate_github_error(stderr).split())[:240]
    if detail:
        return f"GitHub rejected the merge attempt: {detail}"
    return "GitHub rejected the merge attempt"


def _transient_github_retry_payload(
    exc: GitHubClientError,
    *,
    context: str,
    pr_number: int,
    wait_seconds: float,
) -> dict[str, object]:
    return {
        "context": context,
        "operation": exc.operation,
        "returncode": exc.returncode,
        "pr_number": pr_number,
        "wait_seconds": wait_seconds,
        "message": _redact_and_truncate_github_error(str(exc)),
        "stderr": _redact_and_truncate_github_error(exc.stderr),
    }


def _transient_base_fetch_retry_payload(
    exc: BaseFetchError,
    *,
    context: str,
    pr_number: int,
    retry_number: int,
    max_retries: int,
    wait_seconds: float,
) -> dict[str, object]:
    return {
        "context": context,
        "operation": "git fetch base",
        "pr_number": pr_number,
        "retry_number": retry_number,
        "max_retries": max_retries,
        "wait_seconds": wait_seconds,
        "message": _redact_and_truncate_github_error(str(exc)),
    }


def _redact_and_truncate_github_error(value: str, *, limit: int = 400) -> str:
    redacted = _URL_CREDENTIAL_RE.sub(r"\1<redacted>@", value)
    redacted = _AUTHORIZATION_BEARER_RE.sub(r"\1<redacted>", redacted)
    redacted = _TOKEN_RE.sub(_REDACTION, redacted).strip()
    if len(redacted) <= limit:
        return redacted
    return redacted[: limit - 3] + "..."


def _is_transient_github_client_error(exc: GitHubClientError) -> bool:
    """Classify GitHub/gh failures that should keep the monitor polling."""

    text = f"{exc.operation}\n{exc.stderr}".lower()
    if any(marker in text for marker in _NON_TRANSIENT_GITHUB_ERROR_MARKERS):
        return False
    return any(marker in text for marker in _TRANSIENT_GITHUB_ERROR_MARKERS)


def _is_transient_base_fetch_error(exc: BaseFetchError) -> bool:
    """Classify git transport failures caused by transient GitHub outages."""

    text = str(exc).lower()
    if any(marker in text for marker in _NON_TRANSIENT_GITHUB_ERROR_MARKERS):
        return False
    if _REMOTE_TRACKING_REF_LOCK_RACE_RE.search(str(exc)):
        return True
    return any(marker in text for marker in _TRANSIENT_GITHUB_ERROR_MARKERS)


def _base_fetch_retry_count_key(context: str) -> str:
    return f"{_BASE_FETCH_RETRY_COUNT_KEY_PREFIX}{context}"


def _increment_base_fetch_retry_count(state: MonitorState, context: str) -> int:
    key = _base_fetch_retry_count_key(context)
    raw_count = state.threads_addressed_ids.get(key, "0")
    try:
        current = int(raw_count)
    except ValueError:
        current = 0
    retry_number = current + 1
    state.threads_addressed_ids[key] = str(retry_number)
    return retry_number


def _clear_transient_base_fetch_retry_state(state: MonitorState, *, context: str) -> bool:
    key = _base_fetch_retry_count_key(context)
    return state.threads_addressed_ids.pop(key, None) is not None


def _ci_transient_rerun_attempt(
    state: MonitorState,
    *,
    head_sha: str,
    failures: tuple[CheckFailure, ...],
    legacy_failures: tuple[CheckFailure, ...] | None = None,
) -> int:
    key = _ci_transient_rerun_state_key(head_sha, failures)
    current = _ci_transient_rerun_count(
        state,
        head_sha=head_sha,
        failures=failures,
        legacy_failures=legacy_failures,
    )
    attempt = current + 1
    state.threads_addressed_ids[key] = str(attempt)
    if legacy_failures is not None and legacy_failures != failures:
        legacy_key = _ci_transient_rerun_state_key(head_sha, legacy_failures)
        state.threads_addressed_ids.pop(legacy_key, None)
    return attempt


def _ci_failure_payload(failure: CheckFailure) -> dict[str, object]:
    return {
        "name": failure.name,
        "conclusion": failure.conclusion,
        "run_id": failure.run_id,
        "test_node_ids": list(failure.test_node_ids),
        "suggested_repro_commands": list(failure.suggested_repro_commands),
        "failing_commands": list(failure.failing_commands),
        "assertion_snippets": list(failure.assertion_snippets),
        "error_summaries": list(failure.error_summaries),
        "evidence_warnings": list(failure.evidence_warnings),
    }


def _base_fetch_retry_wait_seconds(
    *,
    retry_number: int,
    initial_backoff_seconds: float,
    max_backoff_seconds: float,
) -> float:
    initial = max(initial_backoff_seconds, 0.0)
    cap = max(max_backoff_seconds, 0.0)
    exponent = min(max(retry_number - 1, 0), 30)
    wait_seconds = initial * float(2**exponent)
    return wait_seconds if wait_seconds < cap else cap


def _notification_key(*, head_sha: str, blocker_reason: str | None) -> str:
    reason = blocker_reason or "ready-to-merge"
    return f"__awf_notify__:{head_sha}:{reason}"


def _merge_queue_wait_key(*, head_sha: str, blocker_candidate_id: str) -> str:
    return f"__awf_merge_queue_wait__:{head_sha}:{blocker_candidate_id}"


def _non_check_reviewer_settle_started_key(
    *,
    pr_number: int,
    head_sha: str,
    activity_signature: str | None = None,
) -> str:
    """Build state key for a non-check reviewer settle start marker."""
    key = f"{_non_check_reviewer_settle_started_prefix(pr_number=pr_number)}{head_sha}"
    if activity_signature is not None:
        return f"{key}:{activity_signature}"
    return key


def _non_check_reviewer_settle_started_prefix(*, pr_number: int) -> str:
    """Build namespace prefix for non-check reviewer settle state keys."""
    return f"__awf_non_check_reviewer_settle_started__:{pr_number}:"


def _non_check_reviewer_settle_done_key(
    *,
    pr_number: int,
    head_sha: str,
    activity_signature: str | None = None,
) -> str:
    """Build state key for a completed non-check reviewer settle window."""
    key = f"__awf_non_check_reviewer_settle_done__:{pr_number}:{head_sha}"
    if activity_signature is not None:
        return f"{key}:{activity_signature}"
    return key


def _non_check_reviewer_settle_skip_visible_key(*, pr_number: int, head_sha: str) -> str:
    """Build skip marker key for missing non-check reviewer visibility checks."""
    return f"__awf_non_check_reviewer_settle_skipped_visible__:{pr_number}:{head_sha}"


def _non_check_reviewer_settle_decision(
    status: PRStatus,
    state: MonitorState,
    config: MonitorConfig,
    *,
    pr_number: int,
    now: float,
    now_wall: datetime | None = None,
) -> _NonCheckReviewerSettleDecision:
    """Return settle decision for non-check reviewers, preferring activity clock when available."""
    configured_reviewers = _normalize_non_check_reviewer_logins(config.non_check_reviewer_logins)
    if not config.auto_merge:
        return _NonCheckReviewerSettleDecision(
            action="not_auto_merge",
            configured_reviewers=configured_reviewers,
        )
    if config.non_check_reviewer_settle_seconds <= 0:
        return _NonCheckReviewerSettleDecision(
            action="disabled",
            configured_reviewers=configured_reviewers,
        )
    if not configured_reviewers:
        return _NonCheckReviewerSettleDecision(action="no_configured_reviewers")

    visible_reviewers, missing_reviewers = _non_check_reviewer_visibility(
        configured_reviewers=configured_reviewers,
        checks=status.checks,
    )
    if not missing_reviewers:
        skip_key = _non_check_reviewer_settle_skip_visible_key(
            pr_number=pr_number,
            head_sha=status.head_sha,
        )
        state_changed = state.threads_addressed_ids.get(skip_key) != "visible_check"
        if state_changed:
            state.mark_addressed(skip_key, "visible_check")
        return _NonCheckReviewerSettleDecision(
            action="visible_check",
            configured_reviewers=configured_reviewers,
            visible_reviewers=visible_reviewers,
            state_changed=state_changed,
        )

    if status.quiet_period_anchor_at is not None:
        return _non_check_reviewer_activity_settle_decision(
            status,
            state,
            config,
            pr_number=pr_number,
            now_wall=now_wall or datetime.now(UTC),
            configured_reviewers=configured_reviewers,
            missing_reviewers=missing_reviewers,
            visible_reviewers=visible_reviewers,
        )

    done_key = _non_check_reviewer_settle_done_key(
        pr_number=pr_number,
        head_sha=status.head_sha,
    )
    if state.threads_addressed_ids.get(done_key) == "elapsed":
        return _NonCheckReviewerSettleDecision(
            action="already_elapsed",
            configured_reviewers=configured_reviewers,
            missing_reviewers=missing_reviewers,
            visible_reviewers=visible_reviewers,
        )

    started_key = _non_check_reviewer_settle_started_key(
        pr_number=pr_number,
        head_sha=status.head_sha,
    )
    started_raw = state.threads_addressed_ids.get(started_key)
    started_now = False
    if started_raw is None:
        started_at = now
        state.mark_addressed(started_key, f"{started_at:.6f}")
        started_now = True
    else:
        try:
            started_at = float(started_raw)
        except (TypeError, ValueError):
            started_at = now
            state.mark_addressed(started_key, f"{started_at:.6f}")
            started_now = True

    elapsed_seconds = max(now - started_at, 0.0)
    remaining_seconds = config.non_check_reviewer_settle_seconds - elapsed_seconds
    if remaining_seconds <= 0:
        state.mark_addressed(done_key, "elapsed")
        return _NonCheckReviewerSettleDecision(
            action="elapsed",
            configured_reviewers=configured_reviewers,
            missing_reviewers=missing_reviewers,
            visible_reviewers=visible_reviewers,
            started_at=started_at,
            elapsed_seconds=elapsed_seconds,
            remaining_seconds=0.0,
            state_changed=True,
        )

    wait_seconds = (
        remaining_seconds
        if config.poll_interval_seconds <= 0
        else min(config.poll_interval_seconds, remaining_seconds)
    )

    return _NonCheckReviewerSettleDecision(
        action="started" if started_now else "waiting",
        wait_seconds=wait_seconds,
        configured_reviewers=configured_reviewers,
        missing_reviewers=missing_reviewers,
        visible_reviewers=visible_reviewers,
        started_at=started_at,
        elapsed_seconds=elapsed_seconds,
        remaining_seconds=remaining_seconds,
        state_changed=started_now,
    )


def _non_check_reviewer_activity_settle_decision(
    status: PRStatus,
    state: MonitorState,
    config: MonitorConfig,
    *,
    pr_number: int,
    now_wall: datetime,
    configured_reviewers: tuple[str, ...],
    missing_reviewers: tuple[str, ...],
    visible_reviewers: tuple[str, ...],
) -> _NonCheckReviewerSettleDecision:
    """Return a settle decision anchored to the latest external review activity."""
    assert status.quiet_period_anchor_at is not None
    anchor_at = _utc_datetime(status.quiet_period_anchor_at)
    now_dt = _utc_datetime(now_wall)
    quiet_until = anchor_at + timedelta(seconds=config.non_check_reviewer_settle_seconds)
    elapsed_seconds = max((now_dt - anchor_at).total_seconds(), 0.0)
    remaining_seconds = max((quiet_until - now_dt).total_seconds(), 0.0)
    signature = _non_check_reviewer_activity_signature(
        status,
        anchor_at=anchor_at,
    )
    done_key = _non_check_reviewer_settle_done_key(
        pr_number=pr_number,
        head_sha=status.head_sha,
        activity_signature=signature,
    )
    if state.threads_addressed_ids.get(done_key) == "elapsed":
        return _NonCheckReviewerSettleDecision(
            action="already_elapsed",
            configured_reviewers=configured_reviewers,
            missing_reviewers=missing_reviewers,
            visible_reviewers=visible_reviewers,
            elapsed_seconds=elapsed_seconds,
            remaining_seconds=0.0,
            activity_anchor_at=anchor_at,
            activity_anchor_source=status.quiet_period_anchor_source,
            quiet_until=quiet_until,
            latest_external_review_activity_at=status.latest_external_review_activity_at,
            latest_external_review_activity_source=status.latest_external_review_activity_source,
            activity_signature=signature,
        )
    if remaining_seconds <= 0:
        state.mark_addressed(done_key, "elapsed")
        return _NonCheckReviewerSettleDecision(
            action="elapsed",
            configured_reviewers=configured_reviewers,
            missing_reviewers=missing_reviewers,
            visible_reviewers=visible_reviewers,
            elapsed_seconds=elapsed_seconds,
            remaining_seconds=0.0,
            activity_anchor_at=anchor_at,
            activity_anchor_source=status.quiet_period_anchor_source,
            quiet_until=quiet_until,
            latest_external_review_activity_at=status.latest_external_review_activity_at,
            latest_external_review_activity_source=status.latest_external_review_activity_source,
            activity_signature=signature,
            state_changed=True,
        )

    wait_seconds = (
        remaining_seconds
        if config.poll_interval_seconds <= 0
        else min(config.poll_interval_seconds, remaining_seconds)
    )
    started_key = _non_check_reviewer_settle_started_key(
        pr_number=pr_number,
        head_sha=status.head_sha,
        activity_signature=signature,
    )
    state_changed = state.threads_addressed_ids.get(started_key) != "activity_wait"
    if state_changed:
        state.mark_addressed(started_key, "activity_wait")
    return _NonCheckReviewerSettleDecision(
        action="started" if state_changed else "waiting",
        wait_seconds=wait_seconds,
        configured_reviewers=configured_reviewers,
        missing_reviewers=missing_reviewers,
        visible_reviewers=visible_reviewers,
        elapsed_seconds=elapsed_seconds,
        remaining_seconds=remaining_seconds,
        activity_anchor_at=anchor_at,
        activity_anchor_source=status.quiet_period_anchor_source,
        quiet_until=quiet_until,
        latest_external_review_activity_at=status.latest_external_review_activity_at,
        latest_external_review_activity_source=status.latest_external_review_activity_source,
        activity_signature=signature,
        state_changed=state_changed,
    )


def _non_check_reviewer_activity_signature(status: PRStatus, *, anchor_at: datetime) -> str:
    """Return a stable signature for the current settle activity anchor."""
    payload = "|".join(
        (
            status.head_sha,
            status.quiet_period_anchor_source or "",
            anchor_at.isoformat(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _utc_datetime(value: datetime) -> datetime:
    """Normalize datetimes to timezone-aware UTC values."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _datetime_iso(value: datetime | None) -> str | None:
    """Serialize an optional datetime to ISO-8601 UTC, or ``None``."""
    if value is None:
        return None
    return _utc_datetime(value).isoformat()


def _non_check_reviewer_settle_wait_operation_context(
    config: MonitorConfig,
    decision: _NonCheckReviewerSettleDecision,
) -> _NonCheckReviewerSettleWaitOperationContext:
    """Build persisted wait-operation context for non-check reviewer settle state."""
    return _NonCheckReviewerSettleWaitOperationContext(
        extra_payload={
            "settle_seconds": config.non_check_reviewer_settle_seconds,
            "configured_reviewers": list(decision.configured_reviewers),
            "missing_reviewers": list(decision.missing_reviewers),
            "visible_reviewers": list(decision.visible_reviewers),
            "elapsed_seconds": decision.elapsed_seconds,
            "remaining_seconds": decision.remaining_seconds,
            "activity_anchor_at": _datetime_iso(decision.activity_anchor_at),
            "activity_anchor_source": decision.activity_anchor_source,
            "quiet_until": _datetime_iso(decision.quiet_until),
            "latest_external_review_activity_at": _datetime_iso(
                decision.latest_external_review_activity_at
            ),
            "latest_external_review_activity_source": (
                decision.latest_external_review_activity_source
            ),
        },
        extra_identity=(
            *decision.configured_reviewers,
            *decision.missing_reviewers,
            decision.started_at,
            decision.activity_signature,
        ),
    )


def _normalize_non_check_reviewer_logins(logins: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Normalize and dedupe configured reviewer logins."""
    normalized: list[str] = []
    seen: set[str] = set()
    for login in logins:
        value = _normalize_non_check_reviewer_identity(login)
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return tuple(normalized)


def _non_check_reviewer_visibility(
    *,
    configured_reviewers: tuple[str, ...],
    checks: tuple[CheckTiming, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Separate configured reviewers into visible and missing based on checks."""
    visible_identities = _visible_check_identities(checks)
    visible_reviewers: list[str] = []
    missing_reviewers: list[str] = []
    for reviewer in configured_reviewers:
        if _reviewer_has_visible_check(reviewer, visible_identities=visible_identities):
            visible_reviewers.append(reviewer)
        else:
            missing_reviewers.append(reviewer)
    return tuple(visible_reviewers), tuple(missing_reviewers)


def _visible_check_identities(checks: tuple[CheckTiming, ...]) -> frozenset[str]:
    """Extract normalized identities from check metadata and creator fields."""
    values: set[str] = set()
    for check in checks:
        for raw in (
            check.name,
            getattr(check, "app_slug", None),
            getattr(check, "app_name", None),
            getattr(check, "creator_login", None),
        ):
            normalized = _normalize_non_check_reviewer_identity(raw)
            if normalized:
                values.add(normalized)
    return frozenset(values)


def _reviewer_has_visible_check(
    reviewer: str,
    *,
    visible_identities: frozenset[str],
) -> bool:
    """Return whether a reviewer has a corresponding visible check identity."""
    aliases = _non_check_reviewer_visible_aliases(reviewer)
    for identity in visible_identities:
        for alias in aliases:
            if identity == alias or identity.startswith(f"{alias}-"):
                return True
            if alias == "greptile" and identity.endswith("-greptile"):
                return True
    return False


def _non_check_reviewer_visible_aliases(reviewer: str) -> frozenset[str]:
    """Expand reviewer identity variants used for check-name matching."""
    aliases = {reviewer}
    if reviewer == "greptile-apps" or reviewer.startswith("greptile-"):
        aliases.update({"greptile", "greptile-apps"})
    return frozenset(aliases)


def _normalize_non_check_reviewer_identity(value: object) -> str:
    """Normalize a reviewer/caller identity into lowercase token form."""
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    if text.endswith("[bot]"):
        text = text[: -len("[bot]")]
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _merge_gate_blocks(gate: _MergeGateResult) -> bool:
    return gate.stale_reason is not None or gate.notify_message is not None


def _gate_requires_validation_recovery(gate: _MergeGateResult) -> bool:
    return gate.stale_reason in _VALIDATION_RECOVERY_STALE_REASONS and gate.req_action in (
        None,
        "validate",
    )


def _is_manual_ready_handoff(
    action: NotifyHuman,
    status: PRStatus,
    state: MonitorState,
    config: MonitorConfig,
) -> bool:
    if config.auto_merge or action.message is not None:
        return False
    auto_merge_action = decide(status, state, replace(config, auto_merge=True))
    if isinstance(auto_merge_action, Merge):
        return True
    return isinstance(
        auto_merge_action,
        NotifyHuman,
    ) and _is_protected_manual_ready_handoff(status, state)


def _is_protected_manual_ready_handoff(status: PRStatus, state: MonitorState) -> bool:
    if status.merge_state_status not in (
        MergeStateStatus.BLOCKED,
        MergeStateStatus.HAS_HOOKS,
    ):
        return False
    if status.blocking_reviews:
        return False
    _, human_deferred = _collect_defer_items(status, state)
    return not human_deferred


def _candidate_stale_required_action(reason: str | None) -> str | None:
    from awf.runtime.merge_eligibility import stale_reason_required_action

    return stale_reason_required_action(reason)


def _pr_monitor_recovery_reason(stale_reason: str) -> str:
    return _PR_MONITOR_STALE_REASON_MESSAGES.get(
        stale_reason,
        f"Merge candidate is stale: {stale_reason}.",
    )


def _pr_monitor_recovery_reason_code(stale_reason: str) -> str:
    if mapped := _PR_MONITOR_REASON_CODES_BY_STALE_REASON.get(stale_reason):
        return mapped
    reason_code = re.sub(r"[^A-Za-z0-9]+", "_", stale_reason).strip("_").upper()
    return reason_code or "STALE"


def _latest_successful_remonitor_at(operations: Iterable[Operation]) -> datetime | None:
    remonitor_times = [
        _operation_observed_at(op)
        for op in operations
        if op.type == OperationType.remonitor.value and op.status == OperationStatus.succeeded.value
    ]
    return max(remonitor_times, default=None)


def _operation_observed_at(operation: Operation) -> datetime:
    return (
        operation.finished_at
        or operation.started_at
        or operation.created_at
        or datetime.min.replace(tzinfo=UTC)
    )


def _initial_review_grace_started_key(pr_number: int) -> str:
    return f"__awf_initial_review_grace_started__:{pr_number}"


def _initial_review_grace_done_key(pr_number: int) -> str:
    return f"__awf_initial_review_grace_done__:{pr_number}"


def _initial_review_grace_wall_started_value(started_wall_seconds: float) -> str:
    return f"{started_wall_seconds:.6f}"


def _initial_review_grace_wall_started_value_from_datetime(started_at: datetime) -> str:
    started_dt = started_at
    if started_dt.tzinfo is None:
        started_dt = started_dt.replace(tzinfo=UTC)
    return _initial_review_grace_wall_started_value(started_dt.timestamp())


def _initial_review_grace_wall_seconds(raw: object) -> float | None:
    if not isinstance(raw, (str, bytes, bytearray, int, float)):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # Values at or above 2001-09-09T01:46:40Z are epoch seconds. Smaller
    # values are legacy process-local ``time.monotonic()`` markers.
    if value >= 1_000_000_000:
        return value
    return None


def _initial_review_grace_state_for_runtime(
    threads_addressed: dict[str, str],
    *,
    pr_number: int,
    now_monotonic: float,
    now_wall_seconds: float,
    legacy_monotonic_fallback: float | None = None,
) -> dict[str, str]:
    started_key = _initial_review_grace_started_key(pr_number)
    started_raw = threads_addressed.get(started_key)
    started_wall_seconds = _initial_review_grace_wall_seconds(started_raw)
    if started_wall_seconds is None:
        if started_raw is not None and legacy_monotonic_fallback is not None:
            threads_addressed[started_key] = f"{legacy_monotonic_fallback:.6f}"
        return threads_addressed

    elapsed_seconds = max(now_wall_seconds - started_wall_seconds, 0.0)
    threads_addressed[started_key] = f"{now_monotonic - elapsed_seconds:.6f}"
    return threads_addressed


def _initial_review_grace_state_for_persistence(
    threads_addressed: dict[str, str],
    *,
    pr_number: int,
    now_monotonic: float,
    now_wall_seconds: float,
) -> dict[str, str]:
    started_key = _initial_review_grace_started_key(pr_number)
    started_raw = threads_addressed.get(started_key)
    if started_raw is None:
        return threads_addressed

    started_wall_seconds = _initial_review_grace_wall_seconds(started_raw)
    if started_wall_seconds is not None:
        threads_addressed[started_key] = _initial_review_grace_wall_started_value(
            started_wall_seconds
        )
        return threads_addressed

    try:
        started_monotonic = float(started_raw)
    except (TypeError, ValueError):
        return threads_addressed

    elapsed_seconds = max(now_monotonic - started_monotonic, 0.0)
    threads_addressed[started_key] = _initial_review_grace_wall_started_value(
        now_wall_seconds - elapsed_seconds
    )
    return threads_addressed


def _non_check_reviewer_settle_state_for_runtime(
    threads_addressed: dict[str, str],
    *,
    pr_number: int,
    now_monotonic: float,
    now_wall_seconds: float,
) -> dict[str, str]:
    """Convert settled wait markers to runtime monotonic timestamps."""
    started_prefix = _non_check_reviewer_settle_started_prefix(pr_number=pr_number)
    for started_key, started_raw in list(threads_addressed.items()):
        if not started_key.startswith(started_prefix):
            continue
        started_wall_seconds = _initial_review_grace_wall_seconds(started_raw)
        if started_wall_seconds is not None:
            elapsed_seconds = max(now_wall_seconds - started_wall_seconds, 0.0)
            threads_addressed[started_key] = f"{now_monotonic - elapsed_seconds:.6f}"
            continue
        try:
            float(started_raw)
        except (TypeError, ValueError):
            continue
        # Legacy persisted settle markers were process-local monotonic values
        # with no wall-clock anchor. Restarting the wait is conservative after
        # a process or container restart because it avoids premature elapsed
        # decisions from comparing unrelated monotonic clocks.
        threads_addressed[started_key] = f"{now_monotonic:.6f}"
    return threads_addressed


def _non_check_reviewer_settle_state_for_persistence(
    threads_addressed: dict[str, str],
    *,
    pr_number: int,
    now_monotonic: float,
    now_wall_seconds: float,
) -> dict[str, str]:
    """Convert settled wait markers back to persisted wall-clock form."""
    started_prefix = _non_check_reviewer_settle_started_prefix(pr_number=pr_number)
    for started_key, started_raw in list(threads_addressed.items()):
        if not started_key.startswith(started_prefix):
            continue
        started_wall_seconds = _initial_review_grace_wall_seconds(started_raw)
        if started_wall_seconds is not None:
            threads_addressed[started_key] = _initial_review_grace_wall_started_value(
                started_wall_seconds
            )
            continue
        try:
            started_monotonic = float(started_raw)
        except (TypeError, ValueError):
            continue
        elapsed_seconds = max(now_monotonic - started_monotonic, 0.0)
        threads_addressed[started_key] = _initial_review_grace_wall_started_value(
            now_wall_seconds - elapsed_seconds
        )
    return threads_addressed


def _initial_review_grace_wait_seconds(
    state: MonitorState,
    *,
    pr_number: int,
    now: float,
    grace_seconds: float,
    poll_interval_seconds: float,
) -> float:
    """Return the one-time initial-review wait, mutating persisted state.

    The key is PR-scoped rather than HEAD-scoped by design: the grace window
    starts when the workspace enters ``monitoring_pr`` and must not restart
    when AWF pushes fix commits.
    """

    if grace_seconds <= 0:
        return 0.0

    done_key = _initial_review_grace_done_key(pr_number)
    if state.threads_addressed_ids.get(done_key) == "elapsed":
        return 0.0

    started_key = _initial_review_grace_started_key(pr_number)
    started_raw = state.threads_addressed_ids.get(started_key)
    if started_raw is None:
        started_at = state.started_at
        state.mark_addressed(started_key, f"{started_at:.6f}")
    else:
        try:
            started_at = float(started_raw)
        except (TypeError, ValueError):
            started_at = state.started_at
            state.mark_addressed(started_key, f"{started_at:.6f}")

    remaining_seconds = grace_seconds - max(now - started_at, 0.0)
    if remaining_seconds <= 0:
        state.mark_addressed(done_key, "elapsed")
        return 0.0

    return min(poll_interval_seconds, remaining_seconds)


def _collect_defer_items(
    status: PRStatus, state: MonitorState
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Collect deferred / needs-human threads/comments, partitioned by author.

    Returns ``(bot_items, human_items)``. Items whose author classifies
    as a bot per ``pr_monitor._is_bot_author`` go into the first list;
    the rest (including unknown-author items, which the merge gate
    treats as human for safety) go into the second — the artifact
    mirrors that classification so orchestrators see the same picture.

    Both ``defer`` and ``needs_human`` verdicts are collected (#305): a
    ``needs_human`` item blocks the merge just as a ``defer`` one does, so
    dropping it would let the terminal artifact and notification under-report
    the open feedback. Each item carries its ``verdict`` so consumers can tell
    the two apart.
    """
    bot_items: list[dict[str, object]] = []
    human_items: list[dict[str, object]] = []
    for t in status.unresolved_inline_threads:
        verdict = state.threads_addressed_ids.get(t.thread_id)
        if verdict not in {"defer", "needs_human"}:
            continue
        bucket = bot_items if _is_bot_review_thread(t) else human_items
        bucket.append(
            {
                "kind": "thread",
                "id": t.thread_id,
                "author": t.author,
                "path": t.path,
                "line": t.line,
                "body": t.body_excerpt,
                "verdict": verdict,
                "agent_verdict_reason": None,
            }
        )
    for c in status.unresolved_review_comments:
        verdict = state.threads_addressed_ids.get(c.comment_id)
        if verdict not in {"defer", "needs_human"}:
            continue
        bucket = bot_items if _is_bot_author(c.author) else human_items
        bucket.append(
            {
                "kind": "review",
                "id": c.comment_id,
                "author": c.author,
                "path": None,
                "line": None,
                "body": c.body_excerpt,
                "verdict": verdict,
                "agent_verdict_reason": None,
            }
        )
    return bot_items, human_items


def _pending_review_feedback_count(status: PRStatus, state: MonitorState) -> int:
    """Count review feedback still requiring agent attention under monitor state.

    This is the operator-facing counterpart to ``review_feedback``: the raw
    outside-diff retained inbox is exposed as ``review_feedback``, while this
    metric only counts items that can still be triaged now (body-hash and prior
    verdict state applied) and is logged as ``unresolved_reviews``.
    """
    return sum(
        1
        for comment in status.unresolved_review_comments
        if _agent_can_triage_review_comment(comment)
        and _review_comment_needs_attention(state, comment)
    )


def _changed_paths_from_porcelain(status_stdout: str) -> list[str]:
    """Extract changed paths from ``git status --porcelain`` output."""
    paths: list[str] = []
    for line in status_stdout.splitlines():
        if not line:
            continue
        if line.startswith("?? ") or (len(line) >= 4 and line[2] == " "):
            path = line[3:]
        else:
            continue
        if " -> " in path:
            old_path, new_path = path.split(" -> ", 1)
            paths.extend([old_path, new_path])
        else:
            paths.append(path)
    return list(dict.fromkeys(paths))


def _porcelain_z_records(status_stdout: str) -> list[tuple[str, str, str | None]]:
    records = status_stdout.split("\0")
    if records and records[-1] == "":
        records = records[:-1]
    parsed: list[tuple[str, str, str | None]] = []
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        if len(record) < 4 or record[2] != " ":
            continue
        status = record[:2]
        path = record[3:]
        original_path: str | None = None
        if (status[:1] in {"R", "C"} or status[1:2] in {"R", "C"}) and i < len(records):
            original_path = records[i]
            i += 1
        parsed.append((status, path, original_path))
    return parsed


def _changed_paths_from_porcelain_z(status_stdout: str) -> list[str]:
    """Extract changed paths from ``git status --porcelain -z`` output."""
    paths: list[str] = []
    for status, path, original_path in _porcelain_z_records(status_stdout):
        if status == "!!":
            continue
        if original_path:
            paths.extend([original_path, path])
        else:
            paths.append(path)
    return list(dict.fromkeys(paths))


def _changed_paths_from_name_status_z(diff_stdout: str) -> tuple[str, ...]:
    """Extract changed paths from ``git diff --name-status -z`` output."""
    try:
        return _parse_name_status_z(diff_stdout)
    except ValueError as exc:
        raise ProtectedScopeDiffError(str(exc)) from exc


def _changed_paths_from_name_only_z(diff_stdout: str) -> tuple[str, ...]:
    """Extract changed paths from ``git diff --name-only -z`` output."""
    parts = diff_stdout.split("\0")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if any(part == "" for part in parts):
        raise ProtectedScopeDiffError("empty path in `--name-only -z` output")
    return tuple(dict.fromkeys(parts))


def _quality_gate_violation_paths(violations: Sequence[QualityGateViolation]) -> list[str]:
    return list(dict.fromkeys(violation.path for violation in violations))


def _read_worktree_text(path: Path, *, display_path: str | None = None) -> str:
    label = display_path or str(path)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ProtectedScopeDiffError(
            f"Could not read protected worktree file {label!r} as UTF-8 for classification"
        ) from exc
    except OSError as exc:
        raise ProtectedScopeDiffError(
            f"Could not read protected worktree file {label!r} for classification: {exc}"
        ) from exc


def _untracked_paths_from_porcelain(status_stdout: str) -> list[str]:
    """Extract untracked paths from ``git status --porcelain`` output."""
    paths: list[str] = []
    for line in status_stdout.splitlines():
        if not line.startswith("?? "):
            continue
        paths.append(line[3:])
    return list(dict.fromkeys(paths))


def _untracked_paths_from_porcelain_z(status_stdout: str) -> list[str]:
    """Extract untracked paths from ``git status --porcelain -z`` output."""
    return list(
        dict.fromkeys(
            path
            for status, path, _original_path in _porcelain_z_records(status_stdout)
            if status == "??"
        )
    )


def _supply_chain_policy_blocked_message(reason_codes: Iterable[str]) -> str:
    codes = list(dict.fromkeys(reason_codes))
    suffix = f": {', '.join(codes)}" if codes else "."
    return f"Supply-chain policy blocked PR monitor publication{suffix}"


def _target_reconcile_payload(result: object) -> dict[str, object]:
    if isinstance(result, dict):
        return dict(result)
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, dict):
            return dict(payload)
    return {"result": str(result)}


def _target_reconcile_log_fields(payload: Mapping[str, object]) -> dict[str, object]:
    fields = dict(payload)
    fields.setdefault("resolver_results", [])
    fields.setdefault("commit_sha", None)
    fields.setdefault("pushed", False)
    fields.setdefault("changed_paths", [])
    fields.setdefault("dry_run", None)
    fields.setdefault("commit_allowed", None)
    fields.setdefault("policy_reason_code", None)
    return fields


def _target_reconcile_failure_payload(
    exc: Exception,
    *,
    error_limit: int,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "failed",
        "reason_code": "TARGET_BRANCH_RECONCILE_FAILED",
        "error": str(exc)[:error_limit],
        "error_type": type(exc).__name__,
        "resolver_results": [],
        "commit_sha": None,
        "pushed": False,
        "changed_paths": [],
        "dry_run": None,
        "commit_allowed": None,
        "policy_reason_code": None,
    }

    operation = getattr(exc, "operation", None)
    if isinstance(operation, str):
        payload["operation"] = operation
    result = getattr(exc, "result", None)
    returncode = getattr(result, "returncode", None)
    if isinstance(returncode, int):
        payload["returncode"] = returncode
    reason_code = getattr(result, "reason_code", None)
    if isinstance(reason_code, str):
        payload["command_reason_code"] = reason_code
    stderr = getattr(result, "stderr", None)
    if isinstance(stderr, str) and stderr:
        payload["stderr"] = stderr[:error_limit]
    stdout = getattr(result, "stdout", None)
    if isinstance(stdout, str) and stdout:
        payload["stdout"] = stdout[:error_limit]
    return payload


def _truncate_target_reconcile_failure_payload(
    payload: Mapping[str, object],
    *,
    error_limit: int,
) -> dict[str, object]:
    truncated = dict(payload)
    for key in ("error", "stderr", "stdout"):
        value = truncated.get(key)
        if isinstance(value, str):
            truncated[key] = value[:error_limit]
    return truncated
