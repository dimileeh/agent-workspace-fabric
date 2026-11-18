"""Applied operator directives for CI-failure repair prompts (#1256).

When an ``AddressOperatorHint`` cycle completes with the hint applied and a
non-empty directive, AWF keeps a short newest-5 history in
``MonitorState.threads_addressed_ids``. Later ``ReportCiFailure`` runs quote
those records so a CI/review-bot failure cannot undo an operator decision that
already landed. A CI repair that ends ``NEEDS_HUMAN`` while those records were
in the prompt parks for a human under
``CI_REPAIR_OPERATOR_DECISION_CONFLICT`` and is not re-dispatched for the same
head and failing-check signature until a new guide arrives or the head /
failure set changes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from awf.common.prompt_evidence import UntrustedEvidence, render_untrusted_evidence
from awf.runtime.monitor_state_keys import (
    _applied_operator_directives_state_key,
    _ci_repair_operator_decision_conflict_key_prefix,
)
from awf.runtime.pr_monitor_runner.comment_verdict import AgentVerdictProtocolError
from awf.runtime.pr_monitor_runner.helpers_verdict import (
    _last_valid_verdict_record,
    _parse_verdict_result,
)
from awf.runtime.pr_monitor_runner.logging import _log

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import CheckFailure, MonitorState, PRStatus
    from awf.runtime.pr_monitor_actions import MonitorAction

APPLIED_OPERATOR_DIRECTIVES_CAP = 5
CI_REPAIR_OPERATOR_DECISION_CONFLICT = "CI_REPAIR_OPERATOR_DECISION_CONFLICT"
CI_REPAIR_APPLIED_OPERATOR_DIRECTIVES_EVENT = "monitor.ci_repair_applied_operator_directives"

_APPLIED_OPERATOR_DIRECTIVES_SECTION_LEAD = (
    "Applied operator decisions already on this PR (newest first). These "
    "operator decisions were already applied on this PR; they outrank a "
    "failing check. Do not undo them, do not re-add code they removed, and do "
    "not work around them. Where two conflict, the newer wins. If a check can "
    "only pass by contradicting one, change nothing for that check and end with "
    "`AWF-VERDICT: NEEDS_HUMAN: <check name> conflicts with operator decision "
    "<operation_id>: <one sentence>`.\n\n"
)


@dataclass(frozen=True, slots=True)
class AppliedOperatorDirective:
    """One applied operator directive kept for later CI-repair prompts."""

    directive: str
    operation_id: str
    applied_at: str
    head_sha: str


def load_applied_operator_directives(
    state: MonitorState,
) -> tuple[AppliedOperatorDirective, ...]:
    """Return newest-first applied directives, or empty when the key is absent."""
    raw = state.threads_addressed_ids.get(_applied_operator_directives_state_key())
    if not raw:
        return ()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, list):
        return ()
    records: list[AppliedOperatorDirective] = []
    for item in payload:
        record = _record_from_mapping(item)
        if record is not None:
            records.append(record)
    return tuple(records)


def append_applied_operator_directive(
    state: MonitorState,
    record: AppliedOperatorDirective,
) -> None:
    """Append ``record`` (newest first), keep at most five, clear conflict parks."""
    existing = list(load_applied_operator_directives(state))
    existing.insert(0, record)
    trimmed = existing[:APPLIED_OPERATOR_DIRECTIVES_CAP]
    state.mark_addressed(
        _applied_operator_directives_state_key(),
        json.dumps([_record_to_mapping(item) for item in trimmed], separators=(",", ":")),
    )
    clear_ci_repair_operator_decision_conflicts(state)


def record_applied_operator_directive_from_hint(
    state: MonitorState,
    *,
    hint: Any,
    head_sha: str | None,
    applied_text: str | None = None,
) -> None:
    """Record applied directive text when non-empty; otherwise no-op.

    ``applied_text`` is what this cycle actually applied. When omitted
    (``None``), falls back to ``hint.directive``. Callers that applied only a
    ruling and passed empty acted feedback must pass ``applied_text=""`` (or
    the ruling-only excerpt) so unexecuted guide instructions are not stored
    as applied decisions for later CI repair (PRRT_kwDOSJAM6s64xWll).
    """
    if applied_text is not None:
        directive = applied_text.strip()
    else:
        directive = (getattr(hint, "directive", None) or "").strip()
    if not directive:
        return
    operation_id = getattr(hint, "operation_id", None) or ""
    applied_at = datetime.now(UTC).isoformat()
    append_applied_operator_directive(
        state,
        AppliedOperatorDirective(
            directive=directive,
            operation_id=str(operation_id),
            applied_at=applied_at,
            head_sha=(head_sha or "").strip(),
        ),
    )


def render_applied_operator_directives_section(
    records: Sequence[AppliedOperatorDirective],
) -> str:
    """Render the fixed AWF lead plus enveloped records (newest first)."""
    if not records:
        return ""
    parts = [_APPLIED_OPERATOR_DIRECTIVES_SECTION_LEAD]
    for record in records:
        parts.append(
            render_untrusted_evidence(
                UntrustedEvidence(
                    source_kind="operator_decision",
                    source_name="AWF operator decision",
                    source_id=record.operation_id,
                    text=record.directive,
                    metadata=(
                        ("applied_at", record.applied_at),
                        ("head_sha", record.head_sha),
                    ),
                )
            )
        )
        parts.append("")
    return "\n".join(parts).rstrip() + "\n\n"


def log_ci_repair_applied_operator_directives_audit(
    records: Sequence[AppliedOperatorDirective],
    *,
    workspace_id: str,
    pr_number: int,
    head_sha: str,
) -> None:
    """Emit one structured audit event (count + operation ids + PR context)."""
    if not records:
        return
    _log.info(
        CI_REPAIR_APPLIED_OPERATOR_DIRECTIVES_EVENT,
        workspace_id=workspace_id,
        pr_number=pr_number,
        head_sha=head_sha,
        count=len(records),
        operation_ids=[record.operation_id for record in records],
    )


def _ci_repair_operator_decision_conflict_state_key(
    head_sha: str,
    failures: tuple[CheckFailure, ...],
) -> str:
    """Build the conflict park key for one PR head / failing-check signature."""
    from awf.runtime.pr_monitor import (
        _CI_TRANSIENT_RERUN_KEY_PREFIX,
        _ci_transient_rerun_state_key,
    )

    rerun_key = _ci_transient_rerun_state_key(head_sha, failures)
    return rerun_key.replace(
        _CI_TRANSIENT_RERUN_KEY_PREFIX,
        _ci_repair_operator_decision_conflict_key_prefix(),
        1,
    )


def mark_ci_repair_operator_decision_conflict(
    state: MonitorState,
    *,
    head_sha: str,
    failures: tuple[CheckFailure, ...],
    reason: str,
) -> None:
    """Park CI repair for this head/signature with the agent's NEEDS_HUMAN reason."""
    state.mark_addressed(
        _ci_repair_operator_decision_conflict_state_key(head_sha, failures),
        reason,
    )


def clear_ci_repair_operator_decision_conflicts(state: MonitorState) -> None:
    """Drop every CI-repair operator-decision conflict park (new guide arrived)."""
    prefix = _ci_repair_operator_decision_conflict_key_prefix()
    for key in [k for k in state.threads_addressed_ids if k.startswith(prefix)]:
        state.threads_addressed_ids.pop(key, None)


def ci_repair_operator_decision_conflict_reason(
    state: MonitorState,
    *,
    head_sha: str,
    failures: tuple[CheckFailure, ...],
) -> str | None:
    """Return the parked conflict reason for this head/signature, if any."""
    raw = state.threads_addressed_ids.get(
        _ci_repair_operator_decision_conflict_state_key(head_sha, failures)
    )
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def report_ci_failure_or_operator_conflict(
    status: PRStatus,
    state: MonitorState,
) -> MonitorAction:
    """Return NotifyHuman while a conflict park matches; else ReportCiFailure."""
    from awf.runtime.pr_monitor import NotifyHuman, ReportCiFailure

    reason = ci_repair_operator_decision_conflict_reason(
        state,
        head_sha=status.head_sha,
        failures=status.ci_failures,
    )
    if reason is not None:
        return NotifyHuman(message=reason)
    return ReportCiFailure(failures=status.ci_failures)


def needs_human_verdict_reason(stdout: str) -> str | None:
    """Return the NEEDS_HUMAN reason from agent stdout, or None."""
    try:
        result = _parse_verdict_result(stdout)
    except AgentVerdictProtocolError:
        fallback = _last_valid_verdict_record(stdout)
        if fallback is None:
            return None
        result = fallback
    if result.verdict != "needs_human":
        return None
    return result.reason


def _record_to_mapping(record: AppliedOperatorDirective) -> dict[str, str]:
    return {
        "directive": record.directive,
        "operation_id": record.operation_id,
        "applied_at": record.applied_at,
        "head_sha": record.head_sha,
    }


def _record_from_mapping(item: object) -> AppliedOperatorDirective | None:
    if not isinstance(item, Mapping):
        return None
    directive = item.get("directive")
    operation_id = item.get("operation_id")
    applied_at = item.get("applied_at")
    head_sha = item.get("head_sha")
    if not isinstance(directive, str) or not directive.strip():
        return None
    if not isinstance(operation_id, str):
        return None
    if not isinstance(applied_at, str) or not isinstance(head_sha, str):
        return None
    return AppliedOperatorDirective(
        directive=directive,
        operation_id=operation_id,
        applied_at=applied_at,
        head_sha=head_sha,
    )
