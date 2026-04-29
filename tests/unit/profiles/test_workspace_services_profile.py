"""Fixture-backed profile tests for workspace-local sidecar services."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.profiles.compose import profile_services
from awf.profiles.models import DockerMode, WorkspaceProfile
from awf.profiles.resolver import ProfileResolver

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "workspace_services" / "dockerized_app"
_POSTGRES_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "workspace_services"
    / "python_postgres_app"
)


def _load_profile() -> WorkspaceProfile:
    assert _FIXTURE.is_dir(), "workspace-services fixture is missing"
    result = ProfileResolver().resolve(worktree_path=_FIXTURE, profile_ref="auto")

    assert result.reason == "repo-local .awf/workspace.yml profile"
    assert result.candidates_considered[0] == "repo:.awf/workspace.yml"
    assert result.profile.source == "repo:.awf/workspace.yml"
    return result.profile


def _load_postgres_profile() -> WorkspaceProfile:
    assert _POSTGRES_FIXTURE.is_dir(), "python-postgres workspace-services fixture is missing"
    result = ProfileResolver().resolve(worktree_path=_POSTGRES_FIXTURE, profile_ref="auto")

    assert result.reason == "repo-local .awf/workspace.yml profile"
    assert result.candidates_considered[0] == "repo:.awf/workspace.yml"
    assert result.profile.source == "repo:.awf/workspace.yml"
    return result.profile


@pytest.mark.unit
def test_dockerized_workspace_services_profile_resolves_repo_local_contract() -> None:
    profile = _load_profile()

    assert profile.name == "dockerized-app-sidecar"
    assert profile.docker.mode is DockerMode.none
    assert profile.runtime.environment == {
        "APP_BASE_URL": "http://app:8080",
        "CACHE_URL": "redis://redis:6379/0",
    }
    assert profile.ports == {
        "app": "http://app:8080",
        "redis": "redis://redis:6379/0",
    }


@pytest.mark.unit
def test_dockerized_workspace_services_profile_preserves_service_schema() -> None:
    profile = _load_profile()

    services = {service.name: service for service in profile.services}
    assert set(services) == {"app", "redis"}

    redis = services["redis"]
    assert redis.image == "redis:7-alpine"
    assert redis.environment == {"REDIS_PORT": "6379"}
    assert redis.healthcheck_cmd == "redis-cli ping"
    assert redis.ports == [(6379, 16379)]
    assert redis.depends_on == []

    app = services["app"]
    assert app.build_context == "."
    assert app.dockerfile == "Dockerfile"
    assert app.env_file == "app.env"
    assert app.environment == {
        "CACHE_URL": "redis://redis:6379/0",
        "PORT": "8080",
    }
    assert app.depends_on == ["redis"]
    assert app.healthcheck_cmd == "wget -qO- http://127.0.0.1:8080/healthz >/dev/null"
    assert app.ports == [(8080, 18080)]
    assert app.command == "python /app/app.py"


@pytest.mark.unit
def test_profile_services_adapter_preserves_renderer_fields() -> None:
    profile = _load_profile()

    services = {service.name: service for service in profile_services(profile)}

    redis = services["redis"]
    assert redis.image == "redis:7-alpine"
    assert redis.environment == (("REDIS_PORT", "6379"),)
    assert redis.healthcheck_cmd == "redis-cli ping"
    assert redis.ports == ((6379, 16379),)

    app = services["app"]
    assert app.build_context == "."
    assert app.dockerfile == "Dockerfile"
    assert app.env_file == "app.env"
    assert app.environment == (
        ("CACHE_URL", "redis://redis:6379/0"),
        ("PORT", "8080"),
    )
    assert app.depends_on == ("redis",)
    assert app.healthcheck_cmd == "wget -qO- http://127.0.0.1:8080/healthz >/dev/null"
    assert app.ports == ((8080, 18080),)
    assert app.command == "python /app/app.py"


@pytest.mark.unit
def test_profile_services_rejects_absolute_repo_local_paths(tmp_path: Path) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "bad-path",
            "services": [
                {
                    "name": "api",
                    "build_context": str(tmp_path / "api"),
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="workspace-relative"):
        profile_services(profile, base_path=tmp_path / "repo")


@pytest.mark.unit
def test_profile_services_rejects_paths_that_escape_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    profile = WorkspaceProfile.model_validate(
        {
            "name": "bad-path",
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "env_file": "../secrets.env",
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="escapes workspace root"):
        profile_services(profile, base_path=repo)


@pytest.mark.unit
def test_python_postgres_workspace_services_profile_resolves_repo_local_contract() -> None:
    profile = _load_postgres_profile()

    assert profile.name == "python-postgres-app"
    assert profile.docker.mode is DockerMode.none
    assert profile.runtime.environment == {
        "APP_BASE_URL": "http://app:8080",
        "DATABASE_URL": "postgresql://awf:${AWF_POSTGRES_PASSWORD}@postgres:5432/awf",
    }
    assert profile.ports == {
        "app": "http://app:8080",
        "postgres": "postgresql://awf:${AWF_POSTGRES_PASSWORD}@postgres:5432/awf",
    }


@pytest.mark.unit
def test_python_postgres_workspace_services_profile_preserves_service_schema() -> None:
    profile = _load_postgres_profile()

    services = {service.name: service for service in profile.services}
    assert set(services) == {"app", "postgres"}

    postgres = services["postgres"]
    assert postgres.image == "postgres:16-alpine"
    assert postgres.environment == {
        "POSTGRES_DB": "awf",
        "POSTGRES_PASSWORD": "${AWF_POSTGRES_PASSWORD}",
        "POSTGRES_USER": "awf",
    }
    assert postgres.healthcheck_cmd == "pg_isready -U awf -d awf"
    assert postgres.volumes == [("postgres_data", "/var/lib/postgresql/data")]
    assert postgres.ports == []
    assert postgres.depends_on == []

    app = services["app"]
    assert app.build_context == "."
    assert app.dockerfile == "Dockerfile"
    assert app.environment == {
        "DATABASE_URL": "postgresql://awf:${AWF_POSTGRES_PASSWORD}@postgres:5432/awf",
        "PORT": "8080",
    }
    assert app.depends_on == ["postgres"]
    assert app.healthcheck_cmd == (
        "python -c \"import urllib.request; "
        "urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).read()\""
    )
    assert app.command == "python /app/app.py"
    assert app.ports == []

    assert [command.command for command in profile.phases.setup] == [
        (
            "python -c \"import os, urllib.request; "
            "body=urllib.request.urlopen(os.environ['APP_BASE_URL'] + '/setup', timeout=10)"
            ".read().decode(); assert body == 'setup ok\\n', body; print(body, end='')\""
        )
    ]
    assert [command.command for command in profile.phases.validate_commands] == [
        (
            "python -c \"import os, urllib.request; "
            "body=urllib.request.urlopen(os.environ['APP_BASE_URL'] + '/validate', timeout=10)"
            ".read().decode(); assert body == 'validated awf-db-profile-fixture\\n', body; "
            "print(body, end='')\""
        )
    ]
    assert [(check.name, check.command) for check in profile.validation.healthchecks] == [
        (
            "app",
            (
                "python -c \"import os, urllib.request; "
                "body=urllib.request.urlopen(os.environ['APP_BASE_URL'] + '/healthz', "
                "timeout=10).read().decode(); assert body == 'ok\\n', body; "
                "print(body, end='')\""
            ),
        )
    ]


@pytest.mark.unit
def test_python_postgres_profile_services_resolves_worktree_paths_and_named_volume() -> None:
    profile = _load_postgres_profile()

    services = {
        service.name: service for service in profile_services(profile, base_path=_POSTGRES_FIXTURE)
    }

    postgres = services["postgres"]
    assert postgres.image == "postgres:16-alpine"
    assert postgres.environment == (
        ("POSTGRES_DB", "awf"),
        ("POSTGRES_PASSWORD", "${AWF_POSTGRES_PASSWORD}"),
        ("POSTGRES_USER", "awf"),
    )
    assert postgres.healthcheck_cmd == "pg_isready -U awf -d awf"
    assert postgres.volumes == (("postgres_data", "/var/lib/postgresql/data"),)

    app = services["app"]
    assert app.build_context == str(_POSTGRES_FIXTURE.resolve())
    assert app.dockerfile == "Dockerfile"
    assert app.environment == (
        ("DATABASE_URL", "postgresql://awf:${AWF_POSTGRES_PASSWORD}@postgres:5432/awf"),
        ("PORT", "8080"),
    )
    assert app.depends_on == ("postgres",)
    assert app.volumes == ()


@pytest.mark.unit
def test_profile_services_rejects_symlink_escape_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    external = tmp_path / "external"
    repo.mkdir()
    external.mkdir()
    (repo / "linked").symlink_to(external, target_is_directory=True)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "bad-path",
            "services": [
                {
                    "name": "api",
                    "build_context": "linked/api",
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="escapes workspace root"):
        profile_services(profile, base_path=repo)


@pytest.mark.unit
def test_profile_services_rejects_absolute_volume_sources(tmp_path: Path) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "bad-path",
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [(str(tmp_path / "host-secrets"), "/run/secrets")],
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="workspace-relative"):
        profile_services(profile, base_path=tmp_path / "repo")


@pytest.mark.unit
def test_profile_services_rejects_volume_sources_that_escape_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    profile = WorkspaceProfile.model_validate(
        {
            "name": "bad-path",
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [("../host-secrets", "/run/secrets")],
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="escapes workspace root"):
        profile_services(profile, base_path=repo)


@pytest.mark.unit
def test_profile_services_rejects_symlink_escape_volume_sources(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    external = tmp_path / "external"
    repo.mkdir()
    external.mkdir()
    (repo / "linked").symlink_to(external, target_is_directory=True)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "bad-path",
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [("linked/host-secrets", "/run/secrets")],
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="escapes workspace root"):
        profile_services(profile, base_path=repo)


@pytest.mark.unit
def test_profile_services_rejects_host_home_auth_mounts_with_reason_code(
    tmp_path: Path,
) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "bad-host-home-auth",
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [("${HOME}", "/home/agent")],
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="HOST_HOME_AUTH_MOUNT_TOO_BROAD") as exc_info:
        profile_services(profile, base_path=tmp_path / "repo")

    assert exc_info.value.reason_code == "HOST_HOME_AUTH_MOUNT_TOO_BROAD"


@pytest.mark.unit
def test_profile_services_rejects_broad_host_home_auth_mounts_under_warn_policy(
    tmp_path: Path,
) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "bad-host-home-auth-warn",
            "security": {"host_home_auth_mounts": {"mode": "warn"}},
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [("${HOME}", "/home/agent")],
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="HOST_HOME_AUTH_MOUNT_TOO_BROAD") as exc_info:
        profile_services(profile, base_path=tmp_path / "repo")

    assert exc_info.value.reason_code == "HOST_HOME_AUTH_MOUNT_TOO_BROAD"
