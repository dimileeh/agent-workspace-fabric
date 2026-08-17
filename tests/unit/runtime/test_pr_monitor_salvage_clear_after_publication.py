"""Clear ``__salvaged_fix_*`` keys after successful publication (PRRT_kwDOSJAM6s6Zzwl4)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import (
    _salvaged_fix_body_hash_state_key,
    _salvaged_fix_head_state_key,
    _salvaged_fix_start_state_key,
)
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _clear_addressed_state_by_id,
    _clear_salvaged_fix_state,
)
from tests.postgres import postgres_test_engine
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _plant_salvage(state: MonitorState, item_id: str) -> None:
    tip = "b" * 40
    start = "a" * 40
    body_hash = "feedback_body_hash_publication_clear"
    state.mark_addressed(_salvaged_fix_head_state_key(item_id), tip)
    state.mark_addressed(_salvaged_fix_body_hash_state_key(item_id), body_hash)
    state.mark_addressed(_salvaged_fix_start_state_key(item_id), start)


@pytest.mark.unit
def test_clear_salvaged_fix_state_drops_all_three_keys() -> None:
    """Publication cleanup must remove head/start/body-hash salvage sidecars."""
    item_id = "PRRT_kwDOSJAM6s6Zzwl4"
    state = MonitorState()
    _plant_salvage(state, item_id)
    state.mark_addressed(item_id, "fix_committed")

    _clear_salvaged_fix_state(state, item_id)

    assert _salvaged_fix_head_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_start_state_key(item_id) not in state.threads_addressed_ids
    # Verdict marker is independent — publication clear is tip-keys only.
    assert state.threads_addressed_ids.get(item_id) == "fix_committed"


@pytest.mark.unit
def test_clear_addressed_state_still_preserves_salvage_for_requeue() -> None:
    """Push/resolve requeue must keep tip evidence (PRRT_kwDOSJAM6s6ZnvBN)."""
    item_id = "PRRT_kwDOSJAM6s6ZnvBN"
    state = MonitorState()
    _plant_salvage(state, item_id)
    state.mark_addressed(item_id, "fix_committed")

    _clear_addressed_state_by_id(state, item_id)

    assert item_id not in state.threads_addressed_ids
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == "b" * 40


class _SuccessfulResolveClient(DefaultMergeMethodGitHubClient):
    def __init__(self, runner: FakeCommandRunner, *, settle_status: PRStatus) -> None:
        super().__init__(runner)
        self._settle_status = settle_status
        self.resolved: list[str] = []

    async def fetch_pr_status(
        self, *, repo: RepoRef, pr_number: int, base_behind_count: int, retry: bool = True
    ) -> PRStatus:
        del repo, pr_number, base_behind_count, retry
        return self._settle_status

    async def resolve_thread(self, *, thread_id: str) -> None:
        self.resolved.append(thread_id)


@pytest.mark.unit
async def test_successful_thread_resolve_clears_salvaged_fix_keys(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """After push + resolve land, retained tip keys must not linger permanently."""
    workspace_id = await seed_monitoring_workspace(factory)
    item_id = "PRRT_kwDOSJAM6s6Zzwl4"
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # push
    cmd.queue_result(returncode=0, stdout="newsha\n")  # rev-parse HEAD
    adapter = FakeAdapter()
    thread = ReviewThread(
        thread_id=item_id,
        path="src/awf/runtime/pr_monitor_runner/comment_verdict.py",
        line=978,
        body_excerpt="Clear retained salvage keys after successful publication",
        author="chatgpt-codex-connector",
    )
    settle = PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )
    gh = _SuccessfulResolveClient(cmd, settle_status=settle)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    _plant_salvage(state, item_id)
    persisted: list[str] = []

    async def _address_thread(**_kwargs: object) -> str:
        state.mark_addressed(item_id, "fix_committed")
        return "fix_committed"

    async def _persist_salvage(
        _workspace_id: str, _state: MonitorState, *, salvage_item_id: str
    ) -> None:
        persisted.append(salvage_item_id)

    runner._address_thread = _address_thread  # type: ignore[method-assign]
    runner._persist_failed_run_salvage_durably = _persist_salvage  # type: ignore[method-assign]

    await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="agent-workspace-fabric"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert gh.resolved == [item_id]
    assert state.threads_addressed_ids.get(item_id) == "fix_committed"
    assert _salvaged_fix_head_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_start_state_key(item_id) not in state.threads_addressed_ids
    assert persisted == [item_id]


@pytest.mark.unit
async def test_successful_push_clears_salvage_for_review_comments(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Review-level fixes clear tip keys only after resolution is recorded."""
    workspace_id = await seed_monitoring_workspace(factory)
    item_id = "RC_publication_clear_review"
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # push
    cmd.queue_result(returncode=0, stdout="newsha\n")  # rev-parse HEAD
    adapter = FakeAdapter()
    comment = ReviewComment(
        comment_id=item_id,
        author="reviewer",
        body_excerpt="please fix this outside-diff note",
    )
    settle = PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )
    gh = _SuccessfulResolveClient(cmd, settle_status=settle)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    _plant_salvage(state, item_id)
    recorded: list[str] = []

    async def _address_review_comment_result(**_kwargs: object) -> object:
        from awf.runtime.pr_monitor_runner.comments import VerdictResult

        state.mark_addressed(item_id, "fix_committed")
        return VerdictResult(verdict="fix_committed")

    async def _record_pr_feedback_resolution(**kwargs: object) -> None:
        comment_obj = kwargs["comment"]
        assert isinstance(comment_obj, ReviewComment)
        # Salvage must still be present until resolution succeeds
        # (PRRT_kwDOSJAM6s6Z0Hbz).
        assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == "b" * 40
        recorded.append(comment_obj.comment_id)

    runner._address_review_comment_result = _address_review_comment_result  # type: ignore[method-assign]
    runner._record_pr_feedback_resolution = _record_pr_feedback_resolution  # type: ignore[method-assign]

    await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="agent-workspace-fabric"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(),
        initial_reviews=(comment,),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert gh.resolved == []
    assert recorded == [item_id]
    assert state.threads_addressed_ids.get(item_id) == "fix_committed"
    assert _salvaged_fix_head_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_body_hash_state_key(item_id) not in state.threads_addressed_ids
    assert _salvaged_fix_start_state_key(item_id) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_review_fixed_keeps_salvage_when_resolution_record_raises(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Do not clear review FIXED salvage before resolution is durably recorded.

    Clearing tip keys first, then failing ``_record_pr_feedback_resolution``,
    strands a valid published fix as ``fixed_without_head_advance`` on restart
    (PRRT_kwDOSJAM6s6Z0Hbz).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    item_id = "RC_resolution_record_cancel_keep_salvage"
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # push
    cmd.queue_result(returncode=0, stdout="newsha\n")  # rev-parse HEAD
    adapter = FakeAdapter()
    comment = ReviewComment(
        comment_id=item_id,
        author="reviewer",
        body_excerpt="keep salvage until resolution lands",
    )
    settle = PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )
    gh = _SuccessfulResolveClient(cmd, settle_status=settle)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    _plant_salvage(state, item_id)
    persisted_clears: list[str] = []

    async def _address_review_comment_result(**_kwargs: object) -> object:
        from awf.runtime.pr_monitor_runner.comments import VerdictResult

        state.mark_addressed(item_id, "fix_committed")
        return VerdictResult(verdict="fix_committed")

    async def _record_pr_feedback_resolution(**_kwargs: object) -> None:
        raise TimeoutError("resolution provenance write interrupted")

    async def _persist_salvage(
        _workspace_id: str, _state: MonitorState, *, salvage_item_id: str
    ) -> None:
        persisted_clears.append(salvage_item_id)

    runner._address_review_comment_result = _address_review_comment_result  # type: ignore[method-assign]
    runner._record_pr_feedback_resolution = _record_pr_feedback_resolution  # type: ignore[method-assign]
    runner._persist_failed_run_salvage_durably = _persist_salvage  # type: ignore[method-assign]

    with pytest.raises(TimeoutError, match="resolution provenance write interrupted"):
        await runner._run_fix_cycle(
            workspace_id=workspace_id,
            repo=RepoRef(owner="dimileeh", name="agent-workspace-fabric"),
            pr_number=42,
            pr_head_sha="abc1234567890def",
            initial_threads=(),
            initial_reviews=(comment,),
            state=state,
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

    assert item_id not in persisted_clears
    assert state.threads_addressed_ids.get(_salvaged_fix_head_state_key(item_id)) == "b" * 40
    assert (
        state.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(item_id))
        == "feedback_body_hash_publication_clear"
    )
    assert state.threads_addressed_ids.get(_salvaged_fix_start_state_key(item_id)) == "a" * 40
