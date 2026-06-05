"""Setup-status payload projection helper tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from awf.host_setup.config import ClientIntegrationConfig, HostSetupConfig, ProviderConfig
from awf.host_setup.source_assets import (
    SOURCE_CHECKOUT_INVALID,
    SourceCheckoutAssetMetadata,
)
from awf.mcp import setup_status_payload


@pytest.mark.unit
def test_setup_status_payload_projects_safe_provider_and_client_metadata() -> None:
    updated_at = datetime(2026, 6, 5, 12, 30, tzinfo=UTC)

    provider_statuses = setup_status_payload._provider_statuses(
        {
            "github": ProviderConfig(
                credential_ref="env://GITHUB_TOKEN",
                backend="env_ref",
                source="env",
                status="ready",
            ),
            "codex": ProviderConfig(status="missing"),
        }
    )
    client_statuses = setup_status_payload._client_statuses(
        {
            "claude": ClientIntegrationConfig(status="configured", updated_at=updated_at),
            "codex": ClientIntegrationConfig(status="not_configured"),
        }
    )

    assert provider_statuses == {
        "github": {
            "status": "ready",
            "backend": "env_ref",
            "source": "env",
            "credential_ref": {"present": True, "scheme": "env"},
        },
        "codex": {"status": "missing", "credential_ref": {"present": False}},
    }
    assert setup_status_payload._credential_ref_metadata("legacy-ref") == {"present": True}
    assert client_statuses == {
        "claude": {
            "status": "configured",
            "updated_at": "2026-06-05T12:30:00+00:00",
        },
        "codex": {"status": "not_configured"},
    }


@pytest.mark.unit
def test_setup_status_payload_filters_invalid_checks_and_issues() -> None:
    assert setup_status_payload._list_of_strings(("not", "a-list")) == []
    assert setup_status_payload._list_of_strings(["docker", 1, "postgres"]) == [
        "docker",
        "postgres",
    ]

    assert setup_status_payload._safe_setup_checks("not-a-list") == []
    assert setup_status_payload._safe_setup_checks(
        [
            {"name": "docker", "level": "ok", "detail": "ignored"},
            {"name": None, "level": "ok"},
            {"name": "postgres", "level": 1},
        ]
    ) == [{"name": "docker", "level": "ok"}]

    assert setup_status_payload._setup_status_issues({"not": "a-list"}) == []
    assert setup_status_payload._setup_status_issues(
        [
            {
                "reason_code": "DOCKER_MISSING",
                "severity": "blocked",
                "details": {"check": "docker", "token": "ignored"},
            },
            {"reason_code": None, "severity": "failed", "details": {"check": "ignored"}},
            {"reason_code": "BAD_SEVERITY", "severity": 1},
            {"reason_code": "NO_CHECK", "severity": "warning", "details": {}},
        ]
    ) == [
        {"reason_code": "DOCKER_MISSING", "severity": "blocked", "check": "docker"},
        {"reason_code": "NO_CHECK", "severity": "warning"},
    ]


@pytest.mark.unit
def test_setup_status_source_checkout_prefers_safe_probed_or_blocks_status(
    tmp_path: Path,
) -> None:
    persisted_root = tmp_path / "persisted-awf"
    probed_root = tmp_path / "probed-awf"
    persisted_at = datetime(2026, 6, 5, 8, 0, tzinfo=UTC)
    probed_at = "2026-06-05T08:05:00+00:00"
    config = HostSetupConfig(
        source_checkout=SourceCheckoutAssetMetadata(
            root=persisted_root,
            verified_at=persisted_at,
            markers=("pyproject.toml", "uv.lock"),
        )
    )
    details = {"source_checkout": {"root": str(probed_root), "verified_at": probed_at}}

    assert setup_status_payload._setup_status_source_checkout(config, {}, []) == {
        "present": True,
        "root": str(persisted_root),
        "verified_at": "2026-06-05T08:00:00+00:00",
        "marker_count": 2,
    }
    assert setup_status_payload._setup_status_source_checkout(
        config,
        {},
        [{"severity": "blocked", "details": {"check": "docker"}}],
    ) == {
        "present": True,
        "root": str(persisted_root),
        "verified_at": "2026-06-05T08:00:00+00:00",
        "marker_count": 2,
    }
    assert setup_status_payload._setup_status_source_checkout(
        config,
        details,
        [{"severity": "warning", "details": {"check": "source_checkout"}}],
        prefer_probed=True,
    ) == {
        "present": True,
        "root": str(probed_root),
        "verified_at": probed_at,
        "marker_count": None,
    }
    assert setup_status_payload._setup_status_source_checkout(
        config,
        details,
        [
            {
                "reason_code": SOURCE_CHECKOUT_INVALID,
                "severity": "blocked",
                "details": {"check": "source_checkout"},
            }
        ],
    ) == {"present": False}
    assert setup_status_payload._setup_status_source_checkout(
        HostSetupConfig(),
        {"source_checkout": {"root": str(probed_root)}},
        issues="not-a-list",
    ) == {"present": False}
