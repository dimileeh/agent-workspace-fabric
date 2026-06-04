"""PR monitor completion filesystem cleanup tests."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    SecretLeaseIssue,
    SecretLeaseRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeTeardownResult
from awf.runtime.pr_monitor_runner import lifecycle
from awf.service import gc as gc_service
from awf.service.gc import (
    COMPOSE_TEARDOWN_CALLBACK_RAISED,
    WorkspaceCleanupExecutionStatus,
    WorkspaceGCCandidate,
    WorkspaceGCComposeTeardownResult,
    WorkspaceGCPath,
    WorkspaceGCPlan,
    WorkspaceGCPreserved,
    WorkspaceGCResult,
    WorkspaceGCWorktreeRemoveResult,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    mock_completed_compose_manager,
    pr_payload,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def cmd() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def sleep_fn() -> RecordedSleep:
    return RecordedSleep()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _mock_worktree_remove_success() -> object:
    return patch(
        "awf.service.gc._default_worktree_remover",
        new=AsyncMock(
            return_value=WorkspaceGCWorktreeRemoveResult(
                status="succeeded",
                reason_code="WORKTREE_REMOVE_SUCCEEDED",
            )
        ),
    )


def _empty_workspace_gc_result(
    work_dir: Path,
    *,
    workspace_id: str | None = None,
    include_statuses: tuple[str, ...] = (WorkspaceStatus.completed.value,),
    preserved: list[WorkspaceGCPreserved] | None = None,
    compose_teardown: WorkspaceGCComposeTeardownResult | None = None,
    status: WorkspaceCleanupExecutionStatus = "succeeded",
    reason_code: str = "CLEANUP_EXECUTION_SUCCEEDED",
) -> WorkspaceGCResult:
    now = datetime.now(UTC)
    return WorkspaceGCResult(
        plan=WorkspaceGCPlan(
            work_dir=work_dir,
            min_age_hours=24,
            cutoff_at=now,
            include_statuses=include_statuses,
            exclude_statuses=(),
            candidates=[],
            preserved=preserved or [],
        ),
        dry_run=False,
        deleted_paths=[],
        delete_errors=[],
        path_outcomes=[],
        compose_teardowns=(
            {workspace_id: compose_teardown}
            if workspace_id is not None and compose_teardown is not None
            else {}
        ),
        secret_lease_revocations={},
        worktree_removes={},
        reservation_releases={},
        status=status,
        reason_code=reason_code,
    )


@pytest.mark.unit
def test_completed_workspace_compose_teardown_uses_teardown_only_template_sentinel(
    tmp_path: Path,
) -> None:
    class _Runner:
        _work_dir = tmp_path

    init_calls: list[tuple[Path, Path]] = []

    class _FakeComposeManager:
        def __init__(self, *, work_dir: Path, template_path: Path) -> None:
            init_calls.append((work_dir, template_path))

    with patch(
        "awf.runtime.pr_monitor_runner.lifecycle.ComposeManager",
        new=_FakeComposeManager,
    ):
        callback = lifecycle._completed_workspace_compose_teardown(
            _Runner(),
            compose_project="monitor_project",
            compose_file=tmp_path / "monitor" / "compose.yml",
        )

    assert callback is not None
    assert init_calls == [
        (
            tmp_path,
            tmp_path / "compose" / ".completed-workspace-teardown-does-not-render.yml.j2",
        )
    ]


@pytest.mark.unit
async def test_completed_workspace_compose_teardown_callback_uses_candidate_metadata(
    tmp_path: Path,
) -> None:
    class _Runner:
        _work_dir = tmp_path

    monitor_compose_file = tmp_path / "monitor" / "compose.yml"
    candidate_compose_file = tmp_path / "candidate" / "compose.yml"
    compose_patch, compose_calls = mock_completed_compose_manager(
        ComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )
    )

    with compose_patch:
        callback = lifecycle._completed_workspace_compose_teardown(
            _Runner(),
            compose_project="monitor_project",
            compose_file=monitor_compose_file,
        )
        assert callback is not None

        candidate_with_metadata = WorkspaceGCCandidate(
            workspace_id="ws-candidate-meta",
            status=WorkspaceStatus.completed.value,
            updated_at=datetime.now(UTC),
            age_hours=0,
            reason_code="COMPLETED_PR_IMMEDIATE_RECLAIM",
            worktree=WorkspaceGCPath(
                kind="worktree",
                path=tmp_path / "worktrees" / "ws-candidate-meta",
                exists=True,
                estimated_bytes=0,
            ),
            compose=WorkspaceGCPath(
                kind="compose",
                path=tmp_path / "compose" / "ws-candidate-meta",
                exists=True,
                estimated_bytes=0,
            ),
            auth=WorkspaceGCPath(
                kind="auth",
                path=tmp_path / "auth" / "ws-candidate-meta",
                exists=True,
                estimated_bytes=0,
            ),
            compose_project_name="candidate_project",
            compose_file_path=str(candidate_compose_file),
        )
        result = await callback(candidate_with_metadata)

        candidate_without_metadata = WorkspaceGCCandidate(
            workspace_id="ws-monitor-meta",
            status=WorkspaceStatus.completed.value,
            updated_at=datetime.now(UTC),
            age_hours=0,
            reason_code="COMPLETED_PR_IMMEDIATE_RECLAIM",
            worktree=WorkspaceGCPath(
                kind="worktree",
                path=tmp_path / "worktrees" / "ws-monitor-meta",
                exists=True,
                estimated_bytes=0,
            ),
            compose=WorkspaceGCPath(
                kind="compose",
                path=tmp_path / "compose" / "ws-monitor-meta",
                exists=True,
                estimated_bytes=0,
            ),
            auth=WorkspaceGCPath(
                kind="auth",
                path=tmp_path / "auth" / "ws-monitor-meta",
                exists=True,
                estimated_bytes=0,
            ),
        )
        fallback_result = await callback(candidate_without_metadata)

    assert result.status == "succeeded"
    assert result.reason_code == "DOCKER_COMPOSE_DOWN_SUCCEEDED"
    assert fallback_result.status == "succeeded"
    assert fallback_result.reason_code == "DOCKER_COMPOSE_DOWN_SUCCEEDED"
    assert compose_calls == [
        ("candidate_project", candidate_compose_file, "ws-candidate-meta", True),
        ("monitor_project", monitor_compose_file, "ws-monitor-meta", True),
    ]


@pytest.mark.unit
async def test_completed_workspace_compose_teardown_uses_project_when_compose_file_missing(
    tmp_path: Path,
) -> None:
    class _Runner:
        _work_dir = tmp_path

    compose_patch, compose_calls = mock_completed_compose_manager(
        ComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_PROJECT_LABEL_REMOVED",
        )
    )

    with compose_patch:
        callback = lifecycle._completed_workspace_compose_teardown(
            _Runner(),
            compose_project="monitor_project",
            compose_file=None,
        )
        assert callback is not None

        result = await callback(
            WorkspaceGCCandidate(
                workspace_id="ws-missing-compose-file",
                status=WorkspaceStatus.completed.value,
                updated_at=datetime.now(UTC),
                age_hours=0,
                reason_code="COMPLETED_PR_IMMEDIATE_RECLAIM",
                worktree=WorkspaceGCPath(
                    kind="worktree",
                    path=tmp_path / "worktrees" / "ws-missing-compose-file",
                    exists=True,
                    estimated_bytes=0,
                ),
                compose=WorkspaceGCPath(
                    kind="compose",
                    path=tmp_path / "compose" / "ws-missing-compose-file",
                    exists=True,
                    estimated_bytes=0,
                ),
                auth=WorkspaceGCPath(
                    kind="auth",
                    path=tmp_path / "auth" / "ws-missing-compose-file",
                    exists=True,
                    estimated_bytes=0,
                ),
            )
        )

    assert result.status == "succeeded"
    assert result.reason_code == "DOCKER_COMPOSE_PROJECT_LABEL_REMOVED"
    assert compose_calls == [
        (
            "monitor_project",
            tmp_path / "compose" / "ws-missing-compose-file" / "compose.yml",
            "ws-missing-compose-file",
            True,
        )
    ]


@pytest.mark.unit
async def test_completed_workspace_compose_teardown_accepts_empty_monitor_project_with_candidate_metadata(
    tmp_path: Path,
) -> None:
    class _Runner:
        _work_dir = tmp_path

    monitor_compose_file = tmp_path / "monitor" / "compose.yml"
    compose_patch, compose_calls = mock_completed_compose_manager(
        ComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )
    )

    with compose_patch:
        callback = lifecycle._completed_workspace_compose_teardown(
            _Runner(),
            compose_project="",
            compose_file=monitor_compose_file,
        )
        assert callback is not None

        result = await callback(
            WorkspaceGCCandidate(
                workspace_id="ws-empty-monitor-project",
                status=WorkspaceStatus.completed.value,
                updated_at=datetime.now(UTC),
                age_hours=0,
                reason_code="COMPLETED_PR_IMMEDIATE_RECLAIM",
                worktree=WorkspaceGCPath(
                    kind="worktree",
                    path=tmp_path / "worktrees" / "ws-empty-monitor-project",
                    exists=True,
                    estimated_bytes=0,
                ),
                compose=WorkspaceGCPath(
                    kind="compose",
                    path=tmp_path / "compose" / "ws-empty-monitor-project",
                    exists=True,
                    estimated_bytes=0,
                ),
                auth=WorkspaceGCPath(
                    kind="auth",
                    path=tmp_path / "auth" / "ws-empty-monitor-project",
                    exists=True,
                    estimated_bytes=0,
                ),
                compose_project_name="candidate_project",
            )
        )

    assert result.status == "succeeded"
    assert compose_calls == [
        ("candidate_project", monitor_compose_file, "ws-empty-monitor-project", True)
    ]


@pytest.mark.unit
async def test_completed_workspace_gc_tears_down_compose_when_plan_is_empty(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    ws_id = "ws-empty-plan"
    compose_file = work_dir / "compose" / ws_id / "compose.yml"
    runner = SimpleNamespace(
        _work_dir=work_dir,
        _deps=SimpleNamespace(session_factory=factory),
    )
    compose_patch, compose_calls = mock_completed_compose_manager(
        ComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )
    )

    with compose_patch, structlog.testing.capture_logs() as captured:
        await lifecycle._gc_completed_workspace_filesystem(
            runner,
            ws_id,
            compose_project="proj_from_monitor",
            compose_file=compose_file,
        )

    assert compose_calls == [("proj_from_monitor", compose_file, ws_id, True)]
    assert any(
        record.get("event") == "monitor.compose_teardown_ok"
        and record.get("workspace_id") == ws_id
        and record.get("reason_code") == "DOCKER_COMPOSE_DOWN_SUCCEEDED"
        for record in captured
    )


@pytest.mark.unit
async def test_completed_workspace_gc_ok_marks_empty_plan_compose_only_success(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    ws_id = "ws-empty-plan-ok-log"
    compose_file = work_dir / "compose" / ws_id / "compose.yml"
    runner = SimpleNamespace(
        _work_dir=work_dir,
        _deps=SimpleNamespace(session_factory=factory),
    )
    compose_patch, _compose_calls = mock_completed_compose_manager(
        ComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )
    )

    with compose_patch, structlog.testing.capture_logs() as captured:
        await lifecycle._gc_completed_workspace_filesystem(
            runner,
            ws_id,
            compose_project="proj_from_monitor",
            compose_file=compose_file,
        )

    ok_records = [
        record for record in captured if record.get("event") == "monitor.filesystem_gc_ok"
    ]
    assert ok_records
    assert ok_records[0].get("deleted_path_count") == 0
    assert ok_records[0].get("compose_teardown_status") == "succeeded"
    assert ok_records[0].get("compose_teardown_only") is True


@pytest.mark.unit
async def test_completed_workspace_gc_logs_compose_teardown_when_gc_raises_after_teardown(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    ws_id = "ws-gc-raises-after-compose"
    compose_file = work_dir / "compose" / ws_id / "compose.yml"
    runner = SimpleNamespace(
        _work_dir=work_dir,
        _deps=SimpleNamespace(session_factory=factory),
    )
    compose_patch, compose_calls = mock_completed_compose_manager(
        ComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )
    )
    auth_teardowns: list[tuple[Path, str]] = []

    async def _raise_after_compose_teardown(
        _session_factory: async_sessionmaker[AsyncSession],
        *,
        compose_teardown: object,
        **_kwargs: object,
    ) -> WorkspaceGCResult:
        assert callable(compose_teardown)
        candidate = WorkspaceGCCandidate(
            workspace_id=ws_id,
            status=WorkspaceStatus.completed.value,
            updated_at=datetime.now(UTC),
            age_hours=0,
            reason_code="COMPLETED_PR_IMMEDIATE_RECLAIM",
            worktree=WorkspaceGCPath(
                kind="worktree",
                path=work_dir / "git" / "worktrees" / ws_id,
                exists=True,
                estimated_bytes=0,
            ),
            compose=WorkspaceGCPath(
                kind="compose",
                path=work_dir / "compose" / ws_id,
                exists=True,
                estimated_bytes=0,
            ),
            auth=WorkspaceGCPath(
                kind="auth",
                path=work_dir / "auth" / ws_id,
                exists=True,
                estimated_bytes=0,
            ),
            compose_project_name="candidate_project",
        )
        await compose_teardown(candidate)
        raise RuntimeError("database unavailable after compose teardown")

    def _record_auth_teardown(work_dir: Path, workspace_id: str) -> None:
        auth_teardowns.append((work_dir, workspace_id))

    with (
        compose_patch,
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle.run_workspace_filesystem_gc",
            new=_raise_after_compose_teardown,
        ),
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle._teardown_completed_workspace_auth_overlay",
            new=_record_auth_teardown,
        ),
        structlog.testing.capture_logs() as captured,
    ):
        await lifecycle._gc_completed_workspace_filesystem(
            runner,
            ws_id,
            compose_project="monitor_project",
            compose_file=compose_file,
        )

    assert compose_calls == [("candidate_project", compose_file, ws_id, True)]
    assert auth_teardowns == [(work_dir, ws_id)]
    assert any(
        record.get("event") == "monitor.filesystem_gc_raised"
        and record.get("workspace_id") == ws_id
        for record in captured
    )
    assert any(
        record.get("event") == "monitor.compose_teardown_ok"
        and record.get("workspace_id") == ws_id
        and record.get("compose_project") == "candidate_project"
        for record in captured
    )


@pytest.mark.unit
async def test_completed_workspace_gc_tracks_callback_raised_when_gc_raises_after_teardown(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    ws_id = "ws-gc-raises-after-compose-callback"
    compose_file = work_dir / "compose" / ws_id / "compose.yml"
    runner = SimpleNamespace(
        _work_dir=work_dir,
        _deps=SimpleNamespace(session_factory=factory),
    )
    compose_patch, compose_calls = mock_completed_compose_manager(
        exc=RuntimeError("compose unavailable")
    )
    auth_teardowns: list[tuple[Path, str]] = []

    async def _raise_after_compose_teardown_callback(
        _session_factory: async_sessionmaker[AsyncSession],
        *,
        compose_teardown: object,
        **_kwargs: object,
    ) -> WorkspaceGCResult:
        assert callable(compose_teardown)
        candidate = WorkspaceGCCandidate(
            workspace_id=ws_id,
            status=WorkspaceStatus.completed.value,
            updated_at=datetime.now(UTC),
            age_hours=0,
            reason_code="COMPLETED_PR_IMMEDIATE_RECLAIM",
            worktree=WorkspaceGCPath(
                kind="worktree",
                path=work_dir / "git" / "worktrees" / ws_id,
                exists=True,
                estimated_bytes=0,
            ),
            compose=WorkspaceGCPath(
                kind="compose",
                path=work_dir / "compose" / ws_id,
                exists=True,
                estimated_bytes=0,
            ),
            auth=WorkspaceGCPath(
                kind="auth",
                path=work_dir / "auth" / ws_id,
                exists=True,
                estimated_bytes=0,
            ),
            compose_project_name="candidate_project",
        )
        with pytest.raises(RuntimeError, match="compose unavailable"):
            await compose_teardown(candidate)
        raise RuntimeError("database unavailable after compose callback")

    def _record_auth_teardown(work_dir: Path, workspace_id: str) -> None:
        auth_teardowns.append((work_dir, workspace_id))

    with (
        compose_patch,
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle.run_workspace_filesystem_gc",
            new=_raise_after_compose_teardown_callback,
        ),
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle._teardown_completed_workspace_auth_overlay",
            new=_record_auth_teardown,
        ),
        structlog.testing.capture_logs() as captured,
    ):
        await lifecycle._gc_completed_workspace_filesystem(
            runner,
            ws_id,
            compose_project="monitor_project",
            compose_file=compose_file,
        )

    assert compose_calls == [("candidate_project", compose_file, ws_id, True)]
    assert auth_teardowns == []
    assert any(
        record.get("event") == "monitor.filesystem_gc_raised"
        and record.get("workspace_id") == ws_id
        for record in captured
    )
    assert any(
        record.get("event") == "monitor.compose_teardown_failed"
        and record.get("workspace_id") == ws_id
        and record.get("compose_project") == "candidate_project"
        and record.get("reason_code") == COMPOSE_TEARDOWN_CALLBACK_RAISED
        and record.get("error") == "RuntimeError: compose unavailable"
        for record in captured
    )


@pytest.mark.unit
async def test_completed_workspace_gc_tracks_shared_callback_failure_result_when_gc_raises_after_teardown(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    class _ChangingMessageError(RuntimeError):
        def __init__(self) -> None:
            self.calls = 0

        def __str__(self) -> str:
            self.calls += 1
            return f"compose unavailable {self.calls}"

    work_dir = tmp_path / "service"
    ws_id = "ws-gc-shared-compose-callback"
    compose_file = work_dir / "compose" / ws_id / "compose.yml"
    runner = SimpleNamespace(
        _work_dir=work_dir,
        _deps=SimpleNamespace(session_factory=factory),
    )
    compose_patch, _compose_calls = mock_completed_compose_manager(exc=_ChangingMessageError())
    compose_results: dict[str, WorkspaceGCComposeTeardownResult] = {}

    async def _raise_after_real_compose_teardown_runner(
        _session_factory: async_sessionmaker[AsyncSession],
        *,
        compose_teardown: object,
        **_kwargs: object,
    ) -> WorkspaceGCResult:
        assert callable(compose_teardown)
        candidate = WorkspaceGCCandidate(
            workspace_id=ws_id,
            status=WorkspaceStatus.completed.value,
            updated_at=datetime.now(UTC),
            age_hours=0,
            reason_code="COMPLETED_PR_IMMEDIATE_RECLAIM",
            worktree=WorkspaceGCPath(
                kind="worktree",
                path=work_dir / "git" / "worktrees" / ws_id,
                exists=True,
                estimated_bytes=0,
            ),
            compose=WorkspaceGCPath(
                kind="compose",
                path=work_dir / "compose" / ws_id,
                exists=True,
                estimated_bytes=0,
            ),
            auth=WorkspaceGCPath(
                kind="auth",
                path=work_dir / "auth" / ws_id,
                exists=True,
                estimated_bytes=0,
            ),
            compose_project_name="candidate_project",
        )
        plan = WorkspaceGCPlan(
            work_dir=work_dir,
            min_age_hours=24,
            cutoff_at=datetime.now(UTC),
            include_statuses=(WorkspaceStatus.completed.value,),
            exclude_statuses=(),
            candidates=[candidate],
            preserved=[],
        )
        compose_results.update(await gc_service._run_gc_compose_teardowns(plan, compose_teardown))
        raise RuntimeError("database unavailable after compose callback")

    with (
        compose_patch,
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle.run_workspace_filesystem_gc",
            new=_raise_after_real_compose_teardown_runner,
        ),
        structlog.testing.capture_logs() as captured,
    ):
        await lifecycle._gc_completed_workspace_filesystem(
            runner,
            ws_id,
            compose_project="monitor_project",
            compose_file=compose_file,
        )

    failed = [
        record for record in captured if record.get("event") == "monitor.compose_teardown_failed"
    ]
    assert failed
    assert compose_results[ws_id].error is not None
    assert failed[0].get("error") == compose_results[ws_id].error


@pytest.mark.unit
async def test_completed_workspace_gc_unmounts_auth_overlay_when_plan_is_empty(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    ws_id = "ws-empty-plan-auth"
    compose_file = work_dir / "compose" / ws_id / "compose.yml"
    runner = SimpleNamespace(
        _work_dir=work_dir,
        _deps=SimpleNamespace(session_factory=factory),
    )
    compose_patch, compose_calls = mock_completed_compose_manager(
        ComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )
    )
    teardown_calls: list[tuple[Path, str, list[tuple[str, Path, str, bool]]]] = []

    def _record_teardown(*, work_dir: Path, workspace_id: str) -> None:
        teardown_calls.append((work_dir, workspace_id, list(compose_calls)))

    with (
        compose_patch,
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle.teardown_workspace_auth_overlay",
            new=_record_teardown,
        ),
    ):
        await lifecycle._gc_completed_workspace_filesystem(
            runner,
            ws_id,
            compose_project="proj_from_monitor",
            compose_file=compose_file,
        )

    expected_compose_calls = [("proj_from_monitor", compose_file, ws_id, True)]
    assert compose_calls == expected_compose_calls
    assert teardown_calls == [(work_dir, ws_id, expected_compose_calls)]


@pytest.mark.unit
async def test_completed_workspace_gc_unmounts_empty_plan_auth_overlay_on_non_compose_partial(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    ws_id = "ws-empty-plan-partial-auth"
    compose_file = work_dir / "compose" / ws_id / "compose.yml"
    teardown = WorkspaceGCComposeTeardownResult(
        status="succeeded",
        reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
    )
    fake_result = _empty_workspace_gc_result(
        work_dir,
        workspace_id=ws_id,
        compose_teardown=teardown,
        status="partial",
        reason_code="CLEANUP_EXECUTION_PARTIAL",
    )
    fake_result.reservation_releases[ws_id] = {
        "released_count": 0,
        "reason_code": "TERMINAL_GC",
        "error": "db connection failed",
    }
    runner = SimpleNamespace(
        _work_dir=work_dir,
        _deps=SimpleNamespace(session_factory=factory),
    )
    auth_teardowns: list[tuple[Path, str]] = []

    def _record_auth_teardown(work_dir: Path, workspace_id: str) -> None:
        auth_teardowns.append((work_dir, workspace_id))

    with (
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle.run_workspace_filesystem_gc",
            new=AsyncMock(return_value=fake_result),
        ),
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle._teardown_completed_workspace_auth_overlay",
            new=_record_auth_teardown,
        ),
        structlog.testing.capture_logs() as captured,
    ):
        await lifecycle._gc_completed_workspace_filesystem(
            runner,
            ws_id,
            compose_project="proj_from_monitor",
            compose_file=compose_file,
        )

    assert auth_teardowns == [(work_dir, ws_id)]
    assert any(
        record.get("event") == "monitor.filesystem_gc_failed"
        and record.get("workspace_id") == ws_id
        and record.get("reservation_releases", {}).get(ws_id, {}).get("error") is not None
        for record in captured
    )


@pytest.mark.unit
async def test_completed_monitor_preserved_compose_teardown_failure_logs_filesystem_gc_failed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    ws_id = "ws-preserved-compose-failed"
    now = datetime.now(UTC)
    teardown = WorkspaceGCComposeTeardownResult(
        status="failed",
        reason_code="DOCKER_COMPOSE_DOWN_FAILED",
        error="volume still in use",
    )
    fake_result = _empty_workspace_gc_result(
        work_dir,
        workspace_id=ws_id,
        preserved=[
            WorkspaceGCPreserved(
                workspace_id=ws_id,
                status=WorkspaceStatus.completed.value,
                updated_at=now,
                age_hours=1,
                reason_code="COMPLETED_PR_NOT_MERGED",
            )
        ],
        compose_teardown=teardown,
        status="partial",
        reason_code="CLEANUP_EXECUTION_PARTIAL",
    )
    runner = SimpleNamespace(_work_dir=work_dir, _deps=SimpleNamespace(session_factory=factory))

    with (
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle.run_workspace_filesystem_gc",
            new=AsyncMock(return_value=fake_result),
        ),
        structlog.testing.capture_logs() as captured,
    ):
        await lifecycle._gc_completed_workspace_filesystem(
            runner,
            ws_id,
            compose_project="proj",
            compose_file=work_dir / "compose" / ws_id / "compose.yml",
        )

    failed = [
        record for record in captured if record.get("event") == "monitor.filesystem_gc_failed"
    ]
    assert failed
    assert failed[0].get("compose_teardowns") == {ws_id: teardown.to_dict()}
    assert not any(record.get("event") == "monitor.filesystem_gc_deferred" for record in captured)


@pytest.mark.unit
async def test_completed_monitor_missing_workspace_compose_teardown_failure_logs_gc_failed_cause(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    ws_id = "ws-missing-compose-failed"
    teardown = WorkspaceGCComposeTeardownResult(
        status="failed",
        reason_code="DOCKER_COMPOSE_DOWN_FAILED",
        error="volume still in use",
    )
    fake_result = _empty_workspace_gc_result(
        work_dir,
        workspace_id=ws_id,
        include_statuses=(),
        compose_teardown=teardown,
        status="partial",
        reason_code="CLEANUP_EXECUTION_PARTIAL",
    )
    runner = SimpleNamespace(_work_dir=work_dir, _deps=SimpleNamespace(session_factory=factory))

    with (
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle.run_workspace_filesystem_gc",
            new=AsyncMock(return_value=fake_result),
        ),
        structlog.testing.capture_logs() as captured,
    ):
        await lifecycle._gc_completed_workspace_filesystem(
            runner,
            ws_id,
            compose_project="proj",
            compose_file=work_dir / "compose" / ws_id / "compose.yml",
        )

    failed = [
        record for record in captured if record.get("event") == "monitor.filesystem_gc_failed"
    ]
    assert failed
    assert failed[0].get("delete_errors") == []
    assert failed[0].get("compose_teardowns") == {ws_id: teardown.to_dict()}


@pytest.mark.unit
async def test_completed_monitor_preserved_success_still_logs_filesystem_gc_deferred(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    ws_id = "ws-preserved-success"
    now = datetime.now(UTC)
    fake_result = _empty_workspace_gc_result(
        work_dir,
        preserved=[
            WorkspaceGCPreserved(
                workspace_id=ws_id,
                status=WorkspaceStatus.completed.value,
                updated_at=now,
                age_hours=1,
                reason_code="COMPLETED_PR_NOT_MERGED",
            )
        ],
    )
    runner = SimpleNamespace(_work_dir=work_dir, _deps=SimpleNamespace(session_factory=factory))

    with (
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle.run_workspace_filesystem_gc",
            new=AsyncMock(return_value=fake_result),
        ),
        structlog.testing.capture_logs() as captured,
    ):
        await lifecycle._gc_completed_workspace_filesystem(runner, ws_id)

    assert any(
        record.get("event") == "monitor.filesystem_gc_deferred"
        and record.get("workspace_id") == ws_id
        and record.get("reason_code") == "COMPLETED_PR_NOT_MERGED"
        for record in captured
    )
    assert not any(record.get("event") == "monitor.filesystem_gc_failed" for record in captured)


@pytest.mark.unit
async def test_completed_monitor_preserved_success_deferred_log_includes_compose_teardown_status(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    ws_id = "ws-preserved-success-compose-status"
    now = datetime.now(UTC)
    fake_result = _empty_workspace_gc_result(
        work_dir,
        workspace_id=ws_id,
        preserved=[
            WorkspaceGCPreserved(
                workspace_id=ws_id,
                status=WorkspaceStatus.completed.value,
                updated_at=now,
                age_hours=1,
                reason_code="COMPLETED_PR_NOT_MERGED",
            )
        ],
        compose_teardown=WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        ),
    )
    runner = SimpleNamespace(_work_dir=work_dir, _deps=SimpleNamespace(session_factory=factory))

    with (
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle.run_workspace_filesystem_gc",
            new=AsyncMock(return_value=fake_result),
        ),
        structlog.testing.capture_logs() as captured,
    ):
        await lifecycle._gc_completed_workspace_filesystem(
            runner,
            ws_id,
            compose_project="proj",
            compose_file=work_dir / "compose" / ws_id / "compose.yml",
        )

    deferred = [
        record for record in captured if record.get("event") == "monitor.filesystem_gc_deferred"
    ]
    assert deferred
    assert deferred[0].get("workspace_id") == ws_id
    assert deferred[0].get("compose_teardown_status") == "succeeded"


@pytest.mark.unit
async def test_completed_monitor_preserved_compose_teardown_log_uses_preserved_project(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    ws_id = "ws-preserved-compose-project"
    now = datetime.now(UTC)
    fake_result = _empty_workspace_gc_result(
        work_dir,
        workspace_id=ws_id,
        preserved=[
            WorkspaceGCPreserved(
                workspace_id=ws_id,
                status=WorkspaceStatus.completed.value,
                updated_at=now,
                age_hours=1,
                reason_code="COMPLETED_PR_NOT_MERGED",
                compose_project_name="proj_from_db",
            )
        ],
        compose_teardown=WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        ),
    )
    runner = SimpleNamespace(_work_dir=work_dir, _deps=SimpleNamespace(session_factory=factory))

    with (
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle.run_workspace_filesystem_gc",
            new=AsyncMock(return_value=fake_result),
        ),
        structlog.testing.capture_logs() as captured,
    ):
        await lifecycle._gc_completed_workspace_filesystem(
            runner,
            ws_id,
            compose_project="proj_from_monitor",
            compose_file=work_dir / "compose" / ws_id / "compose.yml",
        )

    assert any(
        record.get("event") == "monitor.compose_teardown_ok"
        and record.get("workspace_id") == ws_id
        and record.get("compose_project") == "proj_from_db"
        for record in captured
    )


async def _seed_old_completed_pr_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    updated_at: datetime,
) -> str:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:dimileeh/aira-web.git",
            branch_base="development",
            task_title="completed monitor cleanup",
            task_prompt="x",
            agent="claude_code",
            test_commands=["pytest -q"],
        )
        workspace.status = WorkspaceStatus.completed.value
        workspace.updated_at = updated_at
        workspace.pr_url = "https://github.com/dimileeh/aira-web/pull/42"
        workspace.pr_number = 42
        workspace.pr_merge_sha = "mergecommit1234567890"
        await session.commit()
        return workspace.id


async def _issue_monitor_secret_lease(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    now: datetime,
) -> None:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        await SecretLeaseRepository(session).issue_declared_leases(
            workspace,
            leases=[
                SecretLeaseIssue(
                    secret_name="api-token",
                    kind="env",
                    target="API_TOKEN",
                    mode="ro",
                    required=True,
                    provider="env",
                    ref_digest="sha256:" + "f" * 64,
                    expires_at=now + timedelta(hours=1),
                    issue_metadata={"profile": "monitor", "declaration_index": 0},
                )
            ],
            now=now,
        )
        await session.commit()


@pytest.mark.unit
async def test_completed_monitor_reclaims_recent_workspace_pressure_dirs_immediately(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    # On a successful merge the pressure dirs (worktree, compose, auth) are
    # reclaimed immediately -- bypassing the retention window -- while the
    # durable record (DB row, events, logs) is preserved.
    work_dir = tmp_path / "service"
    worktrees_root = work_dir / "git" / "worktrees"
    ws_id = await seed_monitoring_workspace(factory, pr_merge_sha="m" * 40)
    worktree = worktrees_root / ws_id
    auth = work_dir / "auth" / ws_id
    log_file = work_dir / "logs" / ws_id / "agent.log"
    _write(worktree / "repo.txt", "repo")
    _write(auth / "codex" / "auth.json", "auth")
    _write(log_file, "keep logs")

    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
    )
    compose_patch, _compose_calls = mock_completed_compose_manager(
        ComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )
    )
    with (
        _mock_worktree_remove_success(),
        compose_patch,
        structlog.testing.capture_logs() as captured,
    ):
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=work_dir / "compose" / ws_id / "compose.yml",
        )

    assert not worktree.exists()
    assert not auth.exists()
    # The durable record is kept.
    assert log_file.exists()
    assert any(
        record.get("event") == "monitor.compose_teardown_ok" and record.get("workspace_id") == ws_id
        for record in captured
    )
    assert any(record.get("event") == "monitor.filesystem_gc_ok" for record in captured)
    assert not any(record.get("event") == "monitor.filesystem_gc_deferred" for record in captured)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
        assert ws.pr_merge_sha == "mergecommit1234567890"


@pytest.mark.unit
async def test_completed_monitor_filesystem_gc_logs_success_for_retained_old_workspace(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    worktrees_root = work_dir / "git" / "worktrees"
    ws_id = await _seed_old_completed_pr_workspace(
        factory,
        updated_at=datetime.now(UTC) - timedelta(days=30),
    )
    worktree = worktrees_root / ws_id
    compose_dir = work_dir / "compose" / ws_id
    auth = work_dir / "auth" / ws_id
    _write(worktree / "repo.txt", "repo")
    _write(compose_dir / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
    )

    with _mock_worktree_remove_success(), structlog.testing.capture_logs() as captured:
        await runner._gc_completed_workspace_filesystem(ws_id)

    assert not worktree.exists()
    assert not compose_dir.exists()
    assert not auth.exists()
    assert any(
        record.get("event") == "monitor.filesystem_gc_ok" and record.get("deleted_path_count") == 3
        for record in captured
    )


@pytest.mark.unit
async def test_completed_monitor_filesystem_gc_unmounts_auth_overlay_before_removal(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    worktrees_root = work_dir / "git" / "worktrees"
    ws_id = await _seed_old_completed_pr_workspace(
        factory,
        updated_at=datetime.now(UTC) - timedelta(days=30),
    )
    auth = work_dir / "auth" / ws_id
    _write(auth / "claude" / "merged" / "settings.json", "auth")

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
    )

    teardown_calls: list[tuple[Path, str, bool]] = []

    def _record_teardown(*, work_dir: Path, workspace_id: str) -> None:
        # Capture whether the auth dir still exists when teardown runs so the
        # test proves the unmount precedes GC's rmtree (not after it, when it
        # would be a no-op and the EBUSY race would already have fired).
        teardown_calls.append((work_dir, workspace_id, auth.exists()))

    with (
        _mock_worktree_remove_success(),
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle.teardown_workspace_auth_overlay",
            new=_record_teardown,
        ),
    ):
        await runner._gc_completed_workspace_filesystem(ws_id)

    assert teardown_calls == [(work_dir, ws_id, True)]
    assert not auth.exists()


@pytest.mark.unit
async def test_completed_monitor_auth_overlay_teardown_runs_off_event_loop(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The overlay teardown runs a blocking ``subprocess.run(["umount", ...])``;
    # it must be offloaded via ``asyncio.to_thread`` so a real (mounted) overlay
    # cannot stall the monitor's event loop for up to 30s. Mirrors the
    # ``service gc`` regression test that asserts the same offload.
    work_dir = tmp_path / "service"
    worktrees_root = work_dir / "git" / "worktrees"
    ws_id = await _seed_old_completed_pr_workspace(
        factory,
        updated_at=datetime.now(UTC) - timedelta(days=30),
    )
    auth = work_dir / "auth" / ws_id
    _write(auth / "claude" / "merged" / "settings.json", "auth")

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
    )

    to_thread_calls: list[str] = []

    async def _record_to_thread(
        func: Callable[..., object],
        /,
        *args: object,
        **kwargs: object,
    ) -> object:
        to_thread_calls.append(func.__name__)
        return func(*args, **kwargs)

    def _noop_teardown(*, work_dir: Path, workspace_id: str) -> None:
        return None

    with (
        _mock_worktree_remove_success(),
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle.teardown_workspace_auth_overlay",
            new=_noop_teardown,
        ),
    ):
        monkeypatch.setattr(lifecycle.asyncio, "to_thread", _record_to_thread)
        await runner._gc_completed_workspace_filesystem(ws_id)

    assert "_teardown_completed_workspace_auth_overlay" in to_thread_calls


@pytest.mark.unit
async def test_completed_monitor_auth_overlay_teardown_failure_does_not_block_gc(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    worktrees_root = work_dir / "git" / "worktrees"
    ws_id = await _seed_old_completed_pr_workspace(
        factory,
        updated_at=datetime.now(UTC) - timedelta(days=30),
    )
    auth = work_dir / "auth" / ws_id
    _write(auth / "claude" / "merged" / "settings.json", "auth")

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
    )

    # ``umount(8)`` exits non-zero with its kernel reason on stderr; ``repr`` of
    # the CalledProcessError drops that text, so the handler must forward stderr
    # explicitly, mirroring the ``service gc`` handler.
    kernel_reason = "umount: /…/merged: target is busy."

    def _raise_busy(*, work_dir: Path, workspace_id: str) -> None:
        raise subprocess.CalledProcessError(32, "umount", stderr=kernel_reason)

    with (
        _mock_worktree_remove_success(),
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle.teardown_workspace_auth_overlay",
            new=_raise_busy,
        ),
        structlog.testing.capture_logs() as captured,
    ):
        await runner._gc_completed_workspace_filesystem(ws_id)

    # The warning must carry diagnostic detail (reason_code + the bound error +
    # the kernel stderr), mirroring the ``service gc`` handler, so operators can
    # tell EBUSY from a missing umount binary or a permission error.
    assert any(
        record.get("event") == "monitor.auth_overlay_teardown_failed"
        and record.get("workspace_id") == ws_id
        and record.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED"
        and record.get("stderr") == kernel_reason
        for record in captured
    )
    # The teardown failure is swallowed; GC still reclaims the auth dir.
    assert not auth.exists()


@pytest.mark.unit
async def test_completed_monitor_auth_overlay_unverifiable_does_not_block_gc(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    from awf.node.auth_mounts import OverlayUnmountUnverifiableError

    # A capability-less completion path cannot see the worker's mount namespace,
    # so ``teardown_workspace_auth_overlay`` raises ``OverlayUnmountUnverifiableError``
    # (a ``RuntimeError``, not OSError/SubprocessError). It must be caught here so
    # the filesystem GC still runs -- letting it propagate out of the ``to_thread``
    # would skip GC entirely and strand every pressure/auth dir, mirroring how the
    # ``service gc`` path swallows the unverifiable result and keeps going.
    work_dir = tmp_path / "service"
    worktrees_root = work_dir / "git" / "worktrees"
    ws_id = await _seed_old_completed_pr_workspace(
        factory,
        updated_at=datetime.now(UTC) - timedelta(days=30),
    )
    worktree = worktrees_root / ws_id
    auth = work_dir / "auth" / ws_id
    _write(worktree / "repo.txt", "repo")
    _write(auth / "codex" / "auth.json", "auth")

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
    )

    def _raise_unverifiable(*, work_dir: Path, workspace_id: str) -> None:
        raise OverlayUnmountUnverifiableError(
            reason_code="CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE",
            message=f"cannot verify Claude auth overlay teardown for {workspace_id}",
        )

    with (
        _mock_worktree_remove_success(),
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle.teardown_workspace_auth_overlay",
            new=_raise_unverifiable,
        ),
        structlog.testing.capture_logs() as captured,
    ):
        await runner._gc_completed_workspace_filesystem(ws_id)

    # The unverifiable teardown is logged with its incapable reason code...
    assert any(
        record.get("event") == "monitor.auth_overlay_teardown_incapable"
        and record.get("workspace_id") == ws_id
        and record.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE"
        for record in captured
    )
    # ...and GC still ran rather than being skipped entirely.
    assert any(record.get("event") == "monitor.filesystem_gc_ok" for record in captured)
    assert not worktree.exists()


@pytest.mark.unit
async def test_completed_monitor_filesystem_gc_revokes_active_secret_leases(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    worktrees_root = work_dir / "git" / "worktrees"
    now = datetime.now(UTC)
    ws_id = await _seed_old_completed_pr_workspace(
        factory,
        updated_at=now - timedelta(days=30),
    )
    await _issue_monitor_secret_lease(factory, ws_id, now=now)
    worktree = worktrees_root / ws_id
    auth = work_dir / "auth" / ws_id
    _write(worktree / "repo.txt", "repo")
    _write(auth / "codex" / "auth.json", "auth")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
    )

    await runner._gc_completed_workspace_filesystem(ws_id)

    async with factory() as session:
        leases = await SecretLeaseRepository(session).list_for_workspace(ws_id)
    assert leases[0].status == "revoked"
    assert leases[0].revoke_reason_code == "TERMINAL_GC"
    assert not auth.exists()


@pytest.mark.unit
async def test_completed_monitor_filesystem_gc_logs_structured_delete_errors(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    worktrees_root = work_dir / "git" / "worktrees"
    ws_id = await _seed_old_completed_pr_workspace(
        factory,
        updated_at=datetime.now(UTC) - timedelta(days=30),
    )
    worktree = worktrees_root / ws_id
    worktree.parent.mkdir(parents=True, exist_ok=True)
    worktree.write_text("not a directory", encoding="utf-8")

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
    )

    with _mock_worktree_remove_success(), structlog.testing.capture_logs() as captured:
        await runner._gc_completed_workspace_filesystem(ws_id)

    assert worktree.exists()
    assert any(
        record.get("event") == "monitor.filesystem_gc_failed"
        and record.get("delete_errors", [{}])[0]["reason_code"] == "PATH_DELETE_FAILED"
        for record in captured
    )


@pytest.mark.unit
async def test_completed_monitor_invokes_target_branch_reconciler(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    worktrees_root = work_dir / "git" / "worktrees"
    ws_id = await seed_monitoring_workspace(factory)
    calls: list[tuple[str, str]] = []

    async def _reconcile(*, repo_url: str, branch: str, workspace_id: str) -> object:
        calls.append((repo_url, branch))
        return {"status": "clean"}

    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
        post_merge_target_reconciler=_reconcile,
    )
    compose_patch, _compose_calls = mock_completed_compose_manager(
        ComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )
    )
    with compose_patch:
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=work_dir / "compose" / ws_id / "compose.yml",
        )

    assert calls == [("git@github.com:dimileeh/aira-web.git", "development")]


@pytest.mark.unit
async def test_completed_monitor_passes_volume_reaping_compose_teardown_to_filesystem_gc(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    worktrees_root = work_dir / "git" / "worktrees"
    ws_id = await seed_monitoring_workspace(factory)
    compose_dir = work_dir / "compose" / ws_id
    compose_file = compose_dir / "compose.yml"

    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))

    captured_teardown: dict[str, object] = {}

    async def _record_gc(*args: object, **kwargs: object) -> WorkspaceGCResult:
        del args
        captured_teardown["callback"] = kwargs.get("compose_teardown")
        return _empty_workspace_gc_result(work_dir)

    compose_patch, compose_calls = mock_completed_compose_manager(
        ComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
    )
    with (
        compose_patch,
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle.run_workspace_filesystem_gc",
            new=_record_gc,
        ),
    ):
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj_from_monitor",
            compose_file=compose_file,
        )

    callback = captured_teardown["callback"]
    assert callable(callback)
    candidate = WorkspaceGCCandidate(
        workspace_id=ws_id,
        status=WorkspaceStatus.completed.value,
        updated_at=datetime.now(UTC),
        age_hours=0,
        reason_code="COMPLETED_PR_IMMEDIATE_RECLAIM",
        worktree=WorkspaceGCPath(
            kind="worktree",
            path=worktrees_root / ws_id,
            exists=True,
            estimated_bytes=0,
        ),
        compose=WorkspaceGCPath(
            kind="compose",
            path=compose_dir,
            exists=True,
            estimated_bytes=0,
        ),
        auth=WorkspaceGCPath(
            kind="auth",
            path=work_dir / "auth" / ws_id,
            exists=True,
            estimated_bytes=0,
        ),
        compose_project_name="proj_from_candidate",
        compose_file_path=str(compose_file),
    )
    result = await callback(candidate)

    assert result.status == "succeeded"
    assert result.reason_code == "DOCKER_COMPOSE_DOWN_SUCCEEDED"
    assert compose_calls == [("proj_from_candidate", compose_file, ws_id, True)]
    assert not any(call.args[:2] == ["docker", "compose"] for call in cmd.calls)
