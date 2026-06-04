"""Unit tests for collect_smoke_report() smoke service."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from awf.service.smoke import (
    _default_config_resolver,
    _default_console_checker,
    _default_disk_profile_preview,
    _default_service_collector,
    _extract_validation_commands,
    _phase_service_readiness,
    collect_smoke_report,
)
from awf.service.worker_heartbeat import (
    worker_heartbeat_age_seconds,
    worker_heartbeat_is_fresh,
    worker_heartbeat_stale_after_seconds,
    worker_heartbeat_write_interval_seconds,
)


def _settings(github_token: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        api_base_url="http://localhost:8000",
        console_url=None,
        database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf",
        github_token=github_token,
    )


def _ok_service_collector():
    async def _fn(settings, *, http_client=None):
        return {"status": "ok"}

    return _fn


def _ok_auth_collector():
    def _fn(settings, *, environ=None, strict_providers=None, **kwargs):
        return {
            "status": "ok",
            "strict_providers": [],
            "providers": {
                "github": {
                    "ok": True,
                    "status": "ok",
                    "reason": "GITHUB_AUTH_OK",
                    "message": "GitHub is ready.",
                    "credential_sources": ["env:GH_TOKEN"],
                    "credential_scope": "read+write+pr",
                    "isolation": "process",
                    "warnings": [],
                },
                "codex": {
                    "ok": True,
                    "status": "ok",
                    "reason": "CODEX_AUTH_OK",
                    "message": "Codex is ready.",
                    "credential_sources": ["env:OPENAI_API_KEY"],
                    "credential_scope": "read+write",
                    "isolation": "process",
                    "warnings": [],
                },
            },
            "security": {},
        }

    return _fn


def _ok_profile_preview_ok():
    def _fn(path, template="auto", include_smoke_request=False):
        from awf.profiles.models import ProfileCommand, WorkspaceProfile
        from awf.profiles.onboarding import (
            DraftProfile,
            PreviewDiagnostics,
            ProjectInspection,
            ProjectOnboardingPreview,
        )

        inspection = ProjectInspection(
            path=path,
            detected_template="python-postgres",
            confidence="medium",
            signals=("pyproject.toml", "alembic.ini"),
        )
        draft = DraftProfile(
            template="python-postgres",
            profile=WorkspaceProfile(
                name="python-postgres",
                source="onboarding:python-postgres",
                confidence="medium",
                phases={
                    "validate": [ProfileCommand(command="pytest -q")],
                },
            ),
            yaml="test: yaml",
            diagnostics=PreviewDiagnostics(
                missing_services=(),
                missing_secrets=(),
                missing_ports=(),
                missing_validation_commands=(),
                missing_healthchecks=(),
            ),
        )
        smoke_request = {}
        if include_smoke_request:
            smoke_request = {
                "repo": {"url": path.as_uri()},
                "task": {},
                "workspace": {},
                "validation": {},
                "resources": {},
            }
        return ProjectOnboardingPreview(
            path=path,
            inspection=inspection,
            draft=draft,
            diagnostics=draft.diagnostics,
            smoke_request=smoke_request,
        )

    return _fn


def _config_resolver(api_base_url="http://localhost:8000"):
    def _fn(settings):
        return {"api_base_url": api_base_url, "console_url": "http://localhost:3000"}

    return _fn


@pytest.fixture(autouse=True)
def _stub_default_console_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fn(_url: str) -> bool:
        return True

    monkeypatch.setattr("awf.service.smoke._default_console_checker", _fn)


def _console_checker(reachable: bool, expected_url: str = "http://localhost:3000"):
    async def _fn(url: str) -> bool:
        assert url == expected_url
        return reachable

    return _fn


@pytest.mark.unit
def test_worker_heartbeat_helpers_handle_invalid_poll_and_naive_datetimes() -> None:
    now = datetime(2026, 6, 4, tzinfo=UTC)
    naive_same_instant = datetime(2026, 6, 4)

    assert worker_heartbeat_stale_after_seconds("not-a-number") == 15.0  # type: ignore[arg-type]
    assert worker_heartbeat_write_interval_seconds("not-a-number") == 1.0  # type: ignore[arg-type]
    assert worker_heartbeat_age_seconds(naive_same_instant, now=now) == 0.0
    assert worker_heartbeat_is_fresh(
        naive_same_instant,
        now=now,
        poll_interval_seconds=None,
    )


@pytest.mark.unit
class TestCollectSmokeReportExceptionPaths:
    async def test_service_collector_exception_returns_fail_phase(self, tmp_path: Path) -> None:
        async def _failing_service(settings, *, http_client=None):
            raise ConnectionError("connection refused")

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_failing_service,
            auth_collector=_ok_auth_collector(),
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        service_phase = next(p for p in report["phases"] if p["name"] == "service_readiness")
        assert service_phase["status"] == "fail"
        assert service_phase["reason_code"] == "SMOKE_SERVICE_UNREACHABLE"
        assert "connection refused" in service_phase["message"]

    async def test_auth_collector_exception_returns_warn_in_mocked(self, tmp_path: Path) -> None:
        def _failing_auth(settings, *, environ=None, **kwargs):
            raise RuntimeError("cannot inspect")

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_failing_auth,
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        auth_phase = next(p for p in report["phases"] if p["name"] == "auth_readiness")
        assert auth_phase["status"] == "warn"
        assert auth_phase["reason_code"] == "SMOKE_AUTH_UNAVAILABLE"

    async def test_auth_collector_exception_fails_in_live_mode(self, tmp_path: Path) -> None:
        def _failing_auth(settings, *, environ=None, **kwargs):
            raise RuntimeError("cannot inspect")

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_ok_service_collector(),
            auth_collector=_failing_auth,
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        auth_phase = next(p for p in report["phases"] if p["name"] == "auth_readiness")
        assert auth_phase["status"] == "fail"
        assert auth_phase["reason_code"] == "SMOKE_AUTH_UNAVAILABLE"

    async def test_auth_completely_unavailable_in_live_mode_fails(self, tmp_path: Path) -> None:
        def _no_usable_auth(settings, *, environ=None, **kwargs):
            return {
                "status": "fail",
                "strict_providers": [],
                "providers": {
                    "github": {
                        "ok": False,
                        "status": "fail",
                        "reason": "GITHUB_TOKEN_ENV_MISSING",
                        "message": "No token.",
                        "credential_sources": [],
                        "credential_scope": "none",
                        "isolation": "process",
                        "warnings": [],
                    },
                },
                "security": {},
            }

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_ok_service_collector(),
            auth_collector=_no_usable_auth,
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        auth_phase = next(p for p in report["phases"] if p["name"] == "auth_readiness")
        assert auth_phase["reason_code"] == "SMOKE_AUTH_UNAVAILABLE"
        assert auth_phase["status"] == "fail"

    async def test_auth_status_ok_but_zero_usable_providers_is_not_ready(
        self, tmp_path: Path
    ) -> None:
        def _ok_status_no_usable(settings, *, environ=None, **kwargs):
            return {
                "status": "ok",
                "strict_providers": [],
                "providers": {
                    "github": {
                        "ok": False,
                        "status": "warn",
                        "reason": "GITHUB_TOKEN_ENV_MISSING",
                        "message": "No token.",
                        "credential_sources": [],
                        "credential_scope": "none",
                        "isolation": "process",
                        "warnings": [],
                    },
                    "codex": {
                        "ok": False,
                        "status": "warn",
                        "reason": "CODEX_KEY_MISSING",
                        "message": "No key.",
                        "credential_sources": [],
                        "credential_scope": "none",
                        "isolation": "process",
                        "warnings": [],
                    },
                },
                "security": {},
            }

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_status_no_usable,
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        auth_phase = next(p for p in report["phases"] if p["name"] == "auth_readiness")
        assert auth_phase["reason_code"] == "SMOKE_AUTH_UNAVAILABLE"
        assert auth_phase["status"] == "fail"

    async def test_profile_removed_between_checks_returns_preview_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Return preview failure when a disk profile disappears before loading."""
        profile_path = tmp_path / ".awf" / "workspace.yml"
        profile_path.parent.mkdir()
        profile_path.write_text(
            "awf:\n  name: generic\n  phases:\n    validate:\n      - pytest -q\n",
            encoding="utf-8",
        )
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

        state = {"calls": 0}

        def _flaky_has_profile(_path: Path) -> bool:
            state["calls"] += 1
            if state["calls"] == 1:
                profile_path.unlink()
                return True
            return False

        monkeypatch.setattr("awf.service.smoke._project_has_awf_profile", _flaky_has_profile)

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=None,
            config_resolver=_config_resolver(),
        )

        profile_phase = next(p for p in report["phases"] if p["name"] == "profile_preview")
        assert profile_phase["status"] == "fail"
        assert profile_phase["reason_code"] == "SMOKE_PROFILE_PREVIEW_FAILED"
        assert profile_phase["action"] == (
            "Fix the on-disk workspace profile (.awf/workspace.yml, .awf/workspace.yaml, "
            "awf.workspace.yml, or awf.workspace.yaml) so it passes schema validation and lint "
            "checks."
        )

    async def test_profile_preview_exception_returns_fail(self, tmp_path: Path) -> None:
        def _failing_preview(project):
            raise OSError("cannot read directory")

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_failing_preview,
            config_resolver=_config_resolver(),
        )

        profile_phase = next(p for p in report["phases"] if p["name"] == "profile_preview")
        assert profile_phase["status"] == "fail"
        assert profile_phase["reason_code"] == "SMOKE_PROFILE_PREVIEW_FAILED"
        assert profile_phase.get("evidence", {}).get("error")

    async def test_extract_validation_commands_returns_empty_when_draft_profile_is_none(
        self, tmp_path: Path
    ) -> None:
        def _preview_no_draft_profile(path, template="auto", include_smoke_request=False):
            from awf.profiles.onboarding import (
                DraftProfile,
                PreviewDiagnostics,
                ProjectInspection,
                ProjectOnboardingPreview,
            )

            inspection = ProjectInspection(
                path=path, detected_template="generic", confidence="low", signals=()
            )
            draft = DraftProfile(
                template="generic",
                profile=None,
                yaml="",
                diagnostics=PreviewDiagnostics((), (), (), (), ()),
            )
            return ProjectOnboardingPreview(
                path=path, inspection=inspection, draft=draft, diagnostics=draft.diagnostics
            )

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_preview_no_draft_profile,
            config_resolver=_config_resolver(),
        )

        validation_phase = next(p for p in report["phases"] if p["name"] == "validation")
        assert validation_phase["reason_code"] == "SMOKE_VALIDATION_MISSING"

    async def test_extract_validation_commands_returns_empty_on_attribute_error(
        self, tmp_path: Path
    ) -> None:
        class BrokenPreview:
            @property
            def draft(self):
                raise AttributeError("boom")

        def _broken_preview(project):
            return BrokenPreview()

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_broken_preview,
            config_resolver=_config_resolver(),
        )

        validation_phase = next(p for p in report["phases"] if p["name"] == "validation")
        assert validation_phase["reason_code"] == "SMOKE_VALIDATION_MISSING"

    async def test_workspace_request_fails_when_draft_profile_is_none(self, tmp_path: Path) -> None:
        def _preview_no_profile(path, template="auto", include_smoke_request=False):
            from awf.profiles.onboarding import (
                DraftProfile,
                PreviewDiagnostics,
                ProjectInspection,
                ProjectOnboardingPreview,
            )

            inspection = ProjectInspection(
                path=path,
                detected_template="python",
                confidence="medium",
                signals=("pyproject.toml",),
            )
            draft = DraftProfile(
                template="python",
                profile=None,
                yaml="",
                diagnostics=PreviewDiagnostics((), (), (), (), ()),
            )
            return ProjectOnboardingPreview(
                path=path, inspection=inspection, draft=draft, diagnostics=draft.diagnostics
            )

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_preview_no_profile,
            config_resolver=_config_resolver(),
        )

        ws_phase = next(p for p in report["phases"] if p["name"] == "workspace_request")
        assert ws_phase["reason_code"] == "SMOKE_WORKSPACE_REQUEST_FAILED"

    async def test_default_config_resolver_returns_console_none_via_none_injection(
        self, tmp_path: Path
    ) -> None:
        def _null_config(settings):
            return {"api_base_url": "http://localhost:8000", "console_url": None}

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_null_config,
            console_checker=_console_checker(False),
        )

        assert report["console_links"]["api_docs"] == "http://localhost:8000/docs"
        assert report["console_links"]["ui"] == "http://localhost:3000"

    async def test_next_actions_collected_from_phases(self, tmp_path: Path) -> None:
        async def _bad_service(settings, *, http_client=None):
            return {"status": "unreachable"}

        def _bad_auth(settings, *, environ=None, **kwargs):
            raise RuntimeError("auth fail")

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_bad_service,
            auth_collector=_bad_auth,
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        assert len(report["next_actions"]) >= 2

    async def test_extract_validation_commands_when_profile_phases_is_none(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

        class NullProfile:
            phases = None

        def _preview_no_phases(path, template="auto", include_smoke_request=False):
            from awf.profiles.onboarding import (
                DraftProfile,
                PreviewDiagnostics,
                ProjectInspection,
                ProjectOnboardingPreview,
            )

            inspection = ProjectInspection(
                path=path,
                detected_template="python",
                confidence="medium",
                signals=("pyproject.toml",),
            )
            draft = DraftProfile(
                template="python",
                profile=NullProfile(),
                yaml="",
                diagnostics=PreviewDiagnostics((), (), (), (), ()),
            )
            return ProjectOnboardingPreview(
                path=path, inspection=inspection, draft=draft, diagnostics=draft.diagnostics
            )

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_preview_no_phases,
            config_resolver=_config_resolver(),
        )

        validation_phase = next(p for p in report["phases"] if p["name"] == "validation")
        assert validation_phase["reason_code"] == "SMOKE_VALIDATION_MISSING"

    async def test_workspace_request_fails_on_exception_in_smoke_request(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

        with patch("awf.profiles.onboarding._smoke_request", side_effect=RuntimeError("fail")):
            report = await collect_smoke_report(
                project=tmp_path,
                settings=_settings(),
                mocked_local=True,
                service_collector=_ok_service_collector(),
                auth_collector=_ok_auth_collector(),
                profile_preview=_ok_profile_preview_ok(),
                config_resolver=_config_resolver(),
            )

        ws_phase = next(p for p in report["phases"] if p["name"] == "workspace_request")
        assert ws_phase["reason_code"] == "SMOKE_WORKSPACE_REQUEST_FAILED"

    async def test_service_readiness_fails_in_mocked_with_unreachable_status(
        self, tmp_path: Path
    ) -> None:
        """No false-green: an unhealthy local Core stays a hard fail in mocked mode.

        Regression lock for the T10 headline bug. The no-token proof relaxes only
        provider/PR requirements; local Core health is never downgraded to warn.
        """

        async def _unreachable_svc(settings, *, http_client=None):
            return {"status": "down"}

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_unreachable_svc,
            auth_collector=_ok_auth_collector(),
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        service_phase = next(p for p in report["phases"] if p["name"] == "service_readiness")
        assert service_phase["status"] == "fail"
        assert service_phase["reason_code"] == "SMOKE_SERVICE_UNREACHABLE"
        assert report["status"] == "fail"

    async def test_service_readiness_ok_in_mocked_exposes_api_and_worker_evidence(
        self, tmp_path: Path
    ) -> None:
        """Mocked success keeps Core health real and proves it via sub-statuses."""

        async def _healthy_svc(settings, *, http_client=None):
            return {
                "status": "ok",
                "api": "ok",
                "worker": "ok",
                "worker_reason": "WORKER_HEARTBEAT_FRESH",
            }

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_healthy_svc,
            auth_collector=_ok_auth_collector(),
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        service_phase = next(p for p in report["phases"] if p["name"] == "service_readiness")
        assert service_phase["status"] == "ok"
        assert service_phase["reason_code"] == "SMOKE_SERVICE_READY"
        assert service_phase["evidence"]["api"] == "ok"
        assert service_phase["evidence"]["worker"] == "ok"
        assert service_phase["evidence"]["worker_reason"] == "WORKER_HEARTBEAT_FRESH"
        # PR path is mocked (no GitHub authority handed over); overall not fail.
        pr_phase = next(p for p in report["phases"] if p["name"] == "pr_monitor")
        assert pr_phase["reason_code"] == "SMOKE_PR_MOCKED_LOCAL"
        assert report["status"] != "fail"

    async def test_service_readiness_fails_when_worker_heartbeat_stale_in_mocked(
        self, tmp_path: Path
    ) -> None:
        """API up but worker heartbeat stale is a hard fail even in mocked mode."""

        async def _worker_down_svc(settings, *, http_client=None):
            return {
                "status": "degraded",
                "api": "ok",
                "worker": "fail",
                "worker_reason": "WORKER_HEARTBEAT_STALE",
                "worker_detail": "Latest worker heartbeat is 60.0s old",
            }

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_worker_down_svc,
            auth_collector=_ok_auth_collector(),
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        service_phase = next(p for p in report["phases"] if p["name"] == "service_readiness")
        assert service_phase["status"] == "fail"
        assert service_phase["reason_code"] == "SMOKE_WORKER_UNAVAILABLE"
        assert service_phase["evidence"]["api"] == "ok"
        assert service_phase["evidence"]["worker"] == "fail"
        assert service_phase["evidence"]["worker_reason"] == "WORKER_HEARTBEAT_STALE"
        assert service_phase["evidence"]["worker_detail"] == (
            "Latest worker heartbeat is 60.0s old"
        )
        assert report["status"] == "fail"

    async def test_service_readiness_plain_ok_collector_stays_ok(self, tmp_path: Path) -> None:
        """Backward compat: a plain ``{"status": "ok"}`` collector is ok."""

        async def _plain_ok_svc(settings, *, http_client=None):
            return {"status": "ok"}

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_plain_ok_svc,
            auth_collector=_ok_auth_collector(),
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        service_phase = next(p for p in report["phases"] if p["name"] == "service_readiness")
        assert service_phase["status"] == "ok"
        assert service_phase["reason_code"] == "SMOKE_SERVICE_READY"
        # The collector never probed the heartbeat, so it must not be claimed as
        # reachable/ok - surface it as unknown / not probed instead.
        assert service_phase["evidence"]["worker"] == "unknown"
        assert service_phase["evidence"]["worker_reason"] == "unknown"
        assert "not probed" in service_phase["message"]
        assert "heartbeat fresh" not in service_phase["message"]

    async def test_default_service_collector_returns_ok_when_worker_fresh(
        self,
    ) -> None:
        """`/healthz` 200 + `/readyz` `checks.worker.ok=true` proves worker liveness."""

        async def _get(url: str):
            if url.endswith("/healthz"):
                return SimpleNamespace(status_code=200)
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "checks": {
                        "db": {"ok": True},
                        "worker": {"ok": True, "reason": "WORKER_HEARTBEAT_FRESH"},
                    }
                },
            )

        with patch("httpx.AsyncClient") as mock_cls:
            mock_instance = SimpleNamespace()
            mock_instance.get = _get
            mock_cls.return_value.__aenter__.return_value = mock_instance

            result = await _default_service_collector(_settings())
        assert result["status"] == "ok"
        assert result["api"] == "ok"
        assert result["worker"] == "ok"
        assert result["worker_reason"] == "WORKER_HEARTBEAT_FRESH"

    async def test_default_service_collector_worker_fail_when_heartbeat_stale_even_on_503(
        self,
    ) -> None:
        """`/readyz` 503 (providers missing) but readable body ⇒ stale worker fails.

        Proves the provider-free worker heartbeat signal works while the overall
        /readyz status is 503 because no provider token is configured.
        """

        async def _get(url: str):
            if url.endswith("/healthz"):
                return SimpleNamespace(status_code=200)
            return SimpleNamespace(
                status_code=503,
                json=lambda: {
                    "checks": {
                        "db": {"ok": True},
                        "worker": {
                            "ok": False,
                            "reason": "WORKER_HEARTBEAT_STALE",
                            "detail": "Latest worker heartbeat is 60.0s old",
                        },
                    }
                },
            )

        with patch("httpx.AsyncClient") as mock_cls:
            mock_instance = SimpleNamespace()
            mock_instance.get = _get
            mock_cls.return_value.__aenter__.return_value = mock_instance

            result = await _default_service_collector(_settings())
        assert result["status"] == "degraded"
        assert result["api"] == "ok"
        assert result["worker"] == "fail"
        assert result["worker_reason"] == "WORKER_HEARTBEAT_STALE"
        assert result["worker_detail"] == "Latest worker heartbeat is 60.0s old"

    async def test_default_service_collector_ok_when_worker_fresh_even_on_503(
        self,
    ) -> None:
        """`/readyz` 503 (providers missing) but `checks.worker.ok=true` ⇒ ok.

        This is the headline no-token proof: providers are unconfigured (so the
        overall ``/readyz`` status is 503) yet the worker heartbeat is fresh, which
        must still report ``status: ok``. Locks in the success side of the 503
        branch so a future HTTP-status guard cannot silently regress it.
        """

        async def _get(url: str):
            if url.endswith("/healthz"):
                return SimpleNamespace(status_code=200)
            return SimpleNamespace(
                status_code=503,
                json=lambda: {
                    "checks": {
                        "db": {"ok": True},
                        "worker": {"ok": True, "reason": "WORKER_HEARTBEAT_FRESH"},
                    }
                },
            )

        with patch("httpx.AsyncClient") as mock_cls:
            mock_instance = SimpleNamespace()
            mock_instance.get = _get
            mock_cls.return_value.__aenter__.return_value = mock_instance

            result = await _default_service_collector(_settings())
        assert result["status"] == "ok"
        assert result["api"] == "ok"
        assert result["worker"] == "ok"
        assert result["worker_reason"] == "WORKER_HEARTBEAT_FRESH"

    async def test_default_service_collector_fails_closed_when_worker_check_missing(
        self,
    ) -> None:
        """A DB-ok `/readyz` body without `checks.worker` cannot prove worker liveness."""

        async def _get(url: str):
            if url.endswith("/healthz"):
                return SimpleNamespace(status_code=200)
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"checks": {"db": {"ok": True}}},
            )

        with patch("httpx.AsyncClient") as mock_cls:
            mock_instance = SimpleNamespace()
            mock_instance.get = _get
            mock_cls.return_value.__aenter__.return_value = mock_instance

            result = await _default_service_collector(_settings())
        assert result["status"] == "degraded"
        assert result["api"] == "ok"
        assert result["worker"] == "fail"
        assert result["worker_reason"] == "WORKER_HEARTBEAT_UNAVAILABLE"

    async def test_default_service_collector_returns_unreachable_on_503_healthz(
        self,
    ) -> None:
        """A non-200 `/healthz` is unreachable without consulting `/readyz`."""

        async def _get(url: str):
            assert url.endswith("/healthz")
            return SimpleNamespace(status_code=503)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_instance = SimpleNamespace()
            mock_instance.get = _get
            mock_cls.return_value.__aenter__.return_value = mock_instance

            result = await _default_service_collector(_settings())
        assert result["status"] == "unreachable"

    async def test_default_service_collector_worker_fail_when_readyz_raises(
        self,
    ) -> None:
        """`/readyz` failing while `/healthz` is up still fails worker evidence."""
        import httpx

        async def _get(url: str):
            if url.endswith("/healthz"):
                return SimpleNamespace(status_code=200)
            raise httpx.ConnectError("readyz refused")

        with patch("httpx.AsyncClient") as mock_cls:
            mock_instance = SimpleNamespace()
            mock_instance.get = _get
            mock_cls.return_value.__aenter__.return_value = mock_instance

            result = await _default_service_collector(_settings())
        assert result["status"] == "degraded"
        assert result["api"] == "ok"
        assert result["worker"] == "fail"
        assert result["worker_reason"] == "WORKER_HEARTBEAT_UNAVAILABLE"

    async def test_default_service_collector_error_bubbles_to_phase_handler(
        self,
    ) -> None:
        with patch("httpx.AsyncClient") as mock_cls:
            mock_get = AsyncMock()
            mock_get.side_effect = RuntimeError("boom")
            mock_instance = SimpleNamespace()
            mock_instance.get = mock_get
            mock_cls.return_value.__aenter__.return_value = mock_instance

            result = await _phase_service_readiness(_settings(), service_collector=None)
        assert result["status"] == "fail"
        assert result["reason_code"] == "SMOKE_SERVICE_UNREACHABLE"
        assert "boom" in result["evidence"]["error"]

    async def test_default_console_checker_rejects_client_error_status(self) -> None:
        with patch("httpx.AsyncClient") as mock_cls:
            mock_get = AsyncMock()
            mock_get.return_value = SimpleNamespace(status_code=404)
            mock_instance = SimpleNamespace()
            mock_instance.get = mock_get
            mock_cls.return_value.__aenter__.return_value = mock_instance

            result = await _default_console_checker("http://localhost:3000")

        assert result is False

    async def test_default_console_checker_treats_malformed_url_as_unreachable(self) -> None:
        result = await _default_console_checker("http://localhost:badport")

        assert result is False

    async def test_default_console_checker_accepts_success_status(self) -> None:
        with patch("httpx.AsyncClient") as mock_cls:
            mock_get = AsyncMock()
            mock_get.return_value = SimpleNamespace(status_code=200)
            mock_instance = SimpleNamespace()
            mock_instance.get = mock_get
            mock_cls.return_value.__aenter__.return_value = mock_instance

            result = await _default_console_checker("http://localhost:3000")

        assert result is True

    async def test_extract_validation_commands_direct_call_no_phases(self) -> None:
        class NoDraftPreview:
            draft = None

        result = _extract_validation_commands(NoDraftPreview())
        assert result == []

    def test_default_config_resolver_direct_call(self) -> None:
        result = _default_config_resolver(_settings())
        assert result["api_base_url"] == "http://localhost:8000"
        assert "console_url" not in result

    def test_default_auth_collector_direct_call(self) -> None:
        with patch(
            "awf.service.provider_readiness.collect_agent_readiness",
            return_value={"status": "ok", "providers": {}, "security": {}},
        ):
            from awf.service.smoke import _default_auth_collector

            result = _default_auth_collector(_settings())
        assert result["status"] == "ok"

    def test_default_profile_preview_direct_call(self) -> None:
        (Path("/tmp") / "pyproject.toml").write_text("[project]\nname='test'\n")
        with patch(
            "awf.profiles.onboarding.preview_project_onboarding",
            return_value=SimpleNamespace(template="python"),
        ):
            from awf.service.smoke import _default_profile_preview

            result = _default_profile_preview(Path("/tmp"))
        assert result is not None

    def test_default_disk_profile_preview_direct_call(self, tmp_path: Path) -> None:
        """Call the on-disk profile preview helper and return the parsed preview."""
        workspace_dir = tmp_path / ".awf"
        workspace_dir.mkdir()
        (workspace_dir / "workspace.yml").write_text(
            "awf:\n  name: generic\n  phases:\n    validate:\n      - pytest -q\n",
            encoding="utf-8",
        )

        result = _default_disk_profile_preview(tmp_path)
        assert result.draft.template == "generic"
        assert result.draft.yaml
        assert "awf:" in result.draft.yaml
        assert result.inspection.detected_template == "generic"

    def test_default_disk_profile_preview_resolves_forge_from_remote(self, tmp_path: Path) -> None:
        """The checkout's bitbucket origin drives ``forge: auto`` resolution to bitbucket."""
        workspace_dir = tmp_path / ".awf"
        workspace_dir.mkdir()
        (workspace_dir / "workspace.yml").write_text(
            "awf:\n  name: generic\n  phases:\n    validate:\n      - pytest -q\n",
            encoding="utf-8",
        )

        captured: dict[str, object] = {}

        def _capture_forge(_project, resolution):
            captured["forge"] = resolution.profile.forge
            return SimpleNamespace(draft=SimpleNamespace(template="generic", yaml=""))

        with (
            patch(
                "awf.common.git_remote.detect_repo_url_from_checkout",
                return_value="https://bitbucket.org/o/r.git",
            ),
            patch(
                "awf.profiles.onboarding.preview_workspace_profile",
                side_effect=_capture_forge,
            ),
        ):
            preview = _default_disk_profile_preview(tmp_path)

        assert captured["forge"] == "bitbucket"
        assert preview.draft.template == "generic"

    async def test_workspace_request_fails_when_smoke_returns_non_dict(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

        with patch("awf.profiles.onboarding._smoke_request", return_value="not_a_dict"):
            report = await collect_smoke_report(
                project=tmp_path,
                settings=_settings(),
                mocked_local=True,
                service_collector=_ok_service_collector(),
                auth_collector=_ok_auth_collector(),
                profile_preview=_ok_profile_preview_ok(),
                config_resolver=_config_resolver(),
            )

        ws_phase = next(p for p in report["phases"] if p["name"] == "workspace_request")
        assert ws_phase["reason_code"] == "SMOKE_WORKSPACE_REQUEST_FAILED"

    async def test_extract_validation_commands_catches_exception_on_broken_phases(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

        class BrokenPhases:
            @property
            def phases(self):
                raise RuntimeError("broken")

        def _broken_phases_preview(path, template="auto", include_smoke_request=False):
            from awf.profiles.onboarding import (
                DraftProfile,
                PreviewDiagnostics,
                ProjectInspection,
                ProjectOnboardingPreview,
            )

            inspection = ProjectInspection(
                path=path,
                detected_template="python",
                confidence="medium",
                signals=("pyproject.toml",),
            )
            draft = DraftProfile(
                template="python",
                profile=BrokenPhases(),
                yaml="",
                diagnostics=PreviewDiagnostics((), (), (), (), ()),
            )
            return ProjectOnboardingPreview(
                path=path, inspection=inspection, draft=draft, diagnostics=draft.diagnostics
            )

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_broken_phases_preview,
            config_resolver=_config_resolver(),
        )

        validation_phase = next(p for p in report["phases"] if p["name"] == "validation")
        assert validation_phase["reason_code"] == "SMOKE_VALIDATION_MISSING"
