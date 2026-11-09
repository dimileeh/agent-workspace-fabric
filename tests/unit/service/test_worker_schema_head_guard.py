"""Real-database tests for the schema-head guard in ``awf.service.worker.run_worker``."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from awf.db.schema_head import (
    DATABASE_SCHEMA_AHEAD_OF_CODE_REASON_CODE,
    DatabaseSchemaAheadOfCodeError,
)
from awf.db.session import make_engine
from awf.service import worker as worker_mod
from tests.postgres import postgres_empty_test_url
from tests.unit.hosted_node.test_lifecycle_migration import _migrate
from tests.unit.service.test_worker import _settings

UNKNOWN_REVISION = "f0f0f0f0f0f0"


class _RecordingWorker:
    """Control-worker stand-in recording which run entry points were used."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def run_once(self) -> None:
        self._calls.append("run_once")

    async def wait_for_execution_tasks(self) -> None:
        self._calls.append("wait_for_execution_tasks")

    async def run_once_and_drain(self) -> None:
        await self.run_once()
        await self.wait_for_execution_tasks()

    async def run_forever(self) -> None:
        self._calls.append("run_forever")


async def _alembic(url: str, *args: str) -> None:
    """Run a real Alembic subprocess, exactly like the compose ``migrate`` service."""
    result = await asyncio.to_thread(_migrate, url, *args)
    assert result.returncode == 0, result.stderr.decode()[-2000:]


class _RecordingEngine:
    """Real engine proxy recording ``dispose`` (``AsyncEngine`` attrs are read-only)."""

    def __init__(self, engine: AsyncEngine, calls: list[str]) -> None:
        self.engine = engine
        self._calls = calls

    def connect(self) -> AsyncConnection:
        return self.engine.connect()

    async def dispose(self) -> None:
        self._calls.append("dispose")
        await self.engine.dispose()


def _use_real_engine(monkeypatch: pytest.MonkeyPatch, url: str, calls: list[str]) -> AsyncEngine:
    """Run ``run_worker`` on a real engine for ``url`` with a recording worker."""
    engine = make_engine(url)
    monkeypatch.setattr(
        worker_mod,
        "build_worker_runtime",
        lambda _settings: SimpleNamespace(
            worker=_RecordingWorker(calls), engine=_RecordingEngine(engine, calls)
        ),
    )
    return engine


async def test_worker_runs_when_database_is_at_head(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify the migrate-then-worker order still runs the worker."""
    calls: list[str] = []
    async with postgres_empty_test_url() as url:
        await _alembic(url, "upgrade", "head")
        _use_real_engine(monkeypatch, url, calls)

        await worker_mod.run_worker(_settings(tmp_path, database_url=url), once=True)

    assert calls == ["run_once", "wait_for_execution_tasks", "dispose"]


async def test_worker_refuses_to_start_when_database_revision_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify the worker refuses a newer schema before claiming any work."""
    calls: list[str] = []
    async with postgres_empty_test_url() as url:
        await _alembic(url, "upgrade", "head")
        engine = _use_real_engine(monkeypatch, url, calls)
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num = :rev"),
                {"rev": UNKNOWN_REVISION},
            )

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(DatabaseSchemaAheadOfCodeError) as exc_info,
        ):
            await worker_mod.run_worker(_settings(tmp_path, database_url=url), once=False)

    assert exc_info.value.database_revisions == (UNKNOWN_REVISION,)
    assert calls == ["dispose"]
    assert [
        entry["reason_code"]
        for entry in captured
        if entry["event"] == "database.schema_ahead_of_code"
    ] == [DATABASE_SCHEMA_AHEAD_OF_CODE_REASON_CODE]
