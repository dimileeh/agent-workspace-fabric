from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

import tests.conftest as root_conftest
import tests.postgres as postgres


class _FakeConnection:
    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def run_sync(self, fn: Any) -> None:
        fn(self)


class _FakeEngine:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self._events = events

    def begin(self) -> _FakeConnection:
        return _FakeConnection()

    async def dispose(self) -> None:
        self._events.append(f"dispose:{self.name}")


class _FakeItem:
    def __init__(self, path: Any, fixturenames: tuple[str, ...] = ()) -> None:
        self.path = path
        self.fixturenames = fixturenames
        self.markers: list[Any] = []

    def get_closest_marker(self, name: str) -> None:
        del name

    def add_marker(self, marker: Any) -> None:
        self.markers.append(marker)


def test_postgres_fixture_tests_get_extended_timeout(tmp_path: Any) -> None:
    test_file = tmp_path / "test_uses_postgres.py"
    test_file.write_text("from tests.postgres import postgres_test_engine\n", encoding="utf-8")
    item = _FakeItem(test_file)

    root_conftest.pytest_collection_modifyitems(None, [item])  # type: ignore[arg-type, list-item]

    assert len(item.markers) == 1
    marker = item.markers[0]
    assert marker.name == "timeout"
    assert marker.args == (120,)


def test_admin_url_keeps_pooling_for_cleanup_connection_reuse() -> None:
    url = postgres._admin_url("postgresql+asyncpg://u:p@localhost/db?ssl=false")

    assert "awf_search_path=public" in url
    assert "awf_null_pool" not in url


def test_make_test_engine_uses_short_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    def fake_make_engine(url: str, **kwargs: Any) -> _FakeEngine:
        calls["url"] = url
        calls["kwargs"] = kwargs
        return _FakeEngine("engine", [])

    monkeypatch.setattr(postgres, "make_engine", fake_make_engine)

    engine = postgres._make_test_engine("postgresql+asyncpg://u:p@localhost/db")

    assert isinstance(engine, _FakeEngine)
    assert calls == {
        "url": "postgresql+asyncpg://u:p@localhost/db",
        "kwargs": {"connect_args": {"timeout": 10}},
    }


@pytest.mark.unit
async def test_postgres_test_engine_serializes_schema_create_and_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def fake_lock(engine: _FakeEngine) -> AsyncIterator[_FakeEngine]:
        events.append(f"lock:{engine.name}")
        try:
            yield engine
        finally:
            events.append(f"unlock:{engine.name}")

    def fake_make_engine(url: str, **_kwargs: Any) -> _FakeEngine:
        name = "admin" if "awf_search_path=public" in url else "schema"
        return _FakeEngine(name, events)

    async def fake_create_schema(engine: _FakeEngine, quoted_schema: str) -> None:
        del quoted_schema
        events.append(f"create:{engine.name}")

    async def fake_drop_schema(
        engine: _FakeEngine,
        schema: str,
        quoted_schema: str,
    ) -> None:
        del schema, quoted_schema
        events.append(f"drop:{engine.name}")

    def fake_create_all(_conn: _FakeConnection) -> None:
        events.append("create_all")

    monkeypatch.setattr(postgres, "postgres_test_database_url", lambda: "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setattr(postgres, "make_engine", fake_make_engine)
    monkeypatch.setattr(postgres, "_postgres_schema_ddl_lock", fake_lock, raising=False)
    monkeypatch.setattr(postgres, "_create_schema", fake_create_schema)
    monkeypatch.setattr(postgres, "_drop_schema", fake_drop_schema)
    monkeypatch.setattr(postgres.Base.metadata, "create_all", fake_create_all)

    async with postgres.postgres_test_engine() as engine:
        events.append(f"yield:{engine.name}")

    assert events == [
        "lock:admin",
        "create:admin",
        "unlock:admin",
        "create_all",
        "yield:schema",
        "dispose:schema",
        "lock:admin",
        "drop:admin",
        "unlock:admin",
        "dispose:admin",
    ]
