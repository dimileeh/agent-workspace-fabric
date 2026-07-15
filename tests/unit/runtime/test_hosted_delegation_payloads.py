"""Hosted delegation payload sanitization edge tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation_payloads import (
    _hosted_pr_identity_payload,
    _hosted_validation_attach_rendered_stack,
    _hosted_validation_omit_environment_entries,
    _hosted_validation_profile_payload,
    _hosted_validation_rendered_stack_payload,
    _hosted_validation_secret_checked_fields,
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
def test_rendered_stack_omit_mode_drops_secret_valued_safe_named_env(
    tmp_path: Path,
) -> None:
    """Validation omit mode drops secret values even when the env name looks safe."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    image: backend:latest
    environment:
      PUBLIC_URL: http://backend:8000
      DATABASE_URL: postgresql://user:literal-url-secret@postgres/awf
      APP_DSN: postgresql://user:literal-dsn-secret@postgres/awf
      ALREADY_REF: ${PUBLIC_HOST}
  worker:
    image: worker:latest
    environment:
      - PUBLIC_URL=http://worker:8000
      - DATABASE_URL=postgresql://user:list-url-secret@postgres/awf
      - REDIS_URL=redis://cache:6379/0
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
        "ALREADY_REF": "${PUBLIC_HOST}",
    }
    assert payload["services"]["worker"]["environment"] == [
        "PUBLIC_URL=http://worker:8000",
        "REDIS_URL=redis://cache:6379/0",
    ]
    body = json.dumps(payload, sort_keys=True)
    assert "DATABASE_URL" not in body
    assert "APP_DSN" not in body
    assert "${DATABASE_URL}" not in body
    assert "${APP_DSN}" not in body
    assert "literal-url-secret" not in body
    assert "literal-dsn-secret" not in body
    assert "list-url-secret" not in body


@pytest.mark.unit
def test_rendered_stack_omit_mode_drops_safe_named_credential_refs(
    tmp_path: Path,
) -> None:
    """Validation omit mode drops ${DATABASE_URL}-style refs on URL/DSN keys."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    image: backend:latest
    environment:
      PUBLIC_URL: http://backend:8000
      DATABASE_URL: ${DATABASE_URL}
      APP_DSN: ${APP_DSN}
      ALREADY_REF: ${PUBLIC_HOST}
  worker:
    image: worker:latest
    environment:
      - PUBLIC_URL=http://worker:8000
      - DATABASE_URL=${DATABASE_URL}
      - REDIS_URL=redis://cache:6379/0
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
        "ALREADY_REF": "${PUBLIC_HOST}",
    }
    assert payload["services"]["worker"]["environment"] == [
        "PUBLIC_URL=http://worker:8000",
        "REDIS_URL=redis://cache:6379/0",
    ]
    body = json.dumps(payload, sort_keys=True)
    assert "DATABASE_URL" not in body
    assert "APP_DSN" not in body
    assert "${DATABASE_URL}" not in body
    assert "${APP_DSN}" not in body


@pytest.mark.unit
def test_rendered_stack_omit_mode_drops_safe_named_bare_passthrough_slots(
    tmp_path: Path,
) -> None:
    """Omit mode drops bare list URL/DSN pass-through slots, not only secret-named ones."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  worker:
    image: worker:latest
    environment:
      - PUBLIC_HOST
      - API_TOKEN
      - DATABASE_URL
      - APP_DSN
      - REDIS_URL=redis://cache:6379/0
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    assert payload["services"]["worker"]["environment"] == [
        "PUBLIC_HOST",
        "REDIS_URL=redis://cache:6379/0",
    ]
    body = json.dumps(payload, sort_keys=True)
    assert "DATABASE_URL" not in body
    assert "APP_DSN" not in body
    assert "API_TOKEN" not in body


@pytest.mark.unit
def test_rendered_stack_omit_mode_drops_credential_source_refs_under_safe_targets(
    tmp_path: Path,
) -> None:
    """Omit mode drops ${CREDENTIAL} refs even when the target env name looks safe."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    image: backend:latest
    environment:
      PUBLIC_URL: http://backend:8000
      DATABASE_URL: ${POSTGRES_PASSWORD}
      APP_HOST: ${API_TOKEN}
      ALREADY_REF: ${PUBLIC_HOST}
  worker:
    image: worker:latest
    environment:
      - PUBLIC_URL=http://worker:8000
      - CACHE_HOST=${POSTGRES_PASSWORD}
      - SERVICE_ENDPOINT=${SERVICE_API_KEY}
      - REDIS_URL=redis://cache:6379/0
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
        "ALREADY_REF": "${PUBLIC_HOST}",
    }
    assert payload["services"]["worker"]["environment"] == [
        "PUBLIC_URL=http://worker:8000",
        "REDIS_URL=redis://cache:6379/0",
    ]
    body = json.dumps(payload, sort_keys=True)
    assert "DATABASE_URL" not in body
    assert "APP_HOST" not in body
    assert "CACHE_HOST" not in body
    assert "SERVICE_ENDPOINT" not in body
    assert "${POSTGRES_PASSWORD}" not in body
    assert "${API_TOKEN}" not in body
    assert "${SERVICE_API_KEY}" not in body


@pytest.mark.unit
def test_rendered_stack_omit_mode_drops_safe_named_credential_source_refs(
    tmp_path: Path,
) -> None:
    """Omit mode drops ${DATABASE_URL}/${APP_DSN} sources under safe target names."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    image: backend:latest
    environment:
      PUBLIC_HEADER: Bearer ${DATABASE_URL}
      SERVICE_CONFIG: prefix-${APP_DSN}
      PUBLIC_CONN: ${DATABASE_URL}
      KEEP_HEADER: Bearer ${PUBLIC_HOST}
  worker:
    image: worker:latest
    environment:
      - PUBLIC_HEADER=Bearer ${DATABASE_URL}
      - SERVICE_CONFIG=prefix-${APP_DSN}
      - PUBLIC_CONN=${DATABASE_URL}
      - KEEP_REF=Bearer ${PUBLIC_HOST}
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
        "KEEP_HEADER": "Bearer ${PUBLIC_HOST}",
    }
    assert payload["services"]["worker"]["environment"] == [
        "KEEP_REF=Bearer ${PUBLIC_HOST}",
    ]
    body = json.dumps(payload, sort_keys=True)
    assert "PUBLIC_HEADER" not in body
    assert "SERVICE_CONFIG" not in body
    assert "PUBLIC_CONN" not in body
    assert "${DATABASE_URL}" not in body
    assert "${APP_DSN}" not in body
    assert "DATABASE_URL" not in body
    assert "APP_DSN" not in body


@pytest.mark.unit
def test_rendered_stack_omit_mode_drops_embedded_credential_source_refs(
    tmp_path: Path,
) -> None:
    """Omit mode drops values that embed ${CREDENTIAL} inside surrounding text."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    image: backend:latest
    environment:
      PUBLIC_HEADER: Bearer ${API_TOKEN}
      CACHE_KEY: prefix-${POSTGRES_PASSWORD}
      KEEP_HEADER: Bearer ${PUBLIC_HOST}
      KEEP_KEY: prefix-${PUBLIC_HOST}-suffix
  worker:
    image: worker:latest
    environment:
      - PUBLIC_HEADER=Bearer ${API_TOKEN}
      - CACHE_KEY=prefix-${POSTGRES_PASSWORD}
      - KEEP_REF=Bearer ${PUBLIC_HOST}
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
        "KEEP_HEADER": "Bearer ${PUBLIC_HOST}",
        "KEEP_KEY": "prefix-${PUBLIC_HOST}-suffix",
    }
    assert payload["services"]["worker"]["environment"] == [
        "KEEP_REF=Bearer ${PUBLIC_HOST}",
    ]
    body = json.dumps(payload, sort_keys=True)
    assert "PUBLIC_HEADER" not in body
    assert "CACHE_KEY" not in body
    assert "${API_TOKEN}" not in body
    assert "${POSTGRES_PASSWORD}" not in body
    assert "API_TOKEN" not in body
    assert "POSTGRES_PASSWORD" not in body


@pytest.mark.unit
def test_rendered_stack_omit_mode_drops_credential_source_refs_with_compose_operators(
    tmp_path: Path,
) -> None:
    """Omit mode drops Compose ${CREDENTIAL:-}/{:?} refs under safe-looking targets."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    image: backend:latest
    environment:
      PUBLIC_URL: ${POSTGRES_PASSWORD:-}
      APP_HOST: ${API_TOKEN:?set API_TOKEN}
      DATABASE_URL: ${DATABASE_URL:-}
      KEEP_REF: ${PUBLIC_HOST:-localhost}
  worker:
    image: worker:latest
    environment:
      - CACHE_HOST=${POSTGRES_PASSWORD-}
      - SERVICE_ENDPOINT=${SERVICE_API_KEY?missing}
      - APP_DSN=${APP_DSN:+override}
      - REDIS_URL=redis://cache:6379/0
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
        "KEEP_REF": "${PUBLIC_HOST:-localhost}",
    }
    assert payload["services"]["worker"]["environment"] == [
        "REDIS_URL=redis://cache:6379/0",
    ]
    body = json.dumps(payload, sort_keys=True)
    assert "PUBLIC_URL" not in body
    assert "APP_HOST" not in body
    assert "DATABASE_URL" not in body
    assert "CACHE_HOST" not in body
    assert "SERVICE_ENDPOINT" not in body
    assert "APP_DSN" not in body
    assert "POSTGRES_PASSWORD" not in body
    assert "API_TOKEN" not in body
    assert "SERVICE_API_KEY" not in body


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


@pytest.mark.unit
def test_hosted_pr_identity_preserves_passwordless_git_ssh_username() -> None:
    """Passwordless git+ssh clone URLs keep the SSH login for hosted fetches."""
    payload = _hosted_pr_identity_payload(
        {
            "repo_url": "git+ssh://git@github.com/org/repo.git",
            "head_repo_url": "git+ssh://git@github.com/fork/repo.git",
        }
    )

    assert payload == {
        "repo_url": "git+ssh://git@github.com/org/repo.git",
        "head_repo_url": "git+ssh://git@github.com/fork/repo.git",
    }


@pytest.mark.unit
def test_hosted_pr_identity_strips_ssh_password_but_keeps_username() -> None:
    """Hosted PR identity carries repository location without URL credentials."""
    payload = _hosted_pr_identity_payload(
        {
            "repo_url": "ssh://git:secret-token@github.com/org/repo.git",
            "head_repo_url": "ssh://agent:fork-secret@github.com/fork/repo.git",
        }
    )

    assert payload == {
        "repo_url": "ssh://git@github.com/org/repo.git",
        "head_repo_url": "ssh://agent@github.com/fork/repo.git",
    }


@pytest.mark.unit
def test_hosted_pr_identity_strips_ssh_password_without_username() -> None:
    """Password-only SSH userinfo is removed entirely from hosted PR identity."""
    payload = _hosted_pr_identity_payload(
        {"repo_url": "ssh://:secret-token@github.com/org/repo.git"}
    )

    assert payload == {"repo_url": "ssh://github.com/org/repo.git"}


@pytest.mark.unit
def test_hosted_pr_identity_strips_query_and_fragment_credentials() -> None:
    """Hosted PR identity never sends query or fragment URL credentials."""
    payload = _hosted_pr_identity_payload(
        {
            "repo_url": "https://github.com/org/repo.git?token=literal-secret",
            "head_repo_url": "https://github.com/fork/repo.git#access_token=other-secret",
        }
    )

    assert payload == {
        "repo_url": "https://github.com/org/repo.git",
        "head_repo_url": "https://github.com/fork/repo.git",
    }


@pytest.mark.unit
def test_hosted_validation_profile_payload_redacts_env_url_query_credentials() -> None:
    """Neutral hosted env names must not carry URL query or fragment credentials."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-env-url-query-credentials",
            "runtime": {
                "environment": {
                    "SERVICE_URL": "https://example.test/api?token=qcred-lit",
                    "CALLBACK_URL": "https://example.test/cb#password=fcred-lit",
                    "PUBLIC_URL": "https://example.test/api?next=/ready",
                    "REPOSITORY_URL": "ssh://git@github.com/org/repo.git",
                },
            },
            "services": [
                {
                    "name": "worker",
                    "image": "worker:latest",
                    "environment": {
                        "WEBHOOK_URL": "https://hooks.example.test/run?access_key=service-key",
                        "DOCS_URL": "https://docs.example.test/public",
                    },
                },
            ],
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "SERVICE_URL": "${SERVICE_URL}",
        "CALLBACK_URL": "${CALLBACK_URL}",
        "PUBLIC_URL": "https://example.test/api?next=/ready",
        "REPOSITORY_URL": "ssh://git@github.com/org/repo.git",
    }
    assert payload["services"][0]["environment"] == {
        "WEBHOOK_URL": "${WEBHOOK_URL}",
        "DOCS_URL": "https://docs.example.test/public",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "qcred-lit" not in body
    assert "fcred-lit" not in body
    assert "service-key" not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    ("profile_payload", "expected_path", "secret_value"),
    [
        (
            {
                "name": "hosted-secret-phase-command",
                "phases": {
                    "setup": ["NPM_TOKEN=literal-phase-secret npm install"],
                },
            },
            "phases.setup[0].command",
            "literal-phase-secret",
        ),
        (
            {
                "name": "hosted-secret-generated-setup",
                "database": {
                    "generated_setup": ["PGPASSWORD=literal-database-secret alembic upgrade head"],
                },
            },
            "database.generated_setup[0].command",
            "literal-database-secret",
        ),
        (
            {
                "name": "hosted-secret-private-package-url",
                "phases": {
                    "pre_agent": [
                        "pip install https://user:literal-url-secret@packages.example/pkg"
                    ],
                },
            },
            "phases.pre_agent[0].command",
            "literal-url-secret",
        ),
        (
            {
                "name": "hosted-secret-refresh",
                "database": {
                    "pre_validation_refresh": ["PASSWORD=literal-refresh-secret ./refresh-db"],
                },
            },
            "database.pre_validation_refresh[0].command",
            "literal-refresh-secret",
        ),
        (
            {
                "name": "hosted-secret-healthcheck",
                "validation": {
                    "healthchecks": [
                        {
                            "name": "app",
                            "command": "curl -H 'Authorization: Bearer abcdefgh123' http://app",
                        }
                    ],
                },
            },
            "validation.healthchecks[0].command",
            "abcdefgh123",
        ),
        (
            {
                "name": "hosted-secret-coverage",
                "validation": {
                    "coverage": {
                        "minimum_percent": 1,
                        "command": "COVERAGE_TOKEN=literal-coverage-secret pytest --cov",
                    },
                },
            },
            "validation.coverage.command.command",
            "literal-coverage-secret",
        ),
        (
            {
                "name": "hosted-secret-service-command",
                "services": [
                    {
                        "name": "worker",
                        "image": "worker:latest",
                        "command": "API_KEY=literal-service-secret ./start-worker",
                    }
                ],
            },
            "services[0].command",
            "literal-service-secret",
        ),
        (
            {
                "name": "hosted-secret-service-healthcheck",
                "services": [
                    {
                        "name": "worker",
                        "image": "worker:latest",
                        "healthcheck_cmd": "PASSWORD=literal-health-secret ./health",
                    }
                ],
            },
            "services[0].healthcheck_cmd",
            "literal-health-secret",
        ),
    ],
)
def test_hosted_validation_profile_payload_rejects_secret_bearing_command_fields(
    profile_payload: dict[str, object],
    expected_path: str,
    secret_value: str,
) -> None:
    """Hosted profile payloads reject command strings that would carry credentials."""
    profile = WorkspaceProfile.model_validate(profile_payload)

    with pytest.raises(ValueError) as excinfo:
        _hosted_validation_profile_payload(profile)

    message = str(excinfo.value)
    assert expected_path in message
    assert secret_value not in message


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "PGPASSWORD=${PGPASSWORD:-} psql -c 'select 1'",
        "PGPASSWORD=${PGPASSWORD-} psql -c 'select 1'",
    ],
)
def test_hosted_validation_profile_payload_allows_empty_default_secret_references(
    command: str,
) -> None:
    """Empty default env references do not carry literal command secrets."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-empty-default-secret-reference",
            "database": {"generated_setup": [command]},
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["database"]["generated_setup"][0]["command"] == command


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "PGPASSWORD=${PGPASSWORD:-literal-database-secret} psql -c 'select 1'",
        "PGPASSWORD=${PGPASSWORD-literal-database-secret} psql -c 'select 1'",
    ],
)
def test_hosted_validation_profile_payload_rejects_non_empty_default_secret_references(
    command: str,
) -> None:
    """Non-empty default words can carry secrets and remain disallowed."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-non-empty-default-secret-reference",
            "database": {"generated_setup": [command]},
        }
    )

    with pytest.raises(ValueError) as excinfo:
        _hosted_validation_profile_payload(profile)

    message = str(excinfo.value)
    assert "database.generated_setup[0].command" in message
    assert "literal-database-secret" not in message


@pytest.mark.unit
def test_hosted_validation_profile_payload_rejects_secret_bearing_healthcheck_url() -> None:
    """Hosted HTTP healthcheck URLs must not carry credentials to the control plane."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-secret-healthcheck-url",
            "validation": {
                "healthchecks": [
                    {
                        "name": "app",
                        "url": "https://user:literal-health-url-secret@example.test/health",
                    },
                ],
            },
        }
    )

    with pytest.raises(ValueError) as excinfo:
        _hosted_validation_profile_payload(profile)

    message = str(excinfo.value)
    assert "validation.healthchecks[0].url" in message
    assert "literal-health-url-secret" not in message


@pytest.mark.unit
def test_hosted_validation_secret_field_scan_ignores_malformed_optional_sections() -> None:
    """Malformed optional command sections are ignored while safe fields remain visible."""
    payload = {
        "phases": {"setup": "pytest -q"},
        "database": {"generated_setup": object()},
        "validation": {
            "coverage": {"command": "pytest --cov"},
            "healthchecks": [
                {"name": "missing-command"},
                "not-a-healthcheck",
                {"name": "app", "url": "https://app.example.test/health"},
            ],
        },
        "services": [
            "not-a-service",
            {"name": "worker", "command": "python -m worker"},
        ],
    }

    assert list(_hosted_validation_secret_checked_fields(payload)) == [
        ("validation.healthchecks[2].url", "https://app.example.test/health"),
        ("services[1].command", "python -m worker"),
    ]


@pytest.mark.unit
def test_hosted_validation_profile_payload_preserves_safe_command_fields() -> None:
    """Non-secret command fields stay available to the hosted validator."""
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-safe-commands",
            "services": [
                {
                    "name": "worker",
                    "image": "worker:latest",
                    "command": "python -m worker",
                    "healthcheck_cmd": "python -m worker.healthcheck",
                }
            ],
            "phases": {
                "setup": ["NPM_TOKEN=${NPM_TOKEN} python -m pip install -e ."],
                "validate": ["pytest tests/unit/runtime/test_hosted_delegation_payloads.py -q"],
            },
            "database": {
                "generated_setup": ["alembic upgrade head"],
                "pre_validation_refresh": ["python scripts/refresh_fixtures.py"],
            },
            "validation": {
                "healthchecks": [
                    {"name": "app", "command": "curl -fsS http://app:8000/health"},
                    {"name": "worker", "url": "https://worker.example.test/health"},
                ],
                "coverage": {
                    "minimum_percent": 1,
                    "command": "pytest tests/unit/runtime --cov=awf.runtime",
                },
            },
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["services"][0]["command"] == "python -m worker"
    assert payload["services"][0]["healthcheck_cmd"] == "python -m worker.healthcheck"
    assert payload["phases"]["setup"][0]["command"] == (
        "NPM_TOKEN=${NPM_TOKEN} python -m pip install -e ."
    )
    assert payload["phases"]["validate"][0]["command"] == (
        "pytest tests/unit/runtime/test_hosted_delegation_payloads.py -q"
    )
    assert payload["database"]["generated_setup"][0]["command"] == "alembic upgrade head"
    assert payload["database"]["pre_validation_refresh"][0]["command"] == (
        "python scripts/refresh_fixtures.py"
    )
    assert payload["validation"]["healthchecks"][0]["command"] == (
        "curl -fsS http://app:8000/health"
    )
    assert payload["validation"]["healthchecks"][1]["url"] == "https://worker.example.test/health"
    assert payload["validation"]["coverage"]["command"]["command"] == (
        "pytest tests/unit/runtime --cov=awf.runtime"
    )


@pytest.mark.unit
def test_hosted_validation_profile_payload_handles_profiles_without_services() -> None:
    """Profiles with no services still produce a sanitized hosted profile payload."""
    payload = _hosted_validation_profile_payload(WorkspaceProfile(name="hosted-no-services"))

    assert payload["name"] == "hosted-no-services"
    assert payload["services"] == []
