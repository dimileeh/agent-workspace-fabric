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


class TestCreateWorkspacePart001:
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
        monkeypatch.setattr(workspaces_route, "create_workspace_row_checked", fail_create)

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
        monkeypatch.setattr(workspaces_route, "create_workspace_row_checked", fail_create)

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
        monkeypatch.setattr(workspaces_route, "create_workspace_row_checked", fail_create)

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
