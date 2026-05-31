"""Shared helpers for the host_setup system_checks test suite."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from awf.host_setup import system_checks
from awf.host_setup.system_checks import (
    CommandResult,
    SetupCheckLevel,
    SetupCheckResult,
)


def _command_runner(
    mapping: dict[tuple[str, ...], CommandResult | None],
) -> system_checks.CommandRunner:
    """Return a fake command runner mapping arg tuples to canned results."""

    def run(args: Sequence[str]) -> CommandResult | None:
        return mapping.get(tuple(args))

    return run


def _stub_non_docker_checks_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every non-docker/compose probe to OK so aggregation runs in isolation."""

    def fake_ok(name: str) -> SetupCheckResult:
        return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")

    monkeypatch.setattr(system_checks, "check_git", lambda: fake_ok("git"))
    monkeypatch.setattr(system_checks, "check_gh", lambda: fake_ok("gh"))
    monkeypatch.setattr(system_checks, "check_python_runtime", lambda: fake_ok("python"))
    monkeypatch.setattr(system_checks, "check_ports", lambda _port: fake_ok("ports"))
    monkeypatch.setattr(
        system_checks, "check_postgres_port", lambda _port: fake_ok("postgres_port")
    )
    monkeypatch.setattr(system_checks, "check_disk", lambda _path: fake_ok("disk"))
    monkeypatch.setattr(system_checks, "check_shell_path", lambda: fake_ok("shell_path"))
    monkeypatch.setattr(system_checks, "check_local_capacity", lambda: fake_ok("local_capacity"))


def _ok(name: str) -> SetupCheckResult:
    return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")


class _FakeCompleted:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_probes_capture_port(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, object],
) -> None:
    """Patch every probe so ``run_system_checks`` runs in isolation, capturing the port."""

    def fake_ok(name: str) -> SetupCheckResult:
        return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")

    def fake_ports(port: int) -> SetupCheckResult:
        captured["port"] = port
        return fake_ok("ports")

    # A real OK docker result carries ``data={"available": True}``; aggregation
    # guards the compose probe on that flag, so the fake must mirror it or compose
    # would be skipped and drop out of the ordered result list.
    monkeypatch.setattr(
        system_checks,
        "check_docker",
        lambda **_kwargs: SetupCheckResult(
            name="docker",
            level=SetupCheckLevel.OK,
            summary="ok",
            detail="ok",
            data={"available": True},
        ),
    )
    monkeypatch.setattr(system_checks, "check_compose", lambda **_kwargs: fake_ok("compose"))
    monkeypatch.setattr(system_checks, "check_git", lambda: fake_ok("git"))
    monkeypatch.setattr(system_checks, "check_gh", lambda: fake_ok("gh"))
    monkeypatch.setattr(system_checks, "check_python_runtime", lambda: fake_ok("python"))
    monkeypatch.setattr(system_checks, "check_ports", fake_ports)
    monkeypatch.setattr(
        system_checks, "check_postgres_port", lambda _port: fake_ok("postgres_port")
    )
    monkeypatch.setattr(system_checks, "check_disk", lambda _path: fake_ok("disk"))
    monkeypatch.setattr(system_checks, "check_shell_path", lambda: fake_ok("shell_path"))
    monkeypatch.setattr(system_checks, "check_local_capacity", lambda: fake_ok("local_capacity"))


def _patch_probes_capture_postgres_port(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, object],
) -> None:
    """Patch every probe so ``run_system_checks`` runs in isolation, capturing the pg port."""

    def fake_ok(name: str) -> SetupCheckResult:
        return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")

    def fake_postgres_port(port: int) -> SetupCheckResult:
        captured["postgres_port"] = port
        return fake_ok("postgres_port")

    # A real OK docker result carries ``data={"available": True}``; aggregation
    # guards the compose probe on that flag, so the fake must mirror it or compose
    # would be skipped and drop out of the ordered result list.
    monkeypatch.setattr(
        system_checks,
        "check_docker",
        lambda **_kwargs: SetupCheckResult(
            name="docker",
            level=SetupCheckLevel.OK,
            summary="ok",
            detail="ok",
            data={"available": True},
        ),
    )
    monkeypatch.setattr(system_checks, "check_compose", lambda **_kwargs: fake_ok("compose"))
    monkeypatch.setattr(system_checks, "check_git", lambda: fake_ok("git"))
    monkeypatch.setattr(system_checks, "check_gh", lambda: fake_ok("gh"))
    monkeypatch.setattr(system_checks, "check_python_runtime", lambda: fake_ok("python"))
    monkeypatch.setattr(system_checks, "check_ports", lambda _port: fake_ok("ports"))
    monkeypatch.setattr(system_checks, "check_postgres_port", fake_postgres_port)
    monkeypatch.setattr(system_checks, "check_disk", lambda _path: fake_ok("disk"))
    monkeypatch.setattr(system_checks, "check_shell_path", lambda: fake_ok("shell_path"))
    monkeypatch.setattr(system_checks, "check_local_capacity", lambda: fake_ok("local_capacity"))


def _patch_probes_capture_disk_path(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, object],
) -> None:
    """Patch every probe so ``run_system_checks`` runs in isolation, capturing the disk path."""

    def fake_ok(name: str) -> SetupCheckResult:
        return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")

    def fake_disk(path: Path) -> SetupCheckResult:
        captured["disk_path"] = path
        return fake_ok("disk")

    # A real OK docker result carries ``data={"available": True}``; aggregation
    # guards the compose probe on that flag, so the fake must mirror it or compose
    # would be skipped and drop out of the ordered result list.
    monkeypatch.setattr(
        system_checks,
        "check_docker",
        lambda **_kwargs: SetupCheckResult(
            name="docker",
            level=SetupCheckLevel.OK,
            summary="ok",
            detail="ok",
            data={"available": True},
        ),
    )
    monkeypatch.setattr(system_checks, "check_compose", lambda **_kwargs: fake_ok("compose"))
    monkeypatch.setattr(system_checks, "check_git", lambda: fake_ok("git"))
    monkeypatch.setattr(system_checks, "check_gh", lambda: fake_ok("gh"))
    monkeypatch.setattr(system_checks, "check_python_runtime", lambda: fake_ok("python"))
    monkeypatch.setattr(system_checks, "check_ports", lambda _port: fake_ok("ports"))
    monkeypatch.setattr(
        system_checks, "check_postgres_port", lambda _port: fake_ok("postgres_port")
    )
    monkeypatch.setattr(system_checks, "check_disk", fake_disk)
    monkeypatch.setattr(system_checks, "check_shell_path", lambda: fake_ok("shell_path"))
    monkeypatch.setattr(system_checks, "check_local_capacity", lambda: fake_ok("local_capacity"))
