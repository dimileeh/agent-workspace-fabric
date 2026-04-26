"""Worker runtime wiring for the local AWF service."""

from __future__ import annotations

import os
import shlex
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
from awf.db.models import Workspace
from awf.db.session import make_engine, make_session_factory
from awf.node.auth_mounts import ServiceAuthMountResolver
from awf.node.compose_manager import ComposeManager
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.node.stack_launcher import ComposeStackLauncher
from awf.profiles.models import WorkspaceProfile
from awf.runtime.logs import LogStore
from awf.runtime.merge_coordinator import InProcessMergeCoordinator
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.release_pr_monitor import build_feature_pr_monitor, build_release_pr_monitor
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
    host_home = Path(settings.host_home).expanduser().resolve()
    template = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"
    git_env = _service_git_environment(host_home, github_token=settings.github_token)
    _apply_service_git_environment(git_env)
    git = GitManager(work_dir / "git", env=git_env)
    compose = ComposeManager(work_dir=work_dir, template_path=template)
    runner = AsyncioSubprocessRunner()
    log_store = LogStore(root=work_dir / "logs", session_factory=session_factory)
    merge_coordinator = InProcessMergeCoordinator()
    validation = ValidationRunner(
        runner=runner,
        artifacts_dir=work_dir / "artifacts",
        log_store=log_store,
    )
    pr_creator = PullRequestCreator(runner)
    gh = GitHubClient(runner)
    auth_mount_resolver = ServiceAuthMountResolver(
        host_home=host_home,
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

    def _pr_monitor_factory(
        adapter: AgentAdapter,
        profile: WorkspaceProfile,
        workspace: Workspace,
    ) -> Any:
        monitor_builder = (
            build_feature_pr_monitor if workspace.auto_merge else build_release_pr_monitor
        )
        grace_seconds = (
            workspace.initial_review_grace_period_seconds
            if workspace.initial_review_grace_period_seconds is not None
            else profile.monitor.initial_review_grace_period_seconds
        )
        monitor_kwargs: dict[str, Any] = {
            "session_factory": session_factory,
            "runner": runner,
            "adapter": adapter,
            "gh": gh,
            "worktrees_root": work_dir / "git" / "worktrees",
            "artifacts_root": work_dir / "artifacts",
            "initial_review_grace_period_seconds": grace_seconds,
            "log_store": log_store,
            "merge_coordinator": merge_coordinator,
        }
        return monitor_builder(**monitor_kwargs)

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
        pr_monitor_factory=_pr_monitor_factory,
        log_store=log_store,
    )
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,
        executor=executor,
        config=WorkerConfig(
            poll_interval_seconds=settings.worker_poll_interval_seconds,
            max_concurrent_provisions=settings.worker_max_concurrent_provisions,
            max_concurrent_executions=settings.worker_max_concurrent_executions,
        ),
    )
    return WorkerRuntime(engine=engine, worker=worker)


def _service_git_environment(host_home: Path, *, github_token: str | None = None) -> dict[str, str]:
    """Git/SSH environment for service-worker host repository operations."""

    env = {"HOME": str(host_home)}
    if github_token:
        # GitHub CLI cannot read macOS Keychain tokens from inside the local
        # service container. Forward an explicit service token to gh subprocesses.
        env["GH_TOKEN"] = github_token
        env["GITHUB_TOKEN"] = github_token
    ssh_command = ["ssh"]
    if ssh_auth_sock := os.environ.get("SSH_AUTH_SOCK"):
        env["SSH_AUTH_SOCK"] = ssh_auth_sock
        ssh_command.extend(["-o", f"IdentityAgent={ssh_auth_sock}"])
    ssh_config = host_home / ".ssh" / "config"
    if ssh_config.is_file():
        ssh_command.extend(["-o", "IgnoreUnknown=UseKeychain", "-F", str(ssh_config)])
    gitconfig = host_home / ".gitconfig"
    if gitconfig.is_file():
        env["GIT_CONFIG_GLOBAL"] = str(gitconfig)
    known_hosts = host_home / ".ssh" / "known_hosts"
    if known_hosts.is_file():
        ssh_command.extend(
            [
                "-o",
                f"UserKnownHostsFile={known_hosts}",
                "-o",
                "StrictHostKeyChecking=accept-new",
            ]
        )
    if len(ssh_command) > 1:
        env["GIT_SSH_COMMAND"] = " ".join(shlex.quote(part) for part in ssh_command)
    return env


def _apply_service_git_environment(env: dict[str, str]) -> None:
    """Apply host git/SSH settings to subprocesses launched by the worker."""

    os.environ.update(env)


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
