"""Focused coverage for hosted Compose safety and credential edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.profiles import compose as compose_module
from awf.profiles import compose_git_config


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "SERVICE_TOKEN_ENDPOINT",
        "SERVICE_APIKEY_URL",
        "PAYMENTS_PWD_ENDPOINT",
    ],
)
def test_secret_like_endpoint_names_cover_token_shapes(name: str) -> None:
    """Token, concatenated-key, and abbreviated-key endpoint names stay secret-like."""

    assert compose_module._is_secret_like_profile_env_name(name) is True


@pytest.mark.unit
def test_url_component_falls_back_to_raw_fields_when_query_parsing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed query parsing still redacts credential fields from raw components."""

    def _raise(*_args: object, **_kwargs: object) -> list[tuple[str, str]]:
        raise ValueError("malformed query")

    monkeypatch.setattr(compose_module, "parse_qsl", _raise)

    assert (
        compose_module._url_component_has_secret_credential_field("safe=1;token=profile-secret")
        is True
    )


@pytest.mark.unit
def test_nested_and_relative_url_credential_helpers_reject_only_credentials() -> None:
    """Nested query values and relative URLs redact credentials without false positives."""

    assert compose_module._url_query_value_has_secret_credential_field("") is False
    assert compose_module._url_query_value_has_secret_credential_field("public-value") is False
    assert compose_module._url_query_value_has_secret_credential_field("token=secret") is True
    assert compose_module._relative_url_value_has_secret_credential_field("") is False
    assert compose_module._relative_url_value_has_secret_credential_field("relative/path") is False
    assert (
        compose_module._relative_url_value_has_secret_credential_field(
            "https://example.test/callback?token=secret"
        )
        is False
    )
    assert (
        compose_module._relative_url_value_has_secret_credential_field("/callback?token=secret")
        is True
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"services": []},
        {"services": {"agent": []}},
        {"services": {"agent": {"volumes": {"source": "target"}}}},
    ],
)
def test_hosted_file_auth_mount_targets_rejects_invalid_compose_shapes(
    tmp_path: Path,
    payload: object,
) -> None:
    """Hosted mount extraction treats malformed Compose shapes as having no mounts."""

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(yaml.safe_dump(payload), encoding="utf-8")

    assert compose_module.hosted_file_auth_mount_targets(compose_file) == ()


@pytest.mark.unit
def test_hosted_file_auth_mount_targets_rejects_unreadable_yaml(tmp_path: Path) -> None:
    """Non-UTF-8 Compose files cannot leak host mount metadata."""

    compose_file = tmp_path / "compose.yml"
    compose_file.write_bytes(b"\xff")

    assert compose_module.hosted_file_auth_mount_targets(compose_file) == ()


@pytest.mark.unit
def test_hosted_file_auth_mount_targets_supports_short_long_and_adc_mounts(
    tmp_path: Path,
) -> None:
    """Supported provider targets preserve order, deduplicate, and include dynamic ADC."""

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "environment": {
                            "GOOGLE_APPLICATION_CREDENTIALS": "/secrets/adc.json",
                        },
                        "volumes": [
                            "/host/codex:/home/agent/.codex:ro",
                            {"target": "/home/agent/.claude"},
                            {"dst": "/secrets/adc.json"},
                            {"destination": "/home/agent/.ssh"},
                            "/duplicate:/home/agent/.codex:ro",
                            "not-a-mount",
                            42,
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert compose_module.hosted_file_auth_mount_targets(
        compose_file,
        compose_env={"GOOGLE_APPLICATION_CREDENTIALS": "/secrets/adc.json"},
        worker_env={},
    ) == (
        "/home/agent/.codex",
        "/home/agent/.claude",
        "/secrets/adc.json",
        "/home/agent/.ssh",
    )
    assert compose_module.hosted_file_auth_mount_targets(
        compose_file,
        worker_env={},
    ) == (
        "/home/agent/.codex",
        "/home/agent/.claude",
        "/secrets/adc.json",
        "/home/agent/.ssh",
    )


@pytest.mark.unit
def test_hosted_file_auth_mount_targets_includes_hosted_placeholder_mounts(
    tmp_path: Path,
) -> None:
    """Rendered hosted placeholder sources surface custom file-auth targets."""

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "volumes": [
                            (
                                "/run/awf/hosted-auth-placeholders/run__secrets__npmrc"
                                ":/run/secrets/npmrc:ro"
                            ),
                            {
                                "type": "bind",
                                "source": (
                                    "/run/awf/hosted-auth-placeholders/run__secrets__pypirc"
                                ),
                                "target": "/run/secrets/pypirc",
                                "read_only": True,
                            },
                            "/host/custom:/run/secrets/not-hosted:ro",
                            "/duplicate:/run/secrets/npmrc:ro",
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert compose_module.hosted_file_auth_mount_targets(compose_file) == (
        "/run/secrets/npmrc",
        "/run/secrets/pypirc",
    )


@pytest.mark.unit
def test_hosted_google_credentials_mount_targets_resolves_supported_forms() -> None:
    """ADC mount discovery accepts absolute literals, pass-through, and aliases only."""

    resolver = compose_module._hosted_google_application_credentials_mount_targets
    assert resolver(None, worker_env={}) == frozenset()
    assert resolver({}, worker_env={}) == frozenset()
    assert resolver(
        {"GOOGLE_APPLICATION_CREDENTIALS": compose_module._COMPOSE_PASSTHROUGH},
        worker_env={"GOOGLE_APPLICATION_CREDENTIALS": "/worker/adc.json"},
    ) == frozenset({"/worker/adc.json"})
    assert resolver(
        {"GOOGLE_APPLICATION_CREDENTIALS": "${ADC_PATH}"},
        worker_env={"ADC_PATH": "/worker/aliased-adc.json"},
    ) == frozenset({"/worker/aliased-adc.json"})
    assert resolver(
        {"GOOGLE_APPLICATION_CREDENTIALS": "/profile/adc.json"},
        worker_env={},
    ) == frozenset({"/profile/adc.json"})
    assert (
        resolver(
            {"GOOGLE_APPLICATION_CREDENTIALS": "relative/adc.json"},
            worker_env={},
        )
        == frozenset()
    )


@pytest.mark.unit
def test_hosted_compose_metadata_helpers_handle_missing_files(tmp_path: Path) -> None:
    """Missing Compose files yield empty mount, pass-through, and alias metadata."""

    missing = tmp_path / "missing-compose.yml"
    assert compose_module.hosted_file_auth_mount_targets(missing) == ()
    assert compose_module.hosted_profile_env_passthrough_names(missing) == ()
    assert compose_module.hosted_profile_env_passthrough_aliases(missing) == ()


@pytest.mark.unit
def test_compose_volume_target_handles_all_supported_shapes() -> None:
    """Short syntax and each Compose long-syntax target alias are normalized."""

    assert compose_module._compose_volume_target("host-only") is None
    assert compose_module._compose_volume_target("host:/container:ro") == "/container"
    assert compose_module._compose_volume_target({"target": "/target"}) == "/target"
    assert compose_module._compose_volume_target({"dst": "/dst"}) == "/dst"
    assert compose_module._compose_volume_target({"destination": "/destination"}) == (
        "/destination"
    )
    assert compose_module._compose_volume_target({"target": 42}) is None
    assert compose_module._compose_volume_target(42) is None


@pytest.mark.unit
def test_compose_volume_source_handles_supported_shapes() -> None:
    """Short syntax and supported long-syntax source aliases are normalized."""

    assert compose_module._compose_volume_source("host-only") is None
    assert compose_module._compose_volume_source("host:/container:ro") == "host"
    assert compose_module._compose_volume_source({"source": "/source"}) == "/source"
    assert compose_module._compose_volume_source({"src": "/src"}) == "/src"
    assert compose_module._compose_volume_source({"source": 42}) is None
    assert compose_module._compose_volume_source(42) is None


@pytest.mark.unit
def test_safe_ssh_git_config_insteadof_keys_reject_unsafe_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only credential-free SSH rewrites for the git user are hosted-safe."""

    checker = compose_git_config._is_safe_ssh_git_config_insteadof_key
    assert checker("user.name") is False
    assert checker("url.https://git@example.test.insteadOf") is False
    assert checker("url.ssh://user@example.test.insteadOf") is False
    assert checker("url.ssh://git:secret@example.test.insteadOf") is False
    assert checker("url.ssh://git@example.test?token=secret.insteadOf") is False
    assert checker("url.ssh://git@example.test.insteadOf") is True

    monkeypatch.setattr(
        compose_git_config,
        "urlsplit",
        lambda _value: (_ for _ in ()).throw(ValueError("bad URL")),
    )
    assert checker("url.ssh://git@example.test.insteadOf") is False


@pytest.mark.unit
def test_hosted_git_config_alias_source_requires_resolvable_nonempty_source() -> None:
    """Git-config aliases are emitted only for selected worker-resolved sources."""

    resolve = compose_git_config._hosted_git_config_value_alias_source
    resolution = compose_module._ComposeEnvResolution
    assert resolve("literal", value_resolution=resolution.LITERAL, worker_env={}) is None
    assert (
        resolve(
            "${SOURCE+}",
            value_resolution=resolution.WORKER_RESOLVED_SLOT,
            worker_env={"SOURCE": "value"},
        )
        is None
    )
    assert (
        resolve(
            "${SOURCE}",
            value_resolution=resolution.WORKER_RESOLVED_SLOT,
            worker_env={},
        )
        is None
    )
    assert (
        resolve(
            "${SOURCE}",
            value_resolution=resolution.WORKER_RESOLVED_SLOT,
            worker_env={"SOURCE": "value"},
        )
        == "SOURCE"
    )
