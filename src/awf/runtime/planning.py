"""Plan → execute → compare prompt and report helpers.

AWF owns this lifecycle instead of relying on vendor-specific interactive
"plan mode" affordances. The coding CLI is invoked non-interactively for three
bounded phases: create a plan artifact, implement it, then produce a structured
conformance report. The executor decides whether another implementation pass is
needed from that report.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from awf.common.compose_exec import DEFAULT_AGENT_WORKDIR
from awf.common.coordination import MAX_COORDINATION_WARNING_OVERLAPS
from awf.common.redaction import redact_secrets
from awf.runtime.git_porcelain import split_porcelain_rename_paths, unquote_porcelain_path
from awf.runtime.workspace_prompt_context import render_workspace_runtime_context_section

if TYPE_CHECKING:
    from awf.adapters.base import AgentRunError

PLAN_CONFORMANCE_UNSATISFIED = "PLAN_CONFORMANCE_UNSATISFIED"
PLAN_CONFORMANCE_REPORTED = "PLAN_CONFORMANCE_REPORTED"
CONFORMANCE_REQUIRES_AWF_VALIDATION = "CONFORMANCE_REQUIRES_AWF_VALIDATION"
AGENT_PLAN_PHASE_SCOPE_VIOLATION = "AGENT_PLAN_PHASE_SCOPE_VIOLATION"
AGENT_STALLED_IN_CONFORMANCE = "AGENT_STALLED_IN_CONFORMANCE"
AGENT_IDLE_TIMEOUT_REASON_CODE = "AGENT_IDLE_TIMEOUT"
AGENT_TIMEOUT_REASON_CODE = "AGENT_TIMEOUT"
MAX_CONFORMANCE_GAPS = 20
MAX_CONFORMANCE_TEXT_CHARS = 1000


class PlanConformanceStatus(StrEnum):
    """Structured conformance verdict emitted by the compare phase."""

    satisfied = "satisfied"
    needs_iteration = "needs_iteration"


@dataclass(frozen=True)
class PlanConformanceReport:
    """Parsed plan-conformance report."""

    status: PlanConformanceStatus
    summary: str
    gaps: tuple[str, ...]
    reason_code: str = PLAN_CONFORMANCE_REPORTED

    @property
    def satisfied(self) -> bool:
        return self.status == PlanConformanceStatus.satisfied


class ConformanceStallKind(StrEnum):
    """Why the conformance loop is judged to be stalled."""

    no_output = "no_output"
    repeated_output = "repeated_output"
    over_duration = "over_duration"


@dataclass(frozen=True)
class ConformanceStallPolicy:
    """Thresholds that decide whether conformance progress has stalled."""

    no_output_seconds: int = 600
    over_duration_seconds: int = 1800
    repeated_output_threshold: int = 3


@dataclass(frozen=True)
class ConformanceIterationRecord:
    """Per-iteration evidence consumed by ``classify_conformance_stall``."""

    iteration: int
    elapsed_seconds: float
    report_digest: str | None
    worktree_changed: bool
    stdout: str = ""
    stderr: str = ""
    error_reason_code: str | None = None


@dataclass(frozen=True)
class ConformanceStallEvidence:
    """Structured evidence for a detected conformance stall."""

    kind: ConformanceStallKind
    iteration_index: int
    elapsed_seconds: float
    no_output_seconds: float
    repeated_output_count: int
    last_report_digest: str | None
    plan_path: str
    report_path: str
    last_output_excerpt: str = ""


def render_workspace_path(template: str, *, workspace_id: str) -> Path:
    """Render a workspace-relative artifact path from profile config."""

    rendered = template.format(workspace_id=workspace_id)
    if not rendered.strip():
        raise ValueError("path template rendered an empty path")
    path = Path(rendered)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"path template must stay inside the workspace: {template!r}")
    return path


AGENT_WORKTREE_ROOT: Final[str] = DEFAULT_AGENT_WORKDIR
"""In-container worktree root the agent process starts in.

Bound to :data:`awf.common.compose_exec.DEFAULT_AGENT_WORKDIR`, the single
source of truth for ``build_tracked_compose_exec``'s default ``workdir`` — the
directory the coding CLI is launched in (``AgentAdapter.run`` relies on that
default rather than passing ``workdir`` explicitly). Sharing the constant keeps
the agent's start directory and the artifact anchor statically coupled, so
changing the compose workdir cannot silently desync agent-prompt paths from
where the CLI actually runs and regress #620. Agents routinely ``cd`` into a
task subdirectory mid-run, so a worktree-relative artifact path handed to the
agent verbatim would resolve under that subdir instead of the repo root."""


def agent_artifact_path(relative_path: Path) -> Path:
    """Anchor a worktree-relative artifact path at the in-container worktree root.

    ``render_workspace_path`` guarantees ``relative_path`` is relative, so the
    absolute base always wins cleanly. Handing the agent the anchored
    ``/workspace/...`` path keeps the plan/conformance artifact at the worktree
    root regardless of the agent's CWD, so it can never land nested under a task
    subdir (e.g. ``apps/console/docs/awf-plans/``) and trip the dirty-tree guard
    (#620). The control plane is Linux-only, so ``Path`` is ``PosixPath`` and
    ``.as_posix()`` yields the correct ``/workspace/...`` string."""
    return Path(AGENT_WORKTREE_ROOT) / relative_path


def render_coordination_warning_section(
    coordination_warnings: Sequence[object] = (),
) -> str:
    warnings: list[dict[str, Any]] = []
    for warning in coordination_warnings:
        if not isinstance(warning, Mapping):
            continue
        warnings.append(_normalized_coordination_warning(warning))
    if not warnings:
        return ""

    lines = [
        "### Coordination warnings",
        "",
        (
            "Each warning is advisory and does not block launch. Coordinate around "
            "the listed workspace ids and owned paths before editing overlapping files."
        ),
        (
            "If target-branch changes land in these paths first, AWF stale policy may "
            "require rebase/revalidation via `STALE_OVERLAP`."
        ),
        "",
    ]
    for warning in warnings:
        blocks_launch = "true" if warning["blocks_launch"] else "false"
        lines.append(
            f"- {warning['warning_code']} ({warning['severity']}; blocks_launch={blocks_launch}): "
            f"{warning['message']}"
        )
        if warning["workspace_ids"]:
            lines.append(f"  - Workspaces: {', '.join(warning['workspace_ids'])}")
        for overlap in warning["overlaps"]:
            lines.append(
                f"  - {overlap['workspace_id']}: {overlap['existing_path']} -> "
                f"{overlap['requested_path']}"
            )
        if warning["overlaps_truncated"]:
            lines.append(
                "  - Overlap list truncated: "
                f"showing {len(warning['overlaps'])} of {warning['overlap_count']} "
                "total overlaps."
            )
        context = warning["stale_policy_context"]
        trigger_type = context.get("trigger_type")
        stale_reason_code = context.get("stale_reason_code")
        if trigger_type or stale_reason_code:
            lines.append(
                f"  - Stale policy: {trigger_type or 'path_overlap'} / "
                f"{stale_reason_code or 'STALE_OVERLAP'}"
            )
    return "\n".join(lines) + "\n\n"


def render_task_tag_commit_guidance(task_tag: str | None) -> str:
    """Return a commit-message guidance section for the task tag, or empty.

    Steers the agent's *own* commits to carry the Jira issue key so they link to
    the issue. AWF-authored commits carry the tag deterministically; this is
    best-effort guidance for the agent's commits.
    """
    if not task_tag:
        return ""
    return (
        f"### Commit message tag\nPrefix every commit message with `{task_tag} ` "
        "so commits link to the Jira issue.\n\n"
    )


def build_agent_task_prompt(
    *,
    task_prompt: str,
    coordination_warnings: Sequence[Mapping[str, Any]] = (),
    workspace_runtime_context: str = "",
    task_tag: str | None = None,
) -> str:
    warning_section = render_coordination_warning_section(coordination_warnings)
    runtime_section = render_workspace_runtime_context_section(workspace_runtime_context)
    tag_section = render_task_tag_commit_guidance(task_tag)
    if not warning_section and not runtime_section and not tag_section:
        return task_prompt
    return f"{runtime_section}{warning_section}{tag_section}### Task\n{task_prompt}\n"


def build_planning_prompt(
    *,
    task_prompt: str,
    plan_path: Path,
    coordination_warnings: Sequence[Mapping[str, Any]] = (),
    workspace_runtime_context: str = "",
) -> str:
    warning_section = render_coordination_warning_section(coordination_warnings)
    runtime_section = render_workspace_runtime_context_section(workspace_runtime_context)
    return (
        "## Planning phase\n\n"
        "Create a concrete implementation plan for the task below.\n\n"
        "Create or update only the configured plan artifact "
        f"`{plan_path.as_posix()}` during planning. Create parent directories if needed.\n"
        "Do not modify implementation files during planning.\n"
        "Do not create, edit, delete, stage, or commit any other files. Files outside "
        "that one plan artifact are out of scope, including source, tests, docs, "
        "config, migrations, and lockfiles.\n"
        "Do not run implementation commands while planning, including apply_patch, "
        "pytest, ruff, mypy, npm, lint commands, format commands, build commands, "
        "git add, or git commit.\n"
        "After writing the plan, stop. Do not perform implementation work in this phase.\n\n"
        "The plan must include:\n"
        "- intended files/modules to touch;\n"
        "- tests to write first;\n"
        "- validation commands;\n"
        "- risks, assumptions, and explicit non-goals.\n\n"
        f"{runtime_section}"
        f"{warning_section}"
        f"### Task\n{task_prompt}\n"
    )


def build_planning_scope_retry_prompt(
    *,
    task_prompt: str,
    evidence: Mapping[str, Any],
) -> str:
    """Build a conservative retry prompt after planning wrote out-of-scope files."""

    required_paths = _evidence_strings(evidence.get("required_paths"))
    offending_paths = _evidence_strings(evidence.get("offending_paths"))
    required_lines = (
        "\n".join(f"- `{path}`" for path in required_paths)
        if required_paths
        else "- No prior required plan paths were captured."
    )
    offending_lines = (
        "\n".join(f"- `{path}`" for path in offending_paths)
        if offending_paths
        else "- No offending paths were captured."
    )
    return (
        "## Retry after planning scope violation\n\n"
        "Discard the premature implementation from the failed planning attempt. Start "
        "from the original task and rerun planning in a clean workspace.\n\n"
        "Rerun planning against the configured plan artifact named by this retry's "
        "planning-phase instructions. Treat the source required paths below as prior "
        "evidence from the failed workspace only; they are not authoritative for this "
        "fresh retry.\n\n"
        "Aside from creating or updating the configured plan artifact, do not edit "
        "source, tests, docs, config, migrations, lockfiles, or any other non-plan "
        "file during this retry planning phase. Do not run implementation commands "
        "such as apply_patch, pytest, ruff, mypy, npm, build commands, git add, or "
        "git commit during planning. These restrictions are phase-scoped: after AWF "
        "accepts the retry plan and moves to implementation, implement the original "
        "task normally within the requested scope.\n\n"
        "### Prior source required plan paths from the failed planning attempt\n"
        f"{required_lines}\n\n"
        "### Offending paths from the failed planning attempt\n"
        f"{offending_lines}\n\n"
        "The preserved branch/worktree is available only for explicit operator salvage; "
        "do not reuse it in this retry unless policy explicitly approves salvage.\n\n"
        f"### Original task\n{task_prompt}\n"
    )


def build_execution_prompt(
    *,
    task_prompt: str,
    plan_path: Path,
    iteration: int,
    gaps: tuple[str, ...],
    coordination_warnings: Sequence[Mapping[str, Any]] = (),
    workspace_runtime_context: str = "",
    task_tag: str | None = None,
) -> str:
    if iteration == 0:
        instruction = (
            "Implement the saved plan. Follow TDD: write or update failing tests first, "
            "then implement until the relevant checks pass."
        )
    else:
        gap_lines = "\n".join(f"- {gap}" for gap in gaps) or "- Re-check the saved plan."
        instruction = (
            f"Iteration {iteration}: close the remaining plan-conformance gaps below.\n"
            f"{gap_lines}\n"
            "Keep changes scoped to the saved plan and explain unavoidable deviations in code/docs."
        )

    warning_section = render_coordination_warning_section(coordination_warnings)
    runtime_section = render_workspace_runtime_context_section(workspace_runtime_context)
    tag_section = render_task_tag_commit_guidance(task_tag)
    return (
        "## Execution phase\n\n"
        f"Read `{plan_path.as_posix()}` and use it as the implementation contract.\n"
        f"{instruction}\n\n"
        "Do not switch branches, push, or open a PR. AWF owns branch and PR lifecycle.\n\n"
        f"{runtime_section}"
        f"{warning_section}"
        f"{tag_section}"
        f"### Task\n{task_prompt}\n"
    )


def build_conformance_prompt(
    *,
    task_prompt: str,
    plan_path: Path,
    report_path: Path,
    iteration: int,
    validation_evidence: str | None = None,
) -> str:
    validation_evidence_section = (
        f"### Validation evidence\n{validation_evidence.strip()}\n\n"
        if validation_evidence and validation_evidence.strip()
        else ""
    )
    return (
        "## Conformance phase\n\n"
        f"Compare the current workspace implementation against `{plan_path.as_posix()}`.\n"
        "Do not modify implementation files. Only write the JSON report requested below.\n"
        "Do not run validation commands or implementation commands during conformance. "
        "That includes pytest, ruff, mypy, coverage, npm, lint commands, build commands, "
        "git add, and git commit. Use existing validation evidence from the workspace "
        "logs, validation summaries, git diff, and artifacts instead. If validation "
        "evidence is missing, stale, or insufficient, report `needs_iteration` with "
        "a specific gap asking AWF to run the missing validation under the validation "
        "phase. Set `reason_code` to `CONFORMANCE_REQUIRES_AWF_VALIDATION` only when "
        "every remaining gap is missing, stale, or insufficient AWF-owned validation "
        "evidence. Do not use it for implementation, API, plan, or documentation gaps.\n\n"
        f"{validation_evidence_section}"
        f"Write a JSON object to `{report_path.as_posix()}` and also print the same JSON object "
        "as your final response. The object must have this shape:\n\n"
        "```json\n"
        '{"status":"satisfied|needs_iteration","summary":"...",'
        '"gaps":["..."],"reason_code":"optional reason code"}\n'
        "```\n\n"
        "Use `satisfied` only when the implementation fully achieves the saved plan. "
        "Use `needs_iteration` when any planned behavior, test, validation, or documented "
        "non-goal handling is missing. Keep gaps actionable and specific.\n\n"
        f"Iteration: {iteration}\n\n"
        f"### Original Task\n{task_prompt}\n"
    )


def build_conformance_failure_evidence(
    *,
    report: PlanConformanceReport,
    iterations_used: int,
    max_iterations: int,
    plan_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    """Return bounded structured evidence for exhausted conformance."""

    return {
        "summary": _safe_conformance_text(report.summary),
        "gaps": [_safe_conformance_text(gap) for gap in report.gaps[:MAX_CONFORMANCE_GAPS]],
        "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
        "report_reason_code": report.reason_code,
        "iterations_used": iterations_used,
        "max_iterations": max_iterations,
        "plan_path": plan_path.as_posix(),
        "report_path": report_path.as_posix(),
    }


def conformance_requires_awf_validation(report: PlanConformanceReport) -> bool:
    """Return true only for explicit validation-evidence-only conformance gaps."""

    if report.status != PlanConformanceStatus.needs_iteration:
        return False
    normalized_reason = report.reason_code.strip().upper().replace("-", "_")
    if normalized_reason != CONFORMANCE_REQUIRES_AWF_VALIDATION:
        return False
    if not report.gaps:
        return False
    return all(_is_awf_validation_evidence_gap(gap) for gap in report.gaps)


def classify_conformance_stall(
    *,
    history: Sequence[ConformanceIterationRecord],
    policy: ConformanceStallPolicy,
    plan_path: Path,
    report_path: Path,
    latest_error: AgentRunError | None,
) -> ConformanceStallEvidence | None:
    """Decide whether the conformance loop has stalled.

    Returns ``None`` for the deterministic ``needs_iteration`` case so the
    existing ``PLAN_CONFORMANCE_UNSATISFIED`` path keeps owning real plan
    gaps. Returns structured evidence only when an explicit stall signal is
    present: an idle timeout (no_output), a wall-clock timeout (over_duration),
    repeated identical reports without any worktree change, or a cumulative
    loop duration above the policy.
    """

    if not history:
        return None

    last = history[-1]
    cumulative_seconds = sum(record.elapsed_seconds for record in history)
    plan_path_text = plan_path.as_posix()
    report_path_text = report_path.as_posix()

    error_reason_code = (
        latest_error.reason_code if latest_error is not None else last.error_reason_code
    )
    if error_reason_code == AGENT_IDLE_TIMEOUT_REASON_CODE:
        # AGENT_IDLE_TIMEOUT is an explicit signal raised by the adapter when
        # AWF_AGENT_IDLE_TIMEOUT_SECONDS elapses without output. That operator-
        # configured timeout is the gate for this branch, not policy.no_output_seconds
        # (which gates the empty-streak detector below). Gating both on the same
        # threshold would swallow legitimate idle-timeout signals whenever the
        # adapter's idle timeout is lower than the loop policy.
        excerpt = _stall_output_excerpt(latest_error, last)
        return ConformanceStallEvidence(
            kind=ConformanceStallKind.no_output,
            iteration_index=last.iteration,
            elapsed_seconds=cumulative_seconds,
            no_output_seconds=last.elapsed_seconds,
            repeated_output_count=0,
            last_report_digest=last.report_digest,
            plan_path=plan_path_text,
            report_path=report_path_text,
            last_output_excerpt=excerpt,
        )

    if error_reason_code == AGENT_TIMEOUT_REASON_CODE:
        excerpt = _stall_output_excerpt(latest_error, last)
        return ConformanceStallEvidence(
            kind=ConformanceStallKind.over_duration,
            iteration_index=last.iteration,
            elapsed_seconds=cumulative_seconds,
            no_output_seconds=0.0,
            repeated_output_count=0,
            last_report_digest=last.report_digest,
            plan_path=plan_path_text,
            report_path=report_path_text,
            last_output_excerpt=excerpt,
        )

    empty_streak_seconds = 0.0
    empty_streak_iterations = 0
    # The executor reads the report file from disk each iteration, so a
    # report left behind by an earlier iteration keeps yielding the same
    # non-None digest even when the current iteration produced no output.
    # Treat the digest as fresh progress only when it differs from the
    # immediately-prior iteration's digest. At iteration 0 there is no
    # prior record to compare against, and a preserved worktree (e.g.,
    # retry of a salvaged workspace) can leave a stale report on disk
    # before the loop even starts; fall back to ``worktree_changed`` so
    # the digest only counts as fresh when iteration 0 actually moved
    # the worktree.
    for index in range(len(history) - 1, -1, -1):
        record = history[index]
        prior = history[index - 1] if index > 0 else None
        fresh_report_digest = record.report_digest is not None and (
            (prior is None and record.worktree_changed)
            or (prior is not None and prior.report_digest != record.report_digest)
        )
        if record.stdout.strip() or record.stderr.strip() or fresh_report_digest:
            break
        empty_streak_seconds += record.elapsed_seconds
        empty_streak_iterations += 1
    if empty_streak_iterations > 0 and empty_streak_seconds >= policy.no_output_seconds:
        return ConformanceStallEvidence(
            kind=ConformanceStallKind.no_output,
            iteration_index=last.iteration,
            elapsed_seconds=cumulative_seconds,
            no_output_seconds=empty_streak_seconds,
            repeated_output_count=0,
            last_report_digest=last.report_digest,
            plan_path=plan_path_text,
            report_path=report_path_text,
            last_output_excerpt=_stall_output_excerpt(latest_error, last),
        )

    threshold = max(2, policy.repeated_output_threshold)
    if last.report_digest is not None and len(history) >= threshold:
        recent = history[-threshold:]
        same_digest = all(record.report_digest == last.report_digest for record in recent)
        no_progress = all(not record.worktree_changed for record in recent)
        if same_digest and no_progress:
            return ConformanceStallEvidence(
                kind=ConformanceStallKind.repeated_output,
                iteration_index=last.iteration,
                elapsed_seconds=cumulative_seconds,
                no_output_seconds=0.0,
                repeated_output_count=threshold,
                last_report_digest=last.report_digest,
                plan_path=plan_path_text,
                report_path=report_path_text,
                last_output_excerpt=_stall_output_excerpt(None, last),
            )

    if cumulative_seconds > policy.over_duration_seconds:
        return ConformanceStallEvidence(
            kind=ConformanceStallKind.over_duration,
            iteration_index=last.iteration,
            elapsed_seconds=cumulative_seconds,
            no_output_seconds=0.0,
            repeated_output_count=0,
            last_report_digest=last.report_digest,
            plan_path=plan_path_text,
            report_path=report_path_text,
            last_output_excerpt=_stall_output_excerpt(None, last),
        )

    return None


def _is_awf_validation_evidence_gap(gap: str) -> bool:
    text = gap.strip().lower()

    def has_marker(marker: str) -> bool:
        return re.search(rf"(?<![a-z0-9_]){re.escape(marker)}(?![a-z0-9_])", text) is not None

    named_validation_command = any(
        has_marker(marker)
        for marker in (
            "pytest",
            "ruff",
            "mypy",
            "coverage",
            "npm",
            "lint",
            "build",
        )
    ) or (re.search(r"(?<![a-z0-9_])git\s+diff\s+--check(?![a-z0-9_])", text) is not None)
    instructional_validation_run = any(
        re.search(pattern, text) is not None
        for pattern in (
            r"(?:^|[.;:]\s*|(?<![a-z0-9_])"
            r"(?:please\s+|(?:(?:must|should|needs?|required|requires?)\s+"
            r"(?:to\s+|be\s+)?|has\s+to\s+)))"
            r"(?:re-?run|run)(?![a-z0-9_])",
            r"(?<![a-z0-9_])(?:must|should|needs?|required|requires?)\s+"
            r"(?:[a-z0-9_.:/\\`-]+\s+){1,8}(?:to\s+|be\s+)"
            r"(?:re-?run|run)(?![a-z0-9_])",
        )
    )
    named_validation_command_handoff = (
        instructional_validation_run
        and any(
            has_marker(marker)
            for marker in (
                "awf",
                "validation",
                "validation phase",
                "profile gate",
                "profile gates",
            )
        )
        and named_validation_command
    )
    evidence_markers = (
        "evidence",
        "provenance",
        "log",
        "logs",
        "missing",
        "stale",
        "insufficient",
        "absent",
        "unavailable",
        "not available",
        "not found",
        "not run",
        "has not run",
        "run validation",
        "validation run",
        "profile gate",
        "profile gates",
    )
    if not named_validation_command_handoff and not any(
        has_marker(marker) for marker in evidence_markers
    ):
        return False
    validation_subject_markers = (
        "validation",
        "coverage",
        "profile",
        "profile gate",
        "profile gates",
        "evidence",
        "provenance",
        "log",
        "logs",
    )
    if not named_validation_command_handoff and not any(
        has_marker(marker) for marker in validation_subject_markers
    ):
        return False
    named_validation_command_evidence = named_validation_command_handoff or (
        named_validation_command
        and any(has_marker(marker) for marker in ("not run", "has not run"))
    )
    negated_saved_plan_scoped_check_record = (
        re.search(
            r"(?<![a-z0-9_])(?:focused|scoped)[-\s]+checks?"
            r"(?:\s+(?:evidence|artifact))?\s+"
            r"(?:(?:is|are|was|were|has|have|had|remains?)\s+)?"
            r"(?:not\s+(?:yet\s+)?(?:been\s+)?recorded|unrecorded)(?![a-z0-9_])"
            r"[^.;:]*\b(?:from|in|inside|within)\s+(?:the\s+)?"
            r"(?:saved\s+)?plan(?![a-z0-9_])",
            text,
        )
        is not None
    )
    saved_plan_scoped_check_evidence = (
        re.search(
            r"(?<![a-z0-9_])(?:focused|scoped)[-\s]+checks?"
            r"\s+(?:evidence|artifact)(?![a-z0-9_])",
            text,
        )
        is not None
    )
    saved_plan_scoped_check_handoff = (
        not negated_saved_plan_scoped_check_record
        and has_marker("saved plan")
        and named_validation_command_handoff
        and saved_plan_scoped_check_evidence
        and re.search(r"(?<![a-z0-9_])(?:focused|scoped)[-\s]+checks?(?![a-z0-9_])", text)
        is not None
        and any(
            has_marker(marker)
            for marker in ("evidence", "record", "recorded", "recording", "validation phase")
        )
    )
    # Migration implementation gaps stay agent-owned; migration-gate evidence
    # gaps are AWF-owned because the profile gate must produce that evidence.
    migration_validation_evidence_gap = has_marker(
        "migration"
    ) and _has_migration_validation_evidence_context(text)
    if has_marker("migration") and not migration_validation_evidence_gap:
        return False
    deterministic_markers = (
        "api",
        "endpoint",
        "implement",
        "implementation",
        "wire",
        "function",
        "class",
    )
    if any(has_marker(marker) for marker in deterministic_markers):
        return False
    if has_marker("schema") and not migration_validation_evidence_gap:
        return False
    saved_plan_scoped_check_missing_pattern = (
        r"(?<![a-z0-9_])(?:focused|scoped)[-\s]+checks?"
        r"(?:\s+(?:evidence|artifact))?\s+"
        r"(?:is|are|was|were|remains?)?\s*"
        r"(?:missing|absent|unavailable|not available|not found)(?![a-z0-9_])"
        r"[^.;:]*\b(?:from|in|inside|within)\s+(?:the\s+)?"
        r"(?:saved\s+)?plan(?![a-z0-9_])"
    )
    if not saved_plan_scoped_check_handoff and re.search(
        saved_plan_scoped_check_missing_pattern,
        text,
    ):
        return False
    deterministic_gap_patterns = (
        r"(?:^|[.;:]\s*|(?<![a-z0-9_])please\s+)document(?![a-z0-9_])"
        r"\s+(?:the\s+)?",
        r"(?<![a-z0-9_])(?:must|should|need|needs|required)\s+"
        r"(?:to\s+)?document(?![a-z0-9_])",
        r"(?<![a-z0-9_])(?:add|create|update|write|revise)\s+"
        r"(?:the\s+)?(?:docs|documentation|document|doc|guide|readme)(?![a-z0-9_])",
        r"(?<![a-z0-9_])(?:docs|documentation|document|doc|guide|readme)(?![a-z0-9_])"
        r"\s+(?:gap|gaps|task|tasks|todo|todos|update|updates|change|changes|"
        r"need|needs|must|should|required|missing|absent|stale|outdated)",
        r"(?<![a-z0-9_])(?:add|create|edit|include|modify|update|write|revise)\s+"
        r"(?:the\s+)?(?:saved\s+)?plan(?![a-z0-9_])",
        r"(?<![a-z0-9_])(?:add|create|edit|include|modify|update|write|revise)\b"
        r"[^.;:]*\b(?:to|in|inside|into|within)\s+(?:the\s+)?"
        r"(?:saved\s+)?plan(?![a-z0-9_])",
        r"(?<![a-z0-9_])(?:saved\s+)?plan(?![a-z0-9_])"
        r"\s+(?:gap|gaps|task|tasks|todo|todos|update|updates|change|changes|"
        r"need|needs|must|should|required|missing|absent|stale|outdated|lacks?)",
        r"(?<![a-z0-9_])(?:from|in|inside|within)\s+(?:the\s+)?"
        r"(?:docs|documentation|document|doc|guide|readme)(?![a-z0-9_])",
        r"(?<![a-z0-9_])(?:docs|documentation|document|doc|guide|readme)"
        r"(?![a-z0-9_])[^.;:]*\b(?:evidence|coverage|profile gate|log|logs)\b[^.;:]*"
        r"\b(?:missing|absent|stale|outdated|insufficient|unavailable|not available|"
        r"not found|not run|has not run)\b",
    )
    if any(re.search(pattern, text) for pattern in deterministic_gap_patterns):
        return False
    saved_plan_artifact_patterns = (
        r"(?<![a-z0-9_])(?:from|in|inside|within)\s+(?:the\s+)?"
        r"(?:saved\s+)?plan(?![a-z0-9_])",
        r"(?<![a-z0-9_])(?:saved\s+)?plan(?![a-z0-9_])"
        r"[^.;:]*\b(?:evidence|coverage|profile gate|log|logs)\b[^.;:]*"
        r"\b(?:missing|absent|stale|outdated|insufficient|unavailable|not available|"
        r"not found|not run|has not run)\b",
    )
    if not saved_plan_scoped_check_handoff and any(
        re.search(pattern, text) for pattern in saved_plan_artifact_patterns
    ):
        return False
    # Mixed mentions remain agent-owned: only "code coverage" is AWF-validation
    # evidence, while any other standalone "code" use is a deterministic gap.
    for match in re.finditer(r"(?<![a-z0-9_])code(?![a-z0-9_])", text):
        if re.match(r"[\s-]+coverage\b", text[match.end() :]):
            continue
        return False
    # Test work is deterministic agent work; only coverage artifact phrases
    # around test/test(s) remain validation evidence.
    for match in re.finditer(r"(?<![a-z0-9_])tests?(?![a-z0-9_])", text):
        if named_validation_command_evidence and text[match.end() :].startswith(("/", "\\")):
            if _has_test_path_work_context(text, match.start()):
                return False
            continue
        if named_validation_command_evidence and _is_test_directory_command_target(
            text, match.start(), match.end()
        ):
            if _has_test_path_work_context(text, match.start()):
                return False
            continue
        if re.match(
            r"[\s\-,:]+(?:coverage|evidence|provenance|logs?|"
            r"(?:suites?|runners?|runs?|reports?)[\s\-,:]+"
            r"(?:coverage|evidence|provenance|logs?))\b",
            text[match.end() :],
        ):
            continue
        return False
    return True


def _is_test_directory_command_target(
    text: str, path_match_start: int, path_match_end: int
) -> bool:
    if (
        re.match(
            r"(?:\s*(?:[`),.;:]|$)|\s+-{1,2}[a-z0-9][a-z0-9-]*|"
            r"\s+(?:(?:\.{1,2}[/\\])|[a-z0-9_.-]+[/\\]|"
            r"[a-z0-9_.-]+\.[a-z0-9_.-]+)[^\s`),;:]*)",
            text[path_match_end:],
        )
        is None
    ):
        return False
    segment_start = max(
        text.rfind("`", 0, path_match_start),
        text.rfind(";", 0, path_match_start),
        text.rfind("\n", 0, path_match_start),
        text.rfind(":", 0, path_match_start),
    )
    command_segment = text[segment_start + 1 : path_match_start]
    if not _starts_with_validation_command_segment(command_segment):
        return False
    if (
        re.search(
            r"(?<![a-z0-9_])(?:has\s+not\s+run|not\s+run)(?![a-z0-9_])",
            command_segment,
        )
        is not None
    ):
        return False
    return (
        re.search(
            r"(?<![a-z0-9_])(?:pytest|ruff\s+check|mypy|coverage|npm|lint|build|"
            r"git\s+diff\s+--check)"
            r"(?![a-z0-9_])",
            command_segment,
        )
        is not None
    )


def _starts_with_validation_command_segment(segment: str) -> bool:
    return (
        re.match(
            r"\s*(?:uv\s+run|python(?:3(?:\.\d+)?)?(?:\s+-m)?|"
            r"/usr/bin/env|env|pytest|ruff\s+check|mypy|coverage|npm|lint|build|"
            r"git\s+diff\s+--check)(?![a-z0-9_])",
            segment,
        )
        is not None
    )


def _has_test_path_work_context(text: str, path_match_start: int) -> bool:
    work_verbs = (
        "add",
        "adding",
        "create",
        "creating",
        "edit",
        "editing",
        "modify",
        "modifying",
        "revise",
        "revising",
        "update",
        "updating",
        "write",
        "writing",
    )
    modifiers = (
        "a",
        "an",
        "the",
        "new",
        "missing",
        "additional",
        "focused",
        "regression",
        "targeted",
        "unit",
        "integration",
        "validation",
    )
    work_objects = (
        "assertion",
        "assertions",
        "case",
        "cases",
        "fixture",
        "fixtures",
        "scenario",
        "scenarios",
    )
    location_prepositions = ("for", "in", "inside", "into", "to", "under", "within")
    work_verb_pattern = rf"(?<![a-z0-9_])(?:{'|'.join(work_verbs)})"
    modifier_pattern = rf"(?:\s+(?:{'|'.join(modifiers)}))*"
    path_prefix = r"(?:(?:\./|\.\\)|[a-z0-9_.-]+[/\\])*"
    prefix = text[:path_match_start]
    if (
        re.search(
            rf"{work_verb_pattern}{modifier_pattern}\s+{path_prefix}$",
            prefix,
            flags=re.IGNORECASE,
        )
        is not None
    ):
        return True
    path_suffix = text[path_match_start:]
    test_path_pattern = r"tests?(?:[/\\][^\s`),;]*)?"
    post_path_work_lead_pattern = (
        r"needs?|requires?|lacks?|missing|"
        r"(?:must|should)\s+(?:add|include|cover|exercise)"
    )
    if (
        re.search(
            rf"^{test_path_pattern}(?:\s+|[,;:]\s+)"
            rf"(?:{post_path_work_lead_pattern}){modifier_pattern}\s+"
            rf"(?:{'|'.join(work_objects)})(?![a-z0-9_])",
            path_suffix,
            flags=re.IGNORECASE,
        )
        is not None
    ):
        return True
    if (
        re.search(
            rf"^{test_path_pattern}(?:\s+|[,;:]\s+)"
            r"(?:is|are|was|were|remains?)\s+missing(?![a-z0-9_])",
            path_suffix,
            flags=re.IGNORECASE,
        )
        is not None
    ):
        return True
    return (
        re.search(
            rf"{work_verb_pattern}{modifier_pattern}\s+"
            rf"(?:{'|'.join(work_objects)})\s+"
            rf"(?:{'|'.join(location_prepositions)})\s+{path_prefix}$",
            prefix,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _has_migration_validation_evidence_context(text: str) -> bool:
    patterns = (
        r"(?<![a-z0-9_])migration\s+validation\s+"
        r"(?:evidence|provenance|logs?|runs?)(?![a-z0-9_])",
        r"(?<![a-z0-9_])(?:(?:alembic(?:[/ -]profile)?|profile)\s+)?"
        r"migration\s+(?:profile\s+)?gates?(?![a-z0-9_])",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def build_conformance_stall_failure_evidence(
    *,
    stall: ConformanceStallEvidence,
    head_sha: str | None,
    base_sha: str | None,
    commit_count: int,
    changed_paths: Sequence[str] = (),
    recovery_action: str | None = None,
) -> dict[str, Any]:
    """Return bounded structured evidence for a detected conformance stall."""

    salvage_paths = [
        _safe_conformance_text(path)
        for path in list(changed_paths)[:MAX_CONFORMANCE_GAPS]
        if _safe_conformance_text(path)
    ]
    payload: dict[str, Any] = {
        "reason_code": AGENT_STALLED_IN_CONFORMANCE,
        "kind": stall.kind.value,
        "iteration_index": stall.iteration_index,
        "elapsed_seconds": stall.elapsed_seconds,
        "no_output_seconds": stall.no_output_seconds,
        "repeated_output_count": stall.repeated_output_count,
        "last_report_digest": stall.last_report_digest,
        "plan_path": stall.plan_path,
        "report_path": stall.report_path,
        "last_output_excerpt": _safe_conformance_text(stall.last_output_excerpt),
        "salvage_hint": {
            "plan_path": stall.plan_path,
            "report_path": stall.report_path,
            "implementation_commit_count": max(0, commit_count),
            "head_sha": head_sha,
            "base_sha": base_sha,
            "changed_paths": salvage_paths,
        },
    }
    if recovery_action is not None:
        payload["recovery_action"] = recovery_action
    return payload


def build_conformance_stall_recovery_prompt(
    *,
    task_prompt: str,
    stall_evidence: Mapping[str, Any],
    prior_gaps: Sequence[str] = (),
) -> str:
    """Resume an agent with conformance-only instructions after a stall."""

    plan_path = _safe_conformance_text(stall_evidence.get("plan_path"))
    report_path = _safe_conformance_text(stall_evidence.get("report_path"))
    plan_line = f"`{plan_path}`" if plan_path else "the existing plan artifact"
    report_line = f"`{report_path}`" if report_path else "the existing conformance report path"
    gap_lines = (
        "\n".join(f"- {gap}" for gap in prior_gaps if gap) or "- No prior gaps were captured."
    )
    return (
        "## Retry after conformance stall\n\n"
        "The prior workspace stalled while generating the plan-conformance JSON. "
        "Implementation commits already exist on the feature branch; only re-run the "
        "compare phase against the saved plan and write the JSON report.\n\n"
        f"Read the existing plan at {plan_line} and use it as the source of truth. "
        f"Write the JSON conformance report to {report_line} and also print the same "
        "JSON object as your final response.\n\n"
        "Do not modify implementation files. Only re-run the compare phase against "
        "the existing plan, and write the JSON to the existing report path. Do not run "
        "validation commands or implementation commands during this recovery compare. "
        "That includes pytest, ruff, mypy, coverage, npm, lint commands, build commands, "
        "git add, and git commit. Use existing validation evidence from logs, validation "
        "summaries, git diff, and artifacts. If validation evidence is missing, stale, "
        "or insufficient, report `needs_iteration` with a specific gap asking AWF to "
        "run the missing validation under the validation phase. Set `reason_code` to "
        "`CONFORMANCE_REQUIRES_AWF_VALIDATION` only when every remaining gap is missing, "
        "stale, or insufficient AWF-owned validation evidence. Do not use it for "
        "implementation, API, plan, or documentation gaps.\n\n"
        "The object must have this shape:\n\n"
        "```json\n"
        '{"status":"satisfied|needs_iteration","summary":"...",'
        '"gaps":["..."],"reason_code":"optional reason code"}\n'
        "```\n\n"
        f"### Prior gaps (advisory)\n{gap_lines}\n\n"
        f"### Original task\n{task_prompt}\n"
    )


def _stall_output_excerpt(
    error: AgentRunError | None,
    record: ConformanceIterationRecord,
) -> str:
    # Stall excerpts are persisted as durable failure details; raw agent
    # stderr/stdout can contain provider tokens or URL credentials, so redact
    # before truncation. Redact-then-truncate avoids splitting a token across
    # the truncation boundary and leaving an exploitable fragment.
    if error is not None:
        result = getattr(error, "result", None)
        if result is not None:
            stderr = (getattr(result, "stderr", "") or "").strip()
            if stderr:
                return _safe_conformance_text(redact_secrets(stderr))
            stdout = (getattr(result, "stdout", "") or "").strip()
            if stdout:
                return _safe_conformance_text(redact_secrets(stdout))
        return _safe_conformance_text(redact_secrets(str(error)))
    text = (record.stderr or "").strip() or (record.stdout or "").strip()
    return _safe_conformance_text(redact_secrets(text))


def build_conformance_retry_prompt(
    *,
    task_prompt: str,
    evidence: Mapping[str, Any],
) -> str:
    """Build a retry prompt that resumes from final conformance gaps."""

    summary = _safe_conformance_text(evidence.get("summary"))
    gaps = _evidence_gaps(evidence)
    gap_lines = "\n".join(f"- {gap}" for gap in gaps) or "- Re-check the saved plan."
    plan_path = _safe_conformance_text(evidence.get("plan_path"))
    report_path = _safe_conformance_text(evidence.get("report_path"))
    artifact_lines = []
    if plan_path:
        artifact_lines.append(f"- Plan: `{plan_path}`")
    if report_path:
        artifact_lines.append(f"- Final conformance report: `{report_path}`")
    artifacts = "\n".join(artifact_lines) or "- Plan artifacts were not recorded."

    return (
        "## Retry after plan conformance failure\n\n"
        "The prior workspace exhausted plan-conformance iterations. Continue from the "
        "original task and finish the remaining plan-conformance gaps below. Do not "
        "restart from scratch unless the existing work is unusable.\n\n"
        f"### Final summary\n{summary or 'Plan conformance was not satisfied.'}\n\n"
        f"### Remaining gaps\n{gap_lines}\n\n"
        f"### Evidence\n{artifacts}\n\n"
        f"### Original task\n{task_prompt}\n"
    )


def parse_conformance_report(text: str) -> PlanConformanceReport:
    """Parse the agent's conformance JSON, degrading invalid output safely."""

    try:
        payload = json.loads(text.strip())
    except json.JSONDecodeError:
        return PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="Conformance report was not valid JSON.",
            gaps=("Produce a valid plan-conformance JSON report.",),
            reason_code="PLAN_CONFORMANCE_REPORT_INVALID",
        )
    if not isinstance(payload, dict):
        return PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="Conformance report JSON was not an object.",
            gaps=("Produce a JSON object with status, summary, and gaps.",),
            reason_code="PLAN_CONFORMANCE_REPORT_INVALID",
        )

    status = _status_from_payload(payload.get("status"))
    gaps = _gaps_from_payload(payload.get("gaps"))
    summary = str(payload.get("summary") or "").strip() or (
        "Plan satisfied." if status == PlanConformanceStatus.satisfied else "Plan gaps remain."
    )
    reason_code = _reason_code_from_payload(payload.get("reason_code"))
    if status == PlanConformanceStatus.satisfied and gaps:
        status = PlanConformanceStatus.needs_iteration
        summary = f"{summary} Report included gaps, so AWF requires another iteration."
    return PlanConformanceReport(
        status=status,
        summary=summary,
        gaps=gaps,
        reason_code=reason_code,
    )


def changed_paths_from_porcelain(output: str) -> set[Path]:
    """Parse ``git status --porcelain=v1`` into repo-relative paths."""

    paths: set[Path] = set()
    for raw in output.splitlines():
        if not raw:
            continue
        status = raw[:2] if len(raw) >= 2 else ""
        path_text = raw[3:] if len(raw) > 3 else raw
        if status[:1] in {"R", "C"} or status[1:2] in {"R", "C"}:
            rename_paths = split_porcelain_rename_paths(path_text)
            if rename_paths:
                path_text = rename_paths[1]
        path_text = path_text.strip()
        if path_text:
            paths.add(Path(unquote_porcelain_path(path_text)))
    return paths


def _status_from_payload(value: Any) -> PlanConformanceStatus:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"satisfied", "pass", "passed", "ok", "complete", "completed"}:
        return PlanConformanceStatus.satisfied
    return PlanConformanceStatus.needs_iteration


def _gaps_from_payload(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    return ()


def _reason_code_from_payload(value: Any) -> str:
    if not isinstance(value, str):
        return PLAN_CONFORMANCE_REPORTED
    normalized = value.strip().upper().replace("-", "_")
    if not normalized:
        return PLAN_CONFORMANCE_REPORTED
    return normalized[:128]


def _normalized_coordination_warning(
    warning: Mapping[str, Any],
) -> dict[str, Any]:
    warning_code = _safe_warning_text(warning.get("warning_code")) or "COORDINATION_WARNING"
    message = _safe_warning_text(warning.get("message")) or warning_code
    severity = _safe_warning_text(warning.get("severity")) or "advisory"
    workspace_ids = _safe_warning_strings(warning.get("workspace_ids"))
    overlaps = _safe_warning_overlaps(warning.get("overlaps"))
    overlap_count = _safe_warning_int(warning.get("overlap_count"))
    context = _safe_warning_context(warning.get("stale_policy_context"))
    return {
        "warning_code": warning_code,
        "message": message,
        "severity": severity,
        "blocks_launch": _safe_warning_bool(warning.get("blocks_launch")),
        "workspace_ids": workspace_ids,
        "overlaps": overlaps,
        "overlap_count": max(
            overlap_count if overlap_count is not None else len(overlaps), len(overlaps)
        ),
        "overlaps_truncated": _safe_warning_bool(warning.get("overlaps_truncated")),
        "stale_policy_context": context,
    }


def _safe_warning_overlaps(value: object) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    overlaps: list[dict[str, str]] = []
    for item in value:
        if len(overlaps) >= MAX_COORDINATION_WARNING_OVERLAPS:
            break
        if not isinstance(item, Mapping):
            continue
        workspace_id = _safe_warning_text(item.get("workspace_id"))
        existing_path = _safe_warning_text(item.get("existing_path"))
        requested_path = _safe_warning_text(item.get("requested_path"))
        if workspace_id is None or existing_path is None or requested_path is None:
            continue
        overlaps.append(
            {
                "workspace_id": workspace_id,
                "existing_path": existing_path,
                "requested_path": requested_path,
            }
        )
    return overlaps


def _safe_warning_strings(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _safe_warning_context(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(item, str)}


def _safe_warning_bool(value: object) -> bool:
    return value if isinstance(value, bool) else False


def _safe_warning_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return max(0, value)


def _safe_warning_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _safe_conformance_text(value: object) -> str:
    text = str(value or "").strip()
    if len(text) <= MAX_CONFORMANCE_TEXT_CHARS:
        return text
    return text[: MAX_CONFORMANCE_TEXT_CHARS - 3].rstrip() + "..."


def _evidence_gaps(evidence: Mapping[str, Any]) -> tuple[str, ...]:
    value = evidence.get("gaps")
    if not isinstance(value, list):
        return ()
    gaps = [
        _safe_conformance_text(item)
        for item in value[:MAX_CONFORMANCE_GAPS]
        if _safe_conformance_text(item)
    ]
    return tuple(gaps)


def _evidence_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        _safe_conformance_text(item)
        for item in value[:MAX_CONFORMANCE_GAPS]
        if _safe_conformance_text(item)
    )
