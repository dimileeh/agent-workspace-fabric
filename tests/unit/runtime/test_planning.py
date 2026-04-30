"""Plan/execute/compare lifecycle helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.runtime.planning import (
    PLAN_CONFORMANCE_UNSATISFIED,
    PlanConformanceStatus,
    _gaps_from_payload,
    build_agent_task_prompt,
    build_conformance_failure_evidence,
    build_conformance_prompt,
    build_conformance_retry_prompt,
    build_execution_prompt,
    build_planning_prompt,
    changed_paths_from_porcelain,
    parse_conformance_report,
    render_coordination_warning_section,
    render_workspace_path,
)
from awf.service.coordination import MAX_COORDINATION_WARNING_OVERLAPS


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
        '{"status":"needs_iteration","summary":"not done","gaps":["wire API"],'
        '"reason_code":"PLAN_CONFORMANCE_API_GAP"}'
    )

    assert report.status == PlanConformanceStatus.needs_iteration
    assert report.summary == "not done"
    assert report.gaps == ("wire API",)
    assert report.reason_code == "PLAN_CONFORMANCE_API_GAP"


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
    satisfied = parse_conformance_report(
        '{"status":"ok","summary":"","gaps":[],"reason_code":"   "}'
    )
    needs_iteration = parse_conformance_report('{"status":"unknown","summary":"","gaps":"rerun mypy"}')

    assert satisfied.status == PlanConformanceStatus.satisfied
    assert satisfied.summary == "Plan satisfied."
    assert satisfied.reason_code == "PLAN_CONFORMANCE_REPORTED"
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


@pytest.mark.unit
def test_coordination_warning_renders_in_planning_and_execution_prompts() -> None:
    warning = {
        "warning_code": "OWNED_PATH_OVERLAP_RISK",
        "message": "Owned paths overlap active workspaces.",
        "severity": "advisory",
        "blocks_launch": False,
        "workspace_ids": ["ws_existing"],
        "overlaps": [
            {
                "workspace_id": "ws_existing",
                "existing_path": "src/awf/service/**",
                "requested_path": "src/awf/service/workspaces.py",
            }
        ],
        "stale_policy_context": {
            "trigger_type": "path_overlap",
            "stale_reason_code": "STALE_OVERLAP",
        },
    }

    planning_prompt = build_planning_prompt(
        task_prompt="Add coordination metadata.",
        plan_path=Path("docs/awf-plans/ws_new.md"),
        coordination_warnings=(warning,),
    )
    execution_prompt = build_execution_prompt(
        task_prompt="Add coordination metadata.",
        plan_path=Path("docs/awf-plans/ws_new.md"),
        iteration=0,
        gaps=(),
        coordination_warnings=(warning,),
    )

    for prompt in (planning_prompt, execution_prompt):
        assert "Coordination warnings" in prompt
        assert "OWNED_PATH_OVERLAP_RISK" in prompt
        assert "ws_existing" in prompt
        assert "src/awf/service/** -> src/awf/service/workspaces.py" in prompt
        assert "advisory and does not block launch" in prompt
        assert "STALE_OVERLAP" in prompt
        assert "rebase/revalidation" in prompt


@pytest.mark.unit
def test_empty_coordination_warnings_do_not_change_prompt_shape() -> None:
    plan = Path("docs/awf-plans/ws_empty.md")

    assert build_agent_task_prompt(task_prompt="Add metrics") == "Add metrics"
    assert build_planning_prompt(
        task_prompt="Add metrics",
        plan_path=plan,
    ) == build_planning_prompt(
        task_prompt="Add metrics",
        plan_path=plan,
        coordination_warnings=(),
    )
    assert build_execution_prompt(
        task_prompt="Add metrics",
        plan_path=plan,
        iteration=0,
        gaps=(),
    ) == build_execution_prompt(
        task_prompt="Add metrics",
        plan_path=plan,
        iteration=0,
        gaps=(),
        coordination_warnings=(),
    )
    assert render_coordination_warning_section(()) == ""


@pytest.mark.unit
def test_coordination_warning_renderer_sanitizes_legacy_warning_shapes() -> None:
    rendered = render_coordination_warning_section(
        (
            "legacy warning",
            {
                "warning_code": 42,
                "message": "",
                "severity": "",
                "blocks_launch": True,
                "workspace_ids": "ws_bad",
                "overlaps": "bad",
                "stale_policy_context": "bad",
            },
            {
                "warning_code": "OWNED_PATH_OVERLAP_RISK",
                "message": "Coordinate around active work.",
                "severity": "advisory",
                "blocks_launch": False,
                "workspace_ids": [],
                "overlaps": [
                    "bad",
                    {"workspace_id": "ws_missing", "existing_path": "src/**"},
                    {
                        "workspace_id": "ws_valid",
                        "existing_path": "src/**",
                        "requested_path": "src/app.py",
                    },
                ],
                "stale_policy_context": {},
            },
        )
    )

    assert "COORDINATION_WARNING (advisory; blocks_launch=true): COORDINATION_WARNING" in rendered
    assert "legacy warning" not in rendered
    assert "OWNED_PATH_OVERLAP_RISK (advisory; blocks_launch=false)" in rendered
    assert "Workspaces:" not in rendered
    assert "ws_valid: src/** -> src/app.py" in rendered
    assert "Stale policy:" not in rendered


@pytest.mark.unit
def test_coordination_warning_renderer_bounds_direct_overlap_payloads() -> None:
    rendered = render_coordination_warning_section(
        (
            {
                "warning_code": "OWNED_PATH_OVERLAP_RISK",
                "message": "Coordinate around active work.",
                "severity": "advisory",
                "overlaps": [
                    {
                        "workspace_id": f"ws_{index}",
                        "existing_path": "src/**",
                        "requested_path": f"src/module_{index}.py",
                    }
                    for index in range(MAX_COORDINATION_WARNING_OVERLAPS + 2)
                ],
            },
        )
    )

    assert rendered.count("  - ws_") == MAX_COORDINATION_WARNING_OVERLAPS
    assert f"ws_{MAX_COORDINATION_WARNING_OVERLAPS - 1}: src/**" in rendered
    assert f"ws_{MAX_COORDINATION_WARNING_OVERLAPS}: src/**" not in rendered


@pytest.mark.unit
def test_conformance_retry_prompt_bounds_text_and_handles_missing_artifacts() -> None:
    prompt = build_conformance_retry_prompt(
        task_prompt="finish the slice",
        evidence={
            "summary": "x" * 1200,
            "gaps": "legacy string gaps are ignored",
        },
    )

    assert "xxx..." in prompt
    assert "- Re-check the saved plan." in prompt
    assert "- Plan artifacts were not recorded." in prompt
    assert "finish the slice" in prompt


@pytest.mark.unit
def test_conformance_failure_evidence_is_structured_and_bounded() -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration","summary":"still missing","gaps":["finish API","run mypy"]}'
    )

    evidence = build_conformance_failure_evidence(
        report=report,
        iterations_used=2,
        max_iterations=2,
        plan_path=Path("docs/awf-plans/ws_123.md"),
        report_path=Path("docs/awf-plans/ws_123.conformance.json"),
    )

    assert evidence == {
        "summary": "still missing",
        "gaps": ["finish API", "run mypy"],
        "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
        "report_reason_code": "PLAN_CONFORMANCE_REPORTED",
        "iterations_used": 2,
        "max_iterations": 2,
        "plan_path": "docs/awf-plans/ws_123.md",
        "report_path": "docs/awf-plans/ws_123.conformance.json",
    }


@pytest.mark.unit
def test_conformance_retry_prompt_steers_agent_to_finish_remaining_gaps() -> None:
    prompt = build_conformance_retry_prompt(
        task_prompt="Implement the billing retry flow.",
        evidence={
            "summary": "Implementation is close but incomplete.",
            "gaps": ["Add regression test", "Wire retry endpoint"],
            "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
            "plan_path": "docs/awf-plans/ws_old.md",
            "report_path": "docs/awf-plans/ws_old.conformance.json",
        },
    )

    assert "Implement the billing retry flow." in prompt
    assert "finish the remaining plan-conformance gaps" in prompt
    assert "- Add regression test" in prompt
    assert "- Wire retry endpoint" in prompt
    assert "Do not restart from scratch" in prompt
