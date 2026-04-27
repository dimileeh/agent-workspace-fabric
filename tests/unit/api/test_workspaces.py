"""Workspace API contract tests.

Each test runs against a fresh in-memory SQLite via the ``client`` fixture.
These are *unit*-flavoured tests because they don't spin up Docker or Postgres;
true integration + E2E tests live under tests/integration/ and tests/e2e/.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.api.routes.workspaces as workspaces_route
from awf.api.app import configure_database, create_app
from awf.api.schemas import WorkspaceCreateRequest, WorkspaceCreateV2Request
from awf.common.config import Settings, get_settings
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.disk import DiskCheck

_MINIMAL_BODY = {
    "repo_url": "git@github.com:dimileeh/aira-agent.git",
    "branch_base": "development",
    "task_title": "Add module docstring",
    "task_prompt": "Add a one-line docstring to src/aira_agent/api/main.py.",
    "agent": "codex",
    "test_commands": ["pytest -q"],
}


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


def _v2_body(
    *,
    repo_url: str = "git@github.com:example/app.git",
    base_branch: str = "development",
    title: str = "Owned path policy test",
    task_class: str | None = None,
    owned_paths: list[str] | None = None,
    model: str | None = None,
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
    return {
        **_V2_MINIMAL_BODY,
        "repo": {
            "url": repo_url,
            "base_branch": base_branch,
        },
        "task": task,
    }


async def _create_workspace(client: AsyncClient, **overrides: object) -> str:
    body = {**_MINIMAL_BODY, **overrides}
    response = await client.post("/v1/workspaces", json=body)
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


async def _create_v2_workspace(
    client: AsyncClient,
    *,
    repo_url: str = "git@github.com:example/app.git",
    base_branch: str = "development",
    title: str = "Owned path policy test",
    task_class: str | None = None,
    owned_paths: list[str] | None = None,
) -> str:
    response = await client.post(
        "/v2/workspaces",
        json=_v2_body(
            repo_url=repo_url,
            base_branch=base_branch,
            title=title,
            task_class=task_class,
            owned_paths=owned_paths,
        ),
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
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                workspace_admission_disk_check=lambda settings: _disk_check(
                    free_bytes=settings.min_free_disk_bytes + 1,
                    threshold_bytes=settings.min_free_disk_bytes,
                    ok=True,
                )
            )
        )
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


@pytest.fixture
async def disk_app_and_client(engine: AsyncEngine) -> AsyncIterator[tuple[Any, AsyncClient]]:
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
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


class TestCreateWorkspace:
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
                session=session,
            )
            replay = await workspaces_route.create_workspace(
                payload,
                idempotency_key="direct-v1-replay",
                session=session,
            )
            conflict = await workspaces_route.create_workspace(
                WorkspaceCreateRequest.model_validate(
                    {**_MINIMAL_BODY, "task_title": "Changed direct replay"}
                ),
                idempotency_key="direct-v1-replay",
                session=session,
            )

        assert first.workspace_id == replay.workspace_id
        assert isinstance(conflict, JSONResponse)
        assert conflict.status_code == 409
        assert json.loads(conflict.body)["error_code"] == "IDEMPOTENCY_CONFLICT"


class TestCreateWorkspaceV2DiskPressure:
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

        response = await client.post("/v2/workspaces", json=_V2_MINIMAL_BODY)
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
        first = await client.post("/v2/workspaces", json=_V2_MINIMAL_BODY, headers=headers)

        app.state.workspace_admission_disk_check = lambda _settings: _disk_check(
            free_bytes=300,
            threshold_bytes=400,
            ok=False,
        )
        replay = await client.post("/v2/workspaces", json=_V2_MINIMAL_BODY, headers=headers)
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

        response = await client.post("/v2/workspaces", json=_V2_MINIMAL_BODY)
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

        response = await client.post("/v2/workspaces", json=_V2_MINIMAL_BODY)

        assert response.status_code == 202
        assert seen["settings"] is settings


class TestCreateWorkspaceV2MonitorPolicy:
    @pytest.mark.unit
    async def test_old_v2_payload_defaults_to_auto_merge_and_profile_grace(
        self,
        client: AsyncClient,
    ) -> None:
        create = await client.post("/v2/workspaces", json=_V2_MINIMAL_BODY)
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

        create = await client.post("/v2/workspaces", json=payload)
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

        first = await client.post("/v2/workspaces", json=first_payload, headers=headers)
        replay = await client.post("/v2/workspaces", json=replay_payload, headers=headers)

        assert first.status_code == 202
        assert replay.status_code == 409
        assert replay.json()["error_code"] == "IDEMPOTENCY_CONFLICT"

    @pytest.mark.unit
    async def test_direct_v2_replay_returns_existing_row_and_conflict_response(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        headers = {"Idempotency-Key": "direct-v2-replay"}
        first = await client.post("/v2/workspaces", json=_V2_MINIMAL_BODY, headers=headers)
        assert first.status_code == 202

        factory = make_session_factory(engine)
        async with factory() as session:
            replay = await workspaces_route.create_workspace_v2(
                WorkspaceCreateV2Request.model_validate(_V2_MINIMAL_BODY),
                _request_with_disk_check(),
                idempotency_key="direct-v2-replay",
                settings=Settings(_env_file=None),
                session=session,
            )
            conflict = await workspaces_route.create_workspace_v2(
                WorkspaceCreateV2Request.model_validate(
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

        assert replay.workspace_id == first.json()["workspace_id"]
        assert replay.warnings == []
        assert isinstance(conflict, JSONResponse)
        assert conflict.status_code == 409
        assert json.loads(conflict.body)["error_code"] == "IDEMPOTENCY_CONFLICT"

    @pytest.mark.unit
    async def test_v2_invalid_profile_ref_returns_structured_422(
        self,
        client: AsyncClient,
    ) -> None:
        payload = {
            **_V2_MINIMAL_BODY,
            "workspace": {"profile_ref": "missing-profile", "profile": None},
        }

        response = await client.post("/v2/workspaces", json=payload)

        assert response.status_code == 422
        assert response.json()["error_code"] == "INVALID_PROFILE"

    @pytest.mark.unit
    async def test_direct_v2_create_success_returns_accepted_response(
        self,
        engine: AsyncEngine,
    ) -> None:
        factory = make_session_factory(engine)
        async with factory() as session:
            accepted = await workspaces_route.create_workspace_v2(
                WorkspaceCreateV2Request.model_validate(_V2_MINIMAL_BODY),
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
    ) -> None:
        factory = make_session_factory(engine)
        async with factory() as session:
            accepted = await workspaces_route.create_workspace_v2(
                WorkspaceCreateV2Request.model_validate(_V2_MINIMAL_BODY),
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


class TestCreateWorkspaceV2PolicyMetadata:
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

        create = await client.post("/v2/workspaces", json=payload)
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
    async def test_persists_agent_model_override_in_task_policy(
        self,
        client: AsyncClient,
    ) -> None:
        payload = _v2_body(model="ollama/glm-5.1:cloud")

        create = await client.post("/v2/workspaces", json=payload)
        assert create.status_code == 202

        ws_id = create.json()["workspace_id"]
        response = await client.get(f"/v1/workspaces/{ws_id}")
        overview = await client.get("/v1/workspaces/overview")
        tasks = await client.get("/v1/tasks")

        assert response.status_code == 200
        assert response.json()["task_policy"]["agent_model"] == "ollama/glm-5.1:cloud"
        _assert_effective_identity(
            response.json(),
            model="ollama/glm-5.1:cloud",
            model_source="task_policy",
        )
        _assert_usage_unavailable(response.json())
        assert overview.status_code == 200
        _assert_effective_identity(
            overview.json()["items"][0],
            model="ollama/glm-5.1:cloud",
            model_source="task_policy",
        )
        _assert_usage_unavailable(overview.json()["items"][0])
        assert tasks.status_code == 200
        _assert_effective_identity(
            tasks.json()["items"][0],
            model="ollama/glm-5.1:cloud",
            model_source="task_policy",
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

        create = await client.post("/v2/workspaces", json=payload)
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
            _assert_effective_identity(row, model="gemini-3-pro-preview")
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

        first = await client.post("/v2/workspaces", json=_V2_MINIMAL_BODY, headers=headers)
        replay = await client.post("/v2/workspaces", json=_V2_MINIMAL_BODY, headers=headers)

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
            "resources": {
                "steady_state_cpu_cores": 4.0,
                "steady_state_memory_gb": 12.0,
                "peak_cpu_cores": 8.0,
                "peak_memory_gb": 24.0,
                "disk_mb": 4096,
            },
        }

        create = await client.post("/v2/workspaces", json=payload)
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
        assert decision["resource_summary"]["peak_cpu"] == 8.0
        assert decision["overlap_risk_summary"]["overlap_count"] == 0
        assert reservation["id"].startswith("rr_")
        assert reservation["node_id"] == "local"
        assert reservation["steady_cpu"] == 4.0
        assert reservation["steady_memory_gb"] == 12.0
        assert reservation["peak_cpu"] == 8.0
        assert reservation["peak_memory_gb"] == 24.0
        assert reservation["disk_mb"] == 4096
        assert reservation["phase"] == "workspace_lifecycle"
        assert reservation["released_at"] is None

    @pytest.mark.unit
    async def test_rejects_zero_disk_reservation(self, client: AsyncClient) -> None:
        payload = {
            **_V2_MINIMAL_BODY,
            "resources": {"disk_mb": 0},
        }

        response = await client.post("/v2/workspaces", json=payload)

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
            for event, occurred_at in zip(
                sorted(workspace.events, key=lambda item: item.occurred_at),
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
            for event, occurred_at in zip(
                sorted(workspace.events, key=lambda item: item.occurred_at),
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

        response = await client.post("/v2/workspaces", json=payload)

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

        response = await client.post("/v2/workspaces", json=payload)

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

        first = await client.post("/v2/workspaces", json=first_payload, headers=headers)
        replay = await client.post("/v2/workspaces", json=replay_payload, headers=headers)

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

        create_response = await client.post("/v2/workspaces", json=payload)
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
        policy_payload = WorkspaceCreateV2Request.model_validate(
            {
                **_V2_MINIMAL_BODY,
                "task": {
                    **_V2_MINIMAL_BODY["task"],
                    "out_of_scope_changes": {"mode": "block"},
                },
            }
        )
        payload = WorkspaceCreateV2Request.model_validate(_V2_MINIMAL_BODY)

        assert workspaces_route._resolved_profile_requested_tier(workspace) == 2  # type: ignore[arg-type]
        assert workspaces_route._resolved_profile_requested_tier(malformed_workspace) is None  # type: ignore[arg-type]
        assert workspaces_route._resolved_profile_requested_tier(missing_profile_workspace) is None  # type: ignore[arg-type]
        assert workspaces_route._resolved_profile_requested_tier(malformed_validation_workspace) is None  # type: ignore[arg-type]
        assert workspaces_route._stored_task_agent_model(workspace) == "gpt-test"  # type: ignore[arg-type]
        assert workspaces_route._stored_task_agent_model(malformed_workspace) is None  # type: ignore[arg-type]
        assert workspaces_route._stored_task_out_of_scope_policy(workspace) == {"mode": "warn"}  # type: ignore[arg-type]
        assert workspaces_route._stored_task_out_of_scope_policy(malformed_workspace) is None  # type: ignore[arg-type]
        assert workspaces_route._requested_task_out_of_scope_policy(payload) is None
        assert workspaces_route._requested_task_out_of_scope_policy(policy_payload) == {
            "mode": "block",
            "allowlist_patterns": [],
        }


class TestCreateWorkspaceV2OwnedPathPolicy:
    @pytest.mark.unit
    async def test_no_requested_owned_paths_do_not_block(
        self,
        client: AsyncClient,
    ) -> None:
        await _create_v2_workspace(
            client,
            title="existing",
            owned_paths=["src/awf/api/**"],
        )

        response = await client.post(
            "/v2/workspaces",
            json=_v2_body(title="new without owned paths", owned_paths=[]),
        )

        assert response.status_code == 202

    @pytest.mark.unit
    async def test_non_overlapping_owned_paths_are_allowed(
        self,
        client: AsyncClient,
    ) -> None:
        await _create_v2_workspace(
            client,
            title="existing",
            owned_paths=["src/awf/api/**"],
        )

        response = await client.post(
            "/v2/workspaces",
            json=_v2_body(title="docs", owned_paths=["docs/**"]),
        )

        assert response.status_code == 202

    @pytest.mark.unit
    async def test_same_paths_on_different_repo_or_base_branch_are_allowed(
        self,
        client: AsyncClient,
    ) -> None:
        await _create_v2_workspace(
            client,
            repo_url="git@github.com:example/other.git",
            base_branch="development",
            title="other repo",
            owned_paths=["src/awf/api/**"],
        )
        await _create_v2_workspace(
            client,
            repo_url="git@github.com:example/app.git",
            base_branch="main",
            title="other base",
            owned_paths=["src/awf/api/**"],
        )

        response = await client.post(
            "/v2/workspaces",
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
        existing_id = await _create_v2_workspace(
            client,
            title=f"existing {existing_status.value}",
            owned_paths=["src/awf/api/**"],
        )
        await _set_workspace_status(engine, existing_id, existing_status)

        response = await client.post(
            "/v2/workspaces",
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
        existing_id = await _create_v2_workspace(
            client,
            title=f"existing {existing_path}",
            task_class="refactor_task",
            owned_paths=[existing_path],
        )

        response = await client.post(
            "/v2/workspaces",
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
        existing_id = await _create_v2_workspace(
            client,
            title=f"existing {task_class}",
            task_class=task_class,
            owned_paths=["migrations/**"],
        )

        response = await client.post(
            "/v2/workspaces",
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
        assert r2.json()["error_code"] == "IDEMPOTENCY_CONFLICT"


class TestGetWorkspace:
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
    async def test_returns_404_for_unknown_id(self, client: AsyncClient) -> None:
        response = await client.get("/v1/workspaces/ws_doesnotexist000000000000")
        assert response.status_code == 404
        assert response.json()["detail"]["error_code"] == "NOT_FOUND"


class TestWorkspaceDirectRoutes:
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
            listed = await workspaces_route.list_workspaces(session=session)
            detail = await workspaces_route.get_workspace(workspace_id, session=session)
            retry_error = await workspaces_route.retry_workspace("ws_missing", session=session)

        assert [event.event_type for event in events.items] == ["workspace.created"]
        assert stale.items == []
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
    @pytest.mark.parametrize(
        ("param", "value"),
        [("status", "not-a-status"), ("agent", "not-an-agent")],
    )
    async def test_rejects_unknown_filter_enums(
        self, client: AsyncClient, param: str, value: str
    ) -> None:
        response = await client.get("/v1/workspaces", params={param: value})

        assert response.status_code == 422
