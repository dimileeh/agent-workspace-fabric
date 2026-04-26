"""Workspace API contract tests.

Each test runs against a fresh in-memory SQLite via the ``client`` fixture.
These are *unit*-flavoured tests because they don't spin up Docker or Postgres;
true integration + E2E tests live under tests/integration/ and tests/e2e/.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.api.app import configure_database, create_app
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
) -> dict[str, object]:
    task = {
        **_V2_MINIMAL_BODY["task"],
        "title": title,
    }
    if task_class is not None:
        task["task_class"] = task_class
    if owned_paths is not None:
        task["owned_paths"] = owned_paths
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


class TestCreateWorkspaceV2DiskPressure:
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
    async def test_legacy_v1_defaults_policy_metadata(self, client: AsyncClient) -> None:
        ws_id = await _create_workspace(client)

        response = await client.get(f"/v1/workspaces/{ws_id}")

        assert response.status_code == 200
        assert response.json()["task_class"] is None
        assert response.json()["owned_paths"] == []

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
