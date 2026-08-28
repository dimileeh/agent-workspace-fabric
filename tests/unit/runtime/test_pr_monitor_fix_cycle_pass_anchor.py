"""Regressions: per-settle-pass evidence anchor HEAD in comment-repair fix cycle."""

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
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewThread,
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


def _clean_status(*, threads: tuple[ReviewThread, ...] = ()) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha="start",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=threads,
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


@pytest.mark.unit
async def test_fix_cycle_later_settle_pass_uses_fresh_pass_head_as_cycle_start_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass-2 evidence anchor must be post-pass-1 HEAD, not the operation-open SHA."""
    cmd = FakeCommandRunner()
    # Pass-start + per-item cat-file probes (empty queue returns ok; queue explicit ones).
    cmd.queue_result(returncode=0)  # pass1 start cat-file
    cmd.queue_result(returncode=0)  # pass1 item cat-file
    cmd.queue_result(returncode=0)  # pass2 start cat-file
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
    operation_open = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    after_pass1 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    # pass1 start, pass1 item, pass2 start, pass2 item
    current_heads = iter((operation_open, after_pass1, after_pass1, after_pass1))
    cycle_start_heads: list[str | None] = []
    address_thread_ids: list[str] = []
    settle_calls = 0

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (operation_open, None)

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
                threads=(
                    ReviewThread(
                        thread_id="T_later",
                        path="src/foo.py",
                        line=20,
                        body_excerpt="new feedback after earlier push",
                        author="reviewer",
                    ),
                )
            )
        return _clean_status()

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
        pr_head_sha=operation_open,
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
    assert cycle_start_heads[0] == operation_open
    # Bug today: pass 2 still gets operation_open. Fix: after_pass1.
    assert cycle_start_heads[1] == after_pass1
    assert cycle_start_heads[1] != operation_open


@pytest.mark.unit
async def test_fix_cycle_multi_item_pass_shares_stable_pass_start_cycle_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items in one settle pass share the pass snapshot while item heads advance.

    On pass 2 the operation-open SHA differs from the live pass HEAD. Both items
    must still receive the same ``cycle_start_head`` (pass snapshot), not the
    stale operation-open SHA and not each item's own advancing HEAD.
    """
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
    operation_open = "operation-open-head"
    pass2_anchor = "pass-two-start-head"
    after_first = "after-first-item-head"
    live_head = {"sha": operation_open}
    cycle_start_heads: list[str | None] = []
    operation_start_heads: list[str | None] = []
    address_thread_ids: list[str] = []
    settle_calls = 0

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (operation_open, None)

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
            live_head["sha"] = pass2_anchor
        elif thread.thread_id == "T_pass2_a":
            live_head["sha"] = after_first
        return "false_positive" if thread.thread_id == "T_pass1" else "needs_human"

    async def _settle(**_kwargs: object) -> PRStatus:
        nonlocal settle_calls
        settle_calls += 1
        if settle_calls == 1:
            return _clean_status(
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
                )
            )
        return _clean_status()

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
        pr_head_sha=operation_open,
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
    # Pass-2 items share one pass anchor (not operation-open, not per-item HEAD).
    assert cycle_start_heads[1:] == [pass2_anchor, pass2_anchor]
    assert operation_open not in cycle_start_heads[1:]
    assert operation_start_heads[1:] == [pass2_anchor, after_first]


@pytest.mark.unit
async def test_fix_cycle_fails_closed_when_pass_start_head_object_is_poisoned(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unverifiable pass-start HEAD must abort before addressing that pass."""
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # pass1 start cat-file
    cmd.queue_result(returncode=0)  # pass1 item cat-file
    cmd.queue_result(returncode=128, stderr="fatal: Not a valid object name poisoned")
    worktrees_root = tmp_path / "worktrees"
    worktree_path = worktrees_root / "ws_pass_poisoned"
    worktree_path.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
    )
    operation_open = "start"
    after_pass1 = "after-pass1"
    poisoned = "poisoned"
    # pass1 start, pass1 item, pass2 start (poisoned)
    current_heads = iter((operation_open, after_pass1, poisoned))
    cycle_start_heads: list[str | None] = []
    address_thread_ids: list[str] = []
    settle_calls = 0

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (operation_open, None)

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
                threads=(
                    ReviewThread(
                        thread_id="T_pass2",
                        path="src/foo.py",
                        line=4,
                        body_excerpt="settle feedback",
                        author="reviewer",
                    ),
                )
            )
        return _clean_status()

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
        workspace_id="ws_pass_poisoned",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=operation_open,
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
        remote_branch="awf/ws_pass_poisoned",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "HEAD_OBJECT_MISSING_UNRECOVERABLE"
    assert "commit object probe failed" in result.stderr
    # Pass 2 never addressed — no silent fallback to operation-open as cycle_start.
    assert address_thread_ids == ["T_pass1"]
    assert cycle_start_heads == [operation_open]


@pytest.mark.unit
async def test_later_pass_anchor_accepts_real_item_line_fix(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Line coords relative to a later pass head must map and accept a real FIXED.

    After an earlier pass inserts lines at the top, a settle-thread line is only
    valid against the newer pass head. Anchoring evidence at the operation-open
    SHA maps that line to failure (``item_line = -1``) and rejects a real fix.
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

    async def _fixed_agent(**_kwargs: object) -> AgentRunResult:
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

    # Stale operation-open anchor must fail-closed for this settle-thread line.
    with pytest.raises(AgentVerdictProtocolError) as stale:
        await comment_verdict._invoke_cli_for_verdict_result(
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
    assert stale.value.reason_code == AGENT_FIXED_WITHOUT_EVIDENCE


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
