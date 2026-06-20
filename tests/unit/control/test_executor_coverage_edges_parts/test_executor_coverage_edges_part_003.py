"""Focused branch-coverage tests for executor helper behavior."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor import helpers as executor_helpers
from awf.control.executor import planning_conformance as executor_planning_conformance
from awf.control.executor import planning_ops as executor_planning_ops
from awf.control.executor import quality_gates as executor_quality_gates
from awf.db.enums import (
    AgentRuntime,
)
from awf.profiles.models import WorkspaceProfile
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    PlanConformanceReport,
    PlanConformanceStatus,
)
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
)


@pytest.mark.unit
def test_agent_runtime_or_none_parses_supported_values_and_rejects_unknown() -> None:
    """Agent runtime coercion accepts known enum/string values and treats
    unknown or non-string values as absent rather than raising."""
    assert executor_helpers._agent_runtime_or_none(AgentRuntime.codex) is AgentRuntime.codex  # noqa: SLF001
    assert executor_helpers._agent_runtime_or_none("opencode") is AgentRuntime.opencode  # noqa: SLF001
    assert executor_helpers._agent_runtime_or_none("not-a-runtime") is None  # noqa: SLF001
    assert executor_helpers._agent_runtime_or_none(object()) is None  # noqa: SLF001


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
        return SimpleNamespace(stdout=stdout, stderr="")


class _CoverageValidation:
    def __init__(self, coverage: ValidationCoverageResult | None) -> None:
        self.coverage = coverage
        self.calls: list[str] = []
        self.kwargs: list[dict[str, object]] = []

    async def run_profile_coverage(
        self, *, phase: str, **_kwargs: object
    ) -> ValidationCoverageResult | None:
        self.calls.append(phase)
        self.kwargs.append(dict(_kwargs))
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
    executor = WorkspaceExecutor(
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
    executor._update_subphase = AsyncMock()  # type: ignore[method-assign]
    return executor


def _required_awf_plan_profile(name: str) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": name,
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
            },
        }
    )


def _queue_planning_scope_failure_commands(
    runner: FakeCommandRunner,
    *,
    dirty_stdout: str = "",
) -> None:
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
    runner.queue_result(returncode=0, stdout=dirty_stdout)  # dirty_paths
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since


def _queue_planning_success_with_conformance_commands(runner: FakeCommandRunner) -> None:
    _queue_planning_scope_failure_commands(runner)
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
    runner.queue_result(returncode=0, stdout="")  # before_compare
    runner.queue_result(returncode=0, stdout="")  # after_compare
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD post-compare


def _autofix_classification(
    *,
    repair_files: tuple[str, ...] = ("src/app.py",),
) -> executor_quality_gates._PostAgentCommitClassification:  # noqa: SLF001
    return executor_quality_gates._PostAgentCommitClassification(  # noqa: SLF001
        reason_code="POST_AGENT_COMMIT_AUTOFIX_NEEDED",
        failed_hooks=("ruff-check",),
        format_repair_files=(),
        normalizer_repair_files=(),
        autofix_repair_files=repair_files,
        summary="ruff reported fixable diagnostics",
        repair_strategy="deterministic_autofix",
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


class _FakeExecutorSession:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> _FakeExecutorSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, _stmt: object) -> object:
        return _EmptyExecuteResult()


class _EmptyExecuteResult:
    def scalars(self) -> tuple[object, ...]:
        return ()


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
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
    runner.queue_result(
        returncode=0, stdout="?? docs/awf-plans/ws_coord_plan.md\n"
    )  # before compare
    runner.queue_result(
        returncode=0, stdout="?? docs/awf-plans/ws_coord_plan.md\n"
    )  # after compare
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
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
            task_tag=None,
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
            task_tag=None,
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
        workspace=SimpleNamespace(id="ws_plan_missing", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is not None
    assert not isinstance(message, str)
    assert message.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert message.message.startswith(
        "planning phase did not create or modify required plan file "
        "`docs/awf-plans/ws_plan_missing.md`"
    )
    assert message.details is not None
    scope = message.details["planning_scope"]
    assert scope["scope_phase"] == "planning"
    assert scope["required_paths"] == ["docs/awf-plans/ws_plan_missing.md"]
    assert scope["offending_paths"] == []
    assert scope["offending_commands"] == []
    assert scope["recovery_strategy"] == "discard_and_replan"
    assert scope["salvage_policy"] == "explicit_salvage_required"
    assert "Retry planning from a clean workspace" in scope["recommended_action"]
    assert len(adapter.prompts) == 1


@pytest.mark.unit
async def test_planning_required_accepts_ignored_plan_file_written_by_agent(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    plan_path = worktree / "docs" / "awf-plans" / "ws_plan_ignored.md"

    class _IgnoredPlanAdapter(_PlanningAdapter):
        async def run(self, **kwargs: object) -> SimpleNamespace:
            result = await super().run(**kwargs)
            if len(self.prompts) == 1:
                plan_path.parent.mkdir(parents=True, exist_ok=True)
                plan_path.write_text("# Plan\n\nUse the on-disk profile.\n", encoding="utf-8")
            return result

    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
    runner.queue_result(returncode=0, stdout="")  # dirty_paths: ignored plan is hidden
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
    runner.queue_result(returncode=0, stdout="")  # before_compare
    runner.queue_result(returncode=0, stdout="")  # after_compare
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD post-compare
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _IgnoredPlanAdapter(
        "plan written",
        "implementation",
        '{"status":"satisfied","summary":"done","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-ignored",
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
        workspace=SimpleNamespace(id="ws_plan_ignored", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert message is None
    assert len(adapter.prompts) == 3


@pytest.mark.unit
async def test_planning_required_recovers_single_ignored_near_miss_plan_file(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    required_path = worktree / "docs" / "awf-plans" / "ws_plan_near_miss.md"
    near_miss_path = worktree / "docs" / "awf-plans" / "ws_plan_near_moss.md"

    class _NearMissPlanAdapter(_PlanningAdapter):
        async def run(self, **kwargs: object) -> SimpleNamespace:
            result = await super().run(**kwargs)
            if len(self.prompts) == 1:
                near_miss_path.parent.mkdir(parents=True, exist_ok=True)
                near_miss_path.write_text(
                    "# Plan\n\nRecover the typoed plan artifact.\n",
                    encoding="utf-8",
                )
            return result

    runner = FakeCommandRunner()
    _queue_planning_success_with_conformance_commands(runner)
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _NearMissPlanAdapter(
        "plan written",
        "implementation",
        '{"status":"satisfied","summary":"done","gaps":[]}',
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(
            id="ws_plan_near_miss",
            task_prompt="do it",
            task_tag=None,
        ),  # type: ignore[arg-type]
        profile=_required_awf_plan_profile("planning-near-miss"),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert message is None
    assert len(adapter.prompts) == 3
    assert required_path.read_text(encoding="utf-8") == (
        "# Plan\n\nRecover the typoed plan artifact.\n"
    )
    assert not near_miss_path.exists()


@pytest.mark.unit
def test_plan_artifact_candidate_digests_rejects_symlinked_plan_dir(
    tmp_path: Path,
) -> None:
    """A plan dir symlinked outside the worktree yields no candidates.

    ``is_dir()``/``glob`` follow symlinks, so a repo tracking
    ``docs/awf-plans`` as a link to an external directory would otherwise let
    near-miss recovery ``replace`` files outside the isolated workspace with the
    elevated control-plane process. Resolution-based containment refuses it.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "docs").mkdir()
    external = tmp_path / "outside"
    external.mkdir()
    # A real near-miss artifact lives in the external dir; following the symlink
    # would surface it under a lexically-in-worktree path.
    (external / "ws_escape.md").write_text("# Outside\n", encoding="utf-8")
    (worktree / "docs" / "awf-plans").symlink_to(external, target_is_directory=True)

    plan_path = Path("docs/awf-plans/ws_escape_plan.md")
    candidates = executor_planning_ops._plan_artifact_candidate_digests(  # noqa: SLF001
        worktree,
        plan_path,
    )

    assert candidates == {}


@pytest.mark.unit
def test_plan_artifact_candidate_digests_rejects_symlinked_plan_dir_into_hidden(
    tmp_path: Path,
) -> None:
    """A plan dir symlinked at a git-hidden in-worktree dir yields no candidates.

    Physical containment alone is insufficient: a ``docs/awf-plans`` symlink
    pointing at an in-worktree but git-ignored/hidden directory (e.g. ``.git``)
    still resolves *under* the worktree, so a ``relative_to`` check would pass.
    But ``glob`` and the later ``source.replace(target)`` follow the link and
    mutate storage the porcelain dirty/changed scope checks never observe,
    letting near-miss recovery mark the logical plan path recovered with no scope
    evidence. The resolved-path equality check refuses it.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "docs").mkdir()
    # An in-worktree but git-hidden directory holding a real near-miss artifact.
    hidden = worktree / ".git" / "awf-hidden"
    hidden.mkdir(parents=True)
    (hidden / "ws_escape.md").write_text("# Hidden\n", encoding="utf-8")
    (worktree / "docs" / "awf-plans").symlink_to(hidden, target_is_directory=True)

    plan_path = Path("docs/awf-plans/ws_escape_plan.md")
    candidates = executor_planning_ops._plan_artifact_candidate_digests(  # noqa: SLF001
        worktree,
        plan_path,
    )

    assert candidates == {}


@pytest.mark.unit
async def test_planning_required_near_miss_refuses_recovery_on_dirty_baseline(
    tmp_path: Path,
) -> None:
    """A pre-dirty source path masks ``after_plan - before_plan``; refuse the move."""
    worktree = tmp_path / "worktree"
    required_path = worktree / "docs" / "awf-plans" / "ws_dirty_base.md"
    near_miss_path = worktree / "docs" / "awf-plans" / "ws_dirty_basf.md"

    class _DirtyBaselinePlanAdapter(_PlanningAdapter):
        async def run(self, **kwargs: object) -> SimpleNamespace:
            result = await super().run(**kwargs)
            if len(self.prompts) == 1:
                near_miss_path.parent.mkdir(parents=True, exist_ok=True)
                near_miss_path.write_text(
                    "# Plan\n\nWritten under a dirty baseline.\n",
                    encoding="utf-8",
                )
            return result

    runner = FakeCommandRunner()
    # ``src/app.py`` is dirty *before* planning and stays dirty after, so the
    # caller's ``after_plan - before_plan`` diff masks the planning agent's edit.
    runner.queue_result(returncode=0, stdout=" M src/app.py\n")  # before_plan (pre-dirty)
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
    runner.queue_result(returncode=0, stdout=" M src/app.py\n")  # dirty_paths after planning
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _DirtyBaselinePlanAdapter("plan written elsewhere")

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(
            id="ws_dirty_base",
            task_prompt="do it",
            task_tag=None,
        ),  # type: ignore[arg-type]
        profile=_required_awf_plan_profile("planning-dirty-baseline-near-miss"),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert message is not None
    assert not isinstance(message, str)
    assert message.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert message.details is not None
    assert message.details["near_miss_plan_artifacts"] == [
        {
            "path": "docs/awf-plans/ws_dirty_basf.md",
            "required_path": "docs/awf-plans/ws_dirty_base.md",
            "reason": "dirty_baseline_before_planning",
            "filename_hamming_distance": 1,
            "dirty_baseline_paths": ["src/app.py"],
        }
    ]
    # The elevated-trust move must not have happened.
    assert near_miss_path.exists()
    assert not required_path.exists()
    assert len(adapter.prompts) == 1


@pytest.mark.unit
async def test_planning_required_near_miss_refuses_recovery_with_stale_report(
    tmp_path: Path,
) -> None:
    """A prewritten conformance report in the ignored plan dir blocks the move.

    The report sits in ``docs/awf-plans`` alongside the plan, so neither the
    porcelain dirty diff nor the ``ws_*.md`` candidate snapshot sees it. If the
    recovery moved the typoed plan and proceeded, the conformance success path
    (``_read_text_if_present(report_path) or stdout``) could consume that stale
    satisfied JSON instead of the compare call's output. Refuse the move.
    """
    worktree = tmp_path / "worktree"
    required_path = worktree / "docs" / "awf-plans" / "ws_report.md"
    near_miss_path = worktree / "docs" / "awf-plans" / "ws_reporu.md"
    stale_report_path = worktree / "docs" / "awf-plans" / "ws_report.json"

    class _StaleReportPlanAdapter(_PlanningAdapter):
        async def run(self, **kwargs: object) -> SimpleNamespace:
            result = await super().run(**kwargs)
            if len(self.prompts) == 1:
                near_miss_path.parent.mkdir(parents=True, exist_ok=True)
                near_miss_path.write_text(
                    "# Plan\n\nTypoed plan with a co-written report.\n",
                    encoding="utf-8",
                )
                stale_report_path.write_text(
                    '{"status":"satisfied","summary":"prewritten","gaps":[]}',
                    encoding="utf-8",
                )
            return result

    runner = FakeCommandRunner()
    _queue_planning_scope_failure_commands(runner)
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _StaleReportPlanAdapter("plan written elsewhere")

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_report", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=_required_awf_plan_profile("planning-stale-report-near-miss"),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert message is not None
    assert not isinstance(message, str)
    assert message.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert message.details is not None
    assert message.details["near_miss_plan_artifacts"] == [
        {
            "path": "docs/awf-plans/ws_reporu.md",
            "required_path": "docs/awf-plans/ws_report.md",
            "reason": "conformance_report_present",
            "filename_hamming_distance": 1,
        }
    ]
    # The elevated-trust move must not have happened, and the stale report must
    # be left for the scope-failure path to surface rather than consumed.
    assert near_miss_path.exists()
    assert not required_path.exists()
    assert len(adapter.prompts) == 1


@pytest.mark.unit
async def test_planning_required_near_miss_fails_when_multiple_candidates(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    candidate_paths = (
        worktree / "docs" / "awf-plans" / "ws_aaab.md",
        worktree / "docs" / "awf-plans" / "ws_aaac.md",
    )

    class _AmbiguousPlanAdapter(_PlanningAdapter):
        async def run(self, **kwargs: object) -> SimpleNamespace:
            result = await super().run(**kwargs)
            if len(self.prompts) == 1:
                for path in candidate_paths:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("# Plan\n\nAmbiguous.\n", encoding="utf-8")
            return result

    runner = FakeCommandRunner()
    _queue_planning_scope_failure_commands(runner)
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _AmbiguousPlanAdapter("plan written elsewhere")

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_aaaa", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=_required_awf_plan_profile("planning-ambiguous-near-miss"),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert message is not None
    assert not isinstance(message, str)
    assert message.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert message.details is not None
    near_miss = message.details["near_miss_plan_artifacts"]
    assert [item["path"] for item in near_miss] == [
        "docs/awf-plans/ws_aaab.md",
        "docs/awf-plans/ws_aaac.md",
    ]
    assert {item["reason"] for item in near_miss} == {"ambiguous_near_miss_candidates"}


@pytest.mark.unit
async def test_planning_required_near_miss_fails_when_source_changes(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    near_miss_path = worktree / "docs" / "awf-plans" / "ws_near_sourcf.md"

    class _SourceChangingPlanAdapter(_PlanningAdapter):
        async def run(self, **kwargs: object) -> SimpleNamespace:
            result = await super().run(**kwargs)
            if len(self.prompts) == 1:
                near_miss_path.parent.mkdir(parents=True, exist_ok=True)
                near_miss_path.write_text("# Plan\n\nUnsafe.\n", encoding="utf-8")
            return result

    runner = FakeCommandRunner()
    _queue_planning_scope_failure_commands(runner, dirty_stdout=" M src/app.py\n")
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _SourceChangingPlanAdapter("plan written elsewhere")

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_near_source", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=_required_awf_plan_profile("planning-near-miss-source"),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert message is not None
    assert not isinstance(message, str)
    assert message.details is not None
    near_miss = message.details["near_miss_plan_artifacts"]
    assert near_miss == [
        {
            "path": "docs/awf-plans/ws_near_sourcf.md",
            "required_path": "docs/awf-plans/ws_near_source.md",
            "reason": "planning_changed_other_paths",
            "filename_hamming_distance": 1,
            "offending_paths": ["src/app.py"],
        }
    ]


@pytest.mark.unit
async def test_planning_required_near_miss_ignores_non_candidate_files(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    plan_dir = worktree / "docs" / "awf-plans"

    class _NonCandidatePlanAdapter(_PlanningAdapter):
        async def run(self, **kwargs: object) -> SimpleNamespace:
            result = await super().run(**kwargs)
            if len(self.prompts) == 1:
                (plan_dir / "nested").mkdir(parents=True, exist_ok=True)
                (plan_dir / "README.md").write_text("# Notes\n", encoding="utf-8")
                (plan_dir / "ws_hidden.json").write_text("{}", encoding="utf-8")
                (plan_dir / "nested" / "ws_hiddem.md").write_text(
                    "# Nested\n",
                    encoding="utf-8",
                )
            return result

    runner = FakeCommandRunner()
    _queue_planning_scope_failure_commands(runner)
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _NonCandidatePlanAdapter("plan written elsewhere")

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_hidden", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=_required_awf_plan_profile("planning-non-candidate"),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert message is not None
    assert not isinstance(message, str)
    assert message.details is not None
    assert message.details["near_miss_plan_artifacts"] == []


@pytest.mark.unit
async def test_planning_required_near_miss_does_not_recover_distant_filename(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    near_miss_path = worktree / "docs" / "awf-plans" / "ws_faraway.md"

    class _DistantPlanAdapter(_PlanningAdapter):
        async def run(self, **kwargs: object) -> SimpleNamespace:
            result = await super().run(**kwargs)
            if len(self.prompts) == 1:
                near_miss_path.parent.mkdir(parents=True, exist_ok=True)
                near_miss_path.write_text("# Plan\n\nToo far.\n", encoding="utf-8")
            return result

    runner = FakeCommandRunner()
    _queue_planning_scope_failure_commands(runner)
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _DistantPlanAdapter("plan written elsewhere")

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_close", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=_required_awf_plan_profile("planning-distant-near-miss"),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert message is not None
    assert not isinstance(message, str)
    assert message.details is not None
    assert message.details["near_miss_plan_artifacts"] == [
        {
            "path": "docs/awf-plans/ws_faraway.md",
            "required_path": "docs/awf-plans/ws_close.md",
            "reason": "filename_not_close_enough",
        }
    ]


@pytest.mark.unit
async def test_planning_required_near_miss_does_not_overwrite_existing_plan(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    required_path = worktree / "docs" / "awf-plans" / "ws_exist.md"
    near_miss_path = worktree / "docs" / "awf-plans" / "ws_exisu.md"
    required_path.parent.mkdir(parents=True, exist_ok=True)
    required_path.write_text("# Original\n", encoding="utf-8")

    class _ExistingPlanAdapter(_PlanningAdapter):
        async def run(self, **kwargs: object) -> SimpleNamespace:
            result = await super().run(**kwargs)
            if len(self.prompts) == 1:
                near_miss_path.write_text("# New plan\n", encoding="utf-8")
            return result

    runner = FakeCommandRunner()
    _queue_planning_scope_failure_commands(runner)
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _ExistingPlanAdapter("plan written elsewhere")

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_exist", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=_required_awf_plan_profile("planning-existing-required"),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert message is not None
    assert not isinstance(message, str)
    assert required_path.read_text(encoding="utf-8") == "# Original\n"
    assert near_miss_path.exists()
    assert message.details is not None
    assert message.details["near_miss_plan_artifacts"] == [
        {
            "path": "docs/awf-plans/ws_exisu.md",
            "required_path": "docs/awf-plans/ws_exist.md",
            "reason": "required_plan_already_existed",
            "filename_hamming_distance": 1,
        }
    ]


@pytest.mark.unit
async def test_planning_required_near_miss_recovers_when_existing_plan_deleted(
    tmp_path: Path,
) -> None:
    """A preserved pre-planning digest must not block recovery once the required
    plan is deleted during planning and only a typo sibling remains."""
    worktree = tmp_path / "worktree"
    required_path = worktree / "docs" / "awf-plans" / "ws_resumed.md"
    near_miss_path = worktree / "docs" / "awf-plans" / "ws_resumef.md"
    required_path.parent.mkdir(parents=True, exist_ok=True)
    required_path.write_text("# Stale prior-run plan\n", encoding="utf-8")

    class _DeleteThenTypoPlanAdapter(_PlanningAdapter):
        async def run(self, **kwargs: object) -> SimpleNamespace:
            result = await super().run(**kwargs)
            if len(self.prompts) == 1:
                required_path.unlink()
                near_miss_path.write_text(
                    "# Plan\n\nReplanned after deleting the stale artifact.\n",
                    encoding="utf-8",
                )
            return result

    runner = FakeCommandRunner()
    _queue_planning_success_with_conformance_commands(runner)
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _DeleteThenTypoPlanAdapter(
        "plan written",
        "implementation",
        '{"status":"satisfied","summary":"done","gaps":[]}',
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_resumed", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=_required_awf_plan_profile("planning-resumed-near-miss"),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert message is None
    assert len(adapter.prompts) == 3
    assert required_path.read_text(encoding="utf-8") == (
        "# Plan\n\nReplanned after deleting the stale artifact.\n"
    )
    assert not near_miss_path.exists()


@pytest.mark.unit
async def test_planning_required_skips_digest_fallback_when_git_reports_plan_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    plan_path = Path("docs/awf-plans/ws_plan_tracked.md")
    digest_paths: list[Path] = []

    def _digest(path: Path) -> str | None:
        digest_paths.append(path.relative_to(worktree))
        return None

    monkeypatch.setattr(executor_planning_ops, "_digest_file_if_present", _digest)
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
    runner.queue_result(returncode=0, stdout=f"?? {plan_path.as_posix()}\n")  # dirty_paths
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
    runner.queue_result(returncode=0, stdout=f"?? {plan_path.as_posix()}\n")  # before_compare
    runner.queue_result(returncode=0, stdout=f"?? {plan_path.as_posix()}\n")  # after_compare
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD post-compare
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan written",
        "implementation",
        '{"status":"satisfied","summary":"done","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-tracked",
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
        workspace=SimpleNamespace(id="ws_plan_tracked", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert message is None
    assert digest_paths == [plan_path]


@pytest.mark.unit
def test_digest_file_if_present_streams_file_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = (b"0123456789abcdef" * 8192) + b"tail"
    path = tmp_path / "large-plan.md"
    path.write_bytes(payload)

    def _read_bytes_should_not_be_used(self: Path) -> bytes:
        raise AssertionError(f"unexpected read_bytes for {self}")

    monkeypatch.setattr(Path, "read_bytes", _read_bytes_should_not_be_used)

    assert executor_helpers._digest_file_if_present(path) == hashlib.sha256(payload).hexdigest()


@pytest.mark.unit
def test_exclude_agent_salvage_artifacts_uses_linked_gitdir_exclude(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    linked_git_dir = tmp_path / "mirror.git" / "worktrees" / "ws"
    worktree.mkdir()
    linked_git_dir.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")

    executor_planning_ops._exclude_agent_salvage_artifacts(object(), worktree)  # noqa: SLF001

    assert (linked_git_dir / "info" / "exclude").read_text(encoding="utf-8") == ("/.awf/salvage/\n")


@pytest.mark.unit
async def test_planning_event_helpers_skip_missing_workspace_and_missing_validation_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _MissingWorkspaceRepo:
        def __init__(self, _session: object) -> None:
            return None

        async def get(self, _workspace_id: str) -> object | None:
            return None

    class _MissingRunRepo:
        def __init__(self, _session: object) -> None:
            return None

        async def get(self, _run_id: str) -> object | None:
            return None

    monkeypatch.setattr(executor_planning_conformance, "WorkspaceRepository", _MissingWorkspaceRepo)
    monkeypatch.setattr(executor_planning_conformance, "ValidationRunRepository", _MissingRunRepo)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._session_factory = lambda: _FakeExecutorSession()  # type: ignore[method-assign]
    handoff = executor_planning_ops._PlanningValidationHandoff(  # noqa: SLF001
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="needs validation",
            gaps=("run tests",),
        ),
        plan_path=Path("plans/task.md"),
        report_path=Path("plans/task.validation.json"),
        iteration=0,
        max_iterations=1,
    )

    await executor_planning_ops._record_planning_validation_handoff_event(  # noqa: SLF001
        executor,
        workspace_id="ws_missing",
        handoff=handoff,
    )
    await executor_planning_ops._record_post_validation_conformance_event(  # noqa: SLF001
        executor,
        workspace_id="ws_missing",
        handoff=handoff,
        report=PlanConformanceReport(
            status=PlanConformanceStatus.satisfied,
            summary="ok",
            gaps=(),
        ),
        validation_run_id="vr_missing",
    )
    evidence = await executor_planning_ops._validation_run_evidence_for_conformance(  # noqa: SLF001
        executor,
        "vr_missing",
    )

    assert '"status": "missing"' in evidence
    assert '"reason_code": "VALIDATION_RUN_NOT_FOUND"' in evidence


@pytest.mark.unit
async def test_auto_retry_planning_scope_failure_records_skip_and_retry_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[tuple[str, str, dict[str, object]]] = []

    class _WorkspaceRepo:
        workspace: object | None = SimpleNamespace(
            id="ws_retry",
            task_policy={"scheduler": {"source_workspace_id": "ws_original"}},
        )

        def __init__(self, _session: object) -> None:
            return None

        async def get(self, _workspace_id: str) -> object | None:
            return self.workspace

        async def get_for_update(self, workspace_id: str) -> object | None:
            return await self.get(workspace_id)

        async def add_event(
            self,
            _workspace: object,
            *,
            event_type: str,
            reason_code: str,
            payload: dict[str, object],
        ) -> None:
            events.append((event_type, reason_code, payload))

    monkeypatch.setattr(executor_planning_ops, "WorkspaceRepository", _WorkspaceRepo)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._session_factory = lambda: _FakeExecutorSession()  # type: ignore[method-assign]
    failure = executor_planning_ops._PlanningRunFailure(  # noqa: SLF001
        message="scope violation",
        reason_code=executor_planning_ops.AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    )

    await executor_planning_ops._auto_retry_planning_scope_failure(  # noqa: SLF001
        executor,
        workspace_id="ws_retry",
        failure=failure,
    )
    assert events[-1][1] == "PLANNING_SCOPE_AUTO_RETRY_ALREADY_RETRIED"

    _WorkspaceRepo.workspace = SimpleNamespace(id="ws_retry", task_policy={})

    async def _retry_failed(_session: object, _workspace_id: str, **_kwargs: object) -> object:
        raise executor_planning_ops.WorkspaceRetryError(
            "cannot retry",
            detail={"reason": "busy"},
        )

    monkeypatch.setattr(executor_planning_ops, "retry_workspace_row", _retry_failed)
    await executor_planning_ops._auto_retry_planning_scope_failure(  # noqa: SLF001
        executor,
        workspace_id="ws_retry",
        failure=failure,
    )

    assert events[-1][0] == "workspace.planning_scope_auto_retry_failed"
    assert events[-1][2]["detail"] == {"reason": "busy"}

    _WorkspaceRepo.workspace = SimpleNamespace(id="ws_retry", task_policy={})

    async def _retry_dup_port(_session: object, _workspace_id: str, **_kwargs: object) -> object:
        raise executor_planning_ops.WorkspaceCreateDuplicateHostPortError(host_port=8080)

    monkeypatch.setattr(executor_planning_ops, "retry_workspace_row", _retry_dup_port)
    await executor_planning_ops._auto_retry_planning_scope_failure(  # noqa: SLF001
        executor,
        workspace_id="ws_retry",
        failure=failure,
    )
    assert events[-1][0] == "workspace.planning_scope_auto_retry_failed"
    assert events[-1][2]["detail"] == {"host_port": 8080}

    async def _retry_port_conflict(
        _session: object, _workspace_id: str, **_kwargs: object
    ) -> object:
        raise executor_planning_ops.WorkspaceCreateHostPortConflictError(
            host_port=9090,
            conflicting_workspace_id="ws_other",
        )

    monkeypatch.setattr(executor_planning_ops, "retry_workspace_row", _retry_port_conflict)
    await executor_planning_ops._auto_retry_planning_scope_failure(  # noqa: SLF001
        executor,
        workspace_id="ws_retry",
        failure=failure,
    )
    assert events[-1][0] == "workspace.planning_scope_auto_retry_blocked"
    assert events[-1][1] == "PLANNING_SCOPE_AUTO_RETRY_HOST_PORT_CONFLICT"
    assert events[-1][2]["detail"]["host_port"] == 9090
    assert events[-1][2]["retry_after"] == "terminal_runtime_released"
