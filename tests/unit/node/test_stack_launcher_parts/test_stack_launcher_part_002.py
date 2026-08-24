"""Stack launcher tests: companion materialization and environment secrets."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.node import stack_launcher as stack_launcher_mod
from awf.node.companion_services import (
    CompanionEnvironmentSecretRef,
    MaterializedCompanionService,
    WorkspaceCompanionSpec,
)
from awf.node.git_manager import WorktreeLayout
from awf.node.stack_launcher import (
    ComposeStackLauncher,
    WorkspaceStackLaunchRequest,
)
from awf.profiles.models import (
    DockerMode,
    ProfileDocker,
    ProfileService,
    WorkspaceProfile,
)
from awf.profiles.resolver import ProfileResolutionError
from tests.unit.node.test_stack_launcher_parts._helpers import _layout, _RecordingCompose


@pytest.mark.unit
async def test_compose_stack_launcher_passes_materialized_companions_to_compose(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    (companion_root / "services" / "api").mkdir(parents=True)
    (companion_root / "config").mkdir()
    (companion_root / "fixtures").mkdir()
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    layout = _layout()
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@github.com:example/backend.git",
            base_branch="development",
            build_context="services/api",
            dockerfile="docker/Dockerfile",
            env_file="config/dev.env",
            environment=(("APP_ENV", "test"),),
            depends_on=("docker",),
            healthcheck_cmd="curl -fsS http://localhost:8000/health",
            ports=((8000, 18000),),
            command="python -m backend",
            volumes=(("./fixtures", "/fixtures"),),
        ),
        layout=WorktreeLayout(
            mirror_path=tmp_path / "backend.git",
            worktree_path=companion_root,
            branch_name="awf/ws_launcher/companion/backend",
        ),
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=layout,
            profile=WorkspaceProfile(
                name="serviceful",
                docker=ProfileDocker(mode=DockerMode.dind),
            ),
            companions=(companion,),
        )
    )

    rendered = compose.specs[0].companions[0]
    assert rendered.name == "backend"
    assert rendered.build_context == str(companion_root / "services" / "api")
    assert rendered.dockerfile == "../../docker/Dockerfile"
    assert rendered.env_file == str(companion_root / "config" / "dev.env")
    assert rendered.depends_on == ("docker",)
    assert rendered.healthcheck_cmd == "curl -fsS http://localhost:8000/health"
    assert rendered.ports == ((8000, 18000),)
    assert rendered.command == "python -m backend"
    assert rendered.volumes == ((str(companion_root / "fixtures"), "/fixtures"),)


@pytest.mark.unit
async def test_compose_stack_launcher_resolves_companion_environment_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "raw-secret-value")
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@github.com:example/backend.git",
            base_branch="development",
            environment_secrets=(
                CompanionEnvironmentSecretRef(
                    target="AIRA_API_KEY",
                    provider="env",
                    kind="env",
                    value_from="ANTHROPIC_API_KEY",
                ),
            ),
        ),
        layout=WorktreeLayout(
            mirror_path=tmp_path / "backend.git",
            worktree_path=companion_root,
            branch_name="awf/ws_launcher/companion/backend",
        ),
    )

    paths = await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(
                name="serviceful",
                docker=ProfileDocker(mode=DockerMode.dind),
            ),
            companions=(companion,),
        )
    )

    rendered = compose.specs[0].companions[0]
    assert rendered.environment == (
        (
            "AIRA_API_KEY",
            "${ANTHROPIC_API_KEY:?COMPANION_ENV_SECRET_SOURCE_MISSING_OR_"
            "COMPANION_ENV_SECRET_SOURCE_EMPTY: "
            "companion=backend, target=AIRA_API_KEY, provider=env, "
            "source=ANTHROPIC_API_KEY}",
        ),
    )
    assert "raw-secret-value" not in repr(rendered)
    assert paths.secret_lease_mount_metadata["companion_env_secret_count"] == 1
    assert paths.secret_lease_mount_metadata["companion_env_secrets"] == (
        {
            "companion": "backend",
            "target": "AIRA_API_KEY",
            "provider": "env",
            "source": "ANTHROPIC_API_KEY",
            "required": True,
        },
    )


@pytest.mark.unit
async def test_compose_stack_launcher_omits_optional_missing_companion_environment_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("OPTIONAL_TOKEN_SOURCE", raising=False)
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@github.com:example/backend.git",
            base_branch="development",
            environment_secrets=(
                CompanionEnvironmentSecretRef(
                    target="OPTIONAL_TOKEN",
                    provider="env",
                    kind="env",
                    value_from="OPTIONAL_TOKEN_SOURCE",
                    required=False,
                ),
            ),
        ),
        layout=WorktreeLayout(
            mirror_path=tmp_path / "backend.git",
            worktree_path=companion_root,
            branch_name="awf/ws_launcher/companion/backend",
        ),
    )

    paths = await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(name="serviceful"),
            companions=(companion,),
            companion_graph_prevalidated=True,
        )
    )

    assert compose.specs[0].companions[0].environment == ()
    assert paths.secret_lease_mount_metadata["companion_env_secret_count"] == 0
    assert paths.secret_lease_mount_metadata["companion_omitted_optional_env_secret_count"] == 1


@pytest.mark.unit
async def test_compose_stack_launcher_fails_required_missing_companion_environment_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@github.com:example/backend.git",
            base_branch="development",
            environment_secrets=(
                CompanionEnvironmentSecretRef(
                    target="AIRA_API_KEY",
                    provider="env",
                    kind="env",
                    value_from="ANTHROPIC_API_KEY",
                ),
            ),
        ),
        layout=WorktreeLayout(
            mirror_path=tmp_path / "backend.git",
            worktree_path=companion_root,
            branch_name="awf/ws_launcher/companion/backend",
        ),
    )

    with pytest.raises(ProfileResolutionError) as exc:
        await launcher.launch(
            WorkspaceStackLaunchRequest(
                workspace_id="ws_launcher",
                layout=_layout(),
                profile=WorkspaceProfile(name="serviceful"),
                companions=(companion,),
                companion_graph_prevalidated=True,
            )
        )

    assert exc.value.reason_code == "COMPANION_ENV_SECRET_SOURCE_MISSING"
    assert compose.specs == []


@pytest.mark.unit
async def test_compose_stack_launcher_skips_companion_graph_validation_when_prevalidated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    companion_root.mkdir()

    def fail_validate_companion_service_graph(**_: object) -> None:
        raise AssertionError("prevalidated companion graph should not be validated again")

    monkeypatch.setattr(
        stack_launcher_mod,
        "validate_companion_service_graph",
        fail_validate_companion_service_graph,
    )
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(name="serviceful"),
            companions=(
                MaterializedCompanionService(
                    spec=WorkspaceCompanionSpec(
                        name="backend",
                        repo_url="git@github.com:example/backend.git",
                        base_branch="development",
                    ),
                    layout=WorktreeLayout(
                        mirror_path=tmp_path / "backend.git",
                        worktree_path=companion_root,
                        branch_name="awf/ws_launcher/companion/backend",
                    ),
                ),
            ),
            companion_graph_prevalidated=True,
        )
    )

    assert compose.specs[0].companions[0].name == "backend"


@pytest.mark.unit
async def test_compose_stack_launcher_rejects_companion_profile_service_collision(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )

    with pytest.raises(ProfileResolutionError) as raised:
        await launcher.launch(
            WorkspaceStackLaunchRequest(
                workspace_id="ws_launcher",
                layout=_layout(),
                profile=WorkspaceProfile(
                    name="serviceful",
                    services=[ProfileService(name="backend", image="backend:latest")],
                ),
                companions=(
                    MaterializedCompanionService(
                        spec=WorkspaceCompanionSpec(
                            name="backend",
                            repo_url="git@github.com:example/backend.git",
                            base_branch="development",
                        ),
                        layout=WorktreeLayout(
                            mirror_path=tmp_path / "backend.git",
                            worktree_path=companion_root,
                            branch_name="awf/ws_launcher/companion/backend",
                        ),
                    ),
                ),
            )
        )

    assert raised.value.reason_code == "COMPANION_SERVICE_NAME_COLLISION"
