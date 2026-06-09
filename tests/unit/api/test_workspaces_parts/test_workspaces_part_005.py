"""Workspace API contract tests.

Each test runs against an isolated PostgreSQL schema via the ``client`` fixture.
These are *unit*-flavoured tests because they don't spin up Docker or Postgres;
true integration + E2E tests live under tests/integration/ and tests/e2e/.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
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
    SecretLeaseIssue,
    SecretLeaseRepository,
    TaskAttemptRepository,
    TaskRepository,
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
        _assert_effective_identity(detail.json(), model="claude-opus-4-8")
        _assert_effective_identity(overview.json()["items"][0], model="claude-opus-4-8")

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
