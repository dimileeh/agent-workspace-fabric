"""Additional PR-monitor resume timeout executor error-path coverage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.executor import monitor_handoff as executor_monitor_handoff
from awf.db.enums import AgentRuntime
from awf.node import companion_services
from awf.profiles.models import WorkspaceProfile
from tests.unit.control.test_executor_error_paths_parts import (
    test_executor_error_paths_part_006 as _part_006,
)

factory = _part_006.factory
fake = _part_006.fake
_make_executor = _part_006._make_executor
_seed_monitoring_pr = _part_006._seed_monitoring_pr


@pytest.mark.unit
def test_present_optional_companion_env_secret_refs_preserves_empty_source() -> None:
    companion_specs = (
        companion_services.WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@example.com:api.git",
            environment_secrets=(
                companion_services.CompanionEnvironmentSecretRef(
                    target="OPTIONAL_TOKEN",
                    value_from="OPTIONAL_TOKEN_SOURCE",
                    required=False,
                ),
            ),
        ),
    )

    assert executor_monitor_handoff._present_optional_companion_env_secret_refs(
        companion_specs=companion_specs,
        environ={"OPTIONAL_TOKEN_SOURCE": ""},
    ) == {"backend": {"OPTIONAL_TOKEN": "${OPTIONAL_TOKEN_SOURCE:-}"}}


class TestExecutorCoverageEdgesPart011:
    @pytest.mark.unit
    async def test_resume_pr_monitor_passes_timeouts_to_adapter(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        captured: dict[str, Any] = {}

        def _get_adapter(_runtime: AgentRuntime, **kwargs: Any) -> object:
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(executor_monitor_handoff, "get_adapter", _get_adapter)

        monitor_calls: list[str] = []

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

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_args: _Monitor(),
        )
        await executor.resume_pr_monitor(ws_id)

        assert monitor_calls == [ws_id]
        assert captured["agent_wall_timeout_seconds"] == executor._config.agent_wall_timeout_seconds
        assert captured["agent_idle_timeout_seconds"] == executor._config.agent_idle_timeout_seconds

    @pytest.mark.unit
    async def test_resume_pr_monitor_preserves_companion_compose_timeout(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        captured: dict[str, Any] = {}

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
                captured.update(
                    {
                        "project_name": project_name,
                        "compose_file": compose_file,
                        "workspace_id": workspace_id,
                        "wait": wait,
                        "compose_up_timeout_seconds": compose_up_timeout_seconds,
                    }
                )

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
            resolved_profile=WorkspaceProfile(name="monitor-timeout").model_dump(mode="json"),
            task_policy={
                "companions": [
                    {
                        "name": "slow-api",
                        "repo_url": "git@github.com:x/slow-api.git",
                        "compose_up_timeout_seconds": 900,
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

        assert captured["workspace_id"] == ws_id
        assert captured["compose_up_timeout_seconds"] == 900
