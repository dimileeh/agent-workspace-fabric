"""Executor recovery branch — validation-only path for monitor-driven dispatch.

Recovery dispatched by the PR monitor (workspaces with a pending
`pr_monitor` validate operation) must NOT re-run planning/agent/feature
execution. The executor must skip Step 1 (`_run_agent_task_with_optional_planning`),
skip Step 1b (post-agent commit + branch-drift recovery), and proceed
directly to validation, push, and monitor handoff. The validation
fix-cycle prompt is allowed because it is `build_fix_prompt`, not the
feature task prompt.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populates registry
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor import execution_flow as execution_flow_mod
from awf.control.executor.validation_cleanup_guards import ExecutionValidationResult
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace as WorkspaceModel
from awf.db.repositories import (
    OperationRepository,
    ValidationRunRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.planning import (
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    PLAN_CONFORMANCE_UNSATISFIED,
)
from awf.runtime.pr_creator import PullRequestCreator, PullRequestError
from awf.runtime.validation import (
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    SETUP_DEPENDENCY_NETWORK_METADATA_KEY,
    ValidationCommandResult,
    ValidationResult,
    ValidationRunner,
)
from tests.postgres import postgres_test_engine
from tests.unit.control.executor_paths import _test_worktrees_root

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


def _queue_validation_head(fake: FakeCommandRunner, head: str = "deadbeef01") -> None:
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # pre-validation rev-parse HEAD


async def _force_workspace_status(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    status: WorkspaceStatus,
) -> None:
    async with factory() as s:
        await s.execute(
            sa_update(WorkspaceModel)
            .where(WorkspaceModel.id == workspace_id)
            .values(status=status.value)
        )
        await s.commit()


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


_FEATURE_TASK_PROMPT = "Implement the customer feature flag wiring for the staging dashboard."


def _make_executor(
    *,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    max_fix_passes: int = 5,
    pr_monitor_factory: Any = None,
    validation: Any = None,
) -> WorkspaceExecutor:
    compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
    validation = validation or ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
    pr = PullRequestCreator(fake)
    return WorkspaceExecutor(
        session_factory=factory,
        runner=fake,
        compose=compose,
        validation=validation,
        pr_creator=pr,
        config=ExecutorConfig(
            worktrees_root=tmp_path / "work" / "worktrees",
            compose_projects_root=tmp_path / "work" / "compose",
            default_models={
                AgentRuntime.codex: "gpt-5",
                AgentRuntime.claude_code: "sonnet",
                AgentRuntime.gemini: "gemini-2.5-pro",
            },
            max_validation_fix_passes=max_fix_passes,
        ),
        pr_monitor_factory=pr_monitor_factory,
    )


class _TerminalAfterSuccessfulValidation:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        final_status: WorkspaceStatus,
    ) -> None:
        self._factory = factory
        self._final_status = final_status
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> Any:
        self.calls.append(phase_names)
        if phase_names == ("post_agent", "validate"):
            await _force_workspace_status(self._factory, workspace_id, self._final_status)
        return SimpleValidationResult()

    async def run_profile_coverage(self, **_kwargs: Any) -> None:
        return None


class SimpleValidationResult:
    all_passed = True
    first_failure = None
    total_retries = 0
    commands: list[Any] = []
    coverage = None


class _SetupFailureValidation:
    def __init__(self, setup_result: ValidationResult) -> None:
        self._setup_result = setup_result
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        if phase_names == ("setup", "pre_agent"):
            return self._setup_result
        return ValidationResult()

    async def run_profile_coverage(self, **_kwargs: Any) -> None:
        return None


class _RecordingSetupFailureValidation(_SetupFailureValidation):
    def __init__(self, setup_result: ValidationResult, events: list[str]) -> None:
        super().__init__(setup_result)
        self._events = events

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **kwargs: Any,
    ) -> ValidationResult:
        if phase_names == ("setup", "pre_agent"):
            self._events.append("setup")
        return await super().run_profile_phases(phase_names=phase_names, **kwargs)


def _setup_dependency_exhausted_result(tmp_path: Path) -> ValidationCommandResult:
    stdout_path = tmp_path / "setup.stdout"
    stderr_path = tmp_path / "setup.stderr"
    stdout_path.write_text("setup stdout\n", encoding="utf-8")
    stderr_path.write_text("setup stderr\n", encoding="utf-8")
    return ValidationCommandResult(
        command="uv sync --extra dev",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="setup",
        reason_code=SETUP_DEPENDENCY_NETWORK_FAILURE,
        retry_count=2,
        metadata={
            SETUP_DEPENDENCY_NETWORK_METADATA_KEY: {
                "reason_code": SETUP_DEPENDENCY_NETWORK_FAILURE,
                "command": "uv sync --extra dev",
                "package": "docker==7.1.0",
                "host": "files.pythonhosted.org",
                "transient_category": "dns",
                "retryable": True,
                "retry_count": 2,
                "retry_budget": 2,
                "retry_exhausted": True,
                "diagnostic": "failed to lookup address information",
            }
        },
    )


def _generic_setup_failure_result(tmp_path: Path) -> ValidationCommandResult:
    stdout_path = tmp_path / "generic-setup.stdout"
    stderr_path = tmp_path / "generic-setup.stderr"
    stdout_path.write_text("setup stdout\n", encoding="utf-8")
    stderr_path.write_text("missing local configuration\n", encoding="utf-8")
    return ValidationCommandResult(
        command="./scripts/setup-local.sh",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="setup",
        reason_code="COMMAND_FAILED",
    )


def _setup_dependency_retry_success_result(tmp_path: Path) -> ValidationCommandResult:
    stdout_path = tmp_path / "setup-success.stdout"
    stderr_path = tmp_path / "setup-success.stderr"
    stdout_path.write_text("setup stdout\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    return ValidationCommandResult(
        command="uv sync --extra dev",
        returncode=0,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="setup",
        reason_code="COMMAND_OK",
        retry_count=1,
        metadata={
            SETUP_DEPENDENCY_NETWORK_METADATA_KEY: {
                "reason_code": "COMMAND_OK",
                "command": "uv sync --extra dev",
                "package": "docker==7.1.0",
                "host": "files.pythonhosted.org",
                "transient_category": "dns",
                "retryable": True,
                "retry_count": 1,
                "retry_budget": 2,
                "diagnostic": "temporary lookup failure recovered",
            }
        },
    )


async def _seed_ready_workspace_with_recovery(
    factory: async_sessionmaker[AsyncSession],
    *,
    pr_url: str = "https://github.com/x/y/pull/1",
    pr_number: int = 1,
    create_worktree: bool = True,
    recovery_mode: str = "validate_only",
    source: str = "pr_monitor",
    operation_type: OperationType = OperationType.validate,
    resolved_profile: dict[str, Any] | None = None,
    recovery_payload_overrides: dict[str, Any] | None = None,
    task_kind: str = "feature_branch_pr",
) -> str:
    """Insert a workspace already in ``ready`` with a pending `pr_monitor`
    validate operation — the shape the monitor's RECOVERY_DISPATCH path
    leaves behind.
    """
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="recovery test",
            task_prompt=_FEATURE_TASK_PROMPT,
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
            resolved_profile=resolved_profile,
            task_kind=task_kind,
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.monitor_last_commit_sha = "d" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.pr_url = pr_url
        ws.pr_number = pr_number
        ws.remote_push_branch = ws.branch_name
        # walk through the executor pipeline once, then re-enter ready via
        # RECOVERY_DISPATCH (mirrors the monitor's transition).
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="RECOVERY_DISPATCH")
        payload = {
            "owner": source,
            "source": source,
            "action": recovery_mode,
            "requested_action": "rebase" if recovery_mode == "rebase_only" else "validate",
            "reason": "validation_insufficient_tier",
            "reason_code": "VALIDATION_INSUFFICIENT_TIER",
            "recovery_mode": recovery_mode,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "source_head_sha": ws.monitor_last_commit_sha,
            "source_base_sha": ws.base_commit,
            "target_branch": ws.branch_base,
            "remote_branch": ws.remote_push_branch,
        }
        if recovery_payload_overrides:
            payload.update(recovery_payload_overrides)
        await OperationRepository(s).create(
            workspace_id=ws.id,
            operation_type=operation_type,
            payload=payload,
            idempotency_key=f"{source}:{recovery_mode}:{ws.id}",
        )
        await s.commit()
        if create_worktree:
            (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


async def _seed_sync_feature_pr_ready_workspace_with_recovery(
    factory: async_sessionmaker[AsyncSession],
    *,
    pr_url: str = "https://github.com/x/y/pull/206",
    pr_number: int = 206,
    head_repo_slug: str | None = None,
    source_head_sha: str = "d" * 40,
    source_base_sha: str = "a" * 40,
) -> str:
    """Seed an adopted feature PR workspace after monitor recovery dispatch."""
    async with factory() as s:
        repo = WorkspaceRepository(s)
        branch_name = "feature/existing-pr"
        adoption = {
            "repo_slug": "x/y",
            "pr_number": pr_number,
            "pr_url": pr_url,
            "head_ref": branch_name,
            "base_ref": "development",
            "head_sha": source_head_sha,
            "base_sha": source_base_sha,
            "source": "existing_github_pr",
        }
        if head_repo_slug is not None:
            adoption["head_repo_slug"] = head_repo_slug
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="adopted PR recovery test",
            task_prompt="Monitor and validate the existing PR.",
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
            task_kind="sync_feature_pr",
            remote_push_branch=branch_name,
            task_policy={
                "pr_adoption": adoption,
            },
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"feature-sync/{ws.id}"
        ws.base_commit = source_base_sha
        ws.monitor_last_commit_sha = source_head_sha
        ws.compose_project_name = f"awf_{ws.id}"
        ws.pr_url = pr_url
        ws.pr_number = pr_number
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="PR_ADOPTED")
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="RECOVERY_DISPATCH")
        await OperationRepository(s).create(
            workspace_id=ws.id,
            operation_type=OperationType.validate,
            payload={
                "owner": "pr_monitor",
                "source": "pr_monitor",
                "action": "validate_only",
                "requested_action": "validate",
                "reason": "validation_insufficient_tier",
                "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                "recovery_mode": "validate_only",
                "pr_number": pr_number,
                "pr_url": pr_url,
                "source_head_sha": source_head_sha,
                "source_base_sha": source_base_sha,
                "target_branch": ws.branch_base,
                "remote_branch": branch_name,
            },
            idempotency_key=f"pr_monitor:validate_only:{ws.id}",
        )
        await s.commit()
        (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


async def _seed_open_pr_ready_workspace_without_recovery(
    factory: async_sessionmaker[AsyncSession],
    *,
    create_worktree: bool = True,
) -> str:
    """Insert a post-PR workspace that was corrupted back to ``ready`` without
    the monitor/operator recovery operation that makes that step-back safe."""
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="corrupted post-pr ready row",
            task_prompt=_FEATURE_TASK_PROMPT,
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.monitor_last_commit_sha = "d" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.pr_url = "https://github.com/x/y/pull/9"
        ws.pr_number = 9
        ws.remote_push_branch = ws.branch_name
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="PR_OPENED")
        assert ws.monitor_started_at is not None
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="LEGACY_READY_RESET")
        await s.commit()
        if create_worktree:
            (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


async def _seed_ready_workspace_no_recovery(
    factory: async_sessionmaker[AsyncSession],
    *,
    create_worktree: bool = True,
) -> str:
    """Seed a workspace in ``ready`` WITHOUT any pr_monitor recovery
    operation (regression guard for the normal feature-execution path)."""
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="normal feature",
            task_prompt=_FEATURE_TASK_PROMPT,
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await s.commit()
        if create_worktree:
            (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


def _queue_push_and_pr(
    fake: FakeCommandRunner, *, pr_url: str = "https://github.com/x/y/pull/1"
) -> None:
    fake.queue_result(returncode=0, stdout="src/fix.py\n")  # final plan-only gate committed diff
    fake.queue_result(returncode=0, stdout="M\0src/fix.py\0")  # committed base..HEAD diff
    fake.queue_result(returncode=0, stdout="deadbeef01\n")  # rev-parse HEAD
    fake.queue_result(returncode=0, stdout="awf/ws_test\n")  # abbrev-ref HEAD
    fake.queue_result(returncode=0, stdout="abc1234 work\n")  # log ahead-of-base
    fake.queue_result(returncode=0)  # git push
    fake.queue_result(returncode=0, stdout=pr_url)  # gh pr create


def _queue_existing_pr_push(fake: FakeCommandRunner, *, head: str = "deadbeef01") -> None:
    fake.queue_result(returncode=0, stdout="src/fix.py\n")  # final plan-only gate committed diff
    fake.queue_result(returncode=0, stdout="M\0src/fix.py\0")  # committed base..HEAD diff
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # rev-parse HEAD
    fake.queue_result(returncode=0, stdout="awf/ws_test\n")  # abbrev-ref HEAD
    fake.queue_result(returncode=0, stdout=f"{head[:7]} fix\n")  # log ahead-of-base
    fake.queue_result(returncode=0)  # git push


def _queue_rebase_recovery(fake: FakeCommandRunner) -> None:
    fake.queue_result(returncode=0)  # git fetch origin <base>
    fake.queue_result(returncode=0)  # git switch <branch>
    fake.queue_result(returncode=1)  # git merge-base --is-ancestor origin/<base> HEAD
    fake.queue_result(returncode=0)  # git rebase origin/<base>
    fake.queue_result(returncode=0, stdout="b" * 40 + "\n")  # rev-parse origin/<base>
    fake.queue_result(returncode=0, stdout="c" * 40 + "\n")  # rev-parse HEAD
    fake.queue_result(returncode=0)  # git push --force-with-lease


def _queue_already_synced_rebase_recovery(fake: FakeCommandRunner) -> None:
    fake.queue_result(returncode=0)  # git fetch origin <base>
    fake.queue_result(returncode=0)  # git switch <branch>
    fake.queue_result(returncode=0)  # git merge-base --is-ancestor origin/<base> HEAD
    fake.queue_result(returncode=0, stdout="b" * 40 + "\n")  # rev-parse origin/<base>
    fake.queue_result(returncode=0, stdout="c" * 40 + "\n")  # rev-parse HEAD


def _all_adapter_args(fake: FakeCommandRunner) -> list[list[str]]:
    """Every `docker compose exec ... codex ...` invocation."""
    return [c.args for c in fake.calls if "exec" in c.args and "codex" in c.args]


def _all_adapter_prompt_values(fake: FakeCommandRunner) -> list[str]:
    """Every prompt streamed to `docker compose exec ... codex ...` on stdin."""
    prompts: list[str] = []
    for call in fake.calls:
        if "exec" not in call.args or "codex" not in call.args:
            continue
        if call.input_bytes is not None:
            prompts.append(call.input_bytes.decode())
    return prompts


def _all_adapter_prompts(fake: FakeCommandRunner) -> str:
    """Concatenate every adapter prompt invocation into a single string for substring search."""
    return "\n".join(_all_adapter_prompt_values(fake))


def _all_push_and_pr_create_calls(fake: FakeCommandRunner) -> list[list[str]]:
    """Every git push or gh pr create invocation."""
    return [
        c.args
        for c in fake.calls
        if ("push" in c.args and "git" in c.args) or (c.args[:3] == ["gh", "pr", "create"])
    ]


@pytest.mark.unit
async def test_recovery_skip_push_with_factory_resumes_monitor_runner(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Recovery with an existing PR and a pr_monitor_factory must transition
    to monitoring_pr AND immediately hand off to the monitor runner, matching
    the normal execution path (Step 4)."""
    monitor_calls: list[dict[str, Any]] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            monitor_calls.append({"workspace_id": workspace_id, "compose_project": compose_project})

    def _monitor_factory(*_args: Any, **_kwargs: Any) -> _FakeMonitor:
        return _FakeMonitor()

    executor = _make_executor(
        fake=fake, factory=factory, tmp_path=tmp_path, pr_monitor_factory=_monitor_factory
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, pr_url="https://github.com/x/y/pull/1"
    )

    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    assert _all_push_and_pr_create_calls(fake) == []
    assert len(monitor_calls) == 1
    assert monitor_calls[0]["workspace_id"] == ws_id

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert any(
            event.event_type == "workspace.state_changed"
            and event.reason_code == "RECOVERY_VALIDATION_OK"
            and event.old_state == WorkspaceStatus.validating.value
            and event.new_state == WorkspaceStatus.monitoring_pr.value
            for event in events
        )


@pytest.mark.unit
async def test_recovery_skip_push_cursor_lower_effort_handoff_uses_implicit_runtime_model(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Cursor lower effort must not hand the monitor a thinking model default."""
    captured: list[dict[str, object]] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            del workspace_id, compose_project, compose_file

    def _monitor_factory(
        adapter: Any,
        *_args: Any,
        provider_recovery_default_model: str | None = None,
        **_kwargs: Any,
    ) -> _FakeMonitor:
        captured.append(
            {
                "agent": adapter.name.value,
                "adapter_args": adapter._cli_args(model=None),
                "provider_recovery_default_model": provider_recovery_default_model,
            }
        )
        return _FakeMonitor()

    executor = _make_executor(
        fake=fake, factory=factory, tmp_path=tmp_path, pr_monitor_factory=_monitor_factory
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, pr_url="https://github.com/x/y/pull/1"
    )
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        ws.agent = AgentRuntime.cursor.value
        ws.task_policy = {"agent_effort": "medium"}
        await s.commit()

    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert captured == [
        {
            "agent": "cursor",
            "adapter_args": [
                "cursor-agent",
                "-p",
                "--force",
                "--output-format",
                "text",
            ],
            "provider_recovery_default_model": None,
        }
    ]


@pytest.mark.unit
async def test_sync_feature_pr_recovery_runs_validation_before_monitor_handoff(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Adopted PR workspaces must honor monitor recovery operations.

    A ``sync_feature_pr`` workspace re-entering ``ready`` from the PR monitor
    must run the pending validate-only recovery before it hands the PR back to
    the monitor. Otherwise the validate operation remains pending forever and
    the monitor loops on ``RECOVERY_IN_PROGRESS``.
    """
    monitor_calls: list[str] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            del compose_project, compose_file
            monitor_calls.append(workspace_id)

    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        pr_monitor_factory=lambda *_args, **_kwargs: _FakeMonitor(),
    )
    ws_id = await _seed_sync_feature_pr_ready_workspace_with_recovery(factory)

    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    assert _all_push_and_pr_create_calls(fake) == []
    assert monitor_calls == [ws_id]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)

    assert ws is not None
    assert ws.status == WorkspaceStatus.monitoring_pr.value
    assert ws.monitor_last_commit_sha == "d" * 40
    assert ws.base_commit == "a" * 40
    recovery_ops = [
        op
        for op in ops
        if op.type == OperationType.validate.value
        and isinstance(op.payload, dict)
        and op.payload.get("source") == "pr_monitor"
    ]
    assert len(recovery_ops) == 1
    assert recovery_ops[0].status == OperationStatus.succeeded.value
    assert len(runs) == 1
    assert runs[0].workspace_head_sha == "d" * 40
    assert runs[0].target_head_sha == "d" * 40
    assert any(
        event.event_type == "workspace.state_changed"
        and event.reason_code == "RECOVERY_VALIDATION_OK"
        and event.old_state == WorkspaceStatus.validating.value
        and event.new_state == WorkspaceStatus.monitoring_pr.value
        for event in events
    )


@pytest.mark.unit
async def test_validate_only_recovery_pushes_existing_pr_after_fix_commit(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """If validate-only recovery needs a fix pass, the validated local
    commit must be pushed back to the already-open PR before monitor handoff."""
    monitor_calls: list[str] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            del compose_project, compose_file
            monitor_calls.append(workspace_id)

    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        max_fix_passes=1,
        pr_monitor_factory=lambda *_args, **_kwargs: _FakeMonitor(),
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        pr_url="https://github.com/x/y/pull/161",
        pr_number=161,
    )
    source_head = "d" * 40
    fixed_head = "e" * 40

    _queue_validation_head(fake, head=source_head)
    fake.queue_result(returncode=1, stderr="pytest: failed")  # initial validation fails
    fake.queue_result(returncode=0)  # adapter.run (fix pass)
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/awf/onboarding.py\n")  # diff --cached
    fake.queue_result(returncode=0)  # git commit
    _queue_validation_head(fake, head=fixed_head)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation passes after fix
    _queue_existing_pr_push(fake, head=fixed_head)

    await executor.execute(ws_id)

    push_and_pr_calls = _all_push_and_pr_create_calls(fake)
    assert any(call[0] == "git" and "push" in call for call in push_and_pr_calls)
    assert not any(call[:3] == ["gh", "pr", "create"] for call in push_and_pr_calls)
    assert monitor_calls == [ws_id]

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)

    assert ws is not None
    assert ws.status == WorkspaceStatus.monitoring_pr.value
    assert ws.monitor_last_commit_sha == fixed_head
    assert runs[-1].workspace_head_sha == fixed_head
    assert runs[-1].target_head_sha == fixed_head
    assert any(
        event.event_type == "workspace.audit.git_push" and event.reason_code == "PR_UPDATED"
        for event in events
    )


@pytest.mark.unit
async def test_sync_feature_pr_validate_only_recovery_pushes_adopted_pr_head(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Adopted PR recovery must update the real PR head, not the local
    feature-sync branch used only inside the workspace."""
    monitor_calls: list[str] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            del compose_project, compose_file
            monitor_calls.append(workspace_id)

    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        max_fix_passes=1,
        pr_monitor_factory=lambda *_args, **_kwargs: _FakeMonitor(),
    )
    source_head = "d" * 40
    fixed_head = "e" * 40
    ws_id = await _seed_sync_feature_pr_ready_workspace_with_recovery(
        factory,
        pr_number=206,
        source_head_sha=source_head,
    )

    _queue_validation_head(fake, head=source_head)
    fake.queue_result(returncode=1, stderr="pytest: failed")  # initial validation fails
    fake.queue_result(returncode=0)  # adapter.run (fix pass)
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="tests/integration/test_alembic_postgres.py\n")
    fake.queue_result(returncode=0)  # git commit
    _queue_validation_head(fake, head=fixed_head)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation passes after fix
    _queue_existing_pr_push(fake, head=fixed_head)

    await executor.execute(ws_id)

    push_calls = [
        call for call in _all_push_and_pr_create_calls(fake) if call[0] == "git" and "push" in call
    ]
    assert len(push_calls) == 1
    assert "HEAD:refs/heads/feature/existing-pr" in push_calls[0]
    assert f"feature-sync/{ws_id}" not in push_calls[0]
    assert not any(
        call[:3] == ["gh", "pr", "create"] for call in _all_push_and_pr_create_calls(fake)
    )
    assert monitor_calls == [ws_id]

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)

    assert ws is not None
    assert ws.status == WorkspaceStatus.monitoring_pr.value
    assert ws.branch_name == f"feature-sync/{ws_id}"
    assert ws.remote_push_branch == "feature/existing-pr"
    assert ws.monitor_last_commit_sha == fixed_head
    assert runs[-1].workspace_head_sha == fixed_head
    assert runs[-1].target_head_sha == fixed_head
    assert any(
        event.event_type == "workspace.audit.git_push"
        and event.reason_code == "PR_UPDATED"
        and event.payload
        and event.payload["remote_branch"] == "feature/existing-pr"
        and event.payload["branch_name"] == f"feature-sync/{ws_id}"
        for event in events
    )


@pytest.mark.unit
@pytest.mark.parametrize("operation", ["git push", "gh pr create"])
async def test_sync_feature_pr_push_error_audit_records_adopted_pr_head(
    operation: str,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        max_fix_passes=1,
    )
    source_head = "d" * 40
    fixed_head = "e" * 40
    ws_id = await _seed_sync_feature_pr_ready_workspace_with_recovery(
        factory,
        pr_number=208,
        source_head_sha=source_head,
    )
    push_attempts: list[dict[str, Any]] = []

    async def fail_push_and_open(**kwargs: Any) -> None:
        push_attempts.append(kwargs)
        raise PullRequestError(
            operation=operation,
            returncode=128 if operation == "git push" else 1,
            stderr="remote rejected the adopted PR head",
            head_sha=fixed_head,
        )

    monkeypatch.setattr(executor._pr_creator, "push_and_open", fail_push_and_open)

    _queue_validation_head(fake, head=source_head)
    fake.queue_result(returncode=1, stderr="pytest: failed")  # initial validation fails
    fake.queue_result(returncode=0)  # adapter.run (fix pass)
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="tests/integration/test_alembic_postgres.py\n")
    fake.queue_result(returncode=0)  # git commit
    _queue_validation_head(fake, head=fixed_head)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation passes after fix
    # Final pre-push gates re-derive committed output: plan-only gate diffs
    # base..HEAD (name-only), then the protected-output gate diffs it again
    # (name-status). The fix pass committed real work, so both pass and the
    # adopted-PR push is attempted (and fails via the mocked creator below).
    fake.queue_result(  # plan-only committed diff
        returncode=0,
        stdout="tests/integration/test_alembic_postgres.py\n",
    )
    fake.queue_result(  # protected committed diff (name-status)
        returncode=0,
        stdout="M\0tests/integration/test_alembic_postgres.py\0",
    )

    await executor.execute(ws_id)

    assert len(push_attempts) == 1
    assert push_attempts[0]["branch_name"] == f"feature-sync/{ws_id}"
    assert push_attempts[0]["remote_branch_name"] == "feature/existing-pr"

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )
        pr_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type="workspace.audit.pr_created",
            limit=10,
        )

    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    events = push_events + pr_events
    assert len(events) == (1 if operation == "git push" else 2)
    assert all(event.payload is not None for event in events)
    for event in events:
        assert event.payload["remote_branch"] == "feature/existing-pr"
        assert event.payload["branch_name"] == f"feature-sync/{ws_id}"
        assert event.payload["source_head_sha"] == fixed_head
    assert any(event.payload["outcome"] == "failed" for event in events)


@pytest.mark.unit
async def test_sync_feature_pr_validate_only_recovery_pushes_fork_head_repo(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Adopted fork PR recovery must update the fork branch, not origin."""
    monitor_calls: list[str] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            del compose_project, compose_file
            monitor_calls.append(workspace_id)

    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        max_fix_passes=1,
        pr_monitor_factory=lambda *_args, **_kwargs: _FakeMonitor(),
    )
    source_head = "d" * 40
    fixed_head = "e" * 40
    ws_id = await _seed_sync_feature_pr_ready_workspace_with_recovery(
        factory,
        pr_number=207,
        head_repo_slug="contributor/aira-agent",
        source_head_sha=source_head,
    )

    _queue_validation_head(fake, head=source_head)
    fake.queue_result(returncode=1, stderr="pytest: failed")  # initial validation fails
    fake.queue_result(returncode=0)  # adapter.run (fix pass)
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/awf/runtime/pr_creator.py\n")
    fake.queue_result(returncode=0)  # git commit
    _queue_validation_head(fake, head=fixed_head)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation passes after fix
    _queue_existing_pr_push(fake, head=fixed_head)

    await executor.execute(ws_id)

    push_calls = [
        call for call in _all_push_and_pr_create_calls(fake) if call[0] == "git" and "push" in call
    ]
    assert len(push_calls) == 1
    push_index = push_calls[0].index("push")
    assert "-u" not in push_calls[0]
    assert push_calls[0][push_index + 1] == "git@github.com:contributor/aira-agent.git"
    assert "HEAD:refs/heads/feature/existing-pr" in push_calls[0]
    assert "origin" not in push_calls[0][push_index + 1 :]
    assert f"feature-sync/{ws_id}" not in push_calls[0]
    assert not any(
        call[:3] == ["gh", "pr", "create"] for call in _all_push_and_pr_create_calls(fake)
    )
    assert monitor_calls == [ws_id]


@pytest.mark.unit
async def test_validate_only_recovery_zero_adapter_calls_on_clean_pass(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Even stricter than the existing skip-planning test: recovery must
    issue zero ``docker compose exec`` adapter invocations on a clean
    validation pass (no fix-cycle needed)."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory)

    _queue_validation_head(fake)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    # Zero adapter calls of any kind — not planning, not execution, not
    # conformance, and not fix-cycle prompts.
    adapter_invocations = _all_adapter_args(fake)
    assert adapter_invocations == []


@pytest.mark.unit
async def test_validate_only_recovery_with_conformance_handoff_skips_report_commit(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        resolved_profile={
            "name": "planned-recovery",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
            },
            "phases": {"validate": ["pytest -q"]},
        },
        recovery_payload_overrides={
            "conformance": {
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "summary": "Recovery needs AWF-owned validation evidence.",
                "gaps": ["AWF-owned validation evidence is missing for pytest."],
            }
        },
    )

    report_path = f"docs/awf-plans/{ws_id}.conformance.json"
    source_head = "d" * 40
    _queue_validation_head(fake, head=source_head)
    fake.queue_result(returncode=0, stdout="tests ok")
    fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
    fake.queue_result(returncode=0, stdout=f"{source_head}\n")  # conformance scope HEAD
    fake.queue_result(
        returncode=0,
        stdout='{"status":"satisfied","summary":"validated recovery","gaps":[]}',
    )
    fake.queue_result(
        returncode=0,
        stdout=f"?? {report_path}\n",
    )
    fake.queue_result(returncode=0, stdout="")  # committed paths since scope HEAD
    # #544: the satisfied report is written but NOT committed (its path is
    # gitignored). HEAD therefore does not advance past the recovery source
    # head, so recovery declines to push a phantom report commit.
    fake.queue_result(returncode=0, stdout=f"{source_head}\n")  # post-conformance HEAD (unchanged)

    await executor.execute(ws_id)

    adapter_args = _all_adapter_args(fake)
    assert len(adapter_args) == 1
    prompt = _all_adapter_prompt_values(fake)[0]
    assert "## Conformance phase" in prompt
    assert "## Planning phase" not in prompt
    assert "## Execution phase" not in prompt
    assert "Validation evidence" in prompt
    assert "VALIDATION_OK" in prompt
    assert "validation.01_validate.stdout" in prompt

    git_calls = [call.args for call in fake.calls if call.args and call.args[0] == "git"]
    # The report is never staged or committed...
    assert not any(call[-3:] == ["add", "--", report_path] for call in git_calls)
    assert not any(
        "commit" in call and "awf: post-validation conformance report" in call for call in git_calls
    )
    # ...and with HEAD unchanged there is no phantom report-commit push.
    assert not any(call[0] == "git" and "push" in call for call in git_calls)
    assert not any(
        call[:3] == ["gh", "pr", "create"] for call in _all_push_and_pr_create_calls(fake)
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.completed.value
    assert ws.monitor_last_commit_sha == source_head
    assert runs[-1].workspace_head_sha == source_head
    assert runs[-1].target_head_sha == source_head
    assert not any(
        event.event_type == "workspace.audit.git_push" and event.reason_code == "PR_UPDATED"
        for event in events
    )
    recovery_ops = [
        op
        for op in ops
        if op.type == OperationType.validate.value
        and isinstance(op.payload, dict)
        and op.payload.get("source") == "pr_monitor"
        and op.payload.get("recovery_mode") == "validate_only"
    ]
    assert len(recovery_ops) == 1
    assert recovery_ops[0].status == OperationStatus.succeeded.value


@pytest.mark.unit
async def test_rebase_only_recovery_with_conformance_handoff_skips_report_commit(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        recovery_mode="rebase_only",
        resolved_profile={
            "name": "planned-rebase-recovery",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
            },
            "phases": {"validate": ["pytest -q"]},
        },
        recovery_payload_overrides={
            "conformance": {
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "summary": "Rebased recovery needs AWF-owned validation evidence.",
                "gaps": ["AWF-owned validation evidence is missing for pytest."],
            }
        },
    )

    report_path = f"docs/awf-plans/{ws_id}.conformance.json"
    rebased_head = "c" * 40
    _queue_rebase_recovery(fake)
    _queue_validation_head(fake, head=rebased_head)
    fake.queue_result(returncode=0, stdout="tests ok")
    fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
    fake.queue_result(returncode=0, stdout=f"{rebased_head}\n")  # conformance scope HEAD
    fake.queue_result(
        returncode=0,
        stdout='{"status":"satisfied","summary":"validated rebased recovery","gaps":[]}',
    )
    fake.queue_result(returncode=0, stdout=f"?? {report_path}\n")
    fake.queue_result(returncode=0, stdout="")  # committed paths since scope HEAD
    # #544: the satisfied report is written but NOT committed. HEAD stays at the
    # rebased head, so recovery only retains the rebase force-push and does not
    # add a second phantom report-commit push.
    fake.queue_result(returncode=0, stdout=f"{rebased_head}\n")  # post-conformance HEAD (unchanged)

    await executor.execute(ws_id)

    git_calls = [call.args for call in fake.calls if call.args and call.args[0] == "git"]
    git_push_calls = [call for call in git_calls if "push" in call]
    # The report is never staged or committed...
    assert not any(call[-3:] == ["add", "--", report_path] for call in git_calls)
    assert not any(
        "commit" in call and "awf: post-validation conformance report" in call for call in git_calls
    )
    # ...the only push is the rebase force-push (no non-force report push).
    assert any("--force-with-lease" in call for call in git_push_calls)
    assert not any("--force-with-lease" not in call for call in git_push_calls)
    assert not any(
        call[:3] == ["gh", "pr", "create"] for call in _all_push_and_pr_create_calls(fake)
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.completed.value
    assert ws.monitor_last_commit_sha == rebased_head
    assert runs[-1].workspace_head_sha == rebased_head
    assert runs[-1].target_head_sha == rebased_head
    assert any(
        event.event_type == "workspace.audit.git_push" and event.reason_code == "REBASE_OK"
        for event in events
    )
    assert not any(
        event.event_type == "workspace.audit.git_push" and event.reason_code == "PR_UPDATED"
        for event in events
    )


@pytest.mark.unit
async def test_validate_only_recovery_conformance_failure_fails_without_fix_loop(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        max_fix_passes=1,
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        resolved_profile={
            "name": "planned-recovery",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
            },
            "phases": {"validate": ["pytest -q"]},
        },
        recovery_payload_overrides={
            "conformance": {
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "summary": "Recovery needs AWF-owned validation evidence.",
                "gaps": ["AWF-owned validation evidence is missing for pytest."],
            }
        },
    )

    source_head = "d" * 40
    unsatisfied_report = (
        '{"status":"needs_iteration",'
        '"summary":"validation evidence still does not satisfy the plan",'
        '"reason_code":"PLAN_CONFORMANCE_VALIDATION_EVIDENCE_GAP",'
        '"gaps":["profile validation evidence is still insufficient"]}'
    )
    _queue_validation_head(fake, head=source_head)
    fake.queue_result(returncode=0, stdout="tests ok")
    fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
    fake.queue_result(returncode=0, stdout=f"{source_head}\n")  # conformance scope HEAD
    fake.queue_result(returncode=0, stdout=unsatisfied_report)
    fake.queue_result(returncode=0, stdout="")  # post-validation conformance after status
    fake.queue_result(returncode=0, stdout="")  # committed paths since scope HEAD

    # These entries document the old, wasteful path: a synthetic validation
    # failure drove a fix prompt and a second full validation run. They must
    # remain unused.
    fake.queue_result(returncode=0, stdout="attempted fix")  # adapter.run (fix pass)
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="")  # git diff --cached --name-only
    _queue_validation_head(fake, head=source_head)
    fake.queue_result(returncode=0, stdout="tests ok again")
    fake.queue_result(returncode=0, stdout="")
    fake.queue_result(returncode=0, stdout=f"{source_head}\n")
    fake.queue_result(returncode=0, stdout=unsatisfied_report)
    fake.queue_result(returncode=0, stdout="")
    fake.queue_result(returncode=0, stdout="")

    with structlog.testing.capture_logs() as captured:
        await executor.execute(ws_id)

    adapter_args = _all_adapter_args(fake)
    assert len(adapter_args) == 1
    prompt = _all_adapter_prompt_values(fake)[0]
    assert "## Conformance phase" in prompt
    assert "Validation failed after your previous pass" not in prompt
    assert any(
        event.get("event") == "executor.post_validation_conformance_recovery_single_attempt"
        and event.get("workspace_id") == ws_id
        and event.get("recovery_mode") == "validate_only"
        and event.get("will_retry") is False
        for event in captured
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        runs = await ValidationRunRepository(s).list_for_workspace(ws_id)

    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    assert ws.failure_reason == "agent_failure"
    assert ws.failure_message is not None
    assert "post-validation plan conformance was not satisfied" in ws.failure_message
    assert len(runs) == 1
    assert runs[0].status == "succeeded"
    recovery_ops = [
        op
        for op in ops
        if op.type == OperationType.validate.value
        and isinstance(op.payload, dict)
        and op.payload.get("source") == "pr_monitor"
        and op.payload.get("recovery_mode") == "validate_only"
    ]
    assert len(recovery_ops) == 1
    assert recovery_ops[0].status == OperationStatus.failed.value
    assert recovery_ops[0].error_code == PLAN_CONFORMANCE_UNSATISFIED
    assert isinstance(recovery_ops[0].result, dict)
    assert recovery_ops[0].result.get("reason_code") == PLAN_CONFORMANCE_UNSATISFIED


@pytest.mark.unit
async def test_rebase_only_recovery_rebases_pushes_and_skips_pr_recreate(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Rebase-only recovery updates the existing PR branch but does not
    recreate the PR."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory, recovery_mode="rebase_only")

    _queue_rebase_recovery(fake)
    _queue_validation_head(fake, head="c" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    assert not any(call.args[:3] == ["gh", "pr", "create"] for call in fake.calls)
    assert any(
        call.args[0] == "git" and "push" in call.args and "--force-with-lease" in call.args
        for call in fake.calls
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )
        assert ws is not None
        assert ws.status in {
            WorkspaceStatus.completed.value,
            WorkspaceStatus.monitoring_pr.value,
        }
    assert len(push_events) == 1
    assert push_events[0].reason_code == "REBASE_OK"
    assert push_events[0].payload == {
        "schema": "control_audit.v1",
        "actor": "executor",
        "source": "executor",
        "action": "rebase_recovery_push",
        "outcome": "succeeded",
        "reason_code": "REBASE_OK",
        "operation_id": push_events[0].payload["operation_id"],
        "operation_type": "rebase",
        "pr_number": 1,
        "pr_url": "https://github.com/x/y/pull/1",
        "source_head_sha": "c" * 40,
        "source_base_sha": "b" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{ws_id}",
        "branch_name": f"awf/{ws_id}",
        "evidence": {
            "previous_source_base_sha": "a" * 40,
            "previous_source_head_sha": "d" * 40,
            "rebased": True,
        },
    }


@pytest.mark.unit
async def test_rebase_only_validation_cleanup_recovery_uses_rebased_base(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory, recovery_mode="rebase_only")
    captured_base_commits: list[str | None] = []

    async def _repair_hooks_after_cleanup_failure(**_kwargs: object) -> bool:
        return True

    async def _recover_missing_head_after_cleanup_failure(
        _exc: ComposeExecCleanupError,
        **kwargs: object,
    ) -> bool:
        captured_base_commits.append(kwargs["base_commit"])
        return True

    async def _run_validation_and_fix_cycle(
        *_args: object,
        **kwargs: object,
    ) -> ExecutionValidationResult:
        cleanup_repair = kwargs["after_agent_cleanup_failure_repair"]
        assert callable(cleanup_repair)
        repaired = await cleanup_repair(
            ComposeExecCleanupError(
                invocation_id="awf_validation_cleanup",
                source="validation",
                label="fix-pass",
                message='service "agent" is not running',
                cleanup_result=CommandResult(
                    returncode=1,
                    stdout="",
                    stderr='service "agent" is not running',
                ),
            )
        )
        assert repaired is True
        return ExecutionValidationResult(
            stop=True,
            successful_validation_run_id=None,
            successful_validation_workspace_head_sha=None,
        )

    monkeypatch.setattr(
        execution_flow_mod,
        "repair_mirror_hooks_path_or_mark_failed",
        _repair_hooks_after_cleanup_failure,
    )
    monkeypatch.setattr(
        execution_flow_mod,
        "repair_mirror_hooks_path_after_agent_cleanup_failure",
        _repair_hooks_after_cleanup_failure,
    )
    monkeypatch.setattr(
        execution_flow_mod,
        "recover_missing_head_after_cleanup_failure",
        _recover_missing_head_after_cleanup_failure,
    )
    monkeypatch.setattr(
        execution_flow_mod._execution_validation,
        "run_validation_and_fix_cycle",
        _run_validation_and_fix_cycle,
    )

    _queue_rebase_recovery(fake)

    await executor.execute(ws_id)

    assert captured_base_commits == ["b" * 40]


@pytest.mark.unit
async def test_rebase_only_recovery_push_failure_records_redacted_audit(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory, recovery_mode="rebase_only")

    fake.queue_result(returncode=0)  # git fetch origin <base>
    fake.queue_result(returncode=0)  # git switch <branch>
    fake.queue_result(returncode=1)  # git merge-base --is-ancestor origin/<base> HEAD
    fake.queue_result(returncode=0)  # git rebase origin/<base>
    fake.queue_result(returncode=0, stdout="b" * 40 + "\n")  # rev-parse origin/<base>
    fake.queue_result(returncode=0, stdout="c" * 40 + "\n")  # rev-parse HEAD
    fake.queue_result(
        returncode=128,
        stderr=("fatal: unable to access https://user:ghp_should_not_persist@github.com/org/repo"),
    )

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )

    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    assert "ghp_should_not_persist" not in (ws.failure_message or "")
    assert "https://[redacted]@github.com/org/repo" in (ws.failure_message or "")
    assert len(push_events) == 1
    assert push_events[0].reason_code == "MONITOR_RECOVERY_REBASE_FAILED"
    assert push_events[0].payload is not None
    assert push_events[0].payload["action"] == "rebase_recovery_push"
    assert push_events[0].payload["outcome"] == "failed"
    assert push_events[0].payload["source_head_sha"] == "c" * 40
    assert push_events[0].payload["source_base_sha"] == "b" * 40
    assert push_events[0].payload["evidence"]["operation"] == "git push --force-with-lease"
    assert push_events[0].payload["evidence"]["returncode"] == 128
    assert "ghp_should_not_persist" not in repr(push_events[0].payload)
    assert "https://[redacted]@github.com/org/repo" in repr(push_events[0].payload)


@pytest.mark.unit
async def test_rebase_only_recovery_marks_operation_failed_when_recording_raises(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory, recovery_mode="rebase_only")

    async def fail_record_success(**_kwargs: object) -> None:
        raise RuntimeError("write exploded")

    monkeypatch.setattr(
        executor,
        "_record_rebase_recovery_success",
        fail_record_success,
    )
    _queue_rebase_recovery(fake)

    with pytest.raises(RuntimeError, match="write exploded"):
        await executor._run_monitor_rebase_recovery(
            workspace_id=ws_id,
            worktree_path=_test_worktrees_root(factory) / ws_id,
            base_branch="development",
            branch_name=f"awf/{ws_id}",
            remote_branch=f"awf/{ws_id}",
            reason="validation_insufficient_tier",
            recovery_payload={
                "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                "pr_number": 1,
                "source_base_sha": "a" * 40,
                "source_head_sha": "d" * 40,
            },
        )

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    rebase_ops = [op for op in ops if op.type == OperationType.rebase.value]
    assert len(rebase_ops) == 1
    assert rebase_ops[0].status == OperationStatus.failed.value
    assert rebase_ops[0].error_code == "MONITOR_RECOVERY_REBASE_FAILED"
    assert rebase_ops[0].error_message == "write exploded"
    assert isinstance(rebase_ops[0].result, dict)
    assert rebase_ops[0].result["reason_code"] == "MONITOR_RECOVERY_REBASE_FAILED"
