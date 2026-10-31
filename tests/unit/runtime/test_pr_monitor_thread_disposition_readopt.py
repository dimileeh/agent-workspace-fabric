"""``fixed`` dispositions crossing a moved-head re-adoption live, once verified (#1138, D2).

A moved head used to retire every code-dependent disposition at re-adoption, so
the #1138 fallback reopened threads AWF had fixed in commits the new head still
contains. Now a bound ``fixed`` / ``fixed_branch_evidence`` record crosses live
with a readopt-pending check; the successor's runner step confirms each cited
commit is an ancestor of the adopted head (``git merge-base --is-ancestor``) and
otherwise retires it exactly as the seed used to. The thread must still be
resolved by AWF's identity with an unchanged conversation (the gate's binding
check). ``false_positive`` keeps today's moved-head retirement.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.forge_thread_audit import AuditedReviewThread
from awf.db.models import WorkspaceEvent
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import (
    _disposition_ack_key,
    _disposition_readopt_pending_key,
    _disposition_retired_key,
)
from awf.runtime.pr_monitor import MonitorState, NotifyHuman
from awf.runtime.pr_monitor_runner.thread_disposition import (
    ThreadDisposition,
    read_item_disposition,
    record_item_disposition,
)
from awf.runtime.pr_monitor_runner.thread_disposition_ack import ACKNOWLEDGES, comment_identity
from awf.runtime.pr_monitor_runner.thread_disposition_readopt import (
    verify_readopted_dispositions,
)
from awf.service.pr_monitor_adoption_seed import seedable_monitor_state
from tests.postgres import postgres_test_engine
from tests.unit.runtime._thread_disposition_fixtures import (
    BOUND_DIGEST,
    acked_thread,
    settled_baseline_state,
)
from tests.unit.runtime.test_pr_monitor_thread_disposition_gate import (
    _AUDIT,
    _SILENT,
    _execute,
    _gate,
    _marker,
    _release_runner,
    _thread,
)
from tests.unit.runtime.test_pr_monitor_thread_disposition_reopen import ReopenForge, audit_of

pytestmark = pytest.mark.unit

_COMMIT = "c" * 40
_ADOPTED = "a" * 40
_CARRIED = "REVIEW_THREAD_DISPOSITION_READOPT_CARRIED"
_RETIRED = "REVIEW_THREAD_DISPOSITION_READOPT_RETIRED"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _predecessor(cls: str = "fixed") -> MonitorState:
    """The predecessor replied ``cls``, resolved, and an audit bound the reply."""
    state = settled_baseline_state()
    verdict = "false_positive" if cls == "false_positive" else "fix_committed"
    state.mark_addressed(_SILENT, verdict)
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id=_SILENT,
            disposition_class=cls,  # type: ignore[arg-type]
            verdict=verdict,
            reason="the caller already rejects None payloads",
            commits=() if cls == "false_positive" else (_COMMIT,),
            reply_comment_id="reply-1",
            resolved=True,
            conversation_digest=BOUND_DIGEST,
        ),
    )
    return state


def _adopt(previous: Mapping[str, str]) -> MonitorState:
    adopted = MonitorState()
    for key, value in seedable_monitor_state(
        previous, head_continuity=False, adopted_head_sha=_ADOPTED
    ).items():
        adopted.mark_addressed(key, value)
    return adopted


def _unchanged(cls: str = "fixed") -> AuditedReviewThread:
    """The thread as the successor audits it: still resolved by AWF, nothing new."""
    return replace(
        _thread(_SILENT, bodies=(_marker(cls, _SILENT),), external=("P1: crash on None",)),
        conversation_digest="f" * 64,
        conversation_digest_before_reply=BOUND_DIGEST,
    )


async def _verify(runner: Any, workspace_id: str, state: MonitorState) -> None:
    await verify_readopted_dispositions(
        runner,
        workspace_id=workspace_id,
        pr_number=42,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        monitor_log=None,
    )


def _probes(cmd: FakeCommandRunner) -> list[list[str]]:
    return [call.args for call in cmd.calls if "merge-base" in call.args]


async def _readopt_events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[dict[str, Any]]:
    async with factory() as session:
        rows = await session.execute(
            select(WorkspaceEvent)
            .where(WorkspaceEvent.workspace_id == workspace_id, WorkspaceEvent.event_type == _AUDIT)
            .order_by(WorkspaceEvent.event_order)
        )
        return [
            dict(row.payload)
            for row in rows.scalars().all()
            if row.payload.get("action") == "review_thread_disposition_readopt_check"
        ]


def _readopt_logs(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in captured if r["event"] == "monitor.review_thread_disposition_readopt_check"]


@pytest.mark.parametrize("cls", ["fixed", "fixed_branch_evidence"])
async def test_an_ancestor_commit_carries_the_disposition_and_certifies_without_reopen(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    cls: str,
) -> None:
    adopted = _adopt(_predecessor(cls).threads_addressed_ids)
    assert adopted.threads_addressed_ids[_disposition_readopt_pending_key(_SILENT)] == _ADOPTED
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(_unchanged(cls)))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    with structlog.testing.capture_logs() as captured:
        await _verify(runner, workspace_id, adopted)

    [probe] = _probes(gh._runner)
    assert probe[-4:] == ["merge-base", "--is-ancestor", _COMMIT, _ADOPTED]
    assert str(tmp_path / "worktrees" / workspace_id) in probe
    assert _disposition_readopt_pending_key(_SILENT) not in adopted.threads_addressed_ids
    logs = _readopt_logs(captured)
    assert [(r["reason_code"], r["thread_ids"]) for r in logs] == [(_CARRIED, [_SILENT])]
    events = await _readopt_events(factory, workspace_id)
    assert [(e["outcome"], e["reason_code"]) for e in events] == [("carried", _CARRIED)]

    gate = await _gate(runner, workspace_id, adopted)
    await _execute(runner, workspace_id, NotifyHuman(), adopted)

    assert gate.count_rows is not None
    expected_row = "fixed" if cls == "fixed" else "fixed (branch evidence)"
    assert dict(gate.count_rows)[expected_row] == 1
    # Certified on the carried marker: no reopen, no blocker comment, no new reply.
    assert gh.unresolved == [] and gh.thread_replies == []
    assert not any("REVIEW_THREAD_DISPOSITION_UNVERIFIED" in c for c in gh.comments)


class _RaisingRunner(FakeCommandRunner):
    async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
        if "merge-base" in args:
            await super().run(args, **kwargs)
            raise OSError("git is missing")
        return await super().run(args, **kwargs)


def _responding(*, returncode: int, reason_code: str | None = None) -> FakeCommandRunner:
    cmd = FakeCommandRunner()
    cmd.respond_when(
        lambda args: "merge-base" in args, returncode=returncode, reason_code=reason_code
    )
    return cmd


@pytest.mark.parametrize(
    ("make_runner", "detail"),
    [
        pytest.param(lambda: _responding(returncode=1), "not_ancestor", id="not-an-ancestor"),
        pytest.param(lambda: _responding(returncode=128), "probe_failed", id="unknown-commit"),
        pytest.param(
            lambda: _responding(returncode=124, reason_code="COMMAND_TIMEOUT"),
            "probe_failed",
            id="timeout",
        ),
        pytest.param(_RaisingRunner, "probe_failed", id="os-error"),
    ],
)
async def test_a_failed_ancestry_check_retires_the_record_as_today_then_falls_back(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    make_runner: Any,
    detail: str,
) -> None:
    adopted = _adopt(_predecessor().threads_addressed_ids)
    gh = ReopenForge(make_runner(), audit=audit_of(_unchanged()))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    with structlog.testing.capture_logs() as captured:
        await _verify(runner, workspace_id, adopted)

    disposition = read_item_disposition(adopted, _SILENT)
    assert disposition is not None and disposition.reply_comment_id is None
    assert _disposition_retired_key(_SILENT, BOUND_DIGEST) in adopted.threads_addressed_ids
    assert _disposition_readopt_pending_key(_SILENT) not in adopted.threads_addressed_ids
    [log] = _readopt_logs(captured)
    assert (log["reason_code"], log["thread_ids"], log["details"]) == (
        _RETIRED,
        [_SILENT],
        {_SILENT: detail},
    )
    events = await _readopt_events(factory, workspace_id)
    assert [(e["outcome"], e["reason_code"]) for e in events] == [("retired", _RETIRED)]

    await _execute(runner, workspace_id, NotifyHuman(), adopted)

    assert gh.unresolved == [_SILENT]


@pytest.mark.parametrize(
    ("commits", "detail"),
    [
        pytest.param(None, "record_missing", id="record-missing"),
        pytest.param(("--output=/tmp/x",), "probe_failed", id="not-a-commit-id"),
        pytest.param((), "record_not_live", id="retired-since"),
    ],
)
async def test_a_check_with_nothing_to_verify_fails_closed_without_probing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    commits: tuple[str, ...] | None,
    detail: str,
) -> None:
    adopted = _adopt(_predecessor().threads_addressed_ids)
    if commits is None:
        del adopted.threads_addressed_ids[f"__awf_item_disposition__:{_SILENT}"]
    else:
        live = read_item_disposition(adopted, _SILENT)
        assert live is not None
        # ``()``: a poll listing the thread open already retired the reply.
        changed = (
            replace(live, commits=commits) if commits else replace(live, reply_comment_id=None)
        )
        record_item_disposition(adopted, changed)
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(_unchanged()))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    with structlog.testing.capture_logs() as captured:
        await _verify(runner, workspace_id, adopted)

    assert _probes(gh._runner) == []
    assert _disposition_readopt_pending_key(_SILENT) not in adopted.threads_addressed_ids
    [log] = _readopt_logs(captured)
    assert log["details"] == {_SILENT: detail}
    gate = await _gate(runner, workspace_id, adopted)
    assert gate.unverified == (_SILENT,)


async def test_a_changed_conversation_after_the_carry_retires_it_and_falls_back(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    adopted = _adopt(_predecessor().threads_addressed_ids)
    moved_on = replace(_unchanged(), conversation_digest_before_reply="9" * 64)
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(moved_on))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    await _verify(runner, workspace_id, adopted)

    await _execute(runner, workspace_id, NotifyHuman(), adopted)

    disposition = read_item_disposition(adopted, _SILENT)
    assert disposition is not None and disposition.reply_comment_id is None
    assert gh.unresolved == [_SILENT]


async def test_a_resolve_by_another_login_after_the_carry_retires_it(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    adopted = _adopt(_predecessor().threads_addressed_ids)
    gh = ReopenForge(
        FakeCommandRunner(), audit=audit_of(replace(_unchanged(), resolved_by="octocat"))
    )
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    await _verify(runner, workspace_id, adopted)

    gate = await _gate(runner, workspace_id, adopted)

    disposition = read_item_disposition(adopted, _SILENT)
    assert disposition is not None and disposition.reply_comment_id is None
    assert gate.count_rows is not None and dict(gate.count_rows)["fixed"] == 0


async def test_false_positive_on_a_moved_head_is_retired_as_today_then_reopened(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    previous = _predecessor("false_positive").threads_addressed_ids

    adopted = _adopt(previous)

    assert seedable_monitor_state(
        previous, head_continuity=False, adopted_head_sha=_ADOPTED
    ) == seedable_monitor_state(previous, head_continuity=False)
    assert _disposition_readopt_pending_key(_SILENT) not in adopted.threads_addressed_ids
    assert read_item_disposition(adopted, _SILENT) is None
    assert _disposition_retired_key(_SILENT, BOUND_DIGEST) in adopted.threads_addressed_ids
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(_unchanged("false_positive")))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    await _verify(runner, workspace_id, adopted)

    await _execute(runner, workspace_id, NotifyHuman(), adopted)

    assert _probes(gh._runner) == []
    assert gh.unresolved == [_SILENT]


async def test_the_gate_fails_closed_while_the_ancestry_check_is_pending(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    adopted = _adopt(_predecessor().threads_addressed_ids)
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(_unchanged()))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    gate = await _gate(runner, workspace_id, adopted)

    assert gate.unverified == (_SILENT,) and gate.count_rows is None


async def test_without_pending_checks_the_step_runs_no_git(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(_unchanged()))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    await _verify(runner, workspace_id, _predecessor())

    assert gh._runner.calls == []
    assert await _readopt_events(factory, workspace_id) == []


async def test_a_carried_fix_with_an_acknowledged_bot_reply_certifies_on_the_successor(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # D1 and D2 together: the predecessor fixed the thread, the bot acknowledged
    # it, and the head moved before re-adoption. Neither is redone.
    thread = acked_thread(_SILENT, _marker("fixed", _SILENT))
    predecessor = _predecessor()
    predecessor.mark_addressed(
        _disposition_ack_key(_SILENT, comment_identity(thread.trailing_comments[0])),
        ACKNOWLEDGES,
    )
    adopted = _adopt(predecessor.threads_addressed_ids)
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(thread))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)

    await _verify(runner, workspace_id, adopted)
    gate = await _gate(runner, workspace_id, adopted)

    assert gate.count_rows is not None and dict(gate.count_rows)["fixed"] == 1
    assert gh.unresolved == [] and runner._deps.adapter.calls == []
