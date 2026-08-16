"""REST contract tests for PR monitor adoption."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.api.app import configure_database, create_app
from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.common.config import Settings, get_settings
from awf.common.github_client import PullRequestAdoptionMetadata, RepoRef
from awf.db.enums import TaskClass, WorkspaceStatus
from awf.db.models import Task, TaskAttempt, Workspace
from awf.db.repositories import TaskRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.hosted_delegation import HostedDelegationConfigError
from awf.service import pr_monitor_adoption_helpers as adoption_helpers
from awf.service.pr_monitor_adoption import (
    _adoption_external_id,
    pr_adoption_idempotency_key,
)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> AsyncIterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _metadata(
    *,
    state: str = "OPEN",
    number: int = 277,
    head_ref: str = "feature/ready",
    base_ref: str = "development",
    head_sha: str = "h" * 40,
    base_sha: str = "b" * 40,
) -> PullRequestAdoptionMetadata:
    return PullRequestAdoptionMetadata(
        number=number,
        head_ref=head_ref,
        head_repo_slug="dimileeh/aira-web",
        base_ref=base_ref,
        head_sha=head_sha,
        base_sha=base_sha,
        state=state,
        is_draft=False,
        closed=state == "CLOSED",
        merged=state == "MERGED",
        author="octocat",
        url=f"https://github.com/dimileeh/aira-web/pull/{number}",
        title="feature: ready",
    )


class _MetadataFetcher:
    def __init__(self, metadata: PullRequestAdoptionMetadata) -> None:
        self.metadata = metadata
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, *, repo: RepoRef, pr_number: int) -> PullRequestAdoptionMetadata:
        self.calls.append((repo.slug(), pr_number))
        return self.metadata


@pytest.fixture
async def adoption_client(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, _MetadataFetcher]]:
    settings = Settings(
        _env_file=None,
        api_token="secret",
        work_dir=str(tmp_path / "awf-state"),
    )
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    get_settings.cache_clear()
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    app.dependency_overrides[get_settings] = lambda: settings
    fetcher = _MetadataFetcher(_metadata())
    app.state.pr_adoption_metadata_fetcher = fetcher
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, fetcher


def _optional_string_schema(schema: dict[str, object]) -> dict[str, object]:
    any_of = schema.get("anyOf")
    assert isinstance(any_of, list)
    string_schema = next(
        (item for item in any_of if isinstance(item, dict) and item.get("type") == "string"),
        None,
    )
    assert string_schema is not None, f"Could not find string schema in anyOf: {any_of}"
    assert isinstance(string_schema, dict)
    return string_schema


@pytest.mark.unit
def test_adoption_request_schema_accepts_model_effort_owned_paths_and_openapi_exposes_fields() -> (
    None
):
    payload = PullRequestMonitorAdoptionRequest(
        repo_slug="dimileeh/aira-web",
        pr_number=277,
        model="gpt-5.3-codex",
        effort="high",
        owned_paths=[".github/workflows/publish.yml", "pyproject.toml"],
    )

    assert payload.model == "gpt-5.3-codex"
    assert payload.effort == "high"
    assert payload.owned_paths == [".github/workflows/publish.yml", "pyproject.toml"]

    schema = create_app(use_lifespan=False).openapi()
    props = schema["components"]["schemas"]["PullRequestMonitorAdoptionRequest"]["properties"]
    model_schema = _optional_string_schema(props["model"])
    effort_schema = _optional_string_schema(props["effort"])
    owned_paths_schema = props["owned_paths"]
    assert model_schema["minLength"] == 1
    assert model_schema["maxLength"] == 128
    assert effort_schema["minLength"] == 1
    assert effort_schema["maxLength"] == 64
    assert owned_paths_schema["maxItems"] == 128
    assert owned_paths_schema["items"] == {
        "maxLength": 512,
        "minLength": 1,
        "type": "string",
    }


@pytest.mark.unit
def test_adoption_request_schema_defaults_execution_policy_to_local_and_exposes_hosted() -> None:
    payload = PullRequestMonitorAdoptionRequest(
        repo_slug="dimileeh/aira-web",
        pr_number=277,
    )
    hosted = PullRequestMonitorAdoptionRequest(
        repo_slug="dimileeh/aira-web",
        pr_number=277,
        execution={"mode": "hosted"},
    )

    assert payload.execution.mode == "local"
    assert hosted.execution.mode == "hosted"

    schema = create_app(use_lifespan=False).openapi()
    props = schema["components"]["schemas"]["PullRequestMonitorAdoptionRequest"]["properties"]
    assert "execution" in props
    execution_ref = props["execution"]["$ref"].rsplit("/", 1)[-1]
    execution_schema = schema["components"]["schemas"][execution_ref]
    assert execution_schema["properties"]["mode"]["enum"] == ["local", "hosted"]


@pytest.mark.unit
async def test_adopt_pr_requires_api_token(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
) -> None:
    client, _fetcher = adoption_client

    response = await client.post(
        "/v1/workspaces/adopt-pr",
        json={"repo_slug": "dimileeh/aira-web", "pr_number": 277},
    )

    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "UNAUTHORIZED"


@pytest.mark.unit
async def test_adopt_pr_accepts_repo_slug_and_pr_number(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
    engine: AsyncEngine,
) -> None:
    client, fetcher = adoption_client

    response = await client.post(
        "/v1/workspaces/adopt-pr",
        headers={"Authorization": "Bearer secret"},
        json={
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "agent": "codex",
            "owned_paths": [".github/workflows/publish.yml"],
            "auto_merge": False,
            "initial_review_grace_period_seconds": 0,
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["workspace_id"].startswith("ws_")
    assert body["status"] == "requested"
    assert body["repo_slug"] == "dimileeh/aira-web"
    assert body["pr_number"] == 277
    assert body["head_ref"] == "feature/ready"
    assert body["base_ref"] == "development"
    assert body["auto_merge"] is False
    assert body["attached_existing"] is False
    assert body["validation_provenance"]["freshness_status"] == "unavailable"
    assert fetcher.calls == [("dimileeh/aira-web", 277)]
    session_factory = make_session_factory(engine)
    async with session_factory() as session:
        workspace = (await session.execute(select(Workspace))).scalar_one()
    assert workspace.owned_paths == [".github/workflows/publish.yml"]
    assert workspace.monitor_last_commit_sha == "h" * 40
    assert workspace.base_commit == "b" * 40


@pytest.mark.unit
async def test_adopt_pr_persists_requested_model_and_effort(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
    engine: AsyncEngine,
) -> None:
    client, _fetcher = adoption_client

    response = await client.post(
        "/v1/workspaces/adopt-pr",
        headers={"Authorization": "Bearer secret"},
        json={
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "model": "gpt-5.3-codex",
            "effort": "high",
        },
    )

    assert response.status_code == 202
    session_factory = make_session_factory(engine)
    async with session_factory() as session:
        workspace = (await session.execute(select(Workspace))).scalar_one()
    assert workspace.task_policy["agent_model"] == "gpt-5.3-codex"
    assert workspace.task_policy["agent_effort"] == "high"


@pytest.mark.unit
async def test_adopt_pr_rejects_hosted_execution_when_delegation_unconfigured(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
) -> None:
    client, _fetcher = adoption_client

    response = await client.post(
        "/v1/workspaces/adopt-pr",
        headers={"Authorization": "Bearer secret"},
        json={
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "execution": {"mode": "hosted"},
        },
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "HOSTED_DELEGATION_NOT_CONFIGURED"
    assert body["detail"] == {
        "missing": [
            "AWF_HOSTED_DELEGATION_BASE_URL",
            "AWF_HOSTED_DELEGATION_BEARER_TOKEN or AWF_HOSTED_DELEGATION_BEARER_TOKEN_ENV",
        ],
    }


@pytest.mark.unit
async def test_adopt_pr_rejects_hosted_execution_when_worker_delegation_unconfigured(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        api_token="secret",
        work_dir=str(tmp_path / "awf-state"),
        hosted_delegation_base_url="https://hosted.example.test",
        hosted_delegation_bearer_token="api-visible-token",
    )
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    worker_preflight: dict[str, object] = {}

    def _raise_missing_worker_config(_settings: object, **kwargs: object) -> object:
        worker_preflight.update(kwargs)
        raise HostedDelegationConfigError(
            missing=(
                "AWF_HOSTED_DELEGATION_BEARER_TOKEN or AWF_HOSTED_DELEGATION_BEARER_TOKEN_ENV",
            )
        )

    monkeypatch.setattr(
        adoption_helpers,
        "hosted_delegation_config_from_service_settings",
        _raise_missing_worker_config,
    )
    monkeypatch.setattr(
        adoption_helpers,
        "resolve_service_settings",
        lambda _settings: object(),
    )
    get_settings.cache_clear()
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.pr_adoption_metadata_fetcher = _MetadataFetcher(_metadata())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/workspaces/adopt-pr",
            headers={"Authorization": "Bearer secret"},
            json={
                "repo_slug": "dimileeh/aira-web",
                "pr_number": 277,
                "execution": {"mode": "hosted"},
            },
        )

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "HOSTED_DELEGATION_NOT_CONFIGURED"
    assert body["detail"] == {
        "missing": ["AWF_HOSTED_DELEGATION_BEARER_TOKEN or AWF_HOSTED_DELEGATION_BEARER_TOKEN_ENV"],
    }
    assert worker_preflight == {"required": True}


@pytest.mark.unit
async def test_adopt_pr_hosted_existing_adoption_requires_delegation_preflight(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configured_settings = Settings(
        _env_file=None,
        api_token="secret",
        work_dir=str(tmp_path / "awf-state"),
        hosted_delegation_base_url="https://hosted.example.test",
        hosted_delegation_bearer_token="api-visible-token",
    )
    missing_settings = Settings(
        _env_file=None,
        api_token="secret",
        work_dir=str(tmp_path / "awf-state"),
    )
    current_settings = {"value": configured_settings}
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    get_settings.cache_clear()
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    app.dependency_overrides[get_settings] = lambda: current_settings["value"]
    fetcher = _MetadataFetcher(_metadata())
    app.state.pr_adoption_metadata_fetcher = fetcher

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(
            "/v1/workspaces/adopt-pr",
            headers={"Authorization": "Bearer secret"},
            json={
                "repo_slug": "dimileeh/aira-web",
                "pr_number": 277,
                "auto_merge": False,
                "execution": {"mode": "hosted"},
            },
        )
        conflict = await client.post(
            "/v1/workspaces/adopt-pr",
            headers={"Authorization": "Bearer secret"},
            json={
                "repo_slug": "dimileeh/aira-web",
                "pr_number": 277,
                "auto_merge": True,
                "execution": {"mode": "hosted"},
            },
        )
        current_settings["value"] = missing_settings
        retry = await client.post(
            "/v1/workspaces/adopt-pr",
            headers={"Authorization": "Bearer secret"},
            json={
                "repo_slug": "dimileeh/aira-web",
                "pr_number": 277,
                "auto_merge": False,
                "execution": {"mode": "hosted"},
            },
        )

    assert first.status_code == 202
    first_body = first.json()
    assert first_body["attached_existing"] is False
    assert conflict.status_code == 409
    conflict_body = conflict.json()
    assert conflict_body["error_code"] == "PR_ADOPTION_POLICY_CONFLICT"
    assert conflict_body["detail"] == {
        "workspace_id": first_body["workspace_id"],
        "existing_auto_merge": False,
        "requested_auto_merge": True,
    }
    assert retry.status_code == 409
    retry_body = retry.json()
    assert retry_body["error_code"] == "HOSTED_DELEGATION_NOT_CONFIGURED"
    assert retry_body["detail"] == {
        "missing": [
            "AWF_HOSTED_DELEGATION_BASE_URL",
            "AWF_HOSTED_DELEGATION_BEARER_TOKEN or AWF_HOSTED_DELEGATION_BEARER_TOKEN_ENV",
        ],
    }
    assert fetcher.calls == [("dimileeh/aira-web", 277)]


@pytest.mark.unit
async def test_adopt_pr_accepts_full_pr_url_idempotently(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
    engine: AsyncEngine,
) -> None:
    client, fetcher = adoption_client
    headers = {"Authorization": "Bearer secret"}

    first = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={"repo_slug": "dimileeh/aira-web", "pr_number": 277},
    )
    second = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={"pr_url": "https://github.com/dimileeh/aira-web/pull/277"},
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["attached_existing"] is True
    assert second.json()["workspace_id"] == first.json()["workspace_id"]
    assert fetcher.calls == [("dimileeh/aira-web", 277)]

    session_factory = make_session_factory(engine)
    unknown_status = "monitoring_review_repair"
    async with session_factory() as session:
        workspace = (
            await session.execute(
                select(Workspace).where(Workspace.id == first.json()["workspace_id"])
            )
        ).scalar_one()
        workspace.status = unknown_status
        await session.commit()

    third = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={"pr_url": "https://github.com/dimileeh/aira-web/pull/277"},
    )

    assert third.status_code == 202
    assert third.json()["attached_existing"] is True
    assert third.json()["workspace_id"] == first.json()["workspace_id"]
    assert third.json()["status"] == unknown_status
    assert fetcher.calls == [("dimileeh/aira-web", 277)]


@pytest.mark.unit
async def test_adopt_pr_supersedes_cancelled_previous_adoption(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
    engine: AsyncEngine,
) -> None:
    client, fetcher = adoption_client
    headers = {"Authorization": "Bearer secret"}
    session_factory = make_session_factory(engine)
    fetcher.metadata = _metadata(
        head_ref="feature/stale",
        base_ref="development-old",
        head_sha="a" * 40,
        base_sha="1" * 40,
    )
    first = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "auto_merge": False,
        },
    )
    assert first.status_code == 202
    first_body = first.json()

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        previous = await repo.get(first_body["workspace_id"])
        assert previous is not None
        await repo.transition(previous, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCEL")
        await session.commit()

    fetcher.metadata = _metadata(
        head_ref="feature/current",
        base_ref="development",
        head_sha="c" * 40,
        base_sha="2" * 40,
    )
    second = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "auto_merge": True,
        },
    )

    assert second.status_code == 202
    body = second.json()
    assert body["attached_existing"] is False
    assert body["workspace_id"] != first_body["workspace_id"]
    assert body["head_ref"] == "feature/current"
    assert body["base_ref"] == "development"
    assert body["head_sha"] == "c" * 40
    assert body["base_sha"] == "2" * 40
    assert body["auto_merge"] is True

    canonical_key = pr_adoption_idempotency_key(repo_slug="dimileeh/aira-web", pr_number=277)
    async with session_factory() as session:
        workspaces = list((await session.execute(select(Workspace))).scalars())
    assert len(workspaces) == 2
    previous = next(
        workspace for workspace in workspaces if workspace.id == first_body["workspace_id"]
    )
    fresh = next(workspace for workspace in workspaces if workspace.id == body["workspace_id"])
    assert previous.status == WorkspaceStatus.cancelled.value
    assert previous.idempotency_key != canonical_key
    assert fresh.idempotency_key == canonical_key
    assert previous.task_policy["pr_adoption"]["head_ref"] == "feature/stale"
    assert fresh.task_policy["pr_adoption"]["head_ref"] == "feature/current"


@pytest.mark.unit
async def test_adopt_pr_ignores_destroyed_prior_adoption_and_creates_fresh_monitor(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
    engine: AsyncEngine,
) -> None:
    client, fetcher = adoption_client
    headers = {"Authorization": "Bearer secret"}

    first = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={"repo_slug": "dimileeh/aira-web", "pr_number": 277},
    )
    assert first.status_code == 202
    old_workspace_id = first.json()["workspace_id"]
    session_factory = make_session_factory(engine)
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(old_workspace_id)
        assert workspace is not None
        workspace.status = WorkspaceStatus.destroyed.value
        await session.commit()

    second = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={"repo_slug": "dimileeh/aira-web", "pr_number": 277},
    )

    assert second.status_code == 202
    body = second.json()
    assert body["attached_existing"] is False
    assert body["workspace_id"] != old_workspace_id
    assert body["status"] == "requested"
    assert fetcher.calls == [("dimileeh/aira-web", 277), ("dimileeh/aira-web", 277)]

    async with session_factory() as session:
        rows = list((await session.execute(select(Workspace))).scalars())
    assert len(rows) == 2
    assert sorted(workspace.status for workspace in rows) == ["destroyed", "requested"]


@pytest.mark.unit
async def test_adopt_pr_active_policy_mismatch_returns_structured_conflict(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
) -> None:
    client, _fetcher = adoption_client
    headers = {"Authorization": "Bearer secret"}

    first = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "auto_merge": False,
        },
    )
    assert first.status_code == 202

    conflict = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "auto_merge": True,
        },
    )

    assert conflict.status_code == 409
    body = conflict.json()
    assert body["error_code"] == "PR_ADOPTION_POLICY_CONFLICT"
    assert body["detail"] == {
        "workspace_id": first.json()["workspace_id"],
        "existing_auto_merge": False,
        "requested_auto_merge": True,
    }


@pytest.mark.unit
async def test_adopt_pr_returns_structured_terminal_pr_error(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        api_token="secret",
        work_dir=str(tmp_path / "awf-state"),
    )
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    get_settings.cache_clear()
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.pr_adoption_metadata_fetcher = _MetadataFetcher(_metadata(state="MERGED"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/workspaces/adopt-pr",
            headers={"Authorization": "Bearer secret"},
            json={"repo_slug": "dimileeh/aira-web", "pr_number": 277},
        )

    assert response.status_code == 409
    assert response.json()["error_code"] == "PR_ALREADY_MERGED"


@pytest.mark.unit
@pytest.mark.parametrize("terminal_status", [WorkspaceStatus.failed, WorkspaceStatus.completed])
async def test_terminal_existing_adoption_fetch_error_preserves_idempotency_key(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
    engine: AsyncEngine,
    terminal_status: WorkspaceStatus,
) -> None:
    client, fetcher = adoption_client
    headers = {"Authorization": "Bearer secret"}

    first = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={"repo_slug": "dimileeh/aira-web", "pr_number": 277},
    )
    assert first.status_code == 202
    workspace_id = first.json()["workspace_id"]
    idempotency_key = pr_adoption_idempotency_key(
        repo_slug="dimileeh/aira-web",
        pr_number=277,
    )

    session_factory = make_session_factory(engine)
    async with session_factory() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        workspace.status = terminal_status.value
        assert workspace.idempotency_key == idempotency_key
        await session.commit()

    fetcher.metadata = _metadata(state="MERGED")
    replay = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={"repo_slug": "dimileeh/aira-web", "pr_number": 277},
    )

    assert replay.status_code == 409
    assert replay.json()["error_code"] == "PR_ALREADY_MERGED"
    async with session_factory() as session:
        workspaces = list((await session.execute(select(Workspace))).scalars())
    assert len(workspaces) == 1
    assert workspaces[0].id == workspace_id
    assert workspaces[0].idempotency_key == idempotency_key


@pytest.mark.unit
def test_adoption_request_schema_accepts_optional_external_id_and_task_class() -> None:
    payload = PullRequestMonitorAdoptionRequest(
        repo_slug="dimileeh/aira-web",
        pr_number=277,
        external_id="  CLOUD-TASK-42  ",
        task_class=TaskClass.test_task,
    )
    omitted = PullRequestMonitorAdoptionRequest(
        repo_slug="dimileeh/aira-web",
        pr_number=277,
    )

    assert payload.external_id == "CLOUD-TASK-42"
    assert payload.task_class == TaskClass.test_task
    assert omitted.external_id is None
    assert omitted.task_class is None

    schema = create_app(use_lifespan=False).openapi()
    props = schema["components"]["schemas"]["PullRequestMonitorAdoptionRequest"]["properties"]
    external_id_schema = _optional_string_schema(props["external_id"])
    assert external_id_schema["maxLength"] == 128
    task_class_schema = props["task_class"]
    any_of = task_class_schema.get("anyOf")
    assert isinstance(any_of, list)
    assert any(
        isinstance(item, dict) and item.get("$ref", "").endswith("/TaskClass") for item in any_of
    )


@pytest.mark.unit
def test_adoption_request_schema_rejects_unknown_extra_and_invalid_identity_fields() -> None:
    with pytest.raises(ValidationError):
        PullRequestMonitorAdoptionRequest(
            repo_slug="dimileeh/aira-web",
            pr_number=277,
            unexpected_field="nope",
        )
    with pytest.raises(ValidationError):
        PullRequestMonitorAdoptionRequest(
            repo_slug="dimileeh/aira-web",
            pr_number=277,
            external_id="x" * 129,
        )
    with pytest.raises(ValidationError):
        PullRequestMonitorAdoptionRequest(
            repo_slug="dimileeh/aira-web",
            pr_number=277,
            task_class="not_a_real_task_class",  # type: ignore[arg-type]
        )


@pytest.mark.unit
async def test_adopt_pr_persists_explicit_external_id_and_task_class(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
    engine: AsyncEngine,
) -> None:
    client, _fetcher = adoption_client

    response = await client.post(
        "/v1/workspaces/adopt-pr",
        headers={"Authorization": "Bearer secret"},
        json={
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "external_id": "CLOUD-TASK-42",
            "task_class": "test_task",
        },
    )

    assert response.status_code == 202
    session_factory = make_session_factory(engine)
    async with session_factory() as session:
        workspace = (await session.execute(select(Workspace))).scalar_one()
        attempt = (
            await session.execute(
                select(TaskAttempt).where(TaskAttempt.workspace_id == workspace.id)
            )
        ).scalar_one()
        task = await session.get(Task, attempt.task_id)
    assert workspace.task_external_id == "CLOUD-TASK-42"
    assert workspace.task_class == "test_task"
    assert task is not None
    assert task.external_id == "CLOUD-TASK-42"
    assert task.task_class == "test_task"


@pytest.mark.unit
async def test_adopt_pr_omitted_identity_fields_keep_generated_external_id(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
    engine: AsyncEngine,
) -> None:
    client, _fetcher = adoption_client
    expected = _adoption_external_id(repo_slug="dimileeh/aira-web", pr_number=277)

    response = await client.post(
        "/v1/workspaces/adopt-pr",
        headers={"Authorization": "Bearer secret"},
        json={"repo_slug": "dimileeh/aira-web", "pr_number": 277},
    )

    assert response.status_code == 202
    session_factory = make_session_factory(engine)
    async with session_factory() as session:
        workspace = (await session.execute(select(Workspace))).scalar_one()
    assert workspace.task_external_id == expected
    assert workspace.task_class is None


@pytest.mark.unit
async def test_adopt_pr_exact_identity_replay_attaches_existing(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
) -> None:
    client, fetcher = adoption_client
    headers = {"Authorization": "Bearer secret"}
    body = {
        "repo_slug": "dimileeh/aira-web",
        "pr_number": 277,
        "external_id": "CLOUD-TASK-42",
        "task_class": "test_task",
    }

    first = await client.post("/v1/workspaces/adopt-pr", headers=headers, json=body)
    second = await client.post("/v1/workspaces/adopt-pr", headers=headers, json=body)

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["attached_existing"] is True
    assert second.json()["workspace_id"] == first.json()["workspace_id"]
    assert fetcher.calls == [("dimileeh/aira-web", 277)]


@pytest.mark.unit
async def test_adopt_pr_different_external_id_or_task_class_conflicts_secret_safe(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
) -> None:
    client, _fetcher = adoption_client
    headers = {"Authorization": "Bearer secret"}
    secret_like = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    first = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "external_id": secret_like,
            "task_class": "test_task",
        },
    )
    assert first.status_code == 202

    id_conflict = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "external_id": "OTHER-TASK",
            "task_class": "test_task",
        },
    )
    class_conflict = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "external_id": secret_like,
            "task_class": "docs_task",
        },
    )

    assert id_conflict.status_code == 409
    assert id_conflict.json()["error_code"] == "PR_ADOPTION_POLICY_CONFLICT"
    assert secret_like not in id_conflict.text
    assert class_conflict.status_code == 409
    assert class_conflict.json()["error_code"] == "PR_ADOPTION_POLICY_CONFLICT"
    assert secret_like not in class_conflict.text


@pytest.mark.unit
async def test_adopt_pr_rejects_overlength_external_id_and_unsupported_task_class(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
) -> None:
    client, _fetcher = adoption_client
    headers = {"Authorization": "Bearer secret"}

    overlength = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "external_id": "x" * 129,
        },
    )
    bad_class = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "task_class": "not_a_real_task_class",
        },
    )

    assert overlength.status_code == 422
    assert bad_class.status_code == 422


@pytest.mark.unit
async def test_adopt_pr_rejects_nul_in_external_id(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
) -> None:
    """Control chars in external_id must 422 at the boundary, not fail at flush."""
    client, _fetcher = adoption_client
    headers = {"Authorization": "Bearer secret"}

    response = await client.post(
        "/v1/workspaces/adopt-pr",
        headers=headers,
        json={
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "external_id": "CLOUD\u0000TASK",
        },
    )

    assert response.status_code == 422
    assert "control characters" in response.text


@pytest.mark.unit
async def test_adopt_pr_colliding_explicit_external_id_leaves_db_unchanged(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
    engine: AsyncEngine,
) -> None:
    """409 TASK_EXTERNAL_ID_CONFLICT must not persist orphan adoption state."""
    client, _fetcher = adoption_client
    colliding_id = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    session_factory = make_session_factory(engine)
    async with session_factory() as session:
        unrelated = await TaskRepository(session).create_or_get(
            repo_url="https://github.com/other/repo.git",
            base_branch="main",
            title="unrelated owner",
            prompt="unrelated prompt",
            external_id=colliding_id,
            idempotency_key="api-unrelated-task-key",
            task_class="docs_task",
            owned_paths=[],
        )
        await session.commit()
        unrelated_id = unrelated.id
        unrelated_external_id = unrelated.external_id
        unrelated_title = unrelated.title

    response = await client.post(
        "/v1/workspaces/adopt-pr",
        headers={"Authorization": "Bearer secret"},
        json={
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "external_id": colliding_id,
        },
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "TASK_EXTERNAL_ID_CONFLICT"
    assert colliding_id not in response.text

    canonical_key = pr_adoption_idempotency_key(
        repo_slug="dimileeh/aira-web",
        pr_number=277,
    )
    async with session_factory() as session:
        workspaces = list((await session.execute(select(Workspace))).scalars())
        tasks = list((await session.execute(select(Task))).scalars())
        task = await session.get(Task, unrelated_id)
        by_key = await WorkspaceRepository(session).get_by_idempotency_key(canonical_key)
    assert workspaces == []
    assert len(tasks) == 1
    assert task is not None
    assert task.external_id == unrelated_external_id
    assert task.title == unrelated_title
    assert by_key is None
