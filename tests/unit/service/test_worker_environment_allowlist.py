"""Tests for ``local_service_worker_environment_keys`` (issue #533).

The doctor models its secret-lease ``host_env`` on the worker container's real
env allowlist -- the KEYS of the worker service's Compose ``environment:`` block,
NOT the bare caller shell. This guards that single-source-of-truth parser.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.service.config import local_service_worker_environment_keys

pytestmark = pytest.mark.unit


_ANCHORED_COMPOSE = """\
name: awf-local-service

x-awf-service: &awf-service
  image: awf-control-plane:local
  environment: &awf-environment
    ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}
    BITBUCKET_API_TOKEN: ${BITBUCKET_API_TOKEN:-}
    GH_TOKEN: ${GH_TOKEN:-}
    AWF_AGENT_RUNTIME_IMAGE: ${AWF_AGENT_RUNTIME_IMAGE:-awf-agent-runtime:latest}

services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_PASSWORD: secret
  worker:
    <<: *awf-service
    command: sh -c "awf worker"
  console:
    image: awf-console:local
    environment:
      NODE_ENV: production
"""


def test_returns_worker_environment_keys_from_compose_file(tmp_path: Path) -> None:
    compose_file = tmp_path / "local-service.yml"
    compose_file.write_text(_ANCHORED_COMPOSE)

    keys = local_service_worker_environment_keys(compose_file=compose_file)

    assert keys is not None
    # Worker inherits the &awf-environment anchor via ``<<: *awf-service``.
    assert {
        "ANTHROPIC_API_KEY",
        "BITBUCKET_API_TOKEN",
        "GH_TOKEN",
        "AWF_AGENT_RUNTIME_IMAGE",
    } <= keys
    # Other services' env keys are NOT the worker's allowlist.
    assert "POSTGRES_PASSWORD" not in keys
    assert "NODE_ENV" not in keys


def test_list_form_environment_supported(tmp_path: Path) -> None:
    compose_file = tmp_path / "local-service.yml"
    compose_file.write_text(
        "services:\n"
        "  worker:\n"
        "    environment:\n"
        "      - ANTHROPIC_API_KEY=value\n"
        "      - GH_TOKEN\n"
    )

    keys = local_service_worker_environment_keys(compose_file=compose_file)

    assert keys is not None
    assert {"ANTHROPIC_API_KEY", "GH_TOKEN"} <= keys


def test_returns_none_when_compose_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yml"

    assert local_service_worker_environment_keys(compose_file=missing) is None


def test_returns_none_on_malformed_yaml(tmp_path: Path) -> None:
    compose_file = tmp_path / "local-service.yml"
    compose_file.write_text("services: [unterminated\n")

    assert local_service_worker_environment_keys(compose_file=compose_file) is None


def test_returns_none_when_no_asset_root(monkeypatch: pytest.MonkeyPatch) -> None:
    # No explicit compose_file and no resolvable asset root -> None (caller then
    # falls back to the unrestricted merged view, preserving legacy behavior).
    monkeypatch.setattr(
        "awf.service.config._local_service_asset_path",
        lambda _path: None,
    )

    assert local_service_worker_environment_keys() is None


def test_returns_none_on_unexpected_shape(tmp_path: Path) -> None:
    # A worker with no environment block (and no x-* anchors) yields no keys.
    compose_file = tmp_path / "local-service.yml"
    compose_file.write_text("services:\n  worker:\n    command: sh -c 'true'\n")

    assert local_service_worker_environment_keys(compose_file=compose_file) is None


def test_returns_none_when_yaml_is_not_mapping(tmp_path: Path) -> None:
    # A document that parses to a non-mapping (e.g. a list) yields no allowlist.
    compose_file = tmp_path / "local-service.yml"
    compose_file.write_text("- just\n- a\n- list\n")

    assert local_service_worker_environment_keys(compose_file=compose_file) is None


def test_unions_x_template_env_when_services_absent(tmp_path: Path) -> None:
    """The x-* fallback supplies the allowlist even if the worker service is absent.

    Mirrors a loader that leaves ``<<`` unexpanded: the robust fallback unions the
    ``x-*`` extension template ``environment`` keys so the anchor keys are still
    captured. Postgres env keys are excluded because only worker + x-* are walked.
    """
    compose_file = tmp_path / "local-service.yml"
    compose_file.write_text(
        "x-awf-service:\n"
        "  environment:\n"
        "    ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}\n"
        "    GH_TOKEN: ${GH_TOKEN:-}\n"
        "services:\n"
        "  postgres:\n"
        "    environment:\n"
        "      POSTGRES_PASSWORD: secret\n"
    )

    keys = local_service_worker_environment_keys(compose_file=compose_file)

    assert keys == frozenset({"ANTHROPIC_API_KEY", "GH_TOKEN"})


def test_x_template_only_without_services_key(tmp_path: Path) -> None:
    # No ``services`` mapping at all -> the x-* fallback is the sole source.
    compose_file = tmp_path / "local-service.yml"
    compose_file.write_text(
        "x-awf-service:\n  environment:\n    GH_TOKEN: ${GH_TOKEN:-}\n",
    )

    assert local_service_worker_environment_keys(compose_file=compose_file) == frozenset(
        {"GH_TOKEN"}
    )


def test_ignores_non_string_worker_environment_keys(tmp_path: Path) -> None:
    """Non-string mapping keys on the worker block are skipped, not raised on."""
    compose_file = tmp_path / "local-service.yml"
    compose_file.write_text(
        "services:\n  worker:\n    environment:\n      GH_TOKEN: x\n      123: numeric-key\n"
    )

    keys = local_service_worker_environment_keys(compose_file=compose_file)

    assert keys == frozenset({"GH_TOKEN"})


def test_ignores_non_string_and_empty_list_entries_in_x_fallback(tmp_path: Path) -> None:
    """Non-string list items and empty key names are skipped in the x-* fallback.

    The fallback only runs when the worker block yields no keys, so this exercises
    the list-form parser via an ``x-*`` template with no worker ``environment``.
    """
    compose_file = tmp_path / "local-service.yml"
    compose_file.write_text(
        "x-awf-service:\n"
        "  environment:\n"
        "    - ANTHROPIC_API_KEY=value\n"
        "    - 456\n"
        "    - =empty-name\n"
    )

    keys = local_service_worker_environment_keys(compose_file=compose_file)

    assert keys == frozenset({"ANTHROPIC_API_KEY"})


def test_x_fallback_skipped_when_worker_environment_present(tmp_path: Path) -> None:
    """An unrelated ``x-*`` env block must not widen the worker allowlist (PR #540).

    Regression for the review note: the ``x-*`` fallback runs ONLY when the worker
    block yields no keys (a loader that left ``<<`` unexpanded). When the worker's
    ``environment`` already resolved via merge-key expansion, a future non-worker
    extension template -- here ``x-debug-service`` -- must NOT leak its env keys
    into the worker's secret-lease allowlist.
    """
    compose_file = tmp_path / "local-service.yml"
    compose_file.write_text(
        "x-awf-service: &awf-service\n"
        "  environment: &awf-environment\n"
        "    GH_TOKEN: ${GH_TOKEN:-}\n"
        "x-debug-service:\n"
        "  environment:\n"
        "    DEBUG_ONLY_SECRET: ${DEBUG_ONLY_SECRET:-}\n"
        "services:\n"
        "  worker:\n"
        "    <<: *awf-service\n"
        '    command: sh -c "awf worker"\n'
    )

    keys = local_service_worker_environment_keys(compose_file=compose_file)

    assert keys is not None
    assert "GH_TOKEN" in keys
    # The unrelated x-debug-service env key never reaches the worker container.
    assert "DEBUG_ONLY_SECRET" not in keys


def test_real_local_service_yml_contains_expected_keys() -> None:
    """Guard the parse against future edits to the canonical compose file."""
    repo_compose = Path(__file__).resolve().parents[3] / "docker/compose/local-service.yml"

    keys = local_service_worker_environment_keys(compose_file=repo_compose)

    assert keys is not None
    assert {
        "ANTHROPIC_API_KEY",
        "BITBUCKET_API_TOKEN",
        "BITBUCKET_EMAIL",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "AWF_AGENT_RUNTIME_IMAGE",
    } <= keys
    # The worker never receives the postgres/console service env keys.
    assert "POSTGRES_PASSWORD" not in keys
    assert "NODE_ENV" not in keys
