"""Regression tests for operator remonitor hints — pause/drop-restart slice (part 5).

Split out of ``test_pr_monitor_operator_hints_part_002`` to keep that module under
the first-party line limit. These cases cover the reserved protected-block state
keys surviving an address-comments/operator-hint pause into ``blocked``, and the
directive-drop restart finalizing (or skipping the completed short-circuit) without
rerunning the agent CLI.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import OperationType
from awf.db.models import Operation
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    AddressComments,
    AddressOperatorHint,
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    OperatorHint,
    PRStatus,
    ReviewThread,
    decide,
)
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

REPO_URL = "git@github.com:dimileeh/aira-web.git"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _ready_status(
    *,
    head_sha: str = "abc1234567890def",
    checks: tuple[CheckTiming, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=checks,
    )


@pytest.mark.unit
def test_monitor_while_blocked_new_comment_not_dropped_on_resume() -> None:
    """Scope #3: the reserved protected-block state keys (preserved-head marker,
    epoch/content notification key) must NOT mark an untriaged comment addressed.
    A review comment that arrived during the block yields ``AddressComments`` once
    the operator hint has been processed on resume — it is not silently dropped."""
    new_thread = ReviewThread(
        thread_id="T_during_block",
        path="src/awf/x.py",
        line=1,
        body_excerpt="please tweak this",
        author="reviewer",
    )
    status = PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(new_thread,),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=(),
    )
    # State carrying the protected-block reserved keys, with the operator hint
    # already processed (cleared) — the resume's next decide() cycle.
    state = MonitorState(
        threads_addressed_ids={
            "__awf_protected_block_preserved_head__": "preserved-sha",
            "__awf_protected_block__:1:digestA": "notified",
        },
        pending_operator_hint=None,
    )

    action = decide(status, state, MonitorConfig(auto_merge=True))

    assert isinstance(action, AddressComments)
    assert new_thread in action.threads


@pytest.mark.unit
async def test_address_comments_paused_into_blocked_ends_monitor_without_failing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fix-cycle that pauses into ``blocked`` ends the monitor cycle cleanly:
    the loop returns True (stop) and records a ``protected_scope_paused`` outcome
    rather than terminally failing the workspace."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    thread = ReviewThread(
        thread_id="T_paused",
        path="src/awf/x.py",
        line=1,
        body_excerpt="tweak",
        author="reviewer",
    )
    state = MonitorState()

    async def _paused_fix_cycle(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            reason_code="PROTECTED_SCOPE_PAUSED_BLOCKED",
            paused_into_blocked=True,
        )

    async def _terminate_must_not_run(**_kwargs: object) -> None:
        pytest.fail("a paused workspace must NOT be terminally failed")

    monkeypatch.setattr(runner, "_run_fix_cycle", _paused_fix_cycle)
    monkeypatch.setattr(runner, "_terminate_failed", _terminate_must_not_run)

    handled = await runner._execute(
        action=AddressComments(threads=(thread,), review_comments=()),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert handled is True
    async with factory() as session:
        operation = (
            (
                await session.execute(
                    select(Operation).where(
                        Operation.workspace_id == workspace_id,
                        Operation.type == OperationType.comment_repair.value,
                    )
                )
            )
            .scalars()
            .one()
        )
    assert operation.result["outcome"] == "protected_scope_paused"


@pytest.mark.unit
async def test_operator_hint_paused_into_blocked_ends_monitor_without_failing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator-hint resume that re-pauses into ``blocked`` ends the monitor
    cycle cleanly with a ``protected_scope_paused`` outcome (not a failure)."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert",
        directive="revert it",
        operation_id="op_paused",
        requested_at="2026-06-16T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _paused_hint_cycle(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            reason_code="PROTECTED_SCOPE_PAUSED_BLOCKED",
            paused_into_blocked=True,
        )

    async def _terminate_must_not_run(**_kwargs: object) -> None:
        pytest.fail("a paused workspace must NOT be terminally failed")

    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _paused_hint_cycle)
    monkeypatch.setattr(runner, "_terminate_failed", _terminate_must_not_run)

    handled = await runner._execute(
        action=AddressOperatorHint(hint=hint),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert handled is True
    async with factory() as session:
        operation = (
            (
                await session.execute(
                    select(Operation).where(
                        Operation.workspace_id == workspace_id,
                        Operation.type == OperationType.comment_repair.value,
                    )
                )
            )
            .scalars()
            .one()
        )
    assert operation.result["outcome"] == "protected_scope_paused"


@pytest.mark.unit
async def test_operator_hint_directive_drop_restart_finalizes_without_rerunning_cli(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart-after-consume recovery for a directive-only resume that DROPPED the
    preserved commit (PRRT_kwDOSJAM6s6KUf46): the resume dropped the preserved
    offending commit, pushed a corrected head, then crashed BEFORE the processed
    marker persisted. On restart there is no grant (a directive-only resume never had
    one) and the dropped preserved SHA is intentionally NOT on the remote, so the
    preserved-on-remote shortcut cannot fire. Re-invoking the directive CLI against
    the already-resolved branch would create extra unreviewed commits or re-block the
    workspace. The monitor must instead recognize the corrected HEAD is already on the
    remote and finalize the bookkeeping WITHOUT re-running the directive CLI."""
    workspace_id = await seed_monitoring_workspace(factory)
    # No active grant: a directive-only revert/drop resume never carried one.
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="drop the protected edit and keep the rest",
        directive="revert the protected-file change, keep the remaining work",
        operation_id="op_directive_drop_restart",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "recorded-preserved-sha")

    calls: list[object] = []

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("recorded-preserved-sha", None)

    async def _cli_must_not_run(**_kwargs: object) -> object:
        pytest.fail("a directive drop restart must NOT re-invoke the CLI")

    async def _on_remote(**kwargs: object) -> bool:
        calls.append(kwargs.get("preserved_head_sha"))
        # The dropped preserved commit is NOT on the remote (the existing
        # preserved-on-remote shortcut is keyed on it), but the corrected LOCAL HEAD
        # IS — the directive-drop branch proves containment of HEAD itself, not just
        # an empty tree diff (PRRT_kwDOSJAM6s6KUx2T).
        return kwargs.get("preserved_head_sha") == "corrected-head-sha"

    async def _push_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("an already-pushed corrected head must NOT be re-pushed")

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "corrected-head-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _cli_must_not_run)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push_must_not_run)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is False
    assert result.failed is False
    # The existing shortcut probed with the recorded SHA (returned False -> dropped),
    # then the directive-drop branch probed with the LOCAL HEAD to confirm the
    # corrected head is contained in the remote (PRRT_kwDOSJAM6s6KUx2T).
    assert calls == ["recorded-preserved-sha", "corrected-head-sha"]
    assert state.last_push_sha == "corrected-head-sha"
    assert state.pending_operator_hint is None  # hint processed without the agent
    assert _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_operator_hint_remonitor_restart_shortcut_keeps_review_wait(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preserved-head shortcut skips the CLI, so remonitor reason text is not
    acted-on feedback and must not retire review-level ``needs_human`` rows."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="remonitor after addressing issue:4788370423 in the PR discussion",
        operation_id="op_remonitor_restart",
        requested_at="2026-06-25T00:00:00+00:00",
    )
    state = MonitorState(
        pending_operator_hint=hint,
        threads_addressed_ids={
            _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY: "preserved-sha",
            "issue:4788370423": "needs_human",
            "__review_comment_body_hash__:issue:4788370423": "body-hash",
            "__needs_human_reason__:issue:4788370423": "operator needs to answer",
        },
    )

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-sha", None)

    async def _already_on_remote(**_kwargs: object) -> bool:
        return True

    async def _cli_must_not_run(**_kwargs: object) -> object:
        pytest.fail("restart finalization must not re-invoke the CLI")

    async def _push_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("an already-pushed preserved commit must NOT be re-pushed")

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "preserved-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _already_on_remote)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _cli_must_not_run)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push_must_not_run)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is False
    assert result.failed is False
    assert state.threads_addressed_ids["issue:4788370423"] == "needs_human"
    assert "__needs_human_reason__:issue:4788370423" in state.threads_addressed_ids
    assert state.pending_operator_hint is None


@pytest.mark.unit
async def test_operator_hint_directive_drop_restart_skips_shortcut_for_revert_on_top_marker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The directive-drop restart shortcut must NOT fire on a revert-on-top marker
    (PRRT_kwDOSJAM6s6KUx2T). A prior directive that reverted the protected edit ON TOP
    and re-blocked resets the preserved marker to that revert-on-top commit, whose TREE
    matches the remote PR branch even though the commit was never pushed and the
    ungranted protected commit still sits below it in local history. A tree-only probe
    (``preserved_head_sha=None``) would see an empty diff and finalize — clearing the
    leak marker and letting a later repair push publish the ungranted history. The
    branch must instead probe with the LOCAL HEAD so the ancestry check refuses the
    shortcut when HEAD is not contained in the remote; the cycle then falls through to
    the directive CLI behind the leak guard with the marker RETAINED."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert the protected edit",
        directive="revert the protected-file change again",
        operation_id="op_directive_revert_on_top_restart",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    # The marker was reset to the revert-on-top commit by the leak re-block; the
    # worktree HEAD is that same commit.
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "revert-on-top-sha")

    probes: list[object] = []
    cli_ran = False

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("revert-on-top-sha", None)

    async def _on_remote(**kwargs: object) -> bool:
        probes.append(kwargs.get("preserved_head_sha"))
        # The revert-on-top commit's TREE matches the remote (a tree-only probe with
        # ``None`` would report already-on-remote — the OLD bug), but the commit
        # itself is NOT contained in the remote, so any SHA-keyed probe is False.
        return kwargs.get("preserved_head_sha") is None

    async def _needs_human_verdict(**_kwargs: object) -> VerdictResult:
        nonlocal cli_ran
        cli_ran = True
        return VerdictResult(verdict="needs_human", reason="still reverting on top")

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "revert-on-top-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _on_remote)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _needs_human_verdict)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is False
    assert result.failed is False
    # The directive-drop branch probed with the LOCAL HEAD, never the bug-triggering
    # ``None``; the first (marker-keyed) shortcut probed with the marker first.
    assert probes == ["revert-on-top-sha", "revert-on-top-sha"]
    assert None not in probes
    # The shortcut was refused, so the directive CLI ran behind the leak guard.
    assert cli_ran is True
    # The preserved marker is RETAINED — nothing was finalized — so the ungranted
    # history cannot later leak through a marker-less repair push.
    assert (
        state.threads_addressed_ids.get(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY)
        == "revert-on-top-sha"
    )


@pytest.mark.unit
async def test_operator_hint_directive_drop_restart_skips_shortcut_when_head_missing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The directive-drop restart shortcut must NOT finalize when the worktree HEAD
    cannot be resolved: with no local HEAD SHA the monitor cannot prove the corrected
    head is contained in the remote, so taking the shortcut would risk clearing the
    leak marker over unverified local history (PRRT_kwDOSJAM6s6KUx2T). Instead the
    cycle falls through to run the directive CLI behind the leak guard; the preserved
    marker is RETAINED so divergence context survives for the corrected resume."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="drop the protected edit and keep the rest",
        directive="revert the protected-file change, keep the remaining work",
        operation_id="op_directive_drop_restart_no_head",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.last_push_sha = "prior-push-sha"
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "recorded-preserved-sha")

    cli_ran = False

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("recorded-preserved-sha", None)

    async def _needs_human_verdict(**_kwargs: object) -> VerdictResult:
        nonlocal cli_ran
        cli_ran = True
        return VerdictResult(verdict="needs_human", reason="cannot prove the drop landed")

    async def _not_on_remote(**_kwargs: object) -> bool:
        # The first (marker-keyed) shortcut must not fire; the directive-drop branch
        # is skipped before it can probe because the local HEAD is unresolvable.
        return False

    async def _no_head(*_args: object, **_kwargs: object) -> str | None:
        return None

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _needs_human_verdict)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_rev_parse_head", _no_head)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is False
    assert result.failed is False
    # The shortcut was skipped (HEAD unresolvable), so the directive CLI ran.
    assert cli_ran is True
    assert state.last_push_sha == "prior-push-sha"  # untouched: nothing was finalized
    # The preserved marker is RETAINED — nothing was finalized — so a corrected
    # resume keeps the divergence context.
    assert (
        state.threads_addressed_ids.get(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY)
        == "recorded-preserved-sha"
    )
