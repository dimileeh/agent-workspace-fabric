"""Stack launcher hosted render and lifecycle tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.node import stack_launcher as stack_launcher_mod
from awf.node.companion_services import (
    CompanionEnvironmentSecretRef,
    MaterializedCompanionService,
    WorkspaceCompanionSpec,
)
from awf.node.compose_manager import AuthMount
from awf.node.git_manager import WorktreeLayout
from awf.node.stack_launcher import ComposeStackLauncher, WorkspaceStackLaunchRequest
from awf.profiles.models import ProfileSecret, WorkspaceProfile
from tests.unit.node.test_stack_launcher_parts._helpers import (
    _FailingDeclaredLeaseResolver,
    _layout,
    _RecordingCompose,
)


@pytest.mark.unit
async def test_compose_stack_launcher_launch_notifies_compose_up_started_once() -> None:
    """The compose-start callback fires once when the first compose up starts."""
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    starts = 0

    async def _started() -> None:
        nonlocal starts
        starts += 1

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(name="generic"),
            on_compose_up_started=_started,
        )
    )

    assert starts == 1
    assert compose.waits == [True]


@pytest.mark.unit
async def test_compose_stack_launcher_render_without_profile_secrets_returns_plain_paths() -> None:
    """Hosted render-only stacks with no leases return ordinary compose paths."""
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        secret_lease_resolver=_FailingDeclaredLeaseResolver(),
    )
    layout = _layout()

    paths = await launcher.render(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=layout,
            profile=WorkspaceProfile(name="hosted-no-secrets"),
        )
    )

    assert paths is not None
    assert paths.secret_lease_mount_metadata == {}
    assert compose.specs == []
    assert len(compose.render_specs) == 1
    assert compose.render_specs[0].auth_mounts == (
        AuthMount(source=str(layout.mirror_path), target=str(layout.mirror_path), mode="rw"),
    )


@pytest.mark.unit
async def test_compose_stack_launcher_render_preserves_companion_env_secret_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted render keeps companion env secret source names without local env resolution."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPTIONAL_TOKEN_SOURCE", raising=False)
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@github.com:example/backend.git",
            environment_secrets=(
                CompanionEnvironmentSecretRef(
                    target="AIRA_API_KEY",
                    value_from="ANTHROPIC_API_KEY",
                ),
                CompanionEnvironmentSecretRef(
                    target="OPTIONAL_TOKEN",
                    value_from="OPTIONAL_TOKEN_SOURCE",
                    required=False,
                ),
            ),
        ),
        layout=_layout(),
    )

    paths = await launcher.render(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(name="hosted-companion-secrets"),
            companions=(companion,),
            companion_graph_prevalidated=True,
        )
    )

    assert paths is not None
    assert compose.specs == []
    assert len(compose.render_specs) == 1
    rendered = compose.render_specs[0].companions[0]
    assert rendered.environment == (
        ("AIRA_API_KEY", "${ANTHROPIC_API_KEY}"),
        ("OPTIONAL_TOKEN", "${OPTIONAL_TOKEN_SOURCE}"),
    )
    assert paths.secret_lease_mount_metadata["companion_env_secret_count"] == 2
    assert paths.secret_lease_mount_metadata["companion_env_secrets"] == (
        {
            "companion": "backend",
            "target": "AIRA_API_KEY",
            "provider": "env",
            "source": "ANTHROPIC_API_KEY",
            "required": True,
        },
        {
            "companion": "backend",
            "target": "OPTIONAL_TOKEN",
            "provider": "env",
            "source": "OPTIONAL_TOKEN_SOURCE",
            "required": False,
        },
    )


@pytest.mark.unit
async def test_compose_stack_launcher_render_attaches_portable_companion_source_metadata(
    tmp_path: Path,
) -> None:
    """Hosted render carries Git/build metadata instead of only Core-local paths."""
    companion_root = tmp_path / "backend"
    (companion_root / "services" / "api").mkdir(parents=True)
    (companion_root / "config").mkdir()
    (companion_root / "fixtures").mkdir()
    (companion_root / "docker").mkdir()
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="ssh://git:source-token@github.com/example/backend.git",
            base_branch="development",
            build_context="services/api",
            dockerfile="docker/Dockerfile",
            env_file="config/dev.env",
            volumes=(("./fixtures", "/fixtures"), ("cache_data", "/cache")),
        ),
        layout=WorktreeLayout(
            mirror_path=tmp_path / "backend.git",
            worktree_path=companion_root,
            branch_name="awf/ws_launcher/companion/backend",
        ),
        commit_sha="abc123def456",
    )

    await launcher.render(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(name="hosted-companion-source"),
            companions=(companion,),
            companion_graph_prevalidated=True,
        )
    )

    rendered = compose.render_specs[0].companions[0]
    assert rendered.build_context == str(companion_root / "services" / "api")
    assert rendered.source_metadata == {
        "schema": "hosted_companion_source.v1",
        "name": "backend",
        "repo_url": "ssh://git@github.com/example/backend.git",
        "base_branch": "development",
        "commit_sha": "abc123def456",
        "build_context": "services/api",
        "dockerfile": "docker/Dockerfile",
        "env_file": "config/dev.env",
        "volumes": (
            {"source": "./fixtures", "target": "/fixtures"},
            {"source": "cache_data", "target": "/cache"},
        ),
    }


@pytest.mark.unit
async def test_compose_stack_launcher_render_records_optional_unrenderable_hosted_secrets() -> None:
    """Optional hosted secret declarations are skipped explicitly, not resolved locally."""
    compose = _RecordingCompose()
    lease_resolver = _FailingDeclaredLeaseResolver()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        secret_lease_resolver=lease_resolver,
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-optional-unrenderable",
            "secrets": [
                {
                    "name": "missing-provider",
                    "kind": "mount",
                    "target": "/home/agent/.missing",
                    "required": False,
                },
                {
                    "name": "blank-provider",
                    "kind": "mount",
                    "target": "/home/agent/.blank",
                    "provider": " ",
                    "required": False,
                },
                {
                    "name": "vault-token",
                    "kind": "env",
                    "target": "VAULT_TOKEN",
                    "provider": "vault",
                    "required": False,
                },
                {
                    "name": "bad-env-ref",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "provider": "env",
                    "ref": "env/1_OPENAI_API_KEY",
                    "required": False,
                },
                {
                    "name": "github-wrong-target",
                    "kind": "env",
                    "target": "NOT_GITHUB_TOKEN",
                    "provider": "github",
                    "required": False,
                },
                {
                    "name": "github-mount",
                    "kind": "mount",
                    "target": "/home/agent/.config/gh",
                    "provider": "github",
                    "required": False,
                },
                {
                    "name": "bitbucket-wrong-target",
                    "kind": "env",
                    "target": "BB_TOKEN",
                    "provider": "bitbucket",
                    "required": False,
                },
                {
                    "name": "local-file-env",
                    "kind": "env",
                    "target": "NPMRC",
                    "provider": "local-file",
                    "required": False,
                },
                {
                    "name": "relative-local-file",
                    "kind": "mount",
                    "target": "relative-npmrc",
                    "provider": "local-file",
                    "required": False,
                },
            ],
        }
    )

    paths = await launcher.render(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=profile,
        )
    )

    assert paths is not None
    assert lease_resolver.calls == []
    assert compose.specs == []
    assert len(compose.render_specs) == 1
    assert paths.secret_lease_mount_metadata == {
        "schema": "secret_lease_mount_metadata.v1",
        "mount_plan": "profile_declared_secret_leases",
        "env_count": 0,
        "mount_count": 0,
        "providers": [],
        "targets": [],
        "skipped_unresolved_count": 9,
    }


@pytest.mark.unit
async def test_compose_stack_launcher_render_deduplicates_hosted_env_targets() -> None:
    """Duplicate hosted env lease targets keep one rendered target and source."""
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-duplicate-env",
            "secrets": [
                {
                    "name": "openai-primary",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "provider": "env",
                    "ref": "env/OPENAI_PRIMARY",
                },
                {
                    "name": "openai-secondary",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "provider": "env",
                    "ref": "env/OPENAI_SECONDARY",
                },
            ],
        }
    )

    paths = await launcher.render(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=profile,
        )
    )

    assert paths is not None
    assert paths.secret_lease_mount_metadata["env_count"] == 1
    assert paths.secret_lease_mount_metadata["targets"] == ["OPENAI_API_KEY"]
    assert paths.secret_lease_mount_metadata["providers"] == ["env"]


@pytest.mark.unit
def test_hosted_env_secret_alias_pairs_ignores_unknown_provider() -> None:
    """The alias helper fails closed for a provider outside the hosted allowlist."""
    secret = ProfileSecret(
        name="unknown",
        kind="env",
        target="UNKNOWN_TOKEN",
        provider="unknown",
        ref="env/UNKNOWN_TOKEN",
    )

    assert (
        stack_launcher_mod._hosted_env_secret_alias_pairs(
            secret,
            provider="unknown",
        )
        is None
    )


@pytest.mark.unit
def test_hosted_dynamic_file_auth_mount_targets_accept_only_absolute_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted ADC file targets preserve metadata without Core-local env resolution."""
    hosted_adc_target = "/home/agent/.config/gcloud/application_default_credentials.json"
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/core/adc/should-not-leak.json")

    assert stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(
        (("GOOGLE_APPLICATION_CREDENTIALS", "${GOOGLE_APPLICATION_CREDENTIALS}"),)
    ) == (hosted_adc_target,)
    assert stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(
        (("GOOGLE_APPLICATION_CREDENTIALS", "/profile/gcp/application.json"),)
    ) == ("/profile/gcp/application.json",)
    assert stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(
        (("GOOGLE_APPLICATION_CREDENTIALS", "$GOOGLE_APPLICATION_CREDENTIALS"),)
    ) == (hosted_adc_target,)
    assert stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(()) == ()
    assert (
        stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(
            (("GOOGLE_APPLICATION_CREDENTIALS", "${OTHER_CREDENTIALS}"),)
        )
        == ()
    )

    assert (
        stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(
            (("GOOGLE_APPLICATION_CREDENTIALS", "relative.json"),)
        )
        == ()
    )
