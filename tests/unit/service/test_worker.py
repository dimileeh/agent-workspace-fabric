"""Local service worker runtime wiring tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.profiles.models import ProfileMonitor, WorkspaceProfile
from awf.service import worker as worker_mod
from awf.service.config import ServiceSettings


def _settings(tmp_path: Path) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'awf.db'}",
        docker_host="unix:///var/run/docker.sock",
        agent_runtime_image="custom-agent-runtime:dev",
        work_dir=str((tmp_path / "awf-work").resolve()),
        host_home=str((tmp_path / "host-home").resolve()),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.25,
        worker_max_concurrent_provisions=2,
        worker_max_concurrent_executions=4,
        node_id="node-1",
    )


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

    class _GitHubClient:
        def __init__(self, runner: object) -> None:
            created["github_runner"] = runner

    class _GitManager:
        def __init__(self, work_dir: Path, *, env: object | None = None) -> None:
            created["git_work_dir"] = work_dir
            created["git_env"] = env

    class _ComposeManager:
        def __init__(self, *, work_dir: Path, template_path: Path) -> None:
            created["compose_work_dir"] = work_dir
            created["compose_template_path"] = template_path

    class _ComposeStackLauncher:
        def __init__(
            self,
            *,
            compose: object,
            agent_runtime_image: str,
            auth_mount_resolver: object,
        ) -> None:
            created["stack_compose"] = compose
            created["stack_agent_runtime_image"] = agent_runtime_image
            created["stack_auth_mount_resolver"] = auth_mount_resolver

    class _Provisioner:
        def __init__(
            self,
            *,
            session_factory: object,
            git: object,
            stack_launcher: object,
            config: object,
        ) -> None:
            created["provisioner_session_factory"] = session_factory
            created["provisioner_git"] = git
            created["provisioner_stack_launcher"] = stack_launcher
            created["provisioner_config"] = config

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

    class _ControlWorker:
        def __init__(
            self,
            *,
            session_factory: object,
            provisioner: object,
            executor: object,
            config: object,
        ) -> None:
            created["worker_session_factory"] = session_factory
            created["worker_provisioner"] = provisioner
            created["worker_executor"] = executor
            created["worker_config"] = config

    engine = _Engine()
    session_factory = object()

    monkeypatch.setattr(worker_mod, "make_engine", lambda _url: engine)
    monkeypatch.setattr(worker_mod, "make_session_factory", lambda _engine: session_factory)
    monkeypatch.setattr(worker_mod, "AsyncioSubprocessRunner", _Runner)
    monkeypatch.setattr(worker_mod, "LogStore", _LogStore)
    monkeypatch.setattr(worker_mod, "ValidationRunner", _ValidationRunner)
    monkeypatch.setattr(worker_mod, "PullRequestCreator", _PullRequestCreator)
    monkeypatch.setattr(worker_mod, "GitHubClient", _GitHubClient)
    monkeypatch.setattr(worker_mod, "GitManager", _GitManager)
    monkeypatch.setattr(worker_mod, "ComposeManager", _ComposeManager)
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
    assert created["github_runner"] is created["executor_runner"]
    assert created["git_env"] == {"HOME": str(Path(settings.host_home).resolve())}
    assert created["applied_git_env"] == created["git_env"]
    assert created["executor_compose"] is created["stack_compose"]
    assert created["executor_log_store"] is created["validation_log_store"]
    assert created["log_root"] == work_dir / "logs"
    assert created["validation_artifacts_dir"] == work_dir / "artifacts"
    assert created["executor_config"].worktrees_root == work_dir / "git" / "worktrees"
    assert created["executor_config"].compose_projects_root == work_dir / "compose"
    assert created["worker_config"].poll_interval_seconds == 0.25
    assert created["worker_config"].max_concurrent_provisions == 2
    assert created["worker_config"].max_concurrent_executions == 4

    default_monitor = created["executor_monitor_factory"](
        object(),
        WorkspaceProfile(name="default"),
        SimpleNamespace(auto_merge=True, initial_review_grace_period_seconds=None),
    )
    assert default_monitor is not None
    assert created["feature_monitor_kwargs"]["initial_review_grace_period_seconds"] == 900

    profile = WorkspaceProfile(
        name="custom",
        monitor=ProfileMonitor(initial_review_grace_period_seconds=321),
    )
    monitor = created["executor_monitor_factory"](
        object(),
        profile,
        SimpleNamespace(auto_merge=True, initial_review_grace_period_seconds=None),
    )

    assert monitor is not None
    assert created["feature_monitor_kwargs"]["initial_review_grace_period_seconds"] == 321
    assert created["feature_monitor_kwargs"]["log_store"] is created["executor_log_store"]
    assert created["feature_monitor_kwargs"]["worktrees_root"] == work_dir / "git" / "worktrees"

    manual_monitor = created["executor_monitor_factory"](
        object(),
        profile,
        SimpleNamespace(auto_merge=False, initial_review_grace_period_seconds=12.5),
    )

    assert manual_monitor is not None
    assert created["release_monitor_kwargs"]["initial_review_grace_period_seconds"] == 12.5
    assert created["release_monitor_kwargs"]["log_store"] is created["executor_log_store"]
    assert created["release_monitor_kwargs"]["worktrees_root"] == work_dir / "git" / "worktrees"


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
def test_service_git_environment_forwards_ssh_agent_socket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/host-services/ssh-auth.sock")

    env = worker_mod._service_git_environment(tmp_path / "host-home")

    assert env["SSH_AUTH_SOCK"] == "/run/host-services/ssh-auth.sock"
    assert "IdentityAgent=/run/host-services/ssh-auth.sock" in env["GIT_SSH_COMMAND"]
