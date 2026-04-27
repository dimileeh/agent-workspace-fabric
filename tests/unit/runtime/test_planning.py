"""Plan/execute/compare lifecycle helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.runtime.planning import (
    PlanConformanceStatus,
    _gaps_from_payload,
    build_conformance_prompt,
    build_execution_prompt,
    build_planning_prompt,
    changed_paths_from_porcelain,
    parse_conformance_report,
    render_workspace_path,
)


@pytest.mark.unit
def test_render_workspace_path_substitutes_workspace_id_and_rejects_escape() -> None:
    assert render_workspace_path("docs/awf-plans/{workspace_id}.md", workspace_id="ws_123") == Path(
        "docs/awf-plans/ws_123.md"
    )

    with pytest.raises(ValueError, match="must stay inside"):
        render_workspace_path("../plans/{workspace_id}.md", workspace_id="ws_123")

    with pytest.raises(ValueError, match="must stay inside"):
        render_workspace_path("/tmp/{workspace_id}.md", workspace_id="ws_123")

    with pytest.raises(ValueError, match="empty path"):
        render_workspace_path("", workspace_id="ws_123")


@pytest.mark.unit
def test_parse_conformance_report_accepts_json_object() -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration","summary":"not done","gaps":["wire API"]}'
    )

    assert report.status == PlanConformanceStatus.needs_iteration
    assert report.summary == "not done"
    assert report.gaps == ("wire API",)


@pytest.mark.unit
def test_parse_conformance_report_marks_invalid_json_as_needs_iteration() -> None:
    report = parse_conformance_report("not json")

    assert report.status == PlanConformanceStatus.needs_iteration
    assert report.reason_code == "PLAN_CONFORMANCE_REPORT_INVALID"


@pytest.mark.unit
def test_parse_conformance_report_rejects_non_object_json() -> None:
    report = parse_conformance_report('["satisfied"]')

    assert report.status == PlanConformanceStatus.needs_iteration
    assert report.summary == "Conformance report JSON was not an object."
    assert report.gaps == ("Produce a JSON object with status, summary, and gaps.",)
    assert report.reason_code == "PLAN_CONFORMANCE_REPORT_INVALID"


@pytest.mark.unit
def test_satisfied_report_with_gaps_is_downgraded() -> None:
    report = parse_conformance_report(
        '{"status":"satisfied","summary":"done","gaps":["missing validation"]}'
    )

    assert report.status == PlanConformanceStatus.needs_iteration
    assert report.summary == "done Report included gaps, so AWF requires another iteration."
    assert report.gaps == ("missing validation",)


@pytest.mark.unit
def test_parse_conformance_report_defaults_and_aliases() -> None:
    satisfied = parse_conformance_report('{"status":"ok","summary":"","gaps":[]}')
    needs_iteration = parse_conformance_report('{"status":"unknown","summary":"","gaps":"rerun mypy"}')

    assert satisfied.status == PlanConformanceStatus.satisfied
    assert satisfied.summary == "Plan satisfied."
    assert needs_iteration.status == PlanConformanceStatus.needs_iteration
    assert needs_iteration.summary == "Plan gaps remain."
    assert needs_iteration.gaps == ("rerun mypy",)


@pytest.mark.unit
def test_parse_conformance_report_filters_blank_gap_items() -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration","summary":"check","gaps":["  fix tests  "," ",""]}'
    )

    assert report.gaps == ("fix tests",)
    assert _gaps_from_payload({"unexpected": "shape"}) == ()


@pytest.mark.unit
def test_changed_paths_from_porcelain_handles_renames_and_short_lines() -> None:
    paths = changed_paths_from_porcelain(
        "\n"
        " M src/awf/runtime/planning.py\n"
        "R  docs/old.md -> docs/new.md\n"
        "?? tests/unit/runtime/test_planning.py\n"
        "   \n"
        "A\n"
    )

    assert paths == {
        Path("src/awf/runtime/planning.py"),
        Path("docs/new.md"),
        Path("tests/unit/runtime/test_planning.py"),
        Path("A"),
    }


@pytest.mark.unit
def test_prompts_reference_plan_and_report_paths() -> None:
    plan = Path("docs/awf-plans/ws_123.md")
    report = Path("docs/awf-plans/ws_123.conformance.json")

    planning_prompt = build_planning_prompt(task_prompt="Add metrics", plan_path=plan)
    execution_prompt = build_execution_prompt(
        task_prompt="Add metrics",
        plan_path=plan,
        iteration=0,
        gaps=(),
    )
    conformance_prompt = build_conformance_prompt(
        task_prompt="Add metrics",
        plan_path=plan,
        report_path=report,
        iteration=0,
    )

    assert str(plan) in planning_prompt
    assert "Do not modify implementation files" in planning_prompt
    assert str(plan) in execution_prompt
    assert str(report) in conformance_prompt
    assert '"status"' in conformance_prompt
