"""SQLite file replacement guard for the local AWF API."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.api.app import configure_database, create_app
from awf.db.base import Base
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory


@pytest.mark.unit
async def test_api_reconnects_when_sqlite_file_is_replaced(tmp_path: Path) -> None:
    """A running API must follow the visible SQLite path after replacement.

    SQLite connections keep serving an unlinked inode if another process
    recreates ``awf.db``. AWF's console then appears to lose newly-created
    workspaces until the API is restarted. The request dependency detects the
    inode swap and rebuilds the engine before serving the next query.
    """
    db_path = tmp_path / "awf.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    old_engine = make_engine(url)
    async with old_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _seed_workspace(old_engine, title="old invisible workspace")

    app = create_app(use_lifespan=False)
    configure_database(
        app,
        make_session_factory(old_engine),
        engine=old_engine,
        database_url=url,
    )

    db_path.unlink()
    new_engine = make_engine(url)
    try:
        async with new_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed_workspace(new_engine, title="new visible workspace")
    finally:
        await new_engine.dispose()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/workspaces/overview")

    assert response.status_code == 200
    titles = [item["title"] for item in response.json()["items"]]
    assert titles == ["new visible workspace"]

    await app.state.db_engine.dispose()


async def _seed_workspace(engine: AsyncEngine, *, title: str) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title=title,
            task_prompt="test",
            agent="codex",
            test_commands=[],
        )
        await session.commit()
