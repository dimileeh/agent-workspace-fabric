"""Dogfood a workspace DinD stack against a real Docker daemon.

This covers the Aira-relevant Docker-in-Docker path: an AWF agent container
gets a profile-owned DinD daemon, uses Docker Compose inside that isolated
daemon, and reaches a tiny project service through the DinD sidecar.

Skipped when:
- no Docker daemon reachable (``docker version`` fails),
- the Docker Compose plugin is unavailable, or
- the ``AWF_SKIP_DOCKER_TESTS=1`` env var is set.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path

import pytest

from awf.node.compose_manager import ComposeManager, WorkspaceComposeSpec

_TEMPLATE = Path(__file__).resolve().parents[2] / "docker" / "compose" / "workspace.base.yml.j2"
_AGENT_IMAGE_BASE = "docker:27-cli"
_DIND_IMAGE = "docker:27-dind"


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


def _run(
    cmd: list[str], *, timeout: int = 60, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _exec_agent(
    *,
    project_name: str,
    compose_file: Path,
    command: str,
    timeout: int = 60,
    check: bool = True,
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
            "agent",
            "sh",
            "-lc",
            command,
        ],
        timeout=timeout,
        check=check,
    )


def _exec_agent_cleanup(
    *,
    project_name: str,
    compose_file: Path,
    command: str,
    timeout: int = 20,
) -> None:
    with suppress(subprocess.TimeoutExpired):
        _exec_agent(
            project_name=project_name,
            compose_file=compose_file,
            command=command,
            timeout=timeout,
            check=False,
        )


def _build_agent_image(tmp_path: Path, workspace_id: str, seed_compose_file: Path) -> str:
    image_dir = tmp_path / "agent-image"
    image_dir.mkdir()
    shutil.copyfile(seed_compose_file, image_dir / "tiny-compose.yml")
    (image_dir / "Dockerfile").write_text(
        (
            f"FROM {_AGENT_IMAGE_BASE}\n"
            "COPY tiny-compose.yml /seed/compose.yml\n"
            "ENTRYPOINT []\n"
            'CMD ["sh", "-c", "sleep infinity"]\n'
        ),
        encoding="utf-8",
    )
    tag = f"awf-dind-agent-test:{workspace_id}"
    _run(["docker", "build", "-t", tag, str(image_dir)], timeout=180)
    return tag


def _write_tiny_project(worktree: Path) -> None:
    project_dir = worktree / "tiny"
    project_dir.mkdir(parents=True)
    (project_dir / "compose.yml").write_text(
        """
services:
  web:
    image: busybox:1.36
    command: >
      sh -c "mkdir -p /www &&
             printf 'hello from isolated dind' > /www/index.html &&
             httpd -f -p 80 -h /www"
    ports:
      - "18080:80"
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://127.0.0.1"]
      interval: 1s
      timeout: 1s
      retries: 30
""".lstrip(),
        encoding="utf-8",
    )


@pytest.mark.integration
@pytest.mark.docker
@pytest.mark.slow
@pytest.mark.timeout(300)
async def test_dind_agent_can_run_project_compose_and_reach_service(tmp_path: Path) -> None:
    workspace_id = f"test_dind_{tmp_path.name}"
    local_project = tmp_path / "local-project"
    worktree_host_path = tmp_path / "agent-worktree"
    worktree_host_path.mkdir()
    _write_tiny_project(local_project)
    agent_image = _build_agent_image(tmp_path, workspace_id, local_project / "tiny" / "compose.yml")

    manager = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
    spec = WorkspaceComposeSpec(
        workspace_id=workspace_id,
        worktree_host_path=worktree_host_path,
        agent_runtime_image=agent_image,
        docker_mode="dind",
        dind_image=_DIND_IMAGE,
    )
    project_name = spec.project_name()
    inner_workspace = f"/workspace/{workspace_id}"
    inner_compose = f"{inner_workspace}/tiny/compose.yml"
    inner_project = "awf-dind-dogfood"
    paths = None

    try:
        paths = await manager.up(spec, wait=True)
        _exec_agent(
            project_name=project_name,
            compose_file=paths.compose_file,
            command=f"mkdir -p {inner_workspace}/tiny && cp /seed/compose.yml {inner_compose}",
        )

        env = _exec_agent(
            project_name=project_name,
            compose_file=paths.compose_file,
            command='printf "%s" "$DOCKER_HOST"',
        )
        assert env.stdout == "tcp://docker:2375"

        info = _exec_agent(
            project_name=project_name,
            compose_file=paths.compose_file,
            command="docker info --format '{{.ServerVersion}}'",
        )
        assert info.stdout.strip()

        _exec_agent(
            project_name=project_name,
            compose_file=paths.compose_file,
            command=(f"docker compose -f {inner_compose} -p {inner_project} up -d --wait"),
            timeout=180,
        )
        response = _exec_agent(
            project_name=project_name,
            compose_file=paths.compose_file,
            command="wget -qO- http://docker:18080",
        )
        assert response.stdout.strip() == "hello from isolated dind"

    finally:
        if paths is not None:
            _exec_agent_cleanup(
                project_name=project_name,
                compose_file=paths.compose_file,
                command=(
                    f"docker compose -f {inner_compose} -p {inner_project} down -v --remove-orphans"
                ),
            )
        if paths is not None:
            _exec_agent_cleanup(
                project_name=project_name,
                compose_file=paths.compose_file,
                command=f"rm -rf {inner_workspace}",
            )
        try:
            await manager.down(spec)
        finally:
            _run(["docker", "image", "rm", "-f", agent_image], check=False)
