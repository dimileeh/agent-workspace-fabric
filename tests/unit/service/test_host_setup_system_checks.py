"""Unit coverage for bounded host readiness checks used by ``awf setup``."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping

import pytest

from awf.host_setup.system_checks import (
    SystemCheck,
    SystemChecksReport,
    run_system_checks,
)

_HEALTHY_DOCKER_INFO = json.dumps({"NCPU": 8, "MemTotal": 17179869184})
_PRESENT_EXECUTABLES = frozenset({"docker", "git", "gh", "awf"})


class _FakeProc:
    """Minimal completed-process stand-in for injected subprocess fakes."""

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeSocket:
    """Socket stand-in that records that it was closed."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _AmpleUsage:
    total = 100 * 1024**3
    used = 10 * 1024**3
    free = 90 * 1024**3


class _StarvedUsage:
    total = 100 * 1024**3
    used = 99 * 1024**3
    free = 1 * 1024**3


def _healthy_which(
    executables: frozenset[str] = _PRESENT_EXECUTABLES,
) -> Callable[[str], str | None]:
    def _which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in executables else None

    return _which


def _docker_runner(
    *,
    docker_info: _FakeProc | Exception | None = None,
    compose: _FakeProc | Exception | None = None,
    calls: list[list[str]] | None = None,
) -> Callable[..., _FakeProc]:
    info_result = docker_info if docker_info is not None else _FakeProc(stdout=_HEALTHY_DOCKER_INFO)
    compose_result = compose if compose is not None else _FakeProc(stdout="2.27.0\n")

    def _run(
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: float,
        env: Mapping[str, str],
    ) -> _FakeProc:
        if calls is not None:
            calls.append(list(args))
        if args[:2] == ["docker", "info"]:
            if isinstance(info_result, Exception):
                raise info_result
            return info_result
        if args[:3] == ["docker", "compose", "version"]:
            if isinstance(compose_result, Exception):
                raise compose_result
            return compose_result
        raise AssertionError(f"unexpected command: {args}")

    return _run


def _refused_connector(address: tuple[str, int], timeout: float) -> _FakeSocket:
    raise OSError("connection refused")


def _build_report(**overrides: object) -> SystemChecksReport:
    """Run checks with healthy fakes, overriding individual dependencies."""
    kwargs: dict[str, object] = {
        "host_port": 8000,
        "work_dir": "/tmp/awf-work",
        "which": _healthy_which(),
        "run_subprocess": _docker_runner(),
        "socket_connector": _refused_connector,
        "disk_usage": lambda _path: _AmpleUsage(),
        "environ": {},
        "python_version": (3, 12),
    }
    kwargs.update(overrides)
    return run_system_checks(**kwargs)  # type: ignore[arg-type]


def _check(report: SystemChecksReport, check_id: str) -> SystemCheck:
    return next(check for check in report.checks if check.id == check_id)


@pytest.mark.unit
def test_all_checks_pass_with_healthy_fakes() -> None:
    """Verify a healthy host reports ``ok`` with populated capacity."""
    report = _build_report()

    assert report.status == "ok"
    assert report.blockers == ()
    assert report.warnings == ()
    assert report.capacity == {"cpu_cores": 8.0, "memory_gb": 16.0}
    assert _check(report, "capacity").status == "ok"


@pytest.mark.unit
def test_docker_cli_missing_is_blocker() -> None:
    """Verify a missing Docker CLI is a reason-coded blocker."""
    report = _build_report(which=_healthy_which(_PRESENT_EXECUTABLES - {"docker"}))

    docker_cli = _check(report, "docker_cli")
    assert docker_cli.status == "fail"
    assert docker_cli.reason == "DOCKER_CLI_NOT_FOUND"
    assert report.status == "fail"
    assert "docker_cli" in [check.id for check in report.blockers]
    # With no CLI, the daemon and compose probes are skipped, not run.
    assert _check(report, "docker_daemon").status == "skipped"
    assert _check(report, "docker_compose").status == "skipped"


@pytest.mark.unit
def test_docker_daemon_unreachable_is_blocker() -> None:
    """Verify a non-zero ``docker info`` is a daemon-unreachable blocker."""
    report = _build_report(
        run_subprocess=_docker_runner(docker_info=_FakeProc(returncode=1, stderr="no daemon"))
    )

    daemon = _check(report, "docker_daemon")
    assert daemon.status == "fail"
    assert daemon.reason == "DOCKER_DAEMON_UNREACHABLE"
    assert daemon.docs_link is not None
    assert report.status == "fail"


@pytest.mark.unit
def test_docker_compose_missing_is_blocker() -> None:
    """Verify a failing ``docker compose version`` is a blocker."""
    report = _build_report(
        run_subprocess=_docker_runner(compose=_FakeProc(returncode=1, stderr="no plugin"))
    )

    compose = _check(report, "docker_compose")
    assert compose.status == "fail"
    assert compose.reason == "COMPOSE_NOT_AVAILABLE"
    assert report.status == "fail"


@pytest.mark.unit
def test_git_missing_is_blocker() -> None:
    """Verify a missing git CLI blocks readiness."""
    report = _build_report(which=_healthy_which(_PRESENT_EXECUTABLES - {"git"}))

    git = _check(report, "git")
    assert git.status == "fail"
    assert git.reason == "GIT_NOT_FOUND"
    assert report.status == "fail"


@pytest.mark.unit
def test_gh_missing_is_warning_not_blocker() -> None:
    """Verify a missing gh CLI is advisory, not blocking."""
    report = _build_report(which=_healthy_which(_PRESENT_EXECUTABLES - {"gh"}))

    gh = _check(report, "gh")
    assert gh.status == "warn"
    assert gh.reason == "GH_NOT_FOUND"
    assert "gh" in [check.id for check in report.warnings]
    assert report.status == "warn"


@pytest.mark.unit
def test_python_below_floor_is_blocker() -> None:
    """Verify an interpreter below the floor blocks readiness."""
    report = _build_report(python_version=(3, 11))

    runtime = _check(report, "python_runtime")
    assert runtime.status == "fail"
    assert runtime.reason == "PYTHON_RUNTIME_BELOW_FLOOR"
    assert runtime.metadata["detected"] == "3.11"
    assert runtime.metadata["required"] == "3.12"
    assert report.status == "fail"


@pytest.mark.unit
def test_api_port_in_use_is_warning() -> None:
    """Verify an occupied port is a warning, not a blocker."""
    sockets: list[_FakeSocket] = []

    def _in_use(address: tuple[str, int], timeout: float) -> _FakeSocket:
        connection = _FakeSocket()
        sockets.append(connection)
        return connection

    report = _build_report(socket_connector=_in_use)

    api_port = _check(report, "port_api")
    assert api_port.status == "warn"
    assert api_port.reason == "PORT_IN_USE"
    assert api_port.metadata["port"] == 8000
    # The probe socket is always closed to avoid leaking descriptors.
    assert all(connection.closed for connection in sockets)
    assert report.status == "warn"


@pytest.mark.unit
def test_ports_free_are_ok() -> None:
    """Verify a refused connection means the port is free."""
    report = _build_report(socket_connector=_refused_connector)

    assert _check(report, "port_api").status == "ok"
    assert _check(report, "port_api").reason == "PORT_AVAILABLE"
    assert _check(report, "port_db").status == "ok"


@pytest.mark.unit
def test_disk_below_threshold_is_blocker() -> None:
    """Verify insufficient free disk is a reason-coded blocker."""
    report = _build_report(disk_usage=lambda _path: _StarvedUsage())

    disk = _check(report, "disk")
    assert disk.status == "fail"
    assert disk.reason == "INSUFFICIENT_DISK"
    assert report.status == "fail"


@pytest.mark.unit
def test_capacity_unavailable_is_warning_only() -> None:
    """Verify missing capacity warns without ever blocking."""
    report = _build_report(run_subprocess=_docker_runner(docker_info=_FakeProc(stdout="{}")))

    capacity = _check(report, "capacity")
    assert capacity.status == "warn"
    assert capacity.reason == "CAPACITY_UNKNOWN"
    assert report.capacity is None
    # The daemon answered, so it is not a blocker.
    assert _check(report, "docker_daemon").status == "ok"
    assert "capacity" not in [check.id for check in report.blockers]


@pytest.mark.unit
def test_shell_path_awf_not_found_is_warning() -> None:
    """Verify a missing awf entry point warns with a PATH hint."""
    report = _build_report(which=_healthy_which(_PRESENT_EXECUTABLES - {"awf"}))

    shell_path = _check(report, "shell_path")
    assert shell_path.status == "warn"
    assert shell_path.reason == "AWF_NOT_ON_PATH"
    assert shell_path.fix is not None and "PATH" in shell_path.fix
    assert report.status == "warn"


@pytest.mark.unit
def test_report_status_rollup() -> None:
    """Verify report status follows fail > warn > ok precedence."""
    ok_check = SystemCheck(id="a", label="A", status="ok", reason="A_OK", message="ok")
    warn_check = SystemCheck(id="b", label="B", status="warn", reason="B_WARN", message="warn")
    fail_check = SystemCheck(id="c", label="C", status="fail", reason="C_FAIL", message="fail")

    assert SystemChecksReport(checks=(ok_check,)).status == "ok"
    assert SystemChecksReport(checks=(ok_check, warn_check)).status == "warn"
    assert SystemChecksReport(checks=(ok_check, warn_check, fail_check)).status == "fail"
    assert SystemChecksReport(checks=(ok_check, warn_check, fail_check)).blockers == (fail_check,)
    assert SystemChecksReport(checks=(ok_check, warn_check, fail_check)).warnings == (warn_check,)


@pytest.mark.unit
def test_subprocess_timeout_is_handled() -> None:
    """Verify a bounded subprocess timeout maps to a fail, not a crash."""
    timeout = subprocess.TimeoutExpired(cmd=["docker", "info"], timeout=5.0)
    report = _build_report(run_subprocess=_docker_runner(docker_info=timeout))

    daemon = _check(report, "docker_daemon")
    assert daemon.status == "fail"
    assert daemon.reason == "DOCKER_DAEMON_UNREACHABLE"
    assert daemon.metadata.get("probe") == "timeout"


@pytest.mark.unit
def test_checks_do_not_start_core() -> None:
    """Verify only bounded read-only probes are invoked (no Core startup)."""
    calls: list[list[str]] = []
    report = _build_report(run_subprocess=_docker_runner(calls=calls))

    assert report.status == "ok"
    assert calls == [
        ["docker", "info", "--format", "{{json .}}"],
        ["docker", "compose", "version", "--short"],
    ]
    flattened = {token for call in calls for token in call}
    assert "up" not in flattened
    assert "bootstrap" not in flattened
    assert "run" not in flattened


@pytest.mark.unit
def test_metadata_is_secret_free() -> None:
    """Verify host environment secrets never leak into the report payload."""
    token = "ghp_abcdefghijklmnopqrstuvwx1234567890"
    report = _build_report(environ={"GH_TOKEN": token, "AWF_GITHUB_TOKEN": token})

    serialized = json.dumps(report.to_dict())
    assert token not in serialized
