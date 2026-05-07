"""REST contract tests for PR monitor adoption."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.api.app import configure_database, create_app
from awf.common.config import Settings, get_settings
from awf.common.github_client import PullRequestAdoptionMetadata, RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.session import make_session_factory
from awf.service.pr_monitor_adoption import pr_adoption_idempotency_key


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> AsyncIterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _metadata(
    *,
    state: str = "OPEN",
    number: int = 277,
) -> PullRequestAdoptionMetadata:
    return PullRequestAdoptionMetadata(
        number=number,
        head_ref="feature/ready",
        head_repo_slug="dimileeh/aira-web",
        base_ref="development",
        head_sha="h" * 40,
        base_sha="b" * 40,
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
    assert workspace.monitor_last_commit_sha == "h" * 40
    assert workspace.base_commit == "b" * 40


@pytest.mark.unit
async def test_adopt_pr_accepts_full_pr_url_idempotently(
    adoption_client: tuple[AsyncClient, _MetadataFetcher],
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
async def test_terminal_existing_adoption_fetch_error_preserves_idempotency_key(
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
        workspace.status = WorkspaceStatus.failed.value
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
