"""Project onboarding profile draft tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import awf.profiles.onboarding as onboarding_module
from awf.profiles.models import DockerMode, WorkspaceProfile
from awf.profiles.onboarding import (
    PreviewDiagnostics,
    ProjectInspection,
    _diagnostics_for,
    _profile_for_template,
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
def test_compose_web_port_scheme_ignores_non_numeric_ports() -> None:
    assert onboarding_module._compose_web_port_scheme("not-a-port") is None


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
def test_compose_diagnostics_are_silent_for_non_dind_profile(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n', encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  api:\n"
        "    image: example/api:latest\n"
        "    environment:\n"
        "      API_TOKEN: ${API_TOKEN}\n",
        encoding="utf-8",
    )

    preview = preview_project_onboarding(tmp_path, template="python")

    assert preview.inspection.compose_services
    assert preview.draft.template == "python"
    assert preview.draft.profile.docker.mode == DockerMode.none
    assert preview.diagnostics.missing_secrets == ()
    assert preview.diagnostics.missing_ports == ()
    assert preview.diagnostics.missing_healthchecks == ()


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
    assert profile.ports == {}
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
def test_compose_port_draft_omits_non_web_endpoints(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text(
        "services:\n"
        "  web:\n"
        "    image: example/web\n"
        "    ports:\n"
        '      - "18080:8080"\n'
        "  secure:\n"
        "    image: example/secure\n"
        "    ports:\n"
        '      - "443:443"\n'
        "  db:\n"
        "    image: postgres:16\n"
        "    ports:\n"
        '      - "5432:5432"\n',
        encoding="utf-8",
    )

    preview = preview_project_onboarding(tmp_path)

    assert preview.draft.profile.ports == {
        "web": "http://web:8080",
        "secure": "https://secure:443",
    }
    assert preview.diagnostics.missing_ports == ("db",)


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
    assert isinstance(preview.diagnostics.missing_services, tuple)
    assert isinstance(preview.diagnostics.missing_secrets, tuple)
    assert isinstance(preview.diagnostics.missing_ports, tuple)
    assert isinstance(preview.diagnostics.missing_validation_commands, tuple)
    assert isinstance(preview.diagnostics.missing_healthchecks, tuple)
    assert isinstance(payload["diagnostics"]["missing_services"], list)
    assert isinstance(payload["diagnostics"]["missing_validation_commands"], list)
    with pytest.raises(AttributeError):
        preview.diagnostics.missing_validation_commands.append("corrupt")  # type: ignore[attr-defined]


@pytest.mark.unit
def test_preview_diagnostics_normalizes_constructor_lists_to_tuples() -> None:
    missing_services = ["api"]

    diagnostics = PreviewDiagnostics(
        missing_services=missing_services,
        missing_secrets=[],
        missing_ports=[],
        missing_validation_commands=[],
        missing_healthchecks=[],
    )
    missing_services.append("worker")

    assert diagnostics.missing_services == ("api",)
    assert diagnostics.to_dict()["missing_services"] == ["api"]


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


@pytest.mark.unit
def test_inspection_rejects_missing_or_file_paths(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    file_path = tmp_path / "README.md"
    file_path.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(ValueError, match="does not exist"):
        inspect_project(missing)
    with pytest.raises(ValueError, match="not a directory"):
        inspect_project(file_path)


@pytest.mark.unit
def test_template_override_validation_errors_are_explicit(tmp_path: Path) -> None:
    inspection = inspect_project(tmp_path)

    with pytest.raises(ValueError, match="unsupported onboarding template: rails"):
        draft_workspace_profile(inspection, template="rails")
    with pytest.raises(ValueError, match="unsupported onboarding template: rails"):
        _profile_for_template(inspection, "rails")


@pytest.mark.unit
def test_python_postgres_without_alembic_keeps_setup_to_python_install(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\ndependencies = ["psycopg"]\n',
        encoding="utf-8",
    )

    preview = preview_project_onboarding(tmp_path)

    assert preview.draft.template == "python-postgres"
    assert [command.command for command in preview.draft.profile.phases.setup] == [
        'uv pip install -e ".[dev]"',
    ]


@pytest.mark.unit
def test_python_uv_lock_prefers_uv_sync(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")

    profile = draft_workspace_profile(inspect_project(tmp_path)).profile

    assert profile.name == "python"
    assert profile.phases.setup[0].command == "uv sync --extra dev"
    assert profile.phases.setup[0].timeout_seconds == 900


@pytest.mark.unit
def test_node_fallback_validation_and_install_variants(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15.0.0"}}),
        encoding="utf-8",
    )

    npm_preview = preview_project_onboarding(tmp_path)
    assert npm_preview.draft.template == "node-nextjs"
    assert npm_preview.draft.profile.phases.setup[0].command == "npm install"
    assert [command.command for command in npm_preview.draft.profile.phases.validate_commands] == [
        "npm run build",
    ]

    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    yarn_preview = preview_project_onboarding(tmp_path)
    assert yarn_preview.draft.profile.phases.setup[0].command == "yarn install --frozen-lockfile"
    assert [command.command for command in yarn_preview.draft.profile.phases.validate_commands] == [
        "yarn build",
    ]

    (tmp_path / "yarn.lock").unlink()
    (tmp_path / "bun.lock").write_text("", encoding="utf-8")
    bun_preview = preview_project_onboarding(tmp_path)
    assert bun_preview.draft.profile.phases.setup[0].command == "bun install --frozen-lockfile"
    assert [command.command for command in bun_preview.draft.profile.phases.validate_commands] == [
        "bun run build",
    ]


@pytest.mark.unit
def test_node_playwright_fallback_command_tracks_package_manager(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"@playwright/test": "latest"}}),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")

    pnpm_preview = preview_project_onboarding(tmp_path)
    assert pnpm_preview.draft.template == "node-playwright"
    assert [command.command for command in pnpm_preview.draft.profile.phases.validate_commands] == [
        "pnpm exec playwright test",
    ]

    (tmp_path / "pnpm-lock.yaml").unlink()
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    yarn_preview = preview_project_onboarding(tmp_path)
    assert [command.command for command in yarn_preview.draft.profile.phases.validate_commands] == [
        "yarn playwright test",
    ]

    (tmp_path / "yarn.lock").unlink()
    (tmp_path / "bun.lockb").write_text("", encoding="utf-8")
    bun_preview = preview_project_onboarding(tmp_path)
    assert [command.command for command in bun_preview.draft.profile.phases.validate_commands] == [
        "bunx playwright test",
    ]


@pytest.mark.unit
def test_playwright_script_detection_ignores_non_playwright_test_script(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"test": "vitest run"},
                "devDependencies": {"@playwright/test": "latest"},
            }
        ),
        encoding="utf-8",
    )

    preview = preview_project_onboarding(tmp_path)

    assert preview.draft.template == "node-playwright"
    assert [command.command for command in preview.draft.profile.phases.validate_commands] == [
        "npx playwright test",
    ]


@pytest.mark.unit
def test_multi_service_template_from_service_directories_without_compose(
    tmp_path: Path,
) -> None:
    (tmp_path / "apps").mkdir()
    (tmp_path / "services").mkdir()

    preview = preview_project_onboarding(tmp_path)

    assert preview.draft.template == "multi-service"
    assert preview.draft.profile.docker.compose_files == []
    assert preview.diagnostics.missing_services == ("apps", "services")


@pytest.mark.unit
def test_compose_parser_handles_long_form_ports_and_secret_shapes(
    tmp_path: Path,
) -> None:
    (tmp_path / "compose.yaml").write_text(
        "services:\n"
        "  web:\n"
        "    image: example/web\n"
        "    healthcheck:\n"
        "      test: ['CMD', 'true']\n"
        "    ports:\n"
        "      - target: 8080\n"
        "        published: 18080\n"
        "      - target: 9090\n"
        "      - name: ignored\n"
        "    environment:\n"
        "      API_TOKEN:\n"
        "      PASSWORD: ${PASSWORD}\n"
        "      PUBLIC_VALUE: visible\n"
        "    env_file: ${PRIVATE_KEY_FILE}\n"
        "    secrets:\n"
        "      - ${CREDENTIAL_FILE}\n"
        "      - 456\n"
        "  worker:\n"
        "    image: example/worker\n"
        "    healthcheck:\n"
        "      disable: true\n"
        "    ports:\n"
        "      - admin\n"
        "    environment:\n"
        "      - ACCESS_KEY\n"
        "      - SERVICE_TOKEN=${SERVICE_TOKEN}\n"
        "      - PUBLIC_VALUE=visible\n"
        "      - 123\n",
        encoding="utf-8",
    )

    preview = preview_project_onboarding(tmp_path)

    assert preview.draft.template == "multi-service"
    assert preview.draft.profile.ports == {"web": "http://web:8080"}
    assert preview.diagnostics.missing_secrets == (
        "ACCESS_KEY",
        "API_TOKEN",
        "CREDENTIAL_FILE",
        "PASSWORD",
        "PRIVATE_KEY_FILE",
        "SERVICE_TOKEN",
    )
    assert preview.diagnostics.missing_healthchecks == ("worker",)


@pytest.mark.unit
def test_compose_parser_ignores_unreadable_or_invalid_documents(tmp_path: Path) -> None:
    (tmp_path / "compose.yml").write_text("[", encoding="utf-8")
    invalid_yaml = preview_project_onboarding(tmp_path)
    assert invalid_yaml.draft.template == "docker-compose"
    assert invalid_yaml.inspection.compose_services == ()

    (tmp_path / "compose.yml").write_bytes(b"\xff")
    non_utf8 = preview_project_onboarding(tmp_path)
    assert non_utf8.draft.template == "docker-compose"
    assert non_utf8.inspection.compose_services == ()

    (tmp_path / "compose.yml").write_text("- just\n- a list\n", encoding="utf-8")
    non_mapping = preview_project_onboarding(tmp_path)
    assert non_mapping.inspection.compose_services == ()

    (tmp_path / "compose.yml").write_text("name: app\n", encoding="utf-8")
    no_services = preview_project_onboarding(tmp_path)
    assert no_services.inspection.compose_services == ()

    (tmp_path / "compose.yml").write_text(
        "services:\n"
        "  123:\n"
        "    image: ignored\n"
        "  api: example/api\n",
        encoding="utf-8",
    )
    mixed_services = preview_project_onboarding(tmp_path)
    assert [service.name for service in mixed_services.inspection.compose_services] == ["api"]


@pytest.mark.unit
def test_malformed_package_json_falls_back_to_generic(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{", encoding="utf-8")
    malformed = preview_project_onboarding(tmp_path)
    assert malformed.draft.template == "generic"

    (tmp_path / "package.json").write_bytes(b"\xff")
    non_utf8 = preview_project_onboarding(tmp_path)
    assert non_utf8.draft.template == "generic"

    (tmp_path / "package.json").write_text('["not", "an", "object"]', encoding="utf-8")
    non_object = preview_project_onboarding(tmp_path)
    assert non_object.draft.template == "generic"


@pytest.mark.unit
def test_postgres_signal_ignores_unreadable_file_and_uses_env_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "app"\n', encoding="utf-8")

    original_read_text = Path.read_text

    def flaky_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == pyproject:
            raise OSError("cannot read pyproject")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    python_preview = preview_project_onboarding(tmp_path)
    assert python_preview.draft.template == "python"

    (tmp_path / ".env.example").write_text("DATABASE_URL=postgres://example\n", encoding="utf-8")
    postgres_preview = preview_project_onboarding(tmp_path)
    assert postgres_preview.draft.template == "python-postgres"


@pytest.mark.unit
def test_postgres_signal_ignores_non_utf8_config_file(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_bytes(b"\xff")
    (tmp_path / ".env.example").write_text("DATABASE_URL=postgres://example\n", encoding="utf-8")

    preview = preview_project_onboarding(tmp_path)

    assert preview.draft.template == "python-postgres"


@pytest.mark.unit
def test_node_playwright_diagnostics_do_not_report_app_when_declared(
    tmp_path: Path,
) -> None:
    inspection = ProjectInspection(
        path=tmp_path,
        detected_template="node-playwright",
        confidence="medium",
        signals=("node", "playwright"),
        package_manager="npm",
        package_scripts={"test:e2e": "playwright test"},
    )
    profile = WorkspaceProfile(
        name="node-playwright",
        source="test",
        ports={"app": "http://app:3000"},
        validation={"healthchecks": [{"name": "app", "command": "curl http://app:3000"}]},
        phases={"validate": ["npm run test:e2e"]},
    )

    draft = draft_workspace_profile(inspection, template="node-playwright")
    diagnostics = draft.diagnostics
    assert "app" in diagnostics.missing_ports
    assert "app" in diagnostics.missing_healthchecks

    declared = _diagnostics_for(inspection, profile=profile, template="node-playwright")
    assert declared.missing_ports == ()
    assert declared.missing_healthchecks == ()
