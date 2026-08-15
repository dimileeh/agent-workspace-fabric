"""Credential-selection failure-path regressions for clarification staging."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.node import stack_launcher_auth_helpers as stack_launcher_auth_helpers_mod
from awf.node.compose_manager import AuthMount
from awf.node.stack_launcher_auth_helpers import (
    _aws_profile_file_targets,
    _external_account_credential_source_references,
    _has_claude_code_auth_credential,
    aws_profile_credential_paths,
    has_claude_code_file_auth,
    has_codex_file_auth,
    mounted_file_source,
)


@pytest.mark.unit
def test_codex_file_auth_ignores_mounts_outside_codex_home(tmp_path: Path) -> None:
    """A credential-shaped file outside the Codex home cannot authenticate a re-ask."""
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "auth.json").write_text('{"OPENAI_API_KEY": "secret"}', encoding="utf-8")

    assert has_codex_file_auth((AuthMount(str(unrelated), "/run/secrets", "ro"),)) is False


@pytest.mark.unit
def test_codex_file_auth_rejects_oversized_credentials(tmp_path: Path) -> None:
    """A writable oversized auth file cannot make the monitor materialize it."""
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "x" * (1024 * 1024 + 1)}), encoding="utf-8"
    )

    assert has_codex_file_auth((AuthMount(str(codex_home), "/home/agent/.codex", "rw"),)) is False


@pytest.mark.unit
def test_codex_file_auth_rejects_oversized_sparse_credentials_without_reading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A sparse credential larger than the limit is refused before it is materialized."""
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    auth_file = codex_home / "auth.json"
    auth_file.touch()
    os.truncate(auth_file, 1024 * 1024 + 1)

    def _unexpected_read(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("oversized sparse credential should not be read")

    monkeypatch.setattr(stack_launcher_auth_helpers_mod.os, "read", _unexpected_read)

    assert has_codex_file_auth((AuthMount(str(codex_home), "/home/agent/.codex", "rw"),)) is False


@pytest.mark.unit
def test_codex_file_auth_rejects_credential_that_grows_past_the_size_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A file that grows after its stat check remains bounded before parsing."""
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "x" * (1024 * 1024 + 1)}), encoding="utf-8"
    )
    real_fstat = stack_launcher_auth_helpers_mod.os.fstat

    def _stale_size(fd: int) -> SimpleNamespace:
        credential_stat = real_fstat(fd)
        return SimpleNamespace(st_mode=credential_stat.st_mode, st_size=0)

    monkeypatch.setattr(stack_launcher_auth_helpers_mod.os, "fstat", _stale_size)

    assert has_codex_file_auth((AuthMount(str(codex_home), "/home/agent/.codex", "rw"),)) is False


@pytest.mark.unit
def test_claude_file_auth_rejects_oversized_credentials(tmp_path: Path) -> None:
    """A writable oversized OAuth store cannot make the monitor materialize it."""
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    (claude_home / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "x" * (1024 * 1024 + 1)}}),
        encoding="utf-8",
    )

    assert (
        has_claude_code_file_auth((AuthMount(str(claude_home), "/home/agent/.claude", "rw"),))
        is False
    )


@pytest.mark.unit
def test_codex_file_auth_rejects_fifo_credentials(tmp_path: Path) -> None:
    """A replaced FIFO cannot block the clarification monitor's Codex probe."""
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    os.mkfifo(codex_home / "auth.json")

    assert has_codex_file_auth((AuthMount(str(codex_home), "/home/agent/.codex", "rw"),)) is False


@pytest.mark.unit
def test_codex_file_auth_rejects_symlinked_credentials(tmp_path: Path) -> None:
    """A replaced symlink cannot redirect a clarification credential probe."""
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    outside_credential = tmp_path / "outside-auth.json"
    outside_credential.write_text('{"OPENAI_API_KEY": "key"}', encoding="utf-8")
    (codex_home / "auth.json").symlink_to(outside_credential)

    assert has_codex_file_auth((AuthMount(str(codex_home), "/home/agent/.codex", "rw"),)) is False


@pytest.mark.unit
def test_claude_file_auth_rejects_fifo_credentials(tmp_path: Path) -> None:
    """A replaced FIFO cannot block the clarification monitor's Claude probe."""
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    os.mkfifo(claude_home / ".credentials.json")

    assert (
        has_claude_code_file_auth((AuthMount(str(claude_home), "/home/agent/.claude", "rw"),))
        is False
    )


@pytest.mark.unit
def test_codex_file_auth_accepts_bounded_regular_credentials(tmp_path: Path) -> None:
    """A normal regular credential file still authenticates the clarification re-ask."""
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text('{"OPENAI_API_KEY": "key"}', encoding="utf-8")

    assert has_codex_file_auth((AuthMount(str(codex_home), "/home/agent/.codex", "rw"),)) is True


@pytest.mark.unit
def test_claude_file_auth_accepts_bounded_regular_credentials(tmp_path: Path) -> None:
    """A normal regular OAuth store still authenticates the clarification re-ask."""
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    (claude_home / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "token"}}', encoding="utf-8"
    )

    assert (
        has_claude_code_file_auth((AuthMount(str(claude_home), "/home/agent/.claude", "rw"),))
        is True
    )


@pytest.mark.unit
def test_claude_file_auth_rejects_non_mapping_credentials() -> None:
    """A valid JSON value must still be an OAuth credential mapping."""
    assert _has_claude_code_auth_credential(["not", "a", "mapping"]) is False


@pytest.mark.unit
def test_aws_profile_file_targets_ignore_relative_mount_targets() -> None:
    """Only absolute mounted paths can be read as AWS profile configuration."""
    assert _aws_profile_file_targets(frozenset({"relative/aws/config"})) == ()


@pytest.mark.unit
def test_aws_profile_credential_paths_ignore_missing_selected_profile(tmp_path: Path) -> None:
    """An unrelated profile cannot leak its credential helper into clarification."""
    config = tmp_path / "config"
    config.write_text("[profile unrelated]\ncredential_process = /run/secrets/helper\n")
    mount = AuthMount(str(config), "/home/agent/.aws/config", "ro")

    assert (
        aws_profile_credential_paths(
            (mount,),
            agent_environment=(("AWS_PROFILE", "selected"),),
            provider_mount_targets=frozenset({mount.target}),
            mirror_target="/host/awf/git/mirror.git",
        )
        == ()
    )


@pytest.mark.unit
def test_aws_profile_credential_paths_reject_symlinked_profile_file(tmp_path: Path) -> None:
    """A writable AWS profile symlink cannot redirect clarification discovery."""
    aws_home = tmp_path / "aws"
    aws_home.mkdir()
    helper = tmp_path / "aws-helper"
    helper_target = "/run/awf/secrets/aws-helper"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    outside_profile = tmp_path / "outside-profile"
    outside_profile.write_text(
        f"[profile selected]\ncredential_process = {helper_target} --json\n",
        encoding="utf-8",
    )
    (aws_home / "config").symlink_to(outside_profile)
    aws_profile = AuthMount(str(aws_home), "/home/agent/.aws", "rw")
    helper_mount = AuthMount(str(helper), helper_target, "ro")

    assert (
        aws_profile_credential_paths(
            (aws_profile, helper_mount),
            agent_environment=(("AWS_PROFILE", "selected"),),
            provider_mount_targets=frozenset({aws_profile.target}),
            mirror_target="/host/awf/git/mirror.git",
        )
        == ()
    )


@pytest.mark.unit
def test_external_account_executable_output_file_is_staged(tmp_path: Path) -> None:
    """External-account executable output files remain available to the isolated agent."""
    adc = tmp_path / "external-account.json"
    adc_target = "/run/secrets/google/external-account.json"
    output_target = "/run/secrets/google/executable-output.json"
    adc.write_text(
        json.dumps(
            {
                "type": "external_account",
                "credential_source": {"executable": {"output_file": output_target}},
            }
        ),
        encoding="utf-8",
    )
    adc_mount = AuthMount(str(adc), adc_target, "ro")

    paths, environment_names = _external_account_credential_source_references(
        (adc_mount,),
        agent_environment=(("GOOGLE_APPLICATION_CREDENTIALS", adc_target),),
        provider_environment_names=frozenset({"GOOGLE_APPLICATION_CREDENTIALS"}),
        mirror_target="/host/awf/git/mirror.git",
        allowed_credential_process_environment_names=None,
    )

    assert paths == (output_target,)
    assert environment_names == frozenset()


@pytest.mark.unit
def test_external_account_ignores_symlinked_adc_under_writable_mount(tmp_path: Path) -> None:
    """A writable Gemini ADC symlink cannot redirect discovery to another file."""
    gemini_home = tmp_path / "gemini"
    gemini_home.mkdir()
    adc_target = "/home/agent/.gemini/application_default_credentials.json"
    subject_token_target = "/run/secrets/google/subject-token"
    outside_adc = tmp_path / "outside-adc.json"
    outside_adc.write_text(
        json.dumps(
            {
                "type": "external_account",
                "credential_source": {"file": subject_token_target},
            }
        ),
        encoding="utf-8",
    )
    (gemini_home / "application_default_credentials.json").symlink_to(outside_adc)
    subject_token = tmp_path / "subject-token"
    subject_token.write_text("subject-token", encoding="utf-8")

    assert _external_account_credential_source_references(
        (
            AuthMount(str(gemini_home), "/home/agent/.gemini", "rw"),
            AuthMount(str(subject_token), subject_token_target, "ro"),
        ),
        agent_environment=(("GOOGLE_APPLICATION_CREDENTIALS", adc_target),),
        provider_environment_names=frozenset({"GOOGLE_APPLICATION_CREDENTIALS"}),
        mirror_target="/host/awf/git/mirror.git",
        allowed_credential_process_environment_names=None,
    ) == ((), frozenset())


@pytest.mark.unit
def test_external_account_ignores_fifo_adc_under_writable_mount(tmp_path: Path) -> None:
    """A writable Gemini ADC FIFO cannot block external-account discovery."""
    gemini_home = tmp_path / "gemini"
    gemini_home.mkdir()
    adc_target = "/home/agent/.gemini/application_default_credentials.json"
    os.mkfifo(gemini_home / "application_default_credentials.json")

    assert _external_account_credential_source_references(
        (AuthMount(str(gemini_home), "/home/agent/.gemini", "rw"),),
        agent_environment=(("GOOGLE_APPLICATION_CREDENTIALS", adc_target),),
        provider_environment_names=frozenset({"GOOGLE_APPLICATION_CREDENTIALS"}),
        mirror_target="/host/awf/git/mirror.git",
        allowed_credential_process_environment_names=None,
    ) == ((), frozenset())


@pytest.mark.unit
def test_external_account_ignores_oversized_adc_under_writable_mount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An oversized writable Gemini ADC is rejected without materializing it."""
    gemini_home = tmp_path / "gemini"
    gemini_home.mkdir()
    adc_target = "/home/agent/.gemini/application_default_credentials.json"
    subject_token_target = "/run/secrets/google/subject-token"
    adc = gemini_home / "application_default_credentials.json"
    adc.write_text(
        json.dumps(
            {
                "type": "external_account",
                "credential_source": {"file": subject_token_target},
                "padding": "x" * (1024 * 1024),
            }
        ),
        encoding="utf-8",
    )

    def _unexpected_read(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("oversized ADC should not be read")

    monkeypatch.setattr(stack_launcher_auth_helpers_mod.os, "read", _unexpected_read)

    assert _external_account_credential_source_references(
        (AuthMount(str(gemini_home), "/home/agent/.gemini", "rw"),),
        agent_environment=(("GOOGLE_APPLICATION_CREDENTIALS", adc_target),),
        provider_environment_names=frozenset({"GOOGLE_APPLICATION_CREDENTIALS"}),
        mirror_target="/host/awf/git/mirror.git",
        allowed_credential_process_environment_names=None,
    ) == ((), frozenset())


@pytest.mark.unit
def test_external_account_ignores_non_mapping_credential_source(tmp_path: Path) -> None:
    """Malformed ADC credential sources cannot select arbitrary clarification mounts."""
    adc = tmp_path / "external-account.json"
    adc_target = "/run/secrets/google/external-account.json"
    adc.write_text(
        json.dumps({"type": "external_account", "credential_source": []}),
        encoding="utf-8",
    )
    adc_mount = AuthMount(str(adc), adc_target, "ro")

    assert _external_account_credential_source_references(
        (adc_mount,),
        agent_environment=(("GOOGLE_APPLICATION_CREDENTIALS", adc_target),),
        provider_environment_names=frozenset({"GOOGLE_APPLICATION_CREDENTIALS"}),
        mirror_target="/host/awf/git/mirror.git",
        allowed_credential_process_environment_names=None,
    ) == ((), frozenset())


@pytest.mark.unit
def test_mounted_file_source_rejects_target_outside_mount() -> None:
    """Clarification cannot resolve a host path for an unrelated container target."""
    mount = AuthMount("/host/secret", "/run/secrets/google", "ro")

    assert mounted_file_source(mount, "/run/secrets/aws/credentials") is None
