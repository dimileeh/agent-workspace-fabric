"""Local service worker runtime wiring tests."""

from __future__ import annotations

import asyncio
import dataclasses
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from awf.common.audit import REDACTION_MARKER
from awf.common.config import Settings
from awf.common.git_auth import bitbucket_git_config_entries
from awf.node.companion_images import CompanionImageBuilder
from awf.profiles.models import ProfileMonitor, ProfileRuntime, ProfileService, WorkspaceProfile
from awf.runtime.merge_coordinator import InProcessMergeCoordinator
from awf.service import worker as worker_mod
from awf.service.config import ServiceSettings, resolve_service_settings


def _settings(
    tmp_path: Path,
    *,
    database_url: str | None = None,
    github_token: str | None = None,
    planning_max_iterations_default: int = 4,
    completed_workspace_retention_hours: float = 168.0,
    auto_cleanup_orphans: bool = False,
    classified_orphan_reap_scan_interval_seconds: float = 3600.0,
    claude_base_reap_scan_interval_seconds: float = 3600.0,
    orphan_reconcile_max_per_scan: int = 50,
    orphan_reconcile_min_age_hours: float = 168.0,
) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url=database_url or "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf",
        docker_host="unix:///var/run/docker.sock",
        agent_runtime_image="custom-agent-runtime:dev",
        work_dir=str((tmp_path / "awf-work").resolve()),
        host_home=str((tmp_path / "host-home").resolve()),
        api_token=None,
        github_token=github_token,
        worker_poll_interval_seconds=0.25,
        worker_max_concurrent_provisions=2,
        worker_max_concurrent_executions=4,
        agent_wall_timeout_seconds=111,
        agent_idle_timeout_seconds=22,
        planning_max_iterations_default=planning_max_iterations_default,
        completed_workspace_retention_hours=completed_workspace_retention_hours,
        auto_cleanup_orphans=auto_cleanup_orphans,
        classified_orphan_reap_scan_interval_seconds=(classified_orphan_reap_scan_interval_seconds),
        claude_base_reap_scan_interval_seconds=claude_base_reap_scan_interval_seconds,
        orphan_reconcile_max_per_scan=orphan_reconcile_max_per_scan,
        orphan_reconcile_min_age_hours=orphan_reconcile_min_age_hours,
        node_id="node-1",
    )


def _in_process_merge_coordinator(
    _database_url: str, *, engine: object
) -> InProcessMergeCoordinator:
    del engine
    return InProcessMergeCoordinator()


@pytest.mark.unit
def test_companion_image_builder_enabled_by_default(tmp_path: Path) -> None:
    """The worker constructs a companion image builder by default."""
    builder = worker_mod._companion_image_builder_for(_settings(tmp_path), object())  # type: ignore[arg-type]
    assert isinstance(builder, CompanionImageBuilder)


@pytest.mark.unit
def test_companion_image_builder_disabled_returns_none(tmp_path: Path) -> None:
    """The worker returns no companion image builder when caching is disabled."""
    settings = dataclasses.replace(_settings(tmp_path), companion_image_cache_enabled=False)
    assert worker_mod._companion_image_builder_for(settings, object()) is None  # type: ignore[arg-type]


@pytest.mark.unit
def test_build_worker_runtime_wires_executor_and_feature_monitor_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created: dict[str, Any] = {}

    class _Engine:
        pass

    class _Runner:
        pass

    class _LogStore:
        def __init__(self, *, root: Path, session_factory: object) -> None:
            created["log_root"] = root
            created["log_session_factory"] = session_factory

    class _ValidationRunner:
        def __init__(self, *, runner: object, artifacts_dir: Path, log_store: object) -> None:
            created["validation_runner"] = runner
            created["validation_artifacts_dir"] = artifacts_dir
            created["validation_log_store"] = log_store

    class _PullRequestCreator:
        def __init__(self, runner: object) -> None:
            created["pr_creator_runner"] = runner

    class _BranchOpenPullRequestResolver:
        def __init__(self, runner: object) -> None:
            created["open_pr_resolver_runner"] = runner

    class _GitManager:
        def __init__(
            self,
            work_dir: Path,
            *,
            env: object | None = None,
            worktree_owner_uid: int | None = None,
            worktree_owner_gid: int | None = None,
        ) -> None:
            created["git_work_dir"] = work_dir
            created["git_env"] = env
            created["git_worktree_owner_uid"] = worktree_owner_uid
            created["git_worktree_owner_gid"] = worktree_owner_gid

    class _ComposeManager:
        def __init__(self, *, work_dir: Path, template_path: Path) -> None:
            created["compose_work_dir"] = work_dir
            created["compose_template_path"] = template_path
            created["compose_instance"] = self

    class _LocalSecretLeaseMountResolver:
        def __init__(
            self,
            *,
            host_home: Path,
            work_dir: Path,
            host_env: object,
        ) -> None:
            created["secret_resolver_host_home"] = host_home
            created["secret_resolver_work_dir"] = work_dir
            created["secret_resolver_host_env"] = host_env

    class _ComposeStackLauncher:
        def __init__(
            self,
            *,
            compose: object,
            agent_runtime_image: str,
            auth_mount_resolver: object,
            secret_lease_resolver: object,
            companion_image_builder: object = None,
        ) -> None:
            created["stack_compose"] = compose
            created["stack_agent_runtime_image"] = agent_runtime_image
            created["stack_auth_mount_resolver"] = auth_mount_resolver
            created["stack_secret_lease_resolver"] = secret_lease_resolver
            created["stack_companion_image_builder"] = companion_image_builder

    class _Provisioner:
        def __init__(
            self,
            *,
            session_factory: object,
            git: object,
            stack_launcher: object,
            config: object,
            service_diagnostics: object = None,
        ) -> None:
            created["provisioner_session_factory"] = session_factory
            created["provisioner_git"] = git
            created["provisioner_stack_launcher"] = stack_launcher
            created["provisioner_config"] = config
            created["provisioner_service_diagnostics"] = service_diagnostics

    class _WorkspaceExecutor:
        def __init__(
            self,
            *,
            session_factory: object,
            runner: object,
            compose: object,
            validation: object,
            pr_creator: object,
            config: object,
            pr_monitor_factory: object,
            log_store: object,
            usage_sampler: object = None,
        ) -> None:
            created["executor"] = self
            created["executor_session_factory"] = session_factory
            created["executor_runner"] = runner
            created["executor_compose"] = compose
            created["executor_validation"] = validation
            created["executor_pr_creator"] = pr_creator
            created["executor_config"] = config
            created["executor_monitor_factory"] = pr_monitor_factory
            created["executor_log_store"] = log_store
            created["executor_usage_sampler"] = usage_sampler

    class _ControlWorker:
        def __init__(
            self,
            *,
            session_factory: object,
            provisioner: object,
            executor: object,
            runtime_cleaner: object,
            open_pr_resolver: object,
            orphan_dir_reconciler: object = None,
            classified_orphan_reaper: object = None,
            claude_base_reaper: object = None,
            terminal_gc_reaper: object = None,
            auth_overlay_work_dir: object = None,
            config: object,
        ) -> None:
            created["worker_session_factory"] = session_factory
            created["worker_provisioner"] = provisioner
            created["worker_executor"] = executor
            created["worker_runtime_cleaner"] = runtime_cleaner
            created["worker_open_pr_resolver"] = open_pr_resolver
            created["worker_orphan_dir_reconciler"] = orphan_dir_reconciler
            created["worker_classified_orphan_reaper"] = classified_orphan_reaper
            created["worker_auth_overlay_work_dir"] = auth_overlay_work_dir
            created["worker_config"] = config

    engine = _Engine()
    session_factory = object()

    monkeypatch.setattr(worker_mod, "make_engine", lambda _url: engine)
    monkeypatch.setattr(worker_mod, "make_session_factory", lambda _engine: session_factory)
    monkeypatch.setattr(worker_mod, "AsyncioSubprocessRunner", _Runner)
    monkeypatch.setattr(worker_mod, "LogStore", _LogStore)
    monkeypatch.setattr(worker_mod, "ValidationRunner", _ValidationRunner)
    monkeypatch.setattr(worker_mod, "PullRequestCreator", _PullRequestCreator)
    forge_client = object()

    def _fake_make_forge_client(forge: object, runner: object) -> object:
        # gh is now built lazily inside _pr_monitor_factory (not at build time)
        # via make_forge_client(resolved forge, runner) — record both.
        created["forge_client_forge"] = forge
        created["forge_client_runner"] = runner
        return forge_client

    monkeypatch.setattr(worker_mod, "make_forge_client", _fake_make_forge_client)
    monkeypatch.setattr(worker_mod, "BranchOpenPullRequestResolver", _BranchOpenPullRequestResolver)
    monkeypatch.setattr(worker_mod, "GitManager", _GitManager)
    monkeypatch.setattr(worker_mod, "ComposeManager", _ComposeManager)
    monkeypatch.setattr(worker_mod, "LocalSecretLeaseMountResolver", _LocalSecretLeaseMountResolver)
    monkeypatch.setattr(worker_mod, "ComposeStackLauncher", _ComposeStackLauncher)
    monkeypatch.setattr(worker_mod, "Provisioner", _Provisioner)
    monkeypatch.setattr(worker_mod, "WorkspaceExecutor", _WorkspaceExecutor)
    monkeypatch.setattr(worker_mod, "ControlWorker", _ControlWorker)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(
        worker_mod,
        "_apply_service_git_environment",
        lambda env: created.setdefault("applied_git_env", env),
    )
    monkeypatch.setattr(
        worker_mod,
        "_merge_coordinator_for_database_url",
        _in_process_merge_coordinator,
    )

    def _build_feature_monitor(**kwargs: object) -> object:
        created["feature_monitor_kwargs"] = kwargs
        return object()

    def _build_release_monitor(**kwargs: object) -> object:
        created["release_monitor_kwargs"] = kwargs
        return object()

    monkeypatch.setattr(worker_mod, "build_feature_pr_monitor", _build_feature_monitor)
    monkeypatch.setattr(worker_mod, "build_release_pr_monitor", _build_release_monitor)

    settings = _settings(tmp_path)
    runtime = worker_mod.build_worker_runtime(settings)

    work_dir = Path(settings.work_dir).resolve()
    assert runtime.engine is engine
    assert created["worker_executor"] is created["executor"]
    assert created["executor_runner"].__class__ is _Runner
    assert created["validation_runner"] is created["executor_runner"]
    assert created["pr_creator_runner"] is created["executor_runner"]
    assert created["open_pr_resolver_runner"] is created["executor_runner"]
    assert created["worker_open_pr_resolver"].__class__ is _BranchOpenPullRequestResolver
    git_env = created["git_env"]
    assert git_env["HOME"] == str(Path(settings.host_home).resolve())
    assert git_env["GIT_CONFIG_COUNT"] == "1"
    assert git_env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert git_env["GIT_CONFIG_VALUE_0"] == "*"
    assert created["git_worktree_owner_uid"] == 1000
    assert created["git_worktree_owner_gid"] == 1000
    assert created["applied_git_env"] == created["git_env"]
    assert created["executor_compose"] is created["stack_compose"]
    assert created["secret_resolver_host_home"] == Path(settings.host_home).resolve()
    assert created["secret_resolver_work_dir"] == work_dir
    assert created["secret_resolver_host_env"] is worker_mod.os.environ
    assert created["stack_auth_mount_resolver"].workspace_owner_uid == 1000
    assert created["stack_auth_mount_resolver"].workspace_owner_gid == 1000
    assert created["stack_secret_lease_resolver"].__class__ is _LocalSecretLeaseMountResolver
    assert isinstance(created["stack_companion_image_builder"], CompanionImageBuilder)
    assert created["executor_log_store"] is created["validation_log_store"]
    assert created["executor_usage_sampler"].__class__ is worker_mod.CcusageCollector
    assert created["executor_usage_sampler"]._runner is created["executor_runner"]
    assert created["executor_usage_sampler"]._work_dir == work_dir
    assert created["log_root"] == work_dir / "logs"
    assert created["validation_artifacts_dir"] == work_dir / "artifacts"
    assert created["executor_config"].worktrees_root == work_dir / "git" / "worktrees"
    assert created["executor_config"].compose_projects_root == work_dir / "compose"
    assert created["executor_config"].agent_wall_timeout_seconds == 111
    assert created["executor_config"].agent_idle_timeout_seconds == 22
    assert created["executor_config"].planning_max_iterations_default == 4
    assert created["worker_config"].poll_interval_seconds == 0.25
    assert created["worker_config"].max_concurrent_provisions == 2
    assert created["worker_config"].max_concurrent_executions == 4
    assert created["worker_config"].node_id == "node-1"
    assert callable(created["worker_orphan_dir_reconciler"])
    assert callable(created["worker_classified_orphan_reaper"])
    # The worker is wired with the work dir so it can unmount terminal overlays
    # in its CAP_SYS_ADMIN namespace before GC removes the auth dir (#374/#380).
    assert created["worker_auth_overlay_work_dir"] == work_dir
    assert created["worker_config"].auto_cleanup_orphans is False
    assert created["worker_config"].classified_orphan_reap_scan_interval_seconds == 3600.0
    assert created["worker_config"].orphan_reconcile_max_per_scan == 50
    # Issue #299: the provisioner receives the ComposeManager as its
    # service-startup diagnostics capturer so companion logs/healthcheck state
    # are captured into the SERVICE_STARTUP_FAILURE event before teardown.
    assert created["provisioner_service_diagnostics"] is created["compose_instance"]

    default_monitor = created["executor_monitor_factory"](
        object(),
        WorkspaceProfile(name="default"),
        SimpleNamespace(
            auto_merge=True,
            initial_review_grace_period_seconds=None,
            task_kind="feature_branch_pr",
            repo_url="https://github.com/o/r.git",
        ),
    )
    assert default_monitor is not None
    # gh is built lazily in the factory from the resolved forge (forge="auto"
    # on a default profile normalizes to github via concrete_forge_for_repo —
    # the github repo_url confirms the host) with the shared runner, and the
    # resulting ForgeClient flows into the monitor kwargs.
    assert created["forge_client_forge"] == "github"
    assert created["forge_client_runner"] is created["executor_runner"]
    assert created["feature_monitor_kwargs"]["gh"] is forge_client
    assert created["feature_monitor_kwargs"]["initial_review_grace_period_seconds"] == 900
    assert created["feature_monitor_kwargs"]["non_check_reviewer_settle_seconds"] == 900
    assert created["feature_monitor_kwargs"]["non_check_reviewer_logins"] == [
        "greptile-apps",
        "chatgpt-codex-connector",
    ]

    raw_database_password = "runtime-db-password"
    raw_database_url = f"postgresql+asyncpg://awf:{raw_database_password}@postgres:5432/awf"
    redacted_database_url = f"postgresql+asyncpg://{REDACTION_MARKER}@postgres:5432/awf"
    profile = WorkspaceProfile(
        name="custom",
        runtime=ProfileRuntime(environment={"AWF_TEST_DATABASE_URL": raw_database_url}),
        services=[ProfileService(name="postgres", image="postgres:16-alpine")],
        monitor=ProfileMonitor(
            initial_review_grace_period_seconds=321,
            non_check_reviewer_settle_seconds=45,
            non_check_reviewer_logins=["custom-reviewer"],
            require_ci=False,
        ),
    )
    monitor = created["executor_monitor_factory"](
        object(),
        profile,
        SimpleNamespace(
            auto_merge=True,
            initial_review_grace_period_seconds=None,
            task_kind="feature_branch_pr",
            repo_url="https://github.com/o/r.git",
        ),
    )

    assert monitor is not None
    assert created["feature_monitor_kwargs"]["initial_review_grace_period_seconds"] == 321
    assert created["feature_monitor_kwargs"]["non_check_reviewer_settle_seconds"] == 45
    assert created["feature_monitor_kwargs"]["non_check_reviewer_logins"] == ["custom-reviewer"]
    assert created["feature_monitor_kwargs"]["require_ci"] is False
    assert created["feature_monitor_kwargs"]["log_store"] is created["executor_log_store"]
    assert created["feature_monitor_kwargs"]["worktrees_root"] == work_dir / "git" / "worktrees"
    expected_runtime_context = created["feature_monitor_kwargs"]["workspace_runtime_context"]
    assert "Workspace runtime context" in expected_runtime_context
    assert "$AWF_TEST_DATABASE_URL" in expected_runtime_context
    assert redacted_database_url in expected_runtime_context
    assert raw_database_url not in expected_runtime_context
    assert raw_database_password not in expected_runtime_context
    assert "post_merge_target_reconciler" in created["feature_monitor_kwargs"]
    reconciler = created["feature_monitor_kwargs"]["post_merge_target_reconciler"]
    assert callable(reconciler)
    assert not isinstance(reconciler, types.MethodType), (
        "post_merge_target_reconciler must be the _post_merge_reconciler closure "
        "that wraps reconcile_and_refresh_stale_candidates, not the bare "
        "TargetBranchReconcileMonitor.reconcile bound method"
    )
    assert "merge_coordinator" in created["feature_monitor_kwargs"]
    assert isinstance(
        created["feature_monitor_kwargs"]["merge_coordinator"],
        InProcessMergeCoordinator,
    )

    manual_monitor = created["executor_monitor_factory"](
        object(),
        profile,
        SimpleNamespace(
            auto_merge=False,
            initial_review_grace_period_seconds=12.5,
            task_kind="feature_branch_pr",
            repo_url="https://github.com/o/r.git",
        ),
    )

    assert manual_monitor is not None
    assert created["release_monitor_kwargs"]["initial_review_grace_period_seconds"] == 12.5
    assert created["release_monitor_kwargs"]["non_check_reviewer_settle_seconds"] == 45
    assert created["release_monitor_kwargs"]["non_check_reviewer_logins"] == ["custom-reviewer"]
    assert created["release_monitor_kwargs"]["require_ci"] is False
    assert created["release_monitor_kwargs"]["log_store"] is created["executor_log_store"]
    assert created["release_monitor_kwargs"]["worktrees_root"] == work_dir / "git" / "worktrees"
    assert "post_merge_target_reconciler" in created["release_monitor_kwargs"]
    release_runtime_context = created["release_monitor_kwargs"]["workspace_runtime_context"]
    assert release_runtime_context == expected_runtime_context
    assert "Workspace runtime context" in release_runtime_context
    assert "$AWF_TEST_DATABASE_URL" in release_runtime_context
    assert redacted_database_url in release_runtime_context
    assert raw_database_url not in release_runtime_context
    assert raw_database_password not in release_runtime_context
    assert (
        created["release_monitor_kwargs"]["merge_coordinator"]
        is created["feature_monitor_kwargs"]["merge_coordinator"]
    )

    # Regression (PRRT_kwDOSJAM6s6EN6XO): a sync_release_pr workspace must get
    # the human-gated release monitor even when its persisted auto_merge is True,
    # so the never-auto-merge guarantee never hinges on that flag staying False.
    created.pop("feature_monitor_kwargs", None)
    created.pop("release_monitor_kwargs", None)
    release_sync_monitor = created["executor_monitor_factory"](
        object(),
        profile,
        SimpleNamespace(
            auto_merge=True,
            initial_review_grace_period_seconds=None,
            task_kind="sync_release_pr",
            repo_url="https://github.com/o/r.git",
        ),
    )
    assert release_sync_monitor is not None
    assert "feature_monitor_kwargs" not in created
    assert "release_monitor_kwargs" in created

    # Regression (issue:4596733729): the PR-monitor factory must mirror the
    # executor forge gate's URL-aware resolution (concrete_forge_for_repo), not
    # plain concrete_forge. A legacy/missing snapshot normalizes profile.forge to
    # "auto"; if such a workspace's repo_url is a Bitbucket URL the factory must
    # route to the bitbucket forge so make_forge_client raises
    # FORGE_NOT_SUPPORTED — never silently constructing a GitHubClient for a
    # Bitbucket repo when the factory runs before the executor gate (e.g. a
    # monitor rebuild on a pre-Phase-1 snapshot). With plain concrete_forge this
    # would resolve "github".
    created.pop("forge_client_forge", None)
    bitbucket_monitor = created["executor_monitor_factory"](
        object(),
        WorkspaceProfile(name="legacy-auto"),
        SimpleNamespace(
            auto_merge=True,
            initial_review_grace_period_seconds=None,
            task_kind="feature_branch_pr",
            repo_url="git@bitbucket.org:ws/repo.git",
        ),
    )
    assert bitbucket_monitor is not None
    assert created["forge_client_forge"] == "bitbucket"


@pytest.mark.unit
def test_post_merge_reconciler_passes_workspace_id_to_exclude_open_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression for PRRT_kwDOSJAM6s59-ihh.

    The PostMergeTargetReconciler Protocol now accepts workspace_id, and the
    worker closure must pass it through to reconcile_and_refresh_stale_candidates
    as exclude_workspace_ids so the just-merged candidate is not re-evaluated.
    """
    from collections.abc import Awaitable, Callable

    called_with: dict[str, object] = {}
    created: dict[str, Any] = {}

    class _FakeReconciler:
        def __init__(self, *, runner: object, work_dir: object) -> None:
            pass

        def checkout_path(self, *, repo_url: str, branch: str) -> Path:
            return tmp_path / "checkout"

        async def reconcile(
            self, *, repo_url: str, branch: str, dry_run: bool = False
        ) -> dict[str, object]:
            return {"status": "clean"}

    class _FakeTargetBranchStateProvider:
        def __init__(self, *, runner: object, checkout_path: Path) -> None:
            created["state_provider_runner"] = runner
            created["state_provider_checkout_path"] = checkout_path

        async def fetch(
            self,
            *,
            repo_url: str,
            branch: str,
            base_sha: str,
        ) -> dict[str, str]:
            called_with["target_state_fetch"] = {
                "repo_url": repo_url,
                "branch": branch,
                "base_sha": base_sha,
            }
            return {"base_sha": base_sha}

    async def _fake_reconcile_and_refresh(
        *,
        reconcile_fn: Callable[..., Awaitable[object]],
        repo_url: str,
        branch: str,
        session_factory: object,
        target_state_for_base_sha: Callable[[str], Awaitable[object]],
        exclude_workspace_ids: set[str] | None = None,
        dry_run: bool = False,
    ) -> object:
        called_with["exclude_workspace_ids"] = exclude_workspace_ids
        called_with["target_state"] = await target_state_for_base_sha("base-sha")
        return {"status": "clean"}

    class _FakeFeatureMonitor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created["reconciler"] = kwargs.get("post_merge_target_reconciler")

    class _FakeWorkspaceExecutor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created["pr_monitor_factory"] = kwargs["pr_monitor_factory"]

    monkeypatch.setattr(
        worker_mod, "reconcile_and_refresh_stale_candidates", _fake_reconcile_and_refresh
    )
    monkeypatch.setattr(worker_mod, "TargetBranchReconcileMonitor", _FakeReconciler)
    monkeypatch.setattr(
        worker_mod, "GitCheckoutTargetBranchStateProvider", _FakeTargetBranchStateProvider
    )
    monkeypatch.setattr(worker_mod, "build_feature_pr_monitor", _FakeFeatureMonitor)
    monkeypatch.setattr(worker_mod, "build_release_pr_monitor", _FakeFeatureMonitor)
    monkeypatch.setattr(worker_mod, "WorkspaceExecutor", _FakeWorkspaceExecutor)
    # silence other heavy constructors
    monkeypatch.setattr(worker_mod, "make_engine", lambda _url: object())
    monkeypatch.setattr(worker_mod, "make_session_factory", lambda _engine: object())
    monkeypatch.setattr(
        worker_mod,
        "AsyncioSubprocessRunner",
        type("_AnyInit", (), {"__init__": lambda _s, **_kw: None}),
    )  # type: ignore[type-var]
    monkeypatch.setattr(
        worker_mod, "LogStore", type("_AnyInit", (), {"__init__": lambda _s, **_kw: None})
    )  # type: ignore[type-var]
    monkeypatch.setattr(
        worker_mod, "ValidationRunner", type("_AnyInit", (), {"__init__": lambda _s, **_kw: None})
    )  # type: ignore[type-var]
    monkeypatch.setattr(
        worker_mod, "PullRequestCreator", type("_AnyInit", (), {"__init__": lambda _s, _r: None})
    )  # type: ignore[type-var]
    monkeypatch.setattr(
        worker_mod, "GitManager", type("_AnyInit", (), {"__init__": lambda _s, _p, **_kw: None})
    )  # type: ignore[type-var]
    monkeypatch.setattr(
        worker_mod, "ComposeManager", type("_AnyInit", (), {"__init__": lambda _s, **_kw: None})
    )  # type: ignore[type-var]
    monkeypatch.setattr(
        worker_mod,
        "ComposeStackLauncher",
        type("_AnyInit", (), {"__init__": lambda _s, **_kw: None}),
    )  # type: ignore[type-var]
    monkeypatch.setattr(
        worker_mod,
        "ServiceAuthMountResolver",
        type("_AnyInit", (), {"__init__": lambda _s, **_kw: None}),
    )  # type: ignore[type-var]
    monkeypatch.setattr(
        worker_mod, "Provisioner", type("_AnyInit", (), {"__init__": lambda _s, **_kw: None})
    )  # type: ignore[type-var]
    monkeypatch.setattr(
        worker_mod, "ControlWorker", type("_AnyInit", (), {"__init__": lambda _s, **_kw: None})
    )  # type: ignore[type-var]
    monkeypatch.setattr(
        worker_mod, "InProcessMergeCoordinator", type("_AnyInit", (), {"__init__": lambda _s: None})
    )  # type: ignore[type-var]
    monkeypatch.setattr(
        worker_mod,
        "_merge_coordinator_for_database_url",
        _in_process_merge_coordinator,
    )
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(worker_mod, "_apply_service_git_environment", lambda _env: None)

    settings = _settings(tmp_path)
    runtime = worker_mod.build_worker_runtime(settings)

    import asyncio

    created["pr_monitor_factory"](
        object(),
        WorkspaceProfile(name="default"),
        SimpleNamespace(
            auto_merge=True,
            initial_review_grace_period_seconds=None,
            task_kind="feature_branch_pr",
            repo_url="https://github.com/o/r.git",
        ),
    )
    reconciler = created["reconciler"]
    _ = asyncio.run(
        reconciler(
            repo_url="https://github.com/org/repo.git",
            branch="main",
            workspace_id="ws-123",
        )
    )
    assert called_with.get("exclude_workspace_ids") == {"ws-123"}
    assert called_with.get("target_state") == {"base_sha": "base-sha"}
    assert called_with.get("target_state_fetch") == {
        "repo_url": "https://github.com/org/repo.git",
        "branch": "main",
        "base_sha": "base-sha",
    }
    assert created["state_provider_checkout_path"] == tmp_path / "checkout"

    del runtime


@pytest.mark.unit
def test_build_worker_runtime_eagerly_uses_postgres_advisory_merge_coordinator_for_postgres(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created: dict[str, Any] = {}

    class _Engine:
        pass

    class _AnyInit:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class _WorkspaceExecutor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created["pr_monitor_factory"] = kwargs["pr_monitor_factory"]

    class _ControlWorker:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class _PostgresCoordinator:
        def __init__(self, engine: object) -> None:
            created["coordinator_engine"] = engine

    engine = _Engine()
    session_factory = object()

    monkeypatch.setattr(worker_mod, "make_engine", lambda _url: engine)
    monkeypatch.setattr(worker_mod, "make_session_factory", lambda _engine: session_factory)
    monkeypatch.setattr(worker_mod, "AsyncioSubprocessRunner", _AnyInit)
    monkeypatch.setattr(worker_mod, "LogStore", _AnyInit)
    monkeypatch.setattr(worker_mod, "ValidationRunner", _AnyInit)
    monkeypatch.setattr(worker_mod, "PullRequestCreator", _AnyInit)
    monkeypatch.setattr(worker_mod, "GitManager", _AnyInit)
    monkeypatch.setattr(worker_mod, "ComposeManager", _AnyInit)
    monkeypatch.setattr(worker_mod, "ServiceAuthMountResolver", _AnyInit)
    monkeypatch.setattr(worker_mod, "ComposeStackLauncher", _AnyInit)
    monkeypatch.setattr(worker_mod, "Provisioner", _AnyInit)
    monkeypatch.setattr(worker_mod, "WorkspaceExecutor", _WorkspaceExecutor)
    monkeypatch.setattr(worker_mod, "ControlWorker", _ControlWorker)
    monkeypatch.setattr(worker_mod, "PostgresAdvisoryMergeCoordinator", _PostgresCoordinator)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(worker_mod, "_apply_service_git_environment", lambda _env: None)

    def _build_feature_monitor(**kwargs: object) -> object:
        created["feature_monitor_kwargs"] = kwargs
        return object()

    monkeypatch.setattr(worker_mod, "build_feature_pr_monitor", _build_feature_monitor)
    monkeypatch.setattr(worker_mod, "build_release_pr_monitor", lambda **_kwargs: object())

    settings = _settings(
        tmp_path,
        database_url="postgresql+asyncpg://awf:pw@localhost:5432/awf",
    )

    worker_mod.build_worker_runtime(settings)
    assert created["coordinator_engine"] is engine

    created["pr_monitor_factory"](
        object(),
        WorkspaceProfile(name="default"),
        SimpleNamespace(
            auto_merge=True,
            initial_review_grace_period_seconds=None,
            task_kind="feature_branch_pr",
            repo_url="https://github.com/o/r.git",
        ),
    )

    assert isinstance(
        created["feature_monitor_kwargs"]["merge_coordinator"],
        _PostgresCoordinator,
    )


@pytest.mark.unit
@pytest.mark.parametrize("auto_cleanup_orphans", [False, True])
def test_build_worker_runtime_wires_orphan_dir_reconciler_execute_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    auto_cleanup_orphans: bool,
) -> None:
    """The reconciler closure runs report-only unless ``auto_cleanup_orphans``."""
    created: dict[str, Any] = {}

    class _Engine:
        pass

    class _AnyInit:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class _ControlWorker:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created["orphan_dir_reconciler"] = kwargs["orphan_dir_reconciler"]
            created["classified_orphan_reaper"] = kwargs["classified_orphan_reaper"]
            created["worker_config"] = kwargs["config"]

    session_factory = object()
    monkeypatch.setattr(worker_mod, "make_engine", lambda _url: _Engine())
    monkeypatch.setattr(worker_mod, "make_session_factory", lambda _engine: session_factory)
    for name in (
        "AsyncioSubprocessRunner",
        "LogStore",
        "ValidationRunner",
        "PullRequestCreator",
        "BranchOpenPullRequestResolver",
        "GitManager",
        "ComposeManager",
        "ServiceAuthMountResolver",
        "LocalSecretLeaseMountResolver",
        "ComposeStackLauncher",
        "Provisioner",
        "WorkspaceExecutor",
        "CcusageCollector",
    ):
        monkeypatch.setattr(worker_mod, name, _AnyInit)
    monkeypatch.setattr(worker_mod, "ControlWorker", _ControlWorker)
    monkeypatch.setattr(
        worker_mod, "_merge_coordinator_for_database_url", _in_process_merge_coordinator
    )
    monkeypatch.setattr(worker_mod, "_companion_image_builder_for", lambda *_a, **_k: None)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(worker_mod, "_apply_service_git_environment", lambda _env: None)
    monkeypatch.setattr(worker_mod, "build_default_compose_teardown", lambda _manager: object())
    classified_teardown = object()
    monkeypatch.setattr(
        worker_mod, "build_orphan_compose_teardown", lambda _manager: classified_teardown
    )

    async def _fake_reconcile(*args: object, **kwargs: object) -> object:
        created["reconcile_kwargs"] = kwargs
        return object()

    monkeypatch.setattr(worker_mod, "reconcile_orphaned_workspace_dirs", _fake_reconcile)

    async def _fake_sweep_classified_orphans(*args: object, **kwargs: object) -> object:
        """Capture classified-orphan sweep wiring from the worker runtime."""
        created["classified_sweep_args"] = args
        created["classified_sweep_kwargs"] = kwargs
        return object()

    monkeypatch.setattr(worker_mod, "sweep_classified_orphans", _fake_sweep_classified_orphans)

    settings = _settings(
        tmp_path,
        auto_cleanup_orphans=auto_cleanup_orphans,
        classified_orphan_reap_scan_interval_seconds=123.0,
        completed_workspace_retention_hours=72.0,
        orphan_reconcile_max_per_scan=9,
        orphan_reconcile_min_age_hours=4.0,
    )
    worker_mod.build_worker_runtime(settings)

    assert created["worker_config"].auto_cleanup_orphans is auto_cleanup_orphans
    assert created["worker_config"].classified_orphan_reap_scan_interval_seconds == 123.0
    assert created["worker_config"].orphan_reconcile_max_per_scan == 9
    reconciler = created["orphan_dir_reconciler"]
    assert callable(reconciler)
    asyncio.run(reconciler())
    assert created["reconcile_kwargs"]["execute"] is auto_cleanup_orphans
    assert created["reconcile_kwargs"]["limit"] == 9
    assert created["reconcile_kwargs"]["min_age_hours"] == 4.0

    classified_reaper = created["classified_orphan_reaper"]
    assert callable(classified_reaper)
    asyncio.run(classified_reaper())
    assert created["classified_sweep_args"][0] is session_factory
    assert created["classified_sweep_kwargs"]["work_dir"] == Path(settings.work_dir).resolve()
    assert created["classified_sweep_kwargs"]["docker_host"] == settings.docker_host
    assert created["classified_sweep_kwargs"]["compose_teardown"] is classified_teardown
    # No-arg call (the periodic backstop) resolves ``enabled`` to the flag default and reaps
    # terminal + missing (``row_less_only`` defaults to False) under the global flag.
    assert created["classified_sweep_kwargs"]["enabled"] is auto_cleanup_orphans
    assert created["classified_sweep_kwargs"]["min_age_hours"] == 4.0
    assert created["classified_sweep_kwargs"]["min_retention_hours"] == 72.0
    assert created["classified_sweep_kwargs"]["row_less_only"] is False
    # The periodic backstop passes no ``--limit``, so the sweep stays unbounded.
    assert created["classified_sweep_kwargs"]["limit"] is None

    # On-demand override (#637): forcing ``enabled=True`` for an operator-requested gc
    # run must pass through to the sweep regardless of the ``auto_cleanup_orphans`` flag.
    # ``row_less_only=True`` (PRRT_kwDOSJAM6s6LB30p) must thread through too so the additive
    # sweep reaps only no-DB-record orphans, never a scoped-out terminal workspace; the
    # operator's ``--limit`` must thread through as well so the additive sweep is bounded
    # oldest-first like the terminal reaper (PRRT_kwDOSJAM6s6LCCJZ).
    asyncio.run(classified_reaper(enabled=True, row_less_only=True, limit=5))
    assert created["classified_sweep_kwargs"]["enabled"] is True
    assert created["classified_sweep_kwargs"]["row_less_only"] is True
    assert created["classified_sweep_kwargs"]["limit"] == 5

    # The operator's ``--min-age-hours`` is forwarded as a safety FLOOR for the row-less
    # orphan grace (PRRT_kwDOSJAM6s6LCiLb): a longer requested window widens the grace so a
    # too-young-by-command orphan is never reaped behind the operator's longer scope, while a
    # shorter/absent one never shrinks the configured ``orphan_reconcile_min_age_hours`` (4.0)
    # mid-provision guard. A longer request wins:
    asyncio.run(classified_reaper(enabled=True, row_less_only=True, limit=5, min_age_hours=10.0))
    assert created["classified_sweep_kwargs"]["min_age_hours"] == 10.0
    # A shorter request is floored at the configured grace:
    asyncio.run(classified_reaper(enabled=True, row_less_only=True, limit=5, min_age_hours=1.0))
    assert created["classified_sweep_kwargs"]["min_age_hours"] == 4.0


@pytest.mark.unit
def test_build_worker_runtime_uses_local_service_node_id_instead_of_container_hostname(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created: dict[str, Any] = {}

    class _Engine:
        pass

    class _Runner:
        pass

    class _AnyInit:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class _Provisioner:
        def __init__(
            self,
            *,
            session_factory: object,
            git: object,
            stack_launcher: object,
            config: object,
            service_diagnostics: object = None,
        ) -> None:
            created["provisioner_config"] = config

    class _ControlWorker:
        def __init__(
            self,
            *,
            session_factory: object,
            provisioner: object,
            executor: object,
            runtime_cleaner: object,
            open_pr_resolver: object,
            orphan_dir_reconciler: object = None,
            classified_orphan_reaper: object = None,
            claude_base_reaper: object = None,
            terminal_gc_reaper: object = None,
            auth_overlay_work_dir: object = None,
            config: object,
        ) -> None:
            created["worker_config"] = config
            created["worker_runtime_cleaner"] = runtime_cleaner
            created["worker_open_pr_resolver"] = open_pr_resolver

    engine = _Engine()
    session_factory = object()

    monkeypatch.setattr(worker_mod, "make_engine", lambda _url: engine)
    monkeypatch.setattr(worker_mod, "make_session_factory", lambda _engine: session_factory)
    monkeypatch.setattr(worker_mod, "AsyncioSubprocessRunner", _Runner)
    monkeypatch.setattr(worker_mod, "LogStore", _AnyInit)
    monkeypatch.setattr(worker_mod, "ValidationRunner", _AnyInit)
    monkeypatch.setattr(worker_mod, "PullRequestCreator", _AnyInit)
    monkeypatch.setattr(worker_mod, "BranchOpenPullRequestResolver", _AnyInit)
    monkeypatch.setattr(worker_mod, "GitManager", _AnyInit)
    monkeypatch.setattr(worker_mod, "ComposeManager", _AnyInit)
    monkeypatch.setattr(worker_mod, "ServiceAuthMountResolver", _AnyInit)
    monkeypatch.setattr(worker_mod, "ComposeStackLauncher", _AnyInit)
    monkeypatch.setattr(worker_mod, "Provisioner", _Provisioner)
    monkeypatch.setattr(worker_mod, "WorkspaceExecutor", _AnyInit)
    monkeypatch.setattr(worker_mod, "ControlWorker", _ControlWorker)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(worker_mod, "_apply_service_git_environment", lambda _env: None)
    monkeypatch.setattr(
        worker_mod,
        "_merge_coordinator_for_database_url",
        _in_process_merge_coordinator,
    )

    settings = resolve_service_settings(
        Settings(
            _env_file=None,
            work_dir=str(tmp_path / "awf-work"),
            host_home=str(tmp_path / "host-home"),
            database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf",
        ),
        environ={"AWF_DATABASE_URL": "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"},
    )

    worker_mod.build_worker_runtime(settings)

    assert created["provisioner_config"].node_id == "local"
    assert created["worker_config"].node_id == "local"
    assert isinstance(created["worker_open_pr_resolver"], _AnyInit)


@pytest.mark.unit
def test_build_worker_runtime_defaults_unset_service_node_id_to_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created: dict[str, Any] = {}

    class _Engine:
        pass

    class _Runner:
        pass

    class _AnyInit:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class _Provisioner:
        def __init__(
            self,
            *,
            session_factory: object,
            git: object,
            stack_launcher: object,
            config: object,
            service_diagnostics: object = None,
        ) -> None:
            created["provisioner_config"] = config

    class _ControlWorker:
        def __init__(
            self,
            *,
            session_factory: object,
            provisioner: object,
            executor: object,
            runtime_cleaner: object,
            open_pr_resolver: object,
            orphan_dir_reconciler: object = None,
            classified_orphan_reaper: object = None,
            claude_base_reaper: object = None,
            terminal_gc_reaper: object = None,
            auth_overlay_work_dir: object = None,
            config: object,
        ) -> None:
            created["worker_config"] = config

    engine = _Engine()
    session_factory = object()

    monkeypatch.setattr(worker_mod, "make_engine", lambda _url: engine)
    monkeypatch.setattr(worker_mod, "make_session_factory", lambda _engine: session_factory)
    monkeypatch.setattr(worker_mod, "AsyncioSubprocessRunner", _Runner)
    monkeypatch.setattr(worker_mod, "LogStore", _AnyInit)
    monkeypatch.setattr(worker_mod, "ValidationRunner", _AnyInit)
    monkeypatch.setattr(worker_mod, "PullRequestCreator", _AnyInit)
    monkeypatch.setattr(worker_mod, "BranchOpenPullRequestResolver", _AnyInit)
    monkeypatch.setattr(worker_mod, "GitManager", _AnyInit)
    monkeypatch.setattr(worker_mod, "ComposeManager", _AnyInit)
    monkeypatch.setattr(worker_mod, "ServiceAuthMountResolver", _AnyInit)
    monkeypatch.setattr(worker_mod, "ComposeStackLauncher", _AnyInit)
    monkeypatch.setattr(worker_mod, "Provisioner", _Provisioner)
    monkeypatch.setattr(worker_mod, "WorkspaceExecutor", _AnyInit)
    monkeypatch.setattr(worker_mod, "ControlWorker", _ControlWorker)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(worker_mod, "_apply_service_git_environment", lambda _env: None)
    monkeypatch.setattr(
        worker_mod,
        "_merge_coordinator_for_database_url",
        _in_process_merge_coordinator,
    )

    settings = dataclasses.replace(_settings(tmp_path), node_id=None)

    worker_mod.build_worker_runtime(settings)

    assert created["provisioner_config"].node_id == "local"
    assert created["worker_config"].node_id == "local"


@pytest.mark.unit
async def test_run_worker_forever_disposes_engine_on_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class _Worker:
        async def run_forever(self) -> None:
            calls.append("run_forever")

        async def run_once(self) -> None:
            raise AssertionError("run_once should not be used")

        async def wait_for_execution_tasks(self) -> None:
            raise AssertionError("wait_for_execution_tasks should not be used")

    class _Engine:
        async def dispose(self) -> None:
            calls.append("dispose")

    monkeypatch.setattr(
        worker_mod,
        "build_worker_runtime",
        lambda _settings: SimpleNamespace(worker=_Worker(), engine=_Engine()),
    )

    await worker_mod.run_worker(_settings(tmp_path), once=False)

    assert calls == ["run_forever", "dispose"]


@pytest.mark.unit
def test_is_postgres_database_url_warns_on_parse_failure() -> None:
    with structlog.testing.capture_logs() as captured:
        assert worker_mod._is_postgres_database_url("not a url") is False

    assert any(
        event.get("event") == "worker.database_url_parse_failed"
        and event.get("log_level") == "warning"
        and event.get("merge_coordinator") == "in_process"
        for event in captured
    )


@pytest.mark.unit
def test_is_postgres_database_url_warns_on_postgres_backend_typo() -> None:
    with structlog.testing.capture_logs() as captured:
        assert worker_mod._is_postgres_database_url("postgresq://awf:secret@localhost/awf") is False

    assert any(
        event.get("event") == "worker.postgres_merge_coordinator_not_selected"
        and event.get("log_level") == "warning"
        and event.get("backend") == "postgresq"
        and event.get("merge_coordinator") == "in_process"
        for event in captured
    )


@pytest.mark.unit
def test_is_postgres_database_url_rejects_non_postgres_backend_without_warning() -> None:
    with structlog.testing.capture_logs() as captured:
        result = worker_mod._is_postgres_database_url("sqlite+aiosqlite:///tmp/awf.db")

    assert result is False
    assert not any(
        event.get("event") == "worker.postgres_merge_coordinator_not_selected" for event in captured
    )


@pytest.mark.unit
def test_service_git_environment_uses_mounted_host_home(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    ssh_dir = host_home / ".ssh"
    ssh_dir.mkdir(parents=True)
    (host_home / ".gitconfig").write_text("[user]\n  name = AWF\n")
    ssh_config = ssh_dir / "config"
    ssh_config.write_text("Host github.com\n  UseKeychain yes\n")
    known_hosts = ssh_dir / "known_hosts"
    known_hosts.write_text("github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA...\n")

    env = worker_mod._service_git_environment(host_home)

    assert env["HOME"] == str(host_home)
    assert env["GIT_CONFIG_GLOBAL"] == str(host_home / ".gitconfig")
    assert "IgnoreUnknown=UseKeychain" in env["GIT_SSH_COMMAND"]
    assert str(ssh_config) in env["GIT_SSH_COMMAND"]
    assert str(known_hosts) in env["GIT_SSH_COMMAND"]
    assert "StrictHostKeyChecking=accept-new" in env["GIT_SSH_COMMAND"]


@pytest.mark.unit
def test_service_git_environment_forwards_github_token_for_gh_cli(tmp_path: Path) -> None:
    env = worker_mod._service_git_environment(
        tmp_path / "host-home",
        github_token="ghp_service_token",
    )

    assert env["GH_TOKEN"] == "ghp_service_token"
    assert env["GITHUB_TOKEN"] == "ghp_service_token"


@pytest.mark.unit
def test_service_git_environment_marks_worker_managed_worktrees_safe(tmp_path: Path) -> None:
    env = worker_mod._service_git_environment(tmp_path / "host-home")

    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert env["GIT_CONFIG_VALUE_0"] == "*"


@pytest.mark.unit
def test_service_git_environment_configures_gh_credential_helper_for_git(
    tmp_path: Path,
) -> None:
    env = worker_mod._service_git_environment(
        tmp_path / "host-home",
        github_token="ghp_service_token",
    )

    count = int(env["GIT_CONFIG_COUNT"])
    entries = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"] for index in range(count)
    }

    assert entries["safe.directory"] == "*"
    assert entries["credential.https://github.com.helper"] == "!gh auth git-credential"
    assert entries["url.https://github.com/.insteadOf"] == "git@github.com:"
    assert all("ghp_service_token" not in value for value in entries.values())


@pytest.mark.unit
def test_service_git_environment_forwards_ssh_agent_socket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/host-services/ssh-auth.sock")

    env = worker_mod._service_git_environment(tmp_path / "host-home")

    assert env["SSH_AUTH_SOCK"] == "/run/host-services/ssh-auth.sock"
    assert "IdentityAgent=/run/host-services/ssh-auth.sock" in env["GIT_SSH_COMMAND"]


@pytest.mark.unit
def test_service_git_environment_wires_bitbucket_helper_without_leaking_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_token = "ATATT-service-token-do-not-render"
    monkeypatch.setenv("BITBUCKET_API_TOKEN", secret_token)
    monkeypatch.setenv("BITBUCKET_EMAIL", "agent@example.com")

    env = worker_mod._service_git_environment(
        tmp_path / "host-home",
        github_token="ghp_service_token",
    )

    count = int(env["GIT_CONFIG_COUNT"])
    entries = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"] for index in range(count)
    }
    # Bitbucket host-scoped helper is wired alongside the (unchanged) GitHub one.
    assert "credential.https://bitbucket.org.helper" in entries
    assert entries["credential.https://github.com.helper"] == "!gh auth git-credential"
    assert entries["url.https://github.com/.insteadOf"] == "git@github.com:"
    # SSH-form bitbucket remotes are rewritten to HTTPS so the token is used
    # (parity with the GitHub insteadOf rewrite above). ``insteadOf`` is
    # multi-valued: the scp-like ``git@bitbucket.org:`` form, the no-port
    # ``ssh://git@bitbucket.org/`` form, and the explicit-default-port
    # ``ssh://git@bitbucket.org:22/`` form (which the preflight accepts as
    # canonical) are all covered.
    bitbucket_insteadof = [
        env[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(count)
        if env[f"GIT_CONFIG_KEY_{index}"] == "url.https://bitbucket.org/.insteadOf"
    ]
    assert "git@bitbucket.org:" in bitbucket_insteadof
    assert "ssh://git@bitbucket.org/" in bitbucket_insteadof
    assert "ssh://git@bitbucket.org:22/" in bitbucket_insteadof
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # The Atlassian token never lands in any git env value.
    assert all(secret_token not in value for value in env.values())


@pytest.mark.unit
def test_service_git_environment_unchanged_without_bitbucket_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("BITBUCKET_API_TOKEN", raising=False)
    monkeypatch.delenv("BITBUCKET_EMAIL", raising=False)

    env = worker_mod._service_git_environment(
        tmp_path / "host-home",
        github_token="ghp_service_token",
    )

    # Pure regression: no bitbucket helper, no terminal-prompt override, and the
    # GitHub credential helper plus safe.directory entries are untouched.
    assert "GIT_TERMINAL_PROMPT" not in env
    count = int(env["GIT_CONFIG_COUNT"])
    entries = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"] for index in range(count)
    }
    # Compare against the exact bitbucket-scoped config keys rather than a substring
    # of the host (a bare-host substring check is an incomplete-URL-sanitization
    # pattern flagged by static analysis).
    assert {key for key, _ in bitbucket_git_config_entries()}.isdisjoint(entries)
    assert entries["credential.https://github.com.helper"] == "!gh auth git-credential"


@pytest.mark.unit
def test_service_git_environment_reads_bitbucket_and_ssh_from_source_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``source_env`` (not the caller os.environ) drives the Bitbucket/SSH wiring.

    The real worker reads its Compose-forwarded container env; callers that run
    from a different process context (``awf profile doctor``) pass that env via
    ``source_env`` so the Bitbucket-conditional additions match the worker. Here
    the creds and agent socket live ONLY in ``source_env`` and are absent from the
    caller os.environ, yet the Bitbucket helper, GIT_TERMINAL_PROMPT, and the
    SSH_AUTH_SOCK/GIT_SSH_COMMAND wiring are still emitted.
    """
    monkeypatch.delenv("BITBUCKET_API_TOKEN", raising=False)
    monkeypatch.delenv("BITBUCKET_EMAIL", raising=False)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    env = worker_mod._service_git_environment(
        tmp_path / "host-home",
        github_token="ghp_service_token",
        source_env={
            "BITBUCKET_API_TOKEN": "bb_token",
            "BITBUCKET_EMAIL": "dev@example.com",
            "SSH_AUTH_SOCK": "/run/host-services/ssh-auth.sock",
        },
    )

    assert env["GIT_TERMINAL_PROMPT"] == "0"
    count = int(env["GIT_CONFIG_COUNT"])
    entries = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"] for index in range(count)
    }
    assert "credential.https://bitbucket.org.helper" in entries
    assert env["SSH_AUTH_SOCK"] == "/run/host-services/ssh-auth.sock"
    assert "IdentityAgent=/run/host-services/ssh-auth.sock" in env["GIT_SSH_COMMAND"]


@pytest.mark.unit
def test_service_git_environment_source_env_overrides_caller_environ(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An explicit ``source_env`` fully replaces os.environ for Bitbucket detection.

    Bitbucket creds in the caller os.environ but absent from ``source_env`` must
    NOT add the Bitbucket helper: the worker context modelled by ``source_env``
    lacks them, so the doctor must not over-add keys the worker would not inject.
    """
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "caller_token")
    monkeypatch.setenv("BITBUCKET_EMAIL", "caller@example.com")

    env = worker_mod._service_git_environment(
        tmp_path / "host-home",
        github_token="ghp_service_token",
        source_env={},
    )

    assert "GIT_TERMINAL_PROMPT" not in env
    count = int(env["GIT_CONFIG_COUNT"])
    entries = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"] for index in range(count)
    }
    # Compare against the exact bitbucket-scoped config keys rather than a substring
    # of the host (a bare-host substring check is an incomplete-URL-sanitization
    # pattern flagged by static analysis).
    assert {key for key, _ in bitbucket_git_config_entries()}.isdisjoint(entries)


class _RecordingForgeClient:
    """Minimal ForgeClient stub that records aclose() calls."""

    def __init__(self) -> None:
        self.aclose_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1


@pytest.mark.unit
def test_release_forge_client_after_build_error_runs_without_event_loop() -> None:
    """Outside a running loop the helper drives aclose() to completion."""
    gh = _RecordingForgeClient()

    worker_mod._release_forge_client_after_build_error(gh)  # type: ignore[arg-type]

    assert gh.aclose_calls == 1


@pytest.mark.unit
def test_release_forge_client_after_build_error_schedules_on_running_loop() -> None:
    """Inside a running loop the helper schedules a tracked close task."""

    async def _drive() -> _RecordingForgeClient:
        gh = _RecordingForgeClient()
        worker_mod._release_forge_client_after_build_error(gh)  # type: ignore[arg-type]
        # Scheduled, not yet run: yield control so the tracked task can finish.
        assert worker_mod._PENDING_FORGE_CLIENT_CLOSERS
        await asyncio.sleep(0)
        return gh

    gh = asyncio.run(_drive())

    assert gh.aclose_calls == 1
    # The done-callback drains the tracking set so it never grows unbounded.
    assert not worker_mod._PENDING_FORGE_CLIENT_CLOSERS


def _stub_worker_runtime_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    created: dict[str, Any],
    *,
    forge_client: object,
    build_feature: Any,
    build_release: Any,
) -> None:
    """Replace build_worker_runtime's heavy collaborators with light stubs."""

    class _Runner:
        pass

    class _LogStore:
        def __init__(self, *, root: Path, session_factory: object) -> None:
            pass

    class _ValidationRunner:
        def __init__(self, *, runner: object, artifacts_dir: Path, log_store: object) -> None:
            pass

    class _PullRequestCreator:
        def __init__(self, runner: object) -> None:
            pass

    class _BranchOpenPullRequestResolver:
        def __init__(self, runner: object) -> None:
            pass

    class _GitManager:
        def __init__(
            self,
            work_dir: Path,
            *,
            env: object | None = None,
            worktree_owner_uid: int | None = None,
            worktree_owner_gid: int | None = None,
        ) -> None:
            pass

    class _ComposeManager:
        def __init__(self, *, work_dir: Path, template_path: Path) -> None:
            pass

    class _LocalSecretLeaseMountResolver:
        def __init__(self, *, host_home: Path, work_dir: Path, host_env: object) -> None:
            pass

    class _ComposeStackLauncher:
        def __init__(
            self,
            *,
            compose: object,
            agent_runtime_image: str,
            auth_mount_resolver: object,
            secret_lease_resolver: object,
            companion_image_builder: object = None,
        ) -> None:
            pass

    class _Provisioner:
        def __init__(
            self,
            *,
            session_factory: object,
            git: object,
            stack_launcher: object,
            config: object,
            service_diagnostics: object = None,
        ) -> None:
            pass

    class _WorkspaceExecutor:
        def __init__(
            self,
            *,
            session_factory: object,
            runner: object,
            compose: object,
            validation: object,
            pr_creator: object,
            config: object,
            pr_monitor_factory: object,
            log_store: object,
            usage_sampler: object = None,
        ) -> None:
            created["executor_monitor_factory"] = pr_monitor_factory

    class _ControlWorker:
        def __init__(
            self,
            *,
            session_factory: object,
            provisioner: object,
            executor: object,
            runtime_cleaner: object,
            open_pr_resolver: object,
            orphan_dir_reconciler: object = None,
            classified_orphan_reaper: object = None,
            claude_base_reaper: object = None,
            terminal_gc_reaper: object = None,
            auth_overlay_work_dir: object = None,
            config: object,
        ) -> None:
            pass

    monkeypatch.setattr(worker_mod, "make_engine", lambda _url: object())
    monkeypatch.setattr(worker_mod, "make_session_factory", lambda _engine: object())
    monkeypatch.setattr(worker_mod, "AsyncioSubprocessRunner", _Runner)
    monkeypatch.setattr(worker_mod, "LogStore", _LogStore)
    monkeypatch.setattr(worker_mod, "ValidationRunner", _ValidationRunner)
    monkeypatch.setattr(worker_mod, "PullRequestCreator", _PullRequestCreator)
    monkeypatch.setattr(worker_mod, "make_forge_client", lambda _forge, _runner: forge_client)
    monkeypatch.setattr(worker_mod, "BranchOpenPullRequestResolver", _BranchOpenPullRequestResolver)
    monkeypatch.setattr(worker_mod, "GitManager", _GitManager)
    monkeypatch.setattr(worker_mod, "ComposeManager", _ComposeManager)
    monkeypatch.setattr(worker_mod, "LocalSecretLeaseMountResolver", _LocalSecretLeaseMountResolver)
    monkeypatch.setattr(worker_mod, "ComposeStackLauncher", _ComposeStackLauncher)
    monkeypatch.setattr(worker_mod, "Provisioner", _Provisioner)
    monkeypatch.setattr(worker_mod, "WorkspaceExecutor", _WorkspaceExecutor)
    monkeypatch.setattr(worker_mod, "ControlWorker", _ControlWorker)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(worker_mod, "_apply_service_git_environment", lambda _env: None)
    monkeypatch.setattr(
        worker_mod, "_merge_coordinator_for_database_url", _in_process_merge_coordinator
    )
    monkeypatch.setattr(worker_mod, "build_feature_pr_monitor", build_feature)
    monkeypatch.setattr(worker_mod, "build_release_pr_monitor", build_release)


@pytest.mark.unit
def test_pr_monitor_factory_closes_forge_client_when_builder_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression PRRT_kwDOSJAM6s6HnRTp: a monitor-build failure must not leak
    the forge client the factory built for the (never-run) monitor."""
    created: dict[str, Any] = {}
    forge_client = _RecordingForgeClient()

    def _raising_builder(**_kwargs: object) -> object:
        raise RuntimeError("builder boom")

    _stub_worker_runtime_dependencies(
        monkeypatch,
        created,
        forge_client=forge_client,
        build_feature=_raising_builder,
        build_release=_raising_builder,
    )

    worker_mod.build_worker_runtime(_settings(tmp_path))
    factory = created["executor_monitor_factory"]

    with pytest.raises(RuntimeError, match="builder boom"):
        factory(
            object(),
            WorkspaceProfile(name="default"),
            SimpleNamespace(
                auto_merge=True,
                initial_review_grace_period_seconds=None,
                task_kind="feature_branch_pr",
                repo_url="https://github.com/o/r.git",
            ),
        )

    assert forge_client.aclose_calls == 1
