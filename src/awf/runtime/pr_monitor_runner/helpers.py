"""Pull request monitor helper functions.

Mechanically extracted from the original orchestrator; behavior is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import (
    UTC,
    datetime,
)
from pathlib import Path

from awf.common.bitbucket_client import (
    BITBUCKET_API_ERROR,
    BITBUCKET_AUTH_FAILED,
    BITBUCKET_MERGE_IN_PROGRESS,
    BITBUCKET_MERGE_TASK_TIMEOUT,
    BITBUCKET_RATE_LIMITED,
    BITBUCKET_TRANSPORT_ERROR,
    BitbucketClientError,
)
from awf.common.github_client import GitHubClientError
from awf.common.github_transient import is_transient_github_error_text
from awf.control.quality_gates import QualityGateViolation
from awf.control.state_machine import WorkspaceStateMachine
from awf.db.enums import (
    OperationStatus,
    OperationType,
    WorkspaceStatus,
)
from awf.db.models import Operation, Workspace
from awf.db.repositories import WorkspaceRepository, pr_feedback_body_hash
from awf.runtime.monitor_state_keys import (
    _initial_review_grace_done_key as _initial_review_grace_done_key,
)
from awf.runtime.monitor_state_keys import (
    _initial_review_grace_started_key as _initial_review_grace_started_key,
)
from awf.runtime.monitor_state_keys import (
    _initial_review_grace_wall_started_value as _initial_review_grace_wall_started_value,
)
from awf.runtime.monitor_state_keys import (
    _initial_review_grace_wall_started_value_from_datetime as _initial_review_grace_wall_started_value_from_datetime,
)
from awf.runtime.monitor_state_keys import (
    _non_check_reviewer_settle_done_key as _non_check_reviewer_settle_done_key,
)
from awf.runtime.monitor_state_keys import (
    _non_check_reviewer_settle_freeze_key as _non_check_reviewer_settle_freeze_key,
)
from awf.runtime.monitor_state_keys import (
    _non_check_reviewer_settle_started_key as _non_check_reviewer_settle_started_key,
)
from awf.runtime.monitor_state_keys import (
    _non_check_reviewer_settle_started_prefix as _non_check_reviewer_settle_started_prefix,
)
from awf.runtime.monitor_state_keys import (
    _outdated_resolve_requeued_key as _outdated_resolve_requeued_key,
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
from awf.runtime.pr_monitor_runner import reviewer_settle as _reviewer_settle
from awf.runtime.pr_monitor_runner.comments import (
    Verdict,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.constants import (
    _AUTHORIZATION_BEARER_RE,
    _AWF_VERDICT,
    _BASE_FETCH_RETRY_COUNT_KEY_PREFIX,
    _BITBUCKET_TRANSIENT_HTTP_STATUSES,
    _FORGE_TRANSIENT_RETRY_COUNT_KEY_PREFIX,
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
)
from awf.runtime.pr_monitor_runner.gates import (
    _MergeGateResult,
)
from awf.runtime.pr_monitor_runner.path_helpers import (
    _changed_paths_from_name_only_z as _changed_paths_from_name_only_z,
)
from awf.runtime.pr_monitor_runner.path_helpers import (
    _quality_gate_violation_paths as _quality_gate_violation_paths,
)
from awf.runtime.pr_monitor_runner.path_helpers import (
    _read_worktree_text as _read_worktree_text,
)
from awf.runtime.pr_monitor_runner.path_helpers import (
    _supply_chain_policy_blocked_message as _supply_chain_policy_blocked_message,
)
from awf.runtime.pr_monitor_runner.path_parsing import (
    _changed_paths_from_name_status_z as _changed_paths_from_name_status_z,
)
from awf.runtime.pr_monitor_runner.path_parsing import (
    _changed_paths_from_porcelain as _changed_paths_from_porcelain,
)
from awf.runtime.pr_monitor_runner.path_parsing import (
    _changed_paths_from_porcelain_z as _changed_paths_from_porcelain_z,
)
from awf.runtime.pr_monitor_runner.path_parsing import (
    _porcelain_z_records as _porcelain_z_records,
)
from awf.runtime.pr_monitor_runner.path_parsing import (
    _split_porcelain_rename_paths as _split_porcelain_rename_paths,
)
from awf.runtime.pr_monitor_runner.path_parsing import (
    _unquote_porcelain_path as _unquote_porcelain_path,
)
from awf.runtime.pr_monitor_runner.path_parsing import (
    _untracked_paths_from_porcelain as _untracked_paths_from_porcelain,
)
from awf.runtime.pr_monitor_runner.path_parsing import (
    _untracked_paths_from_porcelain_z as _untracked_paths_from_porcelain_z,
)
from awf.runtime.pr_monitor_runner.target_reconcile import (
    _target_reconcile_failure_payload as _target_reconcile_failure_payload,
)
from awf.runtime.pr_monitor_runner.target_reconcile import (
    _target_reconcile_log_fields as _target_reconcile_log_fields,
)
from awf.runtime.pr_monitor_runner.target_reconcile import (
    _target_reconcile_payload as _target_reconcile_payload,
)
from awf.runtime.pr_monitor_runner.target_reconcile import (
    _truncate_target_reconcile_failure_payload as _truncate_target_reconcile_failure_payload,
)
from awf.runtime.pr_monitor_runner.types import BaseFetchError

_datetime_iso = _reviewer_settle._datetime_iso
_non_check_reviewer_activity_settle_decision = (
    _reviewer_settle._non_check_reviewer_activity_settle_decision
)
_non_check_reviewer_activity_signature = _reviewer_settle._non_check_reviewer_activity_signature
_non_check_reviewer_activity_freeze_elapsed_seconds = (
    _reviewer_settle._non_check_reviewer_activity_freeze_elapsed_seconds
)
_non_check_reviewer_settle_decision = _reviewer_settle._non_check_reviewer_settle_decision
_non_check_reviewer_settle_skip_visible_key = (
    _reviewer_settle._non_check_reviewer_settle_skip_visible_key
)
_non_check_reviewer_settle_wait_operation_context = (
    _reviewer_settle._non_check_reviewer_settle_wait_operation_context
)
_non_check_reviewer_visibility = _reviewer_settle._non_check_reviewer_visibility
_non_check_reviewer_visible_aliases = _reviewer_settle._non_check_reviewer_visible_aliases
_normalize_non_check_reviewer_identity = _reviewer_settle._normalize_non_check_reviewer_identity
_normalize_non_check_reviewer_logins = _reviewer_settle._normalize_non_check_reviewer_logins
_reviewer_has_visible_check = _reviewer_settle._reviewer_has_visible_check
_utc_datetime = _reviewer_settle._utc_datetime
_visible_check_identities = _reviewer_settle._visible_check_identities

_BARE_VERDICT_LINE = re.compile(
    r"^(?P<label>FALSE\s+POSITIVE|DEFER|NEEDS[\s_]+HUMAN)\s*:\s*(?P<reason>[^\n\r]*)$",
    re.IGNORECASE,
)
_VERDICT_REASON_TEMPLATE_PLACEHOLDER = re.compile(
    r"<\s*(?:what|one[-\s]?sentence|summary|reason|track|decision|defer|need)\b[^>\n\r]{0,80}>",
    re.IGNORECASE,
)
_CODE_FORMATTED_VERDICT_LINE = re.compile(r"^(?P<ticks>`+)\s*(?P<line>.*?)\s*(?P=ticks)$")


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
    # AWF-prefixed verdicts are canonical and must win over bare fallback lines,
    # even when bare lines appear later in output. When multiple AWF verdicts are
    # present, the final AWF line wins. If that line omits a reason, preserve an
    # earlier reason for the same verdict. Sanitized non-blocking placeholders
    # (for example ``AWF-VERDICT: FIXED: <one-sentence summary>``) may fall back
    # to an earlier reasoned verdict so a prompt echo cannot clear a hard block;
    # blocking final verdicts remain authoritative even with no usable reason.
    awf_verdicts: list[VerdictResult] = []
    bare_verdicts: list[VerdictResult] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        for verdict_line in _verdict_line_candidates(stripped):
            awf_match = _AWF_VERDICT.fullmatch(verdict_line)
            if awf_match is not None:
                awf_verdicts.append(
                    _verdict_result_from_match(
                        label=awf_match.group("label"),
                        reason=awf_match.group("reason"),
                    )
                )
            bare_match = _BARE_VERDICT_LINE.fullmatch(verdict_line)
            if bare_match is not None:
                bare_verdicts.append(
                    _verdict_result_from_match(
                        label=bare_match.group("label"),
                        reason=bare_match.group("reason"),
                    )
                )
    verdicts = awf_verdicts or bare_verdicts
    if verdicts is awf_verdicts:
        latest = verdicts[-1]
        if latest.reason is None:
            latest_verdict = latest.verdict
            for parsed in reversed(verdicts[:-1]):
                if parsed.verdict == latest_verdict and parsed.reason is not None:
                    return parsed
            if latest_verdict in {"defer", "needs_human"}:
                return latest
            for parsed in reversed(verdicts[:-1]):
                if parsed.reason is not None:
                    return parsed
            return latest
        return latest
    for verdict in ("needs_human", "false_positive", "defer", "fix_committed"):
        selected: VerdictResult | None = None
        for parsed in reversed(verdicts):
            if parsed.verdict != verdict:
                continue
            if parsed.reason is not None:
                return parsed
            if selected is None:
                selected = parsed
        if selected is not None:
            return selected
    return VerdictResult(verdict="fix_committed")


def _verdict_line_candidates(stripped: str) -> Iterable[str]:
    yield stripped
    code_match = _CODE_FORMATTED_VERDICT_LINE.fullmatch(stripped)
    if code_match is None:
        return
    inner = code_match.group("line").strip()
    if inner:
        yield inner


def _verdict_result_from_match(*, label: str, reason: str | None) -> VerdictResult:
    # Canonicalize any run of whitespace/underscores to a single space, so
    # every separator variant the label regex accepts (NEEDS_HUMAN,
    # NEEDS HUMAN, NEEDS_ HUMAN, ...) maps to one label. The regex and this
    # normalization must stay equally permissive, or a matched NEEDS_HUMAN
    # could silently fall through to fix_committed — the unsafe direction (#305).
    normalized_label = re.sub(r"[\s_]+", " ", label.strip().lower())
    cleaned_reason = _sanitize_verdict_reason(reason)
    if normalized_label == "false positive":
        return VerdictResult(verdict="false_positive", reason=cleaned_reason)
    if normalized_label == "needs human":
        return VerdictResult(verdict="needs_human", reason=cleaned_reason)
    if normalized_label == "defer":
        return VerdictResult(verdict="defer", reason=cleaned_reason)
    return VerdictResult(verdict="fix_committed", reason=cleaned_reason)


def _sanitize_verdict_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    cleaned = reason.strip()
    if not cleaned:
        return None
    if _VERDICT_REASON_TEMPLATE_PLACEHOLDER.search(cleaned):
        return None
    return cleaned


def _review_comment_resolution_body(comment: ReviewComment) -> str:
    return comment.body or comment.body_excerpt or ""


def _review_comment_body_state_key(comment_id: str) -> str:
    return f"__review_comment_body_hash__:{comment_id}"


def _defer_reason_state_key(thread_id: str) -> str:
    """State key holding the agent's ``DEFER: <reason>`` text for a thread.

    ``_address_thread`` only returns the verdict, so the reason is stashed here
    when the agent defers and read back by ``_capture_deferred_review_thread`` so
    the filed tracking issue preserves the agent's specific follow-up, not just
    the GitHub thread conversation.
    """
    return f"__defer_reason__:{thread_id}"


def _needs_human_reason_state_key(item_id: str) -> str:
    return f"__needs_human_reason__:{item_id}"


def _sync_needs_human_reason(
    state: MonitorState,
    item_id: str,
    result: VerdictResult,
) -> None:
    """Persist or clear the agent's needs-human reason for a review item."""
    reason_key = _needs_human_reason_state_key(item_id)
    if result.verdict == "needs_human" and (reason := _sanitize_verdict_reason(result.reason)):
        state.mark_addressed(reason_key, reason)
    else:
        state.threads_addressed_ids.pop(reason_key, None)


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
    state.threads_addressed_ids.pop(_needs_human_reason_state_key(item_id), None)
    state.threads_addressed_ids.pop(_defer_reason_state_key(item_id), None)
    state.threads_addressed_ids.pop(_outdated_resolve_requeued_key(item_id), None)


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

    def payload(self: _StalePendingCheckWarning) -> dict[str, object]:
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


def _awaiting_required_checks_first_seen_key(head_sha: str) -> str:
    """Reserved ``MonitorState.threads_addressed_ids`` key carrying the first
    poll time (epoch float string) the given head showed "required CI expected
    but absent" (#655). One key per ``head_sha`` so each monitor push gets its
    own grace window; stale keys for old heads linger harmlessly, like the
    stale-pending and reviewer-settle markers."""
    return f"__awf_awaiting_required_checks_first_seen__:{head_sha}"


def _awaiting_required_checks_grace(
    status: PRStatus,
    state: MonitorState,
    config: MonitorConfig,
    *,
    now: datetime,
) -> tuple[bool, bool]:
    """Return ``(grace_active, state_changed)`` for the transient required-CI gap.

    Tracks, per ``head_sha``, the first poll time the head showed required CI
    expected-but-absent (``config.require_ci`` and ``status.no_checks_observed``)
    and reports whether that first observation is still within
    ``config.awaiting_required_checks_grace_seconds``. A new ``head_sha`` starts a
    fresh window (its key is absent → first observation again).

    ``state_changed`` is ``True`` only when this call recorded a new first-seen
    marker, signalling the caller to persist ``state`` — mandatory, because the
    runner reloads ``state`` from the DB every poll, so an unpersisted marker
    would re-read as absent forever and the window would never expire.

    Returns ``(False, False)`` and records nothing when the condition does not
    apply (``require_ci`` off, checks present, or the grace disabled with
    ``grace_seconds <= 0``)."""
    if not (config.require_ci and status.no_checks_observed):
        return (False, False)
    grace_seconds = config.awaiting_required_checks_grace_seconds
    if grace_seconds <= 0:
        return (False, False)
    key = _awaiting_required_checks_first_seen_key(status.head_sha)
    raw = state.threads_addressed_ids.get(key)
    now_ts = _as_utc(now).timestamp()
    if raw is None:
        state.mark_addressed(key, f"{now_ts:.6f}")
        return (True, True)  # first observation → within grace
    try:
        first_seen = float(raw)
    except (TypeError, ValueError):
        state.mark_addressed(key, f"{now_ts:.6f}")
        return (True, True)
    return (now_ts - first_seen < grace_seconds, False)


def _notify_human_reason(status: PRStatus, state: MonitorState) -> str | None:
    if status.blocking_reviews:
        return "a merge-blocking changes-requested review remains unresolved"
    if reason := _first_needs_human_reason(status, state):
        return reason
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


def _first_needs_human_reason(status: PRStatus, state: MonitorState) -> str | None:
    for item_id in [t.thread_id for t in status.unresolved_inline_threads] + [
        c.comment_id for c in status.unresolved_review_comments
    ]:
        if (
            state.threads_addressed_ids.get(item_id) == "needs_human"
            and (reason := state.threads_addressed_ids.get(_needs_human_reason_state_key(item_id)))
            and (sanitized_reason := _sanitize_verdict_reason(reason))
        ):
            return sanitized_reason
    return None


def _merge_rejection_reason(stderr: str) -> str:
    detail = " ".join(_redact_and_truncate_forge_error(stderr).split())[:240]
    if detail:
        return f"GitHub rejected the merge attempt: {detail}"
    return "GitHub rejected the merge attempt"


def _bitbucket_merge_rejection_reason(exc: BitbucketClientError) -> str:
    """Describe a deterministic Bitbucket merge failure for a human notification.

    Mirrors :func:`_merge_rejection_reason`. ``exc`` already carries a redacted
    body (set at construction) and always renders a non-empty
    ``operation failed (status=...)`` summary, so surface it directly; collapse
    whitespace and cap to match the GitHub wording.
    """
    detail = " ".join(str(exc).split())[:240]
    return f"Bitbucket rejected the merge attempt: {detail}"


def _attach_transient_retry_counters(
    payload: dict[str, object],
    *,
    retry_number: int | None,
    max_retries: int | None,
) -> dict[str, object]:
    """Attach the optional retry-budget fields shared by forge retry payloads.

    Both the GitHub and Bitbucket transient-retry payloads carry ``retry_number``
    and ``max_retries`` only when the bounded-retry budget supplies them; centralise
    that conditional attachment so the two builders stay in lockstep.
    """
    if retry_number is not None:
        payload["retry_number"] = retry_number
    if max_retries is not None:
        payload["max_retries"] = max_retries
    return payload


def _transient_github_retry_payload(
    exc: GitHubClientError,
    *,
    context: str,
    pr_number: int,
    wait_seconds: float,
    retry_number: int | None = None,
    max_retries: int | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "context": context,
        "operation": exc.operation,
        "returncode": exc.returncode,
        "pr_number": pr_number,
        "wait_seconds": wait_seconds,
        "message": _redact_and_truncate_forge_error(str(exc)),
        "stderr": _redact_and_truncate_forge_error(exc.stderr),
    }
    return _attach_transient_retry_counters(
        payload, retry_number=retry_number, max_retries=max_retries
    )


def _transient_bitbucket_retry_payload(
    exc: BitbucketClientError,
    *,
    context: str,
    pr_number: int,
    wait_seconds: float,
    retry_number: int | None = None,
    max_retries: int | None = None,
) -> dict[str, object]:
    # ``exc`` already carries a redacted body (set at construction), so its
    # ``str()`` is safe to log/persist; truncate to match the GitHub payload.
    payload: dict[str, object] = {
        "context": context,
        "operation": exc.operation,
        "status": exc.status,
        "reason_code": exc.reason_code,
        "pr_number": pr_number,
        "wait_seconds": wait_seconds,
        "message": str(exc)[:400],
    }
    return _attach_transient_retry_counters(
        payload, retry_number=retry_number, max_retries=max_retries
    )


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
        "message": _redact_and_truncate_forge_error(str(exc)),
    }


def _redact_and_truncate_forge_error(value: str, *, limit: int = 400) -> str:
    redacted = _URL_CREDENTIAL_RE.sub(r"\1<redacted>@", value)
    redacted = _AUTHORIZATION_BEARER_RE.sub(r"\1<redacted>", redacted)
    redacted = _TOKEN_RE.sub(_REDACTION, redacted).strip()
    redacted = redacted.replace("[redacted]", _REDACTION)
    if len(redacted) <= limit:
        return redacted
    return redacted[: limit - 3] + "..."


def _is_transient_github_client_error(exc: GitHubClientError) -> bool:
    """Classify GitHub/gh failures that should keep the monitor polling."""

    return is_transient_github_error_text(operation=exc.operation, stderr=exc.stderr)


def _is_transient_bitbucket_client_error(exc: BitbucketClientError) -> bool:
    """Classify Bitbucket failures that should keep the monitor polling.

    Symmetric to :func:`_is_transient_github_client_error`: recoverable blips
    should make the monitor wait and re-poll rather than terminating the
    workspace. Recoverable cases are rate limiting that survived the client's
    internal ``Retry-After`` backoff, transport-level faults (connection
    reset/refused, timeout, DNS — surfaced as ``BITBUCKET_TRANSPORT_ERROR``),
    a 409 on the merge POST signalling an already-in-flight merge
    (``BITBUCKET_MERGE_IN_PROGRESS``), an exhausted async-merge poll budget while
    the merge task was still PENDING (``BITBUCKET_MERGE_TASK_TIMEOUT`` — Bitbucket
    may still complete it server-side), 5xx server faults, and — symmetric with
    GitHub's ambiguous-401 handling (#515) — any 401/403 auth fault
    (``BITBUCKET_AUTH_FAILED``, set only for 401/403). A momentary auth blip
    recovers within the bounded forge-retry budget; a genuinely bad token exhausts
    the budget and terminates. ``BITBUCKET_AUTH_NOT_CONFIGURED`` (a deterministic
    env-config error) is a distinct code and stays non-transient. Other
    deterministic faults — non-auth 4xx, JSON parse, and the pagination/SSRF safety
    aborts (which also carry ``status=None`` but map to ``BITBUCKET_API_ERROR``) —
    must fail fast.
    """

    if exc.reason_code in (
        BITBUCKET_RATE_LIMITED,
        BITBUCKET_TRANSPORT_ERROR,
        BITBUCKET_MERGE_IN_PROGRESS,
        BITBUCKET_MERGE_TASK_TIMEOUT,
        BITBUCKET_AUTH_FAILED,
    ):
        return True
    if exc.reason_code != BITBUCKET_API_ERROR:
        return False
    return exc.status in _BITBUCKET_TRANSIENT_HTTP_STATUSES


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


def _forge_transient_retry_count_key(context: str) -> str:
    return f"{_FORGE_TRANSIENT_RETRY_COUNT_KEY_PREFIX}{context}"


def _increment_forge_transient_retry_count(state: MonitorState, context: str) -> int:
    """Bump and return the per-context transient-forge retry count.

    Mirrors :func:`_increment_base_fetch_retry_count` (own key prefix, own
    counter) and is corrupt-value-safe: a non-integer stored value resets to 1 so
    a malformed resumed state can never wedge the budget.
    """
    key = _forge_transient_retry_count_key(context)
    raw_count = state.threads_addressed_ids.get(key, "0")
    try:
        current = int(raw_count)
    except ValueError:
        current = 0
    retry_number = current + 1
    state.threads_addressed_ids[key] = str(retry_number)
    return retry_number


def _clear_transient_forge_retry_state(state: MonitorState, *, context: str) -> bool:
    key = _forge_transient_retry_count_key(context)
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


def _exponential_backoff_wait_seconds(
    *,
    retry_number: int,
    initial_backoff_seconds: float,
    max_backoff_seconds: float,
) -> float:
    """Compute the capped exponential backoff wait for the Nth retry.

    Shared by the base-fetch and forge transient-retry paths so both use a single,
    byte-identical schedule (DRY) without coupling their separate config knobs or
    retry counters. ``retry_number`` is 1-based: the first retry waits
    ``initial_backoff_seconds``, the second ``2x``, and so on, capped at
    ``max_backoff_seconds``.
    """
    initial = max(initial_backoff_seconds, 0.0)
    cap = max(max_backoff_seconds, 0.0)
    exponent = min(max(retry_number - 1, 0), 30)
    wait_seconds = initial * float(2**exponent)
    return wait_seconds if wait_seconds < cap else cap


def _base_fetch_retry_wait_seconds(
    *,
    retry_number: int,
    initial_backoff_seconds: float,
    max_backoff_seconds: float,
) -> float:
    return _exponential_backoff_wait_seconds(
        retry_number=retry_number,
        initial_backoff_seconds=initial_backoff_seconds,
        max_backoff_seconds=max_backoff_seconds,
    )


def _notification_key(*, head_sha: str, blocker_reason: str | None) -> str:
    reason = blocker_reason or "ready-to-merge"
    return f"__awf_notify__:{head_sha}:{reason}"


def _protected_block_violations_digest(violations: Sequence[QualityGateViolation]) -> str:
    """Return a stable, order-independent digest of protected-scope violations.

    Keyed on the sorted ``(path, protected_pattern, reason)`` tuples so the same
    set of violations always hashes the same regardless of discovery order, while
    a different violation set (different path/pattern/reason) hashes differently —
    the content half of the notification dedupe key."""
    tuples = sorted((v.path, v.protected_pattern, v.reason or "") for v in violations)
    payload = json.dumps(tuples, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _protected_block_notification_key(*, block_epoch: int, violations_digest: str) -> str:
    """Build the protected-block human-notification dedupe key.

    Keyed on the block epoch + violation content rather than head/lifetime so a
    second/different violation (which bumps ``block_epoch``) or a changed
    directive re-notifies, while a re-notify within one block epoch is
    suppressed."""
    return f"__awf_protected_block__:{block_epoch}:{violations_digest}"


def _merge_queue_wait_key(*, head_sha: str, blocker_candidate_id: str) -> str:
    return f"__awf_merge_queue_wait__:{head_sha}:{blocker_candidate_id}"


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
    bot_items, human_deferred = _collect_defer_items(status, state)
    if human_deferred:
        return False
    # #305: a bot inline thread (defer/needs_human) or a bot needs_human comment
    # also blocks the merge in decide() gate 7, even though it isn't
    # human-deferred. A PR is only a "ready for human merge (branch protection)"
    # handoff when none of those are present — mirror the _notify_human_reason
    # guard so we never broadcast "ready" while decide() is still blocking.
    return not any(
        item["kind"] == "thread" or item.get("verdict") == "needs_human" for item in bot_items
    )


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
                "agent_verdict_reason": state.threads_addressed_ids.get(
                    _needs_human_reason_state_key(t.thread_id)
                ),
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
                "agent_verdict_reason": state.threads_addressed_ids.get(
                    _needs_human_reason_state_key(c.comment_id)
                ),
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
