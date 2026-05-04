"""Tests for the telemetry-free support bundle module."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from awf.service.config import ServiceSettings
from awf.service.support_bundle import (
    BUNDLE_FILENAME_PREFIX,
    ISSUE_TEMPLATE_PATH,
    collect_support_bundle,
    write_support_bundle,
)


def _settings(tmp_path: Path) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf",
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home"),
    )


def _green_status() -> dict[str, object]:
    return {
        "service": "awf",
        "status": "ok",
        "checks": {
            "api": {"ok": True, "status": "ok", "version": "test"},
            "docker": {"ok": True, "status": "ok", "version": "27.0.3"},
            "workspace_cleanup": {
                "ok": True,
                "status": "ok",
                "reason": "NO_CLEANUP_CANDIDATES",
                "candidate_count": 0,
                "preserved_count": 0,
                "examples": [],
            },
            "orphan_resources": {
                "ok": True,
                "status": "ok",
                "reason": "NO_ORPHANS",
                "orphan_count": 0,
                "examples": [],
            },
            "stranded_workspaces": {
                "ok": True,
                "status": "ok",
                "reason": "NO_STRANDED_WORKSPACES",
                "stranded_count": 0,
                "examples": [],
            },
            "network_posture": {
                "ok": True,
                "status": "ok",
                "reason": "NETWORK_POSTURE_NO_ACTIVE_OPEN",
                "active_counts_by_posture": {
                    "restricted": 0,
                    "offline": 0,
                    "open": 0,
                    "unknown": 0,
                },
                "open_examples": [],
            },
        },
        "agent_readiness": {
            "status": "ok",
            "strict_providers": [],
            "providers": {
                "github": {
                    "ok": True,
                    "status": "ok",
                    "reason": "GITHUB_AUTH_OK",
                    "message": "GitHub CLI auth is usable.",
                },
            },
        },
    }


class DoctorReportProxy:
    def __init__(self, service: str, status: str, diagnostics: list[dict[str, object]]) -> None:
        self.service = service
        self.status = status
        self.diagnostics = diagnostics

    def to_dict(self) -> dict[str, object]:
        return {
            "service": self.service,
            "status": self.status,
            "summary": {
                "ok": sum(1 for d in self.diagnostics if d.get("status") == "ok"),
                "warn": sum(1 for d in self.diagnostics if d.get("status") == "warn"),
                "fail": sum(1 for d in self.diagnostics if d.get("status") == "fail"),
            },
            "diagnostics": self.diagnostics,
        }


def _green_doctor() -> DoctorReportProxy:
    return DoctorReportProxy(
        service="awf",
        status="ok",
        diagnostics=[
            {
                "id": "docker",
                "label": "Docker",
                "status": "ok",
                "reason": "DOCKER_OK",
                "message": "Docker daemon is reachable.",
                "action": "No action required.",
                "source": "checks.docker",
                "metadata": {},
            }
        ],
    )


def _mock_failure_summary() -> dict[str, object]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_start": datetime.now(UTC).isoformat(),
        "since_hours": 24,
        "total_failed_workspaces": 0,
        "failure_groups": [],
        "latest_examples": [],
        "root_cause_clusters": [],
    }


@pytest.mark.unit
def test_support_bundle_collects_required_sections(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    async def _status_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        return _green_status()

    async def _doctor_collector(_: ServiceSettings, **_kw: object) -> DoctorReportProxy:
        return _green_doctor()

    bundle = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={},
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=lambda **_: _mock_failure_summary(),
        )
    )

    assert "generated_at" in bundle
    assert "version" in bundle
    assert "service_status" in bundle
    assert "doctor_report" in bundle
    assert "provider_readiness_summary" in bundle
    assert "orphan_cleanup_posture" in bundle
    assert "recent_failure_summary" in bundle
    assert "config_fingerprint" in bundle
    assert "log_pointers" in bundle
    assert "issue_template_pointer" in bundle
    assert bundle["issue_template_pointer"] == ISSUE_TEMPLATE_PATH


@pytest.mark.unit
def test_support_bundle_redacts_secrets(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    github_secret = "ghp_bundlesecret123456"
    db_secret = "bundle-db-secret"
    openai_secret = "sk-bundlesecret123456"

    status = _green_status()
    checks = status["checks"]
    assert isinstance(checks, dict)
    checks["api"] = {
        "ok": False,
        "status": "fail",
        "reason": "API_UNREACHABLE",
        "detail": f"token={openai_secret} db=postgresql://awf:{db_secret}@localhost/awf",
        openai_secret: "value under secret key",
    }
    readiness = status["agent_readiness"]
    assert isinstance(readiness, dict)
    providers = readiness["providers"]
    assert isinstance(providers, dict)
    providers["github"] = {
        "ok": False,
        "status": "fail",
        "reason": "GITHUB_AUTH_UNUSABLE",
        "message": f"bad token {github_secret}",
    }

    async def _status_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        return status

    async def _doctor_collector(_: ServiceSettings, **_kw: object) -> DoctorReportProxy:
        return _green_doctor()

    bundle = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={
                "OPENAI_API_KEY": openai_secret,
                "AWF_GITHUB_TOKEN": github_secret,
            },
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=lambda **_: _mock_failure_summary(),
        )
    )

    serialized = json.dumps(bundle, sort_keys=True)
    for secret in (github_secret, db_secret, openai_secret):
        assert secret not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_support_bundle_omits_raw_prompts_and_outputs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    def _failure_summary() -> dict[str, object]:
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "window_start": datetime.now(UTC).isoformat(),
            "since_hours": 24,
            "total_failed_workspaces": 2,
            "failure_groups": [
                {
                    "failure_reason": "agent_failure",
                    "count": 1,
                    "retryable": True,
                    "recommended_action": "Retry",
                }
            ],
            "latest_examples": [
                {
                    "workspace_id": "ws_1",
                    "failure_reason": "agent_failure",
                    "reason_code": "AGENT_FAILURE",
                    "status": "failed",
                    "updated_at": datetime.now(UTC).isoformat(),
                    "count": 1,
                    "task_prompt": "Fix this code",
                    "stdout": "output",
                    "stderr": "error",
                }
            ],
            "root_cause_clusters": [
                {
                    "failure_reason": "agent_failure",
                    "reason_code": "AGENT_FAILURE",
                    "count": 1,
                    "sample_workspace_ids": ["ws_1"],
                    "task_prompt": "Fix this code",
                    "stdout": "output",
                }
            ],
        }

    async def _failure_summary_async(**_: object) -> dict[str, object]:
        return _failure_summary()

    async def _status_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        return _green_status()

    async def _doctor_collector(_: ServiceSettings, **_kw: object) -> DoctorReportProxy:
        return _green_doctor()

    bundle = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={},
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=_failure_summary_async,
        )
    )

    recent = bundle["recent_failure_summary"]
    assert isinstance(recent, dict)
    examples = recent.get("latest_examples")
    assert isinstance(examples, list) and examples
    safe_keys = {"workspace_id", "failure_reason", "reason_code", "status", "updated_at", "count"}
    assert set(examples[0].keys()) <= safe_keys
    clusters = recent.get("root_cause_clusters")
    assert isinstance(clusters, list) and clusters
    safe_cluster_keys = {"failure_reason", "reason_code", "count", "sample_workspace_ids"}
    assert set(clusters[0].keys()) <= safe_cluster_keys


@pytest.mark.unit
def test_support_bundle_writes_readable_artifact(tmp_path: Path) -> None:
    bundle = {
        "generated_at": datetime.now(UTC).isoformat(),
        "version": "0.1.0",
        "service_status": {"status": "ok"},
    }
    out_dir = tmp_path / "bundles"
    path = write_support_bundle(bundle, directory=out_dir)

    assert path.exists()
    assert path.is_absolute()
    assert path.parent == out_dir
    assert path.name.startswith(BUNDLE_FILENAME_PREFIX)
    assert path.suffix == ".json"

    loaded = json.loads(path.read_text())
    assert loaded["version"] == "0.1.0"


@pytest.mark.unit
def test_support_bundle_is_repeatable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    failure_summary = _mock_failure_summary()

    async def _status_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        return _green_status()

    async def _doctor_collector(_: ServiceSettings, **_kw: object) -> DoctorReportProxy:
        return _green_doctor()

    bundle_a = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={},
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=lambda **_: failure_summary,
        )
    )
    bundle_b = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={},
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=lambda **_: failure_summary,
        )
    )

    # Remove runtime-only fields that are expected to differ
    for b in (bundle_a, bundle_b):
        b.pop("generated_at", None)

    assert json.dumps(bundle_a, sort_keys=True, default=str) == json.dumps(
        bundle_b, sort_keys=True, default=str
    )


@pytest.mark.unit
def test_support_bundle_degrades_when_db_unreachable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    async def _bad_failure_collector(**_: object) -> dict[str, object]:
        raise RuntimeError("DB is down")

    async def _status_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        return _green_status()

    async def _doctor_collector(_: ServiceSettings, **_kw: object) -> DoctorReportProxy:
        return _green_doctor()

    bundle = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={},
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=_bad_failure_collector,
        )
    )

    recent = bundle["recent_failure_summary"]
    assert isinstance(recent, dict)
    assert recent.get("degraded") is True
    assert "DB is down" in str(recent.get("error", ""))
