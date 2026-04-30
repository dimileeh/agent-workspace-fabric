"""Focused branch-coverage tests for executor helper behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import awf.control.executor as executor_mod
from awf.adapters.base import AgentDefaults
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
    _agent_defaults_for_workspace,
    _agent_model_for_workspace,
    _agent_pr_identity,
    _apply_baseline_coverage_ratchet,
    _call_pr_monitor_factory,
    _coverage_preserves_below_threshold_baseline,
    _failure_reason_for_phase,
    _failure_salvage_payload,
    _get_active_recovery_payload,
    _MonitorRebaseRecoveryError,
    _profile_with_planning_iteration_default,
    _raw_profile_has_explicit_planning_max_iterations,
    _read_text_if_present,
    _validation_failure_message,
    _validation_run_command_records,
    _validation_run_coverage_metadata,
    _validation_run_log_stream_refs,
    _validation_run_reason_code,
    _validation_tier_for_workspace,
)
from awf.db.enums import FailureReason, OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.profiles.models import ProfilePlanning, WorkspaceProfile
from awf.runtime.planning import PLAN_CONFORMANCE_UNSATISFIED
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
)


def _command_result(tmp_path: Path, *, returncode: int = 1) -> ValidationCommandResult:
    stdout = tmp_path / "cmd.stdout"
    stderr = tmp_path / "cmd.stderr"
    stdout.write_text("stdout", encoding="utf-8")
    stderr.write_text("stderr", encoding="utf-8")
    return ValidationCommandResult(
        command="pytest --cov",
        returncode=returncode,
        duration_seconds=0.1,
        stdout_path=stdout,
        stderr_path=stderr,
        phase="coverage",
        reason_code="COVERAGE_BELOW_THRESHOLD",
        policy_failed=returncode != 0,
    )


def _coverage(
    tmp_path: Path,
    *,
    percent: float | None,
    minimum: float = 99,
    reason_code: str = "COVERAGE_BELOW_THRESHOLD",
    status: str = "failed",
    command_result: ValidationCommandResult | None = None,
) -> ValidationCoverageResult:
    return ValidationCoverageResult(
        provider="python",
        percent=percent,
        minimum_percent=minimum,
        enforce=True,
        status=status,
        reason_code=reason_code,
        command_result=command_result if command_result is not None else _command_result(tmp_path),
    )


class _PlanningAdapter:
    def __init__(self, *stdout_values: str) -> None:
        self.stdout_values = list(stdout_values)
        self.prompts: list[str] = []

    async def run(self, **kwargs: object) -> SimpleNamespace:
        prompt = kwargs.get("prompt")
        assert isinstance(prompt, str)
        self.prompts.append(prompt)
        stdout = self.stdout_values.pop(0) if self.stdout_values else ""
        return SimpleNamespace(stdout=stdout)


class _CoverageValidation:
    def __init__(self, coverage: ValidationCoverageResult | None) -> None:
        self.coverage = coverage
        self.calls: list[str] = []

    async def run_profile_coverage(self, *, phase: str, **_kwargs: object) -> ValidationCoverageResult | None:
        self.calls.append(phase)
        return self.coverage


def _coordination_task_policy() -> dict[str, object]:
    return {
        "coordination": {
            "warnings": [
                {
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
            ]
        }
    }


def _executor_with_runner(
    runner: FakeCommandRunner,
    tmp_path: Path,
    *,
    validation: object | None = None,
) -> WorkspaceExecutor:
    return WorkspaceExecutor(
        session_factory=object(),  # type: ignore[arg-type]
        runner=runner,
        compose=object(),  # type: ignore[arg-type]
        validation=validation or object(),  # type: ignore[arg-type]
        pr_creator=object(),  # type: ignore[arg-type]
        config=ExecutorConfig(
            worktrees_root=tmp_path / "worktrees",
            compose_projects_root=tmp_path / "compose",
        ),
    )


@pytest.mark.unit
def test_failure_reason_for_phase_maps_setup_timeout_and_healthcheck() -> None:
    assert (
        _failure_reason_for_phase(SimpleNamespace(phase="healthcheck", reason_code="COMMAND_FAILED"))
        == FailureReason.health_check_failure
    )
    assert (
        _failure_reason_for_phase(SimpleNamespace(phase="validate", reason_code="PHASE_TIMEOUT"))
        == FailureReason.phase_timeout
    )
    assert (
        _failure_reason_for_phase(SimpleNamespace(phase="pre_agent", reason_code="COMMAND_FAILED"))
        == FailureReason.service_startup_failure
    )
    assert _failure_reason_for_phase(None) == FailureReason.validation_failure


@pytest.mark.unit
def test_executor_small_helpers_handle_absent_optional_metadata(tmp_path: Path) -> None:
    assert _raw_profile_has_explicit_planning_max_iterations(None) is False
    assert _raw_profile_has_explicit_planning_max_iterations({"planning": []}) is False

    salvage = _failure_salvage_payload(  # type: ignore[arg-type]
        SimpleNamespace(branch_name=None, remote_push_branch=None),
        worktree_path=tmp_path / "worktree",
    )

    assert salvage == {
        "hint": "Workspace worktree and branch were preserved for salvage.",
        "worktree_path": str(tmp_path / "worktree"),
    }


@pytest.mark.unit
def test_validation_run_log_stream_refs_preserve_only_string_stream_ids() -> None:
    refs = _validation_run_log_stream_refs(
        [
            {"stream_ids": {"stdout": "validation.01.stdout", "stderr": 123}},
            {"stream_ids": "not-a-dict"},
            {},
        ]
    )

    assert refs == {
        "commands": [
            {"stdout": "validation.01.stdout", "stderr": None},
            {"stdout": None, "stderr": None},
            {"stdout": None, "stderr": None},
        ]
    }


@pytest.mark.unit
def test_validation_run_command_records_include_healthchecks_and_coverage() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records",
            "phases": {
                "post_agent": ["ruff format --check"],
                "validate": ["pytest -q"],
            },
            "validation": {
                "healthchecks": [{"name": "api", "command": "curl -fsS localhost/health"}],
                "coverage": {"command": "pytest --cov=awf --cov-report=term"},
            },
        }
    )

    records = _validation_run_command_records(
        profile=profile,
        phase_names=("post_agent", "validate"),
        run_healthchecks=True,
    )

    assert [(record["phase"], record["command_index"]) for record in records] == [
        ("healthcheck", 1),
        ("post_agent", 1),
        ("validate", 1),
        ("coverage", 1),
    ]
    assert records[-1]["stream_ids"] == {
        "stdout": "validation.01_coverage.stdout",
        "stderr": "validation.01_coverage.stderr",
    }


@pytest.mark.unit
def test_validation_run_command_records_include_http_healthcheck_display() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records-http-healthcheck",
            "phases": {"validate": ["pytest -q"]},
            "validation": {
                "healthchecks": [
                    {
                        "name": "api",
                        "url": "http://api:8080/healthz",
                        "expected_status": 204,
                    }
                ]
            },
        }
    )

    records = _validation_run_command_records(
        profile=profile,
        phase_names=("validate",),
        run_healthchecks=True,
    )

    assert records[0] == {
        "phase": "healthcheck",
        "command_index": 1,
        "command": "GET http://api:8080/healthz expected 204",
        "healthcheck_name": "api",
        "healthcheck_kind": "http",
        "target": "http://api:8080/healthz",
        "stream_ids": {
            "stdout": "validation.01_healthcheck.stdout",
            "stderr": "validation.01_healthcheck.stderr",
        },
    }


@pytest.mark.unit
def test_validation_run_command_records_can_skip_healthchecks_and_coverage() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records-no-healthchecks",
            "phases": {
                "setup": ["uv sync"],
                "validate": ["pytest -q"],
            },
            "validation": {
                "healthchecks": [{"name": "api", "command": "curl -fsS localhost/health"}],
                "coverage": {"command": "pytest --cov=awf --cov-report=term"},
            },
        }
    )

    records = _validation_run_command_records(
        profile=profile,
        phase_names=("setup",),
        run_healthchecks=False,
    )

    assert [(record["phase"], record["command"]) for record in records] == [("setup", "uv sync")]


@pytest.mark.unit
def test_validation_tier_for_workspace_uses_task_class_floor() -> None:
    profile = WorkspaceProfile.model_validate(
        {"name": "tier", "validation": {"requested_tier": 1}}
    )

    assert (
        _validation_tier_for_workspace(
            SimpleNamespace(task_class=TaskClass.migration_task.value),  # type: ignore[arg-type]
            profile,
        )
        == 3
    )
    assert (
        _validation_tier_for_workspace(
            SimpleNamespace(task_class=TaskClass.refactor_task.value),  # type: ignore[arg-type]
            profile,
        )
        == 2
    )
    assert (
        _validation_tier_for_workspace(
            SimpleNamespace(task_class=None),  # type: ignore[arg-type]
            profile,
        )
        == 1
    )


@pytest.mark.unit
async def test_baseline_coverage_preflight_returns_logged_policy_result(
    tmp_path: Path,
) -> None:
    baseline = _coverage(tmp_path, percent=88, minimum=99)
    validation = _CoverageValidation(baseline)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path, validation=validation)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "coverage-preflight",
            "validation": {
                "coverage": {
                    "minimum_percent": 99,
                    "enforce": True,
                    "command": "pytest --cov=awf",
                }
            },
        }
    )

    result = await executor._run_baseline_coverage_preflight(
        workspace_id="ws_preflight",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
    )

    assert result is baseline
    assert validation.calls == ["baseline_coverage"]


@pytest.mark.unit
async def test_baseline_coverage_preflight_returns_passing_policy_result(
    tmp_path: Path,
) -> None:
    baseline = _coverage(tmp_path, percent=100, minimum=99, status="passed")
    validation = _CoverageValidation(baseline)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path, validation=validation)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "coverage-preflight-passing",
            "validation": {
                "coverage": {
                    "minimum_percent": 99,
                    "enforce": True,
                    "command": "pytest --cov=awf",
                }
            },
        }
    )

    result = await executor._run_baseline_coverage_preflight(
        workspace_id="ws_preflight_ok",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
    )

    assert result is baseline
    assert validation.calls == ["baseline_coverage"]


@pytest.mark.unit
async def test_planning_required_prompts_include_coordination_warning(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_coord_plan.md\n",
    )  # dirty after planning
    runner.queue_result(returncode=0, stdout="")  # committed paths since baseline
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_coord_plan.md\n")  # before compare
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_coord_plan.md\n")  # after compare
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan",
        "implementation",
        '{"status":"satisfied","summary":"done","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-coordination",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(
            id="ws_coord_plan",
            task_prompt="do overlapping work",
            task_policy=_coordination_task_policy(),
        ),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None
    assert len(adapter.prompts) == 3
    assert "Coordination warnings" in adapter.prompts[0]
    assert "Coordination warnings" in adapter.prompts[1]
    assert "OWNED_PATH_OVERLAP_RISK" in adapter.prompts[0]
    assert "ws_existing" in adapter.prompts[1]
    assert "STALE_OVERLAP" in adapter.prompts[1]


@pytest.mark.unit
async def test_planning_disabled_direct_prompt_includes_coordination_warning(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    adapter = _PlanningAdapter("done")
    profile = WorkspaceProfile.model_validate({"name": "direct-coordination"})

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(
            id="ws_coord_direct",
            task_prompt="do overlapping work",
            task_policy=_coordination_task_policy(),
        ),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None
    assert len(adapter.prompts) == 1
    assert adapter.prompts[0] != "do overlapping work"
    assert "Coordination warnings" in adapter.prompts[0]
    assert "OWNED_PATH_OVERLAP_RISK" in adapter.prompts[0]
    assert "src/awf/service/** -> src/awf/service/workspaces.py" in adapter.prompts[0]
    assert "do overlapping work" in adapter.prompts[0]


@pytest.mark.unit
async def test_planning_required_fails_when_plan_file_is_not_changed(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")
    runner.queue_result(returncode=0, stdout="sha1\n")
    runner.queue_result(returncode=0, stdout="")
    runner.queue_result(returncode=0, stdout="")
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter("plan written elsewhere")
    profile = WorkspaceProfile.model_validate(
        {"name": "planning-missing", "planning": {"required": True}}
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_plan_missing", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message == (
        "planning phase did not create or modify required plan file "
        "`docs/awf-plans/ws_plan_missing.md`"
    )
    assert len(adapter.prompts) == 1


@pytest.mark.unit
async def test_planning_required_reports_invalid_rendered_paths(tmp_path: Path) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    adapter = _PlanningAdapter()
    profile = WorkspaceProfile.model_construct(
        name="planning-invalid-path",
        planning=ProfilePlanning.model_construct(
            required=True,
            plan_path="/tmp/{workspace_id}.md",
            conformance_report_path="docs/awf-plans/{workspace_id}.json",
            max_iterations=0,
            enforce_plan_only_changes=True,
            fail_on_unexplained_deviation=True,
        ),
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_bad_path", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is not None
    assert message.startswith("planning profile is invalid:")
    assert adapter.prompts == []


@pytest.mark.unit
async def test_planning_required_rejects_extra_plan_phase_changes(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_plan_extra.md\n?? src/changed.py\n",
    )  # dirty_paths
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter("plan plus code")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-extra",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_plan_extra", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message == (
        "planning phase changed files outside `docs/awf-plans/ws_plan_extra.md`: "
        "src/changed.py"
    )


@pytest.mark.unit
async def test_planning_required_allows_extra_plan_changes_when_policy_disabled(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_plan_unenforced.md\n?? src/changed.py\n",
    )
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_plan_unenforced.md\n?? src/changed.py\n",
    )
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_plan_unenforced.md\n?? src/changed.py\n",
    )
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan plus code",
        "implementation",
        '{"status":"satisfied","summary":"done","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-extra-unenforced",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "enforce_plan_only_changes": False,
                "max_iterations": 0,
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_plan_unenforced", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None


@pytest.mark.unit
async def test_conformance_phase_rejects_extra_report_phase_changes(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan (1)
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD (2)
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_compare.md\n")  # dirty after plan (3)
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since (empty) (4)
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_compare.md\n")  # before_compare (5)
    runner.queue_result(
        returncode=0,
        stdout=(
            "?? docs/awf-plans/ws_compare.md\n"
            "?? docs/awf-plans/ws_compare.json\n"
            "?? src/side_effect.py\n"
        ),
    )  # after_compare (6)
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan",
        "implementation",
        '{"status":"satisfied","summary":"done","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-conformance-extra",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_compare", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message == (
        "conformance phase changed files outside `docs/awf-plans/ws_compare.json`: "
        "src/side_effect.py"
    )


@pytest.mark.unit
async def test_conformance_phase_allows_side_effects_when_deviation_policy_disabled(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_compare_unenforced.md\n")
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_compare_unenforced.md\n")
    runner.queue_result(
        returncode=0,
        stdout=(
            "?? docs/awf-plans/ws_compare_unenforced.md\n"
            "?? docs/awf-plans/ws_compare_unenforced.json\n"
            "?? src/side_effect.py\n"
        ),
    )
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan",
        "implementation",
        '{"status":"satisfied","summary":"done","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-conformance-unenforced",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "fail_on_unexplained_deviation": False,
                "max_iterations": 0,
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_compare_unenforced", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None


@pytest.mark.unit
async def test_planning_required_reports_unsatisfied_conformance_after_iterations(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_unsat.md\n")  # dirty after plan
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_unsat.md\n")  # before_compare
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_unsat.md\n")  # after_compare (first)
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_unsat.md\n?? docs/awf-plans/ws_unsat.json\n",
    )  # after_compare (second)
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan",
        "implementation",
        '{"status":"needs_iteration","summary":"more tests needed","gaps":["gap one"]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-unsatisfied",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
            },
        }
    )

    failure = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_unsat", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert failure is not None
    assert not isinstance(failure, str)
    assert failure.message == "plan conformance was not satisfied after 0 iteration(s): gap one"
    assert failure.reason_code == PLAN_CONFORMANCE_UNSATISFIED
    assert failure.details["conformance"] == {
        "summary": "more tests needed",
        "gaps": ["gap one"],
        "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
        "report_reason_code": "PLAN_CONFORMANCE_REPORTED",
        "iterations_used": 1,
        "max_iterations": 0,
        "plan_path": "docs/awf-plans/ws_unsat.md",
        "report_path": "docs/awf-plans/ws_unsat.json",
    }


@pytest.mark.unit
def test_planning_iteration_settings_default_applies_only_when_profile_omits_value() -> None:
    omitted = WorkspaceProfile.model_validate(
        {"name": "planning-default", "planning": {"required": True}}
    )
    explicit = WorkspaceProfile.model_validate(
        {
            "name": "planning-explicit",
            "planning": {"required": True, "max_iterations": 1},
        }
    )

    assert (
        _profile_with_planning_iteration_default(omitted, 4).planning.max_iterations
        == 4
    )
    assert (
        _profile_with_planning_iteration_default(explicit, 4).planning.max_iterations
        == 1
    )


@pytest.mark.unit
async def test_changed_paths_raises_when_git_status_fails(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=128, stderr="not a git repository")
    executor = _executor_with_runner(runner, tmp_path)

    with pytest.raises(RuntimeError, match="git status failed"):
        await executor._changed_paths(tmp_path / "worktree")


@pytest.mark.unit
async def test_committed_paths_since_raises_when_git_diff_fails(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=128, stderr="bad object")
    executor = _executor_with_runner(runner, tmp_path)

    with pytest.raises(RuntimeError, match="git diff --name-only failed"):
        await executor._committed_paths_since(tmp_path / "worktree", "baseline-sha")


@pytest.mark.unit
def test_baseline_coverage_ratchet_accepts_no_regression(tmp_path: Path) -> None:
    command = _command_result(tmp_path, returncode=1)
    result = ValidationResult(
        commands=[command],
        coverage=_coverage(tmp_path, percent=90, command_result=command),
    )
    baseline = _coverage(tmp_path, percent=90, status="failed")

    adjusted = _apply_baseline_coverage_ratchet(result, baseline_coverage=baseline)

    assert adjusted.all_passed
    assert adjusted.coverage is not None
    assert adjusted.coverage.status == "baseline_debt"
    assert adjusted.coverage.reason_code == "COVERAGE_BASELINE_DEBT_NO_REGRESSION"
    assert adjusted.commands[0].returncode == 0
    assert adjusted.commands[0].reason_code == "COVERAGE_BASELINE_DEBT_NO_REGRESSION"


@pytest.mark.unit
def test_baseline_coverage_ratchet_accepts_no_regression_without_command_result(
    tmp_path: Path,
) -> None:
    result = ValidationResult(
        coverage=ValidationCoverageResult(
            provider="python",
            percent=90,
            minimum_percent=99,
            enforce=True,
            status="failed",
            reason_code="COVERAGE_BELOW_THRESHOLD",
            command_result=None,
        ),
    )
    baseline = _coverage(tmp_path, percent=90, status="failed")

    adjusted = _apply_baseline_coverage_ratchet(result, baseline_coverage=baseline)

    assert adjusted.all_passed
    assert adjusted.coverage is not None
    assert adjusted.coverage.command_result is None
    assert adjusted.coverage.reason_code == "COVERAGE_BASELINE_DEBT_NO_REGRESSION"


@pytest.mark.unit
def test_baseline_coverage_ratchet_rejects_missing_or_regressed_measurements(
    tmp_path: Path,
) -> None:
    coverage = _coverage(tmp_path, percent=88)
    baseline = _coverage(tmp_path, percent=90)

    assert not _coverage_preserves_below_threshold_baseline(None, baseline_coverage=baseline)
    assert not _coverage_preserves_below_threshold_baseline(
        _coverage(tmp_path, percent=None),
        baseline_coverage=baseline,
    )
    assert not _coverage_preserves_below_threshold_baseline(
        _coverage(tmp_path, percent=99, status="passed", reason_code="COVERAGE_OK"),
        baseline_coverage=baseline,
    )
    assert not _coverage_preserves_below_threshold_baseline(
        coverage,
        baseline_coverage=_coverage(tmp_path, percent=99, status="passed", reason_code="COVERAGE_OK"),
    )
    assert not _coverage_preserves_below_threshold_baseline(coverage, baseline_coverage=baseline)


@pytest.mark.unit
def test_validation_coverage_metadata_includes_baseline_fields(tmp_path: Path) -> None:
    result = ValidationResult(coverage=_coverage(tmp_path, percent=91, status="reported"))
    baseline = _coverage(tmp_path, percent=None, status="failed", reason_code="COVERAGE_NOT_FOUND")

    metadata = _validation_run_coverage_metadata(result, baseline_coverage=baseline)

    assert metadata is not None
    assert metadata["percent"] == 91.0
    assert metadata["baseline_percent"] is None
    assert metadata["baseline_status"] == "failed"
    assert metadata["baseline_reason_code"] == "COVERAGE_NOT_FOUND"
    assert _validation_run_coverage_metadata(ValidationResult()) is None

    no_baseline = _validation_run_coverage_metadata(
        ValidationResult(
            coverage=ValidationCoverageResult(
                provider="python",
                percent=None,
                minimum_percent=99,
                enforce=True,
                status="failed",
                reason_code="COVERAGE_NOT_FOUND",
            )
        )
    )
    assert no_baseline == {
        "provider": "python",
        "minimum_percent": 99.0,
        "enforce": True,
        "status": "failed",
        "reason_code": "COVERAGE_NOT_FOUND",
    }


@pytest.mark.unit
def test_validation_failure_message_carries_coverage_context(tmp_path: Path) -> None:
    below_threshold = ValidationResult(
        coverage=_coverage(tmp_path, percent=88, minimum=99, reason_code="COVERAGE_BELOW_THRESHOLD")
    )
    command_failed = ValidationResult(
        coverage=_coverage(
            tmp_path,
            percent=99,
            minimum=99,
            reason_code="COVERAGE_COMMAND_FAILED",
        )
    )
    baseline = _coverage(tmp_path, percent=90, minimum=99)

    assert "pre-agent base coverage was 90.0%" in _validation_failure_message(
        below_threshold,
        baseline_coverage=baseline,
    )
    assert "coverage command failed" in _validation_failure_message(
        command_failed,
        baseline_coverage=baseline,
    )
    assert "unsupported coverage provider" in _validation_failure_message(
        ValidationResult(
            coverage=ValidationCoverageResult(
                provider="lcov",
                percent=None,
                minimum_percent=90,
                enforce=True,
                status="failed",
                reason_code="COVERAGE_PROVIDER_UNSUPPORTED",
            )
        )
    )
    assert (
        _validation_failure_message(
            ValidationResult(
                coverage=_coverage(
                    tmp_path,
                    percent=None,
                    minimum=99,
                    reason_code="COVERAGE_NOT_FOUND",
                )
            )
        )
        == "validation failed: coverage output was not found"
    )
    assert _validation_failure_message(
        ValidationResult(commands=[_command_result(tmp_path)])
    ) == "validation failed: pytest --cov"
    assert (
        _validation_failure_message(
            ValidationResult(
                coverage=ValidationCoverageResult(
                    provider="python",
                    percent=None,
                    minimum_percent=90,
                    enforce=True,
                    status="failed",
                    reason_code="COVERAGE_UNKNOWN",
                    command_result=None,
                )
            )
        )
        == "validation failed"
    )


@pytest.mark.unit
def test_validation_failure_message_carries_healthcheck_context(tmp_path: Path) -> None:
    stdout = tmp_path / "health.stdout"
    stderr = tmp_path / "health.stderr"
    stdout.write_text("starting", encoding="utf-8")
    stderr.write_text("connection refused", encoding="utf-8")
    failure = ValidationCommandResult(
        command="GET http://api:8080/healthz expected 200",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout,
        stderr_path=stderr,
        phase="healthcheck",
        reason_code="HEALTHCHECK_HTTP_STATUS_MISMATCH",
        stream_ids={
            "stdout": "validation.01_healthcheck.stdout",
            "stderr": "validation.01_healthcheck.stderr",
        },
        metadata={
            "healthcheck_name": "api",
            "healthcheck_kind": "http",
            "target": "http://api:8080/healthz",
            "attempts": 3,
            "timeout_seconds": 30,
        },
    )

    message = _validation_failure_message(ValidationResult(commands=[failure]))

    assert "health check api" in message
    assert "http://api:8080/healthz" in message
    assert "HEALTHCHECK_HTTP_STATUS_MISMATCH" in message
    assert "validation.01_healthcheck.stderr" in message


@pytest.mark.unit
def test_validation_failure_message_omits_missing_healthcheck_optional_context(
    tmp_path: Path,
) -> None:
    stdout = tmp_path / "health-minimal.stdout"
    stderr = tmp_path / "health-minimal.stderr"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("failed", encoding="utf-8")
    failure = ValidationCommandResult(
        command="GET http://api:8080/healthz expected 200",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout,
        stderr_path=stderr,
        phase="healthcheck",
        reason_code="HEALTHCHECK_HTTP_STATUS_MISMATCH",
        stream_ids={},
        metadata={
            "healthcheck_name": "api",
            "healthcheck_kind": "http",
            "target": "http://api:8080/healthz",
        },
    )

    message = _validation_failure_message(ValidationResult(commands=[failure]))

    assert "health check api" in message
    assert " after " not in message
    assert " across " not in message
    assert "; logs:" not in message


@pytest.mark.unit
def test_validation_run_reason_code_defaults_when_no_failure_detail(tmp_path: Path) -> None:
    assert _validation_run_reason_code(ValidationResult()) == "VALIDATION_OK"
    assert (
        _validation_run_reason_code(  # type: ignore[arg-type]
            SimpleNamespace(all_passed=False, coverage=None, first_failure=None)
        )
        == "VALIDATION_FAILED"
    )
    assert (
        _validation_run_reason_code(
            ValidationResult(
                coverage=ValidationCoverageResult(
                    provider="python",
                    percent=None,
                    minimum_percent=90,
                    enforce=True,
                    status="failed",
                    reason_code="COVERAGE_UNKNOWN",
                    command_result=None,
                )
            )
        )
        == "COVERAGE_UNKNOWN"
    )
    assert (
        _validation_run_reason_code(
            ValidationResult(
                commands=[
                    ValidationCommandResult(
                        command="pytest -q",
                        returncode=1,
                        duration_seconds=0,
                        stdout_path=tmp_path / "pytest.out",
                        stderr_path=tmp_path / "pytest.err",
                        reason_code="COMMAND_FAILED",
                    )
                ]
            )
        )
        == "COMMAND_FAILED"
    )


@pytest.mark.unit
def test_read_text_if_present_handles_empty_missing_and_present_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"
    empty = tmp_path / "empty.txt"
    present = tmp_path / "present.txt"
    empty.write_text(" \n", encoding="utf-8")
    present.write_text(" useful output \n", encoding="utf-8")

    assert _read_text_if_present(missing) is None
    assert _read_text_if_present(empty) is None
    assert _read_text_if_present(present) == "useful output"


@pytest.mark.unit
def test_read_text_if_present_returns_none_when_file_read_raises() -> None:
    class _UnreadablePath:
        def is_file(self) -> bool:
            return True

        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            raise OSError("permission denied")

    assert _read_text_if_present(_UnreadablePath()) is None  # type: ignore[arg-type]


@pytest.mark.unit
def test_call_pr_monitor_factory_uses_widest_supported_signature() -> None:
    calls: list[tuple[object, object, object]] = []
    adapter = object()
    profile = WorkspaceProfile.model_validate({"name": "factory-profile"})
    workspace = object()

    def factory(adapter_arg: object, profile_arg: object, workspace_arg: object) -> object:
        calls.append((adapter_arg, profile_arg, workspace_arg))
        return "monitor"

    assert (
        _call_pr_monitor_factory(
            factory,
            adapter=adapter,  # type: ignore[arg-type]
            profile=profile,
            workspace=workspace,  # type: ignore[arg-type]
        )
        == "monitor"
    )
    assert calls == [(adapter, profile, workspace)]


@pytest.mark.unit
def test_call_pr_monitor_factory_uses_two_argument_fallback_when_signature_is_opaque() -> None:
    class _OpaqueFactory:
        @property
        def __signature__(self) -> object:
            raise ValueError("opaque callable")

        def __call__(self, adapter_arg: object, profile_arg: object) -> object:
            return (adapter_arg, profile_arg)

    adapter = object()
    profile = WorkspaceProfile.model_validate({"name": "factory-profile"})

    assert _call_pr_monitor_factory(
        _OpaqueFactory(),
        adapter=adapter,  # type: ignore[arg-type]
        profile=profile,
        workspace=object(),  # type: ignore[arg-type]
    ) == (adapter, profile)


@pytest.mark.unit
def test_call_pr_monitor_factory_surfaces_bind_error() -> None:
    adapter = object()
    profile = WorkspaceProfile.model_validate({"name": "factory-profile"})

    def factory(*, required_keyword: str) -> object:
        return required_keyword

    with pytest.raises(TypeError):
        _call_pr_monitor_factory(
            factory,
            adapter=adapter,  # type: ignore[arg-type]
            profile=profile,
            workspace=object(),  # type: ignore[arg-type]
        )


@pytest.mark.unit
async def test_planning_required_accepts_committed_plan_file(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    # before_plan (clean)
    runner.queue_result(returncode=0, stdout="")
    # rev-parse HEAD -> baseline sha
    runner.queue_result(returncode=0, stdout="abc1234\n")
    # dirty after planning (still clean because agent committed)
    runner.queue_result(returncode=0, stdout="")
    # git diff --name-only <base>..HEAD -> plan file
    runner.queue_result(returncode=0, stdout="docs/awf-plans/ws_plan_commit.md\n")
    # before_compare
    runner.queue_result(returncode=0, stdout="")
    # after_compare
    runner.queue_result(returncode=0, stdout="")
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan committed",
        "implemented",
        '{"status":"satisfied","summary":"ok","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-committed",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_plan_commit", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None
    assert len(adapter.prompts) == 3


@pytest.mark.unit
async def test_planning_required_rejects_committed_code_as_outside_plan(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="base5678\n")  # rev-parse HEAD
    runner.queue_result(returncode=0, stdout="")  # dirty after planning (clean)
    runner.queue_result(
        returncode=0,
        stdout=(
            "docs/awf-plans/ws_plan_code.md\n"
            "src/awf/executor.py\n"
        ),
    )  # committed paths since baseline
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter("plan plus code")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-code-committed",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "enforce_plan_only_changes": True,
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_plan_code", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message == (
        "planning phase changed files outside `docs/awf-plans/ws_plan_code.md`: "
        "src/awf/executor.py"
    )


@pytest.mark.unit
async def test_planning_required_falls_back_to_porcelain_when_no_baseline_sha(
    tmp_path: Path,
) -> None:
    # Fresh repo or detached state where rev-parse HEAD fails.
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=128, stderr="fatal: not a git repository")  # rev-parse HEAD fails
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_plan_fallback.md\n")  # dirty after planning
    runner.queue_result(returncode=0, stdout="")  # before_compare
    runner.queue_result(returncode=0, stdout="")  # after_compare
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan fallback",
        "implemented",
        '{"status":"satisfied","summary":"ok","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-fallback",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_plan_fallback", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None
    # No git diff --name-only call should have been issued because rev-parse failed.
    diff_calls = [call for call in runner.calls if "diff" in call.args and "--name-only" in call.args]
    assert not diff_calls


@pytest.mark.unit
async def test_planning_required_dirty_plan_still_accepted(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="old_sha\n")  # rev-parse HEAD
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_plan_dirty.md\n")  # dirty after planning
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
    runner.queue_result(returncode=0, stdout="")  # before_compare
    runner.queue_result(returncode=0, stdout="")  # after_compare
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "dirty plan",
        "implemented",
        '{"status":"satisfied","summary":"ok","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-dirty",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_plan_dirty", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None
    # Because no new commits, the diff call should return empty; porcelain still carries the plan.
    diff_calls = [call for call in runner.calls if "diff" in call.args and "--name-only" in call.args]
    assert diff_calls


@pytest.mark.unit
async def test_planning_required_dirty_extra_file_still_rejected(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="base_sha\n")  # rev-parse HEAD
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_plan_extra_dirty.md\n?? src/extra.py\n",
    )  # after_plan
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter("dirty extra")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-extra-dirty",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "enforce_plan_only_changes": True,
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_plan_extra_dirty", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message == (
        "planning phase changed files outside `docs/awf-plans/ws_plan_extra_dirty.md`: "
        "src/extra.py"
    )


@pytest.mark.unit
def test_agent_model_for_workspace_prefers_nonblank_policy_override() -> None:
    defaults = AgentDefaults(model="default-model")

    assert (
        _agent_model_for_workspace(  # type: ignore[arg-type]
            SimpleNamespace(task_policy={"agent_model": "  gpt-special  "}),
            defaults,
        )
        == "gpt-special"
    )
    assert (
        _agent_model_for_workspace(  # type: ignore[arg-type]
            SimpleNamespace(task_policy={"agent_model": "   "}),
            defaults,
        )
        == "default-model"
    )
    assert (
        _agent_model_for_workspace(  # type: ignore[arg-type]
            SimpleNamespace(task_policy=None),
            None,
        )
        is None
    )


@pytest.mark.unit
def test_agent_defaults_for_workspace_binds_policy_model_for_monitor_recovery() -> None:
    defaults = AgentDefaults(model="ollama/kimi-k2.6:cloud", effort="xhigh")

    bound = _agent_defaults_for_workspace(  # type: ignore[arg-type]
        SimpleNamespace(task_policy={"agent_model": "  ollama/glm-5.1:cloud  "}),
        defaults,
    )

    assert bound is not None
    assert bound.model == "ollama/glm-5.1:cloud"
    assert bound.effort == "xhigh"


@pytest.mark.unit
def test_agent_defaults_for_workspace_handles_policy_without_base_defaults() -> None:
    effort_only = _agent_defaults_for_workspace(  # type: ignore[arg-type]
        SimpleNamespace(task_policy={"agent_effort": "high"}),
        None,
    )
    model_only = _agent_defaults_for_workspace(  # type: ignore[arg-type]
        SimpleNamespace(task_policy={"agent_model": "gpt-5.4-mini"}),
        None,
    )
    bound = _agent_defaults_for_workspace(  # type: ignore[arg-type]
        SimpleNamespace(task_policy={"agent_model": "gpt-special", "agent_effort": "high"}),
        None,
    )
    created = _agent_defaults_for_workspace(  # type: ignore[arg-type]
        SimpleNamespace(task_policy={"agent_model": "gpt-5.5", "agent_effort": "xhigh"}),
        None,
    )

    assert effort_only is None
    assert model_only == AgentDefaults(model="gpt-5.4-mini", effort=None)
    assert bound == AgentDefaults(model="gpt-special", effort="high")
    assert created == AgentDefaults(model="gpt-5.5", effort="xhigh")


@pytest.mark.unit
def test_agent_pr_identity_omits_missing_model_and_effort() -> None:
    assert (
        _agent_pr_identity(  # type: ignore[arg-type]
            SimpleNamespace(agent="codex", task_policy={}),
            defaults=None,
        )
        == "agent: `codex`"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("queued", "message"),
    [
        ([(1, "", "fetch failed")], "git fetch origin main failed"),
        ([(0, "", ""), (1, "", "switch failed")], "git switch awf/ws failed"),
        (
            [(0, "", ""), (0, "", ""), (128, "", "merge-base failed")],
            "merge-base --is-ancestor origin/main HEAD failed",
        ),
        (
            [
                (0, "", ""),
                (0, "", ""),
                (1, "", ""),
                (1, "", "conflict"),
                (0, "", ""),
            ],
            "git rebase origin/main failed",
        ),
        (
            [
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (1, "", "no target"),
            ],
            "could not resolve origin/main",
        ),
        (
            [
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (0, "b" * 40 + "\n", ""),
                (1, "", "no head"),
            ],
            "could not resolve HEAD",
        ),
        (
            [
                (0, "", ""),
                (0, "", ""),
                (1, "", ""),
                (0, "", ""),
                (0, "b" * 40 + "\n", ""),
                (0, "c" * 40 + "\n", ""),
                (1, "", "lease failed"),
            ],
            "git push --force-with-lease failed",
        ),
    ],
)
async def test_monitor_rebase_recovery_reports_git_failures(
    queued: list[tuple[int, str, str]],
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeCommandRunner()
    for returncode, stdout, stderr in queued:
        runner.queue_result(returncode=returncode, stdout=stdout, stderr=stderr)
    executor = _executor_with_runner(runner, tmp_path)

    async def skip_begin_operation(**_kwargs: object) -> None:
        return None

    async def skip_finish_operation(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        executor,
        "_begin_rebase_recovery_operation",
        skip_begin_operation,
    )
    monkeypatch.setattr(
        executor,
        "_finish_rebase_recovery_operation",
        skip_finish_operation,
    )
    monkeypatch.setattr(
        executor,
        "_record_executor_pr_audit_event",
        AsyncMock(),
    )

    with pytest.raises(_MonitorRebaseRecoveryError, match=message):
        await executor._run_monitor_rebase_recovery(
            workspace_id="ws_rebase",
            worktree_path=tmp_path / "worktrees" / "ws_rebase",
            base_branch="main",
            branch_name="awf/ws",
            remote_branch="awf/ws",
            reason="stale",
            recovery_payload={},
        )


@pytest.mark.unit
def test_active_recovery_payload_ignores_rebase_validate_only_operations() -> None:
    workspace = SimpleNamespace(
        operations=[
            SimpleNamespace(
                status=OperationStatus.pending.value,
                type=OperationType.rebase.value,
                payload={
                    "source": "pr_monitor",
                    "recovery_mode": "validate_only",
                },
            ),
            SimpleNamespace(
                status=OperationStatus.running.value,
                type=OperationType.validate.value,
                payload={
                    "source": "operator_api",
                    "recovery_mode": "validate_only",
                    "reason": "operator requested validation",
                },
            ),
        ]
    )

    payload = _get_active_recovery_payload(workspace)

    assert payload == {
        "source": "operator_api",
        "recovery_mode": "validate_only",
        "reason": "operator requested validation",
    }


@pytest.mark.unit
async def test_rebase_operation_helpers_noop_for_lightweight_executor(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)

    assert (
        await executor._begin_rebase_recovery_operation(
            workspace_id="ws_rebase",
            base_branch="main",
            remote_branch="awf/ws",
            reason="stale",
            reason_code="STALE_TARGET_BRANCH",
            source_base_sha=None,
            source_head_sha=None,
            recovery_payload={},
        )
        is None
    )
    await executor._finish_rebase_recovery_operation(
        SimpleNamespace(operation_id="op_skip", should_finish=False),  # type: ignore[arg-type]
        status=OperationStatus.succeeded,
        result={"status": "succeeded"},
    )
    await executor._finish_rebase_recovery_operation(
        None,
        status=OperationStatus.succeeded,
        result={"status": "succeeded"},
    )


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.unit
async def test_record_rebase_recovery_success_ignores_terminal_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    workspace = SimpleNamespace(id="ws_terminal", status=WorkspaceStatus.completed.value)
    ignored_callbacks: list[tuple[str, str]] = []
    finished_callbacks: list[dict[str, object]] = []

    class FakeWorkspaceRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, workspace_id: str) -> object:
            assert workspace_id == workspace.id
            return workspace

        async def record_ignored_stale_callback(
            self,
            _workspace: object,
            *,
            callback_source: str,
            callback_action: str,
            expected_status: WorkspaceStatus,
            reason_code: str,
        ) -> None:
            assert expected_status == WorkspaceStatus.running
            assert reason_code == "STALE_CALLBACK_IGNORED"
            ignored_callbacks.append((callback_source, callback_action))

    async def finish_ignored(
        _session: object,
        **kwargs: object,
    ) -> None:
        finished_callbacks.append(kwargs)

    monkeypatch.setattr(executor_mod, "WorkspaceRepository", FakeWorkspaceRepository)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._session_factory = lambda: session  # type: ignore[method-assign]
    monkeypatch.setattr(
        executor,
        "_finish_ignored_stale_callback_operations_in_session",
        finish_ignored,
    )

    await executor._record_rebase_recovery_success(
        workspace_id=workspace.id,
        base_sha="b" * 40,
        head_sha="h" * 40,
        source_base_sha="old-base",
        source_head_sha="old-head",
        operation=SimpleNamespace(operation_id="op", should_finish=True),  # type: ignore[arg-type]
        pushed=True,
        rebased=True,
    )

    assert ignored_callbacks == [("executor", "rebase_recovery")]
    assert finished_callbacks[0]["workspace_id"] == workspace.id
    assert finished_callbacks[0]["actual_status"] == WorkspaceStatus.completed.value
    assert session.commits == 1


@pytest.mark.unit
async def test_record_rebase_recovery_success_updates_candidate_and_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    workspace = SimpleNamespace(
        id="ws_rebased",
        status=WorkspaceStatus.running.value,
        base_commit="old-base",
        monitor_last_commit_sha="old-head",
    )
    candidate_workspace = SimpleNamespace(base_commit="old-base", monitor_last_commit_sha="old-head")
    candidate = SimpleNamespace(
        id="candidate",
        workspace_id=workspace.id,
        attempt_id="attempt",
        task_id="task",
        base_sha="old-base",
        head_sha="old-head",
        workspace=candidate_workspace,
        attempt=SimpleNamespace(id="attempt"),
    )
    readiness_calls: list[tuple[str, str]] = []
    finished_operations: list[dict[str, object]] = []

    class FakeWorkspaceRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, workspace_id: str) -> object:
            assert workspace_id == workspace.id
            return workspace

    class FakeMergeCandidateRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get_open_for_workspace_with_merge_inputs(self, workspace_id: str) -> object:
            assert workspace_id == workspace.id
            return candidate

    def sync_readiness(_candidate: object, **kwargs: object) -> None:
        readiness_calls.append(
            (
                kwargs["workspace"].base_commit,  # type: ignore[index, union-attr]
                kwargs["workspace"].monitor_last_commit_sha,  # type: ignore[index, union-attr]
            )
        )

    async def finish_operation(_session: object, **kwargs: object) -> None:
        finished_operations.append(kwargs)

    monkeypatch.setattr(executor_mod, "WorkspaceRepository", FakeWorkspaceRepository)
    monkeypatch.setattr(executor_mod, "MergeCandidateRepository", FakeMergeCandidateRepository)
    monkeypatch.setattr(executor_mod, "sync_candidate_readiness", sync_readiness)
    monkeypatch.setattr(executor_mod, "finish_monitor_operation", finish_operation)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._session_factory = lambda: session  # type: ignore[method-assign]
    monkeypatch.setattr(executor, "_add_executor_pr_audit_event", AsyncMock())

    await executor._record_rebase_recovery_success(
        workspace_id=workspace.id,
        base_sha="new-base",
        head_sha="new-head",
        source_base_sha="old-base",
        source_head_sha="old-head",
        operation=SimpleNamespace(operation_id="op_rebase", should_finish=True),  # type: ignore[arg-type]
        pushed=True,
        rebased=True,
    )

    assert workspace.base_commit == "new-base"
    assert workspace.monitor_last_commit_sha == "new-head"
    assert candidate.base_sha == "new-base"
    assert candidate.head_sha == "new-head"
    assert readiness_calls == [("new-base", "new-head")]
    assert finished_operations[0]["operation_id"] == "op_rebase"
    assert finished_operations[0]["status"] == OperationStatus.succeeded
    assert finished_operations[0]["result"]["target_base_sha"] == "new-base"
    assert session.commits == 1


@pytest.mark.unit
async def test_clear_rebase_recovery_staleness_refreshes_candidate_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    candidate = SimpleNamespace(
        id="candidate",
        workspace_id="ws_rebase",
        attempt_id="attempt",
        task_id="task",
        stale=True,
        stale_reason="target moved",
        workspace=SimpleNamespace(id="ws_rebase"),
        attempt=SimpleNamespace(id="attempt"),
    )
    replaced_findings: list[dict[str, object]] = []
    readiness_calls: list[object] = []

    class FakeMergeCandidateRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get_open_for_workspace_with_merge_inputs(self, workspace_id: str) -> object:
            assert workspace_id == candidate.workspace_id
            return candidate

    class FakeStaleReasonRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def replace_active_findings(self, **kwargs: object) -> None:
            replaced_findings.append(kwargs)

    def sync_readiness(candidate_arg: object, **_kwargs: object) -> None:
        readiness_calls.append(candidate_arg)

    monkeypatch.setattr(executor_mod, "MergeCandidateRepository", FakeMergeCandidateRepository)
    monkeypatch.setattr(executor_mod, "StaleReasonRepository", FakeStaleReasonRepository)
    monkeypatch.setattr(executor_mod, "sync_candidate_readiness", sync_readiness)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._session_factory = lambda: session  # type: ignore[method-assign]

    await executor._clear_rebase_recovery_staleness(workspace_id="ws_rebase")

    assert replaced_findings == [
        {
            "workspace_id": "ws_rebase",
            "candidate_id": "candidate",
            "attempt_id": "attempt",
            "task_id": "task",
            "findings": [],
        }
    ]
    assert candidate.stale is False
    assert candidate.stale_reason is None
    assert readiness_calls == [candidate]
    assert session.commits == 1
