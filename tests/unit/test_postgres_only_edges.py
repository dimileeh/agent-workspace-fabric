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
from tests import postgres as postgres_mod


class _FakeSchemaConnection:
    def __init__(self, schemas: list[str]) -> None:
        self._schemas = schemas

    async def execute(self, _statement: object, _parameters: object = None) -> list[tuple[str]]:
        return [(schema,) for schema in self._schemas]


class _FakeSchemaBegin:
    def __init__(self, schemas: list[str]) -> None:
        self._schemas = schemas

    async def __aenter__(self) -> _FakeSchemaConnection:
        return _FakeSchemaConnection(self._schemas)

    async def __aexit__(self, *_exc_info: object) -> None:
        return None


class _FakeSchemaEngine:
    def __init__(self, schemas: list[str]) -> None:
        self._schemas = schemas

    def begin(self) -> _FakeSchemaBegin:
        return _FakeSchemaBegin(self._schemas)


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
def test_postgres_test_schema_name_is_scoped_to_current_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "ci-shard-a")

    namespace = postgres_mod._postgres_test_schema_namespace()
    schema = postgres_mod._new_postgres_test_schema()

    assert len(namespace) == 16
    assert schema.startswith(f"awf_test_{namespace}_")
    assert len(schema) == len("awf_test_") + 16 + 1 + 32


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_postgres_schema_listing_skips_active_and_unowned_schemas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
    inactive_namespace = "1" * 16
    active_namespace = "2" * 16
    inactive_schema = f"awf_test_{inactive_namespace}_{'a' * 32}"
    active_schema = f"awf_test_{active_namespace}_{'b' * 32}"
    legacy_unowned_schema = f"awf_test_{'c' * 32}"
    seen_namespaces: list[str] = []

    def _is_active(url: str, namespace: str) -> bool:
        assert url == database_url
        seen_namespaces.append(namespace)
        return namespace == active_namespace

    monkeypatch.setattr(postgres_mod, "_is_postgres_test_schema_namespace_active", _is_active)

    schemas = await postgres_mod._list_stale_postgres_test_schemas(
        _FakeSchemaEngine([active_schema, legacy_unowned_schema, inactive_schema]),  # type: ignore[arg-type]
        database_url,
    )

    assert schemas == [inactive_schema]
    assert seen_namespaces == [inactive_namespace, active_namespace]


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
