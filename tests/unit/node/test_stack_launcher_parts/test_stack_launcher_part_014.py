"""Clarification credential helper tests split from stack launcher part 008."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.node import stack_launcher_auth_helpers as stack_launcher_auth_helpers_mod
from awf.node.compose_manager import AuthMount


@pytest.mark.unit
@pytest.mark.parametrize("operator", (":=", ":+", ":?"))
def test_credential_process_references_remaining_parameter_operators(operator: str) -> None:
    """Credential helpers retain paths and inputs from remaining shell operators."""
    helper_target = "/run/awf/secrets/aws-helper"
    token_target = "/run/awf/secrets/credential-process-token"

    assert stack_launcher_auth_helpers_mod._credential_process_references(  # noqa: SLF001
        f"sh -c '{helper_target} --token-file \"${{TOKEN_FILE{operator}{token_target}}}\"'"
    ) == ((helper_target, token_target), frozenset({"TOKEN_FILE"}))


@pytest.mark.unit
def test_credential_mount_helpers_reject_unsafe_paths_and_keep_nested_credentials(
    tmp_path: Path,
) -> None:
    """Credential staging never escapes mounts and retains valid nested files."""
    credentials = tmp_path / "credentials"
    nested = credentials / "nested"
    nested.mkdir(parents=True)
    token = nested / "token"
    token.write_text("credential", encoding="utf-8")
    nested_mount = AuthMount(source=str(credentials), target="/run/awf/secrets", mode="ro")

    assert (
        stack_launcher_auth_helpers_mod._read_bounded_clarification_auth_credential(  # noqa: SLF001
            str(credentials),
            "../outside",
        )
        is None
    )
    assert (
        stack_launcher_auth_helpers_mod._read_bounded_clarification_auth_credential(  # noqa: SLF001
            str(credentials),
            "nested/token",
        )
        == "credential"
    )
    assert (
        stack_launcher_auth_helpers_mod._read_bounded_mounted_clarification_auth_credential(  # noqa: SLF001
            nested_mount,
            target="/outside/token",
        )
        is None
    )
    assert (
        stack_launcher_auth_helpers_mod.mounted_file_source(  # noqa: SLF001
            nested_mount,
            "/run/awf/secrets",
        )
        == credentials
    )
    assert (
        stack_launcher_auth_helpers_mod.mounted_file_source(  # noqa: SLF001
            nested_mount,
            "/run/awf/secrets/nested/token",
        )
        == token
    )


@pytest.mark.unit
def test_malformed_aws_profile_file_is_ignored_for_credential_staging(tmp_path: Path) -> None:
    """Invalid profile syntax cannot make clarification stage unrelated credentials."""
    config = tmp_path / "config"
    config.write_text("[profile missing bracket\n", encoding="utf-8")
    config_mount = AuthMount(
        source=str(config),
        target="/home/agent/.aws/config",
        mode="ro",
    )

    assert (
        stack_launcher_auth_helpers_mod.aws_profile_credential_environment_names(
            (config_mount,),
            agent_environment=(("AWS_PROFILE", "awf-bedrock"),),
            provider_mount_targets=frozenset({config_mount.target}),
            mirror_target="/host/awf/git/mirrors/repo.git",
        )
        == frozenset()
    )
