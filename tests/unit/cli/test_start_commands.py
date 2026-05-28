"""CLI coverage for the reserved ``awf start`` first-run surface."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from awf.cli.main import app

_runner = CliRunner()


@pytest.mark.unit
def test_start_help_describes_local_core_surface() -> None:
    result = _runner.invoke(app, ["start", "--help"], env={"COLUMNS": "180"})

    assert result.exit_code == 0, result.output
    assert "Start local AWF Core" in result.output
    assert "awf setup" in result.output
    assert "awf init <path>" in result.output
    assert "Traceback" not in result.output


@pytest.mark.unit
def test_start_placeholder_pretty_has_stable_reason_code() -> None:
    result = _runner.invoke(app, ["start"])

    assert result.exit_code == 1
    assert "AWF_START_PLACEHOLDER" in result.output
    assert "awf start" in result.output
    assert "awf setup" in result.output
    assert "awf init <path>" in result.output
    assert "Traceback" not in result.output


@pytest.mark.unit
def test_start_placeholder_json_has_stable_shape() -> None:
    result = _runner.invoke(app, ["start", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload == {
        "status": "blocked",
        "reason_code": "AWF_START_PLACEHOLDER",
        "command": "awf start",
        "message": "awf start is reserved; local Core startup lands in a later start slice.",
        "next_steps": [
            "Run awf setup first once setup is implemented.",
            "Run awf init <path> to onboard a project repository.",
        ],
    }
