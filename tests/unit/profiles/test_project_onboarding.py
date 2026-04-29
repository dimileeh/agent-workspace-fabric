"""Project onboarding profile draft tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from awf.profiles.models import DockerMode, WorkspaceProfile
from awf.profiles.onboarding import (
    draft_workspace_profile,
    inspect_project,
    preview_project_onboarding,
)


@pytest.mark.unit
def test_detects_generic_template_for_unknown_repo(tmp_path: Path) -> None:
    inspection = inspect_project(tmp_path)
    preview = preview_project_onboarding(tmp_path)

    assert inspection.detected_template == "generic"
    assert inspection.confidence == "low"
    assert preview.draft.template == "generic"
    assert preview.draft.profile.confidence == "low"
    assert preview.diagnostics.missing_validation_commands


@pytest.mark.unit
def test_detects_python_template_and_generates_valid_profile(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n', encoding="utf-8")

    inspection = inspect_project(tmp_path)
    draft = draft_workspace_profile(inspection)

    assert inspection.detected_template == "python"
    assert draft.template == "python"
    assert draft.profile.name == "python"
    assert [command.command for command in draft.profile.phases.validate_commands] == ["pytest -q"]
    assert WorkspaceProfile.model_validate(draft.profile.model_dump(mode="json")) == draft.profile


@pytest.mark.unit
def test_detects_node_nextjs_template_from_package_json(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"lint": "next lint", "test": "vitest", "build": "next build"},
                "dependencies": {"next": "15.0.0"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")

    draft = draft_workspace_profile(inspect_project(tmp_path))

    assert draft.template == "node-nextjs"
    assert draft.profile.name == "node-nextjs"
    assert draft.profile.phases.setup[0].command == "pnpm install --frozen-lockfile"
    assert [command.command for command in draft.profile.phases.validate_commands] == [
        "pnpm run lint",
        "pnpm test",
        "pnpm run build",
    ]
    assert WorkspaceProfile.model_validate(draft.profile.model_dump(mode="json")).name == (
        "node-nextjs"
    )


@pytest.mark.unit
def test_detects_docker_compose_template_and_reports_gaps(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  api:\n"
        "    image: example/api:latest\n"
        "    environment:\n"
        "      API_TOKEN: ${API_TOKEN}\n",
        encoding="utf-8",
    )

    preview = preview_project_onboarding(tmp_path)

    assert preview.draft.template == "docker-compose"
    assert preview.draft.profile.docker.mode == DockerMode.dind
    assert preview.draft.profile.docker.compose_files == ["docker-compose.yml"]
    assert preview.draft.profile.phases.setup[0].command == "docker compose up -d --wait"
    assert "api" in preview.diagnostics.missing_healthchecks
    assert "api" in preview.diagnostics.missing_ports
    assert "API_TOKEN" in preview.diagnostics.missing_secrets


@pytest.mark.unit
def test_detects_python_postgres_template(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\ndependencies = ["psycopg"]\n',
        encoding="utf-8",
    )
    (tmp_path / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")

    preview = preview_project_onboarding(tmp_path)
    profile = preview.draft.profile

    assert preview.draft.template == "python-postgres"
    assert [service.name for service in profile.services] == ["postgres"]
    assert profile.runtime.environment["DATABASE_URL"].startswith("postgresql+psycopg://")
    assert "${POSTGRES_PASSWORD}" in profile.runtime.environment["DATABASE_URL"]
    assert all(secret.ref is None for secret in profile.secrets)
    assert [secret.target for secret in profile.secrets] == ["POSTGRES_PASSWORD"]
    assert profile.validation.healthchecks[0].name == "postgres"
    assert profile.services[0].healthcheck_cmd == "pg_isready -U awf -d awf"


@pytest.mark.unit
def test_detects_node_playwright_template(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"test:e2e": "playwright test"},
                "devDependencies": {"@playwright/test": "latest"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")

    preview = preview_project_onboarding(tmp_path)

    assert preview.draft.template == "node-playwright"
    assert "npm run test:e2e" in [
        command.command for command in preview.draft.profile.phases.validate_commands
    ]
    assert "browser" in preview.diagnostics.missing_services
    assert "app" in preview.diagnostics.missing_ports
    assert "app" in preview.diagnostics.missing_healthchecks


@pytest.mark.unit
def test_detects_multi_service_template_when_compose_has_multiple_services(
    tmp_path: Path,
) -> None:
    (tmp_path / "compose.yaml").write_text(
        "services:\n"
        "  api:\n"
        "    image: example/api\n"
        "    ports:\n"
        '      - "8000:8000"\n'
        "  worker:\n"
        "    image: example/worker\n"
        "    environment:\n"
        "      WORKER_TOKEN: ${WORKER_TOKEN}\n",
        encoding="utf-8",
    )

    preview = preview_project_onboarding(tmp_path)

    assert preview.draft.template == "multi-service"
    assert preview.draft.profile.docker.mode == DockerMode.dind
    assert preview.draft.profile.docker.compose_files == ["compose.yaml"]
    assert "api" in preview.diagnostics.missing_healthchecks
    assert "worker" in preview.diagnostics.missing_healthchecks
    assert "worker" in preview.diagnostics.missing_ports
    assert "WORKER_TOKEN" in preview.diagnostics.missing_secrets


@pytest.mark.unit
def test_preview_reports_missing_sections(tmp_path: Path) -> None:
    preview = preview_project_onboarding(tmp_path)
    payload = preview.to_dict()

    assert set(payload["diagnostics"]) == {
        "missing_services",
        "missing_secrets",
        "missing_ports",
        "missing_validation_commands",
        "missing_healthchecks",
    }
    assert isinstance(preview.diagnostics.missing_services, list)
    assert isinstance(preview.diagnostics.missing_secrets, list)
    assert isinstance(preview.diagnostics.missing_ports, list)
    assert isinstance(preview.diagnostics.missing_validation_commands, list)
    assert isinstance(preview.diagnostics.missing_healthchecks, list)


@pytest.mark.unit
def test_draft_yaml_round_trips_through_workspace_profile_schema(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    preview = preview_project_onboarding(tmp_path)
    raw = yaml.safe_load(preview.draft.yaml)

    assert set(raw) == {"awf"}
    parsed = WorkspaceProfile.model_validate(raw["awf"])
    assert parsed.name == preview.draft.profile.name
    assert parsed.phases.validate_commands[0].command == "pytest -q"


@pytest.mark.unit
def test_smoke_request_shape_is_optional_and_does_not_launch(tmp_path: Path) -> None:
    preview = preview_project_onboarding(tmp_path)
    with_smoke = preview_project_onboarding(tmp_path, include_smoke_request=True)

    assert preview.smoke_request is None
    assert with_smoke.smoke_request is not None
    assert with_smoke.smoke_request["repo"]["url"].startswith("file://")
    assert with_smoke.smoke_request["task"]["agent"] == "codex"
    assert with_smoke.smoke_request["task"]["auto_merge"] is False
    assert with_smoke.smoke_request["workspace"]["profile"] == (
        with_smoke.draft.profile.model_dump(mode="json", by_alias=True, exclude_none=True)
    )
    assert with_smoke.smoke_request["validation"]["commands"] == [
        command.command for command in with_smoke.draft.profile.phases.validate_commands
    ]
