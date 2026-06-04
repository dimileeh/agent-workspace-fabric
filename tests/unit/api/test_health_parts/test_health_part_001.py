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
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.api.routes.health as health_route
import awf.service.provider_readiness as provider_readiness
from awf import __version__
from awf.api.app import configure_database, create_app
from awf.common.commands import AsyncioSubprocessRunner, CommandResult, FakeCommandRunner
from awf.common.config import Settings, get_settings
from awf.db.enums import WorkspaceStatus
from awf.db.models import WorkerHeartbeat
from awf.db.repositories import EgressAuditRepository, WorkerHeartbeatRepository
from awf.db.session import make_session_factory
from awf.service.readiness import CoreReadinessCheck, CoreReadinessReport
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


async def _seed_ready_worker_heartbeat(
    app: Any,
    *,
    worker_id: str = "readyz-test-worker",
    node_id: str = "local",
    last_heartbeat_at: datetime | None = None,
    poll_interval_seconds: float = 1.0,
) -> None:
    heartbeat_at = last_heartbeat_at or datetime.now(UTC)
    async with app.state.db_session_factory() as session:
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id=worker_id,
            node_id=node_id,
            started_at=heartbeat_at,
            last_heartbeat_at=heartbeat_at,
            poll_interval_seconds=poll_interval_seconds,
        )
        await session.commit()


async def _clear_worker_heartbeats(app: Any) -> None:
    async with app.state.db_session_factory() as session:
        await session.execute(delete(WorkerHeartbeat))
        await session.commit()


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
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    await _seed_ready_worker_heartbeat(app)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            c.headers["Authorization"] = "Bearer unit-test-api-token"
            yield app, c
    finally:
        original_get_settings.cache_clear()
        get_settings.cache_clear()


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


@pytest.mark.unit
async def test_release_readiness_returns_core_scorecard(
    ready_app_and_client: tuple[Any, AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, client = ready_app_and_client
    calls: list[dict[str, object]] = []

    async def _collect(**kwargs: object) -> CoreReadinessReport:
        calls.append(kwargs)
        return CoreReadinessReport(
            status="ok",
            checks=(
                CoreReadinessCheck(
                    name="prd_slo_thresholds",
                    status="ok",
                    reason_code="PRD_SLO_THRESHOLDS_MET",
                    message="rolling PRD SLO thresholds meet Core release criteria",
                    evidence={"since_hours": 168},
                ),
            ),
            next_actions=(),
        )

    monkeypatch.setattr(health_route, "collect_core_readiness_report", _collect)

    response = await client.get(
        "/release-readiness",
        params={
            "provider": "codex",
            "failure_window_hours": 12,
            "slo_window_hours": 168,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"][0]["name"] == "prd_slo_thresholds"
    assert calls[0]["failure_window_hours"] == 12
    assert calls[0]["slo_window_hours"] == 168
    assert calls[0]["strict_providers"] == frozenset({"codex"})


@pytest.mark.unit
async def test_release_readiness_returns_503_when_scorecard_fails(
    ready_app_and_client: tuple[Any, AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, client = ready_app_and_client

    async def _collect(**_kwargs: object) -> CoreReadinessReport:
        return CoreReadinessReport(
            status="fail",
            checks=(
                CoreReadinessCheck(
                    name="recent_failure_taxonomy",
                    status="fail",
                    reason_code="GENERIC_FAILURE_REASON_BLOCKS_RELEASE",
                    message="recent failed workspaces include generic or unknown reasons",
                    evidence={},
                ),
            ),
            next_actions=("Classify or reconcile recent generic workspace failures.",),
        )

    monkeypatch.setattr(health_route, "collect_core_readiness_report", _collect)

    response = await client.get("/release-readiness")

    assert response.status_code == 503
    assert response.json()["status"] == "fail"


@pytest.mark.unit
async def test_release_readiness_invalid_provider_returns_422(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    _app, client = ready_app_and_client

    response = await client.get("/release-readiness", params={"provider": "bogus"})

    assert response.status_code == 422
    assert "unknown provider" in response.json()["detail"]


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
        "worker",
        "docker_cli",
        "docker_daemon",
        "docker_compose",
        "agent_runtime_image",
        "orphan_resources",
        "egress_audit",
    }
    for name, check in checks.items():
        assert check["ok"] is True, f"{name} should be ok"
        if name == "orphan_resources":
            assert check["status"] == "ok"
            assert check["reason"] == "NO_ORPHANS"
            assert check["orphan_count"] == 0
            assert check["cleanup_readiness"]["ready"] is True
        elif name == "egress_audit":
            assert check["status"] == "ok"
            assert check["reason"] is not None
        elif name == "worker":
            assert check["status"] == "ok"
            assert check["reason"] == "WORKER_HEARTBEAT_FRESH"
        else:
            assert check["status"] == "ok"
            assert check.get("reason") is None
    assert body["agent_readiness"]["status"] == "ok"
    assert set(body["agent_readiness"]["providers"]) == {
        "github",
        "codex",
        "claude_code",
        "cursor",
        "gemini",
        "opencode",
        "grok",
        "docker",
    }
    assert body["agent_readiness"]["security"]["status"] == "warning"
    assert "DOCKER_HOST_BROAD_CONTROL" in body["agent_readiness"]["security"]["reason_codes"]
    assert body["agent_readiness"]["providers"]["github"]["reason"] == ("GITHUB_TOKEN_ENV_MISSING")


@pytest.mark.unit
async def test_readyz_worker_heartbeat_fresh_returns_worker_ok(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    response = await client.get("/readyz")
    body = response.json()

    assert response.status_code == 200
    worker = body["checks"]["worker"]
    assert worker["ok"] is True
    assert worker["status"] == "ok"
    assert worker["reason"] == "WORKER_HEARTBEAT_FRESH"


@pytest.mark.unit
async def test_readyz_worker_heartbeat_uses_effective_service_node_id(
    ready_app_and_client: tuple[Any, AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, client = ready_app_and_client
    monkeypatch.setattr(
        health_route,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            host_home=str(tmp_path / "home"),
            work_dir=str(tmp_path / "work"),
            worker_node_id="   ",
        ),
    )
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    response = await client.get("/readyz")
    body = response.json()

    assert response.status_code == 200
    worker = body["checks"]["worker"]
    assert worker["ok"] is True
    assert worker["reason"] == "WORKER_HEARTBEAT_FRESH"
    assert worker["detail"] == "Latest worker heartbeat is fresh for node 'local'"


@pytest.mark.unit
async def test_readyz_worker_heartbeat_missing_returns_503(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    app, client = ready_app_and_client
    await _clear_worker_heartbeats(app)
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    response = await client.get("/readyz")
    body = response.json()

    assert response.status_code == 503
    assert body["status"] == "fail"
    worker = body["checks"]["worker"]
    assert worker["ok"] is False
    assert worker["status"] == "fail"
    assert worker["reason"] == "WORKER_HEARTBEAT_MISSING"


@pytest.mark.unit
async def test_readyz_worker_stale_heartbeat_returns_503(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    app, client = ready_app_and_client
    await _clear_worker_heartbeats(app)
    await _seed_ready_worker_heartbeat(
        app,
        worker_id="stale-worker",
        last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=60),
        poll_interval_seconds=1.0,
    )
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    response = await client.get("/readyz")
    body = response.json()

    assert response.status_code == 503
    assert body["status"] == "fail"
    worker = body["checks"]["worker"]
    assert worker["ok"] is False
    assert worker["status"] == "fail"
    assert worker["reason"] == "WORKER_HEARTBEAT_STALE"


@pytest.mark.unit
async def test_worker_heartbeat_check_timeout_uses_unavailable_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session:
        async def close(self) -> None:
            return None

    class _SlowWorkerHeartbeatRepository:
        def __init__(self, _session: _Session) -> None:
            pass

        async def latest_for_node(self, *, node_id: str) -> None:
            del node_id
            await asyncio.sleep(1)

    monkeypatch.setattr(health_route, "_CHECK_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(
        health_route,
        "WorkerHeartbeatRepository",
        _SlowWorkerHeartbeatRepository,
    )

    result = await health_route._check_worker_heartbeat(lambda: _Session(), node_id="local")

    assert result.ok is False
    assert result.reason == "WORKER_HEARTBEAT_UNAVAILABLE"
    assert "exceeded" in (result.detail or "")


@pytest.mark.unit
async def test_worker_heartbeat_check_query_failure_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session:
        async def close(self) -> None:
            return None

    class _FailingWorkerHeartbeatRepository:
        def __init__(self, _session: _Session) -> None:
            pass

        async def latest_for_node(self, *, node_id: str) -> None:
            del node_id
            raise RuntimeError(
                "postgresql+asyncpg://awf:secret@db.internal:5432/awf "
                "Authorization: Bearer ghp_secret123"
            )

    monkeypatch.setattr(
        health_route,
        "WorkerHeartbeatRepository",
        _FailingWorkerHeartbeatRepository,
    )

    result = await health_route._check_worker_heartbeat(lambda: _Session(), node_id="local")

    assert result.ok is False
    assert result.reason == "WORKER_HEARTBEAT_UNAVAILABLE"
    assert "secret" not in (result.detail or "")
    assert "ghp_secret123" not in (result.detail or "")
    assert "postgresql+asyncpg://[redacted]@db.internal:5432/awf" in (result.detail or "")


@pytest.mark.unit
async def test_readyz_egress_audit_returns_posture_counts(
    ready_app_and_client: tuple[Any, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    now = datetime.now(UTC)
    factory = make_session_factory(engine)
    async with factory() as session:
        for posture, decision, destination in (
            ("open", "allow", "public_internet"),
            ("restricted", "deferred", "allowlisted_public"),
            ("offline", "deny", "internal_only"),
        ):
            workspace_id = await create_workspace(
                factory,
                status=WorkspaceStatus.running,
                updated_at=now,
                task_title=f"{posture} egress audited",
            )
            await EgressAuditRepository(session).create(
                workspace_id=workspace_id,
                attempt_id=None,
                policy_posture=posture,
                decision=decision,
                destination_category=destination,
                reason_code=f"LOCAL_EGRESS_{posture.upper()}_TEST",
                details={},
            )
        await session.commit()

    response = await client.get("/readyz")
    body = response.json()

    assert response.status_code == 200
    egress_audit = body["checks"]["egress_audit"]
    assert egress_audit["resource_count"] == 3
    assert egress_audit["egress_posture_counts"] == {
        "offline": 1,
        "open": 1,
        "restricted": 1,
    }


@pytest.mark.unit
async def test_readyz_egress_audit_timeout_is_degraded_not_failure(
    ready_app_and_client: tuple[Any, AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    class _TimeoutEgressAuditRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def summary_counts_by_posture(self) -> dict[str, int]:
            raise TimeoutError("egress audit timed out")

    monkeypatch.setattr(health_route, "EgressAuditRepository", _TimeoutEgressAuditRepository)

    response = await client.get("/readyz")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ok"
    egress_audit = body["checks"]["egress_audit"]
    assert egress_audit["ok"] is True
    assert egress_audit["status"] == "unknown"
    assert egress_audit["reason"] == "EGRESS_AUDIT_TIMEOUT"


@pytest.mark.unit
async def test_readyz_egress_audit_in_flight_uses_dedicated_reason(
    ready_app_and_client: tuple[Any, AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    async def _raise_in_flight(_factory: object, _state: object) -> dict[str, int]:
        raise health_route.EgressAuditSummaryInFlightError

    monkeypatch.setattr(
        health_route,
        "_egress_audit_summary_counts_with_timeout",
        _raise_in_flight,
    )

    response = await client.get("/readyz")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ok"
    egress_audit = body["checks"]["egress_audit"]
    assert egress_audit["ok"] is True
    assert egress_audit["status"] == "unknown"
    assert egress_audit["reason"] == "EGRESS_AUDIT_IN_FLIGHT"
    assert egress_audit["detail"] == "Previous egress audit summary_counts is still in flight"


@pytest.mark.unit
async def test_readyz_egress_audit_timeout_drains_completed_session_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = asyncio.Event()

    class _Session:
        async def close(self) -> None:
            await asyncio.sleep(0)
            closed.set()

    class _SlowEgressAuditRepository:
        def __init__(self, _session: _Session) -> None:
            pass

        async def summary_counts_by_posture(self) -> dict[str, int]:
            await asyncio.sleep(1)
            return {}

    monkeypatch.setattr(health_route, "_CHECK_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(health_route, "EgressAuditRepository", _SlowEgressAuditRepository)
    state = SimpleNamespace()

    with pytest.raises(TimeoutError, match="egress audit summary timed out"):
        await health_route._egress_audit_summary_counts_with_timeout(_Session, state)

    assert closed.is_set()


@pytest.mark.unit
async def test_egress_audit_summary_timeout_gates_pending_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_created = 0
    summary_calls = 0
    closed_sessions = 0
    release = asyncio.Event()
    all_sessions_closed = asyncio.Event()

    class _Session:
        def __init__(self) -> None:
            nonlocal sessions_created
            sessions_created += 1

        async def close(self) -> None:
            nonlocal closed_sessions
            closed_sessions += 1
            if closed_sessions == sessions_created:
                all_sessions_closed.set()

    class _CancellationResistantEgressAuditRepository:
        def __init__(self, _session: _Session) -> None:
            pass

        async def summary_counts_by_posture(self) -> dict[str, int]:
            nonlocal summary_calls
            summary_calls += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                raise

    monkeypatch.setattr(health_route, "_CHECK_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(health_route, "_EGRESS_AUDIT_CANCEL_DRAIN_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(
        health_route,
        "EgressAuditRepository",
        _CancellationResistantEgressAuditRepository,
    )
    state = SimpleNamespace()

    try:
        with pytest.raises(TimeoutError):
            await health_route._egress_audit_summary_counts_with_timeout(_Session, state)

        assert sessions_created == 1

        with pytest.raises(health_route.EgressAuditSummaryInFlightError):
            await health_route._egress_audit_summary_counts_with_timeout(_Session, state)

        assert sessions_created == 1
        assert summary_calls == 1
    finally:
        release.set()
        if sessions_created:
            await asyncio.wait_for(all_sessions_closed.wait(), timeout=1.0)


@pytest.mark.unit
async def test_egress_audit_summary_timeout_consumes_inner_task_on_outer_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    closed = asyncio.Event()

    class _Session:
        async def close(self) -> None:
            await asyncio.sleep(0)
            closed.set()

    class _HangingEgressAuditRepository:
        def __init__(self, _session: _Session) -> None:
            pass

        async def summary_counts_by_posture(self) -> dict[str, int]:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    monkeypatch.setattr(health_route, "EgressAuditRepository", _HangingEgressAuditRepository)
    monkeypatch.setattr(health_route, "_EGRESS_AUDIT_CANCEL_DRAIN_TIMEOUT_SECONDS", 1.0)
    state = SimpleNamespace()

    task = asyncio.create_task(
        health_route._egress_audit_summary_counts_with_timeout(_Session, state)
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
        assert closed.is_set()
        await asyncio.wait_for(cancelled.wait(), timeout=1.0)
    finally:
        await asyncio.wait_for(cancelled.wait(), timeout=1.0)
        await asyncio.wait_for(closed.wait(), timeout=1.0)


@pytest.mark.unit
async def test_egress_audit_summary_invalidates_transient_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _Session:
        async def invalidate(self) -> None:
            events.append("invalidate")

        async def rollback(self) -> None:
            events.append("rollback")

        async def close(self) -> None:
            events.append("close")

    class _FailingEgressAuditRepository:
        def __init__(self, _session: _Session) -> None:
            pass

        async def summary_counts_by_posture(self) -> dict[str, int]:
            raise _closed_connection_error()

    monkeypatch.setattr(health_route, "EgressAuditRepository", _FailingEgressAuditRepository)

    with pytest.raises(InterfaceError, match="connection is closed"):
        await health_route._egress_audit_summary_counts(_Session)

    assert events == ["invalidate", "close", "invalidate", "close"]


@pytest.mark.unit
async def test_egress_audit_summary_retries_transient_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    attempts = 0

    class _Session:
        def __init__(self) -> None:
            nonlocal attempts
            attempts += 1
            self.attempt = attempts
            events.append(f"open-{self.attempt}")

        async def invalidate(self) -> None:
            events.append(f"invalidate-{self.attempt}")

        async def rollback(self) -> None:
            events.append(f"rollback-{self.attempt}")

        async def close(self) -> None:
            events.append(f"close-{self.attempt}")

    class _RetryingEgressAuditRepository:
        def __init__(self, session: _Session) -> None:
            self._session = session

        async def summary_counts_by_posture(self) -> dict[str, int]:
            if self._session.attempt == 1:
                raise _closed_connection_error()
            return {"restricted": 2}

    monkeypatch.setattr(health_route, "EgressAuditRepository", _RetryingEgressAuditRepository)

    result = await health_route._egress_audit_summary_counts(_Session)

    assert result == {"restricted": 2}
    assert events == ["open-1", "invalidate-1", "close-1", "open-2", "close-2"]


@pytest.mark.unit
async def test_egress_audit_summary_timeout_not_delayed_by_session_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health_route, "_CHECK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(health_route, "_EGRESS_AUDIT_CANCEL_DRAIN_TIMEOUT_SECONDS", 0.001)
    close_started = asyncio.Event()
    close_released = asyncio.Event()

    class _Session:
        slow_close = False

        async def close(self) -> None:
            if self.slow_close:
                close_started.set()
                try:
                    await close_released.wait()
                except asyncio.CancelledError:
                    await close_released.wait()

    class _TimeoutEgressAuditRepository:
        def __init__(self, session: _Session) -> None:
            session.slow_close = True

        async def summary_counts_by_posture(self) -> dict[str, int]:
            raise TimeoutError("egress audit timed out")

    monkeypatch.setattr(health_route, "EgressAuditRepository", _TimeoutEgressAuditRepository)
    state = SimpleNamespace()

    try:
        with pytest.raises(TimeoutError, match="egress audit summary timed out"):
            await asyncio.wait_for(
                health_route._egress_audit_summary_counts_with_timeout(_Session, state),
                timeout=1.0,
            )

        assert close_started.is_set()
    finally:
        close_released.set()
        task = getattr(state, health_route._EGRESS_AUDIT_SUMMARY_COUNTS_TASK_STATE_ATTR, None)
        if task is not None:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1.0)


@pytest.mark.unit
async def test_readyz_egress_audit_error_is_degraded_not_failure(
    ready_app_and_client: tuple[Any, AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = ready_app_and_client
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    class _FailingEgressAuditRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def summary_counts_by_posture(self) -> dict[str, int]:
            raise RuntimeError(
                "could not connect to "
                "postgresql+asyncpg://awf:db_password@db.internal:5432/awf; "
                "Authorization: Bearer ghp_secret1234567890; "
                f"{'x' * 400}"
            )

    monkeypatch.setattr(health_route, "EgressAuditRepository", _FailingEgressAuditRepository)

    response = await client.get("/readyz")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ok"
    egress_audit = body["checks"]["egress_audit"]
    assert egress_audit["ok"] is True
    assert egress_audit["status"] == "unknown"
    assert egress_audit["reason"] == "EGRESS_AUDIT_UNAVAILABLE"
    assert len(egress_audit["detail"]) <= 240
    assert "db_password" not in egress_audit["detail"]
    assert "ghp_secret1234567890" not in egress_audit["detail"]
    assert "postgresql+asyncpg://[redacted]@db.internal:5432/awf" in egress_audit["detail"]
    assert "Authorization: Bearer [redacted]" in egress_audit["detail"]


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
    assert body["agent_readiness"]["providers"]["github"]["reason"] == ("GITHUB_TOKEN_ENV_MISSING")


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
    assert any(warning["reason"] == "DOCKER_HOST_BROAD_CONTROL" for warning in docker["warnings"])


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
def test_readyz_retention_defaults_use_gc_retention_constant() -> None:
    source = inspect.getsource(health_route)
    tree = ast.parse(source)
    expected_default_name = "DEFAULT_MIN_AGE_HOURS"
    expected_functions = {
        "_workspace_view_for_readyz",
        "_check_orphan_resources",
    }

    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name in expected_functions
    }

    assert set(functions) == expected_functions
    for function in functions.values():
        defaults_by_name = dict(
            zip(
                [arg.arg for arg in function.args.kwonlyargs],
                function.args.kw_defaults,
                strict=True,
            )
        )
        assert isinstance(defaults_by_name["min_retention_hours"], ast.Name)
        assert defaults_by_name["min_retention_hours"].id == expected_default_name


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
async def test_readyz_db_closed_connection_returns_specific_diagnostic(
    ready_app_and_client: tuple[Any, AsyncClient],
) -> None:
    app, client = ready_app_and_client
    select_one_session_events: list[list[str]] = []
    runner = FakeCommandRunner()
    _queue_all_ok(runner)
    app.state.command_runner = runner

    class _ClosedConnectionSession:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def invalidate(self) -> None:
            self.events.append("invalidate")

        async def execute(self, *args: object, **_kwargs: object) -> None:
            if args and str(args[0]) == "SELECT 1":
                select_one_session_events.append(self.events)
                raise _closed_connection_error()
            raise RuntimeError("query failed")

        async def close(self) -> None:
            self.events.append("close")

    app.state.db_session_factory = lambda: _ClosedConnectionSession()

    response = await client.get("/readyz")
    body = response.json()

    assert response.status_code == 503
    assert body["status"] == "fail"
    db_check = body["checks"]["db"]
    assert db_check["ok"] is False
    assert db_check["reason"] == "DB_CONNECTION_CLOSED"
    assert "connection is closed" in (db_check["detail"] or "")
    assert select_one_session_events == [["invalidate", "close"]]


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
    events: list[str] = []

    class _SlowSession:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            await asyncio.sleep(1)

        async def rollback(self) -> None:
            events.append("rollback")

        async def close(self) -> None:
            events.append("close")

    previous_timeout = health_route._CHECK_TIMEOUT_SECONDS
    health_route._CHECK_TIMEOUT_SECONDS = 0.001
    try:
        result = await health_route._check_db(lambda: _SlowSession())
    finally:
        health_route._CHECK_TIMEOUT_SECONDS = previous_timeout

    assert result.ok is False
    assert result.reason == "DB_TIMEOUT"
    assert "SELECT 1 exceeded" in (result.detail or "")
    assert events == ["rollback", "close"]


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
