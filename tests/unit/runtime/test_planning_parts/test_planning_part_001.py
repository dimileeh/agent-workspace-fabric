"""Plan/execute/compare lifecycle helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.runtime import planning as planning_mod
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    AGENT_WORKTREE_ROOT,
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    MAX_CONFORMANCE_TEXT_CHARS,
    PLAN_CONFORMANCE_REPORTED,
    PLAN_CONFORMANCE_UNSATISFIED,
    PlanConformanceReport,
    PlanConformanceStatus,
    _gaps_from_payload,
    agent_artifact_path,
    build_agent_task_prompt,
    build_conformance_failure_evidence,
    build_conformance_prompt,
    build_conformance_retry_prompt,
    build_execution_prompt,
    build_planning_prompt,
    build_planning_scope_retry_prompt,
    changed_paths_from_porcelain,
    conformance_requires_awf_validation,
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
    needs_iteration = parse_conformance_report(
        '{"status":"unknown","summary":"","gaps":"rerun mypy"}'
    )
    blank_reason = parse_conformance_report(
        '{"status":"needs_iteration","summary":"x","gaps":[],"reason_code":"  "}'
    )

    assert satisfied.status == PlanConformanceStatus.satisfied
    assert satisfied.summary == "Plan satisfied."
    assert satisfied.reason_code == "PLAN_CONFORMANCE_REPORTED"
    assert needs_iteration.status == PlanConformanceStatus.needs_iteration
    assert needs_iteration.summary == "Plan gaps remain."
    assert needs_iteration.gaps == ("rerun mypy",)
    assert blank_reason.reason_code == "PLAN_CONFORMANCE_REPORTED"


@pytest.mark.unit
def test_parse_conformance_report_defaults_blank_reason_code() -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration","summary":"still checking","gaps":[],"reason_code":"   "}'
    )

    assert report.reason_code == "PLAN_CONFORMANCE_REPORTED"


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
def test_changed_paths_from_porcelain_decodes_quoted_report_paths() -> None:
    paths = changed_paths_from_porcelain(' M "docs/awf plans/report.json"\n')

    assert paths == {Path("docs/awf plans/report.json")}


@pytest.mark.unit
def test_changed_paths_from_porcelain_preserves_quoted_literal_arrow_paths() -> None:
    paths = changed_paths_from_porcelain(' M "docs/awf -> plans/ws.conformance.json"\n')

    assert paths == {Path("docs/awf -> plans/ws.conformance.json")}


@pytest.mark.unit
def test_agent_artifact_path_anchors_relative_path_at_worktree_root() -> None:
    """Worktree-relative artifact paths resolve to the in-container worktree root (#620)."""
    assert AGENT_WORKTREE_ROOT == "/workspace"
    assert (
        agent_artifact_path(Path("docs/awf-plans/ws_123.md")).as_posix()
        == "/workspace/docs/awf-plans/ws_123.md"
    )
    assert (
        agent_artifact_path(Path("docs/awf-plans/ws_123.conformance.json")).as_posix()
        == "/workspace/docs/awf-plans/ws_123.conformance.json"
    )


@pytest.mark.unit
def test_agent_worktree_root_is_coupled_to_compose_exec_default_workdir() -> None:
    """The artifact anchor and the compose-exec start dir share one constant (#620)."""
    from awf.common.compose_exec import DEFAULT_AGENT_WORKDIR, build_tracked_compose_exec

    # Both sides resolve from the same source of truth, so they cannot desync.
    assert AGENT_WORKTREE_ROOT == DEFAULT_AGENT_WORKDIR

    # The agent adapter launches the CLI relying on the default ``workdir``, so the
    # directory the agent actually starts in must equal the artifact-anchor root.
    invocation = build_tracked_compose_exec(
        compose_project="proj",
        compose_file=Path("docker-compose.yml"),
        cli_args=["codex"],
        source="agent",
        label="codex",
    )
    assert invocation.workdir == AGENT_WORKTREE_ROOT


@pytest.mark.unit
def test_prompts_anchor_plan_artifacts_at_worktree_root() -> None:
    """Plan/conformance prompts carry the worktree-root-anchored artifact paths (#620)."""
    plan = agent_artifact_path(Path("docs/awf-plans/ws_123.md"))
    report = agent_artifact_path(Path("docs/awf-plans/ws_123.conformance.json"))

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

    assert "/workspace/docs/awf-plans/ws_123.md" in planning_prompt
    assert "/workspace/docs/awf-plans/ws_123.md" in execution_prompt
    assert "/workspace/docs/awf-plans/ws_123.md" in conformance_prompt
    assert "/workspace/docs/awf-plans/ws_123.conformance.json" in conformance_prompt


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
def test_agent_planning_and_execution_prompts_include_workspace_runtime_context() -> None:
    plan = Path("docs/awf-plans/ws_123.md")
    context = "Workspace runtime context\n- Use `$AWF_TEST_DATABASE_URL` for DB tests."

    agent_prompt = build_agent_task_prompt(
        task_prompt="Add metrics",
        workspace_runtime_context=context,
    )
    planning_prompt = build_planning_prompt(
        task_prompt="Add metrics",
        plan_path=plan,
        workspace_runtime_context=context,
    )
    execution_prompt = build_execution_prompt(
        task_prompt="Add metrics",
        plan_path=plan,
        iteration=0,
        gaps=(),
        workspace_runtime_context=context,
    )

    for prompt in (agent_prompt, planning_prompt, execution_prompt):
        assert "Workspace runtime context" in prompt
        assert "$AWF_TEST_DATABASE_URL" in prompt


@pytest.mark.unit
def test_agent_and_execution_prompts_render_task_tag_commit_guidance() -> None:
    plan = Path("docs/awf-plans/ws_123.md")
    agent_prompt = build_agent_task_prompt(task_prompt="Add metrics", task_tag="PROJ-123")
    execution_prompt = build_execution_prompt(
        task_prompt="Add metrics",
        plan_path=plan,
        iteration=0,
        gaps=(),
        task_tag="PROJ-123",
    )
    for prompt in (agent_prompt, execution_prompt):
        assert "Prefix every commit message with `PROJ-123 `" in prompt


@pytest.mark.unit
def test_prompts_omit_task_tag_guidance_when_absent() -> None:
    plan = Path("docs/awf-plans/ws_123.md")
    # No tag, no other sections → agent prompt is the bare task prompt (no-op).
    assert build_agent_task_prompt(task_prompt="Add metrics") == "Add metrics"
    execution_prompt = build_execution_prompt(
        task_prompt="Add metrics",
        plan_path=plan,
        iteration=0,
        gaps=(),
    )
    assert "Commit message tag" not in execution_prompt


@pytest.mark.unit
def test_conformance_prompt_is_evidence_only_and_does_not_rerun_validation() -> None:
    prompt = build_conformance_prompt(
        task_prompt="Add metrics",
        plan_path=Path("docs/awf-plans/ws_123.md"),
        report_path=Path("docs/awf-plans/ws_123.conformance.json"),
        iteration=0,
    )

    assert "Do not run validation commands" in prompt
    assert "Use existing validation evidence" in prompt
    assert "missing, stale, or insufficient" in prompt
    for command in (
        "pytest",
        "ruff",
        "mypy",
        "coverage",
        "npm",
        "lint",
        "build",
        "git add",
        "git commit",
    ):
        assert command in prompt


@pytest.mark.unit
def test_conformance_prompt_documents_awf_validation_handoff_reason_code() -> None:
    prompt = build_conformance_prompt(
        task_prompt="Add metrics",
        plan_path=Path("docs/awf-plans/ws_123.md"),
        report_path=Path("docs/awf-plans/ws_123.conformance.json"),
        iteration=0,
    )

    assert '"reason_code"' in prompt
    assert CONFORMANCE_REQUIRES_AWF_VALIDATION in prompt
    assert (
        "only when every remaining gap is missing, stale, or insufficient AWF-owned validation evidence"
        in prompt
    )
    assert "Do not use it for implementation, API, plan, or documentation gaps" in prompt


@pytest.mark.unit
def test_conformance_requires_awf_validation_accepts_validation_evidence_only_gap() -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration",'
        '"summary":"Implementation appears complete; AWF validation evidence is missing.",'
        '"reason_code":"CONFORMANCE_REQUIRES_AWF_VALIDATION",'
        '"gaps":["AWF-owned validation evidence is missing for the required pytest gate."]}'
    )

    assert conformance_requires_awf_validation(report)


@pytest.mark.unit
def test_conformance_requires_awf_validation_accepts_hyphenated_reason_code() -> None:
    report = PlanConformanceReport(
        status=PlanConformanceStatus.needs_iteration,
        summary="Implementation appears complete; AWF validation evidence is missing.",
        reason_code="CONFORMANCE-REQUIRES-AWF-VALIDATION",
        gaps=("AWF-owned validation evidence is missing for the required pytest gate.",),
    )

    assert conformance_requires_awf_validation(report)


@pytest.mark.unit
def test_conformance_requires_awf_validation_rejects_empty_gaps_even_with_reason_code() -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration",'
        '"summary":"No explicit gaps were provided.",'
        '"reason_code":"CONFORMANCE_REQUIRES_AWF_VALIDATION",'
        '"gaps":[]}'
    )

    assert not conformance_requires_awf_validation(report)


@pytest.mark.unit
def test_conformance_requires_awf_validation_rejects_satisfied_reports() -> None:
    report = PlanConformanceReport(
        status=PlanConformanceStatus.satisfied,
        summary="Validation evidence is still missing, but status is final.",
        gaps=("AWF validation evidence is missing for the required pytest gate.",),
        reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
    )

    assert not conformance_requires_awf_validation(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        "AWF-owned coverage evidence is stale.",
        "Required profile gate logs are missing from the workspace artifacts.",
        "AWF functional test coverage logs are missing.",
        "AWF unit test suite coverage evidence is missing.",
        "AWF validation test suite evidence is missing.",
        "AWF test runner coverage not found.",
        "AWF validation tests evidence is missing.",
        "Validation test logs are absent.",
        "AWF test provenance is missing.",
        "AWF coverage classification log is missing.",
    ),
)
def test_conformance_requires_awf_validation_accepts_validation_subject_gaps_without_literal_validation(
    gap: str,
) -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration",'
        '"summary":"Implementation appears complete; AWF validation evidence is missing.",'
        '"reason_code":"CONFORMANCE_REQUIRES_AWF_VALIDATION",'
        f'"gaps":["{gap}"]}}'
    )

    assert conformance_requires_awf_validation(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        "Migration validation evidence is missing.",
        "Schema migration validation evidence is missing.",
        "AWF validation evidence is missing for the Alembic/profile migration gate.",
        "Schema migration profile gate logs are missing from the workspace artifacts.",
        "Profile migration gate logs are missing from the workspace artifacts.",
    ),
)
def test_conformance_requires_awf_validation_accepts_migration_gate_evidence_gaps(
    gap: str,
) -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration",'
        '"summary":"Implementation appears complete; AWF migration gate evidence is missing.",'
        '"reason_code":"CONFORMANCE_REQUIRES_AWF_VALIDATION",'
        f'"gaps":["{gap}"]}}'
    )

    assert conformance_requires_awf_validation(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        "AWF validation run code coverage evidence is missing.",
        "AWF-owned validation run code coverage has not been generated.",
        "AWF-owned validation evidence is missing for the required reason_code.",
    ),
)
def test_conformance_requires_awf_validation_accepts_code_coverage_evidence_gaps(
    gap: str,
) -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration",'
        '"summary":"Implementation appears complete; AWF validation evidence is missing.",'
        '"reason_code":"CONFORMANCE_REQUIRES_AWF_VALIDATION",'
        f'"gaps":["{gap}"]}}'
    )

    assert conformance_requires_awf_validation(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        "AWF validation evidence is missing; see docs for profile gate configuration.",
        "AWF validation evidence is missing; see the document for profile gate configuration.",
        "AWF-owned validation evidence is missing, see documented profile gates.",
    ),
)
def test_conformance_requires_awf_validation_accepts_documentation_context_gaps(
    gap: str,
) -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration",'
        '"summary":"Implementation appears complete; AWF validation evidence is missing.",'
        '"reason_code":"CONFORMANCE_REQUIRES_AWF_VALIDATION",'
        f'"gaps":["{gap}"]}}'
    )

    assert conformance_requires_awf_validation(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        "Run pytest under AWF validation.",
        "rerun pytest under AWF.",
        "AWF validation needs pytest to run.",
        "AWF validation has to run pytest.",
    ),
)
def test_conformance_requires_awf_validation_accepts_named_validation_command_handoff_gaps(
    gap: str,
) -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration",'
        '"summary":"Implementation appears complete; AWF validation command is needed.",'
        '"reason_code":"CONFORMANCE_REQUIRES_AWF_VALIDATION",'
        f'"gaps":["{gap}"]}}'
    )

    assert conformance_requires_awf_validation(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        "Run pytest under AWF validation and wire the API endpoint required by the plan.",
        "Rerun mypy under validation after you implement the endpoint.",
    ),
)
def test_conformance_requires_awf_validation_rejects_mixed_named_command_handoff_gaps(
    gap: str,
) -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration",'
        '"summary":"Validation command handoff and implementation work remain.",'
        '"reason_code":"CONFORMANCE_REQUIRES_AWF_VALIDATION",'
        f'"gaps":["{gap}"]}}'
    )

    assert not conformance_requires_awf_validation(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        "No AWF-owned validation evidence is present for the required commands in "
        "this conformance phase; please run during validation: uv run --python 3.12 "
        "--extra dev pytest tests/integration/test_local_service_compose.py "
        "tests/unit/service/test_config.py tests/unit/service/test_doctor.py -q; "
        "uv run --python 3.12 --extra dev ruff check src/awf "
        "tests/integration/test_local_service_compose.py "
        "tests/unit/service/test_config.py tests/unit/service/test_doctor.py; "
        "uv run --python 3.12 --extra dev mypy src/awf",
        "Run and record the required validation commands for this plan: "
        "`uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py -q`; "
        "`uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py "
        "tests/unit/cli/test_cli.py`; "
        "`uv run --python 3.12 --extra dev mypy src/awf`.",
        "No AWF-owned validation evidence is present; please run during validation: "
        "uv run --python 3.12 --extra dev ruff check tests src/awf; "
        "uv run --python 3.12 --extra dev mypy src/awf.",
    ),
)
def test_conformance_requires_awf_validation_accepts_named_command_handoff_with_paths(
    gap: str,
) -> None:
    report = PlanConformanceReport(
        status=PlanConformanceStatus.needs_iteration,
        summary="Implementation is complete; AWF validation evidence is missing.",
        reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        gaps=(gap,),
    )

    assert conformance_requires_awf_validation(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        "Run lint during AWF validation: lint tests.",
        "Run build during AWF validation: build tests.",
        "Run coverage during AWF validation: coverage tests.",
    ),
)
def test_conformance_requires_awf_validation_accepts_named_validation_tools_targeting_tests(
    gap: str,
) -> None:
    report = PlanConformanceReport(
        status=PlanConformanceStatus.needs_iteration,
        summary="Implementation is complete; AWF validation evidence is missing.",
        reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        gaps=(gap,),
    )

    assert conformance_requires_awf_validation(report)


@pytest.mark.unit
def test_conformance_requires_awf_validation_accepts_not_run_evidence_with_test_command_path() -> (
    None
):
    report = PlanConformanceReport(
        status=PlanConformanceStatus.needs_iteration,
        summary="Implementation is complete; AWF validation evidence is missing.",
        reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        gaps=(
            "AWF validation evidence is missing; pytest has not run for tests/unit/test_cli.py.",
        ),
    )

    assert conformance_requires_awf_validation(report)


@pytest.mark.unit
def test_conformance_requires_awf_validation_rejects_not_run_evidence_with_missing_test_path() -> (
    None
):
    report = PlanConformanceReport(
        status=PlanConformanceStatus.needs_iteration,
        summary="Validation evidence and test work remain.",
        reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        gaps=(
            "AWF validation evidence is missing; pytest has not run because "
            "tests/unit/test_cli.py is missing.",
        ),
    )

    assert not conformance_requires_awf_validation(report)


@pytest.mark.unit
def test_conformance_requires_awf_validation_rejects_not_run_evidence_with_test_work() -> None:
    report = PlanConformanceReport(
        status=PlanConformanceStatus.needs_iteration,
        summary="Validation evidence and test work remain.",
        reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        gaps=(
            "AWF validation evidence is missing; pytest has not run and the feature needs tests.",
        ),
    )

    assert not conformance_requires_awf_validation(report)


@pytest.mark.unit
def test_conformance_requires_awf_validation_rejects_not_run_evidence_with_test_path_work() -> None:
    report = PlanConformanceReport(
        status=PlanConformanceStatus.needs_iteration,
        summary="Validation evidence and test work remain.",
        reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        gaps=(
            "AWF validation evidence is missing; pytest has not run after updating "
            "tests/unit/test_cli.py.",
        ),
    )

    assert not conformance_requires_awf_validation(report)


@pytest.mark.unit
def test_conformance_requires_awf_validation_accepts_saved_plan_scoped_check_handoff() -> None:
    gap = (
        "Missing focused-check evidence from the saved plan for scoped validation "
        "commands: `uv run --python 3.12 --extra dev pytest "
        "tests/unit/runtime/test_planning_parts/test_planning_part_001.py -q`; "
        "`uv run --python 3.12 --extra dev ruff check src/awf tests`; "
        "`uv run --python 3.12 --extra dev mypy src/awf`; `git diff --check`. "
        "The AWF validation phase must run and record scoped checks."
    )
    report = PlanConformanceReport(
        status=PlanConformanceStatus.needs_iteration,
        summary="Implementation is complete; only AWF-owned validation evidence is missing.",
        reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        gaps=(gap,),
    )

    assert conformance_requires_awf_validation(report)


@pytest.mark.unit
def test_saved_plan_command_snippets_do_not_count_as_handoff() -> None:
    gap = (
        "Missing focused-check evidence from the saved plan for scoped validation "
        "commands: `uv run --python 3.12 --extra dev pytest "
        "tests/unit/runtime/test_planning_parts/test_planning_part_001.py -q`; "
        "`uv run --python 3.12 --extra dev ruff check src/awf tests`; "
        "`uv run --python 3.12 --extra dev mypy src/awf`; `git diff --check`."
    )
    report = PlanConformanceReport(
        status=PlanConformanceStatus.needs_iteration,
        summary="Implementation is complete; only saved-plan evidence is missing.",
        reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        gaps=(gap,),
    )

    assert not conformance_requires_awf_validation(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        (
            "Saved plan evidence is missing; add scoped checks to the saved plan. "
            "The AWF validation phase must run pytest and record scoped checks."
        ),
        (
            "Saved plan evidence is missing; include scoped checks in the saved plan. "
            "The AWF validation phase must run pytest and record scoped checks."
        ),
        (
            "Focused checks are missing in the saved plan. "
            "The AWF validation phase must run pytest and record scoped checks."
        ),
    ),
)
def test_saved_plan_scoped_check_handoff_rejects_saved_plan_edits(gap: str) -> None:
    report = PlanConformanceReport(
        status=PlanConformanceStatus.needs_iteration,
        summary="Implementation is complete, but saved-plan artifact work remains.",
        reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        gaps=(gap,),
    )

    assert not conformance_requires_awf_validation(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        "Run pytest during AWF validation and add tests/unit/runtime/test_planning.py.",
        "Run pytest during AWF validation and add assertions in tests/unit/runtime/test_planning.py.",
        "Rerun pytest under validation after updating tests/unit/test_widget.py.",
        "Run pytest in validation, then create test/unit/test_cli.py for the new case.",
        "Run pytest during AWF validation and add src/tests/unit/test_widget.py.",
        "Rerun pytest under validation after updating ./tests/unit/test_widget.py.",
        "Run pytest during AWF validation and add validation tests.",
        "Run pytest during AWF validation and the feature needs tests.",
        "AWF validation evidence is missing; git diff --check has not run and "
        "tests/unit/test_cli.py needs assertions.",
    ),
)
def test_conformance_requires_awf_validation_rejects_mixed_named_command_test_path_work_gaps(
    gap: str,
) -> None:
    report = PlanConformanceReport(
        status=PlanConformanceStatus.needs_iteration,
        summary="Validation command handoff and test work remain.",
        reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        gaps=(gap,),
    )

    assert not conformance_requires_awf_validation(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        "Add tests/unit/test_widget.py to cover the new case.",
        "Update assertions in tests/unit/test_widget.py after the refactor.",
    ),
)
def test_test_path_work_context_accepts_sentence_case_work_verbs(gap: str) -> None:
    assert planning_mod._has_test_path_work_context(gap, gap.index("tests/"))


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        "AWF test suite, coverage report is missing.",
        "AWF test run: coverage evidence is absent.",
    ),
)
def test_conformance_requires_awf_validation_accepts_punctuated_test_evidence_gaps(
    gap: str,
) -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration",'
        '"summary":"Implementation appears complete; AWF validation evidence is missing.",'
        '"reason_code":"CONFORMANCE_REQUIRES_AWF_VALIDATION",'
        f'"gaps":["{gap}"]}}'
    )

    assert conformance_requires_awf_validation(report)


@pytest.mark.unit
def test_conformance_requires_awf_validation_rejects_mixed_or_ordinary_gaps() -> None:
    mixed = parse_conformance_report(
        '{"status":"needs_iteration",'
        '"summary":"Validation evidence is missing and the API is incomplete.",'
        '"reason_code":"CONFORMANCE_REQUIRES_AWF_VALIDATION",'
        '"gaps":["AWF-owned validation evidence is missing.",'
        '"Wire the API endpoint required by the plan."]}'
    )
    ordinary = parse_conformance_report(
        '{"status":"needs_iteration",'
        '"summary":"Implementation gap remains.",'
        '"gaps":["Wire the API endpoint required by the plan."]}'
    )

    assert not conformance_requires_awf_validation(mixed)
    assert not conformance_requires_awf_validation(ordinary)


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        "Missing approval artifact for the handoff.",
        "Schema evidence is missing for the generated artifact.",
        "AWF validation evidence is missing for code review logs.",
    ),
)
def test_conformance_requires_awf_validation_rejects_non_validation_artifact_gaps(
    gap: str,
) -> None:
    report = PlanConformanceReport(
        status=PlanConformanceStatus.needs_iteration,
        summary="Implementation appears complete.",
        gaps=(gap,),
        reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
    )

    assert not conformance_requires_awf_validation(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        "Document the validation provenance behavior before the gap is satisfied.",
        "Update docs for the validation profile gate before the gap is satisfied.",
    ),
)
def test_conformance_requires_awf_validation_rejects_documentation_work_gaps(
    gap: str,
) -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration",'
        '"summary":"Documentation work remains.",'
        '"reason_code":"CONFORMANCE_REQUIRES_AWF_VALIDATION",'
        f'"gaps":["{gap}"]}}'
    )

    assert not conformance_requires_awf_validation(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        "AWF validation evidence is missing from the saved plan.",
        "AWF validation evidence is missing from the README documentation.",
    ),
)
def test_conformance_requires_awf_validation_rejects_plan_and_documentation_artifact_gaps(
    gap: str,
) -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration",'
        '"summary":"Plan or documentation artifact work remains.",'
        '"reason_code":"CONFORMANCE_REQUIRES_AWF_VALIDATION",'
        f'"gaps":["{gap}"]}}'
    )

    assert not conformance_requires_awf_validation(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        "Migration implementation is missing validation evidence.",
        "Migration profile gate implementation is missing.",
        "Migration validation logic is missing.",
    ),
)
def test_conformance_requires_awf_validation_rejects_migration_implementation_gaps(
    gap: str,
) -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration",'
        '"summary":"Migration implementation work remains.",'
        '"reason_code":"CONFORMANCE_REQUIRES_AWF_VALIDATION",'
        f'"gaps":["{gap}"]}}'
    )

    assert not conformance_requires_awf_validation(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    "gap",
    (
        "Missing validation tests for the new parser.",
        "Add validation tests for the new parser.",
        "Write parser tests covering invalid input.",
    ),
)
def test_conformance_requires_awf_validation_rejects_test_work_gaps(
    gap: str,
) -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration",'
        '"summary":"Test work remains.",'
        '"reason_code":"CONFORMANCE_REQUIRES_AWF_VALIDATION",'
        f'"gaps":["{gap}"]}}'
    )

    assert not conformance_requires_awf_validation(report)


@pytest.mark.unit
def test_planning_prompt_is_plan_artifact_only_and_stops_before_implementation() -> None:
    plan = Path("docs/awf-plans/ws_scope_prompt.md")

    planning_prompt = build_planning_prompt(
        task_prompt="Implement planning scope controls.",
        plan_path=plan,
    )

    assert AGENT_PLAN_PHASE_SCOPE_VIOLATION == "AGENT_PLAN_PHASE_SCOPE_VIOLATION"
    assert (
        "Create or update only the configured plan artifact "
        "`docs/awf-plans/ws_scope_prompt.md`" in planning_prompt
    )
    assert "Do not create, edit, delete, stage, or commit any other files" in planning_prompt
    for term in ("source", "tests", "docs", "config", "migrations", "lockfiles"):
        assert term in planning_prompt
    assert "Do not run implementation commands" in planning_prompt
    for command in (
        "apply_patch",
        "pytest",
        "ruff",
        "mypy",
        "npm",
        "build",
        "git add",
        "git commit",
    ):
        assert command in planning_prompt
    assert "After writing the plan, stop" in planning_prompt


@pytest.mark.unit
def test_planning_scope_retry_prompt_discards_premature_implementation() -> None:
    prompt = build_planning_scope_retry_prompt(
        task_prompt="Add the feature after planning.",
        evidence={
            "required_paths": ["docs/awf-plans/ws_retry.md"],
            "offending_paths": ["src/awf/runtime/planning.py", "tests/unit/test_planning.py"],
        },
    )

    assert "Discard the premature implementation from the failed planning attempt" in prompt
    assert "Rerun planning against the configured plan artifact" in prompt
    assert "Prior source required plan paths from the failed planning attempt" in prompt
    assert "- `docs/awf-plans/ws_retry.md`" in prompt
    assert "Create or update only `docs/awf-plans/ws_retry.md`" not in prompt
    assert "Aside from creating or updating the configured plan artifact" in prompt
    assert "during this retry planning phase" in prompt
    assert "phase-scoped" in prompt
    assert "or any other file during this retry planning phase" not in prompt
    assert "src/awf/runtime/planning.py" in prompt
    assert "tests/unit/test_planning.py" in prompt
    assert "After writing the plan, stop" not in prompt
    assert "Add the feature after planning." in prompt


@pytest.mark.unit
def test_composed_planning_scope_retry_prompt_has_one_authoritative_plan_artifact() -> None:
    retry_task_prompt = build_planning_scope_retry_prompt(
        task_prompt="Add the feature after planning.",
        evidence={
            "required_paths": ["docs/awf-plans/ws_scope_old.md"],
            "offending_paths": ["src/awf/runtime/planning.py"],
        },
    )

    composed_prompt = build_planning_prompt(
        task_prompt=retry_task_prompt,
        plan_path=Path("docs/awf-plans/ws_scope_new.md"),
    )

    assert (
        "Create or update only the configured plan artifact "
        "`docs/awf-plans/ws_scope_new.md`" in composed_prompt
    )
    assert "After writing the plan, stop. Do not perform implementation work in this phase." in (
        composed_prompt
    )
    assert composed_prompt.count("Create or update only") == 1
    assert composed_prompt.count("After writing the plan, stop") == 1
    assert "Create or update only `docs/awf-plans/ws_scope_old.md`" not in composed_prompt
    assert "Prior source required plan paths from the failed planning attempt" in composed_prompt
    assert "- `docs/awf-plans/ws_scope_old.md`" in composed_prompt


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
def test_coordination_warning_renderer_requires_bool_blocks_launch() -> None:
    rendered = render_coordination_warning_section(
        (
            {
                "warning_code": "OWNED_PATH_OVERLAP_RISK",
                "message": "Coordinate around active work.",
                "severity": "advisory",
                "blocks_launch": "yes",
            },
        )
    )

    assert "OWNED_PATH_OVERLAP_RISK (advisory; blocks_launch=false)" in rendered


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
def test_coordination_warning_renderer_surfaces_truncated_overlap_count() -> None:
    rendered = render_coordination_warning_section(
        (
            {
                "warning_code": "OWNED_PATH_OVERLAP_RISK",
                "message": "Coordinate around active work.",
                "severity": "advisory",
                "overlap_count": 5,
                "overlaps_truncated": True,
                "overlaps": [
                    {
                        "workspace_id": "ws_one",
                        "existing_path": "src/awf/**",
                        "requested_path": "src/awf/runtime/planning.py",
                    },
                    {
                        "workspace_id": "ws_two",
                        "existing_path": "tests/**",
                        "requested_path": "tests/unit/runtime/test_planning.py",
                    },
                ],
            },
        )
    )

    assert "Overlap list truncated: showing 2 of 5 total overlaps." in rendered


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


@pytest.mark.unit
def test_conformance_retry_prompt_defaults_when_evidence_omits_artifacts_and_gaps() -> None:
    prompt = build_conformance_retry_prompt(
        task_prompt="Finish the lease mount slice.",
        evidence={"summary": ""},
    )

    assert "Plan conformance was not satisfied." in prompt
    assert "- Re-check the saved plan." in prompt
    assert "- Plan artifacts were not recorded." in prompt


@pytest.mark.unit
def test_conformance_retry_prompt_truncates_long_evidence_text() -> None:
    prompt = build_conformance_retry_prompt(
        task_prompt="Finish the lease mount slice.",
        evidence={
            "summary": "s" * 6000,
            "gaps": ["g" * 6000],
        },
    )

    assert ("s" * 997) + "..." in prompt
    assert ("g" * 997) + "..." in prompt


@pytest.mark.unit
def test_conformance_retry_prompt_handles_missing_and_oversized_evidence() -> None:
    long_summary = "x" * (MAX_CONFORMANCE_TEXT_CHARS + 50)
    prompt = build_conformance_retry_prompt(
        task_prompt="Finish endpoint metadata coverage.",
        evidence={
            "summary": long_summary,
            "gaps": "not-a-list",
            "plan_path": "",
            "report_path": None,
        },
    )

    assert ("x" * (MAX_CONFORMANCE_TEXT_CHARS - 3) + "...") in prompt
    assert long_summary not in prompt
    assert "- Re-check the saved plan." in prompt
    assert "- Plan artifacts were not recorded." in prompt


@pytest.mark.unit
def test_conformance_report_defaults_blank_reason_code() -> None:
    report = parse_conformance_report(
        '{"status":"needs_iteration","summary":"done","gaps":[],"reason_code":"   "}'
    )

    assert report.reason_code == PLAN_CONFORMANCE_REPORTED
    assert report.summary == "done"
