"""Profile security lint tests."""

from __future__ import annotations

import json

import pytest

import awf.profiles.lint as profile_lint
from awf.profiles.lint import (
    lint_workspace_profile,
    profile_lint_errors,
    profile_service_volume_lint_errors,
)
from awf.profiles.models import ProfileLintSeverity, WorkspaceProfile
from awf.profiles.resolver import ProfileResolutionError, resolve_workspace_profile

_PRIVATE_KEY_SENTINEL = "-----BEGIN " + "PRIVATE KEY-----"
_PRIVATE_KEY_SAMPLE = "-----BEGIN " + "PRIVATE KEY-----\nsecret\n-----END " + "PRIVATE KEY-----"


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
def test_declared_local_secret_leases_are_safe_default_profile_shape() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "local-declared-leases",
            "secrets": [
                {
                    "name": "github-token",
                    "kind": "env",
                    "target": "GH_TOKEN",
                    "provider": "github",
                    "ref": "token",
                },
                {
                    "name": "openai-token",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "provider": "env",
                    "ref": "env/OPENAI_API_KEY",
                },
                {
                    "name": "github-cli-config",
                    "kind": "mount",
                    "target": "/home/agent/.config/gh",
                    "provider": "local-auth",
                    "ref": ".config/gh",
                },
                {
                    "name": "service-account",
                    "kind": "mount",
                    "target": "/run/awf/secrets/gcp/credentials.json",
                    "provider": "host-file",
                    "ref": "/var/lib/awf/secrets/gcp/credentials.json",
                },
            ],
        }
    )

    assert lint_workspace_profile(profile) == ()
    assert profile_lint_errors(profile) == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    "ref",
    [
        "~",
        "${HOME}",
        "${AWF_HOST_HOME}",
        "/home/alice",
        "/Users/alice",
    ],
)
def test_broad_declared_local_file_refs_are_rejected_without_echoing_ref(ref: str) -> None:
    profile = _profile_with_secret(
        {
            "name": "host-home",
            "kind": "mount",
            "target": "/run/awf/secrets/host-home",
            "provider": "local-file",
            "ref": ref,
        }
    )

    errors = profile_lint_errors(profile)

    assert [finding.reason_code for finding in errors] == ["SECRET_LEASE_SOURCE_TOO_BROAD"]
    assert ref not in json.dumps([finding.model_dump(mode="json") for finding in errors])


@pytest.mark.unit
def test_writable_declared_local_auth_lease_is_rejected_with_explicit_reason() -> None:
    profile = _profile_with_secret(
        {
            "name": "github-cli-config",
            "kind": "mount",
            "target": "/home/agent/.config/gh",
            "provider": "local-auth",
            "ref": ".config/gh",
            "mode": "rw",
        }
    )

    errors = profile_lint_errors(profile)

    assert [finding.reason_code for finding in errors] == ["SECRET_LEASE_WRITABLE_UNSUPPORTED"]
    assert ".config/gh" not in json.dumps([finding.model_dump(mode="json") for finding in errors])


@pytest.mark.unit
def test_relative_declared_local_auth_target_is_rejected_as_broad() -> None:
    profile = _profile_with_secret(
        {
            "name": "github-cli-config",
            "kind": "mount",
            "target": "home/agent/.config/gh",
            "provider": "local-auth",
            "ref": ".config/gh",
        }
    )

    errors = profile_lint_errors(profile)

    assert [finding.reason_code for finding in errors] == ["SECRET_TARGET_TOO_BROAD"]


@pytest.mark.unit
def test_declared_local_file_ref_under_home_placeholder_is_rejected() -> None:
    profile = _profile_with_secret(
        {
            "name": "host-home-file",
            "kind": "mount",
            "target": "/run/awf/secrets/host-home-file",
            "provider": "host-file",
            "ref": "${HOME}/.ssh/id_rsa",
        }
    )

    errors = profile_lint_errors(profile)

    assert [finding.reason_code for finding in errors] == ["SECRET_LEASE_SOURCE_TOO_BROAD"]
    assert "${HOME}/.ssh/id_rsa" not in json.dumps(
        [finding.model_dump(mode="json") for finding in errors]
    )


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
def test_relative_secret_mount_target_returns_structured_lint_error() -> None:
    profile = _profile_with_secret(
        {
            "name": "github-token",
            "kind": "mount",
            "target": "relative/path",
            "provider": "vault",
            "ref": "kv/data/awf/github-token",
        }
    )

    errors = profile_lint_errors(profile)

    assert len(errors) == 1
    assert errors[0].reason_code == "SECRET_TARGET_TOO_BROAD"
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
@pytest.mark.parametrize(
    "raw_secret",
    [
        "line-one\nline-two",
        "aaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbb.cccccccccccccccc",
    ],
)
def test_multiline_and_jwt_like_secret_refs_are_rejected(raw_secret: str) -> None:
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
def test_blank_secret_ref_is_not_treated_as_raw_secret_material() -> None:
    profile = _profile_with_secret(
        {
            "name": "api-token",
            "kind": "env",
            "target": "API_TOKEN",
            "provider": "vault",
            "ref": " ",
        }
    )

    assert profile_lint_errors(profile) == ()


@pytest.mark.unit
def test_secret_without_provider_or_ref_is_allowed() -> None:
    profile = _profile_with_secret(
        {
            "name": "api-token",
            "kind": "env",
            "target": "API_TOKEN",
        }
    )

    assert profile_lint_errors(profile) == ()


@pytest.mark.unit
def test_secret_provider_ref_mismatch_is_rejected() -> None:
    profile = _profile_with_secret(
        {
            "name": "api-token",
            "kind": "env",
            "target": "API_TOKEN",
            "provider": "vault",
        }
    )

    errors = profile_lint_errors(profile)

    assert [finding.reason_code for finding in errors] == ["SECRET_PROVIDER_REF_MISMATCH"]


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
def test_profile_lint_private_path_and_raw_secret_edges() -> None:
    providerless_secret = _profile_with_secret(
        {"name": "providerless", "target": "/run/awf/secrets/providerless"}
    ).secrets[0]
    assert profile_lint._normalized_secret_provider(providerless_secret) is None
    assert profile_lint._secret_mount_target_is_too_broad("relative/path") is True
    assert profile_lint._looks_like_raw_secret(None) is False
    assert profile_lint._looks_like_raw_secret("   ") is False
    assert profile_lint._looks_like_raw_secret(_PRIVATE_KEY_SENTINEL) is True
    assert profile_lint._looks_like_raw_secret("line-one\nline-two") is True
    assert profile_lint._looks_like_raw_secret("a" * 16 + "." + "b" * 16 + "." + "c" * 16) is True
    assert profile_lint._looks_like_raw_secret(("x" * 128) + "!") is True

    home_source = profile_lint._host_home_source("~")
    assert home_source is not None
    assert home_source.is_root is True
    assert home_source.relative_path == ""
    prefixed_source = profile_lint._host_home_source("${AWF_HOST_HOME}/.docker/config.json")
    assert prefixed_source is not None
    assert prefixed_source.relative_path == ".docker/config.json"

    assert profile_lint._normalize_container_path("relative") is None
    split = profile_lint._split_volume_target("/home/agent/.config/gh:rw,z")
    assert split.path == "/home/agent/.config/gh"
    assert split.mode == "rw"


@pytest.mark.unit
def test_blank_provider_ref_is_not_treated_as_raw_secret() -> None:
    profile = _profile_with_secret(
        {
            "name": "api-token",
            "kind": "env",
            "target": "API_TOKEN",
            "provider": "inline",
            "ref": "   ",
        }
    )

    assert profile_lint_errors(profile) == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw_ref",
    [
        _PRIVATE_KEY_SAMPLE,
        ("aaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbb.cccccccccccccccc"),
    ],
)
def test_pem_and_jwt_refs_are_rejected_without_exposing_value(raw_ref: str) -> None:
    profile = _profile_with_secret(
        {
            "name": "api-token",
            "kind": "env",
            "target": "API_TOKEN",
            "provider": "inline",
            "ref": raw_ref,
        }
    )

    errors = profile_lint_errors(profile)

    assert [finding.reason_code for finding in errors] == ["SECRET_REF_LOOKS_RAW"]
    assert raw_ref not in json.dumps([finding.model_dump(mode="json") for finding in errors])


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


@pytest.mark.unit
def test_relative_secret_mount_target_is_rejected_as_too_broad() -> None:
    profile = _profile_with_secret(
        {
            "name": "api-token",
            "kind": "mount",
            "target": "relative/path",
            "provider": "vault",
            "ref": "kv/data/api-token",
        }
    )

    errors = profile_lint_errors(profile)

    assert [finding.reason_code for finding in errors] == ["SECRET_TARGET_TOO_BROAD"]


@pytest.mark.unit
def test_host_home_volume_with_relative_container_target_is_ignored() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "relative-volume-target",
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [("${AWF_HOST_HOME}/.config/gh", "relative-target")],
                }
            ],
        }
    )

    assert lint_workspace_profile(profile) == ()


@pytest.mark.unit
def test_host_home_mount_with_relative_container_target_is_ignored() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "relative-target",
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [("${HOME}/.config/gh", "relative/target")],
                }
            ],
        }
    )

    assert profile_lint_errors(profile) == ()


@pytest.mark.unit
def test_non_host_home_service_volume_is_ignored() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "ordinary-volume",
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [("/var/cache/app", "/cache")],
                }
            ],
        }
    )

    assert lint_workspace_profile(profile) == ()
    assert profile_service_volume_lint_errors(profile) == ()


@pytest.mark.unit
def test_host_home_mount_with_relative_mode_target_is_ignored() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "relative-mode-target",
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [("${HOME}/.config/gh", "relative/target:ro")],
                }
            ],
        }
    )

    assert profile_lint_errors(profile) == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    "target",
    [
        "/workspace/cache",
        "/workspace/cache:",
    ],
)
def test_specific_host_home_mount_to_non_auth_target_is_allowed(target: str) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "specific-host-home",
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [("~/project/cache", target)],
                }
            ],
        }
    )

    assert lint_workspace_profile(profile) == ()


@pytest.mark.unit
def test_host_home_mount_to_non_auth_workspace_path_is_allowed() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "workspace-cache",
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [("${HOME}/projects", "/workspace/projects")],
                }
            ],
        }
    )

    assert profile_lint_errors(profile) == ()


@pytest.mark.unit
def test_tilde_prefixed_known_local_credential_mount_warns_under_compatibility_policy() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "tilde-local-compat",
            "security": {"host_home_auth_mounts": {"mode": "warn"}},
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [("~/.config/gh", "/home/agent/.config/gh:ro")],
                }
            ],
        }
    )

    findings = lint_workspace_profile(profile)

    assert len(findings) == 1
    assert findings[0].reason_code == "HOST_HOME_AUTH_MOUNT_COMPATIBILITY"
    assert findings[0].severity is ProfileLintSeverity.warning


@pytest.mark.unit
@pytest.mark.parametrize(
    "target",
    [
        "/home/agent/cache",
        "/workspace/.docker",
    ],
)
def test_specific_host_home_mount_to_broad_or_auth_like_target_blocks(target: str) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "specific-host-home-risk",
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [("${AWF_HOST_HOME}/project", target)],
                }
            ],
        }
    )

    errors = profile_lint_errors(profile)

    assert len(errors) == 1
    assert errors[0].reason_code == "HOST_HOME_AUTH_MOUNT_TOO_BROAD"
    assert errors[0].severity is ProfileLintSeverity.error


@pytest.mark.unit
def test_host_home_mount_to_auth_like_or_root_target_is_blocked() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "auth-like-targets",
            "services": [
                {
                    "name": "api",
                    "image": "example/api:latest",
                    "volumes": [
                        ("${HOME}/projects", "/workspace/.ssh"),
                        ("${HOME}/projects", "/root/.config"),
                    ],
                }
            ],
        }
    )

    errors = profile_lint_errors(profile)

    assert [finding.reason_code for finding in errors] == [
        "HOST_HOME_AUTH_MOUNT_TOO_BROAD",
        "HOST_HOME_AUTH_MOUNT_TOO_BROAD",
    ]
