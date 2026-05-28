"""Focused tests for managed companion service conversion helpers."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from awf.node.companion_services import (
    COMPANION_ENV_SECRET_SOURCE_MISSING,
    MaterializedCompanionService,
    WorkspaceCompanionSpec,
    companion_service_from_materialized,
    companion_specs_from_task_policy,
    validate_companion_service_graph,
)
from awf.node.compose_manager import ComposeService
from awf.node.git_manager import WorktreeLayout
from awf.profiles.models import DockerMode
from awf.profiles.resolver import ProfileResolutionError


def _layout(root: Path) -> WorktreeLayout:
    return WorktreeLayout(
        mirror_path=root.parent / "mirror.git",
        worktree_path=root,
        branch_name="awf/ws_parent/companion/backend",
    )


@pytest.mark.unit
def test_companion_specs_from_task_policy_normalizes_optional_values(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    (companion_root / "fixtures").mkdir(parents=True)
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "environment": "not-a-mapping",
                    "depends_on": ["db", 3],
                    "ports": [[8000, 18000]],
                    "volumes": [["cache-volume", "/cache"], ["./fixtures", "/fixtures"]],
                }
            ]
        }
    )[0]

    assert spec.environment == ()
    assert spec.depends_on == ("db",)
    assert spec.ports == ((8000, 18000),)

    service = companion_service_from_materialized(
        MaterializedCompanionService(spec=spec, layout=_layout(companion_root))
    )

    assert service.volumes == (
        ("cache-volume", "/cache"),
        (str(companion_root / "fixtures"), "/fixtures"),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "companion",
    [
        {
            "name": "backend",
            "repo_url": "git@example.com:api.git",
        },
        {
            "name": "backend",
            "repo_url": "git@example.com:api.git",
            "base_branch": None,
        },
    ],
)
def test_companion_specs_from_task_policy_preserves_omitted_base_branch(
    companion: dict[str, object],
) -> None:
    spec = companion_specs_from_task_policy({"companions": [companion]})[0]

    assert spec.base_branch is None


@pytest.mark.unit
def test_companion_specs_from_task_policy_stringifies_environment_values() -> None:
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "environment": {"DEBUG": True, "PORT": 8000},
                }
            ]
        }
    )[0]

    assert spec.environment == (("DEBUG", "True"), ("PORT", "8000"))


@pytest.mark.unit
def test_companion_specs_from_task_policy_preserves_environment_secret_references() -> None:
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "environment_secrets": {
                        "AIRA_API_KEY": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "ANTHROPIC_API_KEY",
                        },
                        "OPTIONAL_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "OPTIONAL_TOKEN_SOURCE",
                            "required": False,
                        },
                    },
                }
            ]
        }
    )[0]

    assert [item.target for item in spec.environment_secrets] == [
        "AIRA_API_KEY",
        "OPTIONAL_TOKEN",
    ]
    assert spec.environment_secrets[0].value_from == "ANTHROPIC_API_KEY"
    assert spec.environment_secrets[0].required is True
    assert spec.environment_secrets[1].required is False


@pytest.mark.unit
def test_companion_specs_from_task_policy_rejects_non_mapping_environment_secret_ref() -> None:
    with pytest.raises(
        ValueError,
        match="companion environment_secrets entry for AIRA_API_KEY must be a mapping",
    ):
        companion_specs_from_task_policy(
            {
                "companions": [
                    {
                        "name": "backend",
                        "repo_url": "git@example.com:api.git",
                        "base_branch": "main",
                        "environment_secrets": {
                            "AIRA_API_KEY": "ANTHROPIC_API_KEY",
                        },
                    }
                ]
            }
        )


@pytest.mark.unit
def test_companion_specs_from_task_policy_rejects_invalid_environment_secret_target() -> None:
    with pytest.raises(
        ValueError,
        match="companion environment secret keys must be Docker-compatible",
    ):
        companion_specs_from_task_policy(
            {
                "companions": [
                    {
                        "name": "backend",
                        "repo_url": "git@example.com:api.git",
                        "base_branch": "main",
                        "environment_secrets": {
                            "BAD-NAME": {
                                "provider": "env",
                                "kind": "env",
                                "value_from": "ANTHROPIC_API_KEY",
                            },
                        },
                    }
                ]
            }
        )


@pytest.mark.unit
def test_companion_specs_from_task_policy_rejects_invalid_environment_secret_value_from() -> None:
    with pytest.raises(
        ValueError,
        match=(
            "companion environment_secrets value_from for 'AIRA_API_KEY' "
            "must be a Docker-compatible"
        ),
    ):
        companion_specs_from_task_policy(
            {
                "companions": [
                    {
                        "name": "backend",
                        "repo_url": "git@example.com:api.git",
                        "base_branch": "main",
                        "environment_secrets": {
                            "AIRA_API_KEY": {
                                "provider": "env",
                                "kind": "env",
                                "value_from": "ANTHROPIC_API_KEY}",
                            },
                        },
                    }
                ]
            }
        )


@pytest.mark.unit
def test_companion_specs_from_task_policy_preserves_compose_up_timeout() -> None:
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "compose_up_timeout_seconds": 900,
                }
            ]
        }
    )[0]

    assert spec.compose_up_timeout_seconds == 900


@pytest.mark.unit
@pytest.mark.parametrize(
    ("timeout", "expected"),
    [
        (-12, 1),
        (0, 1),
        (1801, 1800),
        (86400, 1800),
        (0.0, 1),
        (1801.0, 1800),
        ("86400", 1800),
    ],
)
def test_companion_specs_from_task_policy_clamps_compose_up_timeout(
    timeout: object,
    expected: int,
) -> None:
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "compose_up_timeout_seconds": timeout,
                }
            ]
        }
    )[0]

    assert spec.compose_up_timeout_seconds == expected


@pytest.mark.unit
def test_companion_specs_from_task_policy_coerces_integral_float_compose_up_timeout() -> None:
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "compose_up_timeout_seconds": 900.0,
                }
            ]
        }
    )[0]

    assert spec.compose_up_timeout_seconds == 900


@pytest.mark.unit
def test_companion_specs_from_task_policy_warns_on_fractional_compose_up_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="awf.node.companion_services")

    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "compose_up_timeout_seconds": 900.5,
                }
            ]
        }
    )[0]

    assert spec.compose_up_timeout_seconds is None
    assert "compose_up_timeout_seconds" in caplog.text
    assert "900.5" in caplog.text


@pytest.mark.unit
def test_companion_specs_from_task_policy_warns_on_boolean_compose_up_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="awf.node.companion_services")

    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "compose_up_timeout_seconds": True,
                }
            ]
        }
    )[0]

    assert spec.compose_up_timeout_seconds is None
    assert "compose_up_timeout_seconds" in caplog.text
    assert "boolean" in caplog.text


@pytest.mark.unit
def test_companion_service_from_materialized_resolves_environment_secret_placeholders(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "environment": {"APP_ENV": "test"},
                    "environment_secrets": {
                        "AIRA_API_KEY": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "ANTHROPIC_API_KEY",
                        }
                    },
                }
            ]
        }
    )[0]

    service = companion_service_from_materialized(
        MaterializedCompanionService(spec=spec, layout=_layout(companion_root)),
        host_env={"ANTHROPIC_API_KEY": "raw-secret-value"},
    )

    assert service.environment == (
        ("APP_ENV", "test"),
        (
            "AIRA_API_KEY",
            "${ANTHROPIC_API_KEY?COMPANION_ENV_SECRET_SOURCE_MISSING: "
            "companion=backend, target=AIRA_API_KEY, provider=env, "
            "source=ANTHROPIC_API_KEY}",
        ),
    )
    assert service.secret_metadata["env_secret_count"] == 1
    assert service.secret_metadata["env_secrets"] == (
        {
            "companion": "backend",
            "target": "AIRA_API_KEY",
            "provider": "env",
            "source": "ANTHROPIC_API_KEY",
            "required": True,
        },
    )
    assert "raw-secret-value" not in repr(service)


@pytest.mark.unit
def test_companion_service_from_materialized_accepts_empty_environment_secret_value(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "environment_secrets": {
                        "AIRA_API_KEY": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "ANTHROPIC_API_KEY",
                        }
                    },
                }
            ]
        }
    )[0]

    service = companion_service_from_materialized(
        MaterializedCompanionService(spec=spec, layout=_layout(companion_root)),
        host_env={"ANTHROPIC_API_KEY": ""},
    )

    assert service.environment == (
        (
            "AIRA_API_KEY",
            "${ANTHROPIC_API_KEY?COMPANION_ENV_SECRET_SOURCE_MISSING: "
            "companion=backend, target=AIRA_API_KEY, provider=env, "
            "source=ANTHROPIC_API_KEY}",
        ),
    )
    assert service.secret_metadata["env_secret_count"] == 1


@pytest.mark.unit
def test_companion_service_from_materialized_preserves_optional_present_secret_ref(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "environment_secrets": {
                        "OPTIONAL_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "OPTIONAL_TOKEN_SOURCE",
                            "required": False,
                        }
                    },
                }
            ]
        }
    )[0]

    service = companion_service_from_materialized(
        MaterializedCompanionService(spec=spec, layout=_layout(companion_root)),
        host_env={"OPTIONAL_TOKEN_SOURCE": "raw-optional-secret"},
    )

    assert service.environment == (("OPTIONAL_TOKEN", "${OPTIONAL_TOKEN_SOURCE:-}"),)
    assert service.secret_metadata["env_secret_count"] == 1
    assert "raw-optional-secret" not in repr(service)


@pytest.mark.unit
def test_companion_service_from_materialized_preserves_optional_empty_secret_ref(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "environment_secrets": {
                        "OPTIONAL_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "OPTIONAL_TOKEN_SOURCE",
                            "required": False,
                        }
                    },
                }
            ]
        }
    )[0]

    service = companion_service_from_materialized(
        MaterializedCompanionService(spec=spec, layout=_layout(companion_root)),
        host_env={"OPTIONAL_TOKEN_SOURCE": ""},
    )

    assert service.environment == (("OPTIONAL_TOKEN", "${OPTIONAL_TOKEN_SOURCE:-}"),)
    assert service.secret_metadata["env_secret_count"] == 1
    assert "omitted_optional_env_secret_count" not in service.secret_metadata


@pytest.mark.unit
def test_companion_service_from_materialized_omits_optional_missing_environment_secret(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "environment_secrets": {
                        "OPTIONAL_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "OPTIONAL_TOKEN_SOURCE",
                            "required": False,
                        }
                    },
                }
            ]
        }
    )[0]

    service = companion_service_from_materialized(
        MaterializedCompanionService(spec=spec, layout=_layout(companion_root)),
        host_env={},
    )

    assert service.environment == ()
    assert service.secret_metadata["env_secret_count"] == 0
    assert service.secret_metadata["omitted_optional_env_secret_count"] == 1
    assert service.secret_metadata["omitted_optional_env_secrets"] == (
        {
            "companion": "backend",
            "target": "OPTIONAL_TOKEN",
            "provider": "env",
            "source": "OPTIONAL_TOKEN_SOURCE",
            "required": False,
        },
    )


@pytest.mark.unit
def test_companion_service_from_materialized_fails_required_missing_environment_secret(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "environment_secrets": {
                        "AIRA_API_KEY": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "ANTHROPIC_API_KEY",
                        }
                    },
                }
            ]
        }
    )[0]

    with pytest.raises(ProfileResolutionError) as exc:
        companion_service_from_materialized(
            MaterializedCompanionService(spec=spec, layout=_layout(companion_root)),
            host_env={},
        )

    assert exc.value.reason_code == COMPANION_ENV_SECRET_SOURCE_MISSING
    assert "backend" in str(exc.value)
    assert "AIRA_API_KEY" in str(exc.value)
    assert "ANTHROPIC_API_KEY" in str(exc.value)


@pytest.mark.unit
def test_companion_specs_from_task_policy_treats_null_optional_sequences_as_empty() -> None:
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "depends_on": None,
                    "ports": None,
                    "volumes": None,
                }
            ]
        }
    )[0]

    assert spec.depends_on == ()
    assert spec.ports == ()
    assert spec.volumes == ()


@pytest.mark.unit
def test_companion_specs_from_task_policy_treats_scalar_dependency_as_single_service() -> None:
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "depends_on": "db",
                }
            ]
        }
    )[0]

    assert spec.depends_on == ("db",)


@pytest.mark.unit
def test_companion_service_from_materialized_keeps_default_dockerfile_context_relative(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    (companion_root / "services" / "api").mkdir(parents=True)
    spec = WorkspaceCompanionSpec(
        name="backend",
        repo_url="git@example.com:api.git",
        base_branch="main",
        build_context="services/api",
    )

    service = companion_service_from_materialized(
        MaterializedCompanionService(spec=spec, layout=_layout(companion_root))
    )

    assert service.build_context == str(companion_root / "services" / "api")
    assert service.dockerfile == "Dockerfile"


@pytest.mark.unit
def test_companion_service_from_materialized_rewrites_explicit_repo_relative_dockerfile(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    (companion_root / "services" / "api").mkdir(parents=True)
    (companion_root / "docker").mkdir()
    spec = WorkspaceCompanionSpec(
        name="backend",
        repo_url="git@example.com:api.git",
        base_branch="main",
        build_context="services/api",
        dockerfile="docker/Dockerfile",
    )

    service = companion_service_from_materialized(
        MaterializedCompanionService(spec=spec, layout=_layout(companion_root))
    )

    assert service.build_context == str(companion_root / "services" / "api")
    assert service.dockerfile == "../../docker/Dockerfile"


@pytest.mark.unit
@pytest.mark.parametrize(
    "companion",
    [
        {
            "name": "backend",
            "repo_url": "git@example.com:api.git",
            "base_branch": "main",
            "ports": ["bad"],
        },
        {
            "name": "backend",
            "repo_url": "git@example.com:api.git",
            "base_branch": "main",
            "volumes": ["bad"],
        },
    ],
)
def test_companion_specs_from_task_policy_rejects_malformed_sequences(
    companion: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="two-item sequences"):
        companion_specs_from_task_policy({"companions": [companion]})


@pytest.mark.unit
def test_companion_service_from_materialized_rejects_escaping_paths(tmp_path: Path) -> None:
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    spec = WorkspaceCompanionSpec(
        name="backend",
        repo_url="git@example.com:api.git",
        base_branch="main",
        build_context="../outside",
    )

    with pytest.raises(ValueError, match="escapes managed worktree"):
        companion_service_from_materialized(
            MaterializedCompanionService(spec=spec, layout=_layout(companion_root))
        )


@pytest.mark.unit
def test_validate_companion_service_graph_rejects_duplicate_companion_names(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "backend-one"
    second_root = tmp_path / "backend-two"
    first_root.mkdir()
    second_root.mkdir()
    first = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@example.com:first.git",
            base_branch="main",
        ),
        layout=_layout(first_root),
    )
    second = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@example.com:second.git",
            base_branch="main",
        ),
        layout=_layout(second_root),
    )

    with pytest.raises(ProfileResolutionError) as exc:
        validate_companion_service_graph(
            profile_services=(),
            companions=(first, second),
            docker_mode=DockerMode.none,
        )

    assert exc.value.reason_code == "COMPANION_SERVICE_NAME_COLLISION"
    assert "duplicate companion service name: backend" in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("profile_services", "companions"),
    [
        (
            (),
            (
                WorkspaceCompanionSpec(
                    name="api",
                    repo_url="git@example.com:api.git",
                    base_branch="main",
                    ports=((8000, 18000), (8001, 18000)),
                ),
            ),
        ),
        (
            (),
            (
                WorkspaceCompanionSpec(
                    name="api",
                    repo_url="git@example.com:api.git",
                    base_branch="main",
                    ports=((8000, 18000),),
                ),
                WorkspaceCompanionSpec(
                    name="worker",
                    repo_url="git@example.com:worker.git",
                    base_branch="main",
                    ports=((9000, 18000),),
                ),
            ),
        ),
        (
            (
                ComposeService(
                    name="redis",
                    image="redis:7",
                    ports=((6379, 18000),),
                ),
            ),
            (
                WorkspaceCompanionSpec(
                    name="api",
                    repo_url="git@example.com:api.git",
                    base_branch="main",
                    ports=((8000, 18000),),
                ),
            ),
        ),
    ],
)
def test_validate_companion_service_graph_rejects_duplicate_host_ports(
    profile_services: tuple[ComposeService, ...],
    companions: tuple[WorkspaceCompanionSpec, ...],
) -> None:
    with pytest.raises(ProfileResolutionError) as exc:
        validate_companion_service_graph(
            profile_services=profile_services,
            companions=companions,
            docker_mode=DockerMode.none,
        )

    assert exc.value.reason_code == "COMPANION_SERVICE_HOST_PORT_COLLISION"
    assert "duplicate companion/profile host port 18000" in str(exc.value)


@pytest.mark.unit
def test_validate_companion_service_graph_rejects_unknown_dependencies(tmp_path: Path) -> None:
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@example.com:api.git",
            base_branch="main",
            depends_on=("missing-companion-target",),
        ),
        layout=_layout(companion_root),
    )

    with pytest.raises(ProfileResolutionError) as exc:
        validate_companion_service_graph(
            profile_services=(ComposeService(name="web", depends_on=("missing-profile-target",)),),
            companions=(companion,),
            docker_mode=DockerMode.none,
        )

    assert exc.value.reason_code == "COMPANION_SERVICE_DEPENDENCY_UNKNOWN"
    assert "backend->missing-companion-target" in str(exc.value)
    assert "web->missing-profile-target" in str(exc.value)


@pytest.mark.unit
def test_validate_companion_service_graph_rejects_unhealthy_dependency_targets(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    worker_root = tmp_path / "worker"
    companion_root.mkdir()
    worker_root.mkdir()
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@example.com:api.git",
            base_branch="main",
            depends_on=("agent", "web", "worker"),
        ),
        layout=_layout(companion_root),
    )
    worker = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="worker",
            repo_url="git@example.com:worker.git",
            base_branch="main",
        ),
        layout=_layout(worker_root),
    )

    with pytest.raises(ProfileResolutionError) as exc:
        validate_companion_service_graph(
            profile_services=(ComposeService(name="web", depends_on=("agent",)),),
            companions=(companion, worker),
            docker_mode=DockerMode.none,
        )

    assert exc.value.reason_code == "COMPANION_SERVICE_DEPENDENCY_UNHEALTHY"
    assert "backend->agent" in str(exc.value)
    assert "backend->web" in str(exc.value)
    assert "backend->worker" in str(exc.value)
    assert "web->agent" in str(exc.value)


@pytest.mark.unit
def test_validate_companion_service_graph_accepts_healthchecked_dependencies(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    worker_root = tmp_path / "worker"
    companion_root.mkdir()
    worker_root.mkdir()
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@example.com:api.git",
            base_branch="main",
            depends_on=("web", "worker"),
        ),
        layout=_layout(companion_root),
    )
    worker = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="worker",
            repo_url="git@example.com:worker.git",
            base_branch="main",
            healthcheck_cmd="curl -fsS http://localhost:8001/health",
        ),
        layout=_layout(worker_root),
    )

    validate_companion_service_graph(
        profile_services=(
            ComposeService(
                name="web",
                depends_on=("worker",),
                healthcheck_cmd="curl -fsS http://localhost:8000/health",
            ),
        ),
        companions=(companion, worker),
        docker_mode=DockerMode.none,
    )


@pytest.mark.unit
def test_validate_companion_service_graph_accepts_dind_docker_dependency(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@example.com:api.git",
            base_branch="main",
            depends_on=("docker",),
        ),
        layout=_layout(companion_root),
    )

    validate_companion_service_graph(
        profile_services=(),
        companions=(companion,),
        docker_mode=DockerMode.dind,
    )


@pytest.mark.unit
def test_validate_companion_service_graph_rejects_self_dependency(tmp_path: Path) -> None:
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@example.com:api.git",
            base_branch="main",
            depends_on=("backend",),
        ),
        layout=_layout(companion_root),
    )

    with pytest.raises(ProfileResolutionError) as exc:
        validate_companion_service_graph(
            profile_services=(),
            companions=(companion,),
            docker_mode=DockerMode.none,
        )

    assert exc.value.reason_code == "COMPANION_SERVICE_DEPENDENCY_CYCLE"
    assert "backend->backend" in str(exc.value)


@pytest.mark.unit
def test_validate_companion_service_graph_rejects_companion_dependency_cycle(
    tmp_path: Path,
) -> None:
    api_root = tmp_path / "api"
    worker_root = tmp_path / "worker"
    api_root.mkdir()
    worker_root.mkdir()
    api = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="api",
            repo_url="git@example.com:api.git",
            base_branch="main",
            depends_on=("worker",),
        ),
        layout=_layout(api_root),
    )
    worker = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="worker",
            repo_url="git@example.com:worker.git",
            base_branch="main",
            depends_on=("api",),
        ),
        layout=_layout(worker_root),
    )

    with pytest.raises(ProfileResolutionError) as exc:
        validate_companion_service_graph(
            profile_services=(),
            companions=(api, worker),
            docker_mode=DockerMode.none,
        )

    assert exc.value.reason_code == "COMPANION_SERVICE_DEPENDENCY_CYCLE"
    assert "api->worker->api" in str(exc.value)
