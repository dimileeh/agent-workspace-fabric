from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from awf.common.config import get_settings
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import (
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service import controls
from awf.service.controls import WorkspaceStackStopError


def _mock_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


async def _create_control_workspace(
    session: AsyncSession,
    *,
    status: WorkspaceStatus,
    compose_project_name: str | None = None,
    compose_file_path: str | None = None,
    pr_url: str | None = None,
) -> object:
    repo = WorkspaceRepository(session)
    workspace = await repo.create(
        repo_url="git@github.com:example/controls.git",
        branch_base="main",
        task_title=f"Control {status.value}",
        task_prompt="Exercise workspace control behavior.",
        agent="codex",
        test_commands=["pytest -q"],
    )
    workspace.status = status.value
    workspace.compose_project_name = compose_project_name
    workspace.compose_file_path = compose_file_path
    workspace.pr_url = pr_url
    await session.flush()
    return workspace


async def _seed_validation_failure_evidence(
    session: AsyncSession,
    workspace: object,
    *,
    failure_message: str,
) -> str:
    workspace.failure_reason = FailureReason.validation_failure.value
    workspace.failure_message = failure_message
    repo = WorkspaceRepository(session)
    event = await repo.add_event(
        workspace,
        event_type="workspace.state_changed",
        reason_code="PYTEST_TEST_FAILURE",
        payload={
            "reason_code": "PYTEST_TEST_FAILURE",
            "message": failure_message,
            "details": {
                "recommended_action": "fix tests before cleanup recovery",
                "recovery_strategy": "retry_after_fix",
            },
        },
    )
    event.new_state = WorkspaceStatus.failed.value
    validation_repo = ValidationRunRepository(session)
    run = await validation_repo.start(
        workspace_id=workspace.id,
        attempt_id=None,
        tier=0,
        commands=[
            {
                "command": "uv run pytest tests/unit/test_controls.py::test_destroy_cleanup",
                "phase": "validation",
            }
        ],
        base_commit="a" * 40,
        target_branch="main",
        target_head_sha="b" * 40,
        workspace_head_sha="c" * 40,
        log_stream_refs={"validation": "logs/control-validation.log"},
    )
    await validation_repo.finish(
        run.id,
        status="failed",
        reason_code="PYTEST_TEST_FAILURE",
        coverage={
            "percent": 94.0,
            "minimum_percent": 99.0,
            "threshold": 99.0,
            "failing_test_node_ids": [
                "tests/unit/test_controls.py::test_destroy_cleanup",
            ],
        },
    )
    await session.flush()
    return run.id


class _RecordingStopper:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def __call__(self, compose_project_name: str | None) -> None:
        self.calls.append(compose_project_name)


class _RecordingCleaner:
    def __init__(self, failures: Sequence[str] = ()) -> None:
        self.failures = list(failures)
        self.calls: list[dict[str, object]] = []

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        companion_worktrees: tuple[tuple[str, str], ...] = (),
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> list[str]:
        _ = companion_worktrees
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "repo_url": repo_url,
                "compose_project_name": compose_project_name,
                "compose_file_path": compose_file_path,
                "worktree_host_path": worktree_host_path,
                "remove_volumes": remove_volumes,
                "remove_worktree": remove_worktree,
            }
        )
        return list(self.failures)


class _SequencedCleaner:
    def __init__(self, results: Sequence[Mapping[str, object]]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        companion_worktrees: tuple[tuple[str, str], ...] = (),
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> Mapping[str, object]:
        _ = companion_worktrees
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "repo_url": repo_url,
                "compose_project_name": compose_project_name,
                "compose_file_path": compose_file_path,
                "worktree_host_path": worktree_host_path,
                "remove_volumes": remove_volumes,
                "remove_worktree": remove_worktree,
            }
        )
        return self.results.pop(0)


@pytest.mark.unit
async def test_control_service_rejects_idempotency_payload_and_version_conflicts(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.requested)
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        await service.cancel_workspace(
            workspace.id,
            reason="same key",
            stop_stack=False,
            idempotency_key="control-conflict",
        )
        stale_expected_version = workspace.version + 1
        actual_version = workspace.version
        with pytest.raises(controls.IdempotencyConflictError):
            await service.stop_workspace(
                workspace.id,
                reason="same key",
                idempotency_key="control-conflict",
            )
        with pytest.raises(controls.VersionConflictError) as version_error:
            await service.destroy_workspace(
                workspace.id,
                force=True,
                remove_volumes=True,
                remove_worktree=True,
                idempotency_key="version-conflict",
                expected_version=stale_expected_version,
            )

    assert version_error.value.detail == {
        "expected_version": stale_expected_version,
        "actual_version": actual_version,
    }


@pytest.mark.unit
async def test_communicate_reports_no_output_failure() -> None:
    proc = _mock_proc(returncode=2)

    with pytest.raises(WorkspaceStackStopError) as exc_info:
        await controls._communicate(proc, operation="stop")  # noqa: SLF001

    assert exc_info.value.message == "docker stop failed (exit=2): <no output>"
    assert exc_info.value.stdout == ""
    assert exc_info.value.stderr == ""


@pytest.mark.unit
def test_default_cleaner_uses_configured_work_dir(tmp_path: Path) -> None:
    previous = os.environ.get("AWF_WORK_DIR")
    try:
        os.environ["AWF_WORK_DIR"] = str(tmp_path)
        get_settings.cache_clear()

        cleaner = controls.default_cleaner()

        assert cleaner._git._work_dir == tmp_path / "git"  # noqa: SLF001
        assert cleaner._compose._projects_dir == tmp_path / "compose"  # noqa: SLF001
    finally:
        if previous is None:
            os.environ.pop("AWF_WORK_DIR", None)
        else:
            os.environ["AWF_WORK_DIR"] = previous
        get_settings.cache_clear()
