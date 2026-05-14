"""CLI coverage for profile preview terminal output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from awf.cli.main import app
from awf.profiles.models import (
    ProfileLintFinding,
    ProfileLintSeverity,
    ProfileResolution,
    WorkspaceProfile,
)

_runner = CliRunner()


@pytest.mark.unit
def test_profile_preview_pretty_is_curated_not_flattened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awf.profiles.resolver as resolver_module

    resolution = ProfileResolution(
        profile=WorkspaceProfile.model_validate(
            {
                "name": "python-postgres",
                "source": "onboarding:python-postgres",
                "confidence": "high",
                "runtime": {"agent_image": "python:3.12"},
                "services": [
                    {"name": "postgres", "image": "postgres:16"},
                ],
                "phases": {
                    "setup": ["uv sync"],
                    "validate": ["pytest -q"],
                },
                "validation": {
                    "coverage": {
                        "minimum_percent": 99,
                        "command": "uv run pytest --cov=awf --cov-report=term",
                    }
                },
            }
        ),
        network_posture="restricted",
        lint_findings=[
            ProfileLintFinding(
                reason_code="HEALTHCHECK_MISSING",
                severity=ProfileLintSeverity.warning,
                message="healthcheck missing",
            )
        ],
        candidates_considered=["python-postgres"],
        reason="Detected pyproject.toml and Postgres service.",
    )

    def _resolve_workspace_profile(**_kwargs: Any) -> object:
        return resolution

    monkeypatch.setattr(
        resolver_module,
        "resolve_workspace_profile",
        _resolve_workspace_profile,
    )

    result = _runner.invoke(app, ["profile", "preview", str(tmp_path), "--format", "pretty"])

    assert result.exit_code == 0, result.output
    assert "Profile: python-postgres" in result.stdout
    assert "Runtime: agent_image=python:3.12" in result.stdout
    assert "Services: postgres" in result.stdout
    assert "Network posture: restricted" in result.stdout
    assert "Validation: pytest -q" in result.stdout
    assert "Coverage target: 99.0%" in result.stdout
    assert "Next: awf init" in result.stdout
    assert "profile.phases.validate[0].command" not in result.stdout
