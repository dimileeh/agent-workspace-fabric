"""ValidationRunner tests with FakeCommandRunner (no docker needed).

Continuation of ``test_validation_part_001`` — setup-dependency retry and
advisory-command behaviours split out to keep each part under the
first-party file line limit.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import (
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    ValidationRunner,
)

_COMPOSE_PROJECT = "awf_ws_val"
_COMPOSE_FILE = Path("/fake/compose.yml")


def _uv_pypi_dns_failure(*, package: str = "docker==7.1.0") -> str:
    return f"""
  x Failed to download `{package}`
  |- Failed to fetch: `https://files.pythonhosted.org/packages/aa/bb/docker-7.1.0.whl`
  |- Request failed after 3 retries
  |- error sending request for url (https://files.pythonhosted.org/packages/aa/bb/docker-7.1.0.whl)
  `- client error (Connect): dns error: failed to lookup address information: No address associated with hostname
""".strip()


@pytest.mark.unit
async def test_setup_dependency_network_failure_retries_and_succeeds_on_cache_hit(
    tmp_path: Path,
) -> None:
    fake = FakeCommandRunner()
    val = ValidationRunner(
        runner=fake,
        artifacts_dir=tmp_path / "artifacts",
        setup_retry_backoff_seconds=(0,),
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "setup-retry",
            "phases": {"setup": ["uv sync --extra dev"]},
        }
    )
    fake.queue_result(returncode=1, stderr=_uv_pypi_dns_failure())
    fake.queue_result(returncode=0, stdout="Using cached docker==7.1.0\n")

    result = await val.run_profile_phases(
        workspace_id="ws_setup_retry",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=profile,
        phase_names=("setup",),
    )

    assert result.all_passed
    assert len(fake.calls) == 2
    command = result.commands[0]
    assert command.retry_count == 1
    retry_metadata = command.metadata["setup_dependency_network"]
    assert retry_metadata["reason_code"] == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert retry_metadata["retry_count"] == 1
    assert retry_metadata["retry_exhausted"] is False
    assert retry_metadata["package"] == "docker==7.1.0"
    assert retry_metadata["host"] == "files.pythonhosted.org"
    stderr_text = command.stderr_path.read_text(encoding="utf-8")
    stdout_text = command.stdout_path.read_text(encoding="utf-8")
    retry_prefix_pattern = r"\[setup dependency network retry 1 at \d+(?:\.\d+)?s\]"
    assert re.search(retry_prefix_pattern, stdout_text)
    assert re.search(retry_prefix_pattern, stderr_text)
    assert _uv_pypi_dns_failure() in stderr_text
    assert "Using cached docker==7.1.0" in stdout_text


@pytest.mark.unit
async def test_setup_dependency_retry_does_not_consume_flaky_retry_budget(
    tmp_path: Path,
) -> None:
    fake = FakeCommandRunner()
    val = ValidationRunner(
        runner=fake,
        artifacts_dir=tmp_path / "artifacts",
        setup_retry_budget=2,
        setup_retry_backoff_seconds=(0,),
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "setup-retry",
            "phases": {"setup": ["uv sync --extra dev"]},
            "validation": {"retry_budget": 1},
        }
    )
    fake.queue_result(returncode=1, stderr=_uv_pypi_dns_failure())
    fake.queue_result(returncode=124, stderr="command timed out")
    fake.queue_result(returncode=0, stdout="Using cached docker==7.1.0\n")

    result = await val.run_profile_phases(
        workspace_id="ws_setup_then_flaky_retry",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=profile,
        phase_names=("setup",),
    )

    assert result.all_passed
    assert len(fake.calls) == 3
    command = result.commands[0]
    assert command.retry_count == 2
    retry_metadata = command.metadata["setup_dependency_network"]
    assert retry_metadata["retry_count"] == 1
    assert retry_metadata["setup_retry_count"] == 1
    assert retry_metadata["flaky_retry_count"] == 1
    assert retry_metadata["total_retry_count"] == 2
    assert retry_metadata["retry_budget"] == 2
    assert retry_metadata["retry_exhausted"] is False
    assert len(retry_metadata["attempts"]) == 1
    assert retry_metadata["attempts"][0]["attempt"] == 1
    assert retry_metadata["attempts"][0]["retry_number"] == 1


@pytest.mark.unit
async def test_optional_command_failure_is_advisory_and_does_not_fail_validation(
    tmp_path: Path,
) -> None:
    """A phase command with ``required: false`` must not block validation.

    Regression for PRRT_kwDOSJAM6s6IRgv7: the optional command's non-zero
    result was appended verbatim, so ``ValidationResult.all_passed`` (and
    ``first_failure``) still treated the workspace as failed even though the
    command was explicitly advisory.
    """
    fake = FakeCommandRunner()
    val = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "optional-cmd",
            "phases": {
                "validate": [
                    {"command": "advisory-lint", "required": False},
                    "pytest -q",
                ]
            },
        }
    )
    fake.queue_result(returncode=1, stderr="advisory lint reported issues")
    fake.queue_result(returncode=0, stdout="all tests passed")

    result = await val.run_profile_phases(
        workspace_id="ws_optional_cmd",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=profile,
        phase_names=("validate",),
    )

    # The advisory failure neither halts the sequence nor fails the verdict.
    assert result.all_passed
    assert result.first_failure is None
    assert len(fake.calls) == 2
    advisory = result.commands[0]
    assert advisory.returncode == 1
    assert advisory.required is False
    assert not advisory.ok
