"""Regressions: settle-pass evidence anchor uses remote PR head from batch status."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunResult
from awf.common.commands import AsyncioSubprocessRunner, FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.feedback_policy import (
    review_thread_body_hash,
    review_thread_body_state_key,
)
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewThread,
    _mark_review_thread_addressed,
)
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_FIXED_WITHOUT_EVIDENCE,
    AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _clean_status(
    *,
    head_sha: str = "start",
    threads: tuple[ReviewThread, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=threads,
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


@pytest.mark.unit
async def test_fix_cycle_later_settle_pass_uses_remote_status_head_as_cycle_start_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass-2 evidence anchor must be settle status.head_sha, not local HEAD.

    Non-hosted agents commit locally before push, so settle re-poll thread
    path/line coords stay relative to the remote PR head. Anchoring at the
    locally advanced HEAD skips remote→local mapping and can misclassify FIXED.
    """
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # pass1 item cat-file
    cmd.queue_result(returncode=0)  # pass2 item cat-file
    worktrees_root = tmp_path / "worktrees"
    worktree_path = worktrees_root / "ws_pass_anchor"
    worktree_path.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
    )
    remote_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    after_pass1_local = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    # pass1 item, pass2 item (no separate pass-start local probe)
    current_heads = iter((remote_head, after_pass1_local))
    cycle_start_heads: list[str | None] = []
    address_thread_ids: list[str] = []
    settle_calls = 0

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (remote_head, None)

    async def _no_dirty(**_kwargs: object) -> None:
        return None

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return next(current_heads)

    async def _address(**kwargs: object) -> str:
        thread = cast(ReviewThread, kwargs["thread"])
        address_thread_ids.append(thread.thread_id)
        cycle_start_heads.append(cast(str | None, kwargs.get("cycle_start_head")))
        return "false_positive"

    async def _settle(**_kwargs: object) -> PRStatus:
        nonlocal settle_calls
        settle_calls += 1
        if settle_calls == 1:
            return _clean_status(
                head_sha=remote_head,
                threads=(
                    ReviewThread(
                        thread_id="T_later",
                        path="src/foo.py",
                        line=20,
                        body_excerpt="new feedback after earlier local commit",
                        author="reviewer",
                    ),
                ),
            )
        return _clean_status(head_sha=remote_head)

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _validated(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    async def _resolve_thread(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_address_thread", _address)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _settle)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated)
    monkeypatch.setattr(runner._deps.gh, "resolve_thread", _resolve_thread)

    result = await runner._run_fix_cycle(
        workspace_id="ws_pass_anchor",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=remote_head,
        initial_threads=(
            ReviewThread(
                thread_id="T_first",
                path="src/foo.py",
                line=3,
                body_excerpt="please fix first",
                author="reviewer",
            ),
        ),
        initial_reviews=(),
        state=MonitorState(),
        remote_branch="awf/ws_pass_anchor",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert address_thread_ids == ["T_first", "T_later"]
    assert cycle_start_heads[0] == remote_head
    # Pass 2 must keep the remote PR head from the settle status, not local tip.
    assert cycle_start_heads[1] == remote_head
    assert cycle_start_heads[1] != after_pass1_local


@pytest.mark.unit
async def test_fix_cycle_settle_preserves_addressed_hash_over_equal_rank_stale_duplicate(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settle must canonicalize with state so a stale equal-rank ghost is ignored.

    Without the state map, ``status.canonical_unresolved_inline_threads`` keeps
    the later distinct body among equal-ranked same-ID copies. During a fix
    cycle for another item that can re-queue the handled thread, overwrite its
    body hash, and later ignore a genuinely newer copy (PRRT_kwDOSJAM6s6dfSrA).
    """
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # pass1 item cat-file
    worktrees_root = tmp_path / "worktrees"
    worktree_path = worktrees_root / "ws_settle_dedupe"
    worktree_path.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
    )
    remote_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    handled = ReviewThread(
        thread_id="T_handled",
        path="src/foo.py",
        line=10,
        body_excerpt="already addressed body",
        author="reviewer",
    )
    stale_ghost = ReviewThread(
        thread_id="T_handled",
        path="src/foo.py",
        line=10,
        body_excerpt="stale equal-rank ghost body",
        author="reviewer",
    )
    other = ReviewThread(
        thread_id="T_other",
        path="src/bar.py",
        line=3,
        body_excerpt="please fix other",
        author="reviewer",
    )
    state = MonitorState()
    _mark_review_thread_addressed(state, handled, "fix_committed")
    recorded_hash = state.threads_addressed_ids[review_thread_body_state_key("T_handled")]
    assert recorded_hash == review_thread_body_hash(handled)
    address_thread_ids: list[str] = []

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (remote_head, None)

    async def _no_dirty(**_kwargs: object) -> None:
        return None

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return remote_head

    async def _address(**kwargs: object) -> str:
        thread = cast(ReviewThread, kwargs["thread"])
        address_thread_ids.append(thread.thread_id)
        return "false_positive"

    async def _settle(**_kwargs: object) -> PRStatus:
        # Matching body first, then equal-rank stale ghost last — state-blind
        # combine would keep the ghost and re-queue T_handled.
        return _clean_status(head_sha=remote_head, threads=(handled, stale_ghost))

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _validated(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    async def _resolve_thread(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_address_thread", _address)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _settle)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated)
    monkeypatch.setattr(runner._deps.gh, "resolve_thread", _resolve_thread)

    result = await runner._run_fix_cycle(
        workspace_id="ws_settle_dedupe",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=remote_head,
        initial_threads=(other,),
        initial_reviews=(),
        state=state,
        remote_branch="awf/ws_settle_dedupe",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert address_thread_ids == ["T_other"]
    assert state.threads_addressed_ids["T_handled"] == "fix_committed"
    assert state.threads_addressed_ids[review_thread_body_state_key("T_handled")] == recorded_hash


@pytest.mark.unit
async def test_fix_cycle_multi_item_pass_shares_stable_remote_batch_anchor(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items in one settle pass share the settle status head while item heads advance."""
    cmd = FakeCommandRunner()
    worktrees_root = tmp_path / "worktrees"
    worktree_path = worktrees_root / "ws_stable_pass"
    worktree_path.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
    )
    remote_head = "operation-open-remote-head"
    after_first = "after-first-item-head"
    live_head = {"sha": remote_head}
    cycle_start_heads: list[str | None] = []
    operation_start_heads: list[str | None] = []
    address_thread_ids: list[str] = []
    settle_calls = 0

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (remote_head, None)

    async def _no_dirty(**_kwargs: object) -> None:
        return None

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return live_head["sha"]

    async def _address(**kwargs: object) -> str:
        thread = cast(ReviewThread, kwargs["thread"])
        address_thread_ids.append(thread.thread_id)
        cycle_start_heads.append(cast(str | None, kwargs.get("cycle_start_head")))
        operation_start_heads.append(cast(str | None, kwargs.get("operation_start_head")))
        if thread.thread_id == "T_pass1":
            live_head["sha"] = "local-after-pass1"
        elif thread.thread_id == "T_pass2_a":
            live_head["sha"] = after_first
        return "false_positive" if thread.thread_id == "T_pass1" else "needs_human"

    async def _settle(**_kwargs: object) -> PRStatus:
        nonlocal settle_calls
        settle_calls += 1
        if settle_calls == 1:
            return _clean_status(
                head_sha=remote_head,
                threads=(
                    ReviewThread(
                        thread_id="T_pass2_a",
                        path="src/a.py",
                        line=1,
                        body_excerpt="settle first",
                        author="reviewer",
                    ),
                    ReviewThread(
                        thread_id="T_pass2_b",
                        path="src/b.py",
                        line=2,
                        body_excerpt="settle second",
                        author="reviewer",
                    ),
                ),
            )
        return _clean_status(head_sha=remote_head)

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _validated(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    async def _resolve_thread(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_address_thread", _address)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _settle)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated)
    monkeypatch.setattr(runner._deps.gh, "resolve_thread", _resolve_thread)

    await runner._run_fix_cycle(
        workspace_id="ws_stable_pass",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=remote_head,
        initial_threads=(
            ReviewThread(
                thread_id="T_pass1",
                path="src/z.py",
                line=1,
                body_excerpt="first pass",
                author="reviewer",
            ),
        ),
        initial_reviews=(),
        state=MonitorState(),
        remote_branch="awf/ws_stable_pass",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert address_thread_ids == ["T_pass1", "T_pass2_a", "T_pass2_b"]
    assert cycle_start_heads[0] == remote_head
    # Pass-2 items share the unchanged remote batch head (not local tip).
    assert cycle_start_heads[1:] == [remote_head, remote_head]
    assert "local-after-pass1" not in cycle_start_heads
    assert after_first not in cycle_start_heads
    assert operation_start_heads[1:] == ["local-after-pass1", after_first]


@pytest.mark.unit
async def test_fix_cycle_breaks_settle_when_remote_head_advances_unreconciled(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not adopt an advanced remote tip as evidence anchor mid-settle.

    Another actor can advance the PR while local repairs are still unpublished.
    Mapping new thread coords from that unfetched/divergent SHA onto local HEAD
    rejects real FIXED verdicts as AGENT_FIXED_WITHOUT_EVIDENCE (PRRT_kwDOSJAM6s6dIQm6).
    Break settle and push; the outer loop re-enters with abandon/reconcile.
    """
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # pass1 item cat-file
    worktrees_root = tmp_path / "worktrees"
    worktree_path = worktrees_root / "ws_remote_advance"
    worktree_path.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
    )
    remote_open = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    remote_advanced = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    # Extra HEADs only matter if settle wrongly continues under the advanced tip.
    current_heads = iter((remote_open, "should-not-reach-second-pass"))
    cycle_start_heads: list[str | None] = []
    address_thread_ids: list[str] = []
    settle_calls = 0

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (remote_open, None)

    async def _no_dirty(**_kwargs: object) -> None:
        return None

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return next(current_heads)

    async def _address(**kwargs: object) -> str:
        thread = cast(ReviewThread, kwargs["thread"])
        address_thread_ids.append(thread.thread_id)
        cycle_start_heads.append(cast(str | None, kwargs.get("cycle_start_head")))
        return "false_positive"

    async def _settle(**_kwargs: object) -> PRStatus:
        nonlocal settle_calls
        settle_calls += 1
        return _clean_status(
            head_sha=remote_advanced,
            threads=(
                ReviewThread(
                    thread_id="T_after_advance",
                    path="src/foo.py",
                    line=9,
                    body_excerpt="feedback on advanced remote tip",
                    author="reviewer",
                ),
            ),
        )

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _validated(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    async def _resolve_thread(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_address_thread", _address)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _settle)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated)
    monkeypatch.setattr(runner._deps.gh, "resolve_thread", _resolve_thread)

    result = await runner._run_fix_cycle(
        workspace_id="ws_remote_advance",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=remote_open,
        initial_threads=(
            ReviewThread(
                thread_id="T_first",
                path="src/foo.py",
                line=3,
                body_excerpt="please fix first",
                author="reviewer",
            ),
        ),
        initial_reviews=(),
        state=MonitorState(),
        remote_branch="awf/ws_remote_advance",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    # One settle re-poll, then the #910 post-action PR re-check (same stub); settle
    # did NOT continue into a second pass under the advanced tip.
    assert settle_calls == 2
    assert address_thread_ids == ["T_first"]
    assert cycle_start_heads == [remote_open]
    assert remote_advanced not in cycle_start_heads


@pytest.mark.unit
async def test_fix_cycle_local_advance_does_not_replace_remote_batch_anchor(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local unpublished commits must not become the settle-pass evidence anchor."""
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # pass1 item
    cmd.queue_result(returncode=0)  # pass2 item
    worktrees_root = tmp_path / "worktrees"
    worktree_path = worktrees_root / "ws_remote_anchor"
    worktree_path.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
    )
    remote_head = "remote-pr-head-sha"
    local_advanced = "local-unpublished-head"
    current_heads = iter((remote_head, local_advanced))
    cycle_start_heads: list[str | None] = []
    settle_calls = 0

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (remote_head, None)

    async def _no_dirty(**_kwargs: object) -> None:
        return None

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return next(current_heads)

    async def _address(**kwargs: object) -> str:
        cycle_start_heads.append(cast(str | None, kwargs.get("cycle_start_head")))
        return "false_positive"

    async def _settle(**_kwargs: object) -> PRStatus:
        nonlocal settle_calls
        settle_calls += 1
        if settle_calls == 1:
            return _clean_status(
                head_sha=remote_head,
                threads=(
                    ReviewThread(
                        thread_id="T_pass2",
                        path="src/foo.py",
                        line=4,
                        body_excerpt="settle feedback on remote head lines",
                        author="reviewer",
                    ),
                ),
            )
        return _clean_status(head_sha=remote_head)

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _validated(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    async def _resolve_thread(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_address_thread", _address)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _settle)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated)
    monkeypatch.setattr(runner._deps.gh, "resolve_thread", _resolve_thread)

    result = await runner._run_fix_cycle(
        workspace_id="ws_remote_anchor",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=remote_head,
        initial_threads=(
            ReviewThread(
                thread_id="T_pass1",
                path="src/foo.py",
                line=3,
                body_excerpt="first pass",
                author="reviewer",
            ),
        ),
        initial_reviews=(),
        state=MonitorState(),
        remote_branch="awf/ws_remote_anchor",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert cycle_start_heads == [remote_head, remote_head]
    assert local_advanced not in cycle_start_heads


@pytest.mark.unit
async def test_later_pass_anchor_accepts_real_item_line_fix(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Line coords relative to a newer remote head must map and accept a real FIXED.

    When the remote PR head advances (or coords are already for a later commit),
    anchoring evidence at that head accepts a line-scoped fix on the first
    attempt. Anchoring at an older SHA maps that line elsewhere, so the same
    contentful change fails the line-anchored gate on both attempts — path
    membership alone must not produce ``fix_committed`` (issue:5558086911).
    """
    worktree = tmp_path / "worktrees" / "ws_protocol"
    worktree.mkdir(parents=True)
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    (worktree / "src").mkdir()
    target = worktree / "src" / "mod.py"
    target.write_text("line1\nREVIEWED\nline3\n", encoding="utf-8")
    _git(worktree, "add", "src/mod.py")
    _git(worktree, "commit", "-qm", "operation open")
    operation_open = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    # Earlier pass: insert five lines so REVIEWED moves from line 2 → line 7.
    target.write_text(
        "pad1\npad2\npad3\npad4\npad5\nline1\nREVIEWED\nline3\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "src/mod.py")
    _git(worktree, "commit", "-qm", "pass1 insert")
    pass_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    # Item-scoped fix at the settle-thread line (7) relative to pass_head.
    target.write_text(
        "pad1\npad2\npad3\npad4\npad5\nline1\nREVIEWED fixed\nline3\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "src/mod.py")
    _git(worktree, "commit", "-qm", "item line fix")
    fixed_tip = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _ok(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(comment_verdict, "repair_agent_runtime_ownership", _ok)
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: None)

    prompts: list[str] = []

    async def _fixed_agent(**kwargs: object) -> AgentRunResult:
        prompts.append(str(kwargs["prompt"]))
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: updated reviewed line",
            stderr="",
        )

    runner._run_monitor_agent_with_service_recovery = _fixed_agent  # type: ignore[method-assign]

    # Fresh pass-head anchor accepts the real line-scoped fix.
    result = await comment_verdict._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_protocol",
        prompt="fix the reviewed line",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head=pass_head,
        evidence_item_path="src/mod.py",
        evidence_item_line=7,
        evidence_anchor_head=pass_head,
        commit_dirty_changes=False,
    )
    assert result.verdict == "fix_committed"
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == fixed_tip
    assert len(prompts) == 1

    # A stale operation-open anchor maps the line elsewhere, so the line gate
    # rejects both attempts. Path membership alone must not accept FIXED; the
    # commit is preserved and escalated instead of failing the monitor.
    prompts.clear()
    stale = await comment_verdict._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_protocol",
        prompt="fix the reviewed line",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head=pass_head,
        evidence_item_path="src/mod.py",
        evidence_item_line=7,
        evidence_anchor_head=operation_open,
        commit_dirty_changes=False,
    )
    assert stale.verdict == "needs_human"
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == fixed_tip
    assert len(prompts) == 2
    assert "no new item-scoped Git change" in prompts[1]


@pytest.mark.unit
async def test_no_change_fixed_rejected_with_correct_pass_anchor(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIXED with no item-scoped delta remains rejected when pass anchor is correct."""
    worktree = tmp_path / "worktrees" / "ws_protocol"
    worktree.mkdir(parents=True)
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    (worktree / "src").mkdir()
    (worktree / "src" / "mod.py").write_text("unchanged\n", encoding="utf-8")
    _git(worktree, "add", "src/mod.py")
    _git(worktree, "commit", "-qm", "pass start")
    pass_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _ok(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(comment_verdict, "repair_agent_runtime_ownership", _ok)
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: None)

    async def _fixed_agent(**_kwargs: object) -> AgentRunResult:
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: claimed without edits",
            stderr="",
        )

    runner._run_monitor_agent_with_service_recovery = _fixed_agent  # type: ignore[method-assign]

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,
            workspace_id="ws_protocol",
            prompt="fix nothing",
            commit_message="fix: review item",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head=pass_head,
            evidence_item_path="src/mod.py",
            evidence_item_line=1,
            evidence_anchor_head=pass_head,
            commit_dirty_changes=False,
        )

    assert caught.value.reason_code == AGENT_FIXED_WITHOUT_EVIDENCE
