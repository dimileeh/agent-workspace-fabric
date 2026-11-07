"""Real-database tests for the schema-head guard in ``awf.api.app._lifespan``."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import text

from awf.api.app import create_app
from awf.common.config import get_settings
from awf.db.schema_head import (
    DATABASE_SCHEMA_AHEAD_OF_CODE_REASON_CODE,
    DatabaseSchemaAheadOfCodeError,
    code_schema_heads,
    core_script_directory,
    schema_head_label,
)
from awf.db.session import make_engine
from tests.postgres import postgres_empty_test_url
from tests.unit.hosted_node.test_lifecycle_migration import _migrate

UNKNOWN_REVISION = "f0f0f0f0f0f0"
AHEAD_EVENT = "database.schema_ahead_of_code"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Keep each test's ``AWF_DATABASE_URL`` from leaking through ``get_settings``."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _alembic(url: str, *args: str) -> None:
    """Run a real Alembic subprocess, exactly like the compose ``migrate`` service."""
    result = await asyncio.to_thread(_migrate, url, *args)
    assert result.returncode == 0, result.stderr.decode()[-2000:]


def _use_database(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """Point the real lifespan settings at ``url``."""
    monkeypatch.setenv("AWF_ENV", "local")
    monkeypatch.setenv("AWF_DATABASE_URL", url)
    get_settings.cache_clear()


def _start_api() -> list[dict[str, object]]:
    """Run the real API lifespan and assert it serves; return captured logs."""
    with (
        structlog.testing.capture_logs() as captured,
        TestClient(create_app(use_lifespan=True)) as client,
    ):
        assert client.get("/healthz").status_code == 200
    return captured


async def test_migrate_then_api_start_serves_with_guard_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the local bootstrap order (migrate, then API) still starts."""
    async with postgres_empty_test_url() as url:
        await _alembic(url, "upgrade", "head")
        _use_database(monkeypatch, url)

        with TestClient(create_app(use_lifespan=True)) as client:
            assert client.get("/healthz").status_code == 200
            discovery = client.get("/.well-known/awf-core.json")

        assert discovery.status_code == 200
        assert discovery.json()["schema_head"] == schema_head_label(code_schema_heads())


async def test_api_starts_when_database_is_behind_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a known older revision keeps today's start behavior."""
    (head,) = code_schema_heads()
    down_revision = core_script_directory().get_revision(head).down_revision
    assert isinstance(down_revision, str)
    async with postgres_empty_test_url() as url:
        await _alembic(url, "upgrade", down_revision)
        _use_database(monkeypatch, url)

        captured = _start_api()

    assert AHEAD_EVENT not in {entry["event"] for entry in captured}


async def test_api_starts_when_database_has_no_version_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a database without ``alembic_version`` keeps today's start behavior."""
    async with postgres_empty_test_url() as url:
        _use_database(monkeypatch, url)

        captured = _start_api()

    assert AHEAD_EVENT not in {entry["event"] for entry in captured}


async def test_api_refuses_to_start_when_database_revision_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a database migrated by newer code refuses startup with a reason code."""
    async with postgres_empty_test_url() as url:
        await _alembic(url, "upgrade", "head")
        engine = make_engine(url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE alembic_version SET version_num = :rev"),
                    {"rev": UNKNOWN_REVISION},
                )
        finally:
            await engine.dispose()
        _use_database(monkeypatch, url)

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(DatabaseSchemaAheadOfCodeError) as exc_info,
            TestClient(create_app(use_lifespan=True)),
        ):
            pass

    assert exc_info.value.reason_code == DATABASE_SCHEMA_AHEAD_OF_CODE_REASON_CODE
    assert exc_info.value.database_revisions == (UNKNOWN_REVISION,)
    assert exc_info.value.code_heads == code_schema_heads()
    refusals = [entry for entry in captured if entry["event"] == AHEAD_EVENT]
    assert refusals == [
        {
            "event": AHEAD_EVENT,
            "log_level": "error",
            "reason_code": DATABASE_SCHEMA_AHEAD_OF_CODE_REASON_CODE,
            "database_revisions": [UNKNOWN_REVISION],
            "code_heads": list(code_schema_heads()),
        }
    ]
