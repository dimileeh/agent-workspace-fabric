"""Focused coverage for database session helper edge paths."""

from __future__ import annotations

import pytest

from awf.db import session as db_session


class _SyncEngine:
    def __init__(self, dialect: object | None) -> None:
        self.dialect = dialect


class _Engine:
    def __init__(self, dialect: object | None) -> None:
        self.sync_engine = _SyncEngine(dialect)


class _Dialect:
    name = "postgresql"
    driver = "asyncpg"
    do_ping: object = None


class _OtherDialect:
    name = "sqlite"
    driver = "pysqlite"


@pytest.mark.unit
@pytest.mark.parametrize("dialect", [None, _OtherDialect(), _Dialect()])
def test_patch_asyncpg_pre_ping_disconnect_detection_ignores_unsupported_dialects(
    dialect: object | None,
) -> None:
    db_session._patch_asyncpg_pre_ping_disconnect_detection(_Engine(dialect))  # noqa: SLF001
