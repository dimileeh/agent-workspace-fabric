"""Fixture-backed profile tests for workspace-local sidecar services."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.profiles.compose import profile_services
from awf.profiles.models import DockerMode, WorkspaceProfile
from awf.profiles.resolver import ProfileResolver

_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "workspace_services" / "dockerized_app"
)
_POSTGRES_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "workspace_services" / "python_postgres_app"
)
_NODE_BROWSER_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "workspace_services"
    / "node_next_browser_app"
)
_REDIS_WORKER_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "workspace_services" / "redis_worker_app"
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


def _load_node_browser_profile() -> WorkspaceProfile:
    assert _NODE_BROWSER_FIXTURE.is_dir(), "node browser workspace-services fixture is missing"
    result = ProfileResolver().resolve(
        worktree_path=_NODE_BROWSER_FIXTURE,
        profile_ref="auto",
    )

    assert result.reason == "repo-local .awf/workspace.yml profile"
    assert result.candidates_considered[0] == "repo:.awf/workspace.yml"
    assert result.profile.source == "repo:.awf/workspace.yml"
    return result.profile


def _load_redis_worker_profile() -> WorkspaceProfile:
    assert _REDIS_WORKER_FIXTURE.is_dir(), "redis-worker workspace-services fixture is missing"
    result = ProfileResolver().resolve(
        worktree_path=_REDIS_WORKER_FIXTURE,
        profile_ref="auto",
    )

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
        "POSTGRES_HOST_AUTH_METHOD": "trust",
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
        'python -c "import urllib.request; '
        "urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).read()\""
    )
    assert app.command == "python /app/app.py"
    assert app.ports == []

    assert [command.command for command in profile.phases.setup] == [
        (
            'python -c "import os, urllib.request; '
            "body=urllib.request.urlopen(os.environ['APP_BASE_URL'] + '/setup', timeout=10)"
            ".read().decode(); assert body == 'setup ok\\n', body; print(body, end='')\""
        )
    ]
    assert [command.command for command in profile.phases.validate_commands] == [
        (
            'python -c "import os, urllib.request; '
            "body=urllib.request.urlopen(os.environ['APP_BASE_URL'] + '/validate', timeout=10)"
            ".read().decode(); assert body == 'validated awf-db-profile-fixture\\n', body; "
            "print(body, end='')\""
        )
    ]
    assert [(check.name, check.command) for check in profile.validation.healthchecks] == [
        (
            "app",
            (
                'python -c "import os, urllib.request; '
                "body=urllib.request.urlopen(os.environ['APP_BASE_URL'] + '/healthz', "
                "timeout=10).read().decode(); assert body == 'ok\\n', body; "
                "print(body, end='')\""
            ),
        )
    ]


@pytest.mark.unit
def test_python_postgres_profile_declares_workspace_local_db_hooks() -> None:
    profile = _load_postgres_profile()
    rendered_profile = profile.model_dump_json()

    assert [service.name for service in profile.services] == ["postgres", "app"]
    assert profile.runtime.environment["DATABASE_URL"] == (
        "postgresql://awf:${AWF_POSTGRES_PASSWORD}@postgres:5432/awf"
    )
    assert [
        (command.command, command.timeout_seconds) for command in profile.database.generated_setup
    ] == [
        (
            (
                'python -c "import os, urllib.request; '
                "assert os.environ['DATABASE_URL'].startswith('postgresql://awf:'); "
                "body=urllib.request.urlopen(os.environ['APP_BASE_URL'] + '/setup', "
                "timeout=10).read().decode(); assert body == 'setup ok\\n', body; "
                "print('generated setup ok')\""
            ),
            30,
        )
    ]
    assert [
        (command.command, command.timeout_seconds)
        for command in profile.database.pre_validation_refresh
    ] == [
        (
            (
                'python -c "import os, urllib.request; '
                "assert os.environ['DATABASE_URL'].startswith('postgresql://awf:'); "
                "body=urllib.request.urlopen(os.environ['APP_BASE_URL'] + '/setup', "
                "timeout=10).read().decode(); assert body == 'setup ok\\n', body; "
                "print('refresh ok')\""
            ),
            30,
        )
    ]
    assert "DATABASE_URL" in rendered_profile
    assert "AWF_DATABASE_URL" not in rendered_profile
    assert "ServiceSettings" not in rendered_profile
    assert "localhost:5433" not in rendered_profile


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
        ("POSTGRES_HOST_AUTH_METHOD", "trust"),
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
def test_node_next_browser_workspace_services_profile_resolves_repo_local_contract() -> None:
    profile = _load_node_browser_profile()

    assert profile.name == "node-next-browser-app"
    assert profile.docker.mode is DockerMode.none
    assert profile.runtime.environment == {
        "APP_BASE_URL": "http://app:3000",
        "BROWSER_VALIDATE_URL": "http://browser:9323/validate",
    }
    assert profile.ports == {
        "app": "http://app:3000",
        "browser": "http://browser:9323/validate",
    }


@pytest.mark.unit
def test_node_next_browser_workspace_services_profile_declares_app_endpoints() -> None:
    profile = _load_node_browser_profile()

    endpoints = {endpoint.name: endpoint for endpoint in profile.app_endpoints}

    assert set(endpoints) == {"app", "browser_validation", "operator_notes"}

    app = endpoints["app"]
    assert app.service == "app"
    assert app.scheme == "http"
    assert app.port == 3000
    assert app.path == "/"
    assert app.visibility == "agent"
    assert app.health is not None
    assert app.health.path == "/healthz"
    assert app.health.method == "GET"
    assert app.health.expected_status == 200

    browser = endpoints["browser_validation"]
    assert browser.service == "browser"
    assert browser.port == 9323
    assert browser.path == "/validate"
    assert browser.visibility == "validation"

    console = endpoints["operator_notes"]
    assert console.service == "app"
    assert console.port == 3000
    assert console.path == "/operator"
    assert console.visibility == "console"
    assert console.health is None


@pytest.mark.unit
def test_node_next_browser_workspace_services_profile_preserves_service_schema() -> None:
    profile = _load_node_browser_profile()

    services = {service.name: service for service in profile.services}
    assert set(services) == {"app", "browser"}

    app = services["app"]
    assert app.build_context == "."
    assert app.dockerfile == "Dockerfile"
    assert app.environment == {"PORT": "3000"}
    assert app.depends_on == []
    assert app.healthcheck_cmd == (
        "node /app/scripts/container-healthcheck.mjs http://127.0.0.1:3000/healthz ok"
    )
    assert app.ports == []
    assert app.command == "node /app/server.mjs"

    browser = services["browser"]
    assert browser.build_context == "."
    assert browser.dockerfile == "Dockerfile.playwright"
    assert browser.environment == {
        "APP_BASE_URL": "http://app:3000",
        "PORT": "9323",
    }
    assert browser.depends_on == ["app"]
    assert browser.healthcheck_cmd == (
        "node /app/scripts/container-healthcheck.mjs http://127.0.0.1:9323/healthz ok"
    )
    assert browser.ports == []
    assert browser.command == "node /app/browser/validator-server.mjs"

    assert [command.command for command in profile.phases.setup] == ["node scripts/setup.mjs"]
    assert [command.command for command in profile.phases.validate_commands] == [
        "node scripts/validate-browser.mjs"
    ]
    assert [(check.name, check.command) for check in profile.validation.healthchecks] == [
        ("app", "node scripts/healthcheck.mjs app"),
        ("browser", "node scripts/healthcheck.mjs browser"),
    ]


@pytest.mark.unit
def test_node_next_browser_profile_services_resolves_worktree_paths_without_host_ports() -> None:
    profile = _load_node_browser_profile()

    services = {
        service.name: service
        for service in profile_services(profile, base_path=_NODE_BROWSER_FIXTURE)
    }

    app = services["app"]
    assert app.build_context == str(_NODE_BROWSER_FIXTURE.resolve())
    assert app.dockerfile == "Dockerfile"
    assert app.environment == (("PORT", "3000"),)
    assert app.depends_on == ()
    assert app.ports == ()
    assert app.volumes == ()

    browser = services["browser"]
    assert browser.build_context == str(_NODE_BROWSER_FIXTURE.resolve())
    assert browser.dockerfile == "Dockerfile.playwright"
    assert browser.environment == (
        ("APP_BASE_URL", "http://app:3000"),
        ("PORT", "9323"),
    )
    assert browser.depends_on == ("app",)
    assert browser.ports == ()
    assert browser.volumes == ()


@pytest.mark.unit
def test_redis_worker_workspace_services_profile_resolves_repo_local_contract() -> None:
    profile = _load_redis_worker_profile()

    assert profile.name == "redis-worker-app"
    assert profile.docker.mode is DockerMode.none
    assert profile.runtime.environment == {
        "APP_BASE_URL": "http://app:8080",
        "REDIS_URL": "redis://redis:6379/0",
        "WORKER_STATUS_URL": "http://app:8080/status",
    }
    assert profile.ports == {
        "app": "http://app:8080",
        "redis": "redis://redis:6379/0",
        "worker": "redis-worker://worker",
    }


@pytest.mark.unit
def test_redis_worker_workspace_services_profile_preserves_service_schema() -> None:
    profile = _load_redis_worker_profile()

    services = {service.name: service for service in profile.services}
    assert set(services) == {"redis", "app", "worker"}

    redis = services["redis"]
    assert redis.image == "redis:7-alpine"
    assert redis.environment == {}
    assert redis.healthcheck_cmd == "redis-cli ping"
    assert redis.volumes == [("redis_data", "/data")]
    assert redis.ports == []
    assert redis.depends_on == []

    app = services["app"]
    assert app.build_context == "."
    assert app.dockerfile == "Dockerfile"
    assert app.environment == {
        "PORT": "8080",
        "REDIS_URL": "redis://redis:6379/0",
    }
    assert app.depends_on == ["redis"]
    assert app.healthcheck_cmd == "python /app/scripts/container_healthcheck.py app"
    assert app.command == "python /app/app.py"
    assert app.ports == []

    worker = services["worker"]
    assert worker.build_context == "."
    assert worker.dockerfile == "Dockerfile"
    assert worker.environment == {
        "REDIS_URL": "redis://redis:6379/0",
        "WORKER_ID": "redis-worker-fixture",
    }
    assert worker.depends_on == ["redis"]
    assert worker.healthcheck_cmd == "python /app/scripts/container_healthcheck.py worker"
    assert worker.command == "python /app/worker.py"
    assert worker.ports == []

    assert "aira" not in profile.model_dump_json().lower()
    assert [(command.command, command.timeout_seconds) for command in profile.phases.setup] == [
        ("python scripts/setup.py", 30)
    ]
    assert [
        (command.command, command.timeout_seconds) for command in profile.phases.validate_commands
    ] == [("python scripts/validate.py", 30)]
    assert [
        (check.name, check.command, check.timeout_seconds, check.attempt_timeout_seconds)
        for check in profile.validation.healthchecks
    ] == [
        ("redis", "python scripts/healthcheck.py redis", 120.0, 15.0),
        ("app", "python scripts/healthcheck.py app", 120.0, 15.0),
        ("worker", "python scripts/healthcheck.py worker", 120.0, 15.0),
    ]


@pytest.mark.unit
def test_redis_worker_profile_services_resolves_worktree_paths_and_named_volume() -> None:
    profile = _load_redis_worker_profile()

    services = {
        service.name: service
        for service in profile_services(profile, base_path=_REDIS_WORKER_FIXTURE)
    }

    redis = services["redis"]
    assert redis.image == "redis:7-alpine"
    assert redis.healthcheck_cmd == "redis-cli ping"
    assert redis.volumes == (("redis_data", "/data"),)

    app = services["app"]
    assert app.build_context == str(_REDIS_WORKER_FIXTURE.resolve())
    assert app.dockerfile == "Dockerfile"
    assert app.environment == (
        ("PORT", "8080"),
        ("REDIS_URL", "redis://redis:6379/0"),
    )
    assert app.depends_on == ("redis",)
    assert app.volumes == ()

    worker = services["worker"]
    assert worker.build_context == str(_REDIS_WORKER_FIXTURE.resolve())
    assert worker.dockerfile == "Dockerfile"
    assert worker.environment == (
        ("REDIS_URL", "redis://redis:6379/0"),
        ("WORKER_ID", "redis-worker-fixture"),
    )
    assert worker.depends_on == ("redis",)
    assert worker.volumes == ()


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


@pytest.mark.unit
def test_app_endpoint_defaults_and_visibility_normalization() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "endpoint-defaults",
            "services": [{"name": "api", "image": "example/api:latest"}],
            "app_endpoints": [
                {
                    "name": "api",
                    "service": "api",
                    "port": 8000,
                    "visibility": "VALIDATION",
                }
            ],
        }
    )

    endpoint = profile.app_endpoints[0]
    assert endpoint.scheme == "http"
    assert endpoint.path == "/"
    assert endpoint.visibility == "validation"
    assert endpoint.health is None


@pytest.mark.unit
@pytest.mark.parametrize("visibility", ["public", "external", "browser"])
def test_app_endpoint_rejects_unsupported_visibility_values(visibility: str) -> None:
    with pytest.raises(ValueError, match="visibility"):
        WorkspaceProfile.model_validate(
            {
                "name": "bad-visibility",
                "services": [{"name": "api", "image": "example/api:latest"}],
                "app_endpoints": [
                    {
                        "name": "api",
                        "service": "api",
                        "port": 8000,
                        "path": "/",
                        "visibility": visibility,
                    }
                ],
            }
        )


@pytest.mark.unit
def test_app_endpoint_service_reference_must_match_profile_services() -> None:
    with pytest.raises(ValueError, match="unknown app endpoint service"):
        WorkspaceProfile.model_validate(
            {
                "name": "bad-service",
                "services": [{"name": "api", "image": "example/api:latest"}],
                "app_endpoints": [
                    {
                        "name": "missing",
                        "service": "worker",
                        "port": 8000,
                        "path": "/",
                    }
                ],
            }
        )


@pytest.mark.unit
def test_app_endpoint_names_must_be_unique() -> None:
    with pytest.raises(ValueError, match="duplicate app endpoint name"):
        WorkspaceProfile.model_validate(
            {
                "name": "duplicate-endpoints",
                "services": [{"name": "api", "image": "example/api:latest"}],
                "app_endpoints": [
                    {"name": "api", "service": "api", "port": 8000, "path": "/"},
                    {"name": "API", "service": "api", "port": 8001, "path": "/alt"},
                ],
            }
        )


@pytest.mark.unit
def test_app_endpoint_environment_names_must_be_unique() -> None:
    with pytest.raises(ValueError, match="duplicate app endpoint environment name"):
        WorkspaceProfile.model_validate(
            {
                "name": "duplicate-endpoint-env",
                "services": [{"name": "api", "image": "example/api:latest"}],
                "app_endpoints": [
                    {"name": "api-v1", "service": "api", "port": 8000, "path": "/"},
                    {"name": "api_v1", "service": "api", "port": 8001, "path": "/alt"},
                ],
            }
        )


@pytest.mark.unit
def test_app_endpoint_environment_names_must_not_be_empty() -> None:
    with pytest.raises(ValueError, match="app endpoint environment name cannot be empty"):
        WorkspaceProfile.model_validate(
            {
                "name": "empty-endpoint-env",
                "services": [{"name": "api", "image": "example/api:latest"}],
                "app_endpoints": [
                    {"name": "_-.", "service": "api", "port": 8000, "path": "/"},
                ],
            }
        )


@pytest.mark.unit
def test_app_endpoint_ports_must_be_tcp_port_numbers() -> None:
    with pytest.raises(ValueError, match="less than or equal to 65535"):
        WorkspaceProfile.model_validate(
            {
                "name": "bad-port",
                "services": [{"name": "api", "image": "example/api:latest"}],
                "app_endpoints": [
                    {"name": "api", "service": "api", "port": 70000, "path": "/"},
                ],
            }
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "healthz",
        " /healthz",
        "http://api:8000/healthz",
        "https://user:password@api:8000/healthz",
        "/healthz?token=secret",
        "/healthz#secret",
    ],
)
def test_app_endpoint_paths_must_be_secret_free_url_paths(path: str) -> None:
    with pytest.raises(ValueError, match="URL path"):
        WorkspaceProfile.model_validate(
            {
                "name": "bad-path",
                "services": [{"name": "api", "image": "example/api:latest"}],
                "app_endpoints": [
                    {"name": "api", "service": "api", "port": 8000, "path": path},
                ],
            }
        )


@pytest.mark.unit
def test_app_endpoint_health_paths_must_be_secret_free_url_paths() -> None:
    with pytest.raises(ValueError, match="health path must be a URL path"):
        WorkspaceProfile.model_validate(
            {
                "name": "bad-health-path",
                "services": [{"name": "api", "image": "example/api:latest"}],
                "app_endpoints": [
                    {
                        "name": "api",
                        "service": "api",
                        "port": 8000,
                        "path": "/",
                        "health": {"path": "http://api:8000/healthz?token=secret"},
                    },
                ],
            }
        )
