"""No-Docker validation-runner contract for the Python/Postgres profile fixture."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from awf.common.commands import FakeCommandRunner
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import ProfileResolver
from awf.runtime.validation import ValidationRunner

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "workspace_services"
    / "python_postgres_app"
)
_COMPOSE_PROJECT = "awf_ws_python_pg"
_COMPOSE_FILE = Path("/fake/compose.yml")


def _load_profile() -> WorkspaceProfile:
    assert _FIXTURE.is_dir(), "python-postgres workspace-services fixture is missing"
    return ProfileResolver().resolve(worktree_path=_FIXTURE, profile_ref="auto").profile


def _load_fixture_app_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "python_postgres_fixture_app",
        _FIXTURE / "app.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_python_postgres_fixture_uses_startup_tolerant_db_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = object()
    calls: list[tuple[tuple[str], dict[str, object]]] = []

    def connect(*args: str, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return connection

    psycopg = ModuleType("psycopg")
    psycopg.__dict__["connect"] = connect
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)

    app = _load_fixture_app_module()
    monkeypatch.setenv("DATABASE_URL", "postgresql://awf@postgres/awf")

    assert app._connect() is connection
    assert calls == [
        (
            ("postgresql://awf@postgres/awf",),
            {"autocommit": True, "connect_timeout": 10},
        )
    ]


@pytest.mark.unit
async def test_python_postgres_profile_setup_runs_setup_phase(tmp_path: Path) -> None:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=0, stdout="setup ok\n")
    validator = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

    result = await validator.run_profile_phases(
        workspace_id="ws_python_pg_setup",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_load_profile(),
        phase_names=("setup",),
    )

    assert result.all_passed
    assert [(command.phase, command.stdout_path.name) for command in result.commands] == [
        ("setup", "01_setup.stdout")
    ]
    assert len(fake.calls) == 1
    assert "/setup" in fake.calls[0].args[-1]
    assert "/healthz" not in fake.calls[0].args[-1]


@pytest.mark.unit
async def test_python_postgres_profile_healthchecks_are_opt_in(tmp_path: Path) -> None:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=0, stdout="validated awf-db-profile-fixture\n")
    validator = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

    result = await validator.run_profile_phases(
        workspace_id="ws_python_pg_no_health",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_load_profile(),
        phase_names=("validate",),
        run_healthchecks=False,
    )

    assert result.all_passed
    assert [(command.phase, command.stdout_path.name) for command in result.commands] == [
        ("validate", "01_validate.stdout")
    ]
    assert len(fake.calls) == 1
    assert "/validate" in fake.calls[0].args[-1]
    assert "/healthz" not in fake.calls[0].args[-1]


@pytest.mark.unit
async def test_python_postgres_profile_healthchecks_precede_validate_phase(
    tmp_path: Path,
) -> None:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=0, stdout="ok\n")
    fake.queue_result(returncode=0, stdout="validated awf-db-profile-fixture\n")
    validator = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

    result = await validator.run_profile_phases(
        workspace_id="ws_python_pg_validate",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_load_profile(),
        phase_names=("post_agent", "validate"),
        run_healthchecks=True,
    )

    assert result.all_passed
    assert [(command.phase, command.stdout_path.name) for command in result.commands] == [
        ("healthcheck", "01_healthcheck.stdout"),
        ("validate", "01_validate.stdout"),
    ]
    assert len(fake.calls) == 2
    assert "/healthz" in fake.calls[0].args[-1]
    assert "/validate" in fake.calls[1].args[-1]
