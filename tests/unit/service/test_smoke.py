"""Unit tests for collect_smoke_report() smoke service."""

from __future__ import annotations

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


def _settings(github_token: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        api_base_url="http://localhost:8000",
        console_url=None,
        database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf",
        github_token=github_token,
    )


@pytest.mark.unit
def test_default_config_resolver_includes_console_url_when_configured() -> None:
    settings = _settings()
    settings.console_url = "http://localhost:3000"

    assert _default_config_resolver(settings) == {
        "api_base_url": "http://localhost:8000",
        "console_url": "http://localhost:3000",
    }


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

    async def test_service_unreachable_produces_fail_phase_with_action(
        self, tmp_path: Path
    ) -> None:
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
                    "github": {
                        "ok": False,
                        "status": "fail",
                        "reason": "GITHUB_TOKEN_ENV_MISSING",
                        "message": "No token.",
                        "credential_sources": [],
                        "credential_scope": "none",
                        "isolation": "process",
                        "warnings": ["missing token"],
                    },
                    "codex": {
                        "ok": True,
                        "status": "ok",
                        "reason": "CODEX_AUTH_OK",
                        "message": "Codex ready.",
                        "credential_sources": ["env:OPENAI_API_KEY"],
                        "credential_scope": "read+write",
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
            auth_collector=_partial_auth,
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_config_resolver(),
        )

        auth_phase = next(p for p in report["phases"] if p["name"] == "auth_readiness")
        assert auth_phase["reason_code"] == "SMOKE_AUTH_PARTIAL"
        assert auth_phase["status"] == "warn"

    async def test_auth_status_ok_but_partial_providers_downgrades_to_warn(
        self, tmp_path: Path
    ) -> None:
        def _ok_status_partial_providers(
            settings, *, environ=None, strict_providers=None, **kwargs
        ):
            return {
                "status": "ok",
                "strict_providers": [],
                "providers": {
                    "github": {
                        "ok": True,
                        "status": "ok",
                        "reason": "GITHUB_AUTH_OK",
                        "message": "GitHub ready.",
                        "credential_sources": [],
                        "credential_scope": "none",
                        "isolation": "process",
                        "warnings": [],
                    },
                    "codex": {
                        "ok": False,
                        "status": "warn",
                        "reason": "CODEX_AUTH_MISSING",
                        "message": "Codex missing.",
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
                profile=WorkspaceProfile(
                    name="node-nextjs", source="onboarding:node-nextjs", confidence="high"
                ),
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

    async def test_profile_preview_prefers_on_disk_profile(self, tmp_path: Path) -> None:
        """Ensure on-disk workspace profile path wins over autodetection."""
        (tmp_path / "apps" / "api").mkdir(parents=True)
        (tmp_path / "apps" / "worker").mkdir(parents=True)
        (tmp_path / ".awf").mkdir()
        (tmp_path / ".awf" / "workspace.yml").write_text(
            "awf:\n"
            "  name: multi-service\n"
            "  confidence: medium\n"
            "  phases:\n"
            "    validate:\n"
            "      - pytest -q\n",
            encoding="utf-8",
        )

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            config_resolver=_config_resolver(),
        )

        profile_phase = next(p for p in report["phases"] if p["name"] == "profile_preview")
        assert profile_phase["status"] == "ok"
        assert profile_phase["reason_code"] == "SMOKE_PROFILE_READY"
        assert profile_phase["evidence"]["template"] == "multi-service"

        validation_phase = next(p for p in report["phases"] if p["name"] == "validation")
        assert validation_phase["reason_code"] == "SMOKE_VALIDATION_READY"
        assert validation_phase["evidence"]["commands"] == ["pytest -q"]

        workspace_request_phase = next(
            p for p in report["phases"] if p["name"] == "workspace_request"
        )
        assert workspace_request_phase["reason_code"] == "SMOKE_WORKSPACE_REQUEST_READY"

    async def test_profile_preview_prefers_root_awf_workspace_yaml(self, tmp_path: Path) -> None:
        """Prefer root workspace profile when a marker exists at repository root."""
        profile_path = tmp_path / "awf.workspace.yml"
        profile_path.write_text(
            "awf:\n  name: root-profile\n  phases:\n    validate:\n      - pytest -q\n",
            encoding="utf-8",
        )

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            config_resolver=_config_resolver(),
        )

        profile_phase = next(p for p in report["phases"] if p["name"] == "profile_preview")
        assert profile_phase["status"] == "ok"
        assert profile_phase["reason_code"] == "SMOKE_PROFILE_READY"
        assert profile_phase["evidence"]["template"] == "root-profile"

        validation_phase = next(p for p in report["phases"] if p["name"] == "validation")
        assert validation_phase["reason_code"] == "SMOKE_VALIDATION_READY"
        assert validation_phase["evidence"]["commands"] == ["pytest -q"]

        workspace_request_phase = next(
            p for p in report["phases"] if p["name"] == "workspace_request"
        )
        assert workspace_request_phase["reason_code"] == "SMOKE_WORKSPACE_REQUEST_READY"

    async def test_profile_preview_prefers_on_disk_profile_yaml_extension(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / ".awf").mkdir()
        (tmp_path / ".awf" / "workspace.yaml").write_text(
            "awf:\n"
            "  name: multi-service-yaml\n"
            "  confidence: medium\n"
            "  phases:\n"
            "    validate:\n"
            "      - pytest -q\n",
            encoding="utf-8",
        )

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            config_resolver=_config_resolver(),
        )

        profile_phase = next(p for p in report["phases"] if p["name"] == "profile_preview")
        assert profile_phase["status"] == "ok"
        assert profile_phase["reason_code"] == "SMOKE_PROFILE_READY"
        assert profile_phase["evidence"]["template"] == "multi-service-yaml"

        validation_phase = next(p for p in report["phases"] if p["name"] == "validation")
        assert validation_phase["reason_code"] == "SMOKE_VALIDATION_READY"
        assert validation_phase["evidence"]["commands"] == ["pytest -q"]

        workspace_request_phase = next(
            p for p in report["phases"] if p["name"] == "workspace_request"
        )
        assert workspace_request_phase["reason_code"] == "SMOKE_WORKSPACE_REQUEST_READY"

    async def test_profile_preview_prefers_root_awf_workspace_yaml_extension(
        self, tmp_path: Path
    ) -> None:
        """Prefer root `.yaml` workspace profile when provided at repository root."""
        profile_path = tmp_path / "awf.workspace.yaml"
        profile_path.write_text(
            "awf:\n  name: root-profile-yaml\n  phases:\n    validate:\n      - pytest -q\n",
            encoding="utf-8",
        )

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            config_resolver=_config_resolver(),
        )

        profile_phase = next(p for p in report["phases"] if p["name"] == "profile_preview")
        assert profile_phase["status"] == "ok"
        assert profile_phase["reason_code"] == "SMOKE_PROFILE_READY"
        assert profile_phase["evidence"]["template"] == "root-profile-yaml"

        validation_phase = next(p for p in report["phases"] if p["name"] == "validation")
        assert validation_phase["reason_code"] == "SMOKE_VALIDATION_READY"
        assert validation_phase["evidence"]["commands"] == ["pytest -q"]

        workspace_request_phase = next(
            p for p in report["phases"] if p["name"] == "workspace_request"
        )
        assert workspace_request_phase["reason_code"] == "SMOKE_WORKSPACE_REQUEST_READY"

    async def test_profile_preview_precedence_prefers_awf_directory(self, tmp_path: Path) -> None:
        """Prefer `.awf/workspace.yml` over `awf.workspace.yml` when both exist."""
        (tmp_path / ".awf").mkdir()
        (tmp_path / ".awf" / "workspace.yml").write_text(
            "awf:\n  name: from-awf-dir\n  confidence: medium\n  phases:\n    validate:\n      - pytest -q\n",
            encoding="utf-8",
        )
        (tmp_path / "awf.workspace.yml").write_text(
            "awf:\n  name: from-root\n  phases:\n    validate:\n      - pytest -q\n",
            encoding="utf-8",
        )

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=False,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            config_resolver=_config_resolver(),
        )

        profile_phase = next(p for p in report["phases"] if p["name"] == "profile_preview")
        assert profile_phase["evidence"]["template"] == "from-awf-dir"


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

    async def test_demo_path_fallback_used_when_project_does_not_resolve(
        self, tmp_path: Path
    ) -> None:
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
                profile=WorkspaceProfile(
                    name="generic", source="onboarding:generic", confidence="low"
                ),
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

    async def test_invalid_on_disk_profile_fails_with_profile_preview_error(
        self, tmp_path: Path
    ) -> None:
        """Report preview failure when .awf/workspace.yml is invalid."""
        (tmp_path / ".awf").mkdir()
        (tmp_path / ".awf" / "workspace.yml").write_text(
            "name: bad\nservices:\n  - name: db\n",
            encoding="utf-8",
        )

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            config_resolver=_config_resolver(),
        )

        profile_phase = next(p for p in report["phases"] if p["name"] == "profile_preview")
        assert profile_phase["status"] == "fail"
        assert profile_phase["reason_code"] == "SMOKE_PROFILE_PREVIEW_FAILED"
        assert profile_phase.get("evidence", {}).get("error")
        assert (
            profile_phase["action"]
            == "Fix the on-disk workspace profile (.awf/workspace.yml, .awf/workspace.yaml, awf.workspace.yml, or awf.workspace.yaml) so it passes schema validation and lint checks."
        )
        assert profile_phase["reason_code"] != "SMOKE_PROFILE_NOT_DETECTED"

        workspace_request_phase = next(
            p for p in report["phases"] if p["name"] == "workspace_request"
        )
        assert workspace_request_phase["reason_code"] == "SMOKE_WORKSPACE_REQUEST_FAILED"

    async def test_invalid_root_awf_workspace_yaml_fails_with_profile_preview_error(
        self, tmp_path: Path
    ) -> None:
        """Report preview failure when awf.workspace.yml is invalid."""
        (tmp_path / "awf.workspace.yml").write_text(
            "awf:\n  name: bad\n  services:\n    - name: db\n",
            encoding="utf-8",
        )

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            config_resolver=_config_resolver(),
        )

        profile_phase = next(p for p in report["phases"] if p["name"] == "profile_preview")
        assert profile_phase["status"] == "fail"
        assert profile_phase["reason_code"] == "SMOKE_PROFILE_PREVIEW_FAILED"
        assert profile_phase.get("evidence", {}).get("error")
        assert (
            profile_phase["action"]
            == "Fix the on-disk workspace profile (.awf/workspace.yml, .awf/workspace.yaml, awf.workspace.yml, or awf.workspace.yaml) so it passes schema validation and lint checks."
        )
        assert profile_phase["reason_code"] != "SMOKE_PROFILE_NOT_DETECTED"

        workspace_request_phase = next(
            p for p in report["phases"] if p["name"] == "workspace_request"
        )
        assert workspace_request_phase["status"] == "fail"
        assert workspace_request_phase["reason_code"] == "SMOKE_WORKSPACE_REQUEST_FAILED"

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
                profile=WorkspaceProfile(
                    name="python", source="onboarding:python", confidence="medium"
                ),
                yaml="test: yaml",
                diagnostics=PreviewDiagnostics((), (), (), (), ()),
            )
            sr = {} if include_smoke_request else None
            return ProjectOnboardingPreview(
                path=path,
                inspection=inspection,
                draft=draft,
                diagnostics=draft.diagnostics,
                smoke_request=sr,
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
            console_checker=_console_checker(False),
        )

        console_phase = next(p for p in report["phases"] if p["name"] == "console_links")
        assert console_phase["reason_code"] == "SMOKE_CONSOLE_UNAVAILABLE"
        assert report["console_links"]["ui"] == "http://localhost:3000"
        assert "npm --prefix apps/console run dev" in console_phase["action"]

    async def test_default_local_console_url_reports_ready_when_reachable(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

        def _no_console(settings):
            return {"api_base_url": "http://localhost:8000"}

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_no_console,
            console_checker=_console_checker(True),
        )

        console_phase = next(p for p in report["phases"] if p["name"] == "console_links")
        assert console_phase["status"] == "ok"
        assert console_phase["reason_code"] == "SMOKE_CONSOLE_READY"
        assert report["console_links"]["ui"] == "http://localhost:3000"

    async def test_configured_console_url_reports_unavailable_when_probe_fails(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        configured_url = "http://localhost:3999"

        def _configured_console(settings):
            return {"api_base_url": "http://localhost:8000", "console_url": configured_url}

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_configured_console,
            console_checker=_console_checker(False, expected_url=configured_url),
        )

        console_phase = next(p for p in report["phases"] if p["name"] == "console_links")
        assert console_phase["status"] == "warn"
        assert console_phase["reason_code"] == "SMOKE_CONSOLE_UNAVAILABLE"
        assert console_phase["evidence"]["source"] == "configured"
        assert report["console_links"]["ui"] == configured_url

    async def test_malformed_configured_console_url_reports_unavailable_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        configured_url = "http://localhost:badport"
        monkeypatch.setattr("awf.service.smoke._default_console_checker", _default_console_checker)

        def _configured_console(settings):
            return {"api_base_url": "http://localhost:8000", "console_url": configured_url}

        report = await collect_smoke_report(
            project=tmp_path,
            settings=_settings(),
            mocked_local=True,
            service_collector=_ok_service_collector(),
            auth_collector=_ok_auth_collector(),
            profile_preview=_ok_profile_preview_ok(),
            config_resolver=_configured_console,
        )

        console_phase = next(p for p in report["phases"] if p["name"] == "console_links")
        assert console_phase["status"] == "warn"
        assert console_phase["reason_code"] == "SMOKE_CONSOLE_UNAVAILABLE"
        assert console_phase["evidence"]["source"] == "configured"
        assert report["console_links"]["ui"] == configured_url


@pytest.mark.unit
class TestDefaultDiskProfilePreview:
    def test_default_disk_profile_preview_preserves_profile_yaml(self, tmp_path: Path) -> None:
        """Smoke preview should preserve raw workspace YAML from disk."""
        (tmp_path / ".awf").mkdir()
        profile_path = tmp_path / ".awf" / "workspace.yml"
        profile_yaml = (
            "awf:\n  name: preserved-profile\n  phases:\n    validate:\n      - pytest -q\n"
        )
        profile_path.write_text(profile_yaml, encoding="utf-8")

        preview = _default_disk_profile_preview(tmp_path)
        assert preview.draft.yaml == profile_yaml


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
