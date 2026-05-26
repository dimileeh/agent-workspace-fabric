"""No-Docker validation-runner contract for the Python/Postgres profile fixture."""

from __future__ import annotations

import importlib.util
import struct
from pathlib import Path
from types import ModuleType

import pytest

from awf.common.commands import FakeCommandRunner
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import ProfileResolver
from awf.runtime.validation import ValidationRunner

_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "workspace_services" / "python_postgres_app"
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
    class _Socket:
        def __init__(self) -> None:
            self.timeout: float | None = None
            self.sent: list[bytes] = []
            self.read_buffer = (
                b"R"
                + struct.pack("!I", 8)
                + struct.pack("!I", 0)
                + b"Z"
                + struct.pack("!I", 5)
                + b"I"
            )

        def settimeout(self, timeout: float) -> None:
            self.timeout = timeout

        def sendall(self, data: bytes) -> None:
            self.sent.append(data)

        def recv(self, size: int) -> bytes:
            chunk = self.read_buffer[:size]
            self.read_buffer = self.read_buffer[size:]
            return chunk

    calls: list[tuple[tuple[str, int], float]] = []
    fake_socket = _Socket()

    app = _load_fixture_app_module()
    monkeypatch.setenv("DATABASE_URL", "postgresql://awf@postgres:5432/awf")

    def create_connection(address: tuple[str, int], *, timeout: float) -> _Socket:
        calls.append((address, timeout))
        return fake_socket

    monkeypatch.setattr(app.socket, "create_connection", create_connection)

    connection = app._connect()

    assert connection.host == "postgres"
    assert connection.port == 5432
    assert connection.database == "awf"
    assert connection.user == "awf"
    assert fake_socket.timeout == 10
    assert calls == [(("postgres", 5432), 10)]
    assert fake_socket.sent[0].endswith(
        b"user\x00awf\x00database\x00awf\x00client_encoding\x00UTF8\x00\x00"
    )


@pytest.mark.unit
async def test_python_postgres_profile_setup_runs_setup_phase(tmp_path: Path) -> None:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=0, stdout="setup ok\n")
    fake.queue_result(returncode=0, stdout="generated setup ok\n")
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
        ("setup", "01_setup.stdout"),
        ("db_generated_setup", "01_db_generated_setup.stdout"),
    ]
    assert len(fake.calls) == 2
    assert "/setup" in fake.calls[0].args[-1]
    assert "/setup" in fake.calls[1].args[-1]
    assert all("/healthz" not in call.args[-1] for call in fake.calls)


@pytest.mark.unit
async def test_python_postgres_profile_healthchecks_are_opt_in(tmp_path: Path) -> None:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=0, stdout="refresh ok\n")
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
        ("db_refresh", "01_db_refresh.stdout"),
        ("validate", "01_validate.stdout"),
    ]
    assert len(fake.calls) == 2
    assert "/setup" in fake.calls[0].args[-1]
    assert "/validate" in fake.calls[1].args[-1]
    assert all("/healthz" not in call.args[-1] for call in fake.calls)


@pytest.mark.unit
async def test_python_postgres_profile_healthchecks_precede_validate_phase(
    tmp_path: Path,
) -> None:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=0, stdout="refresh ok\n")
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
        ("db_refresh", "01_db_refresh.stdout"),
        ("healthcheck", "01_healthcheck.stdout"),
        ("validate", "01_validate.stdout"),
    ]
    assert len(fake.calls) == 3
    assert "/setup" in fake.calls[0].args[-1]
    assert "/healthz" in fake.calls[1].args[-1]
    assert "/validate" in fake.calls[2].args[-1]
