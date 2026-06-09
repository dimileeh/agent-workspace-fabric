"""Quality gates, coverage validation, pre-commit checking, and conformance logic."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from awf.control.executor import WorkspaceExecutor
    from awf.profiles.models import WorkspaceProfile

from awf.common.audit import redact_audit_text, redact_audit_value
from awf.common.commands import CommandResult
from awf.common.logging import get_logger
from awf.control.executor.constants import (
    _AWF_RUFF_CHECK_HOOK_ID,
    _AWF_RUFF_FORMAT_CHECK_HOOK_ID,
    _PRE_COMMIT_DETERMINISTIC_REPAIR_HOOK_IDS,
    _PRE_COMMIT_FIXING_PATH_PATTERN,
    _PRE_COMMIT_HOOK_ID_PATTERN,
    _PRE_COMMIT_WOULD_REFORMAT_PATTERN,
    _RUFF_DIAGNOSTIC_PATH_PATTERN,
    _RUFF_DIAGNOSTIC_PATTERN,
    POST_AGENT_COMMIT_FAILED_REASON_CODE,
    POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE,
    POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE,
)
from awf.control.executor.types import (
    _CoverageEvidenceResult,
    _PlanningRunFailure,
)
from awf.db.enums import FailureReason
from awf.runtime.validation import ValidationCoverageResult

_log = get_logger(__name__)


@dataclass(frozen=True)
class _PostAgentCommitClassification:
    """Structured result of classifying a post-agent ``git commit`` failure.

    Carries the reason code, failed hook names, repair file lists, and
    the repair strategy so the caller can decide whether to attempt
    automatic repair, prompt the agent, or fail the workspace.
    """

    reason_code: str
    failed_hooks: tuple[str, ...]
    format_repair_files: tuple[str, ...]
    summary: str
    deterministic_hooks: tuple[str, ...] = ()
    semantic_hooks: tuple[str, ...] = ()
    normalizer_repair_files: tuple[str, ...] = ()
    autofix_repair_files: tuple[str, ...] = ()
    repair_strategy: str = "none"


class _PostAgentCommitStepError(RuntimeError):
    """Raised when post-agent ``git add`` / ``git commit`` exits non-zero.

    Carries the structured classification so the outer exception handler
    can emit a specific reason code instead of falling back to
    ``INFRASTRUCTURE_FAILURE``. The ``stage`` field distinguishes
    ``git add`` failures from ``git commit`` failures; the
    ``classification`` field is ``None`` for ``git add`` failures (we
    only classify commit output). ``reason_code_override`` lets the
    format-repair path surface a distinct code (e.g. when
    ``ruff format`` itself crashed) without mutating the parsed
    classification. ``failure_reason_override`` lets policy gates retain
    their terminal failure class when they run inside the repair helper.
    """

    def __init__(
        self: Any,
        *,
        stage: str,
        result: CommandResult,
        classification: _PostAgentCommitClassification | None,
        format_repair_attempted: bool = False,
        precommit_repair_attempted: bool = False,
        repair_strategy: str | None = None,
        reason_code_override: str | None = None,
        failure_reason_override: FailureReason | None = None,
    ) -> None:
        """Initialise the error with stage, result, and optional overrides."""
        self.stage = stage
        self.result = result
        self.classification = classification
        self.format_repair_attempted = format_repair_attempted
        self.precommit_repair_attempted = precommit_repair_attempted
        self.repair_strategy = repair_strategy
        self.reason_code_override = reason_code_override
        self.failure_reason_override = failure_reason_override
        output = (result.stderr or result.stdout or "").strip()
        super().__init__(f"post-agent {stage} failed (exit={result.returncode}): {output}")


def _ruff_check_autofix_repair_files(output: str) -> tuple[str, ...]:
    """Return Ruff paths only when every observed diagnostic is auto-fixable."""
    paths: list[str] = []
    pending_fixable_path = False
    saw_diagnostic = False
    saw_unfixable = False
    for line in output.splitlines():
        diagnostic = _RUFF_DIAGNOSTIC_PATTERN.match(line)
        if diagnostic is not None:
            saw_diagnostic = True
            pending_fixable_path = diagnostic.group("fixable") is not None
            if not pending_fixable_path:
                saw_unfixable = True
            continue
        if pending_fixable_path:
            path = _RUFF_DIAGNOSTIC_PATH_PATTERN.match(line)
            if path is not None:
                paths.append(path.group("path").strip())
                pending_fixable_path = False
    if not saw_diagnostic or saw_unfixable:
        return ()
    return tuple(dict.fromkeys(paths))


def _classify_post_agent_commit_failure(
    result: CommandResult,
) -> _PostAgentCommitClassification:
    """Classify why post-agent ``git commit`` exited non-zero.

    Inspects the combined stdout/stderr for pre-commit hook names,
    ruff-format reformat hints, and the generic failure case. Returns
    a :class:`_PostAgentCommitClassification` with the appropriate
    reason code, hook lists, repair file lists, and repair strategy.
    """
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    combined = f"{stdout}\n{stderr}"

    failed_hooks = tuple(
        dict.fromkeys(
            match.group("hook_id") for match in _PRE_COMMIT_HOOK_ID_PATTERN.finditer(combined)
        )
    )
    format_repair_files = tuple(
        match.group("path").strip()
        for match in _PRE_COMMIT_WOULD_REFORMAT_PATTERN.finditer(combined)
    )
    normalizer_repair_files = tuple(
        dict.fromkeys(
            match.group("path").strip()
            for match in _PRE_COMMIT_FIXING_PATH_PATTERN.finditer(combined)
        )
    )
    autofix_repair_files = (
        _ruff_check_autofix_repair_files(combined)
        if _AWF_RUFF_CHECK_HOOK_ID in failed_hooks
        else ()
    )
    deterministic_hooks = tuple(
        hook for hook in failed_hooks if hook in _PRE_COMMIT_DETERMINISTIC_REPAIR_HOOK_IDS
    )
    semantic_hooks = tuple(
        hook for hook in failed_hooks if hook not in _PRE_COMMIT_DETERMINISTIC_REPAIR_HOOK_IDS
    )
    repair_strategy = (
        "agent" if semantic_hooks else "deterministic" if deterministic_hooks else "none"
    )

    raw_summary = stderr.strip() or stdout.strip()
    summary = raw_summary[:2000]

    if failed_hooks:
        if set(failed_hooks) == {_AWF_RUFF_FORMAT_CHECK_HOOK_ID} and format_repair_files:
            return _PostAgentCommitClassification(
                reason_code=POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE,
                failed_hooks=failed_hooks,
                format_repair_files=format_repair_files,
                summary=summary or "ruff format --check reported files would be reformatted",
                deterministic_hooks=deterministic_hooks,
                semantic_hooks=semantic_hooks,
                normalizer_repair_files=normalizer_repair_files,
                autofix_repair_files=autofix_repair_files,
                repair_strategy=repair_strategy,
            )
        return _PostAgentCommitClassification(
            reason_code=POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE,
            failed_hooks=failed_hooks,
            format_repair_files=format_repair_files,
            summary=summary or "pre-commit hooks rejected the post-agent commit",
            deterministic_hooks=deterministic_hooks,
            semantic_hooks=semantic_hooks,
            normalizer_repair_files=normalizer_repair_files,
            autofix_repair_files=autofix_repair_files,
            repair_strategy=repair_strategy,
        )

    return _PostAgentCommitClassification(
        reason_code=POST_AGENT_COMMIT_FAILED_REASON_CODE,
        failed_hooks=(),
        format_repair_files=(),
        summary=summary or "git commit exited non-zero with no output",
    )


_NOTHING_TO_COMMIT_PATTERN = re.compile(
    r"nothing to commit|working tree clean",
    re.IGNORECASE,
)


def _is_nothing_to_commit(result: CommandResult) -> bool:
    """Return True when ``git commit`` failed because there was nothing to commit.

    Detects the benign ``nothing to commit, working tree clean`` output
    (or variants) that git emits when the index matches HEAD. This is
    NOT a real git error — it means the agent (or a prior repair step)
    already committed the work, so the post-agent commit should fall
    through to the rev-list check instead of failing the workspace.

    Returns False if the command succeeded (no failure to explain) or
    if a pre-commit hook failure is present in the output, since either
    case means the "nothing to commit" substring is not the real signal.
    """
    if result.ok:
        return False
    combined = f"{result.stdout or ''}\n{result.stderr or ''}"
    if _PRE_COMMIT_HOOK_ID_PATTERN.search(combined):
        return False
    return bool(_NOTHING_TO_COMMIT_PATTERN.search(combined))


def _build_post_agent_precommit_repair_prompt(
    *,
    classification: _PostAgentCommitClassification,
    staged_paths: Sequence[str],
) -> str:
    """Build the repair prompt sent to the agent when semantic pre-commit hooks fail.

    Includes failed hook names, semantic hook list, staged path preview,
    normalizer-rewritten paths, formatter-reported paths, and a redacted
    tail of the pre-commit output so the agent can fix the failures.
    """
    failed_hooks = ", ".join(classification.failed_hooks) or "unknown"
    semantic_hooks = ", ".join(classification.semantic_hooks) or "unknown"
    staged_preview = "\n".join(f"- {path}" for path in staged_paths[:80])
    if len(staged_paths) > 80:
        staged_preview += f"\n- ... and {len(staged_paths) - 80} more"
    normalizer_preview = "\n".join(
        f"- {path}" for path in classification.normalizer_repair_files[:40]
    )
    if len(classification.normalizer_repair_files) > 40:
        normalizer_preview += f"\n- ... and {len(classification.normalizer_repair_files) - 40} more"
    formatter_preview = "\n".join(f"- {path}" for path in classification.format_repair_files[:40])
    if len(classification.format_repair_files) > 40:
        formatter_preview += f"\n- ... and {len(classification.format_repair_files) - 40} more"
    summary = redact_audit_text(classification.summary, limit=3000)
    return (
        "AWF post-agent pre-commit repair is required.\n\n"
        "The implementation work has returned, but `git commit` was rejected "
        "by semantic pre-commit hook failures. Fix only the hook failures below, "
        "then stop. Do not bypass pre-commit. Do not change coverage policy, "
        "dependency files, CI, or unrelated files. Remove temporary/debug files "
        "if they caused the hook failure.\n\n"
        f"Failed hooks: {failed_hooks}\n"
        f"Semantic hooks that require code/test repair: {semantic_hooks}\n\n"
        "Currently staged paths:\n"
        f"{staged_preview or '- <none>'}\n\n"
        "Normalizer-rewritten paths, if any:\n"
        f"{normalizer_preview or '- <none>'}\n\n"
        "Formatter-reported paths, if any:\n"
        f"{formatter_preview or '- <none>'}\n\n"
        "Pre-commit output tail:\n"
        f"{summary or '<no output>'}\n"
    )


def _post_validation_conformance_failure_text(failure: _PlanningRunFailure) -> str:
    """Format a human-readable summary of a post-validation conformance failure.

    Extracts the conformance details dict from the failure and renders
    the summary, report reason code, and remaining gap descriptions.
    """
    lines = [failure.message]
    details = failure.details or {}
    conformance = details.get("conformance")
    if isinstance(conformance, Mapping):
        summary = conformance.get("summary")
        if isinstance(summary, str) and summary.strip():
            lines.append(f"Summary: {summary.strip()}")
        report_reason = conformance.get("report_reason_code")
        if isinstance(report_reason, str) and report_reason.strip():
            lines.append(f"Report reason code: {report_reason.strip()}")
        gaps = conformance.get("gaps")
        if isinstance(gaps, list):
            gap_lines = [str(gap).strip() for gap in gaps if str(gap).strip()]
            if gap_lines:
                lines.append("Remaining conformance gaps:")
                lines.extend(f"- {gap}" for gap in gap_lines)
    return "\n".join(lines)


def _post_validation_conformance_agent_failure_message(exc: Any) -> str:
    """Build a short human-readable message for a conformance agent failure.

    Reads ``reason_code`` and ``result`` attributes from *exc* and
    returns a redacted, truncated message suitable for logging.
    """
    reason_code = getattr(exc, "reason_code", None) or "AGENT_CLI_FAILED"
    result = getattr(exc, "result", None)
    stderr = getattr(result, "stderr", "") if result else ""
    stdout = getattr(result, "stdout", "") if result else ""
    returncode = getattr(result, "returncode", None) if result else None
    output = stderr.strip() or stdout.strip() or "<no output>"
    safe_output = redact_audit_text(output, limit=1000)
    return (
        "post-validation conformance agent failed "
        f"({reason_code}, exit {returncode}): {safe_output}"
    )[:2000]


def _post_validation_conformance_agent_failure_details(
    exc: Any,
    *,
    validation_run_id: str,
) -> dict[str, Any]:
    """Build a structured details dict for a conformance agent failure.

    Returns a dict with a ``conformance`` sub-dict (phase, reason_code,
    returncode, redacted stdout/stderr) and the validation run id, plus
    any agent details carried on the exception.
    """
    reason_code = getattr(exc, "reason_code", None) or "AGENT_CLI_FAILED"
    result = getattr(exc, "result", None)
    stderr = getattr(result, "stderr", "") if result else ""
    stdout = getattr(result, "stdout", "") if result else ""
    returncode = getattr(result, "returncode", None) if result else None
    conformance: dict[str, Any] = {
        "phase": "post_validation",
        "reason_code": reason_code,
        "returncode": returncode,
    }
    stdout_redacted = redact_audit_text(stdout.strip(), limit=1000)
    stderr_redacted = redact_audit_text(stderr.strip(), limit=1000)
    if stdout_redacted:
        conformance["stdout"] = stdout_redacted
    if stderr_redacted:
        conformance["stderr"] = stderr_redacted
    details: dict[str, Any] = {
        "conformance": conformance,
        "validation_run_id": validation_run_id,
    }
    exc_details = getattr(exc, "details", None)
    if exc_details:
        details["agent"] = redact_audit_value(exc_details)
    return details


async def _run_baseline_coverage_preflight(
    executor: WorkspaceExecutor,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    profile: WorkspaceProfile,
) -> ValidationCoverageResult | None:
    """Run the baseline coverage preflight check before agent work begins.

    Skips when the profile sets ``baseline_coverage`` to ``"skip"`` or
    when no coverage command is configured. Returns ``None`` on skip;
    logs and returns the result even if below threshold.
    """
    coverage = profile.validation.coverage
    if profile.validation.strategy.baseline_coverage == "skip":
        _log.info(
            "executor.baseline_coverage_skipped_by_policy",
            workspace_id=workspace_id,
            reason_code="BASELINE_COVERAGE_SKIPPED_BY_POLICY",
        )
        return None
    if coverage.command is None:
        return None
    result = await executor._validation.run_profile_coverage(
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
        profile=profile,
        phase="baseline_coverage",
    )
    if result is not None and not result.ok:
        _log.info(
            "executor.baseline_coverage_below_policy",
            workspace_id=workspace_id,
            percent=result.percent,
            minimum_percent=result.minimum_percent,
            reason_code=result.reason_code,
        )
    return result


async def _run_final_coverage_gate(
    executor: WorkspaceExecutor,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    profile: WorkspaceProfile,
    validation_tier: int,
    workspace_head_sha: str | None,
) -> _CoverageEvidenceResult:
    """Run the final coverage quality gate after validation.

    Attempts to reuse prior coverage evidence when the profile allows it
    and the evidence is fresh; otherwise executes the coverage command
    and returns an :class:`_CoverageEvidenceResult` with the outcome.
    """
    from awf.control.executor.helpers import (
        _coverage_result_from_metadata,
        _validation_run_command_records,
    )
    from awf.db.repositories import ValidationRunRepository
    from awf.db.validation_runs import validation_run_coverage_payload
    from awf.runtime.validation_identity import (
        environment_identity_digest,
        resolved_profile_digest,
    )

    coverage = profile.validation.coverage
    if coverage.command is None:
        return _CoverageEvidenceResult(coverage=None)

    command_records = _validation_run_command_records(
        profile=profile,
        phase_names=("post_agent", "validate"),
        run_healthchecks=True,
    )
    strategy = profile.validation.strategy
    if strategy.reuse_evidence:
        async with executor._session_factory() as session:
            reusable = await ValidationRunRepository(session).find_reusable_coverage_evidence(
                workspace_id=workspace_id,
                tier=validation_tier,
                commands=command_records,
                workspace_head_sha=workspace_head_sha,
                resolved_profile_digest=resolved_profile_digest(profile),
                environment_identity_digest=environment_identity_digest(profile),
                max_age_seconds=strategy.freshness_max_age_seconds,
                now=datetime.now(UTC),
            )
        if reusable is not None:
            metadata = validation_run_coverage_payload(reusable)
            if metadata:
                return _CoverageEvidenceResult(
                    coverage=_coverage_result_from_metadata(metadata),
                    evidence_status="reused",
                    reason_code="VALIDATION_EVIDENCE_REUSED",
                    source_run_id=reusable.id,
                )

    result = await executor._validation.run_profile_coverage(
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
        profile=profile,
        phase="coverage",
        parallel_worker_cpu_limit=await executor._parallel_worker_cpu_limit_for_workspace(
            workspace_id,
            profile=profile,
        ),
    )
    return _CoverageEvidenceResult(
        coverage=result,
        evidence_status="executed" if result is not None else None,
        reason_code="VALIDATION_EVIDENCE_EXECUTED" if result is not None else None,
    )
