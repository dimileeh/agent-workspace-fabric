"""Hosted PR monitor resume handoff.

Hosted adoption is explicit workspace policy, not an inference from an injected
``AgentRuntimeExecutor``. Local/default monitor resumes still restart Compose
because local validation needs the workspace stack. Explicit hosted adoption
resumes with only the worktree/profile metadata and skips Compose entirely.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populates registry
from awf.adapters.runtime_executor import (
    AgentRuntimeExecRequest,
    AgentRuntimeExecResult,
)
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor import monitor_handoff as monitor_handoff_module
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager, ComposeOperationError
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner
from tests.postgres import postgres_test_engine

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


async def _seed_monitoring_pr(
    factory: async_sessionmaker[AsyncSession],
    *,
    task_policy: dict[str, Any] | None = None,
    compose_project_name: str | None = "awf_x",
    compose_file_path: str | None = "/tmp/awf/x/compose.yml",
    task_kind: str = "feature_branch_pr",
    repo_url: str = "git@github.com:x/y.git",
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url=repo_url,
            branch_base="development",
            task_title="monitor-resume-hosted",
            task_prompt="p",
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
            auto_merge=True,
            task_policy=task_policy,
        )
        ws.task_kind = task_kind
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = (
            f"feature-sync/{ws.id}" if task_kind == "sync_feature_pr" else f"awf/{ws.id}"
        )
        ws.remote_push_branch = ws.branch_name
        ws.base_commit = "a" * 40
        ws.compose_project_name = compose_project_name
        ws.compose_file_path = compose_file_path
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="SEED")
        ws.pr_url = "https://github.com/x/y/pull/42"
        ws.pr_number = 42
        ws.monitor_last_commit_sha = "b" * 40
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="SEED")
        await s.commit()
        return ws.id


class _RecordingCompose:
    """Records ``ensure_project_up`` calls so we can assert restart skipped."""

    def __init__(self) -> None:
        self.ensure_project_up_calls: list[str] = []

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
        del project_name, compose_file, wait, compose_up_timeout_seconds
        del force_recreate, services
        self.ensure_project_up_calls.append(workspace_id)


class _FailingCompose(_RecordingCompose):
    """Records ``ensure_project_up`` calls and then fails the restart."""

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
        await super().ensure_project_up(
            project_name=project_name,
            compose_file=compose_file,
            workspace_id=workspace_id,
            wait=wait,
            compose_up_timeout_seconds=compose_up_timeout_seconds,
            force_recreate=force_recreate,
            services=services,
        )
        raise ComposeOperationError(
            operation="up",
            returncode=1,
            stdout="",
            stderr="compose unavailable",
            reason_code="COMPOSE_UP_FAILED",
        )


class _RecordingExecutor:
    """Hosted executor stub that records the execute() call."""

    def __init__(self) -> None:
        self.calls: list[AgentRuntimeExecRequest] = []

    async def execute(self, request: AgentRuntimeExecRequest) -> AgentRuntimeExecResult:
        self.calls.append(request)
        return AgentRuntimeExecResult(returncode=0, stdout="hosted ok", stderr="")


class _RecordingMonitor:
    """Monitor stub that records ``run`` invocation."""

    def __init__(self) -> None:
        self.run_calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> None:
        self.run_calls.append(dict(kwargs))


def _make_executor(
    *,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    compose: Any,
    pr_monitor_factory: Any,
    agent_runtime_executor: Any = None,
    ensure_hosted_monitor_checkout: Any = None,
) -> WorkspaceExecutor:
    validation = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
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
            max_validation_fix_passes=5,
        ),
        pr_monitor_factory=pr_monitor_factory,
        agent_runtime_executor=agent_runtime_executor,
        ensure_hosted_monitor_checkout=ensure_hosted_monitor_checkout,
    )


class TestResumeHandoffHostedSeam:
    @pytest.mark.unit
    async def test_explicit_hosted_policy_skips_compose_resume_with_null_compose_metadata(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_pr(
            factory,
            task_policy={"pr_adoption": {"execution": {"mode": "hosted"}}},
            compose_project_name=None,
            compose_file_path=None,
        )
        compose = _RecordingCompose()
        executor = _RecordingExecutor()
        monitor = _RecordingMonitor()
        real_compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        executor_obj = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            compose=real_compose,
            pr_monitor_factory=lambda *_args, **_kwargs: monitor,
            agent_runtime_executor=executor,
        )
        executor_obj._compose = compose  # type: ignore[method-assign]

        await executor_obj.resume_pr_monitor(ws_id)

        assert compose.ensure_project_up_calls == []
        assert len(monitor.run_calls) == 1
        assert monitor.run_calls[0]["workspace_id"] == ws_id
        assert monitor.run_calls[0]["compose_project"] == f"awf_{ws_id}"

    @pytest.mark.unit
    async def test_injected_executor_without_hosted_policy_still_restarts_compose_for_validation(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        compose = _RecordingCompose()
        executor = _RecordingExecutor()
        monitor = _RecordingMonitor()
        # Use a ComposeManager-compatible compose for the constructor (the
        # recording compose is only used to assert restart is preserved).
        real_compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        executor_obj = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            compose=real_compose,
            pr_monitor_factory=lambda *_args, **_kwargs: monitor,
            agent_runtime_executor=executor,
        )
        # Swap in the recording compose so we can assert ensure_project_up
        # is still called on the hosted path (validation needs the stack).
        executor_obj._compose = compose  # type: ignore[method=assign]

        await executor_obj.resume_pr_monitor(ws_id)

        # Injection alone does not opt a workspace into hosted mode.
        assert compose.ensure_project_up_calls == [ws_id]
        assert len(monitor.run_calls) == 1
        assert monitor.run_calls[0]["workspace_id"] == ws_id

    @pytest.mark.unit
    async def test_injected_executor_stops_when_compose_restart_is_unusable(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        compose = _FailingCompose()
        executor = _RecordingExecutor()
        monitor = _RecordingMonitor()

        async def _runtime_unusable(_compose_project: str, _compose_file: Path) -> bool:
            return False

        monkeypatch.setattr(
            monitor_handoff_module,
            "_compose_runtime_usable_after_restart_failure",
            _runtime_unusable,
        )
        real_compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        executor_obj = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            compose=real_compose,
            pr_monitor_factory=lambda *_args, **_kwargs: monitor,
            agent_runtime_executor=executor,
        )
        executor_obj._compose = compose  # type: ignore[method-assign]

        await executor_obj.resume_pr_monitor(ws_id)

        assert compose.ensure_project_up_calls == [ws_id]
        assert monitor.run_calls == []

    @pytest.mark.unit
    async def test_local_path_without_executor_still_restarts_compose(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        compose = _RecordingCompose()
        monitor = _RecordingMonitor()
        real_compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        executor_obj = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            compose=real_compose,
            pr_monitor_factory=lambda *_args, **_kwargs: monitor,
            agent_runtime_executor=None,
        )
        executor_obj._compose = compose  # type: ignore[method-assign]

        await executor_obj.resume_pr_monitor(ws_id)

        # Local path still restarts compose exactly once.
        assert compose.ensure_project_up_calls == [ws_id]
        assert len(monitor.run_calls) == 1


class TestResumeHandoffHostedCheckoutRestore:
    @pytest.mark.unit
    async def test_pod_restart_restores_checkout_before_monitor_at_pull_head(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_pr(
            factory,
            task_policy={"pr_adoption": {"execution": {"mode": "hosted"}}},
            compose_project_name=None,
            compose_file_path=None,
            task_kind="sync_feature_pr",
        )
        worktree = tmp_path / "work" / "worktrees" / ws_id
        assert not worktree.exists()

        order: list[str] = []
        restore_calls: list[str] = []

        async def _restore(workspace_id: str) -> None:
            restore_calls.append(workspace_id)
            order.append("restore")
            worktree.mkdir(parents=True)
            (worktree / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")

        class _OrderedMonitor(_RecordingMonitor):
            async def run(self, **kwargs: Any) -> None:
                order.append("monitor_run")
                await super().run(**kwargs)

        compose = _RecordingCompose()
        monitor = _OrderedMonitor()
        factory_calls: list[str] = []

        def _factory(*_args: Any, **_kwargs: Any) -> _OrderedMonitor:
            factory_calls.append("factory")
            order.append("factory")
            return monitor

        real_compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        executor_obj = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            compose=real_compose,
            pr_monitor_factory=_factory,
            agent_runtime_executor=_RecordingExecutor(),
            ensure_hosted_monitor_checkout=_restore,
        )
        executor_obj._compose = compose  # type: ignore[method-assign]

        await executor_obj.resume_pr_monitor(ws_id)

        assert restore_calls == [ws_id]
        assert compose.ensure_project_up_calls == []
        assert order[0] == "restore"
        assert "factory" in order
        assert order.index("restore") < order.index("factory")
        assert order.index("restore") < order.index("monitor_run")
        assert len(monitor.run_calls) == 1

    @pytest.mark.unit
    async def test_idempotent_restore_skips_when_worktree_valid(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.node import provisioner as provisioner_module
        from awf.node.git_manager import GitManager, WorktreeLayout
        from awf.node.provisioner import Provisioner, ProvisionerConfig

        ws_id = await _seed_monitoring_pr(
            factory,
            task_policy={"pr_adoption": {"execution": {"mode": "hosted"}}},
            compose_project_name=None,
            compose_file_path=None,
            task_kind="sync_feature_pr",
        )
        worktree = tmp_path / "work" / "worktrees" / ws_id
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")

        git = GitManager(tmp_path / "work" / "git")
        add_calls: list[str] = []

        async def _add_worktree(**kwargs: Any) -> WorktreeLayout:
            add_calls.append(str(kwargs.get("workspace_id")))
            raise AssertionError("add_worktree must not run for a valid checkout")

        monkeypatch.setattr(git, "add_worktree", _add_worktree)
        provisioner = Provisioner(
            session_factory=factory,
            git=git,
            config=ProvisionerConfig(node_id="node-a", branch_prefix="awf"),
        )
        # Point GitManager worktrees at the executor worktrees root layout.
        git._worktrees_dir = tmp_path / "work" / "worktrees"  # noqa: SLF001

        compose = _RecordingCompose()
        monitor = _RecordingMonitor()
        real_compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        executor_obj = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            compose=real_compose,
            pr_monitor_factory=lambda *_a, **_k: monitor,
            agent_runtime_executor=_RecordingExecutor(),
            ensure_hosted_monitor_checkout=provisioner.ensure_hosted_monitor_worktree,
        )
        executor_obj._compose = compose  # type: ignore[method-assign]

        # ensure_worktree path uses git._worktrees_dir; provisioner helper uses
        # _provision_checkout_base_branch — spy that tip for pull-head coverage.
        tips: list[str] = []
        real_checkout_base = provisioner_module._provision_checkout_base_branch

        def _spy_checkout_base(ws: Any) -> str:
            tip = real_checkout_base(ws)
            tips.append(tip)
            return tip

        monkeypatch.setattr(
            provisioner_module, "_provision_checkout_base_branch", _spy_checkout_base
        )

        await executor_obj.resume_pr_monitor(ws_id)

        assert add_calls == []
        assert tips == ["refs/pull/42/head"]
        assert compose.ensure_project_up_calls == []
        assert len(monitor.run_calls) == 1

    @pytest.mark.unit
    async def test_fail_closed_when_checkout_restore_impossible(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        from awf.db.repositories import WorkspaceEventRepository
        from awf.node.git_manager import GitOperationError

        ws_id = await _seed_monitoring_pr(
            factory,
            task_policy={"pr_adoption": {"execution": {"mode": "hosted"}}},
            compose_project_name=None,
            compose_file_path=None,
            task_kind="sync_feature_pr",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.monitor_claimed_by = "worker-old"
            await s.commit()
        monitor = _RecordingMonitor()

        async def _restore(_workspace_id: str) -> None:
            raise GitOperationError(
                operation="hosted_monitor.ensure_worktree",
                returncode=1,
                stdout="",
                stderr="missing adoption tip",
                reason_code="MONITOR_RECOVERY_CHECKOUT_RESTORE_FAILED",
            )

        real_compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        executor_obj = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            compose=real_compose,
            pr_monitor_factory=lambda *_a, **_k: monitor,
            agent_runtime_executor=_RecordingExecutor(),
            ensure_hosted_monitor_checkout=_restore,
        )
        executor_obj._compose = _RecordingCompose()  # type: ignore[method-assign]

        await executor_obj.resume_pr_monitor(ws_id)

        assert monitor.run_calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
            assert any(
                event.reason_code == "MONITOR_RECOVERY_CHECKOUT_RESTORE_FAILED" for event in events
            )

    @pytest.mark.unit
    async def test_superseded_restore_failure_does_not_fail_new_owner(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Lease takeover mid-restore must not let the old owner call mark_failed.

        PRRT_kwDOSJAM6s6dNBTV: a slow hosted checkout restore can outlive the
        monitor lease; if restore then errors, status-only ``_mark_failed`` would
        fail ``monitoring_pr`` underneath the replacement claimant.
        """
        from awf.node.git_manager import GitOperationError

        ws_id = await _seed_monitoring_pr(
            factory,
            task_policy={"pr_adoption": {"execution": {"mode": "hosted"}}},
            compose_project_name=None,
            compose_file_path=None,
            task_kind="sync_feature_pr",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.monitor_claimed_by = "worker-old"
            await s.commit()

        monitor = _RecordingMonitor()

        async def _restore_after_takeover(_workspace_id: str) -> None:
            async with factory() as s:
                ws = await WorkspaceRepository(s).get(ws_id)
                assert ws is not None
                ws.monitor_claimed_by = "worker-new"
                await s.commit()
            raise GitOperationError(
                operation="hosted_monitor.ensure_worktree",
                returncode=1,
                stdout="",
                stderr="restore raced with takeover",
                reason_code="MONITOR_RECOVERY_CHECKOUT_RESTORE_FAILED",
            )

        real_compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        executor_obj = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            compose=real_compose,
            pr_monitor_factory=lambda *_a, **_k: monitor,
            agent_runtime_executor=_RecordingExecutor(),
            ensure_hosted_monitor_checkout=_restore_after_takeover,
        )
        executor_obj._compose = _RecordingCompose()  # type: ignore[method-assign]

        await executor_obj.resume_pr_monitor(ws_id)

        assert monitor.run_calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.monitor_claimed_by == "worker-new"

    @pytest.mark.unit
    async def test_superseded_before_restore_skips_checkout(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Pre-restore monitor-owner recheck must skip restore after takeover."""
        ws_id = await _seed_monitoring_pr(
            factory,
            task_policy={"pr_adoption": {"execution": {"mode": "hosted"}}},
            compose_project_name=None,
            compose_file_path=None,
            task_kind="sync_feature_pr",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.monitor_claimed_by = "worker-old"
            await s.commit()

        restore_calls: list[str] = []

        async def _restore(workspace_id: str) -> None:
            restore_calls.append(workspace_id)

        # Capture owner at load, then transfer claim before handoff continues past
        # the early status recheck by transferring after load via a patched recheck.
        real_compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        executor_obj = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            compose=real_compose,
            pr_monitor_factory=lambda *_a, **_k: _RecordingMonitor(),
            agent_runtime_executor=_RecordingExecutor(),
            ensure_hosted_monitor_checkout=_restore,
        )
        executor_obj._compose = _RecordingCompose()  # type: ignore[method-assign]

        original_recheck = executor_obj._recheck_status
        saw_pre_restore = False

        async def _recheck(
            workspace_id: str,
            *,
            expected: WorkspaceStatus,
            action: str,
            reason_code: str = "EXECUTOR_STALE_STATUS",
            owner_id: str | None = None,
            owner_mismatch_reason_code: str = "EXECUTOR_STALE_CLAIM",
            monitor_owner_id: str | None = None,
            monitor_owner_mismatch_reason_code: str = "EXECUTOR_STALE_MONITOR_CLAIM",
        ) -> bool:
            nonlocal saw_pre_restore
            if action == "resume_hosted_checkout" and not saw_pre_restore:
                saw_pre_restore = True
                async with factory() as s:
                    ws = await WorkspaceRepository(s).get(ws_id)
                    assert ws is not None
                    ws.monitor_claimed_by = "worker-new"
                    await s.commit()
            return await original_recheck(
                workspace_id,
                expected=expected,
                action=action,
                reason_code=reason_code,
                owner_id=owner_id,
                owner_mismatch_reason_code=owner_mismatch_reason_code,
                monitor_owner_id=monitor_owner_id,
                monitor_owner_mismatch_reason_code=monitor_owner_mismatch_reason_code,
            )

        executor_obj._recheck_status = _recheck  # type: ignore[method-assign]

        await executor_obj.resume_pr_monitor(ws_id)

        assert saw_pre_restore
        assert restore_calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.monitor_claimed_by == "worker-new"

    @pytest.mark.unit
    async def test_checkout_restore_failure_redacts_git_diagnostics(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        import structlog.testing

        from awf.node.git_manager import GitOperationError

        secret_stderr = (
            "fatal: unable to access "
            "https://x-access-token:ghp_should_not_persist@github.com/org/repo/"
        )
        ws_id = await _seed_monitoring_pr(
            factory,
            task_policy={"pr_adoption": {"execution": {"mode": "hosted"}}},
            compose_project_name=None,
            compose_file_path=None,
            task_kind="sync_feature_pr",
        )
        monitor = _RecordingMonitor()

        async def _restore(_workspace_id: str) -> None:
            raise GitOperationError(
                operation="hosted_monitor.ensure_worktree",
                returncode=128,
                stdout="",
                stderr=secret_stderr,
                reason_code="MONITOR_RECOVERY_CHECKOUT_RESTORE_FAILED",
            )

        real_compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        executor_obj = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            compose=real_compose,
            pr_monitor_factory=lambda *_a, **_k: monitor,
            agent_runtime_executor=_RecordingExecutor(),
            ensure_hosted_monitor_checkout=_restore,
        )
        executor_obj._compose = _RecordingCompose()  # type: ignore[method-assign]

        with structlog.testing.capture_logs() as captured:
            await executor_obj.resume_pr_monitor(ws_id)

        restore_logs = [
            entry
            for entry in captured
            if entry.get("event") == "executor.resume_hosted_checkout_restore_failed"
        ]
        assert restore_logs
        assert "ghp_should_not_persist" not in repr(restore_logs)
        assert "https://[redacted]@github.com/org/repo/" in str(restore_logs[0].get("stderr", ""))

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "ghp_should_not_persist" not in (ws.failure_message or "")
            assert "https://[redacted]@github.com/org/repo/" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_resume_reuses_pending_monitor_comment_operation(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        from awf.db.enums import OperationStatus, OperationType
        from awf.db.repositories import OperationRepository
        from awf.runtime.pr_monitor_operations import (
            create_or_start_monitor_operation,
            monitor_operation_idempotency_key,
        )

        ws_id = await _seed_monitoring_pr(
            factory,
            task_policy={"pr_adoption": {"execution": {"mode": "hosted"}}},
            compose_project_name=None,
            compose_file_path=None,
            task_kind="sync_feature_pr",
        )
        idempotency_key = monitor_operation_idempotency_key(
            workspace_id=ws_id,
            action="address_comments",
            pr_number=42,
            reason_code="ADDRESS_COMMENTS",
            source_head_sha="b" * 40,
            source_base_sha="a" * 40,
        )
        async with factory() as s:
            handle = await create_or_start_monitor_operation(
                s,
                workspace_id=ws_id,
                operation_type=OperationType.comment_repair,
                payload={"owner": "pr_monitor", "action": "address_comments"},
                idempotency_key=idempotency_key,
                status=OperationStatus.pending,
            )
            await s.commit()
            original_id = handle.operation_id

        async def _restore(workspace_id: str) -> None:
            path = tmp_path / "work" / "worktrees" / workspace_id
            path.mkdir(parents=True, exist_ok=True)
            (path / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")

        monitor = _RecordingMonitor()
        real_compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        executor_obj = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            compose=real_compose,
            pr_monitor_factory=lambda *_a, **_k: monitor,
            agent_runtime_executor=_RecordingExecutor(),
            ensure_hosted_monitor_checkout=_restore,
        )
        executor_obj._compose = _RecordingCompose()  # type: ignore[method-assign]

        await executor_obj.resume_pr_monitor(ws_id)

        assert len(monitor.run_calls) == 1
        async with factory() as s:
            reused = await create_or_start_monitor_operation(
                s,
                workspace_id=ws_id,
                operation_type=OperationType.comment_repair,
                payload={"owner": "pr_monitor", "action": "address_comments"},
                idempotency_key=idempotency_key,
                status=OperationStatus.running,
            )
            ops = await OperationRepository(s).list_for_workspace(ws_id)
            matching = [op for op in ops if op.idempotency_key == idempotency_key]
            assert len(matching) == 1
            assert reused.operation_id == original_id


@pytest.mark.unit
def test_compose_runtime_requirements_respect_profiles_and_completion_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback inspection mirrors Compose profile filtering and one-shot deps."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: app
    depends_on:
      migrate:
        condition: service_completed_successfully
      cache: service_started
      external:
        condition: service_completed_successfully
  migrate:
    image: migrate
  cache:
    image: cache
    profiles: ["worker"]
  skipped:
    image: skipped
    profiles: ["debug"]
  malformed:
    image: malformed
    profiles: ["worker", 7]
  scalar: scalar-service
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("COMPOSE_PROFILES", "worker")

    requirements = monitor_handoff_module._compose_runtime_requirements_from_file(compose_file)

    assert requirements is not None
    assert requirements.service_names == {"app", "migrate", "cache", "malformed", "scalar"}
    assert requirements.successful_completion_services == {"migrate"}


@pytest.mark.unit
def test_compose_runtime_requirements_handle_unreadable_and_empty_shapes(
    tmp_path: Path,
) -> None:
    """Unavailable service metadata is distinguished from empty service metadata."""
    compose_file = tmp_path / "compose.yml"

    compose_file.write_text("services: [", encoding="utf-8")
    assert monitor_handoff_module._compose_runtime_requirements_from_file(compose_file) is None

    compose_file.write_text("[]\n", encoding="utf-8")
    list_payload = monitor_handoff_module._compose_runtime_requirements_from_file(compose_file)
    assert list_payload is not None
    assert list_payload.service_names == set()
    assert list_payload.successful_completion_services == set()

    compose_file.write_text("services: []\n", encoding="utf-8")
    list_services = monitor_handoff_module._compose_runtime_requirements_from_file(compose_file)
    assert list_services is not None
    assert list_services.service_names == set()
    assert list_services.successful_completion_services == set()


@pytest.mark.unit
async def test_compose_runtime_fallback_accepts_running_stack_with_completed_one_shot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed restart can resume when required services are already satisfied."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: app
    depends_on:
      migrate:
        condition: service_completed_successfully
  migrate:
    image: migrate
""",
        encoding="utf-8",
    )

    async def _inspect(_compose_project: str) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(name="app", container_id="1", image="app", state="running"),
                RuntimeService(
                    name="migrate",
                    container_id="2",
                    image="migrate",
                    state="exited",
                    status="Exited (0) 3 seconds ago",
                ),
            ],
        )

    monkeypatch.setattr(monitor_handoff_module, "_inspect_compose_runtime", _inspect)

    assert await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
        "awf_ws", compose_file
    )


@pytest.mark.unit
async def test_compose_runtime_fallback_rejects_unusable_existing_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing or unsatisfied services keep monitor resume stopped after restart failure."""
    compose_file = tmp_path / "compose.yml"
    empty_compose_file = tmp_path / "empty-compose.yml"
    one_shot_compose_file = tmp_path / "one-shot-compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: app
  migrate:
    image: migrate
""",
        encoding="utf-8",
    )
    empty_compose_file.write_text("services: {}\n", encoding="utf-8")
    one_shot_compose_file.write_text(
        """
services:
  app:
    image: app
    depends_on:
      migrate:
        condition: service_completed_successfully
  migrate:
    image: migrate
""",
        encoding="utf-8",
    )
    snapshots: list[RuntimeSnapshot] = [
        RuntimeSnapshot(stack_state="running"),
        RuntimeSnapshot(stack_state="stopped"),
        RuntimeSnapshot(
            stack_state="running",
            services=[RuntimeService(name="app", container_id="1", image="app", state="running")],
        ),
        RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(
                    name="app",
                    container_id="1",
                    image="app",
                    state="running",
                    health="unhealthy",
                ),
                RuntimeService(
                    name="migrate",
                    container_id="2",
                    image="migrate",
                    state="created",
                    status="Created",
                ),
            ],
        ),
    ]

    async def _inspect(_compose_project: str) -> RuntimeSnapshot:
        return snapshots.pop(0)

    monkeypatch.setattr(monitor_handoff_module, "_inspect_compose_runtime", _inspect)

    assert not await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
        "awf_ws", empty_compose_file
    )
    assert not await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
        "awf_ws", compose_file
    )
    assert not await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
        "awf_ws", compose_file
    )
    assert not await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
        "awf_ws", one_shot_compose_file
    )
