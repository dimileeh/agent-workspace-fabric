"""Worker runtime wiring for the local AWF service."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

import awf.adapters.registry  # noqa: F401 - populate adapter registry for service execution
from awf.adapters.base import AgentAdapter
from awf.common.commands import AsyncioSubprocessRunner
from awf.common.github_client import GitHubClient
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.session import make_engine, make_session_factory
from awf.node.auth_mounts import ServiceAuthMountResolver
from awf.node.compose_manager import ComposeManager
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.node.stack_launcher import ComposeStackLauncher
from awf.profiles.models import WorkspaceProfile
from awf.runtime.logs import LogStore
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.release_pr_monitor import build_feature_pr_monitor
from awf.runtime.validation import ValidationRunner
from awf.service.config import ServiceSettings


@dataclass(frozen=True)
class WorkerRuntime:
    engine: AsyncEngine
    worker: ControlWorker


def build_worker_runtime(settings: ServiceSettings) -> WorkerRuntime:
    """Construct the DB/session, provisioner, executor, and worker."""

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    work_dir = Path(settings.work_dir).expanduser().resolve()
    template = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"
    git = GitManager(work_dir / "git")
    compose = ComposeManager(work_dir=work_dir, template_path=template)
    runner = AsyncioSubprocessRunner()
    log_store = LogStore(root=work_dir / "logs", session_factory=session_factory)
    validation = ValidationRunner(
        runner=runner,
        artifacts_dir=work_dir / "artifacts",
        log_store=log_store,
    )
    pr_creator = PullRequestCreator(runner)
    gh = GitHubClient(runner)
    auth_mount_resolver = ServiceAuthMountResolver(
        host_home=Path(settings.host_home).expanduser().resolve(),
        work_dir=work_dir,
    )
    stack_launcher = ComposeStackLauncher(
        compose=compose,
        agent_runtime_image=settings.agent_runtime_image,
        auth_mount_resolver=auth_mount_resolver,
    )
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git,
        stack_launcher=stack_launcher,
        config=ProvisionerConfig(
            node_id=settings.node_id or socket.gethostname(),
            branch_prefix=settings.branch_prefix,
        ),
    )

    def _feature_pr_monitor_factory(
        adapter: AgentAdapter,
        profile: WorkspaceProfile,
    ) -> Any:
        # Local service-created API workspaces currently represent feature PR
        # tasks only. Manual/release monitor routing needs a task-kind API
        # surface before the service can select build_release_pr_monitor here.
        return build_feature_pr_monitor(
            session_factory=session_factory,
            runner=runner,
            adapter=adapter,
            gh=gh,
            worktrees_root=work_dir / "git" / "worktrees",
            artifacts_root=work_dir / "artifacts",
            initial_review_grace_period_seconds=(
                profile.monitor.initial_review_grace_period_seconds
            ),
            log_store=log_store,
        )

    executor = WorkspaceExecutor(
        session_factory=session_factory,
        runner=runner,
        compose=compose,
        validation=validation,
        pr_creator=pr_creator,
        config=ExecutorConfig(
            worktrees_root=work_dir / "git" / "worktrees",
            # Matches ComposeManager(work_dir=work_dir): render path is
            # <work_dir>/compose/<workspace_id>/compose.yml. The executor
            # first uses Workspace.compose_file_path persisted by the
            # provisioner, so this is only a legacy-row fallback.
            compose_projects_root=work_dir / "compose",
        ),
        pr_monitor_factory=_feature_pr_monitor_factory,
        log_store=log_store,
    )
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,
        executor=executor,
        config=WorkerConfig(
            poll_interval_seconds=settings.worker_poll_interval_seconds,
            max_concurrent_provisions=settings.worker_max_concurrent_provisions,
        ),
    )
    return WorkerRuntime(engine=engine, worker=worker)


async def run_worker(settings: ServiceSettings, *, once: bool = False) -> None:
    """Run the control worker, disposing the DB engine on exit."""

    runtime = build_worker_runtime(settings)
    try:
        if once:
            await runtime.worker.run_once()
            await runtime.worker.wait_for_execution_tasks()
            return
        await runtime.worker.run_forever()
    finally:
        await runtime.engine.dispose()
