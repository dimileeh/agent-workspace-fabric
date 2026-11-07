"""Real-database tests for the Core schema-head startup guard."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from awf.db import schema_head
from awf.db.schema_head import (
    DATABASE_SCHEMA_AHEAD_OF_CODE_REASON_CODE,
    CoreMigrationsNotFoundError,
    DatabaseSchemaAheadOfCodeError,
    code_schema_heads,
    core_script_directory,
    schema_head_label,
    verify_database_schema_not_ahead,
)
from awf.db.session import make_engine
from tests.postgres import postgres_empty_test_url
from tests.unit.hosted_node.test_lifecycle_migration import _migrate

_ROOT = Path(__file__).resolve().parents[3]
UNKNOWN_REVISION = "f0f0f0f0f0f0"


async def _alembic(url: str, *args: str) -> None:
    """Run a real Alembic subprocess against ``url`` and assert it succeeded."""
    result = await asyncio.to_thread(_migrate, url, *args)
    assert result.returncode == 0, result.stderr.decode()[-2000:]


def _single_head_down_revision() -> str:
    """Return the down revision of the (single) code head."""
    (head,) = code_schema_heads()
    down = core_script_directory().get_revision(head).down_revision
    assert isinstance(down, str)
    return down


def test_unknown_revision_fixture_is_absent_from_code_graph() -> None:
    """Verify the simulated newer-build revision is not part of Core's graph."""
    known = {rev.revision for rev in core_script_directory().walk_revisions()}
    assert UNKNOWN_REVISION not in known


def test_code_schema_heads_matches_repository_graph() -> None:
    """Verify the resolver finds the repository's own migration graph."""
    script = ScriptDirectory.from_config(Config(str(_ROOT / "alembic.ini")))
    assert code_schema_heads() == tuple(sorted(script.get_heads()))


def test_schema_head_label_is_sorted_comma_join() -> None:
    """Verify multiple heads use the documented sorted comma-joined label."""
    assert schema_head_label(("b", "a")) == "a,b"
    assert schema_head_label(("x",)) == "x"


def test_core_migrations_dir_prefers_packaged_assets_then_source_layout(
    tmp_path: Path,
) -> None:
    """Verify wheel assets win over the source checkout layout."""
    package_dir = tmp_path / "src" / "awf"
    source_versions = tmp_path / "migrations" / "versions"
    source_versions.mkdir(parents=True)
    package_dir.mkdir(parents=True)
    assert schema_head._core_migrations_dir(package_dir) == tmp_path / "migrations"

    packaged = package_dir / "bootstrap_assets" / "migrations"
    (packaged / "versions").mkdir(parents=True)
    assert schema_head._core_migrations_dir(package_dir) == packaged


def test_core_migrations_dir_raises_when_absent(tmp_path: Path) -> None:
    """Verify a missing migration graph is a loud packaging defect."""
    package_dir = tmp_path / "src" / "awf"
    package_dir.mkdir(parents=True)
    (tmp_path / "migrations").mkdir()  # no versions/ directory: not a graph

    with pytest.raises(CoreMigrationsNotFoundError):
        schema_head._core_migrations_dir(package_dir)


async def test_verify_reports_each_state_against_real_database() -> None:
    """Verify unversioned, behind, at-head, and emptied states start normally."""
    async with postgres_empty_test_url() as url:
        engine = make_engine(url)
        try:
            assert await verify_database_schema_not_ahead(engine) == "unversioned"

            await _alembic(url, "upgrade", _single_head_down_revision())
            assert await verify_database_schema_not_ahead(engine) == "behind_head"

            await _alembic(url, "upgrade", "head")
            assert await verify_database_schema_not_ahead(engine) == "at_head"

            async with engine.begin() as connection:
                await connection.execute(text("DELETE FROM alembic_version"))
            assert await verify_database_schema_not_ahead(engine) == "unversioned"
        finally:
            await engine.dispose()


async def test_verify_refuses_unknown_rows_with_named_reason_code() -> None:
    """Verify unknown revisions refuse, including a known-prefix lookalike."""
    (head,) = code_schema_heads()
    prefixed_unknown = f"{head}ff"
    async with postgres_empty_test_url() as url:
        await _alembic(url, "upgrade", "head")
        engine = make_engine(url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
                    {"rev": UNKNOWN_REVISION},
                )
            with pytest.raises(DatabaseSchemaAheadOfCodeError) as mixed:
                await verify_database_schema_not_ahead(engine)
            assert mixed.value.reason_code == DATABASE_SCHEMA_AHEAD_OF_CODE_REASON_CODE
            assert mixed.value.database_revisions == tuple(sorted((head, UNKNOWN_REVISION)))
            assert mixed.value.code_heads == code_schema_heads()
            assert UNKNOWN_REVISION in str(mixed.value)
            assert head in str(mixed.value)

            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM alembic_version WHERE version_num = :rev"),
                    {"rev": UNKNOWN_REVISION},
                )
                await connection.execute(
                    text("UPDATE alembic_version SET version_num = :rev"),
                    {"rev": prefixed_unknown},
                )
            with pytest.raises(DatabaseSchemaAheadOfCodeError) as prefixed:
                await verify_database_schema_not_ahead(engine)
            assert prefixed.value.database_revisions == (prefixed_unknown,)
        finally:
            await engine.dispose()
