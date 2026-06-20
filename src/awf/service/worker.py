"""Worker runtime wiring for the local AWF service."""

from __future__ import annotations

import asyncio
import os
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.adapters.registry  # noqa: F401 - populate adapter registry for service execution
from awf.adapters.base import AgentAdapter
from awf.common.commands import AsyncioSubprocessRunner
from awf.common.forge import ForgeClient, concrete_forge_for_repo, make_forge_client
from awf.common.git_auth import add_git_config_entries as _add_git_config_entries
from awf.common.git_auth import apply_bitbucket_git_auth
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
from awf.service.gc import run_service_workspace_gc
from awf.service.gc_claude_base import reap_superseded_claude_bases
from awf.service.gc_reconcile import (
    OrphanDirReconcileResult,
    build_default_compose_teardown,
    reconcile_orphaned_workspace_dirs,
)
from awf.service.gc_terminal_passes import (
    combine_terminal_gc_reports as _combine_terminal_gc_reports,
)
from awf.service.gc_terminal_passes import (
    terminal_gc_discarded_statuses as _terminal_gc_discarded_statuses,
)
from awf.service.node_identity import effective_service_node_id
from awf.service.orphan_resources import (
    OrphanReapResult,
    build_orphan_compose_teardown,
    sweep_classified_orphans,
)
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


# Tasks scheduled by _release_forge_client_after_build_error are kept referenced
# here until they finish so the event loop does not GC a pending close mid-flight
# (the close is fire-and-forget on the error path; nothing else awaits it).
_PENDING_FORGE_CLIENT_CLOSERS: set[asyncio.Task[None]] = set()


def _release_forge_client_after_build_error(gh: ForgeClient) -> None:
    """Close a forge client whose PR-monitor build failed before handoff.

    ``_pr_monitor_factory`` builds the forge client *before* invoking the monitor
    builder, and the runner only releases that client in ``run()``'s ``finally``.
    If the builder raises, ``run()`` never executes, so the freshly built client
    would otherwise leak — for a ``BitbucketClient`` that strands an ``httpx``
    connection pool until GC (a ``GitHubClient`` close is a no-op). Closing here
    keeps the per-monitor client's lifecycle bounded on the build-error path too.

    ``aclose`` is async while the factory is synchronous. In production the
    factory runs inside the executor's event loop, so schedule the close as a
    tracked task; when no loop is running (direct unit-test calls) drive it to
    completion synchronously.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(gh.aclose())
        return
    closer = loop.create_task(gh.aclose())
    _PENDING_FORGE_CLIENT_CLOSERS.add(closer)
    closer.add_done_callback(_PENDING_FORGE_CLIENT_CLOSERS.discard)


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
    node_id = effective_service_node_id(settings)
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
        # never re-resolved). github → GitHubClient; bitbucket → BitbucketClient
        # (issue #345). A genuinely-unknown forge raises ForgeNotSupportedError so
        # the monitor build fails fast rather than mis-routing to GitHub. Use
        # concrete_forge_for_repo (not plain concrete_forge) to mirror the
        # executor forge gate: a legacy/missing snapshot normalizes profile.forge
        # to "auto", so fall back to the workspace repo_url's host. Without this,
        # a monitor rebuild that runs before the executor gate on a pre-Phase-1
        # Bitbucket snapshot would silently construct a GitHubClient instead of
        # routing to Bitbucket. This client owns resources (a BitbucketClient holds
        # an httpx connection pool); the monitor runner closes it via gh.aclose()
        # when its run() finishes, so the per-monitor client is not leaked. If the
        # builder below raises before that handoff, run() never closes it, so the
        # build is guarded to release the client on the error path too.
        gh = make_forge_client(concrete_forge_for_repo(profile.forge, workspace.repo_url), runner)
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
            "require_ci": profile.monitor.require_ci,
            "log_store": log_store,
            "merge_coordinator": merge_coordinator,
            "post_merge_target_reconciler": _post_merge_reconciler,
            "workspace_runtime_context": render_workspace_runtime_context(profile),
            "provider_recovery_default_model": provider_recovery_default_model,
        }
        try:
            return monitor_builder(**monitor_kwargs)
        except Exception:
            # The runner takes ownership of ``gh`` and closes it in run()'s
            # finally, but only once the monitor is built. A build failure means
            # run() never executes, so release the freshly built client here to
            # avoid leaking a Bitbucket httpx pool (PRRT_kwDOSJAM6s6HnRTp).
            _release_forge_client_after_build_error(gh)
            raise

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
    orphan_dir_teardown = build_default_compose_teardown(compose)
    classified_orphan_teardown = build_orphan_compose_teardown(compose)

    async def _reconcile_orphan_dirs() -> OrphanDirReconcileResult:
        # Flag off => report-only (execute=False) so operators still see the
        # leak; flag on => actually reap (compose teardown + filesystem removal).
        return await reconcile_orphaned_workspace_dirs(
            session_factory,
            work_dir=work_dir,
            compose_teardown=orphan_dir_teardown,
            min_age_hours=settings.orphan_reconcile_min_age_hours,
            limit=settings.orphan_reconcile_max_per_scan,
            execute=settings.auto_cleanup_orphans,
        )

    async def _reap_classified_orphans(*, enabled: bool | None = None) -> OrphanReapResult:
        """Sweep classified orphan resources using the worker runtime dependencies.

        ``enabled`` defaults to the ``auto_cleanup_orphans`` flag so the periodic
        backstop stays default-off. The on-demand ``service gc`` path passes
        ``enabled=True`` to force a reap for the explicit operator request regardless
        of the flag (#637); all other scope (retention, min-age) is unchanged.
        """
        resolved_enabled = settings.auto_cleanup_orphans if enabled is None else enabled
        return await sweep_classified_orphans(
            session_factory,
            work_dir=work_dir,
            docker_host=settings.docker_host,
            compose_teardown=classified_orphan_teardown,
            enabled=resolved_enabled,
            min_age_hours=settings.orphan_reconcile_min_age_hours,
            min_retention_hours=settings.completed_workspace_retention_hours,
        )

    async def _reap_superseded_claude_bases() -> dict[str, object]:
        """Reap superseded shared ``~/.claude`` overlay bases from the worker (#509).

        The worker holds CAP_SYS_ADMIN and shares the agent's mount namespace, so
        unlike the API ``/v1/service/gc`` path its ``/proc/mounts`` live-mount
        verification is trustworthy and it can actually reap. The reaper is a
        blocking filesystem operation (multi-GB ``rmtree``, no DB), so run it in a
        worker thread to keep the event loop responsive (mirrors the auth-overlay
        teardown's ``asyncio.to_thread``).
        """
        return await asyncio.to_thread(
            reap_superseded_claude_bases,
            work_dir=work_dir,
            host_home=host_home,
            execute=True,
        )

    async def _reap_terminal_workspace_gc(
        *,
        min_age_hours: float | None = None,
        limit: int | None = None,
        statuses: Sequence[str] | None = None,
        exclude_statuses: Sequence[str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Reap per-workspace terminal-workspace auth dirs from the worker (#513).

        The per-workspace ``auth/<id>/`` dirs (~1.7 GB each) of completed/cancelled/
        destroyed workspaces leak because reclaiming one first unmounts a possible
        live Claude overlay at ``auth/<id>/claude/merged``, and
        ``teardown_workspace_auth_overlay`` raises ``OverlayUnmountUnverifiableError``
        whenever the caller cannot prove it holds CAP_SYS_ADMIN — so the
        capability-less API ``/v1/service/gc`` path records every auth path ``skipped``
        and deletes nothing. The worker holds CAP_SYS_ADMIN and shares the agent's
        mount namespace, so driving the same already-tested GC here makes the
        unmount-before-remove succeed and the dir is actually removed (same root cause
        as #509's shared-base reaper, but for the per-workspace dirs it did not cover).

        Reuse the exact assembly wrapper the API route calls
        (``run_service_workspace_gc``) so the volume-removing compose teardown,
        companion-image prune, and same-pass claude-base reap are threaded identically
        and worker/API behaviour stays in lockstep. It is ``async`` and offloads its
        blocking multi-GB ``rmtree``/DB work via ``asyncio.to_thread`` internally, so
        just ``await`` it (no extra ``to_thread`` wrap, unlike the sync claude-base
        reaper above). Reuse the worker's ``compose`` ComposeManager so the GC does not
        re-resolve the workspace template into a second manager. Return the report dict
        for the worker's summary logger. The first pass inherits its ``preserves_failed_
        workspaces=True`` policy and ``min_age_hours`` retention — failed and too-young
        workspaces are preserved.

        The conservative default policy only classifies completed/failed/superseded rows
        (``plan_terminal_workspace_gc``; guarded by the
        ``test_default_gc_policy_ignores_non_pr_terminal_and_unknown_statuses``
        regression), so it never even looks at ``cancelled``/``destroyed`` workspaces —
        yet their ~1.7 GB auth dirs leak just the same (#513 lists completed/cancelled/
        destroyed). Those discarded rows carry no merged-PR work to preserve, so a second
        explicit ``include_statuses`` pass reaps them by age without disturbing the first
        pass's completed-merged / failed-preservation / superseded age-cap nuance. The
        companion-image prune already ran in the first pass, so the second pass leaves it
        off — but the claude-base reap runs in *both* passes: a superseded
        ``_shared/claude-base`` pinned only by a cancelled/destroyed workspace stays
        protected through the first pass (its ``base.signature`` pin is still on disk
        there) and becomes reapable only once this second pass deletes that auth dir, so
        without a second reap the GB-scale base would leak until a later GC
        (PRRT_kwDOSJAM6s6JbT1B). Both reports are folded into one summary.

        An on-demand ``awf service gc --execute`` delegation forwards the operator's
        resolved ``min_age_hours``/``limit`` and ``--status``/``--exclude-status`` filters
        so this capability-gated reap matches the scope of the API-side worktree/compose
        pass the operator just ran instead of diverging onto the worker's server defaults
        (#590). An explicit ``--status`` set scopes the first pass directly (so the
        augmentation pass is skipped — see ``_terminal_gc_discarded_statuses``);
        ``--exclude-status`` removes statuses from both passes so an excluded auth dir is
        never reclaimed. ``now`` is the API's retention-cutoff anchor (its invocation
        clock): forwarding it to both passes derives the same ``cutoff_at`` the API-side
        pass used rather than recomputing eligibility from this worker's (minutes-later)
        claim clock, so a workspace just under ``--min-age-hours`` at invocation is never
        reaped behind a plan/dry-run that excluded it (PRRT_kwDOSJAM6s6JbriQ). The periodic
        backstop and the test wiring call with no overrides, falling back to the configured
        retention/batch defaults, the full two-pass sweep, and the live clock.
        """
        retention_hours = (
            settings.completed_workspace_retention_hours if min_age_hours is None else min_age_hours
        )
        batch_limit = settings.workspace_cleanup_batch_limit if limit is None else limit
        requested_statuses = tuple(statuses) if statuses else None
        excluded_statuses = tuple(exclude_statuses) if exclude_statuses else None
        default_result = await run_service_workspace_gc(
            session_factory,
            work_dir=work_dir,
            execute=True,
            min_age_hours=retention_hours,
            limit=batch_limit,
            include_statuses=requested_statuses,
            exclude_statuses=excluded_statuses,
            cleanup_enabled=settings.workspace_cleanup_enabled,
            companion_image_cache_enabled=settings.companion_image_cache_enabled,
            companion_image_retention_hours=settings.companion_image_retention_hours,
            host_home=host_home,
            reap_claude_bases=settings.claude_base_gc_enabled,
            compose_manager=compose,
            now=now,
        )
        # The conservative default policy never classifies cancelled/destroyed rows, so a
        # second explicit pass reaps those discarded auth dirs that would otherwise leak
        # (#513) — but only for the statuses the first pass did not already cover and the
        # operator did not exclude (#590). When that set is empty (an explicit ``--status``
        # scope, or both discarded statuses excluded) the first pass is the whole sweep.
        discarded_statuses = _terminal_gc_discarded_statuses(requested_statuses, excluded_statuses)
        if not discarded_statuses:
            return _combine_terminal_gc_reports(default_result.to_dict(), {})
        # Share one cleanup-batch budget across both passes. ``plan_terminal_workspace_gc``
        # caps each call to the batch limit candidates (the "maximum cleanup candidates
        # per batch" invariant), but giving the discarded-status pass the full limit
        # again would let a single sweep reclaim up to ~2x the budget. Subtract the
        # first pass's selected candidates so the combined sweep never exceeds one batch
        # budget; a fully-spent budget makes the second pass a no-op (``limit=0`` selects
        # nothing).
        discarded_limit = max(
            batch_limit - len(default_result.plan.candidates),
            0,
        )
        discarded_result = await run_service_workspace_gc(
            session_factory,
            work_dir=work_dir,
            execute=True,
            min_age_hours=retention_hours,
            limit=discarded_limit,
            cleanup_enabled=settings.workspace_cleanup_enabled,
            include_statuses=discarded_statuses,
            # Re-run the claude-base reap on this pass. The reaper runs *after* the
            # pass deletes its candidate auth dirs (and their ``base.signature`` pins),
            # so a superseded base pinned only by a cancelled/destroyed workspace — kept
            # protected through the first pass while its pin was still on disk — is reaped
            # now instead of leaking until a later GC (PRRT_kwDOSJAM6s6JbT1B). The
            # companion-image prune stays off (it already ran in the first pass).
            host_home=host_home,
            reap_claude_bases=settings.claude_base_gc_enabled,
            compose_manager=compose,
            now=now,
        )
        return _combine_terminal_gc_reports(default_result.to_dict(), discarded_result.to_dict())

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,
        executor=executor,
        runtime_cleaner=runtime_cleaner,
        open_pr_resolver=open_pr_resolver,
        orphan_dir_reconciler=_reconcile_orphan_dirs,
        classified_orphan_reaper=_reap_classified_orphans,
        claude_base_reaper=_reap_superseded_claude_bases,
        terminal_gc_reaper=_reap_terminal_workspace_gc,
        # The work dir backs ``auth/<id>/claude`` overlays; the worker (alone
        # holding CAP_SYS_ADMIN + the agent's mount namespace) unmounts a reaped
        # workspace's overlay on terminal-runtime-release before GC removes the dir.
        auth_overlay_work_dir=work_dir,
        config=WorkerConfig(
            poll_interval_seconds=settings.worker_poll_interval_seconds,
            max_concurrent_provisions=settings.worker_max_concurrent_provisions,
            max_concurrent_executions=settings.worker_max_concurrent_executions,
            orphan_reconcile_scan_interval_seconds=(
                settings.orphan_reconcile_scan_interval_seconds
            ),
            classified_orphan_reap_scan_interval_seconds=(
                settings.classified_orphan_reap_scan_interval_seconds
            ),
            claude_base_gc_enabled=settings.claude_base_gc_enabled,
            claude_base_reap_scan_interval_seconds=(
                settings.claude_base_reap_scan_interval_seconds
            ),
            terminal_workspace_gc_enabled=settings.terminal_workspace_gc_enabled,
            terminal_workspace_gc_scan_interval_seconds=(
                settings.terminal_workspace_gc_scan_interval_seconds
            ),
            orphan_reconcile_max_per_scan=settings.orphan_reconcile_max_per_scan,
            orphan_reconcile_min_age_hours=settings.orphan_reconcile_min_age_hours,
            auto_cleanup_orphans=settings.auto_cleanup_orphans,
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


def _service_git_environment(
    host_home: Path,
    *,
    github_token: str | None = None,
    source_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Git/SSH environment for service-worker host repository operations.

    ``source_env`` is the process environment the Bitbucket/SSH wiring inspects for
    credentials and the agent socket. It defaults to ``os.environ`` — what the real
    worker sees inside the service container (Compose-forwarded vars). Callers that
    run from a *different* process context (e.g. ``awf profile doctor`` from the
    operator shell) pass the worker's forwarded env explicitly so the
    Bitbucket-conditional additions match what the worker would inject, rather than
    silently reading the caller shell.
    """

    resolved_source: Mapping[str, str] = os.environ if source_env is None else source_env
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
    # Bitbucket git-over-HTTPS auth, host-scoped to bitbucket.org so the GitHub
    # path above is byte-for-byte unchanged. Reads ``source_env`` (the live process
    # environment by default, where ``BitbucketClient.from_env`` also reads its
    # credentials) and never places the token in any returned env value — git
    # invokes the helper, which reads the secret from the environment on demand.
    apply_bitbucket_git_auth(env, resolved_source)
    ssh_command = ["ssh"]
    if ssh_auth_sock := resolved_source.get("SSH_AUTH_SOCK"):
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
