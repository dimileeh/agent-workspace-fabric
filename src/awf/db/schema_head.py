"""Fail-closed guard against running Core on a database migrated by newer code.

Core's API and worker compare the database's ``alembic_version`` rows with the
migration graph shipped with this build before serving:

- revisions at the code head, known revisions behind it, or no recorded revision
  start normally (the separate ``migrate`` step still owns upgrades);
- any revision unknown to the code graph means a newer build migrated the
  database, so startup refuses with ``DATABASE_SCHEMA_AHEAD_OF_CODE``.

The code head is also advertised through Core discovery as ``schema_head``. A
single head is the bare revision id; multiple heads are sorted ascending and
joined with commas (for example ``"a1b2c3d4e5f6,c3d4e5f6a7b8"``).
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Literal

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import awf
from awf.common.logging import get_logger

_log = get_logger(__name__)

DATABASE_SCHEMA_AHEAD_OF_CODE_REASON_CODE = "DATABASE_SCHEMA_AHEAD_OF_CODE"

SchemaHeadStatus = Literal["at_head", "behind_head", "unversioned"]


class CoreMigrationsNotFoundError(FileNotFoundError):
    """Core's own Alembic migration graph is missing from this installation."""


class DatabaseSchemaAheadOfCodeError(RuntimeError):
    """The database records an Alembic revision this Core build does not know."""

    reason_code = DATABASE_SCHEMA_AHEAD_OF_CODE_REASON_CODE

    def __init__(self, database_revisions: tuple[str, ...], code_heads: tuple[str, ...]) -> None:
        self.database_revisions = database_revisions
        self.code_heads = code_heads
        super().__init__(
            f"{self.reason_code}: database revision(s) {', '.join(database_revisions)} "
            f"are unknown to this Core build (code heads: {', '.join(code_heads)})"
        )


def _core_migrations_dir(package_dir: Path) -> Path:
    """Return Core's migrations directory for a wheel or source/image layout."""
    candidates = (
        package_dir / "bootstrap_assets" / "migrations",
        package_dir.parents[1] / "migrations",
    )
    for candidate in candidates:
        if (candidate / "versions").is_dir():
            return candidate
    raise CoreMigrationsNotFoundError(
        "AWF Core migrations not found; looked in " + ", ".join(str(c) for c in candidates)
    )


@lru_cache(maxsize=1)
def core_script_directory() -> ScriptDirectory:
    """Return the Alembic script directory for Core's own migrations (no env.py run)."""
    return ScriptDirectory(str(_core_migrations_dir(Path(awf.__file__).resolve().parent)))


def code_schema_heads() -> tuple[str, ...]:
    """Return the sorted Alembic head revision(s) of this Core build."""
    return tuple(sorted(core_script_directory().get_heads()))


def schema_head_label(heads: Iterable[str]) -> str:
    """Return the deterministic discovery label: sorted heads joined by commas."""
    return ",".join(sorted(heads))


async def verify_database_schema_not_ahead(engine: AsyncEngine) -> SchemaHeadStatus:
    """Refuse startup when the database records a revision unknown to this build.

    Connectivity errors propagate unchanged; they are neither retried nor hidden.
    """
    async with engine.connect() as connection:
        if await connection.scalar(text("SELECT to_regclass('alembic_version')")) is None:
            return "unversioned"
        result = await connection.execute(text("SELECT version_num FROM alembic_version"))
        database_revisions = tuple(sorted(str(row[0]) for row in result))
    if not database_revisions:
        return "unversioned"
    script = core_script_directory()
    # Exact membership: ``get_revision`` would prefix-match an unknown full id.
    known = {revision.revision for revision in script.walk_revisions()}
    code_heads = code_schema_heads()
    if any(revision not in known for revision in database_revisions):
        _log.error(
            "database.schema_ahead_of_code",
            reason_code=DATABASE_SCHEMA_AHEAD_OF_CODE_REASON_CODE,
            database_revisions=list(database_revisions),
            code_heads=list(code_heads),
        )
        raise DatabaseSchemaAheadOfCodeError(database_revisions, code_heads)
    if set(database_revisions) == set(code_heads):
        return "at_head"
    return "behind_head"
