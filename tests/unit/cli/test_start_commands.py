"""CLI coverage for the reserved ``awf start`` first-run surface."""

from __future__ import annotations

import json

import click
import pytest
from typer.testing import CliRunner

from awf.cli.main import app

_runner = CliRunner()


@pytest.mark.unit
def test_start_help_describes_local_core_surface() -> None:
    result = _runner.invoke(app, ["start", "--help"], env={"COLUMNS": "180"})
    visible_help = click.unstyle(result.output)

    assert result.exit_code == 0, result.output
    assert "Start local AWF Core" in visible_help
    assert "awf service bootstrap" in visible_help
    assert "awf init <path>" in visible_help
    assert "Traceback" not in visible_help


@pytest.mark.unit
def test_start_placeholder_pretty_has_stable_reason_code() -> None:
    result = _runner.invoke(app, ["start"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "AWF_START_PLACEHOLDER" in result.stderr
    assert "awf start" in result.stderr
    assert "awf service bootstrap" in result.stderr
    assert "awf init <path>" in result.stderr
    assert "Traceback" not in result.stderr


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
            "Run awf service bootstrap for current local Core startup.",
            "Run awf init <path> to onboard a project repository.",
        ],
    }
