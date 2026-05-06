from __future__ import annotations

from pathlib import Path

import pytest

import tests.conftest as root_conftest


class _FakeItem:
    def __init__(self, path: Path, fixturenames: tuple[str, ...] = ()) -> None:
        self.path = path
        self.fixturenames = fixturenames


class _FakeSession:
    def __init__(self, items: list[_FakeItem]) -> None:
        self.items = items


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

    root_conftest.pytest_collection_finish(_FakeSession([_FakeItem(test_file)]))  # type: ignore[arg-type]

    assert cleanup_calls == ["cleanup"]
