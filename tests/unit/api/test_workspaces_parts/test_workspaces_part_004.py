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
