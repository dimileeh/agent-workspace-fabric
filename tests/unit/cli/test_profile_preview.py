"""CLI coverage for profile preview terminal output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from awf.cli.main import app

_runner = CliRunner()


@pytest.mark.unit
def test_profile_preview_pretty_is_curated_not_flattened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awf.profiles.resolver as resolver_module

    class _Resolution:
        def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "profile": {
                    "name": "python-postgres",
                    "source": "onboarding:python-postgres",
                    "confidence": "high",
                    "runtime": {"image": "python:3.12", "dockerfile": "Dockerfile"},
                    "services": [
                        {"name": "postgres", "image": "postgres:16"},
                    ],
                    "phases": {
                        "setup": [{"command": "uv sync"}],
                        "validate": [{"command": "pytest -q"}],
                    },
                    "validation": {"coverage": {"target": 0.99}},
                    "security": {"network": {"egress": "limited"}},
                },
                "network_posture": {"status": "warn", "reason": "legacy-open"},
                "lint_findings": [
                    {"severity": "warn", "message": "healthcheck missing"},
                ],
                "candidates_considered": [
                    {"name": "python-postgres", "confidence": "high"},
                ],
                "reason": "Detected pyproject.toml and Postgres service.",
            }

    def _resolve_workspace_profile(**_kwargs: Any) -> object:
        return _Resolution()

    monkeypatch.setattr(
        resolver_module,
        "resolve_workspace_profile",
        _resolve_workspace_profile,
    )

    result = _runner.invoke(app, ["profile", "preview", str(tmp_path), "--format", "pretty"])

    assert result.exit_code == 0, result.output
    assert "Profile: python-postgres" in result.stdout
    assert "Runtime: image=python:3.12 dockerfile=Dockerfile" in result.stdout
    assert "Services: postgres" in result.stdout
    assert "Validation: pytest -q" in result.stdout
    assert "Next: awf init" in result.stdout
    assert "profile.phases.validate[0].command" not in result.stdout
