"""Provenance guards for unpublished repair rollback."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import CommandResult
from awf.db.enums import OperationStatus, OperationType
from awf.db.models import Operation
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_operations import build_monitor_operation_payload
from awf.runtime.pr_monitor_runner import remote_repair_unpublished
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import seed_monitoring_workspace


class _RollbackCommandRunner:
    def __init__(self, *, remote_head: str, local_head: str) -> None:
        self.remote_head = remote_head
        self.local_head = local_head
        self.calls: list[tuple[str, ...]] = []

    async def run(self, args: list[str], **_kwargs: object) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        if "rev-parse" in call:
            ref = call[call.index("rev-parse") + 1]
            head = self.remote_head if ref == "FETCH_HEAD" else self.local_head
            return CommandResult(returncode=0, stdout=f"{head}\n", stderr="")
        if "merge-base" in call and "--is-ancestor" in call:
            return CommandResult(returncode=0, stdout="", stderr="")
        if "diff" in call:
            return CommandResult(returncode=0, stdout="M\0src/example.py\0", stderr="")
        if "reset" in call:
            self.local_head = self.remote_head
            return CommandResult(returncode=0, stdout="", stderr="")
        if "status" in call:
            return CommandResult(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {call}")


def _runner(tmp_path: Path, command_runner: _RollbackCommandRunner) -> SimpleNamespace:
    async def _fetch(**_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout="", stderr="")

    return SimpleNamespace(
        _worktrees_root=tmp_path,
        _deps=SimpleNamespace(runner=command_runner),
        _remote_branch_fetch_once=_fetch,
    )


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture(autouse=True)
def _verified_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        remote_repair_unpublished,
        "_verified_awf_comment_repair_worktree",
        lambda **_kwargs: True,
        raising=False,
    )

    async def _ownership_ok(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(remote_repair_unpublished, "repair_agent_runtime_ownership", _ownership_ok)


async def _seed_unpublished_operation(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    operation_type: str,
    action: str,
    remote_head: str,
    status: OperationStatus = OperationStatus.running,
    local_terminal_head_sha: str | None = None,
) -> str:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        payload = build_monitor_operation_payload(
            workspace=workspace,
            action=action,
            requested_action=action,
            reason="test",
            reason_code="TEST",
            pr_number=42,
            source_head_sha=remote_head,
            source_base_sha=workspace.base_commit,
            target_branch="main",
            remote_branch=f"awf/{workspace_id}",
        )
        operation = await OperationRepository(session).create(
            workspace_id=workspace_id,
            operation_type=operation_type,
            status=status,
            payload=payload,
        )
        if local_terminal_head_sha is not None:
            operation.result = {"local_terminal_head_sha": local_terminal_head_sha}
            await session.flush()
        await session.commit()
        return operation.id


@pytest.mark.unit
async def test_operator_hint_unpublished_commit_is_not_reset_without_comment_provenance(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    remote_head = "a" * 40
    operator_hint_head = "b" * 40
    await _seed_unpublished_operation(
        factory,
        workspace_id,
        operation_type=OperationType.comment_repair.value,
        action="operator_hint_repair",
        remote_head=remote_head,
    )
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    commands = _RollbackCommandRunner(remote_head=remote_head, local_head=operator_hint_head)
    runner = _runner(tmp_path, commands)
    runner._deps.session_factory = factory

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=operator_hint_head,
        state=MonitorState(),
        current_operation_id="op_comment_repair_current",
    )

    assert result is None
    assert restored_head == operator_hint_head
    assert commands.local_head == operator_hint_head
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_unpublished_comment_repair_provenance_ignores_operator_hint_operations(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    remote_head = "a" * 40
    operator_hint_head = "c" * 40
    await _seed_unpublished_operation(
        factory,
        workspace_id,
        operation_type=OperationType.comment_repair.value,
        action="operator_hint_repair",
        remote_head=remote_head,
        local_terminal_head_sha=operator_hint_head,
    )
    runner = SimpleNamespace(_deps=SimpleNamespace(session_factory=factory))

    has_comment_provenance = (
        await remote_repair_unpublished._unpublished_comment_repair_has_operation_provenance(
            runner,
            workspace_id=workspace_id,
            remote_pr_head=remote_head,
            discarded_local_head=operator_hint_head,
        )
    )
    has_conflicting_provenance = (
        await remote_repair_unpublished._unpublished_non_comment_repair_has_operation_provenance(
            runner,
            workspace_id=workspace_id,
            remote_pr_head=remote_head,
            discarded_local_head=operator_hint_head,
        )
    )

    assert has_comment_provenance is False
    assert has_conflicting_provenance is True


@pytest.mark.unit
async def test_failed_comment_repair_without_terminal_head_does_not_own_user_commits(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    remote_head = "a" * 40
    user_commit_head = "b" * 40
    await _seed_unpublished_operation(
        factory,
        workspace_id,
        operation_type=OperationType.comment_repair.value,
        action="comment_repair",
        remote_head=remote_head,
        status=OperationStatus.failed,
    )
    runner = SimpleNamespace(_deps=SimpleNamespace(session_factory=factory))

    has_comment_provenance = (
        await remote_repair_unpublished._unpublished_comment_repair_has_operation_provenance(
            runner,
            workspace_id=workspace_id,
            remote_pr_head=remote_head,
            discarded_local_head=user_commit_head,
        )
    )

    assert has_comment_provenance is False


@pytest.mark.unit
async def test_failed_comment_repair_with_terminal_head_matches_discarded_commits(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    remote_head = "a" * 40
    repair_head = "b" * 40
    await _seed_unpublished_operation(
        factory,
        workspace_id,
        operation_type=OperationType.comment_repair.value,
        action="comment_repair",
        remote_head=remote_head,
        status=OperationStatus.failed,
        local_terminal_head_sha=repair_head,
    )
    runner = SimpleNamespace(_deps=SimpleNamespace(session_factory=factory))

    has_comment_provenance = (
        await remote_repair_unpublished._unpublished_comment_repair_has_operation_provenance(
            runner,
            workspace_id=workspace_id,
            remote_pr_head=remote_head,
            discarded_local_head=repair_head,
        )
    )

    assert has_comment_provenance is True


@pytest.mark.unit
async def test_running_comment_repair_without_terminal_head_does_not_own_user_commits(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    remote_head = "a" * 40
    user_commit_head = "b" * 40
    await _seed_unpublished_operation(
        factory,
        workspace_id,
        operation_type=OperationType.comment_repair.value,
        action="comment_repair",
        remote_head=remote_head,
        status=OperationStatus.running,
    )
    runner = SimpleNamespace(_deps=SimpleNamespace(session_factory=factory))

    has_comment_provenance = (
        await remote_repair_unpublished._unpublished_comment_repair_has_operation_provenance(
            runner,
            workspace_id=workspace_id,
            remote_pr_head=remote_head,
            discarded_local_head=user_commit_head,
        )
    )

    assert has_comment_provenance is False


@pytest.mark.unit
async def test_running_comment_repair_with_terminal_head_matches_discarded_commits(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    remote_head = "a" * 40
    repair_head = "b" * 40
    await _seed_unpublished_operation(
        factory,
        workspace_id,
        operation_type=OperationType.comment_repair.value,
        action="comment_repair",
        remote_head=remote_head,
        status=OperationStatus.running,
        local_terminal_head_sha=repair_head,
    )
    runner = SimpleNamespace(_deps=SimpleNamespace(session_factory=factory))

    has_comment_provenance = (
        await remote_repair_unpublished._unpublished_comment_repair_has_operation_provenance(
            runner,
            workspace_id=workspace_id,
            remote_pr_head=remote_head,
            discarded_local_head=repair_head,
        )
    )

    assert has_comment_provenance is True


@pytest.mark.unit
def test_operation_owns_discarded_commits_requires_terminal_head_for_failed_operation() -> None:
    remote_head = "a" * 40
    user_head = "b" * 40
    operation = Operation(
        id="op_failed",
        workspace_id="ws",
        type=OperationType.comment_repair.value,
        status=OperationStatus.failed.value,
        payload={"source_head_sha": remote_head},
    )
    assert (
        remote_repair_unpublished._operation_owns_discarded_commits(
            operation,
            remote_pr_head=remote_head,
            discarded_local_head=user_head,
        )
        is False
    )


@pytest.mark.unit
def test_operation_owns_discarded_commits_matches_recorded_terminal_head() -> None:
    remote_head = "a" * 40
    repair_head = "b" * 40
    operation = Operation(
        id="op_failed",
        workspace_id="ws",
        type=OperationType.comment_repair.value,
        status=OperationStatus.failed.value,
        payload={"source_head_sha": remote_head},
        result={"local_terminal_head_sha": repair_head},
    )
    assert (
        remote_repair_unpublished._operation_owns_discarded_commits(
            operation,
            remote_pr_head=remote_head,
            discarded_local_head=repair_head,
        )
        is True
    )


@pytest.mark.unit
async def test_failed_comment_repair_with_terminal_head_in_failure_evidence_matches_discarded_commits(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    remote_head = "a" * 40
    repair_head = "b" * 40
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        payload = build_monitor_operation_payload(
            workspace=workspace,
            action="comment_repair",
            requested_action="address_comments",
            reason="test",
            reason_code="TEST",
            pr_number=42,
            source_head_sha=remote_head,
            source_base_sha=workspace.base_commit,
            target_branch="main",
            remote_branch=f"awf/{workspace_id}",
        )
        operation = await OperationRepository(session).create(
            workspace_id=workspace_id,
            operation_type=OperationType.comment_repair.value,
            status=OperationStatus.failed,
            payload=payload,
        )
        operation.result = {
            "status": "failed",
            "outcome": "agent_verdict_protocol_violation",
            "reason_code": "AGENT_VERDICT_PROTOCOL_VIOLATION",
            "pushed": False,
            "failure_evidence": {
                "local_terminal_head_sha": repair_head,
                "reason_code": "AGENT_VERDICT_PROTOCOL_VIOLATION",
            },
        }
        await session.flush()
        await session.commit()
    runner = SimpleNamespace(_deps=SimpleNamespace(session_factory=factory))

    has_comment_provenance = (
        await remote_repair_unpublished._unpublished_comment_repair_has_operation_provenance(
            runner,
            workspace_id=workspace_id,
            remote_pr_head=remote_head,
            discarded_local_head=repair_head,
        )
    )

    assert has_comment_provenance is True


@pytest.mark.unit
async def test_failed_comment_repair_without_terminal_head_is_not_reset(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    remote_head = "a" * 40
    user_commit_head = "b" * 40
    await _seed_unpublished_operation(
        factory,
        workspace_id,
        operation_type=OperationType.comment_repair.value,
        action="comment_repair",
        remote_head=remote_head,
        status=OperationStatus.failed,
    )
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    commands = _RollbackCommandRunner(remote_head=remote_head, local_head=user_commit_head)
    runner = _runner(tmp_path, commands)
    runner._deps.session_factory = factory

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=user_commit_head,
        state=MonitorState(),
        current_operation_id="op_comment_repair_current",
    )

    assert result is None
    assert restored_head == user_commit_head
    assert commands.local_head == user_commit_head
    assert all("reset" not in call for call in commands.calls)
