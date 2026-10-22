"""Rollout baseline for the review-thread disposition gate (#1018).

Threads AWF resolved before it replied before every resolve carry no marker.
On a workspace's first gate run those unmarked AWF resolutions are recorded as
a legacy baseline — an audit event with their ids — and do not block; the cert
counts them separately. Every resolution after that first run still needs its
marker.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.forge_thread_audit import AuditedReviewThread, ReviewThreadAudit
from awf.common.github_client import RepoRef
from awf.db.models import WorkspaceEvent
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import (
    _REVIEW_THREAD_DISPOSITION_BASELINE_STATE_KEY,
    _disposition_unverified_key,
    _with_settled_disposition_baseline,
)
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    NotifyHuman,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.thread_disposition import (
    ThreadDisposition,
    record_item_disposition,
)
from awf.runtime.pr_monitor_runner.thread_disposition_gate import (
    review_thread_disposition_gate,
)
from awf.service.pr_monitor_adoption_seed import seedable_monitor_state
from tests.postgres import postgres_test_engine
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

pytestmark = pytest.mark.unit

_REPO = RepoRef(owner="acme", name="widgets")
_VIEWER = "awf-operator"
_LEGACY = "PRRT_test_legacy"
_LATER = "PRRT_test_later"
_AUDIT = "workspace.audit.comment_resolution"
_LEGACY_LABEL = "legacy baseline (resolved before AWF dispositions)"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _marker(cls: str, tid: str) -> str:
    return f"<!-- AWF:thread-disposition:v1 class={cls} thread={tid} -->"


def _thread(tid: str, *, bodies: tuple[str, ...] = (), resolved_by: str = _VIEWER) -> Any:
    return AuditedReviewThread(
        thread_id=tid,
        is_resolved=True,
        is_outdated=False,
        resolved_by=resolved_by,
        viewer_comment_bodies=bodies,
    )


class _AuditForge(DefaultMergeMethodGitHubClient):
    def __init__(self, runner: FakeCommandRunner, threads: tuple[Any, ...]) -> None:
        super().__init__(runner)
        self.threads = threads
        self.comments: list[str] = []

    async def fetch_review_thread_audit(
        self, *, repo: RepoRef, pr_number: int
    ) -> ReviewThreadAudit:
        del repo, pr_number
        return ReviewThreadAudit(supported=True, viewer_login=_VIEWER, threads=self.threads)

    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        del repo, pr_number
        self.comments.append(body)


async def _runner(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, gh: Any
) -> tuple[str, Any]:
    workspace_id = await seed_monitoring_workspace(factory, auto_merge=False)
    runner = make_runner(
        factory=factory,
        cmd=gh._runner,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
        gh=gh,
    )
    return workspace_id, runner


async def _gate(runner: Any, workspace_id: str, state: MonitorState) -> Any:
    return await review_thread_disposition_gate(
        runner,
        workspace_id=workspace_id,
        repo=_REPO,
        pr_number=42,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        monitor_log=None,
    )


async def _baseline_events(
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
            if row.payload.get("outcome") == "legacy_baseline"
        ]


async def test_first_gate_run_baselines_unmarked_resolutions_and_later_ones_still_block(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _AuditForge(
        FakeCommandRunner(),
        threads=(
            _thread(_LEGACY),
            _thread("PRRT_test_fixed", bodies=(_marker("fixed", "PRRT_test_fixed"),)),
            _thread("PRRT_test_bot", resolved_by="review-bot"),
        ),
    )
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()

    first = await _gate(runner, workspace_id, state)

    assert first.blocker is None
    assert first.count_rows is not None
    rows = dict(first.count_rows)
    assert rows[_LEGACY_LABEL] == 1 and rows["fixed"] == 1 and rows["resolved by others"] == 1
    assert json.loads(
        state.threads_addressed_ids[_REVIEW_THREAD_DISPOSITION_BASELINE_STATE_KEY]
    ) == [_LEGACY]
    assert _disposition_unverified_key(_LEGACY) not in state.threads_addressed_ids
    events = await _baseline_events(factory, workspace_id)
    assert len(events) == 1
    assert events[0]["reason_code"] == "REVIEW_THREAD_DISPOSITION_LEGACY_BASELINE"
    assert events[0]["evidence"]["thread_ids"] == [_LEGACY]

    # A resolution after the first run still needs its marker; the baseline holds.
    gh.threads = (*gh.threads, _thread(_LATER))
    later = await _gate(runner, workspace_id, state)

    assert later.blocker is not None
    assert later.blocker.startswith("REVIEW_THREAD_DISPOSITION_UNVERIFIED:")
    assert _LATER in later.blocker and _LEGACY not in later.blocker
    assert len(await _baseline_events(factory, workspace_id)) == 1


async def test_first_gate_run_without_unmarked_resolutions_still_closes_the_baseline(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _AuditForge(FakeCommandRunner(), threads=())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()

    first = await _gate(runner, workspace_id, state)

    assert first.blocker is None
    assert first.count_rows is not None
    assert _LEGACY_LABEL not in dict(first.count_rows)
    assert await _baseline_events(factory, workspace_id) == []

    gh.threads = (_thread(_LATER),)
    blocked = await _gate(runner, workspace_id, state)
    assert blocked.blocker is not None and _LATER in blocked.blocker


async def test_cert_shows_the_legacy_baseline_count_separately(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _AuditForge(
        FakeCommandRunner(),
        threads=(
            _thread(_LEGACY),
            _thread("PRRT_test_fp", bodies=(_marker("false_positive", "PRRT_test_fp"),)),
        ),
    )
    workspace_id, runner = await _runner(factory, tmp_path, gh)

    await runner._execute(
        action=NotifyHuman(),
        workspace_id=workspace_id,
        repo_url="git@github.com:acme/widgets.git",
        repo=_REPO,
        pr_number=42,
        status=PRStatus(
            number=42,
            head_sha="abc1234567890def",
            mergeable=MergeableState.MERGEABLE,
            check_state=CheckState.SUCCESS,
            unresolved_inline_threads=(),
            unresolved_review_comments=(),
            base_behind_count=0,
            merge_state_status=MergeStateStatus.CLEAN,
        ),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=Path("/tmp/compose.yml"),
        monitor_log=None,
    )

    assert len(gh.comments) == 1
    assert "is ready to merge" in gh.comments[0]
    assert (
        "AWF dispositions: fixed 0, fixed (branch evidence) 0, false positive 1, deferred 0, "
        f"operator acknowledged 0; resolved by others 0; {_LEGACY_LABEL} 1."
    ) in gh.comments[0]


@pytest.mark.parametrize("record", ["{not json", '{"ids": []}', "[1, 2]"])
async def test_malformed_baseline_exempts_nothing_and_is_never_re_taken(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    record: str,
) -> None:
    gh = _AuditForge(FakeCommandRunner(), threads=(_thread(_LEGACY),))
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()
    state.mark_addressed(_REVIEW_THREAD_DISPOSITION_BASELINE_STATE_KEY, record)

    result = await _gate(runner, workspace_id, state)

    # The first run already happened: an unreadable baseline fails closed.
    assert result.blocker is not None and _LEGACY in result.blocker
    assert state.threads_addressed_ids[_REVIEW_THREAD_DISPOSITION_BASELINE_STATE_KEY] == record
    assert await _baseline_events(factory, workspace_id) == []


async def test_re_adopted_successor_keeps_the_predecessor_baseline(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _AuditForge(FakeCommandRunner(), threads=(_thread(_LEGACY),))
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    predecessor = MonitorState()
    await _gate(runner, workspace_id, predecessor)
    gh.threads = (*gh.threads, _thread(_LATER))
    flagged = await _gate(runner, workspace_id, predecessor)
    assert flagged.blocker is not None and _LATER in flagged.blocker

    successor_id, successor_runner = await _runner(factory, tmp_path, gh)
    successor = MonitorState()
    for key, value in seedable_monitor_state(
        predecessor.threads_addressed_ids, head_continuity=False
    ).items():
        successor.mark_addressed(key, value)

    result = await _gate(successor_runner, successor_id, successor)

    # The successor does not re-take the first run: the predecessor's flagged
    # thread still blocks, and only the predecessor's legacy thread is exempt.
    assert result.blocker is not None
    assert _LATER in result.blocker and _LEGACY not in result.blocker
    assert await _baseline_events(factory, successor_id) == []


async def test_pr_awf_opened_starts_settled_and_blocks_a_silent_first_audit_resolution(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A PR AWF opened itself has no pre-rollout history: its handoff settles the
    # baseline empty, so the very first audit grandfathers nothing.
    gh = _AuditForge(FakeCommandRunner(), threads=(_thread(_LATER),))
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()
    for key, value in _with_settled_disposition_baseline(None).items():
        state.mark_addressed(key, value)

    result = await _gate(runner, workspace_id, state)

    assert result.blocker is not None and _LATER in result.blocker
    assert state.threads_addressed_ids[_REVIEW_THREAD_DISPOSITION_BASELINE_STATE_KEY] == "[]"
    assert await _baseline_events(factory, workspace_id) == []


def test_settled_baseline_never_replaces_an_existing_record() -> None:
    record = json.dumps([_LEGACY])
    existing = {_REVIEW_THREAD_DISPOSITION_BASELINE_STATE_KEY: record, "PRRT_test_x": "fixed"}

    assert _with_settled_disposition_baseline(existing) == existing


async def test_first_gate_run_never_grandfathers_a_thread_this_monitor_dispositioned(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A reused / adopted PR keeps the one-time baseline, but a thread this
    # monitor already recorded a disposition for was handled after the rollout:
    # its unmarked resolution is not legacy and blocks on the very first audit.
    gh = _AuditForge(FakeCommandRunner(), threads=(_thread(_LEGACY), _thread(_LATER)))
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()
    record_item_disposition(
        state, ThreadDisposition(item_id=_LATER, disposition_class="fixed", verdict="fixed")
    )

    result = await _gate(runner, workspace_id, state)

    assert result.blocker is not None
    assert _LATER in result.blocker and _LEGACY not in result.blocker
    assert json.loads(
        state.threads_addressed_ids[_REVIEW_THREAD_DISPOSITION_BASELINE_STATE_KEY]
    ) == [_LEGACY]
    events = await _baseline_events(factory, workspace_id)
    assert [event["evidence"]["thread_ids"] for event in events] == [[_LEGACY]]
