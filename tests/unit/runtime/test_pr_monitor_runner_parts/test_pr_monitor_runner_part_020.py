"""Unit tests for focused ``pr_monitor_runner`` provider-recovery execute paths."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_mock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.adapters.provider_failures import AGENT_SERVICE_UNHEALTHY
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import AgentRuntime, OperationStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    AddressComments,
    AddressOperatorHint,
    CheckFailure,
    MonitorState,
    OperatorHint,
    ReportCiFailure,
    ReviewThread,
    SyncBase,
)
from awf.runtime.pr_monitor_runner.helpers import _with_ci_failures
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

from .test_pr_monitor_runner_part_004 import (
    _configure_provider_monitor_workspace,
    _green_status,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_provider_agent_error_still_raises_full_fallback_for_non_monitor_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _configure_provider_monitor_workspace(
        factory,
        workspace_id,
        max_same_provider_retries=0,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.provider_ops.create_provider_recovery_attempt_row",
        return_value=SimpleNamespace(action="fallback", in_place=False),
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    exc = AgentRunError(
        agent=AgentRuntime.claude_code,
        result=CommandResult(
            returncode=1,
            stdout="",
            stderr="Gemini MODEL_CAPACITY_EXHAUSTED",
        ),
        details={"provider": "google", "model": "gemini-2.5-pro"},
    )

    with pytest.raises(ProviderRecoveryFallbackError):
        await runner._handle_provider_agent_run_error(workspace_id, exc)


@pytest.mark.unit
async def test_provider_agent_auth_failure_raises_provider_auth_failed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _configure_provider_monitor_workspace(
        factory,
        workspace_id,
        agent="codex",
        model="gpt-5.5",
        fallback_agent="gemini",
        fallback_provider="google",
        fallback_model="gemini-3.1-pro-preview",
        max_same_provider_retries=3,
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    exc = AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(
            returncode=1,
            stdout="",
            stderr=(
                "Failed to refresh token: Your access token could not be refreshed "
                "because your refresh token was already used. websocket 401 Unauthorized "
                "token_expired"
            ),
        ),
        details={"provider": "openai", "model": "gpt-5.5"},
    )

    with pytest.raises(ProviderRecoveryAuthError):
        await runner._handle_provider_agent_run_error(workspace_id, exc)

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        terminal_events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.provider_recovery_terminal"
        ]

    assert len(terminal_events) == 1
    assert terminal_events[0].reason_code == "PROVIDER_AUTH_FAILED"
    assert workspace.task_policy["provider_recovery_state"]["action"] == "terminal"
    assert workspace.task_policy["provider_recovery_state"]["source_reason_code"] == (
        "AGENT_AUTH_FAILED"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    ["sync_base", "ci_repair", "comment_repair", "operator_hint_repair"],
)
async def test_agent_service_recovery_sentinel_finishes_monitor_operation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    case: str,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    status = _green_status()
    state = MonitorState(started_at=0.0)
    recovery_details: dict[str, object] = {
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "source_reason_code": "AGENT_IDLE_TIMEOUT",
        "service_healthy": False,
        "restart_attempts": 2,
    }
    expected_result: dict[str, object] = {
        "status": "failed",
        "outcome": "agent_service_recovery_failed",
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "agent_service_recovery": recovery_details,
        "pushed": False,
    }

    if case == "sync_base":
        action = SyncBase()
        target_method = "_run_sync_base"
        expected_type = "sync_base"
    elif case == "ci_repair":
        failures = (CheckFailure(name="tests", conclusion="FAILURE", log_excerpt="boom"),)
        action = ReportCiFailure(failures=failures)
        status = _with_ci_failures(status, failures)
        target_method = "_run_ci_fix"
        expected_type = "ci_repair"
        expected_result["failure_count"] = 1
    elif case == "comment_repair":
        thread = ReviewThread(
            thread_id="T_service",
            path="src/app.py",
            line=12,
            body_excerpt="please fix",
            author="reviewer",
        )
        action = AddressComments(threads=(thread,), review_comments=())
        status = replace(status, unresolved_inline_threads=(thread,))
        target_method = "_run_fix_cycle"
        expected_type = "comment_repair"
        expected_result.update({"thread_count": 1, "review_comment_count": 0})
    else:
        hint = OperatorHint(
            reason="repair after operator guide",
            directive="fix it",
            operation_id="op_operator_hint",
            requested_at="2026-06-27T00:00:00+00:00",
            reason_code="OPERATOR_GUIDE",
        )
        action = AddressOperatorHint(hint=hint)
        state = MonitorState(started_at=0.0, pending_operator_hint=hint)
        target_method = "_run_operator_hint_cycle"
        expected_type = "comment_repair"

    async def _raise_agent_service_recovery_failed(**_kwargs: object) -> object:
        raise _MonitorAgentServiceRecoveryFailedError(
            "agent service unhealthy",
            reason_code=AGENT_SERVICE_UNHEALTHY,
            details=recovery_details,
        )

    mocker.patch.object(runner, target_method, _raise_agent_service_recovery_failed)

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError):
        await runner._execute(
            action=action,
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=status,
            state=state,
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    operation = operations[0]
    assert operation.type == expected_type
    assert operation.status == OperationStatus.failed.value
    assert operation.result == expected_result
    assert operation.error_code == AGENT_SERVICE_UNHEALTHY
    assert operation.error_message == "agent service unhealthy"


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    ["sync_base", "ci_repair", "comment_repair", "operator_hint_repair"],
)
async def test_superseded_agent_service_recovery_cancels_monitor_operation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    case: str,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    status = _green_status()
    state = MonitorState(started_at=0.0)
    recovery_details: dict[str, object] = {
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "source_reason_code": "AGENT_IDLE_TIMEOUT",
        "service_healthy": False,
        "restart_attempts": 1,
        "superseded_reason": "monitor_claim_changed",
    }
    expected_result: dict[str, object] = {
        "status": "cancelled",
        "outcome": "agent_service_recovery_superseded",
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "agent_service_recovery": recovery_details,
        "pushed": False,
    }

    if case == "sync_base":
        action = SyncBase()
        target_method = "_run_sync_base"
        expected_type = "sync_base"
    elif case == "ci_repair":
        failures = (CheckFailure(name="tests", conclusion="FAILURE", log_excerpt="boom"),)
        action = ReportCiFailure(failures=failures)
        status = _with_ci_failures(status, failures)
        target_method = "_run_ci_fix"
        expected_type = "ci_repair"
        expected_result["failure_count"] = 1
    elif case == "comment_repair":
        thread = ReviewThread(
            thread_id="T_service",
            path="src/app.py",
            line=12,
            body_excerpt="please fix",
            author="reviewer",
        )
        action = AddressComments(threads=(thread,), review_comments=())
        status = replace(status, unresolved_inline_threads=(thread,))
        target_method = "_run_fix_cycle"
        expected_type = "comment_repair"
        expected_result.update({"thread_count": 1, "review_comment_count": 0})
    else:
        hint = OperatorHint(
            reason="repair after operator guide",
            directive="fix it",
            operation_id="op_operator_hint",
            requested_at="2026-06-27T00:00:00+00:00",
            reason_code="OPERATOR_GUIDE",
        )
        action = AddressOperatorHint(hint=hint)
        state = MonitorState(started_at=0.0, pending_operator_hint=hint)
        target_method = "_run_operator_hint_cycle"
        expected_type = "comment_repair"

    async def _raise_agent_service_recovery_superseded(**_kwargs: object) -> object:
        raise _MonitorAgentServiceRecoverySupersededError(
            "agent service recovery superseded",
            reason_code=AGENT_SERVICE_UNHEALTHY,
            details=recovery_details,
        )

    mocker.patch.object(runner, target_method, _raise_agent_service_recovery_superseded)

    with pytest.raises(_MonitorAgentServiceRecoverySupersededError):
        await runner._execute(
            action=action,
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=status,
            state=state,
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    operation = operations[0]
    assert operation.type == expected_type
    assert operation.status == OperationStatus.cancelled.value
    assert operation.result == expected_result
    assert operation.error_code == AGENT_SERVICE_UNHEALTHY
    assert operation.error_message == "agent service recovery superseded"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error_cls", "outcome", "reason_code"),
    [
        (ProviderRecoveryRetryError, "provider_retry", "PROVIDER_OUTAGE"),
        (ProviderRecoveryFallbackError, "provider_fallback", "PROVIDER_FALLBACK"),
        (ProviderRecoveryAuthError, "provider_auth_failed", "PROVIDER_AUTH_FAILED"),
    ],
)
async def test_sync_base_provider_recovery_exceptions_finish_operation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    error_cls: type[Exception],
    outcome: str,
    reason_code: str,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _raise_provider_error(**_kwargs: object) -> object:
        raise error_cls()

    mocker.patch.object(runner, "_run_sync_base", _raise_provider_error)

    with pytest.raises(error_cls):
        await runner._execute(
            action=SyncBase(),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=_green_status(),
            state=MonitorState(started_at=0.0),
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    operation = operations[0]
    assert operation.type == "sync_base"
    assert operation.status == OperationStatus.failed.value
    assert operation.result == {
        "status": "failed",
        "outcome": outcome,
        "reason_code": reason_code,
        "pushed": False,
    }
    assert operation.error_code == reason_code


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error_cls", "outcome", "reason_code"),
    [
        (ProviderRecoveryRetryError, "provider_retry", "PROVIDER_OUTAGE"),
        (ProviderRecoveryFallbackError, "provider_fallback", "PROVIDER_FALLBACK"),
        (ProviderRecoveryAuthError, "provider_auth_failed", "PROVIDER_AUTH_FAILED"),
    ],
)
async def test_ci_repair_provider_recovery_exceptions_finish_operation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    error_cls: type[Exception],
    outcome: str,
    reason_code: str,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _raise_provider_error(**_kwargs: object) -> object:
        raise error_cls()

    mocker.patch.object(runner, "_run_ci_fix", _raise_provider_error)
    failures = (CheckFailure(name="tests", conclusion="FAILURE", log_excerpt="boom"),)

    with pytest.raises(error_cls):
        await runner._execute(
            action=ReportCiFailure(failures=failures),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=_with_ci_failures(_green_status(), failures),
            state=MonitorState(started_at=0.0),
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    operation = operations[0]
    assert operation.type == "ci_repair"
    assert operation.status == OperationStatus.failed.value
    assert operation.result == {
        "status": "failed",
        "outcome": outcome,
        "reason_code": reason_code,
        "failure_count": 1,
        "pushed": False,
    }
    assert operation.error_code == reason_code


@pytest.mark.unit
async def test_ci_repair_provider_recovery_includes_repair_salvage_in_operation_result(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6N6xha: salvage metadata must surface on provider retry."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    repair_salvage = {
        "patch_path": str(tmp_path / "artifacts/salvage/ws.patch"),
        "patch_sha256": "d" * 64,
        "patch_bytes": 10,
        "affected_paths": ["src/fix.py"],
        "phase": "ci_repair_commit_sink",
        "operation_type": "ci_repair",
        "operation_id": None,
        "operation_start_head": "abc1234567890def",
        "created_at": "2026-07-02T00:00:00+00:00",
    }

    async def _raise_provider_retry_with_salvage(**_kwargs: object) -> object:
        raise ProviderRecoveryRetryError(
            details={
                "phase": "ci_repair_commit_sink",
                "stranded_paths": ["src/fix.py"],
                "repair_salvage": repair_salvage,
            }
        )

    mocker.patch.object(runner, "_run_ci_fix", _raise_provider_retry_with_salvage)
    failures = (CheckFailure(name="tests", conclusion="FAILURE", log_excerpt="boom"),)

    with pytest.raises(ProviderRecoveryRetryError):
        await runner._execute(
            action=ReportCiFailure(failures=failures),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=_with_ci_failures(_green_status(), failures),
            state=MonitorState(started_at=0.0),
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    operation = operations[0]
    assert operation.type == "ci_repair"
    assert operation.result is not None
    assert operation.result["repair_salvage"] == repair_salvage
    assert operation.result["stranded_paths"] == ["src/fix.py"]
    assert operation.result["phase"] == "ci_repair_commit_sink"


@pytest.mark.unit
async def test_ci_repair_dirty_commit_salvage_error_surfaces_in_operation_result(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6N8a5t: salvage failures must surface terminally."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    salvage_error = {
        "reason_code": "REPAIR_SALVAGE_UNEXPECTED",
        "message": "Command '['git', 'diff']' timed out after 30.0 seconds",
    }
    failure_details = {
        "phase": "ci_repair_commit_sink",
        "stranded_paths": ["src/fix.py"],
        "salvage_error": salvage_error,
        "provider_error_stderr": "MODEL_CAPACITY_EXHAUSTED",
        "pushed": False,
    }

    async def _return_dirty_commit_salvage_failure(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=(
                "CI repair commit sink failed; dirty repair output could not "
                "be salvaged before provider recovery."
            ),
            reason_code="REPAIR_DIRTY_COMMIT_FAILED",
            details=failure_details,
        )

    mocker.patch.object(runner, "_run_ci_fix", _return_dirty_commit_salvage_failure)
    failures = (CheckFailure(name="tests", conclusion="FAILURE", log_excerpt="boom"),)

    terminated = await runner._execute(
        action=ReportCiFailure(failures=failures),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_with_ci_failures(_green_status(), failures),
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminated is True
    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    operation = operations[0]
    assert operation.type == "ci_repair"
    assert operation.result is not None
    failure_evidence = operation.result["failure_evidence"]
    assert isinstance(failure_evidence, dict)
    assert failure_evidence["salvage_error"] == salvage_error
    assert failure_evidence["stranded_paths"] == ["src/fix.py"]
    assert failure_evidence["phase"] == "ci_repair_commit_sink"
    assert failure_evidence["provider_error_stderr"] == "MODEL_CAPACITY_EXHAUSTED"
    assert operation.error_code == "REPAIR_DIRTY_COMMIT_FAILED"


@pytest.mark.unit
async def test_ci_repair_dirty_commit_rollback_error_surfaces_in_operation_result(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """Rollback failures on the commit-sink recovery path must surface terminally."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    rollback_error = {
        "cause": "reset_failed",
        "message": "CI repair residue rollback failed (git reset --hard): fatal: could not parse object",
    }
    failure_details = {
        "phase": "ci_repair_commit_sink",
        "rollback_error": rollback_error,
        "provider_error_stderr": "MODEL_CAPACITY_EXHAUSTED",
        "pushed": False,
    }

    async def _return_dirty_commit_rollback_failure(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=(
                "CI repair commit sink failed; salvage succeeded but "
                "worktree rollback failed before provider recovery."
            ),
            reason_code="REPAIR_DIRTY_COMMIT_FAILED",
            details=failure_details,
        )

    mocker.patch.object(runner, "_run_ci_fix", _return_dirty_commit_rollback_failure)
    failures = (CheckFailure(name="tests", conclusion="FAILURE", log_excerpt="boom"),)

    terminated = await runner._execute(
        action=ReportCiFailure(failures=failures),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_with_ci_failures(_green_status(), failures),
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminated is True
    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    operation = operations[0]
    assert operation.type == "ci_repair"
    assert operation.result is not None
    failure_evidence = operation.result["failure_evidence"]
    assert isinstance(failure_evidence, dict)
    assert failure_evidence["rollback_error"] == rollback_error
    assert failure_evidence["phase"] == "ci_repair_commit_sink"
    assert failure_evidence["provider_error_stderr"] == "MODEL_CAPACITY_EXHAUSTED"
    assert operation.error_code == "REPAIR_DIRTY_COMMIT_FAILED"


@pytest.mark.unit
async def test_comment_repair_provider_auth_exception_finishes_operation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_auth",
        path="src/app.py",
        line=12,
        body_excerpt="please fix",
        author="reviewer",
    )

    async def _raise_provider_auth(**_kwargs: object) -> object:
        raise ProviderRecoveryAuthError()

    mocker.patch.object(runner, "_run_fix_cycle", _raise_provider_auth)

    with pytest.raises(ProviderRecoveryAuthError):
        await runner._execute(
            action=AddressComments(threads=(thread,), review_comments=()),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=replace(_green_status(), unresolved_inline_threads=(thread,)),
            state=MonitorState(started_at=0.0),
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    operation = operations[0]
    assert operation.type == "comment_repair"
    assert operation.status == OperationStatus.failed.value
    assert operation.result == {
        "status": "failed",
        "outcome": "provider_auth_failed",
        "reason_code": "PROVIDER_AUTH_FAILED",
        "pushed": False,
    }
    assert operation.error_code == "PROVIDER_AUTH_FAILED"
