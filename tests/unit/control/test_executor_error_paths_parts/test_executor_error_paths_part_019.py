"""Error-path coverage for ``WorkspaceExecutor`` PR-monitor resume.

Split out of ``test_executor_error_paths_part_004.py`` to keep each first-party
test file under the maintainability line limit. Hosts ``TestPrMonitorResume``
together with the shared executor/seed helpers it relies on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import (
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.profiles.models import ProfileMonitor, WorkspaceProfile
from awf.runtime.logs import LogStore
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner
from tests.postgres import postgres_test_engine
from tests.unit.control.executor_paths import _test_worktrees_root


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
        force_recreate: bool = False,
        services: tuple[str, ...] = (),
    ) -> None:
        del project_name, compose_file, workspace_id, wait, compose_up_timeout_seconds
        del force_recreate, services


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


class TestPrMonitorResume:
    @pytest.mark.unit
    async def test_resume_pr_monitor_logs_unknown_workspace(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake, factory, tmp_path)

        with structlog.testing.capture_logs() as captured:
            await executor.resume_pr_monitor("ws_never_existed")

        assert fake.calls == []
        assert any(
            event.get("event") == "executor.resume_skip_unknown"
            and event.get("workspace_id") == "ws_never_existed"
            for event in captured
        )

    @pytest.mark.unit
    async def test_resume_pr_monitor_logs_unexpected_status(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)

        def _monitor_factory(*_args: Any) -> object:
            raise AssertionError("monitor factory must not run for non-monitoring workspaces")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        with structlog.testing.capture_logs() as captured:
            await executor.resume_pr_monitor(ws_id)

        assert fake.calls == []
        assert any(
            event.get("event") == "executor.resume_skip_not_monitoring_pr"
            and event.get("workspace_id") == ws_id
            and event.get("status") == WorkspaceStatus.ready.value
            for event in captured
        )

    @pytest.mark.unit
    async def test_resume_pr_monitor_uses_persisted_workspace_metadata(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        compose_file = tmp_path / "persisted-compose" / "compose.yml"
        resolved_profile = WorkspaceProfile(
            name="persisted",
            monitor=ProfileMonitor(initial_review_grace_period_seconds=321),
        ).model_dump(mode="json")
        factory_calls: list[dict[str, Any]] = []
        monitor_calls: list[dict[str, Any]] = []

        class _FakeMonitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                monitor_calls.append(
                    {
                        "workspace_id": workspace_id,
                        "compose_project": compose_project,
                        "compose_file": compose_file,
                    }
                )

        def _monitor_factory(adapter: Any, profile: Any, workspace: Any) -> _FakeMonitor:
            factory_calls.append(
                {
                    "adapter": adapter,
                    "profile_name": profile.name,
                    "profile_grace": profile.monitor.initial_review_grace_period_seconds,
                    "auto_merge": workspace.auto_merge,
                    "workspace_grace": workspace.initial_review_grace_period_seconds,
                    "pr_number": workspace.pr_number,
                    "pr_url": workspace.pr_url,
                    "remote_push_branch": workspace.remote_push_branch,
                }
            )
            return _FakeMonitor()

        ws_id = await _seed_monitoring_pr(
            factory,
            pr_number=77,
            pr_url="https://github.com/x/y/pull/77",
            remote_push_branch="awf/persisted",
            compose_project_name="persisted_project",
            compose_file_path=str(compose_file),
            resolved_profile=resolved_profile,
            auto_merge=False,
            initial_review_grace_period_seconds=12.5,
        )
        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        await executor.resume_pr_monitor(ws_id)

        assert fake.calls == []
        assert len(factory_calls) == 1
        assert factory_calls[0]["profile_name"] == "persisted"
        assert factory_calls[0]["profile_grace"] == 321
        assert factory_calls[0]["auto_merge"] is False
        assert factory_calls[0]["workspace_grace"] == 12.5
        assert factory_calls[0]["pr_number"] == 77
        assert factory_calls[0]["pr_url"] == "https://github.com/x/y/pull/77"
        assert factory_calls[0]["remote_push_branch"] == "awf/persisted"
        assert monitor_calls == [
            {
                "workspace_id": ws_id,
                "compose_project": "persisted_project",
                "compose_file": compose_file,
            }
        ]

    @pytest.mark.unit
    async def test_resume_pr_monitor_restarts_persisted_compose_stack_before_monitor(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        compose_file = tmp_path / "persisted-compose" / "compose.yml"
        compose_file_path = compose_file
        call_order: list[str] = []

        class _RecordingCompose:
            async def ensure_project_up(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                wait: bool = True,
                compose_up_timeout_seconds: int = 300,
                force_recreate: bool = False,
                services: tuple[str, ...] = (),
            ) -> None:
                call_order.append("compose")
                assert project_name == "persisted_project"
                assert compose_file == compose_file_path
                assert workspace_id == ws_id
                assert wait is True
                assert compose_up_timeout_seconds == 300
                assert force_recreate is True
                assert services == ()

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                call_order.append("monitor")
                assert call_order == ["compose", "monitor"]
                assert workspace_id == ws_id
                assert compose_project == "persisted_project"
                assert compose_file == compose_file_path

        ws_id = await _seed_monitoring_pr(
            factory,
            compose_project_name="persisted_project",
            compose_file_path=str(compose_file),
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_args: _Monitor(),
            compose=_RecordingCompose(),
        )

        await executor.resume_pr_monitor(ws_id)

        assert call_order == ["compose", "monitor"]

    @pytest.mark.unit
    async def test_resume_pr_monitor_recovers_feature_branch_remote_push_branch(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_calls: list[str] = []

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(
            factory,
            branch_name="awf/legacy-feature",
            remote_push_branch=None,
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        await executor.resume_pr_monitor(ws_id)

        assert monitor_calls == [ws_id]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.remote_push_branch == "awf/legacy-feature"
            recovery_events = [
                event
                for event in ws.events
                if event.event_type == "workspace.remote_push_branch_recovered"
            ]
            assert len(recovery_events) == 1
            assert recovery_events[0].reason_code == "REMOTE_PUSH_BRANCH_RECOVERED"
            assert recovery_events[0].payload == {
                "remote_push_branch": "awf/legacy-feature",
                "source": "branch_name",
            }

    @pytest.mark.unit
    async def test_recover_feature_branch_remote_push_branch_skips_ineligible_rows(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake, factory, tmp_path)
        ready_id = await _seed_ready(factory)
        existing_id = await _seed_monitoring_pr(
            factory,
            branch_name="awf/existing",
            remote_push_branch="awf/persisted",
        )
        sync_id = await _seed_monitoring_pr(
            factory,
            task_kind="sync_feature_pr",
            branch_name="feature-sync/local",
            remote_push_branch=None,
        )

        assert (
            await executor._recover_feature_branch_remote_push_branch(
                workspace_id="ws_missing",
                remote_push_branch="awf/missing",
            )
            is None
        )
        assert (
            await executor._recover_feature_branch_remote_push_branch(
                workspace_id=ready_id,
                remote_push_branch="awf/ready",
            )
            is None
        )
        assert (
            await executor._recover_feature_branch_remote_push_branch(
                workspace_id=existing_id,
                remote_push_branch="awf/recovered",
            )
            == "awf/persisted"
        )
        assert (
            await executor._recover_feature_branch_remote_push_branch(
                workspace_id=sync_id,
                remote_push_branch="feature-sync/local",
            )
            is None
        )

    @pytest.mark.unit
    async def test_resume_pr_monitor_does_not_use_stale_recovery_when_status_changed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        class _UnexpectedCompose:
            async def ensure_project_up(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                wait: bool = True,
                compose_up_timeout_seconds: int = 300,
                force_recreate: bool = False,
                services: tuple[str, ...] = (),
            ) -> None:
                del project_name, compose_file, workspace_id, wait, compose_up_timeout_seconds
                del force_recreate, services
                raise AssertionError("compose must not restart after recovery skips")

        ws_id = await _seed_monitoring_pr(
            factory,
            branch_name="awf/concurrent-feature",
            remote_push_branch=None,
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_args: object(),
            compose=_UnexpectedCompose(),
        )
        original_load_workspace = executor._load_workspace

        async def _load_then_complete(workspace_id: str) -> Any:
            ws = await original_load_workspace(workspace_id)
            async with factory() as s:
                repo = WorkspaceRepository(s)
                fresh = await repo.get(workspace_id)
                assert fresh is not None
                await repo.transition(
                    fresh,
                    to=WorkspaceStatus.completed,
                    reason_code="CONCURRENT_COMPLETED",
                )
                await s.commit()
            return ws

        executor._load_workspace = _load_then_complete  # type: ignore[method-assign]

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.remote_push_branch is None
