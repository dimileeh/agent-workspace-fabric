"""Docker smoke test for a DB-backed profile-declared Python service stack."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from awf.common.commands import AsyncioSubprocessRunner
from awf.node.compose_manager import ComposeManager, WorkspaceComposeSpec
from awf.profiles.compose import profile_services
from awf.profiles.resolver import ProfileResolver
from awf.runtime.validation import ValidationRunner

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "workspace_services" / "python_postgres_app"
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


@pytest.mark.integration
@pytest.mark.docker
@pytest.mark.slow
@pytest.mark.timeout(600)
async def test_python_postgres_profile_runs_setup_health_validate_and_cleans_up(
    tmp_path: Path,
) -> None:
    assert _FIXTURE.is_dir(), "python-postgres workspace-services fixture is missing"
    workspace_id = f"test_python_pg_{os.getpid()}_{tmp_path.name}"
    manager = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
    profile = ProfileResolver().resolve(worktree_path=_FIXTURE, profile_ref="auto").profile
    spec = WorkspaceComposeSpec(
        workspace_id=workspace_id,
        worktree_host_path=_FIXTURE,
        agent_runtime_image="python:3.12-alpine",
        postgres_password="integration-postgres-password",
        agent_environment=tuple(profile.runtime.environment.items()),
        services=profile_services(profile, base_path=_FIXTURE),
    )
    validator = ValidationRunner(
        runner=AsyncioSubprocessRunner(),
        artifacts_dir=tmp_path / "artifacts",
    )

    try:
        paths = await manager.up(spec, wait=True)

        setup_result = await validator.run_profile_phases(
            workspace_id=workspace_id,
            compose_project=spec.project_name(),
            compose_file=paths.compose_file,
            profile=profile,
            phase_names=("setup",),
        )
        assert setup_result.all_passed
        assert [command.phase for command in setup_result.commands] == [
            "setup",
            "db_generated_setup",
        ]
        assert setup_result.commands[0].stdout_path.read_text(encoding="utf-8") == "setup ok\n"
        assert (
            setup_result.commands[1].stdout_path.read_text(encoding="utf-8")
            == "generated setup ok\n"
        )

        validation_result = await validator.run_profile_phases(
            workspace_id=workspace_id,
            compose_project=spec.project_name(),
            compose_file=paths.compose_file,
            profile=profile,
            phase_names=("post_agent", "validate"),
            run_healthchecks=True,
        )
        assert validation_result.all_passed
        assert [command.phase for command in validation_result.commands] == [
            "db_refresh",
            "healthcheck",
            "validate",
        ]
        assert (
            validation_result.commands[0].stdout_path.read_text(encoding="utf-8") == "refresh ok\n"
        )
        healthcheck_stdout = validation_result.commands[1].stdout_path.read_text(encoding="utf-8")
        assert healthcheck_stdout.splitlines()[0].startswith("[healthcheck attempt 1 elapsed ")
        assert healthcheck_stdout.endswith("ok\n")
        assert (
            validation_result.commands[2].stdout_path.read_text(encoding="utf-8")
            == "validated awf-db-profile-fixture\n"
        )

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
