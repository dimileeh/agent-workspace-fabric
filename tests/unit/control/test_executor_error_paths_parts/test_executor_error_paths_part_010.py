"""Additional PR monitor companion-secret executor error-path coverage."""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.repositories import WorkspaceRepository
from awf.node import companion_services
from awf.node.compose_manager import ComposeOperationError
from tests.unit.control.test_executor_error_paths_parts import (
    test_executor_error_paths_part_006 as _part_006,
)

factory = _part_006.factory
fake = _part_006.fake
_make_executor = _part_006._make_executor
_seed_monitoring_pr = _part_006._seed_monitoring_pr


class TestExecutorCoverageEdgesPart010:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("source_value", "expected_reason_code"),
        (
            (None, companion_services.COMPANION_ENV_SECRET_SOURCE_MISSING),
            ("", companion_services.COMPANION_ENV_SECRET_SOURCE_EMPTY),
        ),
    )
    async def test_resume_pr_monitor_preserves_required_companion_env_secret_reason_code(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        source_value: str | None,
        expected_reason_code: str,
    ) -> None:
        if source_value is None:
            monkeypatch.delenv("REQUIRED_TOKEN_SOURCE", raising=False)
        else:
            monkeypatch.setenv("REQUIRED_TOKEN_SOURCE", source_value)
        compose_calls: list[str] = []
        monitor_calls: list[str] = []

        class _Compose:
            async def ensure_project_up(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                wait: bool = True,
                compose_up_timeout_seconds: int = 300,
            ) -> None:
                del project_name, compose_file, wait, compose_up_timeout_seconds
                compose_calls.append(workspace_id)
                raise ComposeOperationError(
                    operation="up",
                    returncode=1,
                    stdout="",
                    stderr="compose interpolation failed",
                    reason_code="COMPOSE_COMMAND_FAILED",
                )

        class _Monitor:
            async def run(
                self,
                *,
                workspace_id: str,
                compose_project: str,
                compose_file: Path,
            ) -> None:
                del compose_project, compose_file
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(
            factory,
            task_policy={
                "companions": [
                    {
                        "name": "backend",
                        "repo_url": "git@github.com:x/backend.git",
                        "environment_secrets": {
                            "REQUIRED_TOKEN": {
                                "provider": "env",
                                "kind": "env",
                                "value_from": "REQUIRED_TOKEN_SOURCE",
                                "required": True,
                            },
                        },
                    }
                ],
            },
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            compose=_Compose(),
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        with structlog.testing.capture_logs() as captured:
            await executor.resume_pr_monitor(ws_id)

        assert compose_calls == []
        assert monitor_calls == [ws_id]
        assert any(
            entry["event"] == "executor.resume_companion_env_secret_precheck_failed"
            and entry["workspace_id"] == ws_id
            and entry["reason_code"] == expected_reason_code
            for entry in captured
        )
        assert not any(entry["event"] == "executor.resume_compose_up_failed" for entry in captured)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            compose_events = [
                event
                for event in ws.events
                if event.event_type == "workspace.monitor_runtime_restart_failed"
            ]
        assert len(compose_events) == 1
        assert compose_events[0].reason_code == "MONITOR_RECOVERY_PRECHECK_FAILED"
        assert compose_events[0].payload["operation"] == "companion_env_secret_precheck"
        assert compose_events[0].payload["returncode"] == 1
        assert compose_events[0].payload["reason_code"] == expected_reason_code
        assert expected_reason_code in compose_events[0].payload["stderr"]
        assert "REQUIRED_TOKEN_SOURCE" in compose_events[0].payload["stderr"]

    @pytest.mark.unit
    async def test_resume_pr_monitor_omits_missing_optional_companion_env_secret(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OPTIONAL_TOKEN_SOURCE", raising=False)
        monkeypatch.setenv("PRESENT_TOKEN_SOURCE", "raw-present-secret")
        compose_file = tmp_path / "persisted-compose" / "compose.yml"
        compose_file.parent.mkdir(parents=True)
        compose_file.write_text(
            """
services:
  backend:
    environment:
      APP_ENV: "test"
      OPTIONAL_TOKEN: "${OPTIONAL_TOKEN_SOURCE:-}"
      PRESENT_TOKEN: "${PRESENT_TOKEN_SOURCE:-}"
  agent:
    image: "awf-agent-runtime:latest"
""".lstrip(),
            encoding="utf-8",
        )

        class _RecordingCompose:
            async def ensure_project_up(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                wait: bool = True,
                compose_up_timeout_seconds: int = 300,
            ) -> None:
                del project_name, workspace_id, wait, compose_up_timeout_seconds
                parsed = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
                assert parsed["services"]["backend"]["environment"] == {
                    "APP_ENV": "test",
                    "PRESENT_TOKEN": "${PRESENT_TOKEN_SOURCE:-}",
                }
                assert "raw-present-secret" not in compose_file.read_text(encoding="utf-8")

        class _Monitor:
            async def run(
                self,
                *,
                workspace_id: str,
                compose_project: str,
                compose_file: Path,
            ) -> None:
                del workspace_id, compose_project, compose_file

        ws_id = await _seed_monitoring_pr(
            factory,
            compose_file_path=str(compose_file),
            task_policy={
                "companions": [
                    {
                        "name": "backend",
                        "repo_url": "git@github.com:x/backend.git",
                        "environment_secrets": {
                            "OPTIONAL_TOKEN": {
                                "provider": "env",
                                "kind": "env",
                                "value_from": "OPTIONAL_TOKEN_SOURCE",
                                "required": False,
                            },
                            "PRESENT_TOKEN": {
                                "provider": "env",
                                "kind": "env",
                                "value_from": "PRESENT_TOKEN_SOURCE",
                                "required": False,
                            },
                        },
                    }
                ],
            },
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            compose=_RecordingCompose(),
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        await executor.resume_pr_monitor(ws_id)

    @pytest.mark.unit
    async def test_resume_pr_monitor_restores_present_optional_companion_env_secret_placeholder(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPTIONAL_TOKEN_SOURCE", "raw-restored-secret")
        compose_file = tmp_path / "persisted-compose" / "compose.yml"
        compose_file.parent.mkdir(parents=True)
        compose_file.write_text(
            """
services:
  backend:
    environment:
      APP_ENV: "test"
  agent:
    image: "awf-agent-runtime:latest"
""".lstrip(),
            encoding="utf-8",
        )

        class _RecordingCompose:
            async def ensure_project_up(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                wait: bool = True,
                compose_up_timeout_seconds: int = 300,
            ) -> None:
                del project_name, workspace_id, wait, compose_up_timeout_seconds
                parsed = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
                assert parsed["services"]["backend"]["environment"] == {
                    "APP_ENV": "test",
                    "OPTIONAL_TOKEN": "${OPTIONAL_TOKEN_SOURCE:-}",
                }
                assert "raw-restored-secret" not in compose_file.read_text(encoding="utf-8")

        class _Monitor:
            async def run(
                self,
                *,
                workspace_id: str,
                compose_project: str,
                compose_file: Path,
            ) -> None:
                del workspace_id, compose_project, compose_file

        ws_id = await _seed_monitoring_pr(
            factory,
            compose_file_path=str(compose_file),
            task_policy={
                "companions": [
                    {
                        "name": "backend",
                        "repo_url": "git@github.com:x/backend.git",
                        "environment_secrets": {
                            "OPTIONAL_TOKEN": {
                                "provider": "env",
                                "kind": "env",
                                "value_from": "OPTIONAL_TOKEN_SOURCE",
                                "required": False,
                            },
                        },
                    }
                ],
            },
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            compose=_RecordingCompose(),
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        await executor.resume_pr_monitor(ws_id)

        rendered = compose_file.read_text(encoding="utf-8")
        assert "${OPTIONAL_TOKEN_SOURCE:-}" in rendered
        assert "raw-restored-secret" not in rendered
