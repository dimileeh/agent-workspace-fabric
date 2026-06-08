"""Local profile-declared secret lease resolution tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf.node.compose_manager import AuthMount
from awf.node.secret_mounts import (
    LocalSecretLeaseMountResolver,
    SecretLeaseResolutionError,
)
from awf.profiles.models import WorkspaceProfile


def _profile(*secrets: dict[str, object]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "lease-profile",
            "secrets": list(secrets),
        }
    )


def _profile_with_runtime_env(
    runtime_environment: dict[str, str],
    *secrets: dict[str, object],
) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "lease-profile",
            "secrets": list(secrets),
            "runtime": {"environment": runtime_environment},
        }
    )


def _resolver(
    tmp_path: Path,
    *,
    host_env: dict[str, str] | None = None,
) -> LocalSecretLeaseMountResolver:
    host_home = tmp_path / "host-home"
    host_home.mkdir(exist_ok=True)
    return LocalSecretLeaseMountResolver(
        host_home=host_home,
        work_dir=tmp_path / "work",
        host_env=host_env or {},
    )


@pytest.mark.unit
def test_env_provider_exposes_placeholder_without_secret_value(tmp_path: Path) -> None:
    raw_secret = "sk-live-do-not-render"
    resolver = _resolver(tmp_path, host_env={"OPENAI_API_KEY": raw_secret})

    resolution = resolver.resolve(
        _profile(
            {
                "name": "openai",
                "kind": "env",
                "target": "OPENAI_API_KEY",
                "provider": "env",
                "ref": "env/OPENAI_API_KEY",
            }
        ),
        workspace_id="ws_secret",
    )

    assert resolution.environment == (("OPENAI_API_KEY", "${OPENAI_API_KEY}"),)
    assert resolution.mounts == ()
    rendered = json.dumps(
        {
            "resolution": repr(resolution),
            "metadata": resolution.metadata,
        },
        default=str,
    )
    assert raw_secret not in rendered
    assert resolution.metadata["env_count"] == 1
    # No AWF-internal env vars here, so the full env count matches the lease count.
    assert resolution.metadata["total_env_count"] == 1
    assert resolution.metadata["mount_count"] == 0
    assert resolution.metadata["providers"] == ["env"]
    assert resolution.metadata["targets"] == ["OPENAI_API_KEY"]


@pytest.mark.unit
def test_resolution_metadata_cannot_be_mutated_after_creation(tmp_path: Path) -> None:
    resolver = _resolver(tmp_path)

    resolution = resolver.resolve(
        _profile(
            {
                "name": "optional-openai",
                "kind": "env",
                "target": "OPENAI_API_KEY",
                "provider": "env",
                "ref": "env/OPENAI_API_KEY",
                "required": False,
            }
        ),
        workspace_id="ws_secret",
    )

    with pytest.raises(TypeError):
        resolution.metadata["extra"] = "injected"
    with pytest.raises(AttributeError):
        resolution.metadata["providers"].append("injected")
    with pytest.raises(TypeError):
        resolution.metadata["omitted_optional"][0]["secret_name"] = "changed"


@pytest.mark.unit
def test_github_provider_prefers_awf_token_and_exposes_standard_placeholders(
    tmp_path: Path,
) -> None:
    raw_secret = "ghp_do_not_render"
    resolver = _resolver(
        tmp_path,
        host_env={
            "AWF_GITHUB_TOKEN": raw_secret,
            "GH_TOKEN": "lower-priority-token",
            "GITHUB_TOKEN": "lowest-priority-token",
        },
    )

    resolution = resolver.resolve(
        _profile(
            {
                "name": "github",
                "kind": "env",
                "target": "GH_TOKEN",
                "provider": "github",
                "ref": "token",
            }
        ),
        workspace_id="ws_secret",
    )

    assert resolution.environment == (
        ("GH_TOKEN", "${AWF_GITHUB_TOKEN}"),
        ("GITHUB_TOKEN", "${AWF_GITHUB_TOKEN}"),
    )
    rendered = json.dumps(
        {
            "resolution": repr(resolution),
            "metadata": resolution.metadata,
        },
        default=str,
    )
    assert raw_secret not in rendered
    assert "lower-priority-token" not in rendered
    assert resolution.satisfied_legacy_targets == frozenset()
    assert resolution.satisfied_legacy_providers == frozenset({"github"})


@pytest.mark.unit
def test_github_provider_rejects_unrelated_env_target_without_secret_value(
    tmp_path: Path,
) -> None:
    raw_secret = "ghp_do_not_render"
    resolver = _resolver(tmp_path, host_env={"AWF_GITHUB_TOKEN": raw_secret})

    with pytest.raises(SecretLeaseResolutionError) as raised:
        resolver.resolve(
            _profile(
                {
                    "name": "github",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "provider": "github",
                    "ref": "token",
                }
            ),
            workspace_id="ws_secret",
        )

    assert raised.value.reason_code == "SECRET_LEASE_TARGET_MISMATCH"
    assert raised.value.target == "OPENAI_API_KEY"
    assert raw_secret not in str(raised.value)


@pytest.mark.unit
def test_local_file_provider_produces_exact_read_only_mount(tmp_path: Path) -> None:
    secret_file = tmp_path / "credentials.json"
    secret_file.write_text('{"token": "do-not-render"}\n', encoding="utf-8")
    resolver = _resolver(tmp_path)

    resolution = resolver.resolve(
        _profile(
            {
                "name": "gcp-credentials",
                "kind": "mount",
                "target": "/run/awf/secrets/gcp/credentials.json",
                "provider": "host-file",
                "ref": str(secret_file),
            }
        ),
        workspace_id="ws_secret",
    )

    assert resolution.mounts == (
        AuthMount(
            source=str(secret_file),
            target="/run/awf/secrets/gcp/credentials.json",
            mode="ro",
        ),
    )
    assert resolution.environment == ()
    assert "do-not-render" not in json.dumps(resolution.metadata, default=str)
    assert resolution.metadata["mount_count"] == 1
    assert resolution.metadata["targets"] == ["/run/awf/secrets/gcp/credentials.json"]


@pytest.mark.unit
def test_local_file_provider_rejects_existing_directory_source(tmp_path: Path) -> None:
    secret_directory = tmp_path / "credentials.d"
    secret_directory.mkdir()
    resolver = _resolver(tmp_path)

    with pytest.raises(SecretLeaseResolutionError) as raised:
        resolver.resolve(
            _profile(
                {
                    "name": "gcp-credentials",
                    "kind": "mount",
                    "target": "/run/awf/secrets/gcp/credentials.json",
                    "provider": "host-file",
                    "ref": str(secret_directory),
                }
            ),
            workspace_id="ws_secret",
        )

    assert raised.value.reason_code == "SECRET_LEASE_SOURCE_INVALID"
    assert str(secret_directory) not in str(raised.value)


@pytest.mark.unit
def test_local_auth_provider_mounts_known_read_only_host_auth_path(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    gh_config = host_home / ".config" / "gh"
    gh_config.mkdir(parents=True)
    resolver = LocalSecretLeaseMountResolver(
        host_home=host_home,
        work_dir=tmp_path / "work",
        host_env={},
    )

    resolution = resolver.resolve(
        _profile(
            {
                "name": "gh-config",
                "kind": "mount",
                "target": "/home/agent/.config/gh",
                "provider": "local-auth",
                "ref": ".config/gh",
            }
        ),
        workspace_id="ws_secret",
    )

    assert resolution.mounts == (
        AuthMount(
            source=str(gh_config),
            target="/home/agent/.config/gh",
            mode="ro",
        ),
    )
    assert resolution.satisfied_legacy_targets == frozenset({"/home/agent/.config/gh"})
    assert resolution.satisfied_legacy_providers == frozenset({"github"})
    assert resolution.metadata["providers"] == ["local-auth"]
    assert resolution.metadata["targets"] == ["/home/agent/.config/gh"]


@pytest.mark.unit
def test_optional_missing_source_is_omitted_with_sanitized_metadata(tmp_path: Path) -> None:
    resolver = _resolver(tmp_path)

    resolution = resolver.resolve(
        _profile(
            {
                "name": "optional-openai",
                "kind": "env",
                "target": "OPENAI_API_KEY",
                "provider": "env",
                "ref": "env/OPENAI_API_KEY",
                "required": False,
            }
        ),
        workspace_id="ws_secret",
    )

    assert resolution.environment == ()
    assert resolution.mounts == ()
    assert resolution.metadata["omitted_optional_count"] == 1
    assert resolution.metadata["omitted_optional"] == [
        {
            "secret_name": "optional-openai",
            "provider": "env",
            "target": "OPENAI_API_KEY",
            "kind": "env",
            "reason_code": "SECRET_LEASE_SOURCE_MISSING",
        },
    ]
    assert "OPENAI_API_KEY" in json.dumps(resolution.metadata, default=str)
    assert "env/OPENAI_API_KEY" not in json.dumps(resolution.metadata, default=str)


@pytest.mark.unit
def test_missing_required_source_raises_structured_error_without_ref(
    tmp_path: Path,
) -> None:
    resolver = _resolver(tmp_path)

    with pytest.raises(SecretLeaseResolutionError) as raised:
        resolver.resolve(
            _profile(
                {
                    "name": "openai",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "provider": "env",
                    "ref": "env/OPENAI_API_KEY",
                }
            ),
            workspace_id="ws_secret",
        )

    assert raised.value.reason_code == "SECRET_LEASE_SOURCE_MISSING"
    assert raised.value.secret_name == "openai"
    assert "env/OPENAI_API_KEY" not in str(raised.value)
    assert "sk-" not in str(raised.value)


@pytest.mark.unit
def test_unsupported_provider_raises_structured_error_without_ref(
    tmp_path: Path,
) -> None:
    resolver = _resolver(tmp_path)

    with pytest.raises(SecretLeaseResolutionError) as raised:
        resolver.resolve(
            _profile(
                {
                    "name": "vault-token",
                    "kind": "env",
                    "target": "VAULT_TOKEN",
                    "provider": "vault",
                    "ref": "kv/data/prod/token",
                }
            ),
            workspace_id="ws_secret",
        )

    assert raised.value.reason_code == "SECRET_LEASE_PROVIDER_UNSUPPORTED"
    assert raised.value.provider == "vault"
    assert "kv/data/prod/token" not in str(raised.value)


@pytest.mark.unit
@pytest.mark.parametrize("ref", ["~", "${HOME}", "${AWF_HOST_HOME}", "/home/alice", "/Users/alice"])
def test_broad_local_file_sources_are_rejected_without_echoing_ref(
    tmp_path: Path,
    ref: str,
) -> None:
    resolver = _resolver(tmp_path)

    with pytest.raises(SecretLeaseResolutionError) as raised:
        resolver.resolve(
            _profile(
                {
                    "name": "local-file",
                    "kind": "mount",
                    "target": "/run/awf/secrets/local-file",
                    "provider": "local-file",
                    "ref": ref,
                }
            ),
            workspace_id="ws_secret",
        )

    assert raised.value.reason_code == "SECRET_LEASE_SOURCE_TOO_BROAD"
    assert ref not in str(raised.value)


@pytest.mark.unit
def test_writable_declared_local_auth_mount_is_rejected(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    (host_home / ".config" / "gh").mkdir(parents=True)
    resolver = LocalSecretLeaseMountResolver(
        host_home=host_home,
        work_dir=tmp_path / "work",
        host_env={},
    )

    with pytest.raises(SecretLeaseResolutionError) as raised:
        resolver.resolve(
            _profile(
                {
                    "name": "gh-config",
                    "kind": "mount",
                    "target": "/home/agent/.config/gh",
                    "provider": "local-auth",
                    "ref": ".config/gh",
                    "mode": "rw",
                }
            ),
            workspace_id="ws_secret",
        )

    assert raised.value.reason_code == "SECRET_LEASE_WRITABLE_UNSUPPORTED"


@pytest.mark.unit
def test_resolver_uses_os_environ_when_host_env_is_not_injected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-from-process-env")
    resolver = LocalSecretLeaseMountResolver(
        host_home=tmp_path / "host-home",
        work_dir=tmp_path / "work",
    )

    resolution = resolver.resolve(
        _profile(
            {
                "name": "openai",
                "kind": "env",
                "target": "OPENAI_API_KEY",
                "provider": "env",
                "ref": "OPENAI_API_KEY",
            }
        ),
        workspace_id="ws_secret",
    )

    assert resolution.environment == (("OPENAI_API_KEY", "${OPENAI_API_KEY}"),)
    assert "sk-live-from-process-env" not in repr(resolution)


@pytest.mark.unit
def test_unresolved_providerless_declarations_are_skipped_for_compatibility(
    tmp_path: Path,
) -> None:
    resolver = _resolver(tmp_path)

    resolution = resolver.resolve(
        _profile(
            {
                "name": "metadata-only",
                "kind": "env",
                "target": "METADATA_ONLY",
            },
            {
                "name": "ref-only",
                "kind": "mount",
                "target": "/run/awf/secrets/ref-only",
                "ref": "metadata/ref",
            },
        ),
        workspace_id="ws_secret",
    )

    assert resolution.environment == ()
    assert resolution.mounts == ()
    assert resolution.metadata["skipped_unresolved_count"] == 2


@pytest.mark.unit
def test_duplicate_env_lease_placeholders_are_deduplicated(tmp_path: Path) -> None:
    resolver = _resolver(
        tmp_path,
        host_env={"OPENAI_API_KEY": "sk-live-one", "CODEX_API_KEY": "sk-live-two"},
    )

    resolution = resolver.resolve(
        _profile(
            {
                "name": "openai",
                "kind": "env",
                "target": "OPENAI_API_KEY",
                "provider": "env",
                "ref": "OPENAI_API_KEY",
            },
            {
                "name": "codex",
                "kind": "env",
                "target": "OPENAI_API_KEY",
                "provider": "env",
                "ref": "CODEX_API_KEY",
            },
        ),
        workspace_id="ws_secret",
    )

    assert resolution.environment == (("OPENAI_API_KEY", "${OPENAI_API_KEY}"),)
    assert resolution.metadata["providers"] == ["env"]
    assert resolution.metadata["targets"] == ["OPENAI_API_KEY"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "reason_code"),
    [
        (
            {
                "name": "env-as-mount",
                "kind": "mount",
                "target": "/run/awf/secrets/env",
                "provider": "env",
                "ref": "OPENAI_API_KEY",
            },
            "SECRET_LEASE_TARGET_KIND_MISMATCH",
        ),
        (
            {
                "name": "env-missing-ref",
                "kind": "env",
                "target": "OPENAI_API_KEY",
                "provider": "env",
            },
            "SECRET_LEASE_SOURCE_INVALID",
        ),
        (
            {
                "name": "env-invalid-ref",
                "kind": "env",
                "target": "OPENAI_API_KEY",
                "provider": "env",
                "ref": "not-valid/ref",
            },
            "SECRET_LEASE_SOURCE_INVALID",
        ),
        (
            {
                "name": "github-as-mount",
                "kind": "mount",
                "target": "/run/awf/secrets/github",
                "provider": "github",
                "ref": "token",
            },
            "SECRET_LEASE_TARGET_KIND_MISMATCH",
        ),
        (
            {
                "name": "file-as-env",
                "kind": "env",
                "target": "FILE_SECRET",
                "provider": "host-file",
                "ref": "/var/lib/awf/secret",
            },
            "SECRET_LEASE_TARGET_KIND_MISMATCH",
        ),
        (
            {
                "name": "file-blank-ref",
                "kind": "mount",
                "target": "/run/awf/secrets/file",
                "provider": "host-file",
                "ref": " ",
            },
            "SECRET_LEASE_SOURCE_INVALID",
        ),
        (
            {
                "name": "file-relative-ref",
                "kind": "mount",
                "target": "/run/awf/secrets/file",
                "provider": "host-file",
                "ref": "relative/secret",
            },
            "SECRET_LEASE_SOURCE_INVALID",
        ),
        (
            {
                "name": "auth-as-env",
                "kind": "env",
                "target": "GH_CONFIG",
                "provider": "local-auth",
                "ref": ".config/gh",
            },
            "SECRET_LEASE_TARGET_KIND_MISMATCH",
        ),
        (
            {
                "name": "auth-missing-ref",
                "kind": "mount",
                "target": "/home/agent/.config/gh",
                "provider": "local-auth",
            },
            "SECRET_LEASE_SOURCE_INVALID",
        ),
        (
            {
                "name": "auth-broad-ref",
                "kind": "mount",
                "target": "/home/agent/.config/gh",
                "provider": "local-auth",
                "ref": "~/secret",
            },
            "SECRET_LEASE_SOURCE_TOO_BROAD",
        ),
        (
            {
                "name": "auth-unknown-ref",
                "kind": "mount",
                "target": "/home/agent/.config/gh",
                "provider": "local-auth",
                "ref": ".docker",
            },
            "SECRET_LEASE_SOURCE_INVALID",
        ),
        (
            {
                "name": "auth-double-prefixed-ref",
                "kind": "mount",
                "target": "/home/agent/.config/gh",
                "provider": "local-auth",
                "ref": "local-auth/auth/.config/gh",
            },
            "SECRET_LEASE_SOURCE_INVALID",
        ),
        (
            {
                "name": "auth-target-mismatch",
                "kind": "mount",
                "target": "/home/agent/.ssh",
                "provider": "local-auth",
                "ref": ".config/gh",
            },
            "SECRET_LEASE_TARGET_MISMATCH",
        ),
    ],
)
def test_invalid_local_lease_shapes_raise_structured_errors(
    tmp_path: Path,
    secret: dict[str, object],
    reason_code: str,
) -> None:
    resolver = _resolver(tmp_path, host_env={"OPENAI_API_KEY": "sk-live-do-not-render"})

    with pytest.raises(SecretLeaseResolutionError) as raised:
        resolver.resolve(_profile(secret), workspace_id="ws_secret")

    assert raised.value.reason_code == reason_code
    assert "sk-live-do-not-render" not in str(raised.value)


def _bitbucket_token_lease() -> dict[str, object]:
    return {
        "name": "bitbucket-token",
        "kind": "env",
        "target": "BITBUCKET_API_TOKEN",
        "provider": "bitbucket",
        "ref": "token",
    }


def _bitbucket_email_lease() -> dict[str, object]:
    return {
        "name": "bitbucket-email",
        "kind": "env",
        "target": "BITBUCKET_EMAIL",
        "provider": "bitbucket",
        "ref": "email",
    }


@pytest.mark.unit
def test_bitbucket_provider_exposes_token_and_email_placeholders(tmp_path: Path) -> None:
    raw_token = "ATATT-do-not-render"
    raw_email = "agent@example.com"
    resolver = _resolver(
        tmp_path,
        host_env={"BITBUCKET_API_TOKEN": raw_token, "BITBUCKET_EMAIL": raw_email},
    )

    resolution = resolver.resolve(
        _profile(_bitbucket_token_lease(), _bitbucket_email_lease()),
        workspace_id="ws_secret",
    )

    env = dict(resolution.environment)
    # The token/email leases stay compose ``${VAR}`` placeholders, never values.
    assert env["BITBUCKET_API_TOKEN"] == "${BITBUCKET_API_TOKEN}"
    assert env["BITBUCKET_EMAIL"] == "${BITBUCKET_EMAIL}"
    # The git token lease additionally wires agent-side git-over-HTTPS auth.
    assert env["GIT_ASKPASS"] == "/run/awf/secrets/bb-askpass.sh"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    instead_of_key = "url.https://x-bitbucket-api-token-auth@bitbucket.org/.insteadOf"
    count = int(env["GIT_CONFIG_COUNT"])
    config = {(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)}
    assert (instead_of_key, "https://bitbucket.org/") in config
    assert (instead_of_key, "git@bitbucket.org:") in config
    assert (instead_of_key, "ssh://git@bitbucket.org/") in config
    # The askpass script is mounted read-only into the agent.
    assert (
        AuthMount(
            source=str(tmp_path / "work" / "secret-leases" / "ws_secret" / "bb-askpass.sh"),
            target="/run/awf/secrets/bb-askpass.sh",
            mode="ro",
        )
        in resolution.mounts
    )
    rendered = json.dumps(
        {"resolution": repr(resolution), "metadata": resolution.metadata},
        default=str,
    )
    assert raw_token not in rendered
    assert resolution.metadata["providers"] == ["bitbucket"]
    # ``targets`` tracks profile-declared lease targets; the AWF-internal askpass
    # mount is reflected by ``mount_count`` only, not as a declared target.
    assert resolution.metadata["targets"] == ["BITBUCKET_API_TOKEN", "BITBUCKET_EMAIL"]
    assert resolution.metadata["mount_count"] == 1
    # ``env_count`` stays a faithful declared-lease proxy (the two token/email
    # leases) and excludes the AWF-internal git-auth env vars (GIT_ASKPASS,
    # GIT_TERMINAL_PROMPT, and the GIT_CONFIG_* insteadOf entries), which inflate
    # ``total_env_count`` only.
    assert resolution.metadata["env_count"] == 2
    assert resolution.metadata["total_env_count"] == len(env)
    assert resolution.metadata["total_env_count"] > resolution.metadata["env_count"]


@pytest.mark.unit
def test_bitbucket_token_only_lease_still_wires_agent_git_auth(tmp_path: Path) -> None:
    # The agent askpass needs only the token; the sentinel username replaces the
    # email for git, so a token-only lease (no BITBUCKET_EMAIL) still wires auth.
    raw_token = "ATATT-do-not-render"
    resolver = _resolver(tmp_path, host_env={"BITBUCKET_API_TOKEN": raw_token})

    resolution = resolver.resolve(
        _profile(_bitbucket_token_lease()),
        workspace_id="ws_secret",
    )

    env = dict(resolution.environment)
    assert env["GIT_ASKPASS"] == "/run/awf/secrets/bb-askpass.sh"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_CONFIG_COUNT" in env
    assert resolution.metadata["mount_count"] == 1
    assert raw_token not in json.dumps(
        {"resolution": repr(resolution), "metadata": resolution.metadata}, default=str
    )


@pytest.mark.unit
def test_bitbucket_email_only_lease_does_not_wire_agent_git_auth(tmp_path: Path) -> None:
    # The email lease feeds REST basic auth only; it must not trigger the agent
    # git askpass wiring (which is gated strictly on the token target).
    resolver = _resolver(tmp_path, host_env={"BITBUCKET_EMAIL": "agent@example.com"})

    resolution = resolver.resolve(
        _profile(_bitbucket_email_lease()),
        workspace_id="ws_secret",
    )

    env = dict(resolution.environment)
    assert env == {"BITBUCKET_EMAIL": "${BITBUCKET_EMAIL}"}
    assert "GIT_ASKPASS" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert resolution.mounts == ()
    # No git wiring, so the declared-lease count equals the full env count.
    assert resolution.metadata["env_count"] == 1
    assert resolution.metadata["total_env_count"] == 1
    assert resolution.metadata["mount_count"] == 0


@pytest.mark.unit
def test_bitbucket_token_lease_skips_git_auth_when_profile_presets_git_askpass(
    tmp_path: Path,
) -> None:
    # When the profile already declares GIT_ASKPASS in runtime.environment, the
    # StackLauncher merge keeps the profile's value (merge_agent_environment does
    # not clobber existing keys). If the resolver still wired its own askpass +
    # insteadOf rewrites, Git would rewrite bitbucket.org URLs to require an
    # askpass password but invoke the profile's unrelated GIT_ASKPASS, breaking
    # private Bitbucket fetch/push. So when the profile presets GIT_ASKPASS the
    # resolver must NOT wire the agent git-auth block (no GIT_ASKPASS override,
    # no insteadOf rewrites, no askpass mount) — it leaves the token placeholder
    # for the profile's own askpass to consume.
    resolver = _resolver(tmp_path, host_env={"BITBUCKET_API_TOKEN": "ATATT-do-not-render"})

    resolution = resolver.resolve(
        _profile_with_runtime_env(
            {"GIT_ASKPASS": "/profile/askpass.sh"},
            _bitbucket_token_lease(),
        ),
        workspace_id="ws_secret",
    )

    env = dict(resolution.environment)
    # The token lease placeholder is still emitted for the profile's askpass.
    assert env == {"BITBUCKET_API_TOKEN": "${BITBUCKET_API_TOKEN}"}
    # No AWF-internal git-auth wiring: the resolver did not override GIT_ASKPASS,
    # emit insteadOf rewrites, or mount the bitbucket askpass script.
    assert "GIT_ASKPASS" not in env
    assert "GIT_TERMINAL_PROMPT" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert resolution.mounts == ()
    assert resolution.metadata["mount_count"] == 0
    assert resolution.metadata["env_count"] == 1
    assert resolution.metadata["total_env_count"] == 1


@pytest.mark.unit
def test_bitbucket_askpass_script_materialized_read_only_executable_and_static(
    tmp_path: Path,
) -> None:
    raw_token = "ATATT-do-not-render"
    resolver = _resolver(tmp_path, host_env={"BITBUCKET_API_TOKEN": raw_token})

    resolution = resolver.resolve(
        _profile(_bitbucket_token_lease()),
        workspace_id="ws_secret",
    )

    askpass_mount = next(
        m for m in resolution.mounts if m.target == "/run/awf/secrets/bb-askpass.sh"
    )
    assert askpass_mount.mode == "ro"
    script_path = Path(askpass_mount.source)
    # Host file is world read+execute (0o555) so the agent can run the ro mount.
    assert script_path.stat().st_mode & 0o111
    assert (script_path.stat().st_mode & 0o777) == 0o555
    content = script_path.read_text(encoding="utf-8")
    # Static script: references the injected env-var name, never the token value.
    assert content.startswith("#!/bin/sh")
    assert '"$BITBUCKET_API_TOKEN"' in content
    assert raw_token not in content


@pytest.mark.unit
def test_bitbucket_askpass_rematerializes_after_chmod_on_reprovision(
    tmp_path: Path,
) -> None:
    # The first materialization writes the script and marks it 0o555 (no write
    # bits). A later resolve for the same workspace (reprovision / stack
    # relaunch) must rewrite the read-only script in place rather than raising
    # PermissionError, matching the idempotent ``mkdir(exist_ok=True)`` intent.
    resolver = _resolver(tmp_path, host_env={"BITBUCKET_API_TOKEN": "ATATT-do-not-render"})
    profile = _profile(_bitbucket_token_lease())

    first = resolver.resolve(profile, workspace_id="ws_secret")
    script_path = Path(
        next(m for m in first.mounts if m.target == "/run/awf/secrets/bb-askpass.sh").source
    )
    assert (script_path.stat().st_mode & 0o777) == 0o555

    # Second resolve must not raise even though the file is non-writable.
    second = resolver.resolve(profile, workspace_id="ws_secret")
    assert any(m.target == "/run/awf/secrets/bb-askpass.sh" for m in second.mounts)
    assert (script_path.stat().st_mode & 0o777) == 0o555
    assert script_path.read_text(encoding="utf-8").startswith("#!/bin/sh")


@pytest.mark.unit
def test_non_bitbucket_lease_does_not_wire_agent_git_auth(tmp_path: Path) -> None:
    # A non-bitbucket (env/OpenAI) lease yields none of the agent git wiring.
    resolver = _resolver(tmp_path, host_env={"OPENAI_API_KEY": "sk-live-do-not-render"})

    resolution = resolver.resolve(
        _profile(
            {
                "name": "openai",
                "kind": "env",
                "target": "OPENAI_API_KEY",
                "provider": "env",
                "ref": "env/OPENAI_API_KEY",
            }
        ),
        workspace_id="ws_secret",
    )

    env = dict(resolution.environment)
    assert "GIT_ASKPASS" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert resolution.mounts == ()


@pytest.mark.unit
def test_bitbucket_provider_rejects_unrelated_target(tmp_path: Path) -> None:
    raw_token = "ATATT-do-not-render"
    resolver = _resolver(tmp_path, host_env={"BITBUCKET_API_TOKEN": raw_token})

    with pytest.raises(SecretLeaseResolutionError) as raised:
        resolver.resolve(
            _profile(
                {
                    "name": "bitbucket",
                    "kind": "env",
                    "target": "GH_TOKEN",
                    "provider": "bitbucket",
                    "ref": "token",
                }
            ),
            workspace_id="ws_secret",
        )

    assert raised.value.reason_code == "SECRET_LEASE_TARGET_MISMATCH"
    assert raw_token not in str(raised.value)


@pytest.mark.unit
def test_bitbucket_provider_rejects_non_env_kind(tmp_path: Path) -> None:
    resolver = _resolver(tmp_path, host_env={"BITBUCKET_API_TOKEN": "ATATT-x"})

    with pytest.raises(SecretLeaseResolutionError) as raised:
        resolver.resolve(
            _profile(
                {
                    "name": "bitbucket",
                    "kind": "mount",
                    "target": "BITBUCKET_API_TOKEN",
                    "provider": "bitbucket",
                    "ref": "token",
                }
            ),
            workspace_id="ws_secret",
        )

    assert raised.value.reason_code == "SECRET_LEASE_TARGET_KIND_MISMATCH"


@pytest.mark.unit
def test_bitbucket_provider_required_missing_source_raises(tmp_path: Path) -> None:
    resolver = _resolver(tmp_path, host_env={})

    with pytest.raises(SecretLeaseResolutionError) as raised:
        resolver.resolve(
            _profile(
                {
                    "name": "bitbucket",
                    "kind": "env",
                    "target": "BITBUCKET_API_TOKEN",
                    "provider": "bitbucket",
                    "ref": "token",
                }
            ),
            workspace_id="ws_secret",
        )

    assert raised.value.reason_code == "SECRET_LEASE_SOURCE_MISSING"


@pytest.mark.unit
def test_bitbucket_provider_optional_missing_source_is_omitted(tmp_path: Path) -> None:
    resolver = _resolver(tmp_path, host_env={})

    resolution = resolver.resolve(
        _profile(
            {
                "name": "bitbucket",
                "kind": "env",
                "target": "BITBUCKET_API_TOKEN",
                "provider": "bitbucket",
                "ref": "token",
                "required": False,
            }
        ),
        workspace_id="ws_secret",
    )

    assert resolution.environment == ()
    assert resolution.metadata["omitted_optional_count"] == 1


@pytest.mark.unit
def test_optional_missing_github_and_mount_sources_are_omitted(
    tmp_path: Path,
) -> None:
    resolver = _resolver(tmp_path)

    resolution = resolver.resolve(
        _profile(
            {
                "name": "github",
                "kind": "env",
                "target": "GH_TOKEN",
                "provider": "github",
                "ref": "token",
                "required": False,
            },
            {
                "name": "host-file",
                "kind": "mount",
                "target": "/run/awf/secrets/file",
                "provider": "host-file",
                "ref": str(tmp_path / "missing-file"),
                "required": False,
            },
            {
                "name": "gh-config",
                "kind": "mount",
                "target": "/home/agent/.config/gh",
                "provider": "local-auth",
                "ref": ".config/gh",
                "required": False,
            },
        ),
        workspace_id="ws_secret",
    )

    assert resolution.environment == ()
    assert resolution.mounts == ()
    assert resolution.metadata["omitted_optional_count"] == 3
    assert [item["secret_name"] for item in resolution.metadata["omitted_optional"]] == [
        "github",
        "host-file",
        "gh-config",
    ]


@pytest.mark.unit
def test_local_auth_can_mount_known_ref_under_safe_secret_target(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    (host_home / ".ssh").mkdir(parents=True)
    resolver = LocalSecretLeaseMountResolver(
        host_home=host_home,
        work_dir=tmp_path / "work",
        host_env={},
    )

    resolution = resolver.resolve(
        _profile(
            {
                "name": "ssh-secret",
                "kind": "mount",
                "target": "/run/awf/secrets/ssh",
                "provider": "auth",
                "ref": "auth/ssh",
            }
        ),
        workspace_id="ws_secret",
    )

    assert resolution.mounts == (
        AuthMount(source=str(host_home / ".ssh"), target="/run/awf/secrets/ssh", mode="ro"),
    )
    assert resolution.satisfied_legacy_targets == frozenset()
