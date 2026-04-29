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

import ast
import asyncio
import inspect
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.api.routes.health as health_route
import awf.service.provider_readiness as provider_readiness
from awf import __version__
from awf.api.app import configure_database, create_app
from awf.common.commands import AsyncioSubprocessRunner, CommandResult, FakeCommandRunner
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

# ---- /healthz ---------------------------------------------------------------


@pytest.mark.unit
async def test_healthz_returns_200(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200


@pytest.mark.unit
async def test_healthz_returns_expected_json_shape(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    body = response.json()

    assert body == {"status": "ok", "service": "awf", "version": __version__}


@pytest.mark.unit
async def test_healthz_does_not_require_auth(client: AsyncClient) -> None:
    """Liveness probes must be reachable without credentials.

    Uptime monitors and cluster health checks don't authenticate; a 401/403 on this
    endpoint would cause false outage alerts.
    """
    response = await client.get("/healthz")
    assert response.status_code != 401
    assert response.status_code != 403


# ---- /readyz fixtures -------------------------------------------------------


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
    monkeypatch.setenv("AWF_HOST_HOME", str(tmp_path / "home"))
    health_route.get_settings.cache_clear()
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield app, c
    finally:
        health_route.get_settings.cache_clear()


# ---- /readyz: happy path ----------------------------------------------------


@pytest.mark.unit
async def test_readyz_all_ok_returns_200(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    response = await client.get("/readyz")
    assert response.status_code == 200


@pytest.mark.unit
async def test_readyz_response_shape_matches_contract(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    """Operators need a stable shape: service / version / status + per-check map."""
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    response = await client.get("/readyz")
    body = response.json()

    assert body["service"] == "awf"
    assert body["version"] == __version__
    assert body["status"] == "ok"

    checks = body["checks"]
    assert set(checks.keys()) == {
        "db",
        "docker_cli",
        "docker_daemon",
        "docker_compose",
        "agent_runtime_image",
        "orphan_resources",
    }
    for name, check in checks.items():
        assert check["ok"] is True, f"{name} should be ok"
        if name == "orphan_resources":
            assert check["status"] == "ok"
            assert check["reason"] == "NO_ORPHANS"
            assert check["orphan_count"] == 0
            assert check["cleanup_readiness"]["ready"] is True
        else:
            assert check["status"] == "ok"
            assert check.get("reason") is None
    assert body["agent_readiness"]["status"] == "ok"
    assert set(body["agent_readiness"]["providers"]) == {
        "github",
        "codex",
        "claude_code",
        "gemini",
        "opencode",
        "docker",
    }
    assert body["agent_readiness"]["security"]["status"] == "warning"
    assert "DOCKER_HOST_BROAD_CONTROL" in body["agent_readiness"]["security"]["reason_codes"]
    assert body["agent_readiness"]["providers"]["github"]["reason"] == (
        "GITHUB_TOKEN_ENV_MISSING"
    )


@pytest.mark.unit
async def test_readyz_provider_warnings_remain_200(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    response = await client.get("/readyz")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["agent_readiness"]["status"] == "ok"
    assert body["agent_readiness"]["providers"]["github"]["status"] == "warn"


@pytest.mark.unit
async def test_readyz_strict_github_provider_missing_auth_returns_503(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    response = await client.get("/readyz?provider=github")
    body = response.json()

    assert response.status_code == 503
    assert body["status"] == "fail"
    assert body["agent_readiness"]["status"] == "fail"
    assert body["agent_readiness"]["providers"]["github"]["status"] == "fail"
    assert body["agent_readiness"]["providers"]["github"]["reason"] == (
        "GITHUB_TOKEN_ENV_MISSING"
    )


@pytest.mark.unit
async def test_readyz_strict_codex_provider_missing_auth_returns_503(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    response = await client.get("/readyz?provider=codex")
    body = response.json()

    assert response.status_code == 503
    assert body["status"] == "fail"
    assert body["agent_readiness"]["status"] == "fail"
    assert body["agent_readiness"]["providers"]["codex"]["status"] == "fail"
    assert body["agent_readiness"]["providers"]["codex"]["reason"] == "CODEX_AUTH_MISSING"


@pytest.mark.unit
async def test_readyz_docker_provider_is_validated_and_reports_security_metadata(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    response = await client.get("/readyz?provider=docker")
    body = response.json()

    assert response.status_code == 200
    assert body["checks"]["docker_daemon"]["status"] == "ok"
    docker = body["agent_readiness"]["providers"]["docker"]
    assert docker["status"] == "ok"
    assert docker["credential_scope"] == "docker_host_control"
    assert any(
        warning["reason"] == "DOCKER_HOST_BROAD_CONTROL"
        for warning in docker["warnings"]
    )


@pytest.mark.unit
async def test_readyz_reuses_validated_provider_names(
    ready_app_and_client: tuple[Any, AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner
    validation_calls: list[tuple[str, ...]] = []
    original_validate = provider_readiness.validate_provider_names

    def _count_validation(values: Any) -> set[provider_readiness.ProviderName]:
        validation_calls.append(tuple(values))
        return original_validate(values)

    monkeypatch.setattr(health_route, "validate_provider_names", _count_validation)
    monkeypatch.setattr(provider_readiness, "validate_provider_names", _count_validation)

    response = await client.get("/readyz?provider=github")

    assert response.status_code == 503
    assert validation_calls == [("github",)]


@pytest.mark.unit
async def test_readyz_starts_orphan_scan_before_slow_peer_checks_finish(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    app, client = ready_app_and_client

    class _OverlappingRunner:
        def __init__(self) -> None:
            self.orphan_scan_started = asyncio.Event()
            self.peer_check_saw_orphan_scan = False

        async def run(
            self,
            args: list[str],
            *,
            input_bytes: bytes | None = None,
            cwd: str | None = None,
        ) -> CommandResult:
            del input_bytes, cwd
            if args == ["docker", "--version"]:
                return CommandResult(returncode=0, stdout="Docker version 27.0.3\n", stderr="")
            if args[:2] == ["docker", "info"]:
                return CommandResult(returncode=0, stdout="27.0.3\n", stderr="")
            if args[:3] == ["docker", "compose", "version"] or args[:3] == [
                "docker",
                "image",
                "inspect",
            ]:
                try:
                    await asyncio.wait_for(self.orphan_scan_started.wait(), timeout=0.05)
                    self.peer_check_saw_orphan_scan = True
                except TimeoutError:
                    pass
                if args[:3] == ["docker", "compose", "version"]:
                    stdout = "v2.29.2\n"
                else:
                    stdout = "sha256:deadbeef\n"
                return CommandResult(returncode=0, stdout=stdout, stderr="")
            if args[:3] in (
                ["docker", "ps", "-a"],
                ["docker", "network", "ls"],
                ["docker", "volume", "ls"],
            ):
                self.orphan_scan_started.set()
                return CommandResult(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected docker call: {args}")

    runner = _OverlappingRunner()
    app.state.command_runner = runner

    response = await client.get("/readyz")

    assert response.status_code == 200
    assert runner.peer_check_saw_orphan_scan is True


@pytest.mark.unit
def test_readyz_does_not_force_task_scheduling_with_zero_sleep() -> None:
    source = inspect.getsource(health_route.readyz)
    tree = ast.parse(source)

    zero_sleep_yields = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "sleep"
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "asyncio"
        and len(node.value.args) == 1
        and isinstance(node.value.args[0], ast.Constant)
        and node.value.args[0].value == 0
    ]

    assert not zero_sleep_yields, "readyz should schedule top-level checks before awaiting"


@pytest.mark.unit
async def test_readyz_invalid_provider_returns_422(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    response = await client.get("/readyz?provider=unknown")

    assert response.status_code == 422


@pytest.mark.unit
async def test_readyz_reports_versions_when_available(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    """Surfacing versions helps operators verify the right Docker / image is rolled out."""
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    response = await client.get("/readyz")
    checks = response.json()["checks"]

    assert "27.0.3" in checks["docker_cli"]["version"]
    assert checks["docker_daemon"]["version"] == "27.0.3"
    assert checks["docker_compose"]["version"] == "v2.29.2"
    assert checks["agent_runtime_image"]["version"] == "sha256:deadbeef"


# ---- /readyz: DB failures ---------------------------------------------------


@pytest.mark.unit
async def test_readyz_db_not_configured_returns_503(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    """If the session factory wasn't wired, the DB check must fail loudly."""
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner
    app.state.db_session_factory = None  # Simulate misconfigured deploy.

    response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "fail"
    db_check = body["checks"]["db"]
    assert db_check["ok"] is False
    assert db_check["reason"] == "DB_NOT_CONFIGURED"


@pytest.mark.unit
async def test_readyz_db_query_failure_returns_503(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    """A live DB outage should be surfaced as a structured failure, not a 500."""
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    class _ExplodingSession:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("connection refused: control-plane DB is down")

        async def close(self) -> None:
            return None

    def _factory() -> _ExplodingSession:
        return _ExplodingSession()

    app.state.db_session_factory = _factory

    response = await client.get("/readyz")
    assert response.status_code == 503
    db_check = response.json()["checks"]["db"]
    assert db_check["ok"] is False
    assert db_check["reason"] == "DB_CONNECTION_FAILED"
    assert "connection refused" in (db_check["detail"] or "")
    orphan_check = response.json()["checks"]["orphan_resources"]
    assert orphan_check["ok"] is True
    assert orphan_check["status"] == "unknown"
    assert orphan_check["reason"] == "DB_UNAVAILABLE"


@pytest.mark.unit
async def test_readyz_db_factory_raises_returns_503(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    """Factory failure (pool exhausted, bad DSN) must surface as 503, not 500."""
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    def _exploding_factory() -> Any:
        raise RuntimeError("QueuePool limit of size 5 overflow 10 reached")

    app.state.db_session_factory = _exploding_factory

    response = await client.get("/readyz")
    assert response.status_code == 503
    db_check = response.json()["checks"]["db"]
    assert db_check["ok"] is False
    assert db_check["reason"] == "DB_CONNECTION_FAILED"
    assert "QueuePool" in (db_check["detail"] or "")


@pytest.mark.unit
def test_readyz_command_runner_falls_back_to_asyncio_subprocess_runner() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    runner = health_route._get_command_runner_for_request(request)  # type: ignore[arg-type]

    assert isinstance(runner, AsyncioSubprocessRunner)


@pytest.mark.unit
def test_readyz_truncates_verbose_dependency_details() -> None:
    detail = "x" * 20

    truncated = health_route._truncate(detail, limit=8)

    assert len(truncated) == 8
    assert truncated.startswith("x" * 7)
    assert truncated.endswith("\N{HORIZONTAL ELLIPSIS}")


@pytest.mark.unit
async def test_readyz_db_timeout_returns_structured_failure() -> None:
    class _SlowSession:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            await asyncio.sleep(1)

        async def close(self) -> None:
            return None

    previous_timeout = health_route._CHECK_TIMEOUT_SECONDS
    health_route._CHECK_TIMEOUT_SECONDS = 0.001
    try:
        result = await health_route._check_db(lambda: _SlowSession())
    finally:
        health_route._CHECK_TIMEOUT_SECONDS = previous_timeout

    assert result.ok is False
    assert result.reason == "DB_TIMEOUT"
    assert "SELECT 1 exceeded" in (result.detail or "")


@pytest.mark.unit
async def test_readyz_db_success_allows_sessions_without_close_method() -> None:
    class _SessionWithoutClose:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            return None

    result = await health_route._check_db(lambda: _SessionWithoutClose())

    assert result.ok is True
    assert result.status == "ok"


@pytest.mark.unit
async def test_readyz_workspace_view_handles_factory_and_session_failures(
    tmp_path: Path,
) -> None:
    missing = await health_route._workspace_view_for_readyz(None)

    def _factory_raises() -> Any:
        raise RuntimeError("factory failed")

    factory_failed = await health_route._workspace_view_for_readyz(_factory_raises)

    class _BadSession:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("query failed")

        async def close(self) -> None:
            raise RuntimeError("close failed")

    session_failed = await health_route._workspace_view_for_readyz(lambda: _BadSession())
    orphan_check = await health_route._check_orphan_resources(
        runner=FakeCommandRunner(),
        factory=None,
        work_dir=str(tmp_path),
        db_check=health_route.CheckResult(ok=True, status="ok"),
        docker_check=health_route.CheckResult(
            ok=False,
            status="fail",
            reason="DOCKER_DAEMON_UNREACHABLE",
            detail="Cannot connect",
        ),
    )

    assert missing.available is False
    assert factory_failed.available is False
    assert session_failed.available is False
    assert orphan_check.ok is True
    assert orphan_check.status == "unknown"
    assert orphan_check.reason == "DB_UNAVAILABLE"


@pytest.mark.unit
async def test_readyz_orphan_check_scans_docker_even_when_db_check_failed(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    for _ in range(3):
        runner.queue_result(stdout="")

    orphan_check = await health_route._check_orphan_resources(
        runner=runner,
        factory=None,
        work_dir=str(tmp_path),
        db_check=health_route.CheckResult(
            ok=False,
            status="fail",
            reason="DB_CONNECTION_FAILED",
        ),
        docker_check=health_route.CheckResult(ok=True, status="ok"),
    )

    assert orphan_check.ok is True
    assert orphan_check.status == "unknown"
    assert orphan_check.reason == "DB_UNAVAILABLE"
    assert len(runner.calls) == 3


@pytest.mark.unit
async def test_readyz_cancel_unneeded_task_cancels_pending_task() -> None:
    task = asyncio.create_task(asyncio.sleep(60))

    await health_route._cancel_unneeded_task(task)

    assert task.cancelled()


@pytest.mark.unit
async def test_docker_check_maps_runner_timeout_to_configured_reason() -> None:
    class _SlowRunner:
        async def run(
            self,
            args: list[str],
            *,
            input_bytes: bytes | None = None,
            cwd: str | None = None,
        ) -> CommandResult:
            assert args == ["docker", "compose", "version", "--short"]
            assert input_bytes is None
            assert cwd is None
            await asyncio.sleep(1)
            return CommandResult(returncode=0, stdout="", stderr="")

    previous_timeout = health_route._CHECK_TIMEOUT_SECONDS
    health_route._CHECK_TIMEOUT_SECONDS = 0.001
    try:
        result = await health_route._docker_check(
            _SlowRunner(),
            args=["docker", "compose", "version", "--short"],
            description="docker compose version",
            fail_reason="DOCKER_COMPOSE_NOT_AVAILABLE",
            timeout_reason="DOCKER_COMPOSE_TIMEOUT",
        )
    finally:
        health_route._CHECK_TIMEOUT_SECONDS = previous_timeout

    assert result.ok is False
    assert result.reason == "DOCKER_COMPOSE_TIMEOUT"
    assert "docker compose version exceeded" in (result.detail or "")


# ---- /readyz: Docker CLI / daemon failures ----------------------------------


@pytest.mark.unit
async def test_readyz_docker_cli_missing_returns_503(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    """When the docker binary is absent, asyncio raises FileNotFoundError —
    the readiness check must catch it and report an actionable reason."""
    app, client = ready_app_and_client

    class _DockerMissingRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def run(
            self,
            args: list[str],
            *,
            input_bytes: bytes | None = None,
            cwd: str | None = None,
        ) -> CommandResult:
            self.calls.append(list(args))
            if args and args[0] == "docker":
                raise FileNotFoundError(args[0])
            return CommandResult(returncode=0, stdout="", stderr="")

    app.state.command_runner = _DockerMissingRunner()

    response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()

    cli_check = body["checks"]["docker_cli"]
    assert cli_check["ok"] is False
    assert cli_check["reason"] == "DOCKER_CLI_NOT_FOUND"

    # Daemon, compose, and image checks all depend on docker — they should also fail
    # (gracefully) rather than crash the request.
    assert body["checks"]["docker_daemon"]["ok"] is False
    assert body["checks"]["docker_compose"]["ok"] is False
    assert body["checks"]["agent_runtime_image"]["ok"] is False


@pytest.mark.unit
async def test_readyz_docker_daemon_unreachable_returns_503(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    runner.queue_result(stdout="Docker version 27.0.3, build abc\n")  # docker --version
    runner.queue_result(  # docker info
        returncode=1,
        stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock\n",
    )
    runner.queue_result(stdout="v2.29.2\n")
    runner.queue_result(stdout="sha256:deadbeef\n")
    app.state.command_runner = runner

    response = await client.get("/readyz")
    assert response.status_code == 503
    daemon = response.json()["checks"]["docker_daemon"]
    assert daemon["ok"] is False
    assert daemon["reason"] == "DOCKER_DAEMON_UNREACHABLE"
    assert "Cannot connect" in (daemon["detail"] or "")
    orphan_check = response.json()["checks"]["orphan_resources"]
    assert orphan_check["ok"] is True
    assert orphan_check["status"] == "unavailable"
    assert orphan_check["reason"] == "DOCKER_RESOURCE_SCAN_UNAVAILABLE"


@pytest.mark.unit
async def test_readyz_orphan_resources_present_returns_503(
    ready_app_and_client: tuple[Any, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = ready_app_and_client
    workspace_id = await create_workspace(
        engine,
        status=WorkspaceStatus.completed,
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
                "state": "exited",
                "status": "Exited (0)",
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
    assert body["status"] == "fail"
    orphan_check = body["checks"]["orphan_resources"]
    assert orphan_check["ok"] is False
    assert orphan_check["reason"] == "ORPHAN_RESOURCES_PRESENT"
    assert orphan_check["orphan_count"] == 1
    assert orphan_check["cleanup_readiness"]["dry_run_only"] is True
    assert orphan_check["examples"][0]["workspace_id"] == workspace_id


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


# ---- /readyz: never-crash contract ------------------------------------------


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
