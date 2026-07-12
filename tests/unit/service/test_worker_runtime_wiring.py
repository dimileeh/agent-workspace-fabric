"""Local service worker runtime wiring tests (continued).

Split out of ``test_worker.py`` to keep each test module under the
first-party line-count guardrail.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from awf.common.config import Settings
from awf.profiles.models import WorkspaceProfile
from awf.runtime.driver import LocalRuntimeDriver
from awf.runtime.hosted_delegation import HostedDelegationConfigError
from awf.service import worker as worker_mod
from awf.service.config import resolve_service_settings
from tests.unit.service.test_worker import _in_process_merge_coordinator, _settings


@pytest.mark.unit
def test_build_worker_runtime_defaults_to_local_runtime_driver_without_changing_worker_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify build worker runtime defaults to local runtime driver without changing worker dependencies."""
    created: dict[str, Any] = {}

    class _Engine:
        """Test helper for Engine."""

        pass

    class _Runner:
        """Test helper for Runner."""

        pass

    class _LogStore:
        """Test helper for LogStore."""

        def __init__(self, *, root: Path, session_factory: object) -> None:
            """Test helper for  init  ."""
            created["log_store"] = self
            created["log_root"] = root
            created["log_session_factory"] = session_factory

    class _ValidationRunner:
        """Test helper for ValidationRunner."""

        def __init__(self, *, runner: object, artifacts_dir: Path, log_store: object) -> None:
            """Test helper for  init  ."""
            created["validation"] = self
            created["validation_runner"] = runner
            created["validation_artifacts_dir"] = artifacts_dir
            created["validation_log_store"] = log_store

    class _PullRequestCreator:
        """Test helper for PullRequestCreator."""

        def __init__(self, runner: object) -> None:
            """Test helper for  init  ."""
            created["pr_creator"] = self
            created["pr_creator_runner"] = runner

    class _BranchOpenPullRequestResolver:
        """Test helper for BranchOpenPullRequestResolver."""

        def __init__(self, runner: object) -> None:
            """Test helper for  init  ."""
            created["open_pr_resolver"] = self
            created["open_pr_resolver_runner"] = runner

    class _GitManager:
        """Test helper for GitManager."""

        def __init__(self, work_dir: Path, **kwargs: object) -> None:
            """Test helper for  init  ."""
            created["git"] = self
            created["git_work_dir"] = work_dir
            created["git_kwargs"] = kwargs

    class _ComposeManager:
        """Test helper for ComposeManager."""

        def __init__(self, *, work_dir: Path, template_path: Path) -> None:
            """Test helper for  init  ."""
            created["compose"] = self
            created["compose_work_dir"] = work_dir
            created["compose_template_path"] = template_path

    class _WorkspaceCleaner:
        """Test helper for WorkspaceCleaner."""

        def __init__(self, *, git: object, compose: object) -> None:
            """Test helper for  init  ."""
            created["runtime_cleaner"] = self
            created["cleaner_git"] = git
            created["cleaner_compose"] = compose

    class _LocalSecretLeaseMountResolver:
        """Test helper for LocalSecretLeaseMountResolver."""

        def __init__(self, **kwargs: object) -> None:
            """Test helper for  init  ."""
            created["secret_lease_resolver"] = self
            created["secret_lease_kwargs"] = kwargs

    class _ComposeStackLauncher:
        """Test helper for ComposeStackLauncher."""

        def __init__(self, **kwargs: object) -> None:
            """Test helper for  init  ."""
            created["stack_launcher"] = self
            created["stack_launcher_kwargs"] = kwargs

    class _Provisioner:
        """Test helper for Provisioner."""

        def __init__(self, **kwargs: object) -> None:
            """Test helper for  init  ."""
            created["provisioner"] = self
            created["provisioner_kwargs"] = kwargs

    class _WorkspaceExecutor:
        """Test helper for WorkspaceExecutor."""

        def __init__(self, **kwargs: object) -> None:
            """Test helper for  init  ."""
            created["executor"] = self
            created["executor_kwargs"] = kwargs

    class _ControlWorker:
        """Test helper for ControlWorker."""

        def __init__(self, **kwargs: object) -> None:
            """Test helper for  init  ."""
            created["worker"] = self
            created["worker_kwargs"] = kwargs

    engine = _Engine()
    session_factory = object()

    monkeypatch.setattr(worker_mod, "make_engine", lambda _url: engine)
    monkeypatch.setattr(worker_mod, "make_session_factory", lambda _engine: session_factory)
    monkeypatch.setattr(worker_mod, "AsyncioSubprocessRunner", _Runner)
    monkeypatch.setattr(worker_mod, "LogStore", _LogStore)
    monkeypatch.setattr(worker_mod, "ValidationRunner", _ValidationRunner)
    monkeypatch.setattr(worker_mod, "PullRequestCreator", _PullRequestCreator)
    monkeypatch.setattr(worker_mod, "BranchOpenPullRequestResolver", _BranchOpenPullRequestResolver)
    monkeypatch.setattr(worker_mod, "GitManager", _GitManager)
    monkeypatch.setattr(worker_mod, "ComposeManager", _ComposeManager)
    monkeypatch.setattr(worker_mod, "WorkspaceCleaner", _WorkspaceCleaner)
    monkeypatch.setattr(worker_mod, "LocalSecretLeaseMountResolver", _LocalSecretLeaseMountResolver)
    monkeypatch.setattr(worker_mod, "ComposeStackLauncher", _ComposeStackLauncher)
    monkeypatch.setattr(worker_mod, "Provisioner", _Provisioner)
    monkeypatch.setattr(worker_mod, "WorkspaceExecutor", _WorkspaceExecutor)
    monkeypatch.setattr(worker_mod, "ControlWorker", _ControlWorker)
    monkeypatch.setattr(
        worker_mod, "_companion_image_builder_for", lambda _settings, _compose: None
    )
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
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    runtime = worker_mod.build_worker_runtime(_settings(tmp_path))

    assert runtime.engine is engine
    assert runtime.worker is created["worker"]
    assert isinstance(runtime.runtime_driver, LocalRuntimeDriver)
    assert runtime.runtime_driver.provisioner is created["provisioner"]
    assert runtime.runtime_driver.executor is created["executor"]
    assert runtime.runtime_driver.cleaner is created["runtime_cleaner"]
    assert runtime.runtime_driver.validation_runner is created["validation"]
    assert runtime.runtime_driver.runtime_inspector.__class__ is worker_mod.RuntimeInspector
    assert runtime.runtime_driver.capabilities == ("workspace.execution.v1",)
    assert created["worker_kwargs"]["provisioner"] is created["provisioner"]
    assert created["worker_kwargs"]["executor"] is created["executor"]
    assert created["worker_kwargs"]["runtime_cleaner"] is created["runtime_cleaner"]
    assert created["worker_kwargs"]["open_pr_resolver"] is created["open_pr_resolver"]
    assert created["executor_kwargs"]["compose"] is created["compose"]
    assert created["executor_kwargs"]["validation"] is created["validation"]
    assert created["executor_kwargs"]["agent_runtime_executor"] is None
    assert created["provisioner_kwargs"]["service_diagnostics"] is created["compose"]
    assert created["cleaner_git"] is created["git"]
    assert created["cleaner_compose"] is created["compose"]


@pytest.mark.unit
def test_worker_hosted_delegation_config_is_bounded_and_fails_closed(tmp_path: Path) -> None:
    settings = dataclasses.replace(
        _settings(tmp_path),
        hosted_delegation_base_url="https://hosted.example.test/",
        hosted_delegation_bearer_token="secret-token",
        hosted_delegation_poll_interval_seconds=3.0,
    )

    config = worker_mod._hosted_delegation_config_for_worker(settings)

    assert config is not None
    assert config.base_url == "https://hosted.example.test"
    assert config.bearer_token == "secret-token"
    assert config.poll_interval_seconds == 3.0

    partial = dataclasses.replace(settings, hosted_delegation_bearer_token=None)
    with pytest.raises(HostedDelegationConfigError) as partial_excinfo:
        worker_mod._hosted_delegation_config_for_worker(partial)
    assert partial_excinfo.value.detail() == {
        "missing": ["AWF_HOSTED_DELEGATION_BEARER_TOKEN or AWF_HOSTED_DELEGATION_BEARER_TOKEN_ENV"],
    }

    invalid = dataclasses.replace(settings, hosted_delegation_base_url="not-a-url")
    with pytest.raises(HostedDelegationConfigError) as invalid_excinfo:
        worker_mod._hosted_delegation_config_for_worker(invalid)
    assert invalid_excinfo.value.detail() == {"missing": ["AWF_HOSTED_DELEGATION_BASE_URL"]}


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

    # The API request-time ``now`` anchor (a ``datetime``) is forwarded to the sweep as an epoch
    # float so the row-less orphan grace freezes at POST time instead of the worker's claim clock
    # (PRRT_kwDOSJAM6s6LCs9R).
    anchor = datetime(2026, 6, 14, 21, 0, tzinfo=UTC)
    asyncio.run(classified_reaper(enabled=True, row_less_only=True, limit=5, now=anchor))
    assert created["classified_sweep_kwargs"]["now"] == anchor.timestamp()
    # No anchor (the periodic backstop) leaves the sweep to default to its own clock (``None``).
    asyncio.run(classified_reaper(enabled=True, row_less_only=True, limit=5))
    assert created["classified_sweep_kwargs"]["now"] is None


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
    """Dispose the SQLAlchemy engine after ``run_worker`` completes ``run_forever``."""
    calls: list[str] = []

    class _Worker:
        async def run_forever(self) -> None:
            calls.append("run_forever")

        async def run_once(self) -> None:
            raise AssertionError("run_once should not be used")

        async def wait_for_execution_tasks(self) -> None:
            raise AssertionError("wait_for_execution_tasks should not be used")

    class _Engine:
        """Test helper for Engine."""

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
        """Test helper for Runner."""

        pass

    class _LogStore:
        """Test helper for LogStore."""

        def __init__(self, *, root: Path, session_factory: object) -> None:
            """Test helper for  init  ."""
            pass

    class _ValidationRunner:
        """Test helper for ValidationRunner."""

        def __init__(self, *, runner: object, artifacts_dir: Path, log_store: object) -> None:
            """Test helper for  init  ."""
            pass

    class _PullRequestCreator:
        """Test helper for PullRequestCreator."""

        def __init__(self, runner: object) -> None:
            """Test helper for  init  ."""
            pass

    class _BranchOpenPullRequestResolver:
        """Test helper for BranchOpenPullRequestResolver."""

        def __init__(self, runner: object) -> None:
            """Test helper for  init  ."""
            pass

    class _GitManager:
        """Test helper for GitManager."""

        def __init__(
            self,
            work_dir: Path,
            *,
            env: object | None = None,
            worktree_owner_uid: int | None = None,
            worktree_owner_gid: int | None = None,
        ) -> None:
            """Test helper for  init  ."""
            pass

    class _ComposeManager:
        """Test helper for ComposeManager."""

        def __init__(self, *, work_dir: Path, template_path: Path) -> None:
            """Test helper for  init  ."""
            pass

    class _LocalSecretLeaseMountResolver:
        """Test helper for LocalSecretLeaseMountResolver."""

        def __init__(self, *, host_home: Path, work_dir: Path, host_env: object) -> None:
            """Test helper for  init  ."""
            pass

    class _ComposeStackLauncher:
        """Test helper for ComposeStackLauncher."""

        def __init__(
            self,
            *,
            compose: object,
            agent_runtime_image: str,
            auth_mount_resolver: object,
            secret_lease_resolver: object,
            companion_image_builder: object = None,
        ) -> None:
            """Test helper for  init  ."""
            pass

    class _Provisioner:
        """Test helper for Provisioner."""

        def __init__(
            self,
            *,
            session_factory: object,
            git: object,
            stack_launcher: object,
            config: object,
            service_diagnostics: object = None,
        ) -> None:
            """Test helper for  init  ."""
            pass

    class _WorkspaceExecutor:
        """Test helper for WorkspaceExecutor."""

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
            agent_runtime_executor: object = None,
        ) -> None:
            """Test helper for  init  ."""
            created["executor_monitor_factory"] = pr_monitor_factory

    class _ControlWorker:
        """Test helper for ControlWorker."""

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
            """Test helper for  init  ."""
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
