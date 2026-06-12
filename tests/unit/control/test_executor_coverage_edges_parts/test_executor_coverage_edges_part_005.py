"""Focused branch-coverage tests for executor helper behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from awf.adapters.base import AgentDefaults
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor import git_methods as executor_git_methods
from awf.control.executor import monitor_handoff_audit as executor_monitor_handoff_audit
from awf.control.executor import quality_gates as executor_quality_gates
from awf.control.executor import state_ops as executor_state_ops
from awf.control.executor.helpers import (
    _agent_defaults_for_workspace,
    _agent_pr_identity,
    _call_pr_monitor_factory,
    _read_text_if_present,
)
from awf.control.executor.recovery_payloads import (
    _get_active_recovery_payload,
)
from awf.control.executor.types import (
    _MonitorRebaseRecoveryError,
)
from awf.db.enums import (
    FailureReason,
    OperationStatus,
    OperationType,
    WorkspaceStatus,
)
from awf.profiles.models import WorkspaceProfile
from awf.runtime.merge_eligibility import VALIDATION_INSUFFICIENT_TIER_STALE_REASON
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
)
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
)
from awf.service.staleness import (
    REASON_TARGET_ADVANCED,
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


def _fake_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    mirror = tmp_path / "mirror.git"
    linked_git_dir = mirror / "worktrees" / "ws_missing_head"
    linked_git_dir.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    return mirror, worktree


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
def test_call_pr_monitor_factory_passes_provider_recovery_default_when_supported() -> None:
    calls: list[tuple[object, object, object, str | None]] = []
    adapter = object()
    profile = WorkspaceProfile.model_validate({"name": "factory-profile"})
    workspace = object()

    def factory(
        adapter_arg: object,
        profile_arg: object,
        workspace_arg: object,
        *,
        provider_recovery_default_model: str | None = None,
    ) -> object:
        calls.append(
            (
                adapter_arg,
                profile_arg,
                workspace_arg,
                provider_recovery_default_model,
            )
        )
        return "monitor"

    assert (
        _call_pr_monitor_factory(
            factory,
            adapter=adapter,  # type: ignore[arg-type]
            profile=profile,
            workspace=workspace,  # type: ignore[arg-type]
            provider_recovery_default_model="gpt-5",
        )
        == "monitor"
    )
    assert calls == [(adapter, profile, workspace, "gpt-5")]


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
    # rev-parse HEAD pre-loop (post-plan progress digest)
    runner.queue_result(returncode=0, stdout="abc1234\n")
    # before_compare
    runner.queue_result(returncode=0, stdout="")
    # after_compare
    runner.queue_result(returncode=0, stdout="")
    # rev-parse HEAD iter 0 post (iteration progress digest)
    runner.queue_result(returncode=0, stdout="abc1234\n")
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
        workspace=SimpleNamespace(id="ws_plan_commit", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
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
        stdout=("docs/awf-plans/ws_plan_code.md\nsrc/awf/executor.py\n"),
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
        workspace=SimpleNamespace(id="ws_plan_code", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is not None
    assert not isinstance(message, str)
    assert message.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert message.details is not None
    scope = message.details["planning_scope"]
    assert scope["required_paths"] == ["docs/awf-plans/ws_plan_code.md"]
    assert scope["offending_paths"] == ["src/awf/executor.py"]
    assert len(adapter.prompts) == 1


@pytest.mark.unit
async def test_planning_required_falls_back_to_porcelain_when_no_baseline_sha(
    tmp_path: Path,
) -> None:
    # Fresh repo or detached state where rev-parse HEAD fails.
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(
        returncode=128, stderr="fatal: not a git repository"
    )  # rev-parse HEAD fails
    runner.queue_result(
        returncode=0, stdout="?? docs/awf-plans/ws_plan_fallback.md\n"
    )  # dirty after planning
    runner.queue_result(
        returncode=128, stderr="fatal: not a git repository"
    )  # rev-parse HEAD pre-loop also fails
    runner.queue_result(returncode=0, stdout="")  # before_compare
    runner.queue_result(returncode=0, stdout="")  # after_compare
    runner.queue_result(
        returncode=128, stderr="fatal: not a git repository"
    )  # rev-parse HEAD iter 0 post also fails
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
        workspace=SimpleNamespace(id="ws_plan_fallback", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None
    # No git diff --name-only call should have been issued because rev-parse failed.
    diff_calls = [
        call for call in runner.calls if "diff" in call.args and "--name-only" in call.args
    ]
    assert not diff_calls


@pytest.mark.unit
async def test_planning_required_dirty_plan_still_accepted(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan
    runner.queue_result(returncode=0, stdout="old_sha\n")  # rev-parse HEAD
    runner.queue_result(
        returncode=0, stdout="?? docs/awf-plans/ws_plan_dirty.md\n"
    )  # dirty after planning
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
    runner.queue_result(returncode=0, stdout="old_sha\n")  # rev-parse HEAD pre-loop
    runner.queue_result(returncode=0, stdout="")  # before_compare
    runner.queue_result(returncode=0, stdout="")  # after_compare
    runner.queue_result(returncode=0, stdout="old_sha\n")  # rev-parse HEAD iter 0 post
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
        workspace=SimpleNamespace(id="ws_plan_dirty", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is None
    # Because no new commits, the diff call should return empty; porcelain still carries the plan.
    diff_calls = [
        call for call in runner.calls if "diff" in call.args and "--name-only" in call.args
    ]
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
        workspace=SimpleNamespace(id="ws_plan_extra_dirty", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path / "worktree",
        model=None,
    )

    assert message is not None
    assert not isinstance(message, str)
    assert message.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert message.details is not None
    scope = message.details["planning_scope"]
    assert scope["required_paths"] == ["docs/awf-plans/ws_plan_extra_dirty.md"]
    assert scope["offending_paths"] == ["src/extra.py"]


@pytest.mark.unit
def test_agent_pr_identity_prefers_nonblank_policy_model_override() -> None:
    defaults = AgentDefaults(model="default-model")

    assert (
        _agent_pr_identity(  # type: ignore[arg-type]
            SimpleNamespace(agent="codex", task_policy={"agent_model": "  gpt-special  "}),
            defaults=defaults,
        )
        == "agent: `codex`, model: `gpt-special`"
    )
    assert (
        _agent_pr_identity(  # type: ignore[arg-type]
            SimpleNamespace(agent="codex", task_policy={"agent_model": "   "}),
            defaults=defaults,
        )
        == "agent: `codex`, model: `default-model`"
    )
    assert (
        _agent_pr_identity(  # type: ignore[arg-type]
            SimpleNamespace(agent="codex", task_policy=None),
            defaults=None,
        )
        == "agent: `codex`"
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
def test_agent_pr_identity_cursor_lower_effort_omits_thinking_model() -> None:
    defaults = AgentDefaults(model="sonnet-4-thinking", effort="xhigh")

    assert (
        _agent_pr_identity(  # type: ignore[arg-type]
            SimpleNamespace(agent="cursor", task_policy={"agent_effort": "medium"}),
            defaults=defaults,
        )
        == "agent: `cursor`, effort: `medium`"
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
async def test_executor_pr_audit_event_defaults_source_head_sha_from_workspace() -> None:
    captured_events: list[dict[str, object]] = []

    class FakeWorkspaceRepository:
        async def add_audit_event(self, _workspace: object, **kwargs: object) -> None:
            captured_events.append(kwargs)

    workspace = SimpleNamespace(
        branch_name="awf/ws",
        remote_push_branch=None,
        pr_number=348,
        pr_url="https://example.test/pr/348",
        monitor_last_commit_sha="h" * 40,
        base_commit="b" * 40,
        branch_base="main",
    )

    await executor_monitor_handoff_audit._add_executor_pr_audit_event(
        object(),
        FakeWorkspaceRepository(),  # type: ignore[arg-type]
        workspace,  # type: ignore[arg-type]
        event_type="monitor_handoff.audit",
        action="handoff",
        outcome="succeeded",
        reason_code="monitor_handoff",
    )

    assert captured_events[0]["source_head_sha"] == "h" * 40


@pytest.mark.unit
async def test_executor_pr_audit_event_preserves_explicit_none_source_head_sha() -> None:
    captured_events: list[dict[str, object]] = []

    class FakeWorkspaceRepository:
        async def add_audit_event(self, _workspace: object, **kwargs: object) -> None:
            captured_events.append(kwargs)

    workspace = SimpleNamespace(
        branch_name="awf/ws",
        remote_push_branch=None,
        pr_number=348,
        pr_url="https://example.test/pr/348",
        monitor_last_commit_sha="h" * 40,
        base_commit="b" * 40,
        branch_base="main",
    )

    await executor_monitor_handoff_audit._add_executor_pr_audit_event(
        object(),
        FakeWorkspaceRepository(),  # type: ignore[arg-type]
        workspace,  # type: ignore[arg-type]
        event_type="monitor_handoff.audit",
        action="handoff",
        outcome="succeeded",
        reason_code="monitor_handoff",
        source_head_sha=None,
    )

    assert captured_events[0]["source_head_sha"] is None


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


@pytest.mark.unit
async def test_healthcheck_failure_event_noops_when_workspace_is_not_validating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    workspace = SimpleNamespace(id="ws_health_stale", status=WorkspaceStatus.completed.value)

    class FakeWorkspaceRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, workspace_id: str) -> object:
            assert workspace_id == workspace.id
            return workspace

        async def add_event(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("stale healthcheck failures should not add events")

    monkeypatch.setattr(executor_state_ops, "WorkspaceRepository", FakeWorkspaceRepository)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._session_factory = lambda: session  # type: ignore[method-assign]

    await executor._record_health_check_failed_event(
        workspace_id=workspace.id,
        failure=_command_result(tmp_path),
    )

    assert session.commits == 0


@pytest.mark.unit
async def test_healthcheck_failure_event_handles_none_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    workspace = SimpleNamespace(id="ws_health", status=WorkspaceStatus.validating.value)
    captured_events: list[dict[str, object]] = []

    class FakeWorkspaceRepository:
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
            **kwargs: object,
        ) -> None:
            captured_events.append({"event_type": event_type, **kwargs})

    monkeypatch.setattr(executor_state_ops, "WorkspaceRepository", FakeWorkspaceRepository)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._session_factory = lambda: session  # type: ignore[method-assign]

    failure = _command_result(tmp_path, returncode=1)
    object.__setattr__(failure, "metadata", None)
    object.__setattr__(failure, "phase", "healthcheck")
    object.__setattr__(failure, "command", "healthcheck")

    await executor._record_health_check_failed_event(
        workspace_id=workspace.id,
        failure=failure,
    )

    assert session.commits == 1
    assert len(captured_events) == 1
    assert captured_events[0]["event_type"] == "workspace.health_check_failed"
    payload = captured_events[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["stream_ids"] == {}


@pytest.mark.unit
async def test_stale_terminal_workspace_paths_record_ignored_callbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    workspace = SimpleNamespace(id="ws_terminal", status=WorkspaceStatus.completed.value)
    stale_events: list[str] = []
    ignored_callbacks: list[dict[str, object]] = []
    finished_callbacks: list[dict[str, object]] = []

    class FakeWorkspaceRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, workspace_id: str) -> object:
            assert workspace_id == workspace.id
            return workspace

        async def get_with_operations(self, workspace_id: str) -> object:
            assert workspace_id == workspace.id
            workspace.operations = []
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
            ignored_callbacks.append(
                {
                    "source": callback_source,
                    "action": callback_action,
                    "expected": expected_status.value,
                    "reason_code": reason_code,
                }
            )

        async def add_event(
            self,
            _workspace: object,
            *,
            event_type: str,
            **_kwargs: object,
        ) -> None:
            stale_events.append(event_type)

        async def transition_if_current(
            self,
            workspace_id: str,
            *,
            from_status: WorkspaceStatus,
            to: WorkspaceStatus,
            reason_code: str,
            payload: dict[str, Any] | None = None,
        ) -> object | None:
            assert workspace_id == workspace.id
            if workspace.status != from_status.value:
                return None
            workspace.status = to.value
            return workspace

        async def transition(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("stale terminal workspace should not transition")

    async def finish_ignored(
        _session: object,
        **kwargs: object,
    ) -> None:
        finished_callbacks.append(kwargs)

    monkeypatch.setattr(executor_state_ops, "WorkspaceRepository", FakeWorkspaceRepository)
    monkeypatch.setattr(executor_git_methods, "WorkspaceRepository", FakeWorkspaceRepository)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._session_factory = lambda: session  # type: ignore[method-assign]
    monkeypatch.setattr(
        executor,
        "_finish_ignored_stale_callback_operations_in_session",
        finish_ignored,
    )

    transitioned = await executor._transition_if_current(
        workspace.id,
        from_status=WorkspaceStatus.running,
        to=WorkspaceStatus.validating,
        reason="RUN_OK",
        action="start_validation",
    )
    worktree_available = await executor._ensure_worktree_available(
        workspace_id=workspace.id,
        worktree_path=tmp_path / "missing-worktree",
        expected=WorkspaceStatus.running,
        action="post_agent_commit",
        validation_run_id="vr_stale",
        requested_tier=2,
    )
    await executor._mark_failed(
        workspace_id=workspace.id,
        from_status=WorkspaceStatus.running,
        failure_reason=FailureReason.infrastructure_failure,
        message="late failure",
    )
    blocked = await executor._block_open_pr_reexecution_without_recovery(
        workspace_id=workspace.id,
    )

    assert transitioned is False
    assert worktree_available is False
    assert blocked.blocked is True
    assert [item["action"] for item in ignored_callbacks] == [
        "start_validation",
        "post_agent_commit",
        "mark_failed",
        "pr_reexecution_guard",
    ]
    assert len(finished_callbacks) == 3
    assert finished_callbacks[1]["validation_run_id"] == "vr_stale"
    assert finished_callbacks[1]["requested_tier"] == 2
    assert stale_events == ["workspace.stale_action_skipped"] * 4
    assert session.commits == 4


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

    monkeypatch.setattr(executor_git_methods, "WorkspaceRepository", FakeWorkspaceRepository)
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
    candidate_workspace = SimpleNamespace(
        base_commit="old-base", monitor_last_commit_sha="old-head"
    )
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

    monkeypatch.setattr(executor_git_methods, "WorkspaceRepository", FakeWorkspaceRepository)
    monkeypatch.setattr(
        executor_git_methods, "MergeCandidateRepository", FakeMergeCandidateRepository
    )
    monkeypatch.setattr(executor_git_methods, "sync_candidate_readiness", sync_readiness)
    monkeypatch.setattr(executor_git_methods, "finish_monitor_operation", finish_operation)
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
    active_stale = [
        SimpleNamespace(
            reason_code=REASON_TARGET_ADVANCED,
            trigger_type="target_advanced",
            trigger_ref="abc123",
            explanation="base moved",
        )
    ]
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

        async def list_active_for_candidate(self, candidate_id: str) -> list[object]:
            assert candidate_id == candidate.id
            return active_stale

    def sync_readiness(candidate_arg: object, **_kwargs: object) -> None:
        readiness_calls.append(candidate_arg)

    monkeypatch.setattr(
        executor_git_methods, "MergeCandidateRepository", FakeMergeCandidateRepository
    )
    monkeypatch.setattr(executor_git_methods, "StaleReasonRepository", FakeStaleReasonRepository)
    monkeypatch.setattr(executor_git_methods, "sync_candidate_readiness", sync_readiness)
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


@pytest.mark.unit
async def test_clear_rebase_recovery_staleness_preserves_validation_tier_stale_reason(
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
        stale_reason=VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
        workspace=SimpleNamespace(id="ws_rebase"),
        attempt=SimpleNamespace(id="attempt"),
    )
    active_stale = [
        SimpleNamespace(
            reason_code=REASON_TARGET_ADVANCED,
            trigger_type="target_advanced",
            trigger_ref="abc123",
            explanation="base moved",
        )
    ]

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
            active = kwargs["findings"]
            assert isinstance(active, list)
            assert active == []

        async def list_active_for_candidate(self, candidate_id: str) -> list[object]:
            assert candidate_id == candidate.id
            return active_stale

    def sync_readiness(candidate_arg: object, **_kwargs: object) -> None:
        assert candidate_arg is candidate

    monkeypatch.setattr(
        executor_git_methods, "MergeCandidateRepository", FakeMergeCandidateRepository
    )
    monkeypatch.setattr(executor_git_methods, "StaleReasonRepository", FakeStaleReasonRepository)
    monkeypatch.setattr(executor_git_methods, "sync_candidate_readiness", sync_readiness)
    executor = _executor_with_runner(FakeCommandRunner(), tmp_path)
    executor._session_factory = lambda: session  # type: ignore[method-assign]

    await executor._clear_rebase_recovery_staleness(workspace_id="ws_rebase")

    assert candidate.stale is True
    assert candidate.stale_reason == VALIDATION_INSUFFICIENT_TIER_STALE_REASON
    assert session.commits == 1
