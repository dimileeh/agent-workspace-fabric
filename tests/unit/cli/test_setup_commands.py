"""CLI coverage for the reserved ``awf setup`` first-run surface."""

from __future__ import annotations

import json

import click
import pytest
from typer.testing import CliRunner

from awf.cli.main import app

_runner = CliRunner()


@pytest.mark.unit
def test_setup_help_describes_first_run_surface() -> None:
    result = _runner.invoke(app, ["setup", "--help"], env={"COLUMNS": "180"})
    visible_help = click.unstyle(result.output)

    assert result.exit_code == 0, result.output
    assert "Prepare this machine for AWF" in visible_help
    assert "awf service bootstrap" in visible_help
    assert "awf init <path>" in visible_help
    assert "Traceback" not in visible_help


@pytest.mark.unit
def test_setup_placeholder_pretty_has_stable_reason_code() -> None:
    result = _runner.invoke(app, ["setup"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "AWF_SETUP_PLACEHOLDER" in result.stderr
    assert "awf setup" in result.stderr
    assert "awf service bootstrap" in result.stderr
    assert "awf init <path>" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.unit
def test_setup_placeholder_json_has_stable_shape() -> None:
    result = _runner.invoke(app, ["setup", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload == {
        "status": "blocked",
        "reason_code": "AWF_SETUP_PLACEHOLDER",
        "command": "awf setup",
        "message": "awf setup is reserved; host setup checks land in a later setup slice.",
        "next_steps": [
            "Run awf service bootstrap for current local Core startup.",
            "Run awf init <path> to onboard a project repository.",
        ],
    }
