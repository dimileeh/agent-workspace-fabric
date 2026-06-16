"""Executor blocked-resume grant-consumption tests (FakeCommandRunner + PostgreSQL).

Split out of ``test_executor_part_002`` to keep first-party files under the
maintainability line limit. These exercise the single-use operator-grant
lifecycle on the ``resume_blocked_execution`` path: a grant is consumed only
after the validating→pushing CAS commits the validated change to push.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapter registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import OperatorGrantAuditRecord
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner
from tests.postgres import postgres_test_engine
from tests.unit.control.executor_paths import _test_worktrees_root

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.fixture
def executor(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> WorkspaceExecutor:
    compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
    validation = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
    pr = PullRequestCreator(fake)
    return WorkspaceExecutor(
        session_factory=factory,
        runner=fake,
        compose=compose,
        validation=validation,
        pr_creator=pr,
        config=ExecutorConfig(
            worktrees_root=tmp_path / "work" / "worktrees",
            compose_projects_root=tmp_path / "work" / "compose",
            default_models={
                AgentRuntime.codex: "gpt-5",
                AgentRuntime.claude_code: "sonnet",
                AgentRuntime.gemini: "gemini-2.5-pro",
            },
        ),
    )


def _queue_pre_push_diagnostics(fake: FakeCommandRunner, *, head: str = "deadbeef01") -> None:
    """Queue executor's committed-diff policy check plus the three canned
    git results ``PullRequestCreator`` reads for its pre-push diagnostic log line
    (``rev-parse HEAD``, ``rev-parse --abbrev-ref HEAD``, ``git log
    origin/<base>..HEAD``)."""
    fake.queue_result(
        returncode=0, stdout="src/fix.py\n"
    )  # final plan-only gate: committed base..HEAD --name-only
    fake.queue_result(returncode=0, stdout="M\0src/fix.py\0")  # committed base..HEAD diff
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # rev-parse HEAD
    fake.queue_result(returncode=0, stdout="awf/ws_test\n")  # abbrev-ref
    fake.queue_result(returncode=0, stdout="abc1234 commit\n")  # log ahead-of-base


def _queue_validation_head(fake: FakeCommandRunner, head: str = "deadbeef01") -> None:
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # pre-validation rev-parse HEAD


async def _seed_resumable_blocked_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    block_epoch: int = 1,
    grant_path: str | None = "pyproject.toml",
    directive: str | None = None,
    reclaim_to_running: bool = True,
) -> str:
    """Insert a workspace the worker has already re-claimed ``blocked → running``
    after an operator resolved the pause, ready for ``resume_blocked_execution``
    to drive to ``pushing``.

    An approve-and-keep grant is seeded when ``grant_path`` is set; a revert/redo
    ``directive`` is armed in ``pending_operator_hint`` when provided. Set
    ``reclaim_to_running=False`` to leave the row ``blocked`` (the worker's resume
    CAS never landed), so ``resume_blocked_execution`` short-circuits on its
    ``running`` recheck."""
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="trivial",
            task_prompt="Add a docstring.",
            agent="codex",
            test_commands=["pytest -q"],
            task_policy={},
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.blocked, reason_code="X")
        ws.block_epoch = block_epoch
        # The worker re-claims the operator-cleared workspace back to ``running``
        # before handing it to ``resume_blocked_execution``.
        if reclaim_to_running:
            await repo.transition(ws, to=WorkspaceStatus.running, reason_code="X")
        if directive is not None:
            ws.pending_operator_hint = {
                "status": "pending",
                "directive": directive,
                "reason": "operator asked for a redo",
            }
        if grant_path is not None:
            s.add(
                OperatorGrantAuditRecord(
                    id=f"grant_{ws.id}",
                    workspace_id=ws.id,
                    operator="op@example.com",
                    reason="approved benign config split",
                    normalized_path=grant_path,
                    block_epoch=block_epoch,
                )
            )
        await s.commit()
        (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


async def _active_grant(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> OperatorGrantAuditRecord:
    async with factory() as s:
        row = await s.execute(
            select(OperatorGrantAuditRecord).where(
                OperatorGrantAuditRecord.workspace_id == workspace_id
            )
        )
        return row.scalars().one()


def _queue_blocked_resume_to_push(fake: FakeCommandRunner, *, ws_id: str) -> None:
    # Approve-and-keep resume skips the agent; the post-agent commit, the
    # validation, the pre-push policy gates, and the push all still run, so
    # the queue mirrors the happy path MINUS the leading ``adapter.run``.
    fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
    fake.queue_result(returncode=0)  # git add
    fake.queue_result(returncode=0, stdout="CHANGELOG.md\n")  # cached diff (non-empty)
    fake.queue_result(returncode=0)  # git commit
    fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
    fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
    _queue_validation_head(fake)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd
    _queue_pre_push_diagnostics(fake)
    fake.queue_result(returncode=0)  # git push
    fake.queue_result(
        returncode=0,
        stdout="https://github.com/dimileeh/aira-agent/pull/777\n",
    )  # gh pr create


@pytest.mark.unit
async def test_blocked_resume_consumes_grants_after_push_transition(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # On a successful resume the validating→pushing CAS commits the validated
    # change to push, so the single-use operator grant is consumed.
    ws_id = await _seed_resumable_blocked_workspace(factory)
    _queue_blocked_resume_to_push(fake, ws_id=ws_id)

    await executor.resume_blocked_execution(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
    grant = await _active_grant(factory, ws_id)
    assert grant.consumed_at is not None


@pytest.mark.unit
async def test_blocked_resume_keeps_grants_when_push_transition_loses(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression for PRRT_kwDOSJAM6s6J2gLd: if the validating→pushing CAS
    # loses (concurrent cancel, version race, stale status) the workspace
    # never enters ``pushing``, so the grant MUST stay active — consuming it
    # before the transition would strand a later protected check on the same
    # resume with no usable grant.
    ws_id = await _seed_resumable_blocked_workspace(factory)
    _queue_blocked_resume_to_push(fake, ws_id=ws_id)

    real_transition = executor._transition_if_current

    async def _transition_or_lose_pushing(
        workspace_id: str, *, from_status: Any, to: Any, reason: str, action: str
    ) -> bool:
        if to is WorkspaceStatus.pushing:
            return False  # simulate a lost CAS at the push transition
        return await real_transition(
            workspace_id,
            from_status=from_status,
            to=to,
            reason=reason,
            action=action,
        )

    monkeypatch.setattr(executor, "_transition_if_current", _transition_or_lose_pushing)

    await executor.resume_blocked_execution(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status != WorkspaceStatus.completed.value
        assert ws.status != WorkspaceStatus.pushing.value
    grant = await _active_grant(factory, ws_id)
    assert grant.consumed_at is None


@pytest.mark.unit
async def test_blocked_resume_with_directive_reinvokes_agent_with_directive(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Revert/redo resume: the operator armed a directive (no approve-and-keep
    # grant), so the agent is re-invoked with the directive appended to the
    # task prompt (NOT skipped as on the grant path) and the run proceeds.
    ws_id = await _seed_resumable_blocked_workspace(
        factory, grant_path=None, directive="revert the protected change"
    )
    fake.queue_result(returncode=0, stdout="codex finished")  # adapter re-invoked
    _queue_blocked_resume_to_push(fake, ws_id=ws_id)

    await executor.resume_blocked_execution(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
    # The agent received the operator directive appended to its prompt.
    agent_calls = [
        call
        for call in fake.calls
        if call.input_bytes is not None and b"Operator directive" in call.input_bytes
    ]
    assert agent_calls, "expected the agent to be re-invoked with the directive"
    assert b"revert the protected change" in agent_calls[0].input_bytes


@pytest.mark.unit
async def test_blocked_resume_missing_workspace_is_a_noop(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
) -> None:
    # The resume target vanished (e.g. concurrent destroy) before dispatch:
    # ``_begin_execution`` loads nothing and returns without touching the agent.
    await executor.resume_blocked_execution("ws_does_not_exist")

    assert fake.calls == []


@pytest.mark.unit
async def test_blocked_resume_short_circuits_when_not_running(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # The worker's blocked→running resume CAS never landed (lost race), so the
    # row is still ``blocked``. ``_begin_execution`` fails its ``running``
    # recheck and returns early: no agent run, status unchanged.
    ws_id = await _seed_resumable_blocked_workspace(factory, reclaim_to_running=False)

    await executor.resume_blocked_execution(ws_id)

    assert fake.calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.blocked.value
