"""Integration test: ComposeManager against a real Docker daemon.

Launches an explicit postgres-only compose stack (no agent service) on the
host Docker daemon, waits for health, then tears it down. The agent-runtime
image may not exist in every environment, so this test renders a profile-owned
Postgres service and then removes the agent service from the rendered file.

Skipped when:
- no Docker daemon reachable (``docker version`` fails), or
- the ``AWF_SKIP_DOCKER_TESTS=1`` env var is set.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from awf.node.compose_manager import ComposeManager, ComposeService, WorkspaceComposeSpec

_TEMPLATE = Path(__file__).resolve().parents[2] / "docker" / "compose" / "workspace.base.yml.j2"


def _docker_available() -> bool:
    if os.environ.get("AWF_SKIP_DOCKER_TESTS") == "1":
        return False
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "version"],
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon not available; set AWF_SKIP_DOCKER_TESTS=1 to force-skip.",
)


@pytest.mark.integration
@pytest.mark.docker
@pytest.mark.slow
@pytest.mark.timeout(240)
async def test_compose_up_waits_for_postgres_health_then_down_cleans_up(
    tmp_path: Path,
) -> None:
    """End-to-end: render → up --wait → verify healthy → down -v."""
    workspace_id = f"test_{os.getpid()}"  # unique per test run to avoid collisions
    manager = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)

    # Render an explicit profile service, then strip the agent service — we only
    # want to exercise service up/down without depending on an agent image.
    spec = WorkspaceComposeSpec(
        workspace_id=workspace_id,
        worktree_host_path=tmp_path / "fake-worktree",
        postgres_password="integration-test-pw",
        services=(
            ComposeService(
                name="postgres",
                image="postgres:16-alpine",
                environment=(
                    ("POSTGRES_USER", "awf"),
                    ("POSTGRES_PASSWORD", "${AWF_POSTGRES_PASSWORD}"),
                    ("POSTGRES_DB", "awf"),
                ),
                healthcheck_cmd="pg_isready -U awf -d awf",
                volumes=(("postgres_data", "/var/lib/postgresql/data"),),
            ),
        ),
    )
    paths = manager.render(spec)
    rendered = yaml.safe_load(paths.compose_file.read_text())
    del rendered["services"]["agent"]
    paths.compose_file.write_text(yaml.safe_dump(rendered), encoding="utf-8")

    project_name = spec.project_name()

    try:
        # Use the manager's up() — but since we've already written the compose
        # file, spec re-rendering would overwrite it. Call the raw compose
        # runner instead so we exercise the same code path minus the render.
        await manager._compose(  # noqa: SLF001 — intentional: testing the runner directly
            project_name,
            paths.compose_file,
            ["up", "-d", "--wait", "--wait-timeout", "120"],
            operation="up",
        )

        # Verify the Postgres container is healthy via ``docker inspect``.
        inspect = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Health.Status}}",
                f"awf-{workspace_id}-postgres",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert inspect.stdout.strip() == "healthy"

    finally:
        # Down must run even if the assertion failed — otherwise we leak
        # containers/volumes between test runs.
        await manager.down(spec)

    # After down, no containers + no volume should remain.
    ps = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=awf-{workspace_id}-", "--format", "{{.Names}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert ps.stdout.strip() == ""

    volumes = subprocess.run(
        [
            "docker",
            "volume",
            "ls",
            "--filter",
            f"name=awf-{workspace_id}-",
            "--format",
            "{{.Name}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert volumes.stdout.strip() == ""
