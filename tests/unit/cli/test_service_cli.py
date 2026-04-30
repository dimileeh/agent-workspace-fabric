"""Service-oriented CLI and local service runtime tests."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import typer
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from awf.cli.main import app
from awf.common.config import Settings
from awf.service.target_branch_monitor import (
    TargetBranchMonitorResult,
    TargetBranchMonitorStatus,
)

_runner = CliRunner()


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


def _create_gc_cli_workspace(
    *,
    db_url: str,
    status: str,
    updated_at: datetime,
    pr: bool = False,
    compose_file_path: str | None = None,
) -> str:
    async def _setup() -> str:
        from awf.db.base import Base
        from awf.db.repositories import WorkspaceRepository
        from awf.db.session import make_engine, make_session_factory

        engine = make_engine(db_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
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
            workspace_id = workspace.id
        await engine.dispose()
        return workspace_id

    return asyncio.run(_setup())


@pytest.mark.unit
def test_service_logs_defaults_to_tail_api_and_worker_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args, returncode=0, stdout="api log\nworker log\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(app, ["service", "logs"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "api log\nworker log\n"
    assert calls == [
        (
            [
                "docker",
                "compose",
                "-f",
                "docker/compose/local-service.yml",
                "logs",
                "--tail",
                "100",
                "api",
                "worker",
            ],
            {"check": False, "capture_output": True, "text": True},
        )
    ]


@pytest.mark.unit
def test_service_logs_accepts_repeated_service_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(
        app,
        [
            "service",
            "logs",
            "--tail",
            "25",
            "--service",
            "postgres",
            "--service",
            "migrate",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        [
            "docker",
            "compose",
            "-f",
            "docker/compose/local-service.yml",
            "logs",
            "--tail",
            "25",
            "postgres",
            "migrate",
        ]
    ]


@pytest.mark.unit
def test_service_logs_follow_streams_without_capturing_subprocess_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, returncode=0, stdout=None, stderr=None)

    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(app, ["service", "logs", "--follow", "--service", "worker"])

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert calls == [
        (
            [
                "docker",
                "compose",
                "-f",
                "docker/compose/local-service.yml",
                "logs",
                "--tail",
                "100",
                "--follow",
                "worker",
            ],
            {"check": False, "capture_output": False, "text": True},
        )
    ]


@pytest.mark.unit
@pytest.mark.parametrize("returncode", [130, -2])
def test_service_logs_follow_ignores_subprocess_interrupt_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=returncode, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(app, ["service", "logs", "--follow"])

    assert result.exit_code == 0, result.output
    assert _combined_output(result) == ""


@pytest.mark.unit
def test_service_logs_follow_keyboard_interrupt_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(app, ["service", "logs", "--follow"])

    assert result.exit_code == 0, result.output
    assert _combined_output(result) == ""


@pytest.mark.unit
def test_service_logs_docker_compose_failure_is_clean_typer_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args,
            returncode=17,
            stdout="",
            stderr='service "api" is not running\n',
        )

    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(app, ["service", "logs"])

    output = _combined_output(result)
    assert result.exit_code == 1
    assert 'error: docker compose logs failed (exit 17): service "api" is not running' in output
    assert "Traceback" not in output


@pytest.mark.unit
def test_service_bootstrap_cli_invokes_helper_and_emits_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service.bootstrap import ServiceBootstrapResult

    settings = object()
    captured: dict[str, object] = {}
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: settings)

    async def _bootstrap(received: object, **kwargs: object) -> ServiceBootstrapResult:
        captured["settings"] = received
        captured.update(kwargs)
        return ServiceBootstrapResult(
            stages=(),
            service_status={"service": "awf", "status": "ok", "checks": {}},
        )

    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = _runner.invoke(app, ["service", "bootstrap"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["service_status"]["status"] == "ok"
    assert captured["settings"] is settings
    options = captured["options"]
    assert options.timeout_seconds == 180
    assert options.poll_interval_seconds == 2
    assert options.skip_agent_runtime_build is False
    assert options.strict_providers == frozenset()


@pytest.mark.unit
def test_service_bootstrap_cli_pretty_output_uses_existing_emitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service.bootstrap import ServiceBootstrapResult

    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: object())

    async def _bootstrap(_settings: object, **_kwargs: object) -> ServiceBootstrapResult:
        return ServiceBootstrapResult(
            stages=(),
            service_status={"service": "awf", "status": "ok", "checks": {}},
        )

    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = _runner.invoke(app, ["service", "bootstrap", "--format", "pretty"])

    assert result.exit_code == 0, result.output
    assert "status: ok" in result.stdout
    assert "service_status.status: ok" in result.stdout


@pytest.mark.unit
def test_service_bootstrap_cli_passes_strict_provider_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service.bootstrap import ServiceBootstrapResult

    captured: dict[str, object] = {}
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: object())

    async def _bootstrap(_settings: object, **kwargs: object) -> ServiceBootstrapResult:
        captured.update(kwargs)
        return ServiceBootstrapResult(
            stages=(),
            service_status={"service": "awf", "status": "ok", "checks": {}},
        )

    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = _runner.invoke(
        app,
        [
            "service",
            "bootstrap",
            "--provider",
            "github",
            "--provider",
            "opencode",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["options"].strict_providers == frozenset({"github", "opencode"})


@pytest.mark.unit
def test_service_bootstrap_cli_helper_failures_exit_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service.bootstrap import ServiceBootstrapError

    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: object())

    async def _bootstrap(_settings: object, **_kwargs: object) -> object:
        raise ServiceBootstrapError(
            reason_code="SERVICE_BOOTSTRAP_TIMEOUT",
            message="timed out waiting for service readiness",
            last_status={"status": "fail", "checks": {"api": {"reason": "API_UNREACHABLE"}}},
        )

    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = _runner.invoke(app, ["service", "bootstrap"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "SERVICE_BOOTSTRAP_TIMEOUT"
    assert payload["last_status"]["status"] == "fail"
    assert "Traceback" not in _combined_output(result)


@pytest.mark.unit
def test_service_bootstrap_cli_rejects_unknown_provider_without_traceback() -> None:
    result = _runner.invoke(app, ["service", "bootstrap", "--provider", "bogus"])

    output = _combined_output(result)
    assert result.exit_code == 2
    assert "error: unknown provider(s): bogus" in output
    assert "Traceback" not in output


@pytest.mark.unit
def test_service_reconcile_target_invokes_target_branch_monitor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import target_branch_monitor

    calls: list[dict[str, object]] = []

    async def _fake_reconcile_once(**kwargs: object) -> TargetBranchMonitorResult:
        calls.append(kwargs)
        return TargetBranchMonitorResult(
            repo_url=str(kwargs["repo_url"]),
            branch=str(kwargs["branch"]),
            checkout_path=tmp_path / "checkout",
            status=TargetBranchMonitorStatus.clean,
            resolver_results=(),
        )

    monkeypatch.setattr(
        target_branch_monitor,
        "run_target_branch_reconcile_once",
        _fake_reconcile_once,
    )

    result = _runner.invoke(
        app,
        [
            "service",
            "reconcile-target",
            "--repo-url",
            "git@github.com:example/repo.git",
            "--branch",
            "development",
            "--work-dir",
            str(tmp_path / "state"),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "clean"
    assert calls[0]["repo_url"] == "git@github.com:example/repo.git"
    assert calls[0]["branch"] == "development"
    assert calls[0]["work_dir"] == (tmp_path / "state").resolve()


@pytest.mark.unit
def test_readme_documents_service_logs_command() -> None:
    readme = Path("README.md").read_text()

    assert "awf service logs" in readme
    assert "docker/compose/local-service.yml" in readme
    assert "--tail" in readme
    assert "--service worker" in readme
    assert "--follow" in readme


@pytest.mark.unit
def test_readme_documents_service_bootstrap_command() -> None:
    readme = Path("README.md").read_text()

    assert "awf service bootstrap" in readme
    assert "uv run --python 3.12 --extra dev awf service bootstrap" in readme
    assert "uv run --python 3.12 --extra dev awf service status --format pretty" in readme
    assert "docker compose -f docker/compose/local-service.yml up --build" in readme
    assert "safe to re-run" in readme


@pytest.mark.unit
def test_readme_documents_control_plane_postgres_backup_restore() -> None:
    readme = Path("README.md").read_text()

    assert "### Control-Plane Postgres Backup And Restore" in readme
    assert "AWF control-plane database" in readme
    assert "workspace or project databases" in readme
    assert "docker compose -f docker/compose/local-service.yml exec -T postgres" in readme
    assert "pg_dump" in readme
    assert "pg_restore" in readme
    assert "docker compose -f docker/compose/local-service.yml stop api worker" in readme
    assert "before restore" in readme.lower()


@pytest.mark.unit
def test_readme_documents_local_service_upgrade_and_image_versioning() -> None:
    readme = Path("README.md").read_text()

    assert "### Local Service Image Versioning" in readme
    assert "### Local Service Upgrade" in readme
    assert "awf-control-plane:local" in readme
    assert "docker compose -f docker/compose/local-service.yml build" in readme
    assert "uv run --python 3.12 --extra dev awf service bootstrap" in readme
    assert "docker build -t awf-agent-runtime:latest" in readme
    assert "docker image inspect awf-control-plane:local" in readme
    assert "docker image inspect awf-agent-runtime:latest" in readme
    assert "migrate" in readme


@pytest.mark.unit
def test_readme_documents_rollback_and_migration_handling() -> None:
    readme = Path("README.md").read_text()

    assert "### Local Service Rollback" in readme
    assert "pre-upgrade backup" in readme
    assert "image rollback" in readme
    assert "database migration rollback" in readme
    assert "awf service logs --service migrate" in readme
    assert "do not delete the Postgres volume" in readme
    assert "backup" in readme


@pytest.mark.unit
def test_readme_documents_local_disaster_recovery() -> None:
    readme = Path("README.md").read_text()

    assert "### Local Disaster Recovery" in readme
    assert "docker compose -f docker/compose/local-service.yml down --remove-orphans" in readme
    assert "${AWF_HOST_WORK_DIR}" in readme
    assert "quarantine" in readme.lower()
    assert "logs, artifacts, backups, and auth" in readme
    assert "Postgres volume" in readme
    assert "uv run --python 3.12 --extra dev awf service status --format pretty" in readme


@pytest.mark.unit
def test_readme_documents_run_awf_compatibility_status() -> None:
    readme = Path("README.md").read_text()
    normalized_readme = " ".join(readme.split())

    assert "`scripts/run_awf.py` is the compatibility dogfood runner" in normalized_readme
    assert "SQLite DB under `--work-dir`" in normalized_readme
    assert "does not require the local Postgres control-plane database" in readme
    assert "service worker is the normal always-on executor" in readme


@pytest.mark.unit
def test_readme_documents_service_gc_command() -> None:
    readme = Path("README.md").read_text()

    assert "awf service gc" in readme
    assert "--execute" in readme
    assert "--min-age-hours" in readme
    assert "dry-run" in readme.lower()
    assert "control-plane database" in readme
    assert "log streams" in readme


@pytest.mark.unit
def test_service_gc_cli_defaults_to_json_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'awf.db'}"
    work_dir = tmp_path / "service"
    workspace_id = _create_gc_cli_workspace(
        db_url=db_url,
        status="completed",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        pr=True,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write_gc_file(worktree / "repo.txt", "repo")
    monkeypatch.setenv("AWF_DATABASE_URL", db_url)
    monkeypatch.setenv("AWF_WORK_DIR", str(work_dir))

    result = _runner.invoke(app, ["service", "gc", "--min-age-hours", "1"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["status"] == "dry_run"
    assert payload["policy"]["retention_hours"] == 1
    assert payload["candidate_count"] == 1
    assert payload["preserved_count"] == 0
    assert payload["reason_code"] == "CLEANUP_DRY_RUN"
    assert payload["deleted_paths"] == []
    assert payload["candidates"][0]["workspace_id"] == workspace_id
    assert payload["candidates"][0]["status"] == "completed"
    assert payload["candidates"][0]["reason_code"] == "COMPLETED_PR_RETENTION_EXPIRED"
    assert payload["candidates"][0]["paths"]["worktree"]["path"] == str(worktree)
    assert worktree.exists()


@pytest.mark.unit
def test_service_gc_cli_execute_deletes_and_supports_pretty_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'awf.db'}"
    work_dir = tmp_path / "service"
    workspace_id = _create_gc_cli_workspace(
        db_url=db_url,
        status="completed",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        pr=True,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    _write_gc_file(worktree / "repo.txt", "repo")
    _write_gc_file(compose / "compose.yml", "compose")
    _write_gc_file(auth / "codex" / "auth.json", "auth")
    monkeypatch.setenv("AWF_DATABASE_URL", db_url)
    monkeypatch.setenv("AWF_WORK_DIR", str(work_dir))

    result = _runner.invoke(
        app,
        [
            "service",
            "gc",
            "--execute",
            "--min-age-hours",
            "1",
            "--format",
            "pretty",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "dry_run: False" in result.stdout
    assert "reason_code: CLEANUP_EXECUTION_SUCCEEDED" in result.stdout
    assert "candidate_count: 1" in result.stdout
    assert workspace_id in result.stdout
    assert "deleted_paths" in result.stdout
    assert not worktree.exists()
    assert not compose.exists()
    assert not auth.exists()


@pytest.mark.unit
def test_service_gc_cli_uses_configured_retention_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'awf.db'}"
    work_dir = tmp_path / "service"
    workspace_id = _create_gc_cli_workspace(
        db_url=db_url,
        status="completed",
        updated_at=datetime.now(UTC) - timedelta(hours=2),
        pr=True,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write_gc_file(worktree / "repo.txt", "repo")
    monkeypatch.setenv("AWF_DATABASE_URL", db_url)
    monkeypatch.setenv("AWF_WORK_DIR", str(work_dir))
    monkeypatch.setenv("AWF_COMPLETED_WORKSPACE_RETENTION_HOURS", "24")

    result = _runner.invoke(app, ["service", "gc"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["policy"]["retention_hours"] == 24
    assert payload["candidate_count"] == 0
    assert payload["preserved_count"] == 1
    assert payload["preserved"][0]["workspace_id"] == workspace_id
    assert payload["preserved"][0]["reason_code"] == "WORKSPACE_WITHIN_RETENTION"
    assert worktree.exists()


@pytest.mark.unit
def test_service_gc_cli_execute_failures_exit_nonzero_with_reason_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'awf.db'}"
    work_dir = tmp_path / "service"
    workspace_id = _create_gc_cli_workspace(
        db_url=db_url,
        status="completed",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        pr=True,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    worktree.parent.mkdir(parents=True, exist_ok=True)
    worktree.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("AWF_DATABASE_URL", db_url)
    monkeypatch.setenv("AWF_WORK_DIR", str(work_dir))

    result = _runner.invoke(
        app,
        ["service", "gc", "--execute", "--min-age-hours", "1"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "partial"
    assert payload["reason_code"] == "CLEANUP_EXECUTION_PARTIAL"
    assert payload["delete_errors"][0]["reason_code"] == "PATH_DELETE_FAILED"
    assert payload["candidates"][0]["paths"]["worktree"]["status"] == "failed"


@pytest.mark.unit
def test_service_config_uses_postgres_default_and_redacts_secrets() -> None:
    from awf.service.config import (
        DEFAULT_LOCAL_SERVICE_DATABASE_URL,
        resolve_service_settings,
        service_config_payload,
    )

    base = Settings(
        _env_file=None,
        api_token="api-secret",
        github_token="ghp_secret",
        database_url="sqlite+aiosqlite:///./awf.db",
        worker_max_concurrent_executions=5,
    )

    settings = resolve_service_settings(base, environ={})
    payload = service_config_payload(settings)
    rendered = json.dumps(payload)

    assert settings.database_url == DEFAULT_LOCAL_SERVICE_DATABASE_URL
    assert settings.worker_max_concurrent_executions == 5
    assert payload["database_url"].startswith("postgresql+asyncpg://awf:")
    assert "awf_dev" not in payload["database_url"]
    assert payload["api_token"] == "<redacted>"
    assert payload["github_token"] == "<redacted>"
    assert "api-secret" not in rendered
    assert "ghp_secret" not in rendered


@pytest.mark.unit
def test_service_config_carries_host_home_for_service_auth_mounts(tmp_path: Path) -> None:
    from awf.service.config import resolve_service_settings

    host_home = tmp_path / "host-home"
    base = Settings(_env_file=None, host_home=str(host_home))

    settings = resolve_service_settings(base, environ={"AWF_HOST_HOME": str(host_home)})

    assert settings.host_home == str(host_home)


@pytest.mark.unit
def test_service_mode_uses_postgres_when_database_env_unset() -> None:
    from awf.service.config import DEFAULT_LOCAL_SERVICE_DATABASE_URL, resolve_service_settings

    base = Settings(_env_file=None)

    service_settings = resolve_service_settings(base, environ={})

    assert base.database_url.startswith("sqlite+aiosqlite")
    assert service_settings.database_url == DEFAULT_LOCAL_SERVICE_DATABASE_URL
    assert service_settings.database_url.startswith("postgresql+asyncpg://")


@pytest.mark.unit
def test_service_mode_preserves_explicit_sqlite_for_throwaway_runs(tmp_path: Path) -> None:
    from awf.service.config import resolve_service_settings

    sqlite_url = f"sqlite+aiosqlite:///{tmp_path / 'throwaway.db'}"
    base = Settings(_env_file=None, database_url=sqlite_url)

    service_settings = resolve_service_settings(base, environ={"AWF_DATABASE_URL": sqlite_url})

    assert service_settings.database_url == sqlite_url


@pytest.mark.unit
def test_service_config_command_prints_redacted_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWF_DATABASE_URL", raising=False)
    monkeypatch.setenv("AWF_API_TOKEN", "api-secret")
    monkeypatch.setenv("AWF_GITHUB_TOKEN", "ghp_secret")

    result = _runner.invoke(app, ["service", "config"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["database_url"].startswith("postgresql+asyncpg://")
    assert body["api_token"] == "<redacted>"
    assert body["github_token"] == "<redacted>"
    assert "api-secret" not in result.stdout
    assert "ghp_secret" not in result.stdout


@pytest.mark.unit
def test_service_status_uses_mocked_api_db_docker_and_image_checks(tmp_path: Path) -> None:
    from awf.service.config import ServiceSettings
    from awf.service.status import collect_service_status

    api_calls: list[str] = []
    subprocess_calls: list[list[str]] = []

    class _Response:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {"status": "ok", "version": "test-version"}

        def raise_for_status(self) -> None:
            return None

    async def _api_get(url: str, *, timeout: float) -> _Response:
        api_calls.append(url)
        return _Response()

    async def _db_probe(database_url: str) -> dict[str, Any]:
        assert database_url == "postgresql+asyncpg://awf:pw@localhost:5433/awf"
        return {"ok": True, "status": "ok"}

    def _run_subprocess(args: list[str], **_kwargs: object) -> Any:
        subprocess_calls.append(args)
        if args[:2] == ["docker", "info"]:
            return type("Completed", (), {"returncode": 0, "stdout": "27.0.3\n", "stderr": ""})()
        if args[:3] == ["docker", "image", "inspect"]:
            return type(
                "Completed",
                (),
                {"returncode": 0, "stdout": "sha256:deadbeef\n", "stderr": ""},
            )()
        if args[:3] == ["docker", "ps", "-a"]:
            return type(
                "Completed",
                (),
                {"returncode": 0, "stdout": "", "stderr": ""},
            )()
        raise AssertionError(f"unexpected subprocess call: {args}")

    settings = ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:pw@localhost:5433/awf",
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home"),
    )

    status = asyncio.run(
        collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_run_subprocess,
            socket_exists=lambda _path: True,
            disk_usage=_ok_disk_usage,
            provider_environ={},
        )
    )

    assert status["status"] == "ok"
    assert api_calls == ["http://localhost:8000/healthz"]
    assert ["docker", "info", "--format", "{{.ServerVersion}}"] in subprocess_calls
    assert [
        "docker",
        "image",
        "inspect",
        "awf-agent-runtime:latest",
        "--format",
        "{{.Id}}",
    ] in subprocess_calls


@pytest.mark.unit
def test_service_status_runs_dependency_checks_concurrently(tmp_path: Path) -> None:
    from awf.service.config import ServiceSettings
    from awf.service.status import collect_service_status

    started = {
        "api": threading.Event(),
        "docker": threading.Event(),
        "image": threading.Event(),
    }
    release = threading.Event()

    class _Response:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {"status": "ok"}

        def raise_for_status(self) -> None:
            return None

    def _wait_for_release(name: str) -> None:
        started[name].set()
        if not release.wait(timeout=2):
            raise AssertionError(f"{name} check was not released")

    async def _api_get(url: str, *, timeout: float) -> _Response:
        started["api"].set()
        if not await asyncio.to_thread(release.wait, 2):
            raise AssertionError("api check was not released")
        return _Response()

    async def _db_probe(database_url: str) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2
        while not all(event.is_set() for event in started.values()):
            if loop.time() >= deadline:
                missing = sorted(name for name, event in started.items() if not event.is_set())
                raise AssertionError(f"checks did not run concurrently: {missing}")
            await asyncio.sleep(0.01)
        release.set()
        return {"ok": True, "status": "ok"}

    def _run_subprocess(args: list[str], **_kwargs: object) -> Any:
        if args[:2] == ["docker", "info"]:
            _wait_for_release("docker")
            return type("Completed", (), {"returncode": 0, "stdout": "27.0.3\n", "stderr": ""})()
        if args[:3] == ["docker", "image", "inspect"]:
            _wait_for_release("image")
            return type(
                "Completed",
                (),
                {"returncode": 0, "stdout": "sha256:deadbeef\n", "stderr": ""},
            )()
        if args[:3] == ["docker", "ps", "-a"]:
            return type(
                "Completed",
                (),
                {"returncode": 0, "stdout": "", "stderr": ""},
            )()
        raise AssertionError(f"unexpected subprocess call: {args}")

    settings = ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:pw@localhost:5433/awf",
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home"),
    )

    status = asyncio.run(
        collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_run_subprocess,
            socket_exists=lambda _path: True,
            disk_usage=_ok_disk_usage,
            provider_environ={},
        )
    )

    assert status["status"] == "ok"


@pytest.mark.unit
def test_service_status_reports_failures_from_mocked_checks(tmp_path: Path) -> None:
    from awf.service.config import ServiceSettings
    from awf.service.status import collect_service_status

    async def _api_get(url: str, *, timeout: float) -> Any:
        raise httpx.ConnectError("connection refused")

    async def _db_probe(database_url: str) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "fail",
            "reason": "DB_CONNECTION_FAILED",
            "detail": "connection refused",
        }

    def _run_subprocess(args: list[str], **_kwargs: object) -> Any:
        return type(
            "Completed",
            (),
            {"returncode": 1, "stdout": "", "stderr": "Cannot connect to Docker\n"},
        )()

    settings = ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:pw@localhost:5433/awf",
        docker_host=f"unix://{tmp_path / 'missing.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home"),
    )

    status = asyncio.run(
        collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_run_subprocess,
            socket_exists=lambda _path: False,
            disk_usage=_ok_disk_usage,
            provider_environ={},
        )
    )

    assert status["status"] == "fail"
    assert status["checks"]["api"]["reason"] == "API_UNREACHABLE"
    assert status["checks"]["db"]["reason"] == "DB_CONNECTION_FAILED"
    assert status["checks"]["docker"]["reason"] == "DOCKER_SOCKET_UNREACHABLE"
    assert status["checks"]["agent_runtime_image"]["reason"] == "AGENT_RUNTIME_IMAGE_MISSING"


@pytest.mark.unit
def test_service_status_pretty_output_includes_disk_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import config as config_mod
    from awf.service import status as status_mod

    settings = object()
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: settings)

    async def _collect(received: object, **_kwargs: object) -> dict[str, object]:
        assert received is settings
        return {
            "service": "awf",
            "status": "ok",
            "checks": {
                "disk": {
                    "ok": True,
                    "status": "ok",
                    "reason": "SUFFICIENT_DISK",
                    "free_bytes": 300,
                    "threshold_bytes": 200,
                }
            },
        }

    monkeypatch.setattr(status_mod, "collect_service_status", _collect)

    result = _runner.invoke(app, ["service", "status", "--format", "pretty"])

    assert result.exit_code == 0, result.output
    assert "checks.disk.free_bytes: 300" in result.stdout
    assert "checks.disk.threshold_bytes: 200" in result.stdout


@pytest.mark.unit
def test_service_status_pretty_output_includes_provider_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import config as config_mod
    from awf.service import status as status_mod

    settings = object()
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: settings)

    async def _collect(received: object, **kwargs: object) -> dict[str, object]:
        assert received is settings
        assert kwargs["strict_providers"] == set()
        return {
            "service": "awf",
            "status": "ok",
            "checks": {},
            "agent_readiness": {
                "status": "ok",
                "strict_providers": [],
                "providers": {
                    "github": {
                        "ok": False,
                        "status": "warn",
                        "reason": "GITHUB_TOKEN_ENV_MISSING",
                    }
                },
            },
        }

    monkeypatch.setattr(status_mod, "collect_service_status", _collect)

    result = _runner.invoke(app, ["service", "status", "--format", "pretty"])

    assert result.exit_code == 0, result.output
    assert "agent_readiness.providers.github.reason: GITHUB_TOKEN_ENV_MISSING" in result.stdout


@pytest.mark.unit
def test_service_status_provider_option_requests_strict_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import config as config_mod
    from awf.service import status as status_mod

    settings = object()
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: settings)

    async def _collect(received: object, **kwargs: object) -> dict[str, object]:
        assert received is settings
        assert kwargs["strict_providers"] == {"github"}
        return {
            "service": "awf",
            "status": "fail",
            "checks": {},
            "agent_readiness": {
                "status": "fail",
                "strict_providers": ["github"],
                "providers": {
                    "github": {
                        "ok": False,
                        "status": "fail",
                        "reason": "GITHUB_TOKEN_ENV_MISSING",
                    }
                },
            },
        }

    monkeypatch.setattr(status_mod, "collect_service_status", _collect)

    result = _runner.invoke(
        app,
        ["service", "status", "--provider", "github", "--format", "pretty"],
    )

    assert result.exit_code == 1, result.output
    assert "agent_readiness.providers.github.status: fail" in result.stdout
    assert "GITHUB_TOKEN_ENV_MISSING" in result.stdout


@pytest.mark.unit
def test_service_status_provider_option_accepts_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import config as config_mod
    from awf.service import status as status_mod

    settings = object()
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: settings)

    async def _collect(received: object, **kwargs: object) -> dict[str, object]:
        assert received is settings
        assert kwargs["strict_providers"] == {"codex"}
        return {
            "service": "awf",
            "status": "fail",
            "checks": {},
            "agent_readiness": {
                "status": "fail",
                "strict_providers": ["codex"],
                "security": {
                    "status": "warning",
                    "warning_count": 1,
                    "providers_with_warnings": ["codex"],
                    "reason_codes": ["CODEX_AUTH_MISSING"],
                },
                "providers": {
                    "codex": {
                        "ok": False,
                        "status": "fail",
                        "reason": "CODEX_AUTH_MISSING",
                    }
                },
            },
        }

    monkeypatch.setattr(status_mod, "collect_service_status", _collect)

    result = _runner.invoke(
        app,
        ["service", "status", "--provider", "codex", "--format", "pretty"],
    )

    assert result.exit_code == 1, result.output
    assert "agent_readiness.providers.codex.status: fail" in result.stdout
    assert "agent_readiness.security.reason_codes: ['CODEX_AUTH_MISSING']" in result.stdout


@pytest.mark.unit
def test_service_doctor_defaults_to_pretty_output_and_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    settings = object()
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: settings)

    report = SimpleNamespace(
        status="ok",
        to_dict=lambda: {
            "service": "awf",
            "status": "ok",
            "summary": {"ok": 1, "warn": 0, "fail": 0},
            "diagnostics": [
                {
                    "id": "docker",
                    "label": "Docker",
                    "status": "ok",
                    "reason": "DOCKER_OK",
                    "message": "Docker daemon is reachable.",
                    "action": "No action required.",
                    "source": "checks.docker",
                    "metadata": {},
                }
            ],
        }
    )

    async def _collect(received: object, **kwargs: object) -> object:
        assert received is settings
        assert kwargs["strict_providers"] == set()
        assert kwargs["provider_environ"] is os.environ
        assert kwargs["environ"] is os.environ
        return report

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect)
    monkeypatch.setattr(doctor_mod, "render_doctor_pretty", lambda _report: "AWF doctor: ok\n")

    result = _runner.invoke(app, ["service", "doctor"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "AWF doctor: ok\n"


@pytest.mark.unit
def test_service_doctor_json_output_is_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: object())
    report = SimpleNamespace(
        status="ok",
        to_dict=lambda: {
            "service": "awf",
            "status": "ok",
            "summary": {"ok": 1, "warn": 0, "fail": 0},
            "diagnostics": [],
        }
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
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: object())
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
        }
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
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: object())

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
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: object())

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
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    captured: dict[str, object] = {}
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: object())
    report = SimpleNamespace(
        status="ok",
        to_dict=lambda: {
            "service": "awf",
            "status": "ok",
            "summary": {"ok": 1, "warn": 0, "fail": 0},
            "diagnostics": [],
        }
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
    from awf.service import config as config_mod
    from awf.service import status as status_mod

    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: object())

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
            self.url = make_url("sqlite+aiosqlite:///:memory:")

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
        ) -> None:
            created["stack_compose"] = compose
            created["stack_agent_runtime_image"] = agent_runtime_image
            created["stack_auth_mount_resolver"] = auth_mount_resolver
            created["stack_secret_lease_resolver"] = secret_lease_resolver

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
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'awf.db'}",
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
    assert created["compose_work_dir"] == host_work_dir
    assert created["compose_template_path"].name == "workspace.base.yml.j2"
    assert created["stack_compose"].__class__ is _ComposeManager
    assert created["stack_agent_runtime_image"] == "custom-agent-runtime:dev"
    assert created["stack_auth_mount_resolver"] is not None
    assert created["stack_auth_mount_resolver"].host_home == (tmp_path / "host-home").resolve()
    assert created["stack_auth_mount_resolver"].work_dir == host_work_dir
    assert created["stack_secret_lease_resolver"] is not None
    assert created["stack_secret_lease_resolver"].host_home == (
        tmp_path / "host-home"
    ).resolve()
    assert created["stack_secret_lease_resolver"].work_dir == host_work_dir
    assert created["stack_secret_lease_resolver"].host_env is worker_mod.os.environ
    assert created["provisioner_session_factory"] is session_factory
    assert created["worker_session_factory"] is session_factory
    assert created["provisioner_stack_launcher"].__class__ is _ComposeStackLauncher
    assert created["worker_executor"] is not None
    assert created["provisioner_config"].node_id == "node-1"
    assert created["worker_config"].poll_interval_seconds == 0.25
    assert created["worker_config"].max_concurrent_provisions == 2
    assert created["worker_config"].max_concurrent_executions == 4
    assert created["worker_config"].node_id == "node-1"
    assert created["run_once"] is True
    assert created["wait_for_execution_tasks"] is True
    assert created["disposed"] is True
