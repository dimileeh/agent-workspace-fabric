"""Hosted delegation payload sanitization edge tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation_payloads import (
    _hosted_pr_identity_payload,
    _hosted_validation_omit_environment_entries,
    _hosted_validation_profile_payload,
    _hosted_validation_rendered_stack_payload,
)


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
    )

    assert payload is not None
    assert set(payload["services"]) == {"backend", "worker"}
    assert payload["services"]["backend"]["environment"] == [
        "PUBLIC_URL=http://backend:8000",
        "API_TOKEN=${API_TOKEN}",
        "DATABASE_URL=${DATABASE_URL}",
        "NO_EQUALS_ENTRY",
        7,
    ]
    assert payload["services"]["worker"]["environment"] == {
        "WORKER_PASSWORD": "${WORKER_PASSWORD}",
    }
    assert payload["networks"] == {"awf_net": {"name": "awf-ws-hosted-net"}}
    body = json.dumps(payload, sort_keys=True)
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
def test_hosted_validation_profile_payload_handles_profiles_without_services() -> None:
    """Profiles with no services still produce a sanitized hosted profile payload."""
    payload = _hosted_validation_profile_payload(WorkspaceProfile(name="hosted-no-services"))

    assert payload["name"] == "hosted-no-services"
    assert payload["services"] == []
