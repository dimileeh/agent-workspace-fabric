"""No-Docker validation-runner contract for the Node/browser profile fixture."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import ProfileResolver
from awf.runtime.validation import HEALTHCHECK_OK, ValidationRunner

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "workspace_services"
    / "node_next_browser_app"
)
_COMPOSE_PROJECT = "awf_ws_node_browser"
_COMPOSE_FILE = Path("/fake/compose.yml")


def _load_profile() -> WorkspaceProfile:
    assert _FIXTURE.is_dir(), "node browser workspace-services fixture is missing"
    return ProfileResolver().resolve(worktree_path=_FIXTURE, profile_ref="auto").profile


@pytest.mark.unit
async def test_node_next_browser_profile_setup_runs_setup_phase(tmp_path: Path) -> None:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=0, stdout="setup ok\n")
    validator = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

    result = await validator.run_profile_phases(
        workspace_id="ws_node_browser_setup",
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
    assert "node scripts/setup.mjs" in fake.calls[0].args[-1]
    assert "validate-browser" not in fake.calls[0].args[-1]


@pytest.mark.unit
async def test_node_next_browser_profile_healthchecks_are_opt_in(tmp_path: Path) -> None:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=0, stdout="browser validated awf-node-profile-fixture\n")
    validator = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

    result = await validator.run_profile_phases(
        workspace_id="ws_node_browser_no_health",
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
    assert "node scripts/validate-browser.mjs" in fake.calls[0].args[-1]
    assert "healthcheck.mjs" not in fake.calls[0].args[-1]


@pytest.mark.unit
async def test_node_next_browser_profile_healthchecks_precede_browser_validate(
    tmp_path: Path,
) -> None:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=0, stdout="ok\n")
    fake.queue_result(returncode=0, stdout="ok\n")
    fake.queue_result(returncode=0, stdout="browser validated awf-node-profile-fixture\n")
    validator = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

    result = await validator.run_profile_phases(
        workspace_id="ws_node_browser_validate",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_load_profile(),
        phase_names=("post_agent", "validate"),
        run_healthchecks=True,
    )

    assert result.all_passed
    assert [(command.phase, command.stdout_path.name) for command in result.commands] == [
        ("healthcheck", "01_healthcheck.stdout"),
        ("healthcheck", "02_healthcheck.stdout"),
        ("validate", "01_validate.stdout"),
    ]
    assert [command.reason_code for command in result.commands[:2]] == [
        HEALTHCHECK_OK,
        HEALTHCHECK_OK,
    ]
    assert len(fake.calls) == 3
    assert "node scripts/healthcheck.mjs app" in fake.calls[0].args[-1]
    assert "node scripts/healthcheck.mjs browser" in fake.calls[1].args[-1]
    assert "node scripts/validate-browser.mjs" in fake.calls[2].args[-1]
