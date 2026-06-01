"""Worker runtime wiring for the local AWF service."""

from __future__ import annotations

import os
import shlex
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.adapters.registry  # noqa: F401 - populate adapter registry for service execution
from awf.adapters.base import AgentAdapter
from awf.common.commands import AsyncioSubprocessRunner
from awf.common.forge import concrete_forge, make_forge_client
from awf.common.github_client import BranchOpenPullRequestResolver
from awf.common.logging import get_logger
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.enums import TaskKind
from awf.db.models import Workspace
from awf.db.session import make_engine, make_session_factory
from awf.node.auth_mounts import ServiceAuthMountResolver
from awf.node.cleanup import WorkspaceCleaner
from awf.node.companion_images import CompanionImageBuilder
from awf.node.compose_manager import ComposeManager
from awf.node.git_manager import AGENT_RUNTIME_GID, AGENT_RUNTIME_UID, GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.node.secret_mounts import LocalSecretLeaseMountResolver
from awf.node.stack_launcher import ComposeStackLauncher
from awf.profiles.models import WorkspaceProfile
from awf.runtime.logs import LogStore
from awf.runtime.merge_coordinator import (
    InProcessMergeCoordinator,
    MergeCoordinator,
    PostgresAdvisoryMergeCoordinator,
)
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.release_pr_monitor import build_feature_pr_monitor, build_release_pr_monitor
from awf.runtime.validation import ValidationRunner
from awf.runtime.workspace_prompt_context import render_workspace_runtime_context
from awf.service.config import ServiceSettings
from awf.service.staleness import TargetBranchState
from awf.service.target_branch_monitor import (
    GitCheckoutTargetBranchStateProvider,
    TargetBranchReconcileMonitor,
    reconcile_and_refresh_stale_candidates,
)
from awf.service.usage_collection import CcusageCollector

_log = get_logger(__name__)


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
    git = GitManager(
        work_dir / "git",
        env=git_env,
        worktree_owner_uid=AGENT_RUNTIME_UID,
        worktree_owner_gid=AGENT_RUNTIME_GID,
    )
    compose = ComposeManager(work_dir=work_dir, template_path=template)
    runtime_cleaner = WorkspaceCleaner(git=git, compose=compose)
    runner = AsyncioSubprocessRunner()
    usage_collector = CcusageCollector(runner=runner, work_dir=work_dir)
    log_store = LogStore(root=work_dir / "logs", session_factory=session_factory)
    merge_coordinator = _merge_coordinator_for_database_url(settings.database_url, engine=engine)
    validation = ValidationRunner(
        runner=runner,
        artifacts_dir=work_dir / "artifacts",
        log_store=log_store,
    )
    pr_creator = PullRequestCreator(runner)
    open_pr_resolver = BranchOpenPullRequestResolver(runner)
    target_branch_reconciler = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=work_dir,
    )

    async def _post_merge_reconciler(*, repo_url: str, branch: str, workspace_id: str) -> object:
        checkout_path = target_branch_reconciler.checkout_path(
            repo_url=repo_url,
            branch=branch,
        )

        provider = GitCheckoutTargetBranchStateProvider(
            runner=runner,
            checkout_path=checkout_path,
        )

        async def _target_state_for_base_sha(base_sha: str) -> TargetBranchState:
            return await provider.fetch(
                repo_url=repo_url,
                branch=branch,
                base_sha=base_sha,
            )

        return await reconcile_and_refresh_stale_candidates(
            reconcile_fn=target_branch_reconciler.reconcile,
            repo_url=repo_url,
            branch=branch,
            session_factory=session_factory,
            target_state_for_base_sha=_target_state_for_base_sha,
            exclude_workspace_ids={workspace_id},
        )

    auth_mount_resolver = ServiceAuthMountResolver(
        host_home=host_home,
        work_dir=work_dir,
        workspace_owner_uid=AGENT_RUNTIME_UID,
        workspace_owner_gid=AGENT_RUNTIME_GID,
    )
    secret_lease_resolver = LocalSecretLeaseMountResolver(
        host_home=host_home,
        work_dir=work_dir,
        host_env=os.environ,
    )
    companion_image_builder = _companion_image_builder_for(settings, compose)
    stack_launcher = ComposeStackLauncher(
        compose=compose,
        agent_runtime_image=settings.agent_runtime_image,
        auth_mount_resolver=auth_mount_resolver,
        secret_lease_resolver=secret_lease_resolver,
        companion_image_builder=companion_image_builder,
    )
    node_id = settings.node_id or socket.gethostname()
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git,
        stack_launcher=stack_launcher,
        service_diagnostics=compose,
        config=ProvisionerConfig(
            node_id=node_id,
            branch_prefix=settings.branch_prefix,
            service_startup_log_tail_lines=settings.service_startup_log_tail_lines,
        ),
    )

    def _pr_monitor_factory(
        adapter: AgentAdapter,
        profile: WorkspaceProfile,
        workspace: Workspace,
        *,
        provider_recovery_default_model: str | None = None,
    ) -> Any:
        # sync_release_pr's contract is "auto_merge forced False; never merges"
        # (TaskKind.sync_release_pr). Bind the release monitor to the task kind so
        # the human-gated guarantee can't hinge on the persisted auto_merge flag
        # being False at every monitor (re)build.
        force_release_monitor = workspace.task_kind == TaskKind.sync_release_pr.value
        monitor_builder = (
            build_feature_pr_monitor
            if workspace.auto_merge and not force_release_monitor
            else build_release_pr_monitor
        )
        # Build the forge client from the persisted resolved forge (reconstructed,
        # never re-resolved). github → GitHubClient (unchanged behavior); an
        # unsupported forge (e.g. bitbucket) raises ForgeNotSupportedError so the
        # monitor build fails fast rather than mis-routing to GitHub.
        gh = make_forge_client(concrete_forge(profile.forge), runner)
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
            "validation": validation,
            "worktrees_root": work_dir / "git" / "worktrees",
            "artifacts_root": work_dir / "artifacts",
            "initial_review_grace_period_seconds": grace_seconds,
            "non_check_reviewer_settle_seconds": (
                profile.monitor.non_check_reviewer_settle_seconds
            ),
            "non_check_reviewer_logins": profile.monitor.non_check_reviewer_logins,
            "log_store": log_store,
            "merge_coordinator": merge_coordinator,
            "post_merge_target_reconciler": _post_merge_reconciler,
            "workspace_runtime_context": render_workspace_runtime_context(profile),
            "provider_recovery_default_model": provider_recovery_default_model,
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
            agent_wall_timeout_seconds=settings.agent_wall_timeout_seconds,
            agent_idle_timeout_seconds=settings.agent_idle_timeout_seconds,
            planning_max_iterations_default=settings.planning_max_iterations_default,
        ),
        pr_monitor_factory=_pr_monitor_factory,
        log_store=log_store,
        usage_sampler=usage_collector,
    )
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,
        executor=executor,
        runtime_cleaner=runtime_cleaner,
        open_pr_resolver=open_pr_resolver,
        config=WorkerConfig(
            poll_interval_seconds=settings.worker_poll_interval_seconds,
            max_concurrent_provisions=settings.worker_max_concurrent_provisions,
            max_concurrent_executions=settings.worker_max_concurrent_executions,
            node_id=node_id,
            local_capacity_cpu_cores=settings.local_capacity_cpu_cores,
            local_capacity_memory_gb=settings.local_capacity_memory_gb,
            local_capacity_dind_slots=settings.local_capacity_dind_slots,
            workspace_steady_cpu=settings.workspace_steady_cpu,
            workspace_steady_memory_gb=settings.workspace_steady_memory_gb,
            workspace_peak_cpu=settings.workspace_peak_cpu,
            workspace_peak_memory_gb=settings.workspace_peak_memory_gb,
        ),
    )
    return WorkerRuntime(engine=engine, worker=worker)


def _companion_image_builder_for(
    settings: ServiceSettings, compose: ComposeManager
) -> CompanionImageBuilder | None:
    """Return a companion image builder unless caching is disabled by config."""
    if not settings.companion_image_cache_enabled:
        return None
    return CompanionImageBuilder(compose)


def _merge_coordinator_for_database_url(
    database_url: str,
    *,
    engine: AsyncEngine,
) -> MergeCoordinator:
    if _is_postgres_database_url(database_url):
        return PostgresAdvisoryMergeCoordinator(engine)
    return InProcessMergeCoordinator()


def _is_postgres_database_url(database_url: str) -> bool:
    try:
        backend = make_url(database_url).get_backend_name()
    except ArgumentError as exc:
        _log.warning(
            "worker.database_url_parse_failed",
            error=str(exc),
            merge_coordinator="in_process",
        )
        return False
    if backend in {"postgres", "postgresql"}:
        return True
    if backend.startswith("postgres"):
        _log.warning(
            "worker.postgres_merge_coordinator_not_selected",
            backend=backend,
            merge_coordinator="in_process",
        )
    return False


def _service_git_environment(host_home: Path, *, github_token: str | None = None) -> dict[str, str]:
    """Git/SSH environment for service-worker host repository operations."""

    env = {"HOME": str(host_home)}
    _add_git_config_entries(env, (("safe.directory", "*"),))
    if github_token:
        # GitHub CLI cannot read macOS Keychain tokens from inside the local
        # service container. Forward an explicit service token to gh subprocesses.
        env["GH_TOKEN"] = github_token
        env["GITHUB_TOKEN"] = github_token
        _add_git_config_entries(
            env,
            (
                ("credential.https://github.com.helper", "!gh auth git-credential"),
                ("url.https://github.com/.insteadOf", "git@github.com:"),
            ),
        )
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


def _add_git_config_entries(
    env: dict[str, str],
    entries: tuple[tuple[str, str], ...],
) -> None:
    start_index = int(env.get("GIT_CONFIG_COUNT", "0"))
    for offset, (key, value) in enumerate(entries):
        index = start_index + offset
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(start_index + len(entries))


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
