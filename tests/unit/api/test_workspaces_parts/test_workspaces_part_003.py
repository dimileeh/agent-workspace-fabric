"""Workspace API contract tests.

Each test runs against an isolated PostgreSQL schema via the ``client`` fixture.
These are *unit*-flavoured tests because they don't spin up Docker or Postgres;
true integration + E2E tests live under tests/integration/ and tests/e2e/.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request

import awf.api.request_admission as request_admission
import awf.api.routes.workspaces as workspaces_route
from awf.api.app import configure_database, create_app
from awf.api.schemas import (
    WorkspaceAcceptedResponse,
    WorkspaceCreateRequest,
)
from awf.common.config import Settings, get_settings
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
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
    "CURSOR_API_KEY",
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
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_output_tokens": None,
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


class TestCreateWorkspacePart002:
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
        settings = Settings(_env_file=None, min_free_disk_bytes=0)
        factory = make_session_factory(engine)
        async with factory() as session:
            first = await workspaces_route.create_workspace(
                payload,
                idempotency_key="direct-v1-replay",
                settings=settings,
                session=session,
            )
            replay = await workspaces_route.create_workspace(
                payload,
                idempotency_key="direct-v1-replay",
                settings=settings,
                session=session,
            )
            conflict = await workspaces_route.create_workspace(
                WorkspaceCreateRequest.model_validate(
                    {**_MINIMAL_BODY, "task_title": "Changed direct replay"}
                ),
                idempotency_key="direct-v1-replay",
                settings=settings,
                session=session,
            )

        assert isinstance(first, WorkspaceAcceptedResponse)
        assert isinstance(replay, WorkspaceAcceptedResponse)
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


class TestCreateWorkspacePart001Overflow:
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
