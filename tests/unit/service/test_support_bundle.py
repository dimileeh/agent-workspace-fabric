"""Tests for the telemetry-free support bundle module."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.host_setup.config import (
    ClientIntegrationConfig,
    ConsentConfig,
    HostSetupConfig,
    HostSetupConfigError,
    ProviderConfig,
)
from awf.host_setup.source_assets import (
    SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS,
    SourceCheckoutAssetMetadata,
)
from awf.service import support_bundle as support_bundle_mod
from awf.service.config import ServiceSettings
from awf.service.support_bundle import (
    BUNDLE_FILENAME_PREFIX,
    ISSUE_TEMPLATE_PATH,
    collect_support_bundle,
    write_support_bundle,
)


def _settings(
    tmp_path: Path,
    *,
    database_url: str = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf",
    api_token: str | None = None,
    work_dir: str | None = None,
) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url=database_url,
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work") if work_dir is None else work_dir,
        api_token=api_token,
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


class SecretEcho:
    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return f"opaque {self.value}"


@dataclass
class FailureSummaryPayload:
    generated_at: str
    since_hours: int
    latest_examples: list[object]
    root_cause_clusters: list[object]
    opaque_detail: object


@pytest.mark.unit
def test_support_bundle_collects_required_sections(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    async def _status_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        return _green_status()

    async def _doctor_collector(_: ServiceSettings, **_kw: object) -> DoctorReportProxy:
        return _green_doctor()

    async def _failure_collector(**_: object) -> dict[str, object]:
        return _mock_failure_summary()

    bundle = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={},
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=_failure_collector,
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
def test_support_bundle_log_pointers_omit_work_dir_path(tmp_path: Path) -> None:
    raw_work_dir = "/home/alice/client/.awf/service"
    settings = _settings(tmp_path, work_dir=raw_work_dir)

    async def _status_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        return _green_status()

    async def _doctor_collector(_: ServiceSettings, **_kw: object) -> DoctorReportProxy:
        return _green_doctor()

    async def _failure_collector(**_: object) -> dict[str, object]:
        return _mock_failure_summary()

    bundle = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={},
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=_failure_collector,
            setup_config_reader=lambda: HostSetupConfig(work_dir=raw_work_dir),
        )
    )

    assert bundle["log_pointers"] == [
        "Service logs: run `awf service logs --tail 100`",
        "Worker logs: run `awf service logs --service worker --tail 100`",
        "State directory: configured",
    ]
    config_fingerprint = bundle["config_fingerprint"]
    assert isinstance(config_fingerprint, dict)
    assert config_fingerprint["work_dir_configured"] is True
    assert "work_dir" not in config_fingerprint
    assert raw_work_dir not in json.dumps(bundle, sort_keys=True)


@pytest.mark.unit
def test_support_bundle_forwards_compose_context_to_collectors(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    compose_file = tmp_path / "docker" / "compose" / "local-service.yml"
    compose_env_file = tmp_path / ".env"
    base_environ = {"PATH": "/usr/bin", "AWF_CUSTOM_SERVICE_VALUE": "from-compose-env"}
    captured_status: dict[str, object] = {}
    captured_doctor: dict[str, object] = {}

    async def _status_collector(_: ServiceSettings, **kwargs: object) -> dict[str, object]:
        captured_status.update(kwargs)
        return _green_status()

    async def _doctor_collector(_: ServiceSettings, **kwargs: object) -> DoctorReportProxy:
        captured_doctor.update(kwargs)
        return _green_doctor()

    async def _failure_collector(**_: object) -> dict[str, object]:
        return _mock_failure_summary()

    asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ=base_environ,
            compose_file=compose_file,
            compose_env_file=compose_env_file,
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=_failure_collector,
        )
    )

    assert captured_status["environ"] == base_environ
    assert captured_status["compose_file"] == compose_file
    assert captured_status["compose_env_file"] == compose_env_file
    assert captured_doctor["environ"] == base_environ
    assert captured_doctor["compose_file"] == compose_file
    assert captured_doctor["compose_env_file"] == compose_env_file


@pytest.mark.unit
def test_support_bundle_forwards_explicit_null_compose_env_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    compose_file = tmp_path / "docker" / "compose" / "local-service.yml"
    captured_status: dict[str, object] = {}
    captured_doctor: dict[str, object] = {}

    async def _status_collector(_: ServiceSettings, **kwargs: object) -> dict[str, object]:
        captured_status.update(kwargs)
        return _green_status()

    async def _doctor_collector(_: ServiceSettings, **kwargs: object) -> DoctorReportProxy:
        captured_doctor.update(kwargs)
        return _green_doctor()

    async def _failure_collector(**_: object) -> dict[str, object]:
        return _mock_failure_summary()

    asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={},
            compose_file=compose_file,
            compose_env_file=None,
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=_failure_collector,
        )
    )

    assert captured_status["compose_file"] == compose_file
    assert captured_status["compose_env_file"] is None
    assert captured_doctor["compose_file"] == compose_file
    assert captured_doctor["compose_env_file"] is None


@pytest.mark.unit
def test_support_bundle_resolves_provider_environment_from_compose_env_file(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    compose_env_file = tmp_path / ".env"
    compose_token = "ghp_compose_bundle_token"
    compose_env_file.write_text(
        f"AWF_GITHUB_TOKEN={compose_token}\nGH_TOKEN=ghp_standard_compose_token\n",
        encoding="utf-8",
    )
    base_environ = {"PATH": "/usr/bin"}
    captured_status: dict[str, object] = {}
    captured_doctor: dict[str, object] = {}

    async def _status_collector(_: ServiceSettings, **kwargs: object) -> dict[str, object]:
        captured_status.update(kwargs)
        status = _green_status()
        checks = status["checks"]
        assert isinstance(checks, dict)
        checks["api"] = {
            "ok": False,
            "status": "fail",
            "reason": "API_UNREACHABLE",
            "detail": f"compose token {compose_token}",
        }
        return status

    async def _doctor_collector(_: ServiceSettings, **kwargs: object) -> DoctorReportProxy:
        captured_doctor.update(kwargs)
        return _green_doctor()

    async def _failure_collector(**_: object) -> dict[str, object]:
        return _mock_failure_summary()

    bundle = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset({"github"}),
            environ=base_environ,
            compose_env_file=compose_env_file,
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=_failure_collector,
        )
    )

    status_provider_env = captured_status["provider_environ"]
    doctor_provider_env = captured_doctor["provider_environ"]
    assert isinstance(status_provider_env, dict)
    assert isinstance(doctor_provider_env, dict)
    assert status_provider_env["AWF_GITHUB_TOKEN"] == compose_token
    assert doctor_provider_env["AWF_GITHUB_TOKEN"] == compose_token
    assert status_provider_env["PATH"] == "/usr/bin"
    assert doctor_provider_env["PATH"] == "/usr/bin"
    serialized = json.dumps(bundle, sort_keys=True)
    assert compose_token not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_support_bundle_redacts_secrets(tmp_path: Path) -> None:
    api_secret = "awf-api-bundle-secret"
    github_secret = "ghp_bundlesecret123456"
    db_secret = "bundle-db-secret"
    openai_secret = "sk-bundlesecret123456"
    settings = _settings(
        tmp_path,
        api_token=api_secret,
        database_url=f"postgresql+asyncpg://awf:{db_secret}@localhost:5433/awf",
    )

    status = _green_status()
    checks = status["checks"]
    assert isinstance(checks, dict)
    checks["api"] = {
        "ok": False,
        "status": "fail",
        "reason": "API_UNREACHABLE",
        "detail": (
            f"api_token={api_secret} token={openai_secret} "
            f"db=postgresql://awf:{db_secret}@localhost/awf"
        ),
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
        "message": f"bad token {github_secret} api={api_secret}",
    }

    async def _status_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        return status

    async def _doctor_collector(_: ServiceSettings, **_kw: object) -> DoctorReportProxy:
        report = _green_doctor()
        report.diagnostics[0]["metadata"] = {
            "api_token": api_secret,
            "database_url": f"postgresql://awf:{db_secret}@localhost/awf",
        }
        return report

    async def _failure_collector(**_: object) -> dict[str, object]:
        summary = _mock_failure_summary()
        summary["opaque_detail"] = (
            f"failure api_token={api_secret} db=postgresql://awf:{db_secret}@localhost/awf"
        )
        return summary

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
            failure_analysis_collector=_failure_collector,
        )
    )

    serialized = json.dumps(bundle, sort_keys=True)
    bundle_path = write_support_bundle(bundle, directory=tmp_path / "bundles")
    written = bundle_path.read_text()
    for secret in (api_secret, github_secret, db_secret, openai_secret):
        assert secret not in serialized
        assert secret not in written
    assert "<redacted>" in serialized
    assert "<redacted>" in written


@pytest.mark.unit
def test_support_bundle_includes_redacted_setup_state_for_credential_backends(
    tmp_path: Path,
) -> None:
    """Include setup-state metadata without leaking credential references."""
    settings = _settings(tmp_path)
    raw_refs = (
        "keyring://awf/github/default",
        "env://OPENAI_API_KEY",
        "plain-file:///home/user/.awf/secrets/codex.default",
    )
    source_root = tmp_path / "private" / "source-checkout"
    config = HostSetupConfig(
        providers={
            "github": ProviderConfig(
                credential_ref=raw_refs[0],
                backend="keyring",
                source="gh",
                status="ready",
            ),
            "openai": ProviderConfig(
                credential_ref=raw_refs[1],
                backend="env_ref",
                source="environment",
                status="ready",
            ),
            "codex": ProviderConfig(
                credential_ref=raw_refs[2],
                backend="plain_file",
                source="setup",
                status="ready",
            ),
        },
        clients={
            "codex": ClientIntegrationConfig(
                status="configured",
                updated_at=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
            )
        },
        consent=ConsentConfig(
            plain_file_secrets=True,
            source_checkout_assets=True,
        ),
        source_checkout=SourceCheckoutAssetMetadata(
            root=source_root,
            verified_at=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        ),
    )

    async def _status_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        """Return healthy service status for setup-state bundle collection."""
        return _green_status()

    async def _doctor_collector(_: ServiceSettings, **_kw: object) -> DoctorReportProxy:
        """Return a healthy doctor report for setup-state bundle collection."""
        return _green_doctor()

    async def _failure_collector(**_: object) -> dict[str, object]:
        """Return an empty recent-failure summary for setup-state tests."""
        return _mock_failure_summary()

    bundle = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={},
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=_failure_collector,
            setup_config_reader=lambda: config,
        )
    )

    setup_state = bundle["setup_state"]
    assert isinstance(setup_state, dict)
    assert setup_state["status"] == "loaded"
    assert setup_state["providers"] == {
        "github": {
            "status": "ready",
            "backend": "keyring",
            "source": "gh",
            "credential_ref_present": True,
            "credential_ref_kind": "keyring",
        },
        "openai": {
            "status": "ready",
            "backend": "env_ref",
            "source": "environment",
            "credential_ref_present": True,
            "credential_ref_kind": "env_ref",
        },
        "codex": {
            "status": "ready",
            "backend": "plain_file",
            "source": "setup",
            "credential_ref_present": True,
            "credential_ref_kind": "plain_file",
        },
    }
    assert setup_state["clients"] == {
        "codex": {
            "status": "configured",
            "updated_at": "2026-05-28T12:00:00Z",
        }
    }
    assert setup_state["consent"] == {
        "plain_file_secrets": True,
        "source_checkout_assets": True,
    }
    assert setup_state["source_checkout"] == {
        "configured": True,
        "verified_at": "2026-05-28T12:00:00Z",
        "marker_count": len(SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS),
    }

    serialized = json.dumps(bundle, sort_keys=True)
    bundle_path = write_support_bundle(bundle, directory=tmp_path / "bundles")
    written = bundle_path.read_text()
    for raw_ref in raw_refs:
        assert raw_ref not in serialized
        assert raw_ref not in written
    assert "/home/user/.awf/secrets/codex.default" not in serialized
    assert "/home/user/.awf/secrets/codex.default" not in written
    assert str(source_root) not in serialized
    assert str(source_root) not in written


@pytest.mark.unit
def test_support_bundle_setup_state_preserves_redacted_name_collisions() -> None:
    """Preserve setup-state entries whose names redact to the same marker."""
    provider_secret_one = "opaque-provider-name-one"
    provider_secret_two = "opaque-provider-name-two"
    client_secret_one = "opaque-client-name-one"
    client_secret_two = "opaque-client-name-two"
    config = HostSetupConfig(
        providers={
            provider_secret_one: ProviderConfig(status="ready-one"),
            provider_secret_two: ProviderConfig(status="ready-two"),
        },
        clients={
            client_secret_one: ClientIntegrationConfig(status="configured-one"),
            client_secret_two: ClientIntegrationConfig(status="configured-two"),
        },
    )
    secrets = frozenset(
        {
            provider_secret_one,
            provider_secret_two,
            client_secret_one,
            client_secret_two,
        }
    )

    setup_state = support_bundle_mod._setup_state(lambda: config, secrets=secrets)

    assert setup_state["status"] == "loaded"
    assert setup_state["providers"] == {
        "<redacted>": {
            "status": "ready-one",
            "backend": None,
            "source": None,
            "credential_ref_present": False,
            "credential_ref_kind": None,
        },
        "<redacted>#2": {
            "status": "ready-two",
            "backend": None,
            "source": None,
            "credential_ref_present": False,
            "credential_ref_kind": None,
        },
    }
    assert setup_state["clients"] == {
        "<redacted>": {
            "status": "configured-one",
            "updated_at": None,
        },
        "<redacted>#2": {
            "status": "configured-two",
            "updated_at": None,
        },
    }
    serialized = json.dumps(setup_state, sort_keys=True)
    for raw_name in secrets:
        assert raw_name not in serialized


@pytest.mark.unit
def test_isoformat_treats_naive_datetime_as_utc() -> None:
    """Treat naive support-bundle timestamps as UTC rather than host-local time."""
    if not hasattr(time, "tzset"):
        pytest.skip("tzset is required to exercise host-local timezone conversion")
    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        assert support_bundle_mod._isoformat(datetime(2026, 5, 28, 12, 0)) == (
            "2026-05-28T12:00:00Z"
        )
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


@pytest.mark.unit
def test_support_bundle_setup_state_redacts_config_load_errors(tmp_path: Path) -> None:
    """Redact host setup config read errors embedded in support bundles."""
    settings = _settings(tmp_path)
    plain_ref = "plain-file:///home/user/.awf/secrets/github.default"

    async def _status_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        """Return healthy service status for config-error bundle collection."""
        return _green_status()

    async def _doctor_collector(_: ServiceSettings, **_kw: object) -> DoctorReportProxy:
        """Return a healthy doctor report for config-error bundle collection."""
        return _green_doctor()

    async def _failure_collector(**_: object) -> dict[str, object]:
        """Return an empty recent-failure summary for config-error tests."""
        return _mock_failure_summary()

    def _config_reader() -> HostSetupConfig:
        """Raise a setup config error containing a credential reference."""
        raise HostSetupConfigError(
            reason_code="HOST_SETUP_CONFIG_CORRUPT",
            message=f"bad credential ref {plain_ref}",
            path=tmp_path / "home" / ".awf" / "config.yml",
            details={"credential_ref": plain_ref},
        )

    bundle = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={},
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=_failure_collector,
            setup_config_reader=_config_reader,
        )
    )

    setup_state = bundle["setup_state"]
    assert isinstance(setup_state, dict)
    assert setup_state["status"] == "failed"
    assert setup_state["reason_code"] == "HOST_SETUP_CONFIG_CORRUPT"
    serialized = json.dumps(bundle, sort_keys=True)
    assert plain_ref not in serialized
    assert "/home/user/.awf/secrets/github.default" not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_support_bundle_setup_state_degrades_unexpected_config_reader_errors(
    tmp_path: Path,
) -> None:
    """Record unexpected setup config reader failures without leaking refs."""
    settings = _settings(tmp_path)
    plain_ref = "plain-file:///home/user/.awf/secrets/github.default"

    class ConfigReaderError(RuntimeError):
        """Synthetic config reader exception carrying redacted details."""

        reason_code = "CONFIG_READER_FAILED"

        def __init__(self) -> None:
            """Populate the synthetic exception with secret-bearing details."""
            super().__init__(f"reader failed for {plain_ref}")
            self.details = {"credential_ref": plain_ref}

    async def _status_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        """Return healthy service status for unexpected-error bundle collection."""
        return _green_status()

    async def _doctor_collector(_: ServiceSettings, **_kw: object) -> DoctorReportProxy:
        """Return a healthy doctor report for unexpected-error bundle collection."""
        return _green_doctor()

    async def _failure_collector(**_: object) -> dict[str, object]:
        """Return an empty recent-failure summary for unexpected-error tests."""
        return _mock_failure_summary()

    def _config_reader() -> HostSetupConfig:
        """Raise an unexpected setup reader error with secret-bearing details."""
        raise ConfigReaderError()

    bundle = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={},
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=_failure_collector,
            setup_config_reader=_config_reader,
        )
    )

    setup_state = bundle["setup_state"]
    assert isinstance(setup_state, dict)
    assert setup_state["status"] == "failed"
    assert setup_state["reason_code"] == "CONFIG_READER_FAILED"
    assert setup_state["message"] == "reader failed for <redacted>"
    assert setup_state["details"] == {"credential_ref": "<redacted>"}
    assert bundle["service_status"] == _green_status()
    recent_failure_summary = bundle["recent_failure_summary"]
    assert isinstance(recent_failure_summary, dict)
    assert recent_failure_summary["since_hours"] == 24
    assert recent_failure_summary["total_failed_workspaces"] == 0
    assert recent_failure_summary["failure_groups"] == []
    serialized = json.dumps(bundle, sort_keys=True)
    assert plain_ref not in serialized
    assert "/home/user/.awf/secrets/github.default" not in serialized


@pytest.mark.unit
def test_support_bundle_setup_state_degrades_unexpected_config_reader_errors_without_reason_code() -> (
    None
):
    """Use the shared generic reader reason when unexpected errors lack one."""
    plain_ref = "plain-file:///home/user/.awf/secrets/github.default"

    def _config_reader() -> HostSetupConfig:
        """Raise an unexpected reader error without a reason code."""
        raise RuntimeError(f"reader failed for {plain_ref}")

    setup_state = support_bundle_mod._setup_state(
        _config_reader,
        secrets=frozenset({plain_ref}),
    )

    assert setup_state["status"] == "failed"
    assert (
        setup_state["reason_code"] == support_bundle_mod._HOST_SETUP_CONFIG_READ_FAILED_REASON_CODE
    )
    assert setup_state["message"] == "reader failed for <redacted>"


@pytest.mark.unit
def test_support_bundle_setup_state_degrades_loaded_config_summary_errors(
    tmp_path: Path,
) -> None:
    """Record loaded setup-state summary failures without leaking refs."""
    settings = _settings(tmp_path)
    plain_ref = "plain-file:///home/user/.awf/secrets/github.default"

    class ExplodingMarkers:
        """Synthetic malformed marker container for loaded config summaries."""

        def __len__(self) -> int:
            """Raise a secret-bearing error while summarizing marker count."""
            raise RuntimeError(f"marker summary failed for {plain_ref}")

    config = HostSetupConfig.model_construct(
        source_checkout=SimpleNamespace(
            verified_at=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
            markers=ExplodingMarkers(),
        )
    )

    async def _status_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        """Return healthy service status for summary-error bundle collection."""
        return _green_status()

    async def _doctor_collector(_: ServiceSettings, **_kw: object) -> DoctorReportProxy:
        """Return a healthy doctor report for summary-error bundle collection."""
        return _green_doctor()

    async def _failure_collector(**_: object) -> dict[str, object]:
        """Return an empty recent-failure summary for summary-error tests."""
        return _mock_failure_summary()

    bundle = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={},
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=_failure_collector,
            setup_config_reader=lambda: config,
        )
    )

    setup_state = bundle["setup_state"]
    assert isinstance(setup_state, dict)
    assert setup_state["status"] == "failed"
    assert setup_state["reason_code"] == "HOST_SETUP_CONFIG_SUMMARY_FAILED"
    assert setup_state["message"] == "marker summary failed for <redacted>"
    assert bundle["service_status"] == _green_status()
    recent_failure_summary = bundle["recent_failure_summary"]
    assert isinstance(recent_failure_summary, dict)
    assert recent_failure_summary["since_hours"] == 24
    assert recent_failure_summary["total_failed_workspaces"] == 0
    assert recent_failure_summary["failure_groups"] == []
    serialized = json.dumps(bundle, sort_keys=True)
    assert plain_ref not in serialized
    assert "/home/user/.awf/secrets/github.default" not in serialized


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

    async def _failure_collector(**_: object) -> dict[str, object]:
        return failure_summary

    bundle_a = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={},
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=_failure_collector,
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
            failure_analysis_collector=_failure_collector,
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


@pytest.mark.unit
def test_support_bundle_degrades_when_status_and_doctor_collectors_fail(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    secret = "sk-support-bundle-secret"

    async def _status_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        raise RuntimeError(f"status failed with {secret}")

    async def _doctor_collector(_: ServiceSettings, **_kw: object) -> DoctorReportProxy:
        raise RuntimeError(f"doctor failed with {secret}")

    async def _failure_collector(**_: object) -> dict[str, object]:
        return _mock_failure_summary()

    bundle = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={"OPENAI_API_KEY": secret},
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=_failure_collector,
        )
    )

    assert bundle["provider_readiness_summary"] == {"status": "fail"}
    service_status = bundle["service_status"]
    assert isinstance(service_status, dict)
    assert service_status["status"] == "fail"
    assert service_status["detail"] == "status failed with <redacted>"
    doctor_report = bundle["doctor_report"]
    assert isinstance(doctor_report, dict)
    assert doctor_report["status"] == "fail"
    assert doctor_report["detail"] == "doctor failed with <redacted>"


@pytest.mark.unit
def test_support_bundle_accepts_plain_doctor_mapping_and_non_mapping_checks(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    async def _status_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        return {
            "service": "awf",
            "status": "ok",
            "checks": "not-a-check-map",
            "agent_readiness": {"status": "warn", "providers": {}},
        }

    async def _doctor_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        return {
            "service": "awf",
            "status": "warn",
            "summary": {"ok": 0, "warn": 1, "fail": 0},
            "diagnostics": [{"id": "config", "status": "warn"}],
        }

    async def _failure_collector(**_: object) -> dict[str, object]:
        return _mock_failure_summary()

    bundle = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={},
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=_failure_collector,
        )
    )

    assert bundle["doctor_report"] == {
        "service": "awf",
        "status": "warn",
        "summary": {"ok": 0, "warn": 1, "fail": 0},
        "diagnostics": [{"id": "config", "status": "warn"}],
    }
    assert bundle["orphan_cleanup_posture"] == {
        "workspace_cleanup": {},
        "orphan_resources": {},
        "stranded_workspaces": {},
    }


@pytest.mark.unit
def test_support_bundle_sanitizes_dataclass_failure_summary(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    secret = "sk-support-dataclass-secret"
    summary = FailureSummaryPayload(
        generated_at=datetime.now(UTC).isoformat(),
        since_hours=24,
        latest_examples=[
            {
                "workspace_id": "ws_secret",
                "failure_reason": "agent_failure",
                "reason_code": "AGENT_FAILURE",
                "status": "failed",
                "updated_at": datetime.now(UTC).isoformat(),
                "task_prompt": f"secret prompt {secret}",
            },
            f"raw example {secret}",
        ],
        root_cause_clusters=[
            {
                "failure_reason": "agent_failure",
                "reason_code": "AGENT_FAILURE",
                "count": 1,
                "sample_workspace_ids": ["ws_secret"],
                "stdout": f"secret output {secret}",
            },
            SecretEcho(secret),
        ],
        opaque_detail=SecretEcho(secret),
    )

    async def _status_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        return _green_status()

    async def _doctor_collector(_: ServiceSettings, **_kw: object) -> DoctorReportProxy:
        return _green_doctor()

    async def _failure_collector(**_: object) -> FailureSummaryPayload:
        return summary

    bundle = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={"OPENAI_API_KEY": secret},
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=_failure_collector,
        )
    )

    recent = bundle["recent_failure_summary"]
    assert isinstance(recent, dict)
    assert recent["opaque_detail"] == "opaque <redacted>"
    assert recent["latest_examples"][0] == {
        "workspace_id": "ws_secret",
        "failure_reason": "agent_failure",
        "reason_code": "AGENT_FAILURE",
        "status": "failed",
        "updated_at": summary.latest_examples[0]["updated_at"],
    }
    assert recent["latest_examples"][1] == {}
    assert recent["root_cause_clusters"][0] == {
        "failure_reason": "agent_failure",
        "reason_code": "AGENT_FAILURE",
        "count": 1,
        "sample_workspace_ids": ["ws_secret"],
    }
    assert recent["root_cause_clusters"][1] == {}
    assert secret not in json.dumps(bundle, sort_keys=True, default=str)


@pytest.mark.unit
def test_support_bundle_marks_opaque_failure_summary_degraded(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    secret = "sk-support-opaque-secret"

    async def _status_collector(_: ServiceSettings, **_kw: object) -> dict[str, object]:
        return _green_status()

    async def _doctor_collector(_: ServiceSettings, **_kw: object) -> DoctorReportProxy:
        return _green_doctor()

    async def _failure_collector(**_: object) -> SecretEcho:
        return SecretEcho(secret)

    bundle = asyncio.run(
        collect_support_bundle(
            settings,
            strict_providers=frozenset(),
            provider_environ={},
            environ={"OPENAI_API_KEY": secret},
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
            failure_analysis_collector=_failure_collector,
        )
    )

    recent = bundle["recent_failure_summary"]
    assert isinstance(recent, dict)
    assert recent == {"error": "opaque <redacted>", "degraded": True}


@pytest.mark.unit
def test_support_bundle_uses_default_failure_analysis_collector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    calls: dict[str, object] = {}

    class FakeEngine:
        async def dispose(self) -> None:
            calls["disposed"] = True

    fake_engine = FakeEngine()
    fake_session_factory = object()

    def _make_engine(database_url: str) -> FakeEngine:
        calls["database_url"] = database_url
        return fake_engine

    def _make_session_factory(engine: FakeEngine) -> object:
        calls["session_engine"] = engine
        return fake_session_factory

    async def _summarize_failure_analysis(
        session_factory: object, *, since_hours: int
    ) -> dict[str, object]:
        calls["session_factory"] = session_factory
        calls["since_hours"] = since_hours
        return _mock_failure_summary()

    monkeypatch.setattr(support_bundle_mod, "make_engine", _make_engine)
    monkeypatch.setattr(support_bundle_mod, "make_session_factory", _make_session_factory)
    monkeypatch.setattr(
        support_bundle_mod,
        "summarize_failure_analysis",
        _summarize_failure_analysis,
    )

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
            failure_window_hours=72,
            status_collector=_status_collector,
            doctor_collector=_doctor_collector,
        )
    )

    assert calls == {
        "database_url": settings.database_url,
        "session_engine": fake_engine,
        "session_factory": fake_session_factory,
        "since_hours": 72,
        "disposed": True,
    }
    assert bundle["recent_failure_summary"]["since_hours"] == 24
