"""Executor monitor-recovery coverage split for normal and existing-PR paths."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.validation import ValidationResult
from tests.postgres import postgres_test_engine
from tests.unit.control.test_executor_monitor_recovery_parts.test_executor_monitor_recovery_part_001 import (
    _FEATURE_TASK_PROMPT,
    _all_adapter_args,
    _all_adapter_prompts,
    _all_push_and_pr_create_calls,
    _make_executor,
    _queue_push_and_pr,
    _queue_validation_head,
    _seed_ready_workspace_no_recovery,
    _seed_ready_workspace_with_recovery,
    _setup_dependency_retry_success_result,
    _SetupFailureValidation,
)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.mark.unit
async def test_setup_dependency_event_recording_failure_does_not_block_agent_run(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation = _SetupFailureValidation(
        ValidationResult(commands=[_setup_dependency_retry_success_result(tmp_path)])
    )
    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        validation=validation,
    )
    ws_id = await _seed_ready_workspace_no_recovery(factory)

    async def _raise_event_recording_failure(**_kwargs: Any) -> None:
        raise RuntimeError("setup dependency event commit failed")

    monkeypatch.setattr(
        executor,
        "_record_setup_dependency_network_events",
        _raise_event_recording_failure,
    )

    fake.queue_result(returncode=0, stdout="codex finished")  # adapter
    fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
    fake.queue_result(returncode=0)  # git add
    fake.queue_result(returncode=0, stdout="CHANGELOG.md\n")  # cached diff
    fake.queue_result(returncode=0)  # git commit
    fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
    fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
    _queue_validation_head(fake)
    _queue_push_and_pr(fake)

    await executor.execute(ws_id)

    assert validation.calls == [("setup", "pre_agent"), ("post_agent", "validate")]
    assert len(_all_adapter_args(fake)) == 1
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
        assert ws.failure_message is None


@pytest.mark.unit
async def test_executor_normal_path_unchanged_when_no_recovery_op(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Regression guard: workspaces without a validate-only recovery op
    must continue running planning/agent/feature execution as before.
    """
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_no_recovery(factory)

    # Standard initial-execution sequence (mirrors test_executor.py).
    fake.queue_result(returncode=0, stdout="codex finished")  # adapter
    fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
    fake.queue_result(returncode=0)  # git add
    fake.queue_result(returncode=0, stdout="CHANGELOG.md\n")  # cached diff
    fake.queue_result(returncode=0)  # git commit
    fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
    fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
    _queue_validation_head(fake)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation
    _queue_push_and_pr(fake)

    await executor.execute(ws_id)

    adapter_invocations = _all_adapter_args(fake)
    assert len(adapter_invocations) == 1
    prompts = _all_adapter_prompts(fake)
    assert _FEATURE_TASK_PROMPT in prompts

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value


@pytest.mark.unit
async def test_recovery_skips_push_when_pr_already_exists(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A workspace in recovery with an existing PR must not re-push or
    re-create the PR.
    """
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, pr_url="https://github.com/x/y/pull/1"
    )

    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_push_and_pr_create_calls(fake) == []

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
        assert any(
            event.event_type == "workspace.state_changed"
            and event.reason_code == "RECOVERY_VALIDATION_OK"
            and event.old_state == WorkspaceStatus.validating.value
            and event.new_state == WorkspaceStatus.completed.value
            for event in events
        )
