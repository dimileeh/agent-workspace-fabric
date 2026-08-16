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
from awf.common.github_transient import (
    GITHUB_AUTH_TRANSIENT_EVIDENCE_MARKERS,
    GitHubErrorDisposition,
    github_error_disposition,
)
from awf.common.redaction import redact_secrets
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
from awf.runtime.monitor_state_keys import (
    _salvaged_fix_body_hash_state_key as _salvaged_fix_body_hash_state_key,
)
from awf.runtime.monitor_state_keys import (
    _salvaged_fix_head_state_key as _salvaged_fix_head_state_key,
)
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckFailureLogResult,
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
    _AMBIGUOUS_GITHUB_AUTH_TRANSIENT_MARKERS,
    _AUTHORIZATION_BEARER_RE,
    _AWF_VERDICT,
    _AWF_VERDICT_MARKER,
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
from awf.runtime.pr_monitor_runner.notify_human_details import (
    _collect_defer_items as _collect_defer_items_impl,
)
from awf.runtime.pr_monitor_runner.notify_human_details import (
    _needs_human_reason_state_key,
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


def _collect_defer_items(
    status: PRStatus, state: MonitorState
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Compatibility delegate for deferred-review consumers."""
    return _collect_defer_items_impl(status, state)


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
# Match whole-reason prompt-template placeholders. An unanchored or start-only
# search falsely strips legitimate mid-reason or leading-content tags such as
# ``added the <summary> section`` / ``<summary> section rewritten``. Allow only
# trailing prompt boilerplate (e.g. `` and exit."``) after the placeholder so
# stored echoes normalize away. Whole-reason ellipsis echoes of the prompt form
# ``FIXED: …`` are also treated as placeholders — not ``...real content``.
_VERDICT_REASON_TEMPLATE_PLACEHOLDER = re.compile(
    r"^\s*(?:"
    r"<\s*(?:what|one[-\s]?sentence|summary|reason|track|decision|defer|need)"
    r"\b[^>\n\r]{0,80}>"
    r"|…|\.{3}"
    r")"
    r"(?:\s+and\s+exit\.?)?"
    r"[\s\"'”’]*$",
    re.IGNORECASE,
)
_VERDICT_REASON_REDACTION_ONLY = re.compile(
    rf"^[\s,;:.!?'\"“”‘’]*(?:(?:[A-Za-z][A-Za-z0-9_-]*\s*[:=]\s*)?"
    rf"[\s,;:.!?'\"“”‘’]*{re.escape(_REDACTION)}[\s,;:.!?'\"“”‘’]*)+$",
    re.IGNORECASE,
)
_CODE_FORMATTED_VERDICT_LINE = re.compile(r"^(?P<ticks>`+)\s*(?P<line>.*?)\s*(?P=ticks)$")
# Multiline Markdown fences (CommonMark-style). Info strings may not contain the
# fence character, so same-line wraps (`` ```verdict``` ``) are not openers.
# Markers inside an open fence must not participate in verdict selection.
_MARKDOWN_FENCE_OPEN = re.compile(
    r"^ {0,3}(?:"
    r"(?P<fence>`{3,})[^`\n]*|"
    r"(?P<fence_tilde>~{3,})[^~\n]*"
    r")[ \t]*$"
)
# CommonMark indented code: four spaces of indent, treating tabs as stops of
# four columns — so a leading tab or 1–3 spaces plus a tab also qualifies.
# Unconditional ``str.strip`` would promote example markers inside these
# regions to authoritative finals.
_MARKDOWN_INDENTED_CODE_LINE = re.compile(r"^(?: {4,}| {0,3}\t)")
# Leading Markdown list markers agents often emit before a canonical verdict line
# (``- AWF-VERDICT: …``, ``1. AWF-VERDICT: …``). Strip only for attempt
# classification so a final garbled list-prefixed marker still fails closed —
# never yield list-stripped forms as successful fullmatch candidates (that
# would make multiline option lists authoritative over an earlier hard block).
_MARKDOWN_LIST_PREFIX = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
# GFM task-list checkboxes (``- [ ] AWF-VERDICT: …``, ``- [x] …``). Plain list
# strip leaves ``[ ]`` before the marker, so the final line is not classified
# as an attempt and an earlier resolvable verdict can win incorrectly.
_MARKDOWN_TASK_LIST_CHECKBOX = re.compile(r"^\[(?: |x|X)\]\s+")
# Leading Markdown blockquote markers (``> AWF-VERDICT: …``, nested ``>>``).
# Same attempt-only strip as list prefixes — a trailing blockquoted marker must
# fail closed rather than leave an earlier resolvable verdict selected.
_MARKDOWN_BLOCKQUOTE_PREFIX = re.compile(r"^(?:>\s*)+")
_MAX_VERDICT_REASON_LENGTH = 500


def _markdown_fence_open_marker(line: str) -> str | None:
    """Return the fence marker that opens a multiline code fence on ``line``."""
    opened = _MARKDOWN_FENCE_OPEN.match(line)
    if opened is None:
        return None
    return opened.group("fence") or opened.group("fence_tilde")


def _markdown_fence_closes(line: str, *, fence: str) -> bool:
    """Return whether ``line`` closes a code fence opened with ``fence``."""
    return re.match(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$", line) is not None


def _iter_non_fenced_verdict_lines(stdout: str) -> Iterable[str]:
    """Yield stripped stdout lines outside Markdown code regions.

    Skips multiline fenced blocks and CommonMark indented-code lines (four
    spaces of indent, including a leading tab or 1–3 spaces plus a tab) so
    quoted example markers cannot override an authoritative unfenced verdict.
    Same-line wrapped fences (`` ```verdict``` ``) are still yielded so
    ``_CODE_FORMATTED_VERDICT_LINE`` can accept them. Unclosed fences shield
    every subsequent line.
    """
    fence: str | None = None
    for line in stdout.splitlines():
        if fence is not None:
            if _markdown_fence_closes(line, fence=fence):
                fence = None
            continue
        if _MARKDOWN_INDENTED_CODE_LINE.match(line):
            continue
        opened = _markdown_fence_open_marker(line)
        if opened is not None:
            fence = opened
            continue
        yield line.strip()


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
    ``AWF-VERDICT:`` line. Markerless, bare-marker, empty, garbled, and FIXED
    placeholder-echo output fail closed to ``needs_human`` — never guess
    ``fix_committed``.
    """
    return _parse_verdict_result(stdout).verdict


def _parse_verdict_result(stdout: str) -> VerdictResult:
    if not stdout.strip():
        # Empty or whitespace-only agent output is a failure to produce, not a
        # considered deferral. Treat it as needs_human so it blocks the merge
        # instead of
        # triggering the follow-up defer capture (comment + filed issue +
        # resolve) on a thread the agent never actually addressed (#305).
        return VerdictResult(verdict="needs_human", reason="empty_verdict_output")
    # AWF-prefixed verdicts are canonical. Bare FALSE POSITIVE / DEFER /
    # NEEDS_HUMAN lines are collected only as hard-block fallbacks when an AWF
    # FIXED line has no usable reason — never as a standalone selected verdict.
    # When multiple AWF verdicts are present, the final AWF line wins. If that
    # line omits a reason (and is not a template placeholder), preserve an
    # earlier reason for the same verdict. A final resolvable placeholder echo
    # must not reuse an earlier same-label reason — that would still resolve or
    # defer contrary to fail-closed grammar.
    # Sanitized non-blocking placeholders (for example
    # ``AWF-VERDICT: FIXED: <one-sentence summary>``) may fall back only to an
    # earlier reasoned hard block (needs_human/defer) or a bare blocking
    # fallback so a prompt echo cannot clear a hard block; the same hard-block
    # fallback applies to FALSE POSITIVE / DEFER placeholders so escalation text
    # is preserved. A genuine no-reason final verdict is otherwise the agent's
    # last word and must not be trumped by an earlier non-blocking verdict
    # (e.g. false_positive).
    # Blocking final verdicts remain authoritative even with no usable reason.
    # Standalone resolvable placeholder echoes fail closed (never resolve).
    # Markerless / bare-only output fails closed to needs_human.
    awf_verdicts: list[VerdictResult] = []
    bare_verdicts: list[VerdictResult] = []
    last_awf_mention_recognized = False
    saw_awf_mention = False
    # Skip fenced and indented code regions so quoted example verdicts cannot
    # override an authoritative unfenced marker (PRRT_kwDOSJAM6s6ZlqAE /
    # PRRT_kwDOSJAM6s6ZlsjH).
    for stripped in _iter_non_fenced_verdict_lines(stdout):
        for verdict_line in _verdict_line_candidates(stripped):
            # Multiple markers on one line are separate verdict units — do not
            # let the first reason group absorb a trailing second marker.
            for segment in _awf_verdict_segments(verdict_line):
                if _AWF_VERDICT_MARKER.search(segment):
                    saw_awf_mention = True
                    awf_match = _AWF_VERDICT.fullmatch(segment)
                    if awf_match is not None:
                        last_awf_mention_recognized = True
                        awf_verdicts.append(
                            _verdict_result_from_match(
                                label=awf_match.group("label"),
                                reason=awf_match.group("reason"),
                            )
                        )
                    elif _awf_verdict_segment_is_attempt(segment):
                        # Leading / split-marker attempts that fail fullmatch are
                        # authoritative garbled finals. Mid-prose quotes of the
                        # marker grammar must not clear a prior recognized verdict.
                        last_awf_mention_recognized = False
                bare_match = _BARE_VERDICT_LINE.fullmatch(segment)
                if bare_match is not None:
                    bare_verdicts.append(
                        _verdict_result_from_match(
                            label=bare_match.group("label"),
                            reason=bare_match.group("reason"),
                        )
                    )
    # A garbled final leading ``AWF-VERDICT:`` attempt fails closed even when an
    # earlier recognized verdict exists — the agent's last marker attempt is
    # authoritative. Embedded prose quotes of the marker do not count.
    if saw_awf_mention and not last_awf_mention_recognized:
        return VerdictResult(verdict="needs_human", reason="garbled_verdict_marker")
    if awf_verdicts:
        latest = awf_verdicts[-1]
        if latest.reason is None:
            latest_verdict = latest.verdict
            # Check the final raw reason before same-label reuse: a template
            # placeholder must not inherit an earlier reasoned same-label verdict
            # (that would still resolve/defer, contrary to fail-closed grammar).
            final_is_resolvable_placeholder = (
                latest_verdict in _RESOLVABLE_PLACEHOLDER_LABELS
                and _last_awf_resolvable_reason_is_placeholder(stdout, verdict=latest_verdict)
            )
            if not final_is_resolvable_placeholder:
                for parsed in reversed(awf_verdicts[:-1]):
                    if parsed.verdict == latest_verdict and parsed.reason is not None:
                        return parsed
            # Template-placeholder echoes (reason sanitized to None) fail closed for
            # every resolvable verdict. Any resolvable placeholder may still fall back
            # to an earlier reasoned hard block (#676 / #822 PRRT_kwDOSJAM6s6ZlxgI),
            # but never to the same label as the placeholder (that would still
            # resolve/defer). FALSE POSITIVE / DEFER placeholders never resolve alone.
            if final_is_resolvable_placeholder:
                for parsed in reversed(awf_verdicts[:-1]):
                    if (
                        parsed.verdict in {"needs_human", "defer"}
                        and parsed.verdict != latest_verdict
                        and parsed.reason is not None
                    ):
                        return parsed
                bare_blocking = _select_bare_verdict(
                    bare_verdicts,
                    # Exclude same-label DEFER so a DEFER placeholder cannot
                    # reuse an earlier reasoned DEFER (fail-closed same-label).
                    priorities=(
                        ("needs_human",) if latest_verdict == "defer" else ("needs_human", "defer")
                    ),
                )
                if bare_blocking is not None:
                    return bare_blocking
                return _fail_closed_resolvable_placeholder_if_needed(stdout, latest)
            if latest_verdict in {"defer", "needs_human"}:
                return latest
            for parsed in reversed(awf_verdicts[:-1]):
                if parsed.verdict in {"needs_human", "defer"} and parsed.reason is not None:
                    return parsed
            bare_blocking = _select_bare_verdict(
                bare_verdicts,
                priorities=("needs_human", "defer"),
            )
            if bare_blocking is not None:
                return bare_blocking
            return latest
        return latest
    if _stdout_mentions_awf_verdict(stdout):
        return VerdictResult(verdict="needs_human", reason="garbled_verdict_marker")
    return VerdictResult(verdict="needs_human", reason="unrecognized_or_markerless_verdict")


def _stdout_mentions_awf_verdict(stdout: str) -> bool:
    for stripped in _iter_non_fenced_verdict_lines(stdout):
        if _AWF_VERDICT_MARKER.search(stripped):
            return True
    return False


_RESOLVABLE_PLACEHOLDER_LABELS = {
    "fix_committed": "fixed",
    "false_positive": "false positive",
    "defer": "defer",
}


def _last_awf_resolvable_reason_is_placeholder(stdout: str, *, verdict: Verdict) -> bool:
    """Return whether the final AWF line for ``verdict`` is a template-placeholder echo."""
    wanted = _RESOLVABLE_PLACEHOLDER_LABELS.get(verdict)
    if wanted is None:
        return False
    last_reason: str | None = None
    found = False
    for stripped in _iter_non_fenced_verdict_lines(stdout):
        for verdict_line in _verdict_line_candidates(stripped):
            for segment in _awf_verdict_segments(verdict_line):
                awf_match = _AWF_VERDICT.fullmatch(segment)
                if awf_match is None:
                    continue
                normalized_label = re.sub(r"[\s_]+", " ", awf_match.group("label").strip().lower())
                if normalized_label != wanted:
                    continue
                found = True
                last_reason = awf_match.group("reason")
    if not found:
        return False
    cleaned = (last_reason or "").strip()
    if not cleaned:
        return False
    return _VERDICT_REASON_TEMPLATE_PLACEHOLDER.search(cleaned) is not None


def _fail_closed_resolvable_placeholder_if_needed(
    stdout: str,
    result: VerdictResult,
) -> VerdictResult:
    """Convert a standalone resolvable placeholder echo into fail-closed needs_human.

    Prompt-template echoes such as ``AWF-VERDICT: FALSE POSITIVE: <one-sentence
    justification>`` sanitize to a reasonless resolvable verdict; those must not
    clear review items. Explicit reasonless non-placeholder verdicts still pass.
    """
    if result.verdict not in _RESOLVABLE_PLACEHOLDER_LABELS or result.reason is not None:
        return result
    if not _last_awf_resolvable_reason_is_placeholder(stdout, verdict=result.verdict):
        return result
    reason = (
        "fixed_placeholder_echo"
        if result.verdict == "fix_committed"
        else "verdict_placeholder_echo"
    )
    return VerdictResult(verdict="needs_human", reason=reason)


def _select_bare_verdict(
    verdicts: Sequence[VerdictResult],
    *,
    priorities: Sequence[Verdict],
) -> VerdictResult | None:
    for verdict in priorities:
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
    return None


def _strip_markdown_list_prefix(stripped: str) -> str:
    """Remove a single leading Markdown list marker, if present."""
    return _MARKDOWN_LIST_PREFIX.sub("", stripped, count=1)


def _strip_markdown_task_list_checkbox(stripped: str) -> str:
    """Remove a leading GFM task-list checkbox (``[ ]`` / ``[x]`` / ``[X]``)."""
    return _MARKDOWN_TASK_LIST_CHECKBOX.sub("", stripped, count=1)


def _strip_markdown_blockquote_prefix(stripped: str) -> str:
    """Remove leading Markdown blockquote markers (``>``, ``>>``), if present."""
    return _MARKDOWN_BLOCKQUOTE_PREFIX.sub("", stripped, count=1)


def _verdict_line_candidates(stripped: str) -> Iterable[str]:
    """Yield line forms that may carry a canonical ``AWF-VERDICT:`` match.

    Do not yield Markdown-list-, task-list-, or blockquote-stripped variants
    here. Those prefixes are stripped only in ``_awf_verdict_segment_is_attempt``
    so garbled finals fail closed; treating ``- AWF-VERDICT: …``,
    ``- [ ] AWF-VERDICT: …``, or ``> AWF-VERDICT: …`` as a successful match would
    let quoted/option-list lines override an earlier hard block.
    """
    yield stripped
    code_match = _CODE_FORMATTED_VERDICT_LINE.fullmatch(stripped)
    if code_match is None:
        return
    inner = code_match.group("line").strip()
    if inner:
        yield inner


def _awf_verdict_segments(verdict_line: str) -> list[str]:
    """Split a candidate so each ``AWF-VERDICT:`` occurrence is its own unit.

    Same-line trailing markers must not be absorbed into an earlier reason
    group; each marker is authoritative in order (final marker wins / fails
    closed), matching multiline parsing.

    When the first marker is preceded by non-whitespace prose, keep the whole
    line as one unit. Splitting would drop that prose and make later quoted
    markers look like leading attempts (mid-prose option lists must not
    override an earlier real verdict).

    Subsequent markers embedded in quoted reason prose (for example a
    ``NEEDS_HUMAN`` reason that cites the marker grammar inside ASCII/curly
    quotes or Markdown backticks) are not split into new attempts — only
    unquoted trailing markers are.
    """
    matches = list(_AWF_VERDICT_MARKER.finditer(verdict_line))
    if len(matches) <= 1:
        return [verdict_line]
    if verdict_line[: matches[0].start()].strip():
        return [verdict_line]
    split_starts = [matches[0].start()]
    for match in matches[1:]:
        if _awf_verdict_marker_embedded_in_reason_prose(verdict_line, match.start()):
            continue
        split_starts.append(match.start())
    if len(split_starts) == 1:
        return [verdict_line]
    segments: list[str] = []
    for index, start in enumerate(split_starts):
        end = split_starts[index + 1] if index + 1 < len(split_starts) else len(verdict_line)
        segment = verdict_line[start:end].strip()
        if segment:
            segments.append(segment)
    return segments or [verdict_line]


def _awf_verdict_marker_embedded_in_reason_prose(verdict_line: str, match_start: int) -> bool:
    """Return whether a same-line marker is quoted inside earlier reason text.

    Distinguishes prose citations of the marker grammar from real trailing
    verdict attempts so a blocking reason cannot be overridden by a quote.
    Tracks ASCII ``"``/``'`` and Markdown backtick *runs* plus curly ``“``/``”``
    and ``‘``/``’`` open/close independently so mid-quote citations (code spans,
    typographic prompt echoes) stay embedded, while a closing delimiter
    immediately before a real trailing marker does not.

    Backtick runs follow CommonMark code-span rules: a consecutive run of N
    backticks opens a span, and only a later run of the same length closes it.
    Toggling once per backtick character would close a double-backtick span at
    the opener and treat an embedded marker as a real trailing verdict.

    ASCII ``'`` is only a quote delimiter when it is not a word-internal
    apostrophe (``don't``, ``user's``) or a leading elision (``'em``, ``'til``,
    ``'cause``); ASCII ``"`` is only a delimiter at plausible quote boundaries
    (not inch/unit marks like ``5"``). Naive toggle would absorb a later
    unquoted same-line marker into an earlier resolvable verdict.
    """
    if match_start <= 0:
        return False
    inside_ascii_double = False
    inside_ascii_single = False
    inside_backtick = False
    backtick_open_len = 0
    inside_curly_double = False
    inside_curly_single = False
    skip_until = 0
    prefix = verdict_line[:match_start]
    for index, char in enumerate(prefix):
        if index < skip_until:
            continue
        if char == '"':
            if _ascii_double_quote_is_delimiter(verdict_line, index, inside_ascii_double):
                inside_ascii_double = not inside_ascii_double
        elif char == "'":
            if _ascii_single_quote_is_delimiter(verdict_line, index, inside_ascii_single):
                inside_ascii_single = not inside_ascii_single
        elif char == "`":
            run_len = 1
            while index + run_len < match_start and prefix[index + run_len] == "`":
                run_len += 1
            if inside_backtick:
                if run_len == backtick_open_len:
                    inside_backtick = False
                    backtick_open_len = 0
            else:
                inside_backtick = True
                backtick_open_len = run_len
            skip_until = index + run_len
        elif char == "“":
            inside_curly_double = True
        elif char == "”":
            inside_curly_double = False
        elif char == "‘":
            inside_curly_single = True
        elif char == "’":
            inside_curly_single = False
    return (
        inside_ascii_double
        or inside_ascii_single
        or inside_backtick
        or inside_curly_double
        or inside_curly_single
    )


def _ascii_double_quote_is_delimiter(
    verdict_line: str, index: int, inside_ascii_double: bool
) -> bool:
    """Return whether ``verdict_line[index]`` is an ASCII double-quote delimiter.

    Inch/unit marks after an alphanumeric (``5"``, ``12"x``) must not open a
    quote span; naive toggle would absorb a later unquoted same-line marker.
    Outside a quote, only open when the previous character is not alphanumeric
    so delimiters sit at plausible quote boundaries. Inside a quote, every
    ``"`` closes (including when jammed against a following token).
    """
    if inside_ascii_double:
        return True
    prev = verdict_line[index - 1] if index > 0 else ""
    return not prev.isalnum()


def _ascii_single_quote_is_delimiter(
    verdict_line: str, index: int, inside_ascii_single: bool
) -> bool:
    """Return whether ``verdict_line[index]`` is an ASCII single-quote delimiter.

    Word-internal apostrophes before a lowercase continuation (``don't``,
    ``user's``, ``it's``) never toggle quote state. Leading elisions after a
    non-alphanumeric boundary (``'em``, ``'til``, ``'cause``) also never toggle
    — otherwise an open span never closes and a later unquoted same-line marker
    is absorbed, or an already-open citation closes early. Outside a quote, only
    open when the previous character is not alphanumeric so plural possessives
    like ``users'`` do not start a span. Inside a quote, a closer jammed against
    a following lowercase token (``'strict'by``) still closes unless the letters
    after the apostrophe are a short English contraction suffix (``n't``,
    ``'s``, ``'re``, …), so trailing unquoted markers are not absorbed.
    """
    prev = verdict_line[index - 1] if index > 0 else ""
    nxt = verdict_line[index + 1] if index + 1 < len(verdict_line) else ""
    if prev.isalnum() and nxt.islower():
        return inside_ascii_single and not _ascii_apostrophe_is_contraction_suffix(
            verdict_line, index
        )
    if _ascii_apostrophe_is_leading_elision(verdict_line, index):
        return False
    return inside_ascii_single or not prev.isalnum()


# Short alphabetic tails after an ASCII apostrophe that mark English contractions
# / clitics (n't, 's, 're, 've, 'll, 'd, 'm). Longer jammed tokens ('strict'by)
# are closers, not apostrophes.
_ASCII_APOSTROPHE_CONTRACTION_SUFFIXES = frozenset({"t", "s", "re", "ve", "ll", "d", "m"})

# Colloquial leading elisions after a word boundary ('em, 'til, 'cause). These
# must not open or close ASCII single-quote spans in verdict reason prose.
_ASCII_LEADING_ELISION_SUFFIXES = frozenset(
    {
        "em",
        "tis",
        "twas",
        "twere",
        "twill",
        "twould",
        "twixt",
        "til",
        "till",
        "cause",
        "bout",
        "round",
        "nother",
        "cept",
        "gainst",
        "fore",
        "stead",
        "cross",
        "neath",
        "ere",
    }
)


def _ascii_apostrophe_is_contraction_suffix(verdict_line: str, index: int) -> bool:
    """Return whether letters after ``verdict_line[index]`` look like a contraction."""
    end = index + 1
    while end < len(verdict_line) and verdict_line[end].isalpha():
        end += 1
    suffix = verdict_line[index + 1 : end].lower()
    return suffix in _ASCII_APOSTROPHE_CONTRACTION_SUFFIXES


def _ascii_apostrophe_is_leading_elision(verdict_line: str, index: int) -> bool:
    """Return whether ``verdict_line[index]`` starts a leading elision (``'em``).

    Requires a non-alphanumeric previous character and an alphabetic run that
    matches a known elision suffix. Longer tokens like ``'emergency`` are not
    elisions and may still open a real quote span.
    """
    prev = verdict_line[index - 1] if index > 0 else ""
    if prev.isalnum():
        return False
    end = index + 1
    while end < len(verdict_line) and verdict_line[end].isalpha():
        end += 1
    suffix = verdict_line[index + 1 : end].lower()
    return suffix in _ASCII_LEADING_ELISION_SUFFIXES


def _strip_markdown_attempt_prefixes(segment: str) -> str:
    """Strip leading Markdown list/blockquote/task-list markers until none apply.

    Agents may nest them in either order (``- > AWF-VERDICT: …``,
    ``> - > AWF-VERDICT: …``, ``- [ ] AWF-VERDICT: …``). One-pass
    blockquote-then-list leaves a residual ``>`` after ``- >``, and plain list
    strip leaves ``[ ]`` after a GFM task-list item, so the marker no longer
    looks like a leading attempt.
    """
    normalized = segment.lstrip()
    while True:
        stripped = _strip_markdown_task_list_checkbox(
            _strip_markdown_list_prefix(_strip_markdown_blockquote_prefix(normalized))
        )
        if stripped == normalized:
            return normalized
        normalized = stripped


def _awf_verdict_segment_is_attempt(segment: str) -> bool:
    """Return whether ``segment`` is a leading/split ``AWF-VERDICT:`` attempt.

    Mid-prose quotes of the marker grammar (prompt echoes in chat) are not
    attempts; only segments that begin with the marker count toward the
    final-marker fail-closed gate. Leading Markdown blockquote, list, and
    task-list checkbox markers are stripped first so ``> AWF-VERDICT: …`` /
    ``- AWF-VERDICT: SHIPPED: …`` / ``- [ ] AWF-VERDICT: …`` still count as
    garbled finals.
    """
    return _AWF_VERDICT_MARKER.match(_strip_markdown_attempt_prefixes(segment)) is not None


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
    """Redact, bound, and normalize a verdict reason, dropping unusable content."""
    if reason is None:
        return None
    cleaned = redact_secrets(reason).strip()
    if not cleaned:
        return None
    if _VERDICT_REASON_REDACTION_ONLY.fullmatch(cleaned):
        return None
    if _VERDICT_REASON_TEMPLATE_PLACEHOLDER.search(cleaned):
        return None
    if len(cleaned) > _MAX_VERDICT_REASON_LENGTH:
        return f"{cleaned[: _MAX_VERDICT_REASON_LENGTH - 1].rstrip()}…"
    return cleaned


def _needs_human_reason_missing(result: VerdictResult) -> bool:
    """Return whether a blocking needs-human result lacks a usable reason."""
    return result.verdict == "needs_human" and _sanitize_verdict_reason(result.reason) is None


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


def _sync_needs_human_reason(
    state: MonitorState,
    item_id: str,
    result: VerdictResult,
) -> None:
    """Persist or clear the agent's blocking-verdict reason for a review item."""
    reason_key = _needs_human_reason_state_key(item_id)
    if result.verdict in {"defer", "needs_human"} and (
        reason := _sanitize_verdict_reason(result.reason)
    ):
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
    state.threads_addressed_ids.pop(_salvaged_fix_head_state_key(item_id), None)
    state.threads_addressed_ids.pop(_salvaged_fix_body_hash_state_key(item_id), None)


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
    """Normalize a persisted monitor verdict string into the typed ``Verdict`` enum."""
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


def _with_ci_failures(
    status: PRStatus,
    failures: tuple[CheckFailure, ...] | CheckFailureLogResult,
) -> PRStatus:
    """Immutable-replace ci_failures on a ``PRStatus`` (frozen dataclass)."""
    # Import dataclasses.replace locally to keep the top-level imports tight.
    from dataclasses import replace

    if isinstance(failures, CheckFailureLogResult):
        return replace(
            status,
            ci_failures=failures.failures,
            ci_runs_in_progress=failures.runs_in_progress,
        )
    return replace(status, ci_failures=failures, ci_runs_in_progress=False)


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


def _notify_human_blocker_items(
    status: PRStatus, state: MonitorState
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Return every feedback item that currently requires human attention.

    Effective changes-requested reviews are merge blockers independently of
    their triage verdict and can have an empty body, so they may not appear in
    ``unresolved_review_comments`` at all. Include them in the rendered
    notification and its digest. When a triaged review is also an effective
    blocker, retain its single item, agent verdict, and reason while marking
    its independent merge-blocking state for rendering.

    Advisory bot review-level deferrals do not block merge and belong only in
    the terminal defer artifact, not a human-attention notification. A bot
    review promoted to ``changes_requested`` remains a notification blocker.
    """
    bot_items, human_items = _collect_defer_items(status, state)
    items_by_id = {str(item["id"]): item for item in bot_items + human_items}
    for review in status.blocking_reviews:
        if existing_item := items_by_id.get(review.comment_id):
            existing_item["is_merge_blocking"] = True
            continue
        is_bot = _is_bot_author(review.author)
        bucket = bot_items if is_bot else human_items
        item: dict[str, object] = {
            "kind": "review",
            "id": review.comment_id,
            "author": review.author,
            "is_bot": is_bot,
            "path": None,
            "line": None,
            "url": review.url,
            "body": review.body_excerpt,
            "verdict": "changes_requested",
            "agent_verdict_reason": None,
            "is_merge_blocking": True,
        }
        bucket.append(item)
        items_by_id[review.comment_id] = item
    bot_items = [
        item
        for item in bot_items
        if (
            item["kind"] != "review"
            or item.get("verdict") != "defer"
            or item.get("is_merge_blocking") is True
        )
    ]
    return bot_items, human_items


def _notify_human_reason(
    status: PRStatus,
    state: MonitorState,
    *,
    blocker_items: tuple[list[dict[str, object]], list[dict[str, object]]] | None = None,
) -> str | None:
    """Summarize the highest-priority unresolved human blocker."""
    if reason := _first_needs_human_reason(status, state):
        return reason
    if status.blocking_reviews:
        return "a merge-blocking changes-requested review remains unresolved"
    bot_items, human_items = blocker_items or _notify_human_blocker_items(status, state)
    # A current escalation must outrank any outdated-thread diagnosis, even if
    # it is bot-authored and has no agent-provided reason. Keep the detailed
    # diagnosis for outdated-only cases ahead of the generic fallback below.
    current_item_ids = {
        *(thread.thread_id for thread in status.unresolved_inline_threads),
        *(comment.comment_id for comment in status.unresolved_review_comments),
    }
    if any(
        item.get("verdict") == "needs_human" and item.get("id") in current_item_ids
        for item in human_items
    ):
        return "human review feedback needs human input and remains unresolved"
    if any(
        item.get("verdict") == "needs_human" and item.get("id") in current_item_ids
        for item in bot_items
    ):
        return "review feedback needs human input and remains unresolved on GitHub"
    if reason := _first_needs_human_reason(status, state, include_outdated=True):
        return reason
    for item in human_items:
        awf_blocker_reason = item.get("awf_blocker_reason")
        if isinstance(awf_blocker_reason, str) and awf_blocker_reason:
            return awf_blocker_reason
    if any(item.get("verdict") == "needs_human" for item in human_items):
        return "human review feedback needs human input and remains unresolved"
    for item in bot_items:
        awf_blocker_reason = item.get("awf_blocker_reason")
        if isinstance(awf_blocker_reason, str) and awf_blocker_reason:
            return awf_blocker_reason
    if human_items:
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


def _first_needs_human_reason(
    status: PRStatus, state: MonitorState, *, include_outdated: bool = False
) -> str | None:
    """Return a stored reason, prioritizing current feedback over outdated threads."""
    item_ids = [t.thread_id for t in status.unresolved_inline_threads] + [
        c.comment_id for c in status.unresolved_review_comments
    ]
    if include_outdated:
        item_ids += [t.thread_id for t in status.outdated_unresolved_inline_threads]
    for item_id in item_ids:
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

    disposition = github_error_disposition(operation=exc.operation, stderr=exc.stderr)
    return disposition in {
        GitHubErrorDisposition.TRANSIENT,
        GitHubErrorDisposition.AMBIGUOUS_AUTH,
    }


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
    has_auth_transient_evidence = any(
        marker in text for marker in GITHUB_AUTH_TRANSIENT_EVIDENCE_MARKERS
    )
    if "bad credentials" in text and not has_auth_transient_evidence:
        return False
    if any(marker in text for marker in _AMBIGUOUS_GITHUB_AUTH_TRANSIENT_MARKERS):
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


def _notification_key(
    *, head_sha: str, blocker_reason: str | None, items_digest: str | None = None
) -> str:
    """Build a stable notification deduplication key."""
    reason = blocker_reason or "ready-to-merge"
    key = f"__awf_notify__:{head_sha}:{reason}"
    return f"{key}:{items_digest}" if items_digest else key


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
