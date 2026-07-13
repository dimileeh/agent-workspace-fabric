"""Stack launcher hosted render-only edge tests."""

from __future__ import annotations

import pytest

from awf.node import stack_launcher as stack_launcher_mod
from awf.node.compose_manager import AuthMount
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
    """Hosted ADC file targets are included only when they resolve to a container path."""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/var/run/gcp/application.json")

    assert stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(
        (("GOOGLE_APPLICATION_CREDENTIALS", "${GOOGLE_APPLICATION_CREDENTIALS}"),)
    ) == ("/var/run/gcp/application.json",)
    assert stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(
        (("GOOGLE_APPLICATION_CREDENTIALS", "/profile/gcp/application.json"),)
    ) == ("/profile/gcp/application.json",)
    assert stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(()) == ()
    assert (
        stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(
            (("GOOGLE_APPLICATION_CREDENTIALS", "${OTHER_CREDENTIALS}"),)
        )
        == ()
    )

    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "relative.json")
    assert (
        stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(
            (("GOOGLE_APPLICATION_CREDENTIALS", "$GOOGLE_APPLICATION_CREDENTIALS"),)
        )
        == ()
    )
