"""Fixture-backed profile tests for workspace-local sidecar services."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.profiles.compose import profile_services
from awf.profiles.models import DockerMode, WorkspaceProfile
from awf.profiles.resolver import ProfileResolver

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "workspace_services" / "dockerized_app"


def _load_profile() -> WorkspaceProfile:
    assert _FIXTURE.is_dir(), "workspace-services fixture is missing"
    result = ProfileResolver().resolve(worktree_path=_FIXTURE, profile_ref="auto")

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
