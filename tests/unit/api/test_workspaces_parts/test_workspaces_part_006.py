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
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request

import awf.api.request_admission as request_admission
import awf.api.routes.workspaces as workspaces_route
import awf.db.resilience as db_resilience
from awf.api.app import configure_database, create_app
from awf.common.config import Settings, get_settings
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    EgressAuditRepository,
    MergeCandidateRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
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
