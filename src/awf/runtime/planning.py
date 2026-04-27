"""Plan → execute → compare prompt and report helpers.

AWF owns this lifecycle instead of relying on vendor-specific interactive
"plan mode" affordances. The coding CLI is invoked non-interactively for three
bounded phases: create a plan artifact, implement it, then produce a structured
conformance report. The executor decides whether another implementation pass is
needed from that report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


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
    reason_code: str = "PLAN_CONFORMANCE_REPORTED"

    @property
    def satisfied(self) -> bool:
        return self.status == PlanConformanceStatus.satisfied


def render_workspace_path(template: str, *, workspace_id: str) -> Path:
    """Render a workspace-relative artifact path from profile config."""

    rendered = template.format(workspace_id=workspace_id)
    path = Path(rendered)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"path template must stay inside the workspace: {template!r}")
    if not str(path):
        raise ValueError("path template rendered an empty path")
    return path


def build_planning_prompt(*, task_prompt: str, plan_path: Path) -> str:
    return (
        "## Planning phase\n\n"
        "Create a concrete implementation plan for the task below.\n\n"
        f"Write the plan to `{plan_path.as_posix()}`. Create parent directories if needed.\n"
        "Do not modify implementation files, tests, config, migrations, lockfiles, or docs other "
        "than the requested plan file. Do not commit.\n\n"
        "The plan must include:\n"
        "- intended files/modules to touch;\n"
        "- tests to write first;\n"
        "- validation commands;\n"
        "- risks, assumptions, and explicit non-goals.\n\n"
        f"### Task\n{task_prompt}\n"
    )


def build_execution_prompt(
    *,
    task_prompt: str,
    plan_path: Path,
    iteration: int,
    gaps: tuple[str, ...],
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

    return (
        "## Execution phase\n\n"
        f"Read `{plan_path.as_posix()}` and use it as the implementation contract.\n"
        f"{instruction}\n\n"
        "Do not switch branches, push, or open a PR. AWF owns branch and PR lifecycle.\n\n"
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
    if status == PlanConformanceStatus.satisfied and gaps:
        status = PlanConformanceStatus.needs_iteration
        summary = f"{summary} Report included gaps, so AWF requires another iteration."
    return PlanConformanceReport(status=status, summary=summary, gaps=gaps)


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
