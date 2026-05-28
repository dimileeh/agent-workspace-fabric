"""Error-path coverage for ``awf.control.executor.WorkspaceExecutor``.

The happy/failure paths are covered in ``test_executor.py``. This
file targets specific error branches that need dedicated fixtures:

 - Constructor validation: pr_monitor + pr_monitor_factory can't both
   be set (line 107).
 - Unexpected exception during agent run (lines 166-174).
 - Missing base_commit on workspace (lines 192-202).
 - Commit step raises RuntimeError when git commit exits non-zero
   (line 227).
 - Unexpected exception wrapping the commit step (lines 318-326).
 - pr_monitor_factory path (line 501) — factory invoked with adapter.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import AsyncioSubprocessRunner, FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor import execution_flow as executor_execution_flow
from awf.control.executor import git_ops as executor_git_ops
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import (
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.profiles.models import ProfileMonitor, WorkspaceProfile
from awf.runtime.logs import LogStore
from awf.runtime.pr_creator import PullRequestCreator, PullRequestResult
from awf.runtime.validation import (
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
    ValidationRunner,
)
from tests.postgres import postgres_test_engine
from tests.unit.control.executor_paths import _test_worktree_path, _test_worktrees_root

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


def _queue_validation_head(fake: FakeCommandRunner, head: str = "deadbeef01") -> None:
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # pre-validation rev-parse HEAD


def _queue_pre_push_checks(
    fake: FakeCommandRunner, *, head: str = "deadbeef01", include_plan_only_diff: bool = False
) -> None:
    if include_plan_only_diff:
        fake.queue_result(returncode=0, stdout="src/fix.py\n")  # plan-only committed diff
    fake.queue_result(returncode=0, stdout="M\0src/fix.py\0")  # committed base..HEAD diff
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # pre-push rev-parse HEAD
    fake.queue_result(returncode=0, stdout="awf/x\n")  # pre-push abbrev-ref
    fake.queue_result(returncode=0, stdout="ab commit\n")  # pre-push log


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


def _make_executor(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    pr_monitor_factory: Any = None,
    compose: Any = None,
    validation: Any = None,
    pr_creator: Any = None,
    log_store: LogStore | None = None,
    max_validation_fix_passes: int = 5,
) -> WorkspaceExecutor:
    compose = compose or _NoopResumeCompose()
    validation = validation or ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
    pr = pr_creator or PullRequestCreator(fake)
    return WorkspaceExecutor(
        session_factory=factory,
        runner=fake,
        compose=compose,
        validation=validation,
        pr_creator=pr,
        config=ExecutorConfig(
            worktrees_root=tmp_path / "work" / "worktrees",
            compose_projects_root=tmp_path / "work" / "compose",
            max_validation_fix_passes=max_validation_fix_passes,
            default_models={
                AgentRuntime.codex: "gpt-5",
                AgentRuntime.claude_code: "sonnet",
                AgentRuntime.gemini: "gemini-2.5-pro",
            },
        ),
        pr_monitor_factory=pr_monitor_factory,
        log_store=log_store,
    )


class _NoopResumeCompose:
    async def ensure_project_up(
        self,
        *,
        project_name: str,
        compose_file: Path,
        workspace_id: str,
        wait: bool = True,
        compose_up_timeout_seconds: int = 300,
    ) -> None:
        del project_name, compose_file, workspace_id, wait, compose_up_timeout_seconds


class _RecordingValidation:
    def __init__(
        self,
        *,
        phase_result: ValidationResult | None = None,
        coverage_result: ValidationCoverageResult | None = None,
    ) -> None:
        self._phase_result = phase_result or ValidationResult()
        self._coverage_result = coverage_result
        self.calls: list[tuple[str, ...]] = []
        self.coverage_calls: list[str | None] = []
        self.phase_kwargs: list[dict[str, Any]] = []
        self.coverage_kwargs: list[dict[str, Any]] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        self.phase_kwargs.append(dict(kwargs))
        if phase_names == ("setup", "pre_agent"):
            return ValidationResult()
        return self._phase_result

    async def run_profile_coverage(self, **_kwargs: Any) -> ValidationCoverageResult | None:
        self.coverage_kwargs.append(dict(_kwargs))
        phase = _kwargs.get("phase")
        self.coverage_calls.append(phase if isinstance(phase, str) else None)
        return self._coverage_result


class _RecordingPrCreator:
    async def push_and_open(self, *, branch_name: str, **_kwargs: Any) -> PullRequestResult:
        return PullRequestResult(
            url="https://github.com/x/y/pull/123",
            branch=branch_name,
            head_sha="b" * 40,
        )


def _validation_command_result(
    tmp_path: Path,
    *,
    returncode: int,
    reason_code: str,
) -> ValidationCommandResult:
    stdout_path = tmp_path / "validation.stdout"
    stderr_path = tmp_path / "validation.stderr"
    stdout_path.write_text("validation stdout\n", encoding="utf-8")
    stderr_path.write_text("validation stderr\n", encoding="utf-8")
    return ValidationCommandResult(
        command="pytest -q",
        returncode=returncode,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="validate",
        reason_code=reason_code,
    )


def _coverage_result(tmp_path: Path, *, percent: float = 99.5) -> ValidationCoverageResult:
    stdout_path = tmp_path / "coverage.stdout"
    stderr_path = tmp_path / "coverage.stderr"
    stdout_path.write_text(f"TOTAL 10 0 {percent:.1f}%\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    command_result = ValidationCommandResult(
        command="pytest --cov=awf",
        returncode=0,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="coverage",
        reason_code="COVERAGE_OK",
    )
    return ValidationCoverageResult(
        provider="python",
        percent=percent,
        minimum_percent=99.0,
        enforce=True,
        status="passed",
        reason_code="COVERAGE_OK",
        command_result=command_result,
    )


def _setup_dependency_metadata(*, retry_exhausted: bool = False) -> dict[str, object]:
    return {
        "reason_code": SETUP_DEPENDENCY_NETWORK_FAILURE,
        "command": "uv sync --extra dev",
        "package": "docker==7.1.0",
        "host": "files.pythonhosted.org",
        "transient_category": "dns",
        "retryable": True,
        "retry_count": 2 if retry_exhausted else 1,
        "retry_budget": 2,
        "retry_exhausted": retry_exhausted,
        "diagnostic": (
            "Failed to download docker==7.1.0 from files.pythonhosted.org: "
            "failed to lookup address information: No address associated with hostname; "
            "Authorization: Bearer ghp_FAKESECRET0000000"
        ),
    }


def _setup_dependency_command_result(
    tmp_path: Path,
    *,
    returncode: int = 0,
    retry_exhausted: bool = False,
) -> ValidationCommandResult:
    stdout_path = tmp_path / "setup.stdout"
    stderr_path = tmp_path / "setup.stderr"
    stdout_path.write_text("setup stdout\n", encoding="utf-8")
    stderr_path.write_text("setup stderr\n", encoding="utf-8")
    return ValidationCommandResult(
        command="uv sync --extra dev",
        returncode=returncode,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="setup",
        reason_code=(SETUP_DEPENDENCY_NETWORK_FAILURE if returncode else "VALIDATION_OK"),
        retry_count=2 if retry_exhausted else 1,
        metadata={
            "setup_dependency_network": _setup_dependency_metadata(retry_exhausted=retry_exhausted)
        },
    )


def _setup_dependency_then_later_failure_command_result(
    tmp_path: Path,
) -> ValidationCommandResult:
    stdout_path = tmp_path / "setup_later_failure.stdout"
    stderr_path = tmp_path / "setup_later_failure.stderr"
    stdout_path.write_text("setup retry stdout\n", encoding="utf-8")
    stderr_path.write_text("local post-install hook failed\n", encoding="utf-8")
    setup_metadata = _setup_dependency_metadata(retry_exhausted=False)
    setup_metadata.update(
        {
            "recovered": False,
            "attempts": [
                {
                    "reason_code": SETUP_DEPENDENCY_NETWORK_FAILURE,
                    "command": "uv sync --extra dev",
                    "package": "docker==7.1.0",
                    "host": "files.pythonhosted.org",
                    "transient_category": "dns",
                    "retryable": True,
                    "attempt": 1,
                    "retry_number": 1,
                }
            ],
            "stream_ids": {},
        }
    )
    return ValidationCommandResult(
        command="uv sync --extra dev",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="setup",
        reason_code="COMMAND_FAILED",
        retry_count=1,
        metadata={"setup_dependency_network": setup_metadata},
    )


class _SetupDependencyValidation(_RecordingValidation):
    def __init__(self, setup_result: ValidationResult, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._setup_result = setup_result

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        self.phase_kwargs.append(dict(kwargs))
        if phase_names == ("setup", "pre_agent"):
            return self._setup_result
        return self._phase_result


class _ExplodingValidation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> SimpleNamespace:
        self.calls.append(phase_names)
        if phase_names == ("post_agent", "validate"):
            raise RuntimeError("docker compose validation failed")
        return SimpleNamespace(all_passed=True, first_failure=None)


class _CancellingSetupValidation:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> SimpleNamespace:
        assert phase_names == ("setup", "pre_agent")
        async with self._factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCELLED")
            await s.commit()
        return SimpleNamespace(all_passed=True, first_failure=None)

    async def run_profile_coverage(self, **_kwargs: Any) -> None:
        return None


class _CancellingSuccessfulValidation:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        if phase_names == ("post_agent", "validate"):
            async with self._factory() as s:
                repo = WorkspaceRepository(s)
                ws = await repo.get(workspace_id)
                assert ws is not None
                await repo.transition(
                    ws, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCELLED"
                )
                await s.commit()
        return ValidationResult()

    async def run_profile_coverage(self, **_kwargs: Any) -> None:
        return None


class _DivergingPrCreator:
    def __init__(self, factory: async_sessionmaker[AsyncSession], workspace_id: str) -> None:
        self._factory = factory
        self._workspace_id = workspace_id

    async def push_and_open(self, *, branch_name: str, **_kwargs: Any) -> PullRequestResult:
        async with self._factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(self._workspace_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="TEST_DIVERGED")
            await s.commit()
        return PullRequestResult(
            url="https://github.com/x/y/pull/42",
            branch=branch_name,
            head_sha="b" * 40,
        )


class _RemovingValidation:
    def __init__(self, worktree_path: Path) -> None:
        self._worktree_path = worktree_path
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        if phase_names == ("post_agent", "validate"):
            shutil.rmtree(self._worktree_path)
        return ValidationResult()

    async def run_profile_coverage(self, **_kwargs: Any) -> None:
        return None


async def _move_to_operator_control_status(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    final_status: WorkspaceStatus,
) -> None:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        assert ws is not None
        await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="TEST_OPERATOR")
        if final_status == WorkspaceStatus.destroyed:
            await repo.transition(ws, to=WorkspaceStatus.destroying, reason_code="TEST_OPERATOR")
            await repo.transition(ws, to=WorkspaceStatus.destroyed, reason_code="TEST_OPERATOR")
        else:
            assert final_status == WorkspaceStatus.cancelled
        await s.commit()


async def _seed_ready(
    factory: async_sessionmaker[AsyncSession],
    *,
    agent: str = "codex",
    base_commit: str | None = "a" * 40,
    auto_merge: bool | None = None,
    resolved_profile: dict[str, Any] | None = None,
    requested_profile: dict[str, Any] | None = None,
    profile_ref: str | None = None,
    task_prompt: str = "p",
    task_policy: dict[str, Any] | None = None,
    owned_paths: list[str] | None = None,
    test_commands: list[str] | None = None,
    task_kind: str = "feature_branch_pr",
    initial_review_grace_period_seconds: float | None = None,
    create_task_attempt: bool = False,
    mark_canonical_attempt: bool = False,
    create_worktree: bool = True,
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:x/y.git",
            branch_base="development",
            task_title="err-path",
            task_prompt=task_prompt,
            agent=agent,
            test_commands=test_commands or ["pytest -q"],
            requires_database=False,
            owned_paths=owned_paths,
            task_policy=task_policy,
            profile_ref=profile_ref,
            requested_profile=requested_profile,
            resolved_profile=resolved_profile,
            initial_review_grace_period_seconds=initial_review_grace_period_seconds,
            task_kind=task_kind,
        )
        if create_task_attempt:
            task = await TaskRepository(s).create_or_get(
                repo_url=ws.repo_url,
                base_branch=ws.branch_base,
                title=ws.task_title,
                prompt=ws.task_prompt,
                external_id=ws.task_external_id,
                idempotency_key=None,
                task_class=ws.task_class,
                owned_paths=list(ws.owned_paths),
            )
            attempt = await TaskAttemptRepository(s).create_for_workspace(
                task=task,
                workspace=ws,
            )
            if mark_canonical_attempt:
                attempt.is_canonical_for_merge = True
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = "awf/x"
        ws.remote_push_branch = "awf/x"
        ws.base_commit = base_commit
        ws.compose_project_name = "awf_x"
        if auto_merge is not None:
            ws.auto_merge = auto_merge
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await s.commit()
        if create_worktree:
            (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


def _salvage_artifact_path(tmp_path: Path, filename: str) -> Path:
    artifact_dir = tmp_path / "work" / "artifacts" / "salvage"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir / filename


def _provider_recovery_policy(*, max_same_provider_retries: int) -> dict[str, Any]:
    return {
        "agent_model": "gemini-2.5-pro",
        "pr_monitor": {"review_grace_seconds": 55},
        "provider_recovery": {
            "fallbacks": [
                {
                    "agent": "codex",
                    "provider": "openai",
                    "model": "gpt-5.3-codex",
                }
            ],
            "max_fallback_attempts": 1,
            "max_same_provider_retries": max_same_provider_retries,
            "cooldown_seconds": 30,
            "backoff_seconds": 30,
            "retry_after_cap_seconds": 300,
        },
    }


def _provider_recovery_resolved_profile() -> dict[str, Any]:
    return WorkspaceProfile(
        name="executor-provider-recovery",
        source="test",
        validation={"requested_tier": 2},
        monitor=ProfileMonitor(
            initial_review_grace_period_seconds=55,
            non_check_reviewer_settle_seconds=12,
            non_check_reviewer_logins=["review-bot"],
        ),
    ).model_dump(mode="json")


def _provider_recovery_requested_profile() -> dict[str, Any]:
    return {
        "name": "requested-provider-profile",
        "source": "inline-test",
        "validation": {"requested_tier": 2},
    }


def _parse_utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def _seed_monitoring_pr(
    factory: async_sessionmaker[AsyncSession],
    *,
    branch_name: str | None = "awf/x",
    task_kind: str = "feature_branch_pr",
    pr_number: int | None = 42,
    pr_url: str | None = "https://github.com/x/y/pull/42",
    remote_push_branch: str | None = "awf/x",
    compose_project_name: str | None = "awf_x",
    compose_file_path: str | None = "/tmp/awf/x/compose.yml",
    resolved_profile: dict[str, Any] | None = None,
    auto_merge: bool = True,
    initial_review_grace_period_seconds: float | None = None,
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:x/y.git",
            branch_base="development",
            task_title="monitor-resume",
            task_prompt="p",
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
            resolved_profile=resolved_profile,
            auto_merge=auto_merge,
            initial_review_grace_period_seconds=initial_review_grace_period_seconds,
        )
        ws.task_kind = task_kind
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = branch_name
        ws.remote_push_branch = remote_push_branch
        ws.base_commit = "a" * 40
        ws.compose_project_name = compose_project_name
        ws.compose_file_path = compose_file_path
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="SEED")
        ws.pr_url = pr_url
        ws.pr_number = pr_number
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="SEED")
        await s.commit()
        return ws.id


def _release_adoption_payload(
    *,
    number: int = 321,
    head_ref: str = "development",
    base_ref: str = "main",
    head_sha: str = "h" * 40,
    base_sha: str = "b" * 40,
) -> str:
    return json.dumps(
        {
            "number": number,
            "headRefName": head_ref,
            "headRepository": {"name": "y", "nameWithOwner": "x/y"},
            "isCrossRepository": False,
            "baseRefName": base_ref,
            "headRefOid": head_sha,
            "baseRefOid": base_sha,
            "state": "OPEN",
            "isDraft": False,
            "author": {"login": "octocat"},
            "url": f"https://github.com/x/y/pull/{number}",
            "title": "Release",
        }
    )


def _release_open_pr_list_payload(*, number: int = 321) -> str:
    return json.dumps(
        [
            {
                "number": number,
                "url": f"https://github.com/x/y/pull/{number}",
                "headRefName": "development",
                "headRefOid": "h" * 40,
                "headRepository": {"name": "y", "nameWithOwner": "x/y"},
                "headRepositoryOwner": {"login": "x"},
            }
        ]
    )


_RELEASE_SYNC_POLICY = {"release_sync": {"source_branch": "development", "target_branch": "main"}}


def _release_sync_policy() -> dict[str, Any]:
    """Return an independent copy so tests can't share nested policy state."""
    return copy.deepcopy(_RELEASE_SYNC_POLICY)


class TestPullRequestUnexpectedErrorPart001:
    @pytest.mark.unit
    def test_salvage_patch_exclusion_supports_linked_worktree_gitdir(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake, factory, tmp_path, validation=_RecordingValidation())
        relative_worktree = tmp_path / "relative-worktree"
        relative_git_dir = tmp_path / "relative-gitdir"
        relative_worktree.mkdir()
        (relative_git_dir / "info").mkdir(parents=True)
        (relative_worktree / ".git").write_text("gitdir: ../relative-gitdir\n", encoding="utf-8")

        executor._exclude_agent_salvage_artifacts(relative_worktree)
        executor._exclude_agent_salvage_artifacts(relative_worktree)

        relative_exclude = relative_git_dir / "info" / "exclude"
        assert relative_exclude.read_text(encoding="utf-8").splitlines() == ["/.awf/salvage/"]

        absolute_worktree = tmp_path / "absolute-worktree"
        absolute_git_dir = tmp_path / "absolute-gitdir"
        absolute_worktree.mkdir()
        (absolute_git_dir / "info").mkdir(parents=True)
        (absolute_worktree / ".git").write_text(
            f"gitdir: {absolute_git_dir}\n",
            encoding="utf-8",
        )

        executor._exclude_agent_salvage_artifacts(absolute_worktree)

        absolute_exclude = absolute_git_dir / "info" / "exclude"
        assert absolute_exclude.read_text(encoding="utf-8").splitlines() == ["/.awf/salvage/"]

    @pytest.mark.unit
    async def test_execute_stops_when_conformance_salvage_cannot_be_prepared(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        validation = _RecordingValidation()
        ws_id = await _seed_ready(
            factory,
            task_policy={"conformance_salvage": {"source_workspace_id": "ws_source"}},
        )
        executor = _make_executor(fake, factory, tmp_path, validation=validation)

        await executor.execute(ws_id)

        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.failed.value
        assert workspace.failure_reason == "infrastructure_failure"
        assert workspace.failure_message is not None
        assert "SALVAGE_PATCH_UNAVAILABLE" in workspace.failure_message
        assert validation.calls == []

    @pytest.mark.unit
    async def test_execute_passes_salvage_conflict_prompt_to_agent(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        patch_path = _salvage_artifact_path(tmp_path, "conflict-execute.patch")
        patch_path.write_text(
            "diff --git a/src/app.py b/src/app.py\n"
            "index 51f15c8..5f2b6d7 100644\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(patch_path.read_bytes()).hexdigest()
        ws_id = await _seed_ready(
            factory,
            task_prompt="finish original task",
            task_policy={
                "conformance_salvage": {
                    "source_workspace_id": "ws_source",
                    "patch_path": str(patch_path),
                    "patch_sha256": digest,
                    "implementation_paths": ["src/app.py"],
                    "remaining_gaps": ["add regression test"],
                }
            },
        )
        worktree_path = _test_worktree_path(factory, ws_id)
        subprocess.run(["git", "init", "-q"], cwd=worktree_path, check=True)
        (worktree_path / "src").mkdir()
        (worktree_path / "src/app.py").write_text("current\n", encoding="utf-8")
        captured: dict[str, str] = {}

        class _CaptureAdapter:
            def get_provider(self, model: str | None) -> str:
                del model
                return "fake"

            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.codex

            async def run(
                self,
                *,
                compose_project: str,
                compose_file: Path,
                prompt: str,
                model: str | None = None,
                workspace_id: str | None = None,
            ) -> None:
                del compose_project, compose_file, model, workspace_id
                captured["prompt"] = prompt
                raise RuntimeError("stop after prompt capture")

        def _get_adapter(_runtime: AgentRuntime, **_kwargs: Any) -> _CaptureAdapter:
            return _CaptureAdapter()

        monkeypatch.setattr(executor_execution_flow, "get_adapter", _get_adapter)
        executor = _make_executor(
            AsyncioSubprocessRunner(),
            factory,
            tmp_path,
            validation=_RecordingValidation(),
        )

        await executor.execute(ws_id)

        prompt = captured["prompt"]
        assert "Automatic AWF salvage conflict" in prompt
        assert ".awf/salvage/conflict-execute.patch" in prompt
        assert "add regression test" in prompt
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
            events = await WorkspaceEventRepository(session).list(workspace_id=ws_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.failed.value
        assert any(
            event.event_type == "workspace.conformance_salvage_conflict"
            and event.payload["agent_patch_path"] == ".awf/salvage/conflict-execute.patch"
            for event in events
        )

    @pytest.mark.unit
    async def test_clean_conformance_salvage_patch_is_applied_before_agent(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        patch_path = _salvage_artifact_path(tmp_path, "source.patch")
        patch_path.write_text(
            "diff --git a/src/restored.py b/src/restored.py\n"
            "new file mode 100644\n"
            "index 0000000..7f5af8e\n"
            "--- /dev/null\n"
            "+++ b/src/restored.py\n"
            "@@ -0,0 +1 @@\n"
            "+VALUE = 'restored'\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(patch_path.read_bytes()).hexdigest()
        ws_id = await _seed_ready(
            factory,
            task_policy={
                "conformance_salvage": {
                    "source_workspace_id": "ws_source",
                    "patch_path": str(patch_path),
                    "patch_sha256": digest,
                    "implementation_paths": ["src/restored.py"],
                    "remaining_gaps": ["finish tests"],
                }
            },
        )
        worktree_path = _test_worktree_path(factory, ws_id)
        subprocess.run(["git", "init", "-q"], cwd=worktree_path, check=True)
        executor = _make_executor(
            AsyncioSubprocessRunner(),
            factory,
            tmp_path,
            validation=_RecordingValidation(),
        )
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                ws_id,
                from_status=WorkspaceStatus.ready,
                to=WorkspaceStatus.running,
                reason_code="TEST",
            )
            assert ws is not None
            await session.commit()

        result = await executor._prepare_conformance_salvage_for_execution(
            workspace_id=ws_id,
            workspace=ws,
            worktree_path=worktree_path,
        )

        assert result is not None
        assert result.status == "applied"
        assert result.prompt_override is None
        assert (worktree_path / "src/restored.py").read_text(encoding="utf-8") == (
            "VALUE = 'restored'\n"
        )
        async with factory() as session:
            events = await WorkspaceEventRepository(session).list(workspace_id=ws_id)
        assert any(
            event.event_type == "workspace.conformance_salvage_applied"
            and event.reason_code == "CONFORMANCE_SALVAGE_APPLIED"
            for event in events
        )

    @pytest.mark.unit
    async def test_conflicting_conformance_salvage_launches_resolver_prompt(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        patch_path = _salvage_artifact_path(tmp_path, "conflict.patch")
        patch_path.write_text(
            "diff --git a/src/app.py b/src/app.py\n"
            "index 51f15c8..5f2b6d7 100644\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(patch_path.read_bytes()).hexdigest()
        ws_id = await _seed_ready(
            factory,
            task_prompt="finish original task",
            task_policy={
                "conformance_salvage": {
                    "source_workspace_id": "ws_source",
                    "patch_path": str(patch_path),
                    "patch_sha256": digest,
                    "implementation_paths": ["src/app.py"],
                    "remaining_gaps": ["add regression test"],
                    "plan_path": "docs/awf-plans/ws_old.md",
                }
            },
        )
        worktree_path = _test_worktree_path(factory, ws_id)
        subprocess.run(["git", "init", "-q"], cwd=worktree_path, check=True)
        (worktree_path / "src").mkdir()
        (worktree_path / "src/app.py").write_text("current\n", encoding="utf-8")
        executor = _make_executor(
            AsyncioSubprocessRunner(),
            factory,
            tmp_path,
            validation=_RecordingValidation(),
        )
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                ws_id,
                from_status=WorkspaceStatus.ready,
                to=WorkspaceStatus.running,
                reason_code="TEST",
            )
            assert ws is not None
            await session.commit()

        result = await executor._prepare_conformance_salvage_for_execution(
            workspace_id=ws_id,
            workspace=ws,
            worktree_path=worktree_path,
        )

        assert result is not None
        assert result.status == "conflict"
        assert result.prompt_override is not None
        assert "could not be applied cleanly" in result.prompt_override
        assert ".awf/salvage/" in result.prompt_override
        assert "add regression test" in result.prompt_override
        assert (worktree_path / "src/app.py").read_text(encoding="utf-8") == "current\n"
        assert (worktree_path / ".awf/salvage/conflict.patch").exists()
        async with factory() as session:
            events = await WorkspaceEventRepository(session).list(workspace_id=ws_id)
        assert any(
            event.event_type == "workspace.conformance_salvage_conflict"
            and event.reason_code == "CONFORMANCE_SALVAGE_CONFLICT"
            for event in events
        )

    @pytest.mark.unit
    async def test_conformance_salvage_digest_mismatch_marks_failed(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        patch_path = _salvage_artifact_path(tmp_path, "source.patch")
        patch_path.write_text("diff --git a/x b/x\n", encoding="utf-8")
        ws_id = await _seed_ready(
            factory,
            task_policy={
                "conformance_salvage": {
                    "source_workspace_id": "ws_source",
                    "patch_path": str(patch_path),
                    "patch_sha256": "0" * 64,
                    "implementation_paths": ["x"],
                }
            },
        )
        worktree_path = _test_worktree_path(factory, ws_id)
        subprocess.run(["git", "init", "-q"], cwd=worktree_path, check=True)
        executor = _make_executor(
            AsyncioSubprocessRunner(),
            factory,
            tmp_path,
            validation=_RecordingValidation(),
        )
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                ws_id,
                from_status=WorkspaceStatus.ready,
                to=WorkspaceStatus.running,
                reason_code="TEST",
            )
            assert ws is not None
            await session.commit()

        result = await executor._prepare_conformance_salvage_for_execution(
            workspace_id=ws_id,
            workspace=ws,
            worktree_path=worktree_path,
        )

        assert result is not None
        assert result.status == "failed"
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.failed.value
        assert workspace.failure_reason == "infrastructure_failure"
        assert workspace.failure_message is not None
        assert "SALVAGE_PATCH_DIGEST_MISMATCH" in workspace.failure_message

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("salvage", "expected_reason"),
        [
            ({}, "SALVAGE_PATCH_UNAVAILABLE"),
            ({"patch_path": "missing.patch"}, "SALVAGE_PATCH_DIGEST_MISMATCH"),
            (
                {"patch_path": "/tmp/awf-missing-salvage.patch", "patch_sha256": "0" * 64},
                "SALVAGE_PATCH_UNAVAILABLE",
            ),
        ],
    )
    async def test_conformance_salvage_missing_patch_metadata_marks_failed(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        salvage: dict[str, str],
        expected_reason: str,
    ) -> None:
        ws_id = await _seed_ready(
            factory,
            task_policy={"conformance_salvage": {"source_workspace_id": "ws_source", **salvage}},
        )
        worktree_path = _test_worktree_path(factory, ws_id)
        subprocess.run(["git", "init", "-q"], cwd=worktree_path, check=True)
        executor = _make_executor(
            AsyncioSubprocessRunner(),
            factory,
            tmp_path,
            validation=_RecordingValidation(),
        )
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                ws_id,
                from_status=WorkspaceStatus.ready,
                to=WorkspaceStatus.running,
                reason_code="TEST",
            )
            assert ws is not None
            await session.commit()

        result = await executor._prepare_conformance_salvage_for_execution(
            workspace_id=ws_id,
            workspace=ws,
            worktree_path=worktree_path,
        )

        assert result is not None
        assert result.status == "failed"
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
        assert workspace is not None
        assert workspace.failure_message is not None
        assert expected_reason in workspace.failure_message

    @pytest.mark.unit
    async def test_conformance_salvage_patch_path_outside_artifacts_is_unavailable(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        outside_patch = tmp_path / "outside.patch"
        outside_patch.write_text("diff --git a/x b/x\n", encoding="utf-8")
        digest = hashlib.sha256(outside_patch.read_bytes()).hexdigest()
        ws_id = await _seed_ready(
            factory,
            task_policy={
                "conformance_salvage": {
                    "source_workspace_id": "ws_source",
                    "patch_path": str(outside_patch),
                    "patch_sha256": digest,
                    "implementation_paths": ["x"],
                }
            },
        )
        worktree_path = _test_worktree_path(factory, ws_id)
        subprocess.run(["git", "init", "-q"], cwd=worktree_path, check=True)
        executor = _make_executor(
            AsyncioSubprocessRunner(),
            factory,
            tmp_path,
            validation=_RecordingValidation(),
        )
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                ws_id,
                from_status=WorkspaceStatus.ready,
                to=WorkspaceStatus.running,
                reason_code="TEST",
            )
            assert ws is not None
            await session.commit()

        result = await executor._prepare_conformance_salvage_for_execution(
            workspace_id=ws_id,
            workspace=ws,
            worktree_path=worktree_path,
        )

        assert result is not None
        assert result.status == "failed"
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
        assert workspace is not None
        assert workspace.failure_message is not None
        assert "SALVAGE_PATCH_UNAVAILABLE" in workspace.failure_message

    @pytest.mark.unit
    async def test_conformance_salvage_patch_read_oserror_marks_unavailable(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        patch_path = _salvage_artifact_path(tmp_path, "source.patch")
        patch_path.write_text("diff --git a/x b/x\n", encoding="utf-8")
        digest = hashlib.sha256(patch_path.read_bytes()).hexdigest()
        ws_id = await _seed_ready(
            factory,
            task_policy={
                "conformance_salvage": {
                    "source_workspace_id": "ws_source",
                    "patch_path": str(patch_path),
                    "patch_sha256": digest,
                    "implementation_paths": ["x"],
                }
            },
        )
        worktree_path = _test_worktree_path(factory, ws_id)
        subprocess.run(["git", "init", "-q"], cwd=worktree_path, check=True)
        executor = _make_executor(
            AsyncioSubprocessRunner(),
            factory,
            tmp_path,
            validation=_RecordingValidation(),
        )

        def _raise_read_bytes(_self: Any) -> bytes:
            raise OSError("simulated read failure")

        monkeypatch.setattr(executor_git_ops.Path, "read_bytes", _raise_read_bytes)

        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                ws_id,
                from_status=WorkspaceStatus.ready,
                to=WorkspaceStatus.running,
                reason_code="TEST",
            )
            assert ws is not None
            await session.commit()

        result = await executor._prepare_conformance_salvage_for_execution(
            workspace_id=ws_id,
            workspace=ws,
            worktree_path=worktree_path,
        )

        assert result is not None
        assert result.status == "failed"
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
        assert workspace is not None
        assert workspace.failure_message is not None
        assert "SALVAGE_PATCH_UNAVAILABLE" in workspace.failure_message

    @pytest.mark.unit
    async def test_conformance_salvage_apply_oserror_marks_unavailable(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        patch_path = _salvage_artifact_path(tmp_path, "source.patch")
        patch_path.write_text("diff --git a/x b/x\n", encoding="utf-8")
        digest = hashlib.sha256(patch_path.read_bytes()).hexdigest()
        ws_id = await _seed_ready(
            factory,
            task_policy={
                "conformance_salvage": {
                    "source_workspace_id": "ws_source",
                    "patch_path": str(patch_path),
                    "patch_sha256": digest,
                    "implementation_paths": ["x"],
                }
            },
        )
        worktree_path = _test_worktree_path(factory, ws_id)

        class _OSErrorRunner:
            async def run(self, *_args: Any, **_kwargs: Any) -> Any:
                raise OSError("simulated git runner failure")

        executor = _make_executor(
            _OSErrorRunner(),  # type: ignore[arg-type]
            factory,
            tmp_path,
            validation=_RecordingValidation(),
        )
        subprocess.run(["git", "init", "-q"], cwd=worktree_path, check=True)
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                ws_id,
                from_status=WorkspaceStatus.ready,
                to=WorkspaceStatus.running,
                reason_code="TEST",
            )
            assert ws is not None
            await session.commit()

        result = await executor._prepare_conformance_salvage_for_execution(
            workspace_id=ws_id,
            workspace=ws,
            worktree_path=worktree_path,
        )

        assert result is not None
        assert result.status == "failed"
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
        assert workspace is not None
        assert workspace.failure_message is not None
        assert "SALVAGE_PATCH_UNAVAILABLE" in workspace.failure_message

    @pytest.mark.unit
    async def test_conformance_salvage_apply_failure_marks_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        patch_path = _salvage_artifact_path(tmp_path, "source.patch")
        patch_path.write_text("diff --git a/x b/x\n", encoding="utf-8")
        digest = hashlib.sha256(patch_path.read_bytes()).hexdigest()
        ws_id = await _seed_ready(
            factory,
            task_policy={
                "conformance_salvage": {
                    "source_workspace_id": "ws_source",
                    "patch_path": str(patch_path),
                    "patch_sha256": digest,
                    "implementation_paths": ["x"],
                }
            },
        )
        worktree_path = _test_worktree_path(factory, ws_id)
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=1, stderr="apply exploded")
        executor = _make_executor(fake, factory, tmp_path, validation=_RecordingValidation())
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                ws_id,
                from_status=WorkspaceStatus.ready,
                to=WorkspaceStatus.running,
                reason_code="TEST",
            )
            assert ws is not None
            await session.commit()

        result = await executor._prepare_conformance_salvage_for_execution(
            workspace_id=ws_id,
            workspace=ws,
            worktree_path=worktree_path,
        )

        assert result is not None
        assert result.status == "failed"
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
        assert workspace is not None
        assert workspace.failure_message is not None
        assert "SALVAGE_PATCH_APPLY_FAILED" in workspace.failure_message

    @pytest.mark.unit
    async def test_plan_only_output_fails_before_validation_and_pr_creation(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        class _UnexpectedPrCreator:
            async def push_and_open(self, **_kwargs: Any) -> PullRequestResult:
                raise AssertionError("plan-only output must not be pushed")

        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0)
        fake.queue_result(
            returncode=0,
            stdout=("docs/awf-plans/ws_plan.md\ndocs/awf-plans/ws_plan.conformance.json\n"),
        )
        validation = _RecordingValidation()

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_creator=_UnexpectedPrCreator(),
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
            assert "only AWF plan/conformance artifact" in (ws.failure_message or "")
            assert ws.pr_url is None
            events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
            assert any(
                event.event_type == "workspace.state_changed"
                and event.reason_code == "PLAN_ONLY_OUTPUT"
                for event in events
            )
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert runs == []

        assert validation.calls == [("setup", "pre_agent")]

    @pytest.mark.unit
    async def test_plan_only_staged_conformance_after_real_commit_is_accepted(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        class _RecordingPrCreator:
            def __init__(self) -> None:
                self.called = False

            async def push_and_open(self, *, branch_name: str, **_kwargs: Any) -> PullRequestResult:
                self.called = True
                return PullRequestResult(
                    url="https://github.com/x/y/pull/123",
                    branch=branch_name,
                    head_sha="b" * 40,
                )

        ws_id = await _seed_ready(factory)
        validation = _RecordingValidation()
        pr_creator = _RecordingPrCreator()

        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")  # drift-check: on expected branch
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(  # only the final conformance artifact remains staged
            returncode=0,
            stdout=f"docs/awf-plans/{ws_id}.conformance.json\n",
        )
        fake.queue_result(  # committed implementation output already exists on the branch
            returncode=0,
            stdout="src/awf/mcp/server.py\ntests/unit/mcp/test_mcp_operator_surfaces.py\n",
        )
        fake.queue_result(returncode=0)  # commit staged conformance artifact
        fake.queue_result(returncode=0, stdout="2\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        fake.queue_result(returncode=0, stdout="validated-head\n")  # pre-validation HEAD

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_creator=pr_creator,
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.failure_reason is None
            assert ws.failure_message is None
            assert ws.pr_url == "https://github.com/x/y/pull/123"
            events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
            assert not any(event.reason_code == "PLAN_ONLY_OUTPUT" for event in events)
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert len(runs) == 1
            assert runs[0].status == "succeeded"
            assert runs[0].workspace_head_sha == "validated-head"

        assert pr_creator.called is True
        assert validation.calls == [("setup", "pre_agent"), ("post_agent", "validate")]
