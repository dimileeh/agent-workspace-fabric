"""CLI coverage for project onboarding profile initialization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from awf.cli.main import app
from awf.profiles.models import WorkspaceProfile

_runner = CliRunner()


@pytest.mark.unit
def test_profile_init_preview_prints_profile_and_diagnostics(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n', encoding="utf-8")

    result = _runner.invoke(app, ["profile", "init", str(tmp_path)])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["draft"]["profile"]["name"] == "python"
    assert payload["draft"]["profile"]["security"]["egress"]["mode"] == "restricted"
    assert "yaml" in payload["draft"]
    assert "missing_validation_commands" in payload["diagnostics"]
    assert not (tmp_path / ".awf" / "workspace.yml").exists()


@pytest.mark.unit
def test_profile_init_write_creates_awf_workspace_yml(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    result = _runner.invoke(app, ["profile", "init", str(tmp_path), "--write"])

    assert result.exit_code == 0
    profile_path = tmp_path / ".awf" / "workspace.yml"
    assert profile_path.is_file()
    payload = json.loads(result.stdout)
    assert payload["written_path"] == str(profile_path)
    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    profile = WorkspaceProfile.model_validate(raw["awf"])
    assert profile.name == "python"
    assert profile.security.egress.mode == "restricted"


@pytest.mark.unit
def test_profile_init_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    profile_dir = tmp_path / ".awf"
    profile_dir.mkdir()
    profile_path = profile_dir / "workspace.yml"
    profile_path.write_text("awf:\n  name: existing\n", encoding="utf-8")

    result = _runner.invoke(app, ["profile", "init", str(tmp_path), "--write"])
    forced = _runner.invoke(app, ["profile", "init", str(tmp_path), "--write", "--force"])

    assert result.exit_code != 0
    assert "already exists" in result.stderr
    assert forced.exit_code == 0
    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    assert WorkspaceProfile.model_validate(raw["awf"]).name == "generic"


@pytest.mark.unit
def test_profile_init_includes_optional_smoke_request(tmp_path: Path) -> None:
    result = _runner.invoke(
        app,
        ["profile", "init", str(tmp_path), "--include-smoke-request"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["smoke_request"]["repo"]["url"].startswith("file://")
    assert payload["smoke_request"]["workspace"]["profile"]["name"] == "generic"
    assert (
        payload["smoke_request"]["workspace"]["profile"]["security"]["egress"]["mode"]
        == "restricted"
    )
    assert payload["smoke_request"]["task"]["auto_merge"] is False
