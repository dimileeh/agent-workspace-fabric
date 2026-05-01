"""Plan → execute → compare prompt and report helpers.

AWF owns this lifecycle instead of relying on vendor-specific interactive
"plan mode" affordances. The coding CLI is invoked non-interactively for three
bounded phases: create a plan artifact, implement it, then produce a structured
conformance report. The executor decides whether another implementation pass is
needed from that report.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from awf.common.coordination import MAX_COORDINATION_WARNING_OVERLAPS

PLAN_CONFORMANCE_UNSATISFIED = "PLAN_CONFORMANCE_UNSATISFIED"
PLAN_CONFORMANCE_REPORTED = "PLAN_CONFORMANCE_REPORTED"
AGENT_PLAN_PHASE_SCOPE_VIOLATION = "AGENT_PLAN_PHASE_SCOPE_VIOLATION"
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


def render_workspace_path(template: str, *, workspace_id: str) -> Path:
    """Render a workspace-relative artifact path from profile config."""

    rendered = template.format(workspace_id=workspace_id)
    if not rendered.strip():
        raise ValueError("path template rendered an empty path")
    path = Path(rendered)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"path template must stay inside the workspace: {template!r}")
    return path


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


def build_agent_task_prompt(
    *,
    task_prompt: str,
    coordination_warnings: Sequence[Mapping[str, Any]] = (),
) -> str:
    warning_section = render_coordination_warning_section(coordination_warnings)
    if not warning_section:
        return task_prompt
    return f"{warning_section}### Task\n{task_prompt}\n"


def build_planning_prompt(
    *,
    task_prompt: str,
    plan_path: Path,
    coordination_warnings: Sequence[Mapping[str, Any]] = (),
) -> str:
    warning_section = render_coordination_warning_section(coordination_warnings)
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
        "Do not edit source, tests, docs, config, migrations, lockfiles, or any other "
        "file during this retry planning phase. Do not run implementation commands "
        "such as apply_patch, pytest, ruff, mypy, npm, build commands, git add, or "
        "git commit. After writing the plan, stop.\n\n"
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
    return (
        "## Execution phase\n\n"
        f"Read `{plan_path.as_posix()}` and use it as the implementation contract.\n"
        f"{instruction}\n\n"
        "Do not switch branches, push, or open a PR. AWF owns branch and PR lifecycle.\n\n"
        f"{warning_section}"
        f"### Task\n{task_prompt}\n"
    )


def build_conformance_prompt(
    *,
    task_prompt: str,
    plan_path: Path,
    report_path: Path,
    iteration: int,
) -> str:
    return (
        "## Conformance phase\n\n"
        f"Compare the current workspace implementation against `{plan_path.as_posix()}`.\n"
        "Do not modify implementation files. Only write the JSON report requested below.\n\n"
        f"Write a JSON object to `{report_path.as_posix()}` and also print the same JSON object "
        "as your final response. The object must have this shape:\n\n"
        '```json\n{"status":"satisfied|needs_iteration","summary":"...","gaps":["..."]}\n```\n\n'
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
        path_text = raw[3:] if len(raw) > 3 else raw
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1]
        path_text = path_text.strip()
        if path_text:
            paths.add(Path(path_text))
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
        "overlap_count": max(overlap_count if overlap_count is not None else len(overlaps), len(overlaps)),
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
