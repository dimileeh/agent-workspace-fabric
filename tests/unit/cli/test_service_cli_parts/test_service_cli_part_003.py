"""Service-oriented CLI and local service runtime tests."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from awf.cli.main import app
from awf.service.gc import WorkspaceGCComposeTeardownResult, WorkspaceGCWorktreeRemoveResult

_runner = CliRunner()
_POSTGRES_TEST_URL = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
_DOCKER_COMPOSE_CALLER_ENV_KEYS = frozenset(
    {
        "AWF_DOCKER_HOST",
        "COMPOSE_PROFILES",
        "COMPOSE_PROJECT_NAME",
        "DOCKER_API_VERSION",
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
    }
)


def _combined_output(result: Any) -> str:
    return f"{result.stdout}{getattr(result, 'stderr', '')}"


class _FakeDiskUsage:
    total = 20 * 1024 * 1024 * 1024
    used = 1 * 1024 * 1024 * 1024
    free = 19 * 1024 * 1024 * 1024


def _ok_disk_usage(_path: Path) -> _FakeDiskUsage:
    return _FakeDiskUsage()


def _write_gc_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _clear_docker_compose_caller_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.upper() in _DOCKER_COMPOSE_CALLER_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)


def _create_gc_cli_workspace(
    *,
    db_url: str,
    status: str,
    updated_at: datetime,
    pr: bool = False,
    compose_file_path: str | None = None,
) -> str:
    async def _setup() -> str:
        from awf.db.repositories import WorkspaceRepository
        from awf.db.session import make_engine, make_session_factory

        engine = make_engine(db_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as session:
                workspace = await WorkspaceRepository(session).create(
                    repo_url="git@github.com:example/repo.git",
                    branch_base="development",
                    task_title="gc cli",
                    task_prompt="p",
                    agent="codex",
                    test_commands=[],
                )
                workspace.status = status
                workspace.updated_at = updated_at
                workspace.compose_file_path = compose_file_path
                if pr:
                    workspace.pr_url = "https://github.com/example/repo/pull/321"
                    workspace.pr_number = 321
                    workspace.pr_merge_sha = "d" * 40
                await session.commit()
                return workspace.id
        finally:
            await engine.dispose()

    return asyncio.run(_setup())


def _mock_compose_teardown_succeeded(_candidate: object) -> WorkspaceGCComposeTeardownResult:
    return WorkspaceGCComposeTeardownResult(
        status="succeeded",
        reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
    )


async def _mock_worktree_remover_succeeded(
    _candidate: object, **_kwargs: object
) -> WorkspaceGCWorktreeRemoveResult:
    return WorkspaceGCWorktreeRemoveResult(
        status="succeeded",
        reason_code="WORKTREE_REMOVE_SUCCEEDED",
    )


def _mock_compose_teardown_failed(_candidate: object) -> WorkspaceGCComposeTeardownResult:
    return WorkspaceGCComposeTeardownResult(
        status="failed",
        reason_code="DOCKER_COMPOSE_DOWN_FAILED",
        error="compose teardown failed",
    )


@pytest.fixture
def _default_local_service_compose_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod

    compose_file = tmp_path / "docker" / "compose" / "local-service.yml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("services: {}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    _clear_docker_compose_caller_env(monkeypatch)


def _write_non_source_compose_env(tmp_path: Path, contents: str) -> Path:
    profile_dir = tmp_path / ".awf"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "workspace.yml").write_text("version: 1\n", encoding="utf-8")
    compose_env = tmp_path / "docker" / "compose" / ".env"
    compose_env.parent.mkdir(parents=True)
    (compose_env.parent / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    compose_env.write_text(contents, encoding="utf-8")
    return compose_env


@pytest.mark.unit
def test_service_doctor_bundle_resolves_existing_root_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import support_bundle as bundle_mod

    workspace_root = tmp_path / "workspace"
    compose = workspace_root / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (workspace_root / "compose.yaml").write_text(
        "include:\n  - ./docker/compose/local-service.yml\n",
        encoding="utf-8",
    )
    root_env = workspace_root / ".env"
    database_url = "postgresql+asyncpg://awf:root-secret@root-db:5432/awf"
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    api_base_url = "http://root-api:8123"
    root_env.write_text(
        "\n".join(
            [
                f"AWF_DATABASE_URL={database_url}",
                f"AWF_DOCKER_HOST={docker_host}",
                f"AWF_API_BASE_URL={api_base_url}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    project_subdir = workspace_root / "project"
    project_subdir.mkdir()
    monkeypatch.chdir(project_subdir)
    for key in ("AWF_DATABASE_URL", "AWF_DOCKER_HOST", "AWF_API_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: workspace_root)
    captured: dict[str, object] = {}

    async def _collect_support_bundle(settings: object, **kwargs: object) -> dict[str, object]:
        captured["settings"] = settings
        captured.update(kwargs)
        return {
            "generated_at": "2025-01-01T00:00:00+00:00",
            "version": "0.1.0",
            "service_status": {"status": "ok"},
            "doctor_report": {"status": "ok"},
            "provider_readiness_summary": {"status": "ok"},
            "orphan_cleanup_posture": {},
            "recent_failure_summary": {},
            "config_fingerprint": {},
            "log_pointers": [],
            "issue_template_pointer": ".github/ISSUE_TEMPLATE/bug_report.yml",
        }

    monkeypatch.setattr(bundle_mod, "collect_support_bundle", _collect_support_bundle)
    monkeypatch.setattr(
        bundle_mod, "write_support_bundle", lambda _bundle: tmp_path / "bundle.json"
    )

    result = _runner.invoke(app, ["service", "doctor", "--bundle"])

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert settings.database_url == database_url
    assert settings.docker_host == docker_host
    assert settings.api_base_url == api_base_url
    provider_environ = captured["provider_environ"]
    assert provider_environ["AWF_DATABASE_URL"] == database_url
    assert provider_environ["AWF_DOCKER_HOST"] == docker_host
    assert provider_environ["AWF_API_BASE_URL"] == api_base_url
    assert captured["environ"] is provider_environ
    assert captured["compose_file"] == workspace_root / "compose.yaml"
    assert captured["compose_env_file"] == root_env


@pytest.mark.unit
def test_service_doctor_json_output_is_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: object())
    report = SimpleNamespace(
        status="ok",
        to_dict=lambda: {
            "service": "awf",
            "status": "ok",
            "summary": {"ok": 1, "warn": 0, "fail": 0},
            "diagnostics": [],
        },
    )

    async def _collect(_settings: object, **_kwargs: object) -> object:
        return report

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect)

    result = _runner.invoke(app, ["service", "doctor", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == report.to_dict()


@pytest.mark.unit
def test_service_doctor_failing_diagnostics_exit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: object())
    report = SimpleNamespace(
        status="fail",
        to_dict=lambda: {
            "service": "awf",
            "status": "fail",
            "summary": {"ok": 0, "warn": 0, "fail": 1},
            "diagnostics": [
                {
                    "id": "docker",
                    "label": "Docker",
                    "status": "fail",
                    "reason": "DOCKER_DAEMON_UNREACHABLE",
                    "message": "Docker is installed but the daemon is not reachable.",
                    "action": "Start Docker Desktop or verify AWF_DOCKER_HOST.",
                    "source": "checks.docker",
                    "metadata": {},
                }
            ],
        },
    )

    async def _collect(_settings: object, **_kwargs: object) -> object:
        return report

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect)
    monkeypatch.setattr(
        doctor_mod,
        "render_doctor_pretty",
        lambda _report: (
            "AWF doctor: fail\n"
            "[fail] Docker: Docker is installed but the daemon is not reachable.\n"
            "       reason: DOCKER_DAEMON_UNREACHABLE\n"
            "       action: Start Docker Desktop or verify AWF_DOCKER_HOST.\n"
        ),
    )

    result = _runner.invoke(app, ["service", "doctor"])

    assert result.exit_code == 1
    assert "DOCKER_DAEMON_UNREACHABLE" in result.stdout
    assert "action: Start Docker Desktop" in result.stdout


@pytest.mark.unit
def test_service_doctor_preserves_typer_exit_from_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: object())

    async def _collect(_settings: object, **_kwargs: object) -> object:
        raise typer.Exit(code=7)

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect)

    result = _runner.invoke(app, ["service", "doctor"])

    assert result.exit_code == 7
    assert "could not collect AWF doctor diagnostics" not in _combined_output(result)


@pytest.mark.unit
def test_service_doctor_does_not_mask_unexpected_collection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: object())

    async def _collect(_settings: object, **_kwargs: object) -> object:
        raise RuntimeError("diagnostic collector exploded")

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect)

    result = _runner.invoke(app, ["service", "doctor"])

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert "could not collect AWF doctor diagnostics" not in _combined_output(result)


@pytest.mark.unit
def test_service_doctor_provider_options_are_validated_and_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    captured: dict[str, object] = {}
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: object())
    report = SimpleNamespace(
        status="ok",
        to_dict=lambda: {
            "service": "awf",
            "status": "ok",
            "summary": {"ok": 1, "warn": 0, "fail": 0},
            "diagnostics": [],
        },
    )

    async def _collect(_settings: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return report

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect)
    monkeypatch.setattr(doctor_mod, "render_doctor_pretty", lambda _report: "AWF doctor: ok\n")

    result = _runner.invoke(
        app,
        ["service", "doctor", "--provider", "github", "--provider", "codex"],
    )

    assert result.exit_code == 0, result.output
    assert captured["strict_providers"] == {"github", "codex"}


@pytest.mark.unit
def test_service_doctor_unknown_provider_exits_two_without_traceback() -> None:
    result = _runner.invoke(app, ["service", "doctor", "--provider", "bogus"])

    output = _combined_output(result)
    assert result.exit_code == 2
    assert "unknown provider" in output
    assert "Traceback" not in output


@pytest.mark.unit
def test_service_doctor_does_not_change_status_pretty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import status as status_mod

    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: object())

    async def _collect(_settings: object, **_kwargs: object) -> dict[str, object]:
        return {
            "service": "awf",
            "status": "ok",
            "checks": {"docker": {"ok": True, "status": "ok", "reason": "DOCKER_OK"}},
        }

    monkeypatch.setattr(status_mod, "collect_service_status", _collect)

    result = _runner.invoke(app, ["service", "status", "--format", "pretty"])

    assert result.exit_code == 0, result.output
    assert "checks.docker.reason: DOCKER_OK" in result.stdout
    assert "AWF doctor:" not in result.stdout


@pytest.mark.unit
def test_service_provider_help_lists_codex_and_docker() -> None:
    status_result = _runner.invoke(app, ["service", "status", "--help"])
    bootstrap_result = _runner.invoke(app, ["service", "bootstrap", "--help"])

    assert status_result.exit_code == 0, status_result.output
    assert bootstrap_result.exit_code == 0, bootstrap_result.output
    for output in (status_result.output, bootstrap_result.output):
        assert "codex" in output
        assert "grok" in output
        assert "docker" in output


@pytest.mark.unit
def test_worker_entrypoint_wires_control_worker_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import worker as worker_mod
    from awf.service.config import ServiceSettings

    created: dict[str, Any] = {}

    class _Engine:
        def __init__(self) -> None:
            self.url = make_url(_POSTGRES_TEST_URL)

        async def dispose(self) -> None:
            created["disposed"] = True

    class _GitManager:
        def __init__(
            self,
            work_dir: Path,
            *,
            env: dict[str, str],
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

    class _ComposeStackLauncher:
        def __init__(
            self,
            *,
            compose: object,
            agent_runtime_image: str,
            auth_mount_resolver: object | None = None,
            secret_lease_resolver: object | None = None,
            companion_image_builder: object | None = None,
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

    class _ControlWorker:
        def __init__(
            self,
            *,
            session_factory: object,
            provisioner: object,
            executor: object,
            runtime_cleaner: object,
            open_pr_resolver: object,
            config: object,
            orphan_dir_reconciler: object = None,
            classified_orphan_reaper: object = None,
            claude_base_reaper: object = None,
            terminal_gc_reaper: object = None,
            auth_overlay_work_dir: object = None,
        ) -> None:
            created["worker_session_factory"] = session_factory
            created["worker_provisioner"] = provisioner
            created["worker_executor"] = executor
            created["worker_runtime_cleaner"] = runtime_cleaner
            created["worker_open_pr_resolver"] = open_pr_resolver
            created["worker_config"] = config
            created["worker_orphan_dir_reconciler"] = orphan_dir_reconciler
            created["worker_classified_orphan_reaper"] = classified_orphan_reaper
            created["worker_claude_base_reaper"] = claude_base_reaper
            created["worker_terminal_gc_reaper"] = terminal_gc_reaper
            created["worker_auth_overlay_work_dir"] = auth_overlay_work_dir

        async def run_once(self) -> int:
            created["run_once"] = True
            return 0

        async def wait_for_execution_tasks(self) -> None:
            created["wait_for_execution_tasks"] = True

    engine = _Engine()
    session_factory = object()

    def _make_engine(url: str) -> _Engine:
        created["db_url"] = url
        engine.url = make_url(url)
        return engine

    def _make_session_factory(eng: _Engine) -> object:
        created["session_engine"] = eng
        return session_factory

    monkeypatch.setattr(worker_mod, "make_engine", _make_engine)
    monkeypatch.setattr(
        worker_mod,
        "make_session_factory",
        _make_session_factory,
    )
    monkeypatch.setattr(worker_mod, "GitManager", _GitManager)
    monkeypatch.setattr(worker_mod, "ComposeManager", _ComposeManager, raising=False)
    monkeypatch.setattr(worker_mod, "ComposeStackLauncher", _ComposeStackLauncher, raising=False)
    monkeypatch.setattr(worker_mod, "Provisioner", _Provisioner)
    monkeypatch.setattr(worker_mod, "ControlWorker", _ControlWorker)

    host_work_dir = (tmp_path / "awf-work").resolve()
    settings = ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url=_POSTGRES_TEST_URL,
        docker_host="unix:///var/run/docker.sock",
        agent_runtime_image="custom-agent-runtime:dev",
        work_dir=str(host_work_dir),
        host_home=str(tmp_path / "host-home"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.25,
        worker_max_concurrent_provisions=2,
        worker_max_concurrent_executions=4,
        node_id="node-1",
    )

    asyncio.run(worker_mod.run_worker(settings, once=True))

    assert created["db_url"] == settings.database_url
    assert created["session_engine"] is engine
    assert created["git_work_dir"] == host_work_dir / "git"
    assert created["git_env"]["HOME"] == str((tmp_path / "host-home").resolve())
    assert created["git_worktree_owner_uid"] == 1000
    assert created["git_worktree_owner_gid"] == 1000
    assert created["compose_work_dir"] == host_work_dir
    assert created["compose_template_path"].name == "workspace.base.yml.j2"
    assert created["stack_compose"].__class__ is _ComposeManager
    assert created["stack_agent_runtime_image"] == "custom-agent-runtime:dev"
    assert created["stack_auth_mount_resolver"] is not None
    assert created["stack_auth_mount_resolver"].host_home == (tmp_path / "host-home").resolve()
    assert created["stack_auth_mount_resolver"].work_dir == host_work_dir
    assert created["stack_secret_lease_resolver"] is not None
    assert created["stack_secret_lease_resolver"].host_home == (tmp_path / "host-home").resolve()
    assert created["stack_secret_lease_resolver"].work_dir == host_work_dir
    assert created["stack_secret_lease_resolver"].host_env is worker_mod.os.environ
    assert created["provisioner_session_factory"] is session_factory
    assert created["worker_session_factory"] is session_factory
    assert created["worker_auth_overlay_work_dir"] == host_work_dir
    assert created["provisioner_stack_launcher"].__class__ is _ComposeStackLauncher
    assert created["worker_executor"] is not None
    assert created["worker_open_pr_resolver"] is not None
    assert created["worker_orphan_dir_reconciler"] is not None
    assert created["worker_classified_orphan_reaper"] is not None
    assert created["worker_claude_base_reaper"] is not None
    assert created["worker_terminal_gc_reaper"] is not None
    assert created["provisioner_config"].node_id == "node-1"
    assert created["worker_config"].poll_interval_seconds == 0.25
    assert created["worker_config"].max_concurrent_provisions == 2
    assert created["worker_config"].max_concurrent_executions == 4
    assert created["worker_config"].node_id == "node-1"
    assert created["run_once"] is True
    assert created["wait_for_execution_tasks"] is True
    assert created["disposed"] is True
