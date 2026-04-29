"""Profile security lint tests."""

from __future__ import annotations

import json

import pytest

from awf.profiles.lint import lint_workspace_profile, profile_lint_errors
from awf.profiles.models import ProfileLintSeverity, WorkspaceProfile
from awf.profiles.resolver import ProfileResolutionError, resolve_workspace_profile


def _profile_with_secret(secret: dict[str, object]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate({"name": "secret-profile", "secrets": [secret]})


@pytest.mark.unit
def test_safe_secret_declarations_produce_no_lint_findings() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "safe-secrets",
            "secrets": [
                {
                    "name": "github-token",
                    "kind": "mount",
                    "target": "/run/awf/secrets/github-token",
                    "provider": "vault",
                    "ref": "kv/data/awf/github-token",
                },
                {
                    "name": "github-env",
                    "kind": "env",
                    "target": "GITHUB_TOKEN",
                    "provider": "vault",
                    "ref": "kv/data/awf/github-token",
                },
            ],
        }
    )

    assert lint_workspace_profile(profile) == ()
    assert profile_lint_errors(profile) == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    "target",
    [
        "/",
        "/home",
        "/home/agent",
        "/home/agent/.ssh",
        "/root",
        "/etc",
        "/tmp",
        "/var",
        "/proc",
        "/sys",
        "/run/awf/secrets/../github-token",
    ],
)
def test_unsafe_secret_mount_targets_return_structured_lint_errors(target: str) -> None:
    profile = _profile_with_secret(
        {
            "name": "github-token",
            "kind": "mount",
            "target": target,
            "provider": "vault",
            "ref": "kv/data/awf/github-token",
        }
    )

    errors = profile_lint_errors(profile)

    assert len(errors) == 1
    assert errors[0].reason_code == "SECRET_TARGET_TOO_BROAD"
    assert errors[0].severity is ProfileLintSeverity.error
    assert errors[0].path == "secrets[0].target"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("target", "reason_code"),
    [
        ("PATH", "SECRET_ENV_TARGET_RESERVED"),
        ("HOME", "SECRET_ENV_TARGET_RESERVED"),
        ("USER", "SECRET_ENV_TARGET_RESERVED"),
        (" ", "SECRET_ENV_TARGET_INVALID"),
        ("1TOKEN", "SECRET_ENV_TARGET_INVALID"),
        ("BAD-NAME", "SECRET_ENV_TARGET_INVALID"),
    ],
)
def test_unsafe_secret_env_targets_return_structured_lint_errors(
    target: str,
    reason_code: str,
) -> None:
    profile = _profile_with_secret(
        {
            "name": "api-token",
            "kind": "env",
            "target": target,
            "provider": "vault",
            "ref": "kv/data/awf/api-token",
        }
    )

    errors = profile_lint_errors(profile)

    assert len(errors) == 1
    assert errors[0].reason_code == reason_code
    assert errors[0].path == "secrets[0].target"


@pytest.mark.unit
def test_raw_looking_secret_ref_is_rejected_without_exposing_value() -> None:
    raw_secret = "sk-live-do-not-echo-this-value"
    profile = _profile_with_secret(
        {
            "name": "api-token",
            "kind": "env",
            "target": "API_TOKEN",
            "provider": "inline",
            "ref": raw_secret,
        }
    )

    errors = profile_lint_errors(profile)

    assert [finding.reason_code for finding in errors] == ["SECRET_REF_LOOKS_RAW"]
    assert raw_secret not in json.dumps([finding.model_dump(mode="json") for finding in errors])


@pytest.mark.unit
def test_long_provider_ref_with_common_cloud_identifier_chars_is_not_raw_secret() -> None:
    cloud_ref = (
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
        "prod/team/*/database/password?stage=$current/"
        "very/long/reference/path/that/exceeds/the/raw-secret-length-threshold"
    )
    assert len(cloud_ref) > 128
    profile = _profile_with_secret(
        {
            "name": "database-password",
            "kind": "mount",
            "target": "/run/awf/secrets/database-password",
            "provider": "aws",
            "ref": cloud_ref,
        }
    )

    assert profile_lint_errors(profile) == ()


@pytest.mark.unit
def test_resolver_rejects_profile_lint_errors_with_primary_reason_code() -> None:
    raw_secret = "github_pat_do-not-echo-this-value"

    with pytest.raises(ProfileResolutionError) as exc_info:
        resolve_workspace_profile(
            worktree_path=None,
            inline_profile={
                "name": "bad-inline",
                "secrets": [
                    {
                        "name": "github-token",
                        "kind": "env",
                        "target": "GITHUB_TOKEN",
                        "provider": "inline",
                        "ref": raw_secret,
                    }
                ],
            },
        )

    exc = exc_info.value
    assert exc.reason_code == "SECRET_REF_LOOKS_RAW"
    assert exc.detail is not None
    assert exc.detail["reason_code"] == "SECRET_REF_LOOKS_RAW"
    assert exc.detail["findings"][0]["reason_code"] == "SECRET_REF_LOOKS_RAW"
    assert raw_secret not in str(exc)
    assert raw_secret not in json.dumps(exc.detail)


@pytest.mark.unit
def test_known_local_credential_mount_warns_under_compatibility_policy() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "local-compat",
            "security": {"host_home_auth_mounts": {"mode": "warn"}},
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [
                        (
                            "${AWF_HOST_HOME}/.config/gh",
                            "/home/agent/.config/gh:ro",
                        )
                    ],
                }
            ],
        }
    )

    findings = lint_workspace_profile(profile)

    assert len(findings) == 1
    assert profile_lint_errors(profile) == ()
    assert findings[0].reason_code == "HOST_HOME_AUTH_MOUNT_COMPATIBILITY"
    assert findings[0].severity is ProfileLintSeverity.warning
    assert findings[0].path == "services[0].volumes[0]"


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        "${AWF_HOST_HOME}/.config/gh",
        "$AWF_HOST_HOME/.config/gh",
    ],
)
def test_known_local_credential_mount_blocks_under_default_strict_policy(source: str) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "local-strict",
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [
                        (
                            source,
                            "/home/agent/.config/gh:ro",
                        )
                    ],
                }
            ],
        }
    )

    errors = profile_lint_errors(profile)

    assert len(errors) == 1
    assert errors[0].reason_code == "HOST_HOME_AUTH_MOUNT_COMPATIBILITY"
    assert errors[0].severity is ProfileLintSeverity.error


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        "~",
        "$HOME",
        "${HOME}",
        "${AWF_HOST_HOME}",
        "/home/alice",
        "/Users/alice",
    ],
)
def test_broad_host_home_auth_mounts_block_by_default(source: str) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "bad-host-home",
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [(source, "/home/agent")],
                }
            ],
        }
    )

    errors = profile_lint_errors(profile)

    assert len(errors) == 1
    assert errors[0].reason_code == "HOST_HOME_AUTH_MOUNT_TOO_BROAD"
    assert errors[0].severity is ProfileLintSeverity.error


@pytest.mark.unit
def test_broad_host_home_auth_mounts_remain_errors_under_warn_policy() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "bad-host-home-warn",
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

    errors = profile_lint_errors(profile)

    assert len(errors) == 1
    assert errors[0].reason_code == "HOST_HOME_AUTH_MOUNT_TOO_BROAD"
    assert errors[0].severity is ProfileLintSeverity.error


@pytest.mark.unit
def test_writable_host_home_auth_mounts_remain_errors_under_warn_policy() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "writable-host-auth",
            "security": {"host_home_auth_mounts": {"mode": "warn"}},
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [("${AWF_HOST_HOME}/.config/gh", "/home/agent/.config/gh:rw")],
                }
            ],
        }
    )

    errors = profile_lint_errors(profile)

    assert len(errors) == 1
    assert errors[0].reason_code == "HOST_HOME_AUTH_MOUNT_WRITABLE"
    assert errors[0].severity is ProfileLintSeverity.error


@pytest.mark.unit
def test_volume_target_flags_without_access_mode_preserve_auth_target() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "selinux-host-auth",
            "security": {"host_home_auth_mounts": {"mode": "warn"}},
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [("${AWF_HOST_HOME}/.config/gh", "/home/agent/.config/gh:z")],
                }
            ],
        }
    )

    errors = profile_lint_errors(profile)

    assert len(errors) == 1
    assert errors[0].reason_code == "HOST_HOME_AUTH_MOUNT_WRITABLE"
    assert errors[0].severity is ProfileLintSeverity.error
