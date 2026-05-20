"""Workspace API contract tests.

Each test runs against an isolated PostgreSQL schema via the ``client`` fixture.
These are *unit*-flavoured tests because they don't spin up Docker or Postgres;
true integration + E2E tests live under tests/integration/ and tests/e2e/.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request

import awf.api.request_admission as request_admission
import awf.api.routes.workspaces as workspaces_route
import awf.db.resilience as db_resilience
import awf.service.workspace_observability as workspace_observability
from awf.api.app import configure_database, create_app
from awf.api.deps import get_db_session
from awf.api.schemas import (
    PullRequestMonitorAdoptionRequest,
    WorkspaceCreateRequest,
)
from awf.common.config import Settings, get_settings
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    EgressAuditRepository,
    MergeCandidateRepository,
    SecretLeaseIssue,
    SecretLeaseRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service import workspaces as workspaces_service
from awf.service.disk import DiskCheck
from tests.unit.helpers import assert_no_internal_error_fields as _assert_no_internal_error_fields

pytestmark = pytest.mark.usefixtures("mock_docker_cli_probe")

_MINIMAL_BODY = {
    "repo_url": "git@github.com:dimileeh/aira-agent.git",
    "branch_base": "development",
    "task_title": "Add module docstring",
    "task_prompt": "Add a one-line docstring to src/aira_agent/api/main.py.",
    "agent": "codex",
    "test_commands": ["pytest -q"],
}


def _closed_connection_error() -> InterfaceError:
    return InterfaceError("SELECT 1", {}, RuntimeError("connection is closed"))


_V2_MINIMAL_BODY = {
    "repo": {
        "url": "git@github.com:dimileeh/aira-agent.git",
        "base_branch": "development",
    },
    "task": {
        "title": "Add module docstring",
        "prompt": "Add a one-line docstring to src/aira_agent/api/main.py.",
        "agent": "codex",
        "kind": "feature_branch_pr",
    },
    "workspace": {"profile_ref": "auto", "profile": None},
    "validation": {"commands": ["pytest -q"], "requested_tier": 1},
    "resources": {},
}


_PROVIDER_AUTH_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "CODEX_API_KEY",
    "CODEX_AUTH_TOKEN",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)

_WORKSPACE_API_TOKEN = "unit-test-workspace-api-token"
_WORKSPACE_AUTH_HEADER = f"Bearer {_WORKSPACE_API_TOKEN}"
_STABLE_REQUEST_ADMISSION_CLOCK = 1000.0


def _unique_idempotency_key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _install_stable_request_admission_limiter(state: object) -> None:
    setattr(
        state,
        request_admission._LIMITER_STATE_KEY,  # noqa: SLF001
        request_admission.RequestAdmissionLimiter(clock=lambda: _STABLE_REQUEST_ADMISSION_CLOCK),
    )


@pytest.mark.unit
def test_internal_error_field_assertion_allows_message_values() -> None:
    _assert_no_internal_error_fields(
        {
            "error_code": "IDEMPOTENCY_CONFLICT",
            "message": "Retry with the original idempotency_key.",
            "detail": {"external_id": "WAVE-1"},
        }
    )


@pytest.fixture(autouse=True)
def _provider_auth_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CODEX_AUTH_TOKEN", "unit-test-provider-token")
    monkeypatch.setenv("AWF_API_TOKEN", _WORKSPACE_API_TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(
    engine: AsyncEngine,
) -> AsyncIterator[AsyncClient]:
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    _install_stable_request_admission_limiter(app.state)
    app.state.workspace_admission_disk_check = lambda settings: _disk_check(
        free_bytes=settings.min_free_disk_bytes + 1,
        threshold_bytes=settings.min_free_disk_bytes,
        ok=True,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.headers["Authorization"] = _WORKSPACE_AUTH_HEADER
        yield c


def _set_codex_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_AUTH_TOKEN", "unit-test-provider-token")


def _endpoint_profile_body() -> dict[str, object]:
    return {
        "name": "api-endpoints",
        "runtime": {
            "environment": {"SECRET_URL": "http://user:password@app:3000/secret?token=abc"}
        },
        "services": [{"name": "app", "image": "example/app:latest"}],
        "app_endpoints": [
            {
                "name": "app",
                "service": "app",
                "port": 3000,
                "path": "/",
                "health": {"path": "/healthz"},
                "visibility": "agent",
            },
            {
                "name": "operator_notes",
                "service": "app",
                "port": 3000,
                "path": "/operator",
                "visibility": "console",
            },
            {
                "name": "internal_metrics",
                "service": "app",
                "port": 3000,
                "path": "/metrics",
                "visibility": "internal",
            },
        ],
        "secrets": [
            {
                "name": "api-token",
                "target": "API_TOKEN",
                "kind": "env",
                "provider": "vault",
                "ref": "secret/data/api-token",
            }
        ],
    }


def _v2_body(
    *,
    repo_url: str = "git@github.com:example/app.git",
    base_branch: str = "development",
    title: str = "Owned path policy test",
    task_class: str | None = None,
    owned_paths: list[str] | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> dict[str, object]:
    task = {
        **_V2_MINIMAL_BODY["task"],
        "title": title,
    }
    if task_class is not None:
        task["task_class"] = task_class
    if owned_paths is not None:
        task["owned_paths"] = owned_paths
    if model is not None:
        task["model"] = model
    if effort is not None:
        task["effort"] = effort
    return {
        **_V2_MINIMAL_BODY,
        "repo": {
            "url": repo_url,
            "base_branch": base_branch,
        },
        "task": task,
    }


def _v2_body_with_preflight_override(**kwargs: object) -> dict[str, object]:
    return {
        **_v2_body(**kwargs),
        "preflight": {
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "unit test bypasses provider auth",
        },
    }


@pytest.mark.unit
def test_v2_replay_helpers_preserve_provider_recovery_and_missing_preflight() -> None:
    body = json.loads(json.dumps(_V2_MINIMAL_BODY))
    body["task"]["provider_recovery"] = {
        "fallbacks": [
            {
                "agent": "opencode",
                "provider": "ollama",
                "model": "ollama/kimi-k2.6:cloud",
            }
        ],
        "max_fallback_attempts": 1,
    }
    payload = WorkspaceCreateRequest.model_validate(body)
    existing = SimpleNamespace(task_policy={})

    assert workspaces_service._requested_task_provider_recovery_policy(payload) == {  # noqa: SLF001
        "fallbacks": [
            {
                "agent": "opencode",
                "provider": "ollama",
                "model": "ollama/kimi-k2.6:cloud",
            }
        ],
        "max_fallback_attempts": 1,
    }
    assert workspaces_service._stored_task_provider_readiness_override(existing) == (  # noqa: SLF001
        False,
        None,
    )


@pytest.mark.unit
def test_provider_readiness_override_reason_match_redaction_edges() -> None:
    assert workspaces_service._override_reasons_match("same", "same") is True  # noqa: SLF001
    assert workspaces_service._override_reasons_match(None, "reason") is False  # noqa: SLF001
    assert workspaces_service._override_reasons_match("reason", None) is False  # noqa: SLF001
    assert workspaces_service._override_reasons_match("stored", "requested") is False  # noqa: SLF001
    assert (
        workspaces_service._override_reasons_match(  # noqa: SLF001
            "stored",
            "prefix-secret-suffix",
            stored_redaction_parts=["prefix-", "-suffix"],
        )
        is False
    )

    missing_preflight = SimpleNamespace(task_policy={})
    malformed_parts = SimpleNamespace(
        task_policy={
            "provider_readiness_preflight": {"override_reason_redaction_parts": ["prefix-only"]}
        }
    )
    non_string_parts = SimpleNamespace(
        task_policy={
            "provider_readiness_preflight": {
                "override_reason_redaction_parts": ["prefix-", 42, "-suffix"]
            }
        }
    )

    assert (
        workspaces_service._stored_task_provider_readiness_override_redaction_parts(  # noqa: SLF001
            missing_preflight
        )
        is None
    )
    assert (
        workspaces_service._stored_task_provider_readiness_override_redaction_parts(  # noqa: SLF001
            malformed_parts
        )
        is None
    )
    assert (
        workspaces_service._stored_task_provider_readiness_override_redaction_parts(  # noqa: SLF001
            non_string_parts
        )
        is None
    )


@pytest.mark.unit
async def test_workspace_stale_reasons_route_maps_invalid_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_invalid_cursor(*_args: Any, **_kwargs: Any) -> object:
        raise workspaces_route.InvalidBoundedListCursorError("bad cursor")

    monkeypatch.setattr(
        workspaces_route,
        "list_workspace_stale_reasons_response",
        _raise_invalid_cursor,
    )

    with pytest.raises(HTTPException) as excinfo:
        await workspaces_route.list_workspace_stale_reasons(
            "ws_test",
            cursor="bad",
            session=object(),  # type: ignore[arg-type]
        )

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == {
        "error_code": "INVALID_CURSOR",
        "message": "Invalid stale reason cursor.",
    }


@pytest.mark.unit
@pytest.mark.parametrize("authorization", ["Bearer wrong-token", None])
async def test_workspace_metadata_routes_require_authorization(
    client: AsyncClient,
    authorization: str | None,
) -> None:
    workspace_id = await _create_workspace(client)
    default_authorization = client.headers.get("Authorization")
    checks = [
        ("POST", "/v1/workspaces", _MINIMAL_BODY),
        ("POST", "/v1/workspaces", _v2_body()),
        ("GET", "/v1/workspaces", None),
        ("GET", f"/v1/workspaces/{workspace_id}", None),
        ("GET", "/v1/workspaces/overview", None),
        ("GET", f"/v1/workspaces/{workspace_id}/events", None),
        ("GET", f"/v1/workspaces/{workspace_id}/stale-reasons", None),
        ("GET", f"/v1/workspaces/{workspace_id}/secret-leases", None),
    ]

    for method, path, body in checks:
        kwargs: dict[str, object] = {}
        sent_wrong_token = False
        if authorization is not None:
            kwargs["headers"] = {"Authorization": authorization}
            sent_wrong_token = True
        if body is not None:
            kwargs["json"] = body
        try:
            if not sent_wrong_token and default_authorization is not None:
                del client.headers["Authorization"]
            response = await client.request(method, path, **kwargs)
        finally:
            if not sent_wrong_token and default_authorization is not None:
                client.headers["Authorization"] = default_authorization
        assert response.status_code == 401, (authorization, method, path, response.text)


@pytest.mark.unit
async def test_workspace_metadata_routes_accept_authorized_requests(client: AsyncClient) -> None:
    workspace_id = await _create_workspace(client)

    assert (await client.post("/v1/workspaces", json=_MINIMAL_BODY)).status_code == 202
    assert (
        await client.post(
            "/v1/workspaces",
            json=_v2_body(owned_paths=["src/**", "tests/**"]),
        )
    ).status_code == 202

    assert (await client.get("/v1/workspaces", params={"limit": 1})).status_code == 200
    assert (await client.get("/v1/workspaces/overview")).status_code == 200
    assert (await client.get(f"/v1/workspaces/{workspace_id}")).status_code == 200
    assert (await client.get(f"/v1/workspaces/{workspace_id}/events")).status_code == 200
    assert (await client.get(f"/v1/workspaces/{workspace_id}/stale-reasons")).status_code == 200
    assert (await client.get(f"/v1/workspaces/{workspace_id}/secret-leases")).status_code == 200


@pytest.mark.unit
async def test_adopt_pr_route_maps_service_errors_to_json_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _FailingAdoptionService:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def adopt(self, _payload: PullRequestMonitorAdoptionRequest) -> object:
            raise workspaces_route.PRMonitorAdoptionError(
                error_code="PR_ALREADY_CLOSED",
                message="PR is closed.",
                status_code=409,
                detail={"repo_slug": "x/y", "pr_number": 42},
            )

    monkeypatch.setattr(
        workspaces_route,
        "PullRequestMonitorAdoptionService",
        _FailingAdoptionService,
    )

    response = await workspaces_route.adopt_pull_request_monitor(
        PullRequestMonitorAdoptionRequest(repo_slug="x/y", pr_number=42),
        request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        settings=Settings(_env_file=None, work_dir=str(tmp_path / "awf-state")),
        session=object(),  # type: ignore[arg-type]
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 409
    assert json.loads(response.body) == {
        "error_code": "PR_ALREADY_CLOSED",
        "message": "PR is closed.",
        "detail": {"repo_slug": "x/y", "pr_number": 42},
    }


async def _create_workspace(
    client: AsyncClient,
    *,
    repo_url: str = "git@github.com:example/app.git",
    base_branch: str = "development",
    title: str = "Owned path policy test",
    task_title: str | None = None,
    agent: str = "codex",
    task_class: str | None = None,
    owned_paths: list[str] | None = None,
) -> str:
    body = _v2_body_with_preflight_override(
        repo_url=repo_url,
        base_branch=base_branch,
        title=task_title or title,
        task_class=task_class,
        owned_paths=owned_paths,
    )
    body["task"]["agent"] = agent
    response = await client.post(
        "/v1/workspaces",
        json=body,
    )
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


def _disk_check(
    *,
    free_bytes: int,
    threshold_bytes: int,
    ok: bool,
) -> DiskCheck:
    return DiskCheck(
        path="/workspace/.awf",
        checked_path="/workspace",
        total_bytes=1000,
        used_bytes=1000 - free_bytes,
        free_bytes=free_bytes,
        percent_free=free_bytes / 10,
        threshold_bytes=threshold_bytes,
        ok=ok,
        status="ok" if ok else "fail",
        reason="SUFFICIENT_DISK" if ok else "INSUFFICIENT_DISK",
        detail=None if ok else "Free disk is below AWF_MIN_FREE_DISK_BYTES.",
    )


def _request_with_disk_check() -> SimpleNamespace:
    state = SimpleNamespace(
        workspace_admission_disk_check=lambda settings: _disk_check(
            free_bytes=settings.min_free_disk_bytes + 1,
            threshold_bytes=settings.min_free_disk_bytes,
            ok=True,
        )
    )
    _install_stable_request_admission_limiter(state)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _workspace_request_without_app_state() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/workspaces",
            "headers": [],
            "client": ("198.51.100.52", 42100),
        }
    )


class _TrackingLock:
    def __init__(self) -> None:
        self.enters = 0
        self.exits = 0

    def __enter__(self) -> None:
        self.enters += 1

    def __exit__(self, *_exc_info: object) -> None:
        self.exits += 1


def _provider_preflight_settings(tmp_path: Any) -> Settings:
    return Settings(
        _env_file=None,
        host_home=str(tmp_path / "home"),
        work_dir=str(tmp_path),
        min_free_disk_bytes=0,
        docker_host="",
    )


def _workspace_request_admission_settings(*, limit: int = 1) -> Settings:
    return Settings(
        _env_file=None,
        api_token=_WORKSPACE_API_TOKEN,
        request_admission_window_seconds=60,
        workspace_create_rate_limit_count=limit,
        callback_register_rate_limit_count=20,
    )


def _assert_workspace_rate_limited(response: Any) -> None:
    assert response.status_code == 429
    body = response.json()
    assert body["error_code"] == "WORKSPACE_CREATE_RATE_LIMITED"
    assert body["message"] == "Workspace creation request rate limit exceeded."
    detail = body["detail"]
    assert detail["reason_code"] == "WORKSPACE_CREATE_RATE_LIMITED"
    assert detail["endpoint_family"] == "workspace_create"
    assert detail["identity_type"] == "bearer_token"
    assert detail["identity_digest"]
    assert detail["limit"] == 1
    assert detail["window_seconds"] == 60
    assert detail["retry_after_seconds"] > 0
    assert response.headers["Retry-After"] == str(detail["retry_after_seconds"])
    assert _WORKSPACE_API_TOKEN not in json.dumps(body)
    assert _WORKSPACE_AUTH_HEADER not in json.dumps(body)


def _clear_workspace_create_replay_key_cache(app: Any) -> None:
    setattr(
        app.state,
        workspaces_route._WORKSPACE_CREATE_REPLAY_KEY_CACHE_STATE_KEY,  # noqa: SLF001
        workspaces_route._new_workspace_create_idempotency_replay_key_cache(),  # noqa: SLF001
    )


def _assert_effective_identity(
    row: dict[str, Any],
    *,
    model: str,
    effort: str = "xhigh",
    model_source: str = "default",
    effort_source: str = "default",
) -> None:
    assert row["agent_model"] == model
    assert row["agent_effort"] == effort
    assert row["agent_model_source"] == model_source
    assert row["agent_effort_source"] == effort_source


def _assert_usage_unavailable(row: dict[str, Any]) -> None:
    assert row["llm_usage"] == {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost_estimate": None,
        "currency": None,
        "status": "unavailable",
        "source": "none",
        "reason": "usage_not_reported",
    }
    assert row.get("pricing") is None


@pytest.fixture
async def disk_app_and_client(engine: AsyncEngine) -> AsyncIterator[tuple[Any, AsyncClient]]:
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    _install_stable_request_admission_limiter(app.state)
    app.state.workspace_admission_disk_check = lambda settings: _disk_check(
        free_bytes=settings.min_free_disk_bytes + 1,
        threshold_bytes=settings.min_free_disk_bytes,
        ok=True,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.headers["Authorization"] = _WORKSPACE_AUTH_HEADER
        yield app, c


async def _transition_workspace(
    engine: AsyncEngine,
    workspace_id: str,
    *targets: WorkspaceStatus,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        assert ws is not None
        for target in targets:
            await repo.transition(ws, to=target, reason_code="TEST")
        await session.commit()


async def _set_workspace_status(
    engine: AsyncEngine,
    workspace_id: str,
    status: WorkspaceStatus,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        assert ws is not None
        ws.status = status.value
        await session.commit()


async def _set_workspace_created_at(
    engine: AsyncEngine,
    workspace_id: str,
    created_at: datetime,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        assert ws is not None
        ws.created_at = created_at
        ws.updated_at = created_at
        await session.commit()


async def _attach_merge_candidate(
    engine: AsyncEngine,
    workspace_id: str,
    *,
    head_sha: str | None = "target-current",
    base_sha: str | None = "base-current",
) -> tuple[str, str]:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.status = WorkspaceStatus.monitoring_pr.value
        workspace.pr_url = "https://github.com/example/app/pull/1"
        workspace.pr_number = 1
        workspace.branch_name = "codex/validation-observability"
        if workspace.task_attempt is not None:
            attempt = workspace.task_attempt
            task = await TaskRepository(session).get(attempt.task_id)
            assert task is not None
        else:
            task = await TaskRepository(session).create_or_get(
                repo_url=workspace.repo_url,
                base_branch=workspace.branch_base,
                title=workspace.task_title,
                prompt=workspace.task_prompt,
                external_id=f"WORKSPACE-{workspace_id}",
                idempotency_key=None,
                task_class=workspace.task_class,
                owned_paths=list(workspace.owned_paths),
            )
            attempt = await TaskAttemptRepository(session).create_for_workspace(
                task=task,
                workspace=workspace,
            )
        attempt.is_canonical_for_merge = True
        candidate = await MergeCandidateRepository(session).create_or_update_open_for_attempt(
            task=task,
            attempt=attempt,
            workspace=workspace,
            head_sha=head_sha,
            base_sha=base_sha,
        )
        await session.commit()
        return attempt.id, candidate.id


async def _insert_validation_run(
    engine: AsyncEngine,
    *,
    run_id: str,
    workspace_id: str,
    attempt_id: str | None,
    tier: int = 2,
    target_head_sha: str | None = "target-current",
    status: str = "succeeded",
    reason_code: str | None = "VALIDATION_OK",
    started_at: datetime = datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
    finished_at: datetime | None = datetime(2026, 4, 26, 12, 4, tzinfo=UTC),
) -> None:
    from sqlalchemy import text

    factory = make_session_factory(engine)
    async with factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO validation_runs (
                    id,
                    workspace_id,
                    attempt_id,
                    tier,
                    command_set_hash,
                    commands,
                    base_commit,
                    base_sha,
                    workspace_head_sha,
                    target_branch,
                    target_head_sha,
                    profile_name,
                    profile_version,
                    profile_source,
                    resolved_profile_digest,
                    environment_identity_digest,
                    environment_identity_inputs,
                    status,
                    reason_code,
                    started_at,
                    finished_at,
                    log_stream_refs
                )
                VALUES (
                    :id,
                    :workspace_id,
                    :attempt_id,
                    :tier,
                    :command_set_hash,
                    :commands,
                    :base_commit,
                    :base_sha,
                    :workspace_head_sha,
                    :target_branch,
                    :target_head_sha,
                    :profile_name,
                    :profile_version,
                    :profile_source,
                    :resolved_profile_digest,
                    :environment_identity_digest,
                    :environment_identity_inputs,
                    :status,
                    :reason_code,
                    :started_at,
                    :finished_at,
                    :log_stream_refs
                )
                """
            ),
            {
                "id": run_id,
                "workspace_id": workspace_id,
                "attempt_id": attempt_id,
                "tier": tier,
                "command_set_hash": "a" * 64,
                "commands": json.dumps(
                    [
                        {
                            "phase": "validate",
                            "command_index": 1,
                            "command": "pytest -q",
                            "stream_ids": {
                                "stdout": "validation.01_validate.stdout",
                                "stderr": "validation.01_validate.stderr",
                            },
                        }
                    ]
                ),
                "base_commit": "legacy-base",
                "base_sha": "base-identity",
                "workspace_head_sha": "workspace-head",
                "target_branch": "codex/validation-observability",
                "target_head_sha": target_head_sha,
                "profile_name": "python",
                "profile_version": 3,
                "profile_source": "repo:.awf/workspace.yml",
                "resolved_profile_digest": "1" * 64,
                "environment_identity_digest": "2" * 64,
                "environment_identity_inputs": json.dumps({"schema_version": 1}),
                "status": status,
                "reason_code": reason_code,
                "started_at": started_at,
                "finished_at": finished_at,
                "log_stream_refs": json.dumps(
                    {
                        "commands": [
                            {
                                "stdout": "validation.01_validate.stdout",
                                "stderr": "validation.01_validate.stderr",
                            }
                        ]
                    }
                ),
            },
        )
        await session.commit()


class TestCreateWorkspace:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("path", "first_body", "second_body", "first_key", "second_key"),
        [
            pytest.param(
                "/v1/workspaces",
                {**_MINIMAL_BODY, "task_title": "fresh key db first v1"},
                {**_MINIMAL_BODY, "task_title": "fresh key db second v1"},
                "workspace-fresh-db-first-v1",
                "workspace-fresh-db-second-v1",
                id="v1",
            ),
            pytest.param(
                "/v1/workspaces",
                _v2_body(title="fresh key db first v2"),
                _v2_body(title="fresh key db second v2"),
                "workspace-fresh-db-first-v2",
                "workspace-fresh-db-second-v2",
                id="v2",
            ),
        ],
    )
    async def test_rate_limit_checks_fresh_idempotency_key_before_exact_durable_replay_miss(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
        monkeypatch: pytest.MonkeyPatch,
        path: str,
        first_body: dict[str, object],
        second_body: dict[str, object],
        first_key: str,
        second_key: str,
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: _workspace_request_admission_settings(
            limit=1
        )
        calls: list[str] = []
        lock_keys: list[str] = []
        lookup_keys: list[str] = []
        probe_keys: list[str] = []
        list_calls = 0
        original_check = getattr(workspaces_route, "check_request_async", None)
        original_lock = WorkspaceRepository.acquire_idempotency_key_lock
        original_lookup = WorkspaceRepository.get_by_idempotency_key

        async def tracked_check_request_async(
            *_args: Any, **_kwargs: Any
        ) -> workspaces_route.RequestAdmissionDecision:
            calls.append("check")
            if original_check is None:
                raise AssertionError("workspace routes must import check_request_async")
            return await original_check(*_args, **_kwargs)

        async def tracked_lock(self: WorkspaceRepository, key: str) -> None:
            calls.append(f"lock:{key}")
            lock_keys.append(key)
            await original_lock(self, key)

        async def tracked_lookup(self: WorkspaceRepository, key: str) -> Any:
            calls.append(f"lookup:{key}")
            lookup_keys.append(key)
            return await original_lookup(self, key)

        async def tracked_probe(self: WorkspaceRepository, key: str) -> bool:
            probe_keys.append(key)
            raise AssertionError("rate-limited replays must not trust a pre-lock probe")

        async def fail_list_replay_keys(_self: WorkspaceRepository) -> list[str]:
            nonlocal list_calls
            list_calls += 1
            raise AssertionError("fresh rejected keys must not trigger full-table replay warmup")

        monkeypatch.setattr(
            WorkspaceRepository,
            "acquire_idempotency_key_lock",
            tracked_lock,
        )
        monkeypatch.setattr(
            WorkspaceRepository,
            "get_by_idempotency_key",
            tracked_lookup,
        )
        monkeypatch.setattr(
            WorkspaceRepository,
            "has_idempotency_key",
            tracked_probe,
        )
        monkeypatch.setattr(
            WorkspaceRepository,
            "list_idempotency_replay_keys",
            fail_list_replay_keys,
        )
        monkeypatch.setattr(
            workspaces_route,
            "check_request_async",
            tracked_check_request_async,
            raising=False,
        )

        first = await client.post(
            path,
            json=first_body,
            headers={"Idempotency-Key": first_key},
        )
        rejected = await client.post(
            path,
            json=second_body,
            headers={"Idempotency-Key": second_key},
        )

        assert first.status_code == 202
        _assert_workspace_rate_limited(rejected)
        assert lock_keys == [first_key, second_key]
        assert lookup_keys == [first_key, second_key]
        assert calls[:3] == ["check", f"lock:{first_key}", f"lookup:{first_key}"]
        assert calls[3:6] == ["check", f"lock:{second_key}", f"lookup:{second_key}"]
        assert probe_keys == []
        assert list_calls == 0

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("payload", "idempotency_key"),
        [
            pytest.param(
                WorkspaceCreateRequest.model_validate(
                    {**_MINIMAL_BODY, "task_title": "post-denial replay v1"}
                ),
                "workspace-post-denial-replay-v1",
                id="v1",
            ),
            pytest.param(
                WorkspaceCreateRequest.model_validate(_v2_body(title="post-denial replay v2")),
                "workspace-post-denial-replay-v2",
                id="v2",
            ),
        ],
    )
    async def test_rate_limited_workspace_create_uses_post_denial_durable_replay(
        self,
        monkeypatch: pytest.MonkeyPatch,
        payload: WorkspaceCreateRequest,
        idempotency_key: str,
    ) -> None:
        request = _request_with_disk_check()
        created_at = datetime(2026, 5, 15, tzinfo=UTC)
        existing = SimpleNamespace(
            id="ws_post_denial_replay",
            status=WorkspaceStatus.requested.value,
            version=7,
            created_at=created_at,
            repo_url=payload.repo_url,
            branch_base=payload.branch_base,
            task_title=payload.task_title,
            task_prompt=payload.task_prompt,
            task_external_id=payload.task_external_id,
            task_class=(
                payload.task.task_class.value if payload.task.task_class is not None else None
            ),
            owned_paths=list(payload.task.owned_paths),
            task_policy={"resource_reservation_request": {}},
            auto_merge=payload.task.auto_merge,
            initial_review_grace_period_seconds=payload.task.initial_review_grace_period_seconds,
            agent=payload.agent.value,
            task_kind=payload.task.kind,
            profile_ref=payload.env_profile,
            requested_profile=None,
            resolved_profile=None,
            test_commands=list(payload.test_commands),
            task_attempt=object(),
            events=[],
        )
        calls: list[str] = []
        lookups = 0

        async def allowed_preview(
            *_args: Any, **_kwargs: Any
        ) -> workspaces_route.RequestAdmissionDecision:
            calls.append("check")
            return workspaces_route.RequestAdmissionDecision(allowed=True, metadata={})

        async def denied_admission(
            *_args: Any, **_kwargs: Any
        ) -> workspaces_route.RequestAdmissionDecision:
            calls.append("admit")
            return workspaces_route.RequestAdmissionDecision(
                allowed=False,
                metadata={
                    "reason_code": "WORKSPACE_CREATE_RATE_LIMITED",
                    "endpoint_family": "workspace_create",
                    "identity_type": "client_host",
                    "identity_digest": "redacted",
                    "limit": 1,
                    "window_seconds": 60,
                    "remaining": 0,
                    "retry_after_seconds": 7,
                },
            )

        async def tracked_lock(_self: WorkspaceRepository, key: str) -> None:
            calls.append(f"lock:{key}")

        async def replay_after_denial(
            _self: WorkspaceRepository,
            key: str,
        ) -> object | None:
            nonlocal lookups
            lookups += 1
            calls.append(f"lookup:{key}:{lookups}")
            return None if lookups == 1 else existing

        async def fail_create(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("post-denial durable replay must not create a workspace")

        monkeypatch.setattr(
            workspaces_route,
            "check_request_async",
            allowed_preview,
            raising=False,
        )
        monkeypatch.setattr(workspaces_route, "admit_request_async", denied_admission)
        monkeypatch.setattr(WorkspaceRepository, "acquire_idempotency_key_lock", tracked_lock)
        monkeypatch.setattr(WorkspaceRepository, "get_by_idempotency_key", replay_after_denial)
        monkeypatch.setattr(workspaces_route, "create_workspace_row", fail_create)

        response = await workspaces_route.create_workspace(
            payload,
            request=request,  # type: ignore[arg-type]
            idempotency_key=idempotency_key,
            settings=_workspace_request_admission_settings(limit=1),
            session=SimpleNamespace(info={}, bind=None),  # type: ignore[arg-type]
        )

        assert not isinstance(response, JSONResponse)
        assert response.workspace_id == existing.id
        assert calls == [
            "check",
            f"lock:{idempotency_key}",
            f"lookup:{idempotency_key}:1",
            "admit",
            f"lock:{idempotency_key}",
            f"lookup:{idempotency_key}:2",
        ]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("payload", "idempotency_key"),
        [
            pytest.param(
                WorkspaceCreateRequest.model_validate(
                    {**_MINIMAL_BODY, "task_title": "fresh retry after v1"}
                ),
                "workspace-refresh-retry-after-v1",
                id="v1",
            ),
            pytest.param(
                WorkspaceCreateRequest.model_validate(_v2_body(title="fresh retry after v2")),
                "workspace-refresh-retry-after-v2",
                id="v2",
            ),
        ],
    )
    async def test_rate_limited_workspace_create_refreshes_preview_after_durable_miss(
        self,
        monkeypatch: pytest.MonkeyPatch,
        payload: WorkspaceCreateRequest,
        idempotency_key: str,
    ) -> None:
        request = _request_with_disk_check()
        calls: list[str] = []
        retry_after_values = [60, 2]

        def denied_decision(retry_after_seconds: int) -> workspaces_route.RequestAdmissionDecision:
            return workspaces_route.RequestAdmissionDecision(
                allowed=False,
                metadata={
                    "reason_code": "WORKSPACE_CREATE_RATE_LIMITED",
                    "endpoint_family": "workspace_create",
                    "identity_type": "client_host",
                    "identity_digest": "redacted",
                    "limit": 1,
                    "window_seconds": 60,
                    "remaining": 0,
                    "retry_after_seconds": retry_after_seconds,
                },
            )

        async def denied_preview(
            *_args: Any, **_kwargs: Any
        ) -> workspaces_route.RequestAdmissionDecision:
            calls.append("check")
            return denied_decision(retry_after_values.pop(0))

        async def fail_admit(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("stale denied previews must refresh before admission")

        async def tracked_lock(_self: WorkspaceRepository, key: str) -> None:
            calls.append(f"lock:{key}")

        async def missing_replay(_self: WorkspaceRepository, key: str) -> None:
            calls.append(f"lookup:{key}")

        async def fail_create(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("rate-limited request must not create a workspace")

        monkeypatch.setattr(
            workspaces_route,
            "check_request_async",
            denied_preview,
            raising=False,
        )
        monkeypatch.setattr(workspaces_route, "admit_request_async", fail_admit)
        monkeypatch.setattr(WorkspaceRepository, "acquire_idempotency_key_lock", tracked_lock)
        monkeypatch.setattr(WorkspaceRepository, "get_by_idempotency_key", missing_replay)
        monkeypatch.setattr(workspaces_route, "create_workspace_row", fail_create)

        response = await workspaces_route.create_workspace(
            payload,
            request=request,  # type: ignore[arg-type]
            idempotency_key=idempotency_key,
            settings=_workspace_request_admission_settings(limit=1),
            session=SimpleNamespace(info={}, bind=None),  # type: ignore[arg-type]
        )

        assert isinstance(response, JSONResponse)
        assert response.status_code == 429
        body = json.loads(response.body.decode())
        assert body["detail"]["retry_after_seconds"] == 2
        assert response.headers["Retry-After"] == "2"
        assert calls == [
            "check",
            f"lock:{idempotency_key}",
            f"lookup:{idempotency_key}",
            "check",
        ]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("path", "payload", "idempotency_key"),
        [
            pytest.param(
                "/v1/workspaces",
                {**_MINIMAL_BODY, "task_title": "cache loss replay v1"},
                "workspace-cache-loss-replay-v1",
                id="v1",
            ),
            pytest.param(
                "/v1/workspaces",
                _v2_body(title="cache loss replay v2"),
                "workspace-cache-loss-replay-v2",
                id="v2",
            ),
        ],
    )
    async def test_cold_cache_persisted_idempotency_replay_bypasses_exhausted_rate_limit(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
        monkeypatch: pytest.MonkeyPatch,
        path: str,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: _workspace_request_admission_settings(
            limit=1
        )
        lock_keys: list[str] = []
        lookup_keys: list[str] = []
        original_lock = WorkspaceRepository.acquire_idempotency_key_lock
        original_lookup = WorkspaceRepository.get_by_idempotency_key

        async def tracked_lock(self: WorkspaceRepository, key: str) -> None:
            lock_keys.append(key)
            await original_lock(self, key)

        async def tracked_lookup(self: WorkspaceRepository, key: str) -> Any:
            lookup_keys.append(key)
            return await original_lookup(self, key)

        monkeypatch.setattr(
            WorkspaceRepository,
            "acquire_idempotency_key_lock",
            tracked_lock,
        )
        monkeypatch.setattr(
            WorkspaceRepository,
            "get_by_idempotency_key",
            tracked_lookup,
        )

        first = await client.post(
            path,
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )
        _clear_workspace_create_replay_key_cache(app)
        replay = await client.post(
            path,
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )

        assert first.status_code == 202
        assert replay.status_code == 202
        assert replay.json()["workspace_id"] == first.json()["workspace_id"]
        assert lock_keys == [idempotency_key, idempotency_key]
        assert lookup_keys == [idempotency_key, idempotency_key]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("path", "payload", "fresh_payload", "replay_key", "fresh_key"),
        [
            pytest.param(
                "/v1/workspaces",
                {**_MINIMAL_BODY, "task_title": "cold replay quota v1"},
                {**_MINIMAL_BODY, "task_title": "fresh after cold replay v1"},
                "workspace-cold-replay-quota-v1",
                "workspace-fresh-after-cold-replay-v1",
                id="v1",
            ),
            pytest.param(
                "/v1/workspaces",
                _v2_body(title="cold replay quota v2"),
                _v2_body(title="fresh after cold replay v2"),
                "workspace-cold-replay-quota-v2",
                "workspace-fresh-after-cold-replay-v2",
                id="v2",
            ),
        ],
    )
    async def test_cold_idempotency_replay_with_remaining_quota_does_not_spend_fresh_slot(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
        path: str,
        payload: dict[str, object],
        fresh_payload: dict[str, object],
        replay_key: str,
        fresh_key: str,
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: _workspace_request_admission_settings(
            limit=2
        )

        first = await client.post(
            path,
            json=payload,
            headers={"Idempotency-Key": replay_key},
        )
        _clear_workspace_create_replay_key_cache(app)
        replay = await client.post(
            path,
            json=payload,
            headers={"Idempotency-Key": replay_key},
        )
        fresh = await client.post(
            path,
            json=fresh_payload,
            headers={"Idempotency-Key": fresh_key},
        )

        assert first.status_code == 202
        assert replay.status_code == 202
        assert replay.json()["workspace_id"] == first.json()["workspace_id"]
        assert fresh.status_code == 202

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("path", "payload", "idempotency_key"),
        [
            pytest.param(
                "/v1/workspaces",
                {**_MINIMAL_BODY, "task_title": "in-flight over quota replay v1"},
                "workspace-inflight-rate-limit-replay-v1",
                id="v1",
            ),
            pytest.param(
                "/v1/workspaces",
                _v2_body(title="in-flight over quota replay v2"),
                "workspace-inflight-rate-limit-replay-v2",
                id="v2",
            ),
        ],
    )
    async def test_rate_limited_duplicate_unknown_key_uses_durable_replay_when_cache_misses(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
        monkeypatch: pytest.MonkeyPatch,
        path: str,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: _workspace_request_admission_settings(
            limit=1
        )
        probe_keys: list[str] = []

        first = await client.post(
            path,
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )
        assert first.status_code == 202
        _clear_workspace_create_replay_key_cache(app)

        async def stale_pre_lock_probe(_self: WorkspaceRepository, key: str) -> bool:
            probe_keys.append(key)
            return False

        monkeypatch.setattr(
            WorkspaceRepository,
            "has_idempotency_key",
            stale_pre_lock_probe,
        )

        replay = await client.post(
            path,
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )

        assert replay.status_code == 202
        assert replay.json()["workspace_id"] == first.json()["workspace_id"]
        assert probe_keys == []

    @pytest.mark.unit
    async def test_rejects_v1_create_burst_after_configured_limit(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: _workspace_request_admission_settings(
            limit=1
        )

        first = await client.post(
            "/v1/workspaces",
            json={**_MINIMAL_BODY, "task_title": "rate limit first v1"},
        )
        rejected = await client.post(
            "/v1/workspaces",
            json={**_MINIMAL_BODY, "task_title": "rate limit second v1"},
        )

        assert first.status_code == 202
        _assert_workspace_rate_limited(rejected)

    @pytest.mark.unit
    async def test_v1_idempotency_replay_bypasses_limit_but_fresh_keys_are_bounded(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: _workspace_request_admission_settings(
            limit=1
        )
        payload = {**_MINIMAL_BODY, "task_title": "idempotent rate limit replay v1"}

        first = await client.post(
            "/v1/workspaces",
            json=payload,
            headers={"Idempotency-Key": "rate-limit-v1-replay"},
        )
        replay = await client.post(
            "/v1/workspaces",
            json=payload,
            headers={"Idempotency-Key": "rate-limit-v1-replay"},
        )
        fresh = await client.post(
            "/v1/workspaces",
            json={**_MINIMAL_BODY, "task_title": "fresh key bounded v1"},
            headers={"Idempotency-Key": "rate-limit-v1-fresh"},
        )
        listed = await client.get("/v1/workspaces")

        assert first.status_code == 202
        assert replay.status_code == 202
        assert replay.json()["workspace_id"] == first.json()["workspace_id"]
        _assert_workspace_rate_limited(fresh)
        assert len(listed.json()) == 1

    @pytest.mark.unit
    async def test_v1_and_v2_create_share_workspace_create_rate_limit_bucket(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: _workspace_request_admission_settings(
            limit=1
        )

        first = await client.post(
            "/v1/workspaces",
            json={**_MINIMAL_BODY, "task_title": "shared bucket first v1"},
        )
        rejected = await client.post(
            "/v1/workspaces",
            json=_v2_body(title="shared bucket second v2"),
        )

        assert first.status_code == 202
        _assert_workspace_rate_limited(rejected)

    @pytest.mark.unit
    async def test_returns_202_with_workspace_id(self, client: AsyncClient) -> None:
        response = await client.post("/v1/workspaces", json=_MINIMAL_BODY)
        assert response.status_code == 202

        body = response.json()
        assert body["workspace_id"].startswith("ws_")
        assert body["status"] == "requested"
        assert body["version"] == 1
        assert body["status_url"] == f"/v1/workspaces/{body['workspace_id']}"
        assert body["events_url"] == f"/v1/workspaces/{body['workspace_id']}/events"
        assert "accepted_at" in body

    @pytest.mark.unit
    def test_workspace_replay_key_cache_without_app_state_is_request_local(self) -> None:
        request = SimpleNamespace()

        cache = workspaces_route._workspace_create_idempotency_replay_key_cache(  # noqa: SLF001
            request
        )

        assert (
            workspaces_route._workspace_create_idempotency_replay_key_cache(  # noqa: SLF001
                request
            )
            is cache
        )
        assert (
            workspaces_route._workspace_create_idempotency_replay_key_cache(  # noqa: SLF001
                SimpleNamespace()
            )
            is not cache
        )
        assert workspaces_route._workspace_create_idempotency_replay_key_cache(  # noqa: SLF001
            None
        ) is not workspaces_route._workspace_create_idempotency_replay_key_cache(  # noqa: SLF001
            None
        )

    @pytest.mark.unit
    def test_workspace_replay_key_cache_tolerates_non_extensible_test_objects(self) -> None:
        class _Slotless:
            __slots__ = ()

        request = _Slotless()

        first = workspaces_route._workspace_create_idempotency_replay_key_cache(  # noqa: SLF001
            request
        )
        second = workspaces_route._workspace_create_idempotency_replay_key_cache(  # noqa: SLF001
            request
        )

        assert isinstance(
            first,
            workspaces_route._WorkspaceCreateIdempotencyReplayKeyCache,  # noqa: SLF001
        )
        assert isinstance(
            second,
            workspaces_route._WorkspaceCreateIdempotencyReplayKeyCache,  # noqa: SLF001
        )
        assert first is not second

    @pytest.mark.unit
    def test_workspace_replay_key_cache_rejects_invalid_size(self) -> None:
        with pytest.raises(ValueError, match="max_entries"):
            workspaces_route._WorkspaceCreateIdempotencyReplayKeyCache(  # noqa: SLF001
                max_entries=0
            )

    @pytest.mark.unit
    def test_workspace_replay_key_cache_real_request_without_app_state_fails_loudly(
        self,
    ) -> None:
        request = _workspace_request_without_app_state()

        with pytest.raises(RuntimeError, match=r"request\.app\.state"):
            workspaces_route._workspace_create_idempotency_replay_key_cache(  # noqa: SLF001
                request
            )

    @pytest.mark.unit
    def test_workspace_replay_key_cache_app_state_is_bounded(self) -> None:
        request = _request_with_disk_check()
        cache = workspaces_route._workspace_create_idempotency_replay_key_cache(  # noqa: SLF001
            request
        )
        payload = WorkspaceCreateRequest.model_validate(
            {**_MINIMAL_BODY, "task_title": "bounded workspace replay key"}
        )
        max_entries = workspaces_route._WORKSPACE_CREATE_REPLAY_KEY_CACHE_MAX_ENTRIES  # noqa: SLF001

        for index in range(max_entries + 1):
            cache.remember(
                payload,
                idempotency_key=f"workspace-app-state-key-{index}",
                api_version=workspaces_route._WORKSPACE_CREATE_API_VERSION,  # noqa: SLF001
            )

        newest_key = f"workspace-app-state-key-{max_entries}"
        assert (
            cache.matches(
                payload,
                idempotency_key="workspace-app-state-key-0",
                api_version=workspaces_route._WORKSPACE_CREATE_API_VERSION,  # noqa: SLF001
            )
            is False
        )
        assert (
            cache.matches(
                payload,
                idempotency_key=newest_key,
                api_version=workspaces_route._WORKSPACE_CREATE_API_VERSION,  # noqa: SLF001
            )
            is True
        )

    @pytest.mark.unit
    def test_workspace_replay_key_cache_default_retains_keys_past_response_cache_limit(
        self,
    ) -> None:
        cache = workspaces_route._WorkspaceCreateIdempotencyReplayKeyCache()  # noqa: SLF001
        payload = WorkspaceCreateRequest.model_validate(
            {**_MINIMAL_BODY, "task_title": "default retain workspace replay key"}
        )

        for index in range(
            workspaces_route._WORKSPACE_CREATE_REPLAY_KEY_CACHE_MAX_ENTRIES + 1  # noqa: SLF001
        ):
            cache.remember(
                payload,
                idempotency_key=f"workspace-default-key-{index}",
                api_version=workspaces_route._WORKSPACE_CREATE_API_VERSION,  # noqa: SLF001
            )

        assert (
            cache.matches(
                payload,
                idempotency_key="workspace-default-key-0",
                api_version=workspaces_route._WORKSPACE_CREATE_API_VERSION,  # noqa: SLF001
            )
            is True
        )

    @pytest.mark.unit
    def test_workspace_replay_key_cache_locks_composite_lru_operations(self) -> None:
        cache = workspaces_route._WorkspaceCreateIdempotencyReplayKeyCache()  # noqa: SLF001
        lock = _TrackingLock()
        cache._lock = lock  # noqa: SLF001
        payload = WorkspaceCreateRequest.model_validate(
            {**_MINIMAL_BODY, "task_title": "locked workspace replay key"}
        )

        cache.remember(
            payload,
            idempotency_key="workspace-locked-key",
            api_version=workspaces_route._WORKSPACE_CREATE_API_VERSION,  # noqa: SLF001
        )
        matched = cache.matches(
            payload,
            idempotency_key="workspace-locked-key",
            api_version=workspaces_route._WORKSPACE_CREATE_API_VERSION,  # noqa: SLF001
        )

        assert matched is True
        assert lock.enters == 2
        assert lock.exits == 2

    @pytest.mark.unit
    async def test_v1_cache_hash_conflict_uses_durable_replay_before_conflict(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        request = _request_with_disk_check()
        payload = WorkspaceCreateRequest.model_validate(
            {**_MINIMAL_BODY, "task_title": "durable replay after stale cache hash"}
        )
        idempotency_key = "workspace-v1-stale-cache-hash"
        replay_key_cache = workspaces_route._workspace_create_idempotency_replay_key_cache(  # noqa: SLF001
            request
        )
        replay_key_cache.remember_hash(
            idempotency_key=idempotency_key,
            request_hash="stale-cache-hash",
        )
        existing = SimpleNamespace(
            id="ws_v1_stale_hash_replay",
            status=WorkspaceStatus.requested.value,
            version=3,
            created_at=datetime(2026, 5, 15, tzinfo=UTC),
            repo_url=payload.repo_url,
            branch_base=payload.branch_base,
            task_title=payload.task_title,
            task_prompt=payload.task_prompt,
            task_external_id=payload.task_external_id,
            agent=payload.agent.value,
            env_profile=payload.env_profile,
            test_commands=list(payload.test_commands),
            requires_database=payload.requires_database,
            task_attempt=None,
        )
        lock_keys: list[str] = []
        lookup_keys: list[str] = []
        create_calls: list[str | None] = []

        async def tracked_lock(_self: WorkspaceRepository, key: str) -> None:
            lock_keys.append(key)

        async def tracked_lookup(_self: WorkspaceRepository, key: str) -> object:
            lookup_keys.append(key)
            return existing

        async def fail_create(_self: WorkspaceRepository, **kwargs: object) -> None:
            create_calls.append(kwargs.get("idempotency_key"))
            raise AssertionError("durable replay must not create a new workspace")

        monkeypatch.setattr(WorkspaceRepository, "acquire_idempotency_key_lock", tracked_lock)
        monkeypatch.setattr(WorkspaceRepository, "get_by_idempotency_key", tracked_lookup)
        monkeypatch.setattr(WorkspaceRepository, "create", fail_create)

        response = await workspaces_route.create_workspace(
            payload,
            request=request,  # type: ignore[arg-type]
            idempotency_key=idempotency_key,
            settings=_workspace_request_admission_settings(limit=10),
            session=SimpleNamespace(info={}, bind=None),  # type: ignore[arg-type]
        )

        assert not isinstance(response, JSONResponse)
        assert response.workspace_id == existing.id
        assert lock_keys == [idempotency_key]
        assert lookup_keys == [idempotency_key]
        assert create_calls == []

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("payload", "idempotency_key"),
        [
            pytest.param(
                WorkspaceCreateRequest.model_validate(
                    {**_MINIMAL_BODY, "task_title": "known missing replay v1"}
                ),
                "known-missing-workspace-v1",
                id="v1",
            ),
            pytest.param(
                WorkspaceCreateRequest.model_validate(_v2_body(title="known missing replay v2")),
                "known-missing-workspace-v2",
                id="v2",
            ),
        ],
    )
    async def test_known_replay_key_db_miss_returns_conflict_without_create(
        self,
        monkeypatch: pytest.MonkeyPatch,
        payload: WorkspaceCreateRequest,
        idempotency_key: str,
    ) -> None:
        request = _request_with_disk_check()
        replay_key_cache = workspaces_route._workspace_create_idempotency_replay_key_cache(  # noqa: SLF001
            request
        )
        replay_key_cache.remember(
            payload,
            idempotency_key=idempotency_key,
            api_version=workspaces_route._WORKSPACE_CREATE_API_VERSION,  # noqa: SLF001
        )
        lock_keys: list[str] = []
        lookup_keys: list[str] = []
        create_calls: list[str | None] = []

        async def tracked_lock(_self: WorkspaceRepository, key: str) -> None:
            lock_keys.append(key)

        async def tracked_lookup(_self: WorkspaceRepository, key: str) -> None:
            lookup_keys.append(key)

        async def fail_create(*_args: object, **kwargs: object) -> None:
            create_calls.append(kwargs.get("idempotency_key"))
            raise AssertionError("known replay-key durable miss must not create a workspace")

        monkeypatch.setattr(WorkspaceRepository, "acquire_idempotency_key_lock", tracked_lock)
        monkeypatch.setattr(WorkspaceRepository, "get_by_idempotency_key", tracked_lookup)
        monkeypatch.setattr(workspaces_route, "create_workspace_row", fail_create)

        session = SimpleNamespace(info={}, bind=None)
        response = await workspaces_route.create_workspace(
            payload,
            request=request,  # type: ignore[arg-type]
            idempotency_key=idempotency_key,
            settings=_workspace_request_admission_settings(limit=10),
            session=session,  # type: ignore[arg-type]
        )

        assert isinstance(response, JSONResponse)
        assert response.status_code == 409
        assert json.loads(response.body)["error_code"] == "IDEMPOTENCY_REPLAY_UNAVAILABLE"
        assert lock_keys == [idempotency_key]
        assert lookup_keys == [idempotency_key]
        assert create_calls == []

    @pytest.mark.unit
    async def test_rejects_empty_task_prompt(self, client: AsyncClient) -> None:
        bad = {**_MINIMAL_BODY, "task_prompt": ""}
        response = await client.post("/v1/workspaces", json=bad)
        assert response.status_code == 422

    @pytest.mark.unit
    async def test_rejects_unknown_agent(self, client: AsyncClient) -> None:
        bad = {**_MINIMAL_BODY, "agent": "my-shiny-agent"}
        response = await client.post("/v1/workspaces", json=bad)
        assert response.status_code == 422

    @pytest.mark.unit
    async def test_rejects_extra_fields(self, client: AsyncClient) -> None:
        bad = {**_MINIMAL_BODY, "hax0r_field": "value"}
        response = await client.post("/v1/workspaces", json=bad)
        assert response.status_code == 422

    @pytest.mark.unit
    async def test_direct_v1_create_replays_same_payload_and_rejects_conflict(
        self,
        engine: AsyncEngine,
    ) -> None:
        payload = WorkspaceCreateRequest.model_validate(_MINIMAL_BODY)
        factory = make_session_factory(engine)
        async with factory() as session:
            first = await workspaces_route.create_workspace(
                payload,
                idempotency_key="direct-v1-replay",
                settings=Settings(_env_file=None),
                session=session,
            )
            replay = await workspaces_route.create_workspace(
                payload,
                idempotency_key="direct-v1-replay",
                settings=Settings(_env_file=None),
                session=session,
            )
            conflict = await workspaces_route.create_workspace(
                WorkspaceCreateRequest.model_validate(
                    {**_MINIMAL_BODY, "task_title": "Changed direct replay"}
                ),
                idempotency_key="direct-v1-replay",
                settings=Settings(_env_file=None),
                session=session,
            )

        assert first.workspace_id == replay.workspace_id
        assert isinstance(conflict, JSONResponse)
        assert conflict.status_code == 409
        body = json.loads(conflict.body)
        assert body["error_code"] == "IDEMPOTENCY_CONFLICT"
        _assert_no_internal_error_fields(body)


class TestCreateWorkspaceDiskPressure:
    @pytest.mark.unit
    async def test_rejects_v2_create_burst_after_configured_limit(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: _workspace_request_admission_settings(
            limit=1
        )

        first = await client.post("/v1/workspaces", json=_v2_body(title="rate limit first v2"))
        rejected = await client.post(
            "/v1/workspaces",
            json=_v2_body(title="rate limit second v2"),
        )

        assert first.status_code == 202
        _assert_workspace_rate_limited(rejected)

    @pytest.mark.unit
    async def test_v2_create_rate_limit_rejects_before_disk_admission(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: _workspace_request_admission_settings(
            limit=1
        )
        disk_checks = 0

        def admission_check(settings: Settings) -> DiskCheck:
            nonlocal disk_checks
            disk_checks += 1
            return _disk_check(
                free_bytes=settings.min_free_disk_bytes + 1,
                threshold_bytes=settings.min_free_disk_bytes,
                ok=True,
            )

        app.state.workspace_admission_disk_check = admission_check

        first = await client.post("/v1/workspaces", json=_v2_body(title="disk first"))
        rejected = await client.post("/v1/workspaces", json=_v2_body(title="disk second"))

        assert first.status_code == 202
        _assert_workspace_rate_limited(rejected)
        assert disk_checks == 1

    @pytest.mark.unit
    async def test_v2_idempotency_replay_bypasses_limit_but_fresh_keys_are_bounded(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: _workspace_request_admission_settings(
            limit=1
        )
        payload = _v2_body(title="idempotent rate limit replay")
        replay_key = _unique_idempotency_key("rate-limit-v2-replay")
        fresh_key = _unique_idempotency_key("rate-limit-v2-fresh")

        first = await client.post(
            "/v1/workspaces",
            json=payload,
            headers={"Idempotency-Key": replay_key},
        )
        replay = await client.post(
            "/v1/workspaces",
            json=payload,
            headers={"Idempotency-Key": replay_key},
        )
        fresh = await client.post(
            "/v1/workspaces",
            json=_v2_body(title="fresh key bounded"),
            headers={"Idempotency-Key": fresh_key},
        )
        listed = await client.get("/v1/workspaces")

        assert first.status_code == 202
        assert replay.status_code == 202
        assert replay.json()["workspace_id"] == first.json()["workspace_id"]
        _assert_workspace_rate_limited(fresh)
        assert len(listed.json()) == 1

    @pytest.mark.unit
    async def test_default_disk_admission_checks_configured_work_dir(
        self,
        tmp_path: Any,
    ) -> None:
        settings = Settings(
            _env_file=None,
            work_dir=str(tmp_path),
            min_free_disk_bytes=0,
        )
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

        disk_check = await workspaces_route._workspace_admission_disk_check(  # noqa: SLF001
            request,  # type: ignore[arg-type]
            settings,
        )

        assert disk_check.ok is True
        assert disk_check.path == str(tmp_path)
        assert disk_check.threshold_bytes == 0

    @pytest.mark.unit
    async def test_rejects_low_disk_without_creating_row(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
    ) -> None:
        app, client = disk_app_and_client
        app.state.workspace_admission_disk_check = lambda _settings: _disk_check(
            free_bytes=300,
            threshold_bytes=400,
            ok=False,
        )

        response = await client.post("/v1/workspaces", json=_V2_MINIMAL_BODY)
        listed = await client.get("/v1/workspaces")

        assert response.status_code == 503
        body = response.json()
        assert body["error_code"] == "INSUFFICIENT_DISK"
        assert body["detail"]["disk"]["free_bytes"] == 300
        assert body["detail"]["disk"]["threshold_bytes"] == 400
        assert listed.status_code == 200
        assert listed.json() == []

    @pytest.mark.unit
    async def test_idempotent_replay_returns_existing_row_under_low_disk(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
    ) -> None:
        app, client = disk_app_and_client
        headers = {"Idempotency-Key": "disk-pressure-replay"}
        app.state.workspace_admission_disk_check = lambda _settings: _disk_check(
            free_bytes=700,
            threshold_bytes=400,
            ok=True,
        )
        first = await client.post("/v1/workspaces", json=_V2_MINIMAL_BODY, headers=headers)

        app.state.workspace_admission_disk_check = lambda _settings: _disk_check(
            free_bytes=300,
            threshold_bytes=400,
            ok=False,
        )
        replay = await client.post("/v1/workspaces", json=_V2_MINIMAL_BODY, headers=headers)
        listed = await client.get("/v1/workspaces")

        assert first.status_code == 202
        assert replay.status_code == 202
        assert replay.json()["workspace_id"] == first.json()["workspace_id"]
        assert len(listed.json()) == 1

    @pytest.mark.unit
    async def test_create_succeeds_when_disk_is_above_threshold(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
    ) -> None:
        app, client = disk_app_and_client
        app.state.workspace_admission_disk_check = lambda _settings: _disk_check(
            free_bytes=700,
            threshold_bytes=400,
            ok=True,
        )

        response = await client.post("/v1/workspaces", json=_V2_MINIMAL_BODY)
        listed = await client.get("/v1/workspaces")

        assert response.status_code == 202
        assert len(listed.json()) == 1
        assert listed.json()[0]["id"] == response.json()["workspace_id"]

    @pytest.mark.unit
    async def test_disk_admission_uses_dependency_overridden_settings(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
    ) -> None:
        app, client = disk_app_and_client
        settings = Settings(
            _env_file=None,
            work_dir="/tmp/awf-test-workspaces",
            min_free_disk_bytes=123,
        )
        seen: dict[str, Settings] = {}

        def admission_check(provider_settings: Settings) -> DiskCheck:
            seen["settings"] = provider_settings
            return _disk_check(
                free_bytes=124,
                threshold_bytes=provider_settings.min_free_disk_bytes,
                ok=True,
            )

        app.dependency_overrides[get_settings] = lambda: settings
        app.state.workspace_admission_disk_check = admission_check

        response = await client.post("/v1/workspaces", json=_V2_MINIMAL_BODY)

        assert response.status_code == 202
        assert seen["settings"] is settings


class TestCreateWorkspaceMonitorPolicy:
    @pytest.mark.unit
    async def test_old_v2_payload_defaults_to_auto_merge_and_profile_grace(
        self,
        client: AsyncClient,
    ) -> None:
        create = await client.post("/v1/workspaces", json=_V2_MINIMAL_BODY)
        assert create.status_code == 202

        ws_id = create.json()["workspace_id"]
        response = await client.get(f"/v1/workspaces/{ws_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["auto_merge"] is True
        assert body["initial_review_grace_period_seconds"] is None

    @pytest.mark.unit
    async def test_persists_explicit_monitor_policy(self, client: AsyncClient) -> None:
        payload = {
            **_V2_MINIMAL_BODY,
            "task": {
                **_V2_MINIMAL_BODY["task"],
                "auto_merge": False,
                "initial_review_grace_period_seconds": 12.5,
            },
        }

        create = await client.post("/v1/workspaces", json=payload)
        assert create.status_code == 202

        ws_id = create.json()["workspace_id"]
        response = await client.get(f"/v1/workspaces/{ws_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["auto_merge"] is False
        assert body["initial_review_grace_period_seconds"] == 12.5

    @pytest.mark.unit
    async def test_idempotency_conflicts_when_monitor_policy_changes(
        self,
        client: AsyncClient,
    ) -> None:
        headers = {"Idempotency-Key": "monitor-policy-key"}
        first_payload = {
            **_V2_MINIMAL_BODY,
            "task": {
                **_V2_MINIMAL_BODY["task"],
                "auto_merge": False,
                "initial_review_grace_period_seconds": 30,
            },
        }
        replay_payload = {
            **_V2_MINIMAL_BODY,
            "task": {
                **_V2_MINIMAL_BODY["task"],
                "auto_merge": True,
                "initial_review_grace_period_seconds": 30,
            },
        }

        first = await client.post("/v1/workspaces", json=first_payload, headers=headers)
        replay = await client.post("/v1/workspaces", json=replay_payload, headers=headers)

        assert first.status_code == 202
        assert replay.status_code == 409
        body = replay.json()
        assert body["error_code"] == "IDEMPOTENCY_CONFLICT"
        _assert_no_internal_error_fields(body)


class TestCreateWorkspaceResourceIdempotency:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("resources", "expected_steady_cpu"),
        [
            pytest.param({}, 2.0, id="all-defaulted"),
            pytest.param({"steady_state_cpu_cores": 4.0}, 4.0, id="partial-defaulted"),
        ],
    )
    async def test_idempotent_replay_preserves_resource_defaults_after_settings_change(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
        resources: dict[str, object],
        expected_steady_cpu: float,
    ) -> None:
        app, client = disk_app_and_client
        old_settings = Settings(
            _env_file=None,
            workspace_steady_cpu=2.0,
            workspace_steady_memory_gb=6.0,
            workspace_peak_cpu=3.0,
            workspace_peak_memory_gb=8.0,
        )
        new_settings = Settings(
            _env_file=None,
            workspace_steady_cpu=7.0,
            workspace_steady_memory_gb=14.0,
            workspace_peak_cpu=9.0,
            workspace_peak_memory_gb=18.0,
        )
        active_settings = old_settings
        app.dependency_overrides[get_settings] = lambda: active_settings
        payload = {**_V2_MINIMAL_BODY, "resources": resources}
        headers = {
            "Idempotency-Key": f"resource-default-replay-{expected_steady_cpu:g}",
        }

        first = await client.post("/v1/workspaces", json=payload, headers=headers)
        active_settings = new_settings
        replay = await client.post("/v1/workspaces", json=payload, headers=headers)

        assert first.status_code == 202
        assert replay.status_code == 202
        assert replay.json()["workspace_id"] == first.json()["workspace_id"]

        detail = await client.get(f"/v1/workspaces/{first.json()['workspace_id']}")
        reservation = detail.json()["active_resource_reservation"]
        assert reservation["steady_cpu"] == expected_steady_cpu
        assert reservation["steady_memory_gb"] == 6.0
        assert reservation["peak_cpu"] == 3.0
        assert reservation["peak_memory_gb"] == 8.0

    @pytest.mark.unit
    async def test_defaulted_resource_create_conflicts_with_explicit_default_replay(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: Settings(
            _env_file=None,
            workspace_steady_cpu=2.0,
            workspace_steady_memory_gb=6.0,
            workspace_peak_cpu=3.0,
            workspace_peak_memory_gb=8.0,
        )
        headers = {"Idempotency-Key": "resource-default-explicit-conflict"}
        replay_payload = {
            **_V2_MINIMAL_BODY,
            "resources": {
                "steady_state_cpu_cores": 2.0,
                "steady_state_memory_gb": 6.0,
                "peak_cpu_cores": 3.0,
                "peak_memory_gb": 8.0,
            },
        }

        first = await client.post("/v1/workspaces", json=_V2_MINIMAL_BODY, headers=headers)
        replay = await client.post("/v1/workspaces", json=replay_payload, headers=headers)

        assert first.status_code == 202
        assert replay.status_code == 409
        assert replay.json()["error_code"] == "IDEMPOTENCY_CONFLICT"


class TestWorkspaceCreateProviderReadinessPreflight:
    @pytest.fixture(autouse=True)
    def _clear_provider_auth_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in _PROVIDER_AUTH_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)

    @pytest.mark.unit
    async def test_v2_create_blocks_missing_selected_provider_readiness(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
        tmp_path: Any,
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: _provider_preflight_settings(tmp_path)

        response = await client.post("/v1/workspaces", json=_V2_MINIMAL_BODY)

        assert response.status_code == 409
        body = response.json()
        assert body["error_code"] == "PROVIDER_READINESS_PRECHECK_FAILED"
        preflight = body["detail"]["provider_readiness_preflight"]
        assert preflight["provider"] == "codex"
        assert preflight["model"] == "gpt-5.5"
        assert preflight["auth_status"] == "fail"
        assert preflight["auth_source"] == "not_observed"
        assert preflight["probe_status"] == "skipped"
        assert preflight["blocks_launch"] is True

    @pytest.mark.unit
    async def test_v2_create_override_returns_preflight_summary(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
        tmp_path: Any,
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: _provider_preflight_settings(tmp_path)
        payload = {
            **_V2_MINIMAL_BODY,
            "preflight": {
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "operator verified local auth",
            },
        }

        response = await client.post("/v1/workspaces", json=payload)

        assert response.status_code == 202
        preflight = response.json()["provider_readiness_preflight"]
        assert preflight["provider"] == "codex"
        assert preflight["readiness_status"] == "admitted_with_override"
        assert preflight["override_used"] is True
        assert preflight["override_reason"] == "operator verified local auth"

    @pytest.mark.unit
    async def test_idempotent_replay_normalizes_blank_override_reason(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
        tmp_path: Any,
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: _provider_preflight_settings(tmp_path)
        payload = {
            **_V2_MINIMAL_BODY,
            "preflight": {
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "   ",
            },
        }
        headers = {"Idempotency-Key": "provider-readiness-blank-reason-replay"}

        first = await client.post("/v1/workspaces", json=payload, headers=headers)
        replay = await client.post("/v1/workspaces", json=payload, headers=headers)

        assert first.status_code == 202
        assert replay.status_code == 202
        assert replay.json()["workspace_id"] == first.json()["workspace_id"]
        assert replay.json()["provider_readiness_preflight"]["override_reason"] is None

    @pytest.mark.unit
    async def test_idempotent_replay_returns_existing_preflight_without_rerun(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
        tmp_path: Any,
    ) -> None:
        app, client = disk_app_and_client
        home = tmp_path / "home"
        codex_home = home / ".codex"
        codex_home.mkdir(parents=True)
        (codex_home / "auth.json").write_text('{"token":"codex_file_secret"}')
        app.dependency_overrides[get_settings] = lambda: _provider_preflight_settings(tmp_path)
        headers = {"Idempotency-Key": "provider-readiness-replay"}

        first = await client.post("/v1/workspaces", json=_V2_MINIMAL_BODY, headers=headers)
        (codex_home / "auth.json").unlink()
        codex_home.rmdir()
        replay = await client.post("/v1/workspaces", json=_V2_MINIMAL_BODY, headers=headers)

        assert first.status_code == 202
        assert replay.status_code == 202
        assert replay.json()["workspace_id"] == first.json()["workspace_id"]
        assert replay.json()["provider_readiness_preflight"]["readiness_status"] == "ready"

    @pytest.mark.unit
    async def test_idempotent_replay_preserves_ready_override_request(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
        tmp_path: Any,
    ) -> None:
        app, client = disk_app_and_client
        home = tmp_path / "home"
        codex_home = home / ".codex"
        codex_home.mkdir(parents=True)
        (codex_home / "auth.json").write_text('{"token":"codex_file_secret"}')
        app.dependency_overrides[get_settings] = lambda: _provider_preflight_settings(tmp_path)
        payload = {
            **_V2_MINIMAL_BODY,
            "preflight": {
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "operator always requires audit",
            },
        }
        headers = {"Idempotency-Key": "provider-readiness-ready-override-replay"}

        first = await client.post("/v1/workspaces", json=payload, headers=headers)
        replay = await client.post("/v1/workspaces", json=payload, headers=headers)

        assert first.status_code == 202
        assert replay.status_code == 202
        preflight = replay.json()["provider_readiness_preflight"]
        assert preflight["readiness_status"] == "ready"
        assert preflight["override_requested"] is True
        assert preflight["override_used"] is False

    @pytest.mark.unit
    async def test_idempotent_replay_accepts_redacted_override_reason(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
        tmp_path: Any,
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: Settings(
            _env_file=None,
            host_home=str(tmp_path / "home"),
            docker_host="",
            github_token="opaque-local-secret",
        )
        payload = {
            **_V2_MINIMAL_BODY,
            "preflight": {
                "provider_readiness_override": True,
                "provider_readiness_override_reason": (
                    "operator verified opaque-local-secret "
                    "and sk-abcdefghijklmnopqrstuvwxyz manually"
                ),
            },
        }
        headers = {"Idempotency-Key": "provider-readiness-redacted-reason-replay"}

        first = await client.post("/v1/workspaces", json=payload, headers=headers)
        replay = await client.post("/v1/workspaces", json=payload, headers=headers)

        assert first.status_code == 202
        assert (
            first.json()["provider_readiness_preflight"]["override_reason"]
            == "operator verified <redacted> and <redacted> manually"
        )
        assert replay.status_code == 202
        assert replay.json()["workspace_id"] == first.json()["workspace_id"]

    @pytest.mark.unit
    async def test_idempotent_replay_rejects_changed_literal_redacted_reason(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
        tmp_path: Any,
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: _provider_preflight_settings(tmp_path)
        payload = {
            **_V2_MINIMAL_BODY,
            "preflight": {
                "provider_readiness_override": True,
                "provider_readiness_override_reason": ("operator typed <redacted> manually"),
            },
        }
        replay_payload = {
            **_V2_MINIMAL_BODY,
            "preflight": {
                "provider_readiness_override": True,
                "provider_readiness_override_reason": ("operator typed changed text manually"),
            },
        }
        headers = {"Idempotency-Key": "provider-readiness-literal-redacted-conflict"}

        first = await client.post("/v1/workspaces", json=payload, headers=headers)
        replay = await client.post("/v1/workspaces", json=replay_payload, headers=headers)

        assert first.status_code == 202
        assert replay.status_code == 409
        assert replay.json()["error_code"] == "IDEMPOTENCY_CONFLICT"

    @pytest.mark.unit
    async def test_idempotent_replay_accepts_redacted_override_reason_after_secret_rotation(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
        tmp_path: Any,
    ) -> None:
        app, client = disk_app_and_client
        old_settings = Settings(
            _env_file=None,
            host_home=str(tmp_path / "home"),
            docker_host="",
            github_token="initial-local-secret",
        )
        new_settings = Settings(
            _env_file=None,
            host_home=str(tmp_path / "home"),
            docker_host="",
            github_token="rotated-local-secret",
        )
        active_settings = old_settings
        app.dependency_overrides[get_settings] = lambda: active_settings
        payload = {
            **_V2_MINIMAL_BODY,
            "preflight": {
                "provider_readiness_override": True,
                "provider_readiness_override_reason": (
                    "operator verified initial-local-secret manually"
                ),
            },
        }
        headers = {"Idempotency-Key": "provider-readiness-redacted-rotated-reason-replay"}

        first = await client.post("/v1/workspaces", json=payload, headers=headers)
        active_settings = new_settings
        replay = await client.post("/v1/workspaces", json=payload, headers=headers)

        assert first.status_code == 202
        assert first.json()["provider_readiness_preflight"]["override_reason"] == (
            "operator verified <redacted> manually"
        )
        assert replay.status_code == 202
        assert replay.json()["workspace_id"] == first.json()["workspace_id"]

    @pytest.mark.unit
    async def test_workspace_detail_list_and_overview_include_stored_preflight(
        self,
        disk_app_and_client: tuple[Any, AsyncClient],
        tmp_path: Any,
    ) -> None:
        app, client = disk_app_and_client
        app.dependency_overrides[get_settings] = lambda: _provider_preflight_settings(tmp_path)
        payload = {
            **_V2_MINIMAL_BODY,
            "preflight": {
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "operator verified local auth",
            },
        }
        created = await client.post("/v1/workspaces", json=payload)
        workspace_id = created.json()["workspace_id"]

        detail = await client.get(f"/v1/workspaces/{workspace_id}")
        listed = await client.get("/v1/workspaces")
        overview = await client.get("/v1/workspaces/overview")

        for item in (detail.json(), listed.json()[0], overview.json()["items"][0]):
            preflight = item["provider_readiness_preflight"]
            assert preflight["provider"] == "codex"
            assert preflight["model"] == "gpt-5.5"
            assert preflight["override_used"] is True

    @pytest.mark.unit
    async def test_direct_v2_replay_returns_existing_row_and_conflict_response(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_codex_auth_env(monkeypatch)
        headers = {"Idempotency-Key": "direct-v2-replay"}
        first = await client.post("/v1/workspaces", json=_V2_MINIMAL_BODY, headers=headers)
        assert first.status_code == 202

        factory = make_session_factory(engine)
        async with factory() as session:
            replay = await workspaces_route.create_workspace(
                WorkspaceCreateRequest.model_validate(_V2_MINIMAL_BODY),
                _request_with_disk_check(),
                idempotency_key="direct-v2-replay",
                settings=Settings(_env_file=None),
                session=session,
            )
            conflict = await workspaces_route.create_workspace(
                WorkspaceCreateRequest.model_validate(
                    {
                        **_V2_MINIMAL_BODY,
                        "task": {
                            **_V2_MINIMAL_BODY["task"],
                            "title": "Changed replay title",
                        },
                    }
                ),
                _request_with_disk_check(),
                idempotency_key="direct-v2-replay",
                settings=Settings(_env_file=None),
                session=session,
            )
            tier_conflict = await workspaces_route.create_workspace(
                WorkspaceCreateRequest.model_validate(
                    {
                        **_V2_MINIMAL_BODY,
                        "validation": {
                            **_V2_MINIMAL_BODY["validation"],
                            "requested_tier": 2,
                        },
                    }
                ),
                _request_with_disk_check(),
                idempotency_key="direct-v2-replay",
                settings=Settings(_env_file=None),
                session=session,
            )
            resource_conflict = await workspaces_route.create_workspace(
                WorkspaceCreateRequest.model_validate(
                    {
                        **_V2_MINIMAL_BODY,
                        "resources": {"steady_state_cpu_cores": 4.0},
                    }
                ),
                _request_with_disk_check(),
                idempotency_key="direct-v2-replay",
                settings=Settings(_env_file=None),
                session=session,
            )

        assert replay.workspace_id == first.json()["workspace_id"]
        assert replay.warnings == []
        assert isinstance(conflict, JSONResponse)
        assert conflict.status_code == 409
        assert json.loads(conflict.body)["error_code"] == "IDEMPOTENCY_CONFLICT"
        assert isinstance(tier_conflict, JSONResponse)
        assert tier_conflict.status_code == 409
        assert json.loads(tier_conflict.body)["error_code"] == "IDEMPOTENCY_CONFLICT"
        assert isinstance(resource_conflict, JSONResponse)
        assert resource_conflict.status_code == 409
        assert json.loads(resource_conflict.body)["error_code"] == "IDEMPOTENCY_CONFLICT"

    @pytest.mark.unit
    async def test_v2_warm_cache_replay_uses_durable_auto_profile_match(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_codex_auth_env(monkeypatch)
        headers = {
            "Idempotency-Key": _unique_idempotency_key("v2-null-profile-then-omitted-workspace")
        }
        initial_payload = json.loads(json.dumps(_V2_MINIMAL_BODY))
        initial_payload["workspace"] = {"profile_ref": None, "profile": None}
        replay_payload = json.loads(json.dumps(_V2_MINIMAL_BODY))
        replay_payload.pop("workspace")

        first = await client.post("/v1/workspaces", json=initial_payload, headers=headers)
        replay = await client.post("/v1/workspaces", json=replay_payload, headers=headers)

        assert first.status_code == 202
        assert replay.status_code == 202
        assert replay.json()["workspace_id"] == first.json()["workspace_id"]

    @pytest.mark.unit
    async def test_v2_rejects_external_id_reuse_for_different_scope(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_codex_auth_env(monkeypatch)
        first_payload = _v2_body(title="docs slice", owned_paths=["docs/**"])
        first_payload["task"]["external_id"] = "WAVE-1"  # type: ignore[index]
        second_payload = _v2_body(
            title="api slice",
            owned_paths=["src/awf/api/**"],
        )
        second_payload["task"]["external_id"] = "WAVE-1"  # type: ignore[index]

        first = await client.post("/v1/workspaces", json=first_payload)
        second = await client.post("/v1/workspaces", json=second_payload)

        assert first.status_code == 202
        assert second.status_code == 409
        body = second.json()
        assert body == {
            "error_code": "TASK_EXTERNAL_ID_CONFLICT",
            "message": (
                "External task ID is already associated with a different "
                "repo/base/task-class/owned-path scope; use a unique external "
                "task ID for this backlog slice or retry the original scope."
            ),
            "detail": {"external_id": "WAVE-1"},
        }
        _assert_no_internal_error_fields(body)

    @pytest.mark.unit
    async def test_v2_external_id_scope_conflict_rolls_back_rejected_workspace(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_codex_auth_env(monkeypatch)
        repo_url = "git@github.com:example/external-id-conflict-rollback.git"
        external_id = "WAVE-ROLLBACK"
        first_payload = _v2_body(
            repo_url=repo_url,
            title="docs slice",
            owned_paths=["docs/**"],
        )
        first_payload["task"]["external_id"] = external_id  # type: ignore[index]
        second_payload = _v2_body(
            repo_url=repo_url,
            title="api slice",
            owned_paths=["src/awf/api/**"],
        )
        second_payload["task"]["external_id"] = external_id  # type: ignore[index]

        first = await client.post("/v1/workspaces", json=first_payload)
        second = await client.post("/v1/workspaces", json=second_payload)

        assert first.status_code == 202
        assert second.status_code == 409

        factory = make_session_factory(engine)
        async with factory() as session:
            rows = await WorkspaceRepository(session).list(repo_url=repo_url, limit=10)

        matching_rows = [row for row in rows if row.task_external_id == external_id]
        assert [row.task_title for row in matching_rows] == ["docs slice"]

    @pytest.mark.unit
    async def test_v2_rejects_external_id_reuse_for_different_title(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_codex_auth_env(monkeypatch)
        first_payload = _v2_body(
            title="docs(onboarding): add prompts",
            owned_paths=["docs/**", "README.md"],
        )
        first_payload["task"]["external_id"] = "WAVE-1"  # type: ignore[index]
        second_payload = _v2_body(
            title="docs(install): document install flow",
            owned_paths=["docs/**", "README.md"],
        )
        second_payload["task"]["external_id"] = "WAVE-1"  # type: ignore[index]

        first = await client.post("/v1/workspaces", json=first_payload)
        second = await client.post("/v1/workspaces", json=second_payload)

        assert first.status_code == 202
        assert second.status_code == 409
        assert second.json()["error_code"] == "TASK_EXTERNAL_ID_CONFLICT"

    @pytest.mark.unit
    async def test_v2_invalid_profile_ref_returns_structured_422(
        self,
        client: AsyncClient,
    ) -> None:
        payload = {
            **_V2_MINIMAL_BODY,
            "workspace": {"profile_ref": "missing-profile", "profile": None},
        }

        response = await client.post("/v1/workspaces", json=payload)

        assert response.status_code == 422
        assert response.json()["error_code"] == "INVALID_PROFILE"

    @pytest.mark.unit
    async def test_v2_invalid_inline_profile_returns_profile_lint_detail_without_secret(
        self,
        client: AsyncClient,
    ) -> None:
        raw_secret = "sk-live-do-not-echo-from-api"
        payload = {
            **_V2_MINIMAL_BODY,
            "workspace": {
                "profile_ref": "auto",
                "profile": {
                    "name": "bad-inline",
                    "secrets": [
                        {
                            "name": "api-token",
                            "kind": "env",
                            "target": "API_TOKEN",
                            "provider": "inline",
                            "ref": raw_secret,
                        }
                    ],
                },
            },
        }

        response = await client.post("/v1/workspaces", json=payload)
        body = response.json()

        assert response.status_code == 422
        assert body["error_code"] == "INVALID_PROFILE"
        assert body["detail"]["reason_code"] == "SECRET_REF_LOOKS_RAW"
        assert body["detail"]["findings"][0]["reason_code"] == "SECRET_REF_LOOKS_RAW"
        assert raw_secret not in json.dumps(body)

    @pytest.mark.unit
    async def test_direct_v2_create_success_returns_accepted_response(
        self,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_codex_auth_env(monkeypatch)
        factory = make_session_factory(engine)
        async with factory() as session:
            accepted = await workspaces_route.create_workspace(
                WorkspaceCreateRequest.model_validate(_V2_MINIMAL_BODY),
                _request_with_disk_check(),
                idempotency_key=None,
                settings=Settings(_env_file=None),
                session=session,
            )

        assert accepted.workspace_id.startswith("ws_")
        assert accepted.status == WorkspaceStatus.requested
        assert accepted.warnings == []

    @pytest.mark.unit
    async def test_direct_v2_create_with_fresh_idempotency_key_creates_workspace(
        self,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_codex_auth_env(monkeypatch)
        factory = make_session_factory(engine)
        async with factory() as session:
            accepted = await workspaces_route.create_workspace(
                WorkspaceCreateRequest.model_validate(_V2_MINIMAL_BODY),
                _request_with_disk_check(),
                idempotency_key="fresh-direct-v2-key",
                settings=Settings(_env_file=None),
                session=session,
            )
            workspace = await WorkspaceRepository(session).get_by_idempotency_key(
                "fresh-direct-v2-key"
            )

        assert accepted.workspace_id.startswith("ws_")
        assert workspace is not None
        assert workspace.id == accepted.workspace_id


class TestCreateWorkspacePolicyMetadata:
    @pytest.mark.unit
    async def test_persists_policy_metadata_and_exposes_workspace_responses(
        self,
        client: AsyncClient,
    ) -> None:
        payload = {
            **_V2_MINIMAL_BODY,
            "task": {
                **_V2_MINIMAL_BODY["task"],
                "task_class": "refactor_task",
                "owned_paths": ["src/awf/**/*.py", "tests/unit/**"],
            },
        }

        create = await client.post("/v1/workspaces", json=payload)
        assert create.status_code == 202

        ws_id = create.json()["workspace_id"]
        response = await client.get(f"/v1/workspaces/{ws_id}")
        listed = await client.get("/v1/workspaces")

        assert response.status_code == 200
        body = response.json()
        assert body["task_class"] == "refactor_task"
        assert body["owned_paths"] == ["src/awf/**/*.py", "tests/unit/**"]
        assert listed.status_code == 200
        assert listed.json()[0]["task_class"] == "refactor_task"
        assert listed.json()[0]["owned_paths"] == ["src/awf/**/*.py", "tests/unit/**"]

    @pytest.mark.unit
    async def test_get_workspace_exposes_sanitized_app_endpoint_metadata(
        self,
        client: AsyncClient,
    ) -> None:
        payload = {
            **_V2_MINIMAL_BODY,
            "workspace": {"profile_ref": "inline", "profile": _endpoint_profile_body()},
        }

        create = await client.post("/v1/workspaces", json=payload)
        assert create.status_code == 202

        ws_id = create.json()["workspace_id"]
        response = await client.get(f"/v1/workspaces/{ws_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["app_endpoints"] == [
            {
                "name": "app",
                "service": "app",
                "scheme": "http",
                "port": 3000,
                "path": "/",
                "internal_url": "http://app:3000/",
                "visibility": "agent",
                "health": {
                    "path": "/healthz",
                    "method": "GET",
                    "expected_status": 200,
                    "internal_url": "http://app:3000/healthz",
                },
            },
            {
                "name": "operator_notes",
                "service": "app",
                "scheme": "http",
                "port": 3000,
                "path": "/operator",
                "internal_url": "http://app:3000/operator",
                "visibility": "console",
                "health": None,
            },
        ]
        rendered = str(body["app_endpoints"])
        assert "internal_metrics" not in rendered
        assert "user:password" not in rendered
        assert "token=abc" not in rendered
        assert "secret/data/api-token" not in rendered

        profile_payload = {
            "requested_profile": body["requested_profile"],
            "resolved_profile": body["resolved_profile"],
        }
        assert profile_payload["requested_profile"] is not None
        assert profile_payload["resolved_profile"] is not None
        assert "environment" not in profile_payload["requested_profile"]["runtime"]
        assert "ref" not in profile_payload["resolved_profile"]["secrets"][0]
        rendered_profiles = json.dumps(profile_payload, sort_keys=True)
        assert "user:password" not in rendered_profiles
        assert "token=abc" not in rendered_profiles
        assert "secret/data/api-token" not in rendered_profiles

    @pytest.mark.unit
    async def test_detail_and_overview_expose_network_posture_from_resolved_profile(
        self,
        client: AsyncClient,
    ) -> None:
        payload = {
            **_V2_MINIMAL_BODY,
            "workspace": {
                "profile_ref": "inline",
                "profile": {
                    "name": "open-local-dogfood",
                    "security": {"egress": {"mode": "open"}},
                },
            },
        }

        create = await client.post("/v1/workspaces", json=payload)
        assert create.status_code == 202

        ws_id = create.json()["workspace_id"]
        detail = await client.get(f"/v1/workspaces/{ws_id}")
        overview = await client.get("/v1/workspaces/overview")

        assert detail.status_code == 200
        assert overview.status_code == 200
        assert detail.json()["network_posture"] == "open"
        overview_item = next(
            item for item in overview.json()["items"] if item["workspace_id"] == ws_id
        )
        assert overview_item["network_posture"] == "open"

    @pytest.mark.unit
    async def test_inline_profile_accepts_and_returns_supply_chain_policy(
        self,
        client: AsyncClient,
    ) -> None:
        payload = {
            **_V2_MINIMAL_BODY,
            "workspace": {
                "profile_ref": "inline",
                "profile": {
                    "name": "supply-chain-guarded",
                    "security": {
                        "supply_chain": {
                            "unpinned_dependency_installs": {"mode": "block"},
                            "remote_script_execution": {"mode": "block"},
                            "unexpected_registry_hosts": {
                                "mode": "warn",
                                "allowed_hosts": ["https://registry.npmjs.org/"],
                            },
                            "lockfile_changes_outside_owned_paths": {"mode": "warn"},
                        }
                    },
                },
            },
        }

        create = await client.post("/v1/workspaces", json=payload)
        assert create.status_code == 202

        detail = await client.get(f"/v1/workspaces/{create.json()['workspace_id']}")

        assert detail.status_code == 200
        supply_chain = detail.json()["resolved_profile"]["security"]["supply_chain"]
        assert supply_chain["unpinned_dependency_installs"]["mode"] == "block"
        assert supply_chain["remote_script_execution"]["mode"] == "block"
        assert supply_chain["unexpected_registry_hosts"] == {
            "mode": "warn",
            "allowed_hosts": ["registry.npmjs.org"],
        }

    @pytest.mark.unit
    async def test_persists_agent_model_and_effort_override_in_task_policy(
        self,
        client: AsyncClient,
    ) -> None:
        payload = _v2_body(model="ollama/glm-5.1:cloud", effort="high")

        create = await client.post("/v1/workspaces", json=payload)
        assert create.status_code == 202

        ws_id = create.json()["workspace_id"]
        response = await client.get(f"/v1/workspaces/{ws_id}")
        overview = await client.get("/v1/workspaces/overview")
        tasks = await client.get("/v1/tasks")

        assert response.status_code == 200
        assert response.json()["task_policy"]["agent_model"] == "ollama/glm-5.1:cloud"
        assert response.json()["task_policy"]["agent_effort"] == "high"
        _assert_effective_identity(
            response.json(),
            model="ollama/glm-5.1:cloud",
            effort="high",
            model_source="task_policy",
            effort_source="task_policy",
        )
        _assert_usage_unavailable(response.json())
        assert overview.status_code == 200
        _assert_effective_identity(
            overview.json()["items"][0],
            model="ollama/glm-5.1:cloud",
            effort="high",
            model_source="task_policy",
            effort_source="task_policy",
        )
        _assert_usage_unavailable(overview.json()["items"][0])
        assert tasks.status_code == 200
        _assert_effective_identity(
            tasks.json()["items"][0],
            model="ollama/glm-5.1:cloud",
            effort="high",
            model_source="task_policy",
            effort_source="task_policy",
        )
        _assert_usage_unavailable(tasks.json()["items"][0])

    @pytest.mark.unit
    async def test_exposes_default_effective_agent_identity_on_workspace_surfaces(
        self,
        client: AsyncClient,
    ) -> None:
        payload = _v2_body(title="default gemini model")
        payload["task"] = {
            **payload["task"],  # type: ignore[index]
            "agent": "gemini",
        }
        payload["preflight"] = {
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "identity projection test",
        }

        create = await client.post("/v1/workspaces", json=payload)
        assert create.status_code == 202
        workspace_id = create.json()["workspace_id"]

        detail = await client.get(f"/v1/workspaces/{workspace_id}")
        listed = await client.get("/v1/workspaces")
        overview = await client.get("/v1/workspaces/overview")
        tasks = await client.get("/v1/tasks")

        assert detail.status_code == 200
        assert listed.status_code == 200
        assert overview.status_code == 200
        assert tasks.status_code == 200
        rows = [
            detail.json(),
            listed.json()[0],
            overview.json()["items"][0],
            tasks.json()["items"][0],
        ]
        for row in rows:
            _assert_effective_identity(row, model="gemini-3.1-pro-preview")
            _assert_usage_unavailable(row)

    @pytest.mark.unit
    async def test_legacy_v1_workspace_exposes_default_effective_identity(
        self,
        client: AsyncClient,
    ) -> None:
        workspace_id = await _create_workspace(client, agent="claude_code")

        detail = await client.get(f"/v1/workspaces/{workspace_id}")
        overview = await client.get("/v1/workspaces/overview")

        assert detail.status_code == 200
        assert overview.status_code == 200
        _assert_effective_identity(detail.json(), model="claude-opus-4-7")
        _assert_effective_identity(overview.json()["items"][0], model="claude-opus-4-7")

    @pytest.mark.unit
    async def test_v2_idempotency_replay_ignores_defaulted_identity_projection(
        self,
        client: AsyncClient,
    ) -> None:
        headers = {"Idempotency-Key": "default-model-idempotency"}

        first = await client.post("/v1/workspaces", json=_V2_MINIMAL_BODY, headers=headers)
        replay = await client.post("/v1/workspaces", json=_V2_MINIMAL_BODY, headers=headers)

        assert first.status_code == 202
        assert replay.status_code == 202
        assert replay.json()["workspace_id"] == first.json()["workspace_id"]

    @pytest.mark.unit
    async def test_workspace_response_exposes_latest_decision_and_active_reservation(
        self,
        client: AsyncClient,
    ) -> None:
        payload = {
            **_V2_MINIMAL_BODY,
            "task": {
                **_V2_MINIMAL_BODY["task"],
                "task_class": "dependency_task",
                "priority": 25,
                "owned_paths": ["pyproject.toml", "uv.lock"],
            },
            "workspace": {
                "profile_ref": "inline",
                "profile": {
                    "name": "api-dind",
                    "docker": {"mode": "dind"},
                },
            },
            "resources": {
                "steady_state_cpu_cores": 4.0,
                "steady_state_memory_gb": 12.0,
                "peak_cpu_cores": 8.0,
                "peak_memory_gb": 24.0,
                "disk_mb": 4096,
            },
        }

        create = await client.post("/v1/workspaces", json=payload)
        assert create.status_code == 202

        ws_id = create.json()["workspace_id"]
        response = await client.get(f"/v1/workspaces/{ws_id}")

        assert response.status_code == 200
        body = response.json()
        decision = body["latest_queue_decision"]
        reservation = body["active_resource_reservation"]
        assert decision["id"].startswith("qd_")
        assert decision["decision"] == "admitted"
        assert decision["reason_code"] == "ADMITTED_LOCAL"
        assert decision["class_priority"] == 4
        assert decision["computed_priority"] == 37
        assert decision["age_boost"] == 0
        assert decision["retry_bonus"] == 0
        assert decision["score_summary"]["base_priority"] == 25
        assert decision["score_summary"]["class_bias"] == 12
        assert decision["score_summary"]["effective_score"] == 37
        assert decision["score_summary"]["human_boost"] == 0
        assert decision["resource_summary"]["peak_cpu"] == 8.0
        assert decision["resource_summary"]["disk_mb"] == 4096
        assert decision["resource_summary"]["dind_slots"] == 1
        assert decision["resource_summary"]["dind_mode"] == "dind"
        assert decision["overlap_risk_summary"]["overlap_count"] == 0
        assert reservation["id"].startswith("rr_")
        assert reservation["node_id"] == "local"
        assert reservation["steady_cpu"] == 4.0
        assert reservation["steady_memory_gb"] == 12.0
        assert reservation["peak_cpu"] == 8.0
        assert reservation["peak_memory_gb"] == 24.0
        assert reservation["disk_mb"] == 4096
        assert reservation["dind_slots"] == 1
        assert reservation["phase"] == "workspace_lifecycle"
        assert reservation["released_at"] is None

    @pytest.mark.unit
    async def test_workspace_detail_and_secret_lease_route_expose_sanitized_status(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        create = await client.post("/v1/workspaces", json=_V2_MINIMAL_BODY)
        assert create.status_code == 202
        ws_id = create.json()["workspace_id"]
        raw_ref = "sk-live-do-not-appear-in-api"
        now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
        factory = make_session_factory(engine)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
            assert workspace is not None
            await SecretLeaseRepository(session).issue_declared_leases(
                workspace,
                leases=[
                    SecretLeaseIssue(
                        secret_name="api-token",
                        kind="env",
                        target="API_TOKEN",
                        mode="ro",
                        required=True,
                        provider="vault",
                        ref_digest="sha256:" + "7" * 64,
                        expires_at=now + timedelta(hours=1),
                        issue_metadata={
                            "profile": "api",
                            "declaration_index": 0,
                            "raw_ref": raw_ref,
                        },
                    )
                ],
                now=now,
            )
            await session.commit()

        detail = await client.get(f"/v1/workspaces/{ws_id}")
        route = await client.get(f"/v1/workspaces/{ws_id}/secret-leases")

        assert detail.status_code == 200
        assert route.status_code == 200
        detail_body = detail.json()
        route_body = route.json()
        assert detail_body["secret_leases"][0]["lease_id"].startswith("sl_")
        assert detail_body["secret_leases"][0]["secret_name"] == "api-token"
        assert route_body["items"] == detail_body["secret_leases"]
        assert raw_ref not in json.dumps(detail_body)
        assert raw_ref not in json.dumps(route_body)

    @pytest.mark.unit
    async def test_secret_lease_route_missing_workspace_matches_child_route_404(
        self,
        client: AsyncClient,
    ) -> None:
        response = await client.get("/v1/workspaces/ws_missing/secret-leases")

        assert response.status_code == 404
        assert response.json()["detail"] == {
            "error_code": "NOT_FOUND",
            "message": "No workspace with id ws_missing",
        }

    @pytest.mark.unit
    async def test_rejects_zero_disk_reservation(self, client: AsyncClient) -> None:
        payload = {
            **_V2_MINIMAL_BODY,
            "resources": {"disk_mb": 0},
        }

        response = await client.post("/v1/workspaces", json=payload)

        assert response.status_code == 422

    @pytest.mark.unit
    async def test_legacy_v1_defaults_policy_metadata(self, client: AsyncClient) -> None:
        ws_id = await _create_workspace(client)

        response = await client.get(f"/v1/workspaces/{ws_id}")

        assert response.status_code == 200
        assert response.json()["task_class"] is None
        assert response.json()["owned_paths"] == []

    @pytest.mark.unit
    async def test_lifecycle_summary_exposed_on_detail_and_overview(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_workspace(client)
        await _transition_workspace(
            engine,
            workspace_id,
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
        )
        base = datetime.now(UTC) - timedelta(minutes=5)
        factory = make_session_factory(engine)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            state_events = [
                event
                for event in workspace.events
                if event.event_type in {"workspace.created", "workspace.state_changed"}
            ]
            for event, occurred_at in zip(
                sorted(state_events, key=lambda item: item.occurred_at),
                [
                    base,
                    base + timedelta(seconds=10),
                    base + timedelta(seconds=25),
                    base + timedelta(seconds=40),
                ],
                strict=True,
            ):
                event.occurred_at = occurred_at
            await session.commit()

        detail = await client.get(f"/v1/workspaces/{workspace_id}")
        overview = await client.get("/v1/workspaces/overview")

        assert detail.status_code == 200
        assert overview.status_code == 200
        for body in [detail.json(), overview.json()["items"][0]]:
            stages = {item["stage"]: item for item in body["lifecycle"]}
            assert stages["requested"]["started_at"] == base.isoformat().replace("+00:00", "Z")
            assert stages["requested"]["ended_at"] == (
                base + timedelta(seconds=10)
            ).isoformat().replace("+00:00", "Z")
            assert stages["requested"]["duration_seconds"] == 10
            assert stages["requested"]["status"] == "completed"
            assert stages["running"]["started_at"] == (
                base + timedelta(seconds=40)
            ).isoformat().replace("+00:00", "Z")
            assert stages["running"]["ended_at"] is None
            assert stages["running"]["duration_seconds"] >= 0
            assert stages["running"]["status"] == "active"

    @pytest.mark.unit
    async def test_terminal_lifecycle_marks_future_stages_skipped(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_workspace(client)
        await _transition_workspace(
            engine,
            workspace_id,
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.failed,
        )
        base = datetime.now(UTC) - timedelta(minutes=5)
        failed_at = base + timedelta(seconds=75)
        factory = make_session_factory(engine)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            state_events = [
                event
                for event in workspace.events
                if event.event_type in {"workspace.created", "workspace.state_changed"}
            ]
            for event, occurred_at in zip(
                sorted(state_events, key=lambda item: item.occurred_at),
                [
                    base,
                    base + timedelta(seconds=10),
                    base + timedelta(seconds=25),
                    base + timedelta(seconds=40),
                    base + timedelta(seconds=60),
                    failed_at,
                ],
                strict=True,
            ):
                event.occurred_at = occurred_at
            await session.commit()

        response = await client.get(f"/v1/workspaces/{workspace_id}")

        assert response.status_code == 200
        stages = {item["stage"]: item for item in response.json()["lifecycle"]}
        assert stages["validating"]["ended_at"] == failed_at.isoformat().replace("+00:00", "Z")
        assert stages["validating"]["duration_seconds"] == 15
        assert stages["validating"]["status"] == "completed"
        assert stages["pushing"]["status"] == "terminal_skipped"
        assert stages["monitoring_pr"]["status"] == "terminal_skipped"
        assert stages["completed"]["status"] == "terminal_skipped"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "task_class",
        ["feature_task", "docs", ""],
    )
    async def test_rejects_unknown_task_class(
        self,
        client: AsyncClient,
        task_class: str,
    ) -> None:
        payload = {
            **_V2_MINIMAL_BODY,
            "task": {**_V2_MINIMAL_BODY["task"], "task_class": task_class},
        }

        response = await client.post("/v1/workspaces", json=payload)

        assert response.status_code == 422

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "owned_paths",
        [
            [""],
            ["a" * 513],
            [f"src/module_{idx}.py" for idx in range(129)],
        ],
    )
    async def test_rejects_owned_path_bounds(
        self,
        client: AsyncClient,
        owned_paths: list[str],
    ) -> None:
        payload = {
            **_V2_MINIMAL_BODY,
            "task": {**_V2_MINIMAL_BODY["task"], "owned_paths": owned_paths},
        }

        response = await client.post("/v1/workspaces", json=payload)

        assert response.status_code == 422

    @pytest.mark.unit
    async def test_idempotency_conflicts_when_policy_metadata_changes(
        self,
        client: AsyncClient,
    ) -> None:
        headers = {"Idempotency-Key": "policy-metadata-key"}
        first_payload = {
            **_V2_MINIMAL_BODY,
            "task": {
                **_V2_MINIMAL_BODY["task"],
                "task_class": "docs_task",
                "owned_paths": ["README.md"],
            },
        }
        replay_payload = {
            **_V2_MINIMAL_BODY,
            "task": {
                **_V2_MINIMAL_BODY["task"],
                "task_class": "test_task",
                "owned_paths": ["README.md"],
            },
        }

        first = await client.post("/v1/workspaces", json=first_payload, headers=headers)
        replay = await client.post("/v1/workspaces", json=replay_payload, headers=headers)

        assert first.status_code == 202
        assert replay.status_code == 409
        body = replay.json()
        assert body["error_code"] == "IDEMPOTENCY_CONFLICT"
        _assert_no_internal_error_fields(body)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("field", "first_value", "replay_value"),
        [
            ("priority", 25, 26),
            ("human_boost", 2, 3),
        ],
    )
    async def test_idempotency_conflicts_when_scheduler_policy_changes(
        self,
        client: AsyncClient,
        field: str,
        first_value: int,
        replay_value: int,
    ) -> None:
        headers = {"Idempotency-Key": f"scheduler-policy-{field}-key"}
        first_payload = {
            **_V2_MINIMAL_BODY,
            "task": {
                **_V2_MINIMAL_BODY["task"],
                field: first_value,
            },
        }
        replay_payload = {
            **_V2_MINIMAL_BODY,
            "task": {
                **_V2_MINIMAL_BODY["task"],
                field: replay_value,
            },
        }

        first = await client.post("/v1/workspaces", json=first_payload, headers=headers)
        replay = await client.post("/v1/workspaces", json=replay_payload, headers=headers)

        assert first.status_code == 202
        assert replay.status_code == 409
        assert replay.json()["error_code"] == "IDEMPOTENCY_CONFLICT"

    @pytest.mark.unit
    async def test_accepts_task_out_of_scope_change_policy_metadata(
        self,
        client: AsyncClient,
    ) -> None:
        payload = {
            **_V2_MINIMAL_BODY,
            "task": {
                **_V2_MINIMAL_BODY["task"],
                "owned_paths": ["src/owned/**"],
                "out_of_scope_changes": {
                    "mode": "block",
                    "allowlist_patterns": ["docs/generated/**"],
                },
            },
        }

        create_response = await client.post("/v1/workspaces", json=payload)
        assert create_response.status_code == 202

        detail_response = await client.get(
            f"/v1/workspaces/{create_response.json()['workspace_id']}"
        )

        assert detail_response.status_code == 200
        task_policy = detail_response.json()["task_policy"]
        assert task_policy["out_of_scope_changes"] == {
            "mode": "block",
            "allowlist_patterns": ["docs/generated/**"],
        }

    @pytest.mark.unit
    def test_stored_profile_and_policy_helpers_handle_missing_or_malformed_data(self) -> None:
        workspace = SimpleNamespace(
            resolved_profile={"validation": {"requested_tier": 2}},
            task_policy={
                "agent_model": "gpt-test",
                "out_of_scope_changes": {"mode": "warn"},
            },
        )
        malformed_workspace = SimpleNamespace(
            resolved_profile={"validation": {"requested_tier": "2"}},
            task_policy={"agent_model": "", "out_of_scope_changes": "warn"},
        )
        missing_profile_workspace = SimpleNamespace(
            resolved_profile=None,
            task_policy={},
        )
        malformed_validation_workspace = SimpleNamespace(
            resolved_profile={"validation": "tier-two"},
            task_policy={},
        )
        non_mapping_profile_workspace = SimpleNamespace(
            resolved_profile=["legacy-corrupt-profile"],
            task_policy={},
        )
        bool_tier_workspace = SimpleNamespace(
            resolved_profile={"validation": {"requested_tier": True}},
            task_policy={},
        )
        policy_payload = WorkspaceCreateRequest.model_validate(
            {
                **_V2_MINIMAL_BODY,
                "task": {
                    **_V2_MINIMAL_BODY["task"],
                    "out_of_scope_changes": {"mode": "block"},
                },
            }
        )
        payload = WorkspaceCreateRequest.model_validate(_V2_MINIMAL_BODY)

        assert workspaces_service._resolved_profile_requested_tier(workspace) == 2  # type: ignore[arg-type]  # noqa: SLF001
        assert workspaces_service._resolved_profile_requested_tier(malformed_workspace) is None  # type: ignore[arg-type]  # noqa: SLF001
        assert (
            workspaces_service._resolved_profile_requested_tier(missing_profile_workspace) is None
        )  # type: ignore[arg-type]  # noqa: SLF001
        assert (
            workspaces_service._resolved_profile_requested_tier(  # noqa: SLF001
                malformed_validation_workspace
            )
            is None
        )  # type: ignore[arg-type]
        assert (
            workspaces_service._resolved_profile_requested_tier(  # noqa: SLF001
                non_mapping_profile_workspace
            )
            is None
        )  # type: ignore[arg-type]
        assert (
            workspaces_service._resolved_profile_requested_tier(  # noqa: SLF001
                bool_tier_workspace
            )
            is None
        )  # type: ignore[arg-type]
        assert workspaces_service._stored_task_agent_model(workspace) == "gpt-test"  # type: ignore[arg-type]  # noqa: SLF001
        assert workspaces_service._stored_task_agent_model(malformed_workspace) is None  # type: ignore[arg-type]  # noqa: SLF001
        assert workspaces_service._stored_task_out_of_scope_policy(workspace) == {  # type: ignore[arg-type]  # noqa: SLF001
            "mode": "warn"
        }
        assert workspaces_service._stored_task_out_of_scope_policy(malformed_workspace) is None  # type: ignore[arg-type]  # noqa: SLF001
        assert workspaces_service._requested_task_out_of_scope_policy(payload) is None  # noqa: SLF001
        assert workspaces_service._requested_task_out_of_scope_policy(policy_payload) == {  # noqa: SLF001
            "mode": "block",
            "allowlist_patterns": [],
        }


class TestCreateWorkspaceOwnedPathPolicy:
    @pytest.mark.unit
    async def test_no_requested_owned_paths_do_not_block(
        self,
        client: AsyncClient,
    ) -> None:
        await _create_workspace(
            client,
            title="existing",
            owned_paths=["src/awf/api/**"],
        )

        response = await client.post(
            "/v1/workspaces",
            json=_v2_body(title="new without owned paths", owned_paths=[]),
        )

        assert response.status_code == 202

    @pytest.mark.unit
    async def test_non_overlapping_owned_paths_are_allowed(
        self,
        client: AsyncClient,
    ) -> None:
        await _create_workspace(
            client,
            title="existing",
            owned_paths=["src/awf/api/**"],
        )

        response = await client.post(
            "/v1/workspaces",
            json=_v2_body(title="docs", owned_paths=["docs/**"]),
        )

        assert response.status_code == 202

    @pytest.mark.unit
    async def test_same_paths_on_different_repo_or_base_branch_are_allowed(
        self,
        client: AsyncClient,
    ) -> None:
        await _create_workspace(
            client,
            repo_url="git@github.com:example/other.git",
            base_branch="development",
            title="other repo",
            owned_paths=["src/awf/api/**"],
        )
        await _create_workspace(
            client,
            repo_url="git@github.com:example/app.git",
            base_branch="main",
            title="other base",
            owned_paths=["src/awf/api/**"],
        )

        response = await client.post(
            "/v1/workspaces",
            json=_v2_body(owned_paths=["src/awf/api/routes/workspaces.py"]),
        )

        assert response.status_code == 202

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "existing_status",
        [
            WorkspaceStatus.completed,
            WorkspaceStatus.failed,
            WorkspaceStatus.cancelled,
            WorkspaceStatus.destroying,
            WorkspaceStatus.destroyed,
        ],
    )
    async def test_terminal_and_teardown_statuses_do_not_block(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
        existing_status: WorkspaceStatus,
    ) -> None:
        existing_id = await _create_workspace(
            client,
            title=f"existing {existing_status.value}",
            owned_paths=["src/awf/api/**"],
        )
        await _set_workspace_status(engine, existing_id, existing_status)

        response = await client.post(
            "/v1/workspaces",
            json=_v2_body(
                title=f"new after {existing_status.value}",
                owned_paths=["src/awf/api/routes/workspaces.py"],
            ),
        )

        assert response.status_code == 202

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("existing_path", "requested_path"),
        [
            (
                "src/awf/api/routes/workspaces.py",
                "src/awf/api/routes/workspaces.py",
            ),
            ("src/awf/api", "src/awf/api/routes/workspaces.py"),
            ("src/awf/api/**", "src/awf/api/routes/workspaces.py"),
        ],
    )
    async def test_active_exact_ancestor_and_wildcard_overlaps_return_202_warning(
        self,
        client: AsyncClient,
        existing_path: str,
        requested_path: str,
    ) -> None:
        existing_id = await _create_workspace(
            client,
            title=f"existing {existing_path}",
            task_class="refactor_task",
            owned_paths=[existing_path],
        )

        response = await client.post(
            "/v1/workspaces",
            json=_v2_body(
                title=f"new {requested_path}",
                task_class="docs_task",
                owned_paths=[requested_path],
            ),
        )

        assert response.status_code == 202
        body = response.json()
        assert body["warnings"] == [
            {
                "warning_code": "OWNED_PATH_OVERLAP_RISK",
                "message": (
                    "Owned paths overlap active workspaces; this may require rebase "
                    "or conflict resolution."
                ),
                "workspace_ids": [existing_id],
                "overlaps": [
                    {
                        "workspace_id": existing_id,
                        "existing_path": existing_path,
                        "requested_path": requested_path,
                    }
                ],
            }
        ]

        events = await client.get(
            f"/v1/workspaces/{body['workspace_id']}/events",
            params={"event_type": "workspace.owned_path_overlap_risk"},
        )

        assert events.status_code == 200
        event_items = events.json()["items"]
        assert len(event_items) == 1
        assert event_items[0]["reason_code"] == "OWNED_PATH_OVERLAP_RISK"
        assert event_items[0]["payload"]["workspace_ids"] == [existing_id]
        assert event_items[0]["payload"]["overlaps"] == [
            {
                "workspace_id": existing_id,
                "existing_path": existing_path,
                "requested_path": requested_path,
            }
        ]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "task_class",
        ["migration_task", "dependency_task", "build_config_task"],
    )
    async def test_high_risk_task_class_owned_path_overlap_is_advisory_without_exclusive_lock(
        self,
        client: AsyncClient,
        task_class: str,
    ) -> None:
        existing_id = await _create_workspace(
            client,
            title=f"existing {task_class}",
            task_class=task_class,
            owned_paths=["migrations/**"],
        )

        response = await client.post(
            "/v1/workspaces",
            json=_v2_body(
                title=f"new {task_class}",
                task_class=task_class,
                owned_paths=["migrations/202604260001_add_index.sql"],
            ),
        )

        assert response.status_code == 202
        body = response.json()
        assert body["warnings"][0]["warning_code"] == "OWNED_PATH_OVERLAP_RISK"
        assert body["warnings"][0]["workspace_ids"] == [existing_id]

    @pytest.mark.unit
    async def test_overlapping_owned_paths_remain_admitted_and_show_in_overlap_graph(
        self,
        client: AsyncClient,
    ) -> None:
        existing_id = await _create_workspace(
            client,
            title="existing migration",
            task_class="migration_task",
            owned_paths=["migrations/**"],
        )

        create = await client.post(
            "/v1/workspaces",
            json=_v2_body(
                title="new concrete migration",
                task_class="migration_task",
                owned_paths=["migrations/versions/202604290001_add_overlap_graph.py"],
            ),
        )

        assert create.status_code == 202
        create_body = create.json()
        new_id = create_body["workspace_id"]
        assert create_body["warnings"][0]["warning_code"] == "OWNED_PATH_OVERLAP_RISK"
        assert create_body["warnings"][0]["workspace_ids"] == [existing_id]

        detail = await client.get(f"/v1/workspaces/{new_id}")
        assert detail.status_code == 200
        decision = detail.json()["latest_queue_decision"]
        assert decision["decision"] == "admitted"
        assert decision["reason_code"] == "ADMITTED_LOCAL"
        assert decision["overlap_risk_summary"]["warning_code"] == "OWNED_PATH_OVERLAP_RISK"

        graph = await client.get(
            "/v1/locks/overlap-graph",
            params={
                "repo_url": "git@github.com:example/app.git",
                "base_branch": "development",
                "task_class": "migration_task",
            },
        )

        assert graph.status_code == 200
        edges = graph.json()["edges"]
        assert len(edges) == 1
        assert edges[0]["severity"] == "advisory"
        assert edges[0]["blocks_launch"] is False
        assert edges[0]["reason_code"] == "OWNED_PATH_OVERLAP_RISK"
        assert edges[0]["affected_workspace_ids"] == sorted([existing_id, new_id])

    @pytest.mark.unit
    async def test_detail_and_overview_expose_typed_coordination_warnings_for_overlaps(
        self,
        client: AsyncClient,
    ) -> None:
        existing_id = await _create_workspace(
            client,
            title="existing service work",
            task_class="refactor_task",
            owned_paths=["src/awf/service/**"],
        )
        create = await client.post(
            "/v1/workspaces",
            json=_v2_body(
                title="new service file work",
                task_class="docs_task",
                owned_paths=["src/awf/service/workspaces.py"],
            ),
        )

        assert create.status_code == 202
        assert create.json()["warnings"][0]["warning_code"] == "OWNED_PATH_OVERLAP_RISK"
        new_id = create.json()["workspace_id"]

        detail = await client.get(f"/v1/workspaces/{new_id}")
        overview = await client.get("/v1/workspaces/overview")

        assert detail.status_code == 200
        assert overview.status_code == 200
        overview_item = next(
            item for item in overview.json()["items"] if item["workspace_id"] == new_id
        )
        for body in (detail.json(), overview_item):
            warnings = body["coordination_warnings"]
            assert len(warnings) == 1
            warning = warnings[0]
            assert warning["warning_code"] == "OWNED_PATH_OVERLAP_RISK"
            assert warning["severity"] == "advisory"
            assert warning["blocks_launch"] is False
            assert warning["workspace_ids"] == [existing_id]
            assert warning["overlaps"] == [
                {
                    "workspace_id": existing_id,
                    "existing_path": "src/awf/service/**",
                    "requested_path": "src/awf/service/workspaces.py",
                    "match_reason_code": "OWNED_PATH_WILDCARD_MATCH",
                    "explanation": (
                        "Wildcard owned-path prefixes overlap: "
                        "src/awf/service/** <-> src/awf/service/workspaces.py."
                    ),
                }
            ]
            assert warning["stale_policy_context"] == {
                "trigger_type": "path_overlap",
                "stale_reason_code": "STALE_OVERLAP",
            }

        assert detail.json()["latest_queue_decision"]["decision"] == "admitted"
        assert detail.json()["latest_queue_decision"]["reason_code"] == "ADMITTED_LOCAL"
        assert detail.json()["task_policy"]["coordination"]["warnings"][0]["workspace_ids"] == [
            existing_id
        ]


class TestIdempotency:
    @pytest.mark.unit
    async def test_same_key_same_body_returns_same_workspace(self, client: AsyncClient) -> None:
        headers = {"Idempotency-Key": "abc-123"}
        r1 = await client.post("/v1/workspaces", json=_MINIMAL_BODY, headers=headers)
        r2 = await client.post("/v1/workspaces", json=_MINIMAL_BODY, headers=headers)

        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r1.json()["workspace_id"] == r2.json()["workspace_id"]

    @pytest.mark.unit
    async def test_same_key_different_body_returns_409(self, client: AsyncClient) -> None:
        headers = {"Idempotency-Key": "abc-123"}
        await client.post("/v1/workspaces", json=_MINIMAL_BODY, headers=headers)

        mutated = {**_MINIMAL_BODY, "task_title": "different"}
        r2 = await client.post("/v1/workspaces", json=mutated, headers=headers)

        assert r2.status_code == 409
        body = r2.json()
        assert body["error_code"] == "IDEMPOTENCY_CONFLICT"
        _assert_no_internal_error_fields(body)


class TestGetWorkspace:
    @pytest.mark.unit
    async def test_stale_running_flag(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        create = await client.post("/v1/workspaces", json=_MINIMAL_BODY)
        ws_id = create.json()["workspace_id"]

        # update DB directly to running and last_activity_at old
        from sqlalchemy.ext.asyncio import AsyncSession

        from awf.db.models import Workspace

        async with AsyncSession(engine) as session:
            from datetime import UTC, datetime, timedelta

            from sqlalchemy import update

            await session.execute(
                update(Workspace)
                .where(Workspace.id == ws_id)
                .values(
                    status="running",
                    subphase="agent",
                    last_activity_at=datetime.now(UTC) - timedelta(minutes=15),
                )
            )
            await session.commit()

        response = await client.get(f"/v1/workspaces/{ws_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "running"
        assert body["subphase"] == "agent"
        assert body["is_stale_running"] is True

    @pytest.mark.unit
    async def test_returns_workspace_shape(self, client: AsyncClient) -> None:
        create = await client.post("/v1/workspaces", json=_MINIMAL_BODY)
        ws_id = create.json()["workspace_id"]

        response = await client.get(f"/v1/workspaces/{ws_id}")
        assert response.status_code == 200

        body = response.json()
        assert body["id"] == ws_id
        assert body["status"] == "requested"
        assert body["version"] == 1
        assert body["task_title"] == _MINIMAL_BODY["task_title"]
        assert body["agent"] == "codex"
        assert body["test_commands"] == _MINIMAL_BODY["test_commands"]

    @pytest.mark.unit
    async def test_get_workspace_retries_once_after_closed_connection(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        create = await client.post("/v1/workspaces", json=_MINIMAL_BODY)
        ws_id = create.json()["workspace_id"]
        original = ValidationRunRepository.list_for_workspace
        failures_remaining = 1
        calls = 0

        async def _flaky_validation_runs(
            self: ValidationRunRepository,
            workspace_id: str,
        ) -> list[object]:
            nonlocal failures_remaining, calls
            calls += 1
            if failures_remaining:
                failures_remaining -= 1
                raise _closed_connection_error()
            return await original(self, workspace_id)

        monkeypatch.setattr(
            ValidationRunRepository,
            "list_for_workspace",
            _flaky_validation_runs,
        )

        response = await client.get(f"/v1/workspaces/{ws_id}")

        assert response.status_code == 200
        assert response.json()["id"] == ws_id
        assert response.json()["validation_provenance"]["reason_code"] == "validation_unavailable"
        assert calls == 2

    @pytest.mark.unit
    async def test_get_workspace_ignores_egress_audit_lookup_failure(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        create = await client.post("/v1/workspaces", json=_MINIMAL_BODY)
        ws_id = create.json()["workspace_id"]
        calls = 0

        async def _fail_audit_lookup(
            self: EgressAuditRepository,
            workspace_id: str,
        ) -> object:
            nonlocal calls
            calls += 1
            raise RuntimeError(f"egress audit unavailable for {workspace_id}")

        monkeypatch.setattr(
            EgressAuditRepository,
            "get_latest_for_workspace",
            _fail_audit_lookup,
        )

        response = await client.get(f"/v1/workspaces/{ws_id}")

        assert response.status_code == 200
        assert response.json()["id"] == ws_id
        assert response.json()["egress_audit"] is None
        assert calls == 1

    @pytest.mark.unit
    async def test_get_workspace_ignores_transient_egress_audit_lookup_failure(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        create = await client.post("/v1/workspaces", json=_MINIMAL_BODY)
        ws_id = create.json()["workspace_id"]
        calls = 0

        async def _fail_audit_lookup(
            self: EgressAuditRepository,
            workspace_id: str,
        ) -> object:
            nonlocal calls
            calls += 1
            raise _closed_connection_error()

        monkeypatch.setattr(
            EgressAuditRepository,
            "get_latest_for_workspace",
            _fail_audit_lookup,
        )

        response = await client.get(f"/v1/workspaces/{ws_id}")

        assert response.status_code == 200
        assert response.json()["id"] == ws_id
        assert response.json()["egress_audit"] is None
        assert calls == 2

    @pytest.mark.unit
    async def test_get_workspace_releases_response_session_before_egress_audit_retry(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        create = await client.post("/v1/workspaces", json=_MINIMAL_BODY)
        ws_id = create.json()["workspace_id"]
        original_retry = workspaces_route.run_db_operation_with_retry
        original_audit_lookup = EgressAuditRepository.get_latest_for_workspace
        audit_calls = 0
        active_retry_operations = 0
        max_active_retry_operations = 0

        async def _flaky_audit_lookup(
            self: EgressAuditRepository,
            workspace_id: str,
        ) -> object:
            nonlocal audit_calls
            audit_calls += 1
            if audit_calls == 1:
                raise _closed_connection_error()
            return await original_audit_lookup(self, workspace_id)

        async def _tracked_retry_operation(*args: Any, **kwargs: Any) -> Any:
            nonlocal active_retry_operations, max_active_retry_operations
            active_retry_operations += 1
            max_active_retry_operations = max(
                max_active_retry_operations,
                active_retry_operations,
            )
            try:
                return await original_retry(*args, **kwargs)
            finally:
                active_retry_operations -= 1

        monkeypatch.setattr(
            workspaces_route,
            "run_db_operation_with_retry",
            _tracked_retry_operation,
        )
        monkeypatch.setattr(
            EgressAuditRepository,
            "get_latest_for_workspace",
            _flaky_audit_lookup,
        )

        response = await client.get(f"/v1/workspaces/{ws_id}")

        assert response.status_code == 200
        assert response.json()["id"] == ws_id
        assert audit_calls == 2
        assert max_active_retry_operations == 1

    @pytest.mark.unit
    async def test_get_workspace_isolates_transient_egress_audit_lookup_from_response_session(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        create = await client.post("/v1/workspaces", json=_MINIMAL_BODY)
        ws_id = create.json()["workspace_id"]
        calls = 0
        audit_sessions: list[object] = []
        cleanup_sessions: list[object] = []
        original_cleanup = db_resilience.invalidate_or_rollback_session

        async def _flaky_audit_lookup(
            self: EgressAuditRepository,
            workspace_id: str,
        ) -> object:
            nonlocal calls
            calls += 1
            audit_sessions.append(self._session)
            if calls == 1:
                raise _closed_connection_error()
            return None

        async def _record_retry_session_cleanup(
            session: object,
            exc: BaseException,
        ) -> None:
            cleanup_sessions.append(session)
            await original_cleanup(session, exc)  # type: ignore[arg-type]

        monkeypatch.setattr(
            EgressAuditRepository,
            "get_latest_for_workspace",
            _flaky_audit_lookup,
        )
        monkeypatch.setattr(
            db_resilience,
            "invalidate_or_rollback_session",
            _record_retry_session_cleanup,
        )

        response = await client.get(f"/v1/workspaces/{ws_id}")

        assert response.status_code == 200
        assert response.json()["id"] == ws_id
        assert calls == 2
        assert len(cleanup_sessions) == 1
        assert cleanup_sessions[0] is audit_sessions[0]

    @pytest.mark.unit
    async def test_get_workspace_retries_transient_egress_audit_lookup_failure(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        create = await client.post("/v1/workspaces", json=_MINIMAL_BODY)
        ws_id = create.json()["workspace_id"]
        original = EgressAuditRepository.get_latest_for_workspace
        calls = 0

        async def _flaky_audit_lookup(
            self: EgressAuditRepository,
            workspace_id: str,
        ) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise _closed_connection_error()
            return await original(self, workspace_id)

        monkeypatch.setattr(
            EgressAuditRepository,
            "get_latest_for_workspace",
            _flaky_audit_lookup,
        )

        response = await client.get(f"/v1/workspaces/{ws_id}")

        assert response.status_code == 200
        assert response.json()["id"] == ws_id
        assert response.json()["egress_audit"] is None
        assert calls == 2

    @pytest.mark.unit
    async def test_get_workspace_exposes_validation_provenance_summary(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        create = await client.post(
            "/v1/workspaces",
            json=_v2_body_with_preflight_override(task_class="refactor_task"),
        )
        assert create.status_code == 202
        ws_id = create.json()["workspace_id"]
        attempt_id, _candidate_id = await _attach_merge_candidate(
            engine,
            ws_id,
            head_sha="target-current",
        )
        await _insert_validation_run(
            engine,
            run_id="vr_workspace_fresh_000001",
            workspace_id=ws_id,
            attempt_id=attempt_id,
            tier=2,
            target_head_sha="target-current",
        )

        response = await client.get(f"/v1/workspaces/{ws_id}")

        assert response.status_code == 200
        provenance = response.json()["validation_provenance"]
        assert provenance["required_tier"] == 2
        assert provenance["latest_satisfied_tier"] == 2
        assert provenance["freshness_status"] == "fresh"
        assert provenance["reason_code"] == "validation_fresh"
        assert provenance["current_target_head_sha"] == "target-current"
        latest = provenance["latest_validation"]
        assert latest["validation_run_id"] == "vr_workspace_fresh_000001"
        assert latest["attempt_id"] == attempt_id
        assert latest["tier"] == 2
        assert latest["status"] == "succeeded"
        assert latest["reason_code"] == "VALIDATION_OK"
        assert latest["command_set_hash"] == "a" * 64
        assert latest["base_commit"] == "legacy-base"
        assert latest["base_sha"] == "base-identity"
        assert latest["workspace_head_sha"] == "workspace-head"
        assert latest["target_branch"] == "codex/validation-observability"
        assert latest["target_head_sha"] == "target-current"
        assert latest["current_target_head_sha"] == "target-current"
        assert latest["profile_name"] == "python"
        assert latest["profile_version"] == 3
        assert latest["profile_source"] == "repo:.awf/workspace.yml"
        assert latest["resolved_profile_digest"] == "1" * 64
        assert latest["environment_identity_digest"] == "2" * 64
        assert latest["environment_identity_inputs"] == {"schema_version": 1}
        assert latest["identity_source"] == "persisted"
        assert latest["fresh_for_target"] is True
        assert latest["freshness_status"] == "fresh"
        assert latest["freshness_reason_code"] == "validation_fresh"

    @pytest.mark.unit
    async def test_get_workspace_marks_validation_stale_when_target_identity_differs(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        create = await client.post(
            "/v1/workspaces",
            json=_v2_body_with_preflight_override(task_class="refactor_task"),
        )
        assert create.status_code == 202
        ws_id = create.json()["workspace_id"]
        attempt_id, _candidate_id = await _attach_merge_candidate(
            engine,
            ws_id,
            head_sha="target-new",
        )
        await _insert_validation_run(
            engine,
            run_id="vr_workspace_stale_000001",
            workspace_id=ws_id,
            attempt_id=attempt_id,
            tier=2,
            target_head_sha="target-old",
        )

        response = await client.get(f"/v1/workspaces/{ws_id}")

        assert response.status_code == 200
        provenance = response.json()["validation_provenance"]
        assert provenance["required_tier"] == 2
        assert provenance["latest_satisfied_tier"] == 2
        assert provenance["freshness_status"] == "stale"
        assert provenance["reason_code"] == "validation_target_stale"
        assert provenance["current_target_head_sha"] == "target-new"
        latest = provenance["latest_validation"]
        assert latest["target_head_sha"] == "target-old"
        assert latest["current_target_head_sha"] == "target-new"
        assert latest["fresh_for_target"] is False
        assert latest["freshness_status"] == "stale"
        assert latest["freshness_reason_code"] == "validation_target_stale"

    @pytest.mark.unit
    async def test_get_workspace_reports_validation_unavailable_for_legacy_workspace(
        self,
        client: AsyncClient,
    ) -> None:
        create = await client.post(
            "/v1/workspaces",
            json=_v2_body_with_preflight_override(task_class="dependency_task"),
        )
        assert create.status_code == 202
        ws_id = create.json()["workspace_id"]

        response = await client.get(f"/v1/workspaces/{ws_id}")

        assert response.status_code == 200
        provenance = response.json()["validation_provenance"]
        assert provenance == {
            "required_tier": 2,
            "latest_satisfied_tier": None,
            "freshness_status": "unavailable",
            "reason_code": "validation_unavailable",
            "current_target_head_sha": None,
            "latest_validation": None,
        }

    @pytest.mark.unit
    async def test_pricing_metadata_null_when_no_pricing_configured(
        self,
        client: AsyncClient,
    ) -> None:
        create = await client.post("/v1/workspaces", json=_MINIMAL_BODY)
        ws_id = create.json()["workspace_id"]

        response = await client.get(f"/v1/workspaces/{ws_id}")
        assert response.status_code == 200
        body = response.json()
        assert body.get("pricing") is None

    @pytest.mark.unit
    async def test_pricing_included_when_profile_has_pricing_stanza(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        pricing_ts = datetime.now(UTC).isoformat()
        profile = {
            "name": "pricing-test",
            "runtime": {},
            "pricing": {
                "pricing": {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-20250514",
                    "currency": "USD",
                    "unit": "per_1M_tokens",
                    "timestamp": pricing_ts,
                    "version": 1,
                }
            },
        }
        payload = {
            "repo": {
                "url": "git@github.com:example/pricing.git",
                "base_branch": "main",
            },
            "task": {
                "title": "Pricing test",
                "prompt": "Test pricing metadata.",
                "kind": "feature_branch_pr",
                "agent": "codex",
                "external_id": "PRICE-1",
            },
            "workspace": {"profile_ref": "auto", "profile": profile},
            "validation": {"commands": ["pytest -q"], "requested_tier": 1},
            "resources": {},
            "preflight": {
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "unit test bypasses provider auth",
            },
        }
        create = await client.post("/v1/workspaces", json=payload)
        assert create.status_code == 202
        ws_id = create.json()["workspace_id"]

        response = await client.get(f"/v1/workspaces/{ws_id}")
        assert response.status_code == 200
        body = response.json()
        pricing = body.get("pricing")
        assert pricing is not None
        assert pricing["provider"] == "anthropic"
        assert pricing["model"] == "claude-sonnet-4-20250514"
        assert pricing["currency"] == "USD"
        assert pricing["unit"] == "per_1M_tokens"
        assert pricing["version"] == 1
        assert pricing["is_current"] is True

    @pytest.mark.unit
    async def test_returns_404_for_unknown_id(self, client: AsyncClient) -> None:
        response = await client.get("/v1/workspaces/ws_doesnotexist000000000000")
        assert response.status_code == 404
        assert response.json()["detail"]["error_code"] == "NOT_FOUND"


class TestWorkspaceDirectRoutes:
    @pytest.mark.unit
    async def test_overview_reports_next_cursor_and_applies_cursor(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        oldest_id = await _create_workspace(client, task_title="oldest overview")
        middle_id = await _create_workspace(client, task_title="middle overview")
        newest_id = await _create_workspace(client, task_title="newest overview")
        base = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
        await _set_workspace_created_at(engine, oldest_id, base)
        await _set_workspace_created_at(engine, middle_id, base + timedelta(seconds=1))
        await _set_workspace_created_at(engine, newest_id, base + timedelta(seconds=2))

        first = await client.get("/v1/workspaces/overview", params={"limit": 1})
        assert first.status_code == 200
        first_body = first.json()
        assert [item["workspace_id"] for item in first_body["items"]] == [newest_id]
        assert first_body["has_more"] is True
        assert first_body["next_cursor"] is not None
        assert first_body["cursor"] is None

        second = await client.get(
            "/v1/workspaces/overview",
            params={"limit": 1, "cursor": first_body["next_cursor"]},
        )
        assert second.status_code == 200
        second_body = second.json()
        assert [item["workspace_id"] for item in second_body["items"]] == [middle_id]
        assert second_body["has_more"] is True
        assert second_body["next_cursor"] is not None
        assert second_body["cursor"] == first_body["next_cursor"]

    @pytest.mark.unit
    def test_overview_cursor_fits_query_param_limit_for_max_workspace_id(self) -> None:
        workspace_id = "ws_" + ("a" * 33)
        created_at = datetime(2026, 4, 28, 12, 0, 0, 123456, tzinfo=UTC)

        cursor = workspaces_route._encode_overview_cursor(
            SimpleNamespace(id=workspace_id, created_at=created_at)
        )

        assert len(cursor) <= 128
        assert workspaces_route._decode_overview_cursor(
            cursor
        ) == workspaces_route._WorkspaceOverviewCursor(
            created_at=created_at,
            workspace_id=workspace_id,
        )

    @pytest.mark.unit
    def test_overview_cursor_decodes_legacy_verbose_payload(self) -> None:
        workspace_id = "ws_" + ("a" * 24)
        created_at = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
        payload = {
            "created_at": created_at.isoformat(),
            "workspace_id": workspace_id,
        }
        cursor = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")

        assert workspaces_route._decode_overview_cursor(
            cursor
        ) == workspaces_route._WorkspaceOverviewCursor(
            created_at=created_at,
            workspace_id=workspace_id,
        )

    @pytest.mark.unit
    async def test_overview_rejects_invalid_cursor(self, client: AsyncClient) -> None:
        response = await client.get(
            "/v1/workspaces/overview",
            params={"cursor": "not-a-cursor"},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == {
            "error_code": "INVALID_CURSOR",
            "message": "Invalid workspace overview cursor.",
        }

    @pytest.mark.unit
    async def test_overview_route_maps_workspace_without_events_or_operations(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_workspace(client, task_title="overview direct")

        factory = make_session_factory(engine)
        async with factory() as session:
            response = await workspaces_route.list_workspace_overview(session=session)

        item = next(item for item in response.items if item.workspace_id == workspace_id)
        assert item.title == "overview direct"
        assert item.active_operation is None
        assert item.last_event is not None
        assert response.next_cursor is None
        assert response.has_more is False
        assert response.limit == 50
        assert response.cursor is None

    @pytest.mark.unit
    async def test_overview_route_reuses_ordered_events_for_last_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class SinglePassEvents:
            def __init__(self, events: list[SimpleNamespace]) -> None:
                self._events = events
                self.iterations = 0

            def __iter__(self) -> Iterator[SimpleNamespace]:
                self.iterations += 1
                if self.iterations > 1:
                    raise AssertionError("workspace events were iterated more than once")
                return iter(self._events)

        base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
        workspace_id = "ws_singlepass"
        created_event = SimpleNamespace(
            id="evt_created",
            workspace_id=workspace_id,
            event_type="workspace.created",
            old_state=None,
            new_state=WorkspaceStatus.requested.value,
            reason_code="CREATED",
            payload=None,
            occurred_at=base,
        )
        latest_event = SimpleNamespace(
            id="evt_latest",
            workspace_id=workspace_id,
            event_type="workspace.test_marker",
            old_state=None,
            new_state=None,
            reason_code="TEST",
            payload={"source": "unit"},
            occurred_at=base + timedelta(seconds=5),
        )
        events = SinglePassEvents([latest_event, created_event])
        workspace = SimpleNamespace(
            id=workspace_id,
            task_external_id=None,
            task_title="single pass overview",
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            branch_name="awf/ws-singlepass",
            task_class=None,
            owned_paths=[],
            agent="codex",
            task_policy={},
            events=events,
            operations=[],
            status=WorkspaceStatus.requested.value,
            pr_url=None,
            failure_reason=None,
            failure_message=None,
            created_at=base,
            updated_at=base,
        )

        class FakeWorkspaceRepository:
            def __init__(self, session: object) -> None:
                del session

            async def list(self, **kwargs: object) -> list[SimpleNamespace]:
                del kwargs
                return [workspace]

        monkeypatch.setattr(
            workspace_observability,
            "WorkspaceRepository",
            FakeWorkspaceRepository,
        )

        response = await workspaces_route.list_workspace_overview(
            session=SimpleNamespace(),
        )

        item = response.items[0]
        assert item.last_event is not None
        assert item.last_event.event_type == "workspace.test_marker"
        assert events.iterations == 1

    @pytest.mark.unit
    async def test_existing_events_stale_reasons_get_retry_and_list_routes_directly(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_workspace(client, task_title="direct route workspace")

        factory = make_session_factory(engine)
        async with factory() as session:
            events = await workspaces_route.list_workspace_events(workspace_id, session=session)
            stale = await workspaces_route.list_workspace_stale_reasons(
                workspace_id,
                include_resolved=True,
                session=session,
            )
            retry_error = await workspaces_route.retry_workspace("ws_missing", session=session)
        listed = await workspaces_route.list_workspaces(session_factory=factory)
        detail = await workspaces_route.get_workspace(workspace_id, session_factory=factory)

        event_types = {event.event_type for event in events.items}
        assert "workspace.created" in event_types
        assert "workspace.provider_readiness_preflight" in event_types
        assert stale.items == []
        assert events.limit == 50
        assert events.cursor is None
        assert stale.next_cursor is None
        assert stale.has_more is False
        assert stale.limit == workspace_observability.DEFAULT_STALE_REASON_LIMIT
        assert stale.cursor is None
        assert [workspace.id for workspace in listed] == [workspace_id]
        assert detail.id == workspace_id
        assert isinstance(retry_error, JSONResponse)
        assert retry_error.status_code == 404
        assert json.loads(retry_error.body)["error_code"] == "WORKSPACE_NOT_FOUND"

    @pytest.mark.unit
    async def test_events_and_stale_reason_routes_reject_missing_workspace_directly(
        self,
        engine: AsyncEngine,
    ) -> None:
        factory = make_session_factory(engine)
        async with factory() as session:
            with pytest.raises(HTTPException) as events_error:
                await workspaces_route.list_workspace_events("ws_missing", session=session)
            with pytest.raises(HTTPException) as stale_error:
                await workspaces_route.list_workspace_stale_reasons(
                    "ws_missing",
                    session=session,
                )

        assert events_error.value.status_code == 404
        assert events_error.value.detail["error_code"] == "NOT_FOUND"
        assert stale_error.value.status_code == 404
        assert stale_error.value.detail["error_code"] == "NOT_FOUND"


class TestListWorkspaces:
    @pytest.mark.unit
    async def test_returns_empty_list_when_none(self, client: AsyncClient) -> None:
        response = await client.get("/v1/workspaces")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.unit
    async def test_read_routes_do_not_open_unused_request_session(
        self,
        client: AsyncClient,
    ) -> None:
        workspace_id = await _create_workspace(client, task_title="single read dependency")
        app = client._transport.app  # noqa: SLF001
        session_resolutions = 0

        async def _fail_if_resolved() -> AsyncIterator[object]:
            nonlocal session_resolutions
            session_resolutions += 1
            raise AssertionError("read routes should not resolve get_db_session")
            yield object()

        app.dependency_overrides[get_db_session] = _fail_if_resolved
        try:
            listed = await client.get("/v1/workspaces")
            detail = await client.get(f"/v1/workspaces/{workspace_id}")
        finally:
            app.dependency_overrides.pop(get_db_session, None)

        assert listed.status_code == 200
        assert detail.status_code == 200
        assert [item["id"] for item in listed.json()] == [workspace_id]
        assert detail.json()["id"] == workspace_id
        assert session_resolutions == 0

    @pytest.mark.unit
    async def test_list_workspaces_retries_once_after_closed_connection(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_workspace(client, task_title="closed connection list")
        original = WorkspaceRepository.list
        failures_remaining = 1
        calls = 0

        async def _flaky_list(
            self: WorkspaceRepository,
            **kwargs: object,
        ) -> list[Any]:
            nonlocal failures_remaining, calls
            calls += 1
            if failures_remaining:
                failures_remaining -= 1
                raise _closed_connection_error()
            return await original(self, **kwargs)

        monkeypatch.setattr(WorkspaceRepository, "list", _flaky_list)

        response = await client.get("/v1/workspaces")

        assert response.status_code == 200
        assert [item["id"] for item in response.json()] == [workspace_id]
        assert calls == 2

    @pytest.mark.unit
    async def test_returns_created_workspaces_newest_first(self, client: AsyncClient) -> None:
        ids: list[str] = []
        for title in ["first", "second", "third"]:
            body = {**_MINIMAL_BODY, "task_title": title}
            create = await client.post("/v1/workspaces", json=body)
            ids.append(create.json()["workspace_id"])

        response = await client.get("/v1/workspaces")
        assert response.status_code == 200
        results = response.json()
        assert len(results) == 3
        # Newest first.
        assert [r["id"] for r in results] == list(reversed(ids))

    @pytest.mark.unit
    async def test_filters_by_status(self, client: AsyncClient, engine: AsyncEngine) -> None:
        ready_id = await _create_workspace(client, task_title="ready")
        await _create_workspace(client, task_title="still requested")
        await _transition_workspace(
            engine,
            ready_id,
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
        )

        response = await client.get("/v1/workspaces", params={"status": "ready"})

        assert response.status_code == 200
        results = response.json()
        assert [r["id"] for r in results] == [ready_id]
        assert {r["status"] for r in results} == {"ready"}

    @pytest.mark.unit
    async def test_filters_by_agent(self, client: AsyncClient) -> None:
        await _create_workspace(client, task_title="codex", agent="codex")
        claude_id = await _create_workspace(
            client,
            task_title="claude",
            agent="claude_code",
        )

        response = await client.get("/v1/workspaces", params={"agent": "claude_code"})

        assert response.status_code == 200
        results = response.json()
        assert [r["id"] for r in results] == [claude_id]
        assert {r["agent"] for r in results} == {"claude_code"}

    @pytest.mark.unit
    async def test_filters_by_exact_repo_url(self, client: AsyncClient) -> None:
        repo_url = "git@github.com:example/app.git"
        matching_id = await _create_workspace(
            client,
            task_title="matching repo",
            repo_url=repo_url,
        )
        await _create_workspace(
            client,
            task_title="similar repo",
            repo_url="git@github.com:example/app-extra.git",
        )

        response = await client.get("/v1/workspaces", params={"repo_url": repo_url})

        assert response.status_code == 200
        results = response.json()
        assert [r["id"] for r in results] == [matching_id]
        assert {r["repo_url"] for r in results} == {repo_url}

    @pytest.mark.unit
    async def test_combines_filters(self, client: AsyncClient, engine: AsyncEngine) -> None:
        repo_url = "git@github.com:example/combined.git"
        matching_id = await _create_workspace(
            client,
            task_title="matching",
            repo_url=repo_url,
            agent="gemini",
        )
        wrong_status_id = await _create_workspace(
            client,
            task_title="wrong status",
            repo_url=repo_url,
            agent="gemini",
        )
        wrong_agent_id = await _create_workspace(
            client,
            task_title="wrong agent",
            repo_url=repo_url,
            agent="codex",
        )
        wrong_repo_id = await _create_workspace(
            client,
            task_title="wrong repo",
            repo_url="git@github.com:example/other.git",
            agent="gemini",
        )
        for workspace_id in [matching_id, wrong_agent_id, wrong_repo_id]:
            await _transition_workspace(
                engine,
                workspace_id,
                WorkspaceStatus.provisioning,
                WorkspaceStatus.ready,
            )

        response = await client.get(
            "/v1/workspaces",
            params={"status": "ready", "agent": "gemini", "repo_url": repo_url},
        )

        assert response.status_code == 200
        assert [r["id"] for r in response.json()] == [matching_id]
        assert wrong_status_id not in [r["id"] for r in response.json()]

    @pytest.mark.unit
    async def test_limit_defaults_to_50(self, client: AsyncClient) -> None:
        for idx in range(51):
            await _create_workspace(client, task_title=f"workspace {idx}")

        response = await client.get("/v1/workspaces")

        assert response.status_code == 200
        assert len(response.json()) == 50

    @pytest.mark.unit
    async def test_limit_bounds_newest_first(self, client: AsyncClient) -> None:
        first_id = await _create_workspace(client, task_title="first")
        second_id = await _create_workspace(client, task_title="second")

        response = await client.get("/v1/workspaces", params={"limit": 1})

        assert response.status_code == 200
        assert [r["id"] for r in response.json()] == [second_id]
        assert first_id not in [r["id"] for r in response.json()]

    @pytest.mark.unit
    @pytest.mark.parametrize("limit", [0, 501])
    async def test_rejects_limit_outside_bounds(self, client: AsyncClient, limit: int) -> None:
        response = await client.get("/v1/workspaces", params={"limit": limit})

        assert response.status_code == 422

    @pytest.mark.unit
    async def test_returns_empty_list_when_filters_match_nothing(self, client: AsyncClient) -> None:
        await _create_workspace(client, task_title="not completed")

        response = await client.get("/v1/workspaces", params={"status": "completed"})

        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.unit
    async def test_list_workspaces_multi_status(self, client: AsyncClient) -> None:
        ws_requested1 = await _create_workspace(client, task_title="Requested ws 1")
        ws_requested2 = await _create_workspace(client, task_title="Requested ws 2")

        # By default, a newly created workspace is 'requested'. Let's search for 'requested'.
        response = await client.get("/v1/workspaces?status=requested&status=running")

        assert response.status_code == 200
        result_ids = [item["id"] for item in response.json()]
        assert ws_requested1 in result_ids
        assert ws_requested2 in result_ids

        # also test one that matches nothing
        response2 = await client.get("/v1/workspaces?status=completed&status=failed")
        assert response2.status_code == 200
        assert response2.json() == []

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("param", "value"),
        [("status", "not-a-status"), ("agent", "not-an-agent")],
    )
    async def test_rejects_unknown_filter_enums(
        self, client: AsyncClient, param: str, value: str
    ) -> None:
        response = await client.get("/v1/workspaces", params={param: value})

        assert response.status_code == 422
