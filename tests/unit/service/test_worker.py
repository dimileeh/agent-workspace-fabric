"""Local service worker runtime wiring tests."""

from __future__ import annotations

import dataclasses
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.common.audit import REDACTION_MARKER
from awf.node.companion_images import CompanionImageBuilder
from awf.profiles.models import ProfileMonitor, ProfileRuntime, ProfileService, WorkspaceProfile
from awf.runtime.merge_coordinator import InProcessMergeCoordinator
from awf.service import worker as worker_mod
from awf.service.config import ServiceSettings


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
    """Verify build worker runtime wires executor and feature monitor factory."""
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
    # Default profile omits the #662 knob -> MonitorConfig 600s default.
    assert created["feature_monitor_kwargs"]["awaiting_required_checks_grace_seconds"] == 600

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
            awaiting_required_checks_grace_seconds=250,
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
    assert created["feature_monitor_kwargs"]["awaiting_required_checks_grace_seconds"] == 250
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
    assert created["release_monitor_kwargs"]["awaiting_required_checks_grace_seconds"] == 250
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

    # The documented ``<= 0`` disable escape hatch must flow from the profile
    # monitor policy to the monitor kwargs (#662).
    created.pop("feature_monitor_kwargs", None)
    created.pop("release_monitor_kwargs", None)
    disable_profile = WorkspaceProfile(
        name="disable-grace",
        monitor=ProfileMonitor(awaiting_required_checks_grace_seconds=0),
    )
    created["executor_monitor_factory"](
        object(),
        disable_profile,
        SimpleNamespace(
            auto_merge=True,
            initial_review_grace_period_seconds=None,
            task_kind="feature_branch_pr",
            repo_url="https://github.com/o/r.git",
        ),
    )
    assert created["feature_monitor_kwargs"]["awaiting_required_checks_grace_seconds"] == 0
