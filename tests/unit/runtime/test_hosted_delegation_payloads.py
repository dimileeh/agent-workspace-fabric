"""Hosted delegation payload sanitization edge tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation_payloads import (
    _hosted_validation_attach_rendered_stack,
    _hosted_validation_omit_environment_entries,
    _hosted_validation_profile_payload,
    _hosted_validation_rendered_stack_payload,
)

_DNS1123_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")


def _assert_dns1123_label(value: str) -> None:
    assert len(value) <= 63
    assert _DNS1123_LABEL_PATTERN.fullmatch(value) is not None


@pytest.mark.unit
def test_rendered_stack_payload_omits_unreadable_compose_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unreadable local Compose metadata must not fail hosted delegation."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    def _raise_read_text(
        self: Path,
        *args: object,
        **kwargs: object,
    ) -> str:
        del args, kwargs
        if self == compose_file:
            raise OSError("permission denied for secret-token-value")
        return Path.read_text(self, encoding="utf-8")

    monkeypatch.setattr(Path, "read_text", _raise_read_text)

    assert (
        _hosted_validation_rendered_stack_payload(
            compose_project="awf_ws_hosted",
            compose_file=compose_file,
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "compose_text",
    [
        "services: [\n",
        "- not-a-compose-document\n",
    ],
)
def test_rendered_stack_payload_omits_malformed_or_nonmapping_compose(
    tmp_path: Path,
    compose_text: str,
) -> None:
    """Malformed or non-object Compose data is optional hosted metadata."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(compose_text, encoding="utf-8")

    assert (
        _hosted_validation_rendered_stack_payload(
            compose_project="awf_ws_hosted",
            compose_file=compose_file,
        )
        is None
    )


@pytest.mark.unit
def test_rendered_stack_payload_accepts_nonmapping_services(tmp_path: Path) -> None:
    """A malformed services block yields an empty service map, not raw data."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services: []
volumes:
  pgdata: {}
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )

    assert payload is not None
    assert payload["services"] == {}
    assert payload["volumes"] == {"pgdata": {}}


@pytest.mark.unit
def test_rendered_stack_payload_normalizes_postgres_data_named_volume(
    tmp_path: Path,
) -> None:
    """Hosted rendered stacks use DNS-1123 volume names for the GKE failure case."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    volumes:
      - postgres_data:/var/lib/postgresql/data
  agent:
    image: awf-agent-runtime:latest
    volumes:
      - postgres_data:/ignored-agent-mount
volumes:
  postgres_data:
    name: awf-ws_hosted-postgres_data
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )

    assert payload is not None
    assert payload["schema"] == "hosted_validation_rendered_stack.v1"
    assert payload["compose_project"] == "awf_ws_hosted"
    assert payload["compose_file_path"] == str(compose_file)
    assert payload["volumes"] == {"postgres-data": {"name": "awf-ws-hosted-postgres-data"}}
    assert payload["services"] == {
        "postgres": {
            "image": "postgres:16",
            "volumes": ["postgres-data:/var/lib/postgresql/data"],
        }
    }
    body = json.dumps(payload, sort_keys=True)
    assert "postgres_data" not in body


@pytest.mark.unit
def test_rendered_stack_payload_uses_one_volume_translation_for_supported_shapes(
    tmp_path: Path,
) -> None:
    """Top-level declarations and supported service refs cannot diverge."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    image: backend:latest
    volumes:
      - cache_data:/cache
      - cache_data:${CACHE_DIR}
      - ./host-cache:/cache-host
      - ${HOST_CACHE}:/cache-env
      - type: volume
        source: cache_data
        target: /cache-long
      - type: volume
        src: cache_data
        target: /cache-src-long
      - source: other_data
        target: /other-long
      - src: other_data
        target: /other-src-long
      - source: ./host-cache-long
        target: /host-long
      - type: bind
        source: bind_data
        target: /bind
      - type: bind
        src: bind_data
        target: /bind-src
volumes:
  cache_data:
    labels:
      purpose: cache
  other_data: {}
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )

    assert payload is not None
    assert payload["volumes"] == {
        "cache-data": {"labels": {"purpose": "cache"}},
        "other-data": {},
    }
    assert payload["services"]["backend"]["volumes"] == [
        "cache-data:/cache",
        "cache-data:${CACHE_DIR}",
        "./host-cache:/cache-host",
        "${HOST_CACHE}:/cache-env",
        {"type": "volume", "source": "cache-data", "target": "/cache-long"},
        {"type": "volume", "src": "cache-data", "target": "/cache-src-long"},
        {"source": "other-data", "target": "/other-long"},
        {"src": "other-data", "target": "/other-src-long"},
        {"source": "./host-cache-long", "target": "/host-long"},
        {"type": "bind", "source": "bind_data", "target": "/bind"},
        {"type": "bind", "src": "bind_data", "target": "/bind-src"},
    ]


@pytest.mark.unit
def test_rendered_stack_payload_preserves_valid_dns1123_named_volume(
    tmp_path: Path,
) -> None:
    """Already valid hosted volume labels stay stable."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    volumes:
      - pg-data:/var/lib/postgresql/data
      - type: volume
        source: pg-data
        target: /backup
volumes:
  pg-data: {}
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )

    assert payload is not None
    assert payload["volumes"] == {"pg-data": {}}
    assert payload["services"]["postgres"]["volumes"] == [
        "pg-data:/var/lib/postgresql/data",
        {"type": "volume", "source": "pg-data", "target": "/backup"},
    ]


@pytest.mark.unit
def test_rendered_stack_payload_disambiguates_volume_normalization_collisions(
    tmp_path: Path,
) -> None:
    """Distinct Compose volume names must not alias after hosted normalization."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  underscore:
    image: postgres:16
    volumes:
      - pg_data:/data-underscore
  dot:
    image: postgres:16
    volumes:
      - pg.data:/data-dot
  valid:
    image: postgres:16
    volumes:
      - pg-data:/data-valid
volumes:
  pg_data: {}
  pg.data: {}
  pg-data: {}
""".lstrip(),
        encoding="utf-8",
    )

    first = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )
    second = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )

    assert first is not None
    assert second is not None
    assert first == second
    translated_names = set(first["volumes"])
    assert len(translated_names) == 3
    assert "pg-data" in translated_names
    for name in translated_names:
        _assert_dns1123_label(name)

    underscore_source = first["services"]["underscore"]["volumes"][0].partition(":")[0]
    dot_source = first["services"]["dot"]["volumes"][0].partition(":")[0]
    valid_source = first["services"]["valid"]["volumes"][0].partition(":")[0]
    assert valid_source == "pg-data"
    assert {underscore_source, dot_source, valid_source} == translated_names
    assert len({underscore_source, dot_source, valid_source}) == 3


@pytest.mark.unit
def test_rendered_stack_payload_disambiguates_long_volume_names(tmp_path: Path) -> None:
    """Long names are bounded without silently aliasing shared prefixes."""
    first_volume = f"shared_{'x' * 80}_alpha"
    second_volume = f"shared_{'x' * 80}_bravo"
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        f"""
services:
  first:
    image: postgres:16
    volumes:
      - {first_volume}:/data-first
  second:
    image: postgres:16
    volumes:
      - {second_volume}:/data-second
volumes:
  {first_volume}: {{}}
  {second_volume}: {{}}
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )

    assert payload is not None
    translated_names = set(payload["volumes"])
    assert len(translated_names) == 2
    for name in translated_names:
        _assert_dns1123_label(name)
    first_source = payload["services"]["first"]["volumes"][0].partition(":")[0]
    second_source = payload["services"]["second"]["volumes"][0].partition(":")[0]
    assert first_source in translated_names
    assert second_source in translated_names
    assert first_source != second_source
    body = json.dumps(payload, sort_keys=True)
    assert first_volume not in body
    assert second_volume not in body


@pytest.mark.unit
def test_rendered_stack_payload_volume_normalization_keeps_payload_secret_free(
    tmp_path: Path,
) -> None:
    """Volume-specific rewriting must still route service values through redaction."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    volumes:
      - postgres_data:/var/lib/postgresql/data
    environment:
      POSTGRES_PASSWORD: literal-postgres-secret
      DATABASE_URL: postgresql://user:literal-url-secret@postgres/awf
      PUBLIC_URL: http://postgres:5432
volumes:
  postgres_data: {}
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    assert payload["volumes"] == {"postgres-data": {}}
    assert payload["services"]["postgres"]["environment"] == {
        "PUBLIC_URL": "http://postgres:5432",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "${POSTGRES_PASSWORD}" not in body
    assert "DATABASE_URL" not in body
    assert "${DATABASE_URL}" not in body
    assert "literal-postgres-secret" not in body
    assert "literal-url-secret" not in body
    assert "user:literal-url-secret" not in body
    assert "postgres_data" not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("token_name", "translated_token_name"),
    [
        ("ghp_volumeSecretToken123456", "ghp-volumesecrettoken123456"),
        ("AIzaVolumeSecretToken123456", "aizavolumesecrettoken123456"),
    ],
)
def test_rendered_stack_payload_redacts_token_volume_key_before_translation(
    tmp_path: Path,
    token_name: str,
    translated_token_name: str,
) -> None:
    """Token-shaped volume keys must redact to a DNS-safe hosted placeholder."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        f"""
services:
  backend:
    image: backend:latest
volumes:
  {token_name}: {{}}
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )

    assert payload is not None
    assert payload["volumes"] == {"redacted": {}}
    _assert_dns1123_label(next(iter(payload["volumes"])))
    body = json.dumps(payload, sort_keys=True)
    assert "<redacted>" not in body
    assert token_name not in body
    assert translated_token_name not in body


@pytest.mark.unit
def test_rendered_stack_payload_disambiguates_redacted_top_level_volume_keys(
    tmp_path: Path,
) -> None:
    """Multiple secret-shaped top-level volume keys must not collide."""
    first_token_name = "ghp_volumeSecretToken123456"
    first_translated_name = "ghp-volumesecrettoken123456"
    second_token_name = "gho_volumeSecretToken123456"
    second_translated_name = "gho-volumesecrettoken123456"
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        f"""
services:
  backend:
    image: backend:latest
    volumes:
      - {first_token_name}:/cache-first
      - type: volume
        source: {second_token_name}
        target: /cache-second
volumes:
  {first_token_name}: {{}}
  {second_token_name}: {{}}
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )

    assert payload is not None
    redacted_volume_names = set(payload["volumes"])
    assert len(redacted_volume_names) == 2
    assert all(
        name == "redacted" or re.fullmatch(r"redacted-[0-9]+", name)
        for name in redacted_volume_names
    )
    for name in redacted_volume_names:
        _assert_dns1123_label(name)
    backend_volumes = payload["services"]["backend"]["volumes"]
    assert {
        backend_volumes[0].partition(":")[0],
        backend_volumes[1]["source"],
    } == redacted_volume_names
    body = json.dumps(payload, sort_keys=True)
    assert "<redacted>" not in body
    for secret in (
        first_token_name,
        first_translated_name,
        second_token_name,
        second_translated_name,
    ):
        assert secret not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("token_name", "translated_token_name"),
    [
        ("ghp_volumeSecretToken123456", "ghp-volumesecrettoken123456"),
        ("AIzaVolumeSecretToken123456", "aizavolumesecrettoken123456"),
    ],
)
def test_rendered_stack_payload_redacts_token_explicit_volume_name_before_translation(
    tmp_path: Path,
    token_name: str,
    translated_token_name: str,
) -> None:
    """Token-shaped explicit names must redact to a DNS-safe hosted placeholder."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        f"""
services:
  backend:
    image: backend:latest
    volumes:
      - cache:/cache
volumes:
  cache:
    name: {token_name}
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )

    assert payload is not None
    assert payload["volumes"] == {"cache": {"name": "redacted"}}
    _assert_dns1123_label(payload["volumes"]["cache"]["name"])
    body = json.dumps(payload, sort_keys=True)
    assert "<redacted>" not in body
    assert token_name not in body
    assert translated_token_name not in body


@pytest.mark.unit
def test_rendered_stack_payload_disambiguates_redacted_explicit_volume_names(
    tmp_path: Path,
) -> None:
    """Multiple secret-shaped explicit volume names must not alias."""
    first_token_name = "ghp_volumeSecretToken123456"
    first_translated_name = "ghp-volumesecrettoken123456"
    second_token_name = "gho_volumeSecretToken123456"
    second_translated_name = "gho-volumesecrettoken123456"
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        f"""
services:
  backend:
    image: backend:latest
    volumes:
      - cache-a:/cache-a
      - cache-b:/cache-b
volumes:
  cache-a:
    name: {first_token_name}
  cache-b:
    name: {second_token_name}
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )

    assert payload is not None
    explicit_names = {
        payload["volumes"]["cache-a"]["name"],
        payload["volumes"]["cache-b"]["name"],
    }
    assert len(explicit_names) == 2
    assert all(
        name == "redacted" or re.fullmatch(r"redacted-[0-9]+", name) for name in explicit_names
    )
    for name in explicit_names:
        _assert_dns1123_label(name)
    body = json.dumps(payload, sort_keys=True)
    assert "<redacted>" not in body
    for secret in (
        first_token_name,
        first_translated_name,
        second_token_name,
        second_translated_name,
    ):
        assert secret not in body


@pytest.mark.unit
def test_rendered_stack_payload_redacts_token_service_volume_sources_before_translation(
    tmp_path: Path,
) -> None:
    """Token-shaped service volume sources redact to DNS-safe hosted placeholders."""
    short_token_name = "ghp_volumeSecretToken123456"
    short_translated_name = "ghp-volumesecrettoken123456"
    source_token_name = "AIzaVolumeSecretToken123456"
    source_translated_name = "aizavolumesecrettoken123456"
    src_token_name = "gho_volumeSecretToken123456"
    src_translated_name = "gho-volumesecrettoken123456"
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        f"""
services:
  backend:
    image: backend:latest
    volumes:
      - {short_token_name}:/cache-short
      - type: volume
        source: {source_token_name}
        target: /cache-source
      - type: volume
        src: {src_token_name}
        target: /cache-src
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )

    assert payload is not None
    backend_volumes = payload["services"]["backend"]["volumes"]
    assert len(backend_volumes) == 3
    assert backend_volumes[0].partition(":")[2] == "/cache-short"
    assert backend_volumes[1]["type"] == "volume"
    assert backend_volumes[1]["target"] == "/cache-source"
    assert backend_volumes[2]["type"] == "volume"
    assert backend_volumes[2]["target"] == "/cache-src"
    sources = {
        backend_volumes[0].partition(":")[0],
        backend_volumes[1]["source"],
        backend_volumes[2]["src"],
    }
    assert len(sources) == 3
    assert all(
        source == "redacted" or re.fullmatch(r"redacted-[0-9]+", source) for source in sources
    )
    for source in sources:
        _assert_dns1123_label(source)
    body = json.dumps(payload, sort_keys=True)
    assert "<redacted>" not in body
    for secret in (
        short_token_name,
        short_translated_name,
        source_token_name,
        source_translated_name,
        src_token_name,
        src_translated_name,
    ):
        assert secret not in body


@pytest.mark.unit
def test_rendered_stack_payload_disambiguates_redacted_service_only_volume_sources(
    tmp_path: Path,
) -> None:
    """Multiple secret-shaped service-only volume sources must not alias."""
    first_token_name = "ghp_volumeSecretToken123456"
    first_translated_name = "ghp-volumesecrettoken123456"
    second_token_name = "gho_volumeSecretToken123456"
    second_translated_name = "gho-volumesecrettoken123456"
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        f"""
services:
  backend:
    image: backend:latest
    volumes:
      - {first_token_name}:/cache-first
      - type: volume
        source: {second_token_name}
        target: /cache-second
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )

    assert payload is not None
    backend_volumes = payload["services"]["backend"]["volumes"]
    assert len(backend_volumes) == 2
    redacted_volume_names = {
        backend_volumes[0].partition(":")[0],
        backend_volumes[1]["source"],
    }
    assert len(redacted_volume_names) == 2
    assert all(
        name == "redacted" or re.fullmatch(r"redacted-[0-9]+", name)
        for name in redacted_volume_names
    )
    for name in redacted_volume_names:
        _assert_dns1123_label(name)
    body = json.dumps(payload, sort_keys=True)
    assert "<redacted>" not in body
    for secret in (
        first_token_name,
        first_translated_name,
        second_token_name,
        second_translated_name,
    ):
        assert secret not in body


@pytest.mark.unit
def test_rendered_stack_payload_sanitizes_list_environment(tmp_path: Path) -> None:
    """List-form Compose environments preserve shape while removing secrets."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    image: backend:latest
    environment:
      - PUBLIC_URL=http://backend:8000
      - API_TOKEN=literal-service-secret
      - DATABASE_URL=postgresql://user:password@postgres/awf
      - NO_EQUALS_ENTRY
      - 7
    labels:
      - hosted
      - 12
  worker:
    image: worker:latest
    environment:
      WORKER_PASSWORD: literal-worker-secret
  agent:
    image: awf-agent-runtime:latest
    environment:
      NPM_TOKEN: literal-agent-secret
networks:
  awf_net:
    name: awf-ws-hosted-net
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    assert set(payload["services"]) == {"backend", "worker"}
    assert payload["services"]["backend"]["environment"] == [
        "PUBLIC_URL=http://backend:8000",
        "NO_EQUALS_ENTRY",
        7,
    ]
    assert payload["services"]["worker"]["environment"] == {}
    assert payload["networks"] == {"awf_net": {"name": "awf-ws-hosted-net"}}
    body = json.dumps(payload, sort_keys=True)
    assert "API_TOKEN" not in body
    assert "WORKER_PASSWORD" not in body
    assert "DATABASE_URL" not in body
    assert "${DATABASE_URL}" not in body
    assert "literal-service-secret" not in body
    assert "literal-worker-secret" not in body
    assert "literal-agent-secret" not in body
    assert "user:password" not in body


@pytest.mark.unit
def test_rendered_stack_omit_drops_exact_client_password_env_names(tmp_path: Path) -> None:
    """Omit mode drops PGPASSWORD/MYSQL_PWD even though they lack a separator before PWD."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    image: backend:latest
    environment:
      PUBLIC_URL: http://backend:8000
      PGPASSWORD: ${PGPASSWORD}
      MYSQL_PWD: literal-mysql-client-secret
  worker:
    image: worker:latest
    environment:
      - PUBLIC_URL=http://worker:9000
      - PGPASSWORD=literal-pg-client-secret
      - MYSQL_PWD=${MYSQL_PWD}
  agent:
    image: awf-agent-runtime:latest
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    assert payload["services"]["backend"]["environment"] == {
        "PUBLIC_URL": "http://backend:8000",
    }
    assert payload["services"]["worker"]["environment"] == [
        "PUBLIC_URL=http://worker:9000",
    ]
    body = json.dumps(payload, sort_keys=True)
    assert "PGPASSWORD" not in body
    assert "MYSQL_PWD" not in body
    assert "literal-mysql-client-secret" not in body
    assert "literal-pg-client-secret" not in body
    assert "${PGPASSWORD}" not in body
    assert "${MYSQL_PWD}" not in body


@pytest.mark.unit
def test_rendered_stack_omit_mode_drops_literal_bearer_env_under_safe_names(
    tmp_path: Path,
) -> None:
    """Literal bearer tokens in safe-named env must omit like command/operator arms."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: app:latest
    environment:
      PUBLIC_HEADER: "Authorization: Bearer opaqueBearerToken123456"
      LOOSE_BEARER: Bearer looseBearerValue123456
      KEEP_BARE: Bearer $PUBLIC_HOST
      KEEP_REF: Bearer ${PUBLIC_HOST}
      SAFE_URL: http://app:8000
  worker:
    image: worker:latest
    environment:
      - "PUBLIC_HEADER=Authorization: Bearer listBearerToken123456"
      - KEEP_LIST=Bearer $PUBLIC_HOST
      - SAFE_URL=http://worker:9000
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    assert payload["services"]["app"]["environment"] == {
        "KEEP_BARE": "Bearer $PUBLIC_HOST",
        "KEEP_REF": "Bearer ${PUBLIC_HOST}",
        "SAFE_URL": "http://app:8000",
    }
    assert payload["services"]["worker"]["environment"] == [
        "KEEP_LIST=Bearer $PUBLIC_HOST",
        "SAFE_URL=http://worker:9000",
    ]
    body = json.dumps(payload, sort_keys=True)
    assert "PUBLIC_HEADER" not in body
    assert "LOOSE_BEARER" not in body
    assert "opaqueBearerToken123456" not in body
    assert "looseBearerValue123456" not in body
    assert "listBearerToken123456" not in body


@pytest.mark.unit
def test_rendered_stack_omit_mode_drops_url_encoded_bearer_and_credential_url_env(
    tmp_path: Path,
) -> None:
    """Omit mode must decode URL variants before bearer/URL/PEM env checks.

    Percent-encoded bearer headers and userinfo URLs must not survive when the
    same raw material is already dropped under safe-named keys.
    """
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: app:latest
    environment:
      PUBLIC_HEADER: Authorization%3A%20Bearer%20opaqueBearerToken123456
      PUBLIC_LINK: https%3A%2F%2Fuser%3Apw%40svc.example
      PUBLIC_PEM: -----BEGIN%20PRIVATE%20KEY-----%0Amaterial%0A-----END%20PRIVATE%20KEY-----
      KEEP_BARE: Bearer $PUBLIC_HOST
      SAFE_URL: http://app:8000
  worker:
    image: worker:latest
    environment:
      - PUBLIC_HEADER=Authorization%3A%20Bearer%20listBearerToken123456
      - PUBLIC_LINK=https%3A%2F%2Flistuser%3Apw%40svc.example
      - KEEP_LIST=Bearer $PUBLIC_HOST
      - SAFE_URL=http://worker:9000
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    assert payload["services"]["app"]["environment"] == {
        "KEEP_BARE": "Bearer $PUBLIC_HOST",
        "SAFE_URL": "http://app:8000",
    }
    assert payload["services"]["worker"]["environment"] == [
        "KEEP_LIST=Bearer $PUBLIC_HOST",
        "SAFE_URL=http://worker:9000",
    ]
    body = json.dumps(payload, sort_keys=True)
    assert "PUBLIC_HEADER" not in body
    assert "PUBLIC_LINK" not in body
    assert "PUBLIC_PEM" not in body
    assert "opaqueBearerToken123456" not in body
    assert "listBearerToken123456" not in body
    assert "user%3Apw" not in body
    assert "listuser%3Apw" not in body
    assert "PRIVATE%20KEY" not in body


@pytest.mark.unit
def test_rendered_stack_omit_mode_drops_form_encoded_bearer_plus_space_env(
    tmp_path: Path,
) -> None:
    """Form-urlencoded '+' spaces must decode before bearer omit checks.

    ``unquote`` leaves '+' intact, so Authorization%3A+Bearer+token must also
    be scanned via a plus-to-space variant or the raw token leaks into payloads.
    """
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: app:latest
    environment:
      PUBLIC_HEADER: Authorization%3A+Bearer+opaqueBearerToken123456
      LOOSE_BEARER: Bearer+looseBearerValue123456
      PUBLIC_PEM: -----BEGIN+PRIVATE+KEY-----+material+-----END+PRIVATE+KEY-----
      KEEP_BARE: Bearer $PUBLIC_HOST
      SAFE_URL: http://app:8000
  worker:
    image: worker:latest
    environment:
      - PUBLIC_HEADER=Authorization%3A+Bearer+listBearerToken123456
      - KEEP_LIST=Bearer $PUBLIC_HOST
      - SAFE_URL=http://worker:9000
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    assert payload["services"]["app"]["environment"] == {
        "KEEP_BARE": "Bearer $PUBLIC_HOST",
        "SAFE_URL": "http://app:8000",
    }
    assert payload["services"]["worker"]["environment"] == [
        "KEEP_LIST=Bearer $PUBLIC_HOST",
        "SAFE_URL=http://worker:9000",
    ]
    body = json.dumps(payload, sort_keys=True)
    assert "PUBLIC_HEADER" not in body
    assert "LOOSE_BEARER" not in body
    assert "PUBLIC_PEM" not in body
    assert "opaqueBearerToken123456" not in body
    assert "looseBearerValue123456" not in body
    assert "listBearerToken123456" not in body
    assert "PRIVATE+KEY" not in body


@pytest.mark.unit
def test_hosted_validation_profile_payload_omits_exact_client_password_env_names() -> None:
    """Profile env sanitize omits exact client password names treated as command secrets."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-client-password-env",
            "runtime": {
                "environment": {
                    "PUBLIC_URL": "http://backend:8000",
                    "PGPASSWORD": "${PGPASSWORD}",
                    "MYSQL_PWD": "literal-mysql-client-secret",
                },
            },
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "PUBLIC_URL": "http://backend:8000",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "PGPASSWORD" not in body
    assert "MYSQL_PWD" not in body
    assert "literal-mysql-client-secret" not in body


@pytest.mark.unit
def test_rendered_stack_payload_sanitizes_non_collection_environment(tmp_path: Path) -> None:
    """Unexpected Compose environment shapes are redacted as plain values."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    image: backend:latest
    environment: false
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )

    assert payload is not None
    assert payload["services"]["backend"]["environment"] is False


@pytest.mark.unit
def test_rendered_stack_payload_handles_unusual_volume_shapes(tmp_path: Path) -> None:
    """Rendered stack metadata preserves odd Compose volume shapes without raw leaks."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    image: backend:latest
    volumes: not-a-list
  worker:
    image: worker:latest
    volumes:
      - 42
      - 'C:\\host\\cache:/windows'
      - cache
      - cache:relative
      - type: volume
        target: /missing-source
      - source: ./host-cache
        target: /host
      - source: named_data
        target: /named
      - type: bind
        source: bind_data
        target: /bind
  agent:
    image: awf-agent-runtime:latest
volumes:
  odd_bool: true
""".lstrip(),
        encoding="utf-8",
    )
    envelope: dict[str, object] = {}

    _hosted_validation_attach_rendered_stack(
        envelope,
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        include_agent_auth_context=True,
    )

    assert set(envelope) == {"rendered_stack"}
    stack = envelope["rendered_stack"]
    assert isinstance(stack, dict)
    assert "agent_auth" not in envelope
    assert stack["volumes"] == {"odd-bool": True}
    assert stack["services"]["backend"]["volumes"] == "not-a-list"
    assert stack["services"]["worker"]["volumes"] == [
        42,
        r"C:\host\cache:/windows",
        "cache",
        "cache:relative",
        {"type": "volume", "target": "/missing-source"},
        {"source": "./host-cache", "target": "/host"},
        {"source": "named-data", "target": "/named"},
        {"type": "bind", "source": "bind_data", "target": "/bind"},
    ]


@pytest.mark.unit
def test_rendered_stack_payload_excludes_core_managed_clarification_service(
    tmp_path: Path,
) -> None:
    """Hosted application stacks must not include Core's local helper service."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: pgvector/pgvector:pg18
  project-helper:
    image: helper:latest
    profiles:
      - project-tools
  clarification:
    image: awf-agent-runtime:latest
    x-awf-persisted-clarification-service-managed: true
    volumes:
      - /run/awf/hosted-auth-placeholders/codex:/run/awf/clarification-auth/0:ro
    profiles:
      - awf-clarification
    networks:
      - clarification_egress_net
    command:
      - sh
      - -c
      - sleep infinity
    restart: "no"
  agent:
    image: awf-agent-runtime:latest
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )

    assert payload is not None
    assert set(payload["services"]) == {"postgres", "project-helper"}


@pytest.mark.unit
def test_hosted_profile_environment_omit_ignores_unexpected_container_shapes() -> None:
    """Coverage profile env omission only mutates dict environment containers."""
    list_container: list[object] = []
    list_environment_container = {"environment": ["PIP_INDEX_URL=https://token@example.test"]}

    _hosted_validation_omit_environment_entries(
        list_container,
        names=frozenset({"PIP_INDEX_URL"}),
    )
    _hosted_validation_omit_environment_entries(
        list_environment_container,
        names=frozenset({"PIP_INDEX_URL"}),
    )

    assert list_container == []
    assert list_environment_container == {
        "environment": ["PIP_INDEX_URL=https://token@example.test"],
    }
