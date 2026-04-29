"""Tests for ``awf.api.app._lifespan``.

The default ``client`` fixture uses ``create_app(use_lifespan=False)``
so the real startup body never runs in that path. This file exercises
the production lifespan end-to-end with a temporary SQLite DB to cover
the engine + table-creation + session-factory wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from awf.api.app import create_app


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """``get_settings`` is ``@lru_cache`` in awf.common.config; without
    clearing it every test in this file would see the first test's env.
    """
    from awf.common import config as _cfg

    _cfg.get_settings.cache_clear()
    yield
    _cfg.get_settings.cache_clear()


class TestLifespan:
    @pytest.mark.unit
    def test_sqlite_lifespan_creates_tables_and_wires_factory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Point AIRA at a fresh SQLite file, enter the ``with TestClient``
        lifespan, and confirm (a) the schema got created and (b) the
        dependency can read from app.state.db_session_factory."""
        db_path = tmp_path / "aira.db"
        monkeypatch.setenv("AWF_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

        app = create_app(use_lifespan=True)
        with TestClient(app) as client:
            # Hit a real route that reads from the DB — list_workspaces
            # bounces off app.state.db_session_factory, which only exists
            # if lifespan wired it.
            resp = client.get("/v1/workspaces")
            assert resp.status_code == 200
            assert resp.json() == []
        # DB file was created.
        assert db_path.exists()

    @pytest.mark.unit
    def test_non_sqlite_lifespan_skips_create_all(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """For Postgres in prod, we skip ``create_all`` and rely on
        Alembic migrations. We can't point at a real Postgres in unit
        tests, so we monkeypatch ``make_engine`` + ``make_session_factory``
        to stubs and assert the skip happens."""
        from awf.api import app as app_mod

        created_all = [False]

        class _FakeConn:
            async def run_sync(self, fn) -> None:
                created_all[0] = True

            async def __aenter__(self) -> _FakeConn:
                return self

            async def __aexit__(self, *args) -> None:
                pass

        class _FakeEngine:
            def begin(self) -> _FakeConn:
                return _FakeConn()

            async def dispose(self) -> None:
                pass

        disposed = [False]
        engine = _FakeEngine()
        orig_dispose = engine.dispose

        async def _track_dispose() -> None:
            disposed[0] = True
            await orig_dispose()

        engine.dispose = _track_dispose  # type: ignore[method-assign]
        monkeypatch.setattr(app_mod, "make_engine", lambda _url: engine)
        monkeypatch.setattr(app_mod, "make_session_factory", lambda _e: lambda: None)
        # Non-sqlite URL so the ``startswith("sqlite")`` branch is False.
        monkeypatch.setenv("AWF_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")

        app = create_app(use_lifespan=True)
        with TestClient(app):
            pass
        assert created_all[0] is False, (
            "non-sqlite URL must NOT call create_all — Alembic owns the schema"
        )
        assert disposed[0] is True

    @pytest.mark.unit
    def test_lifespan_disposes_original_engine_when_state_engine_is_replaced(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.api import app as app_mod

        class _FakeEngine:
            def __init__(self) -> None:
                self.dispose_count = 0

            async def dispose(self) -> None:
                self.dispose_count += 1

        original_engine = _FakeEngine()
        replacement_engine = _FakeEngine()
        monkeypatch.setattr(app_mod, "make_engine", lambda _url: original_engine)
        monkeypatch.setattr(app_mod, "make_session_factory", lambda _e: lambda: None)
        monkeypatch.setenv("AWF_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")

        app = create_app(use_lifespan=True)
        with TestClient(app):
            app.state.db_engine = replacement_engine

        assert replacement_engine.dispose_count == 1
        assert original_engine.dispose_count == 1
