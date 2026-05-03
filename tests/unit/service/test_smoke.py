"""Unit tests for collect_smoke_report() smoke service."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from awf.service.smoke import (
    _default_config_resolver,
    _default_service_collector,
    _extract_validation_commands,
    _phase_service_readiness,
    collect_smoke_report,
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
                "github": {"ok": True, "status": "ok", "reason": "GITHUB_AUTH_OK", "message": "GitHub is ready.", "credential_sources": ["env:GH_TOKEN"], "credential_scope": "read+write+pr", "isolation": "process", "warnings": []},
                "codex": {"ok": True, "status": "ok", "reason": "CODEX_AUTH_OK", "message": "Codex is ready.", "credential_sources": ["env:OPENAI_API_KEY"], "credential_scope": "read+write", "isolation": "process", "warnings": []},
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
            smoke_request = {"repo": {"url": path.as_uri()}, "task": {}, "workspace": {}, "validation": {}, "resources": {}}
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


@pytest.mark.unit
class TestCollectSmokeReportLiveMode:
    async def test_all_phases_pass_with_valid_project(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        (tmp_path / "alembic.ini").write_text("[alembic]\n")

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        assert report["status"] in ("ok", "warn")
        assert report["project"] == str(tmp_path)
        assert report["mode"] == "live"
        assert len(report["phases"]) == 7

        phase_names = [p["name"] for p in report["phases"]]
        assert "service_readiness" in phase_names
        assert "auth_readiness" in phase_names
        assert "profile_preview" in phase_names
        assert "validation" in phase_names
        assert "workspace_request" in phase_names
        assert "pr_monitor" in phase_names
        assert "console_links" in phase_names

        for phase in report["phases"]:
            assert isinstance(phase["reason_code"], str)
            assert phase["reason_code"].startswith("SMOKE_")
            if phase["name"] == "pr_monitor":
                assert phase["status"] == "warn"
                assert phase["reason_code"] == "SMOKE_PR_UNAVAILABLE"
            else:
                assert phase["status"] == "ok"

        assert report["console_links"]["ui"] == "http://localhost:3000"
        assert report["console_links"]["api_docs"] == "http://localhost:8000/docs"

    async def test_all_phases_ok_when_github_token_configured(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        (tmp_path / "alembic.ini").write_text("[alembic]\n")

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(github_token="ghp_test_token"),
            mocked_local=False,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        pr_phase = next(p for p in report["phases"] if p["name"] == "pr_monitor")
        assert pr_phase["status"] == "ok"
        assert pr_phase["reason_code"] == "SMOKE_PR_READY"
        assert report["status"] == "ok"

    async def test_service_unreachable_produces_fail_phase_with_action(self, tmp_path: Path) -> None:
        async def _unreachable_service(settings, *, http_client=None):
            return {"status": "unreachable"}

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_unreachable_service,
            auth_collector=_ok_auth_collector(),
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        assert report["status"] in ("warn", "fail")
        service_phase = next(p for p in report["phases"] if p["name"] == "service_readiness")
        assert service_phase["status"] in ("warn", "fail")
        assert service_phase["reason_code"] == "SMOKE_SERVICE_UNREACHABLE"
        assert len(report["next_actions"]) >= 1

    async def test_partial_auth_reports_warn(self, tmp_path: Path) -> None:
        def _partial_auth(settings, *, environ=None, strict_providers=None, **kwargs):
            return {
                "status": "fail",
                "strict_providers": [],
                "providers": {
                    "github": {"ok": False, "status": "fail", "reason": "GITHUB_TOKEN_ENV_MISSING", "message": "No token.", "credential_sources": [], "credential_scope": "none", "isolation": "process", "warnings": ["missing token"]},
                    "codex": {"ok": True, "status": "ok", "reason": "CODEX_AUTH_OK", "message": "Codex ready.", "credential_sources": ["env:OPENAI_API_KEY"], "credential_scope": "read+write", "isolation": "process", "warnings": []},
                },
                "security": {},
            }

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_ok_service_collector(),
            auth_collector=_partial_auth,
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        auth_phase = next(p for p in report["phases"] if p["name"] == "auth_readiness")
        assert auth_phase["reason_code"] == "SMOKE_AUTH_PARTIAL"
        assert auth_phase["status"] == "warn"

    async def test_auth_status_ok_but_partial_providers_downgrades_to_warn(self, tmp_path: Path) -> None:
        def _ok_status_partial_providers(settings, *, environ=None, strict_providers=None, **kwargs):
            return {
                "status": "ok",
                "strict_providers": [],
                "providers": {
                    "github": {"ok": True, "status": "ok", "reason": "GITHUB_AUTH_OK", "message": "GitHub ready.", "credential_sources": [], "credential_scope": "none", "isolation": "process", "warnings": []},
                    "codex": {"ok": False, "status": "warn", "reason": "CODEX_AUTH_MISSING", "message": "Codex missing.", "credential_sources": [], "credential_scope": "none", "isolation": "process", "warnings": []},
                },
                "security": {},
            }

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_status_partial_providers,
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        auth_phase = next(p for p in report["phases"] if p["name"] == "auth_readiness")
        assert auth_phase["reason_code"] == "SMOKE_AUTH_PARTIAL"
        assert auth_phase["status"] == "warn"

    async def test_profile_phase_detects_template(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"dependencies": {"next": "^14"}}')

        def _nextjs_preview(path, template="auto", include_smoke_request=False):
            from awf.profiles.models import WorkspaceProfile
            from awf.profiles.onboarding import (
                DraftProfile,
                PreviewDiagnostics,
                ProjectInspection,
                ProjectOnboardingPreview,
            )

            inspection = ProjectInspection(
                path=path,
                detected_template="node-nextjs",
                confidence="high",
                signals=("package.json", "next.config.js"),
            )
            draft = DraftProfile(
                template="node-nextjs",
                profile=WorkspaceProfile(name="node-nextjs", source="onboarding:node-nextjs", confidence="high"),
                yaml="test: yaml",
                diagnostics=PreviewDiagnostics((), (), (), (), ()),
            )
            return ProjectOnboardingPreview(
                path=path,
                inspection=inspection,
                draft=draft,
                diagnostics=draft.diagnostics,
                smoke_request={},
            )

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_nextjs_preview,
            config_resolver=_config_resolver(),
        )

        profile_phase = next(p for p in report["phases"] if p["name"] == "profile_preview")
        assert profile_phase["status"] == "ok"
        assert profile_phase["reason_code"] == "SMOKE_PROFILE_READY"
        evidence = profile_phase.get("evidence", {})
        assert evidence.get("template") == "node-nextjs"


@pytest.mark.unit
class TestCollectSmokeReportMockedMode:
    async def test_mocked_mode_force_flag_produces_mocked_reason(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        assert report["mode"] == "mocked_local"
        pr_phase = next(p for p in report["phases"] if p["name"] == "pr_monitor")
        assert pr_phase["reason_code"] == "SMOKE_PR_MOCKED_LOCAL"
        assert pr_phase["status"] == "ok"

    async def test_mocked_mode_succeeds_without_live_providers(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

        def _no_auth(settings, *, environ=None, strict_providers=None, **kwargs):
            return {
                "status": "fail",
                "strict_providers": [],
                "providers": {},
                "security": {},
            }

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_no_auth,
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        assert report["status"] in ("ok", "warn")

    async def test_console_links_phase_produces_expected_urls(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(api_base_url="http://127.0.0.1:8000"),
        )

        assert report["console_links"]["ui"] == "http://localhost:3000"
        assert report["console_links"]["api_docs"] == "http://127.0.0.1:8000/docs"

    async def test_demo_project_missing_produces_reason_code(self, tmp_path: Path) -> None:
        missing_project = tmp_path / "nonexistent"

        report = await collect_smoke_report(
            project=missing_project,
            settings=_settings(),
            mocked_local=True,
            demo_path=None,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            config_resolver=_config_resolver(),
        )

        service_phase = next(p for p in report["phases"] if p["name"] == "service_readiness")
        assert service_phase["status"] == "ok"

        after_service = report["phases"][1:]
        demo_missing_phase = next(
            (p for p in after_service if p["reason_code"] == "SMOKE_DEMO_PROJECT_MISSING"),
            None,
        )
        assert demo_missing_phase is not None, (
            f"Expected SMOKE_DEMO_PROJECT_MISSING in phases after service, got {[p['reason_code'] for p in after_service]}"
        )
        assert demo_missing_phase["status"] == "fail"
        assert str(missing_project) in demo_missing_phase["message"]

    async def test_demo_path_fallback_used_when_project_does_not_resolve(self, tmp_path: Path) -> None:
        missing_project = tmp_path / "nonexistent"
        (tmp_path / "demo_project").mkdir()
        (tmp_path / "demo_project" / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        demo_path = tmp_path / "demo_project"

        report = await collect_smoke_report(
            project=missing_project,
            settings=_settings(),
            mocked_local=True,
            demo_path=demo_path,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            config_resolver=_config_resolver(),
        )

        profile_phase = next(p for p in report["phases"] if p["name"] == "profile_preview")
        assert profile_phase["status"] in ("ok", "warn")
        assert profile_phase["reason_code"] in ("SMOKE_PROFILE_READY", "SMOKE_PROFILE_NOT_DETECTED")

    async def test_demo_path_fallback_used_when_project_exists_but_has_no_profile(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "existing_no_profile"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]\nname = 'no-profile'\n")
        (tmp_path / "demo_project").mkdir()
        (tmp_path / "demo_project" / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        demo_path = tmp_path / "demo_project"

        report = await collect_smoke_report(
            project=project,
            settings=_settings(),
            mocked_local=True,
            demo_path=demo_path,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            config_resolver=_config_resolver(),
        )

        profile_phase = next(p for p in report["phases"] if p["name"] == "profile_preview")
        assert profile_phase["status"] in ("ok", "warn")
        assert profile_phase["reason_code"] in ("SMOKE_PROFILE_READY", "SMOKE_PROFILE_NOT_DETECTED")
        assert report["project"] == str(demo_path)

    async def test_no_validation_commands_reports_validation_missing(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

        from awf.profiles.models import WorkspaceProfile
        from awf.profiles.onboarding import (
            DraftProfile,
            PreviewDiagnostics,
            ProjectInspection,
            ProjectOnboardingPreview,
        )

        def _profile_no_validation(path, template="auto", include_smoke_request=False):
            inspection = ProjectInspection(
                path=path,
                detected_template="generic",
                confidence="low",
                signals=(),
            )
            draft = DraftProfile(
                template="generic",
                profile=WorkspaceProfile(name="generic", source="onboarding:generic", confidence="low"),
                yaml="test: yaml",
                diagnostics=PreviewDiagnostics((), (), (), (), ()),
            )
            return ProjectOnboardingPreview(
                path=path,
                inspection=inspection,
                draft=draft,
                diagnostics=draft.diagnostics,
            )

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_profile_no_validation,
            config_resolver=_config_resolver(),
        )

        validation_phase = next(p for p in report["phases"] if p["name"] == "validation")
        assert validation_phase["reason_code"] == "SMOKE_VALIDATION_MISSING"

    async def test_workspace_request_ready_when_smoke_request_valid(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

        def _profile_with_smoke(path, template="auto", include_smoke_request=False):
            from awf.profiles.models import WorkspaceProfile
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
                profile=WorkspaceProfile(name="python", source="onboarding:python", confidence="medium"),
                yaml="test: yaml",
                diagnostics=PreviewDiagnostics((), (), (), (), ()),
            )
            sr = {} if include_smoke_request else None
            return ProjectOnboardingPreview(
                path=path, inspection=inspection, draft=draft, diagnostics=draft.diagnostics, smoke_request=sr
            )

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_profile_with_smoke,
            config_resolver=_config_resolver(),
        )

        ws_phase = next(p for p in report["phases"] if p["name"] == "workspace_request")
        assert ws_phase["reason_code"] == "SMOKE_WORKSPACE_REQUEST_READY"
        assert ws_phase["status"] == "ok"

    async def test_console_unavailable_reports_reason_code(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

        def _no_console(settings):
            return {"api_base_url": "http://localhost:8000", "console_url": None}

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_no_console,
        )

        console_phase = next(p for p in report["phases"] if p["name"] == "console_links")
        assert console_phase["reason_code"] == "SMOKE_CONSOLE_UNAVAILABLE"


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
                    "github": {"ok": False, "status": "fail", "reason": "GITHUB_TOKEN_ENV_MISSING", "message": "No token.", "credential_sources": [], "credential_scope": "none", "isolation": "process", "warnings": []},
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

    async def test_auth_status_ok_but_zero_usable_providers_is_not_ready(self, tmp_path: Path) -> None:
        def _ok_status_no_usable(settings, *, environ=None, **kwargs):
            return {
                "status": "ok",
                "strict_providers": [],
                "providers": {
                    "github": {"ok": False, "status": "warn", "reason": "GITHUB_TOKEN_ENV_MISSING", "message": "No token.", "credential_sources": [], "credential_scope": "none", "isolation": "process", "warnings": []},
                    "codex": {"ok": False, "status": "warn", "reason": "CODEX_KEY_MISSING", "message": "No key.", "credential_sources": [], "credential_scope": "none", "isolation": "process", "warnings": []},
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
        assert "cannot read directory" in profile_phase["message"]

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
                path=path, detected_template="python", confidence="medium", signals=("pyproject.toml",)
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
        )

        assert report["console_links"]["api_docs"] == "http://localhost:8000/docs"
        assert report["console_links"]["ui"] == "unavailable"

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
                path=path, detected_template="python", confidence="medium", signals=("pyproject.toml",)
            )
            draft = DraftProfile(
                template="python", profile=NullProfile(), yaml="",
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

    async def test_service_readiness_warns_in_mocked_with_unreachable_status(
        self, tmp_path: Path
    ) -> None:
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
        assert service_phase["status"] == "warn"
        assert service_phase["reason_code"] == "SMOKE_SERVICE_UNREACHABLE"

    async def test_default_service_collector_returns_ok_on_200(
        self,
    ) -> None:
        with patch("httpx.AsyncClient") as mock_cls:
            mock_get = AsyncMock()
            mock_get.return_value = SimpleNamespace(status_code=200)
            mock_instance = SimpleNamespace()
            mock_instance.get = mock_get
            mock_cls.return_value.__aenter__.return_value = mock_instance

            result = await _default_service_collector(_settings())
        assert result["status"] == "ok"

    async def test_default_service_collector_returns_unreachable_on_503(
        self,
    ) -> None:
        with patch("httpx.AsyncClient") as mock_cls:
            mock_get = AsyncMock()
            mock_get.return_value = SimpleNamespace(status_code=503)
            mock_instance = SimpleNamespace()
            mock_instance.get = mock_get
            mock_cls.return_value.__aenter__.return_value = mock_instance

            result = await _default_service_collector(_settings())
        assert result["status"] == "unreachable"

    async def test_default_service_collector_error_bubbles_to_phase_handler(
        self,
    ) -> None:
        with patch("httpx.AsyncClient") as mock_cls:
            mock_get = AsyncMock()
            mock_get.side_effect = RuntimeError("boom")
            mock_instance = SimpleNamespace()
            mock_instance.get = mock_get
            mock_cls.return_value.__aenter__.return_value = mock_instance

            result = await _phase_service_readiness(
                _settings(), mocked_local=False, service_collector=None
            )
        assert result["status"] == "fail"
        assert result["reason_code"] == "SMOKE_SERVICE_UNREACHABLE"
        assert "boom" in result["evidence"]["error"]

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
                path=path, detected_template="python", confidence="medium", signals=("pyproject.toml",)
            )
            draft = DraftProfile(
                template="python", profile=BrokenPhases(), yaml="",
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
