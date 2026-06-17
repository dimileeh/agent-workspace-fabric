"""Regression tests for operator remonitor hints — directive leak slice (part 3).

Split out of ``test_pr_monitor_operator_hints_part_002`` to keep that module under
the first-party line limit. These cases cover the directive-resume guard that
refuses to publish the preserved ungranted protected commit when a directive
resolves the block by reverting ON TOP of it rather than dropping it
(PRRT_kwDOSJAM6s6KFytV / comment 4512006075).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    MonitorState,
    OperatorHint,
)
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError
from tests.postgres import postgres_test_engine
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


async def _seed_grant_and_block_violations(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    grant_path: str,
    violation_paths: tuple[str, ...],
) -> str:
    """Seed an active operator grant and record the block's protected violations.

    Mirrors the state a combined ``--directive ... --grant ...`` guide leaves: the
    grant covers ``grant_path`` and ``block_violations`` lists the protected paths
    that caused the block (i.e. the preserved commit's protected changes)."""
    from awf.common.ids import new_operator_grant_id
    from awf.db.models import OperatorGrantAuditRecord
    from awf.db.repositories import WorkspaceRepository

    grant_id = new_operator_grant_id()
    async with factory() as session:
        session.add(
            OperatorGrantAuditRecord(
                id=grant_id,
                workspace_id=workspace_id,
                operator="op@example.com",
                reason="approved keeping the protected change",
                normalized_path=grant_path,
                block_epoch=0,
                approve_policy_downgrade=True,
            )
        )
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.block_violations = [{"path": path} for path in violation_paths]
        await session.commit()
    return grant_id


@pytest.mark.unit
async def test_operator_hint_directive_with_covering_grant_keeps_preserved_commit_pushes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A combined ``--directive ... --grant ...`` resume that intentionally KEEPS the
    preserved protected edit (the grant covers every block-violation path) while the
    directive fixes other files must NOT be wedged at needs_human. The same-request
    grant authorizes publishing the preserved commit, so the leak guard short-circuits
    BEFORE the range check and the validated push proceeds (PRRT_kwDOSJAM6s6KG1hs)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_grant_and_block_violations(
        factory,
        workspace_id,
        grant_path="pyproject.toml",
        violation_paths=("pyproject.toml",),
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="keep pyproject.toml and fix the rest",
        directive="redo the unrelated files but keep the protected edit",
        operation_id="op_directive_plus_grant",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-granted-sha")
    push_calls: list[dict[str, object]] = []

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-granted-sha", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _range_must_not_run(**_kwargs: object) -> bool:
        pytest.fail("the leak guard must short-circuit on a covering grant")

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _validated_push(**kwargs: object) -> _GitPushResult:
        push_calls.append(kwargs)
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "pushed-granted-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_in_unpushed_range", _range_must_not_run)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated_push)
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

    assert result.pushed is True
    assert result.failed is False
    assert push_calls, "the granted keep-the-preserved-edit directive push must run"
    assert state.pending_operator_hint is None


@pytest.mark.unit
async def test_operator_hint_directive_with_uncovering_grant_still_leaks_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directive resume whose active grant covers a DIFFERENT path than the block's
    protected violation still leaves the preserved protected change ungranted. The
    leak guard must keep firing — a partial/mismatched grant cannot authorize
    publishing the ungranted preserved commit (PRRT_kwDOSJAM6s6KG1hs guards against
    over-honoring grants)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_grant_and_block_violations(
        factory,
        workspace_id,
        grant_path="setup.cfg",
        violation_paths=("pyproject.toml",),
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert pyproject.toml",
        directive="revert it",
        operation_id="op_directive_uncovering_grant",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-ungranted-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-ungranted-sha", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _preserved_in_range(**_kwargs: object) -> bool:
        return True

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _validated_push_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("a partial-grant leak must be refused before push")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_in_unpushed_range", _preserved_in_range)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated_push_must_not_run)

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
    # Operator-actionable, NOT terminal: a ``failed``/terminal result would
    # ``_terminate_failed`` the workspace and strand the needs_human hint
    # (PRRT_kwDOSJAM6s6KHEEU). Refuse the leak WITHOUT failing the workspace.
    assert result.failed is False
    assert result.terminal_monitor_failure is False
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    assert "preserved-ungranted-sha" in (state.pending_operator_hint.status_reason or "")


@pytest.mark.unit
async def test_operator_hint_directive_revert_on_top_leaks_preserved_commit_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DIRECTIVE that resolves a protected block by adding a revert commit ON TOP
    of the preserved offending commit leaves the net tree matching the remote PR
    branch, so ``_protected_scope_push_block`` is None. Pushing HEAD would still
    fast-forward the remote branch over the ungranted protected-file commit plus its
    revert. The directive guard detects the preserved commit is still in the pushed
    range and surfaces needs_human WITHOUT pushing (comment 4512006075)."""
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
        directive="revert it",
        operation_id="op_directive_revert_on_top",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-offending-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-offending-sha", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _preserved_in_range(**_kwargs: object) -> bool:
        return True

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _validated_push_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("leaking directive push must be refused before validation/push")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_in_unpushed_range", _preserved_in_range)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated_push_must_not_run)

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
    # Refuse the leak WITHOUT terminally failing: the guard surfaces needs_human
    # and the loop must park the workspace at ``monitoring_pr`` awaiting the
    # operator, not ``_terminate_failed`` it (PRRT_kwDOSJAM6s6KHEEU).
    assert result.failed is False
    assert result.terminal_monitor_failure is False
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    assert "preserved-offending-sha" in (state.pending_operator_hint.status_reason or "")
    # The preserved marker is retained so a corrected resume keeps the divergence
    # context; it is only dropped once the resume is finalized.
    assert (
        state.threads_addressed_ids.get(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY)
        == "preserved-offending-sha"
    )


@pytest.mark.unit
async def test_operator_hint_directive_revert_dropped_preserved_commit_pushes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the directive dropped the preserved commit (reset away), the guard does
    not fire and the validated push proceeds: the legitimate revert path is not
    wedged by the leak guard."""
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
        directive="revert it",
        operation_id="op_directive_revert_dropped",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "dropped-preserved-sha")
    push_calls: list[dict[str, object]] = []

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("redone-head-sha", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _preserved_not_in_range(**_kwargs: object) -> bool:
        return False

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _validated_push(**kwargs: object) -> _GitPushResult:
        push_calls.append(kwargs)
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "redone-head-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_in_unpushed_range", _preserved_not_in_range)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated_push)
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

    assert result.pushed is True
    assert result.failed is False
    assert push_calls, "the legitimate dropped-commit directive push must run"
    assert state.pending_operator_hint is None


@pytest.mark.unit
async def test_preserved_commit_in_unpushed_range_missing_inputs_returns_false(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """No preserved SHA, or a missing worktree, cannot leak: return False without
    touching git."""
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktrees" / "ws_present"
    worktree.mkdir(parents=True)

    assert (
        await runner._preserved_commit_in_unpushed_range(
            workspace_id="ws_present",
            worktree_path=worktree,
            remote_branch="awf/ws_present",
            preserved_head_sha=None,
        )
        is False
    )
    assert (
        await runner._preserved_commit_in_unpushed_range(
            workspace_id="ws_missing",
            worktree_path=tmp_path / "worktrees" / "ws_missing",
            remote_branch="awf/ws_missing",
            preserved_head_sha="preserved-sha",
        )
        is False
    )


@pytest.mark.unit
async def test_preserved_commit_in_unpushed_range_reset_away_returns_false(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A preserved commit no longer reachable from HEAD (the directive reset it
    away) is not in the pushed range — no fetch is needed."""
    worktree = tmp_path / "worktrees" / "ws_reset"
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    # merge-base --is-ancestor <preserved> HEAD exits non-zero: not an ancestor.
    cmd.queue_result(returncode=1)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert (
        await runner._preserved_commit_in_unpushed_range(
            workspace_id="ws_reset",
            worktree_path=worktree,
            remote_branch="awf/ws_reset",
            preserved_head_sha="preserved-sha",
        )
        is False
    )
    ancestry_calls = [c for c in cmd.calls if "--is-ancestor" in c.args]
    assert len(ancestry_calls) == 1
    assert "preserved-sha" in ancestry_calls[0].args
    assert "HEAD" in ancestry_calls[0].args


@pytest.mark.unit
async def test_preserved_commit_in_unpushed_range_diff_error_fails_closed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the preserved commit is confirmed an ancestor of HEAD, a containment
    fetch failure cannot prove it is already published, so fail closed (True) to
    surface needs_human rather than leak the ungranted protected commit."""
    worktree = tmp_path / "worktrees" / "ws_err"
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # ancestor of HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _diff(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        raise ProtectedScopeDiffError("fetch failed")

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _diff)

    assert (
        await runner._preserved_commit_in_unpushed_range(
            workspace_id="ws_err",
            worktree_path=worktree,
            remote_branch="awf/ws_err",
            preserved_head_sha="preserved-sha",
        )
        is True
    )
    # Failing the fetch must not reach the FETCH_HEAD containment merge-base: only
    # the ancestry check ran before fail-closed.
    ancestry_calls = [c for c in cmd.calls if "--is-ancestor" in c.args]
    assert len(ancestry_calls) == 1
    assert "HEAD" in ancestry_calls[0].args


@pytest.mark.unit
@pytest.mark.parametrize(
    ("on_remote_returncode", "expected"),
    [
        (1, True),  # preserved NOT on remote: still in the unpushed range -> leak
        (0, False),  # preserved already on remote: cannot be un-leaked
    ],
)
async def test_preserved_commit_in_unpushed_range_on_remote_decides_outcome(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    on_remote_returncode: int,
    expected: bool,
) -> None:
    """An ancestor-of-HEAD preserved commit leaks only when it is not already
    contained in the freshly-fetched remote PR head."""
    worktree = tmp_path / "worktrees" / "ws_range"
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # merge-base --is-ancestor <preserved> HEAD: ancestor
    cmd.queue_result(returncode=on_remote_returncode)  # ... <preserved> FETCH_HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _diff(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("base-sha", ())

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _diff)

    assert (
        await runner._preserved_commit_in_unpushed_range(
            workspace_id="ws_range",
            worktree_path=worktree,
            remote_branch="awf/ws_range",
            preserved_head_sha="preserved-sha",
        )
        is expected
    )
    ancestry_calls = [c for c in cmd.calls if "--is-ancestor" in c.args]
    assert len(ancestry_calls) == 2
    assert "HEAD" in ancestry_calls[0].args
    assert "FETCH_HEAD" in ancestry_calls[1].args


@pytest.mark.unit
async def test_preserved_protected_change_fully_granted_edge_cases(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The grant-coverage helper fails closed: no grants, a vanished workspace, or a
    block with no recorded violation paths all return False so the leak guard keeps
    firing rather than over-honoring a grant (PRRT_kwDOSJAM6s6KG1hs)."""
    from awf.control.quality_gates import GrantSpec

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    grant = GrantSpec(path="pyproject.toml", approve_policy_downgrade=True)

    # No grants at all -> ungranted.
    assert (
        await runner._preserved_protected_change_fully_granted(
            workspace_id="ws_missing", grant_specs=[]
        )
        is False
    )
    # Grant present but the workspace row is gone -> cannot confirm coverage.
    assert (
        await runner._preserved_protected_change_fully_granted(
            workspace_id="ws_missing", grant_specs=[grant]
        )
        is False
    )
    # Grant present but the block recorded no violation paths -> nothing to confirm
    # the grant authorizes keeping, so fail closed.
    workspace_id = await seed_monitoring_workspace(factory)
    assert (
        await runner._preserved_protected_change_fully_granted(
            workspace_id=workspace_id, grant_specs=[grant]
        )
        is False
    )
