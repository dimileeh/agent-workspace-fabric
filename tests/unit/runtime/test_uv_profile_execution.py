"""Command execution, not uv flag spelling, decides validation success."""

from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import ValidationRunner


@pytest.mark.unit
@pytest.mark.parametrize("command", ["uv run pytest -q", "uv run --no-sync pytest -q"])
@pytest.mark.parametrize("returncode", [0, 1, 127])
async def test_uv_validation_uses_actual_command_result(
    tmp_path: Path, command: str, returncode: int
) -> None:
    profile = WorkspaceProfile.model_validate(
        {"name": "uv", "phases": {"setup": ["uv sync --extra dev"], "validate": [command]}}
    )
    runner = FakeCommandRunner()
    runner.queue_result(returncode=returncode, stdout="actual execution", stderr="")
    validation = ValidationRunner(runner=runner, artifacts_dir=tmp_path)
    result = await validation.run_profile_phases(
        workspace_id="ws_uv",
        compose_project="uv",
        compose_file=tmp_path / "compose.yml",
        profile=profile,
        phase_names=("validate",),
        include_coverage=False,
    )
    assert result.all_passed == (returncode == 0)
    assert len(result.commands) == 1
    assert result.commands[0].returncode == returncode
    assert runner.calls[-1].args[-1].endswith(command)
