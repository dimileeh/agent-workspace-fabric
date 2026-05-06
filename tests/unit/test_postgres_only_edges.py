"""PostgreSQL-only edge behavior that used to be hidden by loose test DBs."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.pool import NullPool

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.common.github_client import (
    PullRequestMetadataError,
    RepoRef,
    _head_repo_slug_from_adoption_payload,
)
from awf.db import session as session_mod
from awf.db.session import make_engine
from awf.runtime.merge_coordinator import InProcessMergeCoordinator
from awf.service.doctor import _database_endpoint
from awf.service.pr_monitor_adoption import (
    PRMonitorAdoptionError,
    _inline_profile_name,
    _raise_if_repo_identity_conflicts,
)
from awf.service.secret_leases import _ensure_utc
from awf.service.status import _utc_datetime
from awf.service.worker import _merge_coordinator_for_database_url
from tests.postgres import _drop_schema


class _AsyncConnectionContext:
    def __init__(self, connection: _RecordingConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _RecordingConnection:
        return self._connection

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class _RecordingConnection:
    def __init__(self, statements: list[tuple[str, object | None]]) -> None:
        self._statements = statements

    async def execute(self, statement: object, params: object | None = None) -> None:
        self._statements.append((str(statement), params))


class _SchemaDropEngine:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object | None]] = []

    def begin(self) -> _AsyncConnectionContext:
        return _AsyncConnectionContext(_RecordingConnection(self.statements))

    def connect(self) -> _AsyncConnectionContext:
        raise AssertionError("_drop_schema should rely on DROP SCHEMA CASCADE")


@pytest.mark.unit
def test_make_engine_strips_test_url_options_and_enables_null_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _create_async_engine(url: str, **kwargs: Any) -> object:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return SimpleNamespace(url=url)

    monkeypatch.setattr(session_mod, "create_async_engine", _create_async_engine)

    engine = make_engine(
        "postgresql+asyncpg://awf:pw@localhost:5433/awf"
        "?awf_search_path=first&awf_search_path=second"
        "&awf_null_pool=true&awf_null_pool=false"
    )

    assert engine.url == "postgresql+asyncpg://awf:pw@localhost:5433/awf"
    assert captured["url"] == "postgresql+asyncpg://awf:pw@localhost:5433/awf"
    assert captured["kwargs"]["connect_args"]["server_settings"]["search_path"] == "first"
    assert captured["kwargs"]["poolclass"] is NullPool


@pytest.mark.unit
async def test_drop_schema_uses_single_cascade_drop() -> None:
    engine = _SchemaDropEngine()

    await _drop_schema(engine, "awf_test")

    assert engine.statements == [('DROP SCHEMA IF EXISTS "awf_test" CASCADE', None)]


@pytest.mark.unit
def test_postgres_datetime_helpers_return_aware_utc_values() -> None:
    naive = datetime(2026, 5, 4, 12, 0)

    assert _ensure_utc(naive) == naive.replace(tzinfo=UTC)
    assert _utc_datetime(naive) == naive.replace(tzinfo=UTC)


@pytest.mark.unit
def test_database_endpoint_reports_missing_host() -> None:
    assert _database_endpoint("postgresql+asyncpg:///awf") == (
        "AWF_DATABASE_URL must include a host for local service mode."
    )


@pytest.mark.unit
def test_worker_merge_coordinator_falls_back_for_nonstandard_postgres_scheme() -> None:
    coordinator = _merge_coordinator_for_database_url(
        "postgresqlfoo://awf:pw@localhost/awf",
        engine=SimpleNamespace(),
    )

    assert isinstance(coordinator, InProcessMergeCoordinator)


@pytest.mark.unit
def test_adoption_rejects_unparseable_request_repo_identity() -> None:
    request = PullRequestMonitorAdoptionRequest(repo_url="not-a-github-repo", pr_number=1)

    with pytest.raises(PRMonitorAdoptionError) as exc_info:
        _raise_if_repo_identity_conflicts(
            canonical_repo=RepoRef(owner="owner", name="repo"),
            request=request,
        )

    assert exc_info.value.error_code == "INVALID_GITHUB_REPO"
    assert exc_info.value.detail == {
        "repo": "not-a-github-repo",
        "field": "repo_url",
    }


@pytest.mark.unit
def test_inline_profile_name_requires_mapping_with_string_name() -> None:
    assert _inline_profile_name(None) is None
    assert _inline_profile_name({"name": 42}) is None
    assert _inline_profile_name({"name": "strict-postgres"}) == "strict-postgres"


@pytest.mark.unit
def test_adoption_head_repo_slug_validation_edges() -> None:
    repo = RepoRef(owner="owner", name="repo")

    assert (
        _head_repo_slug_from_adoption_payload(
            {"isCrossRepository": False},
            repo=repo,
            pr_number=7,
        )
        == "owner/repo"
    )

    with pytest.raises(PullRequestMetadataError) as invalid_repo:
        _head_repo_slug_from_adoption_payload(
            {"headRepository": {"nameWithOwner": "not enough parts"}},
            repo=repo,
            pr_number=7,
        )
    assert invalid_repo.value.reason_code == "PR_METADATA_INVALID"

    with pytest.raises(PullRequestMetadataError) as missing_fork_repo:
        _head_repo_slug_from_adoption_payload(
            {"isCrossRepository": True},
            repo=repo,
            pr_number=7,
        )
    assert missing_fork_repo.value.reason_code == "PR_METADATA_INVALID"
