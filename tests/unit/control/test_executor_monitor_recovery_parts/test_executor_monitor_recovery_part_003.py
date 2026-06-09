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
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populates registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor.types import _MonitorRebaseRecoveryError
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace as WorkspaceModel
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import (
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    SETUP_DEPENDENCY_NETWORK_METADATA_KEY,
    ValidationCommandResult,
    ValidationResult,
    ValidationRunner,
)
from tests.postgres import postgres_test_engine
from tests.unit.control.executor_paths import _test_worktree_path, _test_worktrees_root

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


def _queue_validation_head(fake: FakeCommandRunner, head: str = "deadbeef01") -> None:
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # pre-validation rev-parse HEAD


def _queue_post_validation_conformance_report_commit(
    fake: FakeCommandRunner, report_path: str
) -> None:
    fake.queue_result(returncode=0)  # git add report
    fake.queue_result(returncode=0, stdout=f"{report_path}\n")  # cached report diff
    fake.queue_result(returncode=0)  # commit refreshed report


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
    fake.queue_result(
        returncode=0
    )  # git merge-base --is-ancestor origin/<base> origin/<remote_branch>
    fake.queue_result(returncode=0, stdout="b" * 40 + "\n")  # rev-parse origin/<base>
    fake.queue_result(returncode=0, stdout="c" * 40 + "\n")  # rev-parse HEAD


def _queue_synced_base_lagging_remote_recovery(fake: FakeCommandRunner) -> None:
    fake.queue_result(returncode=0)  # git fetch origin <base>
    fake.queue_result(returncode=0)  # git switch <branch>
    fake.queue_result(returncode=0)  # git merge-base --is-ancestor origin/<base> HEAD
    fake.queue_result(
        returncode=1
    )  # git merge-base --is-ancestor origin/<base> origin/<remote_branch>
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
async def test_rebase_recovery_reuses_active_operation_with_partial_payload_identity(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        recovery_mode="rebase_only",
        source="operator_api",
        operation_type=OperationType.rebase,
    )

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        rebase_op = next(op for op in ops if op.type == OperationType.rebase.value)
        rebase_op.payload = {
            **dict(rebase_op.payload or {}),
            "candidate_id": "candidate-1",
            "log_stream_refs": {"monitor": "monitor.log"},
        }
        rebase_op_id = rebase_op.id
        await s.commit()

    fake.queue_result(returncode=0)  # git fetch origin <base>
    fake.queue_result(returncode=0)  # git switch <branch>
    fake.queue_result(returncode=1)  # git merge-base --is-ancestor origin/<base> HEAD
    fake.queue_result(returncode=1, stderr="conflict on README.md")  # git rebase
    fake.queue_result(returncode=0)  # git rebase --abort

    with pytest.raises(_MonitorRebaseRecoveryError):
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
    assert [op.id for op in rebase_ops] == [rebase_op_id]
    assert rebase_ops[0].status == OperationStatus.failed.value
    assert rebase_ops[0].payload is not None
    assert rebase_ops[0].payload["candidate_id"] == "candidate-1"
    assert rebase_ops[0].payload["log_stream_refs"] == {"monitor": "monitor.log"}


@pytest.mark.unit
async def test_rebase_only_recovery_skips_rebase_when_target_already_merged(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """If an earlier SyncBase already merged the target branch into the
    PR branch, rebase recovery should record that refreshed head and move
    straight to Tier 2 validation instead of replaying commits again."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory, recovery_mode="rebase_only")

    _queue_already_synced_rebase_recovery(fake)
    _queue_validation_head(fake, head="c" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    git_calls = [call.args for call in fake.calls if call.args and call.args[0] == "git"]
    assert not any("rebase" in call for call in git_calls)
    assert not any("push" in call for call in git_calls)
    assert any("merge-base" in call for call in git_calls)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.base_commit == "b" * 40
        assert ws.monitor_last_commit_sha == "c" * 40
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    rebase_ops = [op for op in ops if op.type == OperationType.rebase.value]
    assert len(rebase_ops) == 1
    assert rebase_ops[0].status == OperationStatus.succeeded.value
    assert rebase_ops[0].result == {
        "status": "succeeded",
        "reason_code": "REBASE_OK",
        "source_base_sha": "a" * 40,
        "source_head_sha": "d" * 40,
        "target_base_sha": "b" * 40,
        "target_head_sha": "c" * 40,
        "pushed": False,
        "rebased": False,
    }


@pytest.mark.unit
async def test_rebase_only_recovery_pushes_already_rebased_head_when_remote_lags(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory, recovery_mode="rebase_only")

    _queue_synced_base_lagging_remote_recovery(fake)
    _queue_validation_head(fake, head="c" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")
    _queue_existing_pr_push(fake, head="c" * 40)

    await executor.execute(ws_id)

    git_calls = [call.args for call in fake.calls if call.args and call.args[0] == "git"]
    assert not any("rebase" in call for call in git_calls)
    assert not any("push" in call and "--force-with-lease" in call for call in git_calls)
    assert any("push" in call and f"HEAD:refs/heads/awf/{ws_id}" in call for call in git_calls)

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    rebase_ops = [op for op in ops if op.type == OperationType.rebase.value]
    assert len(rebase_ops) == 1
    assert rebase_ops[0].result == {
        "status": "succeeded",
        "reason_code": "REBASE_OK",
        "source_base_sha": "a" * 40,
        "source_head_sha": "d" * 40,
        "target_base_sha": "b" * 40,
        "target_head_sha": "c" * 40,
        "pushed": False,
        "rebased": False,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "final_status",
    [
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroyed,
        WorkspaceStatus.destroying,
        WorkspaceStatus.completed,
        WorkspaceStatus.failed,
    ],
)
async def test_stale_callback_terminal_status_blocks_recovery(
    final_status: WorkspaceStatus,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """If a workspace enters a callback-terminal state after executor claim,
    recovery must stop and close the monitor-created operation as ignored."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory)

    original_recheck = executor._recheck_status

    async def _patched_recheck(
        workspace_id: str,
        *,
        expected: WorkspaceStatus,
        action: str,
        reason_code: str = "EXECUTOR_STALE_STATUS",
    ) -> bool:
        if action == "execute" and expected == WorkspaceStatus.running:
            await _force_workspace_status(factory, workspace_id, final_status)
        return await original_recheck(
            workspace_id,
            expected=expected,
            action=action,
            reason_code=reason_code,
        )

    executor._recheck_status = _patched_recheck

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == final_status.value
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id, limit=20)
    pr_monitor_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
    ]
    assert len(pr_monitor_ops) == 1
    assert pr_monitor_ops[0].status == OperationStatus.cancelled.value
    assert pr_monitor_ops[0].result == {
        "status": "ignored",
        "reason_code": "STALE_CALLBACK_IGNORED",
        "callback_source": "executor",
        "callback_action": "execute",
        "expected_status": WorkspaceStatus.running.value,
        "actual_status": final_status.value,
    }
    ignored_events = [
        event for event in events if event.event_type == "workspace.stale_callback_ignored"
    ]
    assert ignored_events[-1].reason_code == "STALE_CALLBACK_IGNORED"
    assert ignored_events[-1].payload == {
        "callback_source": "executor",
        "callback_action": "execute",
        "expected_status": WorkspaceStatus.running.value,
        "actual_status": final_status.value,
        "reason_code": "EXECUTOR_STALE_STATUS",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "final_status",
    [
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroyed,
        WorkspaceStatus.destroying,
        WorkspaceStatus.completed,
        WorkspaceStatus.failed,
    ],
)
async def test_stale_validation_callback_terminal_status_cancels_recovery_operation(
    final_status: WorkspaceStatus,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    monitor_calls: list[str] = []
    validation = _TerminalAfterSuccessfulValidation(factory, final_status)

    class _Monitor:
        async def run(
            self,
            *,
            workspace_id: str,
            compose_project: str,
            compose_file: Path,
        ) -> None:
            del compose_project, compose_file
            monitor_calls.append(workspace_id)

    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        validation=validation,
        pr_monitor_factory=lambda *_args: _Monitor(),
    )
    ws_id = await _seed_ready_workspace_with_recovery(factory)
    _queue_validation_head(fake)

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id, limit=30)
    pr_monitor_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
    ]

    assert validation.calls == [("setup", "pre_agent"), ("post_agent", "validate")]
    assert ws.status == final_status.value
    assert monitor_calls == []
    assert pr_monitor_ops[0].status == OperationStatus.cancelled.value
    assert pr_monitor_ops[0].result == {
        "status": "ignored",
        "reason_code": "STALE_CALLBACK_IGNORED",
        "callback_source": "executor",
        "callback_action": "validate",
        "expected_status": WorkspaceStatus.validating.value,
        "actual_status": final_status.value,
        "validation_run_id": pr_monitor_ops[0].result["validation_run_id"],
        "requested_tier": pr_monitor_ops[0].result["requested_tier"],
        "log_stream_refs": pr_monitor_ops[0].result["log_stream_refs"],
    }
    ignored_events = [
        event for event in events if event.event_type == "workspace.stale_callback_ignored"
    ]
    assert ignored_events[-1].payload == {
        "callback_source": "executor",
        "callback_action": "validate",
        "expected_status": WorkspaceStatus.validating.value,
        "actual_status": final_status.value,
        "reason_code": "STALE_CALLBACK_IGNORED",
    }


@pytest.mark.unit
async def test_executor_recovery_does_not_run_planning_when_planning_profile_required(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Even when the workspace's profile mandates planning, recovery
    must skip plan/execute/compare entirely. This is the strongest
    form of the "do not rewrite plan files" rule."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)

    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="recovery planned",
            task_prompt=_FEATURE_TASK_PROMPT,
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
            resolved_profile={
                "name": "planned",
                "planning": {
                    "required": True,
                    "plan_path": "docs/awf-plans/{workspace_id}.md",
                    "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
                    "max_iterations": 1,
                    "enforce_plan_only_changes": True,
                },
                "phases": {"validate": ["pytest -q"]},
            },
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.pr_url = "https://github.com/x/y/pull/1"
        ws.pr_number = 1
        ws.remote_push_branch = ws.branch_name
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="RECOVERY_DISPATCH")
        await OperationRepository(s).create(
            workspace_id=ws.id,
            operation_type=OperationType.validate,
            payload={
                "source": "pr_monitor",
                "reason": "validation_insufficient_tier",
                "recovery_mode": "validate_only",
            },
        )
        await s.commit()
        ws_id = ws.id
        (_test_worktrees_root(factory) / ws_id).mkdir(parents=True, exist_ok=True)

    plan_path = _test_worktree_path(factory, ws_id) / "docs" / "awf-plans" / f"{ws_id}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# pre-existing plan\n", encoding="utf-8")
    plan_mtime_before = plan_path.stat().st_mtime

    _queue_validation_head(fake)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation
    _queue_push_and_pr(fake)

    await executor.execute(ws_id)

    prompts = _all_adapter_prompts(fake)
    assert "## Planning phase" not in prompts
    assert "## Execution phase" not in prompts
    assert "## Conformance phase" not in prompts
    assert plan_path.read_text(encoding="utf-8") == "# pre-existing plan\n"
    assert plan_path.stat().st_mtime == plan_mtime_before


@pytest.mark.unit
async def test_rebase_recovery_reuses_active_operation_with_extra_payload_context(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        recovery_mode="rebase_only",
        source="operator_api",
        operation_type=OperationType.rebase,
    )

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        rebase_op = next(op for op in ops if op.type == OperationType.rebase.value)
        rebase_op.payload = {
            **dict(rebase_op.payload or {}),
            "candidate_id": "candidate-1",
            "log_stream_refs": {"monitor": "monitor.log"},
        }
        rebase_op_id = rebase_op.id
        await s.commit()

    fake.queue_result(returncode=0)  # git fetch origin <base>
    fake.queue_result(returncode=0)  # git switch <branch>
    fake.queue_result(returncode=1)  # git merge-base --is-ancestor origin/<base> HEAD
    fake.queue_result(returncode=1, stderr="conflict on README.md")  # git rebase
    fake.queue_result(returncode=0)  # git rebase --abort

    with pytest.raises(_MonitorRebaseRecoveryError):
        await executor._run_monitor_rebase_recovery(
            workspace_id=ws_id,
            worktree_path=_test_worktrees_root(factory) / ws_id,
            base_branch="development",
            branch_name=f"awf/{ws_id}",
            remote_branch=f"awf/{ws_id}",
            reason="validation_insufficient_tier",
            recovery_payload={
                "source": "operator_api",
                "recovery_mode": "rebase_only",
                "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                "pr_number": 1,
                "source_base_sha": "a" * 40,
                "source_head_sha": "d" * 40,
            },
        )

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    rebase_ops = [op for op in ops if op.type == OperationType.rebase.value]
    assert [op.id for op in rebase_ops] == [rebase_op_id]
    assert rebase_ops[0].status == OperationStatus.failed.value
    assert rebase_ops[0].payload is not None
    assert rebase_ops[0].payload["candidate_id"] == "candidate-1"
    assert rebase_ops[0].payload["log_stream_refs"] == {"monitor": "monitor.log"}
