"""Exercise real migration chains while checking their subprocess isolation."""

import importlib
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.postgres import postgres_alembic_subprocess_lock


@pytest.mark.parametrize(
    ("module_name", "test_name", "expected_calls"),
    [
        ("test_lifecycle_migration", "test_lifecycle_migration_roundtrip", 4),
        ("test_migration", "test_upgrade_downgrade_upgrade", 4),
    ],
)
async def test_migration_subprocesses_hold_shared_lock_and_explicit_cwd(
    module_name, test_name, expected_calls, monkeypatch, tmp_path
):
    module = importlib.import_module(f"tests.unit.hosted_node.{module_name}")
    root = Path(__file__).resolve().parents[3]
    local = threading.local()
    calls = []
    run = subprocess.run

    @contextmanager
    def locked(url):
        with postgres_alembic_subprocess_lock(url):
            local.url = url
            try:
                yield
            finally:
                local.url = None

    def checked_run(*args, **kwargs):
        cwd = kwargs.get("cwd")
        database_url = kwargs["env"]["AWF_DATABASE_URL"]
        locked_url = getattr(local, "url", None)
        assert cwd == root
        assert locked_url == database_url
        calls.append(args[0])
        return run(*args, **kwargs)

    monkeypatch.setattr(module, "postgres_alembic_subprocess_lock", locked, raising=False)
    monkeypatch.setattr(module.subprocess, "run", checked_run)
    monkeypatch.chdir(tmp_path)
    await getattr(module, test_name)()
    assert len(calls) == expected_calls
