"""Docker smoke test for workspace services and sidecar reachability.

The unit tests cover profile resolution and rendered compose semantics without
Docker. This smoke test starts only the fixture-owned app and Redis services,
then proves that a peer container on the AWF workspace network reaches them by
Compose service name and container port. The agent service is removed from the
rendered file so the test does not require a local awf-agent-runtime image.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from awf.node.compose_manager import ComposeManager, WorkspaceComposeSpec
from awf.profiles.compose import profile_services
from awf.profiles.resolver import ProfileResolver

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "workspace_services" / "dockerized_app"
)
_TEMPLATE = Path(__file__).resolve().parents[2] / "docker" / "compose" / "workspace.base.yml.j2"


def _docker_available() -> bool:
    if os.environ.get("AWF_SKIP_DOCKER_TESTS") == "1":
        return False
    if shutil.which("docker") is None:
        return False
    for cmd in (["docker", "version"], ["docker", "compose", "version"]):
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                timeout=5,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return False
    return True


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon or Compose plugin not available; set AWF_SKIP_DOCKER_TESTS=1 to force-skip.",
)


def _run(cmd: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _compose_exec(
    *,
    project_name: str,
    compose_file: Path,
    service: str,
    python: str,
) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            "docker",
            "compose",
            "-p",
            project_name,
            "-f",
            str(compose_file),
            "exec",
            "-T",
            service,
            "python",
            "-c",
            python,
        ]
    )


@pytest.mark.integration
@pytest.mark.docker
@pytest.mark.slow
@pytest.mark.timeout(300)
async def test_workspace_services_are_reachable_by_service_name(tmp_path: Path) -> None:
    assert _FIXTURE.is_dir(), "workspace-services fixture is missing"
    workspace_id = f"test_ws_services_{tmp_path.name}"
    manager = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
    profile = ProfileResolver().resolve(worktree_path=_FIXTURE, profile_ref="auto").profile
    spec = WorkspaceComposeSpec(
        workspace_id=workspace_id,
        worktree_host_path=_FIXTURE,
        postgres_password="unused-by-fixture",
        agent_environment=tuple(profile.runtime.environment.items()),
        services=profile_services(profile, base_path=_FIXTURE),
    )
    paths = manager.render(spec)
    rendered = yaml.safe_load(paths.compose_file.read_text())
    del rendered["services"]["agent"]
    rendered["services"]["probe"] = {
        "image": "python:3.12-alpine",
        "command": ["sleep", "infinity"],
        "depends_on": {
            "app": {"condition": "service_healthy"},
            "redis": {"condition": "service_healthy"},
        },
        "networks": ["awf_net"],
        "restart": "no",
    }
    paths.compose_file.write_text(yaml.safe_dump(rendered), encoding="utf-8")

    project_name = spec.project_name()

    try:
        await manager._compose(  # noqa: SLF001 - integration smoke uses rendered file.
            project_name,
            paths.compose_file,
            ["up", "-d", "--wait", "--wait-timeout", "120"],
            operation="up",
        )

        app_probe = _compose_exec(
            project_name=project_name,
            compose_file=paths.compose_file,
            service="probe",
            python=(
                "import urllib.request; "
                "print(urllib.request.urlopen('http://app:8080/healthz', timeout=5)"
                ".read().decode())"
            ),
        )
        assert app_probe.stdout.strip() == "ok"

        redis_probe = _compose_exec(
            project_name=project_name,
            compose_file=paths.compose_file,
            service="probe",
            python=(
                "import socket; "
                "s=socket.create_connection(('redis', 6379), 5); "
                "s.sendall(b'*1\\r\\n$4\\r\\nPING\\r\\n'); "
                "print(s.recv(64).decode())"
            ),
        )
        assert "+PONG" in redis_probe.stdout

    finally:
        await manager.down(spec)

    ps = _run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"name=awf-{workspace_id}-",
            "--format",
            "{{.Names}}",
        ]
    )
    assert ps.stdout.strip() == ""

    volumes = _run(
        [
            "docker",
            "volume",
            "ls",
            "--filter",
            f"name=awf-{workspace_id}-",
            "--format",
            "{{.Name}}",
        ]
    )
    assert volumes.stdout.strip() == ""
