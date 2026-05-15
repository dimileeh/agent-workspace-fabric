"""Tests for ``awf.api.app._lifespan``.

The default ``client`` fixture uses ``create_app(use_lifespan=False)`` so the
real startup body never runs in that path. This file exercises the production
lifespan wiring with stubs.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from awf.api.app import create_app
from awf.common.config import DEFAULT_LOCAL_DATABASE_URL, ProductionSettingsError


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
    def test_lifespan_validates_production_settings_before_engine_creation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.api import app as app_mod

        def _fail_make_engine(_url: str) -> None:
            raise AssertionError("make_engine should not run for invalid production settings")

        monkeypatch.setattr(app_mod, "make_engine", _fail_make_engine)
        monkeypatch.setenv("AWF_ENV", "prod")
        monkeypatch.setenv("AWF_DATABASE_URL", DEFAULT_LOCAL_DATABASE_URL)
        monkeypatch.setenv("AWF_API_TOKEN", "local-dev-token")

        with (
            pytest.raises(ProductionSettingsError) as exc_info,
            TestClient(create_app(use_lifespan=True)),
        ):
            pass

        assert "production_default_database_url" in {
            diagnostic.code for diagnostic in exc_info.value.diagnostics
        }

    @pytest.mark.unit
    def test_lifespan_wires_factory_without_create_all(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Postgres deployments rely on Alembic migrations, not create_all."""
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
        monkeypatch.setenv("AWF_ENV", "local")
        monkeypatch.setenv("AWF_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")

        app = create_app(use_lifespan=True)
        with TestClient(app):
            pass
        assert created_all[0] is False, "lifespan must not call create_all; Alembic owns the schema"
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
        monkeypatch.setenv("AWF_ENV", "local")
        monkeypatch.setenv("AWF_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")

        app = create_app(use_lifespan=True)
        with TestClient(app):
            app.state.db_engine = replacement_engine

        assert replacement_engine.dispose_count == 1
        assert original_engine.dispose_count == 1

    @pytest.mark.unit
    def test_lifespan_shuts_down_callback_target_validation_executor(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.api import app as app_mod

        class _FakeEngine:
            async def dispose(self) -> None:
                pass

        shutdown_calls: list[bool] = []

        def shutdown_callback_executor(*, wait: bool = False) -> None:
            shutdown_calls.append(wait)

        monkeypatch.setattr(app_mod, "make_engine", lambda _url: _FakeEngine())
        monkeypatch.setattr(app_mod, "make_session_factory", lambda _e: lambda: None)
        monkeypatch.setattr(
            app_mod,
            "shutdown_callback_target_validation_executor",
            shutdown_callback_executor,
            raising=False,
        )
        monkeypatch.setenv("AWF_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")

        app = create_app(use_lifespan=True)
        with TestClient(app):
            pass

        assert shutdown_calls == [False]
