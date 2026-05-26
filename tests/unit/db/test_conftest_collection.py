from __future__ import annotations

from pathlib import Path

import pytest

import tests.conftest as root_conftest


def test_positive_int_env_uses_default_and_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_name = "AWF_POSTGRES_TEST_TIMEOUT_SECONDS"
    monkeypatch.delenv(env_name, raising=False)
    assert root_conftest._positive_int_env(env_name, 120) == 120

    monkeypatch.setenv(env_name, "300")
    assert root_conftest._positive_int_env(env_name, 120) == 300

    monkeypatch.setenv(env_name, "0")
    with pytest.raises(RuntimeError, match="must be a positive integer"):
        root_conftest._positive_int_env(env_name, 120)

    monkeypatch.setenv(env_name, "nope")
    with pytest.raises(RuntimeError, match="must be a positive integer"):
        root_conftest._positive_int_env(env_name, 120)


class _FakeItem:
    def __init__(
        self,
        path: Path,
        fixturenames: tuple[str, ...] = (),
        *,
        name: str = "test_selected",
        lineno: int = 0,
    ) -> None:
        self.path = path
        self.fixturenames = fixturenames
        self.name = name
        self.location = (str(path), lineno, name)


class _FakeSession:
    def __init__(self, items: list[_FakeItem]) -> None:
        self.items = items


class _FakeCollectionItem(_FakeItem):
    def __init__(
        self,
        path: Path,
        fixturenames: tuple[str, ...] = (),
        *,
        markers: dict[str, pytest.Mark] | None = None,
    ) -> None:
        super().__init__(path, fixturenames)
        self.markers = markers or {}
        self.added_markers: list[tuple[pytest.MarkDecorator, bool]] = []

    def get_closest_marker(self, name: str) -> pytest.Mark | None:
        return self.markers.get(name)

    def add_marker(self, marker: pytest.MarkDecorator, append: bool = True) -> None:
        self.added_markers.append((marker, append))
        self.markers[marker.name] = marker.mark


def test_collection_finish_skips_cleanup_for_source_only_helper_mentions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    helper_source = "from " + "tests." + "postgres import " + "postgres_" + "test_" + "engine\n"
    test_file = tmp_path / "test_helper_coverage.py"
    test_file.write_text(helper_source, encoding="utf-8")
    postgres_mod = pytest.importorskip("tests." + "postgres")
    cleanup_name = "cleanup_stale_" + "postgres_" + "test_" + "schemas"

    def fail_cleanup() -> None:
        raise AssertionError("source-only helper coverage should not require DB cleanup")

    monkeypatch.setattr(postgres_mod, cleanup_name, fail_cleanup)

    root_conftest.pytest_collection_finish(_FakeSession([_FakeItem(test_file)]))  # type: ignore[arg-type]


def test_collection_modifyitems_extends_docker_test_timeout(tmp_path: Path) -> None:
    test_file = tmp_path / "test_docker.py"
    test_file.write_text("def test_docker():\n    pass\n", encoding="utf-8")
    item = _FakeCollectionItem(
        test_file,
        markers={
            "docker": pytest.mark.docker.mark,
            "timeout": pytest.mark.timeout(300).mark,
        },
    )

    root_conftest.pytest_collection_modifyitems(None, [item])  # type: ignore[arg-type, list-item]

    assert len(item.added_markers) == 1
    added_marker, append = item.added_markers[0]
    assert added_marker.name == "timeout"
    assert added_marker.mark.args == (root_conftest._DOCKER_TEST_TIMEOUT_SECONDS,)
    assert append is False
    assert item.get_closest_marker("timeout").args == (  # type: ignore[union-attr]
        root_conftest._DOCKER_TEST_TIMEOUT_SECONDS,
    )


def test_collection_modifyitems_preserves_larger_docker_timeout(tmp_path: Path) -> None:
    test_file = tmp_path / "test_docker_long.py"
    test_file.write_text("def test_docker_long():\n    pass\n", encoding="utf-8")
    item = _FakeCollectionItem(
        test_file,
        markers={
            "docker": pytest.mark.docker.mark,
            "timeout": pytest.mark.timeout(root_conftest._DOCKER_TEST_TIMEOUT_SECONDS + 1).mark,
        },
    )

    root_conftest.pytest_collection_modifyitems(None, [item])  # type: ignore[arg-type, list-item]

    assert item.added_markers == []


def test_collection_finish_runs_cleanup_for_managed_db_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_db_fixture.py"
    test_file.write_text("def test_uses_fixture(engine):\n    pass\n", encoding="utf-8")
    postgres_mod = pytest.importorskip("tests." + "postgres")
    cleanup_name = "cleanup_stale_" + "postgres_" + "test_" + "schemas"
    cleanup_calls: list[str] = []

    monkeypatch.setattr(postgres_mod, cleanup_name, lambda: cleanup_calls.append("cleanup"))

    root_conftest.pytest_collection_finish(
        _FakeSession([_FakeItem(test_file, fixturenames=("engine",))])  # type: ignore[arg-type]
    )

    assert cleanup_calls == ["cleanup"]


def test_collection_finish_runs_cleanup_for_direct_postgres_helper_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_direct_db_helper.py"
    test_file.write_text(
        "\n".join(
            [
                "from tests.postgres import create_postgres_test_engine",
                "",
                "async def test_uses_direct_helper():",
                "    engine = await create_postgres_test_engine()",
                "    await engine.dispose()",
            ]
        ),
        encoding="utf-8",
    )
    postgres_mod = pytest.importorskip("tests." + "postgres")
    cleanup_name = "cleanup_stale_" + "postgres_" + "test_" + "schemas"
    cleanup_calls: list[str] = []

    monkeypatch.setattr(postgres_mod, cleanup_name, lambda: cleanup_calls.append("cleanup"))

    root_conftest.pytest_collection_finish(
        _FakeSession(
            [
                _FakeItem(
                    test_file,
                    name="test_uses_direct_helper",
                    lineno=2,
                )
            ]
        )  # type: ignore[arg-type]
    )

    assert cleanup_calls == ["cleanup"]


@pytest.mark.parametrize(
    "helper_name, call_line",
    [
        ("postgres_empty_test_url", "    async with postgres_empty_test_url() as database_url:"),
        ("postgres_test_session", "    async with postgres_test_session() as session:"),
    ],
)
def test_collection_finish_runs_cleanup_for_all_schema_allocating_helpers(
    helper_name: str,
    call_line: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_schema_allocating_helpers.py"
    test_file.write_text(
        "\n".join(
            [
                f"from tests.postgres import {helper_name}",
                "",
                "async def test_uses_schema_allocating_helper():",
                call_line,
                "        pass",
            ]
        ),
        encoding="utf-8",
    )
    postgres_mod = pytest.importorskip("tests." + "postgres")
    cleanup_name = "cleanup_stale_" + "postgres_" + "test_" + "schemas"
    cleanup_calls: list[str] = []

    monkeypatch.setattr(postgres_mod, cleanup_name, lambda: cleanup_calls.append("cleanup"))

    root_conftest.pytest_collection_finish(
        _FakeSession(
            [
                _FakeItem(
                    test_file,
                    name="test_uses_schema_allocating_helper",
                    lineno=2,
                )
            ]
        )  # type: ignore[arg-type]
    )

    assert cleanup_calls == ["cleanup"]


def test_collection_finish_runs_cleanup_for_schema_allocating_module_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_module_fixture_db.py"
    test_file.write_text(
        "\n".join(
            [
                "from collections.abc import AsyncIterator",
                "import pytest",
                "from sqlalchemy.ext.asyncio import AsyncSession",
                "from tests.postgres import postgres_test_session",
                "",
                "@pytest.fixture",
                "async def session() -> AsyncIterator[AsyncSession]:",
                "    async with postgres_test_session() as s:",
                "        yield s",
                "",
                "async def test_uses_module_fixture(session: AsyncSession):",
                "    assert session",
            ]
        ),
        encoding="utf-8",
    )
    postgres_mod = pytest.importorskip("tests." + "postgres")
    cleanup_name = "cleanup_stale_" + "postgres_" + "test_" + "schemas"
    cleanup_calls: list[str] = []

    monkeypatch.setattr(postgres_mod, cleanup_name, lambda: cleanup_calls.append("cleanup"))

    root_conftest.pytest_collection_finish(
        _FakeSession(
            [
                _FakeItem(
                    test_file,
                    fixturenames=("session",),
                    name="test_uses_module_fixture",
                    lineno=10,
                )
            ]
        )  # type: ignore[arg-type]
    )

    assert cleanup_calls == ["cleanup"]


def test_collection_finish_skips_unselected_postgres_helper_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_mixed_db_and_non_db.py"
    test_file.write_text(
        "\n".join(
            [
                "from tests.postgres import postgres_test_url",
                "",
                "def test_selected_without_db():",
                "    assert True",
                "",
                "async def test_unselected_uses_db():",
                "    async with postgres_test_url() as database_url:",
                "        assert database_url",
            ]
        ),
        encoding="utf-8",
    )
    postgres_mod = pytest.importorskip("tests." + "postgres")
    cleanup_name = "cleanup_stale_" + "postgres_" + "test_" + "schemas"

    def fail_cleanup() -> None:
        raise AssertionError("unselected helper calls should not require DB cleanup")

    monkeypatch.setattr(postgres_mod, cleanup_name, fail_cleanup)

    root_conftest.pytest_collection_finish(
        _FakeSession(
            [
                _FakeItem(
                    test_file,
                    name="test_selected_without_db",
                    lineno=2,
                )
            ]
        )  # type: ignore[arg-type]
    )
