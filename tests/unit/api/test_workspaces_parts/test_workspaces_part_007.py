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
import awf.service.workspace_observability as workspace_observability
from awf.api.app import configure_database, create_app
from awf.api.deps import get_db_session
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
            pr_url="https://github.com/example/app/pull/7",
            pr_number=7,
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
        assert item.pr_number == 7
        assert item.pr_url == "https://github.com/example/app/pull/7"

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
    async def test_filters_by_recovering_status(
        self, client: AsyncClient, engine: AsyncEngine
    ) -> None:
        # ``recovering`` is the auto-healing provider-retry pause (#612). It is a
        # plain string status, so the ``status=recovering`` list filter must be
        # accepted and the status round-trips in the response body.
        recovering_id = await _create_workspace(client, task_title="recovering")
        await _create_workspace(client, task_title="still requested")
        await _set_workspace_status(engine, recovering_id, WorkspaceStatus.recovering)

        response = await client.get("/v1/workspaces", params={"status": "recovering"})

        assert response.status_code == 200
        results = response.json()
        assert [r["id"] for r in results] == [recovering_id]
        assert {r["status"] for r in results} == {"recovering"}

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
