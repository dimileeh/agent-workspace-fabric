"""Health + readiness endpoint contracts.

``/healthz`` is the liveness probe — dependency-free, must never depend on DB or
Docker. See module docstring in ``awf.api.routes.health`` for rationale.

``/readyz`` is the readiness probe required by PRD v2.2 §12 and §18.2/§18.3 — it
reports per-dependency status (DB + Docker stack + configured agent runtime
image) so an operator can see *which* dependency is down rather than just "AWF
unhealthy". The response shape lets dashboards and uptime probes alert on the
specific failing check rather than a generic 503.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.api.routes.health as health_route
from awf.api.app import configure_database, create_app
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.config import Settings, get_settings
from awf.db.enums import WorkspaceStatus
from awf.db.session import make_session_factory
from tests.unit.helpers import create_workspace

_PROVIDER_ENV_KEYS = (
    "AWF_GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "CODEX_API_KEY",
    "CODEX_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CURSOR_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_ACCESS_TOKEN",
    "OLLAMA_API_KEY",
    "AWF_OPENCODE_OLLAMA_BASE_URL",
    "OLLAMA_HOST",
    "DOCKER_AUTH_CONFIG",
    "DOCKER_HOST",
)


def _closed_connection_error() -> InterfaceError:
    return InterfaceError("SELECT 1", {}, RuntimeError("connection is closed"))


def _queue_all_ok(runner: FakeCommandRunner) -> None:
    """Queue successful results for the four docker-related subprocess calls.

    Order matches the readiness handler's sequential check order:
    docker --version → docker info → docker compose version → docker image inspect.
    """
    runner.queue_result(stdout="Docker version 27.0.3, build abc1234\n")
    runner.queue_result(stdout="27.0.3\n")
    runner.queue_result(stdout="v2.29.2\n")
    runner.queue_result(stdout="sha256:deadbeef\n")


@pytest.fixture
async def ready_app_and_client(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[tuple[Any, AsyncClient]]:
    """App + client pair so tests can mutate ``app.state`` (inject command runner)."""
    for key in _PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWF_API_TOKEN", "unit-test-api-token")
    get_settings.cache_clear()
    original_get_settings = health_route.get_settings
    original_get_settings.cache_clear()
    test_settings = Settings(
        _env_file=None,
        host_home=str(tmp_path / "home"),
        work_dir=str(tmp_path / "work"),
    )
    monkeypatch.setattr(health_route, "get_settings", lambda: test_settings)

    # Part 002 covers Docker/orphan-resource readiness paths. The DB health
    # probe and egress summary timeout behavior have dedicated coverage in part
    # 001, so keep these endpoint tests isolated from CI database latency.
    async def _db_ok(_factory: Any) -> health_route.CheckResult:
        return health_route.CheckResult(ok=True, status="ok")

    async def _empty_egress_counts(_factory: Any, _state: Any) -> dict[str, int]:
        return {}

    monkeypatch.setattr(health_route, "_check_db", _db_ok)
    monkeypatch.setattr(
        health_route,
        "_egress_audit_summary_counts_with_timeout",
        _empty_egress_counts,
    )
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            c.headers["Authorization"] = "Bearer unit-test-api-token"
            yield app, c
    finally:
        original_get_settings.cache_clear()
        get_settings.cache_clear()


@pytest.mark.unit
async def test_readyz_terminal_workspace_with_only_retained_worktree_stays_healthy(
    ready_app_and_client: tuple[Any, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = ready_app_and_client
    workspace_id = await create_workspace(
        engine,
        status=WorkspaceStatus.completed,
        updated_at=datetime.now(UTC),
    )
    settings = health_route.get_settings()
    worktree = Path(settings.work_dir) / "git" / "worktrees" / workspace_id
    worktree.mkdir(parents=True)

    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    runner.queue_result(stdout="")
    runner.queue_result(stdout="")
    runner.queue_result(
        stdout=json.dumps(
            {
                "name": f"awf_{workspace_id}_pgdata",
                "project": f"awf_{workspace_id}",
                "driver": "local",
                "scope": "local",
            }
        )
        + "\n"
    )
    app.state.command_runner = runner

    response = await client.get("/readyz")
    body = response.json()

    assert response.status_code == 200
    orphan_check = body["checks"]["orphan_resources"]
    assert orphan_check["ok"] is True
    assert orphan_check["reason"] == "NO_ORPHANS"
    assert orphan_check["orphan_count"] == 0
    assert orphan_check["expected_count"] == 2


@pytest.mark.unit
async def test_readyz_retains_recent_terminal_worktree_without_failing(
    ready_app_and_client: tuple[Any, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = ready_app_and_client
    workspace_id = await create_workspace(
        engine,
        status=WorkspaceStatus.completed,
        updated_at=datetime.now(UTC),
    )
    settings = health_route.get_settings()
    worktree = Path(settings.work_dir) / "git" / "worktrees" / workspace_id
    worktree.mkdir(parents=True)

    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    response = await client.get("/readyz")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ok"
    orphan_check = body["checks"]["orphan_resources"]
    assert orphan_check["ok"] is True
    assert orphan_check["reason"] == "NO_ORPHANS"
    assert orphan_check["orphan_count"] == 0
    assert orphan_check["expected_count"] == 1


@pytest.mark.unit
async def test_readyz_docker_compose_missing_returns_503(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    runner.queue_result(stdout="Docker version 27.0.3\n")
    runner.queue_result(stdout="27.0.3\n")
    runner.queue_result(  # docker compose version
        returncode=1,
        stderr="docker: 'compose' is not a docker command.\n",
    )
    runner.queue_result(stdout="sha256:deadbeef\n")
    app.state.command_runner = runner

    response = await client.get("/readyz")
    assert response.status_code == 503
    compose = response.json()["checks"]["docker_compose"]
    assert compose["ok"] is False
    assert compose["reason"] == "DOCKER_COMPOSE_NOT_AVAILABLE"


@pytest.mark.unit
async def test_readyz_agent_runtime_image_missing_returns_503(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    runner.queue_result(stdout="Docker version 27.0.3\n")
    runner.queue_result(stdout="27.0.3\n")
    runner.queue_result(stdout="v2.29.2\n")
    runner.queue_result(  # docker image inspect
        returncode=1,
        stderr="Error: No such image: awf-agent-runtime:latest\n",
    )
    app.state.command_runner = runner

    response = await client.get("/readyz")
    assert response.status_code == 503
    image_check = response.json()["checks"]["agent_runtime_image"]
    assert image_check["ok"] is False
    assert image_check["reason"] == "AGENT_RUNTIME_IMAGE_MISSING"
    # The configured image name must show up in the detail so the operator knows
    # which tag was expected — "image missing" with no name is unactionable.
    assert "awf-agent-runtime:latest" in (image_check["detail"] or "")


@pytest.mark.unit
async def test_readyz_uses_configured_agent_runtime_image(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    """The image inspect call must reference the runtime image from settings."""
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    await client.get("/readyz")

    image_inspect_call = runner.calls[3]  # 4th call: docker image inspect ...
    assert image_inspect_call.args[:3] == ["docker", "image", "inspect"]
    assert "awf-agent-runtime:latest" in image_inspect_call.args


@pytest.mark.unit
async def test_readyz_never_crashes_when_docker_completely_unavailable(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    """Even with every docker call exploding, /readyz must return a structured
    response (503 with per-check reasons) rather than a 500."""
    app, client = ready_app_and_client

    class _AlwaysExplodingRunner:
        async def run(
            self,
            args: list[str],
            *,
            input_bytes: bytes | None = None,
            cwd: str | None = None,
        ) -> CommandResult:
            raise OSError("no such file or directory")

    app.state.command_runner = _AlwaysExplodingRunner()

    response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    # DB check should still report OK (it doesn't touch docker).
    assert body["checks"]["db"]["ok"] is True
    # All docker-related checks fail with structured reasons, not tracebacks.
    for name in ("docker_cli", "docker_daemon", "docker_compose", "agent_runtime_image"):
        check = body["checks"][name]
        assert check["ok"] is False
        assert check["reason"] is not None


@pytest.mark.unit
async def test_readyz_terminal_workspace_with_live_container_reports_leak(
    ready_app_and_client: tuple[Any, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = ready_app_and_client
    workspace_id = await create_workspace(
        engine,
        status=WorkspaceStatus.failed,
        updated_at=datetime.now(UTC),
    )
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    runner.queue_result(
        stdout=json.dumps(
            {
                "id": "abc",
                "name": f"awf_{workspace_id}-agent-1",
                "project": f"awf_{workspace_id}",
                "service": "agent",
                "state": "running",
                "status": "Up 5 minutes",
            }
        )
        + "\n"
    )
    runner.queue_result(stdout="")
    runner.queue_result(stdout="")
    app.state.command_runner = runner

    response = await client.get("/readyz")
    body = response.json()

    assert response.status_code == 503
    orphan_check = body["checks"]["orphan_resources"]
    assert orphan_check["ok"] is False
    assert orphan_check["reason"] == "ORPHAN_RESOURCES_PRESENT"
    assert orphan_check["orphan_count"] == 1
    example = orphan_check["examples"][0]
    assert example["workspace_id"] == workspace_id
    assert example["classification"] == "terminal"
    assert example["reason"] == "WORKSPACE_TERMINAL_LIVE_RUNTIME"
