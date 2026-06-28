"""Additional executor validation, planning, and salvage edge coverage tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.executor import helpers as executor_helpers
from awf.control.executor import planning_ops as executor_planning_ops
from awf.control.executor import validation_cleanup_guards as executor_validation_cleanup_guards
from awf.control.executor.helpers import (
    _failure_salvage_payload,
    _profile_with_planning_iteration_default,
    _raw_profile_has_explicit_planning_max_iterations,
    _validation_tier_for_workspace,
)
from awf.db.enums import (
    AgentRuntime,
    FailureReason,
    OperationStatus,
    OperationType,
    TaskClass,
    WorkspaceStatus,
)
from awf.profiles.models import ProfilePlanning, WorkspaceProfile
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    PLAN_CONFORMANCE_UNSATISFIED,
    PlanConformanceReport,
    PlanConformanceStatus,
)
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup
from awf.runtime.validation_worktree_constants import VALIDATION_WORKTREE_CLEANUP_FAILED
from tests.unit.control.test_executor_coverage_edges_parts.test_executor_coverage_edges_part_003 import (
    _executor_with_runner,
    _PlanningAdapter,
)


@pytest.mark.unit
async def test_stale_validation_cleanup_failure_records_secondary_failure_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSession:
        def __init__(self) -> None:
            self.commits = 0

        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            self.commits += 1

    session = _FakeSession()
    workspace = SimpleNamespace(
        id="ws_stale_cleanup",
        status=WorkspaceStatus.failed.value,
        failure_reason=FailureReason.validation_failure.value,
        failure_message="pytest failed before cleanup",
    )
    events: list[dict[str, object]] = []

    class _FakeWorkspaceRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, workspace_id: str) -> object:
            assert workspace_id == workspace.id
            return workspace

        async def add_event(
            self,
            _workspace: object,
            *,
            event_type: str,
            reason_code: str | None = None,
            payload: dict[str, object] | None = None,
        ) -> object:
            events.append(
                {
                    "event_type": event_type,
                    "reason_code": reason_code,
                    "payload": payload or {},
                }
            )
            return SimpleNamespace()

    async def _load_failure_causality_snapshot(
        _session: object,
        _workspace: object,
    ) -> object:
        return SimpleNamespace(
            primary_failure={
                "failure_reason": FailureReason.validation_failure.value,
                "reason_code": "PYTEST_TEST_FAILURE",
                "message": "pytest failed before cleanup",
            },
            secondary_failures=({"reason_code": "OLDER_SECONDARY"},),
        )

    monkeypatch.setattr(
        executor_validation_cleanup_guards,
        "WorkspaceRepository",
        _FakeWorkspaceRepository,
    )
    monkeypatch.setattr(
        executor_validation_cleanup_guards,
        "load_failure_causality_snapshot",
        _load_failure_causality_snapshot,
    )

    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("generated.log",),
        untracked_paths=("generated.log",),
    )
    cleanup_result = ValidationWorktreeCleanup(
        cleaned=False,
        check=dirty_check,
        restore_ref="c" * 40,
        reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        message="restore failed",
        cleanup_command="git restore --source cccccccc -- generated.log",
        cleanup_stderr="restore failed",
        verify_check=dirty_check,
    )
    executor = SimpleNamespace(_session_factory=lambda: session)

    await executor_validation_cleanup_guards._record_stale_validation_cleanup_failure(
        executor,
        workspace_id=workspace.id,
        validation_run_id="vr-stale-cleanup",
        reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        message="VALIDATION_WORKTREE_CLEANUP_FAILED: restore failed",
        cleanup_result=cleanup_result,
    )

    assert session.commits == 1
    assert workspace.failure_reason == FailureReason.validation_failure.value
    assert workspace.failure_message == "pytest failed before cleanup"
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "workspace.secondary_failure_recorded"
    assert event["reason_code"] == "PYTEST_TEST_FAILURE"
    payload = event["payload"]
    assert isinstance(payload, dict)
    assert payload["synthetic"] is True
    assert payload["primary_failure"] == {
        "failure_reason": FailureReason.validation_failure.value,
        "reason_code": "PYTEST_TEST_FAILURE",
        "message": "pytest failed before cleanup",
    }
    secondary_failure = payload["secondary_failure"]
    assert isinstance(secondary_failure, dict)
    assert secondary_failure["reason_code"] == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert secondary_failure["validation_run_id"] == "vr-stale-cleanup"
    assert secondary_failure["cleanup"]["remaining_paths"] == ["generated.log"]
    assert payload["secondary_failures"][-1]["reason_code"] == (VALIDATION_WORKTREE_CLEANUP_FAILED)


@pytest.mark.unit
def test_executor_metadata_helpers_cover_unreadable_and_invalid_edges(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class UnreadablePath:
        def is_file(self) -> bool:
            return True

        def open(self, *_args: object, **_kwargs: object) -> object:
            raise OSError("cannot read")

    assert executor_helpers._digest_file_if_present(UnreadablePath()) is None  # type: ignore[arg-type]

    unreadable = tmp_path / "unreadable.md"
    unreadable.write_text("content", encoding="utf-8")
    original_open = Path.open

    def _raise_for_unreadable(self: Path, *args: object, **kwargs: object) -> object:
        if self == unreadable:
            raise OSError("permission denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _raise_for_unreadable)

    assert executor_helpers._digest_file_if_present(unreadable) is None  # noqa: SLF001
    assert (
        executor_helpers._requested_tier_from_metadata(  # noqa: SLF001
            {"validation": {"requested_tier": 0}}
        )
        is None
    )
    assert (
        executor_helpers._requested_tier_from_metadata(  # noqa: SLF001
            {"validation": {"requested_tier": True}}
        )
        is None
    )

    profile = WorkspaceProfile.model_validate({"name": "tier", "validation": {"requested_tier": 1}})
    workspace = SimpleNamespace(
        task_class=TaskClass.build_config_task.value,
        operations=[
            SimpleNamespace(
                type=OperationType.validate,
                status=OperationStatus.succeeded,
                payload={"requested_tier": 2},
                result={"validation": {"requested_tier": 4}},
            ),
            SimpleNamespace(
                type=OperationType.validate,
                status=OperationStatus.running,
                payload={"validation": {"requested_tier": 3}},
            ),
        ],
    )

    assert _validation_tier_for_workspace(workspace, profile) == 4  # type: ignore[arg-type]
    assert executor_helpers._validate_operation_requested_tier(workspace) == 4  # noqa: SLF001

    active_tier_workspace = SimpleNamespace(
        operations=[
            SimpleNamespace(
                type=OperationType.validate,
                status=OperationStatus.succeeded,
                payload={"requested_tier": 1},
                result={"requested_tier": 2},
            ),
            SimpleNamespace(
                type=OperationType.validate,
                status=OperationStatus.running,
                payload={"validation": {"requested_tier": 3}},
            ),
        ],
    )

    assert executor_helpers._validate_operation_requested_tier(active_tier_workspace) == 3  # noqa: SLF001
    assert _validation_tier_for_workspace(workspace, profile) == 4  # type: ignore[arg-type]

    coverage = executor_helpers._coverage_result_from_metadata(  # noqa: SLF001
        {
            "provider": "",
            "percent": "99.0",
            "minimum_percent": "99",
            "enforce": "yes",
            "status": "",
            "reason_code": "",
            "gaps": [{"file": "src/awf/control/executor.py"}, "ignored"],
            "failing_test_node_ids": ["tests/test_a.py::test_one", 42],
            "failing_test_evidence": [object(), "AssertionError"],
            "provider_failure_evidence": ["provider down", None],
            "parallel_workers_requested": "8",
            "parallel_workers_effective": 8,
            "parallel_distribution": 5,
        }
    )

    assert coverage.provider == "python"
    assert coverage.percent is None
    assert coverage.minimum_percent == 0.0
    assert coverage.enforce is True
    assert coverage.status == "passed"
    assert coverage.reason_code == "COVERAGE_OK"
    assert coverage.gaps == [{"file": "src/awf/control/executor.py"}]
    assert coverage.failing_test_node_ids == ["tests/test_a.py::test_one"]
    assert coverage.failing_test_evidence == ["AssertionError"]
    assert coverage.provider_failure_evidence == ["provider down"]
    assert coverage.parallel_workers_requested is None
    assert coverage.parallel_workers_effective == 8
    assert coverage.parallel_distribution is None


@pytest.mark.unit
async def test_planning_conformance_reraises_non_timeout_agent_error(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # baseline HEAD
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_non_timeout.md\n")
    runner.queue_result(returncode=0, stdout="")  # committed paths
    runner.queue_result(returncode=0, stdout="sha1\n")  # implementation baseline
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_non_timeout.md\n")
    executor = _executor_with_runner(runner, tmp_path)

    class _NonTimeoutConformanceAdapter(_PlanningAdapter):
        async def run(self, **kwargs: object) -> SimpleNamespace:
            if len(self.prompts) == 2:
                prompt = kwargs.get("prompt")
                assert isinstance(prompt, str)
                self.prompts.append(prompt)
                raise AgentRunError(
                    agent=AgentRuntime.codex,
                    result=CommandResult(returncode=2, stdout="", stderr="tool failed"),
                    reason_code="AGENT_CLI_FAILED",
                )
            return await super().run(**kwargs)

    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-non-timeout",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
            },
        }
    )
    adapter = _NonTimeoutConformanceAdapter("plan", "implementation")

    with pytest.raises(AgentRunError, match="AGENT_CLI_FAILED"):
        await executor._run_agent_task_with_optional_planning(
            adapter=adapter,  # type: ignore[arg-type]
            workspace=SimpleNamespace(id="ws_non_timeout", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
            profile=profile,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            worktree_path=tmp_path / "worktree",
            model=None,
        )


@pytest.mark.unit
async def test_planning_conformance_timeout_uses_fresh_report_file(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    report_file = worktree / "docs" / "awf-plans" / "ws_timeout.json"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # baseline HEAD
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_timeout.md\n")
    runner.queue_result(returncode=0, stdout="")  # committed paths
    runner.queue_result(returncode=0, stdout="sha1\n")  # implementation baseline
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_timeout.md\n")
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_timeout.md\n?? docs/awf-plans/ws_timeout.json\n",
    )
    runner.queue_result(returncode=0, stdout="sha1\n")
    executor = _executor_with_runner(runner, tmp_path)

    class _TimeoutConformanceAdapter(_PlanningAdapter):
        async def run(self, **kwargs: object) -> SimpleNamespace:
            if len(self.prompts) == 2:
                prompt = kwargs.get("prompt")
                assert isinstance(prompt, str)
                self.prompts.append(prompt)
                report_file.parent.mkdir(parents=True, exist_ok=True)
                report_file.write_text(
                    '{"status":"needs_iteration","summary":"still missing","gaps":["gap"]}',
                    encoding="utf-8",
                )
                raise AgentRunError(
                    agent=AgentRuntime.codex,
                    result=CommandResult(returncode=124, stdout="", stderr="timeout"),
                    reason_code="AGENT_TIMEOUT",
                )
            return await super().run(**kwargs)

    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-timeout",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
            },
        }
    )
    adapter = _TimeoutConformanceAdapter("plan", "implementation")

    failure = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_timeout", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert failure is not None
    assert not isinstance(failure, str)
    assert failure.reason_code == executor_planning_ops.AGENT_STALLED_IN_CONFORMANCE  # noqa: SLF001
    assert failure.details is not None
    assert failure.details["conformance"]["gaps"] == ["gap"]


@pytest.mark.unit
async def test_conformance_stall_failure_records_diff_and_event_failures(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._git_rev_parse_head = AsyncMock(return_value="h" * 40)  # type: ignore[method-assign]
    executor._git_commit_count_since = AsyncMock(return_value=2)  # type: ignore[method-assign]
    executor._committed_paths_since = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("diff failed")
    )

    class _RaisingSessionContext:
        async def __aenter__(self) -> object:
            raise RuntimeError("database unavailable")

        async def __aexit__(
            self,
            exc_type: object,
            exc: object,
            traceback: object,
        ) -> None:
            del exc_type, exc, traceback

    executor._session_factory = lambda: _RaisingSessionContext()  # type: ignore[method-assign]
    stall = executor_planning_ops.ConformanceStallEvidence(  # noqa: SLF001
        kind=executor_planning_ops.ConformanceStallKind.no_output,  # noqa: SLF001
        iteration_index=1,
        elapsed_seconds=700.0,
        no_output_seconds=700.0,
        repeated_output_count=0,
        last_report_digest=None,
        plan_path="docs/plan.md",
        report_path="docs/report.json",
    )
    last_report = PlanConformanceReport(
        status=PlanConformanceStatus.needs_iteration,
        summary="still missing validation",
        gaps=("rerun tests",),
    )

    failure = await executor._build_conformance_stall_failure(  # noqa: SLF001
        workspace=SimpleNamespace(id="ws_stall"),  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        baseline_sha="b" * 40,
        last_report=last_report,
        stall=stall,
        iterations_used=2,
        max_iterations=3,
        plan_path=Path("docs/plan.md"),
        report_path=Path("docs/report.json"),
        recovery_action="notify",
    )

    assert failure.reason_code == executor_planning_ops.AGENT_STALLED_IN_CONFORMANCE  # noqa: SLF001
    assert failure.details is not None
    salvage_hint = failure.details["conformance_stall"]["salvage_hint"]
    assert salvage_hint["implementation_commit_count"] == 2
    assert salvage_hint["changed_paths"] == []
    assert failure.details["conformance"]["gaps"] == ["rerun tests"]


@pytest.mark.unit
async def test_conformance_validation_handoff_diff_failure_fails_closed(
    tmp_path: Path,
) -> None:
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    worktree = tmp_path / "worktree"
    plan_path = Path("docs/awf-plans/ws_diff.md")
    executor._changed_paths = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            set(),
            {plan_path},
            set(),
            set(),
        ]
    )
    executor._committed_paths_since = AsyncMock(  # type: ignore[method-assign]
        side_effect=[set(), RuntimeError("diff failed")]
    )
    executor._git_rev_parse_head = AsyncMock(return_value="head-sha")  # type: ignore[method-assign]

    executor._runner.queue_result(returncode=0, stdout="base-sha\n")
    report = (
        '{"status":"needs_iteration","summary":"AWF validation evidence is missing.",'
        f'"reason_code":"{CONFORMANCE_REQUIRES_AWF_VALIDATION}",'
        '"gaps":[{"kind":"awf_validation_evidence","detail":"Run AWF validation."}]}'
    )

    result = await executor._run_agent_task_with_optional_planning(  # noqa: SLF001
        adapter=_PlanningAdapter("plan written", "implemented", report),  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_diff", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=WorkspaceProfile.model_validate(
            {
                "name": "planned",
                "planning": {
                    "required": True,
                    "plan_path": "docs/awf-plans/{workspace_id}.md",
                    "max_iterations": 0,
                },
            }
        ),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    assert not isinstance(result, str)
    assert result is not None
    assert result.reason_code == PLAN_CONFORMANCE_UNSATISFIED
    assert executor._committed_paths_since.await_count == 2  # type: ignore[attr-defined]


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
        workspace=SimpleNamespace(id="ws_bad_path", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
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
        workspace=SimpleNamespace(id="ws_plan_extra", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
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
        "planning phase changed files outside `docs/awf-plans/ws_plan_extra.md`"
    )
    assert message.details is not None
    scope = message.details["planning_scope"]
    assert scope["required_paths"] == ["docs/awf-plans/ws_plan_extra.md"]
    assert scope["offending_paths"] == ["src/changed.py"]
    assert scope["recovery_strategy"] == "discard_and_replan"
    assert "preserved branch" in scope["recommended_action"]
    assert len(adapter.prompts) == 1


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
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_plan_unenforced.md\n?? src/changed.py\n",
    )
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_plan_unenforced.md\n?? src/changed.py\n",
    )
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
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
        workspace=SimpleNamespace(id="ws_plan_unenforced", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
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
    runner.queue_result(
        returncode=0, stdout="?? docs/awf-plans/ws_compare.md\n"
    )  # dirty after plan (3)
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since (empty) (4)
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop (5)
    runner.queue_result(
        returncode=0, stdout="?? docs/awf-plans/ws_compare.md\n"
    )  # before_compare (6)
    runner.queue_result(
        returncode=0,
        stdout=(
            "?? docs/awf-plans/ws_compare.md\n"
            "?? docs/awf-plans/ws_compare.json\n"
            "?? src/side_effect.py\n"
        ),
    )  # after_compare (7) — but should not get this far on scope violation
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
        workspace=SimpleNamespace(id="ws_compare", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
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
        "conformance phase changed files outside `docs/awf-plans/ws_compare.json`"
    )
    assert message.details is not None
    scope = message.details["planning_scope"]
    assert scope["scope_phase"] == "conformance"
    assert scope["required_paths"] == ["docs/awf-plans/ws_compare.json"]
    assert scope["offending_paths"] == ["src/side_effect.py"]


@pytest.mark.unit
async def test_conformance_phase_allows_side_effects_when_deviation_policy_disabled(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_compare_unenforced.md\n")
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_compare_unenforced.md\n")
    runner.queue_result(
        returncode=0,
        stdout=(
            "?? docs/awf-plans/ws_compare_unenforced.md\n"
            "?? docs/awf-plans/ws_compare_unenforced.json\n"
            "?? src/side_effect.py\n"
        ),
    )
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
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
        workspace=SimpleNamespace(id="ws_compare_unenforced", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None


@pytest.mark.unit
async def test_planning_required_allows_extra_changes_when_profile_disables_guards(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_permissive.md\n?? src/allowed.py\n",
    )  # dirty after plan
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_permissive.md\n?? src/allowed.py\n",
    )  # before_compare
    runner.queue_result(
        returncode=0,
        stdout=(
            "?? docs/awf-plans/ws_permissive.md\n"
            "?? docs/awf-plans/ws_permissive.json\n"
            "?? src/allowed.py\n"
            "?? src/compare_extra.py\n"
        ),
    )  # after_compare
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
    executor = _executor_with_runner(runner, tmp_path)
    adapter = _PlanningAdapter(
        "plan",
        "implementation",
        '{"status":"satisfied","summary":"ok","gaps":[]}',
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-permissive",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
                "enforce_plan_only_changes": False,
                "fail_on_unexplained_deviation": False,
            },
        }
    )

    message = await executor._run_agent_task_with_optional_planning(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_permissive", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None
    assert len(adapter.prompts) == 3


@pytest.mark.unit
async def test_planning_required_reports_unsatisfied_conformance_after_iterations(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_unsat.md\n")  # dirty after plan
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_unsat.md\n")  # before_compare
    runner.queue_result(
        returncode=0, stdout="?? docs/awf-plans/ws_unsat.md\n"
    )  # after_compare (first)
    runner.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
    runner.queue_result(
        returncode=0,
        stdout="?? docs/awf-plans/ws_unsat.md\n?? docs/awf-plans/ws_unsat.json\n",
    )  # after_compare (second) — unused on max_iterations=0
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
        workspace=SimpleNamespace(id="ws_unsat", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
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

    assert _profile_with_planning_iteration_default(omitted, 4).planning.max_iterations == 4
    assert _profile_with_planning_iteration_default(explicit, 4).planning.max_iterations == 1


@pytest.mark.unit
def test_raw_profile_planning_detection_handles_missing_profile() -> None:
    assert _raw_profile_has_explicit_planning_max_iterations(None) is False
    assert _raw_profile_has_explicit_planning_max_iterations({"planning": {}}) is False
    assert (
        _raw_profile_has_explicit_planning_max_iterations({"planning": {"required": True}}) is False
    )
    assert (
        _raw_profile_has_explicit_planning_max_iterations({"planning": {"max_iterations": 0}})
        is True
    )
    assert (
        _raw_profile_has_explicit_planning_max_iterations({"planning": {"max_iterations": 2}})
        is True
    )


@pytest.mark.unit
def test_failure_salvage_payload_omits_empty_branch_fields(tmp_path: Path) -> None:
    payload = _failure_salvage_payload(
        SimpleNamespace(branch_name=None, remote_push_branch=None),  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
    )

    assert payload == {
        "hint": "Workspace worktree and branch were preserved for salvage.",
        "worktree_path": str(tmp_path / "worktree"),
    }


@pytest.mark.unit
def test_failure_salvage_payload_defaults_remote_branch_to_branch(tmp_path: Path) -> None:
    payload = _failure_salvage_payload(
        SimpleNamespace(branch_name="awf/ws_123", remote_push_branch=None),  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
    )

    assert payload["branch_name"] == "awf/ws_123"
    assert payload["remote_push_branch"] == "awf/ws_123"


@pytest.mark.unit
async def test_changed_paths_raises_when_git_status_fails(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=128, stderr="not a git repository")
    executor = _executor_with_runner(runner, tmp_path)

    with pytest.raises(RuntimeError, match="git status failed"):
        await executor._changed_paths(tmp_path / "worktree")
